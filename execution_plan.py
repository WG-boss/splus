# -*- coding: utf-8 -*-
"""
execution_plan.py —— 敞口比例 → 金额 / 份额 / 整手（P1-① 个人可执行层）

为什么单独一层：engine 的 L3/L4 输出的是「指数点位 + NAV 百分比」，
个人既无法用指数点位下单，也无法把百分比直接变成委托单。
本层把两者翻译成「买哪只、多少钱、多少份」，并强制做三重 fail-closed：

  ① 市场闸门否决 / 治理门禁未通过 → 只出「预案」，绝不出可下单指令；
  ② 账户真实净值未配置或无 capital → 不出金额与份额；
  ③ 场内标的未 confirmed 或行情快照过期 → 不出份额（金额最多作为预案）。

另：敞口权重取自 buckets[].per_name（同桶合并后的组内均分），
不能用 per_index[].sizing.final —— 后者是单标的约束上限，
同桶多只分别乘一次会把仓位静默放大数倍。

本层不改任何模型判断、不产生新的仓位数字，只做单位换算与阻断。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import etf_quotes as qmod


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else default
    except Exception:
        return default


def build(cfg: Dict[str, Any], base_dir: str, account_state: Dict[str, Any],
          actionable_exposure: float, buckets: Dict[str, Any],
          per_index: Dict[str, Any], mapping: Dict[str, Any],
          market: Dict[str, Any]) -> Dict[str, Any]:
    """返回执行换算表。任何异常都降级为不可执行，绝不静默给数字。"""
    ec = cfg.get("execution_plan") or {}
    lot = int(ec.get("lot_size") or 100)
    rate = _f(ec.get("commission_rate"), 0.0003)
    min_fee = _f(ec.get("min_commission"), 5.0)
    slip = _f(ec.get("slippage_rate"), 0.001)
    fee_note = str(ec.get("fee_note") or "佣金估算，以券商实际收费为准")

    veto = bool(market.get("veto"))
    account_ok = bool(account_state.get("configured"))
    capital = account_state.get("capital")
    capital_ok = bool(account_ok and capital and _f(capital) > 0)
    snapshot = qmod.load(base_dir, cfg)

    plan: Dict[str, Any] = {
        "enabled": bool(ec.get("enabled", True)),
        "status": "blocked",
        "mode": "预案（今日不可执行）",
        "account_ok": account_ok,
        "capital": round(_f(capital), 2) if capital_ok else None,
        "capital_base": "account.capital（本策略可用资金）",
        "actionable_exposure": round(_f(actionable_exposure), 6),
        "budget_cny": None,
        "veto": veto,
        "lot_size": lot,
        "commission_rate": rate,
        "min_commission": min_fee,
        "slippage_rate": slip,
        "quotes": {"ok": bool(snapshot.get("ok")),
                   "count": snapshot.get("count", 0),
                   "fetched_at": snapshot.get("fetched_at"),
                   "stale": bool(snapshot.get("stale")),
                   "reason": snapshot.get("reason", "")},
        "rows": [], "summary": {}, "warnings": [],
        "disclaimer": "报价为快照，下单前须以券商实时价为准；"
                      "场内 ETF 买入最小 100 份、T+1；QDII 需关注溢价与限购。"
                      f"佣金+滑点（{slip:.2%} 单边估算）合计为成本口径，实际以成交为准。" + fee_note,
    }

    if capital_ok:
        plan["budget_cny"] = round(_f(capital) * _f(actionable_exposure), 2)

    if veto:
        plan["warnings"].append("市场闸门顶层否决：今日不开新仓，下表仅为闸门通过后的预案")
    if not account_ok:
        plan["warnings"].append("账户真实净值未配置：不换算任何金额/份额")
    elif not capital_ok:
        plan["warnings"].append("account.capital 未填或非正数：不换算任何金额/份额")
    if snapshot.get("stale") or not snapshot.get("ok"):
        plan["warnings"].append("行情快照不可用：" + str(snapshot.get("reason") or "未知"))
    plan["warnings"].extend(market.get("reasons") or [])

    rows: List[Dict[str, Any]] = []
    ins_all = (mapping.get("instruments") or {})
    n_ready = 0
    n_confirmed = 0
    for code, v in per_index.items():
        if not v.get("tradeable", True):
            continue
        bk = (buckets or {}).get(v.get("bucket")) or {}
        weight = _f(bk.get("per_name"))
        ins = ins_all.get(code) or {}
        confirmed = bool(ins.get("status") == "confirmed" and ins.get("etf_code"))
        if confirmed:
            n_confirmed += 1
        gate = v.get("governance_gate") or {}
        gov_ok = bool(v.get("open_allowed")) and bool(gate.get("allowed"))

        reasons: List[str] = []
        if veto:
            reasons.append("市场闸门否决")
        if not gov_ok:
            reasons.append("治理门禁未通过" if gate.get("open_intent") else "无开仓意向")
        if not capital_ok:
            reasons.append("账户资金未配置")
        if not confirmed:
            reasons.append("场内标的未核实")

        target_cny = round(_f(capital) * weight, 2) if capital_ok else None
        # 闸门否决时预算归零，但保留「预案目标金额」供闸门打开后复用
        quote = {"fresh": False, "reason": "未核实标的，不取报价"}
        if confirmed:
            quote = qmod.for_code(snapshot, str(ins.get("etf_code")))
        if not quote.get("fresh"):
            reasons.append("报价不可用（" + str(quote.get("reason") or "—") + "）")

        price = quote.get("price")
        shares = None
        actual = None
        fee = None
        slip_cny = None
        if capital_ok and confirmed and price and target_cny and target_cny > 0:
            lots = int(math.floor(target_cny / (price * lot)))
            shares = lots * lot
            actual = round(shares * price, 2)
            fee = round(max(actual * rate, min_fee), 2) if shares > 0 else 0.0
            # 滑点估算（P1 成本模型补强 2026-11-07）：按 slippage_rate 单边计提
            slip_cny = round(actual * slip, 2) if shares > 0 else 0.0
            if shares <= 0:
                reasons.append("金额不足 1 手")

        # 只有「全链路通过」才是可下单指令；其余一律预案
        executable = bool(not veto and gov_ok and capital_ok and confirmed
                          and quote.get("fresh") and shares and shares > 0)
        if executable:
            n_ready += 1

        rows.append({
            "code": code,
            "name": v.get("name") or code,
            "bucket": v.get("bucket"),
            "target_weight": round(weight, 6),
            "target_cny": target_cny,
            "etf_code": ins.get("etf_code") if confirmed else None,
            "etf_name": ins.get("etf_name") if confirmed else None,
            "instrument_status": ins.get("status") or "unmapped",
            "price": price,
            "shares": shares,
            "lots": (shares // lot) if shares else None,
            "actual_cny": actual,
            "fee_cny": fee,
            "slippage_cny": slip_cny,
            "total_cny": round(actual + fee + (slip_cny or 0.0), 2)
                         if actual is not None and fee is not None else None,
            "executable": executable,
            "reasons": reasons,
            "quote_reason": quote.get("reason") or "",
        })

    plan["rows"] = rows
    plan["summary"] = {
        "tradeable": len(rows),
        "confirmed": n_confirmed,
        "executable": n_ready,
        "target_total_cny": round(sum(r["target_cny"] or 0 for r in rows), 2) if capital_ok else None,
        "actual_total_cny": round(sum(r["actual_cny"] or 0 for r in rows), 2) if capital_ok else None,
        "fee_total_cny": round(sum(r["fee_cny"] or 0 for r in rows), 2) if capital_ok else None,
        "slippage_total_cny": round(sum(r["slippage_cny"] or 0 for r in rows), 2) if capital_ok else None,
    }

    if veto:
        plan["status"] = "blocked_veto"
    elif not account_ok or not capital_ok:
        plan["status"] = "no_account"
    elif n_confirmed == 0:
        plan["status"] = "no_instrument"
        plan["warnings"].append("11 只可交易标的均未人工确认 ETF 代码；"
                                "请逐只 --confirm 后再看份额")
    elif not snapshot.get("ok") or snapshot.get("stale"):
        plan["status"] = "no_quotes"
    elif n_ready == 0:
        plan["status"] = "no_open_intent"
        plan["warnings"].append("闸门已开但无标的通过治理门禁/达到 1 手，今日无委托")
    elif n_ready < len(rows):
        plan["status"] = "partial"
    else:
        plan["status"] = "ready"
        plan["mode"] = "可执行（仍需人工复核报价与触发位）"

    return plan
