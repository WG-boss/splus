# -*- coding: utf-8 -*-
"""B2 仓位执行闭环交互看板（S 级）。

从最新 data/splus_decision_YYYYMMDD.json 生成自包含交互页面
data/position_dashboard.html（内嵌 JSON 数据、零外部依赖）：

  1. 交互输入总仓位金额 → 自动联动各动态标的：
     档位权限 × M 调制 → 目标权重 → 目标金额 → ETF 价格（可手改/下拉候选）
     → 数量（手）→ 佣金 → 实际金额 → 调整方向（加/减/持有/禁开/清仓）
  2. 映射 SLA 徽标（confirmed / needs_review / unmapped）+ 候选 ETF 下拉。
  3. 当前持仓输入（localStorage 持久化）→ 与目标差值 → 仓位调整信息。
  4. 资格分四因子、sizing 分解、闸门 reasons 全量展开 —— 可追溯。
  5. 每日随 run.py 自动重建（output.dashboard_html 控制，默认开启）。

用法：
  python tools_position_dashboard.py                # 取最新决策 JSON 生成
  python tools_position_dashboard.py --date 20261106
  python tools_position_dashboard.py --check        # 幂等校验（内容一致）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

TIER_ORDER = ["FULL", "REDUCED", "OBSERVE", "FROZEN"]
FAMILY_CN = {"offense": "进攻", "event": "事件", "reference": "参考"}


def load_latest_decision(date: str | None = None):
    if date:
        path = os.path.join(DATA_DIR, f"splus_decision_{date}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"decision JSON not found: {path}")
        return path
    files = sorted(glob.glob(os.path.join(DATA_DIR, "splus_decision_*.json")))
    if not files:
        raise FileNotFoundError("no data/splus_decision_*.json found — 先运行 run.py")
    return files[-1]


def build_payload(dec_path: str, cfg: dict) -> dict:
    with open(dec_path, encoding="utf-8") as fh:
        d = json.load(fh)

    el_cfg = cfg.get("eligibility") or {}
    out_cfg = cfg.get("output") or {}
    ep = d.get("execution_plan") or {}
    per_index = d.get("per_index") or {}

    # ---- 资格档位全量分布（118 指数） ----
    tier_summary = {t: 0 for t in TIER_ORDER}
    for code, pi in per_index.items():
        t = ((pi.get("eligibility") or {}).get("tier")) or "FROZEN"
        tier_summary[t if t in tier_summary else "FROZEN"] += 1

    rows = []
    for r in ep.get("rows") or []:
        pi = per_index.get(r.get("code")) or {}
        el = pi.get("eligibility") or {}
        inst = pi.get("instrument") or {}
        siz = pi.get("sizing") or {}
        ex = pi.get("execution") or {}
        l2 = pi.get("l2_suppress") or {}
        st = pi.get("state") or {}
        rb = ex.get("rebound") or {}
        rows.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "bucket": r.get("bucket"),
            "family": el.get("family") or (pi.get("role") or ""),
            "score": el.get("score"),
            "tier": el.get("tier") or "FROZEN",
            "tier_multiplier": el.get("tier_multiplier"),
            "factors": el.get("factors") or {},
            "elig_notes": el.get("notes") or [],
            "dominant": st.get("dominant"),
            "dominant_name": st.get("dominant_name"),
            "confidence": st.get("confidence"),
            "state_days": st.get("state_days"),
            "state_start": st.get("state_start"),
            "model_final": siz.get("final"),
            "binding": siz.get("binding"),
            "sizing_notes": siz.get("notes") or {},
            "target_weight": r.get("target_weight") or 0.0,
            "etf_code": r.get("etf_code"),
            "etf_name": r.get("etf_name"),
            "sla": r.get("instrument_status") or "unmapped",
            "price": r.get("price"),
            "candidates": (inst.get("candidates") or [])[:6],
            "executable": bool(r.get("executable")),
            "reasons": r.get("reasons") or [],
            "quote_reason": r.get("quote_reason") or "",
            "trigger_price": ex.get("trigger_price"),
            "fail_price": ex.get("fail_price"),
            "chase_cap_price": ex.get("chase_cap_price"),
            "l2_factor": l2.get("factor"),
            "rebound_label": rb.get("label"),
            "open_allowed": pi.get("open_allowed"),
        })

    mc = d.get("market_context") or {}
    card = d.get("decision_card") or {}
    return {
        "meta": {
            "as_of": d.get("as_of"),
            "generated_at": d.get("generated_at"),
            "config_version": d.get("config_version"),
            "decision_path": os.path.basename(dec_path),
            "tool_version": "1.0.0",
        },
        "card": {
            "execution_status": card.get("execution_status"),
            "headline": card.get("headline"),
            "reasons": card.get("reasons") or [],
            "account_label": card.get("account_label"),
            "account_configured": card.get("account_configured"),
            "model_exposure": card.get("model_exposure"),
            "actionable_exposure": card.get("actionable_exposure"),
            "hedge": card.get("hedge"),
            "instrument_confirmed": card.get("instrument_confirmed"),
            "instrument_tradeable": card.get("instrument_tradeable"),
        },
        "market": {
            "regime": (mc.get("regime") or {}).get("state"),
            "regime_score": (mc.get("regime") or {}).get("score"),
            "allow_open": mc.get("allow_open"),
            "veto": mc.get("veto"),
            "reasons": mc.get("reasons") or [],
        },
        "modulation": d.get("eligibility_modulation") or {},
        "buckets": d.get("buckets") or {},
        "kill_switches": d.get("kill_switches") or [],
        "lifecycle": d.get("lifecycle") or {},
        "quotes_meta": {
            "fetched_at": (ep.get("quotes") or {}).get("fetched_at"),
            "stale": (ep.get("quotes") or {}).get("stale"),
            "count": (ep.get("quotes") or {}).get("count"),
        },
        "defaults": {
            "lot_size": ep.get("lot_size") or 100,
            "commission_rate": ep.get("commission_rate") or 0.0003,
            "min_commission": ep.get("min_commission") or 5.0,
            "tier_permissions": (el_cfg.get("tier_permissions")
                                 or {"FULL": 1.0, "REDUCED": 0.5, "OBSERVE": 0.0, "FROZEN": 0.0}),
            "tiers_thresholds": el_cfg.get("tiers") or {"full": 80, "reduced": 60, "observe": 40},
        },
        "tier_summary": tier_summary,
        "total_indices": len(per_index),
        "rows": rows,
    }


# ----------------------------------------------------------------------
# HTML 模板（自包含；__DATA__ 运行期替换）
# ----------------------------------------------------------------------
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B2 仓位执行闭环看板 · __AS_OF__</title>
<style>
:root{
  --bg:#f5f7fa; --panel:#ffffff; --line:#e3e8ef; --tx:#1f2937; --tx2:#6b7280;
  --green:#0e9f6e; --green-bg:#def7ec; --blue:#1c64f2; --blue-bg:#e1effe;
  --amber:#c27803; --amber-bg:#fdf6b2; --red:#e02424; --red-bg:#fde8e8;
  --gray:#6b7280; --gray-bg:#e5e7eb; --purple:#7e3af2;
  --mono:"JetBrains Mono",Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--tx);padding:20px}
.wrap{max-width:1280px;margin:0 auto}
h1{font-size:20px;margin-bottom:2px}
h2{font-size:15px;margin:0 0 10px;color:var(--tx)}
.sub{color:var(--tx2);font-size:12px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
.b-FULL{background:var(--green-bg);color:var(--green)} .b-REDUCED{background:var(--blue-bg);color:var(--blue)}
.b-OBSERVE{background:var(--amber-bg);color:var(--amber)} .b-FROZEN{background:var(--gray-bg);color:var(--gray)}
.sla-confirmed{background:var(--green-bg);color:var(--green)} .sla-needs_review{background:var(--amber-bg);color:var(--amber)} .sla-unmapped{background:var(--red-bg);color:var(--red)}
.dir-green{background:var(--green-bg);color:var(--green)} .dir-blue{background:var(--blue-bg);color:var(--blue)}
.dir-amber{background:var(--amber-bg);color:var(--amber)} .dir-red{background:var(--red-bg);color:var(--red)} .dir-gray{background:var(--gray-bg);color:var(--gray)}
.kv{display:flex;gap:18px;flex-wrap:wrap}
.kv>div{min-width:130px}
.kv .k{font-size:11px;color:var(--tx2)} .kv .v{font-size:14px;font-weight:600}
.headline{font-size:17px;font-weight:700;margin-bottom:6px}
.reason{font-size:12px;color:var(--tx2)}
.inputs{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:3px}
.field label{font-size:11px;color:var(--tx2)}
.field input,.field select{padding:6px 9px;border:1px solid var(--line);border-radius:7px;font-size:14px;width:150px;background:#fff}
.field input:focus,.field select:focus{outline:2px solid var(--blue);outline-offset:-1px}
input.cell-in{width:88px;padding:3px 6px;border:1px solid var(--line);border-radius:5px;font-size:12.5px;font-family:var(--mono);text-align:right}
input.cell-in.px{border-color:var(--amber);background:#fffbeb}
input.cell-in.hold{width:80px}
.tbl-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{background:#f9fafb;color:var(--tx2);font-size:11px;font-weight:600;text-align:left;padding:8px 8px;border-bottom:2px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none;position:sticky;top:0}
th:hover{color:var(--tx)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
tr.main:hover{background:#f8fafc}
.num{font-family:var(--mono);text-align:right}
.pos{color:var(--red)} .neg{color:var(--green)}
tr.detail>td{padding:0;background:#f8fafc}
.detail-in{padding:10px 16px;display:none}
.open .detail-in{display:block}
.why{font-size:12px;color:var(--tx2)}
.why b{color:var(--tx)}
.why ul{margin:4px 0 8px 18px}
.chip{cursor:pointer;border:1px solid var(--line);background:#fff;border-radius:999px;padding:3px 12px;font-size:12px;margin-right:6px}
.chip.on{background:var(--tx);color:#fff;border-color:var(--tx)}
.warn{font-size:12px;color:var(--amber)}
.mono{font-family:var(--mono)}
.mut{color:var(--tx2)}
.kpi{display:inline-block;margin-right:22px}
.kpi .k{font-size:11px;color:var(--tx2)} .kpi .v{font-size:17px;font-weight:700;font-family:var(--mono)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolbar input[type=search]{padding:6px 10px;border:1px solid var(--line);border-radius:7px;width:200px}
.btn{padding:6px 14px;border:1px solid var(--line);background:#fff;border-radius:7px;cursor:pointer;font-size:12.5px}
.btn:hover{background:#f3f4f6}
.snap{font-size:11px;color:var(--tx2);font-family:var(--mono)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.tag{display:inline-block;padding:0 7px;border-radius:5px;font-size:11px;background:var(--gray-bg);color:var(--tx);margin-right:5px}
</style>
</head>
<body>
<div class="wrap">
  <h1>B2 仓位执行闭环看板</h1>
  <div class="sub">四因子资格分 → 3 角色族 × 5 态 StateFit → 四档权限 → M 状态调制 → 执行预案 ｜ as_of <b id="asof"></b> ｜ <span class="snap" id="snap"></span></div>

  <div class="card" id="cardStatus">
    <div class="headline" id="headline"></div>
    <div id="statusReasons" class="reason" style="margin-bottom:10px"></div>
    <div class="kv" id="statusKv"></div>
  </div>

  <div class="card">
    <h2>执行参数（交互输入，全表实时联动）</h2>
    <div class="inputs">
      <div class="field"><label>总仓位金额 ¥（本策略可用资金）</label><input id="capital" type="number" min="0" step="1000" placeholder="如 200000"></div>
      <div class="field"><label>佣金率（默认万 2.5）</label><input id="comm" type="number" step="0.0001" min="0"></div>
      <div class="field"><label>最低佣金 ¥</label><input id="mincomm" type="number" step="1" min="0"></div>
      <div class="field"><label>每手股数</label><input id="lot" type="number" step="100" min="1"></div>
      <div class="field"><label>档位筛选</label>
        <select id="tierFilter">
          <option value="">全部档位</option><option>FULL</option><option>REDUCED</option><option>OBSERVE</option><option>FROZEN</option>
        </select>
      </div>
      <div class="field"><label>SLA 筛选</label>
        <select id="slaFilter">
          <option value="">全部 SLA</option><option>confirmed</option><option>needs_review</option><option>unmapped</option>
        </select>
      </div>
      <button class="btn" id="btnExport">导出 CSV</button>
      <button class="btn" id="btnReset">清空持仓输入</button>
    </div>
    <div style="margin-top:10px" id="summaryKpi"></div>
  </div>

  <div class="card">
    <h2>动态可交易标的 · 仓位控制明细 <span class="mut" style="font-weight:400" id="rowNote"></span></h2>
    <div class="toolbar"><input type="search" id="q" placeholder="搜索指数 / ETF / 代码"></div>
    <div class="tbl-wrap">
      <table id="tbl">
        <thead><tr>
          <th data-k="tier">档位</th><th data-k="code">指数 / ETF</th><th data-k="sla">映射SLA</th>
          <th data-k="score">资格分</th><th data-k="dominant">状态</th>
          <th data-k="model_final">模型权重</th><th data-k="target_weight">目标权重</th>
          <th>目标 ¥</th><th>ETF 价格 ¥</th><th>数量（股/手）</th><th>实际 ¥</th><th>佣金 ¥</th>
          <th>当前持仓（股）</th><th>调整方向</th><th></th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="mut" style="font-size:11px;margin-top:8px">· 价格为行情快照（缺失标黄请手动输入或从候选下拉选择）；· 目标权重 = 模型权重 × 档位权限 × 全局 M 调制（引擎落盘口径）；· 数量向下取整到整手。</div>
  </div>

  <div class="grid2">
    <div class="card" id="cardBucket"><h2>桶敞口与 M 调制</h2><div id="bucketBody"></div></div>
    <div class="card" id="cardKill"><h2>Kill-Switch 与生命周期</h2><div id="killBody"></div></div>
  </div>

  <div class="card"><h2>资格档位全量分布（<span id="nTotal"></span> 指数）</h2><div id="tierDist"></div></div>

  <div class="card"><span class="snap" id="foot"></span></div>
</div>

<script id="b2data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('b2data').textContent);
const LS_KEY = 'b2_dash_holdings_v1', LS_PX = 'b2_dash_px_v1', LS_CAP='b2_dash_cap_v1';
let holdings = JSON.parse(localStorage.getItem(LS_KEY)||'{}');
let pxOver = JSON.parse(localStorage.getItem(LS_PX)||'{}');
let sortK='target_weight', sortDir=-1, expanded=new Set();

const fmtCny = v => v==null||isNaN(v)?'—':v.toLocaleString('zh-CN',{maximumFractionDigits:0});
const fmtW = v => (v==null||isNaN(v))?'—':(v*100).toFixed(2)+'%';
const fmtP = v => (v==null||isNaN(v)||v<=0)?'':v.toFixed(3);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function getState(){return{
  capital:parseFloat(document.getElementById('capital').value)||0,
  comm:parseFloat(document.getElementById('comm').value)||0,
  mincomm:parseFloat(document.getElementById('mincomm').value)||0,
  lot:parseInt(document.getElementById('lot').value)||100};}

function priceOf(r){
  const ov=pxOver[r.code];
  if(ov!=null&&ov>0)return ov;
  return (r.price!=null&&r.price>0)?r.price:null;
}
function tierMult(r){return (DATA.defaults.tier_permissions[r.tier]??0);}

/* 调整方向判定（与 B2 档位语义一致） */
function direction(r,curShares,px,cap,marketVeto){
  const pxv=px>0?px:null; if(!pxv)return{t:'需价格',c:'dir-gray'};
  const targetCny=r.target_weight*cap, curCny=curShares*pxv, diff=targetCny-curCny;
  const dH=Math.floor(Math.abs(diff)/pxv/DATA.defaults.lot)* (diff>0?1:-1);
  const hands=isNaN(dH)?0:Math.abs(dH);
  const hs=hands?`（${hands} 手）`:'';
  if(r.tier==='FROZEN'){
    if(curShares>0)return{t:'清仓退出'+(curShares?'':''),c:'dir-red',n:'资格冻结（×0）：存量持仓建议清零，禁止一切新增'};
    return{t:'冻结禁操作',c:'dir-gray',n:'资格分 '+r.score+' < 40 → FROZEN，禁开仓禁持有'};
  }
  if(r.tier==='OBSERVE'){
    if(diff>0&&curShares===0)return{t:'禁开新仓（观察池）',c:'dir-amber',n:'OBSERVE 档当前权限 ×0：只允许对存量持有/减仓，禁加仓'};
    if(diff>0)return{t:'仅限持有（禁加仓）',c:'dir-amber',n:'观察池：目标低于当前即减仓'+hs+'；禁止加仓'};
    if(diff<0)return{t:'仅限减仓'+hs,c:'dir-amber',n:'观察池：只减不加'};
    return{t:'持有观望',c:'dir-gray',n:'观察池存量与目标一致'};
  }
  if(marketVeto&&diff>0&&curShares===0)return{t:'今日禁开新仓',c:'dir-red',n:'市场闸门否决（存量/增量），仅允许存量管理'};
  if(diff>0&&hands>0)return{t:'加仓'+hs,c:'dir-green',n:(r.tier==='REDUCED'?'REDUCED 档 ×0.5 半仓执行':'FULL 档标准执行')};
  if(diff<0&&hands>0)return{t:'减仓'+hs,c:'dir-blue',n:'目标低于当前持仓'};
  return{t:'持有',c:'dir-gray',n:'当前与目标一致'};
}

function rowHtml(r,i){
  const st=getState(),px=priceOf(r),cap=st.capital;
  const cur=holdings[r.code]?parseFloat(holdings[r.code])||0:0;
  const targetCny=r.target_weight*cap;
  const shares=(px>0&&targetCny>0)?Math.floor(targetCny/px/st.lot)*st.lot:0;
  const actual=shares*px, fee=actual>0?Math.max(actual*st.comm,st.mincomm):0;
  const dir=direction(r,cur,px,cap,DATA.market.veto);
  const sla=r.sla, tierM=tierMult(r);
  const open=expanded.has(r.code);
  const cand=(r.candidates||[]).map(c=>esc(c.code+' '+c.name)).join('|');
  const dlId='cand_'+r.code;
  const pxAttr=px!=null?fmtP(px):'';
  return `<tr class="main" data-code="${esc(r.code)}">
    <td><span class="badge b-${r.tier}" title="档位权限 ×${tierM}｜${esc((r.elig_notes||[]).join('；'))}">${r.tier}</span></td>
    <td><b>${esc(r.name)}</b> <span class="mut mono">${esc(r.code)}</span><br><span class="mut" style="font-size:11px">${esc(r.etf_code||'未映射')} ${esc(r.etf_name||'')}</span></td>
    <td><span class="badge sla-${sla}">${sla==='confirmed'?'SLA✓':sla==='needs_review'?'待复核':'未映射'}</span></td>
    <td class="num"><b>${r.score==null?'—':r.score}</b><br><span class="mut" style="font-size:10px">×${tierM}</span></td>
    <td><span class="tag">${esc(r.dominant||'')} ${esc(r.dominant_name||'')}</span><br><span class="mut" style="font-size:10px">${r.state_days==null?'':r.state_days+' 天'}</span></td>
    <td class="num">${fmtW(r.model_final)}<br><span class="mut" style="font-size:10px">约束 ${esc(r.binding||'')}</span></td>
    <td class="num"><b>${fmtW(r.target_weight)}</b></td>
    <td class="num">${fmtCny(targetCny)}</td>
    <td><input class="cell-in ${px==null?'px':''}" type="number" step="0.001" min="0" value="${pxAttr}" placeholder="手填"
      list="${dlId}" data-px="${esc(r.code)}" ${px==null?'title="行情缺失：手动输入或从下拉候选选择"':''}>
      <datalist id="${dlId}">${px!=null?`<option value="${fmtP(r.price)}">${esc(r.etf_code||'')} 快照价</option>`:''}${(r.candidates||[]).map(c=>`<option value="${esc(c.code)}">${esc(c.code)} ${esc(c.name)}</option>`).join('')}</datalist>
      ${px==null?'<div class="warn">缺价</div>':''}</td>
    <td class="num">${px>0&&shares>0?`${shares.toLocaleString()} / ${(shares/st.lot).toFixed(0)}手`:'—'}</td>
    <td class="num">${fmtCny(actual)}</td>
    <td class="num">${fmtCny(fee)}</td>
    <td><input class="cell-in hold" type="number" min="0" step="100" value="${cur||''}" placeholder="0" data-hold="${esc(r.code)}"></td>
    <td><span class="badge ${dir.c}" title="${esc(dir.n||'')}">${dir.t}</span></td>
    <td><button class="btn" style="padding:2px 9px;font-size:11px" data-toggle="${esc(r.code)}">为什么</button></td>
  </tr>
  <tr class="detail ${open?'open':''}" data-detail="${esc(r.code)}"><td colspan="15"><div class="detail-in why" id="why_${esc(r.code)}">${whyHtml(r,dir,tierM)}</div></td></tr>`;
}

function whyHtml(r,dir,tierM){
  const f=r.factors||{};
  const li=(a)=>a&&a.length?a.map(x=>`<li>${esc(x)}</li>`).join(''):'<li class="mut">无</li>';
  const sn=r.sizing_notes||{};
  return `<div><b>判定链</b>：资格分 <b>${r.score}</b> = investability ${(f.investability??0)*30|0} + state_fit ${(f.state_fit??0)*40|0} + data_quality ${(f.data_quality??0)*20|0} + map_ready ${(f.map_ready??0)*10|0}（权重 30/40/20/10）→ ${r.tier}（权限 ×${tierM}）→ 状态调制 M=${DATA.modulation.multiplier??1} → 目标权重 ${fmtW(r.target_weight)} → ${dir.t}</div>
  <ul><li><b>四因子</b>：investability ${f.investability??0}｜state_fit ${f.state_fit??0}｜data_quality ${f.data_quality??0}｜map_ready ${f.map_ready??0}</li>
  <li><b>资格注记</b><ul>${li(r.elig_notes)}</ul></li>
  <li><b>sizing 分解</b>：${Object.entries(sn).map(([k,v])=>esc(k)+': '+esc(v)).join(' ｜ ')||'—'}</li>
  <li><b>闸门/预案 reasons</b><ul>${li(r.reasons)}</ul></li>
  ${r.quote_reason?`<li><b>行情</b>：${esc(r.quote_reason)}</li>`:''}
  ${r.l2_factor!=null?`<li><b>L2 领先滞后抑制</b>：factor ${r.l2_factor}</li>`:''}
  ${r.rebound_label?`<li><b>右侧反弹确认</b>：${esc(r.rebound_label)}</li>`:''}
  ${r.trigger_price?`<li><b>指数级执行参考价</b>：触发 ${r.trigger_price}｜追高上限 ${r.chase_cap_price??'—'}｜止损 ${r.fail_price??'—'}</li>`:''}
  ${(r.candidates||[]).length?`<li><b>候选 ETF（映射复核）</b>：${r.candidates.map(c=>esc(c.code+' '+c.name+'（'+c.score+'）')).join('，')}</li>`:''}
  </ul>`;
}

function render(){
  const st=getState();
  const q=document.getElementById('q').value.trim().toLowerCase();
  const tf=document.getElementById('tierFilter').value, sf=document.getElementById('slaFilter').value;
  let rows=DATA.rows.filter(r=>{
    if(tf&&r.tier!==tf)return false;
    if(sf&&r.sla!==sf)return false;
    if(q){const s=(r.code+r.name+(r.etf_code||'')+(r.etf_name||'')).toLowerCase();if(!s.includes(q))return false;}
    return true;});
  rows.sort((a,b)=>{const va=a[sortK]??-1,vb=b[sortK]??-1;return (va>vb?1:va<vb?-1:0)*sortDir;});
  document.getElementById('tbody').innerHTML=rows.map((r,i)=>rowHtml(r,i)).join('')||'<tr><td colspan="15" class="mut">无匹配行</td></tr>';

  /* KPI 汇总 */
  let sumT=0,sumA=0,sumF=0,execN=0,cash=0;
  for(const r of DATA.rows){
    const px=priceOf(r),tc=r.target_weight*st.capital;sumT+=tc;
    const sh=(px>0&&tc>0)?Math.floor(tc/px/st.lot)*st.lot:0;
    sumA+=sh*px;sumF+=sh*px>0?Math.max(sh*px*st.comm,st.mincomm):0;
    if(r.executable)execN++;
  }
  cash=st.capital-sumA;
  document.getElementById('summaryKpi').innerHTML=
    `<span class="kpi"><div class="k">目标敞口合计</div><div class="v">${fmtW(st.capital?sumT/st.capital:0)}</div></span>
     <span class="kpi"><div class="k">计划投入 ¥</div><div class="v">${fmtCny(sumA)}</div></span>
     <span class="kpi"><div class="k">预计佣金 ¥</div><div class="v">${fmtCny(sumF)}</div></span>
     <span class="kpi"><div class="k">剩余现金 ¥</div><div class="v">${fmtCny(cash)}</div></span>
     <span class="kpi"><div class="k">预案可执行标的</div><div class="v">${execN} / ${DATA.rows.length}</div></span>`;
  document.getElementById('rowNote').textContent=`（${rows.length}/${DATA.rows.length} 行显示）`;
}

function renderStatic(){
  document.getElementById('asof').textContent=DATA.meta.as_of;
  document.getElementById('snap').textContent=`决策快照 ${DATA.meta.decision_path} · 生成 ${DATA.meta.generated_at}`;
  document.getElementById('headline').textContent=DATA.card.headline||'—';
  document.getElementById('statusReasons').innerHTML=(DATA.card.reasons||[]).map(x=>`· ${esc(x)}`).join('<br>');
  const m=DATA.modulation;
  const kv=[
    ['执行状态',(DATA.card.execution_status||'—')],['市场 regime',`${DATA.market.regime||'—'}（${DATA.market.regime_score??'—'}）`],
    ['市场闸门',DATA.market.veto?'关闭（禁开新仓）':'开放'],
    ['M 全局调制',`×${m.multiplier??1}（E态占比 ${((m.e_ratio??0)*100).toFixed(1)}%）`],
    ['模型敞口',fmtW(DATA.card.model_exposure)],['可执行敞口',fmtW(DATA.card.actionable_exposure)],
    ['映射 SLA',`confirmed ${DATA.card.instrument_confirmed??0} ｜ 可交易标的 ${DATA.card.instrument_tradeable??0}`],
    ['行情快照',`${DATA.quotes_meta.fetched_at||'—'}${DATA.quotes_meta.stale?'（过期）':''}`]];
  document.getElementById('statusKv').innerHTML=kv.map(([k,v])=>`<div><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join('');
  /* 桶敞口 */
  const bk=DATA.buckets||{};
  document.getElementById('bucketBody').innerHTML=Object.entries(bk).map(([k,v])=>
    `<div style="margin-bottom:8px"><b>${esc(k)}</b> <span class="mut mono" style="font-size:11px">${esc((v.codes||[]).join(','))}</span><br>
     敞口 ${fmtW(v.exposure)}（调制前 ${fmtW(v.exposure_pre_switch)}）· <span class="mut">${esc(v.note||'')}</span></div>`).join('');
  /* kill + lifecycle */
  const ks=(DATA.kill_switches||[]).map(k=>`<li><b>[${esc(k.level)}] ${esc(k.id)}</b>：${esc(k.detail||'')} → ${esc(k.action||'')}</li>`).join('');
  const lf=DATA.lifecycle||{};
  const lfHtml=`<div style="margin-top:8px"><b>生命周期（${esc(lf.as_of||'')}）</b>：晋升 ${((lf.promotions||[]).length)} ｜ 降级 ${((lf.demotions||[]).length)} ｜ 冷却中 ${((lf.cooldown_active||[]).length)}
   ${(lf.promotions||[]).length?`<ul>${lf.promotions.map(p=>`<li>晋升：${esc(JSON.stringify(p))}</li>`).join('')}</ul>`:''}
   ${(lf.demotions||[]).length?`<ul>${lf.demotions.map(p=>`<li>降级：${esc(JSON.stringify(p))}</li>`).join('')}</ul>`:''}</div>`;
  document.getElementById('killBody').innerHTML=`<ul style="margin-left:18px;font-size:12.5px">${ks||'<li class="mut">无触发</li>'}</ul>${lfHtml}`;
  /* 档位分布 */
  const ts=DATA.tier_summary,n=DATA.total_indices;
  document.getElementById('nTotal').textContent=n;
  document.getElementById('tierDist').innerHTML=TIER_ORDER.map(t=>`<span class="kpi"><div class="k"><span class="badge b-${t}">${t}</span></div><div class="v">${ts[t]??0}</div></span>`).join('');
  document.getElementById('foot').textContent=`sources: ${DATA.meta.decision_path} ｜ quotes ${DATA.quotes_meta.fetched_at||'—'}（${DATA.quotes_meta.count??0} 条） ｜ 账户口径：${DATA.card.account_configured?'已配置':'未配置（'+(DATA.card.account_label||'')+'）'} ｜ 对冲：${DATA.card.hedge||'—'} ｜ tool v${DATA.meta.tool_version}`;
  document.getElementById('capital').value=localStorage.getItem(LS_CAP)||'';
  document.getElementById('comm').value=DATA.defaults.commission_rate;
  document.getElementById('mincomm').value=DATA.defaults.min_commission;
  document.getElementById('lot').value=DATA.defaults.lot_size;
}
const TIER_ORDER=['FULL','REDUCED','OBSERVE','FROZEN'];

/* 事件 */
function refocus(sel){const el=document.querySelector(sel);if(el){el.focus();try{el.setSelectionRange(el.value.length,el.value.length);}catch(_){}}}
document.getElementById('tbl').addEventListener('input',e=>{
  const t=e.target;
  if(t.dataset.px!=null){pxOver[t.dataset.px]=parseFloat(t.value)||null;localStorage.setItem(LS_PX,JSON.stringify(pxOver));render();refocus(`[data-px="${t.dataset.px}"]`);}
  if(t.dataset.hold!=null){holdings[t.dataset.hold]=parseFloat(t.value)||0;localStorage.setItem(LS_KEY,JSON.stringify(holdings));render();refocus(`[data-hold="${t.dataset.hold}"]`);}
});
document.getElementById('capital').addEventListener('input',()=>{localStorage.setItem(LS_CAP,document.getElementById('capital').value);render();});
for(const id of['comm','mincomm','lot'])document.getElementById(id).addEventListener('input',render);
document.getElementById('q').addEventListener('input',render);
document.getElementById('tierFilter').addEventListener('change',render);
document.getElementById('slaFilter').addEventListener('change',render);
document.getElementById('tbl').addEventListener('click',e=>{
  const tg=e.target.closest('[data-toggle]');
  if(tg){const c=tg.dataset.toggle;expanded.has(c)?expanded.delete(c):expanded.add(c);
    document.querySelector(`[data-detail="${c}"]`).classList.toggle('open');
    document.querySelector(`[data-code="${c}"]`).classList.toggle('open');return;}
  const th=e.target.closest('th[data-k]');
  if(th){const k=th.dataset.k;if(sortK===k)sortDir*=-1;else{sortK=k;sortDir=-1;}render();}
});
document.getElementById('btnReset').addEventListener('click',()=>{holdings={};pxOver={};localStorage.removeItem(LS_KEY);localStorage.removeItem(LS_PX);render();});
document.getElementById('btnExport').addEventListener('click',()=>{
  const st=getState();const head=['指数代码','指数','ETF代码','ETF名称','资格分','档位','档位权限','状态','模型权重','目标权重','目标金额','ETF价格','数量(股)','手数','实际金额','佣金','当前持仓(股)','调整方向','SLA','可执行'];
  const lines=[head.join(',')];
  for(const r of DATA.rows){
    const px=priceOf(r)||0,tc=r.target_weight*st.capital;
    const sh=(px>0&&tc>0)?Math.floor(tc/px/st.lot)*st.lot:0;
    const dir=direction(r,holdings[r.code]||0,px,st.capital,DATA.market.veto);
    lines.push([r.code,r.name,r.etf_code||'',r.etf_name||'',r.score,r.tier,tierMult(r),r.dominant,r.model_final,r.target_weight,tc.toFixed(2),px,sh,(sh/st.lot).toFixed(0),(sh*px).toFixed(2),sh*px>0?Math.max(sh*px*st.comm,st.mincomm).toFixed(2):0,holdings[r.code]||0,dir.t,r.sla,r.executable].join(','));
  }
  const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`b2_position_plan_${DATA.meta.as_of}.csv`;a.click();});
renderStatic();render();
</script>
</body>
</html>
"""


def render_html(data: dict) -> str:
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE.replace("__DATA__", js)
    html = html.replace("__AS_OF__", str(data.get("meta", {}).get("as_of") or ""))
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 仓位执行闭环交互看板")
    ap.add_argument("--date", help="决策日期 YYYYMMDD（默认取最新）")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "position_dashboard.html"))
    ap.add_argument("--check", action="store_true", help="幂等校验：重新生成内容与现存文件一致")
    args = ap.parse_args()

    try:
        import engine
        cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    except Exception:
        cfg = {}

    dec = load_latest_decision(args.date)
    payload = build_payload(dec, cfg)
    html = render_html(payload)

    if args.check:
        if not os.path.exists(args.out):
            print(f"[FAIL] not found: {args.out}")
            return 1
        with open(args.out, encoding="utf-8") as fh:
            cur = fh.read()
        # 幂等口径：同一决策 JSON + 同一模板 → 字节级一致（generated_at 取自决策快照，非当前时间）
        ok = cur == html
        print(("[OK] dashboard check passed: " if ok else "[FAIL] dashboard check FAILED (content drift): ")
              + f"{args.out} (as_of={payload['meta']['as_of']}, rows={len(payload['rows'])})")
        return 0 if ok else 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    n_exec = sum(1 for r in payload["rows"] if r["executable"])
    print(f"DASH: {args.out} (as_of={payload['meta']['as_of']}, rows={len(payload['rows'])}, executable={n_exec}, "
          f"tier={payload['tier_summary']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
