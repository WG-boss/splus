# -*- coding: utf-8 -*-
"""S++ 决策 CLI：跑一次，输出 JSON + Markdown 摘要。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine

BASE = os.path.dirname(os.path.abspath(__file__))

PLAYBOOK = {
    "A": "分批吸纳（单批≤1/3目标仓），只在区间上沿突破确认后加仓；下沿放量破位则清出底仓",
    "B": "突破确认后建仓（要求上游链条同向），追价不超上限；3日内不站稳判定假突破并回滚",
    "C": "持有为主，让利润奔跑；用EMA20/通道下轨移动止盈；缩量回调可加仓",
    "D": "主动降仓不低吸；反弹至上沿分批减；禁止以估值便宜作为加仓理由",
    "E": "以观察为主，通道内不抄底；需反向spike+波动收敛+区间形成+首次H2/L2四项齐备才试探",
}


def pct(x, d=1):
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def pp(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def render(res):
    L = []
    L.append(f"# S++ 投研决策 · {res['as_of']}")
    L.append("")
    L.append(f"> 生成 {res['generated_at']} ｜ 配置 v{res['config_version']}")
    L.append("")

    # ---- 0 一页纸决策卡（个人投资参考：今天做/不做）----
    card = res.get("decision_card") or {}
    if card.get("enabled"):
        L.append("## 今日行动卡")
        L.append("")
        L.append(f"> **{card.get('headline')}**")
        L.append("")
        L.append(f"- 账户口径：{card.get('account_label')}")
        alloc = card.get("allocation_cny")
        L.append(f"- 模型建议敞口 {card.get('model_exposure', 0) * 100:.2f}% ｜ "
                 f"可执行敞口 **{card.get('actionable_exposure', 0) * 100:.2f}%**"
                 + (f"（约 ¥{alloc:,.0f}）" if alloc else ""))
        L.append(f"- 场内标的映射：{card.get('instrument_confirmed')}/"
                 f"{card.get('instrument_tradeable')} 已核实；未确认项禁止据此下单")
        L.append(f"- 对冲：{card.get('hedge')}")
        L.append(f"- 概率口径：{card.get('calibration_label')}")
        L.append(f"- 复核时点：{card.get('review_at')}")
        if card.get("reasons"):
            L.append("")
            L.append("否决/提示原因：")
            for r in card["reasons"]:
                L.append(f"  - {r}")
        if card.get("steps"):
            L.append("")
            L.append("今天该做的：")
            for s in card["steps"]:
                L.append(f"  - {s}")
        L.append("")

    # ---- 0 风险开关 ----
    sw = res.get("kill_switches") or []
    if sw:
        L.append("## 生效中的风险开关（警告）")
        for s in sw:
            L.append(f"- **[{s['level']}] {s['id']}** — {s['action']}")
            L.append(f"  - {s['detail']}")
    else:
        L.append("## 无风险开关触发")
    L.append("")

    # ---- 0.1 数据披露 ----
    gaps = res.get("data_gaps") or []
    excl = res.get("data_excluded") or []
    qr = res.get("quality_report") or {}
    if gaps or excl or qr.get("total"):
        L.append("## 数据口径披露")
        for g in gaps:
            L.append(f"- 样本不足，未纳入：{g}")
        for e in excl:
            L.append(f"- 硬异常，已排除：{e}")
        lv = qr.get("levels") or {}
        if qr.get("total"):
            L.append("")
            L.append(f"### 数据软提示分级（共 {qr['total']} 条："
                     f"HIGH {lv.get('HIGH', 0)} ／ MED {lv.get('MED', 0)} "
                     f"／ LOW {lv.get('LOW', 0)}）")
            L.append("")
            L.append(f"> {qr.get('policy')}")
            L.append("")
            if qr.get("high_note"):
                L.append(f"> **HIGH：{qr['high_note']}**")
                L.append("")
            for it in (qr.get("items") or []):
                if it.get("level") == "HIGH":
                    L.append(f"- **[HIGH/{it['label']}] {it['name']}**：{it['text']}")
            low_n = lv.get("MED", 0) + lv.get("LOW", 0)
            L.append("")
            L.append(f"- MED / LOW 共 {low_n} 条按级别折叠于 HTML 报告（不改变样本是否参与计算）")
        L.append("")

    # ---- 0.2 读数纪律（口径披露）：哪些数字不能当证据 ----
    dsc = res.get("disclosures") or []
    if dsc:
        L.append("## 读数纪律（口径披露）")
        L.append("")
        for it in dsc:
            L.append(f"- **[{it.get('level')}]** {it.get('text')}")
        L.append("")

    # ---- 1 状态 ----
    L.append("## 一、状态概率向量（L1）")
    L.append("")
    L.append("| 指数 | 收盘 | 年化波动 | 距52周高 | 主状态 | 评级 | 概率 | 置信 | 期望持续 | 状态权重 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for c, v in res["per_index"].items():
        tag = "" if v.get("tradeable", True) else "（参照）"
        st = v["state"]
        tr = st.get("transition") or {}
        exp = tr.get("expected_duration_days")
        rating = st.get("rating") or st["dominant"]
        L.append(
            f"| {v['name']}{tag} | {v['close']:.1f} | {pp(v['ann_vol_20'])} | "
            f"{pct(v['dd_from_high'])} | {st['dominant_name']} | "
            f"**{rating}** | "
            f"{pp(st['probs'][st['dominant']])} | "
            f"{st['confidence']:.2f} | "
            f"{('%s 日' % exp) if exp else '—'} | {v['state_weight']:.2f} |"
        )
    L.append("")
    _na_n = sum(1 for v in res["per_index"].values()
                if (v["state"].get("rating") or v["state"]["dominant"]) == "NA")
    L.append(f"> 评级 **NA** = 置信度低于门槛，模型自己不确定：只观察/只持有，不得开新仓。"
             f"今日 {_na_n}/{len(res['per_index'])} 只为 NA。"
             f"期望持续来自经验转移矩阵（面板实测，非模型参数）。")
    L.append("")
    L.append("### 概率分布明细")
    L.append("")
    L.append("| 指数 | A吸筹 | B突破 | C通道 | D派发 | E下跌 |")
    L.append("|---|---|---|---|---|---|")
    for c, v in res["per_index"].items():
        p = v["state"]["probs"]
        L.append(f"| {v['name']} | {pp(p['A'])} | {pp(p['B'])} | {pp(p['C'])} | "
                 f"{pp(p['D'])} | {pp(p['E'])} |")
    L.append("")

    # ---- 2 传导链 ----
    L.append("## 二、跨市场传导链（L2）")
    L.append("")
    ch = res.get("chain") or {}
    if ch.get("error"):
        L.append(f"⚠️ {ch['error']}")
    else:
        L.append(f"- 上游锚：**{ch.get('leader')}**（最新 {ch.get('leader_as_of', '—')}）")
        L.append(f"- 上游 20 日 {pct(ch.get('leader_ret20'))}｜60 日 "
                 f"{pct(ch.get('leader_ret60'))}｜距高点 {pct(ch.get('leader_dd'))}")
        L.append("")
        L.append("| 下游 | 最佳滞后 | 领先相关 | 重叠样本 |")
        L.append("|---|---|---|---|")
        for c, v in (ch.get("lags") or {}).items():
            if not v.get("ok"):
                L.append(f"| {v['name']} | — | — | {v.get('reason', '—')} |")
                continue
            L.append(f"| {v['name']} | {v['best_lag_days']} 日 | "
                     f"{v['best_corr']:.3f} | {v['n_overlap']} |")
    L.append("")

    # ---- 3 相关性 ----
    L.append("## 三、相关性（L3 输入）")
    L.append("")
    cor = res.get("correlation") or {}
    if cor.get("ok"):
        L.append(f"- 60 日平均两两相关 **{cor['mean_pairwise']:.2f}**"
                 f"（样本 {cor['n_days']} 日）")
        L.append("")
        names = list(cor["matrix"].keys())
        L.append("| | " + " | ".join(names) + " |")
        L.append("|---" * (len(names) + 1) + "|")
        for a in names:
            L.append(f"| {a} | " + " | ".join(
                f"{cor['matrix'][a][b]:.2f}" for b in names) + " |")
    else:
        L.append(f"⚠️ {cor.get('reason')}")
    L.append("")

    # ---- 3.5 动态可交易池（R6 宽缓冲）----
    pool = res.get("pool") or {}
    if pool.get("enabled"):
        mem = pool.get("members") or []
        L.append("## 三·补 动态可交易池（R6 宽缓冲）")
        L.append("")
        pm = pool.get("params") or {}
        L.append(f"> 规则 **{pool.get('rule')}**：进 {pm.get('size')} / 出 {pm.get('n_out')} / "
                 f"平滑 {pm.get('smooth')} 日 ｜ 排序轴 `{pm.get('rank_axis')}`（不是五态评分）"
                 f" ｜ 相关闸 {pm.get('corr_max')} ｜ AUM {pm.get('aum_yi')} 亿")
        L.append("")
        L.append(f"- 可执行宇宙 **{pool.get('universe_executable')}** 只 → 池内 **{len(mem)}** 只"
                 f"（单边换手 {100 * float((pool.get('metrics') or {}).get('turnover_one_way') or 0):.1f}%）")
        cap = pool.get("capacity") or {}
        if cap.get("quote_stale"):
            L.append(f"- ⚠️ ETF 行情快照过期 {cap.get('quote_age_hours')} 小时 → "
                     f"容量口径 unknown {cap.get('unknown')} 只，不做硬闸，下单前须复核")
        if cap.get("quote_intraday"):
            L.append(f"- ⚠️ ETF 快照取于盘中（半日成交额）：容量闸跳过、池沿用收盘口径、"
                     f"不驱动换仓；容量口径 unknown {cap.get('unknown')} 只，"
                     "收盘后刷新才恢复全天硬闸")
        if pool.get("intraday_hold"):
            L.append(f"- ⏸ 盘中运行：{pool.get('note')}")
        L.append("")
        L.append("| # | 代码 | 名称 | 状态 | 评级 | rs20(5日) | 置信 | 状态动作 | 指数ADV(亿) | 目标仓位 | 入池 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        _zero = []
        for m in mem:
            _v = res["per_index"].get(m.get("code")) or {}
            _fin = float(((_v.get("sizing") or {}).get("final")) or 0.0)
            if _fin <= 0:
                _zero.append(m.get("name"))
            L.append(f"| {m.get('rank')} | {m.get('code')} | {m.get('name')} | {m.get('state')} | "
                     f"{m.get('rating')} | {float(m.get('rs20') or 0) * 100:+.2f}% | "
                     f"{(m.get('conf') if m.get('conf') is not None else float('nan')):.2f} | "
                     f"{m.get('state_action')} | {float(m.get('adv20') or 0) / 1e8:.1f} | "
                     f"{_fin * 100:.1f}% | {m.get('reason')}@{m.get('since')} |")
        L.append("")
        L.append("> 池回答的是「买得动 ∩ 相对强」，仓位回答的是「资格分 × 状态 × 风险预算」——"
                 "两者是不同问题：入池不等于配仓。")
        if _zero:
            L.append(f"> 今日 {len(_zero)}/{len(mem)} 只入池但目标仓位为 0："
                     f"{'、'.join(_zero)}——多为 ETF 日额偏薄（investability 低 → 资格 OBSERVE）"
                     "或 E 态禁新增。它们进池是因为指数层面流动性够，配不了是因为**可交易的 ETF**不够。")
        L.append("")
        if pool.get("exits"):
            L.append("- 出池：" + "、".join(
                f"{e.get('name') or e.get('code')}（{e.get('reason')}）"
                for e in pool["exits"]))
        if pool.get("frozen_today"):
            L.append("- 熔断：" + "；".join(pool["frozen_today"].values()))
        L.append(f"- {pool.get('rationale', '')}")
        L.append("")

    # ---- 4 仓位 ----
    L.append("## 四、仓位建议（L3 三重约束）")
    L.append("")
    L.append("| 指数 | vol上限 | 回撤系数 | 相关系数 | 状态权重 | 最终仓位 |")
    L.append("|---|---|---|---|---|---|")
    for c, v in res["per_index"].items():
        s = v["sizing"]
        if not v.get("tradeable", True):
            L.append(f"| {v['name']}（参照） | — | — | — | {s['state_weight']:.2f} | 不计仓 |")
            continue
        L.append(f"| {v['name']} | {pp(s['cap_vol'])} | {s['coef_dd']:.2f} | "
                 f"{s['coef_corr']:.2f} | {s['state_weight']:.2f} | "
                 f"**{s['final'] * 100:.1f}%** |")
    L.append("")

    L.append("### 按桶合并（同桶视为一个仓位）")
    L.append("")
    L.append("| 桶 | 标的 | 压缩前 | **压缩后** | 说明 |")
    L.append("|---|---|---|---|---|")
    for b, v in res["buckets"].items():
        L.append(f"| {b} | {', '.join(v['codes'])} | "
                 f"{v.get('exposure_pre_switch', v['exposure']) * 100:.1f}% | "
                 f"**{v['exposure'] * 100:.1f}%** | {v['note']}"
                 f"{'；' + v['cap_reason'] if v.get('cap_reason') else ''} |")
    L.append("")
    L.append(f"**合计建议敞口：{res['total_exposure'] * 100:.1f}%**")
    L.append(f"（开关压缩 {res.get('total_exposure_pre_switch', 0) * 100:.1f}% → "
             f"{res.get('total_exposure', 0) * 100:.1f}%，系数 "
             f"{res.get('switch_haircut', 1.0):.2f}，触发 "
             f"{', '.join(res.get('switch_haircut_applied') or ['无'])}）")
    L.append("")
    L.append(f"**对冲建议：** {res.get('hedge_note')}")
    L.append("")

    # ---- 5 执行 ----
    L.append("## 五、执行层（L4）")
    L.append("")
    L.append("> 设计说明：在 E 阶段下所有价格均低于触发位，执行层的作用是"
             "**明确“涨到什么位置才允许动手”**，避免在下跌通道中提前进场。")
    L.append("")
    L.append("| 指数 | 触发位 | 追价上限 | 失效位 | 距触发位 | 反弹资格 | 单笔风险 |")
    L.append("|---|---|---|---|---|---|---|")
    for c, v in res["per_index"].items():
        if not v.get("tradeable", True):
            continue
        e = v.get("execution") or {}
        rb = e.get("rebound") or {}
        dist = None
        if e.get("trigger_price") and v.get("close"):
            dist = e["trigger_price"] / v["close"] - 1
        if rb.get("active"):
            rb_txt = f"[可参与] 战术参与（上限 {e.get('rebound_allowance', 0)*100:.0f}%）"
        else:
            rb_txt = f"[不可] 下跌中继（{rb.get('score', 0)}/4 项）"
        L.append(f"| {v['name']} | {e.get('trigger_price') or '—'} | "
                 f"{e.get('chase_cap_price') or '—'}（{e.get('chase_atr_mult')} ATR） | "
                 f"{e.get('fail_price') or '—'} | {pct(dist)} | {rb_txt} | "
                 f"{e.get('single_trade_risk_nav', 0) * 100:.1f}% NAV |")
    L.append("")

    # ---- 5·补 执行换算（P1-① 个人下单参考）----
    ep = res.get("execution_plan") or {}
    if ep.get("enabled") and ep.get("rows"):
        def fm(x):
            return "—" if x is None else f"{x:,.2f}"
        L.append("## 五·补 执行换算（个人下单参考）")
        L.append("")
        L.append(f"> **{ep.get('mode') or '—'}** ｜ 状态 `{ep.get('status') or '—'}` ｜ "
                 f"基数 {ep.get('capital_base')} ｜ 最小 {ep.get('lot_size')} 份/手")
        L.append("")
        for wn in ep.get("warnings") or []:
            L.append(f"- ⚠️ {wn}")
        L.append("")
        L.append("| 指数 | 场内标的 | 预案权重 | 预案金额(元) | 现价 | "
                 "整手份额 | 实际金额(元) | 费用(元) | 状态 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in ep["rows"]:
            etf = r.get("etf_code") or "未核实"
            status = ("✅ 可下单" if r.get("executable")
                      else "⛔ " + "；".join(r.get("reasons") or []))
            shares = r.get("shares") if r.get("shares") is not None else "—"
            L.append(f"| {r['name']} | {etf} {r.get('etf_name') or ''} | "
                     f"{r['target_weight'] * 100:.2f}% | {fm(r.get('target_cny'))} | "
                     f"{fm(r.get('price'))} | {shares} | {fm(r.get('actual_cny'))} | "
                     f"{fm(r.get('fee_cny'))} | {status} |")
        s = ep.get("summary") or {}
        L.append("")
        L.append(f"- 合计：预案 {fm(s.get('target_total_cny'))} 元 ／ 实际 "
                 f"{fm(s.get('actual_total_cny'))} 元 ／ 费用 {fm(s.get('fee_total_cny'))} 元；"
                 f"可下单 {s.get('executable', 0)}/{s.get('tradeable', 0)} 只")
        q = ep.get("quotes") or {}
        L.append(f"- 报价快照：{q.get('count', 0)} 只（{q.get('fetched_at') or '—'}）；"
                 f"{ep.get('disclaimer')}")
        L.append("")

    # ---- 6 行为矩阵 ----
    L.append("## 六、阶段行为矩阵（动作指令）")
    L.append("")
    L.append("| 指数 | 主状态 | 置信 | 开仓许可 | 行为指令 |")
    L.append("|---|---|---|---|---|")
    thr = res.get("min_confidence_to_act", 0.65)
    for c, v in res["per_index"].items():
        if not v.get("tradeable", True):
            continue
        st = v["state"]
        conf_ok = st["confidence"] >= thr
        state_ok = st["dominant"] in ("A", "B", "C")
        if conf_ok and state_ok:
            gate = "允许开仓"
            play = PLAYBOOK.get(st["dominant"], "—")
        elif conf_ok and not state_ok:
            gate = "置信达标但状态禁新仓"
            play = PLAYBOOK.get(st["dominant"], "—")
        else:
            # 置信不达门槛时，行为指令不能再说“分批吸纳”。
            # 否则同一行里门禁说禁止开仓、动作说买入，是自相矛盾的输出——
            # 这类矛盾会被执行层误读为“可以干”，是比不输出更危险的失败模式。
            gate = "置信不足"
            play = (f"只观察不动手（置信 {st['confidence']:.2f} < 门槛 {thr:.2f}）；"
                    f"若{st['dominant_name']}概率升至门槛以上再执行“{PLAYBOOK.get(st['dominant'], '—')}”")
        L.append(f"| {v['name']} | {st['dominant_name']} | {st['confidence']:.2f} | "
                 f"{gate} | {play} |")
    L.append("")

    # ---- 7 元层 ----
    L.append("## 七、元层台账（L6）")
    L.append("")
    cal = res.get("calibration") or {}
    L.append(f"- 概率校准：{cal.get('status', '台账未建立')}")
    L.append(f"- 前瞻窗口 {cal.get('horizon_days', '—')} 交易日；"
             f"Brier 阈值 {cal.get('brier_alert', '—')}")
    L.append(f"- 已结算样本 {cal.get('settled_n', 0)} 条；本次新结算 "
             f"{cal.get('newly_settled', 0)} 条")
    L.append(f"- 预测台账：{res.get('_paths', {}).get('ledger', '—')}")
    card = res.get("decision_card") or {}
    L.append(f"- 概率口径：{card.get('calibration_label', '台账未建立')}"
             f"（{card.get('calibration_status', '—')}）")
    gp = res.get("governance") or {}
    L.append(f"- 治理台账：日级汇总 {gp.get('daily_records', 0)} 条，"
             f"本次开仓意向 {gp.get('checked_today', 0)} 个、通过 "
             f"{gp.get('allowed_today', 0)} 个")
    mc = res.get("market_context") or {}
    L.append(f"- 市场闸门：{mc.get('label', '—')}")
    em = res.get("etf_map") or {}
    L.append(f"- ETF 映射：{em.get('count', 0)} 只全覆盖，"
             f"已核实 {(em.get('stats') or {}).get('confirmed', 0)} 只，"
             f"候选 {(em.get('stats') or {}).get('candidates', 0)} 只")
    sp = res.get("settlement_progress") or {}
    L.append(f"- 结算进度：{sp.get('label', '—')}；已结算 {sp.get('settled_n', 0)}/"
             f"{sp.get('min_settled', 30)} 条，待结算 {sp.get('pending_n', 0)} 条；"
             f"最早一批还需约 {sp.get('next_remaining_days', '—')} 个交易日"
             f"（≈{sp.get('next_est_weeks', '—')} 周）")
    L.append(f"> {sp.get('note', '')}")
    ar = res.get("attribution_rebuild") or {}
    L.append(f"- 归因台账：{ar.get('total_records', 0)} 条（本次新写 "
             f"{ar.get('written', 0)} 条，基准 {ar.get('benchmark_code', '—')}）；"
             f"{ar.get('reason') or ar.get('note') or ''}")
    ep = res.get("execution_plan") or {}
    L.append(f"- 执行换算：{ep.get('status', '—')} ｜ "
             f"可下单 {(ep.get('summary') or {}).get('executable', 0)}/"
             f"{(ep.get('summary') or {}).get('tradeable', 0)} 只")
    L.append("")

    L.append("---")
    L.append("*由 S++ 引擎按外置配置自动生成。所有阈值均为可调参数，不构成买卖建议。"
             "仓位为模型建议比例而非下单指令；指数点位与场内 ETF 代码须自行核实，"
             "未确认的候选标的一律不得据此下单。概率未经校准，不得解读为胜率。"
             "执行换算在闸门否决/账户未配置/标的未核实时一律为「预案」，不得据此下单；"
             "报价为快照，下单前以券商实时价为准。*")
    return "\n".join(L)


def _report_html_enabled(cfg_path):
    """从配置读 output.report_html；配置缺失时默认开启。"""
    try:
        c = engine.load_config(cfg_path)
        return bool((c.get("output") or {}).get("report_html", True))
    except Exception:
        return True


def main():
    cfg_path = os.path.join(BASE, "splus_config.yaml")
    res = engine.run(cfg_path, BASE)
    out_dir = os.path.join(BASE, "data")
    os.makedirs(out_dir, exist_ok=True)
    # B2 P3 生命周期评估已移入 engine.run（决策 JSON 落盘前），res["lifecycle"] 直接可用。

    md_path = os.path.join(out_dir, f"splus_report_{res['as_of'].replace('-', '')}.md")

    md = render(res)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    # HTML 报告（配置 output.report_html 控制；渲染失败不得阻断主流程）
    html_path = None
    if _report_html_enabled(cfg_path):
        try:
            import render_html
            html_path = render_html.write_html(res, out_dir, res["as_of"])
        except Exception as exc:
            print(f"[warn] HTML 渲染失败（Markdown 已生成）：{exc}", file=sys.stderr)

    # B2 仓位执行闭环交互看板（配置 output.dashboard_html 控制；默认开启；失败不阻断主流程）
    dash_path = None
    try:
        c = engine.load_config(cfg_path)
        if bool((c.get("output") or {}).get("dashboard_html", True)):
            import tools_position_dashboard as _dash
            dash_path = os.path.join(out_dir, "position_dashboard.html")
            with open(dash_path, "w", encoding="utf-8") as fh:
                fh.write(_dash.render_html(_dash.build_payload(res["_paths"]["decision"], c)))
            print("DASH:", dash_path)
    except Exception as exc:
        print(f"[warn] 交互看板生成失败（不影响主流程）：{exc}", file=sys.stderr)

    print(md)
    print()
    print("---")
    print("JSON:", res["_paths"]["decision"])
    print("MD  :", md_path)
    if html_path:
        print("HTML:", html_path)


if __name__ == "__main__":
    main()
