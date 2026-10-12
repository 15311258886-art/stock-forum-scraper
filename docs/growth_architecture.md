# 架构设计｜复盘成长面板（growth）· 集中面板

> 架构师：高见远 ｜ 日期：2026-10-09 ｜ 关联 PRD：`docs/growth_prd.md`
> 工程根：`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`
> 设计依据：已逐行核实 `viewer/predictor.py`（表结构 + SIGNALS + payload 函数）、`viewer/server.py`（API 注册风格）、`viewer/static/app.js`（NAV / itemsOfCat / selectNav / ensurePanel / 手绘 SVG 惯例）、`config/kb_sources.yaml`（轨道字段）。

---

## 1. 实现方案 + 框架选型

### 1.1 核心结论
**完全沿用现有栈，零新增框架**。前端原生 JS + 手绘 SVG（不引图表库），后端 Python + SQLite（不引 ORM），回归测试用 Node `.cjs` + 内置 `assert`/`fetch`。新增的 `growth` 子面板与现有 `prediction` 面板平级，复用其 3 个只读 API；仅新增 1 个只读 API `/api/growth/adjust_log`。

### 1.2 为什么不再引图表库 / ORM
| 考量 | 结论 |
|---|---|
| 全站已验证零依赖、手绘 SVG 风格统一 | 引入图表库会破坏一致的「手绘」观感、增加体积 |
| 数据量极小（权重演化 ≤ 数百点、命中率逐日 ≤ 数百天） | 图表库属杀鸡用牛刀，原生 polyline 足够 |
| 现有建表惯例是 `executescript(DDL)` + `_migrate` 幂等加列 | ORM 会打破该轻量模式，且零 JOIN 隔离靠手写 SQL 更可控 |
| 隔离机制要求「新增表与现有表零 JOIN」 | 手写 SQL 能精确保证解耦，ORM 关系映射反而易产生隐式 JOIN |

### 1.3 架构分层（沿用现有）
- **数据层（predictor.py）**：在现有 `DDL` 字符串内追加 `knowledge_source` / `growth_notes` 两张预留表；新增 `growth_adjust_log_payload(con)` 纯读函数。
- **接口层（server.py）**：新增 `api_growth_adjust_log()` → 注册到 `_do_GET` 的 `if route ==` 链，沿用 `_send_json(api_growth_adjust_log())` 风格。
- **视图层（app.js）**：在 `NAV.learn` 固定子项里加 `growth`；在 `itemsOfCat` 的 `fixed` 数组显式并回；在 `selectNav` 加 `if (tab === "growth")` 分发；新增 `GROWTH` 状态对象 + `loadGrowth()` / `renderGrowth()` + 五个区渲染函数 + 手绘 SVG 辅助函数。面板容器由 `ensurePanel('growth')` 按需创建，**无需改 `index.html`**。

---

## 2. 文件列表（精确到相对路径）

### 修改
- `viewer/predictor.py` —— ① `DDL` 追加 `knowledge_source`、`growth_notes` 两张建表 SQL；② 新增 `growth_adjust_log_payload(con)` 函数（纯读）。
- `viewer/server.py` —— ① 新增 `api_growth_adjust_log()` 函数；② 在 `_do_GET` 路由链（`/api/prediction/track` 附近）注册 `/api/growth/adjust_log`。
- `viewer/static/app.js` —— ① `NAV.learn.items` 占位项加 `{tab:"growth",label:"成长总览"}`；② `itemsOfCat` 的 `fixed` 数组加 `growth`；③ `selectNav` 加 `growth` 分发；④ 新增 `GROWTH` 状态 + `loadGrowth/renderGrowth` + A/D/B/C/E 五个区渲染函数 + SVG 辅助。

### 新增
- `viewer/tests/growth_api.cjs`（或 `qa/growth_api.cjs`）—— 回归测试：覆盖 `/api/growth/adjust_log` 空表/有数据、零 JOIN 验证、A 区维度动态读取。

> 说明：`.cjs` 测试在仓库内目前不存在（已 Glob 全盘确认），本设计将「建立测试惯例」作为 T05 一并落地；若工程师发现既有测试目录，放入同目录即可。

---

## 3. 数据结构与接口

### 3.1 新增预留表 `knowledge_source`（隔离机制核心）

```sql
-- 隔离机制核心表：不同外部知识源/轨道理论独立存储、独立验证、独立开关。
-- 与 predictor_score / prediction_* 零 JOIN（无任何外键/关联查询）。
CREATE TABLE IF NOT EXISTS knowledge_source (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key    TEXT    NOT NULL,   -- 来源主键：某博主ID / 媒体名 / 内部轨道 doc_key（全局唯一语义）
  category      TEXT    NOT NULL,   -- technical_emotion | macro_strategy | internal_track（两类外部 + 内部轨道）
  track         TEXT,               -- 所属轨道，对齐 kb_sources.yaml 的 group/track
  name          TEXT,               -- 展示名（如「博主A·放量突破战法」）
  enabled       INTEGER DEFAULT 1,  -- 独立开关：1=启用 0=停用；停用仅置 0，绝不级联影响其它来源
  trust_score   REAL    DEFAULT 0.0,-- 该来源可信度评分（0~1）。验证结果【只写本列】，默认不回写 predictor_score.weight
  signal_key    TEXT,               -- 被信号化的目标信号（可空，如映射到已有 'extref'）
  promoted      INTEGER DEFAULT 0,  -- 是否已「提升权到信号权重」：仅当 =1（用户显式确认）才允许后续提权流程（P2）
  content_json  TEXT,               -- 论点/结论/方法 JSON 占位（P2 抓取后填充）
  updated_at    TEXT,
  created_at    TEXT
);
-- 隔离语义索引：同一来源在同一 category+track 下唯一；category / enabled 高频筛选
CREATE UNIQUE INDEX IF NOT EXISTS idx_ks_src ON knowledge_source(source_key, category, track);
CREATE INDEX IF NOT EXISTS idx_ks_cat     ON knowledge_source(category);
CREATE INDEX IF NOT EXISTS idx_ks_enabled ON knowledge_source(enabled);
```

**隔离落地纪律（代码注释级约束）**：
- `trust_score` 只在本表内读写；任何验证/回测逻辑**默认不得** `UPDATE predictor_score SET weight=...`。
- 唯 `promoted=1` 才允许经「用户二次确认 + 可回滚」流程回写 `predictor_score.weight`（P2 闭环，本期仅预留 `promoted` 字段与按钮占位）。
- 前端 C 区按 `source_key` 并排展示冲突理论，每条标注 `source_key` + `trust_score`，**绝不自动合并 / 加权平均**（写入渲染函数注释）。
- 本表与 `prediction_*` / `predictor_score` 间不允许任何 JOIN。

### 3.2 新增预留表 `growth_notes`（人工笔记，P1）

```sql
CREATE TABLE IF NOT EXISTS growth_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  section     TEXT,               -- 归属区标记（'A' | 'D' | 'B' | 自由），仅用于轻量归类
  content     TEXT,               -- 笔记正文
  created_at  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gn_sec ON growth_notes(section);
```
> 与 `learn_tracks` / KB 轨道**零耦合**（独立表）；本期仅建表 + 占位 UI，完整 CRUD 接口留 P1 轻量实现（见 §8 待确认 8）。

### 3.3 新增只读 API `/api/growth/adjust_log`

**请求**：`GET /api/growth/adjust_log`（无参数，纯读幂等）

**响应（JSON）**：
```json
{
  "ok": true,
  "has_data": true,
  "signals": [
    { "key": "tech", "name": "技术面", "icon": "◈", "kind": "signal" }
    /* 共 12 项，与 predictor.SIGNALS 完全一致——前端据此【动态】决定画几条权重线，绝不硬编码 6 */
  ],
  "weights_series": {
    "tech": [ { "log_date": "2026-09-01", "weight": 0.08 },
              { "log_date": "2026-10-01", "weight": 0.12 } ],
    "sentiment": [ { "log_date": "2026-09-05", "weight": 0.05 } ]
    /* 每个 signal_key 一条轨迹，来自 predictor_adjust_log 按 signal_key 分组、log_date 升序 */
  },
  "current_weights": { "tech": 0.12, "sentiment": 0.065 },
  "recent_adjusts": [
    { "id": 1, "log_date": "2026-10-08", "signal_key": "tech", "kind": "signal",
      "weight_before": 0.08, "weight_after": 0.12, "reason": "覆盖度上升，贝叶斯收缩收敛" }
    /* 最近 N=8 条 predictor_adjust_log，供 D 区「最近调整」 */
  ],
  "caliber": { "label": "权重只按 2024-10 起的近 2.0 年样本统计" }
}
```
- `has_data=false` 时：`signals` 仍返回 12 维（保证 UI 不崩），`weights_series={}`、`current_weights={}`、`recent_adjusts=[]`。
- 复用现有 `predictor.window_caliber()` 生成 `caliber.label`。

### 3.4 现有 API 复用说明（不改动）
| API | 用途 | 消费区 |
|---|---|---|
| `/api/prediction/track` → `track_payload` | 逐日 `hit_t1/t3/t5/t10` 命中率 | A 区右图（命中率轨迹，窗口可切） |
| `/api/prediction/scores` → `scores_payload` | `predictor_score` 按 kind 分组当前权重 | D 区上（权重条） |
| `/api/prediction/today` → `today_payload` | 当前预测/研判 + 权重快照 | D 区下（「今日若重跑」预览，**只读不重跑**） |
| `/api/prediction/status` | 引擎状态（可选） | 顶部状态条 |

---

## 4. 程序调用流程（文字时序）

```
用户点击 ▸ 复盘成长 ▸ 成长总览(growth)
  │
  ├─ selectNav('learn','growth',true)
  │     └─ ensurePanel('growth')        → 动态创建 #p-growth（无 index.html 改动）
  │     └─ if (GROWTH.data) renderGrowth()  else loadGrowth()
  │
  ├─ loadGrowth()  →  Promise.all([
  │       api('/api/growth/adjust_log'),   // T01/T02：权重演化时序 + 最近调整
  │       api('/api/prediction/scores'),   // 复用：D 区当前权重
  │       api('/api/prediction/track'),    // 复用：A 区命中率轨迹
  │       api('/api/prediction/today'),    // 复用：D 区「今日若重跑」预览
  │     ])
  │     └─ GROWTH.data = {...}; renderGrowth();
  │
  └─ renderGrowth()
        ├─ 画顶部五区 Tab：[A 判断进化] [D 校准自检] [B 教训沉淀*] [C 知识轨道*] [E 论点验证*]
        ├─ 默认激活 A：
        │     ├─ SVG① 权重演化曲线：遍历 signals（动态 12 维）→ 每维一条 <polyline>
        │     │     x=log_date, y=weight（weights_series + current_weights 拼接）；空 → 虚线/占位
        │     └─ SVG② 命中率轨迹：读 track.days[].all.t{w}，按当前窗口 T+1/3/5/10 切换
        ├─ 切到 D：
        │     ├─ 上：各信号当前权重条（scores，按 kind 分组）
        │     ├─ 中：最近调整（recent_adjusts：before→after + reason）
        │     └─ 下：「今日若重跑」预览卡（today_payload，标注「只读预览·未实际重跑」）
        ├─ B/C/E 占位：
        │     ├─ B：自动卡片占位 + 笔记编辑框（growth_notes 有数据才可用）
        │     ├─ C：knowledge_source 列表（两分类）+ 每源 [启/停开关] + [trust_score badge]
        │     │       有数据 → 按 source_key 并排、冲突理论标注来源与可信度（不合并）
        │     └─ E：论点验证闭环占位（按钮禁用态）
        └─ 空安全：任一 API 返回 has_data:false / 空数组 → 该区显示 .empty 占位，不报错不崩
```

---

## 5. 任务列表（有序、含依赖、按 P0/P1 标注）

> 说明：本面板改动集中在 `predictor.py`/`server.py`（后端）与 `app.js`（前端）。为贴合工程师实际落地，任务按「后端基础 → 前端框架 → 各功能区 → 测试」顺序编排；前端 T02 可与后端 T01 并行起步（字段契约已在 §3 锁定），但渲染函数（T03/T04）须等 API 契约落地后实施。

| ID | 任务 | 源文件 | 依赖 | 优先级 |
|---|---|---|---|---|
| **T01** | 后端数据模型 + 隔离表：① `DDL` 追加 `knowledge_source`、`growth_notes` 建表+索引；② 新增 `growth_adjust_log_payload(con)`（纯读，输出 §3.3 schema） | `viewer/predictor.py` | 无 | P0 |
| **T02** | 新增只读 API + 路由：① `api_growth_adjust_log()` 调 `predictor.growth_adjust_log_payload(DB.conn)`；② `_do_GET` 注册 `/api/growth/adjust_log` | `viewer/server.py` | T01 | P0 |
| **T03** | 前端导航接入 + 面板框架：① `NAV.learn.items` 占位加 `growth`；② `itemsOfCat` 的 `fixed` 数组显式并回 `growth`（防轨道加载后丢失）；③ `selectNav` 加 `growth` 分发；④ `GROWTH` 状态对象 + `loadGrowth()` + `renderGrowth()`（五区 Tab 骨架 + 空安全）；⑤ 手绘 SVG 辅助函数（viewBox 固定、`mx()/my()` 映射、调色板 `GROWTH_PALETTE` 12 色） | `viewer/static/app.js` | 无（可并行 T01） | P0 |
| **T04** | A 区判断进化轨迹：① 动态多维权重演化曲线（读 `weights_series`，维度数=signals.length，**绝不硬编码 6 线**）；② 命中率轨迹（读 `track.days`，T+1/3/5/10 切换）；③ 图例 + `caliber.label` 口径标注；④ 样本不足处理（<3 点虚线，<2 点占位） | `viewer/static/app.js` | T02, T03 | P0 |
| **T05** | D 区校准自检 + B/C/E 占位 + 隔离开关 UI：① D 区权重条(scores) + 最近调整(recent_adjusts) + 今日重跑预览(today，标注只读)；② B/C/E 占位卡；③ C 区 `knowledge_source` 列表渲染 + 每源 enabled 开关 + trust_score badge + 冲突并排（注释「不合并」）；④ 空态文案 | `viewer/static/app.js` | T02, T03 | P0 + P1 |
| **T06** | 回归测试（建立 `.cjs` 惯例）：覆盖 `/api/growth/adjust_log` 空表不崩、有数据维度动态、零 JOIN（不引用 prediction_* 任何列）、`has_data` 语义 | `viewer/tests/growth_api.cjs` | T02 | P0 |

> 任务数 6：因本面板实际跨「数据模型/API/导航框架/两个只读区/测试」六个内聚步骤，强行压到 5 个会割裂 D 区与 B/C/E 占位（二者同属 growth 面板渲染层）。若需严格 ≤5，可将 T05 的 B/C/E 占位拆入 T04，但会稀释「只读核心区」的独立性，故保留 6。

---

## 6. 依赖包列表

**无新增依赖**。沿用现状：
- 前端：原生 JS + 浏览器原生 SVG / `fetch`，无 npm 包。
- 后端：Python 标准库 `sqlite3` / `json` / `http.server`，无新 pip 包。
- 测试：Node.js 18+ 内置 `assert` + 全局 `fetch` + `child_process`（拉起 `server.py` 测试端口），无 npm 包。
- 唯一环境前提：运行 `.cjs` 需 Node 18+（项目既有 `.cjs` 测试惯例假设）；若环境无 Node，T06 可降级为手动 curl 校验，不阻塞 P0。

---

## 7. 共享知识（跨文件约定）

1. **SVG 配色来源**：优先用 CSS 变量 `var(--panel)`/`--accent`（与现有 `.empty`/面板同色系）；多折线取新增常量 `GROWTH_PALETTE`（12 色数组，顺序**严格对齐** `predictor.SIGNALS` 顺序，保证权重线颜色与信号一一对应）。绝不硬编码 6 色。
2. **空数据处理约定**：所有 growth 相关 API 返回 `{ok:true, has_data:bool, ...}`；前端 `render*` 一律先判 `has_data` / 数组 `length`，空则 `box.innerHTML = '<div class="empty">…</div>'`（复用既有 `.empty` 样式，见 `app.js` `ensureKbDocFor` 空态）。**绝不抛异常、绝不画 NaN 坐标**。
3. **隔离字段命名约定**：凡涉及外部知识源的表/接口，统一使用 `source_key` / `category` / `track` / `enabled` / `trust_score` / `promoted` 六字段名；`promoted=1` 是回写 `predictor_score.weight` 的唯一闸门（仅 P2 实现，本期仅留字段+按钮占位）。
4. **API 响应包裹**：沿用 `_send_json` 的 `{ok, ...}` 形态；新增 API 不引入新包裹格式。
5. **手绘 SVG 作图惯例**：手写 `<polyline>`/`<path>`，固定 `viewBox`（建议 `0 0 680 240`）；坐标经 `mx(x)`/`my(y)` 线性映射后拼接 `points`；`stroke-width` 1.6–2，`stroke-linejoin/linecap="round"`（参考 `app.js:923`/`:3841`）。
6. **维度动态读取纪律**：A 区权重线数量、D 区权重条数量一律由接口返回的 `signals` 驱动，禁止在前端写死 12 或 6。

---

## 8. 待确认事项（需王总 / 产品经理拍板，附建议默认值）

| # | PRD 问题 | 建议默认值 | 理由 |
|---|---|---|---|
| 1 | 权重演化曲线按「原始六路」还是「当前 12 维」？ | **按 `predictor.SIGNALS` 全部 12 维动态渲染**，A 区只画 `kind=signal` 的 12 条线；school/strategy 在 D 区用权重条展示 | 已核实引擎已扩到 12 维；硬编码 6 会导致与引擎口径脱节（历史教训见 `app.js:12225`） |
| 2 | 外部知识抓取归属（自己抓 vs 豆包抓） | 本期**只留接口位（P2）**，不阻塞 | PRD 明确 P2 |
| 3 | `trust_score` 0–1 还是 0–100、阈值/算法 | **0–1 浮点**（与 `weight` 同量纲）；高可信阈值默认 **0.7**；本期只存不计算，算法 P2 定 | 同量纲便于将来提权映射 |
| 4 | 冲突理论并排默认排序 | **按 `trust_score` 降序**，同分按 `source_key` 字典序 | 高可信优先呈现，符合王总「不被噪声污染」诉求 |
| 5 | 提权确认交互形态 | P2 用「开关 + 二次确认弹窗 + 可回滚按钮」；本期仅占位 | 防误提权，留 UX 方向免返工 |
| 6 | A 区曲线样本不足（贝叶斯收缩前仅 1–2 点） | **<3 点画虚线**（`stroke-dasharray`），**<2 点（仅当前值）显示「样本不足」占位**，不强行连线 | 避免误导，诚实呈现 |
| 7 | `growth_notes` 与 `learn_tracks` 关系 | **独立表、零耦合**（本设计已实现）；笔记仅本面板使用 | PRD P1-1 本就独立 |
| 8 | P1 笔记 `growth_notes` 是否需完整 CRUD 接口 | 本期**仅建表 + 占位 UI**；完整 CRUD（POST/DELETE `/api/growth/notes`）列为 P1 轻量下一迭代，不与只读 `adjust_log` 混用 | 隔离「只读 API」与「写 API」，符合现有只读优先惯例 |

> 第 8 条为设计补充：若王总要求本期即支持笔记增删改，则 T05 追加 `/api/growth/notes` 写接口（仍属 P1，不影响 P0 只读闭环）。
