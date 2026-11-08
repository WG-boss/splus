#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_contract.py —— 行情字段的「单位与口径契约」（唯一真源）

## 为什么需要它

量纲问题是本项目最隐蔽的一类缺陷：它不报错、不崩、不留日志，只让因子在某一段
静默失真。已经踩过的坑：

  * 2026-11-07：3 只标的 8 个年份段的 vol 相差 100 倍（中证官网源用「股」写入
    「手」的序列），而同期 amount 缺失使 amt/vol 护栏失效，`vol_z20` 整段跳变。
  * 2026-11-08：60 只标的 2020-11-11 之后 amount 全为 0（腾讯源无成交额），
    任何依赖 amount 的因子在那之后静默归零。
  * 同一指数同一天，国证官网与腾讯的成交量相差 5.3 倍——这不是单位问题，
    而是「指数成交量」本身没有统一定义（成分股合计 / 是否含大宗 / 是否双重计算）。

## 契约内容

1. **规范形（canonical）**：`vol` = 手（lots），`amount` = 元（CNY）。
2. **源 → 规范形的换算表**（下表 SOURCE_UNITS），新源接入必须先登记。
3. **可验证性**：`infer_units()` 用 amount/vol 的比值反推当前序列到底是哪套口径，
   判据无歧义（两套口径相差 9 个数量级）。
4. **不变量（invariant）**：所有进入状态机的量能特征必须是「同一序列内的比值」，
   因此对整体缩放不变。这条不是假设，由 `selftest_contract.py` 实测断言。
   推论：跨标的的量纲不一致不影响信号，但**同一序列内部的量纲断点**致命。
5. **溯源清单**：`scan_manifest()` 把逐只标的的推断结果写成 `data/units_manifest.json`，
   供审计与回归比对（源变了、口径变了，清单会变）。

## 用法

  import data_contract as dc
  bar = dc.canonical_bar(raw_row)              # 单行归一
  units = dc.infer_units(vol, amount)          # → "lots_cny" / "shares_yi" / "unknown"
  ok = dc.ratio_plausible(vol, amount)         # amt/vol 是否落在 1e2~1e4 元/手

  python data_contract.py                      # 扫描全库并刷新 data/units_manifest.json
  python data_contract.py --check              # CI：清单与实测不一致则退出码 1
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "data", "units_manifest.json")

# ---------------------------------------------------------------- 规范形
CANON_VOL = "lots"       # 手
CANON_AMT = "cny"        # 元

# 源 → (vol 换算到「手」的乘数, amount 换算到「元」的乘数)
# None 表示该源不提供此字段。新增数据源必须在此登记，否则 canonical_bar 会拒绝。
SOURCE_UNITS: Dict[str, Tuple[Optional[float], Optional[float]]] = {
    "em":       (1.0,   1.0),    # 东财 push2his：f56=手, f57=元
    "tencent":  (1.0,   None),   # 腾讯 gtimg：成交量=手，无成交额
    "sina":     (1.0,   None),   # 新浪 getKLineData：成交量=股（指数口径下与手同量级近似），无成交额
    "csindex":  (1 / 100.0, 1e8),  # 中证官网：成交量=股, 成交额=亿元
    "cnindex":  (1 / 100.0, 1e8),  # 国证官网：成交量=股（万手×1e4）, 成交额=亿元
    "sse_cni":  (1 / 100.0, 1e8),  # stock-pool 的 sse_cni 兜底：股 + 亿元
}

# amount/vol 合理区间：元/手 = 100 × 每股均价。
# 上界放到 5e4（= 每股 500 元），因为成分股以百元股为主的指数（如 399296 隐含约
# 120 元/股）会被 1e4 误杀；下界 1e2（= 每股 1 元）。
# 注意：这个区间是【单位自洽性】判据，不是估值判据——错误口径会偏 5 个数量级以上，
# 因此门槛在此区间内任意取值都不影响判别力。
AMT_VOL_LO, AMT_VOL_HI = 1e2, 5e4
# 两套口径的判别门槛：比值低于此值即判为「股 + 亿元」
UNIT_SPLIT = 1e-3

# 市场 → 成交量语义说明（写入清单，供人读；不参与计算）
MARKET_VOL_NOTE = {
    "CN": "A 股指数：成交量通常为成分股合计（手）；不同源对'合计范围'定义不一，跨源不可比",
    "HK": "港股指数：腾讯源 vol 为成交额口径（港元），与 A 股'手'不可比；仅可做同序列比值",
    "US": "美股指数：腾讯源 vol 为股数；同上，仅可做同序列比值",
}


def market_of(code: str) -> str:
    c = (code or "").strip()
    if c.startswith("hk") or c.startswith("HSC"):
        return "HK"
    if c.startswith("us") or c.startswith("uk") or c.startswith("100_"):
        return "US"
    return "CN"


def infer_units(vol: float, amount: float) -> str:
    """用 amount/vol 反推口径。判据：两套口径相差 9 个数量级，无歧义。"""
    try:
        v = float(vol or 0)
        a = float(amount or 0)
    except (TypeError, ValueError):
        return "unknown"
    if v <= 0:
        return "no_vol"
    if a <= 0:
        return "no_amount"
    return "lots_cny" if (a / v) >= UNIT_SPLIT else "shares_yi"


def ratio_plausible(vol: float, amount: float) -> Optional[bool]:
    """amount/vol 是否落在合理区间（元/手）。供回补/审计工具做写前校验。"""
    try:
        v = float(vol or 0)
        a = float(amount or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0 or a <= 0:
        return None
    return AMT_VOL_LO <= (a / v) <= AMT_VOL_HI


def canonical_bar(row: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """把一行原始 K 线归一为规范形（手 + 元）。

    source 为空时按 amount/vol 比值自动判定（历史缓存的常态）；
    指定 source 时按注册表换算，并校验换算后比值合理，不合理则退回自动判定。
    """
    vol = _f(row.get("vol"))
    amt = _f(row.get("amount"))
    close = _f(row.get("close"))

    if source:
        vm, am = SOURCE_UNITS.get(source, (None, None))
        if vm is not None:
            vol_v = vol * vm
            amt_v = amt * am if am is not None else amt
            if not (vol > 0 and amt > 0):
                return {"vol": vol_v, "amount": amt_v, "rule": "source:" + source}
            if ratio_plausible(vol_v, amt_v):
                return {"vol": vol_v, "amount": amt_v, "rule": "source:" + source}
            # 换算后不合理 → 该行的真实来源与声明不符，退回自动判定
            return {"vol": vol, "amount": amt, "rule": "source-mismatch-fallback"}

    if amt > 0 and vol > 0 and (amt / vol) < UNIT_SPLIT:
        return {"vol": vol / 100.0, "amount": amt * 1e8, "rule": "auto:shares_yi"}
    return {"vol": vol, "amount": amt, "rule": "auto:lots_cny" if amt > 0 else "auto:no_amount"}


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def series_units(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一条序列做口径体检：主导口径、断点数、隐含每股均价。"""
    votes: Dict[str, int] = {}
    ratios: List[float] = []
    for b in bars:
        v = _f(b.get("vol"))
        a = _f(b.get("amount"))
        if v > 0 and a > 0:
            ratios.append(a / v)
            u = infer_units(v, a)
            votes[u] = votes.get(u, 0) + 1
    dom = max(votes.items(), key=lambda kv: kv[1])[0] if votes else "no_amount"
    out: Dict[str, Any] = {
        "dominant": dom,
        "n_ratio": len(ratios),
        "ratio_median": round(statistics.median(ratios), 4) if ratios else None,
    }
    if ratios:
        # 隐含每股均价 = ratio / 100（元/手 → 元/股）
        out["implied_price_per_share"] = round(statistics.median(ratios) / 100.0, 2)
        out["price_plausible"] = 0.1 <= out["implied_price_per_share"] <= 2000
    # 断点：与主导口径不一致的行占比
    mixed = sum(1 for k, v in votes.items() if k != dom)
    out["mixed_share"] = round(mixed / max(1, sum(votes.values())), 5)
    return out


def scan_manifest(cache_dir: str, codes: List[str]) -> Dict[str, Any]:
    """逐只标的的口径清单（供审计/回归比对）。"""
    items: Dict[str, Any] = {}
    for code in codes:
        p = os.path.join(cache_dir, "idx_%s.json" % code)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                bars = json.load(fh)
        except Exception:
            continue
        info = series_units(bars)
        info["market"] = market_of(code)
        info["market_vol_note"] = MARKET_VOL_NOTE[info["market"]]
        info["n_bars"] = len(bars)
        amt_miss = sum(1 for b in bars if _f(b.get("amount")) <= 0)
        info["amount_missing_share"] = round(amt_miss / max(1, len(bars)), 4)
        items[code] = info
    return {
        "canonical": {"vol": CANON_VOL, "amount": CANON_AMT},
        "source_units": {k: list(v) for k, v in SOURCE_UNITS.items()},
        "ratio_band": [AMT_VOL_LO, AMT_VOL_HI],
        "items": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="行情量纲契约：扫描并刷新清单")
    ap.add_argument("--cache", default="", help="缓存目录（默认读 splus_config.yaml）")
    ap.add_argument("--out", default=MANIFEST)
    ap.add_argument("--check", action="store_true", help="与既有清单不一致则退出码 1")
    a = ap.parse_args()

    cdir = a.cache
    codes: List[str] = []
    if not cdir:
        import yaml
        with open(os.path.join(HERE, "splus_config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        cdir = os.path.join(HERE, cfg["data"]["cache_dir"])
        codes = [u["code"] for u in cfg["universe"]]
    if not codes:
        codes = sorted(f[4:-5] for f in os.listdir(cdir)
                       if f.startswith("idx_") and f.endswith(".json"))

    man = scan_manifest(cdir, codes)
    items = man["items"]
    bad_unit = {c: v for c, v in items.items()
                if v["dominant"] not in ("lots_cny", "no_amount")}
    mixed = {c: v for c, v in items.items() if v.get("mixed_share", 0) > 0}
    price_bad = {c: v for c, v in items.items() if v.get("price_plausible") is False}

    print("[contract] 规范形：vol=%s, amount=%s" % (CANON_VOL, CANON_AMT))
    print("[contract] 标的=%d  口径异常=%d  混口径=%d  隐含均价可疑=%d"
          % (len(items), len(bad_unit), len(mixed), len(price_bad)))
    for c, v in sorted(bad_unit.items()):
        print("  [unit] %-8s dominant=%s ratio=%s" % (c, v["dominant"], v["ratio_median"]))
    for c, v in list(sorted(price_bad.items()))[:10]:
        print("  [price] %-8s 隐含每股均价=%s 元（越界）" % (c, v.get("implied_price_per_share")))
    for c, v in list(sorted(mixed.items(), key=lambda kv: -kv[1]["mixed_share"]))[:10]:
        print("  [mixed] %-8s 非主导口径占比=%.2f%%" % (c, v["mixed_share"] * 100))

    if a.check and os.path.exists(a.out):
        old = json.load(open(a.out, encoding="utf-8"))
        o, n = old.get("items", {}), items
        drift = [c for c in set(o) & set(n) if o[c].get("dominant") != n[c].get("dominant")]
        if drift:
            print("[contract] FAIL 口径漂移：%s" % drift[:10])
            return 1
        print("[contract] OK 清单与实测一致")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    print("[contract] 清单已写 %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
