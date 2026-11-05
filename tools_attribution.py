# -*- coding: utf-8 -*-
"""
tools_attribution.py —— 归因台账 CLI（P2-②）

  python tools_attribution.py --status    查看台账状态与健康度
  python tools_attribution.py --rebuild  从已结算预测 + 基准 bar 重建（幂等）

注意：无已结算预测时 --rebuild 不会产生任何记录，这是正确行为。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import engine  # noqa: E402
import attribution  # noqa: E402

CFG = os.path.join(BASE, "splus_config.yaml")


def bars_map(cfg):
    cache_dir = os.path.join(BASE, cfg["data"]["cache_dir"])
    out = {}
    for u in cfg["universe"]:
        bars = engine.load_bars(cache_dir, u["code"])
        if bars:
            out[u["code"]] = bars
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="S++ 四桶归因台账")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    cfg = engine.load_config(CFG)
    ledger_dir = os.path.join(BASE, cfg["data"]["ledger_dir"])
    if args.rebuild:
        r = attribution.rebuild(ledger_dir, bars_map(cfg), cfg)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(attribution.status(ledger_dir, cfg),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
