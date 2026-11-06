# -*- coding: utf-8 -*-
"""tools_backfill_dominant_history.py — 从历史决策 JSON 回填逐日主状态 JSONL。

背景：state/state_dominant_history.jsonl（L1「主状态起始日/持续交易日」连续段
回算数据源，engine.load_dominant_history / state_streak）自 2026-11-05 功能上线
才开始落盘。跨机迁移后该文件仅剩 1 天，连续段窗口退化。

本工具从已入库的 data/splus_decision_{YYYYMMDD}.json（per_index[].state.dominant）
回填缺失日期，重建为按日期升序、按日去重的 JSONL。

语义与安全：
- 只补缺：已存在的日期行（引擎当日写入）一律保留，不被决策 JSON 覆盖；
- 幂等：重复运行结果一致；
- 只动 state/state_dominant_history.jsonl 一个文件，写前留 .bak 备份。

用法：python tools_backfill_dominant_history.py [--dry-run]
"""
import glob
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, "state", "state_dominant_history.jsonl")
GLOB = os.path.join(ROOT, "data", "splus_decision_*.json")


def load_existing():
    """读现有 JSONL -> {date: {code: dominant}}；坏行跳过。"""
    out = {}
    if not os.path.exists(HIST):
        return out
    with open(HIST, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                out[str(row["date"])] = {str(k): str(v) for k, v in row["d"].items()}
            except Exception:
                continue
    return out


def main():
    dry = "--dry-run" in sys.argv
    existing = load_existing()
    filled, skipped_dates = [], []
    for path in sorted(glob.glob(GLOB)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                dec = json.load(f)
            as_of = str(dec["as_of"])
            doms = {}
            for code, v in (dec.get("per_index") or {}).items():
                dom = ((v or {}).get("state") or {}).get("dominant")
                if dom:
                    doms[str(code)] = str(dom)
            if not doms:
                skipped_dates.append((as_of, "empty per_index"))
                continue
            if as_of in existing:
                skipped_dates.append((as_of, "already present (kept engine row)"))
                continue
            existing[as_of] = doms
            filled.append((as_of, len(doms)))
        except Exception as e:  # 单文件坏不影响其余
            skipped_dates.append((os.path.basename(path), "error: %s" % e))

    dates = sorted(existing)
    print("回填 %d 天: %s" % (len(filled), ["%s(%d只)" % t for t in filled]))
    print("跳过 %d 项: %s" % (len(skipped_dates), skipped_dates))
    print("最终历史天数: %d (%s ~ %s)" % (len(dates), dates[0] if dates else "-", dates[-1] if dates else "-"))
    if dry:
        print("[dry-run] 未写盘")
        return 0

    if os.path.exists(HIST):
        shutil.copy2(HIST, HIST + ".bak")
    tmp = HIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for d in dates:
            f.write(json.dumps({"date": d, "d": existing[d]}, ensure_ascii=False) + "\n")
    os.replace(tmp, HIST)

    # 验证：引擎读取 + 连续段抽验
    sys.path.insert(0, ROOT)
    import engine
    hist = engine.load_dominant_history(HIST)
    print("引擎读取: %d 天, 升序=%s" % (len(hist),
          all(hist[i][0] < hist[i + 1][0] for i in range(len(hist) - 1))))
    sample = hist[-1][1]
    for code in sorted(sample)[:3]:
        sd, days = engine.state_streak(hist, code, sample[code])
        print("抽验 %s: 主状态=%s 起始=%s 持续=%d 交易日" % (code, sample[code], sd, days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
