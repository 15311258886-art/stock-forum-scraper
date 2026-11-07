## D60 「哪些是股票、哪个板块」收口到 `security_master`；`selection._bad_code` 退役（2026-11-01）
- **背景**：P0「全 A 股宇宙不完整」根因是**板块口径各写一份代码段白名单**——`selection._bad_code`、`strategy_sim._liquid_from_kline`、`holders_fetch.collect_codes`、`fetch_a_shares_tencent._market_prefix`、`tools/intraday_snap._qq` 五处各自判断「是不是北交所 / 该用哪个市场前缀」。920 段（2023 年启用的北交所新段）在其中的表现是**静默出错**：腾讯被拼成 `sh920000` 返回空、新浪落到 `sz` 前缀返回空数组、宇宙表整块漏收 348 只。全都**不报错**，症状统一表现为「这只票没有数据」。
- **结论**：
  1. 板块 / 交易所 / 标的类型的**唯一定义处**是 `viewer/security_master.py`（`classify_kind` / `board_of` / `exchange_of`）。任何模块不得再写代码段白名单；已迁移的 6 处见 `security_master` 各函数 docstring 与 `tests/test_universe_consumers.py`（含一条 AST 守卫：`Compare` 节点出现 2~8 位纯数字字符串常量即判失败）。
  2. **`selection._bad_code` 已于 2026-11-01 退役**，语义等价物是 `security_master.stock_codes(conn, include_bse=False, exclude_delist_suspects=True)`。`_bad_code` 所表达的「选股池统一排除北交所」**是产品选择，不是数据缺失**——宇宙统计必须包含北交所（`include_bse=True`），买入候选 / 正向信号必须排除（`include_bse=False`）。
  3. 分页纪律：`stock_codes()` 的 `exclude_delist_suspects` 默认 `False`，只有「买入候选 / 正向信号」路径传 `True`；覆盖率、审计、宇宙统计传 `False`（否则分母被悄悄缩小，缺口再次隐身）。
- **保留旧记录的理由**：D2 / D20 等条目里提到 `_bad_code` 的段落**不改写**。它们记录的是**当时**的取舍与当时的证据，改写历史会让后人失去「为什么当初这么做、后来为什么变」的线索。失效引用以本条为准。
- **影响模块**：`viewer/security_master.py`（新增，唯一口径源）、`viewer/db.py`、`viewer/selection.py`、`tools/fetch_a_shares_snapshot.py`、`tools/fetch_a_shares_tencent.py`、`tools/verify_fetch.py`、`tools/audit_coverage.py`、`tools/intraday_snap.py`、`strategy_sim.py`、`holders_fetch.py`。
- **推翻条件**：若某天出现「同一代码在不同交易所口径下归属不同」的真实案例（如跨市场 CDR 双重上市），不得在消费方各自特判，必须在 `security_master` 里加字段并同步本条。
- **历史两日北交所缺口确认不可补、不采用跨源回补（2026-11-01 终局）**：`2026-10-27` / `2026-10-29` 两天的北交所缺口（各 348 只）**永久保留为 `no_source`，不补齐**。腾讯 `qt.gtimg.cn` 是实时接口、无日期参数（实测 348/348 只的 `f[30]` 都指向最新一场），所以无法按期回填；`data/kline_daily.db` 虽有 343/350 只的 OHLC，但**决定 `not_adopted`**：混源会让 `source` 不可追溯，且该库无 PE/PB/市值，回补会使当日「估值非空率」假性归零 —— 那比缺口更难发现。证据见 `data/snapshots/backfill_missing_<date>.json`（含 `decision/decided_by/decided_at/reason_final`），`tools/verify_fetch.py` 对这两日长期标 `no_source` 并单列近期覆盖率表。**这条是结论，不是待办。**

---
