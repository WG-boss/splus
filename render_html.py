# -*- coding: utf-8 -*-
"""
render_html.py —— 把 engine.run() 的结果渲染成单文件 HTML 报告

设计取舍：
  1. 不解析 Markdown，直接读 res 对象。解析 Markdown 反过来生成 HTML
     是最容易出错的做法（表格/转义/嵌套），而且两边内容会漂移。
  2. 零外部依赖，CSS 内联，双击即开，不联网。
  3. 加一列概率条形图 —— 终端里看不到的信息，HTML 才有意义。
  4. 所有数值来自 res，本模块不含任何决策阈值（不做第二套逻辑）。
  5. 分层渲染（2026-10-29）：可交易标的主表优先，观察池（watch/ref/lead）
     折叠展示 —— 106 只观察池全量铺表曾把相关性矩阵撑到 118×118（450KB），
     可读性与体积双输。渲染层只做「呈现分层」，不做任何决策过滤。
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

STATE_ORDER = ["A", "B", "C", "D", "E"]
STATE_FULL = {"A": "A 吸筹", "B": "B 突破", "C": "C 通道", "D": "D 派发", "E": "E 下跌"}

CSS = """
:root{
  --bg:#f0f7f0; --card:#ffffff; --line:#d4e4d4; --tx:#1a2820;
  --dim:#5a7a5e; --red:#c0392b; --grn:#27ae60; --amb:#b8860b; --blu:#2980b9;
}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--tx);
  font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px;margin-bottom:14px}
h2{font-size:17px;margin:34px 0 12px;padding-left:10px;
  border-left:3px solid var(--blu);letter-spacing:.3px}
h3{font-size:14px;color:var(--dim);margin:18px 0 8px;font-weight:600}
table{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:13px}
th{background:#e8f0e8;color:#3a5a40;font-weight:600;text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #e0ece0;vertical-align:middle}
tr:hover td{background:#eaf5ea}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.badge{display:inline-block;padding:0 7px;border-radius:9px;font-size:11px;
  font-weight:700;line-height:18px;vertical-align:middle;margin-left:4px}
.badge.tg{background:#e3f2e8;color:#1e8449}
.badge.tb{background:#e3edf8;color:#1a5fb4}
.badge.tm{background:#eef1f4;color:#5a6b7f}
.badge.tr{background:#fbe9e7;color:#c0392b}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:14px 0}
.dcard{background:var(--card);border:1px solid var(--line);
  border-left:5px solid var(--red);border-radius:10px;padding:16px 20px;margin:16px 0}
.dcard.ok{border-left-color:var(--grn)}
.dcard h2{margin:0 0 8px;border:0;padding:0;font-size:19px}
.dcard ul{margin:8px 0 0;padding-left:20px}
.dcard li{margin:3px 0}
.dcard .kv{display:flex;flex-wrap:wrap;gap:8px 26px;font-size:13px;margin:8px 0}
.dcard .kv b{font-variant-numeric:tabular-nums}
.banner{display:flex;flex-wrap:wrap;gap:22px;background:var(--card);
  border:1px solid var(--line);border-left:4px solid var(--blu);
  border-radius:8px;padding:12px 18px;margin:12px 0 4px;font-size:13px}
.banner b{font-size:16px}
.banner .bl{color:var(--dim);font-size:12px}
.kpi{display:flex;flex-wrap:wrap;gap:14px;margin:16px 0}
.kpi>div{flex:1 1 170px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:14px 16px}
.kpi .lbl{color:var(--dim);font-size:12px;margin-bottom:6px}
.kpi .val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.pos{color:var(--grn)} .neg{color:var(--red)} .warn{color:var(--amb)}
.sw{border-left:3px solid var(--red);background:#fdf0ee;padding:10px 14px;
  border-radius:6px;margin:8px 0}
.sw .lv{font-weight:700;color:var(--red);margin-right:6px}
.note{color:var(--dim);font-size:12px;margin-top:6px}
.bar{display:flex;height:16px;border-radius:4px;overflow:hidden;
  background:#e8f0e8;min-width:120px}
.bar span{display:block;height:100%}
.ba{background:#4c8ef7}.bb{background:#3fb950}.bc{background:#26a69a}
.bd{background:#d29922}.be{background:#e5534b}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin:6px 0 14px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;
  border:1px solid var(--line);color:var(--dim)}
details.fold{border:1px solid var(--line);border-radius:8px;background:var(--card);
  margin:10px 0;padding:0}
details.fold>summary{cursor:pointer;padding:10px 14px;color:var(--dim);
  font-size:13px;user-select:none;list-style:none}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::before{content:"\\25B8 ";color:var(--blu)}
details.fold[open]>summary::before{content:"\\25BE "}
details.fold[open]>summary{border-bottom:1px solid #e0ece0}
details.fold .inner{padding:4px 14px 12px}
.foot{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12px}
code{background:#e8f0e8;padding:1px 5px;border-radius:3px;font-size:12px}
.qtag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600;
  margin-right:6px;white-space:nowrap}
.qhigh{background:#fdecea;color:var(--red);border:1px solid #f0c0b8}
.qmed{background:#fdf6e3;color:var(--amb);border:1px solid #ead9a0}
.qlow{background:#eaf5ea;color:var(--dim);border:1px solid var(--line)}
.plan-note{background:#fdf0ee;border-left:3px solid var(--red);padding:10px 14px;
  border-radius:6px;margin:10px 0;font-size:13px}
/* 表头排序（点击循环 升序→降序；▲▼ 指示当前方向）*/
th.srt{cursor:pointer;user-select:none}
th.srt:hover{background:#dceadc}
th.srt[data-dir="asc"]::after{content:" \\25B2";color:var(--blu);font-size:11px}
th.srt[data-dir="desc"]::after{content:" \\25BE";color:var(--blu);font-size:11px}
"""

# 通用表头排序脚本（2026-11-04）：全部指数列表点击表头即排序。
# 规则：数值列按数值（td 的 data-v 优先，其次清洗文本后 parseFloat）；
# 文本列按 zh 拼音序；"—"/空缺恒排末尾；同值保持原序（稳定）；
# 再点一次反向；▲▼ 指示方向；相关性矩阵（.mx）不参与。
SORT_JS = r"""
(function(){
  function cellVal(td){
    var d=td.getAttribute('data-v');
    if(d!==null&&d!==''){var f=parseFloat(d);if(isFinite(f))return{k:'n',v:f};}
    var t=(td.textContent||'').replace(/[\s,%]/g,'').replace(/日$/,'');
    if(t==='\u2014'||t==='')return{k:'m'};
    var f2=parseFloat(t);
    if(isFinite(f2))return{k:'n',v:f2};
    return{k:'s',v:t};
  }
  document.querySelectorAll('table:not(.mx)').forEach(function(tb){
    var head=tb.rows[0];if(!head||head.cells.length<2)return;
    var body=tb.tBodies[tb.tBodies.length-1]||tb;
    Array.prototype.forEach.call(head.cells,function(th,ci){
      if(th.classList.contains('srt'))return;
      th.classList.add('srt');th.title='点击排序';
      th.addEventListener('click',function(){
        var dir=th.getAttribute('data-dir')==='asc'?'desc':'asc';
        Array.prototype.forEach.call(head.cells,function(h){h.removeAttribute('data-dir');});
        th.setAttribute('data-dir',dir);
        var rows=[],r;
        for(r=1;r<tb.rows.length;r++)rows.push(tb.rows[r]);
        var nums=[],strs=[],miss=[],i;
        for(i=0;i<rows.length;i++){rows[i]._i=i;
          var v=cellVal(rows[i].cells[ci]);
          if(v.k==='n')nums.push(rows[i]);
          else if(v.k==='s')strs.push(rows[i]);
          else miss.push(rows[i]);
        }
        var mul=(dir==='asc')?1:-1;
        function cmp(a,b){
          var va=cellVal(a.cells[ci]),vb=cellVal(b.cells[ci]);
          if(va.k==='n'){var d=va.v-vb.v;if(d!==0)return d*mul;}
          else{var ds=va.v.localeCompare(vb.v,'zh');if(ds!==0)return ds*mul;}
          return a._i-b._i;
        }
        nums.sort(cmp);strs.sort(cmp);
        miss.sort(function(a,b){return a._i-b._i;});
        nums.concat(strs,miss).forEach(function(tr){body.appendChild(tr);});
      });
    });
  });
})();
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


def _split(res):
    """按 tradeable 拆分：主层（可交易）与观察层（watch/ref/lead）。"""
    tr, wa = [], []
    for c, v in (res.get("per_index") or {}).items():
        (tr if v.get("tradeable", True) else wa).append((c, v))
    return tr, wa


def _state_dist(rows):
    cnt = {k: 0 for k in STATE_ORDER}
    for _c, v in rows:
        d = v["state"]["dominant"]
        if d in cnt:
            cnt[d] += 1
    return cnt


def _dist_bar(cnt):
    total = sum(cnt.values()) or 1
    seg = []
    for k in STATE_ORDER:
        w = cnt[k] / total * 100
        if w <= 0:
            continue
        seg.append(f'<span class="b{k.lower()}" style="width:{w:.2f}%" '
                   f'title="{STATE_FULL[k]}: {cnt[k]} 只"></span>')
    return f'<div class="bar">{"".join(seg)}</div>'


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
    trade_rows, watch_rows = _split(res)
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

    # ---- 顶部状态横幅（一眼结论）----
    n_sw = len(res.get("kill_switches") or [])
    a('<div class="banner">')
    a(f'<span><b>{len(trade_rows)}</b><div class="bl">可交易标的</div></span>')
    # B2 资格分层统计（P2）：从 per_index 资格分汇总四档
    _tiers = {"FULL": 0, "REDUCED": 0, "OBSERVE": 0, "FROZEN": 0}
    for _c, _v in list(trade_rows) + list(watch_rows):
        _eg = _v.get("eligibility")
        if _eg:
            _tiers[_eg.get("tier", "FROZEN")] = _tiers.get(_eg.get("tier", "FROZEN"), 0) + 1
    a(f'<span><b class="{"neg" if _tiers["FROZEN"] > (len(trade_rows) + len(watch_rows)) * 0.6 else "pos"}">'
      f'{_tiers["FULL"] + _tiers["REDUCED"]}</b>'
      f'<div class="bl">资格可执行（FULL {_tiers["FULL"]} ｜ REDUCED {_tiers["REDUCED"]}）</div></span>')
    a(f'<span><b>{_tiers["FROZEN"]}</b><div class="bl">资格冻结（OBSERVE {_tiers["OBSERVE"]}）</div></span>')
    a(f'<span><b>{len(watch_rows)}</b><div class="bl">观察池（折叠于下方各表）</div></span>')
    a(f'<span><b>{res.get("total_exposure", 0) * 100:.2f}%</b>'
      f'<div class="bl">总敞口（压缩前 '
      f'{res.get("total_exposure_pre_switch", 0) * 100:.2f}%）</div></span>')
    a(f'<span><b class="{"neg" if n_sw else "pos"}">{n_sw}</b>'
      f'<div class="bl">风险开关生效</div></span>')
    hedge = res.get("hedge_need", 0) or 0
    a(f'<span><b class="{"warn" if hedge > 0 else ""}">{hedge * 100:.0f}%</b>'
      f'<div class="bl">对冲建议（beta）</div></span>')
    a("</div>")

    # ---- 一页纸决策卡（个人投资参考：今天做/不做）----
    import etf_map as _etf
    dc = res.get("decision_card") or {}
    if dc.get("enabled"):
        veto = bool((res.get("market_context") or {}).get("veto"))
        a('<div class="dcard%s">' % ("" if veto else " ok"))
        a(f'<h2>今日行动卡 · {_e(dc.get("headline"))}</h2>')
        a('<div class="kv">')
        a(f'<span>账户口径：<b>{_e(dc.get("account_label"))}</b></span>')
        a(f'<span>模型建议敞口：<b>{dc.get("model_exposure", 0) * 100:.2f}%</b></span>')
        cls = "neg" if not dc.get("actionable_exposure") else "pos"
        a(f'<span>可执行敞口：<b class="{cls}">'
          f'{dc.get("actionable_exposure", 0) * 100:.2f}%</b></span>')
        if dc.get("allocation_cny"):
            a(f'<span>参考金额：<b>¥{dc["allocation_cny"]:,.0f}</b></span>')
        a(f'<span>场内已核实：<b>{dc.get("instrument_confirmed", 0)}/'
          f'{dc.get("instrument_tradeable", 0)}</b></span>')
        a("</div>")
        if dc.get("reasons"):
            a('<div class="note"><b>否决原因：</b>' +
              "；".join(_e(x) for x in dc["reasons"]) + "</div>")
        a("<ul>")
        for s in dc.get("steps") or []:
            a(f"<li>{_e(s)}</li>")
        a("</ul>")
        a(f'<div class="note">概率口径：{_e(dc.get("calibration_label"))} ｜ '
          f'复核：{_e(dc.get("review_at"))}</div>')
        a("</div>")

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
    qr = res.get("quality_report") or {}
    lag = res.get("data_lag") or []
    if gaps or excl or qr.get("total") or lag:
        a("<h2>数据口径披露</h2><div class='card'>")
        for g in gaps:
            a(f"<div class='warn'>样本不足，未纳入：{_e(g)}</div>")
        for e in excl:
            a(f"<div class='neg'>硬异常，已排除：{_e(e)}</div>")
        lv = qr.get("levels") or {}
        if qr.get("total"):
            a(f"<h3>数据软提示分级 · 共 {qr['total']} 条 "
              f"（HIGH {lv.get('HIGH', 0)} ／ MED {lv.get('MED', 0)} "
              f"／ LOW {lv.get('LOW', 0)}）</h3>")
            a(f"<div class='note'>{_e(qr.get('policy'))}</div>")
            if qr.get("high_note"):
                a(f"<div class='neg'><b>HIGH：</b>{_e(qr['high_note'])}</div>")
            groups = qr.get("groups") or {}
            for cat, items in groups.items():
                if not items:
                    continue
                lvl = items[0].get("level", "MED")
                qcls = {"HIGH": "qhigh", "MED": "qmed", "LOW": "qlow"}.get(lvl, "qlow")
                a("<details class='fold'%s><summary><span class='qtag %s'>%s</span>"
                  "%s · %d 条</summary><div class='inner'>"
                  % (" open" if lvl == "HIGH" else "", qcls, _e(lvl),
                     _e(items[0].get("label") or cat), len(items)))
                for it in items:
                    a(f"<div class='note'>{_e(it.get('name'))}：{_e(it.get('text'))}</div>")
                a("</div></details>")
        for x in lag:
            a(f"<div class='note'>滞后对齐：{_e(x.get('name'))} "
              f"{_e(x.get('had'))} → {_e(x.get('used'))}</div>")
        a("</div>")

    # ---- KPI ----
    dd = (res.get("portfolio_dd") or {})
    a('<div class="kpi">')
    a(f'<div><div class="lbl">账户回撤（节流输入）</div>'
      f'<div class="val {"neg" if dd.get("ok") else "warn"}">{_pct(dd.get("dd"))}</div>'
      f'<div class="note">{_e(dd.get("label") or dd.get("source"))}</div></div>')
    a(f'<div><div class="lbl">合计建议敞口</div>'
      f'<div class="val">{res.get("total_exposure", 0) * 100:.2f}%</div>'
      f'<div class="note">压缩前 {res.get("total_exposure_pre_switch", 0) * 100:.2f}%'
      f'（系数 {res.get("switch_haircut", 1):.2f}）</div></div>')
    a(f'<div><div class="lbl">对冲建议（模型）</div>'
      f'<div class="val warn">{res.get("hedge_need", 0) * 100:.0f}%</div>'
      f'<div class="note">beta 敞口；个人不可直接执行，替代为降敞口/现金</div></div>')
    n_e = sum(1 for _c, v in trade_rows if v["state"]["dominant"] == "E")
    a(f'<div><div class="lbl">可交易标的处 E 阶段</div>'
      f'<div class="val neg">{n_e}/{len(trade_rows)}</div>'
      f'<div class="note">下跌/衰退</div></div>')
    a("</div>")

    # ---- L1 ----
    a("<h2>一、状态概率向量（L1）</h2>")
    a('<div class="legend"><span><i class="ba"></i>A 吸筹</span>'
      '<span><i class="bb"></i>B 突破</span><span><i class="bc"></i>C 通道</span>'
      '<span><i class="bd"></i>D 派发</span><span><i class="be"></i>E 下跌</span></div>')
    a("<table><tr><th>指数</th><th>收盘</th><th>年化波动</th><th>距52周高</th>"
      "<th>主状态</th><th>主状态起始日</th><th>持续交易日</th>"
      "<th>概率分布</th><th>置信</th><th>状态权重</th><th>仓位</th><th>L2抑制</th>"
      "<th>资格分</th><th>开仓许可</th></tr>")
    _TIER_CLS = {"FULL": "tg", "REDUCED": "tb", "OBSERVE": "tm", "FROZEN": "tr"}
    for _c, v in trade_rows:
        st = v["state"]
        _sd = st.get("state_start")
        _days = int(st.get("state_days", 0) or 0)
        pos = (v.get("sizing") or {}).get("final")
        _gov = v.get("governance_gate")
        _oa = bool(v.get("open_allowed"))
        if bool((res.get("market_context") or {}).get("veto")):
            _perm_icon, _perm_tip = "✕", "顶层市场否决：今日禁止开新仓"
        elif _gov is not None and not _gov.get("allowed"):
            _perm_icon, _perm_tip = "✕", "治理门禁未通过：" + (
                ((_gov.get("errors") or ["未通过"])[0]) if _gov.get("errors") else "未通过")
        elif _oa:
            _perm_icon, _perm_tip = "✓", "允许开仓（治理通过且市场未否决）"
        else:
            _perm_icon, _perm_tip = "—", "未配置治理门禁"
        _perm_cell = f"<td class='mono' title='{_e(_perm_tip)}'>{_perm_icon}</td>"
        _l2 = v.get("l2_suppress")
        if _l2:
            _l2f = _l2.get("factor")
            _l2detail = f"领涨20日 {_l2['leader_ret20'] * 100:.2f}% ｜ 状态权重 {_l2['before']:.2f}→{_l2['after']:.2f}"
            _l2cell = f"<td class='num' data-v='{_l2f}' title='{_e(_l2detail)}'>×{_l2f:.2f}</td>"
        else:
            _l2cell = "<td class='mono'>—</td>"
        _eg = v.get("eligibility")
        if _eg:
            _f = _eg.get("factors") or {}
            _tip = (f"四因子: 可投资性 {_f.get('investability', 0):.2f} ｜ "
                    f"状态契合 {_f.get('state_fit', 0):.2f} ｜ "
                    f"数据质量 {_f.get('data_quality', 0):.2f} ｜ "
                    f"映射就绪 {_f.get('map_ready', 0):.2f}")
            if _eg.get("notes"):
                _tip += " ｜ " + "；".join(_eg["notes"])
            _ecls = _TIER_CLS.get(_eg.get("tier"), "tm")
            _elig_cell = (f"<td class='num' data-v='{_eg.get('score', 0)}' "
                          f"title='{_e(_tip)}'>{_eg.get('score', 0):.0f} "
                          f"<span class='badge {_ecls}'>{_e(_eg.get('tier', ''))}</span></td>")
        else:
            _elig_cell = "<td class='mono' title='资格分未启用'>—</td>"
        a(f"<tr><td>{_e(v['name'])}</td>"
          f"<td class='num'>{v['close']:.1f}</td>"
          f"<td class='num'>{_pp(v.get('ann_vol_20'))}</td>"
          f"<td class='num neg'>{_pct(v.get('dd_from_high'))}</td>"
          f"<td data-v='{STATE_ORDER.index(st['dominant'])}'>{_e(st['dominant_name'])}</td>"
          f"<td class='mono'>{_e(_sd) if _sd else '—'}</td>"
          f"<td class='num' data-v='{_days}'>{_days}</td>"
          f"<td data-v='{(st['probs'].get(st['dominant']) or 0):.4f}'>{_probbar(st['probs'])}</td>"
          f"<td class='num'>{st['confidence']:.2f}</td>"
          f"<td class='num'>{v['state_weight']:.2f}</td>"
          f"<td class='num'><b>{pos * 100:.2f}%</b></td>"
          f"{_l2cell}{_elig_cell}{_perm_cell}</tr>")
    a("</table>")

    # 观察池折叠表
    if watch_rows:
        cnt = _state_dist(watch_rows)
        non_e = cnt["A"] + cnt["B"] + cnt["C"]
        a(f'<details class="fold"><summary>观察池（{len(watch_rows)} 只 · '
          f'不参与仓位）—— A {cnt["A"]} ｜ B {cnt["B"]} ｜ C {cnt["C"]} ｜ '
          f'D {cnt["D"]} ｜ E {cnt["E"]}'
          f'{" ｜ ◆ 非 E 状态 " + str(non_e) + " 只值得关注" if non_e else ""}</summary>'
          f'<div class="inner">')
        a('<div class="note">观察池来自 106 只筛选源与参照系（bucket=watch/ref/lead），'
          '参与五状态评分但无仓位、无执行计划。非 E 状态（A/B/C）= 潜在候选。</div>')
        a(_dist_bar(cnt))
        a("<table><tr><th>指数</th><th>收盘</th><th>年化波动</th><th>距52周高</th>"
          "<th>主状态</th><th>主状态起始日</th><th>持续交易日</th>"
          "<th>概率分布</th><th>置信</th><th>角色</th><th>L2抑制</th><th>资格分</th></tr>")
        for _c, v in watch_rows:
            st = v["state"]
            _sd = st.get("state_start")
            _days = int(st.get("state_days", 0) or 0)
            _l2 = v.get("l2_suppress")
            if _l2:
                _l2f = _l2.get("factor")
                _l2detail = f"领涨20日 {_l2['leader_ret20'] * 100:.2f}% ｜ 状态权重 {_l2['before']:.2f}→{_l2['after']:.2f}"
                _l2cell = f"<td class='num' data-v='{_l2f}' title='{_e(_l2detail)}'>×{_l2f:.2f}</td>"
            else:
                _l2cell = "<td class='mono'>—</td>"
            _eg = v.get("eligibility")
            if _eg:
                _f = _eg.get("factors") or {}
                _tip = (f"四因子: 可投资性 {_f.get('investability', 0):.2f} ｜ "
                        f"状态契合 {_f.get('state_fit', 0):.2f} ｜ "
                        f"数据质量 {_f.get('data_quality', 0):.2f} ｜ "
                        f"映射就绪 {_f.get('map_ready', 0):.2f}")
                if _eg.get("notes"):
                    _tip += " ｜ " + "；".join(_eg["notes"])
                _ecls = {"FULL": "tg", "REDUCED": "tb", "OBSERVE": "tm",
                         "FROZEN": "tr"}.get(_eg.get("tier"), "tm")
                _elig_cell = (f"<td class='num' data-v='{_eg.get('score', 0)}' "
                              f"title='{_e(_tip)}'>{_eg.get('score', 0):.0f} "
                              f"<span class='badge {_ecls}'>{_e(_eg.get('tier', ''))}</span></td>")
            else:
                _elig_cell = "<td class='mono'>—</td>"
            a(f"<tr><td>{_e(v['name'])}</td>"
              f"<td class='num'>{v['close']:.1f}</td>"
              f"<td class='num'>{_pp(v.get('ann_vol_20'))}</td>"
              f"<td class='num neg'>{_pct(v.get('dd_from_high'))}</td>"
              f"<td data-v='{STATE_ORDER.index(st['dominant'])}'>{_e(st['dominant_name'])}</td>"
              f"<td class='mono'>{_e(_sd) if _sd else '—'}</td>"
              f"<td class='num' data-v='{_days}'>{_days}</td>"
              f"<td data-v='{(st['probs'].get(st['dominant']) or 0):.4f}'>{_probbar(st['probs'])}</td>"
              f"<td class='num'>{st['confidence']:.2f}</td>"
              f"<td class='note'>{_e(v.get('role', '—'))}</td>"
          f"{_l2cell}{_elig_cell}</tr>")
        a("</table></div></details>")

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
        trade_codes = {c for c, _v in trade_rows}
        main_lags = [(c, v) for c, v in (ch.get("lags") or {}).items()
                     if c in trade_codes]
        rest_lags = [(c, v) for c, v in (ch.get("lags") or {}).items()
                     if c not in trade_codes]
        a("<table><tr><th>下游（可交易）</th><th>最佳滞后</th><th>领先相关</th>"
          "<th>重叠样本</th></tr>")
        for _c, v in main_lags:
            if not v.get("ok"):
                a(f"<tr><td>{_e(v.get('name'))}</td><td>—</td><td>—</td>"
                  f"<td class='note'>{_e(v.get('reason', '—'))}</td></tr>")
                continue
            a(f"<tr><td>{_e(v['name'])}</td><td class='num'>{v['best_lag_days']} 日</td>"
              f"<td class='num'>{v['best_corr']:.3f}</td>"
              f"<td class='num'>{v['n_overlap']}</td></tr>")
        a("</table>")
        if rest_lags:
            ok_n = sum(1 for _c, v in rest_lags if v.get("ok"))
            a(f'<details class="fold"><summary>参照系与观察池传导（{len(rest_lags)} 只'
              f' · 有效 {ok_n}）</summary><div class="inner">')
            a("<table><tr><th>下游</th><th>最佳滞后</th><th>领先相关</th>"
              "<th>重叠样本</th></tr>")
            for _c, v in rest_lags:
                if not v.get("ok"):
                    a(f"<tr><td>{_e(v.get('name'))}</td><td>—</td><td>—</td>"
                      f"<td class='note'>{_e(v.get('reason', '—'))}</td></tr>")
                    continue
                a(f"<tr><td>{_e(v['name'])}</td><td class='num'>{v['best_lag_days']} 日</td>"
                  f"<td class='num'>{v['best_corr']:.3f}</td>"
                  f"<td class='num'>{v['n_overlap']}</td></tr>")
            a("</table></div></details>")

    # ---- 相关性 ----
    cor = res.get("correlation") or {}
    a("<h2>三、相关性（L3 输入）</h2>")
    if cor.get("ok"):
        a(f'<div class="note">全池 60 日平均两两相关 '
          f'<b>{cor["mean_pairwise"]:.2f}</b>（样本 {cor["n_days"]} 日；'
          f'L3 惩罚与对冲口径）。下表为可交易标的子矩阵。</div>')
        full = cor["matrix"]
        names = [c for c, _v in trade_rows if c in full]
        nm = {c: v.get("name") or c for c, v in trade_rows}
        a("<table class=\"mx\"><tr><th></th>"
          + "".join(f"<th>{_e(nm.get(n, n))}</th>" for n in names) + "</tr>")
        for x in names:
            row = full.get(x) or {}
            cells = []
            for y in names:
                val = row.get(y)
                cells.append(f"<td class='num'>{val:.2f}</td>"
                             if val is not None else "<td class='num'>—</td>")
            a(f"<tr><td>{_e(nm.get(x, x))}</td>" + "".join(cells) + "</tr>")
        a("</table>")
    else:
        a(f'<div class="card warn">{_e(cor.get("reason"))}</div>')

    # ---- 仓位 ----
    a("<h2>四、仓位建议（L3 四层约束取最小）</h2>")
    a(f'<div class="note">观察池与参照系共 {len(watch_rows)} 只不参与仓位，'
      f'下表仅列可交易标的。</div>')
    a("<table><tr><th>指数</th><th>vol 上限</th><th>回撤系数</th><th>相关惩罚</th>"
      "<th>状态权重</th><th>生效约束</th><th>最终仓位</th></tr>")
    for _c, v in trade_rows:
        s = v["sizing"]
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
    a("<table><tr><th>指数</th><th>场内标的（执行参考）</th><th>触发位</th>"
      "<th>追价上限</th><th>失效位</th><th>距触发位</th><th>反弹资格</th>"
      "<th>单笔风险</th></tr>")
    for _c, v in trade_rows:
        e = v.get("execution") or {}
        rb = e.get("rebound") or {}
        dist = None
        if e.get("trigger_price") and v.get("close"):
            dist = e["trigger_price"] / v["close"] - 1
        if rb.get("active"):
            rb_txt = (f'<span class="pos">[可参与] 战术参与 '
                      f'上限 {e.get("rebound_allowance", 0) * 100:.0f}%</span>')
        else:
            rb_txt = (f'<span style="color:var(--dim)">[不可] 下跌中继 '
                      f'（{rb.get("score", 0)}/4 项）</span>')
        ins = v.get("instrument") or {}
        txt, icls = _etf.display(ins)
        top = (ins.get("candidates") or [])[:3]
        cand = "、".join(f"{_e(c.get('code'))} {_e(c.get('name'))}" for c in top)
        etf_cell = (f"<td><span class='{icls}'>{_e(txt)}</span>"
                    + (f"<div class='note'>候选：{cand}</div>" if cand else "") + "</td>")
        a(f"<tr><td>{_e(v['name'])}</td>{etf_cell}"
          f"<td class='num'>{_e(e.get('trigger_price'))}</td>"
          f"<td class='num'>{_e(e.get('chase_cap_price'))}"
          f"（{_e(e.get('chase_atr_mult'))} ATR）</td>"
          f"<td class='num'>{_e(e.get('fail_price'))}</td>"
          f"<td class='num warn'>{_pct(dist)}</td>"
          f"<td>{rb_txt}</td>"
          f"<td class='num'>{e.get('single_trade_risk_nav', 0) * 100:.1f}% NAV</td></tr>")
    a("</table>")

    # ---- 五·补 执行换算（P1-① 个人下单参考）----
    ep = res.get("execution_plan") or {}
    if ep.get("enabled") and ep.get("rows"):
        a("<h2>五·补 执行换算（个人下单参考）</h2>")
        a(f"<div class='plan-note'><b>{_e(ep.get('mode'))}</b> ｜ 状态 "
          f"{_e(ep.get('status'))} ｜ 基数 {_e(ep.get('capital_base'))} ｜ "
          f"最小 {_e(ep.get('lot_size'))} 份/手</div>")
        for wn in ep.get("warnings") or []:
            a(f"<div class='note warn'>{_e(wn)}</div>")
        a("<table><tr><th>指数</th><th>场内标的</th><th>预案权重</th>"
          "<th>预案金额(元)</th><th>现价</th><th>整手份额</th>"
          "<th>实际金额(元)</th><th>费用(元)</th><th>状态</th></tr>")
        for r in ep["rows"]:
            def _numtd(v, money=False):
                if v is None:
                    return "<td data-v=''>—</td>"
                txt = f"{v:,.2f}" if money else str(v)
                return f"<td class='num' data-v='{v}'>{txt}</td>"
            etf = r.get("etf_code") or "未核实"
            if r.get("executable"):
                status = "<span class='pos'>✅ 可下单</span>"
            else:
                status = "<span class='neg'>⛔ " + _e("；".join(r.get("reasons") or [])) + "</span>"
            a(f"<tr><td>{_e(r['name'])}</td>"
              f"<td>{_e(etf)} {_e(r.get('etf_name') or '')}</td>"
              f"<td class='num' data-v='{r['target_weight']}'>"
              f"{r['target_weight'] * 100:.2f}%</td>"
              + _numtd(r.get("target_cny"), True)
              + _numtd(r.get("price"), True)
              + _numtd(r.get("shares"))
              + _numtd(r.get("actual_cny"), True)
              + _numtd(r.get("fee_cny"), True)
              + f"<td class='note'>{status}</td></tr>")
        a("</table>")
        s = ep.get("summary") or {}
        q = ep.get("quotes") or {}
        a(f"<div class='note'>合计：预案 {_e(s.get('target_total_cny'))} 元 ／ 实际 "
          f"{_e(s.get('actual_total_cny'))} 元 ／ 费用 {_e(s.get('fee_total_cny'))} 元；"
          f"可下单 {s.get('executable', 0)}/{s.get('tradeable', 0)} 只</div>")
        a(f"<div class='note'>报价快照 {q.get('count', 0)} 只"
          f"（{_e(q.get('fetched_at'))}）；{_e(ep.get('disclaimer'))}</div>")

    # ---- 行为矩阵 ----
    a("<h2>六、阶段行为矩阵（动作指令）</h2>")
    thr = res.get("min_confidence_to_act", 0.65)
    a("<table><tr><th>指数</th><th>主状态</th><th>置信</th><th>开仓许可</th>"
      "<th>行为指令</th></tr>")
    for _c, v in trade_rows:
        st = v["state"]
        conf_ok = st["confidence"] >= thr
        state_ok = st["dominant"] in ("A", "B", "C")
        if conf_ok and state_ok:
            gate = '<span class="pos">允许开仓</span>'
        elif conf_ok and not state_ok:
            gate = '<span class="warn">置信达标但状态禁新仓</span>'
        else:
            gate = '<span class="neg">置信不足</span>'
        a(f"<tr><td>{_e(v['name'])}</td>"
          f"<td data-v='{STATE_ORDER.index(st['dominant'])}'>{_e(st['dominant_name'])}</td>"
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
      f"{'必需' if gp.get('require_redteam') else '免'}"
      f"；日级台账 {gp.get('daily_records', 0)} 条，本次开仓意向 "
      f"{gp.get('checked_today', 0)} 个、通过 {gp.get('allowed_today', 0)} 个</div>")
    dc = res.get("decision_card") or {}
    a(f"<div class='note warn'>概率口径：{_e(dc.get('calibration_label'))}"
      f"（{_e(dc.get('calibration_status') or '—')}）</div>")
    mc = res.get("market_context") or {}
    _mc_ideal = "（即时态）" if mc.get("use_ideal_state") else ""
    a(f"<div class='note'>市场闸门：{_e(mc.get('label') or '—')}；"
      f"口径 {_e(mc.get('require_state') or '—')}{_mc_ideal} + 大盘闸门"
      f"{'开' if (mc.get('gate') or {}).get('allow') else '关'}</div>")
    em = res.get("etf_map") or {}
    a(f"<div class='note'>场内映射：{em.get('count', 0)} 只全覆盖，"
      f"已核实 {(em.get('stats') or {}).get('confirmed', 0)} 只、"
      f"待确认 {(em.get('stats') or {}).get('candidates', 0)} 只；"
      f"候选未核实跟踪指数，禁止据此下单</div>")
    sp = res.get("settlement_progress") or {}
    a(f"<h3>结算进度 · {_e(sp.get('label'))}</h3>")
    a(f"<div class='note'>已结算 {sp.get('settled_n', 0)}/{sp.get('min_settled', 30)} 条"
      f"（{sp.get('progress_pct', 0)}%）｜ 待结算 {sp.get('pending_n', 0)} 条 ｜ "
      f"距门槛还差 {sp.get('to_threshold', '—')} 条 ｜ 最早一批还需约 "
      f"{_e(sp.get('next_remaining_days'))} 个交易日（≈{_e(sp.get('next_est_weeks'))} 周）</div>")
    a(f"<div class='note'>{_e(sp.get('note'))}</div>")
    ar = res.get("attribution_rebuild") or {}
    a(f"<div class='note'>归因台账：{ar.get('total_records', 0)} 条"
      f"（本次新写 {ar.get('written', 0)} 条，基准 {_e(ar.get('benchmark_code'))}）；"
      f"{_e(ar.get('reason') or ar.get('note'))}</div>")
    ep = res.get("execution_plan") or {}
    a(f"<div class='note'>执行换算：{_e(ep.get('status'))} ｜ 可下单 "
      f"{(ep.get('summary') or {}).get('executable', 0)}/"
      f"{(ep.get('summary') or {}).get('tradeable', 0)} 只</div>")
    a("</div>")

    a('<div class="foot">由 S++ 引擎按外置配置自动生成。'
      '所有阈值均为可调参数，不构成买卖建议。'
      '本文件单文件自包含，无外部依赖。</div>')
    a("<script>" + SORT_JS + "</script>")
    a("</div></body></html>")
    return "\n".join(L)


def _write_atomic(path, text):
    """原子写盘：先写临时文件再 os.replace，避免并发读到半写文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_html(res, out_dir, as_of):
    os.makedirs(out_dir, exist_ok=True)
    html = render_html(res)
    name = f"splus_report_{as_of.replace('-', '')}.html"
    path = os.path.join(out_dir, name)
    _write_atomic(path, html)
    # 固定名镜像（stock-pool 首页链接指向它）：与日期版同内容同步落盘，
    # 消除「仅靠定时任务复制维护、复制失效则首页链接静默消失」的脆弱依赖。
    fixed = os.path.join(out_dir, "splus_report.html")
    _write_atomic(fixed, html)
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import engine
    BASE = os.path.dirname(os.path.abspath(__file__))
    r = engine.run(os.path.join(BASE, "splus_config.yaml"), BASE)
    p = write_html(r, os.path.join(BASE, "data"), r["as_of"])
    print("HTML:", p)
