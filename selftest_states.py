# -*- coding: utf-8 -*-
"""
S++ 状态判别自测 —— 可证伪版

与原版的区别（这是审计缺陷 3 的修复）：
  原版:  engine.state_probs(f, None, 0.55, 0.85)   # 参数硬编码，改配置测不出来
  本版:  从 splus_config.yaml 加载全部参数，并额外执行 MUTATION TEST
         —— 故意破坏参数，自测必须 FAIL。测不出失败的测试等于没有测试。

运行:  python selftest_states.py
退出码: 0 = 通过；1 = 失败（破坏测试未失败 / 状态判别失败）
"""
import io
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splus_config.yaml")


# ============================================================
# 合成行情
# ============================================================

def mk_bars(cs, vs, highs=None, lows=None):
    bars = []
    d0 = 20240101
    for i, c in enumerate(cs):
        h = highs[i] if highs and i < len(highs) else c * 1.006
        l = lows[i] if lows and i < len(lows) else c * 0.994
        bars.append({"date": f"D{d0 + i}", "open": c, "high": max(h, c),
                     "low": min(l, c), "close": c, "vol": vs[i]})
    return bars


def gen_A(n=300, seed=11):
    """吸筹：深跌 -30% 后在低位窄幅横盘，波动收敛、缩量。"""
    rnd = random.Random(seed)
    cs, vs = [], []
    p = 1000.0
    for i in range(120):
        p *= (1 - 0.0030)
        cs.append(p); vs.append(2.4e6)
    base = p
    for i in range(n - 120):
        p = base * (1 + rnd.uniform(-0.010, 0.010))
        cs.append(p); vs.append(1.0e6 * (1 - 0.5 * i / max(1, n - 120)))
    return mk_bars(cs, vs)


def gen_B(n=300, seed=13):
    """突破：长期横盘后最后一日放量创新高。"""
    rnd = random.Random(seed)
    cs, vs = [], []
    p = 800.0
    for i in range(n - 1):
        p = 800.0 * (1 + 0.04 * math.sin(i / 9.0) + rnd.uniform(-0.004, 0.004))
        cs.append(p); vs.append(1.2e6)
    cs.append(max(cs) * 1.055); vs.append(6.0e6)
    return mk_bars(cs, vs)


def gen_C(n=300, seed=17):
    """通道：稳定上行，效率高，均线多头排列。"""
    rnd = random.Random(seed)
    cs, vs = [], []
    p = 500.0
    for i in range(n):
        p *= (1 + 0.0042)
        p *= (1 + rnd.uniform(-0.0012, 0.0012))
        cs.append(p); vs.append(1.5e6)
    return mk_bars(cs, vs)


def gen_D(n=300, seed=23):
    """派发：高位大幅震荡，反复上冲失败并收回区间内。"""
    rnd = random.Random(seed)
    cs, vs, highs, lows = [], [], [], []
    p = 1000.0
    for i in range(80):
        p *= (1 + 0.0055)
        cs.append(p); vs.append(1.8e6); highs.append(p * 1.01); lows.append(p * 0.99)
    mid = p
    for i in range(n - 80):
        phase = (i % 20) / 20.0
        if phase < 0.5:
            q = mid * (1 + 0.030 * phase * 2)
            h = q * 1.018
        else:
            q = mid * (1 + 0.030 * (1 - phase) * 2)
            h = q * 1.002
        p = q * (1 + rnd.uniform(-0.006, 0.006))
        cs.append(p); vs.append(2.2e6 if phase < 0.5 else 1.4e6)
        highs.append(h); lows.append(p * 0.984)
    return mk_bars(cs, vs, highs, lows)


def gen_E(n=300, seed=19):
    """下跌：单边下跌通道。"""
    rnd = random.Random(seed)
    p = 1000.0
    cs, vs = [], []
    for i in range(n):
        p *= (1 - 0.0032)
        p *= (1 + rnd.uniform(-0.002, 0.002))
        cs.append(p); vs.append(1.3e6)
    return mk_bars(cs, vs)


CASES = [("吸筹A", gen_A, "A"), ("突破B", gen_B, "B"), ("通道C", gen_C, "C"),
         ("派发D", gen_D, "D"), ("下跌E", gen_E, "E")]


# ============================================================
# 判定
# ============================================================

def classify(cfg, bars):
    f = engine.compute_features(bars)
    sp = engine.state_probs(
        f, None,
        float(cfg["state"]["persistence"]),
        float(cfg["state"]["temperature"]),
        cfg["state"]["scoring"],
        float(cfg["state"].get("uniform_prior", 0.2)))
    sp["state_weight"] = engine.state_weighted_position(sp["probs"], cfg["state"]["weights"])
    return sp


def run_cases(cfg, verbose=True, min_conf=0.30, min_gap=0.05):
    """
    返回 (全部通过?, 明细列表)

    断言三重化（修复审计缺陷：原版只看 dominant，参数被压平也能蒙对）：
      ① 方向正确      dom == expect
      ② 置信度足够    conf >= min_conf      —— 概率被压平时必然暴露
      ③ 与次位有差距  gap  >= min_gap       —— 两状态打平时必然暴露
    """
    ok_all = True
    rows = []
    for name, gen, expect in CASES:
        sp = classify(cfg, gen())
        probs, dom = sp["probs"], sp["dominant"]
        top2 = sorted(probs.items(), key=lambda x: -x[1])[:2]
        gap = top2[0][1] - top2[1][1]
        conf_ok = probs[dom] >= min_conf
        gap_ok = gap >= min_gap
        ok = (dom == expect) and conf_ok and gap_ok
        ok_all = ok_all and ok
        why = ""
        if dom != expect:
            why = f"方向错({dom}≠{expect})"
        elif not conf_ok:
            why = f"置信不足({probs[dom]:.2f}<{min_conf})"
        elif not gap_ok:
            why = f"与次位无差距({gap:.2f}<{min_gap})"
        rows.append({"name": name, "expect": expect, "got": dom, "ok": ok,
                     "conf": probs[dom], "second": top2[1][0], "gap": gap, "why": why})
        if verbose:
            print(f"  {name}: 期望 {expect} | 实际 {dom} "
                  f"({'PASS' if ok else 'FAIL ' + why}) | 置信 {probs[dom]:.2f} | "
                  f"次位 {top2[1][0]} {top2[1][1]:.2f} | 差距 {gap:.2f}")

    # 方向可分性：稳定上行 vs 稳定下行，主状态必须不同
    up = classify(cfg, gen_C(seed=31))
    dn = classify(cfg, gen_E(seed=37))
    sep = (up["dominant"] != dn["dominant"]
           and up["probs"][up["dominant"]] >= min_conf
           and dn["probs"][dn["dominant"]] >= min_conf)
    ok_all = ok_all and sep
    if verbose:
        print(f"  方向可分性：上行→{up['dominant']}({up['probs'][up['dominant']]:.2f})，"
              f"下行→{dn['dominant']}({dn['probs'][dn['dominant']]:.2f}) → "
              f"{'PASS' if sep else 'FAIL'}")
    return ok_all, rows


def mutate(cfg, path, value):
    """深拷贝配置并改一个参数。path 形如 ['state','temperature']"""
    import copy
    c = copy.deepcopy(cfg)
    node = c
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return c


# ============================================================
# MUTATION TEST —— 自测必须能够失败
# ============================================================

def mutation_tests(cfg):
    """
    故意破坏参数，要求自测 FAIL。
    若破坏后仍然全 PASS，说明自测没有真正校验参数路径 → 判定为"测试失效"。
    """
    print("\n" + "=" * 70)
    print("MUTATION TEST —— 故意破坏参数，自测必须失败")
    print("=" * 70)

    ratio_min = float(cfg["state"].get("mutation_min_fail_ratio", 0.6))
    trials = []

    # 1) 温度极大：似然完全平坦，模型退化为按先验分配
    trials.append(("temperature=200 (似然平坦)", mutate(cfg, ["state", "temperature"], 200.0)))


    # 4) 清空打分的 core：所有状态只剩 gate，无法区分
    flat = {k: {"gate": cfg["state"]["scoring"][k].get("gate", []),
                "gate_op": cfg["state"]["scoring"][k].get("gate_op", "mul"),
                "core": []} for k in ("A", "B", "C", "D", "E")}
    trials.append(("清空 core 打分项", mutate(cfg, ["state", "scoring"], flat)))

    # 5) 把所有 gate 去掉：必须项失效，强势指数会被误判为吸筹
    nogate = {k: {"core": cfg["state"]["scoring"][k].get("core", [])}
              for k in ("A", "B", "C", "D", "E")}
    trials.append(("移除全部 gate 必须项", mutate(cfg, ["state", "scoring"], nogate)))

    failed_count = 0
    for label, c in trials:
        ok, rows = run_cases(c, verbose=False)
        if ok:
            print(f"  [测试失效] {label} → 仍全 PASS，该场景未被检出")
        else:
            bad = [r["name"] for r in rows if not r["ok"]]
            print(f"  [已检出]  {label} → FAIL（错判: {','.join(bad) or '方向可分性'}）")
            failed_count += 1

    ratio = failed_count / len(trials) if trials else 0.0
    print(f"\n  参数校验检出率 {failed_count}/{len(trials)} = {ratio:.0%}（门槛 {ratio_min:.0%}）")

    # 权重突变单独校验：它不影响概率，只影响仓位映射，必须由仓位测试捕获
    w = dict(cfg["state"]["weights"])
    flipped = {"A": w["E"], "B": w["B"], "C": w["C"], "D": w["D"], "E": w["A"]}
    det = {}
    for k in "ABCDE":
        det[k] = engine.state_weighted_position({k: 1.0, **{o: 0.0 for o in "ABCDE" if o != k}}, w)
        fk = engine.state_weighted_position({k: 1.0, **{o: 0.0 for o in "ABCDE" if o != k}}, flipped)
        det[k] = (det[k], fk)
    w_ok = all(abs(a - b) < 1e-9 for k, (a, b) in det.items() if k not in ("A", "E"))
    w_changed = abs(det["A"][0] - det["A"][1]) > 1e-9 and abs(det["E"][0] - det["E"][1]) > 1e-9
    print(f"  [{'已检出' if (w_changed) else '测试失效'}]  weights 翻转 (A<->E) → "
          f"纯状态仓位 A {det['A'][0]:.2f}→{det['A'][1]:.2f}, "
          f"E {det['E'][0]:.2f}→{det['E'][1]:.2f}, 其余不变={w_ok}")
    w_pass = w_changed and w_ok

    # 数值稳定性检查：温度极小时 argmax 排序不变是正确特性，
    # 但绝不允许出现 nan/inf。原实现 exp(scores/temp) 会溢出为 inf → nan。
    stable_pass = True
    for tval in (0.001, 0.01, 1e-6):
        cc = mutate(cfg, ["state", "temperature"], tval)
        for name, gen, _ in CASES:
            sp = classify(cc, gen())
            ps = list(sp["probs"].values())
            bad = (any(not math.isfinite(x) for x in ps)
                   or abs(sum(ps) - 1.0) > 1e-6
                   or any(x < 0 for x in ps))
            if bad:
                stable_pass = False
                print(f"  [测试失效]  temp={tval} {name} → 概率非法 {ps}")
                break
        if not stable_pass:
            break
    if stable_pass:
        print(f"  [已检出]  数值稳定性 temp=1e-6/0.01/0.001 → 概率均有限且归一（log-sum-exp 生效）")

    total = ratio >= ratio_min and w_pass and stable_pass
    return total, ratio


# ============================================================

def main():
    print("=" * 70)
    print("S++ 状态判别自测（可证伪版）")
    print("=" * 70)
    print(f"配置来源: {CFG_PATH}")

    cfg = engine.load_config(CFG_PATH)
    print(f"参数读自配置: persistence={cfg['state']['persistence']}, "
          f"temperature={cfg['state']['temperature']}, "
          f"scoring 状态数={len(cfg['state']['scoring'])}")

    print("\n[1] 五状态判别")
    ok1, rows = run_cases(cfg)

    print("\n[2] 仓位映射")
    w = cfg["state"]["weights"]
    pure = {k: engine.state_weighted_position({k: 1.0, **{o: 0.0 for o in "ABCDE" if o != k}}, w)
            for k in "ABCDE"}
    order_ok = pure["C"] >= pure["B"] > pure["D"] > pure["A"] > pure["E"]
    print(f"  纯状态仓位系数: " + " ".join(f"{k}={v:.2f}" for k, v in pure.items()))
    print(f"  单调性 C>=B>D>A>E → {'PASS' if order_ok else 'FAIL'}")

    print("\n[3] 可证伪性检查（MUTATION）")
    ok3, ratio = mutation_tests(cfg)

    print("\n" + "=" * 70)
    total_ok = ok1 and order_ok and ok3
    if total_ok:
        print("总判定： 通过（状态判别 + 仓位映射 + 可证伪性 全部满足）")
    else:
        fails = []
        if not ok1:
            fails.append("状态判别")
        if not order_ok:
            fails.append("仓位映射")
        if not ok3:
            fails.append(f"可证伪性(检出率 {ratio:.0%})")
        print("总判定： 失败 → " + "; ".join(fails))
    print("=" * 70)
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
