# -*- coding: utf-8 -*-
"""eligibility.py — B2 动态资格分引擎（P1 · 2026-11-06）

S++ 可交易标的体系 B2 改良版核心模块（策划案见 docs/tradeable_universe_review_20261106.html）。

设计要点（与 B1 原案的差异全部源于批判性复审）：
  1. 四因子资格分：investability 30 / state_fit 40 / data_quality 20 / map_ready 10（0–100）。
  2. StateFit 矩阵 5 态 × 3 角色族——角色族由现有 config role 字段归并，零新概念。
  3. 风险预算不在此实现（用现有 bucket_max 系数表，纯配置）。
  4. 生命周期不在此实现（P3 用 2 条连续性规则读本模块落盘的 JSONL）。
  5. fail-closed：任何异常 → 全标的 FROZEN（与 market_context on_error=veto 同哲学）。

纯函数模块：除标准库外零依赖；输入全部来自现有产物
（decision JSON per_index / etf_quotes.json / quality_warnings），不改写任何现有结构。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 常量与默认

TIERS = ("FULL", "REDUCED", "OBSERVE", "FROZEN")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "weights": {"investability": 0.30, "state_fit": 0.40,
                "data_quality": 0.20, "map_ready": 0.10},
    "tiers": {"full": 80, "reduced": 60, "observe": 40},
    "hard_rules": {"state_fit_zero_frozen": True},
    "investability": {"amount_full_yi": 5.0, "amount_floor_yi": 0.3,
                      "premium_max_pct": 1.5},
    "role_families": {
        "offense": ["进攻候选", "上游映射", "固定跟踪"],
        "event": ["事件驱动", "左侧候选"],
        "reference": ["市场基准", "风格参照", "全球上游", "领先观察", "筛选源观察"],
    },
    "reference_tradeable_as_offense": True,
    "state_fit": {
        "offense":   {"A": 0.5, "B": 1.0, "C": 1.0, "D": 0.5, "E": 0.0},
        "event":     {"A": 0.8, "B": 0.6, "C": 0.3, "D": 0.0, "E": 0.0},
        # reference 行 = 状态质量的中性衡量（非执行许可）：
        # watch 池晋升通道依赖此行——A/B 态给分使其可到 OBSERVE/FULL，由 P3
        # 连续性规则决定晋升；执行仍由 tier × 现行 tradeable 联合决定。
        # E 态 0 → 硬闸 FROZEN 不变；D 态 0.2 走 OBSERVE 档。
        "reference": {"A": 0.5, "B": 0.8, "C": 0.6, "D": 0.2, "E": 0.0},
    },
    "data_quality": {"gap_penalty": 0.3, "warn_penalty": 0.6},
    "map_ready": {"confirmed_verified": 1.0, "confirmed_unverified": 0.8,
                  "needs_review": 0.5, "none": 0.0},
    "fail_closed": True,
}

# quality_warnings 中"日期断裂"类关键词 → 最重惩罚档
_GAP_KEYWORDS = ("日期断裂",)
_STATE_KEYS = ("A", "B", "C", "D", "E")


def _cfg_merge(cfg_section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """config eligibility 段与 DEFAULTS 深合并（段缺失/部分字段均安全）。"""
    base = json.loads(json.dumps(DEFAULTS))  # deep copy
    if not cfg_section:
        return base
    for k, v in cfg_section.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def _clamp01(x: Optional[float]) -> float:
    if x is None:
        return 0.0
    return max(0.0, min(1.0, float(x)))


# ---------------------------------------------------------------- 因子一：可投资性

def investability_score(amount: Optional[float], premium: Optional[float] = None,
                        cfg: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """成交额线性插值 0.3–5 亿；|溢价率|>1.5% 线性扣分。

    amount 单位：元（etf_quotes.json quotes[code]["amount"]）。
    premium：溢价率（小数，如 0.012 = 1.2%）；None → 不罚（P4 接入溢价源）。
    amount 缺失 → None（上层按保守 0 计）。
    """
    c = (cfg or {}).get("investability", DEFAULTS["investability"])
    full = float(c["amount_full_yi"]) * 1e8
    floor = float(c["amount_floor_yi"]) * 1e8
    if amount is None:
        return None
    amt = float(amount)
    if amt <= floor:
        base = 0.0
    elif amt >= full:
        base = 1.0
    else:
        base = (amt - floor) / (full - floor)
    score = base
    pmax = float(c.get("premium_max_pct", 1.5)) / 100.0
    if premium is not None:
        over = abs(float(premium)) - pmax
        if over > 0:
            score = max(0.0, base - min(1.0, over / pmax))  # 超一倍阈值 → 归零
    return _clamp01(score)


# ---------------------------------------------------------------- 因子二：状态契合

def role_family(role: Optional[str], tradeable: bool,
                cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """现有 role 字段 → 角色族（offense/event/reference）。

    参照族但现行 tradeable=true（人工已确认执行资格，如恒生科技"领先观察"）
    → 按进攻族计（reference_tradeable_as_offense）。
    未登记 role → None（StateFit 保守 0）。
    """
    c = _cfg_merge(cfg) if cfg and "state_fit" not in cfg else (cfg or DEFAULTS)
    fams = c.get("role_families") or DEFAULTS["role_families"]
    for fam, roles in fams.items():
        if role in roles:
            if (fam == "reference" and tradeable
                    and c.get("reference_tradeable_as_offense", True)):
                return "offense"
            return fam
    return None


def state_fit_score(family: Optional[str], dominant: Optional[str],
                    cfg: Optional[Dict[str, Any]] = None) -> float:
    """查 5 态 × 3 角色族矩阵；族未定/态未定 → 0（保守）。"""
    c = _cfg_merge(cfg) if cfg and "state_fit" not in cfg else (cfg or DEFAULTS)
    matrix = c.get("state_fit") or DEFAULTS["state_fit"]
    if family not in matrix or dominant not in _STATE_KEYS:
        return 0.0
    return _clamp01(matrix[family].get(dominant, 0.0))


# ---------------------------------------------------------------- 因子三：数据质量

def data_quality_score(name: Optional[str], warnings: Optional[List[str]],
                       cfg: Optional[Dict[str, Any]] = None) -> float:
    """quality_warnings 按标的名称前缀（"名称：..."）匹配；多重警告取最重档。

    无警告 1.0 / 断裂类 0.3 / 其他（仅收盘价、跳空等）0.6。
    name 缺失 → 有任何全局警告即按 0.6 保守计？否——P1 口径：name 缺失视为无
    匹配信息，返回 1.0 会在 fail-open；改为 None 并由上层按 0 计更安全。
    这里 name=None 直接返回 None。
    """
    c = (cfg or {}).get("data_quality", DEFAULTS["data_quality"])
    if name is None:
        return None
    if not warnings:
        return 1.0
    worst = 1.0
    for w in warnings:
        w = str(w)
        if not w.startswith(str(name)):
            continue
        if any(k in w for k in _GAP_KEYWORDS):
            worst = min(worst, float(c["gap_penalty"]))
        else:
            worst = min(worst, float(c["warn_penalty"]))
    return worst


# ---------------------------------------------------------------- 因子四：映射就绪

def map_ready_score(instrument: Optional[Dict[str, Any]],
                    cfg: Optional[Dict[str, Any]] = None) -> float:
    """instrument（per_index 内嵌）status/verified → 1.0/0.8/0.5/0.0。"""
    c = (cfg or {}).get("map_ready", DEFAULTS["map_ready"])
    if not instrument:
        return float(c["none"])
    status = instrument.get("status")
    if status == "confirmed":
        return float(c["confirmed_verified"] if instrument.get("verified")
                     else c["confirmed_unverified"])
    if status == "needs_review":
        return float(c["needs_review"])
    return float(c["none"])


# ---------------------------------------------------------------- 合成

def tier_of(score: float, cfg: Optional[Dict[str, Any]] = None,
            state_fit: float = 1.0) -> str:
    """四档权限；硬规则：StateFit=0（E 态全族 / D 态事件族）→ 强制 FROZEN。"""
    c = _cfg_merge(cfg) if cfg and "tiers" not in cfg else (cfg or DEFAULTS)
    if c.get("hard_rules", {}).get("state_fit_zero_frozen", True) and state_fit <= 0.0:
        return "FROZEN"
    t = c.get("tiers") or DEFAULTS["tiers"]
    if score >= float(t["full"]):
        return "FULL"
    if score >= float(t["reduced"]):
        return "REDUCED"
    if score >= float(t["observe"]):
        return "OBSERVE"
    return "FROZEN"


def compute_entry(v: Dict[str, Any], warnings: Optional[List[str]],
                  quotes: Optional[Dict[str, Any]], amount: Optional[float],
                  cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对单只标的计算资格分。

    v：decision JSON per_index 条目（含 name/role/tradeable/state/instrument）。
    amount：该标的 confirmed ETF 当日成交额（元）；由调用方从 quotes 查得；
            无映射/无行情 → None（因子保守 0）。
    返回：{"score", "tier", "family", "factors", "notes"}（notes 记录保守化原因）。
    """
    c = _cfg_merge(cfg)
    w = c["weights"]
    notes: List[str] = []
    name = v.get("name")

    # 因子 1：可投资性
    inv = investability_score(amount, None, c)
    if inv is None:
        notes.append("amount 缺失 → investability 保守 0")
        inv_f = 0.0
    else:
        inv_f = inv

    # 因子 2：状态契合
    fam = role_family(v.get("role"), bool(v.get("tradeable")), c)
    dominant = (v.get("state") or {}).get("dominant")
    sf = state_fit_score(fam, dominant, c)
    if fam is None:
        notes.append("role 未登记 → StateFit 保守 0")
    if sf <= 0.0 and dominant:
        notes.append(f"StateFit=0（{dominant} 态 × {fam or '未定族'}）→ FROZEN 硬闸")

    # 因子 3：数据质量
    dq = data_quality_score(name, warnings, c)
    if dq is None:
        notes.append("数据质量信息缺失 → 保守 0")
        dq_f = 0.0
    else:
        dq_f = dq

    # 因子 4：映射就绪
    mr = map_ready_score(v.get("instrument"), c)

    score = 100.0 * (w["investability"] * inv_f + w["state_fit"] * sf
                     + w["data_quality"] * dq_f + w["map_ready"] * mr)
    score = round(max(0.0, min(100.0, score)), 1)
    return {
        "score": score,
        "tier": tier_of(score, c, state_fit=sf),
        "family": fam or "unknown",
        "dominant": dominant,
        "factors": {"investability": round(inv_f, 4), "state_fit": round(sf, 4),
                    "data_quality": round(dq_f, 4), "map_ready": round(mr, 4)},
        "notes": notes,
    }


def compute_all(per_index: Dict[str, Dict[str, Any]],
                warnings: Optional[List[str]],
                quotes: Optional[Dict[str, Any]],
                cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """全 universe 逐标的计算；fail-closed：任何单标的异常 → 该标的 FROZEN。

    fail_closed 全局开关开启时，引擎级异常（如 quotes 结构损坏）→ 全部 FROZEN。
    """
    c = _cfg_merge(cfg)
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for code, v in per_index.items():
            try:
                instrument = v.get("instrument") or {}
                etf_code = instrument.get("etf_code")
                amount = None
                if quotes and etf_code:
                    q = quotes.get(str(etf_code)) or {}
                    amount = q.get("amount")
                out[code] = compute_entry(v, warnings, quotes, amount, c)
            except Exception as exc:  # 单标的异常隔离
                out[code] = {"score": 0.0, "tier": "FROZEN", "family": "unknown",
                             "dominant": None,
                             "factors": {"investability": 0.0, "state_fit": 0.0,
                                         "data_quality": 0.0, "map_ready": 0.0},
                             "notes": [f"单标的计算异常: {exc!r}"]}
    except Exception as exc:  # 引擎级异常 → 全体 fail-closed
        if c.get("fail_closed", True):
            frozen = {"score": 0.0, "tier": "FROZEN", "family": "unknown",
                      "dominant": None,
                      "factors": {"investability": 0.0, "state_fit": 0.0,
                                  "data_quality": 0.0, "map_ready": 0.0},
                      "notes": [f"引擎级异常 fail-closed: {exc!r}"]}
            return {code: dict(frozen) for code in per_index}
        raise
    return out


# ---------------------------------------------------------------- JSONL 落盘（P3 连续性规则读取）

def append_history(path: str, as_of: str,
                   scores: Dict[str, Dict[str, Any]]) -> int:
    """逐日资格分落盘（同 dominant history 模式）：同 as_of 幂等覆盖，其余追加。

    行格式：{"as_of": "...", "scores": {code: {"score","tier","family",...}}}
    返回写入后总行数。磁盘失败向上抛（调用方决定是否阻断——fail-closed 哲学）。
    """
    rows: List[Dict[str, Any]] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 跳过损坏行（尾部半行等），主链路写盘为原子单行
                if r.get("as_of") != as_of:
                    rows.append(r)
    rows.append({"as_of": as_of, "scores": scores})
    rows.sort(key=lambda r: r.get("as_of", ""))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)  # 原子替换
    return len(rows)


def load_history(path: str) -> List[Dict[str, Any]]:
    """读全部历史（as_of 升序）；文件缺失 → 空表。"""
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("as_of", ""))
    return rows
