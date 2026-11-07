# 股票论坛可视化查看器 · 遗留系统重构体检报告
> 视角：遗留系统重构与测试专家（15 年企业级重构经验）
> 日期：2026-11-07
> 对象：`stock-forum-scraper`（viewer/ + tools/，约 200+ 模块）

## 0. 总体结论

代码"**能用、有纪律、有文档**"，但"**长得太快、收得太少**"。

最大的风险不是单点 bug，而是**结构性的可维护性债**：
上帝类 `server.py` + 回测引擎碎片化 + 环境脆弱 + 知识沉在巨型 markdown。

好消息：回测引擎本身**设计正确**（时间之墙 / 同池基准 / 成本先扣），单测 16/16 通过，有 CI。
重构有基础，不必推倒重来——按"先补伞、再小步挪、最后清垃圾"的顺序走即可。

---

## 1. 代码痛点诊断（坏味道 + 证据）

### 1.1 上帝类 `server.py`（最高危）
- `viewer/server.py` **17,428 行 / 514 个函数**；`_do_GET` **964 行**、`_do_POST` **512 行**，前 30 大函数平均 203 行。
- 手写 stdlib HTTP 框架：`server.py:37` 导入 `BaseHTTPRequestHandler, ThreadingHTTPServer`，`server.py:14407` 定义 `class Handler`。等于在一份文件里自研了半个 Web 框架。
- **几乎零直接单测**：全仓 697 个测试函数里，仅 `tests/test_ai_expert_desk.py` 一处 `import viewer.server` 且只测了一小块。
- 风险：任何改动无伞、圈复杂度爆炸、新人无法下手。这是"动 A 漏 B"类回归的温床。

### 1.2 回测引擎碎片化 + 口径漂移（第二高危，直击产品内核）
- 全仓 **81 个文件**含"回测"，仅 `backtest*.py` 就有 **14 个**。
- 前向收益 `forward_return / fwd_return / _fwd_ret` **至少 5 份独立实现**，签名各异：
  - `viewer/factor_import.py:195`
  - `viewer/person_score.py:202`
  - `tools/ab_box_mode.py:96`
  - `tools/router_backtest.py:215`
  - `tools/signal_ic.py:81`
  → 改一处成本口径，其余默默漂移，且不会报错。
- **成本口径不统一（关键）**：`signal_backtest` 用集中常量 `params.BACKTEST_FEE_ROUNDTRIP = 0.0015`（15bp，`signal_backtest.py:46`），而 T10 的 `router_backtest.py` 默认 `cost_bp=13.0`（13bp，`main()` 默认值）。**T10 与 P4 的结论不在同一个成本 footing 上，不能直接横比。**
- `backtest_oss*.py` 虽是 5 文件流水线（engine / basis_check / report / selfcheck / verify），组织得好，但仍是 `router_backtest` 之外的**第二套独立实现**。

### 1.3 依赖与环境脆弱（本次重跑就踩了）
- `requirements.txt` 全 `>=`（`requests>=2.31`、`pytest>=8`…），**无 `pip freeze` 锁** → 换机器/重装可能装到 numpy 2.x 而炸。
- 默认 shell（WorkBuddy 捆绑 python 3.13）**没有 numpy/pytest**，必须用 `.venv`；但**无激活脚本、无 make 入口**。本次重跑第一脚就 `ModuleNotFoundError: numpy`——换 shell 即跑不起来。

### 1.4 诊断垃圾与生产代码混放
- `tools/` 共 179 个 `.py`，其中 **54 个（30%）是 `probe_/audit_/qa_/_` 前缀的诊断脚本**，在主干里干扰视线，分不清哪些是"真在用的功能"。

### 1.5 备份文件垃圾
- 全仓 **103 个 `.bak*`**（含 `app.db.bak-*` 数据库备份、`pillars.py.bak-*` 代码备份）直接塞在工作树里。

### 1.6 知识沉在巨型 `DECISIONS.md`
- **427KB、100+ 条 ADR** 全挤在一个 markdown；决策与代码易漂移、难检索、无机器可读索引。

### 1.7 魔法数字
- 成本/窗口常量（cost_bp / fee / lookback / 260 / 250）**121 处散落**；虽有 `params.BACKTEST_FEE_ROUNDTRIP` 单一常量，但 `router_backtest` 默认 13bp 没用它。

---

## 2. 做得好的地方（别只挑刺）

- 回测引擎三条防作弊**设计正确且隔离干净**：时间之墙单点 `window_at`（`router_backtest.py:200`）、同池基准、成本先扣——前视偏差类 bug 被物理阻断。
- `oss` 回测有完整流水线（engine + 价格基准核对 + 报告渲染 + QA 自检 + 手算复核），工程扎实。
- `ARCHITECTURE.md` 四层职责 + 干净宇宙入口写得清楚，说明团队有架构纪律。
- **CI 存在**（`selfcheck-gate.yml` / `overseas-relay.yml`）。
- **异常吞没极少**（仅 3 处 `except: pass`），不掩盖错误。
- 回测引擎单测 **16/16 通过**，pytest 全量 **732 passed**。

---

## 3. 重构路线（安全优先，行为等价）

### 阶段一（低风险，立刻可做）
- **P1 抽路由表**：把 `_do_GET/_do_POST` 的 964+512 行分发，抽成 `ROUTES = {path: handler}` 注册表，每个 handler 独立小函数 + 独立单测。不动行为只挪位置，立刻让 `server.py` 可测、圈复杂度下降。
- **P2 依赖固化**：`pip freeze > requirements.lock.txt`（或 pip-tools）；加 `make run` / 激活脚本；把"必须用 `.venv`"写进 README 首行。

### 阶段二（中风险，需回归测试护体）
- **P3 建回测内核 `BacktestKernel`**：把 `forward_return / 成本扣减 / 同池基准 / 时间之墙` 抽成单一模块，所有引擎（`signal_backtest` / `router_backtest` / `backtest_oss` / `quant.factor_backtest`）共用 → 5 份 → 1 份。
- **P4 统一成本常量**：全部 `import params.BACKTEST_FEE_ROUNDTRIP`（15bp）或显式 per-tool 覆盖，禁字面量 `0.0013 / 0.0015`。
- **P5 隔离诊断脚本**：`tools/` 下新建 `diagnostics/`，把 54 个 `probe_/audit_/qa_` 移进去。

### 阶段三（清理）
- **P6 清理 `.bak`**：DB 备份移到 `.backups/`（git-ignored），代码 `.bak` 直接删（git 有历史）。
- **P7 治理 `DECISIONS.md`**：拆成 `docs/adr/` 小文件 + YAML 索引；或只留近 90 天，历史归档。
- **P8 落实冷热分离**（已有 `docs/storage_plan.md`）：`app.db` 8.9GB → 近 260 日留 SQLite，历史转 Parquet/DuckDB。

---

## 4. 破坏性风险提示

- 抽路由表时，handler 的副作用（TTL 缓存 / `_send_json` 状态 / 快照落盘）**必须原样保留**，否则前端缓存策略被打回原形。
- 回测内核统一时，旧"13bp vs 15bp"的差异要**显式标注**，不能偷偷改口径让历史超额变好看——这正是你们反复强调的"事实与判断分开"。
- 删 `.bak` 前确认：DB 备份不在 git，需先确认无待恢复；代码 `.bak` 删前确保 git 有提交。

---

## 5. 与"产品内核"主线呼应

这些回测引擎是验证**产品内核**（决策台四 tab 判据）的"工具货架"。
货架碎片化 = 内核验证逻辑碎片化。
**统一回测内核 = 让"内核判据对不对"只由一个可信引擎回答，而不是 5 个各说各话。**
这才是进哥要的"不断迭代、提升内核"——验证能力先收口，内核才能放心进化。

---

## 6. 本次回测重跑状态（T10 主体）

| 检查项 | 结果 |
|---|---|
| 回测引擎单测 `tests/test_router_backtest.py` | **16/16 通过** |
| 单工具冒烟 `box_bottom`（5 年） | 确认能跑能算；前台 120s 超时说明**单工具就要 >2min**，全量分钟级 |
| 全量 K 线组 7 工具 · 5 年 · T+20 | **后台运行中**（task `2D8801`），完成后贴汇总表 |
| `oss` 组 / `pick` 组 / T08 账本 | 结论已在 `DECISIONS.md` 落定（5 年回测：tv_wbottom −0.57pp/t=−3.19、db_chan −0.56pp/t=−2.81、db_wave −0.66pp/t=−5.02、box_bottom +0.22pp/t=1.35）。如需我把整条 T10→T08 链重新实跑一遍，请确认，预计 30+ 分钟。 |

### 重跑过程中暴露的两个真实工程坑（本身就是本次体检结论的注脚）
1. **默认 shell 不激活 `.venv`** → 任何新会话 `python` 缺 numpy/pytest，必须显式用 `.venv/Scripts/python.exe`。对应 P2。
2. **后台长任务重定向路径错位 / 进程被回收** → `cd tools/` 后用相对路径 `viewer/logs/...` 落盘失败；`nohup &` 子进程在工具返回后被沙箱回收。必须用 `run_in_background=true` + 绝对路径。对应 P2（环境可复现性）。
