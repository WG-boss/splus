# -*- coding: utf-8 -*-
"""selftest_disclosure.py — 口径披露 + 经验转移矩阵的自测

覆盖：
  * disclosure.build 的静态/动态条目、级别排序、缺失数据时不崩；
  * state_transition 的矩阵自检、annotate 字段、persistence_for 默认标量。

跑法：python selftest_disclosure.py
"""

from __future__ import annotations

import json
import os
import tempfile

import disclosure as D
import state_transition as S

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ✓ %s" % name)
    else:
        _fail += 1
        print("  ✗ %s  %s" % (name, detail))


CFG = {
    "state": {"min_confidence_to_act": 0.65},
    "disclosure": {"enabled": True, "items": [
        {"id": "s1", "level": "LOW", "text": "低级别"},
        {"id": "s2", "level": "HIGH", "text": "高级别"}]},
}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="disctest_")

    print("【T1】静态条目必带 id / level，且按级别排序")
    res = {"per_index": {}, "as_of": "2026-11-06"}
    items = D.build(CFG, res, tmp)
    check("两条静态披露都在", {i["id"] for i in items} >= {"s1", "s2"})
    check("HIGH 排在 LOW 前", items[0]["level"] == "HIGH", str([i["level"] for i in items]))
    check("每条都有 id", all(i.get("id") for i in items))

    print("\n【T2】低置信 → 动态披露 NA 占比")
    res2 = {"as_of": "2026-11-06", "per_index": {
        "a": {"tradeable": True, "state": {"rating": "NA"}},
        "b": {"tradeable": True, "state": {"rating": "C"}},
        "c": {"tradeable": False, "state": {"rating": "NA"}},
        "d": {"tradeable": False, "state": {"rating": "E"}},
    }}
    items2 = D.build(CFG, res2, tmp)
    na = next((i for i in items2 if i["id"] == "d_na"), None)
    check("有 d_na 条目", na is not None)
    check("2/4 只 NA", na and "2/4" in na["text"], na["text"] if na else "")

    print("\n【T3】池为空/异常时不得崩，也不得编造")
    res3 = {"per_index": {}, "pool": {"enabled": True, "members": [],
                                      "universe_executable": 0,
                                      "metrics": {}, "capacity": {}}}
    items3 = D.build(CFG, res3, tmp)
    check("池条目存在且成员数 0", any(i["id"] == "d_pool" and "池内 0 只" in i["text"]
                                      for i in items3))

    print("\n【T4】行情过期 → HIGH 级披露")
    res4 = {"per_index": {}, "pool": {"enabled": True, "members": [],
                                      "metrics": {}, "universe_executable": 5,
                                      "capacity": {"quote_stale": True,
                                                   "quote_age_hours": 94.5,
                                                   "unknown": 3}}}
    items4 = D.build(CFG, res4, tmp)
    q = next((i for i in items4 if i["id"] == "d_quote"), None)
    check("有 d_quote 且 HIGH", q is not None and q["level"] == "HIGH")
    check("写明小时数与 unknown 只数", q and "94.5" in q["text"] and "3" in q["text"])

    print("\n【T5】render_md 只挑 HIGH")
    md = D.render_md(items4, only_high=True)
    check("只留 HIGH", all("[HIGH]" in line for line in md), str(md))
    check("至少一条", len(md) >= 1)

    print("\n【T6】转移矩阵自检（行和=1、自持率∈(0,1)、期望持续>0）")
    mat = S.load()
    if not mat:
        m = S.rebuild()
    else:
        m = mat
    ok_rows = True
    for st, row in m["states"].items():
        sm = sum(row["next"].values())
        if abs(sm - 1.0) > 0.02:
            ok_rows = False
        if not (0 < row["self_hold"] < 1):
            ok_rows = False
        if not row.get("expected_duration_days") or row["expected_duration_days"] <= 0:
            ok_rows = False
    check("每行概率和 ≈1、自持率/期望持续合法", ok_rows)
    check("A 的期望持续 > B（吸筹比突破更黏）",
          m["states"]["A"]["expected_duration_days"] >
          m["states"]["B"]["expected_duration_days"])

    print("\n【T7】annotate 写入次态与期望持续，且不臆造")
    st = {"dominant": "B", "state_days": 1}
    S.annotate(st, m, {"state": {"persistence": 0.55}})
    tr = st.get("transition") or {}
    check("有 expected_duration_days", bool(tr.get("expected_duration_days")))
    check("有 most_likely_next 且不是自己", tr.get("most_likely_next") not in (None, "B"),
          str(tr.get("most_likely_next")))
    check("age_ratio = 已持续/期望", abs((tr.get("age_ratio") or 0) - 1.0 /
                                        tr["expected_duration_days"]) < 0.02)
    st2 = {"dominant": "B"}
    S.annotate(st2, None, {})
    check("矩阵缺失时什么都不写", "transition" not in st2)

    print("\n【T8】persistence_for 默认标量，per_state 才查表")
    check("标量模式返回 0.55",
          abs(S.persistence_for("A", {"state": {"persistence": 0.55}}) - 0.55) < 1e-9)
    cfg_ps = {"state": {"persistence": 0.55, "persistence_mode": "per_state",
                        "persistence_by_state": {"A": 0.945}}}
    check("per_state 模式返回 0.945",
          abs(S.persistence_for("A", cfg_ps) - 0.945) < 1e-9)
    check("per_state 但表缺失 → 回落标量",
          abs(S.persistence_for("Z", cfg_ps) - 0.55) < 1e-9)

    print("\n【T9】主状态历史覆盖不足 → HIGH 护栏；充足时不打扰")
    cfg_h = {"state": {"min_confidence_to_act": 0.65},
             "disclosure": {"enabled": True, "items": [], "min_history_rows": 250},
             "data": {"state_file": "state/last_state.json"}}
    hd = os.path.join(tmp, "state")
    os.makedirs(hd, exist_ok=True)
    hp = os.path.join(hd, "state_dominant_history.jsonl")
    with open(hp, "w", encoding="utf-8") as fh:
        for i in range(9):
            fh.write(json.dumps({"date": "2026-10-%02d" % (21 + i),
                                 "d": {"a": "E"}}) + "\n")
    it9 = D.build(cfg_h, {"per_index": {}, "as_of": "2026-11-06"}, tmp)
    h9 = next((i for i in it9 if i["id"] == "d_state_hist"), None)
    check("9 行 < 250 → 有 HIGH 护栏", h9 is not None and h9["level"] == "HIGH")
    check("写明行数与回填命令",
          h9 and "9 行" in h9["text"] and "tools_state_history_backfill.py" in h9["text"],
          h9["text"] if h9 else "")
    with open(hp, "w", encoding="utf-8") as fh:
        for i in range(300):
            fh.write(json.dumps({"date": "2025-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                                 "d": {"a": "E"}}) + "\n")
    it10 = D.build(cfg_h, {"per_index": {}, "as_of": "2026-11-06"}, tmp)
    check("300 行 ≥ 250 → 不产生噪音",
          not any(i["id"] == "d_state_hist" for i in it10))
    cfg_off = {"disclosure": {"enabled": True, "items": []}}
    check("未配门槛 → 检查关闭（向后兼容）",
          not any(i["id"] == "d_state_hist"
                  for i in D.build(cfg_off, {"per_index": {}}, tmp)))
    os.remove(hp)
    it11 = D.build(cfg_h, {"per_index": {}}, tmp)
    h11 = next((i for i in it11 if i["id"] == "d_state_hist"), None)
    check("文件缺失 → 也提示 HIGH 且写明缺失",
          h11 is not None and "缺失" in h11["text"], h11["text"] if h11 else "")

    print("\n" + "=" * 42)
    print("PASS=%d  FAIL=%d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
