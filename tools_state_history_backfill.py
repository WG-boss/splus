# -*- coding: utf-8 -*-
"""tools_state_history_backfill.py — 主状态连续段的「落库」修复

问题（docs/STATE_DAILY_AUDIT_20261108.md）：
    `state_days` 由 `state/state_dominant_history.jsonl` 回算，而该文件是
    **逐日运行时追加**的，只有 9 行。于是「半导体已 E 态 85 天」在报告里显示
    成 8 天——不是算法错，是历史没落库。

修复：
    把生产同链回放面板（state/pool_panel.jsonl，118 只 × 1,333 日，字段
    `d`/`c`/`s`）转写成逐日主状态历史，与现有 9 行合并（面板覆盖的日期以
    面板为准），写回 `state/state_dominant_history.jsonl`。

口径披露：
    * 面板状态由 `engine.compute_features → engine.state_probs` 以**生产参数**
      回放得到（persistence=0.55 标量口径），与今日前向计算同源；
    * 但它是**回放值**，不是当年的实时记录——若历史某日真实运行时参数不同，
      这一段会与当时的报告不一致；
    * 只影响"已持续多少天"这类展示量，不参与状态概率与仓位计算；
    * 覆盖写之前会自动备份到同目录 `.bak.<时间戳>`。

用法：
    python tools_state_history_backfill.py            # 干跑，只报告
    python tools_state_history_backfill.py --apply    # 实际写入（自动备份）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import statistics
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PANEL = os.path.join(BASE, "state", "pool_panel.jsonl")
HIST = os.path.join(BASE, "state", "state_dominant_history.jsonl")


def _read_hist(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("date") and isinstance(r.get("d"), dict):
                out[str(r["date"])] = r["d"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--hist", default=HIST)
    ap.add_argument("--max-days", type=int, default=750,
                    help="只保留最近 N 个交易日（默认 750 ≈ 3 年；最长实测连续段 156 日）")
    a = ap.parse_args()

    if not os.path.exists(a.panel):
        print("缺面板 %s：先跑 python tools_pool_dynamic.py --build" % a.panel,
              file=sys.stderr)
        return 2

    by_date: dict = defaultdict(dict)
    with open(a.panel, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            by_date[str(r["d"])][str(r["c"])] = str(r["s"])
    panel_dates = sorted(by_date)
    old = _read_hist(a.hist)
    merged = dict(old)
    for d in panel_dates:
        merged[d] = by_date[d]          # 面板覆盖的日期以面板为准
    later = sorted(d for d in old if d > panel_dates[-1])
    dates = sorted(merged)
    if a.max_days and len(dates) > a.max_days:
        cut = dates[-a.max_days:]
        print("  仅保留最近 %d 个交易日（%s 起）——逐日回算只需覆盖最长连续段"
              % (a.max_days, cut[0]))
        dates = cut

    print("主状态历史回填")
    print("  面板：%s → %s，%d 个交易日 × 最多 %d 只"
          % (panel_dates[0], panel_dates[-1], len(panel_dates),
             max(len(v) for v in by_date.values())))
    print("  现有历史：%d 行（%s%s）" % (len(old), (min(old) + " → ") if old else "空",
                                        max(old) if old else ""))
    print("  合并后：%d 行；面板之后保留的实时行 %d 行" % (len(dates), len(later)))

    # 连续段抽样：回填前 vs 回填后
    import engine
    hist_before = [(d, old[d]) for d in sorted(old)]
    hist_after = [(d, merged[d]) for d in dates]
    thr = 68
    sample = []
    for code in list(by_date[panel_dates[-1]])[:200]:
        target = by_date[panel_dates[-1]][code]
        _s0, n0 = engine.state_streak(hist_before, code, target)
        _s1, n1 = engine.state_streak(hist_after, code, target)
        sample.append((code, n0, n1))
    changed = [s for s in sample if s[2] != s[1]]
    print("  连续段抽样（末态相同者 %d 只）：回填前中位 %s 天 → 回填后中位 %s 天；%d 只被修正"
          % (len(sample),
             statistics.median([s[1] for s in sample]) if sample else 0,
             statistics.median([s[2] for s in sample]) if sample else 0,
             len(changed)))
    print("  例：%s" % "；".join("%s %d→%d" % s for s in changed[:6]))
    _ = thr

    if not a.apply:
        print("\n干跑结束；加 --apply 写入。")
        return 0

    if os.path.exists(a.hist) and old:
        bak = a.hist + ".bak." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(a.hist, bak)
        print("  备份 → %s" % bak)
    tmp = a.hist + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for d in dates:
            fh.write(json.dumps({"date": d, "d": merged[d]}, ensure_ascii=False) + "\n")
    os.replace(tmp, a.hist)
    print("  写入 → %s（%d 行）" % (a.hist, len(dates)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
