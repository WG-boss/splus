# -*- coding: utf-8 -*-
"""selftest_pool.py — tools_pool_dynamic.py 的自测（纯构造数据，已知答案）

覆盖点：排序正确性、缓冲带语义、主题上限、容量闸门、成本口径、
无前视、相关系数、Newey-West 退化情形。
"""

from __future__ import annotations

import math
import sys

import tools_pool_dynamic as X

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


def mk_days(codes: list, n_days: int, w_of=None, rs_of=None,
            bucket_of=None) -> list:
    """构造 n_days 天 × codes 的面板日期表。"""
    out = []
    for t in range(n_days):
        rows = []
        for c in codes:
            rows.append({
                "c": c, "s": "C", "k": 0.8,
                "w": (w_of(c, t) if w_of else 0.5),
                "cl": 100.0 + t,
                "rs20": (rs_of(c, t) if rs_of else 0.0),
                "bucket": (bucket_of(c) if bucket_of else "b1"),
                "role": "上游映射", "tradeable": False,
            })
        out.append({"d": "2024-01-%02d" % (t + 1), "rows": rows})
    return out


def main() -> int:
    print("【T1】按评分排序取前 N 必须严格递减")
    codes = ["c%02d" % i for i in range(20)]
    days = mk_days(codes, 4, w_of=lambda c, t: int(c[1:]) / 100.0)
    pools = X.rule_top_score(days, n=10, smooth=1)
    check("首日前 10 = c19..c10",
          pools[0] == ["c%02d" % i for i in range(19, 9, -1)], str(pools[0][:3]))
    check("池大小恒为 10", all(len(p) == 10 for p in pools))

    print("\n【T2】smooth=3 必须用近 3 日均值，而不是当日")
    # c00 前 3 天很高，第 4 天暴跌；c01 反之
    def w2(c, t):
        if c == "c00":
            return 0.9 if t < 3 else 0.01
        if c == "c01":
            return 0.01 if t < 3 else 0.99
        return 0.2
    d2 = mk_days(["c00", "c01"] + ["x%02d" % i for i in range(10)], 4, w_of=w2)
    p1 = X.rule_top_score(d2, n=3, smooth=1)
    p3 = X.rule_top_score(d2, n=3, smooth=3)
    check("1 日口径：第 4 天 c01 进前 3", "c01" in p1[3], str(p1[3]))
    check("3 日口径：第 4 天 c00 仍在（均值未掉出）", "c00" in p3[3], str(p3[3]))

    print("\n【T3】缓冲带：进 10 / 出 15")
    # 16 只，第 0 天按 A 排序；第 1 天把原第 1 名降到第 12 名
    n = 20
    cs = ["c%02d" % i for i in range(n)]

    def rs3(c, t):
        i = int(c[1:])
        if t == 0:
            return 1.0 - i * 0.01
        # c00 掉到第 12；其余上移
        if i == 0:
            return 1.0 - 0.115
        return 1.0 - (i - 1) * 0.01
    d3 = mk_days(cs, 3, rs_of=rs3)
    pools3 = [["c%02d" % i for i in range(10)]] + [None, None]
    meta3 = {c: {"bucket": c} for c in cs}
    got = X.rule_funnel(d3, meta3, n_in=10, n_out=15, smooth=1,
                        amt20={c: {"2024-01-0%d" % (t + 1): 1e9 for t in range(3)}
                               for c in cs},
                        bars_n={c: 600 for c in cs})
    # smooth=1 时第 0 天用当日 rs，第 1 天 rs 变化 → 手工推演
    check("第 0 天按 RS 前 10", got[0] == cs[:10], str(got[0]))
    # 第 1 天：原 9 只（c01..c09）仍在 ≤15，c00 排第 12 → 仍保留（缓冲带语义）
    check("掉到第 12 名的原持仓被保留（缓冲带）", "c00" in got[1], str(got[1]))
    check("第 11 名从未入池者不得因第 12 名让位而进", "c10" not in got[1], str(got[1]))

    print("\n【T4】主题组上限")
    d4 = mk_days(cs, 2, rs_of=lambda c, t: 1.0 - int(c[1:]) * 0.01,
                 bucket_of=lambda c: "same" if c in cs[:8] else c)
    got4 = X.rule_funnel(d4, {c: {"bucket": ("same" if c in cs[:8] else c)}
                              for c in cs}, n_in=10, n_out=15, smooth=1,
                         max_per_bucket=3,
                         amt20={c: {"2024-01-01": 1e9, "2024-01-02": 1e9} for c in cs},
                         bars_n={c: 600 for c in cs})
    same = sum(1 for c in got4[0] if c in cs[:8])
    check("同 bucket 最多 3 只", same <= 3, "实际 %d" % same)
    check("池仍补满 10 只", len(got4[0]) == 10, str(len(got4[0])))

    print("\n【T5】容量闸门：成交额不足必须剔除")
    d5 = mk_days(cs, 2, rs_of=lambda c, t: 1.0 - int(c[1:]) * 0.01)
    amt5 = {}
    for c in cs:
        amt5[c] = {"2024-01-01": (1e9 if c != "c00" else 1e5),
                   "2024-01-02": (1e9 if c != "c00" else 1e5)}
    got5 = X.rule_funnel(d5, {c: {"bucket": c} for c in cs}, n_in=10, n_out=15,
                         smooth=1, amt20=amt5, bars_n={c: 600 for c in cs})
    check("成交额不足的第一名被剔除", "c00" not in got5[0], str(got5[0]))
    check("顺位补入第 11 名", "c10" in got5[0], str(got5[0][-3:]))

    print("\n【T6】无前视：最后一天不得有 fwd")
    d6 = mk_days(cs, 30, rs_of=lambda c, t: 0.0)
    X.attach_forward(d6, 20)
    check("末日 fwd 缺失", all("fwd" not in r for r in d6[-1]["rows"]))
    check("倒数第 21 日有 fwd", all("fwd" in r for r in d6[-22]["rows"]))
    r = d6[0]["rows"][0]
    check("fwd 等于 20 日后收益 (t=0 → t=20)",
          abs(r["fwd"] - (d6[20]["rows"][0]["cl"] / r["cl"] - 1)) < 1e-12,
          str(r["fwd"]))

    print("\n【T7】成本口径：零换手不扣钱，全换手扣 8bp")
    d7 = mk_days(cs, 31, rs_of=lambda c, t: 1.0 - int(c[1:]) * 0.01)
    for d in d7:                      # 价格恒定 → 日收益恰为 0，只剩成本
        for r in d["rows"]:
            r["cl"] = 100.0
    flat = [cs[:10] for _ in range(31)]
    s_flat = X.nav_stats(flat, d7, "flat")
    check("零换手组合年化=0（无成本、无漂移）", abs(s_flat["ann_pct"]) < 0.5,
          "ann %.4f" % s_flat["ann_pct"])
    # 每日全换（20 只里轮换 10 只 → 换手 100%）
    rot = [[cs[(i * 10 + k) % 20] for k in range(10)] for i in range(31)]
    s_rot = X.nav_stats(rot, d7[:31], "rot")
    check("全换手年化显著为负（成本拖累）", s_rot["ann_pct"] < -10,
          "ann %.1f" % s_rot["ann_pct"])

    print("\n【T8】spearman 已知答案")
    check("完全正相关 = 1", abs(X.spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) - 1) < 1e-12)
    check("完全负相关 = -1", abs(X.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) + 1) < 1e-12)
    check("常数序列 → None", X.spearman([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None)

    print("\n【T9】Newey-West 退化情形")
    m, se, t = X.nw_t([0.01] * 100, 10)
    check("常数序列 se=0 → t 为 nan 或 0", (t != t) or abs(t) < 1e-9, str(t))
    m2, se2, t2 = X.nw_t([0.001 * (1 if i % 2 else -1) for i in range(200)], 5)
    check("零均值噪声 |t| < 2", abs(t2) < 2.0, "t=%.2f" % t2)

    print("\n【T10】p 值与 t 的单调关系")
    check("|t|=1.96 → p≈0.05", abs(X.p_two(1.96) - 0.05) < 0.001, "%.4f" % X.p_two(1.96))
    check("|t|=0 → p=1", abs(X.p_two(0.0) - 1.0) < 1e-12)
    check("nan → p=1（保守）", X.p_two(float("nan")) == 1.0)

    print("\n" + "=" * 46)
    print("PASS=%d  FAIL=%d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
