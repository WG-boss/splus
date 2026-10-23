# -*- coding: utf-8 -*-
"""
L6 元层：概率校准台账 + 决策日志

职责：
  1. 记录每次状态概率预测，在 N 个交易日后回看实际走势，计算是否命中
  2. 按概率桶统计实际命中率，生成校准曲线（reliability）
  3. 计算 Brier score，劣化则告警
  4. 维护决策日志（含事前验尸字段）

说明：
  - 台账文件：data/ledger/predictions.jsonl（追加写，不可变）
  - 校准结果：data/ledger/calibration.json
  - 样本不足时不给结论，只披露当前观测数（避免用噪声下判断）
"""

from __future__ import annotations
import os
import json
import math
import datetime
from typing import Any, Dict, List, Optional

STATE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def record_prediction(ledger_dir: str, as_of: str, per_index: Dict[str, Any]) -> str:
    """记录当期概率预测，供未来结算。"""
    path = os.path.join(ledger_dir, "predictions.jsonl")
    existing = _load_jsonl(path)
    # 幂等：同一 as_of 同标的不重复记
    seen = {(r.get("as_of"), r.get("code")) for r in existing}

    n_new = 0
    for code, v in per_index.items():
        if not v.get("tradeable", True):
            continue
        if (as_of, code) in seen:
            continue
        rec = {
            "as_of": as_of,
            "code": code,
            "name": v["name"],
            "close": v["features"]["close"],
            "probs": v["state"]["probs"],
            "dominant": v["state"]["dominant"],
            "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "settled": False,
        }
        _append_jsonl(path, rec)
        n_new += 1
    return path


def settle_predictions(ledger_dir: str, bars_by_code: Dict[str, List[Dict[str, Any]]],
                       horizon: int = 20) -> Dict[str, Any]:
    """
    结算已到期的预测：取 as_of 之后第 horizon 个交易日的收盘，
    判断主状态方向是否与实际一致。

    命中定义（可证伪，不模糊）：
      - 主状态为 B/C（看多）→ 前瞻收益 > 0 记命中
      - 主状态为 E（看空）→ 前瞻收益 < 0 记命中
      - 主状态为 A/D（中性/震荡）→ |前瞻收益| < 5% 记命中
    """
    path = os.path.join(ledger_dir, "predictions.jsonl")
    recs = _load_jsonl(path)
    if not recs:
        return {"ok": False, "reason": "无预测记录"}

    changed = False
    results = []
    for r in recs:
        if r.get("settled"):
            continue
        code = r["code"]
        bars = bars_by_code.get(code) or []
        if not bars:
            continue
        dates = [b["date"] for b in bars]
        if r["as_of"] not in dates:
            continue
        i0 = dates.index(r["as_of"])
        i1 = i0 + horizon
        if i1 >= len(bars):
            continue  # 未到期

        fwd = bars[i1]["close"] / bars[i0]["close"] - 1.0
        dom = r["dominant"]
        if dom in ("B", "C"):
            hit = fwd > 0
        elif dom == "E":
            hit = fwd < 0
        else:
            hit = abs(fwd) < 0.05

        r["settled"] = True
        r["settle_date"] = bars[i1]["date"]
        r["fwd_return"] = round(fwd, 5)
        r["hit"] = bool(hit)
        changed = True
        results.append({"code": code, "state": dom, "fwd": fwd, "hit": hit})

    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {
        "ok": True,
        "newly_settled": len(results),
        "results": results,
    }


def calibration(ledger_dir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """生成校准曲线 + Brier score。"""
    path = os.path.join(ledger_dir, "predictions.jsonl")
    recs = [r for r in _load_jsonl(path) if r.get("settled")]

    horizon = cfg["meta_layer"]["calibration"]["horizon_days"]
    min_n = cfg["meta_layer"]["calibration"]["min_bucket_n"]
    alert = cfg["meta_layer"]["calibration"]["brier_alert"]

    out: Dict[str, Any] = {
        "horizon_days": horizon,
        "brier_alert": alert,
        "settled_n": len(recs),
    }

    if len(recs) < min_n:
        out["status"] = f"样本不足（已结算 {len(recs)} 条 < 门槛 {min_n}），不建议据此调整参数"
        out["calibration_curve"] = []
        return out

    # 按"主状态置信度"分桶，用 hit 作为二分类真值
    buckets = [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]
    curve = []
    brier_terms = []
    for lo, hi in buckets:
        grp = [r for r in recs if lo <= max(r["probs"].values()) < hi]
        if len(grp) < min_n:
            curve.append({"range": f"{lo:.1f}-{hi:.1f}", "n": len(grp),
                          "note": "样本不足"})
            continue
        pred = sum(max(r["probs"].values()) for r in grp) / len(grp)
        act = sum(1 for r in grp if r.get("hit")) / len(grp)
        curve.append({
            "range": f"{lo:.1f}-{hi:.1f}",
            "n": len(grp),
            "pred_confidence": round(pred, 3),
            "actual_hit_rate": round(act, 3),
            "gap": round(act - pred, 3),
        })

    for r in recs:
        p = max(r["probs"].values())
        y = 1.0 if r.get("hit") else 0.0
        brier_terms.append((p - y) ** 2)

    bs = sum(brier_terms) / len(brier_terms)
    out["brier"] = round(bs, 4)
    out["calibration_curve"] = curve

    gaps = [abs(c["gap"]) for c in curve if "gap" in c]
    mean_gap = sum(gaps) / len(gaps) if gaps else None

    if bs > alert:
        out["status"] = f"⚠️ Brier {bs:.3f} > 阀值 {alert}，概率体系已劣化，需重新校准"
    elif mean_gap is not None and mean_gap > 0.10:
        out["status"] = f"⚠️ 平均校准偏差 {mean_gap:.2f}，置信度与实际命中率偏离过大"
    else:
        out["status"] = f"✅ 校准正常（Brier {bs:.3f}，平均偏差 {mean_gap if mean_gap is None else round(mean_gap,3)}）"

    out_path = os.path.join(ledger_dir, "calibration.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return out


def read_jsonl(path):
    """公共读取接口（selftest 与治理层共用）。"""
    return _load_jsonl(path)
