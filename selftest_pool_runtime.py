# -*- coding: utf-8 -*-
"""selftest_pool_runtime.py — pool.py（R6 生产运行时）的自测（纯构造数据、已知答案）

覆盖：排序轴与状态无关、缓冲带（进 10 / 出 30）、容量闸与行情过期降级、
无前视、状态动作映射、熔断冻结、落盘幂等、dynamic/shadow 权限差异。

跑法：python selftest_pool_runtime.py
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile

import pool as P

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


def mk_bars(n: int = 300, step: float = 0.002, amount: float = 1e9,
            close0: float = 100.0) -> list:
    out = []
    d0 = datetime.date(2024, 1, 1)
    c = close0
    for i in range(n):
        c = c * (1 + step)
        d = d0 + datetime.timedelta(days=i)
        out.append({"date": d.isoformat(), "open": c, "high": c, "low": c,
                    "close": c, "vol": 1e6, "amount": amount})
    return out


def mk_universe(n: int = 20, conf: float = 0.9) -> tuple:
    """code → loaded / per_index；第 i 只的日涨幅 = i*0.0005（rs20 单调）。"""
    loaded, per = {}, {}
    for i in range(n):
        c = "c%02d" % i
        loaded[c] = {"bars": mk_bars(step=0.0005 * i),
                     "quality_warnings": []}
        per[c] = {"name": "标的%d" % i, "tradeable": False,
                  "role": "筛选源观察", "bucket": "w",
                  "state": {"dominant": "C", "confidence": conf},
                  "instrument": {"status": "confirmed", "verified": True,
                                 "etf_code": "5%05d" % i}}
    return loaded, per


CFG = {
    "state": {"min_confidence_to_act": 0.65},
    "execution_plan": {"quotes_file": "data/etf_quotes.json",
                       "price_max_age_hours": 72},
    "pool": {"mode": "shadow", "size": 10, "n_out": 30, "smooth": 5,
             "amt_floor_yi": 0.0, "min_bars": 100, "min_cross_section": 5,
             "aum_yi": 0.3, "adv_share": 0.05, "etf_adv_floor_yi": 0.0},
}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pooltest_")

    print("【T1】排序轴是 rs_20，不是五态评分/状态")
    loaded, per = mk_universe()
    # 把状态与置信度做成与强弱完全相反，若排序轴误用 state 就会翻过来
    for i, c in enumerate(sorted(per)):
        per[c]["state"] = {"dominant": "A" if i < 10 else "E",
                           "confidence": 0.9 if i < 10 else 0.9}
    as_of = loaded["c00"]["bars"][-1]["date"]
    p1 = P.build(CFG, per, loaded, as_of, tmp)
    codes = [m["code"] for m in p1["members"]]
    check("池内 10 只", len(codes) == 10, str(len(codes)))
    check("按 rs20 降序 = c19..c10", codes == ["c%02d" % i for i in range(19, 9, -1)],
          str(codes))
    check("E 态标的照样能进池（状态不进排序）",
          any(m["state"] == "E" for m in p1["members"]))

    print("\n【T2】缓冲带：老成员排名 <30 保留，跌出 30 才卖")
    prev = {"as_of": "2024-01-01", "members": [
        {"code": "c00", "name": "标的0", "since": "2024-01-01", "adv20": 1e9},
        {"code": "c01", "name": "标的1", "since": "2024-01-01", "adv20": 1e9}]}
    per2 = json.loads(json.dumps(per))
    p2 = P.build(CFG, per2, loaded, as_of, tmp, prev)
    inpool = {m["code"] for m in p2["members"]}
    check("排名垫底的老成员被保留（缓冲带生效）",
          "c00" in inpool and "c01" in inpool, str(sorted(inpool)))
    check("保留仓标记为 keep", any(m["code"] == "c00" and m["reason"] == "keep"
                                   for m in p2["members"]))
    prev_far = {"as_of": "2024-01-01", "members": [
        {"code": "c00", "name": "标的0", "since": "2024-01-01", "adv20": 1e9}]}
    cfg_small = json.loads(json.dumps(CFG))
    cfg_small["pool"]["size"] = 2
    cfg_small["pool"]["n_out"] = 3
    p3 = P.build(cfg_small, per2, loaded, as_of, tmp, prev_far)
    check("排名 ≥ n_out 的老成员出池",
          "c00" not in {m["code"] for m in p3["members"]},
          str([m["code"] for m in p3["members"]]))
    check("出池原因标 rank_out",
          any(e["code"] == "c00" and e["reason"] == "rank_out" for e in p3["exits"]))

    print("\n【T3】容量闸：ETF 日额不足 → 不进池（行情新鲜时）")
    dp = os.path.join(tmp, "data")
    os.makedirs(dp, exist_ok=True)
    qpath = os.path.join(dp, "etf_quotes.json")
    # need = (0.3亿/10)/5% = 6000 万（0.6 亿）；给 c19（第一名）只有 100 万
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "quotes": {"5%05d" % 19: {"amount": 1e6}}}, fh)
    p4 = P.build(CFG, per, loaded, as_of, tmp)
    check("日额 100 万 < 需 6000 万 → 出局",
          "c19" not in {m["code"] for m in p4["members"]},
          str([m["code"] for m in p4["members"]]))
    check("拒因记录 capacity",
          any(r["code"] == "c19" and r["reason"] == "capacity" for r in p4["rejected"]))

    print("\n【T4】行情过期 → 容量降级为 flag，不误杀")
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": (datetime.datetime.now()
                                  - datetime.timedelta(hours=100)).isoformat(timespec="seconds"),
                   "quotes": {"5%05d" % 19: {"amount": 3.5e7}}}, fh)
    p5 = P.build(CFG, per, loaded, as_of, tmp)
    check("过期快照：c19 仍可进池",
          "c19" in {m["code"] for m in p5["members"]})
    check("容量状态标 stale",
          any((m.get("capacity") or {}).get("status") == "stale"
              for m in p5["members"] if m["code"] == "c19"))
    check("capacity.unknown 统计包含 stale", (p5.get("capacity") or {}).get("unknown", 0) >= 1)

    print("\n【T5】无前视：as_of 之后的 bar 不影响任何分数")
    tmp2 = tempfile.mkdtemp(prefix="pooltest_nolook_")   # 干净目录，避免上一测试的行情文件干扰
    loaded_cut, per_cut = mk_universe()
    for c in loaded_cut:
        bars = loaded_cut[c]["bars"]
        cut = [b for b in bars if b["date"] <= as_of]
        tail = [dict(b, close=b["close"] * 3.0,
                     date=(datetime.date.fromisoformat(as_of)
                           + datetime.timedelta(days=k + 1)).isoformat())
                for k, b in enumerate(bars[-5:])]
        loaded_cut[c]["bars"] = cut + tail
    p6 = P.build(CFG, per_cut, loaded_cut, as_of, tmp2)
    s_a = {m["code"]: m["rs20"] for m in p1["members"]}
    s_b = {m["code"]: m["rs20"] for m in p6["members"]}
    check("as_of 后插入 3 倍价格，rs20 完全不变", s_a == s_b,
          "%s vs %s" % (list(s_a.items())[:2], list(s_b.items())[:2]))

    print("\n【T6】状态动作：E 禁新增 / 低置信 NA / D 减半")
    loaded7, per7 = mk_universe(12, conf=0.9)
    # 状态挂在**排名靠前**的标的上，确保它们真能进池（否则测不到动作映射）
    _order = sorted(per7)[::-1]
    e_code, na_code, d_code = _order[0], _order[1], _order[2]
    per7[e_code]["state"] = {"dominant": "E", "confidence": 0.9}
    per7[na_code]["state"] = {"dominant": "C", "confidence": 0.30}
    per7[d_code]["state"] = {"dominant": "D", "confidence": 0.9}
    p7 = P.build(CFG, per7, loaded, as_of, tmp2)
    mm = {m["code"]: m for m in p7["members"]}
    check("E 态 → hold_only 且不可加仓",
          e_code in mm and mm[e_code]["state_action"] == "hold_only"
          and not mm[e_code]["add_allowed"], str(mm.get(e_code)))
    check("conf 0.30 < 0.65 → 评级 NA",
          na_code in mm and mm[na_code]["rating"] == "NA", str(mm.get(na_code)))
    check("NA → 禁止加仓", na_code in mm and not mm[na_code]["add_allowed"])
    check("D 态 → half（权重 ×0.5）",
          d_code in mm and mm[d_code]["state_action"] == "half"
          and abs(mm[d_code]["weight_mult"] - 0.5) < 1e-9, str(mm.get(d_code)))

    print("\n【T7】落盘幂等：同一 as_of 写两次只留一行历史")
    cfg_w = json.loads(json.dumps(CFG))
    cfg_w["pool"]["mode"] = "shadow"
    P.save(tmp, cfg_w, p1)
    P.save(tmp, cfg_w, p1)
    with open(os.path.join(tmp, "state", "tradeable_pool_history.jsonl"),
              encoding="utf-8") as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    check("历史仅 1 行", len(rows) == 1, str(len(rows)))

    print("\n【T8】熔断：ADV 骤降 >50% → 出池 + 冻结")
    prev8 = {"as_of": "2024-01-01", "members": [
        {"code": "c19", "name": "标的19", "since": "2024-01-01", "adv20": 1e12}]}
    p8 = P.build(CFG, per, loaded, as_of, tmp, prev8)
    check("c19 被强制出池", "c19" not in {m["code"] for m in p8["members"]})
    check("记录冻结原因", bool(p8.get("frozen_today")))

    print("\n【T9】dynamic 覆盖 tradeable；shadow 不改权限")
    per_d = json.loads(json.dumps(per))
    P.apply(per_d, p1, mode="dynamic")
    check("dynamic：池内 tradeable=True",
          all(v["tradeable"] for k, v in per_d.items()
              if k in {m["code"] for m in p1["members"]}))
    check("dynamic：池外 tradeable=False",
          all(not v["tradeable"] for k, v in per_d.items()
              if k not in {m["code"] for m in p1["members"]}))
    per_s = json.loads(json.dumps(per))
    P.apply(per_s, p1, mode="shadow")
    check("shadow：权限完全不变", all(not v["tradeable"] for v in per_s.values()))

    print("\n【T10】桶敞口单一口径：桶 = 成员 final 求和（只在 bucket_max / haircut 截断）")
    import engine as E
    per_x = {}
    for i, b in enumerate(["core", "core", "theme"]):
        per_x["x%d" % i] = {"tradeable": True, "bucket": b,
                            "sizing": {"final": 0.05}}
    cfg_x = {"risk": {"bucket_max": 0.30, "theme_buckets": ["theme"],
                      "theme_cap_under_e": 0.06}}
    bx, pre = E.build_bucket_exposure(per_x, cfg_x, haircut=1.0, e_dom_n=0)
    check("core 桶 = 0.05+0.05", abs(bx["core"]["exposure"] - 0.10) < 1e-9,
          str(bx["core"]))
    check("total_pre = 各桶之和", abs(pre - 0.15) < 1e-9, str(pre))
    check("无截断时不写 cap 说明", "截断" not in bx["core"]["note"], bx["core"]["note"])
    per_x["x9"] = {"tradeable": True, "bucket": "core", "sizing": {"final": 0.40}}
    bx2, _ = E.build_bucket_exposure(per_x, cfg_x, haircut=1.0, e_dom_n=0)
    check("求和 0.50 > 桶上限 0.30 → 截断到 0.30",
          abs(bx2["core"]["exposure"] - 0.30) < 1e-9, str(bx2["core"]))
    check("截断写明求和值与上限", "截断" in bx2["core"]["note"], bx2["core"]["note"])
    bx3, _ = E.build_bucket_exposure(per_x, cfg_x, haircut=0.5, e_dom_n=0)
    check("开关 haircut 作用在截断之后",
          abs(bx3["core"]["exposure"] - 0.15) < 1e-9, str(bx3["core"]))

    print("\n" + "=" * 42)
    print("PASS=%d  FAIL=%d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
