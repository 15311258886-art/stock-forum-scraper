# 阶段 0：测试自检闸 + 种子缺陷落地清单

> 项目：股票论坛可视化查看器（stock-forum-scraper）
> 角色：QA 工程师（Edward）
> 目标：先暴露「PASS 可能是假的」这个元问题 —— 用已知变异注入证明现有测试套件抓不到 bug，并补上能抓 bug 的正确性断言。
> 红线：本清单**不改动任何生产源码**，仅新增方案文件与测试闸；变异只在「临时副本 / 原地备份 + 强制还原」下施加，绝不污染仓库。

---

## 0. 元问题复述（已确诊）

现有测试只做「存在性 / 结构 / 不报错」断言，从不断言「结果是否正确」，也没有故障注入来证明测试能抓 bug。导致 10 轮 `PASS` 但 bug 全靠人工发现。

已逐文件核实的现状（行号以当前仓库为准）：

| 验证脚本 | 实际断言（只会「假 PASS」的部分） |
|---|---|
| `verify_backend.py` | B7 回测仅断言 `b.get("n")==2 and b.get("win_rate") is not None and b.get("metrics") is not None`（第 59 行）；沙箱里「诚实报错（error 含『真实』字样）」也算 PASS（第 29-31、72-73 行）。**无正确性校验**。 |
| `verify_prediction.cjs` | 仅断言 `items.length===20 / core===10 / watch===10 / symbol 为 6 位代码 / 面板元素存在 / 控制台零报错`（第 60、63、64、65、113-118、138 行）。**无筛选/预测正确性、无日期/asof 校验**。 |
| `verify_screener_enh2.cjs` / `verify_screener_enh3.cjs` | B7 仅断言路由 `b.ok===true && b.n===2`（enh2 第 126-127、enh3 第 66 行）；B7 面板仅检查「总收益/年化/回撤/夏普/索提诺/幸存者偏差/坑位提示」**文本出现**（enh2 第 166-176 行），不校验数字。 |
| `verify_e2e.js` | B1-B3 纯 UI/交互（工具栏、策略增删、热力图、列设置、CSV），**不涉及回测数字正确性**。 |

结论：上述脚本在「成本漏扣 / 前视偏差 / 涨跌幅符号反 / 幸存者未剔除 / 日历陈旧」五类经典 bug 下**全部仍会 PASS**。这正是阶段 0 要证明并修复的。

---

## 1. 阶段 0 两步实施步骤

### 1.1 测试自检闸（Mutation / Fault-Injection Gate）

**目标**：对生产代码施加「已知良性变异」（模拟经典 bug），跑测试套件；若套件仍全 PASS，则判「套件不足」并让构建失败。它把「测试能不能抓 bug」从玄学变成可自动判定的硬指标。

**新增文件与职责**：

1. `stage0_mutations.json`（新增）—— 变异清单（manifest）。每条 = 一个种子缺陷的 find/replace 规格：
   ```json
   [
     {
       "id": "cost_leak",
       "target": "viewer/screener_extra.py",
       "anchor": "def _trade_cost(amount: float, is_buy: bool, code: str, mult: float) -> float:",
       "find": "    if amount <= 0:\n        return 0.0",
       "replace": "    if amount <= 0:\n        return 0.0\n    return 0.0  # [SEED] 成本漏扣：绕过所有成本",
       "expect_fail": true,
       "test_case": "test_cost_not_leaked"
     }
     /* 其余 4 个种子同理（见第 2 节表） */
   ]
   ```
2. `stage0_selfcheck_gate.py`（新增）—— 闸门编排器（用 `.venv/Scripts/python.exe` 跑）：
   - 对 `stage0_mutations.json` 中每条变异：
     1. **备份** 目标文件到 `<file>.stage0.bak`（hash 校验，防还原错位）；
     2. **施加变异**（按 `anchor`+`find` 定位并替换；找不到则报错退出，不静默跳过）；
     3. **跑对应正确性用例**：`python -m pytest test_stage0_correctness.py::<test_case> -q`（无 pytest 则直接调脚本内对应函数并记录 PASS/FAIL）；
     4. **强制还原** 目标文件（放在 `finally`，即使崩溃/断言失败也必还原；还原后比 hash 确认与备份一致）；
     5. 记录 `{id, mutation_applied, test_result}`。
   - 元演示（仅报告、不阻断）：用其中 1 个变异（如 `cost_leak`）额外跑一遍**现有** `verify_backend.py`，打印 `LEGACY SUITE BLIND: verify_backend.py 未捕获 <seed>` 作为「PASS 是假的」证据。
3. `test_stage0_correctness.py`（新增）—— **缺失的那类正确性断言本身**（离线、确定性，靠 monkeypatch + 合成数据，不依赖 baostock/akshare/DeepSeek/网络）。这是闸能判定的「正确答案基准」。

**触发方式**：
```bash
# 项目根
.venv/Scripts/python.exe stage0_selfcheck_gate.py
# 可选：仅跑正确性用例（不含变异）
.venv/Scripts/python.exe test_stage0_correctness.py
```

**判定「套件不足」的规则（核心）**：
- 若任一 `expect_fail=true` 的变异施加后，对应 `[test_case]` **仍 PASS**（即变异没被抓到）→ 闸打印 `BUILD FAIL: test suite insufficient — seed <id> not caught by any assertion`，并 `exit 1`（构建失败）。
- 全部种子都被对应用例抓到（变异下用例 FAIL）→ 打印 `OK: all 5 seeded bugs are caught by correctness tests`，`exit 0`。
- 额外约束（防误判）：还原校验失败、变异未生效、用例本身在干净代码上 FAIL，都判 `exit 1` 并明确报错，绝不「静默 PASS」。
- **绝不在仓库留下变异**：所有注入只对「备份后的原地文件 / 临时副本」施加；`finally` 强制还原 + hash 校验；提供 `--dry-run`（只 diff 不写）与 `--no-restore-off` 安全开关（默认开启还原）。

> 设计要点：阶段 0 的闸**只用新增的 `test_stage0_correctness.py` 做判定基准**，因为现有 verify 脚本依赖 baostock（沙箱可用但非确定）和 chromium+DeepSeek（重、易飘）。新增正确性用例 100% 离线确定性，才配当 CI 闸门。

### 1.2 种子缺陷（已知变异注入）

5 个种子，每个代表一类经典 bug 族。全部遵循「注入已知变异 → 现有套件仍 PASS（证明盲）→ 新增正确性断言 FAIL（证明已能抓）」。具体注入点 / 内容 / 期望失败测试 / 需补断言见下表。

---

## 2. 种子缺陷表

| # | 缺陷名 / bug 族 | 注入点（文件:行） | 注入内容（old → new） | 期望失败测试（**现有**，结论：都抓不到） | 需补断言（落到 `test_stage0_correctness.py`） |
|---|---|---|---|---|---|
| 1 | **前视偏差**（look-ahead） | `viewer/predictor.py:1683-1684`（`_generate_inner` 数据隔离截断） | 原：`bars_map = {c: [b for b in v if str(b.get("d",""))[:10] <= td] for c, v in bars_map.items()}`<br>变异：**整段删除这两行**（或把 `<= td` 改成 `>= td`），让 td 之后的真实 K 线进入信号计算 | 无。`verify_prediction.cjs` 不校验预测是否用了未来数据；`verify_backend.py` 不涉及 generate | `test_asof_isolation()`：monkeypatch `predictor.fetch_bars` 返回「含未来 bar（date>td）」的 bars_map；两次调 `_generate_inner(con, run_date=td)`（一次含未来 bar、一次不含）；断言两次产出（run_date / items 打分 / scores）**完全一致**。泄漏 → 不一致 → FAIL。 |
| 2 | **成本漏扣** | `viewer/screener_extra.py:245-253`（`_trade_cost`）；更直接的变异是置 0 顶部常量（第 80-87 行 `COMMISSION_RATE/STAMP_RATE/SLIPPAGE_RATE/TRANSFER_RATE`） | 在 `_trade_cost` 开头注入 `return 0.0`（绕过全部成本） | 无。`verify_backend.py` B7 仅断言 `n==2 / win_rate not None / metrics not None`；`verify_screener_enh*.cjs` 仅断言路由 `ok && n==2`——成本置 0 仍满足 | `test_cost_not_leaked()`：monkeypatch `se.get_kline` 返回「横盘」（close 恒定）合成 K 线（2 票 / 1 年 / 月度再平衡）；调 `se.backtest(...)`。断言：真实成本下 `metrics["total_return"] < 0`（横盘被成本吃成负）；且 `metrics_2x["total_return"] < metrics["total_return"]`（2 倍成本更差）。漏扣后两值≈0 → FAIL。 |
| 3 | **涨跌幅公式符号反** | `viewer/predictor.py:1980`（`_window_ret` 返回 `c1 / c0 - 1.0`） | 原：`return c1 / c0 - 1.0`<br>变异：`return 1.0 - c1 / c0`（或 `c0 / c1 - 1.0`），正负号翻转 | 无。现有脚本不校验复盘收益 / 涨跌幅正负 | `test_window_ret_sign()`：合成 `k=[{"c":100},{"c":110}]`，`_window_ret(k, 0, 1)` 必须 `== 0.10`（涨为正）。符号反 → 返回 `-0.10` 或 `1-1.1` → FAIL。附带：校验 `review` 中 `hit = (r > b)`（第 1919 行）的方向——样本命中率须与真实方向一致，符号反会让「跑赢基准」判定反向。 |
| 4 | **幸存者未剔除** | `viewer/screener_extra.py:392-408`（中途退市强制平仓块 → 影响 `constraints["delisted_exits"]` 与 `final_nav`） | 注释 / 跳过 392-408 的强制平仓逻辑，使中途退市票持仓不被按最后可得价结算、净值冻结在最后收盘价而虚增 | 无。`verify_backend.py` 不检查 `delisted_exits`；B7 面板只检查「幸存者偏差」**文案出现**（enh2 第 171 行），不检查数字 | `test_survivorship_delisted_exit()`：monkeypatch `se.get_kline` 让一只票窗口内最后交易日 < 窗口 end（中途退市、末段无数据），另一只正常；调 `se.backtest(...)`。断言：`constraints["delisted_exits"] >= 1`，且 `final_nav`（退市后）应低于「按最后可得价强制平仓」的合理值；变异后 `delisted_exits == 0` 且 nav 虚增 → FAIL。 |
| 5 | **日期 / asof 错位**（陈旧日历） | `auction.py:226-234`（`market_trade_days` 的「落后就重抓」逻辑，尤其第 233 行守卫 `if cached is not None and now - c["ts"] < _TD_TTL and c["asof"] == today:`） | 把 233 行守卫改成 `if cached is not None:`（永远用缓存、忽略 asof/落后）；或把 230 行 `return cached` 的限频去掉让 behind 分支直接返回缓存 → 日历返回陈旧值 | 无。现有脚本不校验 `run_date` / 日历新鲜度；前端「陈旧角标」也无断言 | `test_trade_calendar_not_stale()`：monkeypatch `auction.sina_kline_dates` 返回含 `"2026-10-09"` 的日期；`auction._TD_CACHE` 注入陈旧 `days`（最大 `"2026-09-30"`，`asof="2026-09-30"`）；以 `time.strftime` 返回 `"2026-10-09"`。断言 `auction.latest_trade_date("2026-10-09") == "2026-10-09"`（必须重抓到今日，而非返回陈旧 `"2026-09-30"`）。变异后返回 `"2026-09-30"` → FAIL。可追加：`predictor._trade_calendar` / `catch_up` 的 `target_end` 必须等于最新交易日。 |

**关于第 1 项（前视偏差）的可测性说明**：隔离截断目前内联在 `_generate_inner`（1683-1684），不是独立函数。阶段 0 优先用「整段 generate 的隔离回归（monkeypatch `fetch_bars` + `SS.universe` + `load_kcache` + 内存 sqlite）」实现，零源码改动即可跑。可选改进（交由后续工程师、非阶段 0 必须）：把截断抽成纯函数 `_asof_truncate(bars_map, td)`，便于直接单测——列在此仅作建议，本阶段不要求改源码。

---

## 3. 预期新增 / 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `stage0_selfcheck_plan.md` | 新增（本文档） | 阶段 0 落地清单，已落盘项目根 |
| `stage0_mutations.json` | 新增 | 5 个种子的 find/replace 变异规格（含 `expect_fail` / `test_case` 映射） |
| `stage0_selfcheck_gate.py` | 新增 | 变异施加 + 跑正确性用例 + 强制还原 + 判定「套件不足」并 `exit`；含 `--dry-run` / 还原校验 |
| `test_stage0_correctness.py` | 新增 | 缺失的正确性断言（5 个离线确定性用例：asof 隔离 / 成本不漏 / 涨跌幅符号 / 幸存者退市 / 日历不陈旧），靠 monkeypatch + 合成数据，不依赖网络 |
| （可选）`Makefile` / `check.bat` / CI workflow | 新增 | 加 `selfcheck` 目标，CI 中跑 `stage0_selfcheck_gate.py`，失败即阻断合并 |
| **生产源码（predictor.py / screener_extra.py / auction.py / 各 verify_*.py）** | **不改** | 阶段 0 红线：变异只在备份副本/临时副本施加，闸跑完必还原；现有 verify 脚本仅在「元演示」时被调用以证明盲，不被修改 |

---

## 4. 运行顺序建议（落地后）

1. 先写 `test_stage0_correctness.py` 与 `stage0_mutations.json`，在**干净代码**上跑 `test_stage0_correctness.py` → 应全 PASS（证明断言本身正确）。
2. 跑 `stage0_selfcheck_gate.py`：
   - 对每个种子：施加变异 → 跑对应用例 → 期望 **FAIL**（证明能抓 bug）→ 还原。
   - 任一没抓到 → `exit 1`，按 `INSUFFICIENT: seed <id>` 修断言。
   - 元演示：用 `cost_leak` 跑现有 `verify_backend.py` → 仍 PASS → 打印 `LEGACY SUITE BLIND` 证据。
3. 全部种子被抓到 → `exit 0`，阶段 0 完成：既证明了「旧 PASS 是假的」，又交付了「能抓 bug 的自检闸」。

> 一句话收口：阶段 0 交付的不是更多「存在性断言」，而是**一个会在 bug 注入时变红的闸** + **5 个能变红的正确性断言**，从此「10 轮 PASS」不再等于「没 bug」。
