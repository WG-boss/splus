# R6 宽缓冲池 · 落地执行与遗留处理

**日期**：2026-11-09　**基线**：`docs/POOL_DYNAMIC_20261109.md`（R0–R6 对比）　**commit**：`1ee5a89`（＋第九节的护栏增量）

**一句话**：把 R6（漏斗 + 进 10 / 出 30 + 5 日平滑）从「回测里最好看的那一行」变成**每天真跑、能回滚、有读数纪律**的生产路径；同时把上一轮遗留的 1 个 P0、5 个 P1、3 个 P2 按「能确定性修的修、要换状态机的不偷换」逐项结清，并把「哪些数字不能当证据」写进每一份决策 JSON。

---

## 一、执行了什么（代码级，可逐行复核）

| 文件 | 性质 | 关键动作 |
|---|---|---|
| `pool.py` | **新建** | R6 生产运行时：L0 容量闸 → L1 `rs_20` 5 日均 → L2 缓冲带（进 10/出 30）+ 相关闸 0.90 → L3 状态进风险预算 → L4 熔断/理由码；原子落盘 + 历史一行/日（同日幂等） |
| `state_transition.py` | **新建** | 从 186,778 次转移统计经验矩阵，输出 `self_hold` / `expected_duration_days` / `P(次态)`；分态 persistence 只提供参数，默认不启用 |
| `disclosure.py` | **新建** | 口径披露：配置里的静态边界 + 当日事实（NA 占比、容量 unknown、行情过期、熔断、回测 t 值） |
| `engine.py` | 改 | ① 状态段后接池（必须在桶合并前，池决定谁 tradeable）② 评级出口 `rating=NA` ③ 桶敞口改为唯一算法 `build_bucket_exposure` ④ OHLC 检测补「最长连续段 / 近 120 根」⑤ 转移矩阵注入 ⑥ 披露注入 |
| `eligibility.py` | 改 | ① `permission.add` 成为开仓的**单一布尔** ② 置信门槛入资格层 ③ `investability_status` 区分「没映射 / 没行情」 ④ 可投性窗口支持 AUM 感知 ⑤ `state_fit_gate` 支持 `frozen` / `permission` 两种语义 |
| `decision_layer.py` | 改 | 开仓意向改读 `permission.add`（缺省回落旧判定并标注来源）；映射核实计数改按可交易集合 |
| `splus_config.yaml` | 改 | 新增 `pool` / `disclosure` 段；`state.persistence_mode`、`state.transition_file`；**修正 `eligibility.state_fit.reference` 全 0 的配置覆盖 bug** |
| `run.py` | 改 | 报告新增「读数纪律」「三·补 动态可交易池」；状态表加「评级 / 期望持续」 |
| `tools_pool_apply.py` | **新建** | 运维入口：重建 / `--write` / `--audit` |
| `tools_state_history_backfill.py` | **新建** | 主状态历史回填（面板 → 逐日历史，自动备份，默认最近 750 日） |
| `selftest_pool_runtime.py` | **新建** | 29 项：排序轴与状态无关、缓冲带、容量闸与过期降级、无前视、状态动作、熔断、幂等、dynamic/shadow、桶单一口径 |
| `selftest_disclosure.py` | **新建** | 19 项：披露条目/级别排序、空数据不崩、转移矩阵自检、annotate、persistence 回落 |

验收：`python run_all_checks.py` → **19/19 通过**（新增 2 项检查已并入总入口）。

---

## 二、参数与三档回滚

| 参数 | 取值 | 为什么是这个值 |
|---|---|---|
| `pool.size` / `n_out` / `smooth` | 10 / 30 / 5 | R6 原参数；出池带 30 + 平滑 5 是唯一「换手 26.7x→14.7x 且年化 13.32%→13.87%」的组合 |
| `pool.rank_axis` | `rs_20` | 五态评分作为排序轴的选择质量只有 RS 的 52%（+0.477% vs +0.911%/20 日） |
| `pool.corr_max` | 0.90 | 与池内已有标的 60 日 \|corr\| 超此值不新增（`bucket` 字段是运营分桶，104 只都叫 watch，不能当主题） |
| `pool.amt_floor_yi` | 0.3 | 只剔「根本没量」，不是容量判断 |
| `pool.aum_yi` / `adv_share` | 0.3 / 5% | 单只目标市值 = AUM/10，占 ETF 日额 ≤5% → 需 0.6 亿日额 |
| `pool.freeze_days` / `adv_crash_pct` | 5 / 50% | ADV 腰斩 → 强制出池并冻结 5 个交易日 |
| `eligibility.state_fit_gate` | `permission` | E 态改「禁新增、允许持有」；证据只支持禁新增（剔除 E 的年化反而低 0.8pp） |
| `state.persistence_mode` | `scalar` | 分态粘性会换掉状态机，必须连带重跑全部检验，不能顺手改 |

**回滚三档，全在配置里，不改代码**：

```yaml
pool.mode: fixed        # 立刻回到配置静态 11 只（旧行为）
pool.mode: shadow       # 池照算、只展示，不覆盖 tradeable（推荐切换前先跑 2 周）
eligibility.hard_rules.state_fit_gate: frozen   # 恢复 E 态 = 清仓 的旧语义
```

---

## 三、当日实跑（as_of 2026-11-06）

| 环节 | 结果 |
|---|---|
| 可执行宇宙 U0 | **18** 只（映射 confirmed+verified ∩ 指数 20 日均额 ≥0.3 亿 ∩ ≥252 根 ∩ 非当前 OHLC 代理） |
| 池内 | **9** 只（相关闸压掉 4 个名额；首日无上期池，单边换手 0%） |
| 池成员 | 红利低波100、红利指数、自由现金流、上证国企、创新药、电网设备主题、创新能源、中证1000、科创芯片 |
| 目标仓位 | 4 只 >0（上证国企 6.1%、创新药 6.0%、红利低波 3.6%、自由现金流 3.6%、中证1000 1.2%），**4 只为 0** |
| 总敞口 | 20.59% →（BROAD_E 开关 ×0.5）→ **10.29%** |
| 评级 NA | **57/118（48%）**置信度低于 0.65 |
| 容量口径 | ETF 快照过期 94.6h → 9 只成员容量标 `unknown`，不做硬闸 |
| OHLC 检测 | 当前仅收盘价 **3 只**（光模块CPO / 磁电存储 / 高频PCB）→ 不进池；历史长连续段 **5 只** → 仅披露 |

三条当日就能读出来的事实：

1. **「入池」与「配仓」是两件事**：9 只入池、4 只目标仓位 0。入池看指数层面「买得动 ∩ 相对强」，配仓看的是**可交易 ETF** 的日额 → 资格分。报告已把这两列并排显示，避免读成"池子 9 只所以该买 9 只"。
2. **池子今天很有防御性**：排名靠前的是红利 / 国企 / 自由现金流，而 5 只 E 态里 3 只只能"持有不能加"。这是 E 阶段市场的正常投影，不是模型在推荐红利。
3. **中证1000 第一次进池**：上一轮审计指出它 ETF 日额 37 亿却因 `role=筛选源观察` 永远进不了池——动态池把这类"制度性浪费"直接消掉了。

---

## 四、遗留问题处理状态

| # | 问题 | 原状 | 处理 | 证据 / 状态 |
|---|---|---|---|---|
| P0-1 | `sizing.final` 与桶敞口差 3 倍、`binding` 仍标 state | 桶用 `cap×dd×corr×wmean` 另算，再按比例回同步 | 桶 = 成员 `final` 求和 → `bucket_max` 截断 → haircut → 主题组硬顶；资格/调制乘数进 `decomposition`，`binding` 重新取最小 | 实测 `pharma 0.0604×0.5=0.0302`、`watch 0.1455×0.5=0.0727` 完全对齐；`selftest_pool_runtime` T10 六项断言 ✅ |
| P1-1 | 29.3% 标的日低于门槛仍给字母 | `dominant=argmax` 必出字母 | `state.rating`：`conf<0.65 → NA`，报告/决策卡同步 | 今日 57/118 NA；`rating_reason` 写明门槛 ✅ |
| P1-2 | 资格 FULL 与 `open_allowed=false` 并存 | 两套门槛并列输出 | `eligibility.permission.add` 为唯一布尔，`decision_layer` 直接读它并标注 `permission_source` | 创新药：FULL ×1.0、`add=false`（置信 0.32），不再自相矛盾 ✅ |
| P1-3 | E 态 = FROZEN = 清仓 | 硬规则 `state_fit=0 → FROZEN` | 新增 `state_fit_gate: permission` → 只禁新增、tier 按分照算（×0.5） | 上证国企 6.1%（原 0%）；剔除 E 年化低 0.8pp 为依据 ✅ |
| P1-4 | 分态 persistence（A 0.945 / B 0.619 …） | 标量 0.55 ≡ 0.640 | 参数与 `state_probs` 支持已就位，**默认 scalar** | 换它会换状态机 → 横截面/显著性/R0–R6 全部失效；必须整批重跑，不在本轮偷换 ⚠️ 待决 |
| P1-5 | 转移矩阵未进决策 | 只回算未输出 | `state.transition`：`expected_duration_days` / `most_likely_next` / `p_next` / `age_ratio` / `aged` | 报告「期望持续」列；矩阵 186,778 次转移，A 18.1 日、B 2.6 日 ✅ |
| P1-6 | `state_days` 由 9 行日志回算（8 天） | 报告显示 8，真实 22/85 | 面板回填逐日主状态历史（750 日，自动备份） | 连续段中位 **8 → 22 日**，117 只中 112 只被修正 ✅ |
| P1-7 | OHLC 合成检测漏 5 只长连续段 | 只看全样本 flat 占比 >90% | 三条任一命中：全样本 >90% / 最长连续 ≥60 根 / 近 120 根 ≥90%；历史段单独披露不误杀 | 当前代理 3 只（进池排除）；历史长段 5 只（仅披露） ✅ |
| P1-8 | investability 假零（恒生科技 A 态但 0 分） | `amount` 缺失一律 0，读作"没流动性" | `investability_status = measured / unknown_no_quote / no_mapping`；窗口支持 AUM 感知 | 报告区分「没取到数」与「没流动性」 ✅ |
| P1-9 | 校准台账采集率 7% | 12 日只记 98 条（应 1,416） | **未改** | 根因是 `record_prediction` 只记 tradeable ∩ 非重复；动态池切换后口径会再变一次。建议连同「结算口径」一起改，避免两次迁移 ❌ 待办 |
| P2-1 | 五个门控共用 `dd_pos`/`above_ma200` | 二维表分区，87/118 恰好 3 态 0 分 | **未改** | 换门控特征 = 换状态机，同上，需整批重跑 ❌ 待办 |
| P2-2 | `watch_pool` 只收 A 态、无排序 | 实测负超额 −0.82% | 由动态池替代（`rs_20` 排序 + 容量闸） | `pool.py` 即新入口 ✅ |
| P2-3 | `state_fit` 配置与代码默认值不一致 | 配置 reference 全 0 → 107 只 watch 永久 FROZEN | 配置恢复为 0.5/0.8/0.6/0.2/0.0 并写明理由 | 否则动态池从 watch 选出的名字会被瞬间归零（本轮最隐蔽的联动 bug） ✅ |
| P2-4 | L2 链条降级未落池 | 只有 `state_weight` 压制 | **未改** | 现有 L2 压制已作用于 `state_weight`，再进池会二次计权，需单独设计 ❌ 待办 |

---

## 五、口径披露（与 `splus_config.yaml` 同步，写入每份决策 JSON 的 `disclosures[]`）

**HIGH**

- R0 固定 11 只的 +0.896%/20 日（t 2.04）含事后选样偏差：11 只是 2026 年已知结果后挑的，回测到 2021 等于把答案抄进试卷。
- 全部日频超额 t 在 1.0–1.8，**无一过 2**；R6 的 14.7x 换手与 13.87% 年化方向一致但不显著。
- 今日 57/118 只置信不足 → 评级 NA：只观察/只持有，"不是跌，是模型自己不确定"。
- ETF 快照过期 94.6h → 9 只容量 unknown：不做硬闸 ≠ 真有流动性。

**MED**：映射快照前视（生存偏差方向）；`state_weight` 与 `eligibility.score` 不可比；回测未建模冲击成本/涨跌停/折溢价。

**LOW**：数据滞后截断；不构成投资建议。

---

## 六、未解决的风险（按严重度）

1. **统计功效不足**：60 日样本 power 仅 0.18（0.05 SD）——"不显著"在此样本不构成否定证据，但也不能当肯定证据。R6 的点估计不应写进任何预期收益承诺。
2. **映射快照前视**：用 2026-11 的映射回测历史。修法是 point-in-time 映射台账（每日快照 `etf_map.yaml` 并只读历史版本），属数据工程，不是模型问题。
3. **行情快照**：东财 `RemoteDisconnected`，护栏正确置空价格。恢复后先 `python tools_etf_quotes.py --refresh`，容量闸会自动从 flag 回到硬闸。
4. **校准回路**：7% 采集率 + 0 结算，`min_bucket_n=30` 永远达不到；这是"概率未校准"标签长期挂着的根因。
5. **回填的历史是回放值**：`state_days` 修正后与当年实时报告可能不一致（参数若曾变动），只影响展示量，不参与计算。

## 七、下一步（建议顺序）

1. 先 `pool.mode: shadow` 跑 2 周，比较「影子池」与「静态 11 只」的成员差集与换手，确认无制度性冲突后再切 `dynamic`。
2. 补 point-in-time 映射台账（每日快照），把 MED 级前视降为 LOW。
3. 行情恢复后重跑容量阶梯，确认 0.3 亿 AUM 下仍是 11 只口径。
4. 若要把 `persistence_mode` 切 `per_state`：一次性重跑 `tools_state_crosssec.py`、`tools_state_significance.py`、`tools_pool_dynamic.py`，三份结论一起换，不允许只换一半。

---

## 八、验收命令

```bash
python run_all_checks.py                  # 19/19 通过
python selftest_pool_runtime.py           # 29 项
python selftest_disclosure.py             # 19 项
python state_transition.py                # 打印经验转移矩阵
python tools_pool_apply.py                # 用最新决策重建池（不改权限）
python tools_pool_apply.py --audit        # 池历史审计（换手/出池原因）
python tools_state_history_backfill.py    # 干跑：连续段 8 → 22 日
python tools_state_history_backfill.py --apply   # 恢复运行时数据（自动备份）
```

---

## 九、交付后复盘：运行时数据没跟上代码（2026-11-09 11:0x）

**现象**：`1ee5a89` 提交完成后，工作区被回退到提交前状态——`pool.py`、
`disclosure.py`、`state_transition.py`、两份新文档、两个新自测全部显示为
`D`，`engine.py` / `splus_config.yaml` 等显示为 `M`。此时磁盘上的生产链路
根本跑不起来。回退前的工作区差异已完整备份为
`/tmp/r6_worktree_before_restore_20261109.patch`（15,085 行），`git restore -- .`
后工作区与提交完全一致。

**连带问题（更隐蔽）**：`state/state_dominant_history.jsonl` 被 `.gitignore`
忽略，属运行时数据，**不随 `git restore` 回来**。它退化成最初的 9 行日志，
于是同一份代码、同一个 `as_of 2026-11-06`，两次运行的 `state_days` 从 21
变成 9、`state_start` 从 10-09 变成 10-21。这不是算法错误，是「代码有版本、
数据没版本」造成的伪不幂等；如果没有对比两次落盘结果，它会一直静默。

**处理**：

| 动作 | 结果 |
|---|---|
| `python tools_state_history_backfill.py --apply` | 9 → **750 行**（2014-05-05 → 2026-11-06 面板最近 750 日），连续段中位 **8 → 22 日**，117 只中 112 只被修正；写前自动备份 `.bak.<时间戳>` |
| `disclosure.min_history_rows: 250` | 新增护栏：历史行数低于门槛时，每份决策 JSON 自动挂一条 **HIGH** 披露 `d_state_hist`，写明行数、覆盖区间与回填命令；正常 750 行时不产生噪音。参数外置，设 0 即关闭 |
| `selftest_disclosure.py` T9 | 4 项断言：9 行→HIGH、写明回填命令、300 行→不打扰、文件缺失→也提示；另验未配门槛时向后兼容 |

**幂等验证**：连续两次 `python run.py`，逐只比对 `state_start` / `state_days`
与 `total_exposure`，结果完全一致（仅 `generated_at` 时间戳变化）。

**仍待办（部署纪律）**：把「首次部署 / 换机 / 代码回滚后必须重跑
`tools_state_history_backfill.py --apply`」写进调度检查清单，或把逐日历史
纳入 point-in-time 台账（与 §六-2 的映射台账一并做）。在此之前，护栏
`d_state_hist` 是唯一兜底。

