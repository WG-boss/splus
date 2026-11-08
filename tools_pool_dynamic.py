# -*- coding: utf-8 -*-
"""tools_pool_dynamic.py — 可交易标的池：现状诊断 + 动态规则对比

回答三个问题：
  1. 现在的「11 只可交易」是怎么来的？它是选池结果，还是映射副产物？
  2. 「近 3 个交易日五态评分前 10」能不能作为入池判据？（用户提案）
  3. 如果不能，动态池应该怎么运营？

面板用生产同链回放（engine.compute_features → engine.state_probs，参数读生产
config），逐日记录 dominant / confidence / w = Σ p·state.weights，其中 w 就是
生产里 `state_weight` 的口径，也就是「五态评分」本身。

所有规则一律 T 日收盘出信号、T+1 收盘成交，无前视；评价基准是 118 只等权
（不是沪深 300——池子的职责是「比自己的可选集更好」，跑赢基准是另一件事）。

用法：
  python tools_pool_dynamic.py --build            # 回放建面板（约 16 分钟）
  python tools_pool_dynamic.py                    # 规则对比 + 报告
  python tools_pool_dynamic.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import yaml  # noqa: E402

CACHE = os.path.join(os.path.dirname(BASE), "stock-pool", "_cache")
PANEL = os.path.join(BASE, "state", "pool_panel.jsonl")
WARMUP = 120
WIN = 320
STATES = ["A", "B", "C", "D", "E"]
STATE_CN = {"A": "吸筹", "B": "突破", "C": "通道", "D": "派发", "E": "下跌"}

# 交易成本：单边 8bp（ETF 佣金+冲击+价差，偏保守）
COST_BP = 8.0
# 容量门槛：指数 20 日成交额中位数（元）。ETF 成交额约为指数的 1–3%，
# 0.3 亿指数成交额是极宽松的下限，只用于剔除"根本没量"的标的。
AMT_FLOOR = 0.3e8
AMT_FULL = 5.0e8


# ------------------------------------------------------------------ 基建
def load_cfg() -> Dict[str, Any]:
    with open(os.path.join(BASE, "splus_config.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_bars(code: str) -> List[Dict[str, Any]]:
    p = os.path.join(CACHE, "idx_%s.json" % code)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def replay_one(args: Tuple[str, Dict[str, Any], str]) -> List[Dict[str, Any]]:
    """单只回放（子进程入口）。生产同链，逐日记录五态评分 w。

    起点从 `--floor` 起算，但特征窗口仍用全历史：compute_features 收到的是
    bars[:i+1] 的尾部 320 根，历史长度不变，所以口径与生产一致。
    """
    code, st_cfg, floor = args
    import engine

    W = st_cfg["weights"]
    bars = load_bars(code)
    out: List[Dict[str, Any]] = []
    if len(bars) <= WARMUP + 30:
        return out
    start = WARMUP
    if floor:
        for i, b in enumerate(bars):
            if str(b["date"])[:10] >= floor:
                start = max(WARMUP, i)
                break
    prev = None
    # 预热：把 start 之前的状态跑出来，保证 prev 与生产一致
    for i in range(WARMUP, start):
        feats = engine.compute_features(bars[max(0, i - WIN + 1):i + 1])
        sp = engine.state_probs(feats, prev, float(st_cfg["persistence"]),
                                float(st_cfg["temperature"]), st_cfg["scoring"],
                                float(st_cfg.get("uniform_prior", 0.2)))
        prev = sp["probs"]
    for i in range(start, len(bars)):
        feats = engine.compute_features(bars[max(0, i - WIN + 1):i + 1])
        sp = engine.state_probs(feats, prev, float(st_cfg["persistence"]),
                                float(st_cfg["temperature"]), st_cfg["scoring"],
                                float(st_cfg.get("uniform_prior", 0.2)))
        prev = sp["probs"]
        w = sum(float(sp["probs"][s]) * float(W[s]) for s in STATES)
        out.append({"c": code, "d": str(bars[i]["date"])[:10], "s": sp["dominant"],
                    "k": round(float(sp["confidence"]), 6), "w": round(w, 6),
                    "cl": float(bars[i]["close"])})
    return out


def build_panel(codes: List[str], st_cfg: Dict[str, Any], path: str = PANEL,
                jobs: int = 1, floor: str = "") -> int:
    """回放建面板；jobs>1 用多进程（4 核机器上 3 worker 最稳）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    n = 0
    with open(tmp, "w", encoding="utf-8") as out:
        args = [(c, st_cfg, floor) for c in codes]
        if jobs > 1:
            import multiprocessing as mp
            with mp.Pool(jobs) as pool:
                for k, rows in enumerate(pool.imap(replay_one, args, chunksize=2)):
                    for r in rows:
                        out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n += len(rows)
                    if (k + 1) % 10 == 0:
                        print("  回放 %d/%d 只（%d 行）" % (k + 1, len(codes), n),
                              file=sys.stderr, flush=True)
        else:
            for k, a in enumerate(args):
                rows = replay_one(a)
                for r in rows:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += len(rows)
                if (k + 1) % 10 == 0:
                    print("  回放 %d/%d 只（%d 行）" % (k + 1, len(codes), n),
                          file=sys.stderr, flush=True)
    os.replace(tmp, path)
    return n


DEC_GLOB = "splus_decision_"


def latest_decision() -> str:
    dp = os.path.join(BASE, "data")
    fs = sorted(f for f in os.listdir(dp)
                if f.startswith(DEC_GLOB) and f.endswith(".json"))
    return os.path.join(dp, fs[-1]) if fs else ""


def load_mapping() -> Dict[str, Dict[str, Any]]:
    """当前映射快照 → {code: {status, verified, etf_amt}}。

    注意：这是"今天"的映射，用于历史回测属轻度前视（生存偏差方向），
    在报告里必须披露；它只决定"这只今天能不能买"，不参与排序。
    """
    out: Dict[str, Dict[str, Any]] = {}
    dp = latest_decision()
    if not dp:
        return out
    with open(dp, encoding="utf-8") as fh:
        dec = json.load(fh)
    qp = os.path.join(BASE, "data", "etf_quotes.json")
    quotes = {}
    if os.path.exists(qp):
        with open(qp, encoding="utf-8") as fh:
            quotes = json.load(fh).get("quotes") or {}
    for c, v in (dec.get("per_index") or {}).items():
        inst = v.get("instrument") or {}
        ec = inst.get("etf_code")
        amt = (quotes.get(str(ec)) or {}).get("amount") if ec else None
        out[c] = {"status": inst.get("status"), "verified": bool(inst.get("verified")),
                  "etf_amt": amt, "etf_code": ec}
    return out


def load_panel(path: str = PANEL) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """→ {date: {code: row}}"""
    out: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[r["d"]][r["c"]] = r
    return out


# ------------------------------------------------------------------ 统计
def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def sd(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def nw_t(xs: List[float], lag: int) -> Tuple[float, float, float]:
    """Newey-West(lag, Bartlett)。返回 (mean, se, t)。"""
    n = len(xs)
    if n < 10:
        return 0.0, float("nan"), float("nan")
    m = mean(xs)
    dev = [x - m for x in xs]
    g0 = sum(d * d for d in dev) / n
    v = g0
    for l in range(1, min(lag, n - 1) + 1):
        gl = sum(dev[i] * dev[i + l] for i in range(n - l)) / n
        v += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    if v <= 0:
        v = g0
    se = math.sqrt(v / n)
    return m, se, (m / se if se > 0 else float("nan"))


def p_two(t: Optional[float]) -> float:
    if t is None or t != t:
        return 1.0
    return math.erfc(abs(t) / math.sqrt(2.0))


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 5:
        return None

    def rank(v: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy)


# ------------------------------------------------------------------ 打分
def build_days(panel: Dict[str, Dict[str, Dict[str, Any]]],
               meta: Dict[str, Dict[str, Any]],
               min_members: int = 40) -> List[Dict[str, Any]]:
    """把面板整理成按日列表，并算当日横截面派生量。

    每个元素：{"d", "rows":[{c,s,w,k,cl,rs20,amt20,bucket}], "ew_ret_next" ...}
    rs20 = 20 日涨跌幅 − 当日横截面中位数（相对强弱，纯价量，无状态）。
    """
    dates = sorted(d for d, rs in panel.items() if len(rs) >= min_members)
    # 每只的收盘序列，用于算 20 日动量
    seq: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for d in dates:
        for c, r in panel[d].items():
            seq[c].append((d, r["cl"]))
    mom: Dict[str, Dict[str, float]] = {}
    for c, xs in seq.items():
        m: Dict[str, float] = {}
        for i in range(len(xs)):
            if i >= 20 and xs[i - 20][1] > 0:
                m[xs[i][0]] = xs[i][1] / xs[i - 20][1] - 1.0
        mom[c] = m

    days: List[Dict[str, Any]] = []
    for d in dates:
        rows = []
        ms = [mom[c].get(d) for c in panel[d]]
        valid = [x for x in ms if x is not None]
        med = statistics.median(valid) if valid else 0.0
        for c, r in panel[d].items():
            mm = mom[c].get(d)
            rows.append({
                "c": c, "s": r["s"], "w": r["w"], "k": r["k"], "cl": r["cl"],
                "rs20": (mm - med) if mm is not None else None,
                "bucket": (meta.get(c) or {}).get("bucket", "?"),
                "role": (meta.get(c) or {}).get("role", "?"),
                "tradeable": bool((meta.get(c) or {}).get("tradeable")),
            })
        days.append({"d": d, "rows": rows})
    return days


def attach_forward(days: List[Dict[str, Any]], h: int) -> None:
    """挂上未来 h 日收益（close→close），及当日横截面等权收益。"""
    idx = {d["d"]: i for i, d in enumerate(days)}
    close: Dict[str, Dict[str, float]] = defaultdict(dict)
    for d in days:
        for r in d["rows"]:
            close[r["c"]][d["d"]] = r["cl"]
    ds = [d["d"] for d in days]
    for i, d in enumerate(days):
        j = i + h
        if j >= len(ds) or ds[j] not in idx:
            continue
        for r in d["rows"]:
            c0 = close[r["c"]].get(ds[i])
            c1 = close[r["c"]].get(ds[j])
            r["fwd"] = (c1 / c0 - 1.0) if (c0 and c1) else None
        vals = [r["fwd"] for r in d["rows"] if r.get("fwd") is not None]
        d["ew_fwd"] = mean(vals) if vals else None


# ------------------------------------------------------------------ 规则
def _top(cands: List[Dict[str, Any]], key: str, n: int) -> List[str]:
    ranked = sorted(cands, key=lambda r: (-r[key], r["c"]))
    return [r["c"] for r in ranked[:n]]


def rule_fixed(days: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[List[str]]:
    fixed = [c for c, m in meta.items() if m.get("tradeable")]
    return [[c for c in fixed if c in {r["c"] for r in d["rows"]}] for d in days]


def rule_top_score(days: List[Dict[str, Any]], n: int = 10,
                   smooth: int = 1) -> List[List[str]]:
    """用户提案：近 smooth 日「五态评分」均值前 n 名。"""
    hist: Dict[str, List[float]] = defaultdict(list)
    out: List[List[str]] = []
    for d in days:
        for r in d["rows"]:
            hist[r["c"]].append(r["w"])
        cands = []
        for r in d["rows"]:
            xs = hist[r["c"]][-smooth:]
            if len(xs) < smooth:
                continue
            cands.append({"c": r["c"], "v": mean(xs)})
        out.append(_top(cands, "v", n))
    return out


def rule_top_rs(days: List[Dict[str, Any]], n: int = 10,
                smooth: int = 1) -> List[List[str]]:
    hist: Dict[str, List[float]] = defaultdict(list)
    out: List[List[str]] = []
    for d in days:
        for r in d["rows"]:
            if r["rs20"] is not None:
                hist[r["c"]].append(r["rs20"])
        cands = []
        for r in d["rows"]:
            xs = hist[r["c"]][-smooth:]
            if len(xs) < smooth:
                continue
            cands.append({"c": r["c"], "v": mean(xs)})
        out.append(_top(cands, "v", n))
    return out


def _corr(a: List[float], b: List[float]) -> float:
    n = len(a)
    if n < 20:
        return 0.0
    ma, mb = mean(a), mean(b)
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va <= 0 or vb <= 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def build_returns(days: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    close: Dict[str, Dict[str, float]] = defaultdict(dict)
    for d in days:
        for r in d["rows"]:
            close[r["c"]][d["d"]] = r["cl"]
    out: Dict[str, Dict[str, float]] = defaultdict(dict)
    for c, m in close.items():
        ds = sorted(m)
        for i in range(1, len(ds)):
            if m[ds[i - 1]]:
                out[c][ds[i]] = m[ds[i]] / m[ds[i - 1]] - 1.0
    return out


def rule_funnel(days: List[Dict[str, Any]], meta: Dict[str, Any],
                n_in: int = 10, n_out: int = 15, smooth: int = 3,
                max_per_bucket: int = 0, gate_state: bool = False,
                amt20: Optional[Dict[str, Dict[str, float]]] = None,
                bars_n: Optional[Dict[str, int]] = None,
                allowed: Optional[set] = None,
                rets: Optional[Dict[str, Dict[str, float]]] = None,
                corr_max: float = 0.90, corr_win: int = 60
                ) -> List[List[str]]:
    """漏斗：硬门槛 → 相对强弱排序 → 缓冲带（进 10 / 出 15）→ 相关性闸。

    集中度用**相关性**而不是 config 的 bucket：实测 bucket 里 104 只都叫
    "watch"，是运营分桶不是主题分类，拿它做上限会把池压到 5 只。
    """
    prev: List[str] = []
    hist: Dict[str, List[float]] = defaultdict(list)
    out: List[List[str]] = []
    dates = [d["d"] for d in days]
    for i, d in enumerate(days):
        for r in d["rows"]:
            if r["rs20"] is not None:
                hist[r["c"]].append(r["rs20"])
        cands = []
        for r in d["rows"]:
            c = r["c"]
            if allowed is not None and c not in allowed:
                continue
            if (bars_n or {}).get(c, 0) < 252:
                continue
            a = (amt20 or {}).get(c, {}).get(d["d"])
            if a is None or a < AMT_FLOOR:
                continue
            if gate_state and r["s"] == "E":
                continue
            xs = hist[c][-smooth:]
            if len(xs) < smooth:
                continue
            cands.append({"c": c, "v": mean(xs), "b": r["bucket"]})
        cands.sort(key=lambda r: -r["v"])
        rank = {r["c"]: i for i, r in enumerate(cands)}
        # 缓冲带：已在池且排名 ≤ n_out 保留；新进必须排名 < n_in
        keep = [c for c in prev if rank.get(c, 10 ** 6) < n_out]
        pool: List[str] = []
        bcnt: Counter = Counter()
        # 近 corr_win 日的收益窗口（用于相关性闸）
        win = dates[max(0, i - corr_win + 1):i + 1]

        def vec(c: str) -> List[float]:
            m = (rets or {}).get(c) or {}
            return [m[x] for x in win if x in m]

        def too_close(c: str, sel: List[str]) -> bool:
            if corr_max >= 1.0 or not sel:
                return False
            vc = vec(c)
            for s2 in sel:
                if abs(_corr(vc, vec(s2))) > corr_max:
                    return True
            return False
        # 第一轮：**保留仓优先进位**（否则排名 12 的老成员会被排名 9 的新进挤出，
        # 缓冲带形同虚设——selftest T3 就是靠这条断言抓到这个 bug 的）
        for c in sorted(keep, key=lambda x: rank.get(x, 10 ** 6)):
            b = (meta.get(c) or {}).get("bucket", "?")
            if max_per_bucket and bcnt[b] >= max_per_bucket:
                continue
            pool.append(c)
            bcnt[b] += 1
        # 第二轮：排名带内（< n_in）新进补满，逐个过相关性闸
        for c in [x for x in rank if rank[x] < n_in and x not in pool]:
            if len(pool) >= n_in:
                break
            b = (meta.get(c) or {}).get("bucket", "?")
            if max_per_bucket and bcnt[b] >= max_per_bucket:
                continue
            if too_close(c, pool):
                continue
            pool.append(c)
            bcnt[b] += 1
        # 第三轮（补足）：集中度约束使得池未满时，从缓冲带（rank < n_out）补
        if len(pool) < n_in:
            for c in [x for x in rank if rank[x] >= n_in and rank[x] < n_out]:
                if c in pool:
                    continue
                b = (meta.get(c) or {}).get("bucket", "?")
                if max_per_bucket and bcnt[b] >= max_per_bucket:
                    continue
                if too_close(c, pool):
                    continue
                pool.append(c)
                bcnt[b] += 1
                if len(pool) >= n_in:
                    break
        prev = pool
        out.append(pool)
    return out


# ------------------------------------------------------------------ 评价
def eval_rule(name: str, pools: List[List[str]], days: List[Dict[str, Any]],
              h: int = 20) -> Dict[str, Any]:
    """规则评价：超额 / 换手 / 退化 / 集中度 / 容量。"""
    excess, ics, sizes, turnovers = [], [], [], []
    top_same_state = 0
    w_spread = []
    hhi = []
    bucket_cnt: Counter = Counter()
    prev: set = set()
    n_valid = 0
    for d, pool in zip(days, pools):
        if not pool:
            continue
        rows = {r["c"]: r for r in d["rows"]}
        mem = [rows[c] for c in pool if c in rows]
        if len(mem) < 3:
            continue
        n_valid += 1
        sizes.append(len(mem))
        # 换手（单边）
        cur = set(c for c in pool)
        if prev:
            turnovers.append(len(cur - prev) / max(1, len(cur)))
        prev = cur
        # 状态构成
        sc = Counter(r["s"] for r in mem)
        bucket_cnt.update(r["bucket"] for r in mem)
        if len(sc) == 1:
            top_same_state += 1
        # 评分在池内的离散度（判断排名是否有分辨力）
        ws = [r["w"] for r in mem]
        if len(ws) > 1 and sd(ws) > 0:
            w_spread.append((max(ws) - min(ws)) / (sd([r["w"] for r in rows.values()]) or 1))
        # 主题集中度
        bc = Counter(r["bucket"] for r in mem)
        n = sum(bc.values())
        hhi.append(sum((v / n) ** 2 for v in bc.values()))
        # 超额
        f = [r.get("fwd") for r in mem if r.get("fwd") is not None]
        if f and d.get("ew_fwd") is not None:
            excess.append(mean(f) - d["ew_fwd"])
        # IC（评分 vs 未来收益，全横截面）
        xs = [(r["w"], r["fwd"]) for r in rows.values() if r.get("fwd") is not None]
        if len(xs) >= 30:
            ic = spearman([a for a, _ in xs], [b for _, b in xs])
            if ic is not None:
                ics.append(ic)
    m, se, t = nw_t(excess, 40)
    icm, _, ict = nw_t(ics, 40) if ics else (0.0, 0.0, 0.0)
    return {
        "rule": name, "days": n_valid,
        "avg_size": round(mean(sizes), 1) if sizes else 0,
        "excess_20d_pct": round(m * 100, 3), "t": round(t, 2) if t == t else None,
        "p": round(p_two(t), 3) if t == t else None,
        "turnover_daily_pct": round(mean(turnovers) * 100, 2) if turnovers else 0.0,
        "turnover_annual_x": round(mean(turnovers) * 244, 1) if turnovers else 0.0,
        "top10_single_state_pct": round(100.0 * top_same_state / max(1, n_valid), 1),
        "ic_w": round(icm, 4) if ics else None, "ic_t": round(ict, 2) if ics else None,
        "hhi": round(mean(hhi), 3) if hhi else None,
        "bucket_mix": {k: round(100.0 * v / sum(bucket_cnt.values()), 1)
                       for k, v in bucket_cnt.most_common(6)},
        "w_spread_ratio": round(mean(w_spread), 3) if w_spread else None,
    }


def univ_daily(days: List[Dict[str, Any]]) -> List[float]:
    """全宇宙等权日收益（与 nav_stats 同一对齐口径：能用上一日收盘就计入）。"""
    close: Dict[str, Dict[str, float]] = defaultdict(dict)
    for d in days:
        for r in d["rows"]:
            close[r["c"]][d["d"]] = r["cl"]
    ds = [d["d"] for d in days]
    out: List[float] = []
    for i in range(len(days) - 1):
        rs = [close[r["c"]].get(ds[i + 1]) / close[r["c"]].get(ds[i]) - 1.0
              for r in days[i]["rows"]
              if close[r["c"]].get(ds[i + 1]) and close[r["c"]].get(ds[i])]
        out.append(mean(rs) if rs else 0.0)
    return out


def nav_stats(pools: List[List[str]], days: List[Dict[str, Any]],
              label: str, univ: Optional[List[float]] = None) -> Dict[str, Any]:
    """等权组合净值（日频调仓 + 成本）：年化、波动、Sharpe、最大回撤、换手。"""
    close: Dict[str, Dict[str, float]] = defaultdict(dict)
    for d in days:
        for r in d["rows"]:
            close[r["c"]][d["d"]] = r["cl"]
    ds = [d["d"] for d in days]
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    rets: List[float] = []
    turn_sum = 0.0
    n_reb = 0
    for i in range(len(days) - 1):
        held = pools[i]
        if not held:
            continue
        d0, d1 = ds[i], ds[i + 1]
        rs = [close[c].get(d1) / close[c].get(d0) - 1.0
              for c in held if close[c].get(d1) and close[c].get(d0)]
        if not rs:
            continue
        r = mean(rs)
        # 收盘调仓 → 只对下一日的池子变化计成本；单边换手 = 新买入 / 池大小
        nxt = pools[i + 1] if i + 1 < len(pools) else held
        cur = set(nxt)
        to = len(cur - set(held)) / max(1, len(cur)) if cur else 0.0
        cost = to * COST_BP / 1e4
        turn_sum += to
        n_reb += 1
        net = r - cost
        rets.append(net)
        nav *= (1.0 + net)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    vol = sd(rets) * math.sqrt(244) if len(rets) > 2 else 0.0
    ann = (nav ** (244.0 / max(1, len(rets))) - 1.0) if rets else 0.0
    # 日频超额（与净值同源，避免"重叠 20 日"与"日频调仓"两套口径打架）
    alpha_ann = alpha_t = None
    if univ and rets:
        n = min(len(rets), len(univ))
        # rets 是从第一天有持仓起算，univ 也从同一天起算（main 已对齐窗口）
        diff = [rets[k] - univ[k] for k in range(n)]
        mu, _, t = nw_t(diff, 40)
        alpha_ann = (math.pow(1.0 + mu, 244) - 1.0) * 100 if mu > -1 else None
        alpha_t = t
    return {"rule": label, "ann_pct": round(ann * 100, 2),
            "alpha_ann_pct": round(alpha_ann, 2) if alpha_ann is not None else None,
            "alpha_t": round(alpha_t, 2) if alpha_t is not None else None,
            "vol_pct": round(vol * 100, 2),
            "sharpe": round(ann / vol, 2) if vol > 0 else None,
            "mdd_pct": round(mdd * 100, 2),
            "turnover_annual_x": round(turn_sum / max(1, n_reb) * 244, 1),
            "final_nav": round(nav, 4), "days": len(rets)}


def universe_nav(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    """118 只等权（同成本口径=0，仅作对照）。"""
    close: Dict[str, Dict[str, float]] = defaultdict(dict)
    for d in days:
        for r in d["rows"]:
            close[r["c"]][d["d"]] = r["cl"]
    ds = [d["d"] for d in days]
    rets = []
    for i in range(len(days) - 1):
        rs = [close[r["c"]].get(ds[i + 1]) / close[r["c"]].get(ds[i]) - 1.0
              for r in days[i]["rows"]
              if close[r["c"]].get(ds[i + 1]) and close[r["c"]].get(ds[i])]
        if rs:
            rets.append(mean(rs))
    nav = 1.0
    for r in rets:
        nav *= (1 + r)
    vol = sd(rets) * math.sqrt(244)
    ann = nav ** (244.0 / max(1, len(rets))) - 1.0
    peak = 1.0
    mdd = 0.0
    n = 1.0
    for r in rets:
        n *= (1 + r)
        peak = max(peak, n)
        mdd = min(mdd, n / peak - 1)
    return {"rule": "118 只等权（基准）", "ann_pct": round(ann * 100, 2),
            "vol_pct": round(vol * 100, 2),
            "sharpe": round(ann / vol, 2) if vol else None,
            "mdd_pct": round(mdd * 100, 2), "turnover_annual_x": 0.0,
            "final_nav": round(nav, 4), "days": len(rets)}


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="回放重建 pool_panel.jsonl")
    ap.add_argument("--jobs", type=int, default=1, help="回放并行进程数")
    ap.add_argument("--floor", default="2020-06-01",
                    help="回放起始日期（特征仍用全历史）；留空则从最早开始")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    cfg = load_cfg()
    meta = {u["code"]: u for u in cfg["universe"]}
    codes = [u["code"] for u in cfg["universe"] if u.get("enabled", True)]

    if a.build:
        n = build_panel(codes, cfg["state"], jobs=a.jobs, floor=a.floor)
        print("面板 %d 行 → %s" % (n, PANEL))
        return 0

    if not os.path.exists(PANEL):
        print("缺面板，先跑 --build", file=sys.stderr)
        return 2

    panel = load_panel()
    days = build_days(panel, meta)
    attach_forward(days, 20)
    dates = [d["d"] for d in days]

    # 容量/历史门槛用的静态量
    amt20: Dict[str, Dict[str, float]] = defaultdict(dict)
    bars_n: Dict[str, int] = {}
    for c in codes:
        bars = load_bars(c)
        bars_n[c] = len(bars)
        run: List[float] = []
        for b in bars:
            av = b.get("amount")
            run.append(float(av) if av else 0.0)
            if len(run) > 20:
                run.pop(0)
            if len(run) == 20:
                amt20[c][str(b["date"])[:10]] = statistics.median(run)

    print("=" * 78)
    print("可交易标的池：现状诊断 + 动态规则对比")
    print("面板 %s → %s，%d 个交易日 × 最多 %d 只"
          % (dates[0], dates[-1], len(dates), max(len(d["rows"]) for d in days)))
    print("=" * 78)

    # ---------- 1. 现状诊断
    n_tr = sum(1 for m in meta.values() if m.get("tradeable"))
    have_amt = sum(1 for c in codes if amt20.get(c))
    pass_floor = sum(1 for c in codes
                     if amt20.get(c) and len(amt20[c]) > 60
                     and statistics.median(list(amt20[c].values())[-250:]) >= AMT_FLOOR)
    mp = load_mapping()
    conf_ok = {c for c, m in mp.items() if m["status"] == "confirmed" and m["verified"]}
    print("\n【1】现状：可交易池 = 配置静态标志")
    print("  118 只中 tradeable=true 的 %d 只；有指数成交额的 %d 只；"
          "20 日均额 ≥0.3 亿的 %d 只" % (n_tr, have_amt, pass_floor))
    print("  映射就绪（confirmed+verified）%d 只；needs_review %d 只；unmapped %d 只"
          % (len(conf_ok),
             sum(1 for m in mp.values() if m["status"] == "needs_review"),
             sum(1 for m in mp.values() if m["status"] == "unmapped")))

    print("\n  11 只 tradeable 的映射实际状态：")
    print("  %-9s%-12s%-12s%-6s%-8s%s"
          % ("代码", "名称", "status", "verif", "ETF额", "能否真的下单"))
    for c in [x for x in codes if meta[x].get("tradeable")]:
        m = mp.get(c) or {}
        amt = m.get("etf_amt")
        ok = m.get("status") == "confirmed" and m.get("verified")
        print("  %-9s%-12s%-12s%-6s%-8s%s"
              % (c, meta[c]["name"], m.get("status"), m.get("verified"),
                 ("%.1f亿" % (amt / 1e8)) if amt else "无",
                 "是" if ok else "否 ← 配了 tradeable 却没映射/未验证"))
    bad_tr = [c for c in codes if meta[c].get("tradeable")
              and not ((mp.get(c) or {}).get("status") == "confirmed"
                       and (mp.get(c) or {}).get("verified"))]
    bad = bad_tr
    print("  → %d/%d 只标了 tradeable 但映射不可用（unmapped 或未验证），"
          "它们在 eligibility 里必然是 FROZEN" % (len(bad), n_tr))

    print("\n  容量阶梯（映射就绪 ∩ ETF 当日成交额）：")
    for lo, lab in ((0.3e8, "≥0.3 亿"), (1e8, "≥1 亿"), (5e8, "≥5 亿"), (1e9, "≥10 亿")):
        g = [c for c in conf_ok if (mp[c].get("etf_amt") or 0) >= lo]
        print("    %-8s %3d 只  %s" % (lab, len(g),
              "、".join(meta[c]["name"] for c in
                        sorted(g, key=lambda x: -(mp[x].get("etf_amt") or 0))[:8])))
    print("  → 动态池的硬约束不是「哪些好看」，是「哪些买得动」。")

    # ---------- 2. 五态评分的分辨力
    print("\n【2】「五态评分」本身能不能排序")
    day_state = []
    w_sd_ratio = []
    for d in days:
        rows = d["rows"]
        sc = Counter(r["s"] for r in rows)
        day_state.append(len(sc))
        ws = [r["w"] for r in rows]
        if len(ws) > 10 and sd(ws) > 0:
            top10 = sorted(ws, reverse=True)[:10]
            w_sd_ratio.append((max(top10) - min(top10)) / sd(ws))
    print("  当日出现的不同状态数：中位 %d（最多 5）"
          % statistics.median(day_state))
    print("  前 10 名内部 w 极差 / 全体 w 标准差：中位 %.2f" %
          (statistics.median(w_sd_ratio) if w_sd_ratio else 0))

    # ---------- 3. 规则对比
    fixed_all = rule_fixed(days, meta)
    i0 = next((i for i, p in enumerate(fixed_all) if len(p) >= 3), 0)
    if i0:
        print("\n  [窗口] 固定池自 %s 起成立，所有规则统一从该日开始评价"
              % dates[i0])
        days = days[i0:]
        dates = [d["d"] for d in days]
        attach_forward(days, 20)
    fixed = rule_fixed(days, meta)
    r_top_w3 = rule_top_score(days, 10, 3)
    r_top_w1 = rule_top_score(days, 10, 1)
    r_top_rs3 = rule_top_rs(days, 10, 3)
    rets = build_returns(days)
    r_funnel = rule_funnel(days, meta, amt20=amt20, bars_n=bars_n, rets=rets)
    r_funnel_st = rule_funnel(days, meta, amt20=amt20, bars_n=bars_n, gate_state=True,
                              rets=rets)
    # 现实可执行口径：只在「映射就绪」宇宙内选（当前快照，轻度前视，报告披露）
    r_funnel_map = rule_funnel(days, meta, amt20=amt20, bars_n=bars_n, allowed=conf_ok,
                               rets=rets)
    # 宽缓冲带：用更宽的出池带 + 更长平滑换低换手（成本与信号滞后之间的权衡）
    r_wide = rule_funnel(days, meta, amt20=amt20, bars_n=bars_n, rets=rets,
                         n_out=30, smooth=5)

    rules = [
        ("R0 固定 11 只（现状）", fixed),
        ("R1 五态评分 3 日均 前10", r_top_w3),
        ("R1b 五态评分 1 日前10", r_top_w1),
        ("R2 相对强弱 3 日均 前10", r_top_rs3),
        ("R3 漏斗(闸门+RS+缓冲+相关)", r_funnel),
        ("R4 R3 + 剔除 E 态", r_funnel_st),
        ("R5 R3 ∩ 映射就绪宇宙", r_funnel_map),
        ("R6 R3 宽缓冲(出30/平滑5)", r_wide),
    ]

    print("\n【3】规则对比（超额 = 池内等权 future 20 日 − 118 只等权同期）")
    hdr = ("%-24s%6s%7s%10s%7s%8s%9s%8s"
           % ("规则", "天数", "均只数", "超额/20日", "NW t", "年换手x", "前10同态%", "HHI"))
    print(hdr)
    print("-" * len(hdr))
    results = []
    for name, pools in rules:
        r = eval_rule(name, pools, days)
        results.append(r)
        print("%-24s%6d%7.1f%9.3f%%%7s%8.1f%9s%8s"
              % (name, r["days"], r["avg_size"], r["excess_20d_pct"],
                 ("%+.2f" % r["t"]) if r["t"] is not None else "—",
                 r["turnover_annual_x"],
                 ("%.0f" % r["top10_single_state_pct"]) if r["top10_single_state_pct"] is not None else "—",
                 ("%.2f" % r["hhi"]) if r["hhi"] is not None else "—"))

    print("\n【4】组合表现（等权、日频调仓、单边 8bp 成本）")
    hdr2 = ("%-24s%9s%9s%8s%9s%9s%10s%7s"
            % ("规则", "年化%", "波动%", "Sharpe", "最大回撤%", "年换手x", "日频超额%", "NW t"))
    print(hdr2)
    print("-" * len(hdr2))
    navs = []
    univ = univ_daily(days)
    for name, pools in rules:
        s = nav_stats(pools, days, name, univ)
        navs.append(s)
        print("%-24s%9.2f%9.2f%8s%9.2f%9.1f%10s%7s"
              % (name, s["ann_pct"], s["vol_pct"],
                 ("%.2f" % s["sharpe"]) if s["sharpe"] is not None else "—",
                 s["mdd_pct"], s["turnover_annual_x"],
                 ("%+.2f" % s["alpha_ann_pct"]) if s["alpha_ann_pct"] is not None else "—",
                 ("%+.2f" % s["alpha_t"]) if s["alpha_t"] is not None else "—"))
    u = universe_nav(days)
    print("%-24s%9.2f%9.2f%8s%9.2f%9.1f%10s%7s"
          % (u["rule"], u["ann_pct"], u["vol_pct"],
             ("%.2f" % u["sharpe"]) if u["sharpe"] else "—",
             u["mdd_pct"], u["turnover_annual_x"], "—", "—"))

    # ---------- 5. 用户提案的压力测试
    print("\n【5】对「近 3 日五态评分前 10」的单点压力测试")
    # 5.1 换手与稳定性
    r1 = next(r for r in results if r["rule"].startswith("R1 "))
    r2 = next(r for r in results if r["rule"].startswith("R2 "))
    r3 = next(r for r in results if r["rule"].startswith("R3 "))
    print("  换手：R1 %.0fx/年 vs R3 %.0fx/年 → 前者多付 %.1f%%/年成本（8bp/边）"
          % (r1["turnover_annual_x"], r3["turnover_annual_x"],
             (r1["turnover_annual_x"] - r3["turnover_annual_x"]) * COST_BP * 2 / 100))
    # 5.2 与 RS 前 10 的重合
    ov = [len(set(a) & set(b)) / max(1, len(set(b)))
          for a, b in zip(r_top_w3, r_top_rs3) if b]
    ov_med = statistics.median(ov) * 100 if ov else 0.0
    print("  R1 与 R2 的成员重合率：中位 %.0f%% → 五态评分排名基本等于动量的粗量化"
          % ov_med)
    # 5.3 退化：单边行情下的并列
    bad = 0
    for d, p in zip(days, r_top_w3):
        rows = {r["c"]: r for r in d["rows"]}
        ss = {rows[c]["s"] for c in p if c in rows}
        if len(ss) == 1:
            bad += 1
    print("  前 10 名全部同态的天数：%d/%d（%.0f%%）——这些天排名由 conf 尾数决定"
          % (bad, len(days), 100.0 * bad / len(days)))
    # 5.4 可交易性
    nt = [sum(1 for c in p if meta.get(c, {}).get("tradeable")) for p in r_top_w3]
    print("  R1 前 10 中「当前可交易」的只数：中位 %d → 现行映射跟不上，必须扩池"
          % statistics.median(nt))
    # 5.5 状态构成
    mix = Counter()
    for d, p in zip(days, r_top_w3):
        rows = {r["c"]: r for r in d["rows"]}
        for c in p:
            if c in rows:
                mix[rows[c]["s"]] += 1
    tot = sum(mix.values())
    print("  R1 池内状态构成：" + " / ".join(
        "%s %s %.0f%%" % (s, STATE_CN[s], 100.0 * mix.get(s, 0) / tot) for s in STATES))

    diag = {
        "overlap_r1_r2_pct": round(ov_med, 1),
        "r1_single_state_days": bad,
        "r1_single_state_pct": round(100.0 * bad / max(1, len(days)), 1),
        "r1_tradeable_median": statistics.median(nt) if nt else 0,
        "r1_state_mix": {s: round(100.0 * mix.get(s, 0) / tot, 1) for s in STATES},
        "mapping_confirmed": len(conf_ok),
        "tradeable_unmapped_or_unverified": bad_tr,
        "capacity_0_3yi": sum(1 for c in conf_ok if (mp[c].get("etf_amt") or 0) >= 0.3e8),
        "capacity_1yi": sum(1 for c in conf_ok if (mp[c].get("etf_amt") or 0) >= 1e8),
        "capacity_5yi": sum(1 for c in conf_ok if (mp[c].get("etf_amt") or 0) >= 5e8),
    }
    out = {"as_of": dates[-1], "days": len(days),
           "fixed_tradeable": n_tr, "pass_capacity": pass_floor,
           "rules": results, "nav": navs, "universe_nav": u, "diag": diag}
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print("\n→ %s" % a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
