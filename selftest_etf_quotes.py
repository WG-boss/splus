# -*- coding: utf-8 -*-
"""selftest_etf_quotes.py — ETF 行情主源/gtimg 热备的自测（不依赖网络）

覆盖：gtimg 字段解析与单位换算、盘中判定、覆盖度闸门、主源失败自动切换、
命中不足拒绝覆盖、原子写、旧条目不混入。

跑法：python selftest_etf_quotes.py
"""
from __future__ import annotations

import json
import os
import tempfile

import tools_etf_quotes as T

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ✓ %s" % name)
    else:
        _fail += 1
        print("  ✗ %s  %s" % (name, detail))


def line(code="510300", name="沪深300ETF", price="4.139",
         ts="20261109113754", f35="4.139/10925980/889947117",
         f37="88995") -> str:
    f = ["1"] * 88
    f[1], f[2], f[3], f[30], f[35], f[37] = name, code, price, ts, f35, f37
    return 'v_sh%s="%s";' % (code, "~".join(f))


def mk_quotes(n: int = 900):
    codes = ["5%05d" % i for i in range(n)]
    return {c: {"name": "ETF" + c, "market": "sh", "price": 1.0,
                "amount": 1e8} for c in codes}


def meta(n: int, requested: int | None = None):
    return {"requested": requested if requested is not None else n,
            "returned": n, "batches": (n + 59) // 60,
            "batch_failed": 0, "quote_time": "20261109113754"}


def main() -> int:
    print("【T1】parse_gtimg：价格/名称/市场/行情时间")
    q, t = T.parse_gtimg(line() + "\n" + line("159949", "创业板50ETF", "1.113"))
    check("解析 2 只", len(q) == 2, str(list(q)))
    check("价格已是元（不缩放）", abs(q["510300"]["price"] - 4.139) < 1e-9)
    check("成交额取 f35 第三段（元）",
          abs(q["510300"]["amount"] - 889947117.0) < 1e-6,
          str(q["510300"]["amount"]))
    check("market 按代码首位判定",
          q["510300"]["market"] == "sh" and q["159949"]["market"] == "sz")
    check("行情时间戳取最新", t == "20261109113754", t)

    print("\n【T2】单位回落与脏数据")
    f = ["1"] * 88
    f[35] = "bad"
    f[37] = "88995"
    check("f35 不可用 → f37 万元 ×1e4",
          abs(T._gtimg_amount(f) - 889950000.0) < 1e-6)
    f[35], f[37] = "1.0/1/-1", "0"
    check("两者均无效 → None", T._gtimg_amount(f) is None)
    bad = 'v_sh510300="%s";' % "~".join(["x"] * 10)
    q2, _ = T.parse_gtimg(bad + "\n" + line("588000"))
    check("字段不足的行被跳过", list(q2) == ["588000"], str(list(q2)))
    out_range = line("510300", price="999")
    q3, _ = T.parse_gtimg(out_range)
    check("价格越界 → price=None（不猜单位）", q3["510300"]["price"] is None)

    print("\n【T3】覆盖度闸门：不足一律拒绝覆盖")
    ok, why = T.quality_ok(mk_quotes(900))
    check("900 只 / 900 有效 → 通过", ok, why)
    ok2, why2 = T.quality_ok(mk_quotes(100))
    check("100 只 → 拒绝", not ok2 and "拒绝覆盖" in why2, why2)
    q_low = mk_quotes(900)
    for v in list(q_low.values())[499:]:   # 有效报价恰好 499 < 门槛 500
        v["price"] = None
    ok3, why3 = T.quality_ok(q_low)
    check("900 只但有效报价 <500 → 拒绝", not ok3, why3)

    print("\n【T4】盘中判定（半日量）")
    check("11:37 → 盘中", T._intraday("20261109113754") is True)
    check("15:05 → 收盘后", T._intraday("20261109150500") is False)
    check("09:20 → 开盘前", T._intraday("20261109092000") is False)
    check("无时间戳不崩", isinstance(T._intraday(None), bool))

    print("\n【T5】主源失败 → 自动切热备，写明来源与盘中标记")
    tmp = tempfile.mkdtemp(prefix="quotetest_")
    out = os.path.join(tmp, "etf_quotes.json")
    orig = (T.OUT, T.fetch, T.fetch_gtimg, T._existing_codes, T.cross_check)
    T.OUT = out
    T.fetch = lambda: (_ for _ in ()).throw(
        RuntimeError("RemoteDisconnected: Remote end closed"))
    q_good = mk_quotes(900)
    T.fetch_gtimg = lambda codes, batch=60: (q_good, meta(900, 1000))
    T._existing_codes = lambda: ["5%05d" % i for i in range(1000)]
    T.cross_check = lambda q, c: "不应被调用"
    rc = T.refresh()
    doc = json.load(open(out, encoding="utf-8"))
    check("refresh 返回 0", rc == 0)
    check("fallback.used=True", (doc.get("fallback") or {}).get("used") is True)
    check("命中/请求写进 fallback",
          doc["fallback"]["returned"] == 900 and doc["fallback"]["requested"] == 1000)
    check("source 写明腾讯热备", "腾讯 gtimg" in doc["source"])
    check("intraday=True（11:37）", doc["intraday"] is True)
    check("跳过抽样交叉校验", "跳过抽样" in doc["cross_check"])
    check("只写本次命中的 900 只，不混入旧条目", len(doc["quotes"]) == 900)
    check("原子写：无 .tmp 残留", not os.path.exists(out + ".tmp"))

    print("\n【T6】热备命中不足 → 返回 1 且绝不覆盖旧快照")
    sentinel = {"fetched_at": "OLD", "quotes": {"OLD": 1}}
    json.dump(sentinel, open(out, "w", encoding="utf-8"))
    T.fetch_gtimg = lambda codes, batch=60: (mk_quotes(100), meta(100, 1000))
    rc2 = T.refresh()
    doc2 = json.load(open(out, encoding="utf-8"))
    check("refresh 返回 1", rc2 == 1)
    check("旧快照原样保留", doc2 == sentinel, str(doc2)[:80])

    print("\n【T7】主源正常 → 不标热备，走抽样交叉校验")
    T.fetch = lambda: mk_quotes(900)
    T.cross_check = lambda q, c: "mock 交叉校验"
    rc3 = T.refresh()
    doc3 = json.load(open(out, encoding="utf-8"))
    check("refresh 返回 0", rc3 == 0)
    check("fallback.used=False", doc3["fallback"]["used"] is False)
    check("source 为东财主源", "东方财富" in doc3["source"])
    check("执行抽样交叉校验", doc3["cross_check"] == "mock 交叉校验")

    (T.OUT, T.fetch, T.fetch_gtimg,
     T._existing_codes, T.cross_check) = orig
    print("\n" + "=" * 42)
    print("PASS=%d  FAIL=%d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
