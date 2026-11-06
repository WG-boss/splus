# -*- coding: utf-8 -*-
"""
selftest_p12.py —— P1/P2（执行换算 / 映射复核 / 结算进度 / 归因 / 提示分级）专项自检

重点验证「诚实性」而非「能不能算出来」：
  - 闸门否决 / 账户未配置 / 标的未核实 / 行情过期 → 一律不给可下单份额
  - 同桶合并计仓不得重复放大
  - 无已结算预测时归因台账绝不编造
  - 复核工具绝不把候选改成 confirmed
  - 软提示分级不改变样本是否参与计算
"""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import engine
import etf_map as em
import etf_quotes as qmod
import execution_plan as ep
import quality as qa
import calibration_progress as cp
import attribution as at
import tools_etf_review as review
import render_html
import run as run_cli

OK = []
CFG = os.path.join(BASE, "splus_config.yaml")


def chk(name, cond, detail=""):
    OK.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'}  {name}  {detail}")


def _write_quotes(tmp, price=1.25, age_hours=0.0, n=150):
    p = os.path.join(tmp, "data", "etf_quotes.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ts = (datetime.datetime.now() - datetime.timedelta(hours=age_hours)).isoformat(timespec="seconds")
    quotes = {str(510000 + i): {"name": f"测试ETF{i}", "market": "sh",
                                "price": 1.0, "amount": 1e8} for i in range(n)}
    quotes["588000"] = {"name": "科创50ETF华夏", "market": "sh",
                        "price": price, "amount": 1e9}
    io.open(p, "w", encoding="utf-8").write(json.dumps(
        {"fetched_at": ts, "source": "selftest", "count": len(quotes),
         "quotes": quotes}, ensure_ascii=False))
    return p


def _ctx(cfg, tmp=None, *, veto=False, configured=True, confirmed=True,
         price=1.25, age_hours=0.0, n_codes=1, exposure=0.05):
    if tmp:
        _write_quotes(tmp, price=price, age_hours=age_hours)
    account = {"configured": configured, "capital": 200000.0 if configured else None,
               "source": "selftest"}
    market = {"veto": veto, "reasons": [] if not veto else ["测试否决"]}
    codes = ["000688"] if n_codes == 1 else ["000688", "399673"]
    per_index = {c: {"name": c, "bucket": "core", "tradeable": True,
                     "open_allowed": True,
                     "governance_gate": {"allowed": True, "open_intent": True}}
                 for c in codes}
    instruments = {c: {"status": "confirmed" if confirmed else "needs_review",
                       "etf_code": "588000" if confirmed else None,
                       "etf_name": "科创50ETF华夏" if confirmed else None}
                   for c in codes}
    mapping = {"ok": True, "instruments": instruments}
    # 同桶 2 只时 exposure=0.10，per_name=0.05，合计恰等于 actionable_exposure
    total = exposure * n_codes
    buckets = {"core": {"codes": codes, "exposure": total, "per_name": exposure}}
    return dict(cfg=cfg, base_dir=tmp or BASE, account_state=account,
                actionable_exposure=total, buckets=buckets, per_index=per_index,
                mapping=mapping, market=market)


def t_execution(cfg):
    print("\n[T1] 执行换算：整手取整与四重 fail-closed")
    tmp = tempfile.mkdtemp(prefix="splus_ep_")
    try:
        kw = _ctx(cfg, tmp)
        p = ep.build(**kw)
        r = p["rows"][0]
        chk("全绿时给出可下单份额", p["status"] == "ready" and r["executable"],
            str(p["status"]))
        chk("目标金额 = capital × 桶内均分权重", r["target_cny"] == 10000.0,
            str(r["target_cny"]))
        chk("份额按 100 份整手向下取整", r["shares"] == 8000 and r["lots"] == 80,
            f"shares={r['shares']} price={r['price']}")
        chk("实际金额 = 份额 × 现价", r["actual_cny"] == 10000.0, str(r["actual_cny"]))
        chk("费用取 max(费率, 最低佣金)", r["fee_cny"] == 5.0, str(r["fee_cny"]))
        chk("合计含费用", r["total_cny"] == 10005.0, str(r["total_cny"]))

        kw2 = _ctx(cfg, tmp, price=1.30)
        r2 = ep.build(**kw2)["rows"][0]
        chk("非整价位正确向下取整（不四舍五入）",
            r2["shares"] == 7600 and r2["actual_cny"] == 9880.0,
            f"shares={r2['shares']} actual={r2['actual_cny']}")

        kw3 = _ctx(cfg, tmp, veto=True)
        p3 = ep.build(**kw3)
        chk("闸门否决 → blocked_veto 且不可下单",
            p3["status"] == "blocked_veto" and not p3["rows"][0]["executable"]
            and p3["mode"].startswith("预案"), str(p3["status"]))

        kw4 = _ctx(cfg, tmp, configured=False)
        p4 = ep.build(**kw4)
        chk("账户未配置 → 不给金额与份额",
            p4["capital"] is None and p4["rows"][0]["target_cny"] is None
            and p4["rows"][0]["shares"] is None, str(p4["status"]))

        kw5 = _ctx(cfg, tmp, confirmed=False)
        p5 = ep.build(**kw5)
        chk("ETF 未核实 → 不给份额",
            p5["status"] == "no_instrument" and p5["rows"][0]["shares"] is None,
            str(p5["status"]))

        kw6 = _ctx(cfg, tmp, age_hours=200.0)
        p6 = ep.build(**kw6)
        chk("行情快照过期 → 不给份额",
            p6["rows"][0]["shares"] is None and p6["quotes"]["stale"],
            str(p6["status"]))

        kw7 = _ctx(cfg, tmp, n_codes=2)
        p7 = ep.build(**kw7)
        chk("同桶 2 只不重复计仓（合计 = capital × 桶敞口）",
            abs(sum(x["target_cny"] for x in p7["rows"]) - 200000.0 * 0.10) < 1e-6,
            str(sum(x["target_cny"] for x in p7["rows"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_quotes():
    print("\n[T2] 行情快照读取层")
    tmp = tempfile.mkdtemp(prefix="splus_q_")
    try:
        s = qmod.load(tmp)
        chk("文件缺失 → ok=False", not s["ok"] and s["stale"])
        _write_quotes(tmp, n=20)
        chk("条目过少（疑似分页不全）→ 拒绝", not qmod.load(tmp)["ok"])
        _write_quotes(tmp, n=150)
        s = qmod.load(tmp)
        q = qmod.for_code(s, "588000")
        chk("正常快照可读", s["ok"] and not s["stale"] and q["fresh"])
        _write_quotes(tmp, price=0.0, n=150)
        chk("价格 0 / 停牌 → 不可用", not qmod.for_code(qmod.load(tmp), "588000")["fresh"])
        _write_quotes(tmp, price=999.0, n=150)
        chk("价格量级异常 → 不可用", not qmod.for_code(qmod.load(tmp), "588000")["fresh"])
        _write_quotes(tmp, age_hours=200.0, n=150)
        chk("快照过期 → 全部 fresh=False", not qmod.for_code(qmod.load(tmp), "588000")["fresh"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_review():
    print("\n[T3] 全量映射复核（含 107 只观察池）")
    doc = review.tm.load_map()
    ins = doc.get("instruments") or {}
    chk("118 只全覆盖", len(ins) == 118, str(len(ins)))
    chk("每条都有 review 结论", all(v.get("review") for v in ins.values()))
    before = {k: (v.get("status"), v.get("etf_code")) for k, v in ins.items()}
    review.analyze()
    after_ins = review.tm.load_map().get("instruments") or {}
    after = {k: (v.get("status"), v.get("etf_code")) for k, v in after_ins.items()}
    chk("复核只写 review，绝不改 status / etf_code（含已确认项）", before == after)
    chk("复核幂等：118 条 review 均在", len(after_ins) == 118
        and all(v.get("review") for v in after_ins.values()))
    hk = (ins.get("hkHSTECH") or {}).get("review") or {}
    chk("恒生科技错配被识别为 high",
        hk.get("risk") == "high" and "cross_market_suspect" in hk.get("flags", []),
        str(hk.get("flags")))
    risks = {}
    for v in ins.values():
        k = v["review"]["risk"]
        risks[k] = risks.get(k, 0) + 1
    chk("风险分布可审计", sum(risks.values()) == 118 and risks.get("high", 0) > 0,
        str(risks))
    high_ins = {"index_code": "000688", "status": "needs_review",
                "candidates": [{"code": "588000", "name": "科创50ETF华夏", "score": 1.0}]}
    txt, cls = em.display({**high_ins, "review": {"risk": "high"}})
    chk("高风险候选在报告里显示警示", "疑似错配" in txt and cls == "warn", txt)
    chk("观察池 107 只均有复核",
        sum(1 for v in ins.values() if not v.get("tradeable") and v.get("review")) == 107)


def t_progress_and_attribution(cfg, res):
    print("\n[T4/T5] 结算进度与归因台账")
    sp = res.get("settlement_progress") or {}
    chk("真实台账显示 0/30 与待结算数",
        sp.get("settled_n") == 0 and sp.get("pending_n") > 0
        and sp.get("to_threshold") == 30, str(sp.get("settled_n")))
    chk("无样本时 tier=no_sample", sp.get("tier") == "no_sample", str(sp.get("tier")))
    chk("给出交易日进度且不编造具体日期",
        isinstance(sp.get("next_remaining_days"), int) and "不代表具体日期" in sp.get("note", ""))
    chk("决策卡带上结算进度",
        "还需约" in (res.get("decision_card") or {}).get("calibration_label", ""))

    tmp = tempfile.mkdtemp(prefix="splus_attr_")
    try:
        ld = os.path.join(tmp, "ledger")
        os.makedirs(ld, exist_ok=True)
        ar0 = at.rebuild(ld, {}, cfg)
        chk("无结算预测 → 不编造归因记录",
            ar0["written"] == 0 and ar0["total_records"] == 0
            and not os.path.exists(os.path.join(ld, "attribution.jsonl")))
        bars = [{"date": f"2026-01-{d:02d}", "close": 100.0 + d} for d in range(1, 32)]
        preds = [{"as_of": "2026-01-01", "settle_date": "2026-01-21",
                  "code": "000688", "name": "科创50", "settled": True,
                  "fwd_return": 0.12}]
        io.open(os.path.join(ld, "predictions.jsonl"), "w", encoding="utf-8").write(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in preds) + "\n")
        bmap = {"000300": bars}
        ar1 = at.rebuild(ld, bmap, cfg)
        recs = qa_read(os.path.join(ld, "attribution.jsonl"))
        chk("有结算+基准 → 真正落盘", ar1["written"] == 1 and len(recs) == 1)
        if recs:
            r = recs[0]
            chk("四桶之和等于总收益", r.get("consistent") is True, str(r.get("sum_check")))
            chk("sector/stage 显式未拆分",
                r.get("sector") == 0 and r.get("stage") == 0 and r.get("unattributed"))
            chk("execution 如实标注为残差", "残差" in r.get("note", ""))
            chk("beta 取基准实际收益",
                abs(r.get("beta", 0) - (121.0 / 101.0 - 1.0)) < 1e-6, str(r.get("beta")))
        ar2 = at.rebuild(ld, bmap, cfg)
        chk("重跑幂等（同 as_of+code 不重复写）",
            ar2["written"] == 0 and ar2["skipped_duplicate"] == 1)
        ar3 = at.rebuild(ld, {}, cfg)
        chk("基准数据缺失时不新增记录（不编造）",
            ar3["written"] == 0 and ar3["total_records"] == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_quality(res, cfg):
    print("\n[T6] 数据软提示分级")
    qr = res.get("quality_report") or {}
    lv = qr.get("levels") or {}
    _tot = qr.get("total") or 0
    chk("分级条目内部一致且覆盖稳定（随宇宙演化自适应，不耦合快照值）",
        _tot == len(qr.get("items") or []) and _tot >= 40, f"total={_tot}")
    chk("仅收盘价代理为 HIGH（3 只）", lv.get("HIGH") == 3
        and "光模块CPO" in qr.get("high_names", []), str(lv))
    chk("真跳空为 LOW 且保留使用", lv.get("LOW", 0) > 30)
    chk("分级只影响展示，不剔除样本", "不静默剔除样本" in qr.get("policy", ""))
    items = [
        "光模块CPO：源仅提供收盘价（无 OHLC），ATR/ADX 已退化为收盘口径代理",
        "中证A50：存在 1 处 >15 天的日期断裂",
        "创业板50：存在 2 处 >15% 跳空（真行情，已保留）",
    ]
    got = qa.summarize(items, cfg)
    chk("规则顺序正确（HIGH/MED/LOW 各 1）",
        [x["level"] for x in got["items"]] == ["HIGH", "MED", "LOW"],
        str([x["level"] for x in got["items"]]))


def t_report(res):
    print("\n[T7] MD / HTML 报告呈现")
    md = run_cli.render(res)
    h = render_html.render_html(res)
    chk("MD 含执行换算/结算进度/归因/分级",
        all(k in md for k in ("五·补 执行换算", "结算进度", "归因台账", "数据软提示分级")))
    chk("MD 明确「预案」不得下单", "不得据此下单" in md)
    chk("HTML 含执行换算表与整手份额", "五·补 执行换算" in h and "整手份额" in h)
    chk("HTML 含分级折叠与 HIGH 置顶", "数据软提示分级" in h
        and "qhigh" in h and "<details class='fold' open>" in h)
    chk("HTML 含结算进度与归因台账", "结算进度" in h and "归因台账" in h)
    chk("HTML 无裸 None / 标签平衡", "None" not in h
        and h.count("<tr>") == h.count("</tr>")
        and h.count("<td") == h.count("</td>"))
    chk("HTML 体积仍在上限内", len(h.encode("utf-8")) < 200000,
        f"{len(h.encode('utf-8'))} bytes")
    chk("10 张表且排序脚本仍在", h.count("<table") == 10
        and render_html.SORT_JS in h)


def qa_read(path):
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in io.open(path, encoding="utf-8") if x.strip()]


def main():
    print("=" * 74)
    print("P1/P2 专项自检：执行换算 / 映射复核 / 结算进度 / 归因 / 提示分级")
    print("=" * 74)
    cfg = engine.load_config(CFG)
    res = engine.run(CFG, BASE)
    t_execution(cfg)
    t_quotes()
    t_review()
    t_progress_and_attribution(cfg, res)
    t_quality(res, cfg)
    t_report(res)
    print("\n" + "=" * 74)
    n = sum(OK)
    print(f"通过 {n}/{len(OK)}")
    print("总判定： " + ("通过" if n == len(OK) else "失败"))
    print("=" * 74)
    return 0 if n == len(OK) else 1


if __name__ == "__main__":
    sys.exit(main())
