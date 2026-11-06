# -*- coding: utf-8 -*-
"""tools_eligibility_snapshot.py — B2 资格分快照落盘 CLI（P1）

读取最新决策 JSON + ETF 行情快照 → 全 universe 资格分 → 追加落盘
state/eligibility_history.jsonl（同 as_of 幂等覆盖）。

用法：
  python tools_eligibility_snapshot.py                # 用 data/ 下最新决策 JSON
  python tools_eligibility_snapshot.py --as-of 2026-11-05
  python tools_eligibility_snapshot.py --check        # 校验历史文件完整性（只读）
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import eligibility as E

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def latest_decision_path() -> str:
    files = sorted(glob.glob(os.path.join(DATA, "splus_decision_*.json")))
    if not files:
        raise FileNotFoundError("data/ 下无 splus_decision_*.json")
    return files[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 资格分快照落盘")
    ap.add_argument("--as-of", default=None, help="覆盖 as_of（默认取决策 JSON 内值）")
    ap.add_argument("--check", action="store_true", help="只读校验历史文件")
    args = ap.parse_args()

    cfg_path = os.path.join(BASE, "splus_config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        import yaml
        cfg = (yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}).get("eligibility") or {}

    hist_rel = cfg.get("history_path", "state/eligibility_history.jsonl")
    hist_path = os.path.join(BASE, hist_rel)

    if args.check:
        rows = E.load_history(hist_path)
        bad = [r for r in rows if not r.get("as_of") or "scores" not in r]
        print(f"历史文件: {hist_path}")
        print(f"行数: {len(rows)}  损坏行: {len(bad)}")
        if rows:
            print(f"区间: {rows[0]['as_of']} ~ {rows[-1]['as_of']}")
        return 1 if bad else 0

    dj = latest_decision_path()
    d = json.load(open(dj, encoding="utf-8"))
    as_of = args.as_of or d.get("as_of")
    qj = os.path.join(DATA, "etf_quotes.json")
    quotes = (json.load(open(qj, encoding="utf-8")).get("quotes") or {}
              if os.path.exists(qj) else {})

    out = E.compute_all(d["per_index"], d.get("quality_warnings"), quotes, cfg)
    n = E.append_history(hist_path, as_of, out)

    # 摘要
    tiers: dict = {}
    for r in out.values():
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    ranked = sorted(out.items(), key=lambda kv: -kv[1]["score"])
    print(f"决策来源: {os.path.basename(dj)}  as_of={as_of}")
    print(f"tier 分布: {json.dumps(tiers, ensure_ascii=False)}  → 落盘 {hist_rel}（共 {n} 行）")
    print("Top 8:")
    for c, r in ranked[:8]:
        name = (d["per_index"].get(c) or {}).get("name", "")
        print(f"  {c} {name:<10} {r['score']:5.1f} {r['tier']:<8} "
              f"{r['family']:<9} {json.dumps(r['factors'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
