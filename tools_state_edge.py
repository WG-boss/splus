#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_state_edge.py —— 状态机实证检验（只读证据采集，不进生产链）

回答三个此前只有定性判断的问题：
  1) 五状态到底有没有前向预测力？——逐指数配对检验（避免重叠窗口虚增样本）
  2) 状态机的「说 yes」通道历史上多久开一次？——信号到达率
  3) 状态占用随时间怎么变？——月度状态占比时间线

方法：
  * 与生产引擎完全同链回放（compute_features → state_probs，参数读生产 config），
    warmup 120 日，杜绝前视偏差。
  * 配对检验：对每只指数分别计算「某状态日 fwd20 均值 − 该指数全期 fwd20 均值」，
    得到逐指数 edge；跨指数求均值与符号检验。重叠窗口只在【同一指数内部】存在，
    跨指数配对后不虚增独立样本量。
  * ⚠ 跨指数 t 值仍受横截面相关性污染（118 只 n_eff≈1.7），
    统计推断须看 tools_state_significance.py 的时间维度（HAC + block bootstrap）检验。
  * fwd20 用收盘价对收盘价（不含成本），仅供方向性判断。

面板（panel）：
  回放结果可落盘为 JSONL（--dump-panel），显著性/尾部/矩阵检验直接复用，
  不必每次重跑十几分钟回放。--from-panel 可完全用面板重算本工具输出，
  用于 CI 复现（不依赖行情缓存）。

用法：
  python tools_state_edge.py                                        # 全量回放并写 state/state_edge.json
  python tools_state_edge.py --limit 8                              # 冒烟
  python tools_state_edge.py --start 2020-11-06 --dump-panel state/state_panel.jsonl
  python tools_state_edge.py --from-panel state/state_panel.jsonl   # 从面板重算（秒级）
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "state", "state_edge.json")
WARMUP = 120
WIN = 320
STATES = ["A", "B", "C", "D", "E"]
FWD = 20


def replay(code: str, bars: List[Dict[str, Any]], st_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import engine
    prev = None
    out: List[Dict[str, Any]] = []
    for i in range(WARMUP, len(bars)):
        feats = engine.compute_features(bars[max(0, i - WIN + 1):i + 1])
        sp = engine.state_probs(
            feats, prev,
            float(st_cfg["persistence"]), float(st_cfg["temperature"]),
            st_cfg["scoring"], float(st_cfg.get("uniform_prior", 0.2)))
        prev = sp["probs"]
        out.append({"date": str(bars[i]["date"])[:10], "dom": sp["dominant"],
                    "conf": round(float(sp["confidence"]), 6),
                    "close": float(bars[i]["close"])})
    return out


def _p_sign(k: int, n: int) -> float:
    """双侧符号检验（H0: p=0.5），正态近似。"""
    if n <= 0:
        return 1.0
    z = (abs(k - n / 2) - 0.5) / math.sqrt(n * 0.25) if n > 0 else 0.0
    return math.erfc(abs(z) / math.sqrt(2))


# --------------------------------------------------------------- 面板读写
def dump_panel(records: Dict[str, List[Dict[str, Any]]], path: str) -> int:
    """落盘回放面板（JSONL）：一行一个 (标的, 交易日)。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for code, recs in records.items():
            for r in recs:
                row = {"c": code, "d": r["date"], "s": r["dom"],
                       "k": round(float(r["conf"]), 6)}
                if r.get("f20") is not None:
                    row["f"] = round(float(r["f20"]), 8)
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
    return n


def load_panel(path: str) -> Dict[str, List[Dict[str, Any]]]:
    """读面板。f20 已在回放阶段算好并落盘，故无需（也不可能有）close。"""
    recs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            recs[o["c"]].append({"date": o["d"], "dom": o["s"], "conf": float(o["k"]),
                                 "f20": (float(o["f"]) if "f" in o else None)})
    for c in recs:
        recs[c].sort(key=lambda x: x["date"])
    return dict(recs)


def attach_f20(recs: List[Dict[str, Any]], bars: List[Dict[str, Any]]) -> int:
    """按收盘价对收盘价补 f20（20 个交易日）。

    两种场景共用：① 现场回放后、落盘前；② 修复历史面板（面板缺 f 字段时）。
    口径必须与 aggregate 内一致：以【面板日期所在的 bar 位置】为起点，向后 20 根 bar。
    返回填充条数。
    """
    idx = {str(b["date"])[:10]: i for i, b in enumerate(bars)}
    n = 0
    for r in recs:
        i = idx.get(str(r["date"])[:10])
        if i is None or i + FWD >= len(bars):
            r["f20"] = None
            continue
        c0 = float(bars[i]["close"])
        r["f20"] = (float(bars[i + FWD]["close"]) / c0 - 1.0) if c0 > 0 else None
        if r["f20"] is not None:
            n += 1
    return n


# --------------------------------------------------------------- 聚合
def aggregate(records: Dict[str, List[Dict[str, Any]]], universe: List[Dict[str, Any]],
              st_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """从回放记录（含 f20）计算逐指数配对 edge / 年度稳健性 / 到达率 / 时间线。

    records 两种来源：① 现场回放（带 close，本函数补 f20）；
    ② 面板（无 close，f20 已在落盘时算好）。两条路径结果必须一致。
    """
    min_conf = float(st_cfg.get("min_confidence_to_act", 0.65))
    tradeable = {u["code"] for u in universe if u.get("tradeable")}
    name_of = {u["code"]: u.get("name", "") for u in universe}

    per_index: Dict[str, Dict[str, Any]] = {}
    timeline: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    arrival_days: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "hit": 0})
    durations: List[int] = []
    yearly: Dict[tuple, List[float]] = defaultdict(list)

    for code, recs in records.items():
        if not recs:
            continue
        if recs[0].get("close") is not None:
            closes = [r["close"] for r in recs]
            for j, r in enumerate(recs):
                r["f20"] = ((closes[j + FWD] / closes[j] - 1.0)
                            if j + FWD < len(closes) else None)
        base_f20 = [r["f20"] for r in recs if r.get("f20") is not None]
        if not base_f20:
            continue
        by_state: Dict[str, List[float]] = defaultdict(list)
        for r in recs:
            if r.get("f20") is not None:
                by_state[r["dom"]].append(r["f20"])
        base = statistics.mean(base_f20)
        per_index[code] = {
            "name": name_of.get(code, ""), "tradeable": code in tradeable,
            "n_days": len(base_f20), "base_f20": base,
            "state_mean": {s: (statistics.mean(v) if v else None) for s, v in by_state.items()},
            "state_n": {s: len(v) for s, v in by_state.items()},
            "state_edge": {s: (statistics.mean(v) - base if v else None) for s, v in by_state.items()},
        }
        # 分年度稳健性：先算该指数「该年度」的基线，再看状态超额。
        yr: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        for r in recs:
            if r.get("f20") is None:
                continue
            yr[r["date"][:4]][r["dom"]].append(r["f20"])
        for y, d in yr.items():
            ally = [x for v in d.values() for x in v]
            if len(ally) < 40:
                continue
            ybase = statistics.mean(ally)
            for s, v in d.items():
                if len(v) >= 10:
                    yearly[(y, s)].append(statistics.mean(v) - ybase)
        # 状态持续段
        run = 1
        for i in range(1, len(recs)):
            if recs[i]["dom"] == recs[i - 1]["dom"]:
                run += 1
            else:
                durations.append(run)
                run = 1
        # 月度占用时间线
        for r in recs:
            timeline[r["date"][:7]][r["dom"]] += 1
        # 信号到达率（仅可交易标的，与决策层同口径 A/B/C + conf>=阈值）
        if code in tradeable:
            for r in recs:
                d = arrival_days[r["date"]]
                d["n"] += 1
                if r["dom"] in ("A", "B", "C") and r["conf"] >= min_conf:
                    d["hit"] += 1

    summary = {}
    for s in STATES:
        edges = [v["state_edge"][s] for v in per_index.values()
                 if v["state_edge"].get(s) is not None]
        means = [v["state_mean"][s] for v in per_index.values()
                 if v["state_mean"].get(s) is not None]
        if not edges:
            continue
        m = statistics.mean(edges)
        sd = statistics.pstdev(edges) if len(edges) > 1 else 0.0
        t = m / (sd / math.sqrt(len(edges))) if sd > 0 else 0.0
        pos = sum(1 for e in edges if e > 0)
        summary[s] = {
            "n_index": len(edges),
            "edge_mean": round(m, 5),
            "edge_std": round(sd, 5),
            "t_stat": round(t, 2),
            "positive_share": round(pos / len(edges), 3),
            "sign_p": round(_p_sign(pos, len(edges)), 4),
            "abs_mean_f20": round(statistics.mean(means), 5) if means else None,
        }

    days = sorted(arrival_days)
    hit_days = sum(1 for d in days if arrival_days[d]["hit"] > 0)
    monthly_arrival: Dict[str, Dict[str, int]] = {}
    for d in days:
        key = d[:7]
        monthly_arrival.setdefault(key, {"n": 0, "hit": 0})
        monthly_arrival[key]["n"] += 1
        if arrival_days[d]["hit"] > 0:
            monthly_arrival[key]["hit"] += 1
    arrival = {
        "days": len(days),
        "days_with_candidate": hit_days,
        "arrival_rate": round(hit_days / len(days), 4) if days else None,
        "min_conf": min_conf,
        "monthly": {k: {"n": v["n"], "hit": v["hit"],
                        "rate": round(v["hit"] / v["n"], 3) if v["n"] else None}
                    for k, v in sorted(monthly_arrival.items())},
    }

    timeline_out = {}
    for mth, cnt in sorted(timeline.items()):
        tot = sum(cnt.values()) or 1
        timeline_out[mth] = {s: round(cnt.get(s, 0) / tot, 3) for s in STATES}

    yearly_out: Dict[str, Dict[str, Any]] = {}
    for (y, s), edges in sorted(yearly.items()):
        if len(edges) < 8:
            continue
        m = statistics.mean(edges)
        sd = statistics.pstdev(edges) if len(edges) > 1 else 0.0
        t = m / (sd / math.sqrt(len(edges))) if sd > 0 else 0.0
        yearly_out.setdefault(y, {})[s] = {
            "n_index": len(edges), "edge_mean": round(m, 5),
            "t_stat": round(t, 2),
            "positive_share": round(sum(1 for e in edges if e > 0) / len(edges), 3)}

    return {
        "state_summary": summary,
        "yearly": yearly_out,
        "arrival": arrival,
        "timeline": timeline_out,
        "avg_state_duration_days": round(statistics.mean(durations), 2) if durations else None,
        "per_index": per_index,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="状态机实证检验（只读）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", default="",
                    help="统一样本窗口起点（如 2020-11-06）。缓存深度不一致时"
                         "（老缓存 2016 起、新深拉 6 年整），不统一窗口会让各指数的"
                         "基线均值取自不同区间，配对 edge 不可比。")
    ap.add_argument("--dump-panel", default="", help="把回放面板写到该路径（JSONL）")
    ap.add_argument("--from-panel", default="", help="不回放，直接从面板重算")
    ap.add_argument("--fill-panel", default="",
                    help="修复面板：用行情缓存补齐缺失的 f20 后原地重写（不重跑回放）")
    ap.add_argument("--out", default=OUT, help="结果 JSON 路径")
    a = ap.parse_args()

    import engine
    cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    st_cfg = cfg["state"]
    universe = cfg["universe"]

    if a.fill_panel:
        cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
        records = load_panel(a.fill_panel)
        filled = 0
        for code, recs in records.items():
            bars = engine.load_bars(cache_dir, code)
            if not bars:
                continue
            filled += attach_f20(recs, bars)
        n = dump_panel(records, a.fill_panel)
        print(f"[panel] 已补齐 f20：{filled} 条 / 总 {n} 条 → {a.fill_panel}")
        agg = aggregate(records, universe, st_cfg)
    elif a.from_panel:
        records = load_panel(a.from_panel)
        print(f"[panel] {a.from_panel} 标的={len(records)} "
              f"行数={sum(len(v) for v in records.values())}")
    else:
        cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
        records = {}
        n_idx = 0
        for u in universe:
            if not u.get("enabled", True):
                continue
            bars = engine.load_bars(cache_dir, u["code"])
            if len(bars) <= WARMUP + 25:
                continue
            recs = replay(u["code"], bars, st_cfg)
            # 统一窗口：预热用窗口前的历史（不浪费样本），但只统计窗口内的记录
            if a.start:
                recs = [r for r in recs if r["date"] >= a.start]
                if len(recs) < 60:
                    continue
            # f20 必须在落盘【之前】算好：此前 dump 早于 aggregate，导致面板里 f 全空
            attach_f20(recs, bars)
            records[u["code"]] = recs
            n_idx += 1
            print(f"  replay {u['code']} {u['name']:<10} days={len(recs)}", flush=True)
            if a.limit and n_idx >= a.limit:
                break
        if a.dump_panel:
            n = dump_panel(records, a.dump_panel)
            print(f"[panel] 已写 {a.dump_panel} 行数={n}")

    if not a.fill_panel:
        agg = aggregate(records, universe, st_cfg)
    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": "production-identical replay; per-index paired edge vs own baseline",
        "replayed": len(agg["per_index"]),
        "window": {"first": min(agg["timeline"], default=None),
                   "last": max(agg["timeline"], default=None)},
        **agg,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    summary = agg["state_summary"]
    arrival = agg["arrival"]
    print("\n状态  指数数   edge(20d)    t    正值占比  符号p   绝对fwd20")
    for s in STATES:
        v = summary.get(s)
        if v:
            print(f"  {s}   {v['n_index']:>4}   {v['edge_mean']:+.4%}   {v['t_stat']:>5.1f}   "
                  f"{v['positive_share']*100:>5.1f}%   {v['sign_p']:>6.3f}   {v['abs_mean_f20']:+.4%}")
    if arrival.get("arrival_rate") is not None:
        print(f"\n信号到达率（可交易标的 A/B/C 且 conf>={arrival['min_conf']}）："
              f"{arrival['days_with_candidate']}/{arrival['days']} 天 = {arrival['arrival_rate']:.1%}")
    print(f"平均状态持续：{out['avg_state_duration_days']} 天")
    print(f"JSON: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
