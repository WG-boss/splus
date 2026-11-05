# -*- coding: utf-8 -*-
"""
etf_map.py —— 指数 → 场内 ETF/基金 映射的运行时读取层（P0-④ 数据层）

用户确认（2026-11-05）：ETF/基金映射「连观察池一起做」，覆盖全部 118 只。

诚实性约束（重要）：
  自动名称匹配只能产出【候选】，不能当成已核实的跟踪标的。
  只有经 `tools_etf_map.py --confirm` 人工确认的条目才是 status=confirmed，
  报告才会显示可下单的 6 位代码；其余一律显示「待确认」。
  本模块不编造任何 ETF 代码。
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_FILE = "etf_map.yaml"


def load(base_dir: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fname = ((cfg or {}).get("etf_map") or {}).get("file") or DEFAULT_FILE
    path = os.path.join(base_dir, fname)
    if yaml is None or not os.path.exists(path):
        return {"ok": False, "path": path, "instruments": {}, "reason": "etf_map.yaml 缺失或未装 pyyaml"}
    try:
        raw = yaml.safe_load(io.open(path, "r", encoding="utf-8").read()) or {}
        ins = raw.get("instruments") or {}
        return {"ok": True, "path": path, "instruments": ins,
                "meta": raw.get("meta") or {},
                "count": len(ins)}
    except Exception as exc:
        return {"ok": False, "path": path, "instruments": {},
                "reason": f"{type(exc).__name__}: {exc}"}


def for_code(m: Dict[str, Any], code: str, name: str, tradeable: bool) -> Dict[str, Any]:
    rec = (m.get("instruments") or {}).get(code) or {}
    return {
        "index_code": code,
        "index_name": rec.get("name") or name,
        "tradeable": bool(tradeable),
        "status": rec.get("status") or "unmapped",
        "etf_code": rec.get("etf_code"),
        "etf_name": rec.get("etf_name"),
        "verified": bool(rec.get("verified")),
        "candidates": rec.get("candidates") or [],
        "note": rec.get("note") or "",
        "market": rec.get("market"),
        "qdii": bool(rec.get("qdii")),
    }


def attach(per_index: Dict[str, Any], mapping: Dict[str, Any]) -> Dict[str, Any]:
    """把 instrument 注入每个标的，返回覆盖统计。原 dict 原地修改。"""
    stats = {"total": len(per_index), "mapped": 0, "confirmed": 0,
             "candidates": 0, "unmapped": 0}
    for code, v in per_index.items():
        ins = for_code(mapping, code, v.get("name", code), v.get("tradeable", True))
        v["instrument"] = ins
        stats["mapped"] += 1
        if ins["status"] == "confirmed" and ins["etf_code"]:
            stats["confirmed"] += 1
        elif ins["candidates"]:
            stats["candidates"] += 1
        else:
            stats["unmapped"] += 1
    return stats


def display(ins: Dict[str, Any]) -> Tuple[str, str]:
    """返回 (展示文本, 语义类)。语义类用于报告配色。"""
    if ins.get("status") == "confirmed" and ins.get("etf_code"):
        return f"{ins['etf_code']} {ins.get('etf_name') or ''}".strip(), "pos"
    n = len(ins.get("candidates") or [])
    if n:
        return f"待确认（候选 {n} 只）", "warn"
    return "未映射（仅观察）", "neg"


def amount_hint(ins: Dict[str, Any]) -> str:
    """个人执行提示：T+1 / QDII 溢价 / 场内最小 100 份。"""
    tips = ["场内 ETF 最小 100 份、T+1"]
    if ins.get("qdii"):
        tips.append("QDII 需关注溢价与限购")
    if ins.get("status") != "confirmed":
        tips.append("代码未核实，禁止据此下单")
    return "；".join(tips)
