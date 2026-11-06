# -*- coding: utf-8 -*-
"""lifecycle.py — B2 生命周期规则（P3 · 2026-11-06）

两条连续性规则 + 冷却 + 事件台账（B2 改良版：不用五态状态机，
事件写入独立 JSONL 台账，报告展示；晋升/降级均为"建议"，人工执行——
自动化晋升可人工否决，自动化降级有 FROZEN 仓位归零兜底，不依赖人工）。

数据源：state/eligibility_history.jsonl（P2 每日落盘，同 as_of 幂等）。
状态文件：state/eligibility_lifecycle.json（冷却期记录）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "promote": {"min_tier_score": 80, "consecutive_days": 3},
    "demote": {"max_score": 60, "consecutive_days": 5},
    "cooldown_days": 10,           # 降级后 10 交易日禁止再晋升（防 churn）
    "state_path": "state/eligibility_lifecycle.json",
    "events_path": "state/lifecycle_events.jsonl",
}


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _asof_index(rows: List[Dict[str, Any]]) -> List[str]:
    return [r.get("as_of", "") for r in rows]


def _consecutive_full(rows: List[Dict[str, Any]], code: str,
                      min_score: float) -> int:
    """从最新日期倒序数连续 score≥min_score 的天数（缺该标的日即断）。"""
    n = 0
    for r in reversed(rows):
        s = ((r.get("scores") or {}).get(code) or {})
        if float(s.get("score", 0.0)) >= min_score:
            n += 1
        else:
            break
    return n


def _consecutive_below(rows: List[Dict[str, Any]], code: str,
                       max_score: float) -> int:
    """从最新日期倒序数连续 score<max_score 的天数。"""
    n = 0
    for r in reversed(rows):
        s = ((r.get("scores") or {}).get(code) or {})
        if float(s.get("score", 100.0)) < max_score:
            n += 1
        else:
            break
    return n


def trading_days_between(d1: str, d2: str, calendar: Optional[List[str]] = None) -> int:
    """两 as_of 之间的交易日数；无日历时用自然日近似（保守偏多→冷却更长）。"""
    if not d1 or not d2:
        return 0
    if calendar:
        try:
            i1 = calendar.index(d1)
            i2 = calendar.index(d2)
            return max(0, i2 - i1)
        except ValueError:
            pass
    from datetime import date
    try:
        a = date(*map(int, d1.split("-")))
        b = date(*map(int, d2.split("-")))
        return max(0, (b - a).days)  # 自然日近似 ≥ 交易日数，冷却判保守方向正确
    except Exception:
        return 0


def evaluate(rows: List[Dict[str, Any]],
             tradeable_map: Dict[str, bool],
             as_of: str,
             base_dir: str,
             cfg: Optional[Dict[str, Any]] = None,
             calendar: Optional[List[str]] = None) -> Dict[str, Any]:
    """评估晋升/降级建议 + 冷却期。

    rows：eligibility_history（as_of 升序）。
    tradeable_map：{code: 现行 tradeable 布尔}。
    返回：{"promotions", "demotions", "cooldown_active", "as_of"}
         每项事件 {code, name?, days, last_score, tier?, kind}
    """
    c = dict(DEFAULTS)
    if cfg:
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    if not c.get("enabled", True):
        return {"promotions": [], "demotions": [], "cooldown_active": [],
                "as_of": as_of, "enabled": False}

    state_path = os.path.join(base_dir, c["state_path"])
    state = _load_json(state_path, {})  # {code: {"downgraded_at": as_of}}

    promotions: List[Dict[str, Any]] = []
    demotions: List[Dict[str, Any]] = []
    cooldown_active: List[Dict[str, Any]] = []

    codes = set(tradeable_map.keys())
    for r in rows[-1:]:
        codes |= set((r.get("scores") or {}).keys())

    for code in sorted(codes):
        is_tr = bool(tradeable_map.get(code, False))
        cd = trading_days_between(state.get(code, {}).get("downgraded_at", ""),
                                  as_of, calendar)
        in_cooldown = (code in state and 0 <= cd < int(c["cooldown_days"]))

        if not is_tr:
            n = _consecutive_full(rows, code, float(c["promote"]["min_tier_score"]))
            if n >= int(c["promote"]["consecutive_days"]):
                if in_cooldown:
                    cooldown_active.append({
                        "code": code, "kind": "promotion_blocked_cooldown",
                        "days": n, "cooldown_remaining":
                            int(c["cooldown_days"]) - cd,
                        "downgraded_at": state[code].get("downgraded_at"),
                    })
                else:
                    promotions.append({"code": code, "kind": "promotion",
                                       "days": n,
                                       "last_score": _last_score(rows, code)})
        else:
            n = _consecutive_below(rows, code, float(c["demote"]["max_score"]))
            if n >= int(c["demote"]["consecutive_days"]):
                demotions.append({"code": code, "kind": "demotion",
                                  "days": n,
                                  "last_score": _last_score(rows, code)})

    return {"promotions": promotions, "demotions": demotions,
            "cooldown_active": cooldown_active, "as_of": as_of,
            "enabled": True}


def _last_score(rows: List[Dict[str, Any]], code: str) -> Optional[float]:
    if not rows:
        return None
    s = ((rows[-1].get("scores") or {}).get(code) or {})
    return s.get("score")


def record_events(base_dir: str, as_of: str,
                  result: Dict[str, Any],
                  cfg: Optional[Dict[str, Any]] = None) -> int:
    """把本次评估事件写入台账 JSONL（append；同 as_of 幂等去重）。"""
    c = dict(DEFAULTS)
    if cfg:
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    path = os.path.join(base_dir, c["events_path"])
    events: List[Dict[str, Any]] = []
    for e in (result.get("promotions") or []):
        events.append({**e, "date": as_of, "bucket": "lifecycle",
                       "action": "建议人工晋升（config tradeable→true）"})
    for e in (result.get("demotions") or []):
        events.append({**e, "date": as_of, "bucket": "lifecycle",
                       "action": "建议人工降级（FROZEN 仓位归零已兜底）"})
    for e in (result.get("cooldown_active") or []):
        events.append({**e, "date": as_of, "bucket": "lifecycle",
                       "action": "晋升被冷却期阻断"})
    if not events:
        return 0
    existing: List[Dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = [json.loads(x) for x in f if x.strip()]
        except Exception:
            existing = []
    seen = {(e.get("date"), e.get("code"), e.get("kind")) for e in existing}
    added = 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            key = (e.get("date"), e.get("code"), e.get("kind"))
            if key in seen:
                continue
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
            seen.add(key)
            added += 1
    return added


def mark_demotions(base_dir: str, as_of: str,
                   result: Dict[str, Any],
                   cfg: Optional[Dict[str, Any]] = None) -> None:
    """把本次降级写入冷却状态文件（downgraded_at），启动 10 日冷却。"""
    c = dict(DEFAULTS)
    if cfg:
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    if not result.get("demotions"):
        return
    state_path = os.path.join(base_dir, c["state_path"])
    state = _load_json(state_path, {})
    for e in result["demotions"]:
        code = e.get("code")
        if code and not state.get(code, {}).get("downgraded_at"):
            state[code] = {"downgraded_at": as_of,
                           "last_score": e.get("last_score")}
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, state_path)


# ---------------------------------------------------------------- 状态调制乘数 M

def modulation_multiplier(e_ratio: float,
                          cfg: Optional[Dict[str, Any]] = None) -> float:
    """E 态占比 → 全局仓位调制 M。

    默认：占比 ≤20% → 1.0；每 +10pp 线性衰减（slope 1.25）；
    下限 0.5。66%（11-05）→ 0.5。
    """
    m = dict(DEFAULTS_MOD)
    if cfg:
        m.update(cfg or {})
    start = float(m.get("start_ratio", 0.20))
    slope = float(m.get("slope", 1.25))
    floor = float(m.get("floor", 0.50))
    over = max(0.0, float(e_ratio) - start)
    return max(floor, min(1.0, 1.0 - over * slope))


DEFAULTS_MOD: Dict[str, Any] = {"start_ratio": 0.20, "slope": 1.25, "floor": 0.50}
