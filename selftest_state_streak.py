# -*- coding: utf-8 -*-
"""
自测：S++ L1「主状态起始日 / 持续交易日」回算逻辑

覆盖：
  1) append_dominant_history 落盘 + 同日重跑幂等替换 + 隔日追加
  2) load_dominant_history 升序 + 坏行跳过
  3) state_streak 连续段回算：正常段 / 状态切换 / 首日 / code 缺行截断
与 stock-pool 指数池同口径：按连续相同主状态段回算，最新日期为段尾。
"""

from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as engine

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def main():
    tmp = tempfile.mkdtemp(prefix="streak_")
    path = os.path.join(tmp, "state_dominant_history.jsonl")

    # 固定历史（无替换）：000688 在 11-01~11-03 为 E，11-04 翻为 C
    #                    399673 11-01 为 E，11-02~11-04 为 C
    engine.append_dominant_history(path, "2026-11-01", {"000688": "E", "399673": "E"})
    engine.append_dominant_history(path, "2026-11-02", {"000688": "E", "399673": "C"})
    engine.append_dominant_history(path, "2026-11-03", {"000688": "E", "399673": "C"})
    engine.append_dominant_history(path, "2026-11-04", {"000688": "C", "399673": "C"})

    hist = engine.load_dominant_history(path)
    check("历史升序 4 行", len(hist) == 4)
    check("首行日期 11-01", hist[0][0] == "2026-11-01")
    check("末行日期 11-04", hist[-1][0] == "2026-11-04")

    # 000688 末态 C（11-04 翻 C），末段仅 11-04 一天
    sd, days = engine.state_streak(hist, "000688", "C")
    check("000688 切到 C 持续=1", days == 1)
    check("000688 切到 C 起始=11-04", sd == "2026-11-04")

    # 000688 若以 E 为目标（非末态），从末行立即截断 -> 0
    sd, days = engine.state_streak(hist, "000688", "E")
    check("000688 非末态目标截断=0", days == 0 and sd is None)

    # 399673 末态 C，末段 11-02~11-04 连续三天
    sd, days = engine.state_streak(hist, "399673", "C")
    check("399673 持续交易日=3", days == 3)
    check("399673 起始日=11-02", sd == "2026-11-02")

    # ---- 同日重跑幂等替换 ----
    engine.append_dominant_history(path, "2026-11-04", {"000688": "E", "399673": "E"})
    hist2 = engine.load_dominant_history(path)
    check("同日重跑不重复追加（仍 4 行）", len(hist2) == 4)
    check("同日重跑末行被替换", hist2[-1][1]["000688"] == "E")
    # 替换后 000688 末态 E，且 11-01~11-04 全 E -> 连续 4 天
    sd, days = engine.state_streak(hist2, "000688", "E")
    check("替换后 000688 持续=4", days == 4)
    check("替换后 000688 起始=11-01", sd == "2026-11-01")
    # 399673 替换后末态 E -> 仅 11-04 一天
    sd, days = engine.state_streak(hist2, "399673", "E")
    check("替换后 399673 持续=1", days == 1)
    check("替换后 399673 起始=11-04", sd == "2026-11-04")

    # ---- 坏行跳过 ----
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{bad json\n")
        fh.write('{"date": "2026-11-05"}\n')  # 缺 "d" 键
    hist3 = engine.load_dominant_history(path)
    check("坏行被跳过（仍 4 行）", len(hist3) == 4)

    # ---- 空历史 ----
    empty = os.path.join(tmp, "empty.jsonl")
    sd_e, days_e = engine.state_streak(engine.load_dominant_history(empty), "000688", "E")
    check("空历史返回 (None, 0)", sd_e is None and days_e == 0)

    # ---- 缺 code 行截断（中段缺失视为断点，连续段不跨缺口）----
    p2 = os.path.join(tmp, "p2.jsonl")
    engine.append_dominant_history(p2, "2026-11-10", {"000688": "A"})
    engine.append_dominant_history(p2, "2026-11-11", {"399673": "A"})  # 000688 缺（缺口）
    engine.append_dominant_history(p2, "2026-11-12", {"000688": "A"})
    h4 = engine.load_dominant_history(p2)
    sd4, days4 = engine.state_streak(h4, "000688", "A")
    # 末行 11-12 有 000688=A（计 1，起始 11-12），11-11 缺 -> 断点，不跨缺口
    check("缺 code 行截断（持续=1）", days4 == 1 and sd4 == "2026-11-12")

    # 末行即缺 code：立即截断 -> 0
    p3 = os.path.join(tmp, "p3.jsonl")
    engine.append_dominant_history(p3, "2026-11-20", {"000688": "A"})
    engine.append_dominant_history(p3, "2026-11-21", {"399673": "A"})  # 末行缺 000688
    h5 = engine.load_dominant_history(p3)
    sd5, days5 = engine.state_streak(h5, "000688", "A")
    check("末行缺 code 立即截断=0", days5 == 0 and sd5 is None)

    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
