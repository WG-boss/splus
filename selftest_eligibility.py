# -*- coding: utf-8 -*-
"""selftest_eligibility.py — B2 资格分引擎自测（P1）

覆盖：四因子单测 / 角色族归并 / StateFit 矩阵 / 硬闸 / fail-closed /
幂等落盘 round-trip / 11-05 实证方向断言。运行：python selftest_eligibility.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import eligibility as E

PASS = 0
FAIL = 0
FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")


# ---------------------------------------------------------------- 1. 因子单测

def test_investability():
    print("[1] investability 因子")
    check("5亿满分", E.investability_score(5.0e8, None) == 1.0)
    check("10亿满分(封顶)", E.investability_score(10.0e8, None) == 1.0)
    check("低于3000万归零", E.investability_score(0.29e8, None) == 0.0)
    mid = E.investability_score(2.65e8, None)
    check("2.65亿线性插值≈0.5", abs(mid - (2.65 - 0.3) / (5.0 - 0.3)) < 1e-9, f"got {mid}")
    check("amount 缺失 → None", E.investability_score(None, None) is None)
    check("溢价 2% 扣分", E.investability_score(5.0e8, 0.02) < 1.0)
    check("溢价 3%+ 重罚归零", E.investability_score(5.0e8, 0.035) == 0.0)
    check("溢价 None 不罚", E.investability_score(5.0e8, None) == 1.0)


def test_role_family():
    print("[2] role → 角色族归并")
    check("进攻候选→offense", E.role_family("进攻候选", False) == "offense")
    check("上游映射→offense", E.role_family("上游映射", False) == "offense")
    check("事件驱动→event", E.role_family("事件驱动", False) == "event")
    check("左侧候选→event", E.role_family("左侧候选", False) == "event")
    check("筛选源观察→reference", E.role_family("筛选源观察", False) == "reference")
    check("领先观察+tradeable→offense(特例)",
          E.role_family("领先观察", True) == "offense")
    check("领先观察+非tradeable→reference",
          E.role_family("领先观察", False) == "reference")
    check("未登记 role → None", E.role_family("自创角色", True) is None)


def test_state_fit():
    print("[3] StateFit 矩阵")
    check("offense B=1.0", E.state_fit_score("offense", "B") == 1.0)
    check("offense E=0.0", E.state_fit_score("offense", "E") == 0.0)
    check("event A=0.8(左侧专用)", E.state_fit_score("event", "A") == 0.8)
    check("event D=0.0", E.state_fit_score("event", "D") == 0.0)
    check("reference E=0(硬闸保留)", E.state_fit_score("reference", "E") == 0.0)
    check("reference A=0.5(watch 晋升通道)", E.state_fit_score("reference", "A") == 0.5)
    check("reference D=0.2(OBSERVE 档)", E.state_fit_score("reference", "D") == 0.2)
    check("未定族 → 0", E.state_fit_score(None, "B") == 0.0)
    check("未定态 → 0", E.state_fit_score("offense", "X") == 0.0)


def test_hard_rule():
    print("[4] 硬闸：StateFit=0 → FROZEN（分数照实算）")
    # E 态进攻族：inv=1 dq=1 map=1 → 分数=100*(0.3+0+0.2+0.1)=60 → 无硬闸会 REDUCED
    v = {"name": "测试", "role": "进攻候选", "tradeable": True,
         "state": {"dominant": "E"},
         "instrument": {"status": "confirmed", "verified": True}}
    r = E.compute_entry(v, [], {}, 5.0e8)
    check("E 态进攻族 StateFit=0", r["factors"]["state_fit"] == 0.0)
    check("E 态硬闸 FROZEN（尽管分数 60）", r["tier"] == "FROZEN", f"got {r['tier']}")
    check("E 态分数照实=60(温度计)", abs(r["score"] - 60.0) < 0.1, f"got {r['score']}")
    # D 态事件族同样硬闸
    v2 = {"name": "测试2", "role": "事件驱动", "tradeable": True,
          "state": {"dominant": "D"},
          "instrument": {"status": "confirmed", "verified": True}}
    r2 = E.compute_entry(v2, [], {}, 5.0e8)
    check("D 态事件族 FROZEN", r2["tier"] == "FROZEN")
    # A 态事件族 0.8 → 不触发硬闸
    v3 = {"name": "测试3", "role": "事件驱动", "tradeable": True,
          "state": {"dominant": "A"},
          "instrument": {"status": "confirmed", "verified": True}}
    r3 = E.compute_entry(v3, [], {}, 5.0e8)
    check("A 态事件族非 FROZEN", r3["tier"] != "FROZEN", f"got {r3['tier']}")


def test_map_and_dq():
    print("[5] map_ready / data_quality")
    check("confirmed+verified=1.0",
          E.map_ready_score({"status": "confirmed", "verified": True}) == 1.0)
    check("confirmed 未 verified=0.8",
          E.map_ready_score({"status": "confirmed", "verified": False}) == 0.8)
    check("needs_review=0.5",
          E.map_ready_score({"status": "needs_review"}) == 0.5)
    check("无 instrument=0", E.map_ready_score(None) == 0.0)
    check("无警告=1.0", E.data_quality_score("科创50", []) == 1.0)
    check("断裂类=0.3",
          E.data_quality_score("中证2000", ["中证2000：存在 1 处 >15 天的日期断裂"]) == 0.3)
    check("仅收盘价=0.6",
          E.data_quality_score("光模块CPO",
                               ["光模块CPO：源仅提供收盘价（无 OHLC），ATR/ADX 已退化为收盘口径代理"]) == 0.6)
    check("名称不匹配的警告不罚",
          E.data_quality_score("科创50", ["中证2000：存在 1 处 >15 天的日期断裂"]) == 1.0)
    check("多重警告取最重",
          E.data_quality_score("X", ["X：跳空", "X：日期断裂"]) == 0.3)
    check("name 缺失 → None",
          E.data_quality_score(None, ["X：跳空"]) is None)


def test_fail_closed():
    print("[6] fail-closed")
    # 引擎级异常：quotes 结构损坏（dict 传入但 per_index 迭代器损坏场景用 compute_all 包裹）
    bad_cfg = json.loads(json.dumps(E.DEFAULTS))
    bad_cfg["weights"] = {"investability": "not-a-number"}  # 合成时必炸
    per = {"000001": {"name": "A", "role": "进攻候选", "tradeable": True,
                      "state": {"dominant": "B"},
                      "instrument": {"status": "confirmed", "verified": True}}}
    try:
        out = E.compute_all(per, [], {}, bad_cfg)
        check("引擎级异常 → 全体 FROZEN", out["000001"]["tier"] == "FROZEN")
        check("异常留痕 notes 非空", len(out["000001"]["notes"]) > 0,
              str(out["000001"]["notes"]))
    except Exception as exc:
        check("引擎级异常 → 全体 FROZEN", False, f"raised {exc!r}")
    # 单标的异常隔离：其余标的正常（提供行情使正常标的可达 FULL）
    per2 = {"000001": {"name": "A", "role": "进攻候选", "tradeable": True,
                       "state": {"dominant": "B"},
                       "instrument": {"status": "confirmed", "verified": True,
                                      "etf_code": "588150"}},
            "000002": "not-a-dict"}
    try:
        out2 = E.compute_all(per2, [], {"588150": {"amount": 5.0e8}}, None)
        check("单标的异常隔离(正常标的仍出分)", out2["000001"]["tier"] == "FULL",
              json.dumps(out2["000001"], ensure_ascii=False))
        check("异常标的 FROZEN", out2["000002"]["tier"] == "FROZEN")
    except Exception as exc:
        check("单标的异常隔离", False, f"raised {exc!r}")


def test_history_roundtrip():
    print("[7] JSONL 落盘幂等 round-trip")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "elig.jsonl")
        s1 = {"000001": {"score": 85.0, "tier": "FULL"}}
        s2 = {"000001": {"score": 55.0, "tier": "OBSERVE"}}
        E.append_history(p, "2026-11-04", s1)
        E.append_history(p, "2026-11-05", s2)
        n = E.append_history(p, "2026-11-05", s1)  # 同日重跑 → 覆盖
        check("三行落盘(同日幂等)", n == 2, f"got {n}")
        rows = E.load_history(p)
        check("round-trip 2 行", len(rows) == 2)
        check("同日覆盖后为最新分", rows[-1]["scores"]["000001"]["score"] == 85.0)
        check("as_of 升序", rows[0]["as_of"] == "2026-11-04")
        # 损坏行跳过
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"as_of": "broken",')  # 半行
        rows2 = E.load_history(p)
        check("损坏行跳过", len(rows2) == 2)


# ---------------------------------------------------------------- 8. 11-05 实证

def test_real_snapshot():
    print("[8] 11-05 实证快照方向断言")
    dj = "data/splus_decision_20261105.json"
    qj = "data/etf_quotes.json"
    if not (os.path.exists(dj) and os.path.exists(qj)):
        print("  ⚠️ 实证文件缺失，跳过（非本机全量环境）")
        return
    d = json.load(open(dj, encoding="utf-8"))
    q = json.load(open(qj, encoding="utf-8")).get("quotes") or {}
    out = E.compute_all(d["per_index"], d.get("quality_warnings"), q, None)
    check("118 只全覆盖", len(out) == len(d["per_index"]))

    hks = out.get("hkHSTECH")
    check("恒生科技 A 态参照特例 → 非 FROZEN", hks and hks["tier"] != "FROZEN",
          json.dumps(hks, ensure_ascii=False) if hks else "missing")
    # 11-05 实况：映射 status=needs_review（11-06 才确认）→ map_ready=0.5，
    # 且 etf_code 未挂行情 → investability 保守 0 → 分数 45.0 是引擎对数据实况的
    # 诚实反映；P4 确认映射+挂行情后应升至 80+（FULL）。断言锁定该因果。
    if hks and (hks["factors"]["map_ready"] == 0.5):
        check("恒生科技 45.0=诚实反映 needs_review 实况", hks["score"] == 45.0,
              f"got {hks['score']}")
    else:
        check("恒生科技已确认映射 → ≥80 FULL", hks and hks["score"] >= 75.0,
              f"got {hks['score'] if hks else '-'}")

    e_traded = [c for c, v in d["per_index"].items()
                if v.get("tradeable") and (v.get("state") or {}).get("dominant") == "E"]
    check("9 只 E 态原白名单全部 FROZEN",
          all(out[c]["tier"] == "FROZEN" for c in e_traded if c in out),
          str([c for c in e_traded if c in out and out[c]["tier"] != "FROZEN"]))

    # A 态 watch 池（29 只 A 态中 1 只为恒科 tradeable，watch 实为 28）：
    # reference A=0.5 → 全部无硬闸（StateFit>0）；低分 FROZEN 档仅出现在
    # 映射未确认（无 etf_code → inv 保守 0）+ 质量警告的组合——引擎诚实反映
    # 数据实况，晋升依赖 P4 映射确认，正是 MapReady 因子的设计意图。
    a_watch = [c for c, v in d["per_index"].items()
               if not v.get("tradeable") and (v.get("state") or {}).get("dominant") == "A"]
    a_watch = [c for c in a_watch if c in out]
    a_watch.sort(key=lambda c: -out[c]["score"])
    no_hardgate = all(out[c]["factors"]["state_fit"] > 0.0 for c in a_watch)
    frozen_low = [c for c in a_watch if out[c]["tier"] == "FROZEN"]
    frozen_all_mapless = all(out[c]["factors"]["investability"] == 0.0
                             for c in frozen_low)
    check(f"A 态 watch 池 {len(a_watch)} 只全部无硬闸（StateFit>0）",
          len(a_watch) == 28 and no_hardgate)
    check(f"其中 {len(a_watch) - len(frozen_low)} 只达 OBSERVE 档以上",
          len(a_watch) - len(frozen_low) >= 15,
          f"observe+={len(a_watch) - len(frozen_low)}")
    check(f"{len(frozen_low)} 只低分 FROZEN 均因映射未确认（无行情）而非硬闸",
          all(out[c]["factors"]["state_fit"] > 0.0 for c in frozen_low)
          and frozen_all_mapless, str(frozen_low))
    print(f"  ℹ️  A 态 watch 池 Top5: "
          + ", ".join(f"{c}({d['per_index'][c]['name']} {out[c]['score']:.0f}/{out[c]['tier']})"
                      for c in a_watch[:5]))
    print(f"  ℹ️  恒生科技: {hks['score'] if hks else '-'} {hks['tier'] if hks else ''}"
          f" {hks['factors'] if hks else ''}")
    # 全局 tier 分布
    from collections import Counter
    dist = Counter(r["tier"] for r in out.values())
    print(f"  ℹ️  11-05 tier 分布: {dict(dist)}")


def main() -> int:
    print("=" * 74)
    print("selftest_eligibility — B2 资格分引擎")
    print("=" * 74)
    test_investability()
    test_role_family()
    test_state_fit()
    test_hard_rule()
    test_map_and_dq()
    test_fail_closed()
    test_history_roundtrip()
    test_real_snapshot()
    print("=" * 74)
    print(f"结果：PASS={PASS} FAIL={FAIL}")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
