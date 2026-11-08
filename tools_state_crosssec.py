#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_state_crosssec.py —— 状态机的「横截面选优」检验（只读）

## 为什么换维度（以及为什么不是"更多样本"）

`tools_state_significance.py` 已证明：把 118 只指数的 fwd20 配对 edge 做跨指数平均，
得到的 t 值不可用——实测平均两两相关 0.57–0.61，有效独立样本 n_eff≈1.6–1.8，
t 被系统性放大 √(n/n_eff)≈8 倍。时间维度检验是对的，但它的样本受 fwd20 重叠硬约束：
1547 个交易日 ≈ 77 个独立 20 日窗口，MDE 在 1.5%–3.4%，比要检出的效应还大。

于是换一个【不同的问题】：

  时间维度问：处于状态 s 的标的，此后 20 日是否比**它自己的历史基线**更好？
             —— 答"是"意味着该状态标记了行情好坏，即择时 / beta 暴露。
  横截面问：  同一天里，处于状态 s 的标的，是否比**当天其他标的**更强？
             —— 答"是"意味着该状态能挑出赢家，即选优 / alpha。

这两个量不可互相推断。横截面口径用当日横截面去均值，把市场共同因子整个减掉
（PC1 解释 59–64%），单个观测的残差方差降到约 1−R²≈0.4 倍，MDE 相应降约 1.6 倍；
但独立样本量并没有变成"118×N"——每日只有一个横截面统计量，时间上的重叠依旧存在。
**真实收益是"每次观测更干净"，不是"观测更多"**，本工具按这个口径报告。

代价（本工具的一等公民）：若某状态是**市场级同步态**（当天几乎全员同态），
横截面上就不存在对照组，该状态的问题不可识别。这不是样本不够，是设计上无法回答。
B 态（突破）正是这种形态：每日成员中位数 1 只，1547 天里只有 301 天凑够 5 只。
因此 §可识别性 先行输出，再给结论——不允许"跑出一个 t 值就当成答案"。

## 五个估计量

  M1 相对 z     z=(f20−当日均值)/当日横截面标准差；γ_t(s)=状态 s 成员的当日 z 均值
  M2 多空价差   γ_t(s)=mean(f20|s) − mean(f20|¬s)，单位是收益率，可直接交易
  M3 FM 回归    z ~ 五个状态哑变量 + mom20 + vol20（当日横截面 OLS），逐日系数做
                Fama-MacBeth。目的：检验扣掉【动量】与【波动】之后是否还有增量信息。
                否则"A 态跑输"可能只是"低波股在上涨市里跑输"的伪影，而不是状态机的洞见。
  M4 排序 IC    每日 Spearman(置信度 k, z)。检验"同状态内分数越高越好"是否成立，
                这是把 k 从"闸门"升级为"权重"的前提。
  M5 置信度分位  把每日 k 切成 5 档，看各档 mean z 是否单调（描述性，不入 Bonferroni 族）。

## 推断

沿用已校准口径（蒙特卡洛见 tools_state_significance.py 模块文档）：
NW(lag=40) + stationary bootstrap(block=80) + 年度整块重抽，判定取三法最保守（max p）。
多重比较：每个估计量族内 5 个状态做 Bonferroni，阈值 0.01。
M1/M2/M3 三个族是同一问题的三种口径，**不**再把它们当独立证据叠加。

## 用法

  python tools_state_crosssec.py                            # 读 state/state_panel.jsonl
  python tools_state_crosssec.py --json state/state_crosssec.json
  python tools_state_crosssec.py --cn-only                  # 只用 A 股标的做横截面
  python tools_state_crosssec.py --calibrate --cal-reps 120 # 附带 size/power 校准

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from tools_state_significance import (          # noqa: E402  统计口径唯一真源
    mean, newey_west_t, naive_t, stationary_bootstrap, year_block_bootstrap,
    p_year_for_rule,
)

PANEL = os.path.join(BASE, "state", "state_panel.jsonl")
OUT = os.path.join(BASE, "state", "state_crosssec.json")
CORR = os.path.join(BASE, "data", "corr_structure.json")
CAL = os.path.join(BASE, "state", "calibration_crosssec.json")
CFG = os.path.join(BASE, "splus_config.yaml")

STATES = ["A", "B", "C", "D", "E"]
FWD = 20
STATE_CN = {"A": "吸筹", "B": "突破", "C": "通道", "D": "派发", "E": "下跌"}
PROBE = ("A", "C", "B")   # 校准探针：大态 / 中态 / 最稀疏态


# ------------------------------------------------------------------ 基础统计
def sd(xs: Sequence[float]) -> float:
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def ranks(xs: Sequence[float]) -> List[float]:
    """平均秩（处理并列）。Spearman 用。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            out[order[t]] = avg
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 8:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def ols(y: Sequence[float], X: Sequence[Sequence[float]]) -> Optional[List[float]]:
    """正规方程 + 高斯消元（带部分主元）。X 不含截距，奇异返回 None。

    只有 7 个回归元，纯 stdlib 足够；不引入 numpy 以维持项目零外部依赖。
    """
    n, k = len(X), len(X[0])
    if n <= k + 2:
        return None
    A = [[0.0] * (k + 1) for _ in range(k)]
    for a in range(k):
        for b in range(k):
            A[a][b] = sum(X[i][a] * X[i][b] for i in range(n))
        A[a][k] = sum(X[i][a] * y[i] for i in range(n))
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-10:
            return None
        A[col], A[piv] = A[piv], A[col]
        pv = A[col][col]
        for r in range(col + 1, k):
            f = A[r][col] / pv
            if f == 0.0:
                continue
            for c2 in range(col, k + 1):
                A[r][c2] -= f * A[col][c2]
    beta = [0.0] * k
    for r in range(k - 1, -1, -1):
        s = A[r][k] - sum(A[r][c2] * beta[c2] for c2 in range(r + 1, k))
        beta[r] = s / A[r][r]
    return beta


# ------------------------------------------------------------------ 数据装配
def load_cfg() -> Dict[str, Any]:
    try:
        import yaml
        with open(CFG, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def cache_dir() -> str:
    cfg = load_cfg()
    rel = (cfg.get("data") or {}).get("cache_dir") or "../stock-pool/_cache"
    return os.path.normpath(os.path.join(BASE, rel))


def market_of(code: str) -> str:
    if code.startswith("hk"):
        return "HK"
    if code.startswith("us"):
        return "US"
    return "CN"


def load_features(cdir: str, codes: Sequence[str]) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """(code, date) -> (mom20, vol20)。

    控制变量必须来自与面板同一条 load_bars 路径，否则会引入第二套量纲口径——
    这正是 data_contract 要消灭的东西。
    """
    from engine import load_bars
    out: Dict[Tuple[str, str], Tuple[float, float]] = {}
    miss = 0
    for c in codes:
        bars = load_bars(cdir, c)
        if len(bars) < 30:
            miss += 1
            continue
        dates = [str(b["date"])[:10] for b in bars]
        close = [float(b["close"]) for b in bars]
        rets = [0.0] + [close[i] / close[i - 1] - 1.0 if close[i - 1] > 0 else 0.0
                        for i in range(1, len(close))]
        for i in range(FWD, len(bars)):
            c0 = close[i - FWD]
            if c0 <= 0:
                continue
            out[(c, dates[i])] = (close[i] / c0 - 1.0, sd(rets[i - FWD + 1:i + 1]) * math.sqrt(252.0))
    if miss:
        print(f"[warn] {miss} 只标的行情不足 30 根，控制变量缺席", file=sys.stderr)
    return out


def load_panel(path: str) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """读面板，保留 k（置信度）——M4 需要它，而 tools_state_significance 的读法丢掉了。"""
    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    all_dates = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            all_dates.add(o["d"])
            if "f" not in o:
                continue
            by_date[o["d"]].append({"c": o["c"], "s": o["s"], "k": float(o.get("k") or 0.0),
                                    "f": float(o["f"])})
    return dict(by_date), sorted(all_dates)


def build_days(by_date: Dict[str, List[Dict[str, Any]]],
               feat: Dict[Tuple[str, str], Tuple[float, float]],
               min_codes: int, cn_only: bool) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把面板压成"每日一个横截面"。

    非交易日剔除沿用旧口径：当日成员数 < max(min_codes, 30%×标的数) 视为非交易日
    （12 只境外标的在 A 股休市日仍有数据，不过滤会用它们污染 A 股状态的横截面）。
    """
    n_codes = len({r["c"] for rows in by_date.values() for r in rows})
    thin = max(min_codes, int(0.30 * n_codes))
    days: List[Dict[str, Any]] = []
    dropped_thin = dropped_flat = 0
    for d in sorted(by_date):
        rows = [r for r in by_date[d] if (not cn_only or market_of(r["c"]) == "CN")]
        if len(rows) < thin:
            dropped_thin += 1
            continue
        fs = [r["f"] for r in rows]
        s = sd(fs)
        if s <= 0:
            dropped_flat += 1
            continue
        m = mean(fs)
        for r in rows:
            r["z"] = (r["f"] - m) / s
            mo, vo = feat.get((r["c"], d), (None, None))
            r["mom"], r["vol"] = mo, vo
        # 控制变量横截面标准化（只对有值的行）
        for key in ("mom", "vol"):
            vals = [r[key] for r in rows if r[key] is not None]
            if len(vals) >= 0.8 * len(rows):
                mm, ss = mean(vals), sd(vals)
                for r in rows:
                    r["z_" + key] = ((r[key] - mm) / ss) if (r[key] is not None and ss > 0) else None
            else:
                for r in rows:
                    r["z_" + key] = None
        days.append({"d": d, "rows": rows, "n": len(rows), "sd_f": s, "mean_f": m})
    meta = {"codes": n_codes, "thin_threshold": thin,
            "days_kept": len(days), "days_dropped_thin": dropped_thin,
            "days_dropped_flat": dropped_flat}
    return days, meta


# ------------------------------------------------------------------ 可识别性
def identification(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    """横截面能不能回答这个问题——先答这个，再谈显著性。"""
    hhi, share = [], {s: [] for s in STATES}
    for day in days:
        cnt = defaultdict(int)
        for r in day["rows"]:
            cnt[r["s"]] += 1
        n = day["n"]
        hhi.append(sum((v / n) ** 2 for v in cnt.values()))
        for s in STATES:
            share[s].append(cnt.get(s, 0) / n)
    per: Dict[str, Any] = {}
    for s in STATES:
        v = share[s]
        per[s] = {
            "share_median": round(statistics.median(v), 4),
            "days_ge5": sum(1 for x in v if x * 110 >= 5),
            "days_no_control": sum(1 for x in v if x >= 0.90),      # 当天几乎全员同态
            "days_informative": sum(1 for x in v if 0.15 <= x <= 0.85),
        }
    return {
        "hhi_median": round(statistics.median(hhi), 4),
        "hhi_mean": round(mean(hhi), 4),
        "days": len(days),
        "states": per,
        "note": ("share 的分布决定该状态在横截面上是否可识别："
                 "days_no_control 高的状态，横截面检验没有对照组，结论为空不代表无效"),
    }


# ------------------------------------------------------------------ 各估计量
def m1_relative_z(days: List[Dict[str, Any]], min_members: int) -> Dict[str, Dict[str, Any]]:
    ser = {s: ([], []) for s in STATES}   # (dates, values)
    for day in days:
        bucket = defaultdict(list)
        for r in day["rows"]:
            bucket[r["s"]].append(r["z"])
        for s in STATES:
            v = bucket.get(s) or []
            if len(v) >= min_members:
                ser[s][0].append(day["d"])
                ser[s][1].append(mean(v))
    return {s: {"dates": ser[s][0], "vals": ser[s][1]} for s in STATES}


def m2_spread(days: List[Dict[str, Any]], min_members: int, min_control: int = 10) -> Dict[str, Dict[str, Any]]:
    ser = {s: ([], []) for s in STATES}
    for day in days:
        ins, outs = defaultdict(list), defaultdict(list)
        for r in day["rows"]:
            ins[r["s"]].append(r["f"])
        allf = [r["f"] for r in day["rows"]]
        for s in STATES:
            a = ins.get(s) or []
            if len(a) < min_members or len(allf) - len(a) < min_control:
                continue
            rest = [r["f"] for r in day["rows"] if r["s"] != s]
            ser[s][0].append(day["d"])
            ser[s][1].append(mean(a) - mean(rest))
    return {s: {"dates": ser[s][0], "vals": ser[s][1]} for s in STATES}


def m3_controlled(days: List[Dict[str, Any]], min_members: int, min_control: int = 10,
                  controls: bool = True) -> Dict[str, Any]:
    """逐状态横截面回归，系数序列做 Fama-MacBeth。

        z_i = α + β_s·1[i∈s] + γ1·z_mom_i + γ2·z_vol_i + ε_i

    β_s = "该状态相对同日其余标的的 z 差，扣掉动量与波动之后"。
    每个状态各自抽样（只要当天该状态 ≥min_members 且对照组 ≥min_control），
    不要求五态齐备——否则 B 态（每日中位数 1 只）会把整个样本压到 73 天，
    让 M3 变成"只在那 73 天成立"的伪结论。

    为什么不把五个哑变量放进同一个回归：那样每个系数都是"相对某个基准态"，
    基准一换全部系数要重算，而 B 态的稀疏会让大多数日子直接被 OLS 判奇异。
    逐态 γ 与 M1/M2 的抽样口径完全一致，三者可直接对照。
    """
    keys = STATES
    coef = {k: [] for k in keys}
    cdates = {k: [] for k in keys}
    ratio = {k: [] for k in keys}   # (n−n_s)/n，用于与 M1 做恒等式校验
    skipped = 0
    for day in days:
        rows = day["rows"]
        if controls:
            rows = [r for r in rows if r.get("z_mom") is not None and r.get("z_vol") is not None]
        if len(rows) < 60:
            skipped += 1
            continue
        for s in STATES:
            n_s = sum(1 for r in rows if r["s"] == s)
            if n_s < min_members or len(rows) - n_s < min_control:
                continue
            y = [r["z"] for r in rows]
            X = []
            for r in rows:
                row = [1.0, 1.0 if r["s"] == s else 0.0]
                if controls:
                    row += [r["z_mom"], r["z_vol"]]
                X.append(row)
            b = ols(y, X)
            if b is None:
                continue
            coef[s].append(b[1])
            cdates[s].append(day["d"])
            ratio[s].append((len(rows) - n_s) / len(rows))
    return {"coef": coef, "dates": cdates, "ratio": ratio, "skipped_days": skipped}


def m3_controls_marginal(days: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """控制变量自身的横截面溢价（不区分状态）：z ~ α + γ1·z_mom + γ2·z_vol。

    这是解释 M1/M2 的钥匙：若 γ_mom 显著为正，说明"A 态跑输"有可能只是
    "低动量跑输"的另一种说法，状态机并没有独立信息。
    """
    out = {k: {"dates": [], "vals": []} for k in ("mom", "vol")}
    for day in days:
        rows = [r for r in day["rows"]
                if r.get("z_mom") is not None and r.get("z_vol") is not None]
        if len(rows) < 60:
            continue
        y = [r["z"] for r in rows]
        X = [[1.0, r["z_mom"], r["z_vol"]] for r in rows]
        b = ols(y, X)
        if b is None:
            continue
        out["mom"]["dates"].append(day["d"])
        out["mom"]["vals"].append(b[1])
        out["vol"]["dates"].append(day["d"])
        out["vol"]["vals"].append(b[2])
    return out


def m4_ic(days: List[Dict[str, Any]], min_members: int) -> Dict[str, Any]:
    """每日 Spearman(k, z)：整体 + 状态内。"""
    all_ic, all_dates = [], []
    within = {s: ([], []) for s in STATES}
    for day in days:
        rows = day["rows"]
        if len(rows) < 20:
            continue
        ic = spearman([r["k"] for r in rows], [r["z"] for r in rows])
        if ic is not None:
            all_ic.append(ic)
            all_dates.append(day["d"])
        for s in STATES:
            sub = [r for r in rows if r["s"] == s]
            if len(sub) >= min_members + 3:
                ic2 = spearman([r["k"] for r in sub], [r["z"] for r in sub])
                if ic2 is not None:
                    within[s][0].append(day["d"])
                    within[s][1].append(ic2)
    return {"all": {"dates": all_dates, "vals": all_ic},
            "within": {s: {"dates": within[s][0], "vals": within[s][1]} for s in STATES}}


def m5_k_quintile(days: List[Dict[str, Any]], q: int = 5) -> Dict[str, Any]:
    """把每日 k 切 q 档，看各档 mean z 是否单调。返回每档的日频序列（可做检验）。"""
    series = {i: ([], []) for i in range(q)}
    for day in days:
        rows = sorted(day["rows"], key=lambda r: r["k"])
        n = len(rows)
        if n < 25:
            continue
        for i in range(q):
            lo, hi = int(i * n / q), int((i + 1) * n / q)
            seg = rows[lo:hi]
            if len(seg) >= 3:
                series[i][0].append(day["d"])
                series[i][1].append(mean([r["z"] for r in seg]))
    return {i: {"dates": series[i][0], "vals": series[i][1]} for i in range(q)}


def duration_stats(by_date: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """状态持续期（交易日）。横截面信号若每天换边就不可实施，必须看这个。"""
    by_code: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for d, rows in by_date.items():
        for r in rows:
            by_code[r["c"]].append((d, r["s"]))
    runs: Dict[str, List[int]] = defaultdict(list)
    for c, seq in by_code.items():
        seq.sort()
        cur, n = None, 0
        for _, s in seq:
            if s == cur:
                n += 1
            else:
                if cur is not None and n > 0:
                    runs[cur].append(n)
                cur, n = s, 1
        if cur is not None:
            runs[cur].append(n)
    return {s: {"n_runs": len(runs[s]),
                "mean": round(mean(runs[s]), 1) if runs[s] else None,
                "median": (round(statistics.median(runs[s]), 1) if runs[s] else None)}
            for s in STATES}


# ------------------------------------------------------------------ 推断
def infer(series: Dict[str, Any], lag: int, block: int, boot: int, seed: int,
          label: str = "") -> Dict[str, Any]:
    xs, dates = series["vals"], series["dates"]
    n = len(xs)
    if n < 60:
        return {"n_days": n, "verdict": "样本不足", "mean": round(mean(xs), 6) if xs else None}
    m, se, t_nw = newey_west_t(xs, lag)
    bs = stationary_bootstrap(xs, block, boot, seed)
    by_year: Dict[str, List[float]] = defaultdict(list)
    for d, v in zip(dates, xs):
        by_year[d[:4]].append(v)
    yb = year_block_bootstrap(by_year, boot, seed + 7)
    p_nw = round(math.erfc(abs(t_nw) / math.sqrt(2)), 4) if (t_nw == t_nw and se > 0) else None
    py = p_year_for_rule(yb)     # 整年块 < 6 时不参与判定（零功效分量不进 max）
    p_eff = max([p for p in (p_nw, bs.get("p"), py) if p is not None] or [1.0])
    year_view = {y: {"n": len(v), "mean": round(mean(v), 6)}
                 for y, v in sorted(by_year.items()) if len(v) >= 40}
    full = [y for y in year_view if y != max(year_view, default="")]
    pos = sum(1 for y in full if year_view[y]["mean"] > 0)
    if p_eff < 0.05:
        verdict = "显著正向" if m > 0 else "显著负向"
    else:
        verdict = "方向为正但不显著" if m > 0 else "方向为负但不显著"
    return {
        "n_days": n,
        "mean": round(m, 6),
        "sd_series": round(sd(xs), 6),
        "nw_t": (round(t_nw, 2) if t_nw == t_nw else None),
        "nw_se": (round(se, 6) if se == se else None),
        "nw_p": p_nw,
        "naive_t": (round(naive_t(xs), 2) if naive_t(xs) == naive_t(xs) else None),
        "boot_p": bs.get("p"),
        "boot_ci": bs.get("ci"),
        "year_p": yb.get("p"),
        "year_blocks": yb.get("years"),
        "year_p_in_rule": py is not None,
        "p_effective": round(p_eff, 4),
        "mde_80": (round(2.8 * se, 6) if se == se else None),
        "year_view": year_view,
        "positive_years": f"{pos}/{len(full)}",
        "verdict": verdict,
    }


# ------------------------------------------------------------------ 校准
def calibrate(reps: int, days_n: int, n_assets: int, seed: int, lag: int) -> Dict[str, Any]:
    """蒙特卡洛：横截面口径的 size 与 power。

    生成过程复刻三个真实结构：
      1. 共同因子 + 3 个板块因子 + 特质噪声（横截面相关 ≈0.6）
      2. 状态由粘性马尔可夫链产生，与未来收益**独立**（零假设为真）
      3. f20 是 20 天日收益的滚动和（重叠结构完整保留）
    state 的平稳分布取面板实测占比，因此"A 态占四成"这种构成也一并复刻。
    若 size 明显高于名义 5%，说明该统计量在横截面口径下也不能用。
    """
    rng = random.Random(seed)
    shares = {"A": 0.41, "B": 0.032, "C": 0.17, "D": 0.157, "E": 0.215}
    names = list(shares)
    prob = [shares[k] for k in names]
    # 每个资产的 beta / 波动 / 板块
    beta = [rng.uniform(0.6, 1.4) for _ in range(n_assets)]
    sig = [rng.uniform(0.008, 0.018) for _ in range(n_assets)]
    sec = [rng.randrange(3) for _ in range(n_assets)]

    def one_rep(alpha: float, target: str, rng_seed_rep: int = 0) -> Dict[str, Any]:
        # 1) 状态链：粘性 0.9，离开时按平稳分布落点
        st = [[None] * n_assets for _ in range(days_n)]
        cur = [rng.choices(names, prob)[0] for _ in range(n_assets)]
        for t in range(days_n):
            for i in range(n_assets):
                if rng.random() > 0.9:
                    cur[i] = rng.choices(names, prob)[0]
                st[t][i] = cur[i]
        # 2) 日收益
        R = [[0.0] * n_assets for _ in range(days_n)]
        for t in range(days_n):
            mkt = rng.gauss(0, 0.010)
            f3 = [rng.gauss(0, 0.006) for _ in range(3)]
            for i in range(n_assets):
                r = beta[i] * mkt + f3[sec[i]] + rng.gauss(0, sig[i])
                if alpha and st[t][i] == target:
                    r += alpha
                R[t][i] = r
        # 3) f20 = 未来 20 日累计（重叠）
        out = {tag: [] for tag in PROBE}
        for t in range(days_n - FWD):
            fs = [sum(R[t + 1 + k][i] for k in range(FWD)) for i in range(n_assets)]
            m, s = mean(fs), sd(fs)
            if s <= 0:
                continue
            for tag in PROBE:
                idx = [i for i in range(n_assets) if st[t][i] == tag]
                if len(idx) >= 5:
                    out[tag].append(mean([(fs[i] - m) / s for i in idx]))
        if target not in out:
            out[target] = []
        res = {}
        for tag in PROBE:
            xs = out.get(tag) or []
            if len(xs) < 200:
                res[tag] = (0.0, 1.0)
                continue
            _, se, t = newey_west_t(xs, lag)
            t = t if t == t else 0.0
            p_nw = math.erfc(abs(t) / math.sqrt(2))
            # 判定规则用的是 p_eff = max(NW, boot)，所以要一并测规则本身的 size，
            # 而不是只测 NW 分量。boot 次数取 200（校准预算内），块长沿用默认 80。
            bs = stationary_bootstrap(xs, 80, 200, rng_seed_rep)
            p_eff = max(p_nw, bs.get("p") if bs.get("p") is not None else 1.0)
            res[tag] = (t, p_eff)
        return res

    size = []
    for r in range(reps):
        size.append(one_rep(0.0, "C", rng_seed_rep=r + 1))
    out_size = {}
    out_rule = {}
    for tag in PROBE:
        out_size[tag] = round(sum(1 for v in size if abs(v[tag][0]) > 1.96) / max(1, len(size)), 3)
        out_rule[tag] = round(sum(1 for v in size if v[tag][1] < 0.05) / max(1, len(size)), 3)
    out_size["any_of_three"] = round(
        sum(1 for v in size if max(abs(v[t][0]) for t in PROBE) > 1.96) / max(1, len(size)), 3)
    out_rule["any_of_three"] = round(
        sum(1 for v in size if min(v[t][1] for t in PROBE) < 0.05) / max(1, len(size)), 3)

    # power：把 alpha 折成"20 日相对横截面 SD 的 β 倍"
    cs_sd20 = 0.010 * math.sqrt(20) * 1.3     # 经验量级
    power = {}
    for b in (0.05, 0.10):
        alpha = b * cs_sd20 / FWD
        hits, hits_rule = [], []
        for r in range(max(30, reps // 2)):
            res = one_rep(alpha, "C", rng_seed_rep=5000 + r)
            hits.append(abs(res["C"][0]) > 1.96)
            hits_rule.append(res["C"][1] < 0.05)
        power[f"beta_{b:.2f}_sd"] = round(sum(hits) / len(hits), 3)
        power[f"beta_{b:.2f}_sd_rule"] = round(sum(hits_rule) / len(hits_rule), 3)

    return {
        "method": "MC: common factor + 3 sector factors + idio; sticky Markov states independent of returns; overlapping fwd20",
        "reps": reps, "days": days_n, "assets": n_assets, "lag": lag,
        "size_at_5pct": out_size,
        "size_at_5pct_rule": out_rule,
        "power": power,
        "note": ("size 目标 ≤0.08（沿用上一轮 6.5–8.5% 的标准）；"
                 "size_at_5pct 是 NW(40) 单分量，size_at_5pct_rule 是实际判定用的 "
                 "p_eff=max(NW, boot) 规则；power 为名义 5% 双侧的检出率"),
    }


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="状态机横截面选优能力检验（只读）")
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--json", default=OUT)
    ap.add_argument("--min-members", type=int, default=5, help="状态当日最少成员数")
    ap.add_argument("--min-codes", type=int, default=40, help="当日最少标的数（判交易日）")
    ap.add_argument("--lag", type=int, default=2 * FWD)
    ap.add_argument("--block", type=int, default=80)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--cn-only", action="store_true", help="横截面只用 A 股标的")
    ap.add_argument("--calibrate", action="store_true",
                    help="重算蒙特卡洛 size/power 并写入 state/calibration_crosssec.json")
    ap.add_argument("--calibrate-only", action="store_true", help="只算校准，不跑主分析")
    ap.add_argument("--cal-reps", type=int, default=120)
    ap.add_argument("--cal-days", type=int, default=900)
    a = ap.parse_args()

    # 校准独立于面板，且是分钟级的——先做，做完可 --calibrate-only 直接退出。
    cal = None
    if a.calibrate or a.calibrate_only:
        cal = calibrate(a.cal_reps, a.cal_days, 118, a.seed + 99, a.lag)
        os.makedirs(os.path.dirname(os.path.abspath(CAL)), exist_ok=True)
        with open(CAL, "w", encoding="utf-8") as fh:
            json.dump(cal, fh, ensure_ascii=False, indent=1)
        print(f"[cal] 已写入 {CAL}  size={cal['size_at_5pct']}")
        if a.calibrate_only:
            return 0
    elif os.path.exists(CAL):
        try:
            cal = json.load(open(CAL, encoding="utf-8"))
            print(f"[cal] 复用 {os.path.basename(CAL)}（size "
                  f"{cal.get('size_at_5pct')}）；重算用 --calibrate")
        except Exception:
            cal = None

    if not os.path.exists(a.panel):
        print(f"[fatal] 面板不存在：{a.panel}\n"
              f"        先生成：python tools_state_edge.py --start 2020-11-06 "
              f"--dump-panel state/state_panel.jsonl", file=sys.stderr)
        return 2

    by_date, all_dates = load_panel(a.panel)
    codes = sorted({r["c"] for rows in by_date.values() for r in rows})
    print(f"[panel] {a.panel}  标的={len(codes)}  交易日={len(all_dates)}  "
          f"{all_dates[0]}~{all_dates[-1]}  口径={'A股' if a.cn_only else '全市场'}")

    feat = load_features(cache_dir(), codes)
    days, dmeta = build_days(by_date, feat, a.min_codes, a.cn_only)
    print(f"[days] 保留 {dmeta['days_kept']} 天（剔除稀疏 {dmeta['days_dropped_thin']}、"
          f"零离散 {dmeta['days_dropped_flat']}），当日成员中位数 "
          f"{statistics.median([d['n'] for d in days]):.0f}")

    # 可识别性（先答"能不能问"）
    ident = identification(days)
    ident["duration"] = duration_stats(by_date)
    print(f"\n【可识别性】每日 HHI 中位数 {ident['hhi_median']:.3f}"
          f"（1.0 = 全员同态，无对照组）")
    print(f"{'态':<4}{'CN':<6}{'中位占比':>9}{'→无对照天':>10}{'信息天':>8}{'持续期':>8}")
    for s in STATES:
        p = ident["states"][s]
        dur = ident["duration"][s]["mean"] or 0
        print(f"{s:<4}{STATE_CN[s]:<5}{p['share_median']*100:>8.1f}%"
              f"{p['days_no_control']:>10}{p['days_informative']:>8}{dur:>7.1f}d")

    # ---------------- 五个估计量
    s1 = m1_relative_z(days, a.min_members)
    s2 = m2_spread(days, a.min_members)
    s3 = m3_controlled(days, a.min_members, controls=True)
    s3n = m3_controlled(days, a.min_members, controls=False)
    s3c = m3_controls_marginal(days)
    s4 = m4_ic(days, a.min_members)
    s5 = m5_k_quintile(days)

    res1 = {s: infer(s1[s], a.lag, a.block, a.boot, a.seed + i) for i, s in enumerate(STATES)}
    res2 = {s: infer(s2[s], a.lag, a.block, a.boot, a.seed + 10 + i) for i, s in enumerate(STATES)}
    res3 = {s: infer({"dates": s3["dates"][s], "vals": s3["coef"][s]},
                     a.lag, a.block, a.boot, a.seed + 20 + i) for i, s in enumerate(STATES)}
    # β 是"相对控制组的 z 差"，与 M1 的"状态成员均值"不同尺度：
    #   β = mean_s·n/(n−n_s)，故 β·(n−n_s)/n 才是可与 M1 并排比较的同一量。
    res3r = {s: infer({"dates": s3["dates"][s],
                       "vals": [c * rt for c, rt in zip(s3["coef"][s], s3["ratio"][s])]},
                      a.lag, a.block, a.boot, a.seed + 25 + i) for i, s in enumerate(STATES)}
    ctrl3 = {k: infer(s3c[k], a.lag, a.block, a.boot, a.seed + 30 + i)
             for i, k in enumerate(("mom", "vol"))}
    res4a = infer(s4["all"], a.lag, a.block, a.boot, a.seed + 40)
    res4w = {s: infer(s4["within"][s], a.lag, a.block, a.boot, a.seed + 50 + i)
             for i, s in enumerate(STATES)}
    res5 = {str(i): infer(s5[i], a.lag, a.block, a.boot, a.seed + 60 + i) for i in s5}

    # 衰减分解：在同一批日期上比较 M1 与控制后折算值，回答"多少是风格暴露、多少是状态独有"
    attenuation: Dict[str, Any] = {}
    for s in STATES:
        d1 = dict(zip(s1[s]["dates"], s1[s]["vals"]))
        pairs = [(d1[d], c * rt) for d, c, rt in
                 zip(s3["dates"][s], s3["coef"][s], s3["ratio"][s]) if d in d1]
        if len(pairs) < 60:
            continue
        raw = mean([p[0] for p in pairs])
        res = mean([p[1] for p in pairs])
        attenuation[s] = {
            "n_days": len(pairs),
            "m1_mean": round(raw, 6),
            "controlled_mean": round(res, 6),
            "retention": (round(res / raw, 3) if abs(raw) > 1e-9 else None),
            "sign_flip": bool(abs(raw) > 1e-9 and res * raw < 0),
        }

    # 恒等式：无控制下 β_s = mean_s·n/(n−n_s)，故 β_s·(n−n_s)/n 必须逐日等于 M1。
    # 这不是独立证据，是两套代码路径的一致性护栏——M1 与 M3 算错一个，这里立刻炸。
    max_gap = 0.0
    for s in STATES:
        m1d = dict(zip(s1[s]["dates"], s1[s]["vals"]))
        for d, b, rt in zip(s3n["dates"][s], s3n["coef"][s], s3n["ratio"][s]):
            if d in m1d:
                max_gap = max(max_gap, abs(m1d[d] - b * rt))
    if max_gap > 1e-9:
        print(f"[warn] M1 与无控制 M3 的恒等式不成立（最大偏差 {max_gap:.2e}）", file=sys.stderr)

    bonf = 0.05 / len(STATES)

    def flag(p: Optional[float]) -> str:
        if p is None:
            return "—"
        if p < bonf:
            return "Bonferroni 后显著"
        if p < 0.05:
            return "名义显著（未过 Bonferroni）"
        return "不显著"

    # ---------------- 报告
    print(f"\n【M1 相对 z】同一天内，状态成员的 f20 相对全体偏离（单位：横截面标准差）")
    print(f"{'态':<4}{'天数':>6}{'均值z':>8}{'NW t':>8}{'NW p':>8}{'boot p':>8}"
          f"{'年度p':>8}{'MDE':>7}{'正年':>7}  判定")
    for s in STATES:
        v = res1[s]
        if v.get("n_days", 0) < 60:
            print(f"{s:<4}{v.get('n_days',0):>6}  样本不足")
            continue
        print(f"{s:<4}{v['n_days']:>6}{v['mean']:>8.4f}{(v['nw_t'] or 0):>8.2f}"
              f"{(v['nw_p'] if v['nw_p'] is not None else 1):>8.3f}"
              f"{(v['boot_p'] if v['boot_p'] is not None else 1):>8.3f}"
              f"{(v['year_p'] if v['year_p'] is not None else 1):>8.3f}"
              f"{v['mde_80']:>7.3f}{v['positive_years']:>7}  {flag(v['p_effective'])}")

    print(f"\n【M2 多空价差】状态成员 − 同日其余标的（可交易口径）")
    print(f"{'态':<4}{'天数':>6}{'价差':>9}{'年化':>8}{'NW t':>8}{'p_eff':>8}  判定")
    for s in STATES:
        v = res2[s]
        if v.get("n_days", 0) < 60:
            print(f"{s:<4}{v.get('n_days',0):>6}  样本不足")
            continue
        print(f"{s:<4}{v['n_days']:>6}{v['mean']*100:>8.3f}%{v['mean']*12.6*100:>7.1f}%"
              f"{(v['nw_t'] or 0):>8.2f}{v['p_effective']:>8.3f}  {flag(v['p_effective'])}")

    print(f"\n【M3 Fama-MacBeth（含 mom20 / vol20 控制）】扣掉动量与波动后还剩多少")
    print(f"{'态':<4}{'天数':>6}{'β(原)':>9}{'折算后':>9}{'M1原值':>9}{'残留':>8}"
          f"{'控制后NW t':>11}{'p_eff':>8}")
    for s in STATES:
        v, vr = res3[s], res3r[s]
        if v.get("n_days", 0) < 60:
            print(f"{s:<4}{v.get('n_days',0):>6}  样本不足")
            continue
        a_ = attenuation.get(s, {})
        rt = a_.get("retention")
        tag = ("反转" if a_.get("sign_flip") else (f"{rt*100:.0f}%" if rt is not None else "—"))
        print(f"{s:<4}{v['n_days']:>6}{v['mean']:>9.4f}{vr['mean']:>9.4f}"
              f"{(a_.get('m1_mean') or 0):>9.4f}{tag:>8}"
              f"{(vr['nw_t'] or 0):>11.2f}{vr['p_effective']:>8.3f}")
    for k in ("mom", "vol"):
        v = ctrl3[k]
        print(f"{k:<4}{v['n_days']:>6}{v['mean']:>9.4f}{'':>9}{'':>9}{'':>8}"
              f"{(v['nw_t'] or 0):>11.2f}{v['p_effective']:>8.3f}   ← 控制变量自身溢价")

    print(f"\n【M4 排序 IC】每日 Spearman(置信度 k, 相对 z)")
    print(f"  全体  n={res4a.get('n_days')}  均值IC={res4a.get('mean'):+.4f}  "
          f"NW t={(res4a.get('nw_t') or 0):+.2f}  p_eff={res4a.get('p_effective')}  "
          f"{flag(res4a.get('p_effective'))}")
    for s in STATES:
        v = res4w[s]
        if v.get("n_days", 0) < 60:
            continue
        print(f"  {s} 内  n={v['n_days']:>4}  均值IC={v['mean']:+.4f}  "
              f"NW t={(v['nw_t'] or 0):+.2f}  p_eff={v['p_effective']}")

    print(f"\n【M5 置信度五分位 → 平均相对 z】（描述性，不入 Bonferroni 族）")
    cells = []
    for i in range(5):
        v = res5[str(i)]
        cells.append(f"Q{i+1} {(v.get('mean') or 0):+.3f}" if v.get("n_days", 0) >= 60 else f"Q{i+1}  —")
    print("  " + "   ".join(cells))
    spread = (res5["4"].get("mean") or 0) - (res5["0"].get("mean") or 0)
    dq5 = dict(zip(s5[4]["dates"], s5[4]["vals"]))
    dq1 = dict(zip(s5[0]["dates"], s5[0]["vals"]))
    common = sorted(set(dq5) & set(dq1))
    vsp = infer({"dates": [d for d in common], "vals": [dq5[d] - dq1[d] for d in common]},
                a.lag, a.block, a.boot, a.seed + 70)
    print(f"  Q5−Q1 = {spread:+.4f}  NW t={(vsp.get('nw_t') or 0):+.2f}  "
          f"p_eff={vsp.get('p_effective')}  MDE={vsp.get('mde_80')}")

    # 方差拆解：横截面口径到底降了多少噪声
    mkt_sd = sd([d["mean_f"] for d in days])
    spread_sd = sd(s2["A"]["vals"]) if s2["A"]["vals"] else 0.0
    var_ratio = (spread_sd / mkt_sd) ** 2 if mkt_sd > 0 else None
    print(f"\n【噪声拆解】f20 市场均值的时间序列 SD = {mkt_sd*100:.2f}%；"
          f"A 态价差序列 SD = {spread_sd*100:.2f}%"
          + (f"  →  残差方差降至 {var_ratio:.2f} 倍" if var_ratio else ""))

    if cal:
        print("\n[calibrate] size @5%："
              + "  ".join(f"{k}={v}" for k, v in cal.get("size_at_5pct", {}).items())
              + f"   (reps={cal.get('reps')}, days={cal.get('days')}, lag={cal.get('lag')})")
        print("  power：" + "  ".join(f"{k}={v}" for k, v in cal.get("power", {}).items()))

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": ("cross-sectional (same-day) inference: date-demeaned relative fwd20; "
                   f"Newey-West(lag={a.lag}) + stationary bootstrap(block={a.block}, B={a.boot}) "
                   "+ year-block bootstrap; Bonferroni within family of 5"),
        "scope": {"universe": "CN only" if a.cn_only else "all markets",
                  "codes": len(codes), "min_members": a.min_members,
                  "bonferroni_threshold": round(bonf, 4), **dmeta},
        "identification": ident,
        "m1_relative_z": res1,
        "m2_spread": res2,
        "m3_controlled": res3,
        "m3_controlled_rescaled": res3r,
        "m3_attenuation": attenuation,
        "m3_controls_marginal": ctrl3,
        "m3_consistency_gap": max_gap,
        "m4_ic": {"all": res4a, "within": res4w},
        "m5_k_quintile": res5,
        "m5_q5_minus_q1": vsp,
        "noise_decomposition": {"sd_market_f20": round(mkt_sd, 6),
                                "sd_a_spread": round(spread_sd, 6),
                                "variance_ratio": (round(var_ratio, 4) if var_ratio else None)},
        "calibration": cal,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
    with open(a.json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\nJSON: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
