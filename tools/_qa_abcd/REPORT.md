# QA 独立验证报告 · ABCD 口径改革

验证人：Edward（qa-abcd-verify）｜ 环境：项目根 `F:\...\stock-forum-scraper`，venv py3.13，chromium-1234
纪律：**只验证、不改业务代码**。全部结论附真实命令输出/截图。未跑通即标"跑不通"。

---

## 总览

| 项 | 主题 | 结论 |
|---|---|---|
| 1 | B 抽取修复（精度+召回） | **FAIL**（1a 精度未达设计 / 1b 裸代码召回缺口 / 1c 脏名） |
| 2 | 各解析器无 1:N | **PASS** |
| 3 | A 机器号 gate 零误杀 | **PASS**（零真人误杀）；但暴露 2 个 gap |
| 4 | C+D 接口+前端 | **C PASS / D 功能 PASS 但 Tier A 含机器号 → FAIL** |
| 5 | 既有回归 | **PASS**（11/11 + t1 护栏全绿） |

---

## 1) B 抽取修复 —— 精度 + 召回（用 `TaogubaParser._extract_trades`，自写用例）

### PASS
- 「拉权重股出货，赶紧清仓」→ `[]`（无具体标的，未凭空生成 SELL）✓
- 「传媒：新华传媒、新华文轩」→ `[]`（列举≠交易）✓
- 「3板 上海洗霸」→ `[]`（无动词不入交易）✓
- 「清仓宁德时代」→ `[(SELL, 宁德时代)]` ✓；「买入贵州茅台」→ `[(BUY, 贵州茅台)]` ✓
- 「宁德时代今天很强。清仓贵州茅台」→ 仅 `[(SELL, 贵州茅台)]`（跨句不错配）✓

### FAIL-1a（精度，明确）—— 「一动作词+一串股名」仍产 **2** 条（应 1 条）
```
'买入九阳股份/传艺科技/华北制药'  -> [('BUY','九阳股份'), ('BUY','传艺科技')]   # 期望 1
'买入九阳股份、传艺科技、华北制药' -> [('BUY','九阳股份'), ('BUY','传艺科技')]   # 期望 1
```
根因（已逐行追踪）：规则③（动后名前）认领最近 1 只（九阳），规则④ 又对**同一个动作词**认领了第 2 只（传艺）。
`_extract_trades` 里规则④ 的 `claimed_actions` **未纳入规则③已消费的动作词**，故"1 动作词最多 1 条"不成立，实际**最多 2 条**。
量化：taoguba/full **1067 帖中 74 帖（6.9%）**存在"单个动作词认领 ≥2 只股"；真实例：作者「三晋才子」复盘帖产出 4 条 BUY（打板九阳/低吸博纳为真，**雪龙集团/新华文轩为列举误报**）。
修复建议：规则④ 遍历动作词时，把规则③（前向+后向）已用过的 action 位置一并加入 `claimed_actions`。

### FAIL-1b（召回）—— 裸 6 位代码不识别
```
'卖出600519'   -> []           # 期望 [('SELL','600519')]
'卖出 600519'  -> []
```
taoguba 代码锚定正则只认 `（600519）` 括号式；裸代码无任何路径。（括号式可识别，见下条。）

### FAIL-1c（数据质量，连带）—— 括号式裸代码取到**脏名**
```
'卖出（600519）' -> [('SELL','为投资贵州茅台','600519')]   # 期望名 = 贵州茅台
'卖出(600519)'  -> [('SELL','为投资贵州茅台','600519')]
```
根因：`scraper/stock_names_full.json` 里 600519 有 5 条条目，脏别名「为投资贵州茅台/提供贵州茅台/汇总贵州茅台/解读贵州茅台」排在真名「贵州茅台」之前；`name_match.code_name()` 用 `setdefault` 取首个 → 脏名。

### ★ 重点探针明确结论
「清仓宁德时代、贵州茅台」（**1 动作词 + 并列 2 标的**的合法句）→ 实际产出 **2 条**（SELL 宁德 + SELL 茅台）。
**判定**：这不是"只认 1 只漏掉第 2 只"的召回损失 —— 恰恰相反，**它与 FAIL-1a 同根**：正因为规则④ 会给同一动作词再补 1 只，并列句才凑齐 2 条。所以：
- 对"合法并列买卖"：**召回 OK（无损失）**；
- 但"1 动作词只认最近 1 只"这一**设计声明不成立**，且对"列举/复盘句"就是**多产 1 条的假阳性**（FAIL-1a）。
建议按 1a 修：修好后「清仓宁德时代、贵州茅台」会退回 1 条（精度优先），届时才是"可接受的精度优先"。当前是"两不像"。

---

## 2) 各解析器一致性 —— PASS（无 1:N）
自构造"标题含多只票 + 单个动作词"的行/条目，用真解析器跑：
- sina：`parse_trades` → **1 条**（symbol 固定取 URL 页面股票）→ 无 1:N
- eastmoney：→ **1 条**（`CODE_RE.search` 只取首个 6 位码）→ 无 1:N
- ths：→ **1 条**（同上；注意 `_is_blocked` 要求 html≥800 字符，短样本需补齐）→ 无 1:N
结论：三站确实"每行/每条至多 1 标的"，**无 1:N 路径**。
（附注：sina 把动作归到 URL 对应股票、忽略标题里的其它代码——是精度取舍，非 1:N。）

---

## 3) A 机器号 gate 零误杀 —— PASS（明确结论：零真人误杀）
反向测试集 = DB `players`(326) ∪ `名单B_选手主页URL.csv`(227) = 553 昵称，跑 `is_bot_author`：
- 命中 **11 个，无一为真人**：
  - 8 个 = `BOT_NAMES`（神光财经/Sina财经产/新浪财经客/新浪财经芝/看财经官博/汽车营销分/机工战略/EC品牌观察）→ 抽样语料确证均为媒体/通稿/刷屏号。
  - 3 个 = 「中国人寿资讯/宁德时代资讯/平安银行资讯」（`XXX资讯` 正则）→ eastmoney、`axis=post`，非真人。
- **关键**：`名单B` 227 真选手里被 gate 的 8 个 = `BOT_NAMES` 全部 → 说明**名单B 本身被这 8 个机器号污染**，gate 掉属**去污染**，非误杀。
- **结论：没有任何真人被 gate。**

### gap-3a（需团队定夺）：机器号 gate 未触达存量库
`quality_filter.py [2b]` 只按 `BOT_NAMES`(8) 改写 `axis='bot'`；`XXX资讯` 正则号不在名单 → 不被打标。实测 `trades` 表现存 **284/348 行 axis=NULL**，且「宁德时代资讯」「贵州茅台资讯」等 `axis` 非 bot → **这些机器号仍留在库、仍进共识统计**（见第 4 项证据：共识里出现 6 个资讯号作者）。

### gap-3b（提醒，暂无实害）：内容启发式误杀风险
`bot_accounts.csv` 另有 9 个仅凭"含刷屏/聚合标记（N 次）"被 `content_looks_like_press` 标记的号（主升龙头空空龙/松间存月/吉吉Queen/薇宝2026…）。当前 gate **未启用**该启发式，故无实害；但 `_SPAM_MARKERS` 含"广告/点击查看"等宽泛词，**一旦启用内容启发式，误杀真人风险高**，须先收紧。

---

## 4) C+D 接口 + 前端（安全重启后）

### 安全重启（按技能纪律）
`netstat` 定位 8765 LISTENING PID=38668 → `MSYS_NO_PATHCONV=1 taskkill /F /PID 38668` → 确认 8765 FREE → `run_in_background` 起 `.venv/Scripts/python.exe -m viewer.server`（新 PID 42896）→ 首页 200、日志无 traceback。
**重启前后对比（证明改动真的生效）**：
| 请求 | 重启前 size | 重启后 size |
|---|---|---|
| /api/consensus?min_holders=2 | 115866 | 126484 |
| …&only_qualified=1 | **115866（与上面完全相同）** | 10520 |
| /api/cohold_recent?days=5 | 24482 | 34543 |
| …&only_qualified=1 | **24482（同上）** | 278 |
→ 重启前运行的是**旧代码**（参数被忽略、字节全等）；重启后 `only_qualified` 真实改变结果。

### C（HOLD 与 BUY 分离）—— PASS
接口新增 `holders / buy_holders / hold_holders / watch_holders`：
- 中国平安 总39 = 买24 + 持15；**平安银行 总31 = 买14 + 持17（HOLD 比 BUY 还多，现已可见化）**；贵州茅台 35=买25+持9+关注1；宁德时代 37=买30+持7。
- 前端表头「口径拆分（总 / 买 / 持 / 关注）」，首行渲染「39 人 买 24 持 15」。
（观察，非 FAIL：排序仍按 `holders` 总数，故列表 Top4 顺序未变；若产品意图是"看多共识看买"，可考虑按 `buy_holders` 排序。）

### D（仅看入围选手 Tier A）—— 功能 PASS，但**名单含机器号 → FAIL**
- 点击开关：consensus **55 行 → 7 行**（真实过滤，非只回显）。前端 even 带 stale-backend 守卫（旧后端未重启时提示"不假装过滤成功"）。
- **FAIL-D**：`only_qualified=1` 结果中，「平安银行」的选手含 **平安银行资讯**（`is_bot_author=True`，`players.axis='post'`）。Tier A(44 人) 里至少含 `新浪财经客`、`Sina财经产`（BOT_NAMES）+ `平安银行资讯` 等 **15 个 eastmoney/post 号**。
→ "仅看入围选手"**不能保证只剩真人**，反而可能放行机器号。
建议：Tier A 过滤叠加 `axis IN ('person','person_id')` 或 `NOT is_bot_author(name)`。
- 附注：cohold 面板 `only_qualified=1` → 三组全空（窗口 10-05~10-09 内无 Tier A 选手 trades）。属"诚实空"，但该开关在此面板实际无数据可看。

### 前端其余主面板（防"改 A 坏 B"）—— PASS
集合竞价 / 每日预测 / 博主荐股 / 个股快讯公告 全部加载有内容、**控制台 0 error、0 pageerror**。
截图（绝对路径）：
- `tools/_qa_abcd/out/fe_consensus_before.png`（55 行，含口径拆分）
- `tools/_qa_abcd/out/fe_consensus_after.png`（勾选后 7 行）
- `tools/_qa_abcd/out/fe_cohold.png`、`fe_dashboard.png`、`fe_prediction.png`、`fe_blogger.png`、`fe_news.png`

---

## 5) 既有回归 —— PASS
- `verify_backend.py` → **11/11 PASS**（B4~B8、K线、DeepSeek Key 等；含诚实报错路径）。未见把断言写松。
- `verify_targets_t1.cjs`（技能指定的 taoguba/name_match/trades 回归护栏）：
  - 选手实盘标的列 rows=200 / chips=200 / withCode=200，**名称+代码 100% 成对**，点开小卡非空；
  - 每日预测 T+1 窗口可点、track 出现「T+1 命中37% (n=71)」；
  - 控制台错误：无。
- `py_compile`：全部改动文件 OK。

---

## 跨项观察（集中度根因）
- 新抽取产物 `trades_v2_staging.jsonl`（1023 行）：**0 行含"资讯"作者**，Top4(宁德/茅台/平安/平安银行) 仅 **0.2%**（旧库 348 行是 **63.8%**）→ 新管道确实根治集中度。
- **但该产物未并入 DB**：线上 `viewer/app.db` 仍是旧 348 行，Top4 依旧 63.8%，且共识仍含 6 个 `XXX资讯` 机器号作者。
→ 若不做数据重灌/重标，C+D 只改了展示口径，喂进去的数据没换，"共识"仍被机器号与噪声占据。

---

## 交付物
- 验证脚本：`tools/_qa_abcd/qa1_extract.py`、`qa1b_probe.py`、`qa2_parsers.py`、`qa3_botgate.py`、`qa4_frontend.mjs`
- API 基线/快照：`tools/_qa_abcd/out/{pre,post}_*.json`
- 截图：`tools/_qa_abcd/out/fe_*.png`
- 服务重启日志：`tools/_qa_abcd/out/server_restart.log`
