# -*- coding: utf-8 -*-
"""state_transition.py — 五态经验转移矩阵（生产只读查表）

来源：state/pool_panel.jsonl（生产同链回放，118 只 × 1,333 交易日，201,000+
标的日），逐标的按日期升序统计一阶转移。本模块只做三件事：

  1. 把「这个状态还能信几天」从标量 0.55 换成经验期望持续 `1/(1-p_self)`；
  2. 给每个标的多输出一个 P(次日状态)，供决策卡显示「最可能的次态」；
  3. 提供分态先验粘性候选值（默认**不启用**，见下）。

为什么默认不启用分态 persistence：
    生产 `state_probs` 的 prior = p·prev + (1-p)·u，p 是**所有状态共用的标量**。
    改成 p_A=0.945 / p_E=0.934 会让状态序列显著更黏，等于换了一台状态机——
    而 docs/CROSS_SECTION_20261109.md 的全部检验、docs/POOL_DYNAMIC_20261109.md
    的 R0–R6 对比都是在标量口径下做的。换参数必须重跑全部检验，不能顺手改。
    因此这里只提供 `persistence_for()` 与配置开关，默认返回标量。

用法：
    python state_transition.py --rebuild     # 从面板重算并写 state/state_transition.json
    python state_transition.py               # 打印当前矩阵
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Any, Dict, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(BASE, "state", "pool_panel.jsonl")
OUT = os.path.join(BASE, "state", "state_transition.json")
STATES = ("A", "B", "C", "D", "E")
STATE_CN = {"A": "吸筹", "B": "突破", "C": "通道", "D": "派发", "E": "下跌"}


def rebuild(panel: str = PANEL, out: str = OUT) -> Dict[str, Any]:
    """统计一阶转移（同一标的前后两个交易日），写盘并返回结果。"""
    seq: Dict[str, list] = collections.defaultdict(list)
    with open(panel, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            seq[r["c"]].append((r["d"], r["s"]))
    cnt: collections.Counter = collections.Counter()
    tot: collections.Counter = collections.Counter()
    for xs in seq.values():
        xs.sort()
        for i in range(1, len(xs)):
            if xs[i][0] <= xs[i - 1][0]:
                continue  # 同日重复/逆序行不计
            tot[xs[i - 1][1]] += 1
            cnt[(xs[i - 1][1], xs[i][1])] += 1
    rows: Dict[str, Any] = {}
    for s in STATES:
        n = tot[s]
        nxt = {t: (cnt[(s, t)] / n if n else 0.0) for t in STATES}
        rows[s] = {
            "n": n,
            "next": {t: round(v, 4) for t, v in nxt.items()},
            "self_hold": round(nxt[s], 4),
            "expected_duration_days": round(1.0 / (1.0 - nxt[s]), 1) if nxt[s] < 1 else None,
        }
    out_obj = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": os.path.basename(panel),
        "method": "逐标的一阶转移（生产同链回放面板，状态为当日 dominant）",
        "n_transitions": int(sum(tot.values())),
        "states": rows,
        "note": ("经验自持率显著高于生产标量 persistence=0.55 的等效值 0.640："
                 "A/E 实际约 0.94。分态 persistence 属换状态机，需重跑全部检验后才可启用。"),
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out_obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return out_obj


def load(path: str = OUT) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def annotate(state: Dict[str, Any], mat: Optional[Dict[str, Any]],
             cfg: Optional[Dict[str, Any]] = None) -> None:
    """给 state 字典补 next_state / expected_duration_days（原地）。

    矩阵缺失 → 不写字段（报告显示「—」），绝不用硬编码数字顶替。
    """
    if not mat:
        return
    d = (state or {}).get("dominant")
    row = ((mat.get("states") or {}).get(d) or {})
    if not row:
        return
    state["transition"] = {
        "self_hold": row.get("self_hold"),
        "expected_duration_days": row.get("expected_duration_days"),
        "next": row.get("next"),
        "source": mat.get("n_transitions"),
        "sample_n": row.get("n"),
    }
    nxt = row.get("next") or {}
    alt = sorted(((v, k) for k, v in nxt.items() if k != d), reverse=True)
    if alt:
        state["transition"]["most_likely_next"] = alt[0][1]
        state["transition"]["p_next"] = round(alt[0][0], 4)
    # 期望持续 vs 已持续：超过 1.5 倍期望仍在同态 → 标注「老状态」
    days = float(state.get("state_days") or 0)
    exp = row.get("expected_duration_days")
    if exp:
        state["transition"]["age_ratio"] = round(days / float(exp), 2)
        state["transition"]["aged"] = bool(days > 1.5 * float(exp))


def persistence_for(state: str, cfg: Optional[Dict[str, Any]]) -> float:
    """分态先验粘性（默认关闭 → 回落标量）。

    cfg["state"]["persistence_mode"] == "per_state" 时才查表；表缺失 → 标量。
    调用方必须自己承担「换状态机」的后果（见模块 docstring）。
    """
    c = (cfg or {}).get("state") or {}
    scalar = float(c.get("persistence", 0.55))
    if str(c.get("persistence_mode", "scalar")) != "per_state":
        return scalar
    return float((c.get("persistence_by_state") or {}).get(state, scalar))


def show(mat: Optional[Dict[str, Any]] = None) -> int:
    mat = mat or load()
    if not mat:
        print("缺矩阵，先跑 python state_transition.py --rebuild", file=sys.stderr)
        return 2
    print("五态经验转移矩阵（%s，%d 次转移）"
          % (mat.get("source"), mat.get("n_transitions", 0)))
    print("%-4s%9s%9s%9s%9s%9s%10s%14s"
          % ("态", "→A", "→B", "→C", "→D", "→E", "自持", "期望持续(日)"))
    for s in STATES:
        r = mat["states"][s]
        print("%-4s%9.3f%9.3f%9.3f%9.3f%9.3f%10.3f%14s"
              % (s, r["next"]["A"], r["next"]["B"], r["next"]["C"],
                 r["next"]["D"], r["next"]["E"], r["self_hold"],
                 r["expected_duration_days"]))
    print("\n" + str(mat.get("note") or ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    if a.rebuild:
        m = rebuild(a.panel, a.out)
        print("→ %s（%d 次转移）" % (a.out, m["n_transitions"]))
        return 0
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
