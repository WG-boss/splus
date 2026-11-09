# -*- coding: utf-8 -*-
"""tools_pool_apply.py — 动态可交易池的运维入口（重建 / 影子 / 审计）

生产路径由 engine.run 自动调用 pool.build_and_apply；本工具用于：
  * 盘后核对：今天的池是不是按 R6 规则算出来的（--shadow 不改任何权限）
  * 起步/切换：先 --shadow 看一周，再改 config `pool.mode: dynamic`
  * 月度审计：成员数、单边换手、出池原因分布（--audit）

用法：
    python tools_pool_apply.py             # 用最新决策 JSON 重建池并打印（不改 tradeable）
    python tools_pool_apply.py --write     # 同上，并写 state/tradeable_pool.json
    python tools_pool_apply.py --audit     # 读池历史做审计
    python tools_pool_apply.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import yaml  # noqa: E402


def _latest_decision() -> str:
    dp = os.path.join(BASE, "data")
    fs = sorted(f for f in os.listdir(dp)
                if f.startswith("splus_decision_") and f.endswith(".json"))
    return os.path.join(dp, fs[-1]) if fs else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入池文件（仍不改 tradeable）")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    with open(os.path.join(BASE, "splus_config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    import engine
    import pool as pool_mod

    if a.audit:
        res = pool_mod.audit(BASE, cfg)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    dp = _latest_decision()
    if not dp:
        print("缺决策 JSON，先跑 python run.py", file=sys.stderr)
        return 2
    with open(dp, encoding="utf-8") as fh:
        dec = json.load(fh)
    per_index = {}
    for c, v in (dec.get("per_index") or {}).items():
        per_index[c] = {"name": v.get("name"), "tradeable": bool(v.get("tradeable")),
                        "state": {"dominant": (v.get("state") or {}).get("dominant"),
                                  "confidence": (v.get("state") or {}).get("confidence")},
                        "instrument": v.get("instrument")}
    cache = os.path.join(BASE, cfg["data"]["cache_dir"])
    loaded = {}
    for c in per_index:
        bars = engine.load_bars(cache, c)
        # 直接用生产同一套体检，避免"从决策 JSON 反解 warning"导致的漏判
        soft = [q.split("|", 1)[1] for q in engine.quality_check(bars)
                if q.startswith("SOFT|")]
        loaded[c] = {"bars": bars, "quality_warnings": soft}
    prev = pool_mod.load(BASE, cfg)
    p = pool_mod.build(cfg, per_index, loaded, dec["as_of"], BASE, prev)
    p["mode"] = "shadow"
    print(pool_mod.render(p))
    n_exit = len(p.get("exits") or [])
    print("  出池 %d 只；可执行宇宙拒因：%s"
          % (n_exit, json.dumps(_count(p.get("rejected") or []), ensure_ascii=False)))
    if a.write:
        path = pool_mod.save(BASE, cfg, p)
        print("→ %s" % path)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False, indent=1)
        print("→ %s" % a.json)
    return 0


def _count(rows):
    out = {}
    for r in rows:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
