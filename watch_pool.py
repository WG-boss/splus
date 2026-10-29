# -*- coding: utf-8 -*-
"""生成 watch 桶「A 吸筹达标池」可跟踪观察名单。

口径（全部来自 splus_decision_<date>.json，不另算）：
  - bucket == watch 且 tradeable == false
  - 主状态 A（吸筹/筑底）
  - 置信度 >= state.min_confidence_to_act（默认 0.65）

输出：
  data/watch_a_pool_<date>.md    人读
  data/watch_a_pool_latest.csv   机器读（可追加跟踪）

为什么不直接给交易信号：
  watch 桶 tradeable=false，不占仓、不生成执行计划；且 BROAD_E 开关下禁止左侧加仓。
  这张表是"候选池"，不是"买入清单"——从候选到可交易，必须过三关：
    (1) BROAD_E 开关解除（可交易标的中 E 阶段不再占多数）
    (2) 该标的反弹四评分 >= 3 项
    (3) 站上 EMA20 且 EMA20 > EMA60
"""
from __future__ import annotations

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")


def find_decision() -> tuple:
    d = os.path.join(HERE, "data")
    fs = sorted([f for f in os.listdir(d) if f.startswith("splus_decision_") and f.endswith(".json")])
    if not fs:
        raise SystemExit("找不到 splus_decision_*.json，先跑 run.py")
    p = os.path.join(d, fs[-1])
    return p, fs[-1].replace("splus_decision_", "").replace(".json", "")


def main() -> int:
    path, asof_file = find_decision()
    J = json.load(open(path, encoding="utf-8"))
    asof = J.get("as_of") or asof_file
    thr = float(J.get("min_confidence_to_act") or 0.65)
    per = J.get("per_index") or {}

    rows = []
    for code, v in per.items():
        if v.get("bucket") != "watch" or v.get("tradeable"):
            continue
        st = v.get("state") or {}
        if st.get("dominant") != "A":
            continue
        rows.append({
            "code": code,
            "name": v.get("name", code),
            "conf": float(st.get("confidence") or 0.0),
            "pass": float(st.get("confidence") or 0.0) >= thr,
            "close": v.get("close"),
            "vol": v.get("ann_vol_20"),
            "dd": v.get("dd_from_high"),
            "pos52": v.get("pos_in_52w"),
            "er20": v.get("er20"),
            "above_ma200": v.get("above_ma200"),
            "probs": st.get("probs") or {},
        })

    passed = sorted([r for r in rows if r["pass"]], key=lambda x: -x["conf"])
    below = sorted([r for r in rows if not r["pass"]], key=lambda x: -x["conf"])

    def fmt_pct(x, nd=1):
        return ("%.*f%%" % (nd, x * 100)) if isinstance(x, (int, float)) else "—"

    md = []
    md.append("# watch 桶「A 吸筹达标池」观察名单 · %s" % asof)
    md.append("")
    md.append("> 数据源：`splus_decision_%s.json`（L1 状态概率向量）。" % asof_file)
    md.append("> 筛选条件：bucket=watch 且 tradeable=false，主状态 = **A 吸筹/筑底**，置信度 ≥ **%.2f**。" % thr)
    md.append("> ⚠️ 本表是**候选池，不是买入清单**。watch 桶不占仓、不生成执行计划；BROAD_E 开关下禁止左侧加仓。")
    md.append("")
    md.append("## 一、达标池（%d 只）" % len(passed))
    md.append("")
    if passed:
        md.append("| # | 指数 | 代码 | A 概率 | 收盘 | 年化波动 | 距52周高 | 52周分位 | ER20 | 距MA200 |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(passed, 1):
            md.append("| %d | %s | %s | **%s** | %.1f | %s | %s | %s | %s | %s |" % (
                i, r["name"], r["code"], fmt_pct(r["conf"], 0),
                r["close"] or 0, fmt_pct(r["vol"], 0), fmt_pct(r["dd"], 1),
                fmt_pct(r["pos52"], 0), ("%.2f" % r["er20"]) if r["er20"] is not None else "—",
                fmt_pct(r["above_ma200"], 1)))
    else:
        md.append("_本日无达标标的。_")
    md.append("")

    if below:
        md.append("## 二、A 状态但置信度未达门槛（%d 只，不入选）" % len(below))
        md.append("")
        md.append("| 指数 | 代码 | A 概率 | 缺口 |")
        md.append("|---|---|---|---|")
        for r in below:
            md.append("| %s | %s | %s | 差 %.2f |" % (r["name"], r["code"], fmt_pct(r["conf"], 0), thr - r["conf"]))
        md.append("")

    # 主题聚合，帮用户看出资金在往哪条线走
    themes = [
        ("医药医疗", ["医疗", "医药", "疫苗", "生科", "港股通创新药"]),
        ("消费", ["消费", "食品", "家电"]),
        ("券商金融", ["证券", "银行", "金融科技"]),
        ("红利/低估值", ["红利", "价值", "国企", "责任", "现金流"]),
        ("电力公用", ["电力", "电网", "交运"]),
        ("资源周期", ["资源", "有色", "油气", "化工", "粮食", "黄金"]),
    ]
    md.append("## 三、达标池的主题分布")
    md.append("")
    md.append("| 主题线 | 命中标的 |")
    md.append("|---|---|")
    hit_any = False
    for tname, kws in themes:
        hits = [r["name"] for r in passed if any(k in r["name"] for k in kws)]
        if hits:
            hit_any = True
            md.append("| %s | %s |" % (tname, "、".join(hits)))
    if not hit_any:
        md.append("| — | 无 |")
    md.append("")

    md.append("## 四、从「候选」到「可交易」的三道关（必须全部通过）")
    md.append("")
    md.append("| 关卡 | 当前状态 | 说明 |")
    md.append("|---|---|---|")
    md.append("| ① BROAD_E 开关解除 | ❌ 未解除 | 可交易标的中 E 阶段仍占 10/11，禁止左侧加仓 |")
    md.append("| ② 反弹四评分 ≥ 3/4 | ❌ 全部 0–1 项 | 站上EMA20 / EMA20>EMA60 / 效率抬升 / 放量 |")
    md.append("| ③ 站上 EMA20 且 EMA20>EMA60 | ❌ 未达 | 需真实价格确认，不看估值分位 |")
    md.append("")
    md.append("**结论：本表标的全部处于「观察」而非「可动手」阶段。** 三道关任一未过，仓位上限都是 0。")
    md.append("")
    md.append("---")
    md.append("*由 watch_pool.py 按 splus_config.yaml 与最新决策 JSON 自动生成，不构成买卖建议。*")

    out_md = os.path.join(HERE, "data", "watch_a_pool_%s.md" % asof)
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))

    # CSV 长表（便于逐日追加做跟踪）
    out_csv = os.path.join(HERE, "data", "watch_a_pool_latest.csv")
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["as_of", "code", "name", "A_prob", "pass_thr", "close",
                    "ann_vol20", "dd_from_high", "pos_in_52w", "er20", "above_ma200"])
        for r in passed + below:
            w.writerow([asof, r["code"], r["name"], round(r["conf"], 4),
                        int(r["pass"]), r["close"],
                        None if r["vol"] is None else round(r["vol"], 4),
                        None if r["dd"] is None else round(r["dd"], 4),
                        None if r["pos52"] is None else round(r["pos52"], 4),
                        None if r["er20"] is None else round(r["er20"], 4),
                        None if r["above_ma200"] is None else round(r["above_ma200"], 4)])

    print("达标池 %d 只 / A状态合计 %d 只  (门槛 %.2f)" % (len(passed), len(rows), thr))
    for r in passed:
        print("  %-22s %-8s A=%.0f%%  dd=%.1f%%  vol=%.0f%%" % (
            r["name"], r["code"], r["conf"] * 100,
            (r["dd"] or 0) * 100, (r["vol"] or 0) * 100))
    print("MD : %s" % out_md)
    print("CSV: %s" % out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
