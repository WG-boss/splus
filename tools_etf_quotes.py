# -*- coding: utf-8 -*-
"""
tools_etf_quotes.py —— 刷新场内 ETF 现价/成交额快照（P1-①）

  python tools_etf_quotes.py --refresh   分页拉全量（push2 每页硬上限 100 条）
  python tools_etf_quotes.py --check     校验缓存完整性与价格量级
  python tools_etf_quotes.py --show 588000

主源：东方财富 push2 clist（f2 最新价、f6 成交额、f12 代码、f14 名称）
校验源/热备：腾讯 gtimg（主源可用时抽样 3 只交叉核对；主源不可用时
             按现有代码清单批量拉取作为热备，见 2026-11-09）
产物：data/etf_quotes.json（gitignore，含个人执行参考但不含资产数据）

诚实性约束（热备）：
  * 备源只写本次真实命中的代码，绝不把旧快照里未命中的条目混进来；
  * 命中数 / 有效报价数不足阈值一律拒绝覆盖已有快照（宁可继续 stale）；
  * 盘中拉取的成交额是半日量，写入 intraday 标记，由披露层单独说明；
  * 写入用 tmp + os.replace，避免半截文件把快照打坏。
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
from typing import Any, Dict, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import etf_quotes  # noqa: E402

OUT = os.path.join(BASE, "data", "etf_quotes.json")
QUOTE_URL = ("https://push2.eastmoney.com/api/qt/clist/get"
             "?pn={pn}&pz=100&po=1&fid=f3&fs=b:MK0021&fields=f2,f6,f12,f14")
GTIMG = "https://qt.gtimg.cn/q={secids}"
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
GTIMG_BATCH = 60        # gtimg 每批代码数（实测 60 只全量返回）
MIN_CODES = 800         # 低于此条数疑似分页不全/批量漏拉，拒绝覆盖
MIN_PRICED = 500        # 有效报价下限


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


def _existing_codes() -> list:
    """热备的代码清单：现有快照 keys ∪ etf_map 的 etf_code / candidates。

    备源没有「全量列表」接口，只能用已经落盘的代码集合反查；
    这些代码本身不含行情，不会被误当成今天的报价。
    """
    codes = set()
    try:
        with open(OUT, encoding="utf-8") as fh:
            for c in (json.load(fh).get("quotes") or {}):
                if re.fullmatch(r"\d{6}", str(c)):
                    codes.add(str(c))
    except Exception:
        pass
    try:
        import yaml
        with open(os.path.join(BASE, "etf_map.yaml"), encoding="utf-8") as fh:
            ins = (yaml.safe_load(fh) or {}).get("instruments") or {}
        for v in ins.values():
            v = v or {}
            ec = str(v.get("etf_code") or "")
            if re.fullmatch(r"\d{6}", ec):
                codes.add(ec)
            for cand in v.get("candidates") or []:
                c = str((cand or {}).get("code") or "")
                if re.fullmatch(r"\d{6}", c):
                    codes.add(c)
    except Exception:
        pass
    return sorted(codes)


def _gtimg_amount(f: list) -> Optional[float]:
    """成交额（元）：优先 f[35] 第三段（精确到元），回落 f[37]（万元 ×1e4）。"""
    try:
        parts = str(f[35]).split("/")
        if len(parts) >= 3:
            v = float(parts[2])
            if v > 0:
                return v
    except Exception:
        pass
    try:
        v = float(f[37]) * 1e4
        return v if v > 0 else None
    except Exception:
        return None


def parse_gtimg(txt: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """解析 gtimg 批量响应 → ({code: 行情}, 最新行情时间戳 yyyymmddHHMMSS)。"""
    out: Dict[str, Dict[str, Any]] = {}
    qtime = ""
    for line in txt.splitlines():
        m = re.match(r'v_\w+="(.*?)";?', line.strip())
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 38 or not re.fullmatch(r"\d{6}", f[2]):
            continue
        try:
            price: Optional[float] = float(f[3])
        except Exception:
            price = None
        if price is not None and not (0.05 < price < 50):
            price = None
        if re.fullmatch(r"\d{14}", str(f[30])):
            qtime = max(qtime, str(f[30]))
        out[f[2]] = {"name": f[1], "market": _market(f[2]),
                     "price": price, "amount": _gtimg_amount(f)}
    return out, qtime


def fetch_gtimg(codes, batch: int = GTIMG_BATCH):
    """腾讯 gtimg 批量热备；单批失败不影响其它批，meta 记录命中/失败数。"""
    out: Dict[str, Dict[str, Any]] = {}
    qtime, failed = "", 0
    for i in range(0, len(codes), batch):
        secids = ",".join(_market(c) + c for c in codes[i:i + batch])
        try:
            req = urllib.request.Request(GTIMG.format(secids=secids),
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20, context=_ctx()) as r:
                txt = r.read().decode("gb18030", errors="replace")
            part, qt = parse_gtimg(txt)
            out.update(part)
            qtime = max(qtime, qt)
        except Exception as exc:
            failed += 1
            print(f"[warn] gtimg 第 {i // batch + 1} 批失败：{type(exc).__name__}",
                  file=sys.stderr)
    meta = {"requested": len(codes), "returned": len(out),
            "batches": (len(codes) + batch - 1) // batch,
            "batch_failed": failed, "quote_time": qtime or None}
    return out, meta


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


def quality_ok(q: Dict[str, Dict[str, Any]]) -> Tuple[bool, str]:
    """覆盖度闸门：条数与有效报价任一不足 → 拒绝覆盖已有快照（宁可继续 stale）。"""
    q = q or {}
    priced = sum(1 for v in q.values() if v.get("price"))
    if len(q) < MIN_CODES:
        return False, f"行情仅 {len(q)} 只（疑似分页/批量不全），拒绝覆盖已有快照"
    if priced < MIN_PRICED:
        return False, f"有效报价仅 {priced} 只，拒绝覆盖已有快照"
    return True, ""


def _intraday(qtime: Optional[str]) -> bool:
    """行情时间戳落在交易时段内 → 成交额是半日量，容量口径偏保守。"""
    s = str(qtime or datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    try:
        hhmmss = int(s[8:14])
    except Exception:
        return False
    return 93000 <= hhmmss < 150000


def refresh() -> int:
    q: Optional[Dict[str, Dict[str, Any]]] = None
    fb: Optional[Dict[str, Any]] = None
    qtime: Optional[str] = None
    primary_error = ""
    try:
        cand = fetch()
        ok, why = quality_ok(cand)
        if ok:
            q = cand
        else:
            primary_error = why
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"

    if q is None:
        print(f"[warn] 主源东财不可用（{primary_error}）→ 切换腾讯 gtimg 热备")
        codes = _existing_codes()
        print(f"  热备代码清单 {len(codes)} 只（现有快照 ∪ 映射台账）")
        if len(codes) < MIN_CODES:
            print(f"[ERR] 代码清单仅 {len(codes)} 只，无法热备；保留旧快照")
            return 1
        q2, meta = fetch_gtimg(codes)
        ok, why = quality_ok(q2)
        if not ok:
            print(f"[ERR] 热备 {why}；保留旧快照（继续 stale，护栏生效）")
            return 1
        q, qtime = q2, meta.get("quote_time")
        fb = {"used": True, "primary_error": primary_error,
              "requested": meta["requested"], "returned": meta["returned"],
              "batches": meta["batches"], "batch_failed": meta["batch_failed"],
              "quote_time": qtime}
        print(f"  gtimg 命中 {meta['returned']}/{meta['requested']} 只，"
              f"{meta['batches']} 批（失败 {meta['batch_failed']} 批）")

    priced = sum(1 for v in q.values() if v.get("price"))
    intraday = _intraday(qtime)
    doc = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": ("东方财富 push2 clist（f2/f6/f12/f14）；腾讯 gtimg 抽样校验"
                   if fb is None else
                   "腾讯 gtimg 批量热备（主源东财不可用）；成交额 f35/f37 已换算为元"),
        "count": len(q), "priced": priced,
        "intraday": intraday,
        "fallback": fb or {"used": False},
        "cross_check": (cross_check(q, ["588000", "159949", "510300"])
                        if fb is None else
                        "主源不可用，备源本身即腾讯口径；跳过抽样交叉校验"),
        "quotes": q,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)          # 原子替换，避免半截快照
    print(f"OK 行情 {len(q)} 只（有效报价 {priced} 只）→ {OUT}")
    print("   来源：" + doc["source"])
    print("   交叉校验：" + doc["cross_check"])
    if intraday:
        print(f"   ⚠️ 盘中行情（{qtime or '本地时间'}）：成交额为半日量，"
              "容量闸偏保守，披露层将标注")
    return 0


def check() -> int:
    snap = etf_quotes.load(BASE)
    print("ETF 行情快照校验")
    print(f"  ok={snap.get('ok')} count={snap.get('count', 0)} "
          f"fetched_at={snap.get('fetched_at')} age_hours={snap.get('age_hours')}")
    print(f"  stale={snap.get('stale')} reason={snap.get('reason') or '—'}")
    try:
        with open(OUT, encoding="utf-8") as fh:
            meta = json.load(fh)
        fb = meta.get("fallback") or {}
        print(f"  source={meta.get('source') or '—'}")
        print(f"  intraday={meta.get('intraday')} fallback_used={fb.get('used', False)}"
              + (f" hit={fb.get('returned')}/{fb.get('requested')}"
                 if fb.get("used") else ""))
    except Exception:
        pass
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
