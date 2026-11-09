"""影子观察台账自测（纯 stdlib + tempfile，不碰生产 state/）。"""
from __future__ import annotations

import copy
import json
import os
import tempfile

import shadow_obs as SO

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ✓ " + name)
    else:
        _fail += 1
        print("  ✗ " + name + ("  | " + str(detail)[:220] if detail else ""))


def mk_cfg(mode: str = "shadow", **over) -> dict:
    sh = {"ledger": "obs.jsonl", "start": "2026-11-09", "end": "2026-11-23",
          "review_on": "2026-11-23", "min_sessions": 3}
    sh.update(over)
    return {"pool": {"mode": mode, "shadow": sh},
            "universe": [
                {"code": "b0", "tradeable": True},
                {"code": "b1", "tradeable": True},
                {"code": "b2", "tradeable": False},
            ]}


def mk_result(as_of: str = "2026-11-09", members: int = 2,
              intraday: bool = False, stale: bool = False,
              err: str = "", enabled: bool = True,
              frozen: bool = False, unknown: bool = False,
              turnover: float = 0.05, generated_at: str = "") -> dict:
    ms, per = [], {}
    for i in range(members):
        code = "s%d" % i
        ms.append({"code": code, "name": "影子%d" % i, "rank": i + 1,
                   "rs20": 0.03, "state": "C", "rating": "C",
                   "state_action": "normal",
                   "capacity": {"status": "unknown" if (unknown and i == 0) else "measured"}})
        per[code] = {
            "instrument": {"status": "confirmed", "verified": True},
            "eligibility": {"tier": "FROZEN" if (frozen and i == 0) else "FULL"},
            "sizing": {"final": 0.0, "decomposition": {}},
            "state": {"rating": "C"},
            "tradeable": False,
            "pool": {"member": True, "applied": False, "weight_mult": None},
        }
    # 现行静态白名单成员 b0/b1
    for c in ("b0", "b1"):
        per[c] = {"instrument": {"status": "confirmed", "verified": True},
                  "eligibility": {"tier": "FULL"}, "sizing": {"final": 0.02},
                  "state": {"rating": "A"}, "tradeable": True}
    pool = {"enabled": enabled, "mode": "shadow", "rule": "R6-wide-buffer",
            "universe_executable": 18, "members": ms,
            "metrics": {"turnover_one_way": turnover},
            "exits": [], "frozen_today": {},
            "capacity": {"quote_intraday": intraday, "quote_stale": stale}}
    if intraday:
        pool["intraday_hold"] = True
    if err:
        pool["error"] = err
    return {"as_of": as_of,
            "generated_at": generated_at or (as_of + "T15:10:00"), "pool": pool,
            "per_index": per, "total_exposure": 0.085,
            "disclosures": [{"id": "d_na", "level": "HIGH"}], "data_gaps": []}


tmp = tempfile.mkdtemp(prefix="shadow_obs_")
CFG = mk_cfg()

print("\n【T1】配置与模式门禁")
c = SO.cfg_of(CFG)
check("默认阈值可读且被配置覆盖", c["min_sessions"] == 3 and c["ledger"] == "obs.jsonl")
check("未配置的判据用 DEFAULTS", c["min_measured_ratio"] == 0.90
      and c["max_conflict_sessions"] == 0)
check("mode=dynamic → 不记录", SO.record(tmp, mk_cfg("dynamic"), mk_result()) is None)
check("mode=fixed → 不记录", SO.record(tmp, mk_cfg("fixed"), mk_result()) is None)
check("shadow.enabled=False → 不记录",
      SO.record(tmp, mk_cfg(enabled=False), mk_result()) is None)
check("台账文件尚未创建（未误写）", not os.path.exists(os.path.join(tmp, "obs.jsonl")))

print("\n【T2】逐日记录、幂等、损坏行")
r1 = SO.record(tmp, CFG, mk_result("2026-11-09"))
check("首日写入成功", r1 is not None and r1["as_of"] == "2026-11-09")
SO.record(tmp, CFG, mk_result("2026-11-09", members=4))
SO.record(tmp, CFG, mk_result("2026-11-10"))
rows = SO._read(os.path.join(tmp, "obs.jsonl"))
check("同 as_of 幂等替换（不重复）", len(rows) == 2, rows)
check("替换的是最新内容（members=4）",
      next(r for r in rows if r["as_of"] == "2026-11-09")["shadow_pool"]["n"] == 4)
check("按 as_of 排序", [r["as_of"] for r in rows] == ["2026-11-09", "2026-11-10"])
p = os.path.join(tmp, "obs.jsonl")
with open(p, "a", encoding="utf-8") as fh:
    fh.write("{{{broken json\n")
check("损坏行跳过不崩", len(SO._read(p)) == 2)

print("\n【T3】对照集与检查量")
row = next(r for r in SO._read(p) if r["as_of"] == "2026-11-10")
check("基线来自 config.universe[tradeable]，不是 per_index 全量",
      row["baseline"]["codes"] == ["b0", "b1"])
check("only_shadow = s0/s1，only_baseline = b0/b1",
      row["diff"]["only_shadow"] == ["s0", "s1"]
      and row["diff"]["only_baseline"] == ["b0", "b1"])
check("完全不重合 → Jaccard=0", row["diff"]["jaccard"] == 0.0)
check("measured/mapped 比例均为 1", row["checks"]["measured_ratio"] == 1.0
      and row["checks"]["mapped_ratio"] == 1.0)
check("现行基线仓位被记录（影子成员为 0）",
      row["shadow_pool"]["members"][0]["sizing_final_baseline"] == 0.0)
check("HIGH 披露 id 与数据缺口进台账",
      row["portfolio"]["high_disclosures"] == ["d_na"])
res_mix = mk_result("2026-11-11", members=3)
res_mix["pool"]["members"][0]["capacity"]["status"] = "unknown"
res_mix["per_index"]["s0"]["eligibility"]["tier"] = "FROZEN"
res_mix["per_index"]["s1"]["instrument"]["verified"] = False
rm = SO.record(tmp, CFG, res_mix)
check("unknown 容量 / FROZEN / 未核实映射分别计数",
      rm["checks"]["capacity_unknown_n"] == 1
      and rm["checks"]["eligibility_frozen_n"] == 1
      and rm["checks"]["mapped_ratio"] == round(2 / 3, 4), rm["checks"])
check("存在任一问题 → conflict=True", rm["checks"]["conflict"] is True)

print("\n【T4】有效交易日：盘中不算观察日")
ri = SO.record(tmp, CFG, mk_result("2026-11-12", intraday=True))
check("intraday_hold → valid_session=False",
      ri["valid_session"] is False and "intraday_hold" in ri["invalid_reason"])
re_ = SO.record(tmp, CFG, mk_result("2026-11-13", err="boom"))
check("pool_error → 无效日", re_["invalid_reason"] == "pool_error")
rs = SO.record(tmp, CFG, mk_result("2026-11-16", stale=True))
check("quote_stale → 无效日", "quote_stale" in rs["invalid_reason"])
rd = SO.record(tmp, CFG, mk_result("2026-11-17", enabled=False))
check("pool_disabled → 无效日", "pool_disabled" in rd["invalid_reason"])

print("\n【T5】窗口过滤与判据")
SO.record(tmp, CFG, mk_result("2026-11-24"))     # end=11-23 之外，应被过滤
s = SO.summarize(tmp, CFG, today="2026-11-20")
check("窗口外日期被过滤（台账 8 行 → 窗口内 7 行）",
      len(SO._read(p)) == 8 and s["sessions_total"] == 7, s["sessions_total"])
check("有效日不计盘中/异常", s["sessions_valid"] == 3, s["sessions_valid"])
check("无效明细写明原因", len(s["invalid_detail"]) == 4, s["invalid_detail"])
check("INCOMPLETE 也给出距复核天数", s["window"]["days_to_review"] == 3)
s4 = SO.summarize(tmp, mk_cfg(min_sessions=4), today="2026-11-20")
check("观察日不足 → INCOMPLETE（不得提前下结论）",
      s4["decision"] == "INCOMPLETE", s4["reasons"])
check("INCOMPLETE 时判据失败降级为早期预警",
      any("早期预警" in x for x in s4["reasons"]), s4["reasons"])
check("INCOMPLETE 仍保留失败判据供追踪",
      "c_invalid" in s4["failed_criteria"], s4["failed_criteria"])
ids = [x["id"] for x in s["criteria"]]
check("判据共 6 条且 id 固定", ids == ["c_sessions", "c_invalid", "c_measured",
                                    "c_mapped", "c_turnover", "c_conflict"], ids)
check("收益不在任何判据里", not any("return" in x["id"] or "alpha" in x["id"]
                                or "excess" in x["id"] for x in s["criteria"]))
check("return_note 明确写明不做收益判定", "不作为 go/no-go 判据" in s["return_note"])

print("\n【T6】GO / NO_GO 与阈值外置")
tmp2 = tempfile.mkdtemp(prefix="shadow_obs_go_")
for d in ("2026-11-09", "2026-11-10", "2026-11-11"):
    SO.record(tmp2, CFG, mk_result(d, turnover=0.05))
g = SO.summarize(tmp2, CFG, today="2026-11-23")
check("达标 → GO", g["decision"] == "GO", (g["decision"], g["reasons"]))
check("GO 时全部判据 pass", all(x["pass"] for x in g["criteria"]))
tmp3 = tempfile.mkdtemp(prefix="shadow_obs_nogo_")
for d in ("2026-11-09", "2026-11-10", "2026-11-11"):
    SO.record(tmp3, CFG, mk_result(d, turnover=0.05, frozen=True))
n = SO.summarize(tmp3, CFG, today="2026-11-23")
check("资格 FROZEN 冲突 → NO_GO", n["decision"] == "NO_GO", n["reasons"])
check("NO_GO 点名失败判据", "c_conflict" in n["failed_criteria"], n["failed_criteria"])
cfg_loose = mk_cfg(max_avg_turnover=0.001)
s_loose = SO.summarize(tmp2, cfg_loose, today="2026-11-23")
check("阈值外置生效（换手门槛收紧 → NO_GO）", s_loose["decision"] == "NO_GO")
check("失败判据是 c_turnover", "c_turnover" in s_loose["failed_criteria"])
cfg_wide = mk_cfg(start="2026-12-01")
check("窗口外置生效（改 start → 0 行）",
      SO.summarize(tmp2, cfg_wide, today="2026-12-05")["sessions_total"] == 0)

print("\n【T7】零干预端到端核对")
v_ok = SO.verify_zero_intervention(mk_result())
check("干净 shadow 决策 → ok", v_ok["ok"] is True, v_ok)
v_bad = copy.deepcopy(mk_result())
v_bad["per_index"]["s0"]["pool"]["applied"] = True
v_bad["per_index"]["s0"]["pool"]["weight_mult"] = 0.5
v_bad["per_index"]["s0"]["sizing"]["decomposition"]["pool_state"] = 0.5
v2 = SO.verify_zero_intervention(v_bad)
check("applied=True / weight_mult / pool_state 三类污染全部抓到",
      not v2["ok"] and v2["bad_applied"] == ["s0"]
      and v2["bad_weight_mult"] == ["s0"] and v2["bad_pool_state"] == ["s0"], v2)

print("\n【T8】Markdown 汇总")
md = SO.render(g)
check("含窗口、结论、判据表", all(k in md for k in
                            ("影子观察期汇总", "结论：`GO`", "预先写死的判据", "c_sessions")))
check("含收益不作判据的声明", "不作为 go/no-go 判据" in md)
check("含人工复核免责", "不构成投资建议" in md)

print("\n【T9】运行日 ≠ 数据日（观察期第一天不能被窗口吃掉）")
tmp4 = tempfile.mkdtemp(prefix="shadow_obs_lag_")
# 11-09 12:40 盘中跑，数据 as_of 仍是 11-06
r_lag = SO.record(tmp4, CFG, mk_result("2026-11-06", intraday=True,
                                       generated_at="2026-11-09T12:40:00"))
check("session_date 取运行日 11-09，as_of 保留 11-06",
      r_lag["session_date"] == "2026-11-09" and r_lag["as_of"] == "2026-11-06")
check("盘中预览被标记无效", r_lag["valid_session"] is False)
s_lag = SO.summarize(tmp4, CFG, today="2026-11-09")
check("该记录落在观察窗口 11-09→11-23 内",
      s_lag["sessions_total"] == 1, s_lag["sessions_total"])
# 同日 15:10 收盘后重跑，数据日仍是 11-06 → 覆盖盘中那条
SO.record(tmp4, CFG, mk_result("2026-11-06",
                               generated_at="2026-11-09T15:10:00"))
rows4 = SO._read(os.path.join(tmp4, "obs.jsonl"))
check("同运行日重复运行 → 幂等覆盖（不留两行）", len(rows4) == 1, rows4)
check("收盘记录覆盖盘中无效标记", rows4[0]["valid_session"] is True)
check("数据日仍可追溯到 11-06", rows4[0]["as_of"] == "2026-11-06")

print("\n" + "=" * 42)
print("PASS=%d  FAIL=%d" % (_ok, _fail))
raise SystemExit(1 if _fail else 0)
