# -*- coding: utf-8 -*-
"""
数据库记分卡（Data Scorecard）——splus 指数历史库的三维体检。

设计原则：
  1) 只读：不修改缓存、不联网。跨源核价由 tools_data_audit.py 负责，本工具只做内证。
  2) 三条独立真相线：
       - 完整性 = 各标的在自身区间内对交易日历的覆盖度 + 缺口长度 + 末端新鲜度
       - 准确性 = OHLC 逻辑约束、非正价、重复日期、收益跳变、零量比
       - 度量统一 = amt/vol 比率判定量纲 + 按年断层扫描 + 日历归属
  3) 日历由数据自身推导（全 A 股并集 + 出现频次门槛），不硬编码节假日表，
     因此对新增标的、新增市场天然兼容。
  4) 幂等：两次运行除 generated_at 外字节一致。

输出：
  data/db_scorecard.json   机读结果（供 CI / 看板 / 后续回归）
  data/db_scorecard.html   人读记分卡（自包含，无外部依赖）

用法：
  python tools_db_scorecard.py                 # 全量
  python tools_db_scorecard.py --only 000300    # 单标的调试
  python tools_db_scorecard.py --no-html        # 只出 json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# 阈值（全部外置，便于随策略升级调整；此处给默认值）
# ----------------------------------------------------------------------------
TH = {
    # 完整性
    "coverage_fail": 0.90,      # 覆盖率 < 90% 判 FAIL
    "coverage_warn": 0.98,      # 覆盖率 < 98% 判 WARN
    "max_gap_fail": 60,         # 单段缺口 > 60 个交易日判 FAIL（超一个季度）
    "max_gap_warn": 5,          # 单段缺口 > 5 个交易日判 WARN（超一周）
    "stale_fail": 5,            # 末端落后最新交易日 > 5 日判 FAIL
    "stale_warn": 1,
    # 准确性
    "ohlc_bad_rate_fail": 0.01,  # OHLC 违规率 > 1% 判 FAIL
    "ret_outlier": 0.15,        # 单日 |收益| > 15% 视为待查（指数无涨跌停）
    "ret_outlier_fail": 5,      # 超过 5 次判 FAIL
    "event_min_codes": 5,       # 同日 >= 5 只标的出现超阈收益 → 判为市场级共因事件
    "sector_min_codes": 2,      # 同日 >= 2 只标的 |收益|>10% → 判为板块级共因事件
    "sector_ret": 0.10,         # 板块级共因的次级收益阈值
    # 度量统一
    "unit_ratio_min": 1e-3,     # amt/vol < 1e-3 判定为「股 + 亿元」
    "unit_break_lo": 0.2,       # 相邻年量纲比率 < 0.2 或 > 5 判断层
    "unit_break_hi": 5.0,
    # 结构
    "min_bars_warn": 240,       # 少于 240 根（约一年）在回测中不可用
    "min_bars_fail": 120,
}


# ----------------------------------------------------------------------------
# 载入
# ----------------------------------------------------------------------------
def load_universe(config_path: str) -> List[Dict[str, Any]]:
    import yaml

    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    out: List[Dict[str, Any]] = []
    for item in cfg.get("universe", []) or []:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": item.get("name", ""),
            "bucket": item.get("bucket", ""),
            "role": item.get("role", ""),
            "tradeable": bool(item.get("tradeable", False)),
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def market_of(code: str, name: str = "") -> str:
    """
    市场归属——决定用哪套交易日历。

    CN      ：纯 A 股日历
    CN_HK   ：港股通 / 香港主题指数。其交易日 = A 股交易日 ∩ 港股交易日，
              比 A 股少约 2-3%（两边休市不重叠的日子），因此**不能**用 A 股日历
              判它"缺数据"，否则每只都会凭空多出 25-43 个伪缺口。
    HK / US ：独立市场日历
    """
    c = code.strip()
    if c.startswith("hk"):
        return "HK"
    if c.startswith("us"):
        return "US"
    if any(h in (name or "") for h in ("港股通", "香港")):
        return "CN_HK"
    return "CN"


def load_bars(cache_dir: str, code: str) -> List[Dict[str, Any]]:
    """读缓存并做与 engine.load_bars 同口径的清洗（不写回）。"""
    path = os.path.join(cache_dir, "idx_%s.json" % code)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return []
    bars: List[Dict[str, Any]] = []
    for r in raw:
        try:
            c = float(r.get("close"))
        except (TypeError, ValueError):
            continue
        if c is None or c <= 0:
            continue
        bars.append({
            "date": str(r.get("date")),
            "open": _f(r.get("open"), c),
            "high": _f(r.get("high"), c),
            "low": _f(r.get("low"), c),
            "close": c,
            "vol": _f(r.get("vol"), 0.0),
            "amount": _f(r.get("amount"), 0.0),
        })
    # 原始重复日期在清洗前统计，故此处保留重复以便下游发现
    bars.sort(key=lambda x: (x["date"], x["close"]))
    return bars


def _f(v: Any, dflt: float) -> float:
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return dflt


# ----------------------------------------------------------------------------
# 交易日历推导
# ----------------------------------------------------------------------------
def build_calendar(all_dates: Dict[str, set], markets: Dict[str, str],
                   spans: Optional[Dict[str, Tuple[str, str]]] = None) -> Dict[str, List[str]]:
    """
    从数据自身推导交易日历，避免硬编码节假日表（易过期、且无法适配新市场）。

    核心难点：少数标的会在法定休市日发布"伪 K 线"（前值填充或数据源错发），
    若简单取并集，2024 年春节/清明/五一/中秋/国庆共 10 个休市日会被 3-6 只标的
    带进日历，导致其余 102 只标的全部"缺 10 天"，覆盖率从 100% 掉到 97.8%——
    这是第二版的主要误报源，也是真实存在的脏数据源。

    解法（动态门槛多数表决）：
        对候选日 d，active(d) = 区间覆盖 d 的标的数（first <= d <= last）
        thresh(d) = min(active(d), max(3, ceil(active(d) * 0.5)))
        出现数 >= thresh(d) 才判为交易日。
      · 满编年份（active≈108）：门槛 54，伪 K 线的 3-6 只少数派被剔除；
      · 早期年份（active=2）：门槛 2，由长历史标的自行定义，不误判为缺口。
    """
    active_by_year: Dict[str, int] = defaultdict(int)
    for c, ds in all_dates.items():
        if markets.get(c) != "CN":
            continue
        for y in {d[:4] for d in ds}:
            active_by_year[y] += 1

    candidates = sorted({d for c, ds in all_dates.items() if markets.get(c) == "CN" for d in ds})
    freq: Dict[str, int] = defaultdict(int)
    for c, ds in all_dates.items():
        if markets.get(c) == "CN":
            for d in ds:
                freq[d] += 1

    # 扫描线求每个候选日的活跃标的数（区间覆盖计数）
    if spans:
        starts: Dict[str, int] = defaultdict(int)
        ends: Dict[str, int] = defaultdict(int)
        for c, (f, l) in spans.items():
            if markets.get(c) != "CN" or not f:
                continue
            starts[f] += 1
            ends[l] += 1
        import bisect
        sf = sorted(starts.items())
        ef = sorted(ends.items())
        s_keys = [k for k, _ in sf]
        e_keys = [k for k, _ in ef]
        s_pref, e_pref = [0], [0]
        for _, v in sf:
            s_pref.append(s_pref[-1] + v)
        for _, v in ef:
            e_pref.append(e_pref[-1] + v)

        def active_on(d: str) -> int:
            ns = s_pref[bisect.bisect_right(s_keys, d)]
            ne = e_pref[bisect.bisect_left(e_keys, d)]
            return ns - ne
    else:
        def active_on(d: str) -> int:  # type: ignore[misc]
            return 10 ** 6

    cal: Dict[str, List[str]] = {}
    kept: List[str] = []
    for d in candidates:
        n_act = active_on(d)
        thresh = min(n_act, max(3, (n_act + 1) // 2))
        if freq[d] >= thresh:
            kept.append(d)
    cal["CN"] = kept

    for mkt in ("HK", "US", "CN_HK"):
        ds = set()
        for code, s in all_dates.items():
            if markets.get(code) == mkt:
                ds |= s
        cal[mkt] = sorted(ds)
    cal["_active_by_year"] = dict(sorted(active_by_year.items()))  # type: ignore[assignment]
    return cal


def _is_weekend(d: str) -> bool:
    import datetime as _dt
    try:
        return _dt.date(int(d[0:4]), int(d[5:7]), int(d[8:10])).weekday() >= 5
    except Exception:
        return True


def weekday_share(dates: List[str]) -> float:
    """周末占比——用于识别日历污染（周末不应有 K 线）。"""
    if not dates:
        return 0.0
    return sum(1 for d in dates if _is_weekend(d)) / len(dates)


# ----------------------------------------------------------------------------
# 单标的体检
# ----------------------------------------------------------------------------
def inspect(code: str, meta: Dict[str, Any], bars: List[Dict[str, Any]],
            cal: List[str], cal_idx: Dict[str, int], last_cal_date: Optional[str],
            event_days: Optional[Dict[str, int]] = None,
            soft_days: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    event_days = event_days or {}
    soft_days = soft_days or {}
    res: Dict[str, Any] = {
        "code": code,
        "name": meta.get("name", ""),
        "bucket": meta.get("bucket", ""),
        "tradeable": meta.get("tradeable", False),
        "market": market_of(code, meta.get("name", "")),
        "bars": len(bars),
        "issues": [],
        "fails": 0,
        "warns": 0,
    }
    if not bars:
        res.update({"status": "MISSING", "first": None, "last": None, "coverage": 0.0})
        res["issues"].append({"level": "FAIL", "kind": "missing", "detail": "缓存文件缺失或读取为空"})
        res["fails"] = 1
        return res

    dates = [b["date"] for b in bars]
    res["first"], res["last"] = dates[0], dates[-1]

    # --- 完整性：覆盖率与缺口（用标的自身市场的日历） ---
    # CN_HK：用 A 股日历算覆盖率，但**不判缺陷**——港股通交易日天然比 A 股少，
    #        差值本身是度量口径事实，记入 calendar_mismatch_days 供上层对齐时使用。
    mkt = res["market"]
    if mkt in ("CN", "CN_HK") and cal:
        lo = cal_idx.get(dates[0])
        hi = cal_idx.get(dates[-1])
        if lo is not None and hi is not None and hi >= lo:
            expected_dates = cal[lo:hi + 1]
            present = set(dates)
            missing = [d for d in expected_dates if d not in present]
            res["expected"] = len(expected_dates)
            res["missing"] = len(missing)
            res["coverage"] = round(len(present & set(expected_dates)) / max(1, len(expected_dates)), 4)
            # 最长连续缺口
            runs: List[int] = []
            cur = 0
            for d in expected_dates:
                if d in present:
                    if cur:
                        runs.append(cur)
                        cur = 0
                else:
                    cur += 1
            if cur:
                runs.append(cur)
            res["max_gap"] = max(runs) if runs else 0
            res["gaps_gt5"] = sum(1 for r in runs if r > 5)
            res["missing_dates_head"] = missing[:10]
            if mkt == "CN_HK":
                res["calendar_mismatch_days"] = len(missing)
        else:
            # 首尾日不在日历内 → 该序列含有日历外日期（周末伪 K 线 / 市场错配）
            res["coverage"] = None
            res["max_gap"] = None
            off = [d for d in dates if d not in cal_idx]
            weekend_off = [d for d in off if _is_weekend(d)]
            res["calendar_off"] = len(off)
            if weekend_off:
                res["issues"].append({
                    "level": "FAIL", "kind": "calendar_off",
                    "detail": "%d 个非交易日日期（其中周末 %d 个：%s），为伪 K 线，必须剔除"
                              % (len(off), len(weekend_off), weekend_off[:5]),
                })
                res["fails"] += 1
            else:
                res["issues"].append({
                    "level": "WARN", "kind": "calendar_gap_early",
                    "detail": "含 %d 个日历外日期（%s ~ %s），该年样本稀疏、日历无法交叉验证，非数据缺陷"
                              % (len(off), off[0], off[-1]),
                })
                res["warns"] += 1
    else:
        # 港股/美股：以自身序列为准，只做周末检查
        res["coverage"] = None
        res["max_gap"] = None
        off = [d for d in dates if _is_weekend(d)]
        if off:
            res["issues"].append({"level": "FAIL", "kind": "calendar_off",
                                  "detail": "%d 条周末伪 K 线（%s）" % (len(off), off[:5])})
            res["fails"] += 1

    # --- 准确性 ---
    dup = len(dates) - len(set(dates))
    res["dup"] = dup
    if dup:
        res["issues"].append({"level": "FAIL", "kind": "dup_date", "detail": "重复日期 %d 条" % dup})
        res["fails"] += 1

    ohlc_bad = 0
    nonpos = 0
    zero_vol = 0
    zero_amt = 0
    for b in bars:
        tol = max(1e-9, 2e-5 * max(abs(b["open"]), abs(b["high"]), abs(b["low"]), b["close"]))
        if b["high"] + tol < max(b["open"], b["close"]) or b["low"] - tol > min(b["open"], b["close"]):
            ohlc_bad += 1
        if min(b["open"], b["high"], b["low"], b["close"]) <= 0:
            nonpos += 1
        if b["vol"] <= 0:
            zero_vol += 1
        if b["amount"] <= 0:
            zero_amt += 1
    res["ohlc_bad"] = ohlc_bad
    res["ohlc_bad_rate"] = round(ohlc_bad / len(bars), 5)
    res["zero_vol_rate"] = round(zero_vol / len(bars), 4)
    res["zero_amt_rate"] = round(zero_amt / len(bars), 4)
    # 量能字段：vol 缺失 → 量能因子直接失效（FAIL）；amount 缺失 → 回测可跑但无法做
    # 成交额加权/冲击成本/流动性分层（WARN，列入升级清单）
    if res["zero_vol_rate"] >= 0.99:
        res["issues"].append({"level": "FAIL", "kind": "field_missing",
                              "detail": "成交量字段全空，vol_z20/er20 等量能因子无法计算"})
        res["fails"] += 1
    elif res["zero_vol_rate"] > 0.01:
        res["issues"].append({"level": "WARN", "kind": "field_missing",
                              "detail": "%.1f%% 的交易日成交量为 0" % (res["zero_vol_rate"] * 100)})
        res["warns"] += 1
    if res["zero_amt_rate"] >= 0.99:
        res["issues"].append({"level": "WARN", "kind": "field_missing",
                              "detail": "成交额字段全空（当前引擎未使用；做流动性/冲击成本时必须补齐）"})
        res["warns"] += 1
    elif res["zero_amt_rate"] > 0.01:
        res["issues"].append({"level": "WARN", "kind": "field_missing",
                              "detail": "%.1f%% 的交易日成交额为 0" % (res["zero_amt_rate"] * 100)})
        res["warns"] += 1
    if nonpos:
        res["issues"].append({"level": "FAIL", "kind": "nonpos_price", "detail": "非正价格 %d 条" % nonpos})
        res["fails"] += 1
    if res["ohlc_bad_rate"] > TH["ohlc_bad_rate_fail"]:
        res["issues"].append({"level": "FAIL", "kind": "ohlc_logic",
                              "detail": "OHLC 逻辑违规 %d 条（%.2f%%）" % (ohlc_bad, res["ohlc_bad_rate"] * 100)})
        res["fails"] += 1
    elif ohlc_bad:
        res["issues"].append({"level": "WARN", "kind": "ohlc_logic",
                              "detail": "OHLC 逻辑违规 %d 条（舍入级）" % ohlc_bad})
        res["warns"] += 1

    # 收益跳变——必须做横截面共因分解，否则 2024-09-30 / 2024-10-08 / 2025-04-07
    # 这类真实政策与关税冲击会被误判成"数据错误"，进而触发灾难性的"修复"。
    # 判据：同一日 >= EVENT_MIN_CODES 只标的同时出现超阈收益 → 市场级事件，不计异常。
    outliers: List[Tuple[str, float]] = []
    event_hits: List[Tuple[str, float]] = []
    sector_hits: List[Tuple[str, float]] = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        if prev > 0:
            r = cur / prev - 1.0
            if abs(r) > TH["ret_outlier"]:
                d = bars[i]["date"]
                if event_days.get(d, 0) >= TH["event_min_codes"]:
                    event_hits.append((d, round(r, 4)))
                elif soft_days.get(d, 0) >= TH["sector_min_codes"]:
                    # 同日另有 >=2 只标的涨幅/跌幅超 10%——板块级行情，非数据错误。
                    # 950125 的 6 次"孤立"全部属于此类：2026-07-21 半导体全线 +10~15%，
                    # 只是同族标的没越过 15% 这条主阈值，被误判为孤立。
                    sector_hits.append((d, round(r, 4)))
                else:
                    outliers.append((d, round(r, 4)))
    res["ret_outliers"] = len(outliers)
    res["ret_event_hits"] = len(event_hits)
    res["ret_sector_hits"] = len(sector_hits)
    res["ret_outlier_top"] = sorted(outliers, key=lambda x: -abs(x[1]))[:5]
    res["ret_event_top"] = sorted(event_hits + sector_hits, key=lambda x: -abs(x[1]))[:5]
    if len(outliers) > TH["ret_outlier_fail"]:
        res["issues"].append({"level": "FAIL", "kind": "ret_jump",
                              "detail": "%d 次真正孤立的单日 |收益|>%.0f%%（同日无任何共因，疑数据错误）"
                                        % (len(outliers), TH["ret_outlier"] * 100)})
        res["fails"] += 1
    elif outliers:
        res["issues"].append({"level": "WARN", "kind": "ret_jump",
                              "detail": "%d 次孤立单日 |收益|>%.0f%%（需人工确认）"
                                        % (len(outliers), TH["ret_outlier"] * 100)})
        res["warns"] += 1
    if event_hits:
        res["issues"].append({"level": "INFO", "kind": "market_event",
                              "detail": "%d 次极端收益属全市场共因（同日 >=%d 只标的同步），非数据缺陷"
                                        % (len(event_hits), TH["event_min_codes"])})
    if sector_hits:
        res["issues"].append({"level": "INFO", "kind": "sector_event",
                              "detail": "%d 次极端收益属板块级共因（同日 >=%d 只标的 |收益|>10%%），非数据缺陷"
                                        % (len(sector_hits), TH["sector_min_codes"])})

    # --- 度量统一：量纲判定 ---
    ratios = [b["amount"] / b["vol"] for b in bars if b["vol"] > 0 and b["amount"] > 0]
    ratios.sort()
    def _pct(xs: List[float], p: float) -> float:
        if not xs:
            return 0.0
        k = max(0, min(len(xs) - 1, int(round((len(xs) - 1) * p))))
        return xs[k]
    r50 = _pct(ratios, 0.5)
    res["amt_vol_ratio"] = r50
    if not ratios:
        res["unit"] = "NO_VOL"
        res["issues"].append({"level": "INFO", "kind": "unit",
                              "detail": "无成交额可校验量纲（amount 缺失），由 field_missing 单独记账"})
    elif r50 < TH["unit_ratio_min"]:
        res["unit"] = "SHARES_YI"   # 股 + 亿元，需要归一
        res["issues"].append({"level": "FAIL", "kind": "unit",
                              "detail": "量纲为「股+亿元」(amt/vol=%.2e)，与「手+元」不一致" % r50})
        res["fails"] += 1
    elif r50 < 1e5:
        res["unit"] = "LOT_YUAN"    # 手 + 元，标准口径
    else:
        res["unit"] = "SUSPECT"
        res["issues"].append({"level": "WARN", "kind": "unit",
                              "detail": "amt/vol=%.2e，高于常规区间，需人工确认" % r50})
        res["warns"] += 1

    # 按年断层扫描：同一序列内量纲漂移（拼接多源时最常见）
    by_year: Dict[str, List[float]] = defaultdict(list)
    for b in bars:
        if b["vol"] > 0 and b["amount"] > 0:
            by_year[b["date"][:4]].append(b["amount"] / b["vol"])
    year_med = {y: _pct(sorted(v), 0.5) for y, v in by_year.items() if len(v) >= 20}
    years = sorted(year_med)
    breaks = []
    for a, b2 in zip(years, years[1:]):
        if year_med[a] > 0:
            k = year_med[b2] / year_med[a]
            if k < TH["unit_break_lo"] or k > TH["unit_break_hi"]:
                breaks.append({"from": a, "to": b2, "ratio": round(k, 4)})
    res["unit_years"] = {y: round(v, 2) for y, v in year_med.items()}
    res["unit_breaks"] = breaks
    if breaks:
        res["issues"].append({"level": "FAIL", "kind": "unit_break",
                              "detail": "量纲按年断层 %d 处：%s" % (len(breaks), breaks[:3])})
        res["fails"] += 1

    # --- 末端新鲜度 ---
    stale = None
    if last_cal_date and mkt == "CN":
        hi = cal_idx.get(last_cal_date)
        li = cal_idx.get(dates[-1])
        if hi is not None and li is not None:
            stale = hi - li
            res["stale_days"] = stale
            if stale > TH["stale_fail"]:
                res["issues"].append({"level": "FAIL", "kind": "stale",
                                      "detail": "末端落后最新交易日 %d 个交易日" % stale})
                res["fails"] += 1
            elif stale > TH["stale_warn"]:
                res["issues"].append({"level": "WARN", "kind": "stale",
                                      "detail": "末端落后 %d 个交易日" % stale})
                res["warns"] += 1

    # --- 完整性格级 ---
    cov = res.get("coverage")
    if cov is not None and res["market"] == "CN_HK":
        # 港股通：缺口来自两边休市不重叠，属口径事实而非缺陷。仅披露，不判级。
        res["issues"].append({"level": "INFO", "kind": "calendar_mismatch",
                              "detail": "港股通交易日比 A 股少 %d 日（%.2f%%，两边休市不重叠），做跨市场对齐时需前值填充"
                                        % (res.get("calendar_mismatch_days", 0), (1 - cov) * 100)})
        cov = None
    if cov is not None:
        if cov < TH["coverage_fail"]:
            res["issues"].append({"level": "FAIL", "kind": "coverage",
                                  "detail": "覆盖率 %.1f%%（缺 %d 日）" % (cov * 100, res.get("missing", 0))})
            res["fails"] += 1
        elif cov < TH["coverage_warn"]:
            res["issues"].append({"level": "WARN", "kind": "coverage",
                                  "detail": "覆盖率 %.1f%%（缺 %d 日）" % (cov * 100, res.get("missing", 0))})
            res["warns"] += 1
        mg = res.get("max_gap") or 0
        if mg > TH["max_gap_fail"]:
            res["issues"].append({"level": "FAIL", "kind": "gap",
                                  "detail": "最长连续缺口 %d 个交易日" % mg})
            res["fails"] += 1
        elif mg > TH["max_gap_warn"]:
            res["issues"].append({"level": "WARN", "kind": "gap",
                                  "detail": "最长连续缺口 %d 个交易日" % mg})
            res["warns"] += 1

    if len(bars) < TH["min_bars_fail"]:
        res["issues"].append({"level": "FAIL", "kind": "too_short",
                              "detail": "仅 %d 根，回测不可用" % len(bars)})
        res["fails"] += 1
    elif len(bars) < TH["min_bars_warn"]:
        res["issues"].append({"level": "WARN", "kind": "too_short",
                              "detail": "仅 %d 根，不足一年" % len(bars)})
        res["warns"] += 1

    res["status"] = "FAIL" if res["fails"] else ("WARN" if res["warns"] else "OK")
    return res


# ----------------------------------------------------------------------------
# 汇总与字段矩阵
# ----------------------------------------------------------------------------
REQUIRED_FIELDS = {
    # 回测最小可用集（当前已具备）
    "date": "交易日，ISO 格式",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘（指数为点位，无需复权）",
    "vol": "成交量（手）",
    "amount": "成交额（元）",
}

UPGRADE_FIELDS = {
    "turnover_rate": ("换手率", "拥挤度/流动性因子，state_fit 与 stock-pool 筛选的核心缺失项"),
    "free_float_mcap": ("自由流通市值", "容量约束：ETF 跟踪容量与冲击成本估算"),
    "pe_ttm": ("估值 PE-TTM", "估值分位择时；缺失导致所有信号只能纯价格驱动"),
    "pb": ("市净率", "与 PE 交叉验证，避免单因子失真"),
    "dividend_yield": ("股息率", "红利类指数的收益来源分解，当前无法归因"),
    "constituent_count": ("成分股数量", "集中度风险；决定单票冲击成本"),
    "top10_weight": ("前十大权重合计", "集中度；theme 桶尤需"),
    "index_nav": ("指数全收益/净收益版本", "分红再投资口径；价格指数低估长期收益 2-3%/年"),
    "etf_nav": ("ETF 单位净值", "折溢价与跟踪误差，映射质量评估"),
    "etf_premium": ("ETF 折溢价率", "执行层最优 ETF 选择的直接依据"),
    "risk_free": ("无风险利率", "夏普/超额收益基准，当前用常数替代"),
    "constituent_change": ("成分股调整记录", "幸存者偏差审计；指数化回测的隐性偏差源"),
    "adj_factor": ("复权因子", "仅 ETF/个股需要；指数不适用"),
}


def summarize(rows: List[Dict[str, Any]], cal: Dict[str, List[str]]) -> Dict[str, Any]:
    ok = [r for r in rows if r["status"] == "OK"]
    warn = [r for r in rows if r["status"] == "WARN"]
    fail = [r for r in rows if r["status"] == "FAIL"]
    miss = [r for r in rows if r["status"] == "MISSING"]
    covs = sorted(r["coverage"] for r in rows if isinstance(r.get("coverage"), (int, float)))
    def q(p: float) -> Optional[float]:
        if not covs:
            return None
        k = max(0, min(len(covs) - 1, int(round((len(covs) - 1) * p))))
        return round(covs[k], 4)
    units: Dict[str, int] = defaultdict(int)
    for r in rows:
        units[r.get("unit", "NA")] += 1
    kind_cnt: Dict[str, int] = defaultdict(int)
    for r in rows:
        for it in r["issues"]:
            if it["level"] == "FAIL":
                kind_cnt[it["kind"]] += 1
    return {
        "n_universe": len(rows),
        "n_ok": len(ok), "n_warn": len(warn), "n_fail": len(fail), "n_missing": len(miss),
        "total_bars": sum(r["bars"] for r in rows),
        "calendar_cn_days": len(cal.get("CN", [])),
        "calendar_cn_range": [cal["CN"][0], cal["CN"][-1]] if cal.get("CN") else None,
        "coverage_p10": q(0.10), "coverage_p50": q(0.50), "coverage_p90": q(0.90),
        "coverage_min": covs[0] if covs else None,
        "earliest": min((r["first"] for r in rows if r.get("first")), default=None),
        "latest": max((r["last"] for r in rows if r.get("last")), default=None),
        "units": dict(units),
        "fail_kinds": dict(kind_cnt),
        "fail_codes": [{"code": r["code"], "name": r["name"], "issues": [i["kind"] for i in r["issues"] if i["level"] == "FAIL"]} for r in fail + miss],
        "warn_codes": [{"code": r["code"], "name": r["name"], "issues": [i["kind"] for i in r["issues"] if i["level"] == "WARN"]} for r in warn],
    }


def backtest_readiness(rows: List[Dict[str, Any]], cal: Dict[str, List[str]]) -> Dict[str, Any]:
    """
    回测可用性分层——不是"有多少只"，而是"多少只可以进哪一个回测口径"。

    L0 不可用：缺失/严重缺口
    L1 短窗可用（>=240 根）：可用于 60 日特征 + 20 日前向收益
    L2 长窗可用（>=1000 根，约 4 年）：可用于跨周期状态机校准
    L3 六年完整（>=1400 根）：可用于全样本 6 年实证
    """
    layers = {"L0": [], "L1": [], "L2": [], "L3": []}
    for r in rows:
        n = r["bars"]
        if n < TH["min_bars_warn"]:
            layers["L0"].append(r["code"])
        elif n < 1000:
            layers["L1"].append(r["code"])
        elif n < 1400:
            layers["L2"].append(r["code"])
        else:
            layers["L3"].append(r["code"])
    return {k: {"n": len(v), "codes": v[:12]} for k, v in layers.items()}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "splus_config.yaml"))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--only", default=None, help="单标的调试")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--out-prefix", default="db_scorecard")
    args = ap.parse_args()

    import yaml
    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cache = args.cache or os.path.join(HERE, cfg["data"]["cache_dir"])
    cache = os.path.normpath(cache)
    out_dir = os.path.join(HERE, cfg["data"].get("output_dir", "data"))

    t0 = time.time()
    universe = load_universe(args.config)
    if args.only:
        universe = [u for u in universe if u["code"] == args.only]
    markets = {u["code"]: market_of(u["code"], u.get("name", "")) for u in universe}

    raw: Dict[str, List[Dict[str, Any]]] = {}
    for u in universe:
        raw[u["code"]] = load_bars(cache, u["code"])

    all_dates = {c: {b["date"] for b in bars} for c, bars in raw.items()}
    spans = {c: (bars[0]["date"], bars[-1]["date"]) for c, bars in raw.items() if bars}
    cal = build_calendar(all_dates, markets, spans)

    # 横截面共因日：先全量扫一遍，供单标的判定时区分"市场事件"与"数据错误"
    event_days: Dict[str, int] = defaultdict(int)
    soft_days: Dict[str, int] = defaultdict(int)
    for bars in raw.values():
        for i in range(1, len(bars)):
            p, c2 = bars[i - 1]["close"], bars[i]["close"]
            if p > 0:
                r = abs(c2 / p - 1.0)
                if r > TH["ret_outlier"]:
                    event_days[bars[i]["date"]] += 1
                if r > TH["sector_ret"]:
                    soft_days[bars[i]["date"]] += 1
    market_events = {d: k for d, k in event_days.items() if k >= TH["event_min_codes"]}

    cal_idx = {d: i for i, d in enumerate(cal.get("CN", []))}
    last_cal_date = cal["CN"][-1] if cal.get("CN") else None

    rows = []
    for u in universe:
        rows.append(inspect(u["code"], u, raw[u["code"]], cal.get("CN", []), cal_idx,
                            last_cal_date, market_events, soft_days))

    # 日历卫生：伪 K 线集中在周末 = 日历污染
    weekend = weekday_share(cal.get("CN", []))

    summary = summarize(rows, cal)
    readiness = backtest_readiness(rows, cal)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cache_dir": os.path.relpath(cache, HERE),
        "thresholds": TH,
        "calendar": {
            "cn_days": len(cal.get("CN", [])),
            "cn_range": [cal["CN"][0], cal["CN"][-1]] if cal.get("CN") else None,
            "cn_weekend_share": round(weekend, 6),
            "hk_days": len(cal.get("HK", [])),
            "us_days": len(cal.get("US", [])),
            "active_by_year": cal.get("_active_by_year", {}),
            "market_events": dict(sorted(market_events.items(), key=lambda kv: -kv[1])[:10]),
        },
        "summary": summary,
        "readiness": readiness,
        "fields": {"present": sorted(REQUIRED_FIELDS), "missing_upgrade": sorted(UPGRADE_FIELDS)},
        "fields_detail": UPGRADE_FIELDS,
        "per_code": rows,
        "elapsed_s": round(time.time() - t0, 2),
    }

    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "%s.json" % args.out_prefix)
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print("[scorecard] n=%d OK=%d WARN=%d FAIL=%d MISSING=%d bars=%d cal=%d"
          % (summary["n_universe"], summary["n_ok"], summary["n_warn"],
             summary["n_fail"], summary["n_missing"], summary["total_bars"], summary["calendar_cn_days"]))
    print("[scorecard] coverage p10/p50/p90 = %s / %s / %s"
          % (summary["coverage_p10"], summary["coverage_p50"], summary["coverage_p90"]))
    print("[scorecard] units=%s fail_kinds=%s" % (summary["units"], summary["fail_kinds"]))
    print("[scorecard] readiness=%s" % {k: v["n"] for k, v in readiness.items()})
    print("[scorecard] -> %s (%.1fs)" % (jp, payload["elapsed_s"]))

    if not args.no_html:
        try:
            import render_db_scorecard as rdb  # 可选依赖
            hp = os.path.join(out_dir, "%s.html" % args.out_prefix)
            rdb.render(payload, hp)
            print("[scorecard] -> %s" % hp)
        except Exception as exc:  # 渲染失败不阻断机读结果
            print("[scorecard] html skipped: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
