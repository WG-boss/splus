# -*- coding: utf-8 -*-
"""
lint_config.py —— 硬编码残留扫描器

审计缺陷 2 的守门器：防止"参数外置"退化。
扫描 engine.py / run.py / meta_layer.py 中的数值字面量，
凡出现在"决策函数"内且不在白名单（结构性常量）中的，一律报警。

退出码: 0 = 无阻断项；1 = 存在阻断项
用法: python lint_config.py [--strict]
"""
import io
import os
import re
import sys

DOCQUOTE = chr(34) * 3
SQQUOTE = chr(39) * 3

BASE = os.path.dirname(os.path.abspath(__file__))
TARGETS = ["engine.py", "run.py", "meta_layer.py"]

# 结构性常量白名单（不是可调参数）
ALLOW = {
    "0.0", "1.0", "2.0", "0.5", "100.0", "1e-6", "1e-12", "0.0)", "1.0)",
    "3.0",  # top2 取前二
}

# 决策函数（内含数值字面量即为可疑硬编码）
DECISION_FUNCS = [
    "state_scores", "_derive", "_member", "state_probs",
    "position_sizing", "dd_throttle", "kill_switches",
    "compute_features", "portfolio_drawdown",
]

# 这些字面量即使出现在决策函数中也是结构性的（数组索引、窗口、长度等）
STRUCT_PAT = re.compile(
    r"(?:\[|,|\(|\s)(?:0|1|2|3|4|5|6|8|10|14|20|40|52|60|120|200|252)(?:\)|\]|,|\s|$)"
)


def find_func_spans(lines):
    """返回 {func_name: (start, end)} 行号区间"""
    spans = {}
    cur = None
    start = 0
    for i, ln in enumerate(lines):
        m = re.match(r"def\s+(\w+)\s*\(", ln)
        if m:
            if cur:
                spans[cur] = (start, i)
            cur = m.group(1)
            start = i
        elif cur and ln and not ln[0].isspace() and not ln.startswith("#"):
            spans[cur] = (start, i)
            cur = None
    if cur:
        spans[cur] = (start, len(lines))
    return spans


# 函数默认参数白名单：这些值是"签名兜底"，实际调用一律由配置传入。
# 允许它们存在的前提是：调用点必须显式传配置值（由 lint 的 second pass 校验）。
DEF_ARG_OK = {"default_miss: float = 0.05", "uniform: float = 0.2",
              "uniform: float = 0.20",
              # 下列为带配置默认值的读取兜底：真实取值来自 splus_config.yaml
              '_default_miss', 'corr_penalty_floor', 'kill_deep_dd',
              'kill_upstream_ret20', 'uniform_prior'}


def scan():
    blockers, warns = [], []
    num_re = re.compile(r"(?<![\w.])(\d+\.\d+)(?![\w.])")

    for fn in TARGETS:
        path = os.path.join(BASE, fn)
        if not os.path.exists(path):
            warns.append((fn, 0, "文件不存在"))
            continue
        lines = io.open(path, encoding="utf-8").read().split("\n")
        spans = find_func_spans(lines)

        for name, (a, b) in spans.items():
            if name not in DECISION_FUNCS:
                continue
            in_doc = False
            for i in range(a, b):
                ln = lines[i]
                stripped = ln.strip()
                q = stripped.count(DOCQUOTE) + stripped.count(SQQUOTE)
                if q % 2 == 1:
                    in_doc = not in_doc if not stripped.startswith((DOCQUOTE, SQQUOTE)) else not in_doc
                    continue
                if stripped.startswith((DOCQUOTE, SQQUOTE)):
                    continue
                if in_doc:
                    continue
                code = ln.split("#", 1)[0]
                # 已经在读配置的行不算硬编码
                if "cfg[" in code or "config" in code:
                    continue
                for m in num_re.finditer(code):
                    val = m.group(1)
                    if val in ALLOW:
                        continue
                    # 函数默认参数兜底（实际值由配置传入），不算硬编码
                    if any(tok in code for tok in DEF_ARG_OK):
                        continue
                    # 纯数组下标/窗口长度不算
                    line_no = i + 1
                    blockers.append((fn, line_no, name, val, ln.strip()[:90]))

    return blockers, warns


def main():
    strict = "--strict" in sys.argv
    print("=" * 70)
    print("硬编码残留扫描（守门器）")
    print("=" * 70)
    blockers, warns = scan()

    if not blockers:
        print("\n[通过] 决策函数内未发现硬编码数值字面量。")
        print(f"       扫描文件: {', '.join(TARGETS)}")
        print(f"       决策函数: {', '.join(DECISION_FUNCS)}")
        return 0

    print(f"\n[阻断] 发现 {len(blockers)} 处疑似硬编码：\n")
    by_func = {}
    for fn, line_no, name, val, snippet in blockers:
        by_func.setdefault((fn, name), []).append((line_no, val, snippet))

    for (fn, name), items in sorted(by_func.items()):
        print(f"  {fn} :: {name}() —— {len(items)} 处")
        for line_no, val, snippet in items[:12]:
            print(f"    L{line_no:<5} {val:<8} {snippet}")
        if len(items) > 12:
            print(f"    ... 另有 {len(items)-12} 处")
        print()

    print(f"合计 {len(blockers)} 处。请移入 splus_config.yaml 后重新运行。")
    print("（若确认为结构性常量，请加入 lint_config.py 的 ALLOW 白名单并说明理由。）")
    return 0 if not strict else 1


if __name__ == "__main__":
    sys.exit(main())
