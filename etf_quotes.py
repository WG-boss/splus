# -*- coding: utf-8 -*-
"""
etf_quotes.py —— 场内 ETF 现价/成交额快照的运行时读取层（P1-①）

诚实性约束：
  - 只认 tools_etf_quotes.py 从公开行情接口拉取并落盘的快照，不猜测价格；
  - 快照过期 / 价格非法 / 文件损坏 → fresh=False，调用方不得据此换算份额；
  - 报价始终是【快照】，报告必须提示「下单前以券商实时价为准」。
"""
from __future__ import annotations

import datetime
import io
import json
import os
from typing import Any, Dict, Optional

DEFAULT_FILE = "data/etf_quotes.json"


def _num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "" or x == "-":
            return None
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    except Exception:
        return None


def load(base_dir: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ec = ((cfg or {}).get("execution_plan") or {})
    fname = ec.get("quotes_file") or DEFAULT_FILE
    path = os.path.join(base_dir, fname)
    max_age = float(ec.get("price_max_age_hours") or 72)
    out: Dict[str, Any] = {"ok": False, "path": path, "quotes": {},
                           "fetched_at": None, "age_hours": None,
                           "stale": True, "max_age_hours": max_age,
                           "reason": ""}
    if not os.path.exists(path):
        out["reason"] = "行情快照不存在（运行 tools_etf_quotes.py --refresh）"
        return out
    try:
        doc = json.load(io.open(path, "r", encoding="utf-8")) or {}
    except Exception as exc:
        out["reason"] = f"行情快照损坏：{type(exc).__name__}: {exc}"
        return out
    q = doc.get("quotes") or {}
    if not isinstance(q, dict) or len(q) < 100:
        out["reason"] = "行情快照条目过少，疑似分页不全"
        return out
    ts = str(doc.get("fetched_at") or "")
    age = None
    try:
        dt = datetime.datetime.fromisoformat(ts[:19])
        age = (datetime.datetime.now() - dt).total_seconds() / 3600.0
    except Exception:
        pass
    out.update(ok=True, quotes=q, fetched_at=ts, age_hours=age,
               count=len(q), source=doc.get("source"))
    if age is None:
        out["stale"] = True
        out["reason"] = "缺少可解析的 fetched_at"
    elif age > max_age:
        out["stale"] = True
        out["reason"] = f"行情快照过期 {age:.1f}h > {max_age:.0f}h"
    else:
        out["stale"] = False
    return out


def for_code(snapshot: Dict[str, Any], code: str) -> Dict[str, Any]:
    """返回单只 ETF 的可用报价。fresh=False 时 price=None。"""
    rec = (snapshot.get("quotes") or {}).get(str(code)) or {}
    price = _num(rec.get("price"))
    amount = _num(rec.get("amount"))
    res: Dict[str, Any] = {
        "code": str(code), "name": rec.get("name") or "",
        "market": rec.get("market"), "price": None, "amount": amount,
        "fresh": False, "reason": "",
    }
    if not rec:
        res["reason"] = "快照中无此代码"
        return res
    if snapshot.get("stale") or not snapshot.get("ok"):
        res["reason"] = snapshot.get("reason") or "快照不可用"
        return res
    if price is None or price <= 0:
        res["reason"] = "停牌或价格非法"
        return res
    # 场内 ETF 合理价格区间；超出说明接口单位缩放异常，宁可不用
    if price < 0.05 or price > 50.0:
        res["reason"] = f"价格 {price} 超出合理区间（疑似单位异常）"
        return res
    res.update(price=price, fresh=True)
    return res
