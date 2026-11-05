# -*- coding: utf-8 -*-
"""
market_context.py —— 与 stock-pool 市场闸门的单向顶层否决（P0-②）

背景（2026-11-05 评估）：splus 全仓 0 命中 market_regime / pool_gate。
同日 stock-pool 为「减量」、market_gate allow=false，两仓可能给出相反动作。
用户已明确「只有在 A 股整体市场为增量状态的情况下才动」。

设计边界（重要）：
  * 单向否决：只决定「今天允不允许开新仓」，不改 S++ 自有的 L1 状态、
    L2 压制、L3 仓位模型、L4 执行位、L5 开关。模型建议敞口照常输出，
    另给 actionable_exposure（可被否决为 0）。
  * fail-closed：库缺失 / 状态缺失 / 闸门未定稿 / 快照过期 → 一律否决。
  * 只读：不写 stock-pool 任何文件，不 import stock-pool 模块（避免路径耦合）。
"""
from __future__ import annotations

import datetime
import io
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "stock_pool_dir": "../stock-pool",
    "db_file": "data/stock_pool.db",
    "gate_state_file": "data/market_gate_state.json",
    "require_state": "增量",
    "require_gate_open": True,
    "use_ideal_state": False,    # O2：默认读滞回确认态 state；置 true 改读 B+ 暴露的即时态 state_ideal（转强时少否决 1-2 日，但更跳变）
    "max_lag_days": 4,
    "on_error": "veto",
}


def _d(x: Any) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except Exception:
        return None


def _fresh(date_str: Any, as_of: str, max_lag: int) -> bool:
    a, b = _d(date_str), _d(as_of)
    if not a or not b:
        return False
    return abs((b - a).days) <= int(max_lag)


def _read_regime(db: str, as_of: str, max_lag: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "state": None, "state_ideal": None, "as_of": None, "score": None,
                           "reason": ""}
    if not os.path.exists(db):
        out["reason"] = "stock_pool.db 不存在"
        return out
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT trade_date, state, state_ideal, score FROM market_regime "
                "ORDER BY trade_date DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            # 旧库无 state_ideal 列（B+ 之前），降级只读 state
            row = con.execute(
                "SELECT trade_date, state, score FROM market_regime "
                "ORDER BY trade_date DESC LIMIT 1").fetchone()
        con.close()
        if not row:
            out["reason"] = "market_regime 无数据"
            return out
        d = dict(row)
        out.update(state=d.get("state"), as_of=d.get("trade_date"), score=d.get("score"))
        if "state_ideal" in d:
            out["state_ideal"] = d.get("state_ideal")
        if not _fresh(d.get("trade_date"), as_of, max_lag):
            out["reason"] = f"市场状态快照过期（{d.get('trade_date')} vs {as_of}）"
            return out
        out["ok"] = True
    except Exception as exc:
        out["reason"] = f"读取 market_regime 失败：{type(exc).__name__}: {exc}"
    return out


def _read_gate(path: str, as_of: str, max_lag: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "allow": None, "status": None,
                           "as_of": None, "reason": ""}
    if not os.path.exists(path):
        out["reason"] = "market_gate_state.json 不存在"
        return out
    try:
        g = json.load(io.open(path, "r", encoding="utf-8"))
        out.update(status=g.get("status"), as_of=g.get("as_of"),
                   allow=bool(g.get("allow")) if g.get("allow") is not None else None,
                   bench=g.get("bench"), close=g.get("close"), ema=g.get("ema"),
                   gap_pct=g.get("gap_pct"))
        if g.get("status") != "OK":
            out["reason"] = f"大盘闸门状态未定稿（status={g.get('status')}）"
            return out
        if not _fresh(g.get("as_of"), as_of, max_lag):
            out["reason"] = f"大盘闸门快照过期（{g.get('as_of')} vs {as_of}）"
            return out
        out["ok"] = True
    except Exception as exc:
        out["reason"] = f"读取闸门失败：{type(exc).__name__}: {exc}"
    return out


def load(base_dir: str, cfg: Optional[Dict[str, Any]], as_of: str) -> Dict[str, Any]:
    """返回闸门裁决结果。任何异常都按配置 on_error 处理（默认否决）。"""
    c = {**DEFAULTS, **(((cfg or {}).get("market_context")) or {})}
    use_ideal = bool(c.get("use_ideal_state"))
    root = os.path.normpath(os.path.join(base_dir, c["stock_pool_dir"]))
    db = os.path.join(root, c["db_file"])
    gp = os.path.join(root, c["gate_state_file"])
    lag = int(c["max_lag_days"])

    reg = _read_regime(db, as_of, lag)
    gate = _read_gate(gp, as_of, lag)
    reasons: List[str] = []
    eff_state = reg.get("state_ideal") if (use_ideal and reg.get("state_ideal") is not None) else reg.get("state")

    if not c["enabled"]:
        return {"enabled": False, "allow_open": True, "veto": False,
                "reasons": ["market_context.enabled=false，顶层否决未启用（仅展示）"],
                "regime": reg, "gate": gate, "as_of": as_of,
                "require_state": c["require_state"], "require_gate_open": c["require_gate_open"],
                "use_ideal_state": use_ideal,
                "effective_state": reg.get("state_ideal") if (use_ideal and reg.get("state_ideal") is not None) else reg.get("state"),
                "source_paths": {"db": db, "gate": gp},
                "label": "顶层否决停用（仅展示市场状态）"}

    if not reg["ok"]:
        reasons.append("市场状态不可用：" + (reg.get("reason") or "未知"))
    elif eff_state != c["require_state"]:
        reasons.append(f"A股整体市场为「{eff_state}」，非「{c['require_state']}」")

    if c["require_gate_open"]:
        if not gate["ok"]:
            reasons.append("大盘闸门不可用：" + (gate.get("reason") or "未知"))
        elif gate["allow"] is not True:
            reasons.append("大盘闸门关闭（沪深300 收盘低于 EMA20）")

    if reasons and str(c["on_error"]).lower() == "warn":
        veto = False
        reasons.append("on_error=warn：仅告警不否决（非默认，慎改）")
    else:
        veto = bool(reasons)

    if veto:
        label = "今日禁止开新仓（顶层否决）"
    else:
        label = f"市场符合「{c['require_state']}」且闸门开启"

    return {
        "enabled": True,
        "allow_open": not veto,
        "veto": veto,
        "reasons": reasons,
        "regime": reg,
        "gate": gate,
        "as_of": as_of,
        "require_state": c["require_state"],
        "require_gate_open": bool(c["require_gate_open"]),
        "use_ideal_state": use_ideal,
        "effective_state": eff_state,
        "max_lag_days": lag,
        "on_error": c["on_error"],
        "source_paths": {"db": db, "gate": gp},
        "label": label,
        "note": "单向顶层否决：不改 S++ 自有状态与仓位模型，只否决开新仓",
    }
