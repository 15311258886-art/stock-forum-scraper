# 导航结构与「跟我学习」板块

> **2026-09-23 二次改造（布局）**：导航原先被包在居中容器 `.wrap{max-width:1280px}` 里，
> 1920 视口下左右各浪费 320px（33%）且导航未贴左缘（实测 x=342）；横向子类条另占 45px 高。
> 现改为**应用外壳**：左侧一列导航**贴视口最左缘**、全高、只留右边框；大类与其子类都在左栏
> 竖向展开（手风琴）；内容区满宽铺开。子类横向条取消，`.panel` 容器与命名不变。
>
> **2026-09-23 首次改造（结构）**：原先是**一排 10 个横向 Tab**，条目偏多、层级不清；改为
> **大类 → 子类** 两级结构，并新增「知识获取」与「跟我学习」两个大类。

## 一、信息架构（分类依据：数据链路四段式 获取 → 研判 → 执行 → 沉淀）

| 左侧大类 | 左栏子类（在选中大类下方缩进竖排） | 来源 |
|---|---|---|
| **总览** | 汇总 | 原有 Tab |
| **知识获取** | 数据源 / 原始存档 / 抓取与登录记录 | 原散落在海外面板与顶栏的运维信息，**归拢**而成 |
| **行情研判** | 当前讨论股票 / 舆论情绪 / 近期热点 / 美股港股舆情 / 国外驱动热点 | 原有 5 个 Tab |
| **实盘操作** | 正在操作股票 / 实盘选手(渠道) / 共同持仓共识 / 近5日共同买入 / 相关人物 | 原有 4 个 Tab（相关人物从「研判」移入，人物归一处）+ **近5日共同买入（新增，Request F）** |
| **跟我学习** | 中短线炒股 / 学习轨道管理 | **新增** |

原有的 10 个 Tab 全部有归属，**无一功能被删**。左栏底部另有一块「知识库」上下文区
（吸取状态 chip + 立即吸取按钮），**只在「跟我学习」展开时出现**。

## 二、改动涉及的组件

### 前端（3 个文件，渲染层零改动）

| 文件 | 改动 |
|---|---|
| `viewer/static/index.html` | 外壳改为 `.shell`（grid `236px + minmax(0,1fr)`）= `.sidenav` + `.content`。<br>原 `.wrap` / `.layout` / `.main` / `#subnav` **已删除**；`.hint` / `.refresh-bar` / `#cards` 与全部 `.panel` 移入 `.content`。<br>**`.panel` 容器与其 `#p-<tab>` 命名原样保留** —— 这是既有 12 个 `renderX()` 一行不用改的关键。 |
| `viewer/static/app.js` | `NAV` 配置（唯一事实来源）→ `renderNav()` 生成**整列左栏**（大类 + 当前大类的子类竖排 + 学习类 rail-foot）；`selectNav()` 切换；`#/大类/子类` **hash 深链接**（刷新与前进/后退保持位置）。<br>`renderNav()` 是**唯一**导航绘制入口：只绑事件、不写 hash、不切面板，因此可被异步数据（学习轨道列表到达）随时调用而不会递归；原为避开递归而拆出的 `paintSubnav()` 已删除。<br>新增 `renderKbDoc()` / `openBlogger()` / `renderKbTracks()` / `renderSourcesPanel()` / `renderArchivePanel()` / `renderFetchLogPanel()`；`renderPlayers()` 的两条横向筛选工具栏改为左侧竖排 `.filter-col`（`filterPlayers()` 同步维护 `.fbtn.active` 互斥高亮）。 |
| `viewer/static/app.css` | 新增 `.shell` / `.content` / `.sidenav` / `.nav-group` / `.nav-item` / `.nav-sub` / `.sub-item` / `.rail-foot` / `.panel-split` / `.filter-col` / `.fbtn` 与 `.kb-*` 系列；`.cards` 栅格 `minmax(150px,1fr)` → `minmax(200px,1fr)`（卡片不再恒为 164px）。<br>`@media (max-width:1080px)` 下左栏折叠为顶部横向；全部沿用既有 `--accent/--border/--panel/--tag/--warn` 变量 → **4 套主题自动继承**。 |

### 后端（新增 1 + 改 3）

| 文件 | 改动 |
|---|---|
| `viewer/kb.py`（**新增**） | 解析知识库 md → 结构化 JSON。纯标准库。 |
| `viewer/db.py` | 新增 `kb_doc` 表（整篇替换的文档型数据，不拆规范化表）+ `kb_save()` / `kb_get()` / `kb_list()`。`kb_save` 按 `content_hash` + `parser_version` **幂等**。 |
| `viewer/server.py` | 新增 `GET /api/kb/list`、`GET /api/kb/doc(?key=&light=1)`、`POST /api/kb/reload`、`GET /api/raw_pages`；`api_summary()` 补 `last_fetch_at`；`api_refresh_status()` 的 `tracked_sources` 并入知识库来源。 |
| `config/kb_sources.yaml`（**新增**） | 声明「去哪找 md 文件」，不关心更新频率。 |

## 三、路由一览（新增部分）

| 路由 | 说明 |
|---|---|
| `GET /api/kb/list` | 学习轨道索引 + 吸取状态（轻量）。**新增轨道后左栏「跟我学习」自动多出一个子类**。 |
| `GET /api/kb/doc?key=zdx_stock&light=1` | 只回 `meta` + `outline`（几 KB），供预检。 |
| `GET /api/kb/doc?key=zdx_stock` | 完整正文（约 250 KB：8 流派 / 50 博主 / 33 条回测）。前端**懒加载**，只在进入该子板块时拉。 |
| `POST /api/kb/reload` | 重新吸取，body `{key?, force?}`。内容未变返回 `skipped:true`。 |
| `GET /api/raw_pages?limit=80` | 原始存档元信息（**不回 HTML 正文**）。 |

前端路由：`#overview/summary`、`#acquire/archive`、`#research/topics`、`#trade/consensus`、`#trade/cohold`、`#learn/learn_zdx_stock` …

## 「近5日共同买入」（Request F）

位于「实盘操作」下、紧邻「共同持仓共识」。**两者口径不同，不可互相替代**：

| | 共同持仓共识 `#trade/consensus` | 近5日共同买入 `#trade/cohold` |
|---|---|---|
| 回答的问题 | 现在还有多少人**持有** | 最近 N 天谁**同时动手**了 |
| 判据 | 持仓快照（不看时间） | 动作发生时点（`buy_time` / `sell_time` 窗口过滤） |
| 看得到 | 当前共识强度、层级加权、已清仓标注 | 窗口内的共买/共卖清单、各自买入时间价格数量、买入后是否已卖出 |

- **数据源必须合并两张表**：`positions`（仓位占比 / 买价 / 现价）＋ `trades`（**成交数量**、更精确成交时刻）。
  只查 `positions` 会漏掉绝大部分——实测共买从 11 个标的掉到 1 个，因为数量与多数成交记录只存在于 `trades`。
- **窗口锚定**：默认 `auto` = 数据最新日（按日历日算会因数据未跟上今天而全空，看起来像功能坏了）；
  可切 `today` / 指定日期。当前用哪种由接口的 `anchor_source` 回传，界面**如实标注**，不做静默替换。
- **浮盈只对买入行计算**：SELL 行的价是卖出价，拿它跟现价比得到的是「卖完之后又涨跌了多少」，
  会被误读成收益，故卖出组一律返回 `null`。
- 参数：`?days=1..60&min_holders=2..1000&anchor=auto|today|YYYY-MM-DD`。
- 路由：`GET /api/cohold_recent`；表方法：`Database.cohold_recent()` / `_cohold_group()`。
- 回归：`tools/verify_cohold.py`（隔离验证）、`tools/ui_probe_cohold.mjs`（真实浏览器）。

## 四、数据配置方式

- **知识库来源**：改 `config/kb_sources.yaml`。`path` 是精确路径，`path_glob` 是兜底
  （上游按「日期目录 + 版本号」导出，换日期换版本时自动跟上，不必改配置）。
- **新增一个学习轨道**：配置里加一条（如 `doc_key: value_investing` / `track: 价值投资`），
  左栏「跟我学习」会**自动多出一个子类**，无需改前端代码。
- **新增一个功能子板块**：① `app.js` 的 `NAV` 里加一项；② `index.html` 加一个空的
  `<div class="panel" id="p-<tab>">`；③ `loadAll()` 里加一次 fetch + render。
- **调整布局**：左栏宽度改 `.shell` 的 `236px`；断点改 `@media (max-width:1080px)`；
  卡片密度改 `.cards` 的 `minmax(200px,1fr)`。

## 五、每日自动更新（重吸取）

上游每天把 md **覆盖写出**；程序按 `内容哈希` + `parser_version` 判断要不要重建——**两者都没变就跳过**，
不写库、不动 `ingested_at`，所以每天定时跑也不会产生无意义重建。

- 手动：左栏「跟我学习」底部的「立即吸取」按钮，或 `python tools/kb_ingest.py`
- 自动：定时任务每天 09:00 执行 `python tools/kb_ingest.py --json`（不依赖查看器进程）
- 时效展示：「学习轨道管理」与知识库头部复用 **Request G 的「最后更新」机制**
  （超时阈值 48 小时 = 2×24 小时周期，可用 `VIEWER_KB_STALE` 调整）

## 六、验证

- `tools/verify_kb.py` —— 隔离验证（临时库 + 临时端口）：解析正确性、落库幂等、
  force 重建、接口结构、时效接入、来源缺失容错、HTTP 层。**58 项全绿**。
- `tools/verify_refresh_status.py` —— Request G 回归，**全绿**。
- `tools/ui_probe_layout.mjs` —— **布局量化**：左栏贴左（x=0）、内容右留白（期望 ≤24px）、
  卡片宽度、手风琴展开组数、筛选栏竖排与点击过滤、1080/880/640 响应式、横向溢出。**73 项全绿**。
- `tools/ui_probe_nav.mjs` —— 真实无头 Edge + CDP：两级导航切换、深链接、知识库
  渲染计数、博主抽屉、1080px 响应式、旧功能无回归。**全绿，JS 异常 0**。
- `tools/ui_probe_themes.mjs` —— 4 套主题下左栏/知识库的计算颜色与结构。**全绿**。

> 验证踩坑（已写进 `web-ui-regression` 技能）：`document.querySelectorAll(".cards > .card")`
> 会把**面板内部**复用的同名 `.cards` 一起数进来（知识获取三块各有一组），本次把 9 张统计
> 卡片误报成 15 张 —— 计数选择器必须带容器限定（`#cards > .card`）。
