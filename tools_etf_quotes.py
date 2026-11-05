# -*- coding: utf-8 -*-
"""
tools_etf_quotes.py —— 刷新场内 ETF 现价/成交额快照（P1-①）

  python tools_etf_quotes.py --refresh   分页拉全量（push2 每页硬上限 100 条）
  python tools_etf_quotes.py --check     校验缓存完整性与价格量级
  python tools_etf_quotes.py --show 588000

主源：东方财富 push2 clist（f2 最新价、f6 成交额、f12 代码、f14 名称）
校验源：腾讯 gtimg（抽样 3 只交叉核对，偏差过大只告警不阻断）
产物：data/etf_quotes.json（gitignore，含个人执行参考但不含资产数据）
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import ssl
import sys
import urllib.request
from typing import Any, Dict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import etf_quotes  # noqa: E402

OUT = os.path.join(BASE, "data", "etf_quotes.json")
QUOTE_URL = ("https://push2.eastmoney.com/api/qt/clist/get"
             "?pn={pn}&pz=100&po=1&fid=f3&fs=b:MK0021&fields=f2,f6,f12,f14")
GTIMG = "https://qt.gtimg.cn/q={secids}"
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _market(code: str) -> str:
    return "sh" if code[:1] in "56" else "sz"


def fetch() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    total, pn, max_pages = None, 1, 30
    while pn <= max_pages:
        req = urllib.request.Request(QUOTE_URL.format(pn=pn), headers=HDR)
        with urllib.request.urlopen(req, timeout=20, context=_ctx()) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        block = data.get("data") or {}
        if total is None:
            total = int(block.get("total") or 0)
            max_pages = min(30, (total + 99) // 100)
        diff = block.get("diff") or {}
        items = diff.values() if isinstance(diff, dict) else diff
        n = 0
        for it in items:
            code = str(it.get("f12") or "")
            name = str(it.get("f14") or "")
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            p, amt = it.get("f2"), it.get("f6")
            try:
                price = float(p) / 1000.0 if p not in (None, "", "-") else None
            except Exception:
                price = None
            try:
                amount = float(amt) if amt not in (None, "", "-") else None
            except Exception:
                amount = None
            if price is not None and not (0 < price < 100):
                price = None
            out[code] = {"name": name, "market": _market(code),
                         "price": price, "amount": amount}
            n += 1
        if n == 0:
            break
        pn += 1
    return out


def cross_check(quotes: Dict[str, Dict[str, Any]], codes) -> str:
    """腾讯 gtimg 抽样校验：偏差 >3% 只告警，不改写主源。"""
    secids = ",".join((quotes[c].get("market") or _market(c)) + c
                      for c in codes if c in quotes)
    if not secids:
        return "无样本"
    try:
        req = urllib.request.Request(GTIMG.format(secids=secids),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ctx()) as r:
            txt = r.read().decode("gb18030", errors="replace")
    except Exception as exc:
        return f"校验源不可用（{type(exc).__name__}），未阻断"
    msgs = []
    for line in txt.splitlines():
        m = re.match(r'v_\w+="(.*?)";?', line.strip())
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 5:
            continue
        code, price = f[2], f[3]
        try:
            pv = float(price)
        except Exception:
            continue
        mine = (quotes.get(code) or {}).get("price")
        if not mine:
            continue
        dev = abs(pv - mine) / mine
        msgs.append(f"{code}: 东财 {mine:.3f} / 腾讯 {pv:.3f}，偏差 {dev:.2%}"
                    + ("  ⚠️ 偏差过大" if dev > 0.03 else "  ✓"))
    return "；".join(msgs) if msgs else "校验源未返回样本"


def refresh() -> int:
    try:
        q = fetch()
    except Exception as exc:
        print(f"[ERR] 拉取行情失败：{type(exc).__name__}: {exc}")
        return 1
    if len(q) < 800:
        print(f"[ERR] 行情异常（{len(q)} 只，疑似分页不全），拒绝覆盖已有快照")
        return 1
    priced = sum(1 for v in q.values() if v.get("price"))
    if priced < 500:
        print(f"[ERR] 有效报价仅 {priced} 只，拒绝覆盖已有快照")
        return 1
    doc = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "东方财富 push2 clist（f2/f6/f12/f14）；腾讯 gtimg 抽样校验",
        "count": len(q), "priced": priced,
        "cross_check": cross_check(q, ["588000", "159949", "510300"]),
        "quotes": q,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    print(f"OK 行情 {len(q)} 只（有效报价 {priced} 只）→ {OUT}")
    print("   交叉校验：" + doc["cross_check"])
    return 0


def check() -> int:
    snap = etf_quotes.load(BASE)
    print("ETF 行情快照校验")
    print(f"  ok={snap.get('ok')} count={snap.get('count', 0)} "
          f"fetched_at={snap.get('fetched_at')} age_hours={snap.get('age_hours')}")
    print(f"  stale={snap.get('stale')} reason={snap.get('reason') or '—'}")
    if not snap.get("ok"):
        print("  判定：失败（先跑 --refresh）")
        return 1
    q = snap["quotes"]
    bad = [c for c, v in q.items()
           if v.get("price") is not None and not (0.05 < v["price"] < 50)]
    print(f"  illegal_price={len(bad)}")
    ok = not bad and len(q) >= 800
    print("  判定：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新/校验场内 ETF 行情快照")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--show", help="查看某只 ETF 快照")
    args = ap.parse_args()
    if args.refresh:
        return refresh()
    if args.check:
        return check()
    snap = etf_quotes.load(BASE)
    if args.show:
        print(json.dumps(etf_quotes.for_code(snap, args.show),
                         ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({k: v for k, v in snap.items() if k != "quotes"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
