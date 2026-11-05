# -*- coding: utf-8 -*-
"""
tools_account.py —— 账户净值维护 CLI（P0-①）

用法：
  python tools_account.py --status
  python tools_account.py --nav 500000 --capital 200000 --as-of 2026-11-05
  python tools_account.py --drawdown -0.08 --as-of 2026-11-05

--nav 会同时追加一行到 data/account_nav.jsonl，历史峰值由 account.py 自动计算。
"""
from __future__ import annotations
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import account as acct

BASE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser(description="维护 S++ 真实账户净值口径")
    ap.add_argument("--status", action="store_true", help="只查看当前账户口径状态")
    ap.add_argument("--nav", type=float, help="当前账户总净值（元）")
    ap.add_argument("--peak-nav", type=float, help="历史最高净值（元），可选")
    ap.add_argument("--capital", type=float, help="可用于本策略的资金（元）")
    ap.add_argument("--drawdown", type=float, help="直接给定当前回撤，如 -0.08")
    ap.add_argument("--as-of", help="快照日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--file", default="account.yaml", help="账户文件名")
    args = ap.parse_args()

    if args.status:
        st = acct.load(BASE, {"account": {"file": args.file}})
        print("账户口径状态：")
        for k in ("configured", "source", "dd", "nav", "peak_nav", "capital",
                  "as_of", "updated_at", "age_days", "stale", "reason", "note"):
            print(f"  {k:<14} = {st.get(k)}")
        print("  展示标签       =", acct.label(st))
        return 0

    if args.drawdown is not None:
        import yaml
        p = os.path.join(BASE, args.file)
        raw = yaml.safe_load(io.open(p, encoding="utf-8").read()) or {}
        day = args.as_of or __import__("datetime").date.today().isoformat()
        a = raw.setdefault("account", {})
        a["enabled"] = True
        a["current_drawdown"] = float(args.drawdown)
        a["as_of"] = day
        if args.capital is not None:
            a["capital"] = float(args.capital)
        raw.setdefault("meta", {})["updated_at"] = day
        with io.open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)
        print("已写入手填回撤：", args.drawdown, "→", p)
    elif args.nav is not None:
        p = acct.set_snapshot(BASE, nav=args.nav, capital=args.capital,
                              as_of=args.as_of, peak_nav=args.peak_nav,
                              fname=args.file)
        print("已更新净值快照：", p)
    else:
        ap.print_help()
        return 2

    st = acct.load(BASE, {"account": {"file": args.file}})
    print("  当前口径：", acct.label(st))
    return 0 if st.get("configured") else 1


if __name__ == "__main__":
    sys.exit(main())
