# -*- coding: utf-8 -*-
"""
selftest_market_context.py —— O2 市场否决即时态选项（use_ideal_state）

覆盖：
  1. 默认 use_ideal_state=false → 读滞回确认态 reg["state"]
  2. use_ideal_state=true  → 读 B+ 暴露的即时态 reg["state_ideal"]
  3. state≠增量 但 state_ideal=增量 时，true 不否决 / false 否决
  4. 旧库无 state_ideal 列（B+ 之前）→ 不崩，回退 state
  5. 增量+增量+闸门开 → 默认不否决
"""
import io
import os
import sqlite3
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market_context as mc


def build_db(path, state, state_ideal, trade_date, with_ideal=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    if with_ideal:
        con.execute("CREATE TABLE market_regime "
                    "(trade_date TEXT, state TEXT, state_ideal TEXT, score REAL)")
        con.execute("INSERT INTO market_regime VALUES (?,?,?,?)",
                    (trade_date, state, state_ideal, 70.0))
    else:
        con.execute("CREATE TABLE market_regime "
                    "(trade_date TEXT, state TEXT, score REAL)")
        con.execute("INSERT INTO market_regime VALUES (?,?,?)",
                    (trade_date, state, 70.0))
    con.commit()
    con.close()


def build_gate(path, as_of):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump({"status": "OK", "allow": True, "as_of": as_of}, f)


def run(state, state_ideal, use_ideal, with_ideal=True):
    d = tempfile.mkdtemp(prefix="mcselftest_")
    db = os.path.join(d, "data", "stock_pool.db")
    gp = os.path.join(d, "data", "market_gate_state.json")
    build_db(db, state, state_ideal, "2026-11-05", with_ideal)
    build_gate(gp, "2026-11-05")
    cfg = {"market_context": {
        "enabled": True, "stock_pool_dir": ".",
        "db_file": "data/stock_pool.db",
        "gate_state_file": "data/market_gate_state.json",
        "require_state": "增量", "require_gate_open": True,
        "max_lag_days": 10, "on_error": "veto",
        "use_ideal_state": use_ideal,
    }}
    return mc.load(d, cfg, "2026-11-05")


fails = []
total = 0


def check(name, cond):
    global total
    total += 1
    mark = "PASS" if cond else "FAIL"
    print("%s  %s" % (mark, name))
    if not cond:
        fails.append(name)


# 1. 默认读滞回确认态 state
r = run("减量", "增量", False)
check("默认 use_ideal=false → effective_state=state(减量)", r.get("effective_state") == "减量")
check("默认：state≠增量 → 否决", r.get("veto") is True)
check("默认：use_ideal_state 字段 false", r.get("use_ideal_state") is False)

# 2. use_ideal=true 读即时态
r = run("减量", "增量", True)
check("use_ideal=true → effective_state=state_ideal(增量)", r.get("effective_state") == "增量")
check("use_ideal=true：state_ideal=增量 → 不否决", r.get("veto") is False)
check("use_ideal=true 字段 true", r.get("use_ideal_state") is True)

# 3. 反向：state=增量 但 state_ideal=减量
r = run("增量", "减量", True)
check("use_ideal=true → effective_state=state_ideal(减量)", r.get("effective_state") == "减量")
check("use_ideal=true：state_ideal≠增量 → 否决", r.get("veto") is True)

# 4. 旧库无 state_ideal 列 → 不崩，回退 state
r = run("增量", "增量", True, with_ideal=False)
check("旧库无 state_ideal 列 → 不崩", r.get("regime", {}).get("ok") is True)
check("旧库无 state_ideal：use_ideal=true 回退 state(增量) → 不否决",
      r.get("veto") is False and r.get("effective_state") == "增量")
check("旧库无 state_ideal：regime.state_ideal 为 None（优雅降级）",
      (r.get("regime") or {}).get("state_ideal") is None)

# 5. 市场为增量且闸门开 → 默认不否决
r = run("增量", "增量", False)
check("增量+增量+闸门开 → 不否决", r.get("veto") is False)

print("\n%d/%d PASS" % (total - len(fails), total))
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
