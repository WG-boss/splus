"""五态评价原始数据体检：针对 status machine 实际消费的 K 线与特征。

只回答四个问题：覆盖全不全、字段缺不缺、口径一致不一致、历史可不可用。
输出同时写 JSON，供报告引用。
"""
import argparse
import collections
import json
import os
import statistics
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.path.dirname(BASE), "stock-pool", "_cache")
PANEL = os.path.join(BASE, "state", "state_panel.jsonl")


def latest_decision():
    """默认取最新一份决策 JSON，便于每日复跑。"""
    d = os.path.join(BASE, "data")
    files = [f for f in os.listdir(d) if f.startswith("splus_decision_") and f.endswith(".json")]
    if not files:
        return os.path.join(d, "splus_decision_20261106.json")
    return os.path.join(d, sorted(files)[-1])


def load_bars(code):
    p = os.path.join(CACHE, "idx_%s.json" % code)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--decision", default="", help="决策 JSON 路径；缺省取最新一份")
    a = ap.parse_args()

    dec_path = a.decision or latest_decision()
    with open(dec_path, encoding="utf-8") as fh:
        dec = json.load(fh)
    print("决策文件：%s（as_of %s）" % (os.path.basename(dec_path), dec["as_of"]))
    pi = dec["per_index"]
    codes = list(pi)

    # ---------- 1. 覆盖 ----------
    cov = {}
    for c in codes:
        d = load_bars(c)
        if not d:
            cov[c] = None
            continue
        cov[c] = {
            "n": len(d),
            "start": d[0]["date"],
            "end": d[-1]["date"],
            "no_ohlc": sum(1 for b in d if b.get("high") is None or b.get("low") is None or b.get("open") is None),
            "no_vol": sum(1 for b in d if b.get("vol") in (None, 0)),
            "no_amt": sum(1 for b in d if b.get("amount") in (None, 0)),
            "flat": sum(1 for b in d if b["high"] == b["low"] == b["close"]),
            "dup": len(d) - len({b["date"] for b in d}),
            "bad_hl": sum(1 for b in d
                          if not (b["low"] - 1e-9 <= b["close"] <= b["high"] + 1e-9) or b["high"] < b["low"]),
        }
    ok = {c: v for c, v in cov.items() if v}
    print("=" * 70)
    print("[1] 覆盖：宇宙 %d 只，可读 %d 只" % (len(codes), len(ok)))
    ns = [v["n"] for v in ok.values()]
    print("    bar 数 min %d / p10 %d / 中位 %d / max %d"
          % (min(ns), sorted(ns)[len(ns) // 10], statistics.median(ns), max(ns)))
    print("    起始日：%s" % dict(collections.Counter(v["start"] for v in ok.values()).most_common(5)))
    print("    结束日：%s" % dict(collections.Counter(v["end"] for v in ok.values()).most_common(5)))
    print("    重复日期 %d 只；OHLC 逻辑违规 %d 只"
          % (sum(1 for v in ok.values() if v["dup"]), sum(1 for v in ok.values() if v["bad_hl"])))

    # ---------- 2. 字段缺失 ----------
    print("=" * 70)
    print("[2] 字段缺失")
    for k, lbl in (("no_ohlc", "OHLC"), ("no_vol", "vol"), ("no_amt", "amount")):
        aff = [(c, v[k]) for c, v in ok.items() if v[k] > 0]
        print("    %-6s 受影响 %d 只 / 共 %d 行%s"
              % (lbl, len(aff), sum(n for _, n in aff),
                 ("：" + ", ".join("%s(%d)" % (pi[c]["name"], n) for c, n in aff[:8])) if aff else ""))

    # ---------- 3. 合成 OHLC（口径退化）----------
    print("=" * 70)
    print("[3] 合成 OHLC（open=high=low=close）：ATR/ADX/hhll 会退化")
    flat_rows = []
    for c, v in ok.items():
        if v["flat"]:
            flat_rows.append((c, pi[c]["name"], v["flat"], v["n"], v["flat"] / v["n"]))
    flat_rows.sort(key=lambda t: -t[2])
    for c, nm, f, n, r in flat_rows[:10]:
        seg = longest_run(load_bars(c))
        tag = "  <-- 当前 252 窗口全平" if recent_flat(load_bars(c)) else ""
        print("    %-12s %-8s %5d/%-5d %5.1f%%  最长连续 %d 根%s" % (nm, c, f, n, r * 100, seg, tag))
    print("    合计 %d 只存在合成 OHLC；其中当前 252 日窗口仍全平 %d 只"
          % (len(flat_rows), sum(1 for c, *_ in flat_rows if recent_flat(load_bars(c)))))

    # ---------- 4. 历史面板污染 ----------
    print("=" * 70)
    print("[4] 历史面板污染（state_panel.jsonl）")
    rows = []
    with open(PANEL, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    flat_map, win_map = {}, {}
    for c in codes:
        d = load_bars(c)
        if not d:
            continue
        fl = [1 if b["high"] == b["low"] == b["close"] else 0 for b in d]
        pref = [0]
        for x in fl:
            pref.append(pref[-1] + x)
        flat_map[c] = {b["date"]: bool(fl[i]) for i, b in enumerate(d)}
        win_map[c] = (pref, {b["date"]: i for i, b in enumerate(d)})
    hit_day = hit_win = tot = 0
    for o in rows:
        tot += 1
        if flat_map.get(o["c"], {}).get(o["d"]):
            hit_day += 1
        pref, di = win_map.get(o["c"], (None, None))
        if pref is None:
            continue
        i = di.get(o["d"])
        if i is None:
            continue
        lo = max(1, i - 251)
        w = pref[i + 1] - pref[lo]
        if w / max(1, i + 1 - lo) > 0.20:
            hit_win += 1
    print("    标的日 %d；当日 K 线即合成 %d（%.1f%%）；252 日窗口 flat>20%% 的 %d（%.1f%%）"
          % (tot, hit_day, 100.0 * hit_day / tot, hit_win, 100.0 * hit_win / tot))

    # ---------- 5. 当日评估自洽 ----------
    print("=" * 70)
    print("[5] 当日（%s）评估自洽" % dec["as_of"])
    dom = collections.Counter(v["state"]["dominant"] for v in pi.values())
    print("    主状态分布 %s" % dict(sorted(dom.items())))
    cs = sorted(v["state"]["confidence"] for v in pi.values())
    print("    置信度 min %.3f 中位 %.3f max %.3f；低于 %.2f 的 %d 只（%.0f%%）"
          % (cs[0], statistics.median(cs), cs[-1], dec["min_confidence_to_act"],
             sum(1 for x in cs if x < dec["min_confidence_to_act"]),
             100.0 * sum(1 for x in cs if x < dec["min_confidence_to_act"]) / len(cs)))
    ident = collections.Counter()
    for v in pi.values():
        p = v["state"]["probs"]
        vals = sorted(p.values(), reverse=True)
        if abs(vals[0] - vals[1]) < 1e-12:
            ident["top2_tie"] += 1
        s = v["state"]["scores"]
        if s["C"] == 0 and s["D"] == 0:
            ident["C_D_both_zero"] += 1
        if s["B"] == 0:
            ident["B_zero"] += 1
    print("    评分退化：C 与 D 同时为 0 的 %d 只；B 为 0 的 %d 只"
          % (ident["C_D_both_zero"], ident["B_zero"]))

    # ---------- 6. 经验转移矩阵（模型当前不输出的一环）----------
    print("=" * 70)
    print("[6] 经验日转移矩阵（真实自持 vs 生产 persistence 口径）")
    S = "ABCDE"
    bd = collections.defaultdict(dict)
    for o in rows:
        bd[o["c"]][o["d"]] = o["s"]
    cnt, rowsum = collections.Counter(), collections.Counter()
    for _c, m in bd.items():
        ds = sorted(m)
        for i in range(len(ds) - 1):
            cnt[(m[ds[i]], m[ds[i + 1]])] += 1
            rowsum[m[ds[i]]] += 1
    trans = {}
    print("       " + "".join("%8s" % x for x in S) + "      自持   期望持续(日)")
    for x in S:
        cells = [cnt[(x, y)] / max(1, rowsum[x]) for y in S]
        pii = cnt[(x, x)] / max(1, rowsum[x])
        trans[x] = {"row": [round(v, 4) for v in cells], "self": round(pii, 4),
                    "duration": round(1 / (1 - pii), 2) if pii < 1 else None}
        print("   %s   " % x + "".join("%8.3f" % v for v in cells)
              + "   %8.3f   %8.1f" % (pii, 1 / (1 - pii) if pii < 1 else 999))
    persist = 0.55
    implied = persist + (1 - persist) / len(S)
    print("    生产 persistence=%.2f → 各态自持一律 %.3f（实际 %.2f–%.2f）"
          % (persist, implied, min(v["self"] for v in trans.values()),
             max(v["self"] for v in trans.values())))

    # ---------- 7. state_days 口径核对 ----------
    print("=" * 70)
    print("[7] state_days 口径核对")
    hist_path = os.path.join(BASE, "state", "state_dominant_history.jsonl")
    rec = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec.append(json.loads(line)["date"])
                except Exception:
                    pass
    if rec:
        bench = load_bars("000300") or []
        cal = [b["date"] for b in bench if b["date"] >= rec[0]]
        print("    历史文件 %d 行 / 区间交易日 %d 个；缺 %s"
              % (len(rec), len(cal), [d for d in cal if d not in rec] or "无"))
        true_days = []
        for c in codes:
            m = bd.get(c) or {}
            ds = sorted(m)
            if not ds or ds[-1] != dec["as_of"]:
                continue
            s0 = m[ds[-1]]
            n = 0
            for d in reversed(ds):
                if m[d] == s0:
                    n += 1
                else:
                    break
            true_days.append(n)
        out_days = [v["state"]["state_days"] for v in pi.values()]
        print("    输出 state_days: 中位 %d / max %d；面板回算真实: 中位 %d / max %d"
              % (statistics.median(out_days), max(out_days),
                 statistics.median(true_days), max(true_days)))

    if a.json:
        out = {"as_of": dec["as_of"], "coverage": ok, "flat_rows": flat_rows,
               "panel": {"rows": tot, "flat_day": hit_day, "flat_win": hit_win},
               "dominant": dict(dom), "degrade": dict(ident),
               "transition": trans,
               "persistence_implied_self": round(implied, 4),
               "state_days_output": {"median": statistics.median(
                   [v["state"]["state_days"] for v in pi.values()]),
                   "max": max(v["state"]["state_days"] for v in pi.values())}}
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print("\n-> %s" % a.json)
    return 0


def longest_run(d):
    idx = [i for i, b in enumerate(d) if b["high"] == b["low"] == b["close"]]
    if not idx:
        return 0
    best = cur = 1
    for k in range(1, len(idx)):
        cur = cur + 1 if idx[k] == idx[k - 1] + 1 else 1
        best = max(best, cur)
    return best


def recent_flat(d, win=252):
    return all(b["high"] == b["low"] == b["close"] for b in d[-win:])


if __name__ == "__main__":
    sys.exit(main())
