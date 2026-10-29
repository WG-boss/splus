# -*- coding: utf-8 -*-
"""为 S++ 新增主题指数抓取并落盘 K 线缓存。

落盘格式与 stock-pool/_cache/idx_<code>.json 完全一致：
  [{date, open, close, high, low, vol, amount}, ...]
源：中证官网（csindex）优先；官网无该码时回落到东财/腾讯。
每只独立重试，避免一处限流误判为"无数据"。
"""
import os, sys, json, time
sys.path.insert(0, r"..\stock-pool")
sys.stdout.reconfigure(encoding="utf-8")

from data_layer import index_global as ig

CACHE = os.path.join("..", "stock-pool", "_cache")

TARGETS = [
    ("931152", "创新药"),
    ("932666", "高频PCB"),
    ("931594", "商业航天"),
    ("931743", "半导体材料设备"),
    ("931723", "光模块CPO"),
    ("932580", "磁电存储"),
]

DAYS = 520
report = []
for code, name in TARGETS:
    bars = []
    src = ""
    for attempt in range(4):
        ig.reset_csi_breaker()
        bars = ig._csindex_bars(code, DAYS)
        if bars:
            src = "csindex"
            break
        time.sleep(2.5)
    if not bars:
        try:
            bars = ig.bars(code, days=DAYS, use_cache=False, expect_name="")
            src = "fallback"
        except Exception as e:
            print("%-8s %-14s FAIL %s" % (code, name, e))
            report.append((code, name, 0, "", ""))
            continue

    if not bars:
        print("%-8s %-14s FAIL(no bars)" % (code, name))
        report.append((code, name, 0, "", ""))
        continue

    path = os.path.join(CACHE, "idx_%s.json" % code)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bars, fh, ensure_ascii=False)
    print("%-8s %-14s n=%-4d %s ~ %s  src=%s" % (
        code, name, len(bars), bars[0]["date"], bars[-1]["date"], src))
    report.append((code, name, len(bars), bars[0]["date"], bars[-1]["date"]))
    time.sleep(1.2)

print("\n--- summary ---")
for r in report:
    print(r)
