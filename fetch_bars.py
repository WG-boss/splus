# -*- coding: utf-8 -*-
"""splus 行情补拉：按 universe 全量刷新 index 日线缓存，并支持"截止到某一交易日"。

为什么要截止：
  盘中运行时，数据源会返回当日尚未走完的 K 线（收盘价=最新价、量能只有半天）。
  这根不完整的 K 线会污染 L1 状态打分（尤其 vol_z20 / er20 / atr_pct_rank），
  导致收盘前跑出的状态与收盘后跑出的状态不一致。所以默认只取到"最后一个已收盘交易日"。

用法：
  python fetch_bars.py                 # 截止到最后一个已收盘交易日（自动判断）
  python fetch_bars.py --asof 2026-10-26
  python fetch_bars.py --codes 000688,399673
  python fetch_bars.py --no-backup

退出码：0=全部成功；1=有失败（失败清单写在报告末尾，不静默）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "stock-pool"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402
from data_layer import index_global as ig  # noqa: E402

CFG = os.path.join(HERE, "splus_config.yaml")
BACKUP_DIR = os.path.join(HERE, "data", "_cache_backup")

# 收盘时间（本地，Asia/Shanghai）。A股 15:00 收盘，数据源落库通常再滞后 5~15 分钟。
CLOSE_HHMM = (15, 10)


def load_universe() -> list:
    c = yaml.safe_load(open(CFG, encoding="utf-8"))
    return [x for x in c["universe"] if x.get("enabled", True)]


def last_closed_trading_day(now: dt.datetime | None = None) -> dt.date:
    """最后一个已收盘交易日（仅按工作日粗判，不含节假日历）。

    节假日会表现为"数据源某日无K线"，此时补拉结果自然停在节前最后交易日，
    不会伪造数据；这里的返回值只是"上界"，用来砍掉当日未走完的K线。
    """
    now = now or dt.datetime.now()
    d = now.date()
    if d.weekday() >= 5:  # 周六 / 周日
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d
    if (now.hour, now.minute) < CLOSE_HHMM:
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
    return d


def tencent_fallback(code: str, days: int) -> list:
    """腾讯日线兜底（带交易所前缀）。

    背景：东财 push2his 对深证系指数（如 399673 创业板50）偶发持续返回空，
    导致该只长期停在旧日期且不报错。腾讯需要 sz/sh 前缀才认，
    裸码请求会静默返回空——所以这里显式拼前缀。
    """
    if code.startswith("sz") or code.startswith("sh"):
        sym = code
    elif code.startswith("399") or code.startswith("39"):
        sym = "sz" + code
    elif code.startswith("000") or code.startswith("93") or code.startswith("95"):
        sym = "sh" + code
    else:
        return []
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,,,%d,qfq" % (sym, days))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=25).read().decode()
        j = json.loads(raw)
    except Exception:
        return []
    d = (j.get("data") or {}).get(sym) or {}
    rows = d.get("day") or d.get("qfqday") or []
    out = []
    for r in rows:
        try:
            out.append({"date": str(r[0])[:10], "open": float(r[1]), "close": float(r[2]),
                        "high": float(r[3]), "low": float(r[4]), "vol": float(r[5]),
                        "amount": None})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


def cache_path(code: str) -> str:
    safe = code.replace(".", "_").replace("/", "_")
    return os.path.join(ROOT, "stock-pool", "_cache", "idx_%s.json" % safe)


def backup_once(code: str) -> None:
    src = cache_path(code)
    if not os.path.exists(src):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, os.path.basename(src))
    if os.path.exists(dst):  # 只留第一次的原始备份，不覆盖
        return
    try:
        shutil.copy2(src, dst)
    except Exception:
        pass


def tail_date(code: str) -> str:
    p = cache_path(code)
    if not os.path.exists(p):
        return ""
    try:
        rows = json.load(open(p, encoding="utf-8"))
        return str(rows[-1].get("date", "")) if rows else ""
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="")
    ap.add_argument("--codes", default="")
    ap.add_argument("--days", type=int, default=560)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    asof = args.asof or last_closed_trading_day().isoformat()
    uni = load_universe()
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        uni = [x for x in uni if x["code"] in want]

    print("[fetch] 截止日=%s  标的数=%d  回溯=%d 根" % (asof, len(uni), args.days))
    t0 = time.time()

    ok, stale, fail = [], [], []
    for i, item in enumerate(uni, 1):
        code, name = item["code"], item["name"]
        if not args.no_backup:
            backup_once(code)
        rows = []
        try:
            rows = ig.bars(code, days=args.days, use_cache=False,
                           expect_name=(name if code.startswith("H") else ""))
        except Exception as e:  # 单只失败不拖垮整批
            fail.append((code, name, "err: %s" % e))
            continue

        # 东财/中证没拿到，或拿到的最后日期落后 asof，则试腾讯带前缀兜底
        if not rows or str(rows[-1].get("date", "")) < asof:
            alt = tencent_fallback(code, args.days)
            if alt and (not rows or str(alt[-1].get("date", "")) > str(rows[-1].get("date", ""))):
                m = {r["date"]: r for r in (rows or [])}
                m.update({r["date"]: r for r in alt})
                rows = [m[k] for k in sorted(m)]

        if not rows:
            fail.append((code, name, "no bars"))
            continue

        # 砍掉 asof 之后的 K 线（含盘中未走完的当日K）
        clipped = [r for r in rows if str(r.get("date", "")) <= asof]
        if not clipped:
            fail.append((code, name, "all bars > asof"))
            continue

        if len(clipped) < len(rows):  # 发生了裁剪，需要回写避免缓存里留着盘中K
            p = cache_path(code)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(clipped, fh, ensure_ascii=False)

        last = str(clipped[-1]["date"])
        (ok if last == asof else stale).append((code, name, last, len(clipped)))
        if i % 15 == 0 or i == len(uni):
            print("  ... %d/%d  elapsed %.0fs" % (i, len(uni), time.time() - t0))

    print("\n[fetch] 成功到 %s: %d 只" % (asof, len(ok)))
    if stale:
        print("[fetch] 未到 %s（数据源缺该日或停牌/非交易日）: %d 只" % (asof, len(stale)))
        for code, name, last, n in stale:
            print("   - %-8s %-20s last=%s n=%d" % (code, name, last, n))
    if fail:
        print("[fetch] 失败: %d 只" % len(fail))
        for code, name, why in fail:
            print("   - %-8s %-20s %s" % (code, name, why))

    rep = {
        "asof": asof,
        "total": len(uni),
        "ok": len(ok),
        "stale": [{"code": c, "name": n, "last": l} for c, n, l, _ in stale],
        "fail": [{"code": c, "name": n, "why": w} for c, n, w in fail],
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    with open(os.path.join(HERE, "data", "fetch_report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)

    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
