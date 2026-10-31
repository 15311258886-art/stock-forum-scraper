# 股票论坛可视化查看器 · 暴露型只读 MCP 服务器（A 方案）

> 把现有 SQLite 语料（情绪 / 荐股 / 共振 / 大V / 帖子 / 新闻 / 信号 / 基本面 / 精选 / 龙虎榜 / 信源信誉）
> 包成一个 **只读 MCP 服务器**，让任意 LLM Agent（WorkBuddy / Cursor 等）用自然语言直接查询你的独家数据。
>
> **零新数据源 · 零账号 · 零第三方依赖 · 对现有抓取与前端零改动。**

---

## 1. 它是什么 / 不是什么

- **是**：在你本地给 `viewer/app.db` 开一个"查询窗口"（MCP 协议），只往外递**查询结果**，且**只读**。
- **不是**：不把你的抓取器、解析规则、方法论、源代码暴露出去；不往数据库写任何东西；不主动联网取数。
- 连接权限只掌握在你自己手里——只有你配置到 AI 助手里的客户端能连，外人连不上。

## 2. 只读保证（已自测通过）

- 连接使用 `file:...?mode=ro` 只读 URI，并叠加 `PRAGMA query_only = ON`。
- 自测 `_selftest.py` 中故意执行 `CREATE TABLE` 会被 SQLite 拒绝（`attempt to write a readonly database`）。
- **没有改动项目内任何现有文件**，仅新增 `mcp_server/` 一个目录。

## 3. 暴露的 12 个工具

| 工具 | 作用 | 主要数据源 |
|---|---|---|
| `resolve_stock` | 按代码/名称/公司模糊搜股票 | `stock_names` |
| `query_stock_sentiment` | 个股论坛情绪：分布/均分/趋势/样例 | `posts` + `stock_mentions` |
| `list_posts` | 论坛帖子检索（可按个股/站点/情绪过滤） | `posts` + `stock_mentions` |
| `list_recommendations` | B 档外部荐股（带回测 pnl） | `extref_signals` |
| `get_resonance` | 个股多源共振概览+明细 | `sig_unified` |
| `top_influencers` | 大V/博主/关键人物排行榜 | `person_scores` |
| `get_signals` | 统一信号查询（博主/战法/席位/情绪/引擎） | `sig_unified` |
| `get_news` | 新闻流 / 个股公告 | `news` / `stock_ann` |
| `get_fundamentals` | 个股基本面（ROE/毛利率/同比等） | `fundamentals` |
| `get_self_pick` | 每日精选/自选（含回测收益） | `self_pick` |
| `get_lhb` | 龙虎榜（含后续涨跌） | `lhb_stocks` |
| `compare_signal_sources` | 信源信誉对比（胜率/超额/选股力） | `res_source` |

## 4. 启动方式

```bash
# 用任意含标准库 sqlite3 的 Python 即可（项目 .venv 或 WorkBuddy 管理 Python 均可）
python mcp_server/launch.py --db viewer/app.db
```

正常启动后，进程通过 stdin/stdout 收发 JSON-RPC；**不要手动往里敲命令**，它由 MCP 客户端托管。

## 5. 接入 WorkBuddy（或 Cursor）

将下面 `mcp_config.example.json` 的内容合并进 MCP 配置文件：

- **WorkBuddy**：`~/.workbuddy/mcp.json`（合并进已有的 `mcpServers` 对象，勿覆盖其他 server）
- **Cursor**：`~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "stock-forum": {
      "command": "C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe",
      "args": [
        "F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\mcp_server\\launch.py",
        "--db",
        "F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\viewer\\app.db"
      ]
    }
  }
}
```

> 说明：路径含 `&`、空格、中文——因为是 **args 数组**（直传进程参数，不经 shell 拼接），`&` 不会被判成命令分隔符，可放心使用。
> 配置后**重启 WorkBuddy / 在连接器页面对该 server 点"信任"**，即可在对话里直接问："600519 的论坛情绪最近怎么样""给我列一下当前 B 档荐股"。

## 6. 自测

```bash
python mcp_server/_selftest.py
```

会依次验证：① MCP 握手 → ② tools/list（12 工具）→ ③ 全部工具调用 → ④ 真实子进程 stdio 分帧 → ⑤ 只读保护。全部 PASS 即就绪。

## 7. 已知限制（诚实标注）

1. **论坛/社媒内容无合规 API 或 MCP**：股吧/雪球/淘股吧数据来自抓取，MCP 只是把这些结果"可被查询"，变不出官方授权访问。
2. **数据时效取决于 `viewer/app.db` 的抓取刷新**：MCP 服务器是查询层，不触发抓取；库未更新则查询结果滞后。
3. **付费官方源（东财妙想 / Tushare / iFinD）未接入**：属于规划 B 方案，需你提供账号/密钥，本次 A 方案不涉及。
4. **解析范围**：`resolve_stock` 依赖 `stock_names` 表；极冷门或新上市代码可能解析不到，此时会返回友好提示而非报错。
