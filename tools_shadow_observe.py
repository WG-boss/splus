"""影子观察期运维 CLI（2026-11-09）

  python tools_shadow_observe.py                 # 进度 + 最近一日对照
  python tools_shadow_observe.py --report       # 两周汇总 + go/no-go
  python tools_shadow_observe.py --json         # 机器可读汇总
  python tools_shadow_observe.py --verify       # 端到端核对 shadow 零干预
  python tools_shadow_observe.py --rebuild      # 从历史决策 JSON 幂等补记台账

纪律：盘中只预览。`--rebuild` 会把 intraday_hold 的日子标为无效观察日，
不会稀释两周样本；收盘后（≥15:00）刷新行情再跑 run.py 才是有效观察日。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import engine  # noqa: E402
import shadow_obs as SO  # noqa: E402

CONFIG = os.path.join(BASE, "splus_config.yaml")


def latest_decision() -> str:
    fs = sorted(glob.glob(os.path.join(BASE, "data", "splus_decision_*.json")))
    if not fs:
        raise SystemExit("未找到 data/splus_decision_*.json")
    return fs[-1]


def load_result(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description="影子观察期运维")
    ap.add_argument("--report", action="store_true", help="输出两周汇总 Markdown")
    ap.add_argument("--json", action="store_true", help="输出机器可读汇总")
    ap.add_argument("--verify", action="store_true", help="核对 shadow 零干预")
    ap.add_argument("--rebuild", action="store_true", help="从历史决策 JSON 幂等补记")
    ap.add_argument("--today", default=None, help="覆盖今天（测试用 YYYY-MM-DD）")
    a = ap.parse_args()

    cfg = engine.load_config(CONFIG)
    c = SO.cfg_of(cfg)
    mode = str((cfg.get("pool") or {}).get("mode"))

    if a.rebuild:
        n = 0
        for p in sorted(glob.glob(os.path.join(BASE, "data", "splus_decision_*.json"))):
            res = load_result(p)
            if str((res.get("pool") or {}).get("mode")) != "shadow":
                continue
            # 历史补记按**交易日**归集：这些决策 JSON 往往是同一天批量重建的，
            # generated_at 会全部塌缩成今天，必须显式按 as_of 定运行日。
            res["generated_at"] = str(res.get("as_of")) + "T15:10:00"
            if SO.record(BASE, cfg, res):
                n += 1
        print("补记 %d 份决策（同运行日幂等替换，历史按 as_of 归集）→ %s"
              % (n, c["ledger"]))
        return 0

    s = SO.summarize(BASE, cfg, a.today)
    if a.verify:
        res = load_result(latest_decision())
        v = SO.verify_zero_intervention(res)
        print(json.dumps(v, ensure_ascii=False, indent=2))
        print("\n决策 JSON：%s" % os.path.basename(latest_decision()))
        return 0 if v["ok"] else 1

    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if a.report:
        print(SO.render(s))
        return 0

    w = s["window"]
    print("影子观察期：%s → %s（复核 %s，距复核 %s 天）"
          % (w["start"], w["end"], w["review_on"], w["days_to_review"]))
    print("当前模式：pool.mode=%s ｜ 判据门槛：%d 个有效交易日"
          % (mode, s["target_sessions"]))
    print("进度：有效 %d / %d ｜ 台账 %d 行 ｜ 无效 %d 行 ｜ 结论 %s"
          % (s["sessions_valid"], s["target_sessions"], s["sessions_total"],
             s["invalid_days"], s["decision"]))
    if s["sessions_valid"]:
        print("对照：影子池均值 %.1f 只 / 现行 %.1f 只 / 重合 %.1f 只 / Jaccard %.3f"
              % (s["avg_shadow_n"], s["avg_baseline_n"],
                 s["avg_overlap"], s["avg_jaccard"]))
        print("运营：日均换手 %.4f ｜ measured %.3f ｜ mapped %.3f ｜ 冲突 %d 天"
              % (s["avg_turnover"], s["measured_ratio_avg"],
                 s["mapped_ratio_avg"], s["conflict_sessions"]))
    if s["invalid_detail"]:
        print("无效日：" + "、".join("%s(%s)" % (d.get("session_date") or d["as_of"],
                                            d["reason"])
                                for d in s["invalid_detail"]))
    print("提示：--report 出完整判据表；--verify 核对零干预；"
          "收盘后刷新行情再跑 run.py 才计入有效观察日")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
