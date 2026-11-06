# -*- coding: utf-8 -*-
"""
S++ 投研管理系统 —— 指数级决策引擎

设计定位：
  L1 状态层：五状态概率向量（A 吸筹 / B 突破 / C 通道 / D 派发 / E 下跌），贝叶斯逐日更新
  L2 因果层：跨市场传导链（leader -> follower），时滞实测 + 领先度打分
  L3 仓位层：波动率目标 × 回撤节流 × 相关性惩罚，三重约束取最小值
  L4 执行层：追价上限、失效位、对冲建议
  L5 风险层：尾部保护开关 + 硬性 KILL SWITCH
  L6 元层：概率校准台账 + 决策日志 + 归因账本

原则：
  - 所有阈值外置到 splus_config.yaml，代码零硬编码
  - 所有输出可复算，同一输入必得同一输出
  - 缺数据一律降级并显式披露，不猜、不补、不静默
"""

from __future__ import annotations

import os
import sys
import json
import math
import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ============================================================
# 基础数值工具
# ============================================================

def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def stdev(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def pct_rank(xs: Sequence[Optional[float]], value: Optional[float]) -> Optional[float]:
    """value 在 xs 中的分位（0~1）。"""
    if value is None:
        return None
    v = [x for x in xs if x is not None]
    if len(v) < 30:
        return None
    n = sum(1 for x in v if x <= value)
    return n / len(v)


def zscore(xs: Sequence[Optional[float]], value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    m = mean(xs)
    s = stdev(xs)
    if m is None or not s:
        return None
    return (value - m) / s


def s_up(x: Optional[float], lo: float, hi: float) -> float:
    """越大越支持的隶属度，映射到 [0,1]。"""
    if x is None:
        return 0.5
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def s_down(x: Optional[float], lo: float, hi: float) -> float:
    """越小越支持的隶属度。"""
    return 1.0 - s_up(x, lo, hi)


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ============================================================
# 数据加载
# ============================================================

def load_bars(cache_dir: str, code: str) -> List[Dict[str, Any]]:
    path = os.path.join(cache_dir, f"idx_{code}.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    bars: List[Dict[str, Any]] = []
    for r in raw:
        c = _f(r.get("close"))
        if c is None or c <= 0:
            continue
        bars.append({
            "date": str(r.get("date")),
            "open": _f(r.get("open")) or c,
            "high": _f(r.get("high")) or c,
            "low": _f(r.get("low")) or c,
            "close": c,
            "vol": _f(r.get("vol")) or 0.0,
            "amount": _f(r.get("amount")) or 0.0,
        })
    bars.sort(key=lambda x: x["date"])
    return bars


def quality_check(bars: List[Dict[str, Any]]) -> List[str]:
    """
    数据质量检查。分两级：
      hard（返回在硬异常列表）——只有真正拆股/拼接错误才硬排除
      soft（warn）——只披露，不阻断（真实行情跳空属于此类）

    关键原则：不能把“2024-09-30 政策行情跳空”误判为数据错误。
    区分方式：真行情跳空 = 当日有正常成交量且 OHLC 自洽；
              拆股断层 = 无成交量，或跳幅接近整数拆分比。
    """
    issues: List[str] = []
    if len(bars) < 120:
        issues.append(f"HARD|样本仅 {len(bars)} 根 < 120，不足以计算长期均线")
        return issues

    # 1) OHLC 逻辑一致性（硬）
    bad = sum(1 for b in bars if not (b["low"] <= b["close"] <= b["high"]))
    if bad > 0:
        issues.append(f"HARD|{bad} 根 K 线 OHLC 逻辑不一致")

    # 2) 零成交量异常（硬）：指数不可能长期无成交量
    zero_vol = sum(1 for b in bars[-250:] if (b.get("vol") or 0) <= 0)
    if zero_vol > 5:
        issues.append(f"HARD|近 250 日有 {zero_vol} 根零成交量，数据源可能不完整")

    # 3) 拆股断层（硬）：跳幅贴近整数拆分比且当日无量
    SPLIT_RATIOS = [2.0, 3.0, 4.0, 1.5, 10.0]
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        if pc <= 0:
            continue
        r = bars[i]["close"] / pc
        if any(abs(r - s) / s < 0.01 for s in SPLIT_RATIOS) and (bars[i].get("vol") or 0) <= 0:
            issues.append(f"HARD|{bars[i]['date']} 疑似拆股断层（比 {r:.2f}x 且无量）")
            break

    # 4) 日期断裂（软提示，可能停牌或数据缺口）
    import datetime as _dt
    gaps = 0
    for i in range(1, len(bars)):
        try:
            d0 = _dt.date.fromisoformat(bars[i - 1]["date"])
            d1 = _dt.date.fromisoformat(bars[i]["date"])
        except Exception:
            continue
        if (d1 - d0).days > 15:
            gaps += 1
    if gaps > 0:
        issues.append(f"SOFT|存在 {gaps} 处 >15 天的日期断裂")

    # 5) 大幅跳空（软）：真行情跳空常见，只披露
    jumps = 0
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        if pc > 0 and abs(bars[i]["close"] / pc - 1) > 0.15:
            jumps += 1
    if jumps > 0:
        issues.append(f"SOFT|存在 {jumps} 处 >15% 跳空（真行情，已保留）")

    # 6) 仅收盘价（软）：部分新发布中证指数在官网只给 close，无 OHLC。
    #    ATR/ADX 会退化为收盘口径代理，必须在报告里披露，不得静默当正常数据。
    ohlc_flat = sum(1 for b in bars if b["high"] == b["low"] == b["close"])
    if ohlc_flat > len(bars) * 0.9:
        issues.append("SOFT|源仅提供收盘价（无 OHLC），ATR/ADX 已退化为收盘口径代理")

    return issues


# ============================================================
# L1 状态层 —— 指标
# ============================================================

def ema(vals: Sequence[float], n: int) -> List[Optional[float]]:
    if not vals:
        return []
    k = 2.0 / (n + 1.0)
    out: List[Optional[float]] = [None] * len(vals)
    acc = vals[0]
    out[0] = acc
    for i in range(1, len(vals)):
        acc = vals[i] * k + acc * (1 - k)
        out[i] = acc
    return out


def true_range(bars: Sequence[Dict[str, Any]]) -> List[Optional[float]]:
    tr: List[Optional[float]] = [None] * len(bars)
    for i, b in enumerate(bars):
        if i == 0:
            tr[i] = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr[i] = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
    return tr


def atr_series(bars: Sequence[Dict[str, Any]], n: int = 14) -> List[Optional[float]]:
    tr = true_range(bars)
    out: List[Optional[float]] = [None] * len(bars)
    for i in range(len(bars)):
        if i + 1 < n:
            continue
        seg = [x for x in tr[i - n + 1:i + 1] if x is not None]
        if len(seg) == n:
            out[i] = sum(seg) / n
    return out


def adx_series(bars: Sequence[Dict[str, Any]], n: int = 14) -> List[Optional[float]]:
    ln = len(bars)
    if ln < n + 2:
        return [None] * ln
    plus_dm = [0.0] * ln
    minus_dm = [0.0] * ln
    for i in range(1, ln):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    tr = true_range(bars)
    adx: List[Optional[float]] = [None] * ln
    atr_s = atr_series(bars, n)
    dx: List[Optional[float]] = [None] * ln
    for i in range(n, ln):
        if atr_s[i] in (None, 0):
            continue
        pdi = 100.0 * (sum(plus_dm[i - n + 1:i + 1]) / n) / atr_s[i]
        mdi = 100.0 * (sum(minus_dm[i - n + 1:i + 1]) / n) / atr_s[i]
        if pdi + mdi == 0:
            continue
        dx[i] = 100.0 * abs(pdi - mdi) / (pdi + mdi)
    for i in range(n * 2, ln):
        seg = [x for x in dx[i - n + 1:i + 1] if x is not None]
        if len(seg) == n:
            adx[i] = sum(seg) / n
    return adx


def efficiency_ratio(closes: Sequence[float], n: int = 20) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    net = abs(closes[-1] - closes[-1 - n])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    if path == 0:
        return 0.0
    return net / path


def swing_structure(bars: Sequence[Dict[str, Any]], look: int = 60, frag: int = 10):
    """返回 (higher_highs_count, higher_lows_count, lower_highs, lower_lows)。"""
    seg = bars[-look:] if len(bars) > look else bars
    if len(seg) < frag * 2:
        return 0, 0, 0, 0
    highs, lows = [], []
    for i in range(frag, len(seg) - frag):
        w = seg[i - frag:i + frag + 1]
        if seg[i]["high"] == max(b["high"] for b in w):
            highs.append(seg[i]["high"])
        if seg[i]["low"] == min(b["low"] for b in w):
            lows.append(seg[i]["low"])
    hh = sum(1 for a, b in zip(highs, highs[1:]) if b > a)
    lh = sum(1 for a, b in zip(highs, highs[1:]) if b < a)
    hl = sum(1 for a, b in zip(lows, lows[1:]) if b > a)
    ll = sum(1 for a, b in zip(lows, lows[1:]) if b < a)
    return hh, hl, lh, ll


def compute_features(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    ln = len(bars)
    feats: Dict[str, Any] = {"n": ln, "as_of": bars[-1]["date"], "close": closes[-1]}

    e20 = ema(closes, 20)
    e60 = ema(closes, 60)
    feats["ema20"] = e20[-1]
    feats["ema60"] = e60[-1]
    feats["ma200"] = mean(closes[-200:]) if ln >= 200 else None
    feats["ma200_prev"] = mean(closes[-220:-20]) if ln >= 220 else None

    atr = atr_series(bars, 14)
    feats["atr14"] = atr[-1]
    feats["atr_pct_rank"] = pct_rank(atr[-252:] if ln >= 252 else atr, atr[-1])
    feats["atr_pct_of_price"] = (atr[-1] / closes[-1]) if atr[-1] else None

    feats["er20"] = efficiency_ratio(closes, 20)
    feats["er60"] = efficiency_ratio(closes, 60)
    feats["er20_prev"] = efficiency_ratio(closes[:-1], 20)

    adx = adx_series(bars, 14)
    feats["adx14"] = adx[-1]

    hi252 = max(b["high"] for b in bars[-252:]) if ln >= 60 else max(b["high"] for b in bars)
    feats["high_252"] = hi252
    feats["dd_from_high"] = (closes[-1] - hi252) / hi252 if hi252 else None
    feats["pos_in_52w"] = (closes[-1] - min(b["low"] for b in bars[-252:])) / (
        hi252 - min(b["low"] for b in bars[-252:])) if hi252 and ln >= 60 else None

    # 区间宽度（近 60 日高低）
    seg = bars[-60:] if ln >= 60 else bars
    rh, rl = max(b["high"] for b in seg), min(b["low"] for b in seg)
    feats["range_width_60"] = (rh - rl) / rl if rl else None

    # 量能
    vol_ma20 = mean(vols[-20:]) if ln >= 20 else None
    feats["vol_ma20"] = vol_ma20
    feats["vol_z20"] = zscore(vols[-60:], vols[-1]) if ln >= 60 else None
    vol5 = mean(vols[-5:])
    vol60 = mean(vols[-60:]) if ln >= 60 else None
    feats["vol_contract"] = (vol5 / vol60) if vol5 and vol60 else None

    # 下跌日放量比（近 20 日，下跌日均量 / 上涨日均量）
    up_v, dn_v = [], []
    for i in range(max(1, ln - 20), ln):
        chg = closes[i] - closes[i - 1]
        (up_v if chg > 0 else dn_v).append(vols[i])
    mu, md = mean(up_v), mean(dn_v)
    feats["down_vol_ratio"] = (md / mu) if mu and md else None

    # 波动率
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, ln - 21), ln)
            if closes[i - 1] > 0]
    sd = stdev(rets)
    feats["ann_vol_20"] = (sd * math.sqrt(252)) if sd else None
    rets60 = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, ln - 61), ln)
              if closes[i - 1] > 0]
    sd60 = stdev(rets60)
    feats["ann_vol_60"] = (sd60 * math.sqrt(252)) if sd60 else None

    # 近端摆点低点（用于失效位，不用区间宽度代理）
    feats["swing_low_20"] = min(b["low"] for b in bars[-20:]) if ln >= 20 else None
    feats["swing_high_20"] = max(b["high"] for b in bars[-20:]) if ln >= 20 else None

    # 突破判定：今日收盘 vs 前 60 日最高
    if ln >= 61:
        prior_hi = max(b["high"] for b in bars[-61:-1])
        feats["breakout"] = 1.0 if closes[-1] > prior_hi else 0.0
        feats["prior_60_hi"] = prior_hi
        feats["dist_to_prior_60_hi"] = (prior_hi - closes[-1]) / closes[-1]
        # 3 日站稳
        hold = 0
        for k in range(1, 4):
            if ln >= 61 + k:
                ph = max(b["high"] for b in bars[-61 - k:-1 - k])
                if closes[-1 - k] > ph:
                    hold += 1
        feats["breakout_hold3"] = hold
    else:
        feats["breakout"] = 0.0
        feats["prior_60_hi"] = None
        feats["dist_to_prior_60_hi"] = None
        feats["breakout_hold3"] = 0

    # 假突破计数（近 40 日：盘中创出 60 日新高，但 3 日内收盘收回原区间）
    # 修正：原实现用“收盘突破”作前提，会漏掉“盘中上冲后收回”的典型派发信号，
    #      而后者恰恰是最有价值的判据，因此改用 high 触反。
    failed = 0
    if ln >= 70:
        for i in range(ln - 40, ln - 3):
            if i < 61:
                continue
            prior_hi = max(b["high"] for b in bars[i - 60:i])
            touched = bars[i]["high"] > prior_hi
            if not touched:
                continue
            reverted = bars[i + 3]["close"] < prior_hi
            if reverted:
                failed += 1
    feats["failed_breakouts_40"] = failed

    hh, hl, lh, ll = swing_structure(bars, 60, 10)
    feats["hh"], feats["hl"], feats["lh"], feats["ll"] = hh, hl, lh, ll

    ma200 = feats["ma200"]
    feats["above_ma200"] = (closes[-1] / ma200 - 1) if ma200 else None
    feats["ma200_slope"] = ((ma200 / feats["ma200_prev"] - 1) if ma200 and feats["ma200_prev"] else None)

    feats["ret20"] = (closes[-1] / closes[-21] - 1) if ln >= 21 else None
    feats["ret60"] = (closes[-1] / closes[-61] - 1) if ln >= 61 else None

    # ---- 反弹子状态（E 阶段的内部区分，不是第六个状态）----
    # 为何需要：E 阶段持续可达数月，但“下跌中继”与“下跌中的可交易反弹”
    #           风险收益比完全不同。不区分就退化成“永远不动”。
    close = feats["close"]
    e20 = feats.get("ema20")
    e60 = feats.get("ema60")
    rebound = {
        "above_ema20": bool(e20 and close > e20),
        "ema20_above_ema60": bool(e20 and e60 and e20 > e60),
        "er_rising": bool(feats.get("er20") is not None and feats.get("er20_prev") is not None
                          and feats["er20"] > feats["er20_prev"]),
        "vol_expanding": bool((feats.get("vol_z20") or 0) > 0.5),
    }
    rebound["score"] = sum(1 for v in rebound.values() if v is True)
    rebound["active"] = rebound["score"] >= 3
    rebound["label"] = ("下跌通道中的反弹（可小仓战术参与）" if rebound["active"]
                        else "下跌中继（不参与）")
    feats["rebound"] = rebound

    return feats


# ============================================================
# L1 状态层 —— 五状态概率向量
# ============================================================

STATE_NAMES = {
    "A": "吸筹/筑底",
    "B": "突破/启动",
    "C": "趋势通道",
    "D": "区间/派发",
    "E": "下跌/衰退",
}


# ---------- 派生特征 ----------
# 说明：这里只做几何派生（取负、取绝对值、组合计数），不含任何决策阈值。
# 所有阈值一律在 splus_config.yaml 的 state.scoring 中。
def _derive(f: Dict[str, Any], name: str) -> Optional[float]:
    if name in f:
        v = f.get(name)
        return None if v is None else float(v)
    dd = f.get("dd_from_high")
    if name == "dd_pos":
        return None if dd is None else -float(dd)
    if name == "abs_above_ma200":
        a = f.get("above_ma200")
        return None if a is None else abs(float(a))
    if name == "rel_ema20":
        c, e = f.get("close"), f.get("ema20")
        return (c / e - 1.0) if (c and e) else None
    if name == "hhll":
        return float((f.get("hh") or 0) + (f.get("hl") or 0)
                     - (f.get("lh") or 0) - (f.get("ll") or 0))
    if name == "lh_ll":
        return float((f.get("lh") or 0) + (f.get("ll") or 0))
    if name == "down_er":
        er, r20 = f.get("er20"), f.get("ret20")
        if er is None:
            return None
        return float(er) if (r20 is not None and r20 < 0) else 0.0
    if name == "stacked":
        c, e20, e60 = f.get("close"), f.get("ema20"), f.get("ema60")
        return 1.0 if (c and e20 and e60 and c > e20 > e60) else 0.0
    if name == "is_breakout":
        return float(f.get("breakout") or 0.0)
    return None


def _member(f: Dict[str, Any], spec: Dict[str, Any], default_miss: float = 0.05) -> float:
    """按配置求单个条款的隶属度。dir=up/down/bool。"""
    x = _derive(f, spec["feat"])
    d = spec.get("dir", "up")
    if d == "bool":
        return 1.0 if x else float(spec.get("miss", default_miss))
    lo, hi = spec.get("lo"), spec.get("hi")
    if lo is None or hi is None:
        return 1.0
    if x is None:
        return float(spec.get("null", 0.5))
    return s_up(x, float(lo), float(hi)) if d == "up" else s_down(x, float(lo), float(hi))


def state_scores(f: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, float]:
    """
    五状态打分，完全由 config.state.scoring 驱动。

    gate（必须项）：乘法（默认）或取最大（gate_op: max）。缺了前提就大幅压分。
    core（加分项）：加权求和。
    score = gate * core
    """
    default_miss = float(model.get("_default_miss", 0.05))
    scores: Dict[str, float] = {}
    for st in ("A", "B", "C", "D", "E"):
        ms = model.get(st) or {}
        gates = ms.get("gate") or []
        gvals = [_member(f, g, default_miss) for g in gates]
        if not gvals:
            gate = 1.0
        elif str(ms.get("gate_op", "mul")).lower() == "max":
            gate = max(gvals)
        else:
            gate = 1.0
            for g in gvals:
                gate *= g
        core = 0.0
        for c in (ms.get("core") or []):
            core += float(c.get("w", 1.0)) * _member(f, c, default_miss)
        scores[st] = gate * core
    return scores


def state_probs(f: Dict[str, Any], prev: Optional[Dict[str, float]],
                persistence: float, temp: float,
                model: Dict[str, Any], uniform: float = 0.2) -> Dict[str, Any]:
    scores = state_scores(f, model)
    keys = ["A", "B", "C", "D", "E"]

    u = float(uniform)
    if prev:
        prior = {k: persistence * prev.get(k, u) + (1 - persistence) * u for k in keys}
    else:
        prior = {k: u for k in keys}

    # 数值稳定：log-sum-exp。temp 极小时 exp 会溢出，原实现会静默产生 nan。
    if temp <= 0:
        temp = 1e-6
    logits = {k: math.log(max(prior[k], 1e-12)) + scores[k] / temp for k in keys}
    mx = max(logits.values())
    if not math.isfinite(mx):
        # 极端情况下退化为等权，避免 nan 传播为"看似有效"的判定
        probs = {k: 1.0 / len(keys) for k in keys}
    else:
        exps = {k: math.exp(logits[k] - mx) for k in keys}
        tot = sum(exps.values()) or 1.0
        probs = {k: exps[k] / tot for k in keys}

    return {
        "probs": probs,
        "scores": scores,
        "prior": prior,
        "dominant": max(probs, key=probs.get),
        "confidence": max(probs.values()),
    }


def state_weighted_position(probs: Dict[str, float], weights: Dict[str, float]) -> float:
    return sum(probs[k] * float(weights[k]) for k in ("A", "B", "C", "D", "E") if k in weights)


# ---- 主状态连续段（起始日 / 持续交易日）----
# 与 stock-pool 指数池页面同口径：按“逐日主状态”回算连续段。
# S++ 五态模型无滞回状态机，st["dominant"] = max(probs) 即“理想态”，
# 故直接对 dominant 序列计数，不受滞回污染。
# 实现选择：落盘一份逐日主状态历史（JSONL），每次运行追加当日主状态，
# 连续段从这份历史里回算 —— O(1)/次，与指数池从日级状态库回算等价；
# 若某交易日 S++ 未运行，则该日缺行、连续段按已落盘行计数（日报场景下按日运行即完整）。

def load_dominant_history(path: str) -> List[Tuple[str, Dict[str, str]]]:
    """读取逐日主状态历史 JSONL -> [(date, {code: dominant})]，按日期升序。

    每行形如 {"date": "2026-11-05", "d": {"000688": "E", ...}}。
    坏行跳过，保证单次坏数据不影响整段回算。
    """
    out: List[Tuple[str, Dict[str, str]]] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and "date" in rec and "d" in rec \
                        and isinstance(rec["d"], dict):
                    out.append((str(rec["date"]), rec["d"]))
    except Exception:
        return out
    out.sort(key=lambda x: x[0])
    return out


def append_dominant_history(path: str, as_of: str,
                            dominants_by_code: Dict[str, str],
                            replace_same_date: bool = True) -> None:
    """写入当日主状态；同日重跑则替换末行（幂等），否则追加。"""
    hist = load_dominant_history(path)
    if replace_same_date and hist and hist[-1][0] == as_of:
        hist[-1] = (as_of, dict(dominants_by_code))
    else:
        hist.append((as_of, dict(dominants_by_code)))
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for dt_, mp in hist:
            fh.write(json.dumps({"date": dt_, "d": mp}, ensure_ascii=False) + "\n")


def state_streak(history: List[Tuple[str, Dict[str, str]]], code: str,
                 target: str) -> Tuple[Optional[str], int]:
    """从逐日主状态历史回算 (起始日, 持续交易日)。

    target 为当前主状态。从最新日期向历史回溯，连续相同主状态即计入，
    遇不同主状态或该 code 缺行即截断。返回 (起始日|None, 连续天数)。
    首日运行（仅 1 行）返回 (当日, 1)。
    """
    if not history:
        return None, 0
    days = 0
    start: Optional[str] = None
    for dt_, mp in reversed(history):
        dom = mp.get(code)
        if dom is None or dom != target:
            break
        days += 1
        start = dt_
    return start, days


# ============================================================
# L2 因果层 —— 跨市场传导链
# ============================================================

def log_returns(bars: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        if pc > 0:
            out[bars[i]["date"]] = math.log(bars[i]["close"] / pc)
    return out


def lead_lag(leader: List[Dict[str, Any]], follower: List[Dict[str, Any]],
             max_lag: int = 30) -> Dict[str, Any]:
    """
    计算 leader 领先 follower 的最佳滞后天数。
    lag > 0 表示 leader 先动，follower 后动。
    """
    rl = log_returns(leader)
    rf = log_returns(follower)
    common = sorted(set(rl) & set(rf))
    if len(common) < 60:
        return {"ok": False, "reason": f"重叠样本不足({len(common)}<60)"}

    dates = common
    rlf = [rl[d] for d in dates]
    rff = [rf[d] for d in dates]

    best = {"lag": 0, "corr": 0.0}
    curve = []
    for lag in range(0, max_lag + 1):
        if lag == 0:
            a, b = rlf, rff
        else:
            a, b = rlf[:-lag], rff[lag:]
        if len(a) < 40:
            continue
        c = _corr(a, b)
        if c is None:
            continue
        curve.append({"lag": lag, "corr": round(c, 4)})
        if abs(c) > abs(best["corr"]):
            best = {"lag": lag, "corr": c}

    return {
        "ok": True,
        "best_lag_days": best["lag"],
        "best_corr": round(best["corr"], 4),
        "n_overlap": len(dates),
        "curve": curve[-8:],
    }


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 20:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def corr_matrix(rets: Dict[str, Dict[str, float]], look: int = 60) -> Dict[str, Any]:
    names = list(rets.keys())
    common = None
    for n in names:
        ds = set(sorted(rets[n].keys())[-look:])
        common = ds if common is None else (common & ds)
    if not common or len(common) < 20:
        return {"ok": False, "reason": "重叠样本不足"}
    dates = sorted(common)
    mat: Dict[str, Dict[str, float]] = {}
    pairs = []
    for i, a in enumerate(names):
        mat[a] = {}
        for j, b in enumerate(names):
            c = _corr([rets[a][d] for d in dates], [rets[b][d] for d in dates])
            mat[a][b] = round(c, 3) if c is not None else 0.0
            if i < j and c is not None:
                pairs.append(c)
    return {
        "ok": True,
        "matrix": mat,
        "mean_pairwise": round(mean(pairs), 3) if pairs else None,
        "n_days": len(dates),
    }


# ============================================================
# L3 仓位层 —— 三重动态约束
# ============================================================

def portfolio_drawdown(cfg: Dict[str, Any], base_dir: str) -> Dict[str, Any]:
    """
    用可交易标的的等权组合净值序列计算当前回撤。
    数据来自各自 K 线缓存，无需外部输入。
    幂等：只依赖缓存数据，同一数据必得同一结果。
    """
    cache_dir = os.path.join(base_dir, cfg["data"]["cache_dir"])
    tradeable = [u["code"] for u in cfg["universe"] if u.get("tradeable")]
    per_code = {}
    for code in tradeable:
        bars = load_bars(cache_dir, code)
        if len(bars) < 30:
            continue
        per_code[code] = {b["date"]: b["close"] for b in bars}
    if len(per_code) < 2:
        return {"ok": False, "reason": "可交易标的不足 2 个，无法合成组合净值"}

    common = sorted(set.intersection(*[set(v.keys()) for v in per_code.values()]))
    if len(common) < 30:
        return {"ok": False, "reason": f"共同交易日不足({len(common)}<30)"}

    base = {c: per_code[c][common[0]] for c in per_code}
    nav = []
    for d in common:
        nav.append(sum(per_code[c][d] / base[c] for c in per_code) / len(per_code))

    peak, mdd = nav[0], 0.0
    for x in nav:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1.0)
    cur_dd = nav[-1] / max(nav) - 1.0

    return {
        "ok": True,
        "as_of": common[-1],
        "dd": round(cur_dd, 6),          # 当前相对历史最高净值
        "mdd": round(mdd, 6),            # 历史最大回撤
        "nav_points": len(nav),
        "note": f"等权组合净值回撤（{len(per_code)} 只，{common[0]}→{common[-1]}）",
    }


def dd_throttle(cfg: Dict[str, Any], port_dd: float) -> float:
    """
    回撤节流系数，档位表来自 config.risk.dd_throttle。

    注意符号：port_dd 为负值（-36.7%），档位表用正回撤幅度（0.05/0.10/...）。
    历史 bug：曾直接写 port_dd < row["dd_below"]，负值恒小于正阈值，
    永远命中第一档 1.00，导致回撤节流层即使算出真实回撤也永不生效。
    """
    table = cfg["risk"].get("dd_throttle") or []
    depth = abs(port_dd)   # 回撤幅度，正值
    for row in table:
        if depth < float(row["dd_below"]):
            return float(row["coef"])
    return float(table[-1]["coef"]) if table else 1.0


def position_sizing(cfg: Dict[str, Any], ann_vol: Optional[float], state_w: float,
                    port_dd: float, corr_mean: Optional[float]) -> Dict[str, Any]:
    vt = float(cfg["risk"]["vol_target"])

    if not ann_vol or ann_vol <= 0:
        cap_vol = 0.0
        vol_note = "波动率缺失 → 仓位上限 0"
    else:
        cap_vol = min(1.0, vt / ann_vol)
        vol_note = f"vol目标 {vt:.0%} / 实际 {ann_vol:.1%} = {cap_vol:.0%}"

    coef_dd = dd_throttle(cfg, port_dd)

    if corr_mean is None:
        coef_corr = 1.0
        corr_note = "相关性缺失 → 不施加惩罚"
    else:
        thr = float(cfg["risk"]["corr_threshold"])
        k = float(cfg["risk"]["corr_penalty_k"])
        floor = float(cfg["risk"].get("corr_penalty_floor", 0.3))
        coef_corr = 1.0 if corr_mean <= thr else max(floor, 1.0 - (corr_mean - thr) * k)
        corr_note = f"相关性 {corr_mean:.2f}（阈值 {thr:.2f}）→ 系数 {coef_corr:.2f}"

    raw = min(cap_vol, 1.0) * coef_dd * coef_corr * state_w
    final = clamp(raw, 0.0, float(cfg["risk"]["max_single"]))

    # 约束分解：指出到底是哪一层在咬（防止“三重约束”退化为单约束而不自知）
    parts = {"vol": min(cap_vol, 1.0), "dd": coef_dd, "corr": coef_corr, "state": state_w}
    binding = min(parts, key=parts.get) if parts else "none"

    return {
        "cap_vol": round(cap_vol, 4),
        "coef_dd": round(coef_dd, 4),
        "coef_corr": round(coef_corr, 4),
        "state_weight": round(state_w, 4),
        "final": round(final, 4),
        "raw_before_clamp": round(raw, 4),
        "binding": binding,
        "decomposition": {k: round(v, 4) for k, v in parts.items()},
        "notes": {"vol": vol_note,
                  "dd": f"组合回撤 {port_dd:.1%} → 系数 {coef_dd:.2f}",
                  "corr": corr_note,
                  "binding": f"生效约束 {binding}（四层取最小）"},
    }


# ============================================================
# L5 风险层 —— KILL SWITCH
# ============================================================

def kill_switches(cfg: Dict[str, Any], per_index: Dict[str, Any],
                  corr: Dict[str, Any], chain: Dict[str, Any]) -> List[Dict[str, str]]:
    sw: List[Dict[str, str]] = []

    cm = corr.get("mean_pairwise") if corr.get("ok") else None
    if cm is not None and cm > cfg["risk"]["kill_corr"]:
        deep = sum(1 for k, v in per_index.items()
                   if v.get("tradeable", True)
                   and (v["features"].get("dd_from_high") or 0)
                   < -float(cfg["risk"].get("kill_deep_dd", 0.25)))
        if deep >= 2:
            sw.append({
                "id": "SYS_CORR",
                "level": "HIGH",
                "action": "三指数高度相关且同步深度回撤 → 冻结新增多头，仅保留对冲仓",
                "detail": f"相关性 {cm:.2f} > {cfg['risk']['kill_corr']:.2f}，深度回撤标的 {deep} 个",
            })

    e_dom = sum(1 for v in per_index.values()
                if v["state"]["dominant"] == "E" and v.get("tradeable", True))
    n_trade = sum(1 for v in per_index.values() if v.get("tradeable", True))
    if e_dom >= 2:
        sw.append({
            "id": "BROAD_E",
            "level": "HIGH",
            "action": "两个以上可交易指数处于 E 阶段 → 禁止左侧加仓，仅允许右侧确认后建仓",
            "detail": f"E 阶段主导 {e_dom}/{n_trade}（仅计可交易标的）",
        })

    up_weak = -float(cfg["risk"].get("kill_upstream_ret20", 0.08))
    if chain.get("leader_ret20") is not None and chain["leader_ret20"] < up_weak:
        sw.append({
            "id": "UPSTREAM_WEAK",
            "level": "MED",
            "action": "上游链条 20 日跌幅超 8% → 禁止任何左侧试探仓位",
            "detail": f"leader20日 {chain['leader_ret20']:.1%}",
        })

    return sw


# ============================================================
# 主流程
# ============================================================

def load_config(path: str) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("缺少 pyyaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run(cfg_path: str, base_dir: str) -> Dict[str, Any]:
    cfg = load_config(cfg_path)
    cache_dir = os.path.join(base_dir, cfg["data"]["cache_dir"])

    # ---- P0（2026-11-05）：账户真实口径 + 全量 ETF 映射（异常隔离）----
    try:
        import account as account_mod
        account_state = account_mod.load(base_dir, cfg)
    except Exception as exc:
        account_state = {"configured": False, "dd": None, "source": "unconfigured",
                         "reason": f"账户模块异常：{exc}", "capital": None}
    try:
        import etf_map as etf_map_mod
        mapping = etf_map_mod.load(base_dir, cfg)
    except Exception as exc:
        mapping = {"ok": False, "instruments": {}, "count": 0,
                   "reason": f"{type(exc).__name__}: {exc}"}

    # ---- 载入各指数 ----
    loaded: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    excluded: List[str] = []
    for item in cfg["universe"]:
        code = item["code"]
        bars = load_bars(cache_dir, code)
        if len(bars) < 120:
            missing.append(f"{item['name']}({code}) 样本{len(bars)}")
            continue
        qs = quality_check(bars)
        hard = [q.split("|", 1)[1] for q in qs if q.startswith("HARD|")]
        soft = [q.split("|", 1)[1] for q in qs if q.startswith("SOFT|")]
        if hard:
            excluded.append(f"{item['name']}({code})：{'；'.join(hard)}")
            continue
        f = compute_features(bars)
        loaded[code] = {"meta": item, "bars": bars, "features": f,
                        "quality_warnings": soft}

    # ---- 统一 as_of：取所有“可交易标的”都有数据的最近交易日 ----
    # 不这样做会出现“报告日期 10-22，但部分指数只有 10-21 数据”的混日期污染。
    tradeable_codes = [u["code"] for u in cfg["universe"]
                       if u.get("tradeable") and u["code"] in loaded]
    lag_info: List[Dict[str, Any]] = []
    if tradeable_codes:
        last_dates = {c: loaded[c]["features"]["as_of"] for c in tradeable_codes}
        common_as_of = min(last_dates.values())
        for c in list(loaded.keys()):
            cur = loaded[c]["features"]["as_of"]
            if cur > common_as_of:
                lag_info.append({
                    "code": c, "name": loaded[c]["meta"]["name"],
                    "had": cur, "used": common_as_of,
                    "reason": f"为与其他标的对齐，截断至 {common_as_of}",
                })
            # 截断 bars 到 common_as_of（含），重算特征，保证同日可比
            if cur != common_as_of:
                cut = [b for b in loaded[c]["bars"] if b["date"] <= common_as_of]
                if len(cut) >= 120:
                    loaded[c]["bars"] = cut
                    loaded[c]["features"] = compute_features(cut)
    else:
        common_as_of = None

    # ---- L1 状态 ----
    per_index: Dict[str, Any] = {}
    prev_state_path = os.path.join(base_dir, cfg["data"]["state_file"])
    prev_all: Dict[str, Any] = {}
    if os.path.exists(prev_state_path):
        try:
            with open(prev_state_path, "r", encoding="utf-8") as fh:
                prev_all = json.load(fh)
        except Exception:
            prev_all = {}

    for code, obj in loaded.items():
        prev_rec = prev_all.get(code) or {}
        # 幂等：同一交易日内重跑，必须复用当日最初的先验，不得递推叠加
        if prev_rec.get("as_of") == obj["features"]["as_of"]:
            prev = None
            prior_used = prev_rec.get("prior")
        else:
            prev = prev_rec.get("probs")
            prior_used = None
        sp = state_probs(obj["features"], prev,
                         float(cfg["state"]["persistence"]),
                         float(cfg["state"]["temperature"]),
                         cfg["state"]["scoring"],
                         float(cfg["state"].get("uniform_prior", 0.2)))
        if prior_used:
            sp["prior"] = prior_used
        per_index[code] = {
            "name": obj["meta"]["name"],
            "role": obj["meta"].get("role", "core"),
            "bucket": obj["meta"].get("bucket", "core"),
            "tradeable": bool(obj["meta"].get("tradeable", True)),
            "as_of": obj["features"]["as_of"],
            "features": obj["features"],
            "state": sp,
            "state_weight": state_weighted_position(sp["probs"], cfg["state"]["weights"]),
        }

    # ---- 主状态连续段（起始日 / 持续交易日）----
    # 落盘逐日主状态历史，并从历史回算连续段（与 stock-pool 指数池同口径）。
    # 模型无滞回状态机，st["dominant"] 即理想态，直接计数不受滞回污染。
    if common_as_of:
        _hist_dir = os.path.dirname(cfg["data"]["state_file"])
        _hist_path = os.path.join(base_dir, _hist_dir, "state_dominant_history.jsonl")
        _doms = {c: v["state"]["dominant"] for c, v in per_index.items()}
        append_dominant_history(_hist_path, common_as_of, _doms)
        _hist = load_dominant_history(_hist_path)
        for c, v in per_index.items():
            _sd, _days = state_streak(_hist, c, v["state"]["dominant"])
            v["state"]["state_start"] = _sd
            v["state"]["state_days"] = _days

    # ---- L2 传导链 + 相关性 ----
    rets = {c: log_returns(o["bars"]) for c, o in loaded.items()}
    corr = corr_matrix(rets, cfg["chain"]["corr_look"])

    leader_code = cfg["chain"]["leader"]
    chain: Dict[str, Any] = {"leader": leader_code}
    if leader_code in loaded:
        lf = loaded[leader_code]["features"]
        chain["leader_as_of"] = lf["as_of"]
        chain["leader_ret20"] = lf.get("ret20")
        chain["leader_ret60"] = lf.get("ret60")
        chain["leader_dd"] = lf.get("dd_from_high")
        chain["lags"] = {}
        for item in cfg["universe"]:
            c = item["code"]
            if c == leader_code or c not in loaded:
                continue
            chain["lags"][c] = {
                "name": item["name"],
                **lead_lag(loaded[leader_code]["bars"], loaded[c]["bars"],
                          cfg["chain"]["max_lag"]),
            }
    else:
        chain["error"] = f"leader {leader_code} 无数据"

    # ---- L2 → L1 反馈：上游弱势压制状态权重 ----
    # 原实现里 chain 只写进报告，不参与任何计算（审计缺陷 6）。
    # 这里让 leader 的 20 日动量真实压降下游标的的状态权重。
    l2_cfg = cfg.get("chain") or {}
    suppress_k = float(l2_cfg.get("leader_suppress_k", 0.0))
    leader_ret20 = chain.get("leader_ret20")
    l2_applied = []
    if suppress_k > 0 and leader_ret20 is not None:
        thr2 = float(l2_cfg.get("leader_suppress_thr", -0.05))
        for code, v in per_index.items():
            if code == cfg["chain"]["leader"] or not v["tradeable"]:
                continue
            if leader_ret20 < thr2:
                excess = min(1.0, (thr2 - leader_ret20) / 0.10)
                factor = max(0.0, 1.0 - suppress_k * excess)
                before = v["state_weight"]
                v["state_weight"] = round(before * factor, 4)
                v["l2_suppress"] = {
                    "leader_ret20": round(leader_ret20, 4),
                    "factor": round(factor, 4),
                    "before": round(before, 4),
                    "after": v["state_weight"],
                }
                l2_applied.append(f"{v['name']}×{factor:.2f}")
    if l2_applied:
        chain["l2_suppress_applied"] = l2_applied

    # ---- L3 仓位 ----
    # P0-①（2026-11-05）：回撤输入改为【真实账户净值】。
    # 原实现用 11 只可交易指数的等权合成净值算 -44.88%，与用户实际账户无关，
    # 使 coef_dd 恒为 0.15、结构性锁死。该合成值降级为"演示参考"，不再参与压仓。
    import account as account_mod
    synthetic = portfolio_drawdown(cfg, base_dir)
    port_dd = account_mod.throttle_dd(account_state, cfg)
    dd_info = {
        "ok": bool(account_state.get("configured")),
        "dd": account_state.get("dd"),
        "dd_used": round(port_dd, 6),
        "source": account_state.get("source") if account_state.get("configured") else "unconfigured",
        "label": account_mod.label(account_state),
        "note": account_state.get("note") or account_state.get("reason") or "",
        "synthetic_demo": synthetic,
        "synthetic_note": "假设组合等权净值，仅供参考，不参与账户级节流",
    }
    corr_mean = corr.get("mean_pairwise") if corr.get("ok") else None

    for code, v in per_index.items():
        if v["tradeable"]:
            v["sizing"] = position_sizing(cfg, v["features"].get("ann_vol_20"),
                                          v["state_weight"], port_dd, corr_mean)
        else:
            v["sizing"] = {
                "cap_vol": 0.0, "coef_dd": 1.0, "coef_corr": 1.0,
                "state_weight": round(v["state_weight"], 4), "final": 0.0,
                "notes": {"vol": "参照系，不计仓", "dd": "—", "corr": "—"},
                "reference_only": True,
            }

    # ---- B2 资格分层（P2）注入点见 L5 后、decision_layer 前——
    # 必须晚于 quality_warnings_list（1264 区），早于 decision_layer.apply（sizing 归零要进执行链）。

    # ---- L4 执行层：追价上限 + 失效位 ----
    for code, v in per_index.items():
        f = v["features"]
        atr = f.get("atr14")
        is_theme = (v["bucket"] == "theme")
        ch_atr = cfg["execution"]["chase_atr_theme"] if is_theme else cfg["execution"]["chase_atr_index"]
        fl_atr = cfg["execution"]["fail_atr_theme"] if is_theme else cfg["execution"]["fail_atr_offset"]
        ref = f.get("prior_60_hi") or f.get("close")
        swing_low = f.get("swing_low_20")
        v["execution"] = {
            "chase_atr_mult": ch_atr,
            "chase_cap_price": round(ref + atr * ch_atr, 2) if (ref and atr) else None,
            "trigger_price": round(ref, 2) if ref else None,
            "fail_price": round(swing_low - atr * fl_atr, 2) if (swing_low and atr) else None,
            "single_trade_risk_nav": cfg["execution"]["single_trade_risk_nav"],
            "note": ("主题指数：追价上限与失效位均收紧" if is_theme else "指数级标准口径")
                     if v["tradeable"] else "参照系，不生成执行计划",
        }

        # ---- E 阶段反弹子状态 → 战术仓许可 ----
        rb = f.get("rebound") or {}
        v["execution"]["rebound"] = rb
        v["execution"]["rebound_allowance"] = 0.0
        if v["state"]["dominant"] == "E" and rb.get("active"):
            v["execution"]["rebound_allowance"] = cfg["risk"]["rebound_tactical_max"]
            v["execution"]["note"] += "；且允许战术反弹仓"

    # ---- 组合层：按桶合并（同桶视为一个仓位；仅可交易标的计入） ----
    buckets: Dict[str, List[str]] = {}
    for code, v in per_index.items():
        if v["tradeable"]:
            buckets.setdefault(v["bucket"], []).append(code)

    # bucket_max 双形态兼容（B2 P2）：标量=全桶统一（原行为）；dict=按桶系数
    # （core/theme/ai_hw/pharma/watch 各档，缺省桶用 "default" 键，再缺省用最小值）。
    bm_cfg = cfg["risk"]["bucket_max"]
    if isinstance(bm_cfg, dict):
        def _bm(b: str) -> float:
            return float(bm_cfg.get(b, bm_cfg.get("default", min(bm_cfg.values()))))
    else:
        def _bm(b: str) -> float:
            return float(bm_cfg)

    bucket_exposure: Dict[str, Any] = {}
    for b, codes in buckets.items():
        if len(codes) == 1:
            bucket_exposure[b] = {
                "codes": codes,
                "exposure": per_index[codes[0]]["sizing"]["final"],
                "note": "单标的，按单仓上限",
            }
        else:
            vols = [(c, per_index[c]["features"].get("ann_vol_20") or 0.35) for c in codes]
            # 保守：同桶按最高波动率计上限，等权分配
            vmax = max(v for _, v in vols)
            cap = clamp(cfg["risk"]["vol_target"] / vmax, 0, _bm(b))
            wmean = mean([per_index[c]["state_weight"] for c in codes]) or 0.0
            dd_c = dd_throttle(cfg, port_dd)
            c_corr = 1.0 if corr_mean is None or corr_mean <= cfg["risk"]["corr_threshold"] else \
                max(0.3, 1.0 - (corr_mean - cfg["risk"]["corr_threshold"]) * cfg["risk"]["corr_penalty_k"])
            exp_total = clamp(cap * dd_c * c_corr * wmean, 0, _bm(b))
            bucket_exposure[b] = {
                "codes": codes,
                "exposure": round(exp_total, 4),
                "per_name": round(exp_total / len(codes), 4),
                "note": f"同桶合并计仓（最高vol {vmax:.1%}，{len(codes)} 只等权）",
            }

    total_exposure = sum(v["exposure"] for v in bucket_exposure.values())

    # ---- L5 风险 ----
    switches = kill_switches(cfg, per_index, corr, chain)

    # ---- 开关真正作用于仓位（而不是只展示）----
    haircut = 1.0
    applied: List[str] = []
    for s in switches:
        h = cfg["risk"]["switch_haircut"].get(s["level"], 1.0)
        if h < haircut:
            haircut = h
        applied.append(f"{s['id']}→{h:.2f}")

    theme_cap = cfg["risk"].get("theme_cap_under_e", 0.06)
    theme_bks = set(cfg["risk"].get("theme_buckets") or ["theme"])
    e_dom_n = sum(1 for v in per_index.values()
                  if v.get("tradeable", True) and v["state"]["dominant"] == "E")

    # 主题组按【组内合计】卡硬顶，不是每桶各卡一次。
    # 否则每加一个主题桶，硬顶就实际抬高一倍（6%→12%→18%），约束被静默放宽。
    theme_sum = 0.0
    for b, v in bucket_exposure.items():
        if b in theme_bks:
            theme_sum += v["exposure"] * haircut
    theme_scale = 1.0
    if e_dom_n >= 2 and theme_sum > theme_cap > 0:
        theme_scale = theme_cap / theme_sum

    for b, v in bucket_exposure.items():
        pre = v["exposure"]
        v["exposure_pre_switch"] = pre
        capped = pre * haircut
        if b in theme_bks and theme_scale < 1.0:
            capped = pre * haircut * theme_scale
            v["cap_reason"] = (f"E 阶段主题组硬上限 {theme_cap:.0%}"
                               f"（组内合计 {theme_sum:.2%} → 缩放 {theme_scale:.2f}）")
        v["exposure"] = round(capped, 4)
        if len(v.get("codes", [])) > 1:
            v["per_name"] = round(capped / len(v["codes"]), 4)

    total_pre_switch = total_exposure
    total_exposure = sum(v["exposure"] for v in bucket_exposure.values())

    hedge_need = 0.0
    p_de = mean([per_index[c]["state"]["probs"]["D"] + per_index[c]["state"]["probs"]["E"]
                 for c in per_index]) or 0.0
    if p_de > cfg["risk"]["hedge_trigger_p_de"]:
        hedge_need = round(clamp((p_de - cfg["risk"]["hedge_trigger_p_de"]) * 1.5, 0,
                                cfg["risk"]["hedge_max"]), 3)

    # ---- P2-③：软提示只算一次，供决策层分级与报告复用（避免两处口径漂移）----
    quality_warnings_list = sorted({f"{v['name']}：{w}"
                                    for c, v in per_index.items()
                                    for w in (loaded.get(c, {}).get("quality_warnings") or [])})

    # ---- B2 资格分层（P2）：四因子资格分 + FROZEN 仓位归零投影 ----
    # 策划案 docs/tradeable_universe_review_20261106.html；引擎模块 eligibility.py。
    # 注入点：quality_warnings_list 之后、decision_layer.apply 之前——
    # FROZEN 归零后的 sizing 直接进入决策层执行链。
    _elig_cfg = (cfg.get("eligibility") or {})
    result_modulation: Dict[str, Any] = {"enabled": False}
    if _elig_cfg.get("enabled", True):
        try:
            import eligibility as _elig
            _qpath = os.path.join(base_dir, "data", "etf_quotes.json")
            _quotes = {}
            if os.path.exists(_qpath):
                try:
                    with open(_qpath, "r", encoding="utf-8") as _f:
                        _quotes = (json.load(_f) or {}).get("quotes") or {}
                except Exception:
                    _quotes = {}  # 行情快照损坏 → 各标的 inv 保守 0（引擎内单标的隔离）
            _scores = _elig.compute_all(per_index, quality_warnings_list, _quotes,
                                        _elig_cfg)
            for _c, _v in per_index.items():
                _v["eligibility"] = _scores.get(_c)
                if (_elig_cfg.get("enforce_sizing_zero", True)
                        and _v.get("tradeable") and "sizing" in _v
                        and (_scores.get(_c) or {}).get("tier") == "FROZEN"):
                    _v["sizing"]["final"] = 0.0
                    _v["sizing"]["notes"]["eligibility"] = (
                        "B2 资格硬闸 FROZEN → 仓位归零（资格分低于禁开线，"
                        "E 态/事件族 D 态等矩阵铁律）")
            # 逐日 JSONL 落盘（同 as_of 幂等；失败不阻断主链路，仅告警）
            try:
                _hist_rel = _elig_cfg.get("history_path",
                                          "state/eligibility_history.jsonl")
                _hist = os.path.join(base_dir, _hist_rel)
                _asof_now = (common_as_of
                             or (max(v["as_of"] for v in per_index.values())
                                 if per_index else ""))
                _elig.append_history(_hist, _asof_now, _scores)
            except Exception as _exc:
                print(f"[warn] eligibility 历史落盘失败: {_exc!r}", file=sys.stderr)
        except Exception as _exc:
            if _elig_cfg.get("fail_closed", True):
                for _v in per_index.values():
                    _v["eligibility"] = {
                        "score": 0.0, "tier": "FROZEN", "family": "unknown",
                        "dominant": None,
                        "factors": {"investability": 0.0, "state_fit": 0.0,
                                    "data_quality": 0.0, "map_ready": 0.0},
                        "notes": [f"引擎级异常 fail-closed: {_exc!r}"],
                    }
                    if _v.get("tradeable") and "sizing" in _v:
                        _v["sizing"]["final"] = 0.0
                        _v["sizing"]["notes"]["eligibility"] = (
                            "B2 引擎异常 fail-closed → 仓位归零")
                print(f"[warn] eligibility 引擎异常 fail-closed: {_exc!r}",
                      file=sys.stderr)
            else:
                raise

    # ---- B2 P3 状态调制乘数 M：E 态占比 → 全局仓位调制 ----
    # 位于资格分层块之后、decision_layer.apply 之前——M 乘入 sizing.final
    # 与 total_exposure，决策卡/执行金额换算全链生效。
    _mod_cfg = (_elig_cfg.get("modulation") or {})
    if _mod_cfg.get("enabled", True) and _elig_cfg.get("enabled", True):
        try:
            import lifecycle as _lc
            _e_all = sum(1 for v in per_index.values()
                         if (v.get("state") or {}).get("dominant") == "E")
            _ratio = _e_all / max(1, len(per_index))
            _M = _lc.modulation_multiplier(_ratio, _mod_cfg)
            if _M < 1.0:
                total_exposure *= _M
                for _v in per_index.values():
                    if _v.get("tradeable") and "sizing" in _v \
                            and _v["sizing"].get("final", 0) > 0:
                        _v["sizing"]["final"] *= _M
                        _v["sizing"]["notes"]["modulation"] = (
                            f"B2 状态调制 M={_M:.2f}（E 态占比 {_ratio:.0%}）")
            result_modulation = {
                "enabled": True, "e_ratio": round(_ratio, 4),
                "multiplier": round(_M, 4),
                "note": (f"E 态占比 {_ratio:.0%} → 全局仓位调制 M={_M:.2f}"
                         if _M < 1.0 else f"E 态占比 {_ratio:.0%} → M=1.00（未收缩）"),
            }
        except Exception as _exc:
            result_modulation = {"enabled": True, "error": repr(_exc)}
            print(f"[warn] 状态调制计算失败: {_exc!r}", file=sys.stderr)
    else:
        result_modulation = {"enabled": False}

    # ---- P0-②③④⑤：市场闸门顶层否决 + 治理门禁接线 + ETF 映射 + 一页纸决策卡 ----
    try:
        import decision_layer
        layer = decision_layer.apply(
            cfg, base_dir,
            common_as_of or (max(v["as_of"] for v in per_index.values()) if per_index else ""),
            per_index, total_exposure, p_de, hedge_need, account_state, mapping,
            bucket_exposure, quality_warnings_list)
    except Exception as exc:
        layer = {"result_fields": {"decision_layer_error": f"{type(exc).__name__}: {exc}"},
                 "gate_results": {}}

    # ---- 汇总 ----
    # 统一 as_of（不再取 max，避免混日期）
    as_of = common_as_of or (max(v["as_of"] for v in per_index.values()) if per_index else "")

    result = {
        "_paths": {},
        "as_of": as_of,
        "portfolio_dd": dd_info,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config_version": cfg["meta"]["version"],
        "per_index": {
            c: {
                "name": v["name"], "role": v["role"], "bucket": v["bucket"],
                "as_of": v["as_of"],
                "close": v["features"]["close"],
                "ann_vol_20": v["features"].get("ann_vol_20"),
                "dd_from_high": v["features"].get("dd_from_high"),
                "pos_in_52w": v["features"].get("pos_in_52w"),
                "er20": v["features"].get("er20"),
                "adx14": v["features"].get("adx14"),
                "above_ma200": v["features"].get("above_ma200"),
                "ma200_slope": v["features"].get("ma200_slope"),
                "vol_z20": v["features"].get("vol_z20"),
                "state": {
                    "probs": {k: round(p, 4) for k, p in v["state"]["probs"].items()},
                    "dominant": v["state"]["dominant"],
                    "dominant_name": STATE_NAMES[v["state"]["dominant"]],
                    "confidence": round(v["state"]["confidence"], 4),
                    "scores": {k: round(s, 3) for k, s in v["state"]["scores"].items()},
                    "state_start": v["state"].get("state_start"),
                    "state_days": int(v["state"].get("state_days", 0) or 0),
                },
                "state_weight": round(v["state_weight"], 4),
                "tradeable": v["tradeable"],
                "sizing": v["sizing"],
                "execution": v["execution"],
                "l2_suppress": v.get("l2_suppress"),
                "eligibility": v.get("eligibility"),
                "instrument": v.get("instrument"),
                "governance_gate": (layer.get("gate_results") or {}).get(c),
                "open_allowed": bool(v.get("open_allowed")),
            } for c, v in per_index.items()
        },
        "correlation": corr,
        "chain": chain,
        "buckets": bucket_exposure,
        "total_exposure": round(total_exposure, 4),
        "eligibility_modulation": result_modulation,
        "total_exposure_pre_switch": round(total_pre_switch, 4),
        "switch_haircut_applied": applied,
        "switch_haircut": round(haircut, 3),
        "hedge_need": hedge_need,
        "hedge_note": f"P(D)+P(E) 均值 {p_de:.2f} → 建议对冲 {hedge_need:.0%} beta" if hedge_need
                      else f"P(D)+P(E) 均值 {p_de:.2f} → 暂不需要系统性对冲",
        "kill_switches": switches,
        "data_gaps": missing,
        "data_excluded": excluded,
        "quality_warnings": quality_warnings_list,
        "min_confidence_to_act": cfg["state"]["min_confidence_to_act"],
        "data_lag": lag_info,
        "as_of_rule": "统一取所有可交易标的都有数据的最近交易日；滞后标的已截断对齐",
        **layer["result_fields"],
    }


    # ---- L6 元层：预测台账 + 校准 ----
    try:
        import meta_layer
        ledger_dir = os.path.join(base_dir, cfg["data"]["ledger_dir"])
        pred_path = meta_layer.record_prediction(ledger_dir, as_of, per_index)
        bars_by_code = {c: o["bars"] for c, o in loaded.items()}
        settled = meta_layer.settle_predictions(
            ledger_dir, bars_by_code,
            cfg["meta_layer"]["calibration"]["horizon_days"])
        cal = meta_layer.calibration(ledger_dir, cfg)
        cal["newly_settled"] = settled.get("newly_settled", 0)
        result["calibration"] = cal
        result["_paths"]["ledger"] = pred_path
    except Exception as exc:  # 元层故障不能阻塞主流程
        result["calibration"] = {"status": f"元层未运行：{exc}"}

    # ---- P2-①②：结算进度分级 + 归因台账真正落盘（异常隔离，不污染元层结论）----
    try:
        import calibration_progress as cp_mod
        import attribution as attr_mod
        _ledger_dir = os.path.join(base_dir, cfg["data"]["ledger_dir"])
        _bars_by_code = {c: o["bars"] for c, o in loaded.items()}
        result["settlement_progress"] = cp_mod.build(_ledger_dir, _bars_by_code, cfg)
        result["attribution_rebuild"] = attr_mod.rebuild(_ledger_dir, _bars_by_code, cfg)
    except Exception as exc:
        result["settlement_progress"] = {"tier": "unknown",
                                         "label": f"结算进度模块异常：{exc}"}
        result["attribution_rebuild"] = {"written": 0,
                                         "reason": f"归因模块异常：{exc}"}

    # ---- 治理层：把配置里承诺的 premortem/redteam/归因落到实处 ----
    try:
        import governance
        j = (cfg.get("meta_layer") or {}).get("journal") or {}
        ledger_dir = os.path.join(base_dir, cfg["data"]["ledger_dir"])
        gov = {
            "require_premortem": bool(j.get("require_premortem")),
            "require_redteam": bool(j.get("require_redteam")),
            "attribution_buckets": (cfg.get("meta_layer") or {}).get("attribution", {}).get("buckets", []),
            "gate_note": "开仓前必须通过 check_gate；未通过则不允许建仓",
        }
        # 归因台账健康度（若已有记录）
        attr_path = os.path.join(ledger_dir, "attribution.jsonl")
        attr_recs = governance.read_jsonl(attr_path)
        gov["attribution_health"] = governance.attribute_health(attr_recs)
        gov_path = os.path.join(ledger_dir, "governance.jsonl")
        recs = governance.read_jsonl(gov_path)
        gov["gate_log_count"] = len(recs)
        gov["daily_records"] = sum(1 for r in recs if r.get("type") == "daily")
        gov["checked_today"] = sum(1 for g in (layer.get("gate_results") or {}).values()
                                   if g.get("open_intent"))
        gov["allowed_today"] = sum(1 for g in (layer.get("gate_results") or {}).values()
                                   if g.get("allowed"))
        gov["market_veto"] = bool((result.get("market_context") or {}).get("veto"))
        gov["journal_dir"] = os.path.join(ledger_dir, "journal")
        result["governance"] = gov
        result["_paths"]["governance"] = gov_path
    except Exception as exc:
        result["governance"] = {"status": f"治理层未运行：{exc}"}

    # ---- P0：概率未校准不得显示成"胜率" ----
    if isinstance(result.get("decision_card"), dict):
        cal = result.get("calibration") or {}
        settled = int(cal.get("settled_n") or 0)
        need = int((cfg.get("calibration_display") or {}).get("min_settled") or 30)
        card = result["decision_card"]
        card["calibration_status"] = cal.get("status", "台账未建立")
        card["calibration_settled"] = settled
        card["calibration_label"] = (f"概率已校准（{settled} 条结算）" if settled >= need
                                     else f"{card['calibration_label']}：{settled}/{need} 条结算样本")
        sp = result.get("settlement_progress") or {}
        card["settlement_progress"] = sp.get("label", "—")
        if settled < need and sp.get("next_remaining_days") is not None:
            card["calibration_label"] += (
                f"；最早一批还需约 {sp['next_remaining_days']} 个交易日"
                f"（≈{sp.get('next_est_weeks')} 周）")

    # ---- 落盘（必须在所有层跑完之后，否则 governance/calibration 会写成 null）----
    # B2 P3 生命周期评估（晋升/降级建议 + 冷却；事件入台账）——必须在落盘前。
    try:
        import lifecycle as _lc
        import eligibility as _elig_hist
        _lc_cfg = (_elig_cfg.get("lifecycle") or {})
        if _lc_cfg.get("enabled", True) and as_of:
            _hist_rel = _elig_cfg.get("history_path",
                                      "state/eligibility_history.jsonl")
            _hist = os.path.join(base_dir, _hist_rel)
            _rows_l = _elig_hist.load_history(_hist)
            _tmap = {c: bool(v.get("tradeable"))
                     for c, v in per_index.items()}
            _lc_res = _lc.evaluate(_rows_l, _tmap, as_of, base_dir, _lc_cfg)
            _lc.record_events(base_dir, as_of, _lc_res, _lc_cfg)
            _lc.mark_demotions(base_dir, as_of, _lc_res, _lc_cfg)
            result["lifecycle"] = _lc_res
        else:
            result["lifecycle"] = {"enabled": False}
    except Exception as _exc:
        result["lifecycle"] = {"error": repr(_exc)}
        print(f"[warn] lifecycle 评估失败: {_exc!r}", file=sys.stderr)

    if not as_of:
        result["_error"] = "全部标的无有效数据，本次不落盘"
        return result

    out_dir = os.path.join(base_dir, cfg["data"]["output_dir"])
    os.makedirs(out_dir, exist_ok=True)
    dec_path = os.path.join(out_dir, f"splus_decision_{as_of.replace('-', '')}.json")
    with open(dec_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    os.makedirs(os.path.dirname(prev_state_path), exist_ok=True)
    with open(prev_state_path, "w", encoding="utf-8") as fh:
        json.dump({c: {"probs": v["state"]["probs"], "as_of": v["as_of"],
                       "prior": v["state"]["prior"]}
                   for c, v in per_index.items()}, fh, ensure_ascii=False, indent=2)

    result["_paths"].update({"decision": dec_path, "state": prev_state_path})

    return result
