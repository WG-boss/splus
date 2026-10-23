# -*- coding: utf-8 -*-
"""
render_html.py —— 把 engine.run() 的结果渲染成单文件 HTML 报告

设计取舍：
  1. 不解析 Markdown，直接读 res 对象。解析 Markdown 反过来生成 HTML
     是最容易出错的做法（表格/转义/嵌套），而且两边内容会漂移。
  2. 零外部依赖，CSS 内联，双击即开，不联网。
  3. 加一列概率条形图 —— 终端里看不到的信息，HTML 才有意义。
  4. 所有数值来自 res，本模块不含任何决策阈值（不做第二套逻辑）。
"""
from __future__ import annotations

import html
import os

PLAYBOOK = {
    "A": "分批吸纳（单批≤1/3目标仓），只在区间上沿突破确认后加仓；下沿放量破位则清出底仓",
    "B": "突破确认后建仓（要求上游链条同向），追价不超上限；3日内不站稳判定假突破并回滚",
    "C": "持有为主，让利润奔跑；用EMA20/通道下轨移动止盈；缩量回调可加仓",
    "D": "主动降仓不低吸；反弹至上沿分批减；禁止以估值便宜作为加仓理由",
    "E": "以观察为主，通道内不抄底；需反向spike+波动收敛+区间形成+首次H2/L2四项齐备才试探",
}

CSS = """
:root{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --tx:#e6e9ef;
  --dim:#8b93a3; --red:#e5534b; --grn:#3fb950; --amb:#d29922; --blu:#4c8ef7;
}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--tx);
  font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
h2{font-size:17px;margin:34px 0 12px;padding-left:10px;
  border-left:3px solid var(--blu);letter-spacing:.3px}
h3{font-size:14px;color:var(--dim);margin:18px 0 8px;font-weight:600}
table{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:13px}
th{background:#1e232c;color:#aab2c0;font-weight:600;text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #1d222a;vertical-align:middle}
tr:hover td{background:#1a1e26}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:14px 0}
.kpi{display:flex;flex-wrap:wrap;gap:14px;margin:16px 0}
.kpi>div{flex:1 1 170px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:14px 16px}
.kpi .lbl{color:var(--dim);font-size:12px;margin-bottom:6px}
.kpi .val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.pos{color:var(--grn)} .neg{color:var(--red)} .warn{color:var(--amb)}
.sw{border-left:3px solid var(--red);background:#241a1a;padding:10px 14px;
  border-radius:6px;margin:8px 0}
.sw .lv{font-weight:700;color:var(--red);margin-right:6px}
.note{color:var(--dim);font-size:12px;margin-top:6px}
.bar{display:flex;height:16px;border-radius:4px;overflow:hidden;
  background:#11141a;min-width:120px}
.bar span{display:block;height:100%}
.ba{background:#4c8ef7}.bb{background:#3fb950}.bc{background:#26a69a}
.bd{background:#d29922}.be{background:#e5534b}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin:6px 0 14px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;
  border:1px solid var(--line);color:var(--dim)}
.foot{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12px}
code{background:#1e232c;padding:1px 5px;border-radius:3px;font-size:12px}
"""


def _e(x):
    return html.escape(str(x)) if x is not None else "—"


def _pct(x, d=1):
    if x is None:
        return "—"
    return f"{x * 100:+.{d}f}%"


def _pp(x):
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def _pnl_cls(x):
    if x is None:
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def _probbar(p):
    """五状态概率条形图。"""
    order = [("A", "ba"), ("B", "bb"), ("C", "bc"), ("D", "bd"), ("E", "be")]
    seg = []
    for k, cls in order:
        v = (p.get(k) or 0) * 100
        if v <= 0.5:
            continue
        seg.append(f'<span class="{cls}" style="width:{v:.2f}%" '
                   f'title="{k}: {v:.1f}%"></span>')
    return f'<div class="bar">{"".join(seg)}</div>'


def _validate(res):
    """
    渲染前必须校验输入完整性。
    为什么：残缺的 res 会渲染出一份“表格齐全但内容全空”的报告，
    看起来正常、实际无信息，比直接报错危险得多。
    """
    if not isinstance(res, dict):
        raise TypeError("res 必须是 dict")
    if not res.get("as_of"):
        raise ValueError("res.as_of 为空，无法确定报告日期")
    pi = res.get("per_index")
    if not pi or not isinstance(pi, dict):
        raise ValueError("res.per_index 为空，无可渲染标的")
    for code, v in pi.items():
        for k in ("name", "state", "close"):
            if k not in v:
                raise ValueError(f"标的 {code} 缺少必备字段 {k}")
        st = v["state"]
        for k in ("dominant", "dominant_name", "probs", "confidence"):
            if k not in st:
                raise ValueError(f"标的 {code} 的 state 缺少 {k}")
    if not res.get("portfolio_dd"):
        raise ValueError("res.portfolio_dd 缺失（回撤层未运行，不应出报告）")


def render_html(res):
    _validate(res)
    L = []
    a = L.append
    a('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    a(f'<title>S++ 投研决策 · {_e(res.get("as_of"))}</title>')
    a(f"<style>{CSS}</style></head><body><div class='wrap'>")

    # ---- 标题 ----
    a(f'<h1>S++ 投研决策 · {_e(res.get("as_of"))}</h1>')
    a(f'<div class="sub">生成 {_e(res.get("generated_at"))} ｜ '
      f'配置 v{_e(res.get("config_version"))} ｜ 统一口径：'
      f'{_e(res.get("as_of_rule", "—"))}</div>')

    # ---- 风险开关 ----
    sw = res.get("kill_switches") or []
    a("<h2>生效中的风险开关</h2>")
    if sw:
        for s in sw:
            a(f'<div class="sw"><span class="lv">[{_e(s.get("level"))}] '
              f'{_e(s.get("id"))}</span>{_e(s.get("action"))}'
              f'<div class="note">{_e(s.get("detail"))}</div></div>')
    else:
        a('<div class="card">无风险开关触发</div>')

    # ---- 数据披露 ----
    gaps = res.get("data_gaps") or []
    excl = res.get("data_excluded") or []
    warns = res.get("quality_warnings") or []
    lag = res.get("data_lag") or []
    if gaps or excl or warns or lag:
        a("<h2>数据口径披露</h2><div class='card'>")
        for g in gaps:
            a(f"<div class='warn'>样本不足，未纳入：{_e(g)}</div>")
        for e in excl:
            a(f"<div class='neg'>硬异常，已排除：{_e(e)}</div>")
        for w in warns:
            a(f"<div class='warn'>软提示（保留使用）：{_e(w)}</div>")
        for x in lag:
            a(f"<div class='note'>滞后对齐：{_e(x.get('name'))} "
              f"{_e(x.get('had'))} → {_e(x.get('used'))}</div>")
        a("</div>")

    # ---- KPI ----
    dd = (res.get("portfolio_dd") or {})
    a('<div class="kpi">')
    a(f'<div><div class="lbl">组合回撤（自动计算）</div>'
      f'<div class="val neg">{_pct(dd.get("dd"))}</div>'
      f'<div class="note">{_e(dd.get("source"))}</div></div>')
    a(f'<div><div class="lbl">合计建议敞口</div>'
      f'<div class="val">{res.get("total_exposure", 0) * 100:.2f}%</div>'
      f'<div class="note">压缩前 {res.get("total_exposure_pre_switch", 0) * 100:.2f}%'
      f'（系数 {res.get("switch_haircut", 1):.2f}）</div></div>')
    a(f'<div><div class="lbl">对冲建议</div>'
      f'<div class="val warn">{res.get("hedge_need", 0) * 100:.0f}%</div>'
      f'<div class="note">beta 敞口</div></div>')
    n_e = sum(1 for v in (res.get("per_index") or {}).values()
              if v.get("tradeable") and v["state"]["dominant"] == "E")
    n_t = sum(1 for v in (res.get("per_index") or {}).values() if v.get("tradeable"))
    a(f'<div><div class="lbl">可交易标的处 E 阶段</div>'
      f'<div class="val neg">{n_e}/{n_t}</div>'
      f'<div class="note">下跌/衰退</div></div>')
    a("</div>")

    # ---- L1 ----
    a("<h2>一、状态概率向量（L1）</h2>")
    a('<div class="legend"><span><i class="ba"></i>A 吸筹</span>'
      '<span><i class="bb"></i>B 突破</span><span><i class="bc"></i>C 通道</span>'
      '<span><i class="bd"></i>D 派发</span><span><i class="be"></i>E 下跌</span></div>')
    a("<table><tr><th>指数</th><th>收盘</th><th>年化波动</th><th>距52周高</th>"
      "<th>主状态</th><th>概率分布</th><th>置信</th><th>状态权重</th><th>仓位</th></tr>")
    for c, v in (res.get("per_index") or {}).items():
        st = v["state"]
        tag = "" if v.get("tradeable", True) else " <span class='tag'>参照</span>"
        pos = (v.get("sizing") or {}).get("final")
        pos_td = (f"{pos * 100:.2f}%" if v.get("tradeable", True) else "—")
        a(f"<tr><td>{_e(v['name'])}{tag}</td>"
          f"<td class='num'>{v['close']:.1f}</td>"
          f"<td class='num'>{_pp(v.get('ann_vol_20'))}</td>"
          f"<td class='num neg'>{_pct(v.get('dd_from_high'))}</td>"
          f"<td>{_e(st['dominant_name'])}</td>"
          f"<td>{_probbar(st['probs'])}</td>"
          f"<td class='num'>{st['confidence']:.2f}</td>"
          f"<td class='num'>{v['state_weight']:.2f}</td>"
          f"<td class='num'><b>{pos_td}</b></td></tr>")
    a("</table>")

    # ---- L2 ----
    ch = res.get("chain") or {}
    a("<h2>二、跨市场传导链（L2）</h2>")
    if ch.get("error"):
        a(f'<div class="card warn">{_e(ch["error"])}</div>')
    else:
        a('<div class="card">')
        a(f'<div>上游锚：<b>{_e(ch.get("leader"))}</b>'
          f'（最新 {_e(ch.get("leader_as_of", "—"))}）</div>')
        a(f'<div class="note">上游 20 日 {_pct(ch.get("leader_ret20"))}｜'
          f'60 日 {_pct(ch.get("leader_ret60"))}｜'
          f'距高点 {_pct(ch.get("leader_dd"))}</div></div>')
        a("<table><tr><th>下游</th><th>最佳滞后</th><th>领先相关</th>"
          "<th>重叠样本</th></tr>")
        for c, v in (ch.get("lags") or {}).items():
            if not v.get("ok"):
                a(f"<tr><td>{_e(v.get('name'))}</td><td>—</td><td>—</td>"
                  f"<td class='note'>{_e(v.get('reason', '—'))}</td></tr>")
                continue
            a(f"<tr><td>{_e(v['name'])}</td><td class='num'>{v['best_lag_days']} 日</td>"
              f"<td class='num'>{v['best_corr']:.3f}</td>"
              f"<td class='num'>{v['n_overlap']}</td></tr>")
        a("</table>")

    # ---- 相关性 ----
    cor = res.get("correlation") or {}
    a("<h2>三、相关性（L3 输入）</h2>")
    if cor.get("ok"):
        a(f'<div class="note">60 日平均两两相关 '
          f'<b>{cor["mean_pairwise"]:.2f}</b>（样本 {cor["n_days"]} 日）</div>')
        names = list(cor["matrix"].keys())
        # 代码 → 中文名（直接展示 000688/hkHSTECH 极难阅读）
        nm = {c: (v.get("name") or c)
              for c, v in (res.get("per_index") or {}).items()}
        a("<table><tr><th></th>"
          + "".join(f"<th>{_e(nm.get(n, n))}</th>" for n in names) + "</tr>")
        for x in names:
            a(f"<tr><td>{_e(nm.get(x, x))}</td>" + "".join(
                f"<td class='num'>{cor['matrix'][x][y]:.2f}</td>" for y in names)
              + "</tr>")
        a("</table>")
    else:
        a(f'<div class="card warn">{_e(cor.get("reason"))}</div>')

    # ---- 仓位 ----
    a("<h2>四、仓位建议（L3 四层约束取最小）</h2>")
    a("<table><tr><th>指数</th><th>vol 上限</th><th>回撤系数</th><th>相关惩罚</th>"
      "<th>状态权重</th><th>生效约束</th><th>最终仓位</th></tr>")
    for c, v in (res.get("per_index") or {}).items():
        s = v["sizing"]
        if not v.get("tradeable", True):
            a(f"<tr><td>{_e(v['name'])} <span class='tag'>参照</span></td>"
              f"<td>—</td><td>—</td><td>—</td>"
              f"<td class='num'>{s['state_weight']:.2f}</td>"
              f"<td colspan='2'>不计仓位</td></tr>")
            continue
        bind = s.get("binding") or "—"
        a(f"<tr><td>{_e(v['name'])}</td>"
          f"<td class='num'>{_pp(s['cap_vol'])}</td>"
          f"<td class='num'>{s['coef_dd']:.2f}</td>"
          f"<td class='num'>{s['coef_corr']:.2f}</td>"
          f"<td class='num'>{s['state_weight']:.2f}</td>"
          f"<td><span class='tag'>{_e(bind)}</span></td>"
          f"<td class='num'><b>{s['final'] * 100:.2f}%</b></td></tr>")
    a("</table>")

    a("<h3>按桶合并（同桶视为一个仓位）</h3>")
    a("<table><tr><th>桶</th><th>标的</th><th>压缩前</th><th>压缩后</th><th>说明</th></tr>")
    for b, v in (res.get("buckets") or {}).items():
        a(f"<tr><td>{_e(b)}</td><td>{_e(', '.join(v['codes']))}</td>"
          f"<td class='num'>{v.get('exposure_pre_switch', v['exposure']) * 100:.2f}%</td>"
          f"<td class='num'><b>{v['exposure'] * 100:.2f}%</b></td>"
          f"<td class='note'>{_e(v['note'])}"
          f"{_e('（' + v['cap_reason'] + '）') if v.get('cap_reason') else ''}</td></tr>")
    a("</table>")

    # ---- 执行层 ----
    a("<h2>五、执行层（L4）</h2>")
    a('<div class="note">在 E 阶段下所有价格均低于触发位，执行层的作用是'
      '明确「涨到什么位置才允许动手」，避免在下跌通道中提前进场。</div>')
    a("<table><tr><th>指数</th><th>触发位</th><th>追价上限</th><th>失效位</th>"
      "<th>距触发位</th><th>反弹资格</th><th>单笔风险</th></tr>")
    for c, v in (res.get("per_index") or {}).items():
        if not v.get("tradeable", True):
            continue
        e = v.get("execution") or {}
        rb = e.get("rebound") or {}
        dist = None
        if e.get("trigger_price") and v.get("close"):
            dist = e["trigger_price"] / v["close"] - 1
        if rb.get("active"):
            rb_txt = (f'<span class="pos">[可参与] 战术参与 '
                      f'上限 {e.get("rebound_allowance", 0) * 100:.0f}%</span>')
        else:
            rb_txt = (f'<span class="dim">[不可] 下跌中继 '
                      f'（{rb.get("score", 0)}/4 项）</span>')
        a(f"<tr><td>{_e(v['name'])}</td>"
          f"<td class='num'>{_e(e.get('trigger_price'))}</td>"
          f"<td class='num'>{_e(e.get('chase_cap_price'))}"
          f"（{_e(e.get('chase_atr_mult'))} ATR）</td>"
          f"<td class='num'>{_e(e.get('fail_price'))}</td>"
          f"<td class='num warn'>{_pct(dist)}</td>"
          f"<td>{rb_txt}</td>"
          f"<td class='num'>{e.get('single_trade_risk_nav', 0) * 100:.1f}% NAV</td></tr>")
    a("</table>")

    # ---- 行为矩阵 ----
    a("<h2>六、阶段行为矩阵（动作指令）</h2>")
    thr = res.get("min_confidence_to_act", 0.65)
    a("<table><tr><th>指数</th><th>主状态</th><th>置信</th><th>开仓许可</th>"
      "<th>行为指令</th></tr>")
    for c, v in (res.get("per_index") or {}).items():
        if not v.get("tradeable", True):
            continue
        st = v["state"]
        conf_ok = st["confidence"] >= thr
        state_ok = st["dominant"] in ("A", "B", "C")
        if conf_ok and state_ok:
            gate = '<span class="pos">允许开仓</span>'
        elif conf_ok and not state_ok:
            gate = '<span class="warn">置信达标但状态禁新仓</span>'
        else:
            gate = '<span class="neg">置信不足</span>'
        a(f"<tr><td>{_e(v['name'])}</td><td>{_e(st['dominant_name'])}</td>"
          f"<td class='num'>{st['confidence']:.2f}</td><td>{gate}</td>"
          f"<td class='note'>{_e(PLAYBOOK.get(st['dominant'], '—'))}</td></tr>")
    a("</table>")

    # ---- 元层 ----
    cal = res.get("calibration") or {}
    a("<h2>七、元层台账（L6）</h2><div class='card'>")
    a(f"<div>概率校准：{_e(cal.get('status', '台账未建立'))}</div>")
    a(f"<div class='note'>前瞻窗口 {_e(cal.get('horizon_days', '—'))} 交易日；"
      f"Brier 阈值 {_e(cal.get('brier_alert', '—'))}；"
      f"已结算 {cal.get('settled_n', 0)} 条，本次新结算 {cal.get('newly_settled', 0)} 条</div>")
    gp = (res.get("governance") or {})
    a(f"<div class='note'>治理门禁：premortem "
      f"{'必需' if gp.get('require_premortem') else '免'} ／ redteam "
      f"{'必需' if gp.get('require_redteam') else '免'}</div>")
    a("</div>")

    a('<div class="foot">由 S++ 引擎按外置配置自动生成。'
      '所有阈值均为可调参数，不构成买卖建议。'
      '本文件单文件自包含，无外部依赖。</div>')
    a("</div></body></html>")
    return "\n".join(L)


def write_html(res, out_dir, as_of):
    os.makedirs(out_dir, exist_ok=True)
    name = f"splus_report_{as_of.replace('-', '')}.html"
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(res))
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import engine
    BASE = os.path.dirname(os.path.abspath(__file__))
    r = engine.run(os.path.join(BASE, "splus_config.yaml"), BASE)
    p = write_html(r, os.path.join(BASE, "data"), r["as_of"])
    print("HTML:", p)
