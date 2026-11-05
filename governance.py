# -*- coding: utf-8 -*-
"""
governance.py —— 组织与认知层（S+++ 的核心，原交付完全缺失）

实现 splus_config.yaml 中已声明但从未实现的承诺：
  meta_layer.journal.require_premortem : 建仓前强制写死亡条件
  meta_layer.journal.require_redteam   : 强制回答"赚的是谁的钱"
  meta_layer.attribution.buckets       : 四桶归因账本

设计原则：
  - 不产生 alpha，只负责"让不该做的动作做不出来"
  - 无状态、可重算、追加写
  - 拒绝执行时给出明确原因，不静默放行
"""
import datetime
import io
import json
import os
import re

BUCKETS = ["beta", "sector", "stage", "execution"]


# ============================================================
# Premortem —— 事前验尸
# ============================================================

PREMORTEM_TEMPLATE = {
    "thesis": None,           # 一句话论点
    "kill_conditions": [],    # 什么情况下判定自己错了（必须可观测）
    "max_loss_pct": None,     # 可承受最大损失（占该笔仓位）
    "time_stop_days": None,   # 多少交易日内未兑现则退出
    "evidence": [],           # 支持该论点的具体证据（不是观点）
}


def validate_premortem(rec):
    """
    校验事前验尸是否合格。
    不合格则返回错误列表；合格返回空列表。
    关键：kill_conditions 必须是"可观测的价格/事件条件"，不能是"感觉不对"。
    """
    errs = []
    if not isinstance(rec, dict):
        return ["premortem 必须是对象"]

    if not rec.get("thesis"):
        errs.append("缺少 thesis（一句话论点）")

    kc = rec.get("kill_conditions") or []
    if len(kc) < 1:
        errs.append("缺少 kill_conditions（至少 1 条可观测的证伪条件）")

    vague = ("感觉", "情绪", "不对", "不舒服", "再看看", "不确定")
    for i, c in enumerate(kc):
        txt = str(c)
        if len(txt.strip()) < 8:
            errs.append(f"kill_conditions[{i}] 过于模糊（<8 字），不可观测")
        if any(v in txt for v in vague):
            errs.append(f"kill_conditions[{i}] 含主观词，必须改为价格/事件条件")

    ml = rec.get("max_loss_pct")
    if ml is None:
        errs.append("缺少 max_loss_pct（该笔可承受最大损失）")
    elif not (0 < float(ml) <= 100):
        errs.append("max_loss_pct 必须在 (0,100] 区间")

    ts = rec.get("time_stop_days")
    if ts is None:
        errs.append("缺少 time_stop_days（时间止损）")
    elif int(ts) <= 0:
        errs.append("time_stop_days 必须 > 0")

    ev = rec.get("evidence") or []
    if len(ev) < 1:
        errs.append("缺少 evidence（支持论点的具体证据，非观点）")

    return errs


# ============================================================
# Redteam —— "赚的是谁的钱"
# ============================================================

REDTEAM_QUESTIONS = [
    "counterparty",   # 对手方为什么愿意在这个价位卖给我
    "edge_source",    # 我的信息/结构/时滞优势具体是什么
    "crowding",       # 这个交易有多拥挤，别人是否已在此布局
    "fail_mode",      # 最可能让我亏钱的具体路径
]


def validate_redteam(rec):
    """校验红队质询是否真的回答，而不是敷衍。"""
    errs = []
    if not isinstance(rec, dict):
        return ["redteam 必须是对象"]
    for q in REDTEAM_QUESTIONS:
        v = rec.get(q)
        if not v or len(str(v).strip()) < 10:
            errs.append(f"缺少或过短：{q}（至少 10 字）")
    # 对手方回答不允许是"不知道"
    cp = str(rec.get("counterparty") or "")
    if any(t in cp for t in ("不知道", "不清楚", "没想过")):
        errs.append("counterparty 不得回答“不知道”，必须给出具体推测")
    return errs


# ============================================================
# 归因账本 —— 四桶
# ============================================================

def attribute_return(beta_ret, sector_ret, stage_ret, total_ret):
    """
    把组合收益拆到四桶：
      beta      —— 市场整体给的（同期基准收益）
      sector    —— 行业/风格选择贡献
      stage     —— 阶段判断（进出时机）贡献
      execution —— 执行损耗（滑点/追价/迟到）
    残差 = total - (beta+sector+stage)，归入 execution。

    说明：这是"事后拆解"框架，不是归因模型。它只能保证四桶之和等于总收益，
    不能保证每桶的因果归属绝对正确。用途是发现"钱到底从哪来的"。
    """
    total = float(total_ret)
    b = float(beta_ret)
    s = float(sector_ret)
    st = float(stage_ret)
    ex = total - (b + s + st)
    out = {
        "beta": round(b, 6),
        "sector": round(s, 6),
        "stage": round(st, 6),
        "execution": round(ex, 6),
        "total": round(total, 6),
        "sum_check": round(b + s + st + ex, 6),
    }
    out["consistent"] = abs(out["sum_check"] - total) < 1e-6
    out["dominant"] = max(BUCKETS, key=lambda k: abs(out[k]))
    return out


def attribute_health(records):
    """
    体检：连续多期看"钱主要来自哪一桶"。
    若长期 dominant == beta，说明这套系统的 alpha 贡献约等于零。
    """
    if not records:
        return {"status": "无归因记录"}
    counts = {k: 0 for k in BUCKETS}
    for r in records:
        d = r.get("dominant")
        if d in counts:
            counts[d] += 1
    n = len(records)
    shares = {k: round(v / n, 3) for k, v in counts.items()}
    verdict = []
    if shares["beta"] >= 0.6:
        verdict.append("收益主来自 beta → 系统未产生可辨识 alpha")
    if shares["execution"] >= 0.3:
        verdict.append("执行损耗占比偏高 → 检查追价/滑点纪律")
    if shares["stage"] >= 0.4:
        verdict.append("阶段判断贡献显著 → 这是系统的主要价值来源")
    return {"n": n, "shares": shares, "verdict": verdict or ["结构均衡，无明显单一来源"]}


# ============================================================
# 建仓前门禁
# ============================================================

def check_gate(cfg, code, premortem=None, redteam=None):
    """
    建仓前门禁。返回 {"allowed": bool, "errors": [...], "warnings": [...]}
    这是 S+++ 与 S++ 的分界：不是"提示要写"，而是"不写就不许建仓"。
    """
    j = (cfg.get("meta_layer") or {}).get("journal") or {}
    errs, warns = [], []

    if j.get("require_premortem"):
        errs += [f"premortem: {e}" for e in validate_premortem(premortem)]
    if j.get("require_redteam"):
        errs += [f"redteam: {e}" for e in validate_redteam(redteam)]

    return {"allowed": len(errs) == 0, "code": code,
            "errors": errs, "warnings": warns}


def log_gate(ledger_dir, code, gate_result, premortem=None, redteam=None):
    """把门禁结果追加写入治理台账（不可变）。"""
    os.makedirs(ledger_dir, exist_ok=True)
    path = os.path.join(ledger_dir, "governance.jsonl")
    rec = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "allowed": gate_result.get("allowed"),
        "errors": gate_result.get("errors"),
        "premortem": premortem,
        "redteam": redteam,
    }
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def log_attribution(ledger_dir, as_of, attr):
    os.makedirs(ledger_dir, exist_ok=True)
    path = os.path.join(ledger_dir, "attribution.jsonl")
    rec = {"as_of": as_of, **attr}
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


# ============================================================
# 2026-11-05 P0-③：让门禁真正接进决策链（此前 check_gate 只在 selftest 调用）
# ============================================================

def journal_path(ledger_dir, as_of, code):
    """每标的、每日一份事前验尸 / 红队质询记录。"""
    return os.path.join(ledger_dir, "journal", f"{as_of}_{code}.json")


def load_journal(ledger_dir, as_of, code):
    """读不到就返回 (None, None)，由 check_gate 如实拦下，不静默放行。"""
    p = journal_path(ledger_dir, as_of, code)
    if not os.path.exists(p):
        return None, None
    try:
        txt = io.open(p, "r", encoding="utf-8").read()
        # 模板里的尖括号占位未填完 → 视为无效，绝不能让空模板骗过门禁
        if re.search(r"<[^>]{2,}>", txt):
            return None, None
        rec = json.loads(txt)
        return rec.get("premortem"), rec.get("redteam")
    except Exception:
        return None, None


def write_template(ledger_dir, as_of, code, name=""):
    """生成待填写模板（不覆盖已有文件）。返回 (路径, 是否已存在)。"""
    p = journal_path(ledger_dir, as_of, code)
    if os.path.exists(p):
        return p, True
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rec = {
        "as_of": as_of, "code": code, "name": name,
        "premortem": {**PREMORTEM_TEMPLATE,
                      "thesis": "<一句话论点>",
                      "kill_conditions": ["<可观测条件，如：收盘跌破 XXXX 点且放量>"],
                      "max_loss_pct": 8, "time_stop_days": 20,
                      "evidence": ["<具体证据，非观点>"]},
        "redteam": {"counterparty": "<对手方为什么愿意在此价位卖给我>",
                    "edge_source": "<我的信息/结构/时滞优势>",
                    "crowding": "<拥挤度与已有布局>",
                    "fail_mode": "<最可能让我亏钱的具体路径>"},
        "_hint": "填完后删除尖括号占位；validate_ 会拒绝主观措辞与过短回答",
    }
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return p, False


def log_daily(ledger_dir, as_of, summary):
    """
    日级门禁汇总台账（幂等）：同一 as_of 只写一条。
    这样即使 engine 同日重跑 10 次，governance.jsonl 也不会被刷屏。
    """
    path = os.path.join(ledger_dir, "governance.jsonl")
    for r in read_jsonl(path):
        if r.get("type") == "daily" and r.get("as_of") == as_of:
            return {"path": path, "written": False}
    os.makedirs(ledger_dir, exist_ok=True)
    rec = {"type": "daily", "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "as_of": as_of, **summary}
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"path": path, "written": True}
