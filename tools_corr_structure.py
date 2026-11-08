# -*- coding: utf-8 -*-
"""
相关性结构审计——回答一个问题：118 只指数到底等于几个独立样本？

为什么必须做：
    跨指数 t 检验把每只指数当一个观测，隐含假设"彼此独立"。
    但 A 股主题指数的日收益两两相关普遍在 0.4-0.9，
    高度相关会让标准误被系统性低估，t 值虚高。
    有效样本量 n_eff = n / (1 + (n-1)·ρ̄)，
    当 ρ̄≈0.5、n=118 时 n_eff≈2——也就是说，
    "118 只指数都验证了同一个结论"在统计上几乎等于"2 个独立样本"。

输出：
    平均/中位相关、n_eff、按相关矩阵第一主成分解释的方差占比（=共同因子强度）、
    聚类数（相关>0.8 视为同族），以及每只标的的平均相关（找"伪分散"标的）。

用法：
    python tools_corr_structure.py                    # 默认最近 2 年
    python tools_corr_structure.py --start 2021-01-01 --end 2023-12-31
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg() -> Dict[str, Any]:
    import yaml
    with open(os.path.join(HERE, "splus_config.yaml"), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def daily_returns(path: str, lo: str, hi: str) -> Dict[str, float]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {}
    m = {}
    for r in raw:
        try:
            c = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c > 0:
            m[str(r["date"])] = c
    ks = sorted(k for k in m if lo <= k <= hi)
    return {ks[i]: m[ks[i]] / m[ks[i - 1]] - 1.0 for i in range(1, len(ks))}


def corr(x: List[float], y: List[float]) -> Optional[float]:
    n = len(x)
    if n < 50:
        return None
    mx, my = st.mean(x), st.mean(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx <= 0 or sy <= 0:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (sx * sy)


def power_iter(M: List[List[float]], iters: int = 60) -> float:
    """相关矩阵第一特征值的幂迭代——衡量共同因子解释力。"""
    n = len(M)
    v = [1.0 / math.sqrt(n)] * n
    lam = 0.0
    for _ in range(iters):
        w = [sum(M[i][j] * v[j] for j in range(n)) for i in range(n)]
        nrm = math.sqrt(sum(x * x for x in w)) or 1.0
        v = [x / nrm for x in w]
        lam = nrm
    return lam


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2099-12-31")
    ap.add_argument("--cluster-rho", type=float, default=0.80)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    cfg = load_cfg()
    cache = os.path.normpath(os.path.join(HERE, cfg["data"]["cache_dir"]))
    uni = [u for u in cfg.get("universe", []) if u.get("enabled", True)]

    R: Dict[str, Dict[str, float]] = {}
    names: Dict[str, str] = {}
    for u in uni:
        c = str(u["code"])
        names[c] = u.get("name", "")
        r = daily_returns(os.path.join(cache, "idx_%s.json" % c), args.start, args.end)
        if len(r) >= 200:
            R[c] = r
    codes = sorted(R, key=lambda c: -len(R[c]))
    if len(codes) < 5:
        print("[corr] 可用标的不足（%d）" % len(codes))
        return 1
    # 交集不能由最短序列独家决定（usNDX 仅 325 根会把窗口压到 14 个月）。
    # 先剔除可用天数不足最长序列 80% 的标的，再取交集。
    max_len = len(R[codes[0]])
    dropped = [c for c in codes if len(R[c]) < 0.8 * max_len]
    codes = [c for c in codes if len(R[c]) >= 0.8 * max_len]
    keys = sorted(set.intersection(*[set(R[c]) for c in codes]))
    if dropped:
        print("[corr] 因区间过短剔除 %d 只：%s" % (len(dropped), ", ".join(dropped[:8])))

    n = len(codes)
    M = [[0.0] * n for _ in range(n)]
    pairs: List[float] = []
    avg_by: Dict[str, List[float]] = {c: [] for c in codes}
    for i in range(n):
        M[i][i] = 1.0
        for j in range(i + 1, n):
            r = corr([R[codes[i]][k] for k in keys], [R[codes[j]][k] for k in keys])
            if r is None:
                continue
            M[i][j] = M[j][i] = r
            pairs.append(r)
            avg_by[codes[i]].append(r)
            avg_by[codes[j]].append(r)

    rho = st.mean(pairs)
    med = st.median(pairs)
    n_eff = n / (1 + (n - 1) * rho) if rho > 0 else float(n)
    lam1 = power_iter(M)
    pct1 = lam1 / n  # 第一主成分解释的方差占比

    # 简单聚类：平均相关 > cluster_rho 归为一族（并查集）
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            if M[i][j] >= args.cluster_rho:
                parent[find(i)] = find(j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    worst = sorted(avg_by.items(), key=lambda kv: -st.mean(kv[1]) if kv[1] else 0)[:10]
    best = sorted(avg_by.items(), key=lambda kv: st.mean(kv[1]) if kv[1] else 9)[:8]

    out = {
        "window": [keys[0], keys[-1]],
        "n_codes": n,
        "n_days": len(keys),
        "mean_rho": round(rho, 4),
        "median_rho": round(med, 4),
        "n_eff": round(n_eff, 2),
        "pc1_var_share": round(pct1, 4),
        "clusters": [{"n": len(v), "codes": [codes[i] for i in sorted(v)]}
                     for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
        "most_correlated": [{"code": c, "name": names.get(c, ""), "avg_rho": round(st.mean(v), 3)} for c, v in worst],
        "most_independent": [{"code": c, "name": names.get(c, ""), "avg_rho": round(st.mean(v), 3)} for c, v in best],
        "t_scale_note": "跨指数 t 值的有效缩放系数 = sqrt(n_eff/n) = %.3f" % math.sqrt(n_eff / n),
    }
    print("[corr] %s ~ %s  n=%d  days=%d" % (keys[0], keys[-1], n, len(keys)))
    print("[corr] mean_rho=%.3f median=%.3f  n_eff=%.2f  PC1 解释 %.1f%%" % (rho, med, n_eff, pct1 * 100))
    print("[corr] t 值缩放系数 sqrt(n_eff/n)=%.3f（即现有跨指数 t 应乘以该系数）" % math.sqrt(n_eff / n))
    print("[corr] 高相关族（ρ>=%.2f）%d 个，最大族 %d 只" % (args.cluster_rho, len(groups),
                                                            max(len(v) for v in groups.values())))
    print("[corr] 最同质: %s" % ", ".join("%s(%.2f)" % (x["code"], x["avg_rho"]) for x in out["most_correlated"][:5]))
    print("[corr] 最独立: %s" % ", ".join("%s(%.2f)" % (x["code"], x["avg_rho"]) for x in out["most_independent"][:5]))

    p = args.json or os.path.join(HERE, cfg["data"].get("output_dir", "data"), "corr_structure.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("[corr] -> %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
