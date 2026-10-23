# -*- coding: utf-8 -*-
"""
selftest_governance.py —— 治理层可证伪自测

证明三件事：
  1. 敷衍的 premortem 会被拒绝（不是提示，是拦截）
  2. 敷衍的 redteam 会被拒绝
  3. 四桶归因数学自洽，且能识别"收益全来自 beta"这种无效系统

退出码 0 = 通过；1 = 失败
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine      # noqa: E402
import governance  # noqa: E402

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splus_config.yaml")


def main():
    print("=" * 70)
    print("治理层自测（premortem / redteam / 四桶归因）")
    print("=" * 70)
    cfg = engine.load_config(CFG)
    ok = True

    # ---------- 1. premortem 门禁 ----------
    print("\n[1] Premortem 门禁")
    bad_cases = [
        ("完全没写", None),
        ("只有论点", {"thesis": "E阶段底部", "kill_conditions": [],
                  "max_loss_pct": 15, "time_stop_days": 20,
                  "evidence": ["估值低"]}),
        ("证伪条件是主观词", {"thesis": "底部反转", "kill_conditions": ["感觉不对劲"],
                       "max_loss_pct": 15, "time_stop_days": 20,
                       "evidence": ["PE 低分位"]}),
        ("没有时间止损", {"thesis": "反弹", "kill_conditions": ["收盘跌破 1249"],
                    "max_loss_pct": 10, "evidence": ["站上 EMA20"]}),
        ("没有证据", {"thesis": "反弹", "kill_conditions": ["收盘跌破 1249"],
                  "max_loss_pct": 10, "time_stop_days": 20}),
    ]
    good = {"thesis": "E 阶段出现可交易反弹，战术仓参与",
            "kill_conditions": ["收盘价跌破 1249.01（前低）", "反弹 5 日内未站上 EMA20"],
            "max_loss_pct": 8, "time_stop_days": 10,
            "evidence": ["站上 EMA20", "EMA20 上穿 EMA60", "效率比抬升", "成交量放大"]}

    for label, rec in bad_cases:
        errs = governance.validate_premortem(rec)
        passed = len(errs) > 0
        ok = ok and passed
        print(f"  {'[已拦截]' if passed else '[放行!]'}  {label} → "
              f"{len(errs)} 项不合格" + (f"（{errs[0][:40]}）" if errs else ""))

    errs = governance.validate_premortem(good)
    passed = len(errs) == 0
    ok = ok and passed
    print(f"  {'[已放行]' if passed else '[误拦!]'}  合格样本 → {errs or '无错误'}")

    # ---------- 2. redteam 门禁 ----------
    print("\n[2] Redteam 门禁（赚的是谁的钱）")
    bad_rt = [
        ("完全没写", None),
        ("敷衍不知道", {"counterparty": "不知道", "edge_source": "看图形判断的",
                   "crowding": "应该不拥挤吧", "fail_mode": "可能继续跌"}),
        ("答案过短", {"counterparty": "别人怕", "edge_source": "我快",
                  "crowding": "不挤", "fail_mode": "跌"}),
    ]
    good_rt = {"counterparty": "被动 ETF 赎回与止损盘在低位割肉，对手方是流动性需求方",
               "edge_source": "无信息优势，仅有纪律优势：不在下跌通道中抄底",
               "crowding": "左侧抄底资金已大量出清，拥挤度低但仍无右侧确认",
               "fail_mode": "反弹失败后跌破前低，触发止损；或长期横盘消耗时间成本"}

    for label, rec in bad_rt:
        errs = governance.validate_redteam(rec)
        passed = len(errs) > 0
        ok = ok and passed
        print(f"  {'[已拦截]' if passed else '[放行!]'}  {label} → "
              f"{len(errs)} 项不合格" + (f"（{errs[0][:40]}）" if errs else ""))

    errs = governance.validate_redteam(good_rt)
    passed = len(errs) == 0
    ok = ok and passed
    print(f"  {'[已放行]' if passed else '[误拦!]'}  合格样本 → {errs or '无错误'}")

    # ---------- 3. 门禁联合判定 ----------
    print("\n[3] 联合门禁 check_gate")
    g1 = governance.check_gate(cfg, "000688", None, None)
    p1 = not g1["allowed"]
    ok = ok and p1
    print(f"  {'[已拦截]' if p1 else '[放行!]'}  无 premortem 无 redteam → "
          f"allowed={g1['allowed']}, {len(g1['errors'])} 项错误")

    g2 = governance.check_gate(cfg, "000688", good, good_rt)
    p2 = g2["allowed"]
    ok = ok and p2
    print(f"  {'[已放行]' if p2 else '[误拦!]'}  完整材料 → allowed={g2['allowed']}")

    # ---------- 4. 四桶归因 ----------
    print("\n[4] 四桶归因")
    a = governance.attribute_return(beta_ret=0.052, sector_ret=0.011,
                                    stage_ret=0.024, total_ret=0.091)
    p3 = a["consistent"] and abs(a["execution"] - 0.004) < 1e-6
    ok = ok and p3
    print(f"  {'[PASS]' if p3 else '[FAIL]'}  一致性："
          f"beta {a['beta']:.1%} + sector {a['sector']:.1%} + stage {a['stage']:.1%} "
          f"+ exec {a['execution']:.1%} = {a['sum_check']:.1%}（总 {a['total']:.1%}）")

    # 无效系统识别：收益几乎全来自 beta
    fake = [{"dominant": "beta"} for _ in range(8)] + [{"dominant": "stage"} for _ in range(2)]
    h = governance.attribute_health(fake)
    p4 = any("beta" in v for v in h["verdict"])
    ok = ok and p4
    print(f"  {'[PASS]' if p4 else '[FAIL]'}  无效系统识别：" + " | ".join(h["verdict"]))

    real = [{"dominant": "stage"} for _ in range(6)] + [{"dominant": "beta"} for _ in range(3)] + \
           [{"dominant": "execution"} for _ in range(1)]
    h2 = governance.attribute_health(real)
    p5 = h2["shares"]["stage"] >= 0.4 and any("阶段" in v for v in h2["verdict"])
    ok = ok and p5
    print(f"  {'[PASS]' if p5 else '[FAIL]'}  有效系统识别：stage 占比 "
          f"{h2['shares']['stage']:.0%} → " + " | ".join(h2["verdict"]))

    print("\n" + "=" * 70)
    print("总判定： " + ("通过（治理层具备真实拦截能力）" if ok else "失败"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
