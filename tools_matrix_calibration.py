#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_matrix_calibration.py —— B2-P4 StateFit 矩阵回放校准（只读证据采集）

B2 策划案 P4：「矩阵 15 格 10 年回放校准，每格需 ≥30 状态日样本」。
数据现实（2026-11-06 实测）：每只指数缓存 573 根 bar（约 2.4 年），
10 年全量校准受数据源深度限制——本工具对【可用窗口】做逐日状态机回放，
产出三件校准证据：
  1) 每格 (角色族 × 状态) 的状态日样本量（<30 标记样本不足）
  2) 每格状态日后 5/20 日均收益（状态质量的方向性证据，对照矩阵先验）
  3) 窗口覆盖说明（不冒充 10 年结论）

回放方法：与生产引擎完全同链——compute_features → state_probs
（贝叶斯逐日递推，persistence/temperature/scoring 全部读生产 config），
warmup 120 日后开始记录 dominant，杜绝前视偏差。

用法：
  python tools_matrix_calibration.py            # 回放并写 state/matrix_calibration.json
  python tools_matrix_calibration.py --limit 5  # 只回放前 N 只（冒烟）
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
OUT_PATH = os.path.join(BASE, "state", "matrix_calibration.json")
WARMUP = 120          # 特征预热期（MA200 需要 200 日？compute_features 自行容错）
WIN = 320             # 回放特征窗口（≥252 最长回看窗 + EMA 收敛缓冲，见 replay_index）
STATES = ["A", "B", "C", "D", "E"]
FAMS = ["offense", "event", "reference"]


def replay_index(code: str, bars: List[Dict[str, Any]],
                 st_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐日回放状态机（与 engine.run 同链），返回 [(date, dominant)]。

    性能：compute_features 全部为尾窗回看（最长 252 日），故每次只传
    320 日尾窗（WIN），EMA60 在 320 根内完全收敛（(1-2/21)^320≈e^-32），
    数值与全历史切片无可测差异；缓存 573 bar 时成本约降 45%。
    """
    import engine
    prev = None
    out: List[Dict[str, Any]] = []
    for i in range(WARMUP, len(bars)):
        feats = engine.compute_features(bars[max(0, i - WIN + 1):i + 1])
        sp = engine.state_probs(
            feats, prev,
            float(st_cfg["persistence"]), float(st_cfg["temperature"]),
            st_cfg["scoring"], float(st_cfg.get("uniform_prior", 0.2)))
        prev = sp["probs"]   # 生产引擎同链：递推先验取 probs 子字典（非整个返回结构）
        out.append({"date": bars[i]["date"], "dominant": sp["dominant"],
                    "close": bars[i]["close"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="StateFit 矩阵回放校准（只读）")
    ap.add_argument("--limit", type=int, default=0, help="只回放前 N 只（冒烟）")
    ap.add_argument("--start", default="",
                    help="统一样本窗口起点（如 2020-11-06）：预热仍用窗前历史，"
                         "只统计窗内状态日，避免各指数基线取自不同区间")
    a = ap.parse_args()

    import yaml
    import engine
    import eligibility as E

    cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
    st_cfg = cfg["state"]

    rows: List[Dict[str, Any]] = []
    for u in cfg["universe"]:
        if not u.get("enabled", True):
            continue
        bars = engine.load_bars(cache_dir, u["code"])
        if len(bars) <= WARMUP + 25:
            rows.append({"code": u["code"], "name": u["name"],
                         "error": f"bar 不足（{len(bars)}）"})
            continue
        fam = E.role_family(u.get("role"), bool(u.get("tradeable")), None)
        recs = replay_index(u["code"], bars, st_cfg)
        if a.start:
            recs = [r for r in recs if str(r["date"]) >= a.start]
            if len(recs) < 60:
                rows.append({"code": u["code"], "name": u["name"],
                             "error": f"窗内样本不足（{len(recs)}）"})
                continue
        closes = [r["close"] for r in recs]
        for j, r in enumerate(recs):
            f5 = (closes[j + 5] / closes[j] - 1.0) if j + 5 < len(closes) else None
            f20 = (closes[j + 20] / closes[j] - 1.0) if j + 20 < len(closes) else None
            r["fwd5"], r["fwd20"] = f5, f20
        rows.append({"code": u["code"], "name": u["name"], "family": fam,
                     "n_days": len(recs),
                     "first": recs[0]["date"], "last": recs[-1]["date"],
                     "recs": recs})
        if a.limit and sum(1 for x in rows if "recs" in x) >= a.limit:
            break

    # 聚合到 (family, state) 格
    cells: Dict[str, Dict[str, Any]] = {}
    by_cell: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        for rec in r.get("recs", []):
            by_cell[(r["family"], rec["dominant"])].append(rec)
    for fam in FAMS:
        for s in STATES:
            recs = by_cell.get((fam, s), [])
            f5 = [r["fwd5"] for r in recs if r["fwd5"] is not None]
            f20 = [r["fwd20"] for r in recs if r["fwd20"] is not None]
            cells[f"{fam}|{s}"] = {
                "family": fam, "state": s,
                "n_days": len(recs),
                "enough": len(recs) >= 30,
                "fwd5_mean": round(statistics.mean(f5), 5) if f5 else None,
                "fwd20_mean": round(statistics.mean(f20), 5) if f20 else None,
                "fwd5_median": round(statistics.median(f5), 5) if f5 else None,
                "fwd20_median": round(statistics.median(f20), 5) if f20 else None,
            }

    replayed = [r for r in rows if "recs" in r]
    windows = sorted({(r["first"], r["last"]) for r in replayed})
    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(
            timespec="seconds"),
        "method": ("逐日贝叶斯状态机回放（compute_features→state_probs，"
                   "参数同生产 config），warmup 120 日，只读证据采集"),
        "window_note": (f"缓存深度约 {len(replayed[0]['recs']) + WARMUP} bar/只"
                        if replayed else "n/a"),
        "replayed": len(replayed),
        "skipped": [r for r in rows if "error" in r],
        "cells": cells,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_PATH)

    print(f"回放 {len(replayed)} 只（窗口≈{out['window_note']}），"
          f"跳过 {len(out['skipped'])} 只 → {os.path.relpath(OUT_PATH, BASE)}")
    print(f"\n{'格':<18} {'样本日':>6} {'≥30':>4} {'fwd5均':>9} {'fwd20均':>9}")
    for k, c in cells.items():
        f5 = f"{c['fwd5_mean']:+.2%}" if c["fwd5_mean"] is not None else "—"
        f20 = f"{c['fwd20_mean']:+.2%}" if c["fwd20_mean"] is not None else "—"
        print(f"{k:<18} {c['n_days']:>6} {'✓' if c['enough'] else '✗':>4} "
              f"{f5:>9} {f20:>9}")
    insufficient = sum(1 for c in cells.values() if not c["enough"])
    print(f"\n样本不足格（<30 状态日）：{insufficient}/15 —— "
          f"这些格维持先验，待数据扩容后复跑本工具")
    return 0


if __name__ == "__main__":
    sys.exit(main())
