# 看板 UI 交互回归报告（真实 Chromium / puppeteer-core）

- 被测实例：`http://127.0.0.1:8765`（PID 34192，`python -m viewer.server`，DB=`viewer/app.db`）
- 运行副本：`%TEMP%\uitest-snapshot-20260925-034141`（仓库 `tests/` 全量复制后从副本运行，仓库 `tests/` 一字未改）
- 脚本：`ui_qa_regression.js`（主回归）、`ui_qa_evidence.js`（证据采集）、`ui_qa_net.js`（网络健康度）
- 结果：**44 用例，PASS 41 / FAIL 3**（另有网络健康度 1 项 = PASS）
- 全程 `console.error=0`、`pageerror=0`、被拦截的破坏性请求 = 0

---

## 一、安全硬约束执行情况（自证）

| 项 | 实际 |
|---|---|
| 浏览器层请求拦截 | 对 `POST /api/demo`、`POST /api/overseas`、`POST /api/fetch` 一律 `req.abort()` |
| 本轮实际被 abort 的请求数 | **0 条**（说明脚本从未触发这些接口） |
| `window.confirm` | 覆盖为恒返回 `false`，调用次数 = **0** |
| `#btnDemo` | **DOM 中已不存在**（`viewer/` 全目录 grep `btnDemo` 0 命中，"载入示例"入口已被移除）→ 最大风险面消失 |
| `#btnOverseas` / `#btnFetch` / `#btnRefreshNow` | 存在但全程未点击；拦截器已兜住 |
| `#btnRefreshNow` 真实行为 | 点击即 `POST /api/fetch`（app.js:2054 写库）；已在拦截名单内 |
| `#btnOverseas` 真实行为 | `POST /api/overseas {live:true,reset:true,confirm:true}`（app.js:2107 清空海外侧）；已在拦截名单内 |

### 跑前 / 跑后四表计数（必须完全一致）

| 表 | 跑前 | 跑后（全部 3 次运行结束后） | 一致 |
|---|---|---|---|
| trades | 294 | 294 | ✅ |
| posts | 985 | 985 | ✅ |
| players | 68 | 68 | ✅ |
| news | 7251 | 7251 | ✅ |
| overseas_news（附加） | 701 | 701 | ✅ |
| overseas_hotspots（附加） | 17 | 17 | ✅ |
| positions（附加） | 0 | 0 | ✅ |

服务进程 PID 34192 **仍存活**（未杀未重启）。

---

## 二、回归结果明细

### A. 导航（左栏 5 大类 + 内容区 16 子类逐一点遍）

| 用例 | 期望 | 实际 | 判定 | 证据 |
|---|---|---|---|---|
| N0 一级导航 | 5 个大类 | 总览(1) / 知识获取(3) / 行情研判(5) / 实盘操作(5) / 跟我学习(2) | PASS | `shots/theme-*.png` |
| N1×16 每个子类 | 面板 `active` + 有内容 + 无报错 | 16/16 均 `active=true`，`htmlLen` 最小 1405（学习轨道管理）、最大 115369（正在操作股票） | PASS | 见 `qa_report.md` 第 5–20 行 |
| N2 子类覆盖 | ≥16 个 | 16 个：汇总 / 数据源 / 原始存档 / 抓取与登录记录 / 当前讨论股票 / 舆论情绪 / 近期热点 / 美股港股舆情 / 国外驱动热点 / 正在操作股票 / 实盘选手(渠道) / 共同持仓共识 / 近5日共同买入 / 相关人物 / 中短线炒股 / 学习轨道管理 | PASS | — |
| NET 网络健康度 | 无 404 / 无失败请求 | 16 个子类 + 首页刷新共 **44 条请求，0 条 4xx/5xx，0 条 requestfailed** | PASS | `net.json` |

> 结论：**没有白屏、没有 404、没有 JS 报错**。上次的导航类问题已消除。

### B. 空 `positions`（持仓=0）容错

| 用例 | 期望 | 实际 | 判定 | 证据 |
|---|---|---|---|---|
| C1 共同持仓共识 | 空持仓下仍渲染 3 行、无 undefined/NaN | 行数=3，含 undefined=false，含 NaN=false | PASS | `shots/consensus.png` |
| C2 共识标的与人数 | 000001/4、300750/4、601318/2 | `000001:4 人 / 300750:4 人 / 601318:2 人` | PASS | 同上 |
| C5 近5日共同买入 | 空窗口不崩、有明确空态 | 文本长 771，`.empty` 空态=true，`.coh-bar` 窗口条=true，窗口 `2026-09-20~2026-09-24`（锚定数据最新日） | PASS | — |
| P1 operating | 空持仓下无 undefined/NaN/null | 200 行，三者均 false | PASS | — |
| P1 players | 同上 | 68 行，三者均 false | PASS | — |
| P1 stocks | 同上 | 卡片正常，三者均 false | PASS | 附注：提示"当前有 2 个代码未识别名称"（属数据补全，非 UI 缺陷） |
| P1 summary | 同上 | 20 行，三者均 false | PASS | — |

### C. `/api/consensus` 新实现的前端渲染

服务端实测返回 3 只（字段齐全：`symbol/symbol_name/company/market/holders/weighted_holders/tiers/platforms/channels/sources/discuss_heat/players`）。
`channels` 恒为 `[]`（trades 兜底源把 channel 置 NULL），这正是下面两个 FAIL 的来源。

| 用例 | 期望 | 实际 | 判定 | 证据 |
|---|---|---|---|---|
| C3 行字段齐全 | 名称/公司/层级/平台/热度非空 | 平安银行 000001 平安银行股份有限公司 加权1.8 层级D 平台1 热度119；宁德时代 300750 …加权1.8 热度57；中国平安 601318 …加权0.9 热度44 | PASS | 空单元格 3 个（即 C6） |
| **C6 「覆盖渠道」列** | 渠道缺失应有占位（如 `—`/`渠道未标注`） | **3/3 行该列为空白格** | **FAIL** | 见下方定位 |
| **C4 「持仓选手明细」空角标** | 不应出现无文字空 badge | **10 个空 badge（4+4+2）**，实测尺寸 16×4px，`bg=rgb(254,243,199)` | **FAIL** | `shots/evidence-empty-badge.png`（红框标出） |

### D. 4 套主题

| 用例 | 实际 | 判定 | 证据 |
|---|---|---|---|
| T0 选择器 | 4 个色块 chip：light-tech / dark-dashboard / minimal-report / card-warm | PASS | 备注：色块无文字标签，仅 `title` 提示，弱可用性（非 BUG） |
| T1 light-tech | href 正确，bg `rgb(250,247,240)`，最低对比度 4.1，无重叠，0 报错 | PASS | `shots/theme-light-tech.png` |
| T1 dark-dashboard | bg `rgb(11,14,20)`，最低对比度 5.27，无重叠，0 报错 | PASS | `shots/theme-dark-dashboard.png` |
| T1 minimal-report | bg `rgb(255,255,255)`，最低对比度 4.34，无重叠，0 报错 | PASS | `shots/theme-minimal-report.png` |
| T1 card-warm | bg `rgb(255,247,237)`，最低对比度 3.75，无重叠，0 报错 | PASS | `shots/theme-card-warm.png` |

> 四套主题均**无文字看不清、无配色冲突、无元素重叠**（对比度采样均 ≥3.75，远高于阈值 2.5）。

### E. 示例/真实角标误标检查

| 用例 | 期望 | 实际 | 判定 | 证据 |
|---|---|---|---|---|
| E1 summary / stocks / players / consensus | 文本「示例」角标 = 0 | 均为 0 | PASS | — |
| E1 operating | 文本「示例」角标 = 0 | chip 总数 200，其中文本「示例」=**0**、「真实」=200 | PASS | 横幅：`真实抓取 200 条 / 示例 0 条` |
| **S1 真实标签套用「示例」样式** | 「真实」不应使用 `.chip-demo` | **200 个「真实」角标带 `class="chip-s chip-demo"`**，实渲为琥珀底 `rgb(254,243,199)` + 虚线框 `#FBBF24` | **FAIL** | `shots/evidence-real-chip.png` |

> 明确结论：**不存在"把真实数据标成示例"的误标**（数据文本全对）。但存在反向的**样式语义冲突**（S1）。

### F. 抽屉交互

| 用例 | 期望 | 实际 | 判定 | 证据 |
|---|---|---|---|---|
| D1 点击共识行开抽屉 | 弹出 + 有内容 + 无 undefined/NaN | `shown=true`，文本长 464，无异常字样 | PASS | `shots/drawer-consensus.png` |
| D2 Esc 关闭 | 关闭 | true | PASS | — |

> 附带佐证 C6：同一个"覆盖渠道"字段，**抽屉里显示 `覆盖渠道 —`（有占位），主表格里却是空白**——前后端两处口径不一致。

---

## 三、FAIL 项：复现步骤 + 定位 + 根因

### FAIL-1（C4）共识「持仓选手明细」渲染 10 个无文字空角标

- **复现**：打开首页 → 左栏「实盘操作」→ 子标签「共同持仓共识」→ 看「持仓选手明细」列，每名选手名前的渠道角标是一个内无文字的琥珀色小色块（红框见 `shots/evidence-empty-badge.png`）。
- **定位**：`viewer/static/app.js:960`
  ```js
  `<div class="chip-row"><span class="badge ch-${escAttr(pl.channel || 'A')}">${esc(pl.channel)}</span> ${esc(pl.player)} ...`
  ```
- **根因**：`/api/consensus` 的 trades 兜底源（`viewer/db.py:759` 起 `NULL AS channel`）使 `pl.channel === null`；
  `esc(null)` 返回空串（`app.js:29` 的 `s ?? ""`），于是渲染出 `<span class="badge ch-A"></span>`。
  `ch-*` 类自带背景色（`app.css:207`），空内容 inline-block 只剩 padding → 16×4px 的可见色块。
- **对照**：同文件 `channelBadge()`（`app.js:812`）对 null 有兜底（输出"渠道未标注"），但此处未复用。
- **建议**：把该 span 改为复用 `channelBadge(pl.channel)`，或对 `pl.channel` 为空时整段不渲染。

### FAIL-2（C6）共识「覆盖渠道」列 3/3 行为空白格

- **复现**：同上页面，「覆盖渠道」列标题下无可视内容（table 空单元格）。
- **定位**：`viewer/static/app.js:970`
  ```js
  <td>${toArr(x.channels).map(channelBadge).join(" ")}</td>
  ```
- **根因**：`channels` 为空数组时 `.map().join()` 得到空串，`<td></td>` 完全留白；而 `channelBadge()` 的空值兜底只对"数组内含空元素"生效，对"空数组"不生效。
- **对照**：抽屉对同一字段渲染为 `覆盖渠道 —`（见 D1 证据），同一数据两种呈现。
- **建议**：空数组时输出 `—` 或"渠道未标注"，与抽屉口径统一。

### FAIL-3（S1）「真实」角标套用 `.chip-demo`（示例）样式类

- **复现**：左栏「实盘操作」→「正在操作股票」，200 行每行选手名后都有一个**琥珀底 + 虚线框**的小 tag，文字为"真实"。
- **定位**：`viewer/static/app.js:664`
  ```js
  <td>${esc(t.author)} <span class="chip-s chip-demo">${demo ? "示例" : "真实"}</span>…
  ```
  样式来源：`viewer/static/app.css:199` 与 `viewer/static/app.css:646-648`（注释原文："示例数据 chip（用在表格行内、紧贴选手名字）"，`background:#FEF3C7!important; border-color:#FBBF24!important`）。
- **根因**：类名 `chip-demo` 被同时用于"示例"与"真实"两种语义，视觉语言（琥珀/虚线）专属于"示例"。
- **实测**：`text="真实"`，`cls="chip-s chip-demo"`，`bg=rgb(254,243,199)`，`border=1px dashed rgb(251,191,36)`。
- **影响**：数据文本无误（未误标数据），但"真实"被画成"示例"的样式，用户扫一眼颜色容易误判成示例数据。
- **建议**：真实态换用中性类（如 `chip-real`，蓝/绿实底），仅 demo 态保留 `chip-demo`。

---

## 四、附带观察（非 FAIL，供参考）

1. 主题选择器是 4 个纯色块、无文字标签，仅 `title` 悬停提示；对不熟悉的用户辨识成本偏高。
2. 「当前讨论股票」面板提示"当前有 2 个代码未识别名称"，属名称补全数据缺口，不是渲染缺陷。
3. 汇总卡片「持仓记录 0」在空持仓下正常显示（`shots/theme-*.png`）。

## 五、产物清单（均在快照目录 `%TEMP%\uitest-snapshot-20260925-034141`）

- `ui_qa_regression.js` / `ui_qa_evidence.js` / `ui_qa_net.js` —— 三个只读脚本
- `qa_report.md` / `qa_result.json` —— 44 用例结构化结果
- `net.json` —— 网络健康度（44 请求，0 个 4xx/5xx）
- `evidence.json` —— 空 badge 尺寸、真实角标配色实测
- `shots/` —— `consensus.png`、`theme-{4}png`、`drawer-consensus.png`、`evidence-empty-badge.png`、`evidence-real-chip.png`、`evidence-channel-col.png`
