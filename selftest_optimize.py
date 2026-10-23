# -*- coding: utf-8 -*-
"""
selftest_optimize.py —— 验证 optimize/sig_test 这套流程本身有效

为什么需要：如果优化流程对任何输入都说"不显著"，那它就是个摆设。
必须构造一个**已知有效**的信号，证明流程能检出。

测试设计：
  T1 强信号必须被检出：构造一个 fwd 与 bull 高度相关的样本，AUC 应 >0.75
  T2 纯噪声必须不被检出：fwd 与 bull 独立，AUC 应 ≈0.5，p 应 >0.05
  T3 方向反转必须被识别：AUC 应 <0.5
  T4 块 bootstrap 的 CI 宽度合理：不能是 0 宽（bug），也不能宽到无意义
  T5 命中率计算正确：全对 = 1.0，全错 = 0.0

退出码 0 = 通过
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimize as O
import sig_test2 as S

random.seed(7)
OK = []


def chk(name, cond, detail=""):
    OK.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'}  {name}  {detail}")


def mk(n, corr_strength, invert=False, dom_pick=None):
    """构造样本：bull 与 fwd 的相关强度可控。"""
    rows = []
    for i in range(n):
        u = random.random()
        bull = u
        noise = random.random()
        # corr_strength=1 → fwd 完全由 bull 决定；0 → 纯噪声
        z = corr_strength * bull + (1 - corr_strength) * noise
        fwd = (z - 0.5) * 0.2
        if invert:
            fwd = -fwd
        # dom：bull 高判 B/C，低判 E
        if dom_pick:
            dom = dom_pick
        else:
            dom = "C" if bull > 0.6 else ("E" if bull < 0.4 else "D")
        rows.append({"code": "x", "date": f"d{i:04d}", "bull": bull,
                     "fwd": fwd, "dom": dom, "fold": 0})
    return rows


def main():
    print("=" * 72)
    print("优化流程自身的有效性检验（否则测不出东西等于摆设）")
    print("=" * 72)

    # T1 强信号
    print("\n[T1] 强信号必须被检出")
    a = mk(300, 0.95)
    auc = S.auc_rows(a)
    chk("强相关样本 AUC > 0.75", auc > 0.75, f"AUC={auc:.4f}")

    # T2 纯噪声
    print("\n[T2] 纯噪声必须不被检出")
    noise_rows = mk(300, 0.0)
    aucn = S.auc_rows(noise_rows)
    chk("噪声样本 AUC 接近 0.5", abs(aucn - 0.5) < 0.08, f"AUC={aucn:.4f}")

    # 两个噪声集之间做 bootstrap，p 应 > 0.05
    b = mk(300, 0.0)
    r = S.block_bootstrap(noise_rows, b, n=300)
    if r:
        chk("两个噪声集差异不显著 (p>0.05)", r["p_two_sided"] > 0.05,
            f"p={r['p_two_sided']}, CI={r['ci95']}")

    # T3 方向反转
    print("\n[T3] 方向反转必须被识别")
    inv = mk(300, 0.95, invert=True)
    auci = S.auc_rows(inv)
    chk("反转样本 AUC < 0.30", auci < 0.30, f"AUC={auci:.4f}")

    # T4 CI 宽度合理
    print("\n[T4] bootstrap 置信区间宽度必须合理")
    if r:
        width = r["ci95"][1] - r["ci95"][0]
        chk("CI 宽度 > 0（非退化）", width > 0.001, f"width={width:.4f}")
        chk("CI 宽度 < 0.6（非无意义）", width < 0.6, f"width={width:.4f}")
        chk("bootstrap 次数足够", r["n_boot"] >= 250, f"n={r['n_boot']}")

    # T5 命中率
    print("\n[T5] 方向命中率计算")
    all_right = [{"fwd": 0.1, "dom": "C"}, {"fwd": -0.1, "dom": "E"}]
    hr, n = S.hit_rows(all_right)
    chk("全对命中率 = 1.0", abs(hr - 1.0) < 1e-9, f"{hr} (n={n})")
    all_wrong = [{"fwd": -0.1, "dom": "C"}, {"fwd": 0.1, "dom": "E"}]
    hw, n2 = S.hit_rows(all_wrong)
    chk("全错命中率 = 0.0", abs(hw - 0.0) < 1e-9, f"{hw} (n={n2})")
    neutral = [{"fwd": 0.1, "dom": "D"}]
    hn, n3 = S.hit_rows(neutral)
    chk("无方向样本返回 None", hn is None and n3 == 0, f"{hn} (n={n3})")

    print("\n" + "=" * 72)
    npass = sum(OK)
    print(f"通过 {npass}/{len(OK)}")
    print("总判定： " + ("通过（优化流程具备检出能力）" if npass == len(OK) else "失败"))
    print("=" * 72)
    return 0 if npass == len(OK) else 1


if __name__ == "__main__":
    sys.exit(main())
