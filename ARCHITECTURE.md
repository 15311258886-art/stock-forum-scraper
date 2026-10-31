# 系统分层架构与准入规则（B 阶段，2026-10-26）

> 背景：进哥指出系统「功能叠加后架构错乱、观点相互悖论、升级无章法」。
> 本文件把系统的分层职责、唯一准入入口、信号消费规则钉死，作为「以后加功能先对照」的架构基线。
> 配合 `DECISIONS.md`（决策记录）与 `CAPABILITIES.md`（能力清单）三件套使用。

---

## 一、四层单一职责

任何新功能先定位它属于哪一层，再落地。四层之间只允许「下层 → 上层」单向依赖。

| 层 | 职责 | 关键模块 | 数据出入口 | 运行环境 |
|----|------|----------|------------|----------|
| **采集层** | 出网抓取原始数据、落原始库 | `tools/fetch_*.py`、`fetch_a_shares_snapshot`、`fetch_plate_members`、`fetch_daily_kline` | 入：东财/抖音等外部源；出：`stock_names`/`stock_daily_snapshot`/`plate_members`/`kline_daily.db` | **需本机出网**（沙箱不可行） |
| **因子层** | 本地纯算技术指标/质量因子 | `viewer/indicators.py`（P1） | 入：`kline_daily.db`/`stock_names`；出：`stock_indicators`/`stock_signals`/`stock_indicators_daily` | 本地纯算（无网可行） |
| **评估层** | 在干净宇宙上做扫描/回测/选股 | `universe_scan`(P2)、`signal_backtest`(P4)、`strategy_templates`(P5)、`universe_filter` | 入：`stock_indicators` 等；出：`stock_universe_scan`/`signal_backtest`/`stock_strategy_picks` | 本地纯算 |
| **展示层** | 只读 API + 前端可视化 | `viewer/server.py` + 前端 | 入：上述所有表；出：JSON API | 本地 |

> 铁律：**展示层只读，绝不写业务数据**；写库只发生在采集层（出网）与因子/评估层（本地计算）。新增模块若要写库，必须先声明属于哪层。

---

## 二、干净宇宙 = 唯一选股/评估准入入口（核心规则）

所有选股、评估、扫描、策略产出，**必须从 `universe_filter.evaluation_universe()` 出发**（= 有因子快照 ∩ 剔除池外）。不允许从「博主挖名池」「positions∪lhb 兜底」等噪声源直接出发。

**剔除池（`exclusion_pool`）只含客观硬垃圾**，理由白名单 `VALID_REASONS`（见 `viewer/config.EXCLUSION_REASONS`）：
- `st` —— ST/退市/风险（客观）
- `no_kline` —— 名称字典有但 K 线缺失（无法分析，客观）
- `illiquid` —— 极弱流动性（可选）

**强制准入机制（2026-10-26 进哥要求，杜绝「永久剔除」类回归）：**
- `get_exclusion_pool()` 读取时，**非白名单理由一律忽略并 `logger.warning`**（历史上曾误塞 `blogger_noise` 把 80 只永久剔出，已修复并释放）。
- `assert_clean_pool(con, strict=False)` 提供硬校验，返回违例列表；`strict=True` 可让启动自检直接报错。
- 博主信号**不**剥夺标的入选资格。博主维度已于 2026-10-30 整体下线（实证反向 + 源 2026-10-27 停更），旧的「带时效反向 overlay」一并作废；避坑改由 `viewer/avoid.py` 按当日量价 + 近 15 日利空公告实时计算，不落库、不设窗口参数，结构上不会陈旧。详见 `DECISIONS.md` D1。

---

## 三、信号权重驱动（P4 实证 → 可消费配置）

`viewer/config.SIGNAL_WEIGHTS` 是 P4 全量回测（5243 只 / 回看 250 日 / 前向 5,10,20）落地的实证表：
- `weight > 0` → 该方向正 alpha，加权优先（如 `RSI6超卖`、`跌破BOLL下轨`，均值回归类）；
- `weight < 0` → 该方向在本环境负 alpha，降权/反用警惕（如 `放量`、`股价在Supertrend上`，趋势跟随类）；
- `weight == 0` → 暂不启用；
- 数值为「初步相对权重」，定期（季度）用 `signal_backtest` 重算，**不写死胜率**。

**消费点**：`strategy_templates.TEMPLATES` 每项带 `alpha_signals`（对应 SIGNAL_WEIGHTS 键），模块加载即预计算 `alpha`（净 alpha 方向与依据），由 `api_strategy_templates` 直接暴露给前端——趋势跟随类模板自动标注「负 alpha / 降权谨慎」。

---

## 四、加功能纪律（防再乱）

1. **先定位层**：新功能属于采集/因子/评估/展示哪一层？写库必须声明。
2. **不绕过入口**：任何「兜底候选源」（博主挖名、positions∪lhb）必须显式标注来源或报错，不得静默混入干净宇宙。
3. **改了逻辑必同步文档**：动了行为 → 同步 `DECISIONS.md`（决策变更 + 推翻条件）+ `CAPABILITIES.md`（覆盖状态）；否则视为流程违规。
4. **单一事实源**：模板元信息、权重、剔除理由集中在 `strategy_templates.TEMPLATES` / `config.py` / `config.EXCLUSION_REASONS`，展示层直接读取，**禁止硬编码副本**。
5. **冒烟优先**：每次改完跑 `python -m viewer.universe_filter`（自检剔除池）+ 编译，挡住「改 A 漏 B」类低级错误。

---

## 五、已知缺口（待本机出网补跑，沙箱不可行）

- **估值维度**（治 C4）：`tools/fetch_a_shares_snapshot.py` —— 填报价/PE/PB 等。
- **官方板块成员**（治 C3/C4 官方池真「全」）：`tools/fetch_plate_members.py` —— 填 `plate_members`，否则 funnel 的 official 模式仍回退博主挖名池。

> 这两步必须在进哥本机 venv 跑（沙箱无出网）。跑完由 server 调度自动日更（15:30 后）。

---

## 六、主工作流编排（一条主线，收敛散入口 · C 阶段 2026-10-27）

对外只呈现**一条主线**，任何功能都是这条线上某个阶段的"可配置开关"，不再并列一堆散入口：

```
采集(net,本机出网)  →  因子(local)  →  评估(local)  →  回测(季度,local)  →  选股(local)
 fetch_a_shares_       indicators       universe_scan     signal_backtest      strategy_
 snapshot/plate_       (backfill)       (三轴打分)         (P4实证→权重)        templates
 members/kline                                                 ↑季度重算
```

- **统一入口**：`tools/pipeline.py`（打印计划 / `--local` 跑本地轻量 / `--full` 跑本地全量）；出网阶段只在进哥本机执行，pipeline 只列命令不自动跑。
- **统一调度**：`tools/sched_job.py` 的 `JOBS` 字典是所有阶段的唯一注册表（含本次补注册的 `signal_backtest`）。**新功能必须先在这里注册才算"接入主线"**，否则等于游离（此前 P4 回测跑了全量却漏注册，正是反面教材）。
- **频率分级**：日更（因子/评估/选股）、出网（采集，日/增量）、季度（回测重算 `signal_backtest`）。回测不在每日自动调度，避免 30min 空耗。

---

## 七、加功能三问准入 checklist（CI 化 · C 阶段 2026-10-27）

**每次新增/修改功能，落地前先逐条自答；任一答不出 = 暂停，先补清楚再动手。**

1. **它属于哪层？**（采集 / 因子 / 评估 / 展示）—— 写库必须声明层，否则违反"四层单一职责"。
2. **它影响 / 依赖哪些已有模块？** —— 列清单；若动到行为，必须同步 `DECISIONS.md`（决策变更 + 推翻条件）与 `CAPABILITIES.md`（覆盖状态）。改 A 必查 B 是否连带。
3. **它解决哪个已记录的矛盾**（DECISIONS 里的 D1–D7），还是**又造了一个新矛盾**？ —— 若制造新矛盾，先回 DECISIONS 修订，不准静默双写。

**CI 化门禁（提交前必跑）**：`python tools/smoke.py`
- 编译全部模块（挡 import 路径错 / 语法错）
- 配置常量一致性（池上限 / 窗口 / 三轴权重合计=1 / 博主时效窗 / 权重表非空 / 剔除白名单）
- DB 健康（因子/扫描/选股/回测表非空）
- **干净宇宙强制断言**：剔除池无非白名单理由、因子表无 ST 残留（挡"永久剔除/污染"类回归）
- 策略模板全带 P4 alpha 背书
- 红线扫描（致锐宇/Zhiruiyu/ZRY）零命中

> 我们此前翻车的"import 漏前缀 / JOBS 漏注册 / score 误填 / 剔除池误塞博主噪声"全部被上述检查覆盖。smoke 不过 = 不准合入。

---

## 八、存储成本（P1-4 · 2026-11-01）

**实测账单**：`viewer/app.db` 文件 8.8 GB + `app.db-wal` 15 MB ≈ **8.9 GB**。其中 7.3 GB 是 `stock_indicators_daily`（2010→今，1,316 万行 × 58 列）。

**但即时查询根本不碰历史**：查看器/选股/评估只取最近 260 个交易日，实测 **10.4% 行数 / 约 763 MB**；2010→去年那 90% 只在季度回测时才整段扫一遍。

**所以方向是冷热分离**（最近 260 日留 SQLite 保低延迟，历史因子转 Parquet/DuckDB 只在回测时按列读）。为什么这个方向对、迁移的前置条件（要改哪几个文件）、依赖与口径一致性风险、以及本轮为什么不做，全部见 **`docs/storage_plan.md`**。

**本轮只清冗余索引，未迁移任何数据。** 清掉的是 `idx_sid_code_date ON stock_indicators_daily(code, date)` —— 它与主键 `(code, date)` 的自动索引逐页完全相同（同为 97,134 页 / 379.4 MB），等于同一份索引存了两遍。`DROP INDEX` 耗时 0.30 s，`freelist_count` 1 → 97,135。

> **删索引后 `du` 不变是正常的** —— SQLite 把 379 MB 挂进 freelist 复用，要真正归还操作系统必须 `VACUUM`（需约 9 GB 空闲 + 长时间锁库），本轮明确不做。**判断空间是否回收看 `PRAGMA freelist_count`，不要看 `du`。**

配套的两个工具：`tools/storage_report.py`（谁占了多少、哪些索引是前缀冗余候选、统计是否过期、缓存是否超期，`--json` 可机读，退出码 `3`=发现冗余索引 / `4`=发现超期缓存）、`tools/ensure_indexes.py`（索引的唯一注册表，含冗余索引回滚 DDL 与 `--check` 哨兵）。

**为什么 `viewer/indicators.py` 里那行建索引必须物理删除、而不是"知道就行"**：`CREATE INDEX IF NOT EXISTS` 既不报错也不提示，留着它就会在每次因子日更重算时把 379 MB 悄悄建回来，这轮的节省会在第二天自动失效。
