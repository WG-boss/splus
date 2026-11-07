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
  * fwd20 用收盘价对收盘价（不含成本），仅供方向性判断。

用法：
  python tools_state_edge.py            # 全量回放并写 state/state_edge.json
  python tools_state_edge.py --limit 8  # 冒烟
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
        out.append({"date": bars[i]["date"], "dom": sp["dominant"],
                    "conf": float(sp["confidence"]), "close": float(bars[i]["close"])})
    return out


def _p_sign(k: int, n: int) -> float:
    """双侧符号检验（H0: p=0.5），正态近似。"""
    if n <= 0:
        return 1.0
    z = (abs(k - n / 2) - 0.5) / math.sqrt(n * 0.25) if n > 0 else 0.0
    return math.erfc(abs(z) / math.sqrt(2))


def main() -> int:
    ap = argparse.ArgumentParser(description="状态机实证检验（只读）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    import engine
    cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
    st_cfg = cfg["state"]
    min_conf = float(st_cfg.get("min_confidence_to_act", 0.65))
    tradeable = {u["code"] for u in cfg["universe"] if u.get("tradeable")}

    per_index: Dict[str, Dict[str, Any]] = {}
    timeline: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    arrival_days: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "hit": 0})
    durations: List[int] = []
    n_idx = 0

    for u in cfg["universe"]:
        if not u.get("enabled", True):
            continue
        bars = engine.load_bars(cache_dir, u["code"])
        if len(bars) <= WARMUP + 25:
            continue
        recs = replay(u["code"], bars, st_cfg)
        closes = [r["close"] for r in recs]
        base_f20 = []
        by_state: Dict[str, List[float]] = defaultdict(list)
        for j, r in enumerate(recs):
            f20 = (closes[j + 20] / closes[j] - 1.0) if j + 20 < len(closes) else None
            r["f20"] = f20
            if f20 is not None:
                base_f20.append(f20)
                by_state[r["dom"]].append(f20)
        if not base_f20:
            continue
        base = statistics.mean(base_f20)
        per_index[u["code"]] = {
            "name": u["name"], "tradeable": u["code"] in tradeable,
            "n_days": len(base_f20), "base_f20": base,
            "state_mean": {s: (statistics.mean(v) if v else None) for s, v in by_state.items()},
            "state_n": {s: len(v) for s, v in by_state.items()},
            "state_edge": {s: (statistics.mean(v) - base if v else None) for s, v in by_state.items()},
        }
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
        if u["code"] in tradeable:
            for r in recs:
                d = arrival_days[r["date"]]
                d["n"] += 1
                if r["dom"] in ("A", "B", "C") and r["conf"] >= min_conf:
                    d["hit"] += 1
        n_idx += 1
        print(f"  replay {u['code']} {u['name']:<10} days={len(recs)}", flush=True)
        if a.limit and n_idx >= a.limit:
            break

    # ---- 状态级汇总（跨指数配对） ----
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

    # ---- 到达率 ----
    days = sorted(arrival_days)
    hit_days = sum(1 for d in days if arrival_days[d]["hit"] > 0)
    monthly_arrival = {}
    for d in days:
        monthly_arrival.setdefault(d[:7], {"n": 0, "hit": 0})
        monthly_arrival[d[:7]]["n"] += 1
        if arrival_days[d]["hit"] > 0:
            monthly_arrival[d[:7]]["hit"] += 1
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

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": "production-identical replay; per-index paired edge vs own baseline",
        "replayed": len(per_index),
        "window": {"first": min((v for v in timeline), default=None),
                   "last": max((v for v in timeline), default=None)},
        "state_summary": summary,
        "arrival": arrival,
        "timeline": timeline_out,
        "avg_state_duration_days": round(statistics.mean(durations), 2) if durations else None,
        "per_index": per_index,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print("\n状态  指数数   edge(20d)    t    正值占比  符号p   绝对fwd20")
    for s in STATES:
        v = summary.get(s)
        if v:
            print(f"  {s}   {v['n_index']:>4}   {v['edge_mean']:+.4%}   {v['t_stat']:>5.1f}   "
                  f"{v['positive_share']*100:>5.1f}%   {v['sign_p']:>6.3f}   {v['abs_mean_f20']:+.4%}")
    print(f"\n信号到达率（可交易标的 A/B/C 且 conf>={min_conf}）："
          f"{arrival['days_with_candidate']}/{arrival['days']} 天 = {arrival['arrival_rate']:.1%}")
    print(f"平均状态持续：{out['avg_state_duration_days']} 天")
    print(f"JSON: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
