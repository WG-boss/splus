# -*- coding: utf-8 -*-
"""
成交量量纲断层修复（一次性数据治理工具，可反复安全运行）。

问题（2026-11-08 记分卡发现）：
    缓存内 3 只标的的 vol 存在整段 100 倍量纲断层，而同期 amount 恰好缺失，
    导致 engine.load_bars 的「amt/vol」护栏失效（护栏只在 amount>0 时生效）：

        000813 细分化工   2017-2020 为「股」，2021+ 为「手」   → 前面多 100 倍
        000922 中证红利   2019-2020 为「股」，其余为「手」     → 中间多 100 倍
        399975 证券公司   2016-2017 为「股」，2018+ 为「手」   → 前面多 100 倍

    后果：vol_z20 / vol_contract / down_vol_ratio 在断点年跳变 100 倍，
    状态机（vol_expanding = vol_z20>0.5）会在断点附近整年误判；
    这 3 只虽在 watch 桶（不建仓），但参与五状态评分与 6 年回放实证。

修复原则：
    1) 以「数据点最多的量级组」为基准量级（票数表决，非人工拍定），
       把偏离 >= 2 个数量级的年份按 10^k 缩放回基准；
    2) 同时校验 amount：若该段 amount 存在，缩放后 amt/vol 必须落在
       [1e2, 1e6]（手+元口径）区间，否则连 amount 一起缩放；
    3) 写前全量备份到 _cache_backup_unitfix_<ts>/，支持 --restore；
    4) 幂等：修完再跑应报 0 处待修；
    5) 默认 dry-run，必须显式 --apply 才落盘。

用法：
    python tools_fix_units.py                 # 预演（不写）
    python tools_fix_units.py --apply         # 执行
    python tools_fix_units.py --check         # 只报状态（CI 用，非 0 退出=仍有断层）
    python tools_fix_units.py --restore <备份目录>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics as st
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# 量级偏离多少个数量级判为断层（2 个数量级 = 100 倍起）
MAG_GAP = 2
# 缩放后 amt/vol 的合理区间（手 + 元口径下 = 100 × 成分股均价）
AMT_VOL_LO, AMT_VOL_HI = 1e2, 1e6


def load_cfg() -> Dict[str, Any]:
    import yaml
    with open(os.path.join(HERE, "splus_config.yaml"), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def mag(x: float) -> int:
    return int(round(math.log10(x))) if x > 0 else 0


def scan_series(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按年计算 vol 中位数，用「相邻年比值 < 8 则同簇」连续聚类，票数最多的簇为基准。

    为什么不用 log10 取整分箱：A 股指数成交量中位数常落在 1e7.4 附近，
    取整会在 7/8 之间抖动，把同一量纲的年份拆成"两个量级"，
    进而让真正的 100 倍断层只显示为"差 1 个量级"而被漏检（000922 即因此逃过第一版）。
    相邻比值聚类对绝对值不敏感，只对"跳变"敏感，更贴近问题本质。

    返回需要缩放到基准的年份段：factor = 10^(-round(log10(簇中位/基准中位)))。
    仅 |指数| >= MAG_GAP 的簇进入修复；恰好 10 倍（|k|=1）只报告不修，交人工判断。
    """
    by_year: Dict[str, List[float]] = defaultdict(list)
    for b in bars:
        v = b.get("vol")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            by_year[str(b["date"])[:4]].append(v)
    year_med = {y: st.median(v) for y, v in by_year.items() if len(v) >= 20}
    years = sorted(year_med)
    if len(years) < 3:
        return []

    # 连续聚类：相邻年比值落在 [1/8, 8] 内视为同簇
    clusters: List[List[str]] = [[years[0]]]
    for a, b in zip(years, years[1:]):
        ratio = year_med[b] / year_med[a] if year_med[a] > 0 else 1.0
        if 1.0 / 8.0 <= ratio <= 8.0:
            clusters[-1].append(b)
        else:
            clusters.append([b])

    cluster_info = []
    for cl in clusters:
        w = sum(len(by_year[y]) for y in cl)          # 票数按数据点计
        med = st.median([year_med[y] for y in cl])
        cluster_info.append({"years": cl, "votes": w, "median": med})
    base = max(cluster_info, key=lambda c: c["votes"])

    fixes = []
    for c in cluster_info:
        if c is base or c["median"] <= 0 or base["median"] <= 0:
            continue
        ratio = c["median"] / base["median"]
        k = int(round(math.log10(ratio)))
        if abs(k) < MAG_GAP:
            continue
        factor = 10.0 ** (-k)
        for y in c["years"]:
            fixes.append({
                "year": y, "median": year_med[y], "exp": -k,
                "factor": factor, "n": len(by_year[y]),
                "base_median": base["median"],
            })
    return fixes


def apply_scale(raw: List[Dict[str, Any]], fixes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    按年份段缩放 vol，但**逐日**用成交额锚点排除已正确的日期。

    为什么必须日级判断：量纲切换点在年中而非年边界。
    000813 的 2020 年有 31 天来自新源（vol=手、amount 存在、amt/vol≈1686），
    其余 212 天来自老源（vol=股、amount 缺失）。若按年整体 ÷100，
    会把那 31 天正确的数据改错 100 倍，制造一个"更隐蔽的断层"。

    判据：该日 amount>0 且 amt/vol 落在 [1e2,1e6]（手+元口径）→ 量纲已正确，跳过。
    """
    fy = {f["year"]: f["factor"] for f in fixes}
    logs: List[str] = []
    skipped: Dict[str, int] = defaultdict(int)
    scaled: Dict[str, int] = defaultdict(int)
    out: List[Dict[str, Any]] = []
    for r in raw:
        r2 = dict(r)
        y = str(r.get("date"))[:4]
        f = fy.get(y)
        if f and f != 1.0:
            try:
                v = float(r2.get("vol") or 0)
            except (TypeError, ValueError):
                v = 0.0
            try:
                a = float(r2.get("amount") or 0) if r2.get("amount") is not None else 0.0
            except (TypeError, ValueError):
                a = 0.0
            if v > 0:
                if a > 0 and AMT_VOL_LO <= (a / v) <= AMT_VOL_HI:
                    skipped[y] += 1          # 锚点证明本日已是「手+元」
                else:
                    v2 = v * f
                    if a > 0:
                        r2["amount"] = round(a * f, 4)
                        logs.append("%s %s amount 同步×%.3g" % (y, r2.get("date"), f))
                    r2["vol"] = round(v2, 4)
                    scaled[y] += 1
        out.append(r2)
    for y in sorted(set(list(skipped) + list(scaled))):
        logs.append("  年度明细 %s: 修正 %d 日 / 跳过(锚点已正确) %d 日" % (y, scaled.get(y, 0), skipped.get(y, 0)))
    return out, logs


def iter_targets(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    cache = os.path.normpath(os.path.join(HERE, cfg["data"]["cache_dir"]))
    out = []
    for u in cfg.get("universe", []) or []:
        c = str(u.get("code", ""))
        p = os.path.join(cache, "idx_%s.json" % c)
        if c and os.path.exists(p):
            out.append((c, p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认只预演）")
    ap.add_argument("--check", action="store_true", help="只检查，有断层则非 0 退出")
    ap.add_argument("--restore", default=None, help="从备份目录恢复")
    ap.add_argument("--limit", default=None, help="只处理单标的（调试）")
    args = ap.parse_args()

    cfg = load_cfg()

    if args.restore:
        src = args.restore
        if not os.path.isdir(src):
            print("[fix] 备份目录不存在: %s" % src)
            return 2
        n = 0
        for fn in os.listdir(src):
            if fn.startswith("idx_") and fn.endswith(".json"):
                shutil.copy2(os.path.join(src, fn), os.path.join(HERE, cfg["data"]["cache_dir"], fn))
                n += 1
        print("[fix] 已恢复 %d 个文件" % n)
        return 0

    targets = iter_targets(cfg)
    if args.limit:
        targets = [(c, p) for c, p in targets if c == args.limit]

    plan = []
    for code, path in targets:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:
            print("[fix] %s 读取失败: %s" % (code, exc))
            continue
        fixes = scan_series(raw)
        if fixes:
            plan.append({"code": code, "path": path, "fixes": fixes, "raw": raw})

    if not plan:
        print("[fix] 未发现 vol 量纲断层（MAG_GAP=%d）" % MAG_GAP)
        return 0

    print("[fix] 发现 %d 只标的、%d 处断层：" % (len(plan), sum(len(p["fixes"]) for p in plan)))
    for p in plan:
        for f in p["fixes"]:
            print("  %-8s %s  median=%.3e  基准=%.3e  修正×%.3g  (n=%d)"
                  % (p["code"], f["year"], f["median"], f["base_median"], f["factor"], f["n"]))

    if args.check:
        return 1
    if not args.apply:
        print("[fix] 预演结束，未写盘。确认后加 --apply")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(HERE, cfg["data"]["cache_dir"], "_cache_backup_unitfix_%s" % ts)
    os.makedirs(bak, exist_ok=True)
    print("[fix] 备份目录: %s" % bak)

    changed = 0
    report = []
    for p in plan:
        new, logs = apply_scale(p["raw"], p["fixes"])
        shutil.copy2(p["path"], os.path.join(bak, os.path.basename(p["path"])))
        with open(p["path"], "w", encoding="utf-8") as fh:
            json.dump(new, fh, ensure_ascii=False)
        changed += 1
        report.append({"code": p["code"], "fixes": p["fixes"], "amount_logs": logs[:5]})
        for lg in logs[:3]:
            print("    " + lg)

    rep_path = os.path.join(HERE, cfg["data"].get("output_dir", "data"), "unit_fix_report.json")
    os.makedirs(os.path.dirname(rep_path), exist_ok=True)
    with open(rep_path, "w", encoding="utf-8") as fh:
        json.dump({"as_of": ts, "backup": os.path.relpath(bak, HERE), "items": report}, fh,
                  ensure_ascii=False, indent=1)

    print("[fix] 已修复 %d 只，报告: %s" % (changed, rep_path))
    print("[fix] 回滚: python tools_fix_units.py --restore \"%s\"" % bak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
