# -*- coding: utf-8 -*-
"""
selftest_p0.py —— P0（账户真实净值 / 市场闸门 / 治理门禁 / ETF 映射）专项自检

覆盖"正确性与诚实性"：
  - 真实账户缺失时绝不偷偷拿假设组合净值顶替（宁可不压仓）
  - 市场闸门 fail-closed：库缺、状态未定稿、快照过期一律否决
  - 治理门禁真的接进决策链，日级台账幂等，空模板骗不过门禁
  - ETF 映射覆盖全部 118 只（含观察池），未确认项绝不展示可下单代码
  - 报告同时给出模型敞口与可执行敞口，不把概率当胜率
"""
from __future__ import annotations
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import account as account_mod
import etf_map as etf_map_mod
import governance as gov
import market_context as mc
import engine
import render_html
import run as run_cli

OK = []


def chk(name, cond, detail=""):
    OK.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'}  {name}  {detail}")


def t_account():
    print("\n[T1] 账户真实净值口径")
    tmp = tempfile.mkdtemp(prefix="splus_acct_")
    try:
        # 未配置 → 不压仓
        st = account_mod.load(tmp)
        chk("文件缺失降级为未配置", not st["configured"] and st["dd"] is None)
        chk("未配置时节流输入为 0", account_mod.throttle_dd(st) == 0.0)
        chk("展示标签含演示口径", "演示口径" in account_mod.label(st))

        today = datetime.date.today().isoformat()
        # 写入未填净值但带日期 → 仍未配置
        io.open(os.path.join(tmp, "account.yaml"), "w", encoding="utf-8").write(
            "meta:\n  updated_at: '%s'\naccount:\n  enabled: true\n  nav: null\n"
            "  peak_nav: null\n  capital: null\n  current_drawdown: null\n" % today)
        st = account_mod.load(tmp)
        chk("净值字段为空不冒充真实账户", not st["configured"])

        # 真实净值 90 / 峰值 100 → -10%
        account_mod.set_snapshot(tmp, nav=90.0, capital=200000.0, as_of=today,
                                 peak_nav=100.0)
        st = account_mod.load(tmp)
        chk("nav+peak 算出 -10%", st["configured"] and abs(st["dd"] + 0.10) < 1e-6,
            str(st.get("dd")))
        chk("来源为真实账户", st["source"] in ("nav_history", "nav_peak"), st["source"])
        chk("金额换算 = 敞口 × capital",
            abs(account_mod.allocation(st, 0.05) - 10000.0) < 1e-6)
        chk("逐日净值已落盘", os.path.exists(os.path.join(tmp, "data", "account_nav.jsonl")))

        # 过期快照 → 降级
        old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        raw = {"meta": {"updated_at": old},
               "account": {"enabled": True, "nav": 90.0, "peak_nav": 100.0,
                           "as_of": old, "capital": 200000.0}}
        import yaml
        io.open(os.path.join(tmp, "account.yaml"), "w", encoding="utf-8").write(
            yaml.safe_dump(raw, allow_unicode=True))
        st = account_mod.load(tmp)
        chk("30 天前快照按过期降级", not st["configured"] and st["stale"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _mk_market(root, state, gate_allow, status="OK", date=None):
    d = date or datetime.date.today().isoformat()
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    db = os.path.join(root, "data", "stock_pool.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE market_regime (trade_date TEXT, state TEXT, score REAL)")
    con.execute("INSERT INTO market_regime VALUES (?,?,?)", (d, state, 60.0))
    con.commit(); con.close()
    io.open(os.path.join(root, "data", "market_gate_state.json"), "w",
            encoding="utf-8").write(json.dumps(
                {"as_of": d, "status": status, "allow": gate_allow}, ensure_ascii=False))
    return d


def t_market():
    print("\n[T2] 市场闸门单向顶层否决（fail-closed）")
    base = tempfile.mkdtemp(prefix="splus_mc_")
    try:
        sp = os.path.join(base, "stock-pool")
        cfg = {"market_context": {"stock_pool_dir": "stock-pool"}}
        d = _mk_market(sp, "增量", True)
        r = mc.load(base, cfg, d)
        chk("增量+闸门开 → 放行", r["allow_open"] and not r["veto"])

        shutil.rmtree(sp); _mk_market(sp, "减量", True, date=d)
        r = mc.load(base, cfg, d)
        chk("减量 → 否决", r["veto"] and not r["allow_open"])
        chk("否决理由写明市场状态", any("减量" in x for x in r["reasons"]))

        shutil.rmtree(sp); _mk_market(sp, "增量", False, date=d)
        r = mc.load(base, cfg, d)
        chk("闸门关 → 否决", r["veto"])

        shutil.rmtree(sp); _mk_market(sp, "增量", True, status="STALE", date=d)
        r = mc.load(base, cfg, d)
        chk("闸门未定稿 → 否决", r["veto"])

        shutil.rmtree(sp)
        r = mc.load(base, cfg, d)
        chk("库缺失 → 否决", r["veto"])

        _mk_market(sp, "增量", True,
                   date=(datetime.date.today() - datetime.timedelta(days=40)).isoformat())
        r = mc.load(base, cfg, d)
        chk("快照过期 → 否决", r["veto"])

        shutil.rmtree(sp); _mk_market(sp, "增量", True, date=d)
        off = {"market_context": {"stock_pool_dir": "stock-pool", "enabled": False}}
        r = mc.load(base, off, d)
        chk("enabled=false 仅展示不否决", r["allow_open"] and not r["enabled"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_governance():
    print("\n[T3] 治理门禁接线（空模板不得骗过）")
    tmp = tempfile.mkdtemp(prefix="splus_gov_")
    try:
        cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
        d = datetime.date.today().isoformat()
        p, existed = gov.write_template(tmp, d, "TEST", "测试")
        chk("模板生成成功", os.path.exists(p) and not existed)
        pre, red = gov.load_journal(tmp, d, "TEST")
        g = gov.check_gate(cfg, "TEST", pre, red)
        chk("未填模板被门禁拦下", not g["allowed"])

        good = {
            "premortem": {"thesis": "突破确认后跟随上游链条",
                          "kill_conditions": ["收盘跌破 4000 点且成交量放大 20% 以上"],
                          "max_loss_pct": 8, "time_stop_days": 20,
                          "evidence": ["沪深300 20 日动量转正且量能连续三日放大"]},
            "redteam": {"counterparty": "止损盘在突破失败后集中离场愿意让出筹码",
                        "edge_source": "上游链条领先滞后 3 日的结构性时滞",
                        "crowding": "同类 ETF 近五日份额变化平稳未见拥挤",
                        "fail_mode": "假突破后快速回落触发时间止损"}}
        io.open(p, "w", encoding="utf-8").write(json.dumps(good, ensure_ascii=False))
        pre, red = gov.load_journal(tmp, d, "TEST")
        g = gov.check_gate(cfg, "TEST", pre, red)
        chk("填齐后门禁放行", g["allowed"], str(g["errors"]))

        r1 = gov.log_daily(tmp, d, {"market_veto": True})
        r2 = gov.log_daily(tmp, d, {"market_veto": False})
        chk("日级台账幂等", r1["written"] and not r2["written"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_engine():
    print("\n[T4] 引擎结果层（真实数据）")
    cfg = os.path.join(BASE, "splus_config.yaml")
    res = engine.run(cfg, BASE)
    pi = res.get("per_index") or {}
    tr = {k: v for k, v in pi.items() if v.get("tradeable")}

    chk("账户口径已落盘", isinstance(res.get("account"), dict))
    chk("组合回撤不再来自假设组合",
        (res.get("portfolio_dd") or {}).get("source") in
        ("unconfigured", "manual_drawdown", "nav_history", "nav_peak"))
    chk("假设组合仅作演示参考",
        isinstance((res.get("portfolio_dd") or {}).get("synthetic_demo"), dict))
    coefs = {(v.get("sizing") or {}).get("coef_dd") for v in tr.values()}
    chk("未配置账户时不压仓 coef_dd=1.0", coefs == {1.0}, str(coefs))

    mc_ = res.get("market_context") or {}
    chk("市场闸门结果已落盘", "veto" in mc_)
    chk("可执行敞口与模型敞口分离", "actionable_exposure" in res)
    if mc_.get("veto"):
        chk("否决时可执行敞口为 0", res["actionable_exposure"] == 0.0)
        chk("否决时无任何标的允许开仓",
            not any(v.get("open_allowed") for v in pi.values()))

    chk("ETF 映射 118 只全覆盖",
        (res.get("etf_map") or {}).get("count") == len(pi) == 118)
    chk("观察池也带 instrument",
        all(v.get("instrument") for v in pi.values()))
    chk("l2_suppress 已落盘",
        all(v.get("l2_suppress") for v in tr.values()))
    chk("治理日级台账已写", (res.get("governance") or {}).get("daily_records", 0) >= 1)
    chk("未校准标签存在", "未校准" in
        (res.get("decision_card") or {}).get("calibration_label", ""))
    chk("对冲明确个人不可执行", "个人不可直接执行" in res.get("hedge_note", ""))

    # 幂等：同日重跑不重复追加日级台账
    gp = os.path.join(BASE, "data", "ledger", "governance.jsonl")
    n1 = len(gov.read_jsonl(gp))
    engine.run(cfg, BASE)
    n2 = len(gov.read_jsonl(gp))
    chk("同日重跑台账不重复", n1 == n2, f"{n1} -> {n2}")

    # 报告层
    h = render_html.render_html(res)
    chk("HTML 含今日行动卡", 'class="dcard' in h and "今日行动卡" in h)
    chk("HTML 含场内标的列", "场内标的（执行参考）" in h)
    chk("HTML 含账户口径标签", "账户回撤（节流输入）" in h)
    chk("HTML 含未校准披露", "概率未校准" in h)
    chk("HTML 无未替换 None", "None" not in h)
    chk("HTML 表数仍为 9", h.count("<table") == 9, str(h.count("<table")))
    md = run_cli.render(res)
    chk("MD 含今日行动卡", "## 今日行动卡" in md)
    chk("MD 含可执行敞口", "可执行敞口" in md)
    chk("MD 含市场闸门", "市场闸门" in md)
    return res


def main():
    print("=" * 72)
    print("P0 专项自检：账户真实净值 / 市场闸门 / 治理门禁 / ETF 映射")
    print("=" * 72)
    t_account()
    t_market()
    t_governance()
    t_engine()
    print("\n" + "=" * 72)
    n = sum(OK)
    print(f"通过 {n}/{len(OK)}")
    print("总判定： " + ("通过" if n == len(OK) else "失败"))
    print("=" * 72)
    return 0 if n == len(OK) else 1


if __name__ == "__main__":
    sys.exit(main())
