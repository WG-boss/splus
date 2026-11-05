# -*- coding: utf-8 -*-
"""
tools_etf_map.py —— 全量指数 → 场内 ETF 映射维护 CLI（P0-④）

用户确认（2026-11-05）：映射「连观察池一起做」，覆盖 universe 全部 118 只。

命令：
  python tools_etf_map.py --refresh     拉取场内 ETF 清单 + 生成候选（保留已确认项）
  python tools_etf_map.py --check       校验覆盖率与代码合法性
  python tools_etf_map.py --stats       查看映射统计
  python tools_etf_map.py --confirm 000688 588000
  python tools_etf_map.py --show 000688

数据源：东方财富 push2 场内基金板块（b:MK0021），仅取代码与名称。
按名称模糊匹配产出【候选】，候选不等于已核实的跟踪标的；
只有 --confirm 之后 status 才变 confirmed，报告才会显示可下单代码。
清单缓存到 data/etf_universe.json，离线 --check / --confirm 不需要联网。
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import io
import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.request
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

BASE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(BASE, "etf_map.yaml")
UNIVERSE = os.path.join(BASE, "data", "etf_universe.json")
CFG = os.path.join(BASE, "splus_config.yaml")
ETF_URL = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn={pn}&pz=100&po=1&fid=f3&fs=b:MK0021&fields=f12,f14")

# 注意：绝不能把单字母 A/C 全局删除 —— "中证A500" 会被削成 "中证500"，
# 从而误匹配到中证500ETF。只允许剥掉名称尾部的份额类别字母。
NOISE = ("ETF", "LOF", "QDII", "基金", "指数", "联接", "人民币", "美元",
         "发起式", "增强", "策略")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).upper()
    for w in NOISE:
        s = s.replace(w.upper(), "")
    s = re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", s)
    s = re.sub(r"[ACE]$", "", s)
    return s


# 指数名本身不含、但基金名里多出来的修饰词 → 说明是增强/风格类，
# 与"跟踪该指数"不是一回事，排序时必须降到纯被动 ETF 之后。
MODIFIERS = ("增强", "成长", "价值", "低波", "策略", "精选", "等权",
             "龙头", "主题", "优选", "动量", "质量")


def similarity(idx: str, fund: str) -> float:
    a, b = norm(idx), norm(fund)
    if not a or not b:
        return 0.0
    base = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b:
        base += 0.35
    elif b in a:
        base += 0.15
    for w in MODIFIERS:
        if w in fund and w not in idx:
            base -= 0.22
            break
    # 短指数名极易误命中，给个下限惩罚
    if len(a) <= 3:
        base -= 0.10
    return round(min(1.0, base), 4)


def fetch_universe() -> List[Dict[str, str]]:
    """push2 每页硬上限 100 条，必须按 total 分页拉全，否则候选严重不全。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    out: Dict[str, Dict[str, str]] = {}
    total, pn, max_pages = None, 1, 30
    while pn <= max_pages:
        req = urllib.request.Request(
            ETF_URL.format(pn=pn),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        block = data.get("data") or {}
        if total is None:
            total = int(block.get("total") or 0)
            max_pages = min(30, (total + 99) // 100)
        diff = block.get("diff") or {}
        items = diff.values() if isinstance(diff, dict) else diff
        n = 0
        for it in items:
            code, name = str(it.get("f12") or ""), str(it.get("f14") or "")
            if re.fullmatch(r"\d{6}", code) and name:
                out[code] = {"code": code, "name": name,
                             "market": "sh" if code[0] in "56" else "sz"}
                n += 1
        if n == 0:
            break
        pn += 1
    return list(out.values())


def load_universe() -> List[Dict[str, str]]:
    if not os.path.exists(UNIVERSE):
        return []
    try:
        return (json.load(io.open(UNIVERSE, encoding="utf-8")) or {}).get("items") or []
    except Exception:
        return []


def load_map() -> Dict[str, Any]:
    import yaml
    if not os.path.exists(MAP):
        return {"meta": {}, "instruments": {}}
    return yaml.safe_load(io.open(MAP, encoding="utf-8").read()) or {"meta": {}, "instruments": {}}


def save_map(doc: Dict[str, Any]) -> None:
    import yaml
    with io.open(MAP, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)


def refresh() -> int:
    try:
        items = fetch_universe()
    except Exception as exc:
        print(f"[ERR] 拉取场内 ETF 清单失败：{type(exc).__name__}: {exc}")
        return 1
    if len(items) < 800:
        print(f"[ERR] 清单异常（{len(items)} 只，疑似分页不全），拒绝覆盖已有映射")
        return 1
    os.makedirs(os.path.dirname(UNIVERSE), exist_ok=True)
    with io.open(UNIVERSE, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "source": ETF_URL, "count": len(items), "items": items},
                  fh, ensure_ascii=False, indent=1)

    cfg = engine.load_config(CFG)
    old = (load_map().get("instruments") or {})
    instruments: Dict[str, Any] = {}
    for u in cfg["universe"]:
        code, name = u["code"], u["name"]
        scored = sorted(((similarity(name, f["name"]), f) for f in items),
                        key=lambda t: t[0], reverse=True)
        cands = [{"code": f["code"], "name": f["name"], "score": s,
                  "market": f["market"]}
                 for s, f in scored if s >= 0.42][:8]
        prev = old.get(code) or {}
        confirmed = prev.get("status") == "confirmed" and prev.get("etf_code")
        instruments[code] = {
            "name": name,
            "bucket": u.get("bucket"),
            "tradeable": bool(u.get("tradeable")),
            "status": "confirmed" if confirmed else ("needs_review" if cands else "unmapped"),
            "etf_code": prev.get("etf_code") if confirmed else None,
            "etf_name": prev.get("etf_name") if confirmed else None,
            "verified": bool(confirmed),
            "market": prev.get("market") if confirmed else None,
            "qdii": bool(prev.get("qdii")),
            "candidates": cands,
            "note": ("人工确认，可用于下单参考" if confirmed else
                     "自动名称匹配候选，未核实跟踪指数，禁止据此下单" if cands else
                     "场内清单中未找到同名候选，需人工指定或走场外联接基金"),
        }
    doc = {
        "meta": {
            "version": "1.0.0",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": "东方财富 push2 场内基金板块（代码+名称）",
            "universe_count": len(instruments),
            "etf_universe_count": len(items),
            "confirmed_count": sum(1 for v in instruments.values()
                                   if v["status"] == "confirmed"),
            "policy": "候选一律 needs_review；仅 --confirm 后 status=confirmed 才展示可下单代码",
        },
        "instruments": instruments,
    }
    save_map(doc)
    print(f"OK 场内清单 {len(items)} 只 → 映射 {len(instruments)} 条；"
          f"有候选 {sum(1 for v in instruments.values() if v['candidates'])} 条；"
          f"已确认 {doc['meta']['confirmed_count']} 条 → {MAP}")
    return 0


def check() -> int:
    cfg = engine.load_config(CFG)
    doc = load_map()
    ins = doc.get("instruments") or {}
    codes = {u["code"] for u in cfg["universe"]}
    missing = sorted(codes - set(ins))
    extra = sorted(set(ins) - codes)
    bad, confirmed = [], 0
    uni = {f["code"]: f for f in load_universe()}
    for c, v in ins.items():
        if v.get("status") == "confirmed":
            confirmed += 1
            ec = str(v.get("etf_code") or "")
            if not re.fullmatch(r"\d{6}", ec) or (uni and ec not in uni):
                bad.append(f"{c}: {ec}")
    print("ETF 映射校验")
    print(f"  universe={len(codes)}  mapped={len(ins)}  missing={len(missing)}  extra={len(extra)}")
    print(f"  confirmed={confirmed}  illegal={len(bad)}")
    if missing:
        print("  缺失：", ", ".join(missing[:20]))
    if bad:
        print("  非法确认：", ", ".join(bad[:20]))
    ok = not missing and not extra and not bad
    print("  判定：", "通过" if ok else "失败")
    return 0 if ok else 1


def confirm(code: str, etf_code: str) -> int:
    doc = load_map()
    ins = doc.setdefault("instruments", {})
    if code not in ins:
        print(f"[ERR] 未知指数代码 {code}")
        return 1
    if not re.fullmatch(r"\d{6}", etf_code):
        print("[ERR] ETF 代码必须是 6 位数字")
        return 1
    uni = {f["code"]: f for f in load_universe()}
    f = uni.get(etf_code)
    if not f and uni:
        print(f"[WARN] {etf_code} 不在场内 ETF 清单缓存中，请先 --refresh")
    ins[code]["status"] = "confirmed"
    ins[code]["etf_code"] = etf_code
    ins[code]["etf_name"] = (f or {}).get("name") or ins[code].get("etf_name")
    ins[code]["market"] = (f or {}).get("market") or ("sh" if etf_code[0] in "56" else "sz")
    ins[code]["verified"] = True
    ins[code]["note"] = (f"人工确认于 {datetime.date.today().isoformat()}；"
                         "请自行核对基金跟踪指数与流动性")
    doc.setdefault("meta", {})["confirmed_count"] = sum(
        1 for v in ins.values() if v.get("status") == "confirmed")
    save_map(doc)
    print(f"OK {code} {ins[code]['name']} → {etf_code} {ins[code]['etf_name'] or ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="维护全量指数→场内ETF映射")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--show", help="显示某指数映射")
    ap.add_argument("--confirm", nargs=2, metavar=("INDEX", "ETF"))
    args = ap.parse_args()
    if args.refresh:
        return refresh()
    if args.check:
        return check()
    if args.confirm:
        return confirm(*args.confirm)
    if args.show:
        print(json.dumps((load_map().get("instruments") or {}).get(args.show),
                         ensure_ascii=False, indent=2))
        return 0
    doc = load_map()
    ins = doc.get("instruments") or {}
    print(json.dumps({"meta": doc.get("meta"), "count": len(ins),
                      "confirmed": sum(1 for v in ins.values() if v.get("status") == "confirmed"),
                      "with_candidates": sum(1 for v in ins.values() if v.get("candidates")),
                      "unmapped": sum(1 for v in ins.values() if not v.get("candidates"))},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
