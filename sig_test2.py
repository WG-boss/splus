# -*- coding: utf-8 -*-
"""
sig_test2.py —— 修正版显著性检验

修正 sig_test.py 的三个方法论缺陷：

缺陷1：bootstrap 在"同一标的内"做 i.i.d. 重采样。
       但 STEP=5、HORIZON=20 意味着相邻样本共享 15 天前瞻收益，
       时间上强自相关。i.i.d. 重采样把这种相关性洗掉了 → 方差被低估 → p 虚低。
       修正：按**时间块**重采样（块长 = HORIZON/STEP = 4），保住重叠结构。

缺陷2：口径不一致。
       optimize.py 对"每折 AUC"取平均；本脚本对"合并样本"AUC。
       两者在折间样本量不等时不等价。这里统一用**合并样本 + 逐折并列报告**。

缺陷3：只看 AUC，没看方向命中率。
       fold2 基线命中率 0.4348 < 0.5（方向反了）。
       一个 AUC 略高但方向命中率 <0.5 的模型没有交易价值。
"""
import io
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import optimize as O

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "..", "stock-pool", "_cache")
CFG_PATH = os.path.join(BASE, "splus_config.yaml")

BOOT = 2000
BLOCK = O.HORIZON // O.STEP      # 时间块长度 = 4
random.seed(20261023)


def collect(seq_by_code, splits):
    """收集 OOS 样本，按日期排序，返回对齐列表。"""
    rows = []
    for tr, te, fi in splits:
        for code, seq in seq_by_code.items():
            for s in seq:
                if s["date"] in te:
                    rows.append({"code": code, "date": s["date"],
                                 "bull": s["bull"], "fwd": s["fwd"],
                                 "dom": s["dom"], "fold": fi})
    rows.sort(key=lambda r: (r["date"], r["code"]))
    return rows


def auc_rows(rows):
    y = [1 if r["fwd"] > 0 else 0 for r in rows]
    sc = [r["bull"] for r in rows]
    return O.auc(y, sc)


def hit_rows(rows):
    """方向命中率：只统计有方向的 B/C/E。"""
    hit = tot = 0
    for r in rows:
        if r["dom"] in ("B", "C"):
            tot += 1
            hit += 1 if r["fwd"] > 0 else 0
        elif r["dom"] == "E":
            tot += 1
            hit += 1 if r["fwd"] < 0 else 0
    return (hit / tot if tot else None), tot


def block_bootstrap(rows_a, rows_b, n=BOOT):
    """
    移动块 bootstrap：以日期块为单元重采样。
    块长 BLOCK，保住重叠前瞻收益的相关结构。
    """
    assert len(rows_a) == len(rows_b)
    # 按日期分组索引
    by_date = {}
    for i, r in enumerate(rows_a):
        by_date.setdefault(r["date"], []).append(i)
    dates = sorted(by_date.keys())
    nd = len(dates)
    if nd < BLOCK * 2:
        return None

    n_blocks = max(1, nd // BLOCK)
    max_start = nd - BLOCK
    diffs = []
    for _ in range(n):
        idx = []
        # moving block bootstrap：每个块独立随机起点，有放回
        for b in range(n_blocks):
            s = random.randrange(0, max_start + 1)
            for d in dates[s:s + BLOCK]:
                idx.extend(by_date[d])
        ar = [rows_a[i] for i in idx]
        br = [rows_b[i] for i in idx]
        aa, ab = auc_rows(ar), auc_rows(br)
        if aa is None or ab is None:
            continue
        diffs.append(aa - ab)
    if not diffs:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * (len(diffs) - 1))]
    hi = diffs[int(0.975 * (len(diffs) - 1))]
    mean = sum(diffs) / len(diffs)
    p_left = sum(1 for d in diffs if d <= 0) / len(diffs)
    p = min(p_left, 1 - p_left) * 2
    return {"mean": round(mean, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "p_two_sided": round(p, 4), "n_boot": len(diffs),
            "block": BLOCK, "n_dates": nd}


def main():
    cfg = engine.load_config(CFG_PATH)
    codes = [u["code"] for u in cfg["universe"]]
    bars = {}
    for c in codes:
        b = engine.load_bars(CACHE, c)
        if len(b) >= O.MIN_HIST + O.HORIZON + 10:
            bars[c] = b

    print("=" * 76)
    print("修正版显著性检验（时间块 bootstrap + 方向命中率）")
    print("=" * 76)
    fc = O.build_feature_cache(bars)
    splits = O.build_splits(fc)

    res = json.load(io.open(os.path.join(BASE, "data", "optimize_result.json"),
                            encoding="utf-8"))
    best = res["best"]["params"]
    base = {"persistence": float(cfg["state"]["persistence"]),
            "temperature": float(cfg["state"]["temperature"]),
            "bull_scale": 1.0, "bear_scale": 1.0}

    a = collect(O.run_params(fc, cfg, best), splits)
    b = collect(O.run_params(fc, cfg, base), splits)

    eff_n = len(a) // BLOCK
    print(f"  名义 OOS 样本 {len(a)} 条")
    print(f"  时间块长 {BLOCK}（= HORIZON/STEP）→ 有效独立样本 ≈ {eff_n} 条")
    print(f"  折数 {len(splits)}")

    auc_a, auc_b = auc_rows(a), auc_rows(b)
    hit_a, n_a = hit_rows(a)
    hit_b, n_b = hit_rows(b)

    print(f"\n  {'':14}{'AUC':>9}{'方向命中':>10}{'有向样本':>10}")
    print(f"  {'基线':14}{auc_b:>9.4f}{(hit_b or 0):>10.4f}{n_b:>10d}")
    print(f"  {'最优组':14}{auc_a:>9.4f}{(hit_a or 0):>10.4f}{n_a:>10d}")
    print(f"  {'差值':14}{auc_a - auc_b:>+9.4f}{(hit_a or 0) - (hit_b or 0):>+10.4f}")

    print(f"\n  逐折明细：")
    for fi in sorted({r["fold"] for r in a}):
        ra = [r for r in a if r["fold"] == fi]
        rb = [r for r in b if r["fold"] == fi]
        ha, _ = hit_rows(ra)
        hb, _ = hit_rows(rb)
        print(f"    fold{fi}: n={len(ra):3d}  AUC {auc_rows(rb):.4f}→{auc_rows(ra):.4f} "
              f"({auc_rows(ra) - auc_rows(rb):+.4f})  "
              f"命中 {hb:.3f}→{ha:.3f}")

    print(f"\n  时间块 bootstrap {BOOT} 次（块长 {BLOCK}）…")
    r = block_bootstrap(a, b)
    if r is None:
        print("  日期块不足，无法 bootstrap")
        return 1
    print(f"  差值均值 {r['mean']:+.4f}")
    print(f"  95% CI [{r['ci95'][0]:+.4f}, {r['ci95'][1]:+.4f}]")
    print(f"  双侧 p = {r['p_two_sided']}")

    # ---------------- 判决（多条件，缺一不可）----------------
    print("\n" + "=" * 76)
    print("判决（四项门槛，缺一不可）")
    print("=" * 76)
    gain = auc_a - auc_b
    conds = []

    c1 = gain >= 0.02
    conds.append(("OOS AUC 提升 >= +0.02", f"{gain:+.4f}", c1))

    c2 = r["ci95"][0] > 0
    conds.append(("95% CI 下界 > 0（显著性）", f"[{r['ci95'][0]:+.4f}]", c2))

    c3 = r["p_two_sided"] < 0.05
    conds.append(("双侧 p < 0.05", f"{r['p_two_sided']}", c3))

    c4 = (hit_a is not None and hit_a > 0.5)
    conds.append(("方向命中率 > 0.50", f"{hit_a:.4f}" if hit_a else "—", c4))

    for name, val, ok in conds:
        print(f"  {'[PASS]' if ok else '[FAIL]'}  {name:34} {val}")

    n_pass = sum(1 for _, _, ok in conds if ok)
    print(f"\n  通过 {n_pass}/4")

    if n_pass == 4:
        verdict = "significant"
        print("\n  结论：四项全过，可以采纳")
    elif c2 and c3:
        verdict = "significant_but_below_threshold"
        print("\n  结论：**统计显著但提升幅度低于门槛**")
        print("  提升真实存在，但 +%.3f 的 AUC 改善不足以改变任何交易决策。" % gain)
        print("  建议：记录为观察项，不改配置。")
    else:
        verdict = "not_significant"
        print("\n  结论：**不显著，不应修改配置**")
        print("  名义样本 %d 条，但有效独立样本仅约 %d 条，" % (len(a), eff_n))
        print("  AUC 标准误约 ±0.08，观测到的提升落在噪声范围内。")

    out = {
        "auc_base": round(auc_b, 4), "auc_best": round(auc_a, 4),
        "diff": round(gain, 4),
        "hit_base": round(hit_b, 4) if hit_b else None,
        "hit_best": round(hit_a, 4) if hit_a else None,
        "n_directional": n_a,
        "bootstrap": r, "effective_n": eff_n, "nominal_n": len(a),
        "thresholds_passed": f"{n_pass}/4",
        "verdict": verdict,
        "params_evaluated": best,
    }
    with io.open(os.path.join(BASE, "data", "sig_test.json"), "w",
                 encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\n  明细写入 data/sig_test.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
