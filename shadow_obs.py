"""影子观察台账（2026-11-09）

`pool.mode: shadow` 观察期的逐日对照记录与预先写死的 go/no-go 判据。

设计立场（S 级）：
1. **零干预**：本模块只读决策 JSON，不改权限、不改仓位、不建池。池模块在
   shadow 下同样零干预（pool.apply → applied=False；engine 的
   apply_pool_risk_budget 只认 applied=True）。
2. **观察运营可行性，不观察收益**：两周约 10 个交易日，任何日频超额 t 都
   不可能显著，用收益做 go/no-go 是伪统计（docs/POOL_DYNAMIC_20261109.md
   里 6 条规则的名次两次重跑就翻转）。因此判据只覆盖：建池稳定性、映射
   就绪度、容量口径、资格分冲突、换手与数据完整性。收益只记录、不判定。
3. **判据在观察开始前写死并外置**：全部阈值在 splus_config.yaml 的
   `pool.shadow` 段，避免两周后按结果挑阈值（事后选样偏差）。
4. **盘中不算观察日**：上一轮实测盘中半日额驱动建池会凭空产生换手；
   intraday_hold 的日子标记 valid_session=False，不计入 10 个交易日。

纯 stdlib，无外部依赖。产物：state/shadow_observation.jsonl（gitignore）。
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional

# 判据默认值（配置可覆盖；观察期开始后不得再改阈值）
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "ledger": "state/shadow_observation.jsonl",
    "start": "2026-11-09",
    "end": "2026-11-23",
    "review_on": "2026-11-23",
    "min_sessions": 9,            # 两周 ≈ 11 个交易日，允许 2 天缺数据
    "max_invalid_days": 1,        # 盘中/异常天数上限
    "min_measured_ratio": 0.90,   # 容量为收盘全天额（measured）的比例
    "min_mapped_ratio": 1.00,     # confirmed ∧ verified 必须 100%
    "max_avg_turnover": 0.20,     # 日均单边换手上限（R6 回测年化 14.7x ≈ 6%/日）
    "max_conflict_sessions": 0,   # 资格 FROZEN / 容量 unknown 的冲突日
    "max_na_ratio": 1.00,         # 只记录，默认不设闸（NA 是模型诚实表达，不是故障）
}


def cfg_of(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    sec = ((cfg or {}).get("pool") or {}).get("shadow") or {}
    out.update({k: v for k, v in sec.items() if v is not None})
    return out


def _path(base_dir: str, c: Dict[str, Any]) -> str:
    p = c["ledger"]
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def _read(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue          # 损坏行跳过，不让一条坏记录毁掉整个台账
            if isinstance(r, dict) and r.get("as_of"):
                rows.append(r)
    return rows


def _write(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in sorted(rows, key=lambda x: x.get("session_date") or x.get("as_of", "")):
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _baseline(cfg: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """对照组 = 配置静态 tradeable 白名单（shadow 下实际生效的权限）。"""
    u = (cfg or {}).get("universe") or []
    codes = [str(x["code"]) for x in u if x.get("tradeable")]
    if codes:
        return codes
    # 兜底：配置不可读时，用决策 JSON 里实际生效的 tradeable（已在 shadow 下）
    return sorted(c for c, v in (result.get("per_index") or {}).items()
                  if v.get("tradeable"))


def record(base_dir: str, cfg: Dict[str, Any],
           result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一个 as_of 的影子对照写进台账（同日幂等替换）。非 shadow 返回 None。

    在 engine.run 落盘前调用：此时 result 已含 per_index / pool / disclosures。
    """
    c = cfg_of(cfg)
    mode = str(((cfg.get("pool") or {}).get("mode")) or "fixed")
    if not c.get("enabled") or mode != "shadow":
        return None
    as_of = str(result.get("as_of") or "")
    if not as_of:
        return None
    # 幂等键 / 窗口键用**运行日**，不是数据日：
    #   - 2026-11-09 12:36 跑，数据 as_of 仍是 11-06；按 as_of 会被窗口
    #     11-09→11-23 直接过滤，观察期第一天凭空消失。
    #   - 数据延迟时连续两天 as_of 相同，按 as_of 覆盖会少计一个观察日。
    # 同日多次运行（盘中预览 + 收盘正式）按设计互相替换，收盘那条胜出。
    session_date = str(result.get("generated_at") or "")[:10] or as_of

    pool = result.get("pool") or {}
    per = result.get("per_index") or {}
    caps: Dict[str, int] = {}
    members: List[Dict[str, Any]] = []
    for m in pool.get("members") or []:
        code = str(m.get("code") or "")
        v = per.get(code) or {}
        ins = v.get("instrument") or {}
        cap = m.get("capacity") or {}
        st = str(cap.get("status") or "unknown")
        caps[st] = caps.get(st, 0) + 1
        members.append({
            "code": code, "name": m.get("name"), "rank": m.get("rank"),
            "rs20": m.get("rs20"), "state": m.get("state"),
            "rating": m.get("rating"), "state_action": m.get("state_action"),
            "capacity_status": st,
            "instrument_status": ins.get("status"),
            "verified": bool(ins.get("verified")),
            "eligibility_tier": (v.get("eligibility") or {}).get("tier"),
            # shadow 下这是「现行基线口径」的仓位；影子池标的常为 0，
            # 因为静态白名单里没有它——这本身就是制度冲突信号。
            "sizing_final_baseline": (v.get("sizing") or {}).get("final"),
        })

    shadow_codes = [m["code"] for m in members]
    base_codes = _baseline(cfg, result)
    s, b = set(shadow_codes), set(base_codes)
    only_shadow, only_base, overlap = sorted(s - b), sorted(b - s), sorted(s & b)
    union = len(s | b)

    n = len(members)
    measured = sum(1 for m in members if m["capacity_status"] == "measured")
    mapped = sum(1 for m in members
                 if m["instrument_status"] == "confirmed" and m["verified"])
    frozen_n = sum(1 for m in members if m["eligibility_tier"] == "FROZEN")
    unknown_n = sum(1 for m in members
                    if m["capacity_status"] in ("unknown", "stale", "intraday"))

    intraday = bool(pool.get("intraday_hold")) or bool(
        (pool.get("capacity") or {}).get("quote_intraday"))
    err = pool.get("error")
    reasons: List[str] = []
    if not pool.get("enabled"):
        reasons.append("pool_disabled")
    if err:
        reasons.append("pool_error")
    if intraday:
        reasons.append("intraday_hold")
    if (pool.get("capacity") or {}).get("quote_stale"):
        reasons.append("quote_stale")
    valid = not reasons

    row = {
        "session_date": session_date,   # 运行日：窗口 / 幂等 / 计数用
        "as_of": as_of,                 # 数据日：仅追溯用
        "generated_at": result.get("generated_at"),
        "mode": mode,
        "valid_session": valid,
        "invalid_reason": ";".join(reasons) or None,
        "intraday_hold": intraday,
        "pool_error": err,
        "shadow_pool": {
            "rule": pool.get("rule"), "universe_executable": pool.get("universe_executable"),
            "n": n, "members": members,
            "turnover_one_way": (pool.get("metrics") or {}).get("turnover_one_way"),
            "exits": [{"code": e.get("code"), "reason": e.get("reason")}
                      for e in pool.get("exits") or []],
            "frozen_today": pool.get("frozen_today") or {},
            "capacity_states": caps,
        },
        "baseline": {"n": len(base_codes), "codes": base_codes,
                     "source": "config.universe[tradeable=true]"},
        "diff": {"only_shadow": only_shadow, "only_baseline": only_base,
                 "overlap_codes": overlap, "overlap": len(overlap),
                 "jaccard": round(len(overlap) / union, 4) if union else 0.0},
        "portfolio": {
            "total_exposure": result.get("total_exposure"),
            "na_count": sum(1 for v in per.values()
                            if (v.get("state") or {}).get("rating") == "NA"),
            "n_all": len(per),
            "high_disclosures": [x.get("id") for x in result.get("disclosures") or []
                                 if x.get("level") == "HIGH"],
            "data_gaps": len(result.get("data_gaps") or []),
        },
        "checks": {
            "measured_ratio": round(measured / n, 4) if n else 0.0,
            "mapped_ratio": round(mapped / n, 4) if n else 0.0,
            "eligibility_frozen_n": frozen_n,
            "capacity_unknown_n": unknown_n,
            "conflict": bool(frozen_n or unknown_n or err),
        },
        "note": pool.get("note"),
    }
    path = _path(base_dir, c)
    rows = [r for r in _read(path)
            if (r.get("session_date") or r.get("as_of")) != session_date]
    rows.append(row)
    _write(path, rows)
    return row


def _window_rows(rows: List[Dict[str, Any]], c: Dict[str, Any]
                 ) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        d = str(r.get("session_date") or r.get("as_of") or "")
        if str(c["start"]) <= d <= str(c["end"]):
            out.append(r)
    return out


def _criteria(s: Dict[str, Any], c: Dict[str, Any]) -> List[Dict[str, Any]]:
    def add(cid: str, label: str, value: Any, thr: Any, ok: bool) -> None:
        out.append({"id": cid, "label": label, "value": value,
                    "threshold": thr, "pass": bool(ok)})
    out: List[Dict[str, Any]] = []
    add("c_sessions", "有效观察交易日 ≥ 门槛", s["sessions_valid"],
        c["min_sessions"], s["sessions_valid"] >= int(c["min_sessions"]))
    add("c_invalid", "无效/异常日 ≤ 上限", s["invalid_days"],
        c["max_invalid_days"], s["invalid_days"] <= int(c["max_invalid_days"]))
    add("c_measured", "容量 measured 比例（均值）≥ 门槛",
        s["measured_ratio_avg"], c["min_measured_ratio"],
        s["measured_ratio_avg"] >= float(c["min_measured_ratio"]))
    add("c_mapped", "映射 confirmed∧verified 比例（均值）≥ 门槛",
        s["mapped_ratio_avg"], c["min_mapped_ratio"],
        s["mapped_ratio_avg"] >= float(c["min_mapped_ratio"]) - 1e-9)
    add("c_turnover", "影子池日均单边换手 ≤ 上限",
        s["avg_turnover"], c["max_avg_turnover"],
        s["avg_turnover"] <= float(c["max_avg_turnover"]))
    add("c_conflict", "制度冲突日（资格 FROZEN/容量 unknown/建池异常）≤ 上限",
        s["conflict_sessions"], c["max_conflict_sessions"],
        s["conflict_sessions"] <= int(c["max_conflict_sessions"]))
    return out


def summarize(base_dir: str, cfg: Optional[Dict[str, Any]] = None,
              today: Optional[str] = None) -> Dict[str, Any]:
    """两周汇总 + 预先写死的 go/no-go 判据。收益明确不参与判定。"""
    c = cfg_of(cfg)
    rows = _window_rows(_read(_path(base_dir, c)), c)
    valid = [r for r in rows if r.get("valid_session")]
    invalid = [r for r in rows if not r.get("valid_session")]

    def avg(xs: List[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    tod = today or datetime.date.today().isoformat()
    try:
        days_left = (datetime.date.fromisoformat(str(c["review_on"]))
                     - datetime.date.fromisoformat(tod)).days
    except Exception:
        days_left = None

    s: Dict[str, Any] = {
        "window": {"start": c["start"], "end": c["end"],
                   "review_on": c["review_on"], "today": tod,
                   "days_to_review": days_left},
        "sessions_total": len(rows),
        "sessions_valid": len(valid),
        "target_sessions": int(c["min_sessions"]),
        "invalid_days": len(invalid),
        "invalid_detail": [{"session_date": r.get("session_date") or r["as_of"],
                            "as_of": r["as_of"], "reason": r.get("invalid_reason")}
                           for r in invalid],
        "avg_shadow_n": avg([_f((r.get("shadow_pool") or {}).get("n")) for r in valid]),
        "avg_baseline_n": avg([_f((r.get("baseline") or {}).get("n")) for r in valid]),
        "avg_overlap": avg([_f((r.get("diff") or {}).get("overlap")) for r in valid]),
        "avg_jaccard": avg([_f((r.get("diff") or {}).get("jaccard")) for r in valid]),
        "avg_turnover": avg([_f((r.get("shadow_pool") or {}).get("turnover_one_way"))
                             for r in valid]),
        "measured_ratio_avg": avg([_f((r.get("checks") or {}).get("measured_ratio"))
                                   for r in valid]),
        "measured_ratio_min": min([_f((r.get("checks") or {}).get("measured_ratio"))
                                   for r in valid], default=0.0),
        "mapped_ratio_avg": avg([_f((r.get("checks") or {}).get("mapped_ratio"))
                                 for r in valid]),
        "conflict_sessions": sum(1 for r in valid
                                 if (r.get("checks") or {}).get("conflict")),
        "conflict_detail": [r["as_of"] for r in valid
                            if (r.get("checks") or {}).get("conflict")],
        "avg_na_ratio": avg([_f((r.get("portfolio") or {}).get("na_count"))
                             / max(1, _f((r.get("portfolio") or {}).get("n_all")))
                             for r in valid]),
        "avg_total_exposure": avg([_f((r.get("portfolio") or {}).get("total_exposure"))
                                   for r in valid]),
        "return_note": ("收益不作为 go/no-go 判据：两周约 10 个交易日，"
                        "日频超额不可能显著；R0–R6 两次独立面板重跑名次互换。"
                        "本观察只回答「能否稳定、可执行、无制度冲突地运营动态池」。"),
    }
    s["criteria"] = _criteria(s, c)
    failed = [x["id"] for x in s["criteria"] if not x["pass"]]
    # 观察期未满时一律 INCOMPLETE，判据失败只作为早期预警，不作最终结论。
    # 否则「异常日多 → 第 3 天就判 NO_GO」等于用不完整样本提前定案，
    # 与「两周后再决策」的约定冲突。
    if s["sessions_valid"] < int(c["min_sessions"]):
        s["decision"] = "INCOMPLETE"
        s["reasons"] = [f"观察期未结束：有效交易日 {s['sessions_valid']}/"
                        f"{c['min_sessions']}，距复核 {days_left} 天；不得提前下结论"]
        if failed:
            s["reasons"].append("早期预警（不作最终判定）：" + "、".join(failed))
    elif failed:
        s["decision"] = "NO_GO"
        s["reasons"] = ["未通过判据：" + "、".join(failed)]
    else:
        s["decision"] = "GO"
        s["reasons"] = ["全部预先写死的运营判据通过；可提交人工复核后切 dynamic"]
    s["failed_criteria"] = failed
    return s


def progress(base_dir: str, cfg: Optional[Dict[str, Any]] = None,
             today: Optional[str] = None) -> Dict[str, Any]:
    """给当日报告用的轻量进度块（不做完整汇总）。"""
    return summarize(base_dir, cfg, today)


def render(s: Dict[str, Any]) -> str:
    """汇总 → Markdown（CLI / 两周后自动化复核复用）。"""
    w = s["window"]
    L = ["# 影子观察期汇总 · R6 宽缓冲动态池", "",
         f"- 窗口：**{w['start']} → {w['end']}**，复核日 **{w['review_on']}**"
         f"（距复核 {w['days_to_review']} 天）",
         f"- 有效观察日：**{s['sessions_valid']}/{s['target_sessions']}**"
         f"（台账共 {s['sessions_total']} 行，无效 {s['invalid_days']} 行）",
         f"- **结论：`{s['decision']}`** —— " + "；".join(s["reasons"]), "",
         "## 一、逐日对照（影子池 vs 现行静态白名单）", "",
         "| 指标 | 均值 |", "|---|---|",
         f"| 影子池只数 | {s['avg_shadow_n']} |",
         f"| 现行可交易只数 | {s['avg_baseline_n']} |",
         f"| 重合只数 / Jaccard | {s['avg_overlap']} / {s['avg_jaccard']} |",
         f"| 影子池日均单边换手 | {s['avg_turnover']:.4f} |",
         f"| 现行组合日均总敞口 | {s['avg_total_exposure']:.4f} |",
         f"| 评级 NA 占比 | {s['avg_na_ratio']:.4f} |", "",
         "## 二、预先写死的判据（观察开始前设定，不得事后调整）", "",
         "| ID | 判据 | 实测 | 门槛 | 结果 |", "|---|---|---|---|---|"]
    for x in s["criteria"]:
        L.append(f"| `{x['id']}` | {x['label']} | {x['value']} | {x['threshold']} | "
                 f"{'✅' if x['pass'] else '❌'} |")
    L += ["", "## 三、异常与冲突", "",
          f"- 无效/异常日：{s['invalid_days']} 天"
          + ("（" + "、".join(f"{d['session_date']}:{d['reason']}"
                          for d in s["invalid_detail"]) + "）"
             if s["invalid_detail"] else ""),
          f"- 制度冲突日：{s['conflict_sessions']} 天"
          + ("（" + "、".join(s["conflict_detail"]) + "）"
             if s["conflict_detail"] else ""),
          f"- 容量 measured 比例：均值 {s['measured_ratio_avg']} / "
          f"最差单日 {s['measured_ratio_min']}",
          "", f"> {s['return_note']}", "",
          "> 本报告不构成投资建议；`GO` 仅表示运营判据通过，"
          "是否切换 `pool.mode: dynamic` 仍需人工确认。", ""]
    return "\n".join(L)


def verify_zero_intervention(result: Dict[str, Any]) -> Dict[str, Any]:
    """端到端核对：shadow 下池确实没有改动权限/仓位。

    检查三件事：①所有 pool.applied=False；②没有 weight_mult 落到仓位；
    ③sizing.decomposition 里没有 pool_state 乘数。
    """
    per = result.get("per_index") or {}
    bad_applied = [c for c, v in per.items()
                   if (v.get("pool") or {}).get("applied") is True]
    bad_mult = [c for c, v in per.items()
                if (v.get("pool") or {}).get("weight_mult") is not None]
    bad_decomp = [c for c, v in per.items()
                  if "pool_state" in ((v.get("sizing") or {}).get("decomposition") or {})]
    ok = not (bad_applied or bad_mult or bad_decomp)
    return {"ok": ok, "mode": (result.get("pool") or {}).get("mode"),
            "n": len(per), "bad_applied": bad_applied,
            "bad_weight_mult": bad_mult, "bad_pool_state": bad_decomp,
            "note": "零干预：影子池只记录，权限/仓位/敞口与 fixed 模式一致"
                    if ok else "存在干预，禁止据此判定观察结果"}
