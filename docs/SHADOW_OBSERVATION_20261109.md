# 影子观察期执行方案 · R6 宽缓冲动态可交易池

**日期**：2026-11-09　**窗口**：2026-11-09 → 2026-11-23（复核日 2026-11-23）
**配置**：`splus_config.yaml` → `pool.mode: shadow`、`pool.shadow.*`
**代码**：`shadow_obs.py`、`tools_shadow_observe.py`、`selftest_shadow_obs.py`
**前置证据**：`docs/POOL_DYNAMIC_20261109.md`（为什么是 R6）、`docs/R6_EXECUTION_20261109.md`（R6 已落地）

---

## 一、结论先行

`pool.mode` 已由 `dynamic` 切为 **`shadow`，观察两周**。池每天照常计算、照常进缓冲带、
照常记台账，但**绝不改变任何权限、仓位或敞口**；实际执行链路与 `pool.mode: fixed`
逐字段完全一致。两周后按**观察开始前写死**的 6 条运营判据出 go/no-go 报告，
`GO` 也不自动切换，仍需人工确认。

**收益不作为判据。** 两周约 10 个交易日，任何日频超额 t 都不可能显著；
`docs/POOL_DYNAMIC_20261109.md` 里 R0–R6 两次独立面板重跑名次互换，
R6 自身的日频超额 t 只有 1.77。用两周收益决定切换，等于把噪声当证据。
本观察只回答一个问题：**这个动态池能不能稳定、可执行、无制度冲突地运营下去。**

---

## 二、为什么必须先修 `shadow` 语义（本轮发现的真 bug）

旧实现里 `pool.apply()` 在 shadow 下仍写入 `pool.weight_mult`，而 `engine.py`
的仓位循环无条件读它做风险预算（D 态 ×0.5）。也就是说"只算不用"的影子池
**一直在改实际仓位**——观察组污染对照组，两周后拿不到干净 A/B。

修法（两道保险，缺一不可）：

| 层 | 改动 |
|---|---|
| `pool.apply()` | shadow 下 `weight_mult=None`、`applied=False`、`mode="shadow"`；dynamic 下 `applied=True` 且保留乘数 |
| `engine.apply_pool_risk_budget()` | 新抽出的独立函数，**只认 `applied=True`**；否则直接 return |

**端到端实测**（本轮两次 `run.py`，同一份数据、只切 mode）：

| 字段 | fixed vs shadow |
|---|---|
| `tradeable`（118 只） | 逐只一致，diff=0 |
| `sizing.final`（118 只） | 逐只一致，diff=0 |
| `sizing.decomposition` | 逐只一致，`pool_state` 乘数 0 处 |
| `eligibility.tier` / `open_allowed` | 逐只一致，diff=0 |
| `state.state_days` | 逐只一致，diff=0 |
| `total_exposure` / `buckets.exposure` | 0.0383 / 逐桶一致 |
| `kill_switches` | `BROAD_E`, `UPSTREAM_WEAK` 一致 |

`python tools_shadow_observe.py --verify` 可随时复验：检查 `applied`、
`weight_mult`、`pool_state` 三类污染，返回 `ok=true` 才算观察有效。

---

## 三、观察台账与两个日期口径

产物 `state/shadow_observation.jsonl`（gitignore，每日重建），**同运行日幂等替换**。

必须区分两个日期，否则观察期第一天就会凭空消失：

- **`as_of`（数据日）**：今天盘中跑仍是 `2026-11-06`（最新交易日 K 线）。
- **`session_date`（运行日）**：`generated_at` 的日期，今天 `2026-11-09`。

窗口过滤、幂等键、有效交易日计数**全部用 `session_date`**；`as_of` 只做追溯。
理由：①按 `as_of` 过滤会把 11-09 的运行挡在 `start=11-09` 之外；
②数据延迟时连续两天 `as_of` 相同，按它覆盖会少计一个观察日；
③盘中预览与收盘正式运行同一天，收盘那条覆盖盘中那条，正是想要的语义。

**盘中不算观察日**：`intraday_hold` / `pool_error` / `quote_stale` /
`pool_disabled` 任一命中 → `valid_session=False`，写清 `invalid_reason`，
不计入 9 个有效交易日。这是上一轮「半日额驱动建池凭空产生 16.7% 换手」
事故的制度化防线。

每日一行记录：影子池成员（含容量状态、映射状态、资格档、基线仓位）、
现行静态白名单、两者 `only_shadow / only_baseline / overlap / Jaccard`、
换手、总敞口、NA 数、HIGH 披露、数据缺口、冲突标记。

---

## 四、预先写死的判据（观察开始前设定，事后不得调阈值）

全部外置在 `pool.shadow.*`，代码默认值见 `shadow_obs.DEFAULTS`。

| ID | 判据 | 阈值 | 为什么是这个值 |
|---|---|---|---|
| `c_sessions` | 有效观察交易日 ≥ | **9** | 两周约 11 个交易日，允许 2 天缺数据 |
| `c_invalid` | 无效/异常日 ≤ | **1** | 盘中纪律或建池故障若反复出现，说明链路不稳定 |
| `c_measured` | 容量 `measured` 比例（均值）≥ | **0.90** | 半日量/隔夜额不能当全天 ADV 建池 |
| `c_mapped` | `confirmed ∧ verified` 比例 ≥ | **1.00** | 池里出现没核实 ETF 的标的 = 无法下单 |
| `c_turnover` | 影子池日均单边换手 ≤ | **0.20** | R6 回测年化 14.7x ≈ 6%/日，留 3 倍余量 |
| `c_conflict` | 制度冲突日 ≤ | **0** | 资格 FROZEN / 容量 unknown / 建池异常，出现即需查 |

判定三态：

- **`INCOMPLETE`**：有效日 < 9 → **一律不下结论**；判据失败只降级为「早期预警」。
  （这是本轮自测抓出来的设计缺陷：观察期第 3 天异常日多就判 `NO_GO`
  = 用不完整样本提前定案，违反两周约定。）
- **`GO`**：满 9 天且 6 条全过 → 可提交人工复核后切 `dynamic`。
- **`NO_GO`**：满 9 天但任一判据失败 → 点名失败项，延长观察或先修链路。

`python tools_shadow_observe.py --report` 出完整判据表；`--json` 给机器读。

---

## 五、首日读数（2026-11-09 12:50，盘中预览）

| 指标 | 值 |
|---|---|
| 台账 | 1 行，`valid_session=False`（`intraday_hold`），有效 0/9 |
| 影子池 / 现行白名单 | 9 只 / 11 只 |
| 重合 / Jaccard | **1 只 / 0.0526** |
| 实际 `tradeable` | **11 只**（静态白名单，零干预） |
| 总敞口 | **3.83%**（基线口径；dynamic 时是 8.50%） |
| 评级 NA | 57/118 |
| 口径披露 | 11 条，含新增 `d_shadow` |

**Jaccard 0.0526 本身就是必须先观察的理由**：从静态 11 只切到动态 9 只，
等于换掉约 90% 的持仓。这不是参数微调，是一次组合替换，制度冲击与
执行成本都必须先看清。注意首日池成员的容量状态全是 `stale`（盘中半日额），
因此首日的 `measured_ratio=0`、`conflict=true` **不构成判据证据**——
该行已被标为无效观察日，不进统计。

---

## 六、每日运行链路（观察期能否采到样本的关键）

现有调度（见 `PROJECT_MEMORY.md` / WorkBuddy 自动化）：

- 15:40 `S++指数投研日报（收盘后）`（OpenClaw 侧 jobId `203cac4f…`）
- 17:00 `splus每日宇宙同步+东财补尾+投研日报（兜底）`（跑 `run.py`）

**但这两个任务都不刷新 `data/etf_quotes.json`。** 若不补上，快照会一直停在
2026-11-09 11:42 的盘中半日额（`intraday=True`），此后每天 17:00 都走
`intraday_hold` → **两周全是无效观察日，观察期空转**。

因此观察期内追加一个**限期**自动化（周一至周五 16:50，`validUntil=2026-11-23`）：
只做 `python tools_etf_quotes.py --refresh` + `--check`，校验
`intraday=False` 与命中数，**不跑 `run.py`、不改配置、不改台账**。
17:00 的既有任务负责生成日报并写入当日观察行。职责单一、不并发、可随时停用。

> 若 `--check` 仍显示 `intraday=True`，说明行情源时间戳没过 15:00
> （数据源未收盘更新），须延后刷新；此时 17:00 的日报会被正确标为无效日，
> 不要手工改台账把它改成有效。

**收盘后手工补跑的标准动作**：
```bash
python tools_etf_quotes.py --refresh && python tools_etf_quotes.py --check
python run.py
python tools_shadow_observe.py          # 进度
python tools_shadow_observe.py --verify # 零干预
```

---

## 七、两周后复核（2026-11-23）

一次性复核自动化会执行 `tools_shadow_observe.py --report`，输出：
有效交易日、六条判据表、异常与冲突清单、`GO/NO_GO/INCOMPLETE`。

| 结论 | 动作 |
|---|---|
| `GO` | **人工确认后**把 `pool.mode` 改 `dynamic`，跑 `run_all_checks.py` 全量，commit/push |
| `NO_GO` | 按失败判据修链路或延长观察；**不得**为了过判据改阈值 |
| `INCOMPLETE` | 有效日不足 → 延长窗口，说明缺哪几天、为什么 |

**任何情况下都不自动切换 mode。** 观察期的意义就是让「切不切」成为一个
有证据、有人签字、可回滚的决策，而不是脚本自己往前走。

---

## 八、回滚与 trade-off

| 操作 | 一行配置 | 影响 |
|---|---|---|
| 回到静态 11 只 | `pool.mode: fixed` | 池完全不计算，台账停止追加 |
| 提前启用动态池 | `pool.mode: dynamic` | 池即权限，需重跑全量验收；**不推荐，等同跳过观察** |
| 关闭台账 | `pool.shadow.enabled: false` | 池仍零干预，但不留观察证据 |

| 取舍 | 选了什么 | 代价 |
|---|---|---|
| 观察 2 周 vs 直接上线 | 观察 | 晚两周用上 R6；换来制度冲突与执行链路的实证 |
| 收益不判 vs 收益判 | 不判 | 无法回答「动态池是否更赚钱」；但 10 天样本本来也回答不了 |
| 盘中不算观察日 vs 都算 | 不算 | 少 1–2 个样本；避免半日额污染建池与换手 |
| 新建限期刷新任务 vs 改既有 17:00 任务 | 新建 | 多一个自动化；不动用户既有调度，观察期结束自动停 |

---

## 九、验收

- `run_all_checks.py` **21/21**（新增「影子观察台账 + go/no-go 判据」）
- `selftest_shadow_obs.py` **51 项**（含运行日≠数据日、盘中无效、INCOMPLETE 优先级、
  判据外置、零干预端到端、Markdown 汇总）
- `selftest_pool_runtime.py` **48 项**（新增 shadow 零干预 5 项）
- `lint_config.py` 通过；`tools_shadow_observe.py --verify` → `ok=true`
- fixed vs shadow 逐字段对照实验 **10/10 一致**

> 本文所有数字来自历史回放与当日模型输出，不构成投资建议；
> 仓位为模型建议比例，不是下单指令。
