# -*- coding: utf-8 -*-
"""tools_state_tail.py — 按状态统计前向收益的**尾部风险**（只读）。

为什么需要：
  tools_state_edge.py 只回答「均值 edge 是否显著」，但仓位权限的依据是
  「最坏情况下会怎样」。均值相同、左尾不同的两个状态，风控含义完全不同。
  结论可能反转：某状态均值不占优，但尾部更薄 → 仍值得给仓位。

方法：
  与生产引擎同链回放（compute_features → state_probs，warmup 120），
  对每个状态日统计 fwd20 分布：均值/中位/标准差/P(<-5%)/P(<-10%)/
  P(>+5%)/最差 5% 均值。

用法：
  python tools_state_tail.py                      # 全部 118 只
  python tools_state_tail.py --tradeable-only     # 只统计可交易标的（默认口径）
  python tools_state_tail.py --start 2020-11-06   # 统一样本窗口
  python tools_state_tail.py --horizon 20
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

WARMUP = 120
WIN = 320
STATES = ["A", "B", "C", "D", "E"]
OUT = os.path.join(BASE, "state", "state_tail.json")


def replay(bars: List[Dict[str, Any]], st_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import engine
    prev = None
    out = []
    for i in range(WARMUP, len(bars)):
        feats = engine.compute_features(bars[max(0, i - WIN + 1):i + 1])
        sp = engine.state_probs(
            feats, prev,
            float(st_cfg["persistence"]), float(st_cfg["temperature"]),
            st_cfg["scoring"], float(st_cfg.get("uniform_prior", 0.2)))
        prev = sp["probs"]
        out.append({"date": bars[i]["date"], "dom": sp["dominant"],
                    "close": float(bars[i]["close"])})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--tradeable-only", action="store_true", default=True)
    ap.add_argument("--all", dest="tradeable_only", action="store_false")
    ap.add_argument("--from-panel", default="",
                    help="复用 tools_state_edge.py --dump-panel 生成的面板（秒级，仅支持 horizon=20）")
    a = ap.parse_args()

    import engine
    cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
    st_cfg = cfg["state"]
    H = a.horizon
    if a.from_panel and H != 20:
        print("[tail] --from-panel 仅支持 horizon=20（面板只存了 20 日前向收益）")
        return 2

    pool = [u for u in cfg["universe"]
            if u.get("enabled", True) and (u.get("tradeable") or not a.tradeable_only)]
    print("[tail] 标的=%d  horizon=%dd  start=%s  src=%s"
          % (len(pool), H, a.start or "-", "panel" if a.from_panel else "replay"))

    by_state: Dict[str, List[float]] = defaultdict(list)
    per_idx = {}
    panel = None
    if a.from_panel:
        from tools_state_edge import load_panel
        panel = load_panel(a.from_panel)
    for u in pool:
        got: Dict[str, List[float]] = defaultdict(list)
        if panel is not None:
            for r in panel.get(u["code"], []):
                if r.get("f20") is None:
                    continue
                if a.start and r["date"] < a.start:
                    continue
                got[r["dom"]].append(float(r["f20"]))
        else:
            bars = engine.load_bars(cache_dir, u["code"])
            if len(bars) <= WARMUP + H + 5:
                continue
            recs = replay(bars, st_cfg)
            closes = [r["close"] for r in recs]
            for j, r in enumerate(recs):
                if j + H >= len(closes):
                    continue
                d = str(r["date"])
                if a.start and d < a.start:
                    continue
                got[r["dom"]].append(closes[j + H] / closes[j] - 1.0)
        for s, v in got.items():
            by_state[s].extend(v)
        per_idx[u["code"]] = {"name": u["name"], "n": {s: len(v) for s, v in got.items()}}
        print("  %-9s %-14s %s" % (u["code"], u["name"],
                                   {s: len(v) for s, v in sorted(got.items())}), flush=True)

    print()
    print("状态    n_obs   均值     中位     标准差   P(<-5%)  P(<-10%)  P(>+5%)  最差5%均值  偏度符号")
    rows = {}
    for s in STATES:
        v = sorted(by_state.get(s) or [])
        if not v:
            continue
        n = len(v)
        p5 = sum(1 for x in v if x < -0.05) / n
        p10 = sum(1 for x in v if x < -0.10) / n
        up5 = sum(1 for x in v if x > 0.05) / n
        tail = statistics.mean(v[:max(1, n // 20)])
        med = statistics.median(v)
        rows[s] = {"n": n, "mean": statistics.mean(v), "median": med,
                   "std": statistics.pstdev(v), "p_lt_5": p5, "p_lt_10": p10,
                   "p_gt_5": up5, "worst5_mean": tail}
        print("%s   %6d  %+7.3f%%  %+7.3f%%  %6.3f%%  %6.1f%%  %7.1f%%  %6.1f%%  %+8.2f%%"
              % (s, n, rows[s]["mean"] * 100, med * 100, rows[s]["std"] * 100,
                 p5 * 100, p10 * 100, up5 * 100, tail * 100))

    # 风险调整排序：用「收益/左尾概率」看单位尾部风险换来的收益
    print()
    print("风险调整排序（均值 ÷ P(<-10%)）：")
    rk = sorted(((r["mean"] / max(r["p_lt_10"], 1e-6), s) for s, r in rows.items()), reverse=True)
    for val, s in rk:
        print("  %s: %.3f" % (s, val))

    json.dump({"horizon": H, "start": a.start, "pool": len(pool),
               "states": rows, "per_index": per_idx},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n[tail] → %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
