# -*- coding: utf-8 -*-
"""
selftest_html.py —— HTML 渲染器自检（含残缺输入必须被拒绝）

为什么必须测"拒绝"：
  渲染器最大的风险不是崩，而是"静默产出一份看起来正常的空报告"。
  这类缺陷不会报错、不会告警，只在人真的去读时才暴露。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import render_html

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "splus_config.yaml")
OK = []


def chk(name, cond, detail=""):
    OK.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'}  {name}  {detail}")


def main():
    print("=" * 72)
    print("HTML 渲染器自检")
    print("=" * 72)
    res = engine.run(CFG, BASE)

    # T1 正常渲染
    print("\n[T1] 正常输入")
    h = render_html.render_html(res)
    chk("输出以 <!DOCTYPE 开头", h.startswith("<!DOCTYPE"))
    chk("输出以 </html> 结尾", h.rstrip().endswith("</html>"))
    chk("包含全部 7 张表", h.count("<table>") == 7, f"实际 {h.count('<table>')}")
    chk("概率条形图数量 = 标的数",
        h.count('class="bar"') == len(res["per_index"]),
        f"{h.count('class=\"bar\"')} vs {len(res['per_index'])}")
    chk("无未替换的 None", "None" not in h)
    chk("无 Python 对象地址泄漏", "object at 0x" not in h)
    chk("标签闭合平衡",
        h.count("<tr>") == h.count("</tr>") and h.count("<td") == h.count("</td>"))

    # T2 残缺输入必须被拒绝（每一项单独测）
    print("\n[T2] 残缺输入必须被拒绝（防止静默产出空报告）")
    cases = [
        ("as_of 为空", {**res, "as_of": ""}),
        ("per_index 为空", {**res, "per_index": {}}),
        ("per_index 非 dict", {**res, "per_index": []}),
        ("portfolio_dd 缺失", {**res, "portfolio_dd": None}),
    ]
    for name, bad in cases:
        try:
            render_html.render_html(bad)
            chk(f"{name} 被拒绝", False, "竟然通过了")
        except Exception as e:
            chk(f"{name} 被拒绝", True, type(e).__name__)

    # 单标的缺字段
    k = list(res["per_index"].keys())[0]
    bad2 = {**res, "per_index": {k: {kk: vv for kk, vv in res["per_index"][k].items()
                                    if kk != "state"}}}
    try:
        render_html.render_html(bad2)
        chk("标的缺 state 被拒绝", False, "竟然通过了")
    except Exception as e:
        chk("标的缺 state 被拒绝", True, type(e).__name__)

    # T3 XSS / 转义
    print("\n[T3] HTML 转义")
    evil = {**res}
    k0 = list(res["per_index"].keys())[0]
    evil_pi = {kk: dict(vv) for kk, vv in res["per_index"].items()}
    evil_pi[k0]["name"] = '<script>alert(1)</script>'
    evil["per_index"] = evil_pi
    he = render_html.render_html(evil)
    chk("script 标签被转义", "<script>alert(1)</script>" not in he)
    chk("转义实体存在", "&lt;script&gt;" in he)

    # T4 写盘
    print("\n[T4] 写盘")
    p = render_html.write_html(res, os.path.join(BASE, "data"), res["as_of"])
    chk("文件存在且非空", os.path.exists(p) and os.path.getsize(p) > 5000,
        f"{os.path.getsize(p)} bytes")
    chk("文件名含日期", res["as_of"].replace("-", "") in os.path.basename(p))
    raw = io.open(p, encoding="utf-8").read()
    chk("写盘内容与渲染一致", raw == h)

    print("\n" + "=" * 72)
    n = sum(OK)
    print(f"通过 {n}/{len(OK)}")
    print("总判定： " + ("通过" if n == len(OK) else "失败"))
    print("=" * 72)
    return 0 if n == len(OK) else 1


if __name__ == "__main__":
    sys.exit(main())
