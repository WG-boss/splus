# -*- coding: utf-8 -*-
"""
calibration_progress.py —— 结算进度分级（P2-①）

现状：报告只写「已结算 0 条」，读者不知道还要等多久、还差多少条。
本模块从 predictions.jsonl + 已有 bar 日期序列，算出：
  - 每批预测已过 / 剩余交易日
  - 距离 min_bucket_n 校准门槛还差多少条
  - 分层结论（无样本 / 积累中 / 接近门槛 / 已可校准）

诚实性：未来交易日历未知，只给「还需约 N 个交易日 / 约 M 周」，
绝不编造一个具体的首次结算日期。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Dict, List

import meta_layer


def _trading_days(bars_by_code: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    days = set()
    for bars in (bars_by_code or {}).values():
        for b in bars or []:
            d = str(b.get("date") or "")[:10]
            if len(d) == 10:
                days.add(d)
    return sorted(days)


def build(ledger_dir: str, bars_by_code: Dict[str, List[Dict[str, Any]]],
          cfg: Dict[str, Any]) -> Dict[str, Any]:
    mc = cfg["meta_layer"]["calibration"]
    horizon = int(mc.get("horizon_days") or 20)
    need = int(mc.get("min_bucket_n") or 30)
    path = os.path.join(ledger_dir, "predictions.jsonl")
    recs = meta_layer.read_jsonl(path)
    days = _trading_days(bars_by_code)
    settled = [r for r in recs if r.get("settled")]
    pending = [r for r in recs if not r.get("settled")]

    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in pending:
        by_day[str(r.get("as_of") or "")[:10]].append(r)

    batches = []
    for day in sorted(by_day):
        elapsed = sum(1 for d in days if d > day)
        remaining = max(0, horizon - elapsed)
        batches.append({
            "as_of": day, "n": len(by_day[day]),
            "elapsed_days": elapsed, "remaining_days": remaining,
            "est_weeks": round(remaining / 5.0, 1),
        })

    settled_n = len(settled)
    pending_n = len(pending)
    next_remaining = min((b["remaining_days"] for b in batches), default=None)
    to_threshold = max(0, need - settled_n)

    if settled_n >= need:
        tier, label = "calibrated", "已可校准（样本达门槛）"
    elif settled_n > 0:
        tier, label = "accumulating", "样本积累中，概率仍不得解读为胜率"
    elif pending_n > 0:
        tier, label = "no_sample", "尚无任何结算样本，概率一律标注未校准"
    else:
        tier, label = "empty", "台账为空"

    note = ("按已有 bar 的交易日序列计算 elapsed；未来日历未知，"
            "周数为按每周 5 个交易日的粗略估算，不代表具体日期")
    return {
        "horizon_days": horizon, "min_settled": need,
        "total_predictions": len(recs),
        "settled_n": settled_n, "pending_n": pending_n,
        "to_threshold": to_threshold,
        "progress_pct": round(settled_n / need * 100, 1) if need else 0.0,
        "next_remaining_days": next_remaining,
        "next_est_weeks": round(next_remaining / 5.0, 1) if next_remaining is not None else None,
        "batches": batches,
        "tier": tier, "label": label,
        "note": note,
        "trading_days_observed": len(days),
        "latest_bar_date": days[-1] if days else None,
    }
