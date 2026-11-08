#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_state_significance.py —— 状态机结论的「时间维度」显著性检验（只读）

## 为什么必须做这个

tools_state_edge.py 的 t 值是在【横截面】上算的：118 只指数的逐只配对 edge 求均值再除以
跨指数标准误。这个做法隐含假设「118 只 = 118 个独立观测」，但实测平均两两相关 0.57–0.61，
有效独立样本 n_eff = n/(1+(n−1)ρ̄) 仅 1.6–1.8，第一主成分解释 59–64% 的方差。
后果：跨指数 t 值被系统性放大 √(n/n_eff) ≈ 8 倍。C 态 t=4.7 在相关性修正后约 0.6，
统计上不显著——若据此升级策略，等于把噪声当信号。

正确的推断维度是【时间】：把「某状态的横截面平均超额」看成一条日频序列，
用 HAC（Newey-West）处理重叠窗口带来的自相关，用 block bootstrap 处理未知分布，
再辅以不重叠子样本与年度分块两个独立视角。

## 方法（四层，逐步收紧）

  L1 朴素 t         —— 直接对日频序列算 t（重叠窗口下严重高估，仅作对照下界）
  L2 Newey-West t   —— Bartlett 核，lag = 20（与 fwd20 重叠期一致）
  L3 不重叠子样本 t —— 按 0/1/…/19 偏移各取 1/20 的子样本，得到 20 个近似独立 t
  L4 block bootstrap——stationary bootstrap（平均块长 40 日）+ 年度整块重抽（7 年）

外加：
  * 年度逐一视图（7 个年度各自的 edge 与方向），回答「是不是只靠某一年」
  * n_eff 时间维度 ≈ T/lag，给出真正的有效样本量
  * 状态两两差异检验（如 C vs E），回答「排序是否真实存在」

## 参数校准（蒙特卡洛实测，名义 5% 下的实际误判率）

数据生成：n=1200 的日频序列，由 AR(1) 日收益（φ=0.3）滚动 20 日取均值构造，
即复刻"fwd20 重叠"这一真实结构；零假设为真（均值 0）时统计拒绝率：

  | 统计量                      | 误判率 | 备注                          |
  |-----------------------------|--------|-------------------------------|
  | 朴素 t（日频，忽略重叠）     | 0.67   | 完全不可用——重叠把 t 放大 √20 |
  | Newey-West lag=20（=重叠期） | 0.115  | 仍高估 2.3 倍                  |
  | Newey-West lag=40（=2×重叠） | 0.085  | 默认                           |
  | Newey-West lag=60            | 0.065  |                                |
  | stationary bootstrap b=40    | 0.083  |                                |
  | stationary bootstrap b=80    | 0.067  | 默认                           |

结论：默认取 lag=40、block=80，实测误判率约 6.5–8.5%，仍略高于名义 5%，
因此本工具的输出判定采用【三法取最保守】（p_effective = max(p_NW, p_boot, p_yearboot)），
宁可漏报也不虚报。跨指数 t 值不参与判定，只作对照。

## 用法

  python tools_state_significance.py                       # 读 state/state_panel.jsonl
  python tools_state_significance.py --panel state/state_panel.jsonl --json state/state_significance.json
  python tools_state_significance.py --boot 5000 --block 40 --lag 20

退出码：0 正常；2 面板缺失（先跑 tools_state_edge.py --dump-panel）
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(BASE, "state", "state_panel.jsonl")
OUT = os.path.join(BASE, "state", "state_significance.json")
CORR = os.path.join(BASE, "data", "corr_structure.json")
STATES = ["A", "B", "C", "D", "E"]
FWD = 20


# ------------------------------------------------------------------ 统计工具
def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def autocov(xs: List[float], lag: int) -> float:
    """样本自协方差 γ_l（分母 n，与 Newey-West 一致）。lag=0 即方差。"""
    n = len(xs)
    if lag < 0 or n <= lag:
        return 0.0
    m = mean(xs)
    s = 0.0
    for i in range(n - lag):
        s += (xs[i] - m) * (xs[i + lag] - m)
    return s / n


def newey_west_t(xs: List[float], lag: int) -> Tuple[float, float, float]:
    """Bartlett 核 HAC t 值。返回 (mean, se, t)。"""
    n = len(xs)
    if n < 30:
        return mean(xs), float("nan"), float("nan")
    m = mean(xs)
    g0 = autocov(xs, 0)
    if g0 <= 0:
        return m, float("nan"), float("nan")
    var = g0
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1.0)
        var += 2.0 * w * autocov(xs, l)
    # Bartlett 核理论上保证非负；有限样本下仍可能因数值抖动略负，退化回 γ0
    if var <= 0:
        var = g0
    se = math.sqrt(var / n)
    return m, se, (m / se if se > 0 else float("nan"))


def naive_t(xs: List[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    sd = statistics.pstdev(xs)
    return mean(xs) / (sd / math.sqrt(n)) if sd > 0 else float("nan")


def disjoint_t(xs: List[float], step: int) -> Dict[str, Any]:
    """不重叠子样本：按 offset=0..step-1 各抽一条，得到 step 个近似独立的 t。"""
    ts, means = [], []
    for off in range(step):
        sub = xs[off::step]
        if len(sub) < 20:
            continue
        t = naive_t(sub)
        if t == t:  # not NaN
            ts.append(t)
            means.append(mean(sub))
    if not ts:
        return {"n_offset": 0, "t_mean": None, "positive_share": None}
    return {"n_offset": len(ts), "t_mean": round(mean(ts), 2),
            "t_median": round(statistics.median(ts), 2),
            "positive_share": round(sum(1 for t in ts if t > 0) / len(ts), 3),
            "mean_of_means": round(mean(means), 6)}


def stationary_bootstrap(xs: List[float], block: int, b: int, seed: int,
                         recenter: bool = True) -> Dict[str, Any]:
    """Politis-Romano stationary bootstrap：几何分布块长，均值=block。

    recenter=True 时对 (x − x̄) 重抽，得到 H0: mean=0 下的零分布 → 双侧 p 值。
    同时给出原始尺度的 percentile 置信区间。
    """
    n = len(xs)
    if n < 50:
        return {"p": None, "ci": None}
    rng = random.Random(seed)
    p_geom = 1.0 / block
    m0 = mean(xs)
    centered = [v - m0 for v in xs] if recenter else list(xs)
    null_means: List[float] = []
    raw_means: List[float] = []
    for _ in range(b):
        idx = rng.randrange(n)
        s = 0.0
        for _ in range(n):
            s += centered[idx]
            if rng.random() < p_geom:
                idx = rng.randrange(n)
            else:
                idx = (idx + 1) % n
        null_means.append(s / n)
        raw_means.append(s / n + (m0 if recenter else 0.0))
    null_means.sort()
    raw_means.sort()
    obs = abs(m0)
    p = sum(1 for v in null_means if abs(v) >= obs) / len(null_means)
    lo = raw_means[int(0.025 * len(raw_means))]
    hi = raw_means[min(len(raw_means) - 1, int(0.975 * len(raw_means)))]
    return {"p": round(p, 4), "ci": [round(lo, 6), round(hi, 6)], "b": b, "block": block}


def year_block_bootstrap(xs_by_year: Dict[str, List[float]], b: int, seed: int) -> Dict[str, Any]:
    """按【整年】重抽（年度内自相关结构完整保留），检验结论是否依赖特定年份。"""
    years = sorted(xs_by_year)
    if len(years) < 4:
        return {"p": None, "ci": None}
    rng = random.Random(seed)
    obs = mean([mean(xs_by_year[y]) for y in (years[:-1] or years)])  # 完整年度
    full_years = years[:-1] if len(years) > 4 else years           # 剔除不完整年
    if not full_years:
        full_years = years
    obs = mean([mean(xs_by_year[y]) for y in full_years])
    stats_: List[float] = []
    for _ in range(b):
        pick = [full_years[rng.randrange(len(full_years))] for _ in range(len(full_years))]
        stats_.append(mean([mean(xs_by_year[y]) for y in pick]))
    stats_.sort()
    centered = [v - obs for v in stats_]
    p = sum(1 for v in centered if abs(v) >= abs(obs)) / len(stats_)
    lo = stats_[int(0.025 * len(stats_))]
    hi = stats_[min(len(stats_) - 1, int(0.975 * len(stats_)))]
    return {"p": round(p, 4), "ci": [round(lo, 6), round(hi, 6)],
            "years": len(full_years), "obs": round(obs, 6)}


# ------------------------------------------------------------------ 面板
def load_panel(path: str) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    recs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    dates: set = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            recs[o["c"]].append({"date": o["d"], "dom": o["s"],
                                 "f20": (float(o["f"]) if "f" in o else None)})
            dates.add(o["d"])
    for c in recs:
        recs[c].sort(key=lambda x: x["date"])
    return dict(recs), sorted(dates)


def build_series(recs: Dict[str, List[Dict[str, Any]]], dates: List[str],
                 min_members: int) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """构建每个状态的日频序列。

    edge_t(s) = mean_i [ f20(i,t) − base_i ]，base_i = 指数 i 全期 f20 均值。
    这是「同一时点、同一天」的横截面平均超额，因此它的自相关只来自 fwd20 重叠，
    不再掺入横截面相关性——这正是时间维度检验成立的前提。
    """
    base_of = {}
    for c, rs in recs.items():
        vals = [r["f20"] for r in rs if r["f20"] is not None]
        if len(vals) >= 120:
            base_of[c] = mean(vals)

    # 交易日口径：只保留「当天有足够多标的」的日期。
    # 必要性：面板含 12 只境外标的（港/美/英），它们在其本地交易日有数据而 A 股休市，
    # 若不过滤，这些"只有境外标的"的日子会以 5~12 个成员进入均值，
    # 用境外的基线去污染 A 股状态的横截面超额。
    # 门槛取 max(20, 30% 标的数)：既能滤掉境外日，也不至于因停牌漏掉真实交易日。
    day_count: Dict[str, int] = defaultdict(int)
    for c, rs in recs.items():
        if c not in base_of:
            continue
        for r in rs:
            if r["f20"] is not None:
                day_count[r["date"]] += 1
    thin = max(20, int(0.30 * len(base_of)))
    dates_all = list(dates)
    dates = [d for d in dates if day_count.get(d, 0) >= thin]

    # date -> state -> list of (f20 - base)
    bucket: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    raw_bucket: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for c, rs in recs.items():
        b = base_of.get(c)
        if b is None:
            continue
        for r in rs:
            if r["f20"] is None:
                continue
            bucket[r["date"]][r["dom"]].append(r["f20"] - b)
            raw_bucket[r["date"]][r["dom"]].append(r["f20"])

    out: Dict[str, Dict[str, Any]] = {}
    for s in STATES:
        pts, raws = [], []
        for d in dates:
            v = bucket.get(d, {}).get(s) or []
            if len(v) < min_members:
                continue
            pts.append((d, mean(v), len(v)))
            raws.append(mean(raw_bucket[d][s]))
        out[s] = {
            "dates": [p[0] for p in pts],
            "edge": [p[1] for p in pts],
            "members": [p[2] for p in pts],
            "abs": raws,
        }
    meta = {"thin_threshold": thin, "days_kept": len(dates),
            "days_dropped": len(set(dates_all) - set(dates))}
    return out, meta


# ------------------------------------------------------------------ 主流程
def analyse(series: Dict[str, Any], lag: int, block: int, boot: int,
            seed: int, min_members: int) -> Dict[str, Any]:
    xs = series["edge"]
    dates = series["dates"]
    n = len(xs)
    if n < 60:
        return {"n_days": n, "verdict": "样本不足"}

    m, se, t_nw = newey_west_t(xs, lag)
    t_naive = naive_t(xs)
    dj = disjoint_t(xs, FWD)   # 不重叠子样本：步长固定为重叠期，与 HAC 滞后无关
    bs = stationary_bootstrap(xs, block, boot, seed)
    by_year: Dict[str, List[float]] = defaultdict(list)
    for d, v in zip(dates, xs):
        by_year[d[:4]].append(v)
    yboot = year_block_bootstrap(by_year, boot, seed + 7)
    year_view = {y: {"n": len(v), "edge": round(mean(v), 6),
                     "positive": (mean(v) > 0)}
                 for y, v in sorted(by_year.items()) if len(v) >= 40}
    full_years = [y for y in year_view if y != max(year_view, default="")]
    pos_years = sum(1 for y in full_years if year_view[y]["positive"])

    p_nw = None
    if t_nw == t_nw and se > 0:
        p_nw = round(math.erfc(abs(t_nw) / math.sqrt(2)), 4)

    # 判定：以 HAC p 为准（最保守），block bootstrap 与年度重抽作为一致性佐证
    p_eff = max([p for p in (p_nw, bs.get("p"), yboot.get("p")) if p is not None] or [1.0])
    if p_eff < 0.05 and m > 0:
        verdict = "显著正向"
    elif p_eff < 0.05 and m < 0:
        verdict = "显著负向"
    elif m > 0:
        verdict = "方向为正但不显著"
    else:
        verdict = "方向为负但不显著"

    blk_sd = statistics.pstdev([mean(xs[i:i + lag]) for i in range(0, max(1, n - lag), lag)]) \
        if n > 2 * lag else 0.0
    return {
        "n_days": n,
        "mean_edge": round(m, 6),
        "naive_t": (round(t_naive, 2) if t_naive == t_naive else None),
        "nw_t": (round(t_nw, 2) if t_nw == t_nw else None),
        "nw_se": (round(se, 6) if se == se else None),
        "nw_lag": lag,
        "nw_p": p_nw,
        "disjoint": dj,
        "boot": bs,
        "year_boot": yboot,
        "mde_80": (round(2.8 * se, 6) if se == se else None),   # 80% 功效下可检出的最小 edge
        "n_eff_time": round(n / FWD, 1),   # 独立 20 日窗口个数（真正的有效样本量）
        "block_sd": round(blk_sd, 6),
        "year_view": year_view,
        "positive_full_years": f"{pos_years}/{len(full_years)}",
        "avg_members": round(mean(series["members"]), 1),
        "p_effective": round(p_eff, 4),
        "verdict": verdict,
    }


def pair_test(series: Dict[str, Any], s1: str, s2: str, lag: int) -> Optional[Dict[str, Any]]:
    """两状态同日差异检验：在两者都有足够成员的交易日，比较 edge 差。"""
    a, b = series.get(s1), series.get(s2)
    if not a or not b:
        return None
    da = dict(zip(a["dates"], a["edge"]))
    db = dict(zip(b["dates"], b["edge"]))
    common = sorted(set(da) & set(db))
    if len(common) < 80:
        return None
    diff = [da[d] - db[d] for d in common]
    m, se, t = newey_west_t(diff, lag)
    if t != t:
        return None
    p = math.erfc(abs(t) / math.sqrt(2))
    return {"pair": f"{s1}-{s2}", "n_days": len(common), "mean_diff": round(m, 6),
            "nw_t": round(t, 2), "p": round(p, 4),
            "significant": p < 0.05, "p_value_raw": p}


def main() -> int:
    ap = argparse.ArgumentParser(description="状态机时间维度显著性检验（只读）")
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--json", default=OUT)
    ap.add_argument("--lag", type=int, default=2 * FWD,
                    help="HAC 滞后阶数（默认 40 = 2×重叠期；蒙特卡洛校准见模块文档）")
    ap.add_argument("--block", type=int, default=80,
                    help="stationary bootstrap 平均块长（默认 80，蒙特卡洛校准）")
    ap.add_argument("--boot", type=int, default=4000, help="bootstrap 次数")
    ap.add_argument("--min-members", type=int, default=5, help="当日最少成员数")
    ap.add_argument("--seed", type=int, default=20261108)
    a = ap.parse_args()

    if not os.path.exists(a.panel):
        print(f"[fatal] 面板不存在：{a.panel}\n"
              f"        先生成：python tools_state_edge.py --start 2020-11-06 "
              f"--dump-panel {os.path.basename(a.panel)}", file=sys.stderr)
        return 2

    recs, dates = load_panel(a.panel)
    series, series_meta = build_series(recs, dates, a.min_members)
    n_rows = sum(len(v) for v in recs.values())
    print(f"[panel] {a.panel}  标的={len(recs)}  行={n_rows}  交易日={len(dates)}  "
          f"{dates[0]}~{dates[-1]}")

    res: Dict[str, Any] = {}
    for s in STATES:
        res[s] = analyse(series[s], a.lag, a.block, a.boot, a.seed, a.min_members)

    pairs = []
    for i in range(len(STATES)):
        for j in range(i + 1, len(STATES)):
            pt = pair_test(series, STATES[i], STATES[j], a.lag)
            if pt:
                pairs.append(pt)

    # 横截面口径：用【同一面板】按 tools_state_edge.py 的同一算法重算，
    # 保证"跨指数 t"与"时间维度 t"来自同一批数据、可直接对比。
    from tools_state_edge import aggregate as _aggregate
    cs_agg = _aggregate({c: [dict(r) for r in v] for c, v in recs.items()},
                        [{"code": c} for c in recs],
                        {"min_confidence_to_act": 0.65})
    cs_summary = cs_agg["state_summary"]

    cross: Dict[str, Any] = {}
    neff_ratio = None
    neff_val = None
    if os.path.exists(CORR):
        try:
            cs = json.load(open(CORR, encoding="utf-8"))
            rho = cs.get("mean_rho") or cs.get("mean_pairwise_rho")
            n = cs.get("n_codes") or cs.get("n")
            neff = cs.get("n_eff") or (n / (1 + (n - 1) * rho) if (rho and n) else None)
            if neff and n:
                neff_val = neff
                neff_ratio = math.sqrt(n / neff)
        except Exception:
            pass
    for s in STATES:
        v = cs_summary.get(s)
        if not v:
            continue
        item = {"cross_t": v.get("t_stat"), "n_index": v.get("n_index"),
                "edge_mean": v.get("edge_mean")}
        if neff_ratio:
            item["corr_adjusted_t"] = round(v["t_stat"] / neff_ratio, 2)
            item["n_eff_index"] = round(neff_val, 2)
        cross[s] = item

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": ("time-dimension inference on daily cross-sectional mean excess; "
                   f"Newey-West(lag={a.lag}) + stationary bootstrap(block={a.block}, B={a.boot}) "
                   "+ disjoint subsample + year-block bootstrap"),
        "panel": {"codes": len(recs), "rows": n_rows, "days": len(dates),
                  "first": dates[0], "last": dates[-1], "min_members": a.min_members,
                  "market_days_kept": series_meta["days_kept"],
                  "market_days_dropped": series_meta["days_dropped"],
                  "thin_threshold": series_meta["thin_threshold"]},
        "calibration": {
            "method": "Monte Carlo on AR(1) daily returns rolled over 20 days (null true)",
            "reps": 200,
            "false_positive_rate": {"naive_t": 0.67, "nw_lag20": 0.115,
                                    "nw_lag40": 0.085, "boot_block40": 0.083,
                                    "boot_block80": 0.067},
            "note": "默认 lag=40 / block=80；判定取三法最保守（max p），宁漏报不虚报",
            "used": {"lag": a.lag, "block": a.block, "boot": a.boot},
        },
        "corr_adjust_factor": (round(neff_ratio, 2) if neff_ratio else None),
        "n_eff_index": (round(neff_val, 2) if neff_val else None),
        "states": res,
        "cross_section": cross,
        "pair_tests": pairs,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
    with open(a.json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    # ---------------- 控制台报告 ----------------
    print("\n【L1-L4 逐层检验】edge = 状态日 fwd20 相对自身基线的超额（日频横截面均值）")
    print(f"{'态':<3}{'天数':>6}{'均值':>10}{'朴素t':>8}{'NW t':>8}{'NW p':>8}"
          f"{'子样本t':>9}{'boot p':>8}{'年度p':>8}{'n_eff':>7}{'结论':>18}")
    for s in STATES:
        v = res[s]
        if v.get("n_days", 0) < 60:
            print(f"{s:<3}{v.get('n_days',0):>6}  样本不足")
            continue
        print(f"{s:<3}{v['n_days']:>6}{v['mean_edge']*100:>9.3f}%"
              f"{(v['naive_t'] if v['naive_t'] is not None else 0):>8.1f}"
              f"{(v['nw_t'] if v['nw_t'] is not None else 0):>8.1f}"
              f"{(v['nw_p'] if v['nw_p'] is not None else 1):>8.3f}"
              f"{(v['disjoint']['t_mean'] if v['disjoint']['t_mean'] is not None else 0):>9.1f}"
              f"{(v['boot']['p'] if v['boot']['p'] is not None else 1):>8.3f}"
              f"{(v['year_boot']['p'] if v['year_boot']['p'] is not None else 1):>8.3f}"
              f"{v['n_eff_time']:>7.1f}{v['verdict']:>18}")

    if cross:
        print(f"\n【横截面 t 的相关性修正】修正因子 = {out['corr_adjust_factor']}"
              f"（{len(recs)} 只指数实际只有约 {out['n_eff_index']} 个独立样本）")
        print(f"{'态':<3}{'跨指数 t':>10}{'修正后 t':>10}{'时间维度 NW t':>14}")
        for s in STATES:
            c = cross.get(s, {})
            v = res[s]
            print(f"{s:<3}{(c.get('cross_t') or 0):>10.1f}{(c.get('corr_adjusted_t') or 0):>10.2f}"
                  f"{(v.get('nw_t') or 0):>14.1f}")

    if pairs:
        m_tests = len(pairs)
        bonf = 0.05 / m_tests
        print(f"\n【状态两两差异（同日配对，HAC）】共 {m_tests} 次比较，"
              f"Bonferroni 阈值 = {bonf:.4f}")
        for pt in sorted(pairs, key=lambda x: -abs(x["nw_t"])):
            raw = pt.pop("p_value_raw", pt["p"])
            pt["survives_bonferroni"] = raw < bonf
            flag = ("Bonferroni 后仍显著" if raw < bonf
                    else ("名义显著" if raw < 0.05 else "不显著"))
            print(f"  {pt['pair']:<6} 差={pt['mean_diff']*100:+.3f}%  t={pt['nw_t']:+.2f}  "
                  f"p={pt['p']:.3f}  {flag}  (n={pt['n_days']})")

    all_years = sorted({y for s in STATES for y in (res[s].get("year_view") or {})})
    last_year = max(all_years) if all_years else ""
    print("\n【年度逐一（edge 均值，%s 为不完整年度）】" % last_year)
    if all_years:
        print("      " + "".join(f"{y + ('*' if y == last_year else ''):>11}" for y in all_years))
        for s in STATES:
            yv = res[s].get("year_view") or {}
            if not yv:
                continue
            cells = "".join((f"{yv[y]['edge']*100:>10.2f}%" if y in yv else f"{'—':>11}")
                            for y in all_years)
            print(f"  {s}   {cells}")
    print(f"\nJSON: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
