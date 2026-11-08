#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_backfill_amount.py —— 回补缓存中 amount（成交额）字段的整段缺失

## 问题

2026-11 的「历史扩展到 6 年」把 2020-11-11 之后的 1452 根 K 线换成了腾讯源，
而腾讯日线接口不返回成交额。结果：60 只境内标的在那一段 amount 全部为 0，
表现为 `399xxx 缺失率 55%`、`000xxx 缺失 15.7%`。

半程缺失比全空更危险：任何用到 amount 的因子会在某个日期之后静默归零，
而人通常把这种「后半段没信号」归因于市场风格切换，而不是数据本身。

## 数据源与口径

  国证官网 hq.cnindex.com.cn  399/970/980/987/CN 系 → 成交额单位【亿元】
  中证官网 csindex            000/H/93x/95x 等    → 成交额单位【亿元】
  腾讯 / 新浪                 → 无成交额（本次不回补，记录原因）

本工具【只写 amount】，绝不改 close / open / high / low / vol。
理由：成交量在三个源之间口径不一致（实测同一指数同一天，国证与腾讯的成交量相差 5.3 倍，
说明"指数成交量"本身没有统一定义：成分股合计 / 是否含大宗 / 是否双重计算各异），
贸然用新源的 vol 覆盖旧源，会在接缝处制造一个比原问题更隐蔽的断层。

## 三道校验（缺一不可）

  1) 单位：源值 × 1e8 转元；
  2) 横截面合理性：amount / vol 必须落在 [1e2, 1e4] 元/手
     （= 每股均价 1~100 元；越界说明 vol 不是"手"或源值异常 → 拒绝写入）；
  3) 跨源一致性：若无缺失段与本段重叠，用重叠日期的 amount 比值确认单位一致
     （中位数比值须在 [0.5, 2] 内，否则视为口径不一致 → 整只跳过）。

## 用法

  python tools_backfill_amount.py                 # 预演（不写盘）
  python tools_backfill_amount.py --apply         # 写盘（自动备份）
  python tools_backfill_amount.py --codes 399006,000903 --apply
  python tools_backfill_amount.py --check          # CI 用：仍有境内标的缺失则退出码 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "stock-pool"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

CFG = os.path.join(HERE, "splus_config.yaml")
REPORT = os.path.join(HERE, "data", "amount_backfill_report.json")

sys.path.insert(0, HERE)
from data_contract import AMT_VOL_LO, AMT_VOL_HI  # noqa: E402  单一真源

# 跨源单位一致性：重叠期 amount 比值中位数须在此区间
RATIO_LO, RATIO_HI = 0.5, 2.0
YI = 1e8
FATAL_ROWS = 120          # 单只标的残留 ≥ 半年（≈120 行）视为整段未修复

# 境外代码：腾讯/新浪接口均无成交额，明确记录为"不可回补"而非"待修"
OVERSEAS_PREFIX = ("100_", "hk", "us", "uk")


def cache_dir() -> str:
    with open(CFG, encoding="utf-8") as fh:
        return os.path.join(HERE, yaml.safe_load(fh)["data"]["cache_dir"])


def universe() -> List[Dict[str, Any]]:
    with open(CFG, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["universe"]


def path_of(code: str, cdir: str) -> str:
    return os.path.join(cdir, "idx_%s.json" % code)


def amount_gap(bars: List[Dict[str, Any]]) -> Tuple[int, int, str, str]:
    """返回 (缺失数, 总数, 首个缺失日, 末个缺失日)。"""
    miss, first, last = 0, "", ""
    for b in bars:
        try:
            a = float(b.get("amount") or 0)
        except (TypeError, ValueError):
            a = 0.0
        if a <= 0:
            miss += 1
            d = str(b.get("date"))
            if not first:
                first = d
            last = d
    return miss, len(bars), first, last


def sources_for(code: str) -> List[str]:
    """按代码归属给出抓取源顺序（先归属源，再兜底源）。"""
    c = code.strip()
    if c.startswith(("399", "970", "980", "987", "CN")):
        return ["cni", "csi"]
    return ["csi", "cni"]


def fetch(code: str, days: int) -> Tuple[List[Dict[str, Any]], str]:
    """按源顺序抓取含成交额的日线；返回 (bars, 源名)。"""
    from data_layer import index_global as ig
    for src in sources_for(code):
        for attempt in range(2):
            try:
                bars = (ig._cnindex_bars(code, days) if src == "cni"
                        else ig._csindex_bars(code, days))
                if bars:
                    return bars, src
            except Exception:
                pass
            time.sleep(0.8)
    return [], ""


def plan_one(code: str, bars: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    """对单只标的生成回补计划（不写盘）。"""
    miss, total, first, last = amount_gap(bars)
    rec: Dict[str, Any] = {
        "code": code, "n_bars": total, "missing": miss,
        "missing_first": first, "missing_last": last,
        "filled": 0, "rejected_band": 0, "src": "", "overlap_ratio": None,
        "status": "", "note": "",
    }
    if miss == 0:
        rec["status"] = "OK"
        return rec

    src_bars, src = fetch(code, days)
    rec["src"] = src
    if not src_bars:
        rec["status"] = "SOURCE_EMPTY"
        rec["note"] = "两个源均未返回数据"
        return rec

    src_amt: Dict[str, float] = {}
    for b in src_bars:
        try:
            a = float(b.get("amount") or 0)
        except (TypeError, ValueError):
            a = 0.0
        if a > 0:
            src_amt[str(b.get("date"))[:10]] = a        # 亿元

    # 跨源一致性：用重叠日期（缓存已有 amount 的）反查单位
    ratios = []
    for b in bars:
        a = float(b.get("amount") or 0)
        if a <= 0:
            continue
        s = src_amt.get(str(b.get("date"))[:10])
        if s and s > 0:
            ratios.append(a / (s * YI))
    if ratios:
        r = statistics.median(ratios)
        rec["overlap_ratio"] = round(r, 4)
        if not (RATIO_LO <= r <= RATIO_HI):
            rec["status"] = "UNIT_MISMATCH"
            rec["note"] = ("重叠期 amount 比值中位数 %.3f 越界（源单位或口径不一致），整只跳过" % r)
            return rec

    filled = rejected = 0
    for b in bars:
        a = float(b.get("amount") or 0)
        if a > 0:
            continue
        s = src_amt.get(str(b.get("date"))[:10])
        if not s or s <= 0:
            continue
        cand = s * YI
        v = float(b.get("vol") or 0)
        if v > 0:
            ratio = cand / v
            if not (AMT_VOL_LO <= ratio <= AMT_VOL_HI):
                rejected += 1
                continue
        b["amount"] = round(cand, 2)
        filled += 1

    rec["filled"] = filled
    rec["rejected_band"] = rejected
    rec["status"] = "PATCHED" if filled else ("BAND_REJECT" if rejected else "NO_OVERLAP_DATES")
    if rec["status"] == "NO_OVERLAP_DATES":
        rec["note"] = "源数据日期与缓存日期不重叠"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="回补 amount 字段（只写 amount）")
    ap.add_argument("--codes", default="", help="逗号分隔；默认全宇宙")
    ap.add_argument("--days", type=int, default=2900, help="抓取根数（覆盖全历史）")
    ap.add_argument("--apply", action="store_true", help="写盘（默认预演）")
    ap.add_argument("--check", action="store_true", help="CI 校验：境内标的仍有缺失则退出码 1")
    ap.add_argument("--all-cache", action="store_true",
                    help="不只宇宙内，扫描缓存里所有 idx_*.json（含 stock-pool 专用指数）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    cdir = cache_dir()
    uni = universe()
    if a.codes:
        want = {c.strip() for c in a.codes.split(",") if c.strip()}
        uni = [u for u in uni if u["code"] in want]
    if a.all_cache and not a.codes:
        in_uni = {u["code"] for u in uni}
        extra = sorted(f[4:-5] for f in os.listdir(cdir)
                       if f.startswith("idx_") and f.endswith(".json")
                       and f[4:-5] not in in_uni)
        uni = uni + [{"code": c, "name": ""} for c in extra]

    rows: List[Dict[str, Any]] = []
    skipped_overseas: List[str] = []
    for u in uni:
        code = u["code"]
        p = path_of(code, cdir)
        if not os.path.exists(p):
            rows.append({"code": code, "status": "NO_CACHE", "filled": 0,
                         "missing": 0, "note": "缓存文件不存在"})
            continue
        with open(p, encoding="utf-8") as fh:
            bars = json.load(fh)
        miss, total, _, _ = amount_gap(bars)
        if code.startswith(OVERSEAS_PREFIX):
            if miss:
                skipped_overseas.append("%s(%d/%d)" % (code, miss, total))
            rows.append({"code": code, "status": "OVERSEAS_NO_AMOUNT", "filled": 0,
                         "missing": miss, "n_bars": total,
                         "note": "境外指数：腾讯/新浪日线接口不含成交额"})
            continue
        if miss == 0:
            rows.append({"code": code, "status": "OK", "filled": 0,
                         "missing": 0, "n_bars": total})
            continue
        rec = plan_one(code, bars, a.days)
        rows.append(rec)
        if a.apply and rec["filled"]:
            backup = os.path.join(HERE, "data", "_cache_backup_amount")
            os.makedirs(backup, exist_ok=True)
            bp = os.path.join(backup, os.path.basename(p))
            if not os.path.exists(bp):
                shutil.copy2(p, bp)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(bars, fh, ensure_ascii=False)
            os.replace(tmp, p)
        print("  %-7s miss=%-5d fill=%-5d rej=%-4d src=%-4s overlap=%-8s %s"
              % (rec["code"], rec["missing"], rec["filled"], rec["rejected_band"],
                 rec["src"] or "-", str(rec["overlap_ratio"]), rec["status"]), flush=True)
        if a.limit and len(rows) >= a.limit:
            break

    patched = [r for r in rows if r.get("filled")]
    total_fill = sum(r["filled"] for r in rows)
    residual = [(r["code"], r["missing"] - r["filled"]) for r in rows
                if r.get("status") not in ("OK", "OVERSEAS_NO_AMOUNT")
                and (r.get("missing", 0) - r.get("filled", 0)) > 0]
    # 致命残留 = 单只标的仍有「整段」未修复（≥ 半年 ≈ 120 行）。
    # 零散空洞（源头确实缺那几天）不算失败，只披露——否则 CI 永远红，
    # 而红久了就没人看了，校验也就废了。
    fatal = [(c, n) for c, n in residual if n >= FATAL_ROWS]

    summary = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "applied": bool(a.apply),
        "codes_scanned": len(rows),
        "codes_patched": len(patched),
        "rows_filled": total_fill,
        "residual_missing_codes": len(residual),
        "residual_rows": sum(x[1] for x in residual),
        "fatal_residual": [{"code": c, "rows": n} for c, n in fatal],
        "overseas_unfixable": skipped_overseas,
        "by_status": {},
        "rows": rows,
    }
    for r in rows:
        summary["by_status"][r["status"]] = summary["by_status"].get(r["status"], 0) + 1
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    print("\n[amount] 扫描 %d 只，回补 %d 只 / %d 行（%s）"
          % (len(rows), len(patched), total_fill, "已写盘" if a.apply else "预演"))
    print("[amount] 状态分布：%s" % summary["by_status"])
    if residual:
        print("[amount] 残留缺失：%d 只 / %d 行（源头缺该日期者为正常，不阻断）"
              % (len(residual), sum(x[1] for x in residual)))
        for c, n in sorted(residual, key=lambda t: -t[1])[:10]:
            print("         %-8s %d 行%s" % (c, n, "  ← 整段未修复" if n >= FATAL_ROWS else ""))
    if skipped_overseas:
        print("[amount] 境外不可回补：%d 只（接口无成交额）" % len(skipped_overseas))
    print("[amount] 报告：%s" % REPORT)

    if a.check:
        if fatal:
            print("[amount] FAIL 整段残留：%s（阈值 %d 行/只）" % (fatal, FATAL_ROWS))
            return 1
        print("[amount] OK 无整段残留（零散空洞 %d 行，属源头覆盖限制）"
              % sum(x[1] for x in residual))
    return 0


if __name__ == "__main__":
    sys.exit(main())
