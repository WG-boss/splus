# -*- coding: utf-8 -*-
"""tools_extend_history.py — 把 118 只 universe 的日线缓存向前扩展到 ≥6 年，并统一量纲。

为什么需要：
  1) 缓存深度严重不均：42 只不足 6 年，最浅仅 320 根（恒生科技 321 根、
     创业板50 557 根、科创50 574 根）。所有回放/配对检验只能退到最浅标的的
     公共尾窗（≈573 根 ≈2.3 年），样本不含 2022 式单边熊市，导致状态机
     实证结论（尤其 E 态左尾优势）缺乏关键压力期。
  2) 量纲污染：stock-pool/sync_universe_splus.py 的 sse_cni_db 兜底路径写缓存时
     用 vol×100（手→股）、amount÷1e8（元→亿元），与东财/腾讯路径（手 + 元）混在
     同一序列里。表现为 amount÷vol 从 ~1e3 突降到 ~1e-7，会让 vol_z20/er20 在接缝处
     失真，且 amount 阈值类判断（万元/亿元口径）整段失效。

本工具的规范化口径（与东财 push2his f56/f57 一致，也是 stock-pool review.py 的假设）：
  vol     = 手（lots）
  amount  = 元（CNY）

判定规则（amount/vol 比值 r）：
  r >= 1e-3  → 已经是「手 + 元」，不动
  r <  1e-3  → 「股 + 亿元」，改为 vol÷100、amount×1e8
  无 amount  → 保留（腾讯源无 amount），后续由审计工具单独统计覆盖率

用法：
  python tools_extend_history.py                    # 扩展到目标 6 年
  python tools_extend_history.py --dry-run          # 只报告不写文件
  python tools_extend_history.py --codes 000688,399673
  python tools_extend_history.py --years 6 --asof 2026-11-06
  python tools_extend_history.py --check            # 校验已达标（幂等，退出码 0/1）

退出码：0=达标；1=存在未达标/失败标的（清单不静默）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "stock-pool"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

CFG = os.path.join(HERE, "splus_config.yaml")
BACKUP_DIR = os.path.join(HERE, "data", "_cache_backup_extend")
SNAP_DIR = os.path.join(HERE, "data", "_cache_backup_20261107")  # 首次深拉前的原样快照

# 6 年目标：A 股年均约 242 个交易日
BARS_PER_YEAR = 242
# 达标双口径：① 根数 ≥ years×242；② 首根日期 ≤ asof − years（覆盖满窗口，
# 上市晚于窗口起点的指数不可能有更多根，不能用根数判失败）
CLOSE_HHMM = (15, 10)


# ------------------------------------------------------------------ 基础工具
def cache_dir() -> str:
    c = yaml.safe_load(open(CFG, encoding="utf-8"))
    return os.path.join(HERE, c["data"]["cache_dir"])


def load_universe() -> list:
    c = yaml.safe_load(open(CFG, encoding="utf-8"))
    return [x for x in c["universe"] if x.get("enabled", True)]


def last_closed_trading_day(now: dt.datetime | None = None) -> dt.date:
    now = now or dt.datetime.now()
    d = now.date()
    if d.weekday() >= 5:
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d
    if (now.hour, now.minute) < CLOSE_HHMM:
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
    return d


def path_of(code: str, cdir: str) -> str:
    safe = code.replace(".", "_").replace("/", "_")
    return os.path.join(cdir, "idx_%s.json" % safe)


def read_bars(p: str) -> list:
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def normalize_units(bars: list) -> tuple[list, int]:
    """把手/股、元/亿元统一到「手 + 元」。返回 (bars, 修正根数)。"""
    fixed = 0
    for b in bars:
        try:
            a = b.get("amount")
            v = b.get("vol")
            a = float(a) if a not in (None, "") else None
            v = float(v) if v not in (None, "") else None
        except Exception:
            continue
        if not a or not v or v <= 0:
            continue
        r = a / v
        if r < 1e-3:               # 股 + 亿元 → 手 + 元
            b["vol"] = v / 100.0
            b["amount"] = a * 1e8
            fixed += 1
    return bars, fixed


def trim_to_tail_segment(bars: list, max_gap_days: int = 45) -> tuple[list, dict]:
    """截断到「以最后一根结尾的最大连续段」。

    为什么必须做：部分标的在指数**正式发布前**有稀疏的基期回补数据
    （如科创50 真实发布 2020-07-22、中证2000 2014-10 发布），这些片段
    只有零星几十根 K 线，中间空档数百天。它们的量能与形态完全不具可比性，
    一旦进入 EMA/波动率/状态机，会把预热期污染成噪声。
    A 股最长连续休市（春节）约 12 天，45 天以上空洞只可能来自数据缺口。
    """
    gap = None
    cut = 0
    for i in range(len(bars) - 1, 0, -1):
        a = dt.date.fromisoformat(str(bars[i - 1]["date"])[:10])
        b = dt.date.fromisoformat(str(bars[i]["date"])[:10])
        if (b - a).days > max_gap_days:
            cut = i
            gap = ((b - a).days, bars[i - 1]["date"], bars[i]["date"])
            break
    if not cut:
        return bars, {"trimmed": 0}
    return bars[cut:], {"trimmed": cut, "gap_days": gap[0],
                        "gap_from": gap[1], "gap_to": gap[2]}


def sanitize(bars: list, asof: str) -> list:
    m = {}
    for b in bars:
        d = str(b.get("date") or "")[:10]
        if not d or d > asof:
            continue
        # 周末伪 K 线（2026-11-07 实测：980001 湾创100 在 2025-02-08/09 出现
        # O=H=L=C=9323.2、量为零头的占位记录，制造 +634% 假跳变）。指数不存在
        # 周末成交，一律剔除；A/港/美三个市场都不例外。
        try:
            if dt.date.fromisoformat(d).weekday() >= 5:
                continue
        except Exception:
            continue
        try:
            c = float(b.get("close"))
        except Exception:
            continue
        if not (c > 0):
            continue
        rec = {"date": d}
        for k in ("open", "high", "low"):
            try:
                val = float(b.get(k)) if b.get(k) not in (None, "") else c
            except Exception:
                val = c
            rec[k] = val if val > 0 else c
        rec["close"] = c
        for k in ("vol", "amount"):
            try:
                val = float(b.get(k)) if b.get(k) not in (None, "") else None
            except Exception:
                val = None
            rec[k] = val
        m[d] = rec
    return [m[k] for k in sorted(m)]


def backup_once(p: str) -> None:
    if not os.path.exists(p):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, os.path.basename(p))
    if os.path.exists(dst):
        return
    try:
        shutil.copy2(p, dst)
    except Exception:
        pass


# ------------------------------------------------------------------ 主流程
def fetch_full(code: str, days: int) -> list:
    from data_layer import index_global as ig
    for attempt in range(2):
        try:
            b = ig.bars(code, days=days, use_cache=False)
            if b:
                return b
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1.5)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=6.0)
    ap.add_argument("--asof", default="")
    ap.add_argument("--codes", default="")
    ap.add_argument("--days", type=int, default=0, help="覆盖目标根数（默认按 years 推算 +10%% 余量）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trim-only", action="store_true",
                    help="不联网，仅对现有缓存做量纲归一化 + 空洞截断")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    cdir = cache_dir()
    asof = args.asof or last_closed_trading_day().isoformat()
    target = args.days or int(args.years * BARS_PER_YEAR)         # 6 年 → 1452 根
    fetch_days = max(target, 320)
    cutoff = (dt.date.fromisoformat(asof)
              - dt.timedelta(days=int(round(args.years * 365.25)))).isoformat()

    def covered(n: int, first: str) -> bool:
        return n >= target or (first and first != "-" and first <= cutoff)

    uni = load_universe()
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        uni = [x for x in uni if x["code"] in want]

    print("[extend] 截止日=%s 目标=%d 根 / 覆盖起点≤%s 标的=%d"
          % (asof, target, cutoff, len(uni)))
    t0 = time.time()

    done, late, short, fail = [], [], [], []
    trimmed = []
    unit_fixed = 0

    for i, item in enumerate(uni, 1):
        code, name = item["code"], item["name"]
        p = path_of(code, cdir)
        old = read_bars(p)
        old_n, old_first = len(old), (old[0]["date"] if old else "-")

        if args.check:
            if covered(old_n, old_first):
                done.append((code, name, old_n, old_first))
            elif old_n >= 300:
                late.append((code, name, old_n, old_first))
            else:
                short.append((code, name, old_n, old_first))
            continue

        fresh = [] if args.trim_only else fetch_full(code, fetch_days)
        # 合并：以 fresh 为主，old 仅补 fresh 未覆盖的日期（避免历史被缩短）
        merged = {str(r["date"])[:10]: r for r in fresh}
        for r in old:
            d = str(r.get("date") or "")[:10]
            if d and d not in merged:
                merged[d] = r
        bars = sanitize(list(merged.values()), asof)
        bars, fixed = normalize_units(bars)
        bars, tr = trim_to_tail_segment(bars)
        unit_fixed += fixed
        if tr.get("trimmed"):
            trimmed.append((code, name, tr["trimmed"], tr.get("gap_days", 0),
                            tr.get("gap_from", ""), bars[0]["date"]))

        if not bars:
            fail.append((code, name, "no bars"))
            continue

        if not args.dry_run:
            backup_once(p)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(bars, fh, ensure_ascii=False)
            os.replace(tmp, p)

        n, first = len(bars), bars[0]["date"]
        added = n - old_n
        ok_depth = covered(n, first)
        if ok_depth:
            done.append((code, name, n, first))
        elif n >= 300:
            late.append((code, name, n, first))
        else:
            short.append((code, name, n, first))
        print("  %-9s %-16s %4d→%4d (+%3d) %s %s"
              % (code, name, old_n, n, added, first,
                 "" if ok_depth else "← 未覆盖满 6 年窗口"), flush=True)

        if i % 20 == 0:
            print("  ... %d/%d  %.0fs" % (i, len(uni), time.time() - t0), flush=True)

    print("\n[extend] 达标（≥%d 根 或 首根≤%s）: %d 只" % (target, cutoff, len(done)))
    if late:
        print("[extend] 深度不足但≥300 根（指数上市较晚，非抓取失败）: %d 只" % len(late))
        for code, name, n, first in late:
            print("   - %-9s %-16s n=%-4d first=%s" % (code, name, n, first))
    if short:
        print("[extend] 深度 <300 根（需人工核查）: %d 只" % len(short))
        for code, name, n, first in short:
            print("   - %-9s %-16s n=%-4d first=%s" % (code, name, n, first))
    if fail:
        print("[extend] 失败: %d 只" % len(fail))
        for code, name, why in fail:
            print("   - %-9s %-16s %s" % (code, name, why))

    if not args.check:
        print("[extend] 量纲归一化修正 K 线: %d 根" % unit_fixed)
        if trimmed:
            print("[extend] 空洞截断（剔除发布前稀疏回补段）: %d 只" % len(trimmed))
            for code, name, n_cut, gd, gf, nf in trimmed:
                print("   - %-9s %-16s 剔除 %d 根（空洞 %d 天 @%s），现首根 %s"
                      % (code, name, n_cut, gd, gf, nf))
        rep = {"asof": asof, "target_bars": target, "years": args.years,
               "cutoff": cutoff, "unit_fixed_bars": unit_fixed,
               "trimmed": [{"code": c, "name": n, "bars_removed": k,
                            "gap_days": g, "gap_from": gf, "new_first": nf}
                           for c, n, k, g, gf, nf in trimmed],
               "done": [{"code": c, "name": n, "bars": b, "first": f} for c, n, b, f in done],
               "late": [{"code": c, "name": n, "bars": b, "first": f} for c, n, b, f in late],
               "short": [{"code": c, "name": n, "bars": b, "first": f} for c, n, b, f in short],
               "fail": [{"code": c, "name": n, "why": w} for c, n, w in fail],
               "elapsed_s": round(time.time() - t0, 1)}
        os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
        with open(os.path.join(HERE, "data", "extend_report.json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)

    bad = len(short) + len(fail)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
