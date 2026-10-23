# -*- coding: utf-8 -*-
"""
optimize.py —— 参数优化（严格走样本外验证，允许得出"不该改"的结论）

方法论与防自欺设计：

1. 目标函数不是"回测收益最大化"（在 550 根样本上必然过拟合），
   而是可证伪的预测力指标：
   - AUC(bull_score, fwd20 > 0)：状态概率能否区分涨跌
   - Brier(bull_score, fwd20 > 0)：概率是否校准
   - 主状态方向命中率

2. 走前向验证（walk-forward）+ 禁运期（embargo），防标签泄漏。

3. 只调 4 个高杠杆参数，不碰 30 个 lo/hi/w（维度即过拟合）。

4. 判决规则预先写死：OOS AUC 提升 >= +0.02 且 Brier 不恶化，否则明确输出"不建议修改"。

性能设计：特征与参数无关，只算一次缓存；参数只影响 state_probs。

用法:
  python optimize.py            # 评估 + 出建议
  python optimize.py --apply    # 采纳并写回配置
"""
from __future__ import annotations

import io
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "..", "stock-pool", "_cache")
CFG_PATH = os.path.join(BASE, "splus_config.yaml")
OUT = os.path.join(BASE, "data", "optimize_result.json")

HORIZON = 20
MIN_HIST = 150
STEP = 5
EMBARGO = HORIZON

GRID = {
    "persistence": [0.55, 0.65, 0.75],
    "temperature": [0.65, 0.85, 1.05],
    "bull_scale": [0.85, 1.00, 1.15],
    "bear_scale": [0.85, 1.00, 1.15],
}

MIN_AUC_GAIN = 0.02
MAX_BRIER_WORSE = 0.005


# ---------------------------------------------------------------- 指标

def auc(y_true, y_score):
    pairs = sorted(zip(y_score, y_true))
    n1 = sum(1 for _, t in pairs if t)
    n0 = len(pairs) - n1
    if n1 == 0 or n0 == 0:
        return None
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    r1 = sum(r for r, (_, t) in zip(ranks, pairs) if t)
    return (r1 - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def brier(prob, outcome):
    if not prob:
        return None
    return sum((p - o) ** 2 for p, o in zip(prob, outcome)) / len(prob)


# ---------------------------------------------------------------- 特征缓存

def build_feature_cache(bars_by_code):
    """
    每个 (code, 日期) 只算一次特征 —— 与参数无关，可复用。
    返回按 code 分组的、按时间排序的窗口列表。
    """
    cache = {}
    for code, bars in bars_by_code.items():
        if len(bars) < MIN_HIST + HORIZON + 10:
            continue
        rows = []
        for i in range(MIN_HIST, len(bars) - HORIZON, STEP):
            try:
                f = engine.compute_features(bars[:i + 1])
            except Exception:
                continue
            c0 = bars[i]["close"]
            c1 = bars[i + HORIZON]["close"]
            rows.append({
                "date": bars[i]["date"],
                "f": f,
                "fwd": c1 / c0 - 1.0,
            })
        if rows:
            cache[code] = rows
    return cache


def build_model(cfg, params):
    model = json.loads(json.dumps(cfg["state"]["scoring"]))
    for st in ("B", "C"):
        for item in (model[st].get("core") or []):
            item["w"] = float(item.get("w", 1.0)) * params["bull_scale"]
    for item in (model["E"].get("core") or []):
        item["w"] = float(item.get("w", 1.0)) * params["bear_scale"]
    return model


def run_params(feature_cache, cfg, params):
    """对每组参数，按 code 逐窗口递推概率（先验链条不能跨标的）。"""
    model = build_model(cfg, params)
    u = float(cfg["state"].get("uniform_prior", 0.2))
    out = {}
    for code, rows in feature_cache.items():
        prev = None
        seq = []
        for r in rows:
            sp = engine.state_probs(r["f"], prev, params["persistence"],
                                    params["temperature"], model, u)
            prev = sp["probs"]
            p = sp["probs"]
            seq.append({
                "date": r["date"],
                "bull": p.get("B", 0) + p.get("C", 0),
                "fwd": r["fwd"],
                "dom": sp["dominant"],
            })
        out[code] = seq
    return out


# ---------------------------------------------------------------- 评估

def score_split(samples):
    if len(samples) < 40:
        return None
    y = [1 if s["fwd"] > 0 else 0 for s in samples]
    sc = [s["bull"] for s in samples]
    a = auc(y, sc)
    b = brier(sc, y)
    hit = tot = 0
    for s in samples:
        if s["dom"] in ("B", "C"):
            tot += 1
            hit += 1 if s["fwd"] > 0 else 0
        elif s["dom"] == "E":
            tot += 1
            hit += 1 if s["fwd"] < 0 else 0
    return {
        "n": len(samples),
        "auc": None if a is None else round(a, 4),
        "brier": None if b is None else round(b, 4),
        "hit_rate": round(hit / tot, 4) if tot else None,
        "n_directional": tot,
        "n_up": sum(y),
    }


def build_splits(feature_cache):
    """按日期切折（所有标的日子合并后统一切）。"""
    all_dates = sorted({r["date"] for rows in feature_cache.values() for r in rows})
    n = len(all_dates)
    if n < 60:
        return None
    folds = 3
    bounds = [int(n * (i + 1) / (folds + 1)) for i in range(folds)]
    splits = []
    for fi in range(folds):
        cut = bounds[fi]
        test_end = bounds[fi + 1] if fi + 1 < folds else n
        tr_dates = set(all_dates[:max(0, cut - EMBARGO)])
        te_dates = set(all_dates[cut:test_end])
        if len(tr_dates) < 40 or len(te_dates) < 30:
            continue
        splits.append((tr_dates, te_dates, fi))
    return splits


def evaluate(seq_by_code, splits):
    is_aucs, oos_aucs, oos_briers, detail = [], [], [], []
    for tr_dates, te_dates, fi in splits:
        tr, te = [], []
        for code, seq in seq_by_code.items():
            for s in seq:
                if s["date"] in tr_dates:
                    tr.append(s)
                elif s["date"] in te_dates:
                    te.append(s)
        a_is, a_oos = score_split(tr), score_split(te)
        if not a_is or not a_oos:
            continue
        if a_is["auc"] is not None:
            is_aucs.append(a_is["auc"])
        if a_oos["auc"] is not None:
            oos_aucs.append(a_oos["auc"])
        if a_oos["brier"] is not None:
            oos_briers.append(a_oos["brier"])
        detail.append({"fold": fi, "is": a_is, "oos": a_oos})
    if not oos_aucs:
        return None
    return {
        "is_auc": round(sum(is_aucs) / len(is_aucs), 4) if is_aucs else None,
        "oos_auc": round(sum(oos_aucs) / len(oos_aucs), 4),
        "oos_brier": round(sum(oos_briers) / len(oos_briers), 4) if oos_briers else None,
        "folds": detail,
    }


def main():
    apply = "--apply" in sys.argv
    cfg = engine.load_config(CFG_PATH)
    codes = [u["code"] for u in cfg["universe"]]
    bars_by_code = {}
    for c in codes:
        b = engine.load_bars(CACHE, c)
        if len(b) >= MIN_HIST + HORIZON + 10:
            bars_by_code[c] = b

    print("=" * 74)
    print("参数优化（走前向验证，允许结论为「不该改」）")
    print("=" * 74)
    print(f"  标的 {len(bars_by_code)} 只；前瞻 {HORIZON} 日；步长 {STEP}；禁运 {EMBARGO} 日")

    print("  构建特征缓存（与参数无关，只算一次）…")
    fc = build_feature_cache(bars_by_code)
    n_win = sum(len(v) for v in fc.values())
    print(f"  窗口数 {n_win}")

    splits = build_splits(fc)
    if not splits:
        print("  样本不足，无法前向验证 —— 不做任何修改")
        return 1
    print(f"  折数 {len(splits)}")

    base = {"persistence": float(cfg["state"]["persistence"]),
            "temperature": float(cfg["state"]["temperature"]),
            "bull_scale": 1.0, "bear_scale": 1.0}
    base_res = evaluate(run_params(fc, cfg, base), splits)
    print(f"\n[基线] persistence={base['persistence']} temperature={base['temperature']}")
    print(f"  IS AUC {base_res['is_auc']} ｜ OOS AUC {base_res['oos_auc']} ｜ "
          f"OOS Brier {base_res['oos_brier']}")

    keys = list(GRID.keys())
    total = 1
    for k in keys:
        total *= len(GRID[k])
    print(f"\n搜索 {total} 组参数…")

    rows, best = [], None
    for combo in itertools.product(*[GRID[k] for k in keys]):
        p = dict(zip(keys, combo))
        r = evaluate(run_params(fc, cfg, p), splits)
        if not r:
            continue
        rows.append((p, r))
        if best is None or (r["oos_auc"] or -1) > (best[1]["oos_auc"] or -1):
            best = (p, r)

    rows.sort(key=lambda x: -(x[1]["oos_auc"] or -1))
    print(f"\n样本外 AUC 前 10 组：")
    hdr = f"  {'persist':>8}{'temp':>7}{'bull':>7}{'bear':>7}{'IS_AUC':>9}{'OOS_AUC':>9}{'OOS_Bri':>9}"
    print(hdr)
    for p, r in rows[:10]:
        print(f"  {p['persistence']:>8.2f}{p['temperature']:>7.2f}"
              f"{p['bull_scale']:>7.2f}{p['bear_scale']:>7.2f}"
              f"{(r['is_auc'] or 0):>9.4f}{r['oos_auc']:>9.4f}"
              f"{(r['oos_brier'] or 0):>9.4f}")

    bp, br = best
    gain = br["oos_auc"] - base_res["oos_auc"]
    brier_ok = (base_res["oos_brier"] is None or br["oos_brier"] is None
                or br["oos_brier"] <= base_res["oos_brier"] + MAX_BRIER_WORSE)
    print("\n" + "=" * 74)
    print("判决")
    print("=" * 74)
    print(f"  最优组：{bp}")
    print(f"  OOS AUC {base_res['oos_auc']} → {br['oos_auc']}（{gain:+.4f}，门槛 +{MIN_AUC_GAIN}）")
    print(f"  OOS Brier {base_res['oos_brier']} → {br['oos_brier']}（容差 +{MAX_BRIER_WORSE}）")
    print(f"  过拟合检查：IS {br['is_auc']} vs OOS {br['oos_auc']}"
          f"（差 {(br['is_auc'] or 0) - br['oos_auc']:+.4f}）")

    accept = (gain >= MIN_AUC_GAIN) and brier_ok
    if accept:
        print("\n  结论：满足门槛，建议采纳")
        if apply:
            _apply(bp)
            print(f"  已写回 {CFG_PATH}")
        else:
            print("  （加 --apply 才写回）")
    else:
        print("\n  结论：**不满足门槛，不建议修改配置**")
        print("  在 550 根样本上追求更高回测指标，得到的是过拟合而非能力提升。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"baseline": base_res,
                   "best": {"params": bp, "result": br},
                   "accept": accept, "gain": round(gain, 4),
                   "grid_size": total, "n_windows": n_win,
                   "top": [{"params": p, "result": r} for p, r in rows[:12]]},
                  fh, ensure_ascii=False, indent=2)
    print(f"\n  明细已写入 {OUT}")
    return 0


def _apply(p):
    import re
    s = io.open(CFG_PATH, encoding="utf-8").read()
    s = re.sub(r"(\n\s*persistence:\s*)[\d.]+",
               lambda m: m.group(1) + str(p["persistence"]), s, count=1)
    s = re.sub(r"(\n\s*temperature:\s*)[\d.]+",
               lambda m: m.group(1) + str(p["temperature"]), s, count=1)
    io.open(CFG_PATH, "w", encoding="utf-8").write(s)


if __name__ == "__main__":
    sys.exit(main())
