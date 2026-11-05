# -*- coding: utf-8 -*-
"""
attribution.py —— 归因台账真正落盘（P2-②）

此前 governance.log_attribution() 有实现、配置里也声明了四桶，
但从未被任何运行路径调用，attribution.jsonl 始终不存在。

本模块的诚实边界（重要）：
  - total 取 predictions.jsonl 里已结算的【实际前瞻收益】，不编造；
  - beta 取同期基准指数【实际收益】，从已有 bar 读取；
  - sector / stage 没有独立基准与归因模型 → 显式置 0 并标注"未拆分"；
  - execution 是 total - beta 的【残差】，含行业、择时与交易成本，
    绝不能单独解读为"滑点损耗"；
  - 没有任何已结算预测时台账保持为空，绝不造数据凑条数。

幂等：同一 (as_of, code) 只写一次；重跑不会刷屏。
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List

import governance


def _close_on(bars: List[Dict[str, Any]], day: str):
    for b in bars or []:
        if str(b.get("date") or "")[:10] == day:
            try:
                return float(b.get("close"))
            except Exception:
                return None
    return None


def rebuild(ledger_dir: str, bars_by_code: Dict[str, List[Dict[str, Any]]],
            cfg: Dict[str, Any]) -> Dict[str, Any]:
    ac = cfg.get("attribution") or {}
    bench = str(ac.get("benchmark_code") or "000300")
    note = str(ac.get("note") or
               "sector/stage 未拆分（无独立基准与归因模型）；"
               "execution 为 total-beta 残差，含行业/择时/成本，不可单独解读为滑点")
    preds = governance.read_jsonl(os.path.join(ledger_dir, "predictions.jsonl"))
    settled = [r for r in preds if r.get("settled")
               and r.get("fwd_return") is not None]
    attr_path = os.path.join(ledger_dir, "attribution.jsonl")
    existing = governance.read_jsonl(attr_path)
    seen = {(str(r.get("as_of") or "")[:10], str(r.get("code") or ""))
            for r in existing}

    written, skip_dup, skip_bench = 0, 0, 0
    bench_bars = (bars_by_code or {}).get(bench) or []
    for r in settled:
        key = (str(r.get("as_of") or "")[:10], str(r.get("code") or ""))
        if key in seen:
            skip_dup += 1
            continue
        day0, day1 = key[0], str(r.get("settle_date") or "")[:10]
        c0, c1 = _close_on(bench_bars, day0), _close_on(bench_bars, day1)
        if not day1 or c0 is None or c1 is None or c0 <= 0:
            skip_bench += 1
            continue
        beta = c1 / c0 - 1.0
        total = float(r["fwd_return"])
        attr = governance.attribute_return(beta, 0.0, 0.0, total)
        rec = {
            "as_of": day0, "settle_date": day1,
            "code": key[1], "name": r.get("name"),
            "basis": "settled_prediction+benchmark_bars",
            "benchmark_code": bench,
            "sector": 0.0, "stage": 0.0,
            "unattributed": True,
            "note": note,
            **attr,
        }
        os.makedirs(ledger_dir, exist_ok=True)
        with io.open(attr_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        seen.add(key)
        written += 1

    all_recs = governance.read_jsonl(attr_path)
    return {
        "written": written, "skipped_duplicate": skip_dup,
        "skipped_no_benchmark": skip_bench,
        "settled_predictions": len(settled),
        "total_records": len(all_recs),
        "benchmark_code": bench,
        "path": attr_path,
        "health": governance.attribute_health(all_recs),
        "note": note,
        "reason": ("无已结算预测，归因台账保持为空（不编造收益）"
                   if not settled else ""),
    }


def status(ledger_dir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = os.path.join(ledger_dir, "attribution.jsonl")
    recs = governance.read_jsonl(path)
    return {"path": path, "exists": os.path.exists(path),
            "count": len(recs),
            "health": governance.attribute_health(recs),
            "note": (cfg.get("attribution") or {}).get("note")}
