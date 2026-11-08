#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_contract.py —— 量纲契约的可执行断言（不是文档，是测试）

六组断言：
  T1 两套口径归一后必须完全相同（换算表正确性）
  T2 infer_units 判别无歧义（两套口径相差 9 个数量级）
  T3 真实缓存的隐含每股均价合理（发现"量纲对了但口径错了"的另一种错）
  T4 量能特征对整体缩放不变（vol×100 后 vol_z20/vol_contract/down_vol_ratio 完全一致）
     —— 这条是"跨标的量纲不一致为何不影响信号"的实证依据
  T5 序列内部断点会破坏特征（注入 ×100 断点后 vol_z20 显著变化，且探测器能定位）
     —— 这条是"为何必须修断点"的实证依据
  T6 清单与实测一致（口径漂移即失败）

用法：python selftest_contract.py [--verbose]
退出码：0 全通过；1 有失败
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import data_contract as dc  # noqa: E402

FAILS: List[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def cache_dir() -> str:
    import yaml
    with open(os.path.join(HERE, "splus_config.yaml"), encoding="utf-8") as fh:
        return os.path.join(HERE, yaml.safe_load(fh)["data"]["cache_dir"])


def load(code: str) -> List[Dict[str, Any]]:
    import engine
    return engine.load_bars(cache_dir(), code)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.parse_args()

    print("[T1] 两套口径归一后一致")
    a = dc.canonical_bar({"close": 2400, "vol": 1.8168e8, "amount": 1.9514e11})   # 手+元
    b = dc.canonical_bar({"close": 2400, "vol": 3.4565e9, "amount": 1951.35})      # 股+亿元
    check("手+元 保持原值", abs(a["vol"] - 1.8168e8) < 1 and abs(a["amount"] - 1.9514e11) < 1,
          f"rule={a['rule']}")
    check("股+亿元 换算为 手+元",
          abs(b["vol"] - 3.4565e7) < 1 and abs(b["amount"] - 1.95135e11) < 1,
          f"rule={b['rule']}")
    check("声明源与数据不符时退回自动判定",
          dc.canonical_bar({"vol": 1.8168e8, "amount": 1.9514e11}, source="csindex")["rule"]
          == "source-mismatch-fallback")

    print("[T2] 口径判别无歧义")
    check("股+亿元 → shares_yi", dc.infer_units(3.4565e9, 1951.35) == "shares_yi")
    check("手+元 → lots_cny", dc.infer_units(1.8168e8, 1.9514e11) == "lots_cny")
    check("无成交额 → no_amount", dc.infer_units(1e8, 0) == "no_amount")
    check("比值区间判定", dc.ratio_plausible(1.8168e8, 1.9514e11) is True
          and dc.ratio_plausible(3.4565e9, 1951.35) is False)

    print("[T3] 真实缓存口径体检")
    for code, want in (("000300", "lots_cny"), ("000688", "lots_cny")):
        bars = load(code)
        if not bars:
            check(f"{code} 缓存存在", False, "缺文件")
            continue
        info = dc.series_units(bars)
        check(f"{code} 主导口径={want}", info["dominant"] == want,
              f"实测={info['dominant']} ratio={info['ratio_median']}")
        if info.get("implied_price_per_share") is not None:
            check(f"{code} 隐含每股均价合理", bool(info.get("price_plausible")),
                  f"{info['implied_price_per_share']} 元/股")
    for code in ("hkHSTECH", "usNDX"):
        bars = load(code)
        if bars:
            info = dc.series_units(bars)
            check(f"{code} 无成交额（境外源限制）", info["dominant"] == "no_amount",
                  f"实测={info['dominant']}")

    print("[T4] 量能特征对整体缩放不变（跨标的量纲不一致为何不影响信号）")
    import engine
    for code in ("000300", "399006", "000688"):
        bars = load(code)
        if len(bars) < 260:
            continue
        f1 = engine.compute_features(bars[-300:])
        scaled = [dict(b, vol=b["vol"] * 100.0) for b in bars[-300:]]
        f2 = engine.compute_features(scaled)
        for key in ("vol_z20", "vol_contract", "down_vol_ratio"):
            v1, v2 = f1.get(key), f2.get(key)
            if v1 is None or v2 is None:
                check(f"{code} {key} 可计算", False, f"{v1} vs {v2}")
                continue
            rel = abs(v1 - v2) / max(1e-12, abs(v1))
            check(f"{code} {key} 缩放不变", rel < 1e-9, f"{v1:.6f} vs {v2:.6f}")
        check(f"{code} vol_ma20 随缩放线性变化（故不可跨标的比较）",
              abs(f2["vol_ma20"] / max(1e-12, f1["vol_ma20"]) - 100.0) < 1e-6)

    print("[T5] 序列内部断点会破坏特征（且能被探测）")
    bars = load("000300")
    if len(bars) >= 500:
        broken = [dict(b) for b in bars]
        # 短断点（30 日）：落在 60 日 z-score 窗口【内部】→ 特征被破坏
        cut = len(broken) - 30
        for i in range(cut, len(broken)):
            broken[i]["vol"] = broken[i]["vol"] * 100.0
        fb = engine.compute_features(broken[-300:])
        fg = engine.compute_features(bars[-300:])
        z_broken, z_good = fb.get("vol_z20"), fg.get("vol_z20")
        check("30 日断点使 vol_z20 失真",
              z_broken is not None and z_good is not None and abs(z_broken - z_good) > 0.2,
              f"正常={z_good:.2f} 断点={z_broken:.2f}")

        # 长断点（150 日）：足以改变年度中位数 → 探测器应能定位
        # 同时也暴露探测器的分辨率极限：年度中位数法看不见"持续不到半年的断点"
        longb = [dict(b) for b in bars]
        for i in range(len(longb) - 150, len(longb)):
            longb[i]["vol"] = longb[i]["vol"] * 100.0
        import tools_fix_units as fu
        fixes = fu.scan_series(longb)
        check("探测器定位到长断点年份", len(fixes) >= 1,
              f"fixes={[(f['year'], round(f['factor'], 4)) for f in fixes]}")
        short_fixes = fu.scan_series(broken)
        print(f"        [已知极限] 30 日短断点探测结果 = {len(short_fixes)} 处"
              f"（年度中位数法看不见 <半年 的断点，故必须用成交额锚点做日级判定）")

    print("[T6] 清单与实测一致")
    man_path = dc.MANIFEST
    if not os.path.exists(man_path):
        check("清单已生成", False, "先运行 python data_contract.py")
    else:
        old = json.load(open(man_path, encoding="utf-8"))
        items = old.get("items", {})
        drift = 0
        for code, v in list(items.items())[:40]:
            bars = load(code)
            if not bars:
                continue
            now = dc.series_units(bars)
            if now["dominant"] != v.get("dominant"):
                drift += 1
                print(f"        drift {code}: {v.get('dominant')} → {now['dominant']}")
        check("前 40 只无口径漂移", drift == 0, f"drift={drift}")

    print(f"\n[result] PASS={PASSES} FAIL={len(FAILS)}")
    if FAILS:
        print("[result] 失败项：" + ", ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
