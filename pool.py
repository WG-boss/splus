# -*- coding: utf-8 -*-
"""pool.py — R6 宽缓冲动态可交易池（生产运行时）

把研究结论（docs/POOL_DYNAMIC_20261109.md §4）落成每日可执行的代码。
R6 是全表唯一「降成本、提收益、提显著性」三向同时改善的规则：

    漏斗（容量闸 → rs_20 排序 → 进 10 / 出 30 → 相关性闸 → 状态进风险预算）
    实测：年化 13.87% / Sharpe 0.51 / 年换手 14.7x / 日频超额 t 1.77
    对照 R3（出 15 / 平滑 3）：13.32% / 0.46 / 26.7x / 1.59

四条不可协商的边界
------------------
1. **容量先于信号**：买不进去的名字不进池。映射未就绪、历史不足、仅有收盘价、
   成交额地板不过关，一律在排序之前出局。
2. **排序轴单一且连续**：`rs_20`（20 日相对全宇宙中位数的超额，5 日均）。
   不用五态评分——权重表 A.25/B.85/C.90/D.40/E.10 本身就是未通过检验的
   收益排序先验，拿它排序等于把先验当证据（实测选择质量仅为 RS 的 52%）。
3. **变更要花钱**：进/出都过缓冲带（进 10 / 出 30），换手进成本口径。
4. **无前视**：只用 as_of 及之前的 bar；映射用当日快照并显式披露。

fail-closed：构建异常 → 返回 enabled=False，调用方保持原 tradeable 不变。

用法（生产由 engine.run 调用；也可单独重建/审计）：
    python tools_pool_apply.py                # 重建今日池并打印
    python tools_pool_apply.py --shadow       # 只出池，不改 tradeable
    python tools_pool_apply.py --audit        # 月度审计（容量/换手/映射漂移）
"""

from __future__ import annotations

import datetime
import json
import math
import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
STATES = ("A", "B", "C", "D", "E")

DEFAULTS: Dict[str, Any] = {
    "mode": "shadow",              # fixed（保留静态 11 只）| shadow（只算不用）| dynamic
    "file": "state/tradeable_pool.json",
    "history": "state/tradeable_pool_history.jsonl",
    "size": 10,
    "n_out": 30,
    "smooth": 5,
    "rank_axis": "rs_20",
    "corr_max": 0.90,
    "corr_win": 60,
    "corr_min_obs": 20,
    "amt_floor_yi": 0.3,           # 指数 20 日成交额中位数地板（只剔「根本没量」）
    "min_bars": 252,
    "exclude_ohlc_proxy": True,    # 仅收盘价 → ATR/ADX 代理，不进池
    "min_cross_section": 40,       # 当日横截面至少多少只才可算中位数
    "aum_yi": 0.3,                 # 组合规模（亿）→ 单只目标市值 = AUM/N
    "adv_share": 0.05,             # 单只不超过 ETF 日成交额的 5%
    "etf_adv_floor_yi": 0.3,
    "quote_max_age_hours": 72,
    "intraday_skip_capacity": True,  # 盘中半日额不参与容量判定（不驱动换仓）
    "intraday_hold_pool": True,      # 盘中直接沿用上一收盘池：不重算/不落盘/换手 0
    "freeze_days": 5,              # 熔断后冻结交易日数
    "adv_crash_pct": 0.50,         # ADV 相对上期跌超此比例 → 强制出池并冻结
    "state": {"half_on_D": True, "no_add_on_E": True, "na_below_conf": True},
}


# ------------------------------------------------------------------ 小工具
def cfg_of(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = json.loads(json.dumps(DEFAULTS))
    sec = (cfg or {}).get("pool") or {}
    for k, v in sec.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _corr(a: List[float], b: List[float]) -> Optional[float]:
    n = len(a)
    if n < 5:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def _bars_until(loaded: Dict[str, Any], code: str, as_of: str) -> List[Dict[str, Any]]:
    bars = (loaded.get(code) or {}).get("bars") or []
    return [b for b in bars if str(b["date"])[:10] <= as_of]


# ------------------------------------------------------------------ 信号
def rs20(loaded: Dict[str, Any], as_of: str, p: Dict[str, Any]
         ) -> Tuple[Dict[str, float], Dict[str, List[float]]]:
    """rs_20 的 smooth 日均值：{code: 分数}, {code: 逐日值}。

    逐日定义与回测完全一致：`close_t / close_{t-20} - 1 − 当日横截面中位数`。
    逐日横截面中位数只在「当日样本 ≥ min_cross_section」时计算，否则该日不计。
    """
    smooth = int(p["smooth"])
    need = smooth + 21
    per_code = {c: _bars_until(loaded, c, as_of)[-need:] for c in loaded}
    dates = sorted({str(b["date"])[:10] for xs in per_code.values() for b in xs})
    daily: Dict[str, Dict[str, float]] = {}
    for d in dates[-smooth:]:
        raw: Dict[str, float] = {}
        for c, xs in per_code.items():
            seg = [b for b in xs if str(b["date"])[:10] <= d]
            if len(seg) >= 21 and seg[-21]["close"]:
                raw[c] = seg[-1]["close"] / seg[-21]["close"] - 1.0
        if len(raw) < int(p["min_cross_section"]):
            continue
        med = statistics.median(raw.values())
        daily[d] = {c: v - med for c, v in raw.items()}
    hist: Dict[str, List[float]] = {}
    for d in sorted(daily):
        for c, v in daily[d].items():
            hist.setdefault(c, []).append(v)
    score = {c: _mean(xs) for c, xs in hist.items() if len(xs) >= smooth}
    return {c: v for c, v in score.items() if v is not None}, hist


def _ret_matrix(loaded: Dict[str, Any], as_of: str, win: int) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for c in loaded:
        bars = _bars_until(loaded, c, as_of)[-(win + 1):]
        for i in range(1, len(bars)):
            p0, p1 = bars[i - 1]["close"], bars[i]["close"]
            if p0:
                out.setdefault(str(bars[i]["date"])[:10], {})[c] = p1 / p0 - 1.0
    return out


def adv20(loaded: Dict[str, Any], code: str, as_of: str) -> Optional[float]:
    """指数 20 日成交额中位数（元）；无成交额字段 → None。"""
    xs = [b.get("amount") for b in _bars_until(loaded, code, as_of)[-20:]]
    vals = [float(x) for x in xs if x]
    return statistics.median(vals) if len(vals) >= 10 else None


def _quote_state(base_dir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """ETF 行情快照 + 新鲜度。过期不丢金额，但必须标 stale（容量口径随之降级）。"""
    rel = (cfg.get("execution_plan") or {}).get("quotes_file", "data/etf_quotes.json")
    path = os.path.join(base_dir, rel)
    out: Dict[str, Any] = {"path": path, "ok": False, "age_hours": None,
                           "stale": True, "intraday": False, "quotes": {}}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        out["quotes"] = raw.get("quotes") or {}
        out["fetched_at"] = raw.get("fetched_at")
        # 盘中快照：成交额是半日量，不能当全天 ADV 用（2026-11-09 热备复盘）
        out["intraday"] = bool(raw.get("intraday"))
        ts = raw.get("fetched_at")
        if ts:
            t0 = datetime.datetime.fromisoformat(str(ts))
            out["age_hours"] = round(
                (datetime.datetime.now() - t0).total_seconds() / 3600.0, 1)
            out["stale"] = out["age_hours"] > float(
                (cfg.get("execution_plan") or {}).get("price_max_age_hours", 72))
        out["ok"] = True
    except Exception as exc:
        out["error"] = repr(exc)
    return out


# ------------------------------------------------------------------ 建池
def build(cfg: Dict[str, Any], per_index: Dict[str, Any], loaded: Dict[str, Any],
          as_of: str, base_dir: str, prev: Optional[Dict[str, Any]] = None
          ) -> Dict[str, Any]:
    """返回池对象（纯函数，不修改 per_index）。异常向上抛，由调用方 fail-closed。"""
    p = cfg_of(cfg)
    n = int(p["size"])
    scores, hist = rs20(loaded, as_of, p)
    rets = _ret_matrix(loaded, as_of, int(p["corr_win"]))
    qs = _quote_state(base_dir, cfg)
    all_dates = sorted({d for d in rets})
    prev_members = {m["code"]: m for m in ((prev or {}).get("members") or [])}
    frozen = dict((prev or {}).get("frozen") or {})

    # ---------- L0 可执行宇宙（硬闸，先于任何排序） ----------
    u0: Dict[str, Dict[str, Any]] = {}
    rejected: List[Dict[str, str]] = []
    for c, v in per_index.items():
        ins = v.get("instrument") or {}
        if ins.get("status") != "confirmed" or not ins.get("verified"):
            rejected.append({"code": c, "reason": "map_not_ready",
                             "detail": str(ins.get("status") or "unmapped")})
            continue
        bars = _bars_until(loaded, c, as_of)
        if len(bars) < int(p["min_bars"]):
            rejected.append({"code": c, "reason": "history_short",
                             "detail": "%d 根 < %d" % (len(bars), p["min_bars"])})
            continue
        warns = (loaded.get(c) or {}).get("quality_warnings") or []
        if p["exclude_ohlc_proxy"] and any("仅提供收盘价" in str(w) for w in warns):
            rejected.append({"code": c, "reason": "ohlc_proxy", "detail": "仅收盘价"})
            continue
        a = adv20(loaded, c, as_of)
        if a is None:
            rejected.append({"code": c, "reason": "amount_missing", "detail": "无成交额字段"})
            continue
        if a < float(p["amt_floor_yi"]) * 1e8:
            rejected.append({"code": c, "reason": "amt_floor",
                             "detail": "%.2f 亿 < %.2f 亿"
                                       % (a / 1e8, float(p["amt_floor_yi"]))})
            continue
        if c not in scores:
            rejected.append({"code": c, "reason": "rs_unavailable", "detail": "横截面不足"})
            continue
        # 容量：单只目标市值 = AUM/N，占 ETF 日额 ≤ adv_share。
        # 只有「收盘后的全天额」才执行硬闸：
        #   * stale（隔夜/过期）→ 门槛 ×0.5 只挡极端情况；
        #   * intraday（盘中半日额）→ 完全跳过容量判定，池沿用收盘口径。
        #     2026-11-09 实测：拿半日额跑硬闸会把 U0 18→9、池 9→6，
        #     并凭空产生 16.7% 换手；盘中刷新不得驱动任何调仓。
        # 收盘后刷新（intraday=False）自动恢复全天硬闸口径。
        need = max(float(p["etf_adv_floor_yi"]) * 1e8,
                   (float(p["aum_yi"]) * 1e8 / max(1, n)) / float(p["adv_share"]))
        etf = (qs.get("quotes") or {}).get(str(ins.get("etf_code"))) or {}
        etf_amt = etf.get("amount")
        stale = bool(qs.get("stale"))
        intraday = bool(qs.get("intraday"))
        cap = {"need": round(need, 1), "etf_amt": etf_amt,
               "status": "unknown", "pass": True,
               "stale": stale, "intraday": intraday}
        if etf_amt and stale:
            cap["status"] = "stale"
            cap["pass"] = float(etf_amt) >= need * 0.5   # 隔夜额 → 只挡极端情况
        elif etf_amt and intraday and bool(p.get("intraday_skip_capacity", True)):
            cap["status"] = "intraday"
            cap["pass"] = True                          # 盘中快照不参与容量判定
        elif etf_amt:
            cap["status"] = "measured"
            cap["pass"] = float(etf_amt) >= need
        if not cap["pass"]:
            rejected.append({"code": c, "reason": "capacity",
                             "detail": "ETF 日额 %.2f 亿 < 需 %.2f 亿"
                                       % (float(etf_amt or 0) / 1e8, need / 1e8)})
            continue
        u0[c] = {"code": c, "name": v.get("name"), "score": scores[c],
                 "state": (v.get("state") or {}).get("dominant"),
                 "conf": (v.get("state") or {}).get("confidence"),
                 "adv20": a, "capacity": cap,
                 "etf_code": ins.get("etf_code")}

    # ---------- 熔断：ADV 骤降 / 上期映射失效 ----------
    frozen_out: Dict[str, str] = {}
    for c in list(u0.keys()):
        pm = prev_members.get(c)
        if not pm:
            continue
        base_adv = pm.get("adv20")
        cur = u0[c]["adv20"]
        if base_adv and cur and cur < float(base_adv) * (1.0 - float(p["adv_crash_pct"])):
            frozen_out[c] = "%s ADV 骤降 %.0f%% → 出池冻结 %d 日" % (
                as_of, 100 * (1 - cur / base_adv), int(p["freeze_days"]))
            frozen[c] = _shift(all_dates, as_of, int(p["freeze_days"]))
    for c, until in list(frozen.items()):
        if until and as_of > until:
            frozen.pop(c, None)

    rank_list = sorted((r for c, r in u0.items() if c not in frozen_out),
                       key=lambda r: (-r["score"], r["code"]))
    rank = {r["code"]: i for i, r in enumerate(rank_list)}     # 0-based

    # ---------- L2 缓冲带（保留仓优先进位 → 新进 → 补足） ----------
    size = min(n, len(rank_list))
    members: List[Dict[str, Any]] = []
    prev_set = set(prev_members)
    keep = [c for c in prev_set if c in rank and rank[c] < int(p["n_out"])]
    keep.sort(key=lambda c: rank[c])

    def _mk(c: str, reason: str) -> Dict[str, Any]:
        r = u0[c]
        since = (prev_members.get(c) or {}).get("since") or as_of
        return {"code": c, "name": r["name"], "rank": rank[c] + 1,
                "rs20": round(float(r["score"]), 6), "state": r["state"],
                "conf": None if r["conf"] is None else round(float(r["conf"]), 4),
                "adv20": r["adv20"], "capacity": r["capacity"],
                "reason": reason, "since": since}

    for c in keep:
        if len(members) >= size:
            break
        members.append(_mk(c, "keep" if c in prev_set else "enter"))

    def _too_close(c: str) -> bool:
        if not members:
            return False
        win = sorted(rets)[-int(p["corr_win"]):]
        vc = [rets[d][c] for d in win if c in rets[d]]
        for m in members:
            vm = [rets[d][m["code"]] for d in win if m["code"] in rets[d]]
            k = min(len(vc), len(vm))
            if k < int(p["corr_min_obs"]):
                continue
            rho = _corr(vc[-k:], vm[-k:])
            if rho is not None and abs(rho) > float(p["corr_max"]):
                return True
        return False

    for c in (x for x in rank if rank[x] < size and x not in {m["code"] for m in members}):
        if len(members) >= size:
            break
        if _too_close(c):
            rejected.append({"code": c, "reason": "corr",
                             "detail": "与池内相关性 > %.2f" % float(p["corr_max"])})
            continue
        members.append(_mk(c, "enter"))
    if len(members) < size:      # 相关性闸压空时，从缓冲带补足
        for c in (x for x in rank if rank[x] >= size and rank[x] < int(p["n_out"])):
            if len(members) >= size:
                break
            if c in {m["code"] for m in members} or _too_close(c):
                continue
            members.append(_mk(c, "enter"))

    # ---------- L3 状态进风险预算（不排序） ----------
    # 分工必须严格区分，否则又会退化成「用评分算仓位」：
    #   rating      → 要不要给评级（低置信 = NA，机构通行做法是留 NR 出口）
    #   add_allowed → 能不能开新仓（NA / E 态禁止新增）
    #   weight_mult → 已持仓的目标权重乘数（D 态减半），与置信度无关
    thr = float((cfg.get("state") or {}).get("min_confidence_to_act", 0.65))
    for m in members:
        st, cf = m["state"], m["conf"]
        low_conf = bool(p["state"].get("na_below_conf", True)
                        and cf is not None and cf < thr)
        m["rating"] = "NA" if low_conf else st
        if st == "D" and p["state"].get("half_on_D", True):
            m["state_action"] = "half"
        elif st == "E" and p["state"].get("no_add_on_E", True):
            m["state_action"] = "hold_only"
        elif low_conf:
            m["state_action"] = "no_add"
        else:
            m["state_action"] = "normal"
        m["weight_mult"] = 0.5 if m["state_action"] == "half" else 1.0
        m["add_allowed"] = bool(m["rating"] != "NA"
                                and m["state_action"] not in ("hold_only",))

    cur_set = {m["code"] for m in members}
    exits = []
    for c in prev_set - cur_set:
        if c in frozen_out:
            reason = "freeze"
        elif c in rank:
            reason = "rank_out" if rank[c] >= int(p["n_out"]) else "buffer"
        else:
            reason = next((r["reason"] for r in rejected if r["code"] == c), "not_executable")
        exits.append({"code": c, "name": (prev_members.get(c) or {}).get("name"),
                      "reason": reason, "rank": (rank.get(c) + 1) if c in rank else None})
    turnover = (len(cur_set - prev_set) / max(1, len(cur_set))) if prev_set else 0.0

    pool = {
        "enabled": True,
        "as_of": as_of,
        "rule": "R6-wide-buffer",
        "params": {k: p[k] for k in ("size", "n_out", "smooth", "rank_axis",
                                     "corr_max", "corr_win", "amt_floor_yi",
                                     "min_bars", "aum_yi", "adv_share")},
        "members": members,
        "exits": exits,
        "frozen": {k: v for k, v in frozen.items() if v and v >= as_of},
        "frozen_today": frozen_out,
        "universe_executable": len(u0),
        "rejected": rejected,
        "capacity": {"quote_stale": bool(qs.get("stale")),
                     "quote_intraday": bool(qs.get("intraday")),
                     "quote_age_hours": qs.get("age_hours"),
                     # stale / intraday 也计入 unknown：隔夜额、半日量都不是全天容量
                     "unknown": sum(1 for m in members
                                    if (m.get("capacity") or {}).get("status")
                                    in ("unknown", "stale", "intraday"))},
        "metrics": {"turnover_one_way": round(turnover, 4),
                    "n": len(members), "n_prev": len(prev_set),
                    "state_mix": _mix(members)},
        "rationale": ("进 %d / 出 %d / 平滑 %d：把排名 10↔11 的抖动挡在成本之外"
                      % (p["size"], p["n_out"], p["smooth"])),
    }
    return pool


def _mix(members: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for m in members:
        k = m.get("rating") or "NA"
        out[k] = out.get(k, 0) + 1
    return out


def _shift(dates: List[str], d: str, k: int) -> str:
    """交易日位移（k 个交易日之后），用于熔断冻结截止日。"""
    if d not in dates:
        return d
    i = dates.index(d)
    return dates[min(len(dates) - 1, i + k)]


# ------------------------------------------------------------------ 落盘/应用
def load(base_dir: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = cfg_of(cfg)
    path = os.path.join(base_dir, p["file"])
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save(base_dir: str, cfg: Dict[str, Any], pool: Dict[str, Any]) -> str:
    """原子写 + 历史追加（同日幂等替换）。返回主文件路径。"""
    p = cfg_of(cfg)
    path = os.path.join(base_dir, p["file"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pool, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    hpath = os.path.join(base_dir, p["history"])
    os.makedirs(os.path.dirname(hpath) or ".", exist_ok=True)
    rows = []
    if os.path.exists(hpath):
        with open(hpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("as_of") != pool["as_of"]:
                    rows.append(r)
    rows.append({"as_of": pool["as_of"],
                 "members": [m["code"] for m in pool["members"]],
                 "turnover_one_way": pool["metrics"]["turnover_one_way"],
                 "exits": [{"code": e["code"], "reason": e["reason"]}
                           for e in pool["exits"]]})
    rows.sort(key=lambda r: r.get("as_of", ""))
    with open(hpath, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def apply(per_index: Dict[str, Any], pool: Dict[str, Any],
          mode: str = "dynamic") -> Dict[str, Any]:
    """把池写回 per_index：dynamic 覆盖 tradeable；shadow **零干预**。

    2026-11-09 修正：旧实现在 shadow 下仍写入 `pool.weight_mult`，而 engine
    的仓位循环无条件读它做风险预算（D 态 ×0.5）。结果是「只算不用」的影子
    观察期其实一直在改实际仓位，对照组被污染，两周后拿不到干净 A/B。
    现在用两道保险：
      1) shadow 下 weight_mult 直接写 None，并标记 applied=False；
      2) engine.apply_pool_risk_budget 只认 applied=True。
    """
    mem = {m["code"]: m for m in pool.get("members") or []}
    shadow = (mode == "shadow")
    for c, v in per_index.items():
        m = mem.get(c)
        if m:
            v["pool"] = {"member": True, "rank": m["rank"], "reason": m["reason"],
                         "rating": m.get("rating"), "state_action": m.get("state_action"),
                         "add_allowed": m.get("add_allowed"),
                         "weight_mult": None if shadow else m.get("weight_mult"),
                         "since": m.get("since"), "rs20": m.get("rs20"),
                         "mode": mode, "applied": not shadow}
        else:
            ex = next((e for e in pool.get("exits") or [] if e["code"] == c), None)
            v["pool"] = {"member": False,
                         "reason": (ex or {}).get("reason") or "not_ranked",
                         "mode": mode, "applied": not shadow}
        if mode == "dynamic":
            was = bool(v.get("tradeable"))
            v["tradeable"] = bool(m)
            if was != v["tradeable"]:
                v["pool_permission_changed"] = {"from": was, "to": v["tradeable"]}
    return {"members": len(mem), "mode": mode}


def build_and_apply(cfg: Dict[str, Any], per_index: Dict[str, Any],
                    loaded: Dict[str, Any], as_of: str, base_dir: str
                    ) -> Dict[str, Any]:
    """engine.run 的唯一入口：建池 → 落盘 → 应用 → 返回披露块。"""
    p = cfg_of(cfg)
    mode = str(p.get("mode") or "shadow")
    if mode == "fixed":
        return {"enabled": False, "mode": "fixed",
                "note": "pool.mode=fixed：沿用配置静态 tradeable 白名单"}
    if not as_of:
        return {"enabled": False, "mode": mode, "note": "无统一 as_of，跳过建池"}
    prev = load(base_dir, cfg)
    # 盘中快照（半日成交额）：池不重算、不落盘，直接沿用上一收盘结果。
    # 2026-11-09 实测：拿 11:42 的半日额跑容量闸，U0 18→9、池 9→6、
    # 凭空产生 16.7% 换手；跳过容量闸又会放到 U0=51。机构口径是
    # 「盘中数据不驱动调仓」，所以整个建池动作都跳过，而不是只松一档阈值。
    qs = _quote_state(base_dir, cfg)
    if qs.get("intraday") and bool(p.get("intraday_hold_pool", True)):
        if not prev:
            return {"enabled": False, "mode": mode, "intraday_hold": True,
                    "note": "盘中快照（半日额）且无上期池 → 跳过建池，沿用静态 tradeable"}
        held = dict(prev)
        held.update(enabled=True, mode=mode, intraday_hold=True, exits=[])
        held["note"] = ("盘中快照（半日成交额）：池沿用上一收盘结果，"
                        "未重算、未落盘、换手 0；收盘后刷新自动恢复")
        # 容量/行情元信息必须反映**当前**快照，不能沿用上期池里过期的
        # quote_age_hours（否则报告会出现「行情过期 94.6h」与盘中披露打架）。
        held["capacity"] = dict(held.get("capacity") or {})
        held["capacity"].update(
            quote_intraday=True, quote_stale=bool(qs.get("stale")),
            quote_age_hours=qs.get("age_hours"),
            unknown=len(held.get("members") or []))
        held["metrics"] = dict(held.get("metrics") or {})
        held["metrics"]["turnover_one_way"] = 0.0
        apply(per_index, held, mode=mode)
        return held
    pool = build(cfg, per_index, loaded, as_of, base_dir, prev)
    pool["mode"] = mode
    save(base_dir, cfg, pool)
    apply(per_index, pool, mode=mode)
    return pool


# ------------------------------------------------------------------ 审计
def audit(base_dir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """池的历史审计：成员数、单边换手、出池原因分布、映射/容量告警。

    只读 state/tradeable_pool_history.jsonl——不重算、不改池。
    """
    p = cfg_of(cfg)
    hpath = os.path.join(base_dir, p["history"])
    rows: List[Dict[str, Any]] = []
    if os.path.exists(hpath):
        with open(hpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    if not rows:
        return {"ok": False, "reason": "无池历史（先跑一次 run.py 或 tools_pool_apply.py）"}
    turns = [float(r.get("turnover_one_way") or 0) for r in rows]
    reasons: Dict[str, int] = {}
    for r in rows:
        for e in r.get("exits") or []:
            reasons[e.get("reason") or "?"] = reasons.get(e.get("reason") or "?", 0) + 1
    sizes = [len(r.get("members") or []) for r in rows]
    return {
        "ok": True,
        "days": len(rows),
        "first": rows[0].get("as_of"), "last": rows[-1].get("as_of"),
        "size_median": statistics.median(sizes) if sizes else 0,
        "turnover_annual_x": round(_mean(turns) * 244, 1) if turns else 0.0,
        "exit_reasons": reasons,
        "last_members": rows[-1].get("members") or [],
        "note": "换手口径 = 单边（新买入/池大小），与回测 R6 的 14.7x 同源",
    }


def render(pool: Dict[str, Any]) -> str:
    """终端/报告用摘要。"""
    if not pool.get("enabled"):
        return "动态池未启用（%s）" % (pool.get("note") or pool.get("mode"))
    L = ["动态可交易池 %s ｜ 规则 %s ｜ 可执行宇宙 %d → 池内 %d"
         % (pool["as_of"], pool["rule"], pool["universe_executable"],
            len(pool["members"])),
         "  参数：进 %d / 出 %d / 平滑 %d 日 / 相关闸 %.2f / AUM %.2f 亿"
         % (pool["params"]["size"], pool["params"]["n_out"], pool["params"]["smooth"],
            pool["params"]["corr_max"], pool["params"]["aum_yi"]),
         "  %-4s%-9s%-12s%-12s%6s%8s%8s%9s"
         % ("#", "代码", "名称", "状态/评级", "rs20", "conf", "动作", "ADV(亿)")]
    for m in pool["members"]:
        L.append("  %-4d%-9s%-12s%-12s%6.3f%8s%8s%9.2f"
                 % (m["rank"], m["code"], m["name"],
                    "%s/%s" % (m.get("state"), m.get("rating")),
                    m["rs20"], ("%.2f" % m["conf"]) if m["conf"] is not None else "—",
                    m.get("state_action"), (m.get("adv20") or 0) / 1e8))
    if pool.get("exits"):
        L.append("  出池：" + "、".join("%s(%s)" % (e["code"], e["reason"])
                                       for e in pool["exits"]))
    if pool.get("frozen_today"):
        L.append("  熔断：" + "；".join(pool["frozen_today"].values()))
    c = pool.get("capacity") or {}
    if c.get("quote_stale"):
        L.append("  ⚠️ ETF 行情快照已过期 %.0f 小时 → 容量口径降级为 unknown（%d 只），"
                 "仅供参考" % (c.get("quote_age_hours") or 0, c.get("unknown") or 0))
    L.append("  " + pool.get("rationale", ""))
    return "\n".join(L)
