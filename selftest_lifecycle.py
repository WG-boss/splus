# -*- coding: utf-8 -*-
"""selftest_lifecycle.py — B2 P3 生命周期规则 + 状态调制自测"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import lifecycle as L

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


def _mk_history(specs: dict, days: int) -> list:
    """specs: {code: score 或 (score, tier)}，生成 days 天连续历史。"""
    rows = []
    for i in range(days):
        scores = {}
        for code, sc in specs.items():
            score = sc[i] if isinstance(sc, (list, tuple)) else sc
            scores[code] = {"score": score,
                            "tier": "FULL" if score >= 80 else "FROZEN"}
        rows.append({"as_of": f"2026-11-{1 + i:02d}", "scores": scores})
    return rows


def test_promotion():
    print("[1] 晋升规则：≥80 连续 3 日（watch → 晋升建议）")
    rows = _mk_history({"931152": 85}, 4)
    r = L.evaluate(rows, {"931152": False}, "2026-11-04", "/nonexistent")
    check("连续 4 日 ≥85 → 晋升建议", len(r["promotions"]) == 1
          and r["promotions"][0]["code"] == "931152")
    rows2 = _mk_history({"931152": [85, 85, 50, 85]}, 4)
    r2 = L.evaluate(rows2, {"931152": False}, "2026-11-04", "/nonexistent")
    check("中断（第 3 日 50）→ 无晋升", len(r2["promotions"]) == 0)
    rows3 = _mk_history({"931152": [85, 50, 85, 85]}, 4)
    r3 = L.evaluate(rows3, {"931152": False}, "2026-11-04", "/nonexistent")
    check("仅末尾连续 2 日 → 无晋升", len(r3["promotions"]) == 0)
    r4 = L.evaluate(_mk_history({"931152": 85}, 3),
                    {"931152": True}, "2026-11-03", "/nonexistent")
    check("已 tradeable → 不出晋升建议", len(r4["promotions"]) == 0)


def test_demotion():
    print("[2] 降级规则：<60 连续 5 日（tradeable → 降级建议）")
    rows = _mk_history({"000688": 40}, 5)
    r = L.evaluate(rows, {"000688": True}, "2026-11-05", "/nonexistent")
    check("连续 5 日 40 → 降级建议", len(r["demotions"]) == 1
          and r["demotions"][0]["code"] == "000688")
    rows2 = _mk_history({"000688": [40, 40, 40, 40, 90]}, 5)
    r2 = L.evaluate(rows2, {"000688": True}, "2026-11-05", "/nonexistent")
    check("末尾回升 90 → 无降级", len(r2["demotions"]) == 0)
    rows3 = _mk_history({"000688": 40}, 4)
    r3 = L.evaluate(rows3, {"000688": True}, "2026-11-04", "/nonexistent")
    check("仅 4 日 → 未达阈值", len(r3["demotions"]) == 0)


def test_cooldown(tmp: str):
    print("[3] 冷却期：降级后 10 交易日阻断晋升")
    state = {"931152": {"downgraded_at": "2026-11-03", "last_score": 55.0}}
    sp = os.path.join(tmp, "state_lifecycle.json")
    json.dump(state, open(sp, "w", encoding="utf-8"))
    cfg = {"state_path": sp, "cooldown_days": 10}
    rows = _mk_history({"931152": 85}, 5)
    r = L.evaluate(rows, {"931152": False}, "2026-11-08", "/nonexistent", cfg)
    check("资格 85 但冷却期内 → 阻断", len(r["promotions"]) == 0
          and len(r["cooldown_active"]) == 1)
    check("剩余冷却天数正确", r["cooldown_active"][0]["cooldown_remaining"] == 5,
          str(r["cooldown_active"]))
    r2 = L.evaluate(rows, {"931152": False}, "2026-11-20", "/nonexistent", cfg)
    check("冷却期满 → 恢复晋升建议", len(r2["promotions"]) == 1
          and len(r2["cooldown_active"]) == 0)


def test_events_and_state(tmp: str):
    print("[4] 事件台账 append 幂等 + 降级状态落盘")
    result = {"promotions": [{"code": "A1", "kind": "promotion", "days": 3,
                              "last_score": 85.0}],
              "demotions": [{"code": "A2", "kind": "demotion", "days": 5,
                             "last_score": 45.0}],
              "cooldown_active": []}
    n1 = L.record_events(tmp, "2026-11-06", result)
    n2 = L.record_events(tmp, "2026-11-06", result)  # 同日重跑
    check("首写 2 条", n1 == 2, f"got {n1}")
    check("同日幂等 0 追加", n2 == 0, f"got {n2}")
    L.mark_demotions(tmp, "2026-11-06", result)
    st = json.load(open(os.path.join(tmp, "state", "eligibility_lifecycle.json"),
                        encoding="utf-8"))
    check("降级状态落盘 downgraded_at",
          st.get("A2", {}).get("downgraded_at") == "2026-11-06")
    L.mark_demotions(tmp, "2026-11-07", result)  # 已有 → 不覆盖
    st2 = json.load(open(os.path.join(tmp, "state", "eligibility_lifecycle.json"),
                         encoding="utf-8"))
    check("已有降级记录不覆盖", st2["A2"]["downgraded_at"] == "2026-11-06")


def test_modulation():
    print("[5] 状态调制乘数 M")
    check("E 占比 10% → 1.0", L.modulation_multiplier(0.10) == 1.0)
    check("E 占比 20% → 1.0", L.modulation_multiplier(0.20) == 1.0)
    check("E 占比 40% → 0.75", abs(L.modulation_multiplier(0.40) - 0.75) < 1e-9)
    check("E 占比 66% → 0.5（11-05 实况）",
          abs(L.modulation_multiplier(0.6610) - 0.5) < 1e-9)
    check("E 占比 100% → 下限 0.5", L.modulation_multiplier(1.0) == 0.5)
    check("自定义 floor", L.modulation_multiplier(1.0, {"floor": 0.3}) == 0.3)
    check("禁用（floor=1）", L.modulation_multiplier(0.8, {"floor": 1.0}) == 1.0)


def main() -> int:
    print("=" * 74)
    print("selftest_lifecycle — B2 P3 生命周期 + 状态调制")
    print("=" * 74)
    test_promotion()
    test_demotion()
    with tempfile.TemporaryDirectory() as tmp:
        test_cooldown(tmp)
        test_events_and_state(tmp)
    test_modulation()
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
