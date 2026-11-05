# -*- coding: utf-8 -*-
"""
tools_etf_review.py —— 全量指数→ETF 映射的【观察池复核】工具（P1-②）

P0 已做到 118 只全覆盖（11 可交易 + 107 观察池），但自动名称匹配只产出
候选。本工具回答「哪些候选最可能是错的」，把复核结论写回 etf_map.yaml 的
review 字段并生成可审计报告，供人工逐只 --confirm。

  python tools_etf_review.py --report             复核并生成 data/etf_review.md
  python tools_etf_review.py --check              校验 118 只都有复核结论
  python tools_etf_review.py --template data/etf_review.tsv   生成人工复核表
  python tools_etf_review.py --stats              只看统计

严格约束：本工具【绝不】把任何候选改成 confirmed，也不编造 ETF 代码。
复核只降低置信展示（review.risk），确认仍必须人工 --confirm。
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import tools_etf_map as tm  # noqa: E402
import etf_quotes as qmod  # noqa: E402

REPORT = os.path.join(BASE, "data", "etf_review.md")
QUOTES = os.path.join(BASE, "data", "etf_quotes.json")

CROSS_INDEX = ("恒生", "港股", "香港", "纳斯达克", "纳指", "标普", "日经",
               "中概", "H股", "海外", "德国", "法国", "东南亚")
CROSS_FUND = ("恒生", "港股", "港", "纳指", "纳斯达克", "QDII", "标普",
              "日经", "中概", "H股", "海外", "德国", "法国", "东南亚")
MODIFIERS = ("增强", "成长", "价值", "低波", "策略", "精选", "等权",
             "龙头", "主题", "优选", "动量", "质量")


def _cfg() -> Dict[str, Any]:
    import engine
    c = engine.load_config(os.path.join(BASE, "splus_config.yaml"))
    return c.get("etf_review") or {}


def _quotes() -> Dict[str, Any]:
    if not os.path.exists(QUOTES):
        return {}
    try:
        return (json.load(io.open(QUOTES, encoding="utf-8")) or {}).get("quotes") or {}
    except Exception:
        return {}


def analyze() -> Dict[str, Any]:
    cfg = _cfg()
    low = float(cfg.get("suspect_score_below") or 0.50)
    mid = float(cfg.get("low_score_below") or 0.65)
    thin = float(cfg.get("thin_liquidity_cny") or 10000000)
    doc = tm.load_map()
    ins = doc.get("instruments") or {}
    quotes = _quotes()

    # 首选代码冲突：同一 ETF 被多只指数列为第一候选 → 至少一个匹配是错的
    first = defaultdict(list)
    for code, v in ins.items():
        cs = v.get("candidates") or []
        if cs and isinstance(cs[0], dict):
            first[str(cs[0].get("code"))].append(code)
    conflicts = {c: ks for c, ks in first.items() if len(ks) > 1}

    for code, v in ins.items():
        cs = v.get("candidates") or []
        top = cs[0] if cs and isinstance(cs[0], dict) else {}
        score = float(top.get("score") or 0)
        iname, fname = str(v.get("name") or ""), str(top.get("name") or "")
        flags: List[str] = []
        if not cs:
            flags.append("no_candidate")
        if cs and any(w in iname for w in CROSS_INDEX) \
                and not any(w in fname for w in CROSS_FUND):
            flags.append("cross_market_suspect")
        if top and str(top.get("code")) in conflicts:
            flags.append("top_candidate_conflict")
        if cs and score < low:
            flags.append("score_very_low")
        elif cs and score < mid:
            flags.append("score_low")
        if cs and any(w in fname and w not in iname for w in MODIFIERS):
            flags.append("enhanced_or_style")
        amt = (quotes.get(str(top.get("code"))) or {}).get("amount") if top else None
        if amt is not None and float(amt) < thin:
            flags.append("thin_liquidity")
        hard = {"no_candidate", "cross_market_suspect",
                "top_candidate_conflict", "score_very_low"}
        risk = "high" if (set(flags) & hard) else ("medium" if flags else "low")
        v["review"] = {
            "risk": risk,
            "flags": flags,
            "top_score": round(score, 4) if cs else None,
            "top_code": str(top.get("code") or "") if top else None,
            "top_name": fname,
            "top_amount_cny": round(float(amt), 2) if amt is not None else None,
            "reviewed_at": datetime.date.today().isoformat(),
            "note": ("自动候选疑似错配，必须人工核实后方可 --confirm"
                     if risk == "high" else
                     "自动候选，需人工核对跟踪指数与流动性" if risk == "medium" else
                     "名称高度吻合，仍需人工核对跟踪指数"),
        }
        # review 结论顺带补进 note 展示，但绝不改 status / etf_code
    doc.setdefault("meta", {})["review_version"] = "1.0.0"
    doc["meta"]["last_reviewed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tm.save_map(doc)

    risks = Counter(v["review"]["risk"] for v in ins.values())
    trade = {k: v for k, v in ins.items() if v.get("tradeable")}
    watch = {k: v for k, v in ins.items() if not v.get("tradeable")}
    return {
        "total": len(ins), "tradeable": len(trade), "watch": len(watch),
        "risk": dict(risks),
        "confirmed": sum(1 for v in ins.values() if v.get("status") == "confirmed"),
        "conflicts": conflicts,
        "high": {k: v for k, v in ins.items() if v["review"]["risk"] == "high"},
        "no_candidate": [k for k, v in ins.items() if not (v.get("candidates") or [])],
        "quotes_available": bool(quotes),
        "thresholds": {"suspect_score_below": low, "low_score_below": mid,
                       "thin_liquidity_cny": thin},
        "ins": ins,
    }


def render_md(a: Dict[str, Any]) -> str:
    L = []
    L.append(f"# 指数→场内 ETF 映射复核报告 · {datetime.date.today().isoformat()}")
    L.append("")
    L.append("> 自动名称匹配只产出【候选】。本报告用于人工复核，"
             "**不构成任何可下单代码**；确认请用 `python tools_etf_map.py --confirm <指数码> <ETF码>`。")
    L.append("")
    L.append("## 一、覆盖与风险分布")
    L.append("")
    L.append(f"- 全量 {a['total']} 只（可交易 {a['tradeable']} ／观察池 {a['watch']}）")
    L.append(f"- 风险：high {a['risk'].get('high', 0)} ／ medium {a['risk'].get('medium', 0)} "
             f"／ low {a['risk'].get('low', 0)}")
    L.append(f"- 已人工确认 {a['confirmed']} 只（确认前报告一律不显示可下单代码）")
    L.append(f"- 行情成交额数据：{'已接入' if a['quotes_available'] else '未接入（流动性列为空）'}")
    L.append("")
    L.append("## 二、必须优先复核（high）")
    L.append("")
    L.append("| 指数码 | 名称 | 层 | 首选候选 | 分 | 风险标记 |")
    L.append("|---|---|---|---|---|---|")
    for k, v in sorted(a["high"].items(),
                       key=lambda kv: (kv[1]["review"]["top_score"] or 0)):
        r = v["review"]
        L.append(f"| {k} | {v.get('name')} | {'可交易' if v.get('tradeable') else '观察池'} | "
                 f"{r['top_code'] or '—'} {r['top_name'] or '—'} | "
                 f"{r['top_score'] if r['top_score'] is not None else '—'} | "
                 f"{', '.join(r['flags']) or '—'} |")
    L.append("")
    L.append("## 三、首选候选冲突（同一 ETF 被多只指数列为首选）")
    L.append("")
    if a["conflicts"]:
        L.append("| ETF 代码 | 冲突的指数 |")
        L.append("|---|---|")
        for c, ks in sorted(a["conflicts"].items()):
            L.append(f"| {c} | {', '.join(ks)} |")
    else:
        L.append("无。")
    L.append("")
    L.append("## 四、无场内候选")
    L.append("")
    L.append("、".join(a["no_candidate"]) or "无。")
    L.append("")
    L.append("> 这几只通常是新发主题指数，场内尚无同名 ETF；"
             "需人工指定场外联接基金或标注「暂不可交易」，不得强行匹配相近名称。")
    L.append("")
    L.append("## 五、复核阈值（外置于 splus_config.yaml.etf_review）")
    L.append("")
    for k, v in a["thresholds"].items():
        L.append(f"- `{k}` = {v}")
    L.append("")
    L.append("---")
    L.append("*复核结论写回 etf_map.yaml 的 review 字段（不改 status / etf_code）。"
             "report 为生成产物，不入库。*")
    return "\n".join(L)


def write_template(a: Dict[str, Any], path: str) -> int:
    cols = ["index_code", "index_name", "layer", "risk", "flags",
            "top_code", "top_name", "top_score", "amount_cny",
            "confirmed_etf_code", "reviewer_note"]
    lines = ["\t".join(cols)]
    for k, v in sorted(a["ins"].items(),
                       key=lambda kv: (0 if kv[1]["review"]["risk"] == "high" else 1, kv[0])):
        r = v["review"]
        lines.append("\t".join([
            k, str(v.get("name") or ""),
            "tradeable" if v.get("tradeable") else "watch",
            r["risk"], ";".join(r["flags"]),
            r["top_code"] or "", r["top_name"] or "",
            "" if r["top_score"] is None else str(r["top_score"]),
            "" if r["top_amount_cny"] is None else str(r["top_amount_cny"]),
            "", "",
        ]))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    io.open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"OK 人工复核表 → {path}（{len(lines) - 1} 行；仅填 confirmed_etf_code 后逐只 --confirm）")
    return 0


def check() -> int:
    doc = tm.load_map()
    ins = doc.get("instruments") or {}
    import engine
    codes = {u["code"] for u in engine.load_config(
        os.path.join(BASE, "splus_config.yaml"))["universe"]}
    missing = sorted(codes - set(ins))
    no_review = [k for k, v in ins.items() if not v.get("review")]
    bad = [k for k, v in ins.items()
           if v.get("status") == "confirmed" and not re.fullmatch(
               r"\d{6}", str(v.get("etf_code") or ""))]
    print("ETF 映射复核校验")
    print(f"  universe={len(codes)} mapped={len(ins)} missing={len(missing)} "
          f"no_review={len(no_review)} illegal_confirmed={len(bad)}")
    print("  risk=" + str(dict(Counter(v.get("review", {}).get("risk") for v in ins.values()))))
    ok = not missing and not no_review and not bad
    print("  判定：" + ("通过" if ok else "失败（先跑 --report）"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="全量指数→ETF 映射复核（含观察池）")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--template", help="生成人工复核 TSV")
    args = ap.parse_args()
    if args.check:
        return check()
    a = analyze()
    if args.template:
        return write_template(a, args.template)
    if args.report or not args.stats:
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        io.open(REPORT, "w", encoding="utf-8").write(render_md(a))
        print(f"OK 复核报告 → {REPORT}")
    print(json.dumps({"total": a["total"], "tradeable": a["tradeable"],
                      "watch": a["watch"], "risk": a["risk"],
                      "confirmed": a["confirmed"],
                      "conflict_groups": len(a["conflicts"]),
                      "no_candidate": a["no_candidate"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
