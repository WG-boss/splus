# -*- coding: utf-8 -*-
"""disclosure.py — 口径披露（把「哪些数字不能当证据」写进每份决策输出）

背景：前两轮审计发现的最危险问题不是算错，而是**读数方式错**——
同一个 +0.896%/20 日，在报告里读成"选池有效"，在统计上只是 2026 年
事后选样的产物；同一个 investability=0，可能是"没流动性"，也可能是
"没取到行情"。报告不写清楚，读者一定会读成前者。

本模块做两件事：
  1. 汇总配置里的静态披露（研究结论的边界、回测口径、免责）；
  2. 按当日事实追加动态披露（NA 占比、池容量未知、行情过期、映射快照）。
所有条目带 level（HIGH/MED/LOW）与 id，便于报告筛选与后续引用。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

LEVEL_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}


def _pool_stats(base_dir: str) -> Optional[Dict[str, Any]]:
    """回测口径的 R6 实测值（有则引用，无则不留空话）。"""
    p = os.path.join(base_dir, "state", "pool_dynamic.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    r6 = next((r for r in d.get("nav") or [] if str(r.get("rule", "")).startswith("R6")), None)
    e6 = next((r for r in d.get("rules") or [] if str(r.get("rule", "")).startswith("R6")), None)
    if not r6:
        return None
    return {"as_of": d.get("as_of"), "days": d.get("days"),
            "ann_pct": r6.get("ann_pct"), "sharpe": r6.get("sharpe"),
            "mdd_pct": r6.get("mdd_pct"), "turnover_annual_x": r6.get("turnover_annual_x"),
            "alpha_ann_pct": r6.get("alpha_ann_pct"), "alpha_t": r6.get("alpha_t"),
            "excess_20d_pct": (e6 or {}).get("excess_20d_pct"),
            "excess_t": (e6 or {}).get("t")}


def _state_history_coverage(base_dir: str,
                            cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """主状态逐日历史的覆盖度（只用于披露，不参与计算）。

    state/state_dominant_history.jsonl 被 .gitignore 忽略，属于运行时数据：
    代码回滚/换机/清理缓存后它可能只剩最近几行，而 state_days 是从它回算的。
    历史不足时报告会静默把「E 态已 22 日」写成 8 日，且不报错——
    因此必须由披露层主动喊出来，而不是让读者自己发现。
    """
    state_file = (cfg.get("data") or {}).get("state_file") or "state/last_state.json"
    hist_dir = os.path.dirname(state_file) or "state"
    path = os.path.join(base_dir, hist_dir, "state_dominant_history.jsonl")
    if not os.path.exists(path):
        return None
    first = last = None
    rows = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                d = str(rec.get("date") or "")
                if not d:
                    continue
                first = d if first is None else min(first, d)
                last = d if last is None else max(last, d)
                rows += 1
    except Exception:
        return {"rows": 0, "first": None, "last": None, "unreadable": True}
    return {"rows": rows, "first": first, "last": last, "unreadable": False}


def _quotes_meta(base_dir: str,
                 cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """行情快照的来源元信息：是否走了热备、是否盘中半日量。"""
    fname = ((cfg.get("execution_plan") or {}).get("quotes_file")
             or "data/etf_quotes.json")
    p = os.path.join(base_dir, fname)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return {"fallback": d.get("fallback") or {},
            "intraday": bool(d.get("intraday")),
            "count": d.get("count")}


def build(cfg: Dict[str, Any], result: Dict[str, Any],
          base_dir: str) -> List[Dict[str, Any]]:
    """静态配置披露 + 当日事实披露，按 level 排序返回。"""
    dc = (cfg.get("disclosure") or {})
    if dc.get("enabled") is False:
        return []
    items: List[Dict[str, Any]] = []
    for it in dc.get("items") or []:
        items.append({"id": it.get("id"), "level": str(it.get("level") or "MED"),
                      "kind": "static", "text": it.get("text")})

    per = result.get("per_index") or {}
    n_all = len(per)
    n_trade = sum(1 for v in per.values() if v.get("tradeable"))
    na = [v for v in per.values() if (v.get("state") or {}).get("rating") == "NA"]
    if n_all:
        items.append({
            "id": "d_na", "level": "HIGH", "kind": "dynamic",
            "text": (f"今日 {len(na)}/{n_all} 只（{100.0 * len(na) / n_all:.0f}%）"
                     f"置信度低于门槛 {(cfg.get('state') or {}).get('min_confidence_to_act')}，"
                     "评级输出 NA（无评级）：只能观察/持有，不得据此开新仓。"
                     "这不是「跌」，是「模型自己不确定」。"),
        })

    pool = result.get("pool") or {}
    if pool.get("enabled"):
        cap = pool.get("capacity") or {}
        items.append({
            "id": "d_pool", "level": "MED", "kind": "dynamic",
            "text": (f"可交易池为动态结果（{pool.get('rule')}）："
                     f"可执行宇宙 {pool.get('universe_executable')} → 池内 {len(pool.get('members') or [])} 只，"
                     f"单边换手 {100 * float((pool.get('metrics') or {}).get('turnover_one_way') or 0):.1f}%。"
                     "池成员是「今天买得动且相对强」，不是「明天会涨」；"
                     "进出由缓冲带决定，排名 10↔11 的抖动不产生交易。"),
        })
        if cap.get("quote_stale"):
            items.append({
                "id": "d_quote", "level": "HIGH", "kind": "dynamic",
                "text": (f"ETF 行情快照已过期 {cap.get('quote_age_hours')} 小时："
                         f"{cap.get('unknown')} 只成员的容量（ETF 日成交额）口径为 unknown。"
                         "容量闸已降级为 flag，不代表这些标的真有流动性；下单前必须复核实时盘口。"),
            })
        if pool.get("frozen_today"):
            items.append({
                "id": "d_freeze", "level": "MED", "kind": "dynamic",
                "text": "熔断出池：" + "；".join(pool["frozen_today"].values()),
            })

    # 行情来源护栏（2026-11-09）：主源东财可用时什么都不写；只有走了
    # gtimg 热备或盘中半日量才披露，避免把备源快照读成常规全日行情。
    qm = _quotes_meta(base_dir, cfg)
    if qm and (qm["fallback"].get("used") or qm["intraday"]):
        fb = qm["fallback"]
        bits = []
        if fb.get("used"):
            bits.append(
                f"ETF 行情主源（东方财富）不可用，本快照由腾讯 gtimg 热备生成"
                f"（命中 {fb.get('returned')}/{fb.get('requested')} 只，"
                f"{fb.get('batches')} 批 / 失败 {fb.get('batch_failed')} 批）")
        if qm["intraday"]:
            bits.append("快照取于盘中，成交额是半日量 → 容量闸与建池均跳过，"
                        "池沿用上一收盘结果（未重算/未落盘/换手 0）、不驱动调仓；"
                        "收盘后刷新才恢复全天硬闸，期间不代表全天流动性")
        items.append({
            "id": "d_quote_src", "level": "MED", "kind": "dynamic",
            "text": "；".join(bits) + "。",
        })

    ps = _pool_stats(base_dir)
    if ps:
        items.append({
            "id": "d_bt", "level": "MED", "kind": "dynamic",
            "text": (f"当前池规则的回测口径：{ps.get('days')} 个交易日，年化 {ps.get('ann_pct')}%、"
                     f"Sharpe {ps.get('sharpe')}、最大回撤 {ps.get('mdd_pct')}%、"
                     f"年换手 {ps.get('turnover_annual_x')}x、日频超额 {ps.get('alpha_ann_pct')}%"
                     f"（NW t {ps.get('alpha_t')}）。t < 2 → 方向一致但不显著，"
                     "不得把点估计当作预期收益。"),
        })

    # 历史覆盖不足 → state_days 会静默低估（2026-11-09 复盘：文件被 .gitignore
    # 忽略，代码回滚后只剩 9 行，同一天的 state_days 从 21 变成 9）。
    # 默认关闭，由 disclosure.min_history_rows 打开；正常 750 行时不产生噪音。
    min_rows = int(dc.get("min_history_rows") or 0)
    if min_rows > 0:
        cov = _state_history_coverage(base_dir, cfg) or {
            "rows": 0, "first": None, "last": None, "unreadable": True}
        if cov["rows"] < min_rows:
            span = (f"{cov['first']} → {cov['last']}" if cov.get("first") else "无有效记录")
            items.append({
                "id": "d_state_hist", "level": "HIGH", "kind": "dynamic",
                "text": (f"主状态逐日历史只有 {cov['rows']} 行（{span}，门槛 {min_rows} 行）："
                         "报告里的「状态起始日 / 已持续 N 日」由该历史回算，覆盖不足会"
                         "系统性低估趋势年龄，不得据此判断「刚切换」或「已持续很久」。"
                         "先跑 python tools_state_history_backfill.py --apply（面板回填约 750 日）"
                         "再重跑生产。"
                         + ("当前文件缺失或不可读。" if cov.get("unreadable") else "")),
            })

    lo = result.get("data_lag") or []
    if lo:
        items.append({
            "id": "d_lag", "level": "LOW", "kind": "dynamic",
            "text": (f"{len(lo)} 只标的数据滞后，已截断至统一 as_of "
                     f"{result.get('as_of')} 后重算（不混日期，但当日形态可能失真）。"),
        })

    items.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), str(x.get("id"))))
    return items


def render_md(items: List[Dict[str, Any]], only_high: bool = False) -> List[str]:
    out: List[str] = []
    for it in items:
        if only_high and it.get("level") != "HIGH":
            continue
        out.append(f"- **[{it['level']}]** {it['text']}")
    return out
