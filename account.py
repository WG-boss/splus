# -*- coding: utf-8 -*-
"""
account.py —— 账户真实净值口径（P0-①）

背景（2026-11-05 评估）：原 engine.portfolio_drawdown() 用 11 只可交易指数的
等权合成净值算"当前组合回撤"，11-04 为 -44.88%，导致 coef_dd 恒为 0.15、
开仓许可全期 0。这个数字与用户实际账户无关：空仓用户被按 -45% 回撤节流，
满仓用户也被按同一个数节流。属于口径错配，不是行情判断。

本模块职责：
  - 只认用户自己维护的 account.yaml / 逐日净值文件；
  - 真实口径优先级：current_drawdown > nav_history > nav+peak_nav；
  - 缺失 / 过期 / 非法一律降级为"未配置"，此时 dd=None → 引擎不施加回撤节流，
    并在报告顶部显式标注「演示口径，回撤节流停用」；
  - 绝不用假设组合净值偷偷顶替真实账户（宁可少一层约束，也不给错的数字）。

设计原则：无副作用、可重算、不抛异常（账户文件坏了也不能阻塞主流程）。
"""
from __future__ import annotations

import datetime
import io
import json
import os
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_FILE = "account.yaml"


def _num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    except Exception:
        return None


def _date(x: Any) -> Optional[datetime.date]:
    try:
        s = str(x)[:10]
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def _read_yaml(path: str) -> Dict[str, Any]:
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _nav_history(path: str) -> List[Dict[str, Any]]:
    """逐日净值 JSONL：每行 {"date": "YYYY-MM-DD", "nav": 123456.78}"""
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                d, v = _date(r.get("date")), _num(r.get("nav"))
                if d and v and v > 0:
                    out.append({"date": d, "nav": v})
    except Exception:
        return []
    return sorted(out, key=lambda r: r["date"])


def _dd_from_series(rows: List[Dict[str, Any]]) -> Optional[float]:
    if len(rows) < 2:
        return None
    peak = 0.0
    for r in rows:
        peak = max(peak, r["nav"])
    return rows[-1]["nav"] / peak - 1.0 if peak > 0 else None


def load(base_dir: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    读取账户状态。返回统一结构，任何异常都降级为 configured=False。
    cfg 里可覆盖 account.file / account.nav_history_file / policy.max_stale_days。
    """
    acfg = ((cfg or {}).get("account") or {})
    pol = ((cfg or {}).get("account_policy") or {})
    fname = acfg.get("file") or DEFAULT_FILE
    path = os.path.join(base_dir, fname)
    raw = _read_yaml(path)
    a = raw.get("account") or {}
    pol = {**pol, **(raw.get("policy") or {})}

    out: Dict[str, Any] = {
        "configured": False,
        "enabled": bool(a.get("enabled", True)),
        "file": fname,
        "path": path,
        "exists": os.path.exists(path),
        "dd": None,
        "nav": _num(a.get("nav")),
        "peak_nav": _num(a.get("peak_nav")),
        "capital": _num(a.get("capital")),
        "as_of": a.get("as_of"),
        "updated_at": (raw.get("meta") or {}).get("updated_at") or a.get("as_of"),
        "source": "unconfigured",
        "stale": False,
        "reason": "",
        "note": "",
        "max_stale_days": int(pol.get("max_stale_days") or 10),
        "on_missing": str(pol.get("on_missing") or "no_throttle"),
    }

    if not out["exists"]:
        out["reason"] = "account.yaml 不存在"
        return out
    if not out["enabled"]:
        out["reason"] = "account.enabled=false（显式停用）"
        return out

    # 新鲜度：净值快照过旧同样按未配置处理（不拿陈旧回撤压仓）
    d = _date(out["updated_at"])
    if d:
        age = (datetime.date.today() - d).days
        out["age_days"] = age
        if age > out["max_stale_days"]:
            out["stale"] = True
            out["reason"] = f"账户快照过期 {age} 天 > {out['max_stale_days']} 天"
            return out
    else:
        out["stale"] = True
        out["reason"] = "缺少 meta.updated_at / account.as_of，无法判定新鲜度"
        return out

    # ① 手填当前回撤（最高优先级，便于快速校准）
    manual = _num(a.get("current_drawdown"))
    if manual is not None:
        if -1.0 < manual <= 0.0:
            out.update(configured=True, dd=round(manual, 6), source="manual_drawdown",
                       note="由 account.account.current_drawdown 直接给定")
            return out
        out["reason"] = "current_drawdown 必须落在 (-1, 0]"
        return out

    # ② 逐日净值序列（最完整的真实口径）
    hpath = os.path.join(base_dir, a.get("nav_history_file") or "data/account_nav.jsonl")
    rows = _nav_history(hpath)
    dd = _dd_from_series(rows)
    if dd is not None:
        out.update(configured=True, dd=round(dd, 6), source="nav_history",
                   nav=rows[-1]["nav"], peak_nav=max(r["nav"] for r in rows),
                   as_of=rows[-1]["date"].isoformat(),
                   note=f"逐日净值 {len(rows)} 点（{hpath}）")
        return out

    # ③ 当前净值 + 历史最高净值
    nav, peak = out["nav"], out["peak_nav"]
    if nav and peak and nav > 0 and peak > 0:
        dd = nav / max(peak, nav) - 1.0
        out.update(configured=True, dd=round(dd, 6), source="nav_peak",
                   note="由 account.nav / account.peak_nav 计算")
        return out

    out["reason"] = ("账户已启用但净值字段未填：请填 nav + peak_nav + capital，"
                     "或 current_drawdown，或提供 nav_history_file")
    return out


def throttle_dd(account: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> float:
    """供 L3 使用的回撤输入。未配置一律返回 0.0（不施加回撤节流）。"""
    if account.get("configured") and account.get("dd") is not None:
        return float(account["dd"])
    return 0.0


def label(account: Dict[str, Any]) -> str:
    if account.get("configured"):
        return (f"真实账户口径（{account['source']}，{account['dd'] * 100:.2f}%，"
                f"快照 {account.get('as_of') or account.get('updated_at')}）")
    return f"演示口径（{account.get('reason') or '未接入真实净值'}），回撤节流停用"


def allocation(account: Dict[str, Any], exposure: float) -> Optional[float]:
    """敞口比例 → 参考金额（元）。capital 未填则返回 None。"""
    cap = account.get("capital")
    if not cap or cap <= 0:
        return None
    return round(cap * float(exposure), 2)


def set_snapshot(base_dir: str, *, nav: float, capital: Optional[float] = None,
                 as_of: Optional[str] = None, peak_nav: Optional[float] = None,
                 fname: str = DEFAULT_FILE) -> str:
    """
    CLI 用：更新 account.yaml 的净值快照（保留其它字段与注释以外的结构）。
    同时把逐日净值追加到 data/account_nav.jsonl，形成可复算的历史峰值。
    """
    path = os.path.join(base_dir, fname)
    raw = _read_yaml(path) or {"meta": {}, "account": {}, "policy": {}}
    meta = raw.setdefault("meta", {})
    a = raw.setdefault("account", {})
    day = as_of or datetime.date.today().isoformat()
    a["enabled"] = True
    a["nav"] = float(nav)
    a["as_of"] = day
    if capital is not None:
        a["capital"] = float(capital)
    if peak_nav is not None:
        a["peak_nav"] = float(peak_nav)
    # 手填回撤与净值序列互斥，写了净值就清掉手填值
    if "current_drawdown" in a and a["current_drawdown"] is not None:
        a["current_drawdown"] = None
    meta["updated_at"] = day
    if yaml is None:
        raise RuntimeError("缺少 pyyaml")
    with io.open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)

    hpath = os.path.join(base_dir, "data", "account_nav.jsonl")
    os.makedirs(os.path.dirname(hpath), exist_ok=True)
    rows = [json.loads(x) for x in io.open(hpath, encoding="utf-8") if x.strip()] \
        if os.path.exists(hpath) else []
    rows = [r for r in rows if str(r.get("date"))[:10] != day]
    rows.append({"date": day, "nav": float(nav)})
    rows.sort(key=lambda r: str(r.get("date")))
    with io.open(hpath, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path
