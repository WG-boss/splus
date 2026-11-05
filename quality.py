# -*- coding: utf-8 -*-
"""
quality.py —— 数据软提示分级（P2-③）

现状：48 条软提示平铺在报告里，读的人无法区分「只是记录了一下真跳空」
和「ATR/ADX 已退化为收盘价代理、触发位可靠性下降」。
本模块把提示按严重度分级，规则全部外置到 splus_config.yaml.quality_display。

分级只影响展示，不改变任何数据是否参与计算（不偷偷剔除样本）。
"""
from __future__ import annotations

from typing import Any, Dict, List

LEVEL_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}


def classify(text: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    q = cfg.get("quality_display") or {}
    for rule in q.get("rules") or []:
        if str(rule.get("match") or "") and str(rule["match"]) in str(text):
            return {
                "level": str(rule.get("level") or "MED").upper(),
                "category": str(rule.get("category") or "other"),
                "label": str(rule.get("label") or rule.get("match")),
            }
    return {"level": str(q.get("default_level") or "MED").upper(),
            "category": "other", "label": "其它数据提示"}


def summarize(warnings: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, str]] = []
    for w in warnings or []:
        txt = str(w)
        if "：" in txt:
            name, detail = txt.split("：", 1)
        else:
            name, detail = "", txt
        c = classify(txt, cfg)
        items.append({"name": name.strip(), "text": detail.strip(), **c})
    items.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), x["name"], x["text"]))

    levels: Dict[str, int] = {"HIGH": 0, "MED": 0, "LOW": 0}
    groups: Dict[str, List[Dict[str, str]]] = {}
    for it in items:
        levels[it["level"]] = levels.get(it["level"], 0) + 1
        groups.setdefault(it["category"], []).append(it)
    high_names = sorted({it["name"] for it in items
                         if it["level"] == "HIGH" and it["name"]})
    q = cfg.get("quality_display") or {}
    return {
        "total": len(items),
        "levels": levels,
        "groups": groups,
        "items": items,
        "high_names": high_names,
        "high_note": str(q.get("high_note") or
                         "高severity提示：相关标的的触发位/失效位可靠性下降，"
                         "不得据此直接下单"),
        "policy": str(q.get("policy") or
                      "分级只影响展示；数据仍按原口径参与计算，不静默剔除样本"),
    }
