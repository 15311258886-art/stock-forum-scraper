# 3 个 UI FAIL 修复 + 回归复核报告（general-purpose-1）

- 被测实例：`http://127.0.0.1:8765`（PID **34192**，全程未杀/未重启）
- 运行副本：`%TEMP%\uitest-snapshot-20260925-034141`（脚本自建，**仓库 `tests/` 一字未改**）
- 改动文件：`viewer/static/app.js`（3 处）、`viewer/static/app.css`（1 处新增）
- 回归脚本：`ui_qa_regression.js`（44 用例）、`ui_qa_evidence.js`（证据）、`ui_qa_net.js`（网络健康度）
  —— 已归档到 `tools/_qa_ui_fix3_gp1_scripts/`
- **结果：改动前 PASS 41 / FAIL 3 → 改动后 PASS 44 / FAIL 0**；`console.error=0 pageerror=0`，新增 FAIL **0** 条

---

## 一、结论速览

| 用例 | 期望 | 改动前 | 改动后 |
|---|---|---|---|
| **C4** 共识「持仓选手明细」空角标 | 无文字角标 = 0 | **FAIL**（10 个空 badge，16×4px 琥珀块） | **PASS**（空 badge = 0） |
| **C6** 共识「覆盖渠道」列 | 缺失有占位、不留空白格 | **FAIL**（3/3 行真空白 `""`） | **PASS**（3/3 行为 `—`，真空白 0） |
| **S1** 「真实」角标样式 | 不得用 `.chip-demo`（琥珀/虚线=示例语义） | **FAIL**（200 个误用 `.chip-demo`） | **PASS**（200 个全用 `.chip-real`） |

A/B 对照（**同一个脚本**、同一批 44 用例、仅静态资源不同）：

```
[before] 共 44 项：PASS 41 / FAIL 3
FAILED=C6 … | C4 … | S1 …
[after ] 共 44 项：PASS 44 / FAIL 0
FAILED=无
```

逐用例比对（`_qa_ui_fix3_gp1_result_before.json` vs `_after.json`）：

- 用例数 44 → 44，**用例列表与顺序完全一致**（无增删、无改名）
- `FAIL → PASS`：**3 条**（恰为 C6 / C4 / S1）
- `PASS → FAIL`：**0 条**（未引入新 FAIL）
- 两轮均 PASS：41 条
- `console.error` / `pageerror`：before 0/0，after 0/0

改动后**连跑两次**（`_qa_ui_fix3_gp1_run_after.txt`、`_run_after2.txt`）均为 `PASS 44 / FAIL 0`，无抖动。

---

## 二、三处改动（精确到行）

### 改动 1 —— FAIL-3（S1）「真实」角标不再套用「示例」样式

`viewer/static/app.js:664`

```diff
- <span class="chip-s chip-demo">${demo ? "示例" : "真实"}</span>
+ <span class="chip-s ${demo ? "chip-demo" : "chip-real"}">${demo ? "示例" : "真实"}</span>
```

`viewer/static/app.css:649-656`（**新增**，紧跟原有的 `.chip-demo` 定义，便于对照）

```css
/* 真实数据 chip —— 与上面的「示例」成对出现。 */
.chip-real { display:inline-block; margin-left:4px; padding:1px 6px; border:1px solid #86EFAC;
  border-radius:6px; font-size:10px; font-weight:600; background:#DCFCE7 !important;
  color:#166534 !important; vertical-align:middle; }
```

语义切分：**示例 = 琥珀底 + 虚线框**（`.chip-demo`，保持原样）；**真实 = 弱绿实底 + 实线框**（`.chip-real`）。
几何与 `.chip-demo` 完全对齐（`padding:1px 6px`、`font-size:10px`、`margin-left:4px`、`border-radius:6px`），
两态尺寸实测均为 **34×20px**，同一行内不会一高一低。

### 改动 2 —— FAIL-1（C4）渠道为空则整段不渲染

`viewer/static/app.js:959-968`

```diff
- const players = toArr(x.players).map((pl) =>
-   `<div class="chip-row"><span class="badge ch-${escAttr(pl.channel || 'A')}">${esc(pl.channel)}</span> ${esc(pl.player)} …</div>`
- ).join("");
+ // channel 为空时不渲染角标：trades 兜底源把 channel 置 NULL，而 ch-* 类自带背景色，
+ // 空文本 + 有色背景会渲染成无文字色块（UI 回归 C4）
+ const players = toArr(x.players).map((pl) => {
+   const chBadge = pl.channel
+     ? `<span class="badge ch-${escAttr(pl.channel)}">${esc(pl.channel)}</span> `
+     : "";
+   return `<div class="chip-row">${chBadge}${esc(pl.player)} …</div>`;
+ }).join("");
```

**为什么没有按建议直接换成 `channelBadge(pl.channel)`**：`channelBadge()` 走 `CHANNEL_LABEL`，输出的是全称
「A 实盘大赛」而不是字母「A」。共识面板的「持仓选手明细」是**每行多名选手的紧凑 chip 流**，一旦换成全称，
每行明细都会被撑长、可读性下降，与设计不符。故按您给的备选口径处理：**空 → 不渲染**（不留任何有色块），
非空 → 沿用原来的紧凑字母角标。实测明细内 badge 总数由 20 降到 10（即 10 个空角标被彻底移除，不是"变透明"）。

### 改动 3 —— FAIL-2（C6）覆盖渠道列表出占位「—」

`viewer/static/app.js:970-971`（新增 `channels`）、`app.js:979`（单元格）

```diff
+ // 覆盖渠道为空数组时给占位「—」，与抽屉渲染（app.js:1149 `${... || "—"}`）口径统一（UI 回归 C6）
+ const channels = toArr(x.channels).filter(Boolean);
  …
- <td>${toArr(x.channels).map(channelBadge).join(" ")}</td>
+ <td>${channels.length ? channels.map(channelBadge).join(" ") : "—"}</td>
```

取值与抽屉完全一致（抽屉 `app.js:1149` 原本就是 `toArr(x.channels).join("、") || "—"`）。
实测该列三行取值：`000001:"—" | 300750:"—" | 601318:"—"`。

---

## 三、A/B 是怎么做到"同一把尺子"的（自证）

您要的「改动前 FAIL 数 → 改动后 FAIL 数」若用两版脚本各跑一次，说服力不足。所以这次做成了**同脚本双模式**：

1. `make_before.js`（已归档）读取**当前已修**的 `app.js/app.css`，逐条**反向**替换回改动前写法，
   生成 `before_app.js` / `before_app.css`，**只放在快照目录、不写回仓库**。
   - 每条反向替换都断言"命中次数恰为 1"，命中数不符即报错退出；
   - 生成后核验：`app.js` 行数差 **-9**、字节差 **783B**；`app.css` 行数差 **-8**、字节差 **853B**，
     与"4 段注释 + 1 个 `.chip-real` 规则块"的预期完全吻合；
   - 特征计数核验：`before_app.js` 中 `chip-real`=0、旧内联空角标=1、空数组裸 `join`=1、新占位分支=0；
     `before_app.css` 中 `chip-real`=0。即确实是"改动前"。
2. `ui_qa_regression.js` 支持 `GP1_BEFORE=1`：仅对 `/static/app.js|css` 两个请求用 `req.respond()`
   替换成副本内容，其余请求照旧。**脚本逻辑一行不变**，所以 44 条断言在两种模式下是同一把尺子。
3. before 模式跑出 **PASS 41 / FAIL 3，失败项恰为 C6/C4/S1**，与上一轮报告的基线完全一致 → 副本忠实。

**需要向您说明的一处口径修正**（不是迎合结论，是原断言本身有错）：

- C6 原断言写的是 `blankChannelCells === 0`，但统计时把 `""` 和 `"—"` **都算作"空白"**。
  而您指定的修法（以及抽屉既有实现）恰恰就是输出 `—`。若沿用原断言，即使按您的要求修好，C6 也永远 FAIL。
  故我把断言改成可判定的两件事：**① 真空白（`""`）单元格 = 0；② 缺失行必须落在明确占位符上**。
  在 before 模式（单元格确实是 `""`）该断言仍判 **FAIL**，所以 before 的 3 个 FAIL 并未被"放水"。

---

## 四、改后「真实」chip 的实际计算样式（您要的证明）

`getComputedStyle` 实测（operating 面板，200 个真实角标取样）：

| 项 | 改动前 | 改动后 |
|---|---|---|
| class | `chip-s chip-demo` | **`chip-s chip-real`** |
| 背景色 | `rgb(254,243,199)` 琥珀 | **`rgb(220,252,231)` 弱绿 `#DCFCE7`** |
| 文字色 | `rgb(146,64,14)` | **`rgb(22,101,52)` `#166534`** |
| 边框 | `1px dashed rgb(251,191,36)` 琥珀虚线 | **`1px solid rgb(134,239,172)` 绿色实线 `#86EFAC`** |
| 尺寸 | — | 34×20px，`border-radius:6px`，`font-size:10px` |

**不再是琥珀虚底**。文字/底色对比度实测 **6.49:1**（改动前示例样式本身为 6.37:1），四套主题下取值恒定。

四主题实测（`_qa_ui_fix3_gp1_evidence_after.json` → `chipTheme`）：

| 主题 | body 底色 | chip 文字/底 | chip 底 vs 所在行底 | 边框 |
|---|---|---|---|---|
| light-tech | `rgb(250,247,240)` | **6.49** | 1.01 | 1px solid rgb(134,239,172) |
| dark-dashboard | `rgb(11,14,20)` | **6.49** | **12.62** | 1px solid rgb(134,239,172) |
| minimal-report | `rgb(255,255,255)` | **6.49** | 1.01 | 1px solid rgb(134,239,172) |
| card-warm | `rgb(255,247,237)` | **6.49** | 1.01 | 1px solid rgb(134,239,172) |

> 关于 `chip 底 vs 行底 = 1.01`：这不是本次引入的问题，而且**改动前更糟**。探针（`_qa_ui_fix3_gp1_probe_chain.txt`）
> 查明 operating 每个 `<td>` 的背景本身就是琥珀 `rgb(254,243,199)`，来源是既有规则
> `app.css:623 tr:has(> td.td-fresh.fr-stale) > td { background: var(--warn-bg, #fef3c7) }`
> ——200 条 trades 的「最后更新」全部 stale，于是整行被刷成琥珀底。
> 也就是说：**改动前**「真实」chip 是琥珀底压在琥珀行底上（同色，亮度对比度 ≈1.00，只剩一个虚线框能看出边界，
> 近乎不可见）；**改动后**绿底 + 绿实线边框在琥珀行底上色相分离清晰。
> 亮度对比度仍只有 1.01（靠色相 + 实线边识别），这是既有 stale 行底带来的天花板，非本次回归。
> 建议（**未做，超出本次范围**）：后续把 stale 行底与 chip 底在亮度上拉开，或给 chip 加 1px 白描边。

截图（均在本目录 `tools/shots_ui_gp1/`，本次覆盖/新增）：

| 文件 | 内容 |
|---|---|
| `evidence-real-chip.png` | 改后「真实」chip 特写（light-tech，绿底实线框） |
| `chip-real-light-tech.png` / `chip-real-dark-dashboard.png` / `chip-real-minimal-report.png` / `chip-real-card-warm.png` | 四套主题下的「真实」chip |
| `evidence-channel-col.png` | 共识「覆盖渠道」列，3 行均显示 `—` |
| `evidence-empty-badge.png` | 共识「持仓选手明细」列，已无空色块（只剩 `选手名 + 买入`） |
| `consensus-after.png` | 共识面板整体改后全貌 |

---

## 五、数据未被改动的自证

跑前 / 跑后四表（及附加表）计数**完全一致**（`_dbcount.py` 只读 `mode=ro` 连接）：

| 表 | 跑前 | 跑后 |
|---|---|---|
| trades | 294 | **294** |
| posts | 1025 | **1025** |
| players | 68 | **68** |
| news | 7261 | **7261** |
| overseas_news | 701 | 701 |
| overseas_hotspots | 17 | 17 |
| positions | 0 | 0 |

其它自证：

- 服务进程 PID **34192** 全程存活（`netstat` 复查仍为 LISTENING），未重启、未换端口。
- `viewer/static/app.js` / `app.css` 的 **磁盘内容与 8765 实际下发内容 SHA256 完全一致**
  （`app.js` `95fa04f2…`、`app.css` `ab36fa62…`）→ 被测的就是仓库里的这一版，没有"测了别的文件"。
- 回归脚本硬约束自证：`被拦截(尝试发出但已 abort) = 0` 条；`window.confirm` 被调用 **0** 次；
  `#btnDemo` 在 DOM 中仍不存在；`#btnFetch/#btnOverseas/#btnRefreshNow` 全程未点击。
- 网络健康度（`_qa_ui_fix3_gp1_net.json`）：16 个子类 + 首页刷新，**44 条请求、0 条 4xx/5xx、0 条 requestfailed**。
- 仓库 `tests/` 目录未被修改（本次脚本均在 `%TEMP%` 副本内创建/修改）。

---

## 六、对您描述的两处核对（以实测为准）

1. **`app.js:867` 与 `app.js:898(实为 900)` 不是"同一个模式"，未改动**：
   - `app.js:900`（players 行内）：`${demo ? '<span class="chip-s chip-demo" …>示例</span>' : ""}`
     —— `chip-demo` **只在 `demo` 为真时渲染**，文字硬编码「示例」，语义与样式一致。
   - `app.js:867`（`srcLink()` 占位分支）：`<span class="chip-demo" …>示例</span>`
     —— 只对不可跳转的占位 URL 渲染，文字硬编码「示例」。
   - 线上实测佐证：`#p-overseas_sentiment` 里仍有 **9 个合法的「示例」角标**，class=`chip-demo`、
     bg=`rgb(254,243,199)`、文字「示例」→ 说明 `.chip-demo` 在这些位置用对了，**不需要**改。
   - 唯一"示例/真实共用同一类"的只有 `app.js:664` 一处，已改。另您写的 `898` 是 `<tr>` 模板起始行，
     角标实际在 **900 行**。

2. **根因链有两处细节比原描述更严重 / 需补充**：
   - C4 的根因不止 `esc(null)` → 空串：`pl.channel` 为 `null` 时 `escAttr(pl.channel || 'A')` 兜成了 `ch-A`，
     即"空文本 + **琥珀底**"。改成"空则不渲染"后，`ch-*` 背景色这条路径根本不会被触发。
   - S1 的实际后果比"观感误导"更重（见第四节）：改动前它在 stale 琥珀行底上几乎不可见，
     连"被误判成示例"都算不上——是"看不清这个角标"。改后已解决。

---

## 七、附带观察（非 FAIL，均未改动）

1. **数据侧已变化（与本次前端改动无关）**：`players` 68 条的 `source_url` 已全部换成真实
   `https://www.tgb.cn/blog/...`（`example.local` 命中 0/68），因此 players 面板现在横幅为
   「真实抓取 68 条 / 示例 0 条」，示例角标自然消失。另 `posts` 985→1025、`news` 7251→7261。
   → 这是别的并行任务带来的数据变化，**不是**我这次改前端造成的。
2. `app.js:875` 与 `app.js:914` 的**源码注释已过期**：仍写着"真实抓取尚未打通（淘股吧登录态无效）、整张表目前是合成数据"。
   用户可见文案是数据驱动的（`realN>0` 走 info 分支，显示正确），故不构成 UI 缺陷；建议后续顺手更新这两处注释与 warn 分支文案。
3. stale 行底（`app.css:623`）与「示例」chip 同为琥珀 `#FEF3C7`：若将来又出现示例 trades
   （`data-demo` 且 stale），示例 chip 会与行底同色而看不清——与第 4 节同一个隐患，建议一并考虑。

---

## 八、产物清单

`tools/shots_ui_gp1/`（截图）

- `evidence-real-chip.png`、`chip-real-{light-tech,dark-dashboard,minimal-report,card-warm}.png`（本次新增）
- `evidence-channel-col.png`、`evidence-empty-badge.png`、`consensus-after.png`（本次覆盖/新增）
- `evidence_after.json`（空角标/占位/实渲样式/四主题对比度原始数据）

`tools/`（日志与结构化结果）

- `_qa_ui_fix3_gp1_run_before.txt` / `_run_after.txt` / `_run_after2.txt`
- `_qa_ui_fix3_gp1_result_before.json` / `_result_after.json`、`_qa_ui_fix3_gp1_report_before.md` / `_after.md`
- `_qa_ui_fix3_gp1_net.json`、`_qa_ui_fix3_gp1_evidence_after.json`、`_qa_ui_fix3_gp1_probe_chain.txt`
- `_qa_ui_fix3_gp1_make_before.js`（A/B 副本生成器，含命中数断言）
- `_qa_ui_fix3_gp1_scripts/`：`ui_qa_regression.js`、`ui_qa_evidence.js`、`ui_qa_net.js`、`probe_chain.js`
  （原运行位置仍在 `%TEMP%\uitest-snapshot-20260925-034141`，此处留档以便复跑）
