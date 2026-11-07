# 能力清单与已知缺口（CAPABILITIES）

> **作用**：直接回答「全不全」。每张表 / 每个维度标注覆盖状态。
> 标记：✓ 已全 ｜ △ 部分 ｜ ✗ 缺失（待本机抓）｜ ⚠ 毒信号（已降级，仅监控）
> 数据截至 2026-10-27。

---

## 一、数据采集层（fetch_*，需本机出网）
| 维度 | 表 / 来源 | 覆盖 | 说明 |
|---|---|---|---|
| 全市场日 K 线 | kline_daily.db.kline | ✓ 5550/5588 (99.3%) | 北交所339 + 沪深5211；缺 38 只新上市 / 停牌 |
| 全A 报价 / 估值 / 市值 | stock_daily_snapshot | ✓ 5240 行 | 沙箱直跑 `fetch_a_shares_tencent.py`（腾讯源，治 C4；原东财 `fetch_a_shares_snapshot.py` 本机亦可）|
| 官方板块成分股 | plate_members | ✓ 4998 行 / 17 板块 | 2026-10-28 由 **AI 在外网经 westock 连接器**（腾讯/聚源源）闭环抓取并并入 `viewer/app.db`；原沙箱出口 IP 受限改走此路（见 C3）|
| 抖音舆情语料 | douyin_corpus_video | △ 2770 行 | 已接调度日更，但需本机刷 |
| 博主荐股 / 舆情 | blogger_* / res_stock_card | ✓ 已采集 | ⚠ 实证反向，仅作噪声监控，不进正选 |

## 二、因子层（indicators，本地纯算，无出网）
| 维度 | 表 | 覆盖 | 说明 |
|---|---|---|---|
| 52 因子快照 | stock_indicators | ✓ 5243 只干净票 | 已剔除 ST / 博主噪声 / 无K线 |
| 因子历史 | stock_indicators_daily | ✓ ~135 万行 | 最近 WINDOW=260 日 |
| 买卖信号 | stock_signals | ✓ 5243 | 派生买卖原因 |

## 三、评估层（干净宇宙上）
| 维度 | 表 | 覆盖 | 说明 |
|---|---|---|---|
| 全宇宙多因子扫描 | stock_universe_scan | ✓ 5243 | 动量0.45 / 量能0.25 / 质量0.30，分档 A/B/C/D |
| 信号回测 | signal_backtest | ✓ 51 行 (17信号×3窗口) | 前向 5/10/20，含同期基线对比（治 C5）|
| 现成策略模板 | stock_strategy_picks | ✓ 2461 条 / 5 模板 | 超卖反弹 / 均线多头 / Supertrend / 放量 / 质量龙头 |

## 四、展示层（server API + 前端，只读）
| 能力 | 路由 | 状态 |
|---|---|---|
| 方法共振漏斗 | /api/funnel/score | ✓ 官方 / 舆情双模式 + 干净宇宙卡 |
| 四支柱方案 | /api/pillars/* | ✓ 候选池已扩至干净宇宙 2000 |
| Kronos K线预测 | /api/kronos/* | ✓ 抽样已净化 |
| 因子信号 / 快照 | /api/indicators* | ✓ |
| 全宇宙扫描（多因子） | /api/universe/scan | ✓ |
| 准入闸门（估值+基本面） | `viewer/gate.py`（内嵌于中线打分） | ✓ D66：硬否决剔除（PB≤0/市值<15亿/负债率>95%/ROE<-30%，实测只碰 3%）、软否决扣分（每条 −2 封顶 −6）；**PE 分位不作主闸门**（全市场 31% 亏损，会误杀困境反转）；行业分位待 plate_members 覆盖 ≥85% 再启用 |
| 评分日变化（较上日） | /api/universe_box → delta | ✓ D66：`universe_box_hist` 每日全量快照 + d_score/d_rank，首日不编数；缓存按**交易日**失效并回 stale_note |
| AI 复盘独立取数 | `AI_INDEPENDENT=1`（默认） | ✓ D66：切断 selection/oss/harvest 三路规则引擎产物，只喂原始层；picks_source 落库便于分组统计胜率；`=0` 可回滚 |
| 工具包（货架 + 级联漏斗） | `viewer/toolbox.py` · `viewer/static/toolbox.js` · /api/toolbox · /api/toolbox/funnel · /api/toolbox/status | ✓ D67：一级入口（决策台右侧）。16 个工具入库（14 可用 + 2 已淘汰带原因），统一 `apply()` 契约，能力卡含收录时间/为什么收/实测表现/短板，淘汰的不撤架；整链离进程算、按签名落盘、按交易日失效。实测 5264 → 闸门 5012 → 箱体 594 → 机构建仓 276 |
| 形态因子库（二期） | `viewer/factors.py` | ✓ D68：把 B站「技术·价值教学」/ 豆包「技术方法」两套语料里的人话编译成 6 个可算因子（缩量回踩 / 双底W / 平台突破 / MACD底背离 / 波浪位置 / 缠论中枢），统一走工具包插口。全市场实测 5264 → 6 / 488 / 21 / 293 / 521 / 44。三个第一版判据被数据推翻并重写（MACD背离自相矛盾出 0 只、W底下跌中继误判 1117 只、波浪只判截面不判顺序 1804 只） |
| 因子准入门禁（三期） | `viewer/factor_import.py` · /api/factors/candidates · /api/factors/admit · /api/factors/sources · /api/factors/source_status | ✓ D68：外来因子必须过四条硬线（|IC|≥0.02 / |ICIR|≥0.30 / |t|≥2.5 / 撞车<0.70）+ 20 交易日影子期，IC 转负自动降权。表达式走 AST 白名单（不裸 eval）；IC 自算不抄仓库数；相关性对 8 个已知风格查换皮（自检实测把 `pct(close,20)` 以 corr=1.00 拒掉）。GitHub 发现每日 16:30 自节流 7 天，**只发现不编译**，扫不到时如实降级为「未取到≠没有」 |
| 全市场箱体波谷（中线主表） | /api/universe_box | ✓ D65：干净宇宙 5003 只 × 750 日箱体，驻留/位置/规整/触及四维打分（驻留 35% 权重），离进程算约 10s 后落盘；与 `/api/universe/scan` 是两件事（那个是多因子扫，这个是箱体形态扫） |
| 策略模板 | /api/strategy/templates | ✓ |
| 回测结果 API + 前端可视化 | /api/signal/backtest | ✓ P4 实证 51 行已开放；前端「信号实证 → 信号回测」面板上线（summary 卡片 + 按夏普降序表 + 当前消费权重对照，支持 5/10/20 日窗口筛选）|

## 五、已知「毒信号」（已降级，仅监控）
- **博主荐股 / 舆情**：实证 T+5 负收益（blogger_perf −3.33%、res_card −2.04%），已降级为逆向 overlay，不进正向共识。

## 六、已知缺口与落地路径（沙箱无网 / 出口 IP 受限）
- **C4 估值/报价/市值**：✓ 已闭环。沙箱直跑 `tools/fetch_a_shares_tencent.py`（腾讯源，5240 只，24s），`tools/verify_fetch.py` 核验覆盖 100%、PE/PB/市值非空 99.9%+。
- **C3 官方板块成分（plate_members）**：✅ **已闭环（2026-10-28）**。最终落地路径与初版设计不同——沙箱出口 IP 被东财整体限制、同花顺限流，故改由 **AI 在外网经 westock 连接器（腾讯/聚源源，结构化、完整、不限长）** 直接抓取 17 板块全量成分股，落暂存库后并入 `viewer/app.db`。
  - **抓取通道**：`mcp__westock-mcp__data_sector`（mode=constituent），板块码体系：申万一级 `sw1_pt0180xxxx` / 申万二级 `sw2_pt0180xxxx` / 聚源概念 `indus_pt02xxxx`。19 个源板块码 → 17 个漏斗板块（消费=食品饮料+消费电子、汽车=乘用车+零部件 合并）。
  - **落库脚本**：`tools/load_plate_members_westock.py`（解析 westock 落盘 JSON→`viewer/plate_members_staging.db`）→ `tools/merge_plate_members.py`（幂等 DELETE(board)+INSERT 并入 `viewer/app.db`，busy_timeout 等待 viewer 释放锁）。
  - **数据规模**：4998 行 / 17 板块；westock 返回每只股票重复 2~3 次，已按 6 位代码去重；计数经"暂存库 vs 落盘原始去重"交叉校验一致。
  - **保留的本地刷新方案（备用）**：东财版 `tools/fetch_plate_members.py` + `run_c3_plate_members.bat` / 调度 `sched_job.py plate_members` 仍可用（本机出网可通东财），用于日后增量刷新；westock 通道为本次 AI 外网闭环的权威来源。

### C3 沙箱数据源排查结论（2026-10-27，事实）
| 源 | 沙箱状态 | 原因 |
|---|---|---|
| 东财 push2 / datacenter | ✗ 不可达 | 云沙箱出口 IP 被东财整体限制（RST + 报表配置不存在）|
| 腾讯 appstuff 主题接口 | ✗ 已下线 | code 11 No dispatch info found |
| 新浪概念 JSON API | ✗ 已废 | Service not valid |
| 同花顺 q.10jqka.com.cn | △ 可通但限流 | 壳页/详情可达，分页抓取 ~5 次后间歇返空/401 |

## 七、一句话总结
- K线 / 因子 / 评估 / 展示 / 估值 主体已全；P4 信号回测实证已开放后端接口（`/api/signal/backtest`）。**C3 官方板块成分（plate_members）已于 2026-10-28 由 AI 在外网经 westock 连接器闭环**（4998 行 / 17 板块，已并入 `viewer/app.db`），漏斗「官方池」模式现已可用。
- 功能叠加纪律见 `DECISIONS.md`；系统旋钮集中在 `viewer/config.py`。

## 八、工具「适用板块」归属（2026-11-03 新增）

`tool_registry` 增加 `pillars` / `pillar_note` 两列，22 个工具逐个声明服务哪个板块（短线热点 / 短线超跌 / 中线波段 / 长期埋伏），
并强制写明理由（`PILLAR_MAP`，缺标注则服务启动即报错）。当前分布：

| 板块 | 工具数 | 专属工具 |
|---|---|---|
| 中线波段 | 14 | 缩量回踩、波浪位置、箱体谷底（主用）|
| 短线热点 | 11 | 短线冲浪、政策漏斗、方法共振 |
| 短线超跌 | 10 | 超跌反弹、超跌修复、MACD 底背离 |
| 长期埋伏 | 7 | **无**（全部为跨板块复用）|

**已知缺口（见 `DECISIONS.md` D70 待办）**：`pillar_*` 四工具是内核镜像需摘除、`box_bottom`/`pick_oversold` 有同义重复、
`tool_run_log` 尚无收益字段故无法做工具级 IC 归因。
