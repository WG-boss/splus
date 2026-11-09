# -*- coding: utf-8 -*-
"""
run_all_checks.py —— S+++ 验收总入口

一条命令跑完全部验收，任何一项失败即退出码非 0。
这是"能不能宣布达标"的唯一依据。

用法: python run_all_checks.py
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

CHECKS = [
    ("硬编码扫描（参数外置）", ["lint_config.py"]),
    ("状态判别 + 可证伪自测", ["selftest_states.py"]),
    ("治理层拦截能力", ["selftest_governance.py"]),
    ("结算路径回放", ["selftest_settlement.py"]),
    ("历史回溯 + 参数敏感性", ["selftest_history.py"]),
    ("参数优化流程自检", ["selftest_optimize.py"]),
    ("HTML 渲染器自检", ["selftest_html.py"]),
    ("P0 账户/闸门/治理/ETF", ["selftest_p0.py"]),
    ("P1/P2 换算/进度/归因/分级", ["selftest_p12.py"]),
    ("L1 主状态连续段回算", ["selftest_state_streak.py"]),
    ("市场闸门 O2 即时态选项", ["selftest_market_context.py"]),
    ("B2 资格分引擎", ["selftest_eligibility.py"]),
    ("B2 生命周期+状态调制", ["selftest_lifecycle.py"]),
    ("动态可交易池 R6（运行时）", ["selftest_pool_runtime.py"]),
    ("口径披露 + 经验转移", ["selftest_disclosure.py"]),
    ("ETF 映射覆盖校验", ["tools_etf_map.py", "--check"]),
    ("B2-P4 映射 SLA（可执行标的 100% confirmed）", ["tools_map_sla.py"]),
    ("ETF 映射复核（含观察池）", ["tools_etf_review.py", "--check"]),
    ("生产运行", ["run.py"]),
]


def main():
    print("=" * 74)
    print("S+++ 验收总入口")
    print("=" * 74)
    results = []
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    for name, cmd in CHECKS:
        print(f"\n▶ {name}  ({' '.join(cmd)})")
        print("-" * 74)
        r = subprocess.run([sys.executable] + cmd, cwd=BASE, env=env,
                           capture_output=True)
        out = r.stdout.decode("utf-8", errors="replace")
        err = r.stderr.decode("utf-8", errors="replace")
        # 只显示尾部，避免刷屏
        lines = [l for l in out.strip().split("\n") if l.strip()]
        for l in lines[-8:]:
            print("  " + l)
        if err.strip():
            for l in err.strip().split("\n")[-4:]:
                print("  [stderr] " + l)
        ok = r.returncode == 0
        results.append((name, ok, r.returncode))
        print(f"  → {'PASS' if ok else 'FAIL'} (exit {r.returncode})")

    print("\n" + "=" * 74)
    print("验收汇总")
    print("=" * 74)
    n_pass = sum(1 for _, ok, _ in results if ok)
    for name, ok, rc in results:
        print(f"  {'✅' if ok else '❌'}  {name:<28} exit={rc}")
    print(f"\n通过 {n_pass}/{len(results)}")

    all_ok = n_pass == len(results)
    print("\n" + ("总判定： 全部通过，具备宣布 S++ 工程达标的依据"
                  if all_ok else "总判定： 存在失败项，不得宣布达标"))
    print("（注：S+++ 的组织与认知层还需真实交易样本校准，非一次验收可判定）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
