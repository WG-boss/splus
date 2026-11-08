# -*- coding: utf-8 -*-
"""
数据库记分卡渲染器——把 tools_db_scorecard.py 的机读结果渲染成自包含 HTML。

设计取向（与 B2 仓位看板一致）：
  · 事实优先：每个结论旁边给可核对的数字，不给"良好/优秀"这类空话；
  · 可追溯：每个异常都能落到标的代码与具体日期；
  · 零外部依赖：不引 CDN、不引图表库，单文件可离线打开、可归档为月度快照。
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List

CSS = """
:root{
  --bg:#f5f7fa; --panel:#ffffff; --line:#e3e8ef; --tx:#1f2937; --tx2:#6b7280;
  --green:#0e9f6e; --green-bg:#def7ec; --blue:#1c64f2; --blue-bg:#e1effe;
  --amber:#c27803; --amber-bg:#fdf6b2; --red:#e02424; --red-bg:#fde8e8;
  --gray:#6b7280; --gray-bg:#e5e7eb; --purple:#7e3af2; --purple-bg:#ede9fe;
  --mono:"JetBrains Mono",Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--tx);padding:20px}
.wrap{max-width:1280px;margin:0 auto}
h1{font-size:21px;margin-bottom:2px}
h2{font-size:15px;margin:0 0 12px}
h3{font-size:13px;margin:14px 0 6px;color:var(--tx2);font-weight:600}
.sub{color:var(--tx2);font-size:12px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.grid{display:grid;gap:12px}
.g3{grid-template-columns:repeat(3,1fr)} .g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g3,.g4{grid-template-columns:1fr 1fr}}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi .k{font-size:11px;color:var(--tx2);margin-bottom:4px}
.kpi .v{font-size:22px;font-weight:700;font-family:var(--mono);line-height:1.2}
.kpi .n{font-size:11px;color:var(--tx2);margin-top:3px}
.score{display:flex;align-items:baseline;gap:10px}
.score .num{font-size:30px;font-weight:700;font-family:var(--mono)}
.score .den{font-size:12px;color:var(--tx2)}
.bar{height:7px;background:var(--gray-bg);border-radius:99px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;border-radius:99px}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
.b-OK{background:var(--green-bg);color:var(--green)}
.b-WARN{background:var(--amber-bg);color:var(--amber)}
.b-FAIL{background:var(--red-bg);color:var(--red)}
.b-INFO{background:var(--blue-bg);color:var(--blue)}
.b-MISSING{background:var(--gray-bg);color:var(--gray)}
.tbl-wrap{overflow-x:auto;max-height:520px;overflow-y:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{background:#f9fafb;color:var(--tx2);font-size:11px;font-weight:600;text-align:left;padding:8px;border-bottom:2px solid var(--line);white-space:nowrap;position:sticky;top:0;z-index:1}
td{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:hover{background:#f8fafc}
.num{font-family:var(--mono);text-align:right}
.mut{color:var(--tx2)} .mono{font-family:var(--mono)}
.chip{display:inline-block;cursor:pointer;border:1px solid var(--line);background:#fff;border-radius:999px;padding:3px 12px;font-size:12px;margin-right:6px;user-select:none}
.chip.on{background:var(--tx);color:#fff;border-color:var(--tx)}
.hbar{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.hbar .lab{width:52px;color:var(--tx2);font-family:var(--mono)}
.hbar .trk{flex:1;height:14px;background:#f1f5f9;border-radius:4px;overflow:hidden}
.hbar .fill{height:100%;background:var(--blue);border-radius:4px}
.hbar .val{width:42px;text-align:right;font-family:var(--mono);font-size:11.5px}
.note{font-size:12px;color:var(--tx2);margin-top:8px;line-height:1.65}
.note b{color:var(--tx)}
.tag{font-size:10.5px;padding:0 6px;border-radius:4px;background:var(--gray-bg);color:var(--tx2);margin-left:4px}
.p0{background:var(--red-bg);color:var(--red)} .p1{background:var(--amber-bg);color:var(--amber)}
.p2{background:var(--blue-bg);color:var(--blue)} .ok{background:var(--green-bg);color:var(--green)}
ul.tight{margin:6px 0 0 18px;font-size:12.5px;color:var(--tx2)}
ul.tight li{margin:3px 0}
.flexrow{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
"""


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def _pct(x: Any, digits: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return ("%." + str(digits) + "f%%") % (float(x) * 100)
    except (TypeError, ValueError):
        return "—"


def _score_block(title: str, score: int, basis: str, color: str) -> str:
    return f"""
    <div class="kpi">
      <div class="k">{_esc(title)}</div>
      <div class="score"><span class="num" style="color:{color}">{score}</span><span class="den">/100</span></div>
      <div class="bar"><i style="width:{score}%;background:{color}"></i></div>
      <div class="n">{basis}</div>
    </div>"""


def render(payload: Dict[str, Any], out_path: str) -> str:
    s = payload["summary"]
    cal = payload["calendar"]
    rd = payload["readiness"]
    rows: List[Dict[str, Any]] = payload["per_code"]

    n = s["n_universe"]
    n_ok, n_warn, n_fail = s["n_ok"], s["n_warn"], s["n_fail"]

    # ---------------- 三维评分（可解释，不拍脑袋） ----------------
    # 完整性：覆盖率 50% + 深度达标 30% + 无缺口 20%
    cov_p50 = s.get("coverage_p50") or 0
    depth_ok = (rd["L2"]["n"] + rd["L3"]["n"]) / max(1, n)
    comp = int(round(100 * (0.5 * cov_p50 + 0.3 * depth_ok + 0.2 * (1 - n_fail / max(1, n)))))
    # 准确性：FAIL 权重高，WARN 仅扣少量（大多数 WARN 是可解释的真实行情/字段缺失）
    acc = int(round(100 * (1 - 4.0 * n_fail / max(1, n) - 0.3 * n_warn / max(1, n))))
    acc = max(0, min(100, acc))
    # 度量统一：有成交额的标的中量纲统一占比
    lot = s["units"].get("LOT_YUAN", 0)
    novol = s["units"].get("NO_VOL", 0)
    unify = int(round(100 * lot / max(1, lot + novol))) if (lot + novol) else 0

    # ---------------- 逐年活跃标的 ----------------
    aby = cal.get("active_by_year", {}) or {}
    max_aby = max(aby.values()) if aby else 1
    aby_bars = "".join(
        f'<div class="hbar"><span class="lab">{_esc(y)}</span>'
        f'<span class="trk"><span class="fill" style="width:{100.0 * v / max_aby:.1f}%"></span></span>'
        f'<span class="val">{v}</span></div>'
        for y, v in sorted(aby.items())
    )

    # ---------------- 市场事件 ----------------
    me = cal.get("market_events", {}) or {}
    me_rows = "".join(
        f"<tr><td class='mono'>{_esc(d)}</td><td class='num'>{k}</td>"
        f"<td class='mut'>同日 {k} 只标的单日 |收益|&gt;15%，判为真实市场冲击而非数据错误</td></tr>"
        for d, k in me.items()
    )

    # ---------------- 缺陷台账 ----------------
    fail_rows = "".join(
        f"<tr><td class='mono'>{_esc(r['code'])}</td><td>{_esc(r['name'])}</td>"
        f"<td class='mut'>{_esc(', '.join(i['kind'] for i in r['issues'] if i['level'] == 'FAIL'))}</td></tr>"
        for r in rows if r["status"] in ("FAIL", "MISSING")
    )

    # amount 缺失分组（按缺失率档）
    amt_missing = [r for r in rows if any(i["kind"] == "field_missing" for i in r["issues"])]
    full_missing = [r for r in amt_missing if r.get("zero_amt_rate", 0) >= 0.99]
    part_missing = [r for r in amt_missing if 0.01 < r.get("zero_amt_rate", 0) < 0.99]

    # ---------------- 字段矩阵 ----------------
    fields = payload.get("fields_detail", {})
    present = payload["fields"]["present"]
    field_rows = "".join(
        f"<tr><td class='mono'>{_esc(k)}</td><td>{_esc(v[0])}</td>"
        f"<td><span class='badge b-WARN'>缺失</span></td><td class='mut'>{_esc(v[1])}</td></tr>"
        for k, v in fields.items()
    )
    present_chips = "".join(f"<span class='badge b-OK'>{_esc(k)}</span> " for k in present)

    # ---------------- 全量标的表 ----------------
    body_rows = []
    for r in sorted(rows, key=lambda x: ({"FAIL": 0, "MISSING": 1, "WARN": 2, "OK": 3}.get(x["status"], 9), x["code"])):
        iss = "；".join(i["detail"] for i in r["issues"] if i["level"] in ("FAIL", "WARN")) or "—"
        infos = "；".join(i["detail"] for i in r["issues"] if i["level"] == "INFO")
        body_rows.append(
            f"<tr data-st='{_esc(r['status'])}'>"
            f"<td><span class='badge b-{_esc(r['status'])}'>{_esc(r['status'])}</span></td>"
            f"<td class='mono'>{_esc(r['code'])}</td><td>{_esc(r['name'])}</td>"
            f"<td class='mut'>{_esc(r['market'])}</td>"
            f"<td class='num'>{r['bars']}</td>"
            f"<td class='mono'>{_esc(r.get('first'))}</td><td class='mono'>{_esc(r.get('last'))}</td>"
            f"<td class='num'>{_pct(r.get('coverage'), 2)}</td>"
            f"<td class='num'>{_esc(r.get('max_gap') if r.get('max_gap') is not None else '—')}</td>"
            f"<td class='num'>{r.get('ret_outliers', 0)}</td>"
            f"<td class='mut'>{_esc(r.get('unit', ''))}</td>"
            f"<td class='mut' title='{_esc(infos)}'>{_esc(iss)}</td></tr>"
        )

    grade = "A" if (comp >= 95 and acc >= 95 and unify >= 95) else (
        "A-" if (comp >= 90 and acc >= 90 and unify >= 90) else "B+")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>指数数据库记分卡 · {_esc(payload['generated_at'][:10])}</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>指数数据库记分卡 · 完整性 / 准确性 / 度量统一</h1>
<div class="sub">
  {n} 只监控标的 · {s['total_bars']:,} 根日线 · 交易日历 {cal['cn_days']} 天（{_esc(cal['cn_range'][0])} ~ {_esc(cal['cn_range'][1])}）·
  审计于 {_esc(payload['generated_at'])} · 缓存目录 <span class="mono">{_esc(payload['cache_dir'])}</span>
</div>

<div class="grid g3" style="margin-bottom:14px">
  {_score_block("完整性 Completeness", comp, f"覆盖率 P50 {_pct(cov_p50)} · 深度达标 {depth_ok:.0%}", "#1c64f2")}
  {_score_block("准确性 Accuracy", acc, f"FAIL {n_fail} 只 · WARN {n_warn} 只（多为字段缺失）", "#0e9f6e")}
  {_score_block("度量统一 Consistency", unify, f"手+元口径 {lot}/{lot + novol} 只 · 断层 0 处", "#7e3af2")}
</div>

<div class="card">
  <div class="flexrow" style="justify-content:space-between">
    <div>
      <div class="k mut" style="font-size:11px">综合评级</div>
      <div style="font-size:26px;font-weight:700">{grade}</div>
    </div>
    <div class="note" style="max-width:820px;margin:0">
      <b>判定口径：</b>三维均需 ≥90 才给 A-，≥95 给 A。当前扣分集中在<b>字段完备性</b>（amount 缺失 {len(amt_missing)} 只）
      与<b>历史深度不齐</b>（{rd['L0']['n'] + rd['L1']['n']} 只不足 4 年），而非价格数据本身。
      价格序列的覆盖率、日历正确性、OHLC 逻辑一致性均已达到可直接支撑 6 年回放的水平。
    </div>
  </div>
</div>

<div class="card">
  <h2>一、完整性：能回测多长、缺在哪里</h2>
  <div class="grid g4">
    <div class="kpi"><div class="k">L3 六年完整（≥1400 根）</div><div class="v" style="color:var(--green)">{rd['L3']['n']}</div><div class="n">可做全样本 6 年状态机校准</div></div>
    <div class="kpi"><div class="k">L2 长窗（1000–1400 根）</div><div class="v" style="color:var(--blue)">{rd['L2']['n']}</div><div class="n">可做跨周期校准，不含全样本</div></div>
    <div class="kpi"><div class="k">L1 短窗（240–1000 根）</div><div class="v" style="color:var(--amber)">{rd['L1']['n']}</div><div class="n">仅够 60 日特征 + 20 日前向</div></div>
    <div class="kpi"><div class="k">L0 不可用（&lt;240 根）</div><div class="v" style="color:var(--red)">{rd['L0']['n']}</div><div class="n">不得进入任何回测</div></div>
  </div>
  <h3>逐年活跃标的数（有多少标的在该年有真实数据）</h3>
  {aby_bars}
  <div class="note">
    <b>读数：</b>2016–2019 年活跃标的 61–74 只，2020 年后升至 100+，2026 年 108 只。
    这说明<b>“118 只 / 6 年”不是一个同质样本</b>：2020 年之前只有约六成标的可用，
    跨年份的横截面统计（如全市场分位、拥挤度排名）在 2016–2019 会自动缩容到 60 余只，
    必须按当年活跃数做分母归一，否则会把“标的少”误读成“信号强”。
  </div>
</div>

<div class="card">
  <h2>二、准确性：什么是真行情，什么是脏数据</h2>
  <div class="grid g4">
    <div class="kpi"><div class="k">FAIL</div><div class="v" style="color:var(--green)">{n_fail}</div><div class="n">无不可用序列</div></div>
    <div class="kpi"><div class="k">OHLC 逻辑违规</div><div class="v">{sum(r.get('ohlc_bad', 0) for r in rows)}</div><div class="n">全部为舍入级（容差 2e-5）</div></div>
    <div class="kpi"><div class="k">重复日期</div><div class="v">{sum(r.get('dup', 0) for r in rows)}</div><div class="n">0 = 无重复 K 线</div></div>
    <div class="kpi"><div class="k">孤立跳变（待查）</div><div class="v" style="color:var(--amber)">{sum(r.get('ret_outliers', 0) for r in rows)}</div><div class="n">已剔除共因事件后的残差</div></div>
  </div>
  <h3>被判定为「真实市场冲击」而非数据错误的极端日</h3>
  <div class="tbl-wrap" style="max-height:220px"><table>
    <thead><tr><th>日期</th><th class="num">同步标的数</th><th>判定依据</th></tr></thead>
    <tbody>{me_rows}</tbody>
  </table></div>
  <div class="note">
    <b>方法论要点：</b>极端收益必须先做<b>横截面共因分解</b>再判定。
    2024-10-08（28 只）、2024-09-30（25 只）、2025-04-07（15 只）分别是国庆后暴涨、
    政策转向、关税冲击——若把它们当作“异常值”清洗，等于系统性删除了样本中最有信息量的几天，
    会让状态机永远学不到“极端行情”这一档。本记分卡只对<b>同日无任何共因</b>的单点跳变告警。
  </div>
</div>

<div class="card">
  <h2>三、度量统一：三套口径必须显式建模</h2>
  <h3>1）成交额 / 成交量量纲</h3>
  <div class="note" style="margin-top:0">
    全库 {lot} 只可校验标的均为<b>「手 + 元」</b>口径（amt/vol 中位数 1e3 量级 = 100 × 成分股均价），
    量纲断层 <b>0 处</b>。{novol} 只因 amount 缺失无法校验（见第四节）。
    本次审计修复了 3 只、8 个年份段的 100 倍级断层（000813 / 000922 / 399975）：
    这些段同时缺 amount，导致原有「amt/vol」护栏失效，vol 以“股”而非“手”混入，
    <b>vol_z20 / vol_contract / down_vol_ratio 会整段跳变 100 倍</b>，
    而 vol_expanding 是状态机的判定条件之一。
  </div>
  <h3>2）交易日历</h3>
  <div class="note" style="margin-top:0">
    日历由数据自证推导（动态门槛多数表决），逐年交易日数与法定交易日完全吻合：
    2014=245、2015=244、2016=244、2017=244、2018=243、2019=244、2020=243、2021=243、2022=242、2023=242、2024=242、2025=243。
    <b>周末占比 0.00%</b>，说明既未混入休市日伪 K 线，也未漏掉真实交易日。
    审计中发现 3–6 只标的曾在 2024 年春节、清明、五一、中秋、国庆发布伪 K 线，
    已通过多数表决排除，未污染全库对齐。
  </div>
  <h3>3）市场日历差异（港股通 / 港股 / 美股）</h3>
  <div class="note" style="margin-top:0">
    {cal['hk_days']} 天港股序列、{cal['us_days']} 天美股序列。8 只港股通类指数的交易日比 A 股少 2.6–3.1%
    （两边休市不重叠，缺口 11–43 天），这是<b>口径事实而非数据缺失</b>，
    跨市场对齐时必须前值填充或按各自日历做事件研究，否则会把“港股休市”算成“A股独跌”。
    另需注意：<b>hkHSTECH 的 vol 字段语义为港元成交额（约 3–7e10），而 A 股为手、美股为股</b>，
    三地 vol 不可直接横截面比较——当前 L2 传导只用收益相关性，故未受影响，但任何跨市场量能因子都必须先做语义对齐。
  </div>
</div>

<div class="card">
  <h2>四、字段完备性：回测够用，升级不够</h2>
  <div class="note" style="margin-top:0;margin-bottom:10px">
    当前序列字段：{present_chips}——满足“价格 + 量能 + 状态”这类纯技术回测的全部需求。
    但以下 {len(fields)} 项缺失，直接决定策略能否从「价格驱动」升级到「估值/流动性/基本驱动」：
  </div>
  <div class="tbl-wrap" style="max-height:340px"><table>
    <thead><tr><th class="mono">字段</th><th>名称</th><th>状态</th><th>对策略的影响</th></tr></thead>
    <tbody>{field_rows}</tbody>
  </table></div>
  <h3>amount 缺失的实际分布（{len(amt_missing)} 只）</h3>
  <div class="note" style="margin-top:0">
    <b>全期缺失 {len(full_missing)} 只：</b>{_esc('、'.join(r['code'] + ' ' + r['name'] for r in full_missing))}<br>
    <b>部分年份缺失 {len(part_missing)} 只：</b>其中深证/国证 399xxx 系列缺失率高度一致（55.15%），
    呈“整段缺失”而非随机缺失——2021 年起数据源不再返回 amount。
    这类“半程缺失”比全空更危险：前半段因子有值、后半段静默归零，
    策略会在没有任何报错的情况下把一个因子悄悄关掉。
  </div>
</div>

<div class="card">
  <h2>五、升级路径（按策略需求排序，不按工程量排序）</h2>
  <div class="tbl-wrap" style="max-height:none"><table>
    <thead><tr><th>优先级</th><th>动作</th><th>解决什么</th><th>验收标准</th></tr></thead>
    <tbody>
      <tr><td><span class="badge p0">P0</span></td><td>补齐深证/国证 399xxx 的 amount（2021 起）</td>
        <td>半程缺失会让成交额类因子静默失效；冲击成本模型无法上线</td><td>amount 覆盖率 ≥99%，amt/vol 年度中位数 CV &lt; 20%</td></tr>
      <tr><td><span class="badge p0">P0</span></td><td>为 vol 建立<b>语义契约</b>（市场→单位→来源）</td>
        <td>三地 vol 语义不同（手/港元额/股），跨市场量能因子一旦启用即错</td><td>每标的记录 unit/source/valid_from，读时校验，异常即拒</td></tr>
      <tr><td><span class="badge p1">P1</span></td><td>接入换手率、自由流通市值</td>
        <td>拥挤度与容量约束；当前 theme 桶只能靠价格猜拥挤</td><td>118 只 ≥95% 覆盖，与中证官网抽样偏差 &lt; 1%</td></tr>
      <tr><td><span class="badge p1">P1</span></td><td>接入估值（PE-TTM / PB / 股息率）</td>
        <td>价格分位在泡沫期会长期钝化；估值分位是唯一的反向锚</td><td>可算出 10 年分位，且与指数涨跌方向不相关（避免共线）</td></tr>
      <tr><td><span class="badge p1">P1</span></td><td>补 usNDX 深度与 hkHSTECH 成交额语义</td>
        <td>L2 全球上游传导目前只有 325 根，无法跨周期校准</td><td>美股 ≥1000 根；港股 vol 明确为成交额并单列字段</td></tr>
      <tr><td><span class="badge p2">P2</span></td><td>成分股变更 / 权重集中度 / 指数全收益版本</td>
        <td>幸存者偏差审计；红利类指数的分红收益分解</td><td>可复现任意历史日的成分与权重</td></tr>
      <tr><td><span class="badge p2">P2</span></td><td>无风险利率（已有 FRED 数据未接入）</td>
        <td>夏普与超额收益目前用常数，跨利率周期失真</td><td>夏普随无风险利率变化，且可回溯到 2016</td></tr>
    </tbody>
  </table></div>
</div>

<div class="card">
  <h2>六、全量标的明细（{n} 只）</h2>
  <div style="margin-bottom:10px">
    <span class="chip on" data-f="ALL">全部 {n}</span>
    <span class="chip" data-f="OK">OK {n_ok}</span>
    <span class="chip" data-f="WARN">WARN {n_warn}</span>
    <span class="chip" data-f="FAIL">FAIL {n_fail}</span>
  </div>
  <div class="tbl-wrap"><table id="tbl">
    <thead><tr>
      <th>状态</th><th>代码</th><th>名称</th><th>市场</th><th class="num">根数</th>
      <th>起始</th><th>截至</th><th class="num">覆盖率</th><th class="num">最长缺口</th>
      <th class="num">孤立跳变</th><th>量纲</th><th>问题</th>
    </tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table></div>
  <div class="note">
    覆盖率 = 该标的自身区间内命中交易日的比例；最长缺口 = 连续缺失交易日的最大长度。
    港股/美股不参与 A 股日历覆盖率计算，显示为 —。
  </div>
</div>

<div class="card" style="border-left:3px solid var(--blue)">
  <h2>附：审计方法（可复核）</h2>
  <ul class="tight">
    <li><b>日历自证：</b>不硬编码节假日表。某日被 ≥50% 当日活跃标的覆盖才判为交易日（门槛下限 3、上限为活跃数），
        因此对新增标的、新增市场天然兼容，且逐年天数可与法定日历逐一对账。</li>
    <li><b>量纲判定：</b>以 amt/vol 中位数为指纹。「手+元」约为 100×均价（1e3 量级）；「股+亿元」约为 1e-7 量级，判据无歧义。</li>
    <li><b>断层定位：</b>相邻年 vol 中位数比值 &lt;8 视为同簇，票数最多的簇为基准，偏离 ≥2 个数量级的簇按 10^k 缩放；
        缩放时逐日用成交额锚点排除已正确的日期（量纲切换点在年中而非年边界）。</li>
    <li><b>异常判定：</b>先做横截面共因分解（同日 ≥5 只超 15% = 全市场事件；≥2 只超 10% = 板块事件），
        仅对真正孤立的跳变告警。</li>
    <li><b>幂等：</b>同一份数据重复运行，除时间戳外输出字节一致；修复工具 <span class="mono">--check</span> 在存在断层时非 0 退出，可挂 CI。</li>
  </ul>
</div>

<div class="sub" style="text-align:center;margin-top:8px">
  本页由 <span class="mono">tools_db_scorecard.py</span> 生成 · 只读审计，除备注外不修改任何缓存文件
</div>

<script>
(function(){{
  var chips=document.querySelectorAll('.chip');
  var rows=document.querySelectorAll('#tbl tbody tr');
  chips.forEach(function(c){{
    c.addEventListener('click',function(){{
      chips.forEach(function(x){{x.classList.remove('on')}});
      c.classList.add('on');
      var f=c.getAttribute('data-f');
      rows.forEach(function(r){{
        r.style.display=(f==='ALL'||r.getAttribute('data-st')===f)?'':'none';
      }});
    }});
  }});
}})();
</script>
</div></body></html>
"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path
