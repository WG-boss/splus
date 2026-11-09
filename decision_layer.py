# -*- coding: utf-8 -*-
"""
decision_layer.py —— 把「个人投资参考」所需的口径与执行层接到决策结果上（P0）

集中处理 5 件事，避免把 engine.run() 继续撑成一坨：
  ① 市场闸门单向顶层否决（market_context）
  ② 治理门禁真正接入行为矩阵（governance.check_gate / journal / 幂等台账）
  ③ 全量指数 → 场内 ETF 映射注入（etf_map，含观察池）
  ④ 账户真实净值的金额换算与口径标签（account）
  ⑤ 一页纸决策卡 + 对冲的个人可执行性披露

设计边界：不改 L1/L2/L3/L4/L5 任何模型判断，只在结果层叠加「能不能真的动手」。
任何子模块异常都被隔离并显式披露，绝不静默放行。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import account as account_mod
import etf_map as etf_map_mod
import governance as gov_mod
import market_context as mc_mod
import execution_plan as ep_mod
import quality as quality_mod


def _hedge_text(cfg: Dict[str, Any], p_de: float, hedge_need: float) -> str:
    retail = (cfg.get("hedge") or {}).get(
        "retail_note",
        "个人可执行替代：减仓、转现金或配置低相关宽基")
    if hedge_need:
        return (f"P(D)+P(E) 均值 {p_de:.2f} → 建议等效降低敞口约 {hedge_need:.0%}"
                f"（{retail}）；融券/期指对冲仅为模型参考口径，个人通常不可执行")
    return f"P(D)+P(E) 均值 {p_de:.2f} → 暂不需要系统性对冲"


def apply(cfg: Dict[str, Any], base_dir: str, as_of: str,
          per_index: Dict[str, Any], total_exposure: float,
          p_de: float, hedge_need: float,
          account_state: Dict[str, Any], mapping: Dict[str, Any],
          buckets: Optional[Dict[str, Any]] = None,
          quality_warnings: Optional[list] = None) -> Dict[str, Any]:
    """返回 {"result_fields": ..., "gate_results": ...}；per_index 原地注入。"""
    ledger_dir = os.path.join(base_dir, cfg["data"]["ledger_dir"])

    # ① 市场闸门（fail-closed）
    try:
        market = mc_mod.load(base_dir, cfg, as_of)
    except Exception as exc:
        market = {"enabled": True, "allow_open": False, "veto": True,
                  "reasons": [f"市场闸门模块异常：{type(exc).__name__}: {exc}"],
                  "regime": {"ok": False}, "gate": {"ok": False},
                  "label": "今日禁止开新仓（闸门异常 fail-closed）", "as_of": as_of}
    veto = bool(market.get("veto"))
    actionable = 0.0 if veto else float(total_exposure)

    # ③ ETF 映射（全量：可交易 + 观察池）
    try:
        etf_stats = etf_map_mod.attach(per_index, mapping)
    except Exception as exc:
        etf_stats = {"error": f"{type(exc).__name__}: {exc}"}

    # ② 治理门禁接入行为矩阵
    # 2026-11-09 口径统一：开仓意向优先读 eligibility.permission.add——
    # 资格层与决策层从此共用**同一个布尔**，不再出现「资格 FULL 但不可开仓」
    # 的两套门槛同时出现在一份 JSON 里（此前创新药 931152 即为此例）。
    # 缺 permission（资格层未跑/回滚）才回落到 conf + 状态的老判定，并标注来源。
    thr = float(cfg["state"]["min_confidence_to_act"])
    gate_results: Dict[str, Any] = {}
    open_intents = []
    for code, v in per_index.items():
        st = v.get("state") or {}
        perm = ((v.get("eligibility") or {}).get("permission") or {})
        if "add" in perm:
            add_ok = bool(perm.get("add"))
            perm_src = "eligibility.permission"
        else:
            add_ok = (st.get("confidence", 0) >= thr
                      and st.get("dominant") in ("A", "B", "C"))
            perm_src = "legacy(conf+state)"
        intent = bool(v.get("tradeable")) and add_ok and not veto
        if intent:
            open_intents.append(code)
            try:
                pre, red = gov_mod.load_journal(ledger_dir, as_of, code)
                g = gov_mod.check_gate(cfg, code, pre, red)
                gov_mod.log_gate(ledger_dir, code, g, pre, red)
            except Exception as exc:
                g = {"allowed": False, "errors": [f"门禁异常：{exc}"], "warnings": []}
        else:
            g = {"allowed": False, "errors": [], "warnings": [],
                 "skipped": "市场闸门否决" if veto else "无开仓意向"}
        allowed = bool(intent and g.get("allowed"))
        gate_results[code] = {
            "allowed": allowed,
            "open_intent": bool(intent),
            "permission_source": perm_src,
            "market_veto": veto,
            "errors": g.get("errors") or [],
            "skipped": g.get("skipped", ""),
            "journal_path": gov_mod.journal_path(ledger_dir, as_of, code) if intent else "",
        }
        v["governance_gate"] = gate_results[code]
        v["open_allowed"] = allowed

    try:
        gov_daily = gov_mod.log_daily(ledger_dir, as_of, {
            "market_veto": veto,
            "market_reasons": market.get("reasons") or [],
            "open_intents": len(open_intents),
            "allowed": sum(1 for g in gate_results.values() if g["allowed"]),
            "tradeable": sum(1 for v in per_index.values() if v.get("tradeable")),
            "account_source": account_state.get("source"),
        })
    except Exception as exc:
        gov_daily = {"written": False, "error": f"{type(exc).__name__}: {exc}"}

    # ④⑤ 一页纸决策卡
    cal_cfg = cfg.get("calibration_display") or {}
    min_settled = int(cal_cfg.get("min_settled") or 30)
    hedge_txt = _hedge_text(cfg, p_de, hedge_need)
    # 只统计「可交易集合」内的映射就绪度：池收缩到 9 只后，
    # 用全局 confirmed(52) 去比 tradeable(9) 会显示成 "52/9 已核实"，纯属错位。
    tradeable_n = sum(1 for v in per_index.values() if v.get("tradeable"))
    confirmed = sum(1 for v in per_index.values()
                    if v.get("tradeable")
                    and (v.get("instrument") or {}).get("status") == "confirmed"
                    and (v.get("instrument") or {}).get("verified"))
    alloc = account_mod.allocation(account_state, actionable)

    # P1-①：敞口 → 金额/份额/整手（三重 fail-closed，异常隔离）
    try:
        execution = ep_mod.build(cfg, base_dir, account_state, actionable,
                                 buckets or {}, per_index, mapping, market)
    except Exception as exc:
        execution = {"enabled": False, "status": "error", "rows": [],
                     "summary": {}, "quotes": {}, "disclaimer": "",
                     "warnings": [f"执行换算模块异常：{type(exc).__name__}: {exc}"]}
    # P2-③：48 条软提示分级（分级只影响展示，不剔除样本）
    try:
        quality_report = quality_mod.summarize(list(quality_warnings or []), cfg)
    except Exception as exc:
        quality_report = {"total": 0, "levels": {}, "groups": {}, "items": [],
                          "high_names": [], "error": str(exc)}

    steps = []
    if veto:
        steps.append("今日不开新仓（顶层否决），已有持仓按各自失效位管理")
        steps.extend(market.get("reasons") or [])
    else:
        steps.append("市场闸门通过；逐个标的检查治理门禁（事前验尸+红队质询）后方可建仓")
    if not account_state.get("configured"):
        steps.append("账户真实净值未接入：本页仓位仅为模型建议，未做账户级回撤节流")
    _na = sum(1 for v in per_index.values()
              if (v.get("state") or {}).get("rating") == "NA")
    if _na:
        steps.append(f"{_na} 只标的置信度不足门槛 → 评级 NA：只观察/只持有，不得开新仓")
    _pool_n = sum(1 for v in per_index.values() if (v.get("pool") or {}).get("member"))
    if _pool_n:
        _modes = {(v.get("pool") or {}).get("mode") for v in per_index.values()
                  if (v.get("pool") or {}).get("mode")}
        if "shadow" in _modes:
            steps.append(f"影子观察期（R6 宽缓冲）：今日影子池 {_pool_n} 只，"
                         "仅记录不生效；实际权限、仓位与敞口沿用配置静态白名单，"
                         "观察期结束并复核通过后才可切 dynamic")
        else:
            steps.append(f"可交易池为动态结果（R6 宽缓冲）：今日 {_pool_n} 只，"
                         "进出由缓冲带决定，排名抖动不产生交易")
    if confirmed < tradeable_n:
        steps.append(f"场内标的映射 {confirmed}/{tradeable_n} 已核实；"
                     "未确认项禁止据此下单")
    if alloc is not None:
        steps.append(f"按可用资金换算，当前可执行敞口约 ¥{alloc:,.0f}")
    steps.append(f"概率{min_settled}条结算样本前不作胜率解读")
    ep_label = {
        "blocked_veto": "执行换算：今日闸门否决，下表仅为预案",
        "no_account": "执行换算：账户资金未配置，不给金额/份额",
        "no_instrument": "执行换算：场内标的未核实，不给份额",
        "no_quotes": "执行换算：行情快照不可用，不给份额",
        "ready": "执行换算：已给出金额与整手份额（下单前复核实时价）",
    }.get(execution.get("status"))
    if ep_label:
        steps.append(ep_label)
    if quality_report.get("high_names"):
        steps.append(f"数据口径：{len(quality_report['high_names'])} 只标的为 HIGH 级提示，"
                     "触发位/失效位可靠性下降，详见数据口径披露")
    card = {
        "enabled": bool((cfg.get("decision_card") or {}).get("enabled", True)),
        "execution_status": execution.get("status"),
        "headline": "今日禁止开新仓" if veto else "市场闸门通过 · 仍需逐标的治理门禁",
        "reasons": market.get("reasons") or [],
        "account_label": account_mod.label(account_state),
        "account_configured": bool(account_state.get("configured")),
        "model_exposure": round(float(total_exposure), 4),
        "actionable_exposure": round(actionable, 4),
        "allocation_cny": alloc,
        "hedge": hedge_txt,
        "hedge_retail_actionable": bool((cfg.get("hedge") or {}).get("retail_actionable", False)),
        "instrument_confirmed": confirmed,
        "instrument_tradeable": tradeable_n,
        "calibration_label": str(cal_cfg.get("uncalibrated_label") or "概率未校准"),
        "calibration_min_settled": min_settled,
        "steps": steps,
        "review_at": "下一交易日收盘后（T 日晚间指数落库 + 21:05 兜底重算）",
    }

    result_fields = {
        "account": account_state,
        "market_context": market,
        "actionable_exposure": round(actionable, 4),
        "etf_map": {
            "ok": bool(mapping.get("ok")),
            "count": mapping.get("count", 0),
            "stats": etf_stats,
            "path": mapping.get("path"),
            "reason": mapping.get("reason", ""),
        },
        "governance_gates": gate_results,
        "governance_daily": gov_daily,
        "decision_card": card,
        "execution_plan": execution,
        "quality_report": quality_report,
        "hedge_note": hedge_txt,
        "hedge_retail_actionable": card["hedge_retail_actionable"],
    }
    return {"result_fields": result_fields, "gate_results": gate_results,
            "market": market, "etf_stats": etf_stats, "gov_daily": gov_daily}
