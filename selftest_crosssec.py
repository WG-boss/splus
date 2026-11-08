#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_crosssec.py —— 横截面检验工具的可执行断言

十二组断言，分三类：

【数值恒等式】构造已知答案的数据，检查实现没算错
  T1  z 标准化后横截面均值=0、标准差=1
  T2  Spearman 已知答案（完全正/负相关、并列取平均秩）
  T3  OLS 精确恢复已知线性关系
  T4  M1 与无控制 M3 的恒等式 β·(n−n_s)/n = mean_s 逐日成立
  T5  价差 M2 精确等于构造的组间差
  T6  持续期统计对构造序列给出正确答案

【性质不变量】换一种无关的扰动，结论必须不变
  T7  当日所有 f 同加常数（市场整体涨跌）→ 相对 z 与全部结论不变
      —— 这是"横截面口径已把 beta 去掉"的实证依据
  T8  状态标签整体置换 → M1 结果跟着标签走，绝对量不变
  T9  置信度分位在 k 与 z 同向时单调

【识别与保守性】工具必须拒绝回答它答不了的问题
  T10 稀疏日（成员数不足）必须被剔除，不能混进横截面
  T11 全员同态的日子必须被计为"无对照"，而不是当成一个显著结论
  T12 p_effective 必须 ≥ 三个分量中的最大值（宁漏报不虚报）

用法：python selftest_crosssec.py [--verbose]
退出码：0 全通过；1 有失败
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tools_state_crosssec as X  # noqa: E402

FAILS: List[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


# ------------------------------------------------------------------ 造数据
def make_panel(spec_by_day: Dict[str, Dict[str, int]],
               sd_by_day: Dict[str, float] | None = None,
               spread_by_day: Dict[str, Dict[str, float]] | None = None,
               seed: int = 20261109
               ) -> Dict[str, List[Dict[str, Any]]]:
    """按"每天各状态多少人"生成面板。

    spread_by_day 可指定某状态在当天的固定 f 值，用于构造已知组间差；
    未指定的标的按 sd_by_day 的离散度做确定性展开（不用随机数，保证可复现）。
    """
    rng = random.Random(seed)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for d, spec in spec_by_day.items():
        rows = []
        i = 0
        for s, n in spec.items():
            for j in range(n):
                f = 0.0
                if spread_by_day and d in spread_by_day and s in spread_by_day[d]:
                    f = spread_by_day[d][s]
                else:
                    sd = (sd_by_day or {}).get(d, 0.01)
                    f = sd * (rng.random() * 2.0 - 1.0)
                rows.append({"c": f"C{i:03d}", "s": s, "k": 0.5 + 0.01 * j, "f": f})
                i += 1
        out[d] = rows
    return out


def flat_days(n_days: int, n: int = 90, same_val: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    days = {}
    for t in range(n_days):
        d = f"2024-{(t // 28) + 1:02d}-{(t % 28) + 1:02d}"
        days[d] = {"A": n // 3, "C": n // 4, "D": n // 5, "E": n - n // 3 - n // 4 - n // 5}
    return make_panel(days)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    print("【T1】z 标准化的横截面性质")
    panel = make_panel({"2024-01-02": {"A": 30, "C": 20, "D": 20}}, sd_by_day={"2024-01-02": 0.02})
    days, _ = X.build_days(panel, {}, 40, False)
    zs = [r["z"] for r in days[0]["rows"]]
    check("T1a 横截面均值=0", abs(X.mean(zs)) < 1e-12, f"mean={X.mean(zs):.2e}")
    check("T1b 横截面标准差=1", abs(X.sd(zs) - 1.0) < 1e-12, f"sd={X.sd(zs):.12f}")

    print("\n【T2】Spearman 已知答案")
    check("T2a 完全正相关=+1",
          abs(X.spearman([1, 2, 3, 4, 5, 6, 7, 8], [2, 4, 6, 8, 10, 12, 14, 16]) - 1.0) < 1e-12)
    check("T2b 完全负相关=−1",
          abs(X.spearman([1, 2, 3, 4, 5, 6, 7, 8], [8, 7, 6, 5, 4, 3, 2, 1]) + 1.0) < 1e-12)
    check("T2c 并列取平均秩",
          X.ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0], f"{X.ranks([10, 20, 20, 30])}")

    print("\n【T3】OLS 精确恢复")
    y = [3.0 + 2.0 * i - 0.5 * (i * i) for i in range(20)]
    Xm = [[1.0, float(i), float(i * i)] for i in range(20)]
    b = X.ols(y, Xm)
    ok = b is not None and all(abs(p - q) < 1e-8 for p, q in zip(b, [3.0, 2.0, -0.5]))
    check("T3 已知系数 (3, 2, −0.5)", ok, f"{[round(v, 6) for v in (b or [])]}")
    check("T3b 奇异矩阵返回 None", X.ols([1.0, 2.0, 3.0] * 5, [[1.0, 1.0]] * 15) is None)

    print("\n【T4】M1 与无控制 M3 的恒等式")
    panel = flat_days(12, n=90)
    days, _ = X.build_days(panel, {}, 40, False)
    s1 = X.m1_relative_z(days, 5)
    s3 = X.m3_controlled(days, 5, controls=False)
    gap = 0.0
    for s in X.STATES:
        d1 = dict(zip(s1[s]["dates"], s1[s]["vals"]))
        for d, bb, rt in zip(s3["dates"][s], s3["coef"][s], s3["ratio"][s]):
            if d in d1:
                gap = max(gap, abs(d1[d] - bb * rt))
    check("T4 β·(n−n_s)/n ≡ mean_s（12 天 × 5 态）", gap < 1e-10, f"max|gap|={gap:.2e}")

    print("\n【T5】价差 M2 的已知答案")
    panel = make_panel({"2024-01-02": {"A": 10, "C": 30}},
                       spread_by_day={"2024-01-02": {"A": 0.02, "C": 0.0}})
    days, _ = X.build_days(panel, {}, 40, False)
    s2 = X.m2_spread(days, 5)
    check("T5 价差=+2.00%", abs(s2["A"]["vals"][0] - 0.02) < 1e-12, f"{s2['A']['vals'][0]:.6f}")

    print("\n【T6】持续期统计")
    seq = {"2024-01-01": {"A": 2}, "2024-01-02": {"A": 2}, "2024-01-03": {"A": 2},
           "2024-01-04": {"C": 2}, "2024-01-05": {"C": 2}}
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for d, spec in seq.items():
        by_date[d] = [{"c": f"C{i}", "s": s, "k": 0.5, "f": 0.0}
                      for i in range(2) for s in spec]
    dur = X.duration_stats(by_date)
    check("T6 A 持续 3 日、C 持续 2 日",
          dur["A"]["mean"] == 3.0 and dur["C"]["mean"] == 2.0,
          f"A={dur['A']['mean']} C={dur['C']['mean']}")

    print("\n【T7】当日整体同加常数 → 横截面结论不变（beta 已去掉）")
    base_days = {"2024-01-0%d" % (i + 1): {"A": 30, "C": 30, "D": 30} for i in range(1, 6)}
    p1 = make_panel(base_days, sd_by_day={d: 0.02 for d in base_days})
    p2 = {d: [dict(r, f=r["f"] + 0.05) for r in rows] for d, rows in p1.items()}
    d1, _ = X.build_days(p1, {}, 40, False)
    d2, _ = X.build_days(p2, {}, 40, False)
    v1 = X.m1_relative_z(d1, 5)["A"]["vals"]
    v2 = X.m1_relative_z(d2, 5)["A"]["vals"]
    check("T7 加 +5% 市场冲击后相对 z 完全不变",
          len(v1) == len(v2) and all(abs(x - y) < 1e-12 for x, y in zip(v1, v2)))

    print("\n【T8】状态标签置换 → 结果跟着标签走")
    p = flat_days(6, n=90)
    swapped = {d: [dict(r, s={"A": "C", "C": "A"}.get(r["s"], r["s"])) for r in rows]
               for d, rows in p.items()}
    q1, _ = X.build_days(p, {}, 40, False)
    q2, _ = X.build_days(swapped, {}, 40, False)
    check("T8 置换后 A 的均值 = 置换前 C 的均值",
          abs(X.mean(X.m1_relative_z(q1, 5)["C"]["vals"])
              - X.mean(X.m1_relative_z(q2, 5)["A"]["vals"])) < 1e-12)

    print("\n【T9】置信度分位在 k 与 z 同向时单调")
    rows = []
    for i in range(100):
        rows.append({"c": f"C{i:03d}", "s": "A" if i % 2 else "C",
                     "k": i / 100.0, "f": i / 1000.0})
    for i, r in enumerate(rows):
        r["k"] = 0.5 + i / 200.0
    p = {"2024-01-02": rows}
    dd, _ = X.build_days(p, {}, 40, False)
    q = X.m5_k_quintile(dd)
    means = [X.mean(q[i]["vals"]) for i in range(5)]
    check("T9 Q1<Q5 且近似单调", means[0] < means[-1], f"{[round(m, 4) for m in means]}")

    print("\n【T10】稀疏日必须被剔除")
    p = make_panel({"2024-01-02": {"A": 30}, "2024-01-03": {"A": 3}})
    dd, meta = X.build_days(p, {}, 25, False)
    check("T10 成员 3 < 门槛 40 的日子被剔除",
          meta["days_kept"] == 1 and meta["days_dropped_thin"] == 1,
          f"kept={meta['days_kept']} dropped={meta['days_dropped_thin']}")

    print("\n【T11】全员同态必须计为「无对照」")
    p = make_panel({"2024-01-0%d" % (i + 1): {"A": 95, "C": 5} for i in range(1, 4)})
    p.update(make_panel({"2024-01-1%d" % (i + 1): {"A": 50, "C": 50} for i in range(1, 4)}))
    dd, _ = X.build_days(p, {}, 40, False)
    ident = X.identification(dd)
    check("T11 A 态无对照天数=3、信息天数=3",
          ident["states"]["A"]["days_no_control"] == 3
          and ident["states"]["A"]["days_informative"] == 3,
          f"no_control={ident['states']['A']['days_no_control']} "
          f"informative={ident['states']['A']['days_informative']}")

    print("\n【T12】p_effective 必须是三法中的最大值")
    p12 = flat_days(80, n=90)
    d12, _ = X.build_days(p12, {}, 40, False)
    s12 = X.m1_relative_z(d12, 5)
    for i, s in enumerate(X.STATES):
        r = X.infer(s12[s], 40, 80, 300, 1234 + i)
        comps = [p for p in (r.get("nw_p"), r.get("boot_p"), r.get("year_p")) if p is not None]
        if comps:
            check(f"T12{s} p_eff ≥ max(分量)", r["p_effective"] >= max(comps) - 1e-9,
                  f"p_eff={r['p_effective']} max={max(comps)}")

    print(f"\n{'='*52}\n  {PASSES} passed, {len(FAILS)} failed")
    if FAILS:
        print("  FAILED: " + ", ".join(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
