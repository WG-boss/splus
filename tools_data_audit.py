# -*- coding: utf-8 -*-
"""tools_data_audit.py — 指数日线缓存的数据质量审计（只读）。

背景：
  2026-11-07 把 118 只 universe 的缓存深拉到 6 年后，必须回答一个问题：
  这份库「能不能作为实证结论的地基」。本工具不生产结论，只出证据。

五个维度：
  A 结构完整性  —— 解析、升序、无重复日期、OHLC 自洽（low≤min(o,c)≤max(o,c)≤high）、
                  价格为正、非零成交量占比。
  B 量纲一致性  —— amount/vol 比值判定单位约定；并反推「隐含每股均价」
                  amount/(vol×100)，正常应在 0.3~800 元。残留「股+亿元」会表现为
                  隐含均价 ≈1e-6 元 → 直接抓出。跨日跳变 >50 倍也单独标记
                  （这是量纲接缝的典型指纹）。
  C 交易日连续性 —— 用全体标的日期的并集（≥60% 标的都有数据的日子）反推市场日历，
                 逐只统计「自身区间内缺失的交易日」= 断档。
  D 跨源复核    —— 随机抽样 N 只 × 最近 M 个交易日，用腾讯 gtimg 独立重取，
                 比较 close 相对偏差（阈值 0.15%，覆盖指数点位四舍五入）。
  E 新鲜度/深度 —— 末根是否到 asof；根数是否覆盖 6 年窗口（或上市不久）。

用法：
  python tools_data_audit.py                    # 全量审计 + 跨源抽检 12 只
  python tools_data_audit.py --sample 20        # 扩大抽检
  python tools_data_audit.py --no-crosscheck    # 只做本地五项（离线可用）
  python tools_data_audit.py --check            # 严格模式：任何 FAIL 即退出码 1

退出码：0=无 FAIL；1=存在 FAIL（清单不静默）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "stock-pool"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

CFG = os.path.join(HERE, "splus_config.yaml")
BARS_PER_YEAR = 242
CROSS_TOL = 0.0015          # 跨源 close 相对偏差容忍度 0.15%
PRICE_LO, PRICE_HI = 0.3, 800.0   # 隐含每股均价的合理区间（元）


def cache_dir() -> str:
    c = yaml.safe_load(open(CFG, encoding="utf-8"))
    return os.path.join(HERE, c["data"]["cache_dir"])


def load_universe() -> list:
    c = yaml.safe_load(open(CFG, encoding="utf-8"))
    return [x for x in c["universe"] if x.get("enabled", True)]


def path_of(code: str, cdir: str) -> str:
    return os.path.join(cdir, "idx_%s.json" % code.replace(".", "_").replace("/", "_"))


def class_of(code: str, name: str = "") -> str:
    """交易日历分类：A 股 / 港股 / 港股通 / 美股。

    必要性：港股与美股休市日与 A 股不同，港股通指数还要两边都开市。
    用统一 A 股日历判缺口，会把「香港重阳节休市」误判成数据断档。
    """
    c = code.strip()
    low = c.lower()
    if low.startswith("us"):
        return "us"
    if low.startswith("hk"):
        return "hk"
    if c.startswith("987") or "港股通" in (name or "") or "港股" in (name or "") \
            or "香港" in (name or ""):
        return "hkx"
    return "a"


def _f(x):
    try:
        v = float(x)
        return v if v == v else None       # 过滤 NaN
    except Exception:
        return None


# ---------------------------------------------------------------- A 结构
def check_structure(bars: list) -> tuple[list, list, dict]:
    hard, soft = [], []
    dates = [str(b.get("date")) for b in bars]
    dup = len(dates) - len(set(dates))
    if dup:
        hard.append("重复日期 %d 条" % dup)
    if dates != sorted(dates):
        hard.append("日期未升序")

    bad_ohlc, neg, zero_vol, round_noise = 0, 0, 0, 0
    weekend = 0
    for b in bars:
        try:
            if dt.date.fromisoformat(str(b.get("date"))[:10]).weekday() >= 5:
                weekend += 1
        except Exception:
            pass
        o, h, l, c = (_f(b.get("open")), _f(b.get("high")),
                      _f(b.get("low")), _f(b.get("close")))
        if None in (o, h, l, c):
            bad_ohlc += 1
            continue
        if min(o, c, l) <= 0:
            neg += 1
        # 三级判据（以收盘价归一）：
        #   >0.1%  → 硬失败（真实结构错位）
        #   2e-5~0.1% → 舍入噪声（中证/国证只保留 4 位小数，实测最大 0.03%）
        #   <2e-5  → 忽略
        px = max(abs(o), abs(h), abs(l), abs(c), 1e-9)
        viol = max(0.0, max(o, c) - h) + max(0.0, l - min(o, c)) + max(0.0, l - h)
        if viol / px > 1e-3:
            bad_ohlc += 1
        elif viol / px > 2e-5:
            round_noise += 1
        v = _f(b.get("vol")) or 0
        if v <= 0:
            zero_vol += 1
    n = max(len(bars), 1)
    if bad_ohlc:
        hard.append("OHLC 自洽失败 %d 条" % bad_ohlc)
    if neg:
        hard.append("非正价格 %d 条" % neg)
    if zero_vol / n > 0.02:
        soft.append("零成交量占比 %.1f%%" % (100 * zero_vol / n))
    if round_noise:
        soft.append("OHLC 舍入噪声 %d 条（<0.1%%，不影响特征）" % round_noise)
    if weekend:
        hard.append("周末 K 线 %d 条（伪数据）" % weekend)

    # 单日跳变：>30% 基本只可能来自串号/拼接/占位脏数据（实测最大合法值为
    # 港股 2024-10-02 的 +28.6%，长假后补涨），超过即判硬失败。
    worst = (0.0, "")
    for a, b in zip(bars, bars[1:]):
        c1, c2 = _f(a.get("close")), _f(b.get("close"))
        if c1 and c2 and c1 > 0:
            r = c2 / c1 - 1.0
            if abs(r) > abs(worst[0]):
                worst = (r, str(b.get("date")))
    if abs(worst[0]) > 0.30:
        hard.append("单日跳变 %.1f%% @%s" % (worst[0] * 100, worst[1]))

    return hard, soft, {"n": len(bars), "zero_vol_pct": round(100 * zero_vol / n, 2),
                        "dup": dup, "round_noise": round_noise}


# ---------------------------------------------------------------- B 量纲
def check_units(bars: list) -> tuple[list, dict]:
    """返回 (告警列表, 统计)。"""
    warn = []
    bad_conv, impl_prices, jumps = 0, [], 0
    prev_ip = None
    for b in bars:
        v, a = _f(b.get("vol")), _f(b.get("amount"))
        if not v or not a or v <= 0 or a <= 0:
            prev_ip = None
            continue
        r = a / v
        if r < 1e-3:                       # 仍是「股+亿元」
            bad_conv += 1
            prev_ip = None
            continue
        ip = a / (v * 100.0)               # 隐含每股均价（元）
        impl_prices.append(ip)
        if ip < PRICE_LO or ip > PRICE_HI:
            bad_conv += 1
        if prev_ip and prev_ip > 0:
            ratio = max(ip, prev_ip) / min(ip, prev_ip)
            if ratio > 50:
                jumps += 1
        prev_ip = ip
    if bad_conv:
        warn.append("量纲异常 %d 条（残留股+亿元或隐含均价越界）" % bad_conv)
    if jumps:
        warn.append("隐含均价跨日跳变>50x %d 处（量纲接缝）" % jumps)
    amt_cov = sum(1 for b in bars if (_f(b.get("amount")) or 0) > 0) / max(len(bars), 1)
    stats = {"implied_price_med": round(statistics.median(impl_prices), 2) if impl_prices else None,
             "bad_unit_bars": bad_conv, "jumps": jumps,
             "amount_coverage_pct": round(100 * amt_cov, 1)}
    return warn, stats


# ---------------------------------------------------------------- D 跨源
def tencent_close(code: str, days: int = 40) -> dict:
    if code.startswith("sz") or code.startswith("sh"):
        sym = code
    elif code.startswith("39"):
        sym = "sz" + code
    elif code.startswith(("000", "93", "95", "H")):
        sym = "sh" + code
    elif code.lower().startswith("hk"):
        sym = "hk" + code[2:]
    elif code.lower().startswith("us"):
        sym = "us" + code[2:]
    else:
        return {}
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,,,%d,qfq" % (sym, days))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=25).read().decode()
        j = json.loads(raw)
    except Exception:
        return {}
    d = (j.get("data") or {}).get(sym) or {}
    rows = d.get("day") or d.get("qfqday") or []
    out = {}
    for r in rows:
        try:
            out[str(r[0])[:10]] = float(r[2])
        except Exception:
            continue
    return out


def csi_close(code: str, days: int = 120) -> dict:
    """中证官网独立复核源（仅 6 位数字/H 码）。"""
    try:
        from data_layer import index_global as ig
    except Exception:
        return {}
    try:
        rows = ig._csindex_bars(code, days)
    except Exception:
        return {}
    out = {}
    for r in rows or []:
        c = _f(r.get("close"))
        if c:
            out[str(r.get("date"))[:10]] = c
    return out


def sina_close(code: str, days: int = 60) -> dict:
    """新浪日线（A 股指数独立第三源）。腾讯对本机限流、中证不覆盖 399 系时用它兜底。"""
    c = code.strip()
    if c.startswith(("39",)):
        sym = "sz" + c
    elif c[:6].isdigit():
        sym = "sh" + c
    else:
        return {}
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, days))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        arr = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception:
        return {}
    out = {}
    for r in arr or []:
        c0 = _f(r.get("close"))
        if c0:
            out[str(r.get("day"))[:10]] = c0
    return out


def cni_close(code: str, days: int = 120) -> dict:
    """国证官网（970/980/987/CN 系）。"""
    try:
        from data_layer import index_global as ig
        rows = ig._cnindex_bars(code, days)
    except Exception:
        return {}
    out = {}
    for r in rows or []:
        c = _f(r.get("close"))
        if c:
            out[str(r.get("date"))[:10]] = c
    return out


def check_cross(bars: list, code: str, recent: int = 5) -> tuple[list, dict]:
    """用独立源复核最近 N 根收盘价。

    说明：腾讯与中证官网两条独立链路。若缓存本身来自其中之一，该源只能证明
    「写入无损」；两源都取到时才构成真正的跨源互证。报告中分别列出，不合并结论。
    """
    tail = bars[-recent:] if len(bars) >= recent else bars
    if not tail:
        return [], {"checked": 0, "mismatch": 0}

    c = code.strip()
    # 源路由（按代码归属，而不是"只要是 6 位数字"）：
    #   中证官网：000/930/931/932 与 H 码
    #   国证官网：970/980/987/CN（中证官网对 980001 会串号返回另一个指数，
    #             实测差 10 倍——所以国证系绝不能拿中证源复核）
    #   新浪：仅 000/399（沪深交易所口径）
    csi_like = c[0].upper() == "H" or (c[:6].isdigit() and c[:3] in ("000", "930", "931", "932"))
    cni_like = c[:2] in ("97", "98") or c.startswith("CN")
    sources = {}
    if csi_like:
        r = csi_close(code, days=150)
        if r:
            sources["中证官网"] = r
    if cni_like:
        r = cni_close(code, days=150)
        if r:
            sources["国证官网"] = r
    if c[:6].isdigit() and (c.startswith("000") or c.startswith("399")):
        r = sina_close(code, days=60)
        if r:
            sources["新浪"] = r
    tx = {}
    for _ in range(2):
        tx = tencent_close(code, days=60)
        if tx:
            break
        time.sleep(1.2)
    if tx:
        sources["腾讯"] = tx
    if not sources:
        return [], {"checked": 0, "mismatch": 0, "note": "复核源均不可用（计为未验证）"}

    mism, total_checked, per_src = [], 0, {}
    for sname, ref in sources.items():
        chk, mm, worst = 0, [], 0.0
        for b in tail:
            d = str(b.get("date"))
            if d not in ref:
                continue
            chk += 1
            c0 = _f(b.get("close"))
            if not c0:
                continue
            rel = abs(c0 - ref[d]) / ref[d]
            worst = max(worst, rel)
            if rel > CROSS_TOL:
                mm.append("%s 本地%.2f %s%.2f (%.2f%%)" % (d, c0, sname, ref[d], rel * 100))
        per_src[sname] = {"checked": chk, "mismatch": len(mm),
                          "worst_rel_pct": round(100 * worst, 3)}
        total_checked += chk
        if mm:
            mism.extend(mm[:2])

    return mism, {"checked": total_checked, "mismatch": len(mism),
                  "sources": list(sources), "per_source": per_src,
                  "worst_rel_pct": max([v["worst_rel_pct"] for v in per_src.values()] or [0])}


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="")
    ap.add_argument("--sample", type=int, default=12, help="跨源抽检只数（0=关闭）")
    ap.add_argument("--recent", type=int, default=5, help="每只抽检最近 N 个交易日")
    ap.add_argument("--years", type=float, default=6.0)
    ap.add_argument("--no-crosscheck", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default="data/data_audit_report.json")
    args = ap.parse_args()

    cdir = cache_dir()
    uni = load_universe()
    asof = args.asof
    if not asof:
        all_last = []
        for u in uni:
            p = path_of(u["code"], cdir)
            if os.path.exists(p):
                try:
                    all_last.append(json.load(open(p, encoding="utf-8"))[-1]["date"])
                except Exception:
                    pass
        asof = max(all_last) if all_last else dt.date.today().isoformat()
    cutoff = (dt.date.fromisoformat(asof)
              - dt.timedelta(days=int(round(args.years * 365.25)))).isoformat()
    target_bars = int(args.years * BARS_PER_YEAR)

    data = {}
    for u in uni:
        p = path_of(u["code"], cdir)
        if not os.path.exists(p):
            data[u["code"]] = (u, None)
            continue
        try:
            bars = json.load(open(p, encoding="utf-8"))
        except Exception:
            bars = None
        data[u["code"]] = (u, bars)

    # ---- 日历构建（两遍法）----
    # 第一遍：按命名/代码规则粗分。
    # 第二遍：用「A 股核心日历」反证——某只自称 A 股，却在 ≥3 个非 A 股交易日有数据，
    #        说明它其实按港股历交易（如「湾创100」含港股成分、港股通系），改判 hkx。
    cls_map = {u["code"]: class_of(u["code"], u.get("name")) for u in uni}

    def build_cal(subset: dict) -> list:
        cnt = {}
        for c in subset:
            for b in subset[c]:
                cnt[str(b.get("date"))] = cnt.get(str(b.get("date")), 0) + 1
        # 类内样本少时阈值降到 1：否则 hk/us 这类单成员市场的日历会是空集，
        # 空集会被上层 or 回退到 A 股日历，反而把正常休市误判成断档。
        thr = 1 if len(subset) < 4 else max(2, int(0.5 * len(subset)))
        return sorted(d for d, n in cnt.items() if n >= thr)

    valid = {c: v for c, v in data.items() if v[1]}
    a_codes = {c: valid[c][1] for c in valid if cls_map.get(c) == "a"}
    a_cal = set(build_cal(a_codes)) if a_codes else set()
    if a_cal:
        for c, bars in list(a_codes.items()):
            ds = {str(b.get("date")) for b in bars}
            off = [d for d in ds if d not in a_cal]
            if len(off) >= 10:            # 阈值 10：避免个别假日脏数据误判
                cls_map[c] = "hkx"

    calendars = {}
    for cls in set(cls_map.values()):
        sub = {c: valid[c][1] for c in valid if cls_map.get(c) == cls}
        if sub:
            calendars[cls] = build_cal(sub)
    calendar = calendars.get("a") or sorted(set().union(*[set(v) for v in calendars.values()]))
    reclassed = [c for c in valid if cls_map.get(c) != class_of(c, valid[c][0].get("name"))]

    print("[audit] asof=%s 目标=%d 根 / 覆盖起点≤%s 标的=%d 日历=%d 日(%s~%s)"
          % (asof, target_bars, cutoff, len(uni), len(calendar),
             calendar[0] if calendar else "-", calendar[-1] if calendar else "-"))

    fails, warns, per = [], [], {}
    depth_ok = 0
    for code, (u, bars) in data.items():
        name = u.get("name")
        if not bars:
            fails.append((code, name, "文件缺失或不可解析"))
            per[code] = {"name": name, "status": "FAIL", "issues": ["文件缺失"]}
            continue
        hard, soft, stat = check_structure(bars)
        uw, ustat = check_units(bars)
        first, last = str(bars[0].get("date")), str(bars[-1].get("date"))

        # C 连续性：按本标的所属市场日历，统计区间内缺失的交易日
        cls = class_of(code, name)
        cal = calendars.get(cls) or calendar
        span = [d for d in cal if first <= d <= last]
        have = {str(b.get("date")) for b in bars}
        gaps = [d for d in span if d not in have]
        gap_pct = 100.0 * len(gaps) / max(len(span), 1)
        if len(gaps) > 5 or gap_pct > 0.5:
            msg = "断档 %d 日（%.1f%%）首缺 %s" % (len(gaps), gap_pct, gaps[0] if gaps else "-")
            # 港股/港股通/美股的交易日历与 A 股不同（台风休市、结算日、半边市），
            # 缺口属制度性差异，降级为提示；只有 A 股日历内的缺口才算硬失败。
            (hard if cls == "a" else soft).append(msg)

        # E 深度
        covers = len(bars) >= target_bars or first <= cutoff
        if covers:
            depth_ok += 1
        if last < asof:
            soft.append("末根 %s 落后 asof" % last)

        issues = hard + soft + uw
        status = "FAIL" if hard else ("WARN" if issues else "OK")
        if hard:
            fails.extend([(code, name, h) for h in hard])
        if soft or uw:
            warns.extend([(code, name, w) for w in (soft + uw)])
        per[code] = {"name": name, "status": status, "bars": len(bars),
                     "first": first, "last": last, "covers_6y": covers,
                     "gaps": len(gaps), "issues": issues[:8], **stat, **ustat}

    # D 跨源抽检
    cross = {}
    if args.sample > 0 and not args.no_crosscheck:
        pool = [c for c, (u, b) in data.items() if b and c in per]
        random.seed(20261107)
        picks = random.sample(pool, min(args.sample, len(pool)))
        print("[audit] 跨源抽检 %d 只 × 最近 %d 日 ..." % (len(picks), args.recent))
        agg_checked, agg_mism, unverified = 0, 0, 0
        for code in picks:
            bars = data[code][1]
            mism, st = check_cross(bars, code, args.recent)
            cross[code] = {"name": per[code]["name"], **st, "mismatch_detail": mism[:3]}
            agg_checked += st.get("checked", 0)
            agg_mism += st.get("mismatch", 0)
            if st.get("note"):
                unverified += 1
                print("   - %-9s %s" % (code, st["note"]))
            elif mism:
                print("   ! %-9s 偏差 %d/%d  最差 %.3f%%"
                      % (code, st["mismatch"], st["checked"], st.get("worst_rel_pct", 0)))
            else:
                ps = st.get("per_source") or {}
                tag = " / ".join("%s %d点" % (k, v["checked"]) for k, v in ps.items())
                print("   ✓ %-9s 一致（%s，最差 %.3f%%）"
                      % (code, tag or "无", st.get("worst_rel_pct", 0)))
            time.sleep(0.8)
        print("[audit] 跨源合计：比对 %d 个点，偏差 %d 个，未验证 %d 只（容忍 %.2f%%）"
              % (agg_checked, agg_mism, unverified, CROSS_TOL * 100))
        if agg_mism:
            fails.append(("CROSS", "-", "跨源比对偏差 %d/%d 点" % (agg_mism, agg_checked)))

    # ---------------------------------------------------------- 汇总
    ok = sum(1 for v in per.values() if v["status"] == "OK")
    warn = sum(1 for v in per.values() if v["status"] == "WARN")
    fail = sum(1 for v in per.values() if v["status"] == "FAIL")
    total_bars = sum(v.get("bars", 0) for v in per.values())
    depth = {"target_bars": target_bars, "cutoff": cutoff, "covers": depth_ok,
             "not_covered": [{"code": c, "name": v["name"], "bars": v.get("bars"),
                              "first": v.get("first")}
                             for c, v in per.items() if not v.get("covers_6y")]}
    print("\n[audit] 结构/量纲/连续性：OK %d | WARN %d | FAIL %d | 总 K 线 %d 根"
          % (ok, warn, fail, total_bars))
    print("[audit] 6 年覆盖：%d/%d 只达标；未达标 %d 只"
          % (depth_ok, len(per), len(depth["not_covered"])))
    for it in depth["not_covered"][:12]:
        print("   - %-9s %-16s n=%-4s first=%s"
              % (it["code"], it["name"], it["bars"], it["first"]))
    if fails:
        print("\n[audit] FAIL 明细（前 20）:")
        for code, name, why in fails[:20]:
            print("   ! %-9s %-16s %s" % (code, name, why))
    if warns:
        print("\n[audit] WARN 明细（前 12）:")
        for code, name, why in warns[:12]:
            print("   ~ %-9s %-16s %s" % (code, name, why))

    rep = {"as_of_audit": asof, "target_bars": target_bars, "cutoff": cutoff,
           "calendar_days": len(calendar),
           "calendar_range": [calendar[0], calendar[-1]] if calendar else None,
           "total_bars": total_bars,
           "status": {"OK": ok, "WARN": warn, "FAIL": fail},
           "depth": depth, "cross_check": cross,
           "fails": [{"code": c, "name": n, "why": w} for c, n, w in fails],
           "warns": [{"code": c, "name": n, "why": w} for c, n, w in warns],
           "per_code": per}
    with open(os.path.join(HERE, args.out), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print("\n[audit] 报告 → %s" % args.out)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
