# -*- coding: utf-8 -*-
"""
tools_gate.py —— 治理门禁维护 CLI（P0-③）

  python tools_gate.py --status
  python tools_gate.py --template 000688 --as-of 2026-11-05
  python tools_gate.py --check 000688 --as-of 2026-11-05

门禁链：市场闸门（增量+大盘闸门开）→ 行为矩阵（置信/状态）
        → 事前验尸 + 红队质询（journal 文件）→ 才允许开仓。
未写 journal 的标的会被如实拦下并落 governance.jsonl，不静默放行。
"""
from __future__ import annotations
import argparse
import datetime
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import governance as gov

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "splus_config.yaml")


def main() -> int:
    ap = argparse.ArgumentParser(description="S++ 治理门禁维护")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--template", help="为某标的生成 journal 模板")
    ap.add_argument("--check", help="校验某标的 journal 是否通过门禁")
    ap.add_argument("--as-of", help="日期，默认今天")
    args = ap.parse_args()

    cfg = engine.load_config(CFG)
    ledger = os.path.join(BASE, cfg["data"]["ledger_dir"])
    day = args.as_of or datetime.date.today().isoformat()

    if args.status:
        recs = gov.read_jsonl(os.path.join(ledger, "governance.jsonl"))
        daily = [r for r in recs if r.get("type") == "daily"]
        print(f"治理台账：{len(recs)} 条（日级汇总 {len(daily)} 条）")
        for r in daily[-5:]:
            print(f"  {r.get('as_of')}  veto={r.get('market_veto')} "
                  f"intents={r.get('open_intents')} allowed={r.get('allowed')} "
                  f"account={r.get('account_source')}")
        return 0

    if args.template:
        name = next((u["name"] for u in cfg["universe"] if u["code"] == args.template), "")
        p, existed = gov.write_template(ledger, day, args.template, name)
        print(("已存在，未覆盖：" if existed else "已生成模板："), p)
        print("填完后运行： python tools_gate.py --check", args.template, "--as-of", day)
        return 0

    if args.check:
        pre, red = gov.load_journal(ledger, day, args.check)
        g = gov.check_gate(cfg, args.check, pre, red)
        print(f"{args.check} @ {day}: allowed={g['allowed']}")
        if g["errors"]:
            for e in g["errors"]:
                print("  -", e)
        else:
            print("  事前验尸与红队质询齐备，治理门禁通过")
        return 0 if g["allowed"] else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
