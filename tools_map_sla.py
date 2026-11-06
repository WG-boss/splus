#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_map_sla.py —— B2-P4 映射 SLA 检查器

B2 策划案执行工程第一条：「Active 标的映射 SLA 100%」。
规则（两层，任一违反即 SLA 违约）：
  R1  可执行 = tradeable=true 且 (sizing.final>0 或 tier ∈ {FULL,REDUCED})
      → 必须 status=confirmed（映射未确认不得进入执行链）
  R2  可执行 → 必须 etf_code 非空（无下单代码=无法执行）

用法：
  python tools_map_sla.py            # 检查并输出报告；违约 exit 1（可入 run_all_checks）
  python tools_map_sla.py --json     # 机器可读输出

设计说明：FROZEN/OBSERVE 且 sizing=0 的 tradeable 标的不构成违约——
资格引擎已把它们挡在执行链之外（B2 分层语义：资格是执行的前置，
映射是下单的前置，两层各自闭环）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
EXEC_TIERS = {"FULL", "REDUCED"}


def _latest_decision() -> Dict[str, Any]:
    files = sorted(glob.glob(os.path.join(BASE, "data", "splus_decision_*.json")))
    if not files:
        return {}
    with open(files[-1], "r", encoding="utf-8") as fh:
        return json.load(fh) or {}


def check() -> Dict[str, Any]:
    import yaml
    with open(os.path.join(BASE, "etf_map.yaml"), "r", encoding="utf-8") as fh:
        inst = (yaml.safe_load(fh) or {}).get("instruments") or {}
    dec = _latest_decision()
    per = dec.get("per_index") or {}

    violations: List[Dict[str, Any]] = []
    exec_count = 0
    for code, m in inst.items():
        if not m.get("tradeable"):
            continue
        v = per.get(code) or {}
        sizing_final = ((v.get("sizing") or {}).get("final") or 0.0)
        tier = ((v.get("eligibility") or {}).get("tier")) or ""
        executable = sizing_final > 0 or tier in EXEC_TIERS
        if not executable:
            continue
        exec_count += 1
        if m.get("status") != "confirmed" or not m.get("etf_code"):
            violations.append({
                "code": code, "name": m.get("name"),
                "status": m.get("status"), "etf_code": m.get("etf_code"),
                "sizing_final": sizing_final, "tier": tier,
            })
    return {"as_of": dec.get("as_of"), "exec_count": exec_count,
            "violations": violations,
            "sla_pct": (100.0 * (exec_count - len(violations)) / exec_count
                        if exec_count else 100.0)}


def main() -> int:
    ap = argparse.ArgumentParser(description="B2-P4 映射 SLA 检查")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"映射 SLA（数据 as_of={r['as_of']}）：可执行标的 "
              f"{r['exec_count']} 只，违约 {len(r['violations'])} 只，"
              f"SLA={r['sla_pct']:.0f}%")
        for v in r["violations"]:
            print(f"  ✗ {v['code']} {v['name']} status={v['status']} "
                  f"etf={v['etf_code']} sizing={v['sizing_final']:.2%} "
                  f"tier={v['tier']}")
        if not r["violations"]:
            print("  ✓ 全部可执行标的映射已确认")
    return 1 if r["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
