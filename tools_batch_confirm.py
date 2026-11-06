#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_batch_confirm.py —— B2-P4 B 级 ETF 批量确认（资格分排序，Top-N 分批）

B2 策划案（docs/tradeable_universe_review_20261106.html §执行工程）P4 动作：
将 needs_review 的映射按【资格分降序】批量确认，先解决高价值标的的
map_ready 因子（0.5 → 1.0），分批推进避免一次性大改。

用法：
  python tools_batch_confirm.py --list --top 30     # 预览（默认 dry-run，只读）
  python tools_batch_confirm.py --apply --top 30    # 实际执行（写 etf_map.yaml）
  python tools_batch_confirm.py --check             # 统计确认进度

安全约束（三重闸门，任一不满足即跳过并留痕）：
  1) 候选名称相似度 >= --min-score（默认 0.85，名称高度吻合才自动确认）
  2) review.risk != high（复核工具标记高风险的绝不自动确认）
  3) 仅处理 status=needs_review 且有候选的条目（unmapped 不碰）
已确认（confirmed）条目一律不动；本工具从不改 tradeable/bucket/role。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(BASE, "etf_map.yaml")
DATA_DIR = os.path.join(BASE, "data")

MIN_SCORE_DEFAULT = 0.85


def _load_yaml(path: str) -> Dict[str, Any]:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def latest_decision() -> Dict[str, Any]:
    """取最新决策 JSON（eligibility 分数来源）。缺失则返回空 dict。"""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "splus_decision_*.json")))
    if not files:
        return {}
    with open(files[-1], "r", encoding="utf-8") as fh:
        return json.load(fh) or {}


def build_ranking(top_n: int, min_score: float) -> Dict[str, Any]:
    """构造按资格分排序的待确认清单（含跳过原因）。纯只读。"""
    import yaml  # noqa: F401  (ensure dependency explicit)
    doc = _load_yaml(MAP_PATH)
    inst = doc.get("instruments") or {}
    dec = latest_decision()
    per = dec.get("per_index") or {}

    ranked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for code, v in inst.items():
        if v.get("status") != "needs_review":
            continue
        cands = v.get("candidates") or []
        if not cands:
            skipped.append({"code": code, "name": v.get("name"),
                            "reason": "无候选(unmapped)"})
            continue
        top = max(cands, key=lambda c: c.get("score") or 0.0)
        reasons = []
        if (top.get("score") or 0.0) < min_score:
            reasons.append(f"相似度{top.get('score'):.2f}<{min_score}")
        rv = v.get("review") or {}
        if rv.get("risk") == "high":
            reasons.append("review.risk=high")
        if reasons:
            skipped.append({"code": code, "name": v.get("name"),
                            "reason": "；".join(reasons),
                            "top": f"{top.get('code')} {top.get('name')}"})
            continue
        elig = (per.get(code) or {}).get("eligibility") or {}
        ranked.append({
            "code": code, "name": v.get("name"),
            "eligibility": elig.get("score"), "tier": elig.get("tier"),
            "etf_code": top.get("code"), "etf_name": top.get("name"),
            "sim": top.get("score"),
        })
    ranked.sort(key=lambda r: (-(r["eligibility"] if r["eligibility"]
                                 is not None else -1.0), -r["sim"]))
    selected = ranked[:top_n]
    return {"doc": doc, "selected": selected, "ranked_all": ranked,
            "skipped": skipped}


def cmd_list(top_n: int, min_score: float) -> int:
    r = build_ranking(top_n, min_score)
    sel, skipped = r["selected"], r["skipped"]
    print(f"待确认队列（按资格分降序，Top {top_n}，相似度>={min_score}，"
          f"排除 risk=high）：")
    print(f"{'#':>3}  {'指数':<12} {'代码':<10} {'资格分':>6} {'档位':<8} "
          f"{'ETF':<22} {'相似度':>5}")
    for i, x in enumerate(sel, 1):
        e = f"{x['eligibility']:.0f}" if x["eligibility"] is not None else "—"
        print(f"{i:>3}  {x['name']:<12} {x['code']:<10} {e:>6} "
              f"{str(x['tier'] or '—'):<8} {x['etf_code']} "
              f"{x['etf_name']:<16} {x['sim']:.2f}")
    print(f"\n合格 {len(r['ranked_all'])} 只，本次选中 {len(sel)} 只，"
          f"跳过 {len(skipped)} 只：")
    for s in skipped[:8]:
        print(f"  跳过 {s['code']} {s['name']}: {s['reason']}")
    if len(skipped) > 8:
        print(f"  ...另 {len(skipped) - 8} 只略")
    return 0


def cmd_apply(top_n: int, min_score: float) -> int:
    r = build_ranking(top_n, min_score)
    sel = r["selected"]
    if not sel:
        print("无符合条件的待确认条目，nothing to do")
        return 0
    import tools_etf_map as tem
    print(f"执行 {len(sel)} 只确认（etf_map.yaml 将更新）...")
    ok = 0
    for x in sel:
        rc = tem.confirm(x["code"], x["etf_code"])
        if rc == 0:
            ok += 1
        else:
            print(f"[FAIL] {x['code']} {x['name']} -> {x['etf_code']}")
    print(f"\n完成：{ok}/{len(sel)} 确认成功。建议复跑 --check 与 "
          f"tools_map_sla.py 验证。")
    return 0 if ok == len(sel) else 1


def cmd_check() -> int:
    doc = _load_yaml(MAP_PATH)
    inst = doc.get("instruments") or {}
    from collections import Counter
    st = Counter(v.get("status") for v in inst.values())
    print("映射状态分布:", dict(st))
    print("SLA：tradeable 且已确认：",
          sum(1 for v in inst.values()
              if v.get("tradeable") and v.get("status") == "confirmed"),
          "/", sum(1 for v in inst.values() if v.get("tradeable")))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B2-P4 B 级 ETF 批量确认")
    ap.add_argument("--list", action="store_true", help="预览排名（只读）")
    ap.add_argument("--apply", action="store_true", help="实际执行确认")
    ap.add_argument("--check", action="store_true", help="确认进度统计")
    ap.add_argument("--top", type=int, default=30, help="本次确认数量上限")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE_DEFAULT,
                    help="候选名称相似度门槛")
    a = ap.parse_args()
    if a.check:
        return cmd_check()
    if a.apply:
        return cmd_apply(a.top, a.min_score)
    return cmd_list(a.top, a.min_score)


if __name__ == "__main__":
    sys.exit(main())
