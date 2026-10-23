# -*- coding: utf-8 -*-
"""
自测二：真实数据上的历史状态回溯 + 参数敏感性

目的：
  1) 用真实指数历史，检查状态切换是否合理（不是天天变）
  2) 揭示状态持续性（persistence）与温度（temperature）的敏感度
  3) 输出每个指数的"状态切换次数"，过频说明参数需调
"""

from __future__ import annotations
import os
import sys
import io
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "..", "stock-pool", "_cache")
CFG_PATH = os.path.join(BASE, "splus_config.yaml")

# 参数与打分模型一律从配置加载（原版硬编码 0.55 / 0.85，改配置测不出来）
_CFG = engine.load_config(CFG_PATH)
MODEL = _CFG["state"]["scoring"]
P = float(_CFG["state"]["persistence"])
T = float(_CFG["state"]["temperature"])
UNIFORM = float(_CFG["state"].get("uniform_prior", 0.2))


def walk_state(bars, persistence, temperature, model, step=5):
    """逐窗口滚动推算状态序列（每 step 日抽样一次，控制计算量）。"""
    if len(bars) < 150:
        return []
    seq = []
    prev = None
    start = 130
    for i in range(start, len(bars), step):
        sub = bars[:i + 1]
        f = engine.compute_features(sub)
        sp = engine.state_probs(f, prev, persistence, temperature, model, UNIFORM)
        seq.append({
            "date": bars[i]["date"],
            "dom": sp["dominant"],
            "conf": round(sp["confidence"], 3),
            "probs": {k: round(v, 3) for k, v in sp["probs"].items()},
        })
        prev = sp["probs"]
    return seq


def switches(seq):
    if not seq:
        return 0
    n = 0
    for a, b in zip(seq, seq[1:]):
        if a["dom"] != b["dom"]:
            n += 1
    return n


def main():
    codes = [
        ("000688", "科创50"),
        ("399673", "创业板50"),
        ("hkHSTECH", "恒生科技"),
        ("000300", "沪深300"),
        ("usNDX", "纳斯达克100"),
    ]

    print("=" * 76)
    print("真实数据历史状态回溯（每 5 日抽样，先验逐次递推）")
    print("=" * 76)

    results = {}
    for code, name in codes:
        bars = engine.load_bars(CACHE, code)
        if len(bars) < 200:
            print(f"  {name}: 样本不足（{len(bars)}）")
            continue
        seq = walk_state(bars, P, T, MODEL)
        sw = switches(seq)
        dist = {}
        for s in seq:
            dist[s["dom"]] = dist.get(s["dom"], 0) + 1
        total = len(seq) or 1
        dist_pct = {k: round(v / total * 100, 1) for k, v in sorted(dist.items())}

        results[name] = {"n_obs": len(seq), "switches": sw, "dist": dist_pct}

        print(f"\n  {name}（{len(bars)} 根，{bars[0]['date']} ~ {bars[-1]['date']}）")
        print(f"    观测点 {len(seq)} 个，状态切换 {sw} 次 "
              f"（平均每 {len(seq)/max(sw,1):.1f} 个观测切换一次）")
        print(f"    状态分布：{dist_pct}")
        # 最近 6 个观测点
        print("    最近轨迹：" + " → ".join(f"{s['dom']}({s['conf']:.2f})"
                                            for s in seq[-6:]))

    # ---- 参数敏感性 ----
    print()
    print("=" * 76)
    print("参数敏感性：持久性 × 温度（以科创50 为例，关注切换频次）")
    print("=" * 76)
    bars = engine.load_bars(CACHE, "000688")
    if len(bars) >= 200:
        print(f"{'persistence':>12} {'temperature':>12} {'切换次数':>10} {'最终置信':>10}")
        for p_ in [0.30, 0.55, 0.75, 0.90]:
            for t_ in [0.50, 0.85, 1.20]:
                seq = walk_state(bars, p_, t_, MODEL)
                sw = switches(seq)
                last = seq[-1]["conf"] if seq else 0
                print(f"{p_:>12.2f} {t_:>12.2f} {sw:>10} {last:>10.3f}")

    # ---- 结论 ----
    print()
    print("=" * 76)
    print("解读")
    print("=" * 76)
    for name, r in results.items():
        avg_run = r["n_obs"] / max(r["switches"], 1)
        verdict = "合理" if avg_run >= 4 else "切换偏频繁"
        print(f"  {name:<10} 平均连续 {avg_run:>5.1f} 个观测/状态（约 "
              f"{avg_run*5:.0f} 交易日）→ {verdict}")


if __name__ == "__main__":
    main()
