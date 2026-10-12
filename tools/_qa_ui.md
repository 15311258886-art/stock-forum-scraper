# UI 全面回归 + 数据一致性体检报告

- 报告文件：`tools/_qa_ui.md`（本文件）
- 被测系统：`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`
- 被测实例：`http://127.0.0.1:8765`（运行中，最新代码）
- 产出时间：2026-09-22
- 执行人：general-purpose-3（任务 #8）
- **本报告只测不修；未改动任何源码、未改动 `viewer/app.db`、未重启服务。**

---

## 一、测试范围与方法

### 1.1 方法与工具

| 项 | 内容 |
|---|---|
| 浏览器 | 真实 Chromium（`ms-playwright/chromium-1234/chrome-win64/chrome.exe`，headless） |
| 驱动 | puppeteer-core（`NODE_PATH=C:\Users\野比&大熊\.workbuddy\binaries\node\workspace\node_modules`） |
| 断言方式 | **真实布局矩形**（`getBoundingClientRect` / `elementFromPoint` / `scrollWidth`）+ 计算样式对比度（WCAG 相对亮度比），不靠肉眼看截图 |
| UI 脚本 | `tests/ui_full_regression.js`（本轮新增，**全程只发 GET，绝不点「载入示例」「抓取并更新」「刷新海外舆情(联网)」「登录检测」**） |
| 接口探针 | `tools/_qa_ui_probe.js`（Node，GET 全部读接口并对账）→ `tools/_qa_ui_api.json` / `tools/_qa_ui_api_console.txt` |
| DB 只读体检 | `tools/_qa_ui_db.py`（Python sqlite3，`mode=ro` 只读打开）→ `tools/_qa_ui_db.txt` |
| 视口 | `1440×1000`（主）、`1024×700`（窄视口） |
| 主题 | `light-tech` / `dark-dashboard` / `minimal-report` / `card-warm` 四套逐个切换 |
| 截图 | `tools/shots_ui/*.png`（**未覆盖** `tools/shots/`） |
| 明细输出 | `tools/_qa_ui_regression.txt`（UI 断言全文）、`tools/_qa_ui_api.json`、`tools/_qa_ui_db.txt` |

> 环境坑记录：本沙箱 Bash 不可用（`dirname`/`cd`/`head` 均 `command not found`），全部改用 PowerShell + Read/Glob/Grep；Python 走 `stock-forum-scraper/.venv/Scripts/python.exe`；Node 脚本输出重定向到文件后再 Read（Windows 控制台中文乱码）。HTTP 探针用 Node `http` 模块（不走 `http_proxy`），避免本机代理 `127.0.0.1:55035` 把异常伪装成 502。

### 1.2 已有资产复用（不重复造）

- `tests/ui_regression.js`（抽屉交互 21 项）：**未直接复用**，因其第 20 行会点 `#btnDemo`（POST `/api/demo` → 会清空并重写各业务表，违反只读约束）。本轮按其做法重写为纯只读版本，并扩展到三种抽屉与四条关闭入口。
- `tests/ui_hotspot.js`（热点卡 31 条断言）：**本轮不重复测**，结论 **31/31 通过**，输出见 `tools/_qa_ui.txt`（17 张热点卡 / 100 条领域行 / 438 个公司标签，四主题配色与无 JS 报错均 PASS）。本轮只把「热点卡」相关的海外面板/热点面板**纳入整体 tab 渲染与四主题回归**，不重跑其专项断言，也不覆盖 `tools/_qa_ui.txt`。

### 1.3 只读约束执行情况

- 全部请求为 GET：`/api/summary`、`/api/stocks`、`/api/operating`、`/api/persons`、`/api/players`、`/api/consensus`、`/api/topics`、`/api/sentiment`、`/api/positions`、`/api/posts`、`/api/login_status`、`/api/stock_names`、`/api/overseas_sentiment`、`/api/overseas_hotspots`、`/api/overseas_sources`、`/api/stock`、`/api/person`、`/api/player`、`/api/detail`。
- 写接口（`/api/demo`、`/api/fetch`、`/api/overseas`、`/api/login_check`、`/api/sync_names`）**一次未调用**。
- 空态/错误态用 **puppeteer 请求拦截在测试页内构造**（abort / 500 / 缺字段 / 类型不符），仅影响测试浏览器，不触碰服务端与数据库。
- DB 体检以 `file:...?mode=ro` URI 只读打开；比对前后 `app.db` 的 mtime 未变（`2026-09-22 21:53:46`）。

---

## 二、覆盖清单（项目 × PASS/FAIL × 实测数字）

### 2.1 UI 回归总账

`tests/ui_full_regression.js` 最终结果：**PASS 71 / FAIL 9 / SKIP 0（共 80 条断言）**，全文见 `tools/_qa_ui_regression.txt`。

| # | 覆盖项 | 结果 | 实测数字 |
|---|---|---|---|
| 1 | 10 个 tab 逐个切换 | **10/10 PASS** | 每 tab `active=true`、文字 138~16519 字、节点 91~2643、横向溢出 0px、脏字符（undefined/NaN/null/Invalid Date）0 处、新增 JS 错误 0 |
| 2 | 抽屉弹出与内容（股票/人物/选手） | **3/3 PASS** | 股票 `宁德时代(300750)` 正文 1831 字；人物 `趋势猎人` 383 字；选手 `大阪柯小李` 2056 字；三者均无脏字符 |
| 3 | 抽屉横向不遮挡所点行 | **3/3 PASS** | 所点行 x[102,402] vs 抽屉 x[760,1422]；[103,375] vs [760,1422]；[196,384] vs [760,1422]，全矩形相交均为 false |
| 4 | 四条关闭入口（✕/Esc/点面板外/底部按钮）×3 类型 | **12/12 PASS** | 12 次关闭全部 `show=false` |
| 5 | 拖拽移动 + 标题栏/手柄留视口 | **6/6 PASS** | (760,76)→(140,36)；headTopInView=true、handleInView=true |
| 6 | 右下角拉伸 | **3/3 PASS** | 662×840 → 802×910，手柄未出视口 |
| 7 | 拖到右下极限后手柄仍在视口 | **0/3 FAIL** | 见 UI-02：位置 (1280,86) 宽 802 → 右缘 2082 > 视口 1440 |
| 8 | 四主题文字色≠背景色 / 对比度 / 元素不消失 | **12/12 PASS** | 对比度：light-tech 16.27/17.40/14.76；dark-dashboard 15.60/14.07/12.87；minimal-report 14.63/14.63/13.35；card-warm 12.32/13.08/11.78（均 ≥ 3.0）；四主题均 cards=24 / boxes=24 / badges=50 / 零尺寸块=0 |
| 9 | 四主题面板高度一致（不错位） | **1/1 PASS** | 四主题均 1004px（差 0） |
| 10 | 长文本：超长中文 | **1/1 PASS** | 页面 scrollWidth 1440→1440 |
| 11 | 长文本：超长无空格 ASCII（`.stk-name`） | **0/1 FAIL** | scrollWidth→2210（> 1440），见 UI-06（判定：断言过严 + 理论缺口） |
| 12 | 长文本：超长 ASCII（`.stk-corp`，真实可达字段） | **1/1 PASS** | scrollWidth→1440（该列已设 `word-break: break-word`） |
| 13 | 响应式 1024×700 / 1440×1000 无横向溢出 | **2/2 PASS** | 溢出 0px / 0px |
| 14 | 响应式顶栏 4 按钮 elementFromPoint 可命中 | **2/2 PASS** | 8/8 命中（1024 档 `#btnFetch`@909,14,93,35；1440 档 @1325,14,93,35） |
| 15 | 主页面全程无 JS 运行时错误 | **1/1 PASS** | `pageerror` 0、`console error` 0、`requestfailed` 0 |
| 16 | 空态（10 tab，接口返回空对象/空数组） | **10/10 PASS** | 每 tab 均出现可读空态文案（如「暂无讨论股票数据」「暂无海外舆情数据…」），脏字符 0，JS 错误 0 |
| 17 | 错误态：`/api` 全部 abort | **INFO** | 页面可见报错提示 **false**；卡片区空；`statusHint` 仍为默认文案；无错误横幅；见 UI-01 |
| 18 | 错误态：`/api` 返回 500 非 JSON | **INFO** | 同上，静默；见 UI-01 |
| 19 | 错误态：`/api` 返回 500 + JSON `{error}` | **INFO** | 同上，静默（`api()` 不校验 `r.ok`，把 500 的 body 当正常结果）；见 UI-01 |
| 20 | 错误态：`/api/summary` 缺 `counts` | **FAIL** | `pageerror: Cannot read properties of undefined (reading 'raw_pages')`，汇总面板整块空白，用户无任何提示；见 UI-03 |
| 21 | 错误态：`/api/topics` 条目缺 `weight` | **FAIL** | 面板渲染出 `缺权重话题A热度 NaN`；见 UI-04 |
| 22 | 错误态：`/api/persons` 条目缺 `mention_count/role` | **FAIL** | 产物 DOM 内出现 `undefined` 2 处；见 UI-05 |
| 23 | 错误态：`/api/summary.counts` 少给 3 个键 | **FAIL** | 卡片区出现 `undefined` 6 处；见 UI-03 |
| 24 | 错误态：`/api/sentiment` 返回 JSON 数组（类型不符） | **FAIL** | `pageerror: (d.map \|\| []).map is not a function`，舆论情绪面板整块空白；见 UI-07 |

### 2.2 数据一致性对账（`/api/summary` vs 各列表接口 vs DB）

| 口径 | API 返回 | DB 实测 | 结论 |
|---|---|---|---|
| posts | 185 | 185 | ✅ 吻合 |
| trades | 46 | 46 | ✅ 吻合（`/api/operating` 46 条，≤ LIMIT 200） |
| news | 3 | 3 | ✅ 吻合 |
| raw_pages | 1 | 1 | ✅ 吻合 |
| persons | 170 | 170 | ⚠️ `/api/persons` 仅返回 **100**（`LIMIT 100`），见 DC-03 |
| stock_mentions | 185 | 185 | ✅ 吻合；`/api/stocks` 24 只的 `mentions` 求和 = **185** ✅ |
| topics | 15 | 15 | ✅ 吻合（`/api/topics` 15 条） |
| players | 160 | 160 | ✅ 吻合（tier 分布 A37/B42/C35/D40/E5/F1；`is_top_player=1` 154；`is_visible=1` 160） |
| positions | 135 | 135 | ✅ 吻合（覆盖 71 位选手、23 只标的） |
| overseas_news | 707 | 707 | ⚠️ `/api/overseas_sentiment.news` 仅返回 40 条（`LIMIT 40`），见 DC-05 |
| overseas_hotspots | 17 | 17 | ✅ 吻合 |
| 情绪分布合计 | 看多 94 + 看空 49 + 中性 42 = **185** | posts 185 | ✅ 与 posts 总数一致 |
| 情绪地图 | `name_covered=24 / name_total=24` | stock_mentions distinct symbol = 24 | ✅ 名称解析 100% 覆盖 |
| 共同持仓共识 | `min_holders=2` → 23 条 | positions distinct symbol = 23 | ✅ 吻合 |
| 登录态 | taoguba = `no_credential` | login_status 1 行 | ✅ 吻合 |

### 2.3 示例数据 vs 真实数据（逐表按来源 URL 判别）

| 表 | 总行数 | 示例（`example.local`） | 真实 | 备注 |
|---|---|---|---|---|
| posts | 185 | **185** | 0 | 当前库为纯示例态 |
| trades | 46 | **46** | 0 | 同上 |
| positions | 135 | **135** | 0 | 同上 |
| players | 160 | **160** | 0 | 同上 |
| news | 3 | **3** | 0 | 同上 |
| overseas_news | 707 | 41 | 666 | **示例与真实同表混存**，见 DC-01 |
| overseas_hotspots | 17 | 0 | 17 | 由海外新闻聚合，间接含示例影响 |

`meta` 表：`demo=1`、`last_ingest=示例数据`、`overseas_last_collect=2026-09-22 21:53:47`。**「示例」标记只有 meta 这一个全局键，行级没有任何标记列**（`grep is_seed|_seed|demo` 在 `viewer/db.py` 零命中）。

---

## 三、问题清单

> 每条含：编号 | 严重级别 | 位置 | 现象 | 复现步骤 | 证据 | 建议修法

### UI-01 ｜ **P1** ｜ 接口失败/异常时前端静默失败，用户看到空白页却没有任何提示

- **位置**：`viewer/static/app.js:11-13`（`api()` 只做 `fetch().then(r => r.json())`，**不校验 `r.ok`，无 `catch`**；确切行号 **app.js:12**）；`viewer/static/app.js:1014-1028`（`loadAll()` 的 10 个请求**全部没有 `.catch`**）。全文件**仅 3 处 `.catch`**（`app.js:133 / 1037 / 1058`），15+ 个 `api()` 调用点无错误处理（含 `app.js:1041` 的「登录检测」与 `app.js:1072` 的实时刷新）。
- **现象**：后端异常时页面**完全空白**——`#cards` 空、10 个面板全空、`#statusHint` 仍是初始的「首次使用请点「载入示例」…」误导文案、无任何错误横幅；仅浏览器控制台有 `net::ERR_FAILED` / `500 Internal Server Error`。用户无法区分「没数据」和「服务坏了」。
- **复现步骤**：puppeteer 请求拦截 `/api/*` → `abort()`（或返回 500 HTML、或返回 500 + JSON `{error}`），打开 `http://127.0.0.1:8765/index.html`，观察页面。
- **证据**：`tools/_qa_ui_regression.txt` 第 88-93 行（三种失败形态 `页面可见报错提示=false`、`卡片区文字=""`、`statusHint` 为默认文案、`顶层错误条=false`）；截图 `tools/shots_ui/fail-全部 _api 直接失败_网络层 abort_.png`、`tools/shots_ui/fail-_api 返回 500 且非 JSON_HTML 错误页_.png`、`tools/shots_ui/fail-_api 返回 500 但 body 是合法 JSON {error}.png`。
- **建议修法**：`api()` 内加 `if (!r.ok) throw new Error("HTTP " + r.status)`，并在 `loadAll()` 统一 `.catch((e) => setHint("加载失败：" + e.message + "（请确认服务在运行）"))`；或增加全局 `window.addEventListener("unhandledrejection", ...)` 兜底提示条。

### UI-02 ｜ **P2** ｜ 抽屉拖到最右后，右下角拉伸手柄被推出视口（与代码注释承诺矛盾）

- **位置**：`viewer/static/app.js:699-705`（拖拽的横向钳制 `const minL = -(w - 160), maxL = vw - 160;`），与之矛盾的注释在 **app.js:699**「保证标题栏与右下角拉伸手柄始终留在可视区内」。
- **现象**：面板宽度 802 > `vw-160=1280` 时，`maxL` 允许左缘到 1280，于是右缘 = 1280+802 = **2082 > 视口 1440**，右下角 20×20 拉伸手柄完全在视口外，鼠标抓不到；此时连标题栏右侧的 ✕ 关闭按钮也点不到（脚本点 ✕ 时直接抛 "Node is either not clickable"）。标题栏左侧仍留 160px 可拖回，故可用性未完全丧失。
- **复现步骤**：打开 1440×1000 → 任一 tab 点开抽屉 → 右下角拉大面板 → 拖标题栏把鼠标拖到 (2000,1400) → 测量 `#drawerResize` 矩形。
- **证据**：`tools/_qa_ui_regression.txt` 第 24 / 35 / 46 行：`@(1280,86) 802x910 handleInView=false`（三种抽屉类型全部复现）。
- **建议修法**：横向上限同时满足「右缘（含手柄）在视口内」与「左侧至少留 160px 可抓」：`const maxL = Math.min(vw - 160, vw - w - 20)`（`w > vw - 20` 时退化为 `vw - w - 20`，即只允许左移）；或把 `#drawerResize` 改为相对视口固定（`position: fixed`）。

### UI-03 ｜ **P2** ｜ 接口少给字段时，前端把 `undefined` 直接渲染到页面；`counts` 整体缺失时还会抛错导致整块空白

- **位置**：`viewer/static/app.js:47`（`renderCards`：`<div class="n">${n}</div>` 无默认值 `?? 0`）；`viewer/static/app.js:1016`（`renderCards(s.counts)` 未做存在性校验）；同类：`app.js:248`（`renderPersons` 的 `${p.mention_count}`）。
- **现象**：① `/api/summary` 的 `counts` 少给键 → 卡片区直接显示 `undefined`（实测 6 处）；② `counts` 整个缺失 → `renderCards(undefined)` 抛 `Cannot read properties of undefined (reading 'raw_pages')`，且因抛错发生在同一个 `.then` 里，`renderSummary(s)` 被跳过，**汇总面板整块空白**；③ `/api/persons` 条目缺 `mention_count` → 表格「出现次数」列渲染 `undefined`（实测 2 处）。
- **复现步骤**：拦截 `/api/summary` 返回 `{counts:{posts:1,trades:2,news:3},...}` 或 `{login_status:[],...}`；拦截 `/api/persons` 返回 `[{name:"缺字段用户A"}]`。
- **证据**：`tools/_qa_ui_regression.txt` 第 96 行（`pageerror: Cannot read properties of undefined (reading 'raw_pages')`、`页面可见报错提示=false`）、第 133 行（`undefined 6 处`）、第 122 行（`undefined 2 处`；DOM 片段 `出现次数缺字段用户A来源未知作者undefined`）；截图 `tools/shots_ui/fail-_api_summary 缺 counts 字段_其余正常_.png`、`tools/shots_ui/fail-缺字段__api_summary.counts 少给 topics_person.png`、`tools/shots_ui/fail-缺字段__api_persons 条目缺 mention_count_role.png`。
- **建议修法**：`renderCards` 用 `(c && c.raw_pages) ?? 0`；`loadAll` 里对 `s.counts` 做 `s.counts || {}` 兜底；`renderPersons` 用 `p.mention_count ?? 0`。建议为所有渲染函数建立「字段 → 默认值」统一映射。

### UI-04 ｜ **P3** ｜ 话题缺 `weight` 时渲染出 `NaN` 与 `width:NaN%`

- **位置**：`viewer/static/app.js:273`（`Math.max(...list.map(t => t.weight), 1)`）、`app.js:275`（`热度 ${Math.round(t.weight)}`）、`app.js:276`（`width:${(t.weight / max * 100).toFixed(0)}%`）。
- **现象**：面板文字出现 `缺权重话题A热度 NaN`，进度条宽度为 `NaN%`（无效 CSS，进度条不可见）。
- **复现步骤**：拦截 `/api/topics` 返回 `[{"topic":"缺权重话题A"},{"topic":"缺权重话题B"}]`。
- **证据**：`tools/_qa_ui_regression.txt` 第 109 行；截图 `tools/shots_ui/fail-_api_topics 条目缺 weight 字段.png`。
- **建议修法**：`const w = Number(t.weight) || 0;` 后统一用 `w`；`max` 至少为 1。

### UI-05 ｜ **P3** ｜ `/api/persons` 条目缺字段时把 `undefined` 写进 DOM

- 见 UI-03 ③（同一根因，位置 `app.js:248`）。此处单列以便按接口追踪。
- **证据**：`tools/_qa_ui_regression.txt` 第 122 行。
- **建议修法**：`${p.mention_count ?? 0}`、`esc(p.role ?? "")`。

### UI-06 ｜ **P3** ｜ `.stk-name` / `.box .t` 未设 `overflow-wrap`，超长无空格 token 会撑破页面（本条 FAIL 判定：断言过严 + 理论缺口）

- **位置**：`viewer/static/app.css:189`（`.stk-name`）、`app.css:50`（`.box .t`）——均无 `word-break`/`overflow-wrap`；对照 `app.css:197` 的 `.stk-corp` **已设** `word-break: break-word`。
- **现象**：注入 160 个连续 `A`（无空格）到 `.stk-name` → 页面 `scrollWidth` 1440 → **2210**（横向溢出 770px）；同样注入 `.stk-corp` → 1440（正常换行）。超长中文（真实场景）→ 1440（正常）。
- **判定说明（重要）**：这条断言**故意用了真实数据不可能出现的极端输入**（股票简称永远是 ≤4 字中文），因此**属我断言过严**；但它**确实暴露了 CSS 防御缺口**：任何无空格长 token（例如海外新闻里被塞进标题的长 URL/长英文串）落到 `.stk-name`/`.box .t` 上都会撑破卡片。**未修改该断言**。
- **复现步骤**：见 `tests/ui_full_regression.js` 第四段（DOM 注入，仅测试页内生效，测完即恢复原文本）。
- **证据**：`tools/_qa_ui_regression.txt` 第 65 行（FAIL，`scrollWidth→2210`）与第 66 行（`.stk-corp` PASS）。
- **建议修法**：`.stk-name, .box .t, .ov-hs-title { overflow-wrap: anywhere; }`（低成本、无副作用）；或加 `min-width: 0` + `text-overflow: ellipsis`。

### UI-07 ｜ **P3** ｜ `/api/sentiment` 若返回 JSON 数组（载荷类型不符），舆论情绪面板整块空白并抛错

- **位置**：`viewer/static/app.js:260`（`const map = (d.map || []).map(...)`）。
- **现象**：当 `d` 是数组时，`d.map` 取到 `Array.prototype.map`（真值），再对函数对象调用 `.map()` → `TypeError: (d.map || []).map is not a function`，`renderSentiment` 中断，面板内容为空，无任何用户提示。
- **复现步骤**：拦截 `/api/sentiment` 返回 `[{"symbol":"600519"}]`。
- **证据**：`tools/_qa_ui_regression.txt` 第 146 行（`pageerror 1 条`、`舆论情绪面板 textContent="<空>"`）；截图 `tools/shots_ui/fail-类型不符__api_sentiment 返回 JSON 数组.png`。
- **备注**：当前服务端 `/api/sentiment` 恒返回 dict（`viewer/server.py:212-225`），故为**鲁棒性缺陷、非日常路径**；但同类写法在 `renderOverseasSentiment` 等处也存在（`app.js:459` 有 `!d.total` 守卫，相对安全）。
- **建议修法**：`const map = Array.isArray(d && d.map) ? d.map : [];`；或入口统一 `if (!d || typeof d !== "object" || Array.isArray(d)) { 显示空态; return; }`。

### DC-01 ｜ **P1** ｜ 示例数据与真实数据可混流，且行级无区分标记；标记会被抓取覆盖

- **位置**：
  - 国内：`viewer/demo_data.py:274-278`（`load_demo` 先 `DELETE` 各业务表）+ `viewer/demo_data.py:381-382`（只把标记写进 `meta`：`demo=1`、`last_ingest=示例数据`）；而真实抓取是**追加**：`viewer/ingest.py:277-299`（`save_trade`/`save_post` 无清表），并在 `viewer/ingest.py:317` **覆盖** `meta.last_ingest`。
  - 海外：`overseas/collect.py:143-155`（`include_seed=True` 时**恒定**把 `overseas/seed.py` 的 `SEED_NEWS` 追加进 `overseas_news`，与真实抓取条目同表）。
- **现象/结论**：
  1. **能混显**。国内：`载入示例` 之后若执行 `抓取并更新`，真实帖子/交易会与示例帖子/交易**留存在同一张表**，且 `posts`/`trades`/`positions` **没有任何行级来源列**（`viewer/db.py:66-153` 建表语句中无 `is_seed`/`source_kind`），事后无法区分。
  2. **标记会丢**。唯一标记 `meta.demo` / `meta.last_ingest` 不在接口暴露（`/api/summary` 只回 `last_ingest`），且真实抓取会把 `last_ingest` 覆盖成 `taoguba:all:{...}` → 「示例」提示消失。
  3. **海外恒定混流**。`overseas_news` 707 条 = **32 条 `kind='seed'`**（`source_name` 前缀「示例源·」，界面 `srcLink` 会打「示例」灰标，**可辨**）+ **9 条 CN 交叉比对示例**（`overseas/seed.py:173-197` 与 `:274-293`，`kind='domestic_crosscheck'`、`source_name='东方财富股吧'/'雪球'/'同花顺'`，与**真实** CN 条目完全同 kind/同 source_name，**只有 URL 的 `example.local` 能区分**）+ 666 条真实。
  4. **会污染统计口径**。净情绪指数（`net_mood=-5.9`）、`distribution`、`region_dist`、按日趋势 `overseas_sentiment_daily`、`overseas_hotspots.heat` 全部**不过滤示例条目**（`viewer/server.py:244-284` 直接全表聚合）。示例占比 41/707 = **5.8%**。
- **证据**：`tools/_qa_ui_db.txt` 第 22-34 行、第 57-63 行（`kind` 分布、`示例源·` 32 条、`example.local` 41 条）；`tools/_qa_ui_api.json` 的 `counts.overseas_news=707`、`crosscheck.overseas_total=707`。
- **建议修法**：① 为 `posts/trades/positions/players/overseas_news` 增行级 `origin` 列（`real`/`demo`/`seed`），`load_demo` 写 `demo`、抓取写 `real`、海外 seed 写 `seed`；② 所有统计接口默认 `WHERE origin='real'`，前端提供「含示例」开关；③ CN 交叉比对示例改用 `source_key='seed_cn_*'`，不要复用真实源 key。

### DC-02 ｜ **P1** ｜ 时间字段缺失时被填成「采集那一刻」，实测污染按日情绪趋势（今日被抬高约 8.6 倍）

- **位置**：
  - 海外新闻：`overseas/collect.py:55` → `"publish_time": item.publish_time or datetime.now().strftime(...)`；该字段是趋势图唯一日期来源：`overseas/collect.py:64-89`（`_daily_series`，`day = (r.get("publish_time") or "")[:10]`）→ 落库 `overseas_sentiment_daily`（`viewer/db.py:891-900`）。
  - 国内帖子：`scraper/parsers/taoguba.py:100` → `ptime = self._parse_time(...) if time_el else datetime.now()`，写出在 `scraper/parsers/taoguba.py:184`；影响 `/api/posts` 排序（`viewer/server.py:604-607`）与 `/api/stocks.last_time`（`viewer/server.py:97`）。
  - 相关潜在缺陷：`overseas/hotspots.py:417` → `min((r.get("publish_time") or "") for r in rows) or datetime.now()...`，「空串最小」使 `or` 兜底**恒真**（当前因 publish_time 已被上面填成 now 而未显形）。
- **现象（量化，本轮独立复现）**：
  - `overseas_news` 中 `publish_time` **精确等于采集时刻**（`21:53:45` = 75 条、`21:53:46` = 99 条）共 **174 条**，占真实（非 seed）675 条的 **25.8%**；落在「重复时间戳」中的条目共 **245/707**。
  - 按源看（前 5）：`nature 75`、`zaobao_world 29`、`zaobao_sea 27`、`zaobao_singapore 27`、`ths 16` → 这些源的整源条目挤在**同一天**（该源根本不产出时间戳）。
  - 对趋势图的影响：`overseas_sentiment_daily` **今日 total = 506**，昨日 = 59、前日 = 24 →**今日被抬高约 8.6 倍**，趋势曲线出现虚假尖峰。今日（`2026-09-22`）的 `publish_time` 共 **412/707 = 58.3%**。
  - 国内侧：当前 185 条 posts 全为示例（时间由示例生成器给出），该缺陷**暂未显形**，但代码路径成立（真实抓取时「无时间元素的帖子」会全部记成抓取时刻）。
- **证据**：`tools/_qa_ui_db.txt` 第 36-49 行（时间字段）、第 57-76 行（重复时间戳与今日 412）、第 78-92 行（今日 total 506 vs 昨日 59）；独立复核结论与既有 `tools/_qa_source_time.txt` 第 1-12 行一致（其统计同源 174 条）。
- **建议修法**：① 缺失时间写 `NULL`/空串，**不要**填 `now()`；② 趋势聚合跳过空时间（`_daily_series` 已 `if not day: continue`，故只需停止填 now 即可修复）；③ 前端对空时间显示「时间未知」而非伪造时间；④ `hotspots.py:417` 改为 `min((r.get("publish_time") or "") for r in rows if r.get("publish_time"))` 并处理空序列。

### DC-03 ｜ **P2** ｜「相关人物」实际只展示 100/170 人，界面文案却声称「不遗漏任何一个名称」

- **位置**：`viewer/server.py:157`（`SELECT name, mention_count, role FROM persons ORDER BY mention_count DESC LIMIT 100`）；界面文案 `viewer/static/app.js:250`（「名称与渠道一一对应，**不遗漏任何一个名称**」）。
- **现象**：`persons` 表 170 行，`/api/persons` 只返回 100 条 → **70 位人物在界面上不可见**，与文案承诺不符。
- **复现步骤**：`GET /api/persons` 长度 = 100；`SELECT COUNT(*) FROM persons` = 170。
- **证据**：`tools/_qa_ui_api.json` 的 `counts.persons=170` 与 `listLengths.persons=100`；`tools/_qa_ui_db.txt` 第 11、112 行。
- **建议修法**：二选一——① 后端支持分页/`limit` 参数并由前端分页展示；② 修改文案为「按提及次数降序展示前 100 位」。二者取其一，消除口径矛盾。

### DC-04 ｜ **P3** ｜ 数据源面板的「n 条」与实际入库数不一致（入库按 `(url,title)` 去重，且不更新 `source_key`）

- **位置**：`viewer/db.py:838-865`（`save_overseas_news` 的 `ON CONFLICT(url,title) DO UPDATE`，**UPDATE 列表不含 `source_key`/`source_name`**，`viewer/db.py:846-850`）；面板状态来自抓取返回值 `overseas/collect.py:157` + `viewer/db.py:873-889`。
- **现象**：同一篇文章被多源抓到时只保留最先写入的源；面板显示的是**去重前的抓取数**，必然 ≥ 实际入库数。实测差异（既有 `tools/_qa_source_time.txt` 第 55-86 行，本轮已核对去重机制确实排除 `source_key`）：联合早报（国际）面板 56 / 入库 29、CNBC 科技 30/21、CNBC 经济 30/25；反向少报 1 例：同花顺 面板 16 / 入库 19。
- **证据**：`tools/_qa_source_time.txt` 第 55-86 行；代码 `viewer/db.py:846-850`。
- **建议修法**：面板计数改用「本次实际入库（去重后）条数」，或把 `source_key/source_name` 一并 `DO UPDATE` 并改用「多源命中列表」字段。

### DC-05 ｜ **P3** ｜「近期海外新闻」因 `区域优先 + LIMIT 40` 只出现 US 条目

- **位置**：`viewer/server.py:271-275`（`ORDER BY CASE region WHEN 'US' THEN 0 ... END, heat DESC LIMIT 40`）。
- **现象**：`overseas_news` 中 US 有 303 条 ≥ 40，因此 `LIMIT 40` 被 US 全部占满，`/api/overseas_sentiment.news` 返回 **40 条全部 region=US**，HK/GLOBAL/CN 条目**永远不会出现在新闻流**里（面板下方还有「数据源构成」显示 HK/GLOBAL，前后自相矛盾）。
- **证据**：`tools/_qa_ui_api.json` 的 `crosscheck.overseas_news_regions={"US":40}`；`tools/_qa_ui_api_console.txt` 第 139-141 行。
- **建议修法**：改成按区域分组取 topN（如每区域 10 条）后再合并，或把 `LIMIT` 提到 120 并做区域配额。

### DC-06 ｜ **P3** ｜ 9 条 CN 交叉比对示例条目挂在真实源 key 之下，`kind`/`source_name` 均无法区分

- **位置**：`overseas/seed.py:173-197`、`overseas/seed.py:274-293`（`NewsItem("eastmoney_guba","东方财富股吧","CN","zh","domestic_crosscheck", ...)`、`"xueqiu_hot"/"雪球"`、`"ths"/"同花顺"`），与真实抓取共用同一 `source_key`/`source_name`/`kind`；`overseas/seed.py:8-9` 的注释声称「**不会**与真实抓取条目混淆」，实际不成立。
- **现象**：这批条目的 `kind='domestic_crosscheck'`、`source_name` 与真实 CN 源完全相同，**唯一可辨特征是 URL 为 `example.local`**（前端靠 `isPlaceholderUrl` 打「示例」灰标，`viewer/static/app.js:314-332`）。若前端某处不提供 URL，就彻底分不出来。
- **证据**：`tools/_qa_ui_db.txt` 第 58-60 行（`domestic_crosscheck: 25`、`示例源·: 32`、`example.local: 41` → 41−32=9 条为「无前缀的 CN 示例」）；代码 `viewer/static/app.js:314-332`。
- **建议修法**：同 DC-01 ① —— 给示例条目独立 `source_key`（如 `seed_cn_eastmoney`）或加 `origin` 列。

### DC-07 ｜ **P3** ｜ `overseas/hotspots.py` 的 `first_seen` 兜底恒真（潜在把示例/旧数据时间写成 now）

- **位置**：`overseas/hotspots.py:417` → `min((r.get("publish_time") or "") for r in rows) or datetime.now().strftime(...)`。
- **现象**：生成器里只要有**任意一条** `publish_time` 为空串，`min(...)` 必得 `""`（空串排序最小），`or` 立即命中 `datetime.now()` → `first_seen` 被写成重建时刻。当前因 `publish_time` 已被 DC-02 填成 now 而**未显形**（实测 17 条热点 `first_seen` 为 `2026-01-02 / 2026-04-13 / 2026-09-09 / 2026-09-14 / 2026-09-19` 等真实日期）。
- **证据**：`tools/_qa_ui_db.txt` 第 104-109 行（当前 `first_seen` 正常）；代码 `overseas/hotspots.py:417`（既有 `tools/audit_probe.py:60` 已标注同一写法）。
- **建议修法**：`vals = [r["publish_time"] for r in rows if r.get("publish_time")]`，`first_seen = min(vals) if vals else _now()`。

### DC-08 ｜ **P3（理论）** ｜ `/api/detail` 抽屉把对象/数组字段 `esc()` 成 `[object Object]`

- **位置**：`viewer/static/app.js:797`（`Object.entries(d).map(([k, v]) => ... esc(v) ...)`）。
- **现象**：若任一字段为对象/数组，`esc(v)` 走 `String(v)` → 渲染出 `[object Object]` 或逗号拼接串。**当前 `/api/detail` 返回全为标量**（`viewer/server.py:508-532`），故**未实际触发**。
- **证据**：代码静态分析；实测抽屉（详情/股票/人物/选手）均无脏字符（`tools/_qa_ui_regression.txt` 第 15/26/37 行）。
- **建议修法**：`esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v)`。

### DC-09 ｜ **P3（非缺陷，需知情）** ｜ `stock_mentions.symbol_name` 全表为空，靠接口层兜底

- **位置**：示例写入时显式给空串 `viewer/demo_data.py:342`（`save_stock_mention(pid, code, "", ...)`）；接口层补名 `viewer/server.py:109`（`_named(r["symbol"], r["symbol_name"] or "")`）。
- **现象**：`stock_mentions` 185 行的 `symbol_name` **全部为空**（`tools/_qa_ui_db.txt` 第 96 行），但 `/api/stocks`、`/api/sentiment` 因经 `_named()` 解析，界面 24/24 名称正常（`name_covered=24/24`，`name_missing` 全 false）。**用户不可见**，但若有其他消费方直接读表会拿到空名称。
- **证据**：`tools/_qa_ui_db.txt` 第 95-96 行；`tools/_qa_ui_api.json` 的 `sentiment_name_covered=24 / name_total=24`。
- **建议修法**：`save_stock_mention` 调用处统一走 `get_resolver(...).resolve(code)` 回填 `symbol_name`。

---

## 四、未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| 写路径功能验证（抓取并更新 / 载入示例 / 刷新海外舆情 / 登录检测 / 补全股票名称） | 硬性只读约束：这些按钮均为 POST 且会写库/联网，本轮明确禁止调用；其接口层正确性由 general-purpose-1（动态功能测试）与 general-purpose-2（安全专项）覆盖 |
| 真实数据下的 UI 表现（真实帖子/交易/持仓） | 当前库为纯示例态（185/185 posts 的 `post_url` 为 `example.local`），无真实抓取数据可测；真实数据相关的时间兜底缺陷只能做代码级判定（见 DC-02） |
| 「登录态可用」状态下的 UI 分支（`dot-ok`/`dot-pub` 等） | 当前 `login_status` 只有 `no_credential` 一种；其余状态需真实登录才产生 |
| 海外模块的专项目标断言（31 条） | 已由 `tests/ui_hotspot.js` 覆盖并 31/31 通过（`tools/_qa_ui.txt`），避免重复；本轮仅把两个海外 tab 纳入整体 tab/主题/空态回归 |
| 移动端（<1024px）与触屏手势 | 需求只给了 `1024×700` 与 `1440×1000` 两档；抽屉基于 `pointerdown` 事件（`app.js:690/717`），触屏未在真实设备验证 |
| 无障碍（键盘 Tab 焦点顺序、屏幕阅读器、`aria-*`） | 不在本轮范围内；仅顺带观察到 `#drawer` 有 `role="dialog"`/`aria-hidden` 且 Esc 关闭可用（12/12 PASS） |
| 性能（万级数据下的渲染耗时） | 不在本轮范围；当前数据量级（posts 185 / overseas_news 707）下交互无明显卡顿 |
| PDF/打印、浏览器兼容（Firefox/Safari） | 本机仅有 Chromium 可用；`color-mix()`（`app.css:215-216`）等新特性在旧浏览器可能失效 |

---

## 附：断言修正记录（透明披露，未「改断言让其变绿」）

本轮 FAIL 9 条，**逐条判定**如下；其中 3 次是我自己的断言/用例写错，已修正并在此说明——修正后相关项**仍如实报告**。

1. **[股票/人物/选手] 拖到右下极限后手柄仍在视口（3 条 FAIL）** → 判定：**代码缺陷**（UI-02）。保留 FAIL。
2. **超长无空格 ASCII 溢出 `.stk-name`（1 条 FAIL）** → 判定：**我的断言过严**（构造了真实数据不可能出现的 160 字符无空格 ASCII 简称）+ 揭示 CSS 理论缺口（UI-06）。**未修改断言**，如实保留 FAIL 并加注说明。
3. **`/api/summary` 缺 `counts` 无提示（1 条 FAIL）** → 判定：**代码缺陷**（UI-01/UI-03）。保留 FAIL。
4. **缺 `weight` → NaN（1 条 FAIL）** → 判定：**代码缺陷**（UI-04）。保留 FAIL。
5. **缺字段 `/api/persons` → undefined（1 条 FAIL）** → 判定：**代码缺陷**（UI-05）。**此处修过一次断言**：初版用 `document.body.innerText` 统计（`innerText` 会排除 `display:none` 的隐藏面板），导致该用例**假绿 PASS**；改用容器的 `textContent` 后如实变红。修的是「测量方式」，不是阈值。
6. **`summary.counts` 少给键 → undefined 6 处（1 条 FAIL）** → 判定：**代码缺陷**（UI-03）。**此处也修过一次断言**：容器选择用 `/persons/` 判名，而该用例名里本身含 `persons` 字样，误取了人物面板造成**假绿 PASS**；改为 `/\/api\/persons/` 后如实变红。
7. **`/api/sentiment` 返回 JSON 数组 → pageerror 白屏（1 条 FAIL）** → 判定：**代码缺陷**（UI-07，鲁棒性）。保留 FAIL。
8. **空态用例的 mock 修正（非断言阈值调整）**：初版把 `/api/sentiment` 也喂成 JSON 数组 `[]`，触发的是「类型不符」而非「空态」，属**用例设计错误**。已把空态 mock 改为该接口真实空形态 `{distribution:{},map:[],name_covered:0,name_total:0,legend:{}}`，并**另立独立用例**（第 7 项）专测类型不符。修正后空态 10/10 PASS，且类型不符缺陷仍被如实捕获。

### 产物清单

| 文件 | 说明 |
|---|---|
| `tools/_qa_ui.md` | 本报告 |
| `tools/_qa_ui_regression.txt` | UI 断言全文（80 条，PASS 71 / FAIL 9） |
| `tools/_qa_ui_regression_console.txt` | 同上的运行日志副本 |
| `tools/_qa_ui_api.json` | 读接口原始聚合（含 counts / 各列表长度 / 对账 / 样例） |
| `tools/_qa_ui_api_console.txt` | 对账摘要 |
| `tools/_qa_ui_db.txt` | DB 只读体检（行数 / meta / 示例-真实拆分 / 时间填充 / 缺失字段） |
| `tools/shots_ui/*.png` | 16 张证据截图（主题 4、视口 2、抽屉 1、空态 1、错误态 8） |
| `tests/ui_full_regression.js` | 本轮新增的只读 UI 回归脚本 |
| `tools/_qa_ui_probe.js`、`tools/_qa_ui_db.py` | 本轮新增的接口探针与 DB 体检脚本 |
| `tools/_qa_ui.txt` | **既有**热点卡 31/31 结论（本轮未改动、未覆盖） |
