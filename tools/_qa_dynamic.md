# 动态功能测试报告（端到端只读）

- 测试对象：`stock-forum-scraper` 本地查看器服务 `http://127.0.0.1:8765`（运行中实例，pid 26300，已加载最新代码）
- 测试人：general-purpose-1（任务 #7）
- 测试日期：2026-09-22
- 输出证据文件：`tools/_qa_dyn_out.txt`（路由探测 + 一致性）、`tools/_qa_dyn_out2.txt`（字段对账）、`tools/_qa_dyn_out3.txt`（百分号编码 + 口径差异）、`tools/_qa_dyn_out4.txt`（排序/空值/类型）
- 可复现脚本：`tools/_qa_dyn_probe.py`、`_qa_dyn_probe2.py`、`_qa_dyn_probe3.py`、`_qa_dyn_probe4.py`（均为只读 GET 探针，未修改任何源码）

---

## 一、测试范围与方法

| 项 | 说明 |
|---|---|
| 只读约束 | 全程只发 **GET**。未调用 `/api/fetch`、`/api/demo`、`/api/sync_names`、`POST /api/overseas`；未改库、未改源码、未重启服务。 |
| 探测方式 | 裸 `socket` 直连 `127.0.0.1:8765` 手工拼 `GET ... HTTP/1.1`，**完全绕过 `http_proxy=http://127.0.0.1:55035`**，避免把服务异常误判为 502。 |
| 记录内容 | HTTP 状态行、`Content-Length`/`Content-Type`/`Server`/CORS 头、字节数、顶层 JSON 类型、字段名集合、首 400 字节样本、以及**连接级异常**（零字节响应）。 |
| 静态比对 | 路由与返回值源码：`viewer/server.py`（`do_GET` 557–630，`do_POST` 632–687，`_send_file` 546–555）；各 `api_*` 实现 86–532；库表结构 `viewer/db.py`（含 `tier/influence/decision/sectors_*_detail` 迁移，300–346）。前端字段读取：`viewer/static/app.js`、`index.html`。 |
| 实例数据基线 | `counts`：posts 185 / trades 46 / news 3 / raw_pages 1 / persons 170 / stock_mentions 185 / topics 15 / players 160 / positions 135 / overseas_news 707 / overseas_hotspots 17；`last_ingest='示例数据'`；`sites=['taoguba']` |
| 探针规模 | 76 条路由/边界用例 + 24 只股票逐只交叉校验 + 3 轮字段对账 |

---

## 二、覆盖清单（路由 × 结果 × 实测数字）

### 2.1 静态资源

| 路由 | 结果 | 实测 |
|---|---|---|
| `GET /` `/index.html` | 通过 | 200，3788B，`text/html` |
| `GET /static/app.js` | 通过 | 200，70434B，`application/javascript` |
| `GET /static/app.css` | 通过 | 200，24354B，`text/css` |
| `GET /static/themes/light-tech.css` | 通过 | 200，399B（4 个主题文件均存在） |
| `GET /static/themes/nope.css` | 通过（404） | 404，330B，`text/html` 英文错误页 |
| `GET /static` | 通过（404） | 404（未命中 `startswith("/static/")`） |
| `GET /static/` | **失败** | **0 字节，连接被直接关闭**（见 P1-1） |
| `GET /static/../app.db` | **失败（高危）** | **200，917504B**，返回原始 SQLite 文件（见 P0-1） |
| `GET /static/../server.py` | **失败（高危）** | **200，31482B**，返回服务端源码 |
| `GET /static/%2e%2e%2fapp.db` | 通过（拦截） | 404（百分号编码未被解码，未穿越） |
| `GET /static/..%2f..%2fviewer%2fapp.db` | 通过（拦截） | 404 |

### 2.2 JSON GET 接口（全部 200）

| 路由 | 实测数字 |
|---|---|
| `/api/summary` | dict，411B，keys=counts,last_ingest,login_status,sites |
| `/api/stocks` | list n=24 |
| `/api/operating` | list n=46（LIMIT 200） |
| `/api/persons` | list n=100（LIMIT 100，库内 170 → 截断） |
| `/api/sentiment` | dict：distribution 看多94/看空49/中性42（Σ=185=counts.posts）；map n=24；name_covered 24/24 |
| `/api/topics` | list n=15（LIMIT 30） |
| `/api/players` | list n=160；`?top=1`→154；`?tier=A`→37、`?tier=E`→5、`?tier=Z`→0；`?channel=A`→37；channel 分布 A37/B42/C35/D46，tier 分布 A37/B42/C35/D40/E5/F1（占比自洽） |
| `/api/players?channel=%27%20OR%201%3D1--` | list n=0（参数化查询，注入无效——通过） |
| `/api/overseas_sentiment` | dict：total=707，net_mood=-5.9，heat_total=22482.75，region_dist 5 区，daily 77 行，source_mix 35 行，news 40 条，symbols 5 |
| `/api/overseas_hotspots` | list n=17；decision 非空 17/17；sectors_up_detail 非空 17/17；sectors_down_detail 非空 16/17（id=1184 恒指反弹确实无下跌领域，合理）；heat 严格降序 True；decision/明细类型全部 `str`/`list`（结构化数组，符合交付口径） |
| `/api/overseas_sources` | dict：sources n=41；status 分布 已接入26/待激活（需VPN）7/未启用5/待激活（需APIKey）1/已连通但无条目1/抓取失败1 |
| `/api/positions` | list n=135（前端零引用，见 P2-5） |
| `/api/consensus` | list n=23；`?min_holders=1`→23、`3`→22、`5`→14、`8`→6、`20`→0、`100`→0（min_holders 生效） |
| `/api/consensus?min_holders=1&symbol=600519` | list n=1，holders=11 |
| `/api/consensus?min_holders=1&symbol=___none` | list n=0 |
| `/api/posts` | list n=20；`?limit=1`→1；`?limit=`→20（空值取默认）；**`?limit=-1`→185**；**`?limit=999999999`→185**（见 P2-3） |
| `/api/login_status` | list n=1（前端零引用） |
| `/api/person?name=___没人` | 200 + 空壳（posts/trades/positions 全 0） |
| `/api/person?name=`（空） | 200 `{"error":"name required"}` |
| `/api/person?name=短线作手老王`（百分号编码） | 200，1901B；posts n=2，trades n=2，positions n=3 |
| `/api/player?name=`（空） | 200 `{"error":"name required"}` |
| `/api/player?name=__x__` | 200 `{"error":"player not found"}` |
| `/api/player?name=大阪柯小李` | 200，15455B：positions 6 / holdings 5 / sells 1 / trades 8 / recent 8 / access_items 2 / access_count 2；字段齐全，与 `app.js` 读取一致 |
| `/api/stock`（缺参） | 200 `{"error":"symbol required"}` |
| `/api/stock?symbol=600519` | 200，11576B；mention_count=10=len(mentions)，trade_count=4，position_count=12，holders=11 |
| `/api/stock?symbol=___nope` | 200 空壳（`market='未知板块'`，name_missing=true） |
| `/api/stock?symbol=../../../../windows/win.ini` | 200 空壳（作为普通字符串处理，**未穿越**——通过） |
| `/api/stock?symbol=😀测试` | 200（Unicode/emoji 正常回显） |
| `/api/stock?symbol=<10000 字符>` | 200（无长度上限，见 P3-5） |
| `/api/stock_names` | list n=24（前端零引用） |
| `/api/detail`（缺参） | 200 `{"error":"not found"}`（kind 默认 post，id 默认 0） |
| `/api/detail?kind=post&id=15952` | 200，keys 与前端一致（含 sentiment_score） |
| `/api/detail?kind=post&id=15952&full=1` | 200，多返回 `raw_html`（本例 88B 示例页） |
| `/api/detail?kind=trade&id=4143` | 200，keys 与前端一致 |
| `/api/detail?kind=news&id=1..3` | 200 `{"error":"not found"}`（示例库 news.id 非 1 起，非缺陷） |
| `/api/detail?kind=bogus&id=1` | 200 `{"error":"unknown kind"}` |
| `/api/detail?kind=post&id=-1` / `id=999999999` | 200 `{"error":"not found"}` |
| `/api/nonexistent`、`/api/summary/`、`/API/summary`、`/api/./summary` | 404（严格匹配，无泄漏路径） |

### 2.3 数值自洽性（重点）

| 校验 | 结果 |
|---|---|
| `counts.positions`(135) = `len(/api/positions)`(135) | OK |
| `counts.players`(160) = `len(/api/players)`(160) | OK |
| `counts.topics`(15) = `len(/api/topics)`(15) | OK |
| `counts.posts`(185) = Σ`sentiment.distribution`(94+49+42) | OK |
| `counts.stock_mentions`(185) = Σ`sentiment.map[].mentions` | OK |
| `counts.overseas_news`(707) = `overseas_sentiment.total`(707) | OK |
| `counts.overseas_hotspots`(17) = `len(overseas_hotspots)`(17) | OK |
| `counts.persons`(170) vs `len(/api/persons)`(100) | **MISMATCH**（LIMIT 100，见 P1-2） |
| `overseas.net_mood` 重算 =(86−128)/707×100=−5.94→−5.9 | OK |
| `Σregion_dist[].count`=707=total；`Σregion_dist[].heat`=22482.75=heat_total；`Σsource_mix[].n`=707=total | OK |
| 单只股票：`/api/stock` 的 mention_count/trade_count/position_count 与 `len(mentions/trades/positions)`、`/api/positions` 条数、`/api/consensus.holders` 全部相等（600519/300750/601318 逐条核对通过） | OK |
| `views` 三类合计 = mention_count = len(mentions)，24 只全部自洽 | OK |
| `sentiment.map[].bull+bear+neutral` = `mentions`，24 条全部自洽 | OK |
| 海外热点 `sectors_up_detail/sectors_down_detail/decision` 均为结构化数组/字符串 | OK |
| `hold_since` vs `hold_days` 语义 | **不自洽**（见 P2-1） |
| 共同持仓人数口径：`/api/consensus.holders` vs `/api/stock.holders` vs `/api/positions` 去重 | **000333：9 vs 10 vs 10**（见 P3-3） |

### 2.4 接口字段 × 前端读取对账

全部前端调用的接口字段**无缺失**，仅 1 处反向缺失：

| 接口 | 前端读取 | 结论 |
|---|---|---|
| summary / stocks / sentiment / operating / persons / topics / players / consensus / stock / detail / player / overseas_sentiment / overseas_hotspots / overseas_sources | 全部字段存在 | 通过 |
| **`/api/person` → `positions[].channel`** | `app.js:834` 读取 `p.channel` | **后端不返回 `channel`**（见 P2-2） |

后端返回但前端未用（非缺陷，信息冗余）：`stocks[].label`、`consensus[].label`、`operating[].raw_text`、`players` 的 `followers/is_visible/tier_weight/added_at/is_certified/id/site`、`summary.counts.overseas_news/overseas_hotspots`。

---

## 三、问题清单

> 每条均已实测复现。复现命令中的 `curl` 需加 `--noproxy '*'`（本会话有 `http_proxy` 干扰）；穿越类用例需加 `--path-as-is` 防客户端归一化。也可直接跑 `tools/_qa_dyn_probe*.py`。

### P0-1【P0 阻断｜高危】静态目录路径穿越，可直接下载 `app.db` 与服务端源码

- 位置：`viewer/server.py:546-555`（`_send_file` 的存在性/前缀校验），入口 `557-568`
- 现象：`/static/` 后拼 `../` 可读到 `viewer/static` 之外的任意文件。`GET /static/../app.db` 返回 **200 + 917504 字节原始 SQLite 二进制**（Body 起始为 `SQLite format 3`）；`GET /static/../server.py` 返回 **31482 字节源码**。校验 `str(path).startswith(str(STATIC_DIR))` 对 `...\static\..\app.db` 这种**未 resolve 的字符串前缀**成立，因此被放行。百分号编码（`%2e%2e%2f`）因 `urlparse` 不解码而 404，即防护只是"碰巧"，不是设计。
- 复现：`curl --noproxy '*' -sS --path-as-is -o NUL -w "%{http_code} %{size_download}\n" "http://127.0.0.1:8765/static/../app.db"`
- 证据：`tools/_qa_dyn_out.txt:137-138`
  ```
  /static/../app.db | status=200 | bytes=917504 (declared=917504) | ctype=text/html; charset=utf-8
      body[:400] = SQLite format 3 ... indexix_positions_symbolpositions...
  /static/../server.py | status=200 | bytes=31482 ...
  ```
- 建议修法：
  ```python
  p = (STATIC_DIR / rel).resolve()
  if not p.is_file() or STATIC_DIR.resolve() not in p.parents:
      self.send_error(404); return
  ```
  并把 `rel` 白名单化（`app.js`、`app.css`、`themes/*.css`），同时把 `Content-Type` 从固定 `text/html` 改为按扩展名推断。（安全线 #6 可能同时报此项，口径以可复现证据为准。）

### P1-1【P1 严重】`do_GET` 无异常兜底：参数异常时**连接被直接关闭（0 字节）**，客户端拿不到任何状态码

- 位置：`viewer/server.py:557`（`do_GET` 全函数无 `try/except`）；触发点 `598`（`int(min_holders)`）、`603`（`int(limit)`）、`627`（`int(id)`）、`546-550`（`/static/` 把目录当文件 `read_bytes()`）
- 现象：以下请求返回 **HTTP 层零字节**（`EMPTY_REPLY`），浏览器 `fetch()` 表现为 `TypeError: Failed to fetch`，无 4xx/5xx 可观测；服务进程**不崩溃**（探针后再打 `/api/summary` 仍 200）。
  - `/api/consensus?min_holders=abc`、`=2.5`、`=%20`（单个空格）
  - `/api/posts?limit=abc`（`?limit=abc&limit=5` 同样触发：`parse_qs()[0]` 取到 `abc`）
  - `/api/detail?kind=post&id=abc`、`id=1.0`
  - `/static/`（`rel=''` → `STATIC_DIR` 本身，`exists()` 为真、前缀校验通过 → 对目录 `read_bytes()` 抛异常）
- 复现：`curl --noproxy '*' -sS -o NUL -w "%{http_code}\n" "http://127.0.0.1:8765/api/posts?limit=abc"` → 空输出（0 字节）
- 证据：`tools/_qa_dyn_out.txt:61-64,73,121,124,13`
  ```
  /api/posts?limit=abc | !! EMPTY_REPLY: 服务器未返回任何字节（连接被直接关闭） | status=None | bytes=0
  /static/ | !! EMPTY_REPLY: 服务器未返回任何字节（连接被直接关闭）
  canary /api/summary status=200 ok=True   （服务线程级存活）
  ```
- 建议修法：`do_GET` 外层包 `try/except Exception`，统一 `_send_json({"error": ..., "code": 400/500}, code=...)` 并 `logging.exception`；把参数解析换成 `_qint(q, key, default, lo, hi)` 安全函数（非法值 → 400 JSON），`_send_file` 先判 `is_file()`。注意：`BaseHTTPRequestHandler` 只在 `socketserver.handle_error` 打印 traceback，客户端侧无任何可诊断信息——这是本次最影响可维护性的缺陷。

### P1-2【P1 严重】人物列表被静默截断（170 → 100 → 前端 80），界面却宣称"不遗漏任何一个名称"

- 位置：`viewer/server.py:155-158`（`LIMIT 100`）；`viewer/static/app.js:246`（`list.slice(0, 80)`）、`app.js:250`（"名称与渠道一一对应，不遗漏任何一个名称"）
- 现象：`/api/summary.counts.persons` = **170**，`/api/persons` 只返回 **100** 条，页面再截到 **80** 条，且无任何"已省略 N 条"提示；用户无法知道有 90 个人物不可见。
- 复现：`curl --noproxy '*' -sS "http://127.0.0.1:8765/api/persons" | python -c "import sys,json;print(len(json.load(sys.stdin)))"` → `100`
- 证据：`tools/_qa_dyn_out.txt:151,157`
  ```
  MISMATCH: counts.persons vs len(/api/persons)  (170 vs 100)
  [len] ... persons=100 (LIMIT 100)
  ```
- 建议修法：接口加 `limit/offset`（默认与现网一致，非法上限 clamp），前端在表格下方显式提示"显示前 80 / 共 170 条"，或补"加载更多"。

### P2-1【P2 一般】`hold_days`（已持有 N 天）与 `hold_since`（持仓起始日期）自相矛盾，大量显示"已持有 0 天"

- 位置：`viewer/server.py:413-416`（对每条持仓把 `hold_since` 统一改写为该股最早建仓日，再用**该条记录自己的** `sell_time/buy_time/post_time` 算 `hold_days`，且 `_hold_days` 内部 `max(...,0)`）；展示位置 `app.js:166`、`app.js:833`、`app.js:875`
- 现象：同一只股票、同一个"持仓起始日期"，各选手的"已持有 N 天"互不相同且常为 0；当该记录 `buy_time` 早于 `hold_since` 时被截成 0。以今日 2026-09-22 计：
  - `600519` `hold_since='2026-09-18'`（应 4 天）→ 12 条持仓中 **10 条 hold_days=0**（如 打板小马 buy_time=2026-08-14 → 0 天）
  - `300750` `hold_since='2026-09-05'`（应 17 天）→ hold_days 分别 1 / 6 / 15 / 16 / 0
  - `000333` `hold_since='2026-09-08'`（应 14 天）→ hold_days 13 / 0 / 1 / 11 …
  - `601318` `hold_since='2026-07-18'`（应 66 天）→ hold_days 2 / 52 / 59 / 34 / 4 / 43 / 56 / 41
- 复现：`tools/_qa_dyn_probe2.py`（E 段，逐条打印 `hold_since/buy_time/sell_time/hold_days` 与 hold_since → 今日的天数）
- 证据：`tools/_qa_dyn_out2.txt:75,88`
  ```
  [600519] 情绪观察 action=HOLD hold_since='2026-09-18' hold_days=0 buy_time='2026-09-16 21:53' (since->今日应=4)
  [601318] 价值投资人 action=HOLD hold_since='2026-07-18' hold_days=2 buy_time='2026-07-20 10:00' (since->今日应=66)
  ```
  另：`600519` 打板小郭 `hold_since='2026-09-18'` 而 `buy_time='2026-08-13'`，同一响应内"该标的首次建仓时间"与"建仓时间"互相矛盾。
- 建议修法：`hold_days` 统一为 `hold_since → (已清仓 ? sell_time : 今日)`；若确实需要"该笔持仓天数"，另起字段 `own_hold_days` 并改名，避免前端把两种口径并排展示。

### P2-2【P2 一般】`/api/person` 不返回 `channel`，人物抽屉里的渠道徽章恒为"A 实盘大赛"

- 位置：`viewer/server.py:471-475`（`SELECT site, symbol, symbol_name, ratio, action, post_url, buy_time, ...`，无 `channel`）；`viewer/static/app.js:834`（`class="badge ch-${esc(p.channel || 'A')}"`）
- 现象：`/api/person?name=短线作手老王` 的 `positions[]` 字段为 `action,buy_price,buy_time,current_price,hold_days,hold_since,post_url,ratio,sell_price,sell_time,site,symbol,symbol_name`——**没有 `channel`**，前端回退默认 `'A'`，因此无论该选手实际渠道是 A/B/C/D，人物抽屉里全部渲染成"A 实盘大赛"，同时 `tier/platform` 也无从展示。
- 复现：`curl --noproxy '*' -sS "http://127.0.0.1:8765/api/person?name=%E7%9F%AD%E7%BA%BF%E4%BD%9C%E6%89%8B%E8%80%81%E7%8E%8B"`
- 证据：`tools/_qa_dyn_out3.txt:7`（positions item_keys 无 `channel`）；对照 `tools/_qa_dyn_out.txt:51`（`/api/positions` 同名记录 `channel` 有值）
- 建议修法：`api_person` 的 positions 查询补 `channel`（顺带 `tier`/`platform`，`_player_index()` 已现成可用）。

### P2-3【P2 一般】`/api/posts` 的 `limit` 无上限，`-1` 即全表

- 位置：`viewer/server.py:601-608`
- 现象：`?limit=-1`（SQLite `LIMIT -1` = 不限制）与 `?limit=999999999` 都返回**全部 185 行、38019 字节**；数据量增长后该接口会成为放大器（当前已是 `/api/posts` 默认响应的 9.3 倍）。
- 复现：`curl --noproxy '*' -sS "http://127.0.0.1:8765/api/posts?limit=-1" | python -c "import sys,json;print(len(json.load(sys.stdin)))"` → `185`
- 证据：`tools/_qa_dyn_out.txt:77-79`
  ```
  /api/posts?limit=-1 | status=200 | bytes=38019 | json=list n=185
  /api/posts?limit=999999999 | status=200 | bytes=38019 | json=list n=185
  ```
- 建议修法：`limit = max(1, min(int(limit), 200))`，越界一律 clamp 或 400。

### P2-4【P2 一般】前端对 `overseas_sources.status` 的判定与后端实际取值不匹配，「已连通但无条目」被误标成红色错误

- 位置：`viewer/static/app.js:508-512`；后端取值见 `viewer/db.py`（`overseas_sources_status`）
- 现象：后端实际 status 集合 = `{已接入, 未启用, 待激活（需 VPN）, 待激活（需 API Key）, 已连通但无条目, 抓取失败}`。前端只匹配 `已接入 / 示例模式 / 待激活 / 未启用`，其余落到 `src-err`（错误样式）。因此：
  - **`示例模式` 分支永远不可能命中**（当前 0 条，属死代码，提示文案"示例模式·未联网"已过期）；
  - `已连通但无条目`（1 条）不是错误，却被渲染为错误态；
  - 只有 `抓取失败`（1 条）该是错误态。
- 复现：`tools/_qa_dyn_probe.py`（`/api/overseas_sources` 探针会打印 status 分布）
- 证据：`tools/_qa_dyn_out.txt:167`
  ```
  [overseas_sources] 条数=41 status 分布={"未启用": 5, "已接入": 26, "待激活（需 VPN）": 7, "已连通但无条目": 1, "抓取失败": 1, "待激活（需 API Key）": 1}
  ```
- 建议修法：改为枚举→样式映射表（`{"已接入":"src-on","已连通但无条目":"src-wait","待激活（需 VPN）":"src-wait",...,"抓取失败":"src-err"}`），删除 `示例模式` 死分支，并让"已连通但无条目"显示条数为 0 的中性说明。

### P2-5【P2 一般】三个 GET 接口是死接口（前端零引用），其中 `/api/positions` 每次回 25KB

- 位置：`viewer/server.py:594`（`/api/positions`，25381B / 135 行）、`622`（`/api/stock_names`，list[str]）、`609`（`/api/login_status`，与 `/api/summary.login_status` 重复）
- 现象：在 `viewer/static/` 全目录检索 `api/positions`、`api/stock_names`、`api/login_status` **零命中**（仅 `api/stock`、`api/operating` 有引用）。前端实际走 `/api/summary.login_status`、`/api/consensus` 获取等价数据。
- 复现：`tools/_qa_dyn_out3.txt`；或用编辑器全局检索 `api/positions` 于 `viewer/static` → 0 命中
- 证据：`tools/_qa_dyn_out.txt:51,81,107`（三个接口均 200 且数据完整，但无消费者）
- 建议修法：删除或标注为"调试/兼容接口"；`/api/login_status` 建议直接由 `/api/summary` 覆盖。

### P3-1【P3 建议】错误一律 `HTTP 200 + {"error": ...}`，无法区分"业务空结果"与"参数错误"

- 位置：`viewer/server.py:382-383,461-462,500-501,518,531-532,630`
- 现象：清单实测（`tools/_qa_dyn_out4.txt:13-22`）——
  `/api/stock`→`symbol required`；`/api/stock?symbol=`→`symbol required`；`/api/person?name=`→`name required`；`/api/player?name=`→`name required`；`/api/player?name=__x__`→`player not found`；`/api/detail`→`not found`；`/api/detail?kind=bogus&id=1`→`unknown kind`；`id=-1/999999999`→`not found`。全部 **HTTP 200**。另：`/api/stock?symbol=___nope` 返回 **200 完整空壳**（含 `market='未知板块'`）而不报错。
- 建议修法：缺参→400、不存在 kind/symbol→404 或 200+`{"error", "not_found": true}` 二者择一并让前端统一处理。

### P3-2【P3 建议】404 页泄露服务实现细节

- 位置：`viewer/server.py:630`（`self.send_error(404)`）
- 现象：`HTTP/1.0 404 Not Found`，响应头带 `Server: BaseHTTP/0.6 Python/3.13.14` 与 `Date`；Body 为英文通用错误页（`<h1>Error response</h1>`），与站点中文界面风格不一致。
- 复现：`curl --noproxy '*' -sSI "http://127.0.0.1:8765/api/nonexistent"`
- 证据：`tools/_qa_dyn_out2.txt:64-65`
  ```
  status_line='HTTP/1.0 404 Not Found'
  headers={"server": "BaseHTTP/0.6 Python/3.13.14", ...}
  ```
- 建议修法：重写 `version_string()`/`server_version` 为中性值，404 改为 JSON 错误体（`{"error":"not found"}`）；顺带把 `protocol_version` 显式设为 `HTTP/1.1` 以便 keep-alive。

### P3-3【P3 建议】同名"共同持仓人数"在两个接口口径不同（是否排除已清仓）

- 位置：`viewer/db.py:638-660`（`consensus_stocks` 对累计卖出 ≥100% 的选手记 `cleared=True` 并不计入 `holders`）vs `viewer/server.py:429`（`api_stock` 的 `holders` = positions 里 player 去重，**不排除已清仓**）
- 现象：`000333` → `/api/consensus.holders=9`（该股 2 条明细 `cleared=True`），而 `/api/stock.holders=10`、`/api/positions` 去重=10。两个数字在 UI 上都叫"共同持仓"（`app.js:216`、`app.js:386`）。
- 复现：`tools/_qa_dyn_probe3.py`（B 段）
- 证据：`tools/_qa_dyn_out3.txt:56`
  ```
  000333: /api/stock.holders=10 /api/positions 去重=10 /api/consensus.holders=9 consensus 明细players=11 其中 cleared=True 的=2
  ```
  注：`688981` 因无任何持仓，`consensus.holders` 为 0（被 `min_holders=1` 过滤）属**正确**行为，已核对。
- 建议修法：统一为"排除已清仓"，或在 UI 把 `/api/stock` 的数字改成"持仓人数（含已清仓）"以明示差别。

### P3-4【P3 建议】海外热点存在重复事件（两条 title 完全相同）

- 位置：`viewer/db.py`（`overseas_hotspots.key` 按 topic 分桶）→ `viewer/server.py:287-306`
- 现象：`key=geo_conflict` 与 `key=oil_energy` 两条热点的 `title` 完全相同：「Brent crude jumps as geopolitical tension raises oil supply risk」（heat 1071.5 / 339.5，confidence 0.87 / 0.72），同一事件被两个主题各产出一条卡片。
- 复现：`tools/_qa_dyn_probe2.py`（H 段会打印"重复标题"）
- 证据：`tools/_qa_dyn_out2.txt:121`
  ```
  重复标题：{"Brent crude jumps as geopolitical tension raises oil supply risk": ["geo_conflict", "oil_energy"]}
  ```
- 建议修法：生成阶段按 `(title 归一化, 主驱动源)` 归并，或按 topic 主次合并为一条并列出多主题标签。

### P3-5【P3 建议】`symbol` 等字符串参数无长度上限

- 位置：`viewer/server.py:621`（`symbol`）、`613/617`（`name`）
- 现象：`/api/stock?symbol=<10000 个 A>` 返回 200 并回显完整 10000 字符（响应 10302B）；`/api/person`/`/api/player` 的 `name` 同理无长度限制。当前无报错，但可被用于制造大响应/无意义查询。
- 复现：`tools/_qa_dyn_probe.py`（`/api/stock?symbol=` + `"A"*10000` 用例）
- 证据：`tools/_qa_dyn_out.txt:105-106`
- 建议修法：`symbol` 限长 16 且校验 `^\d{6}$`（或明确允许的代码形态）；`name` 限长 64。

### P3-6【P3 建议】请求行中的非 ASCII 字节未做重解码（仅影响直连 API 的调用方）

- 位置：`viewer/server.py:558-559`（`urlparse` 直接使用 `self.path`，而 `BaseHTTPRequestHandler` 按 latin-1 解码请求行）
- 现象：**不**做百分号编码、直接把 UTF-8 字节放进 URL 时（如脚本/curl 手写中文），服务端得到 `ç¿­çº¿ä½æèç`，`/api/person?name=短线作手老王` 与 `/api/player?name=大阪柯小李` 均返回"查不到人/player not found"。改用 `%E7%9F%AD...` 编码后一切正常（`app.js:821/849` 用了 `encodeURIComponent`，**真实用户无影响**）。
- 复现：对比 `tools/_qa_dyn_out2.txt:36-47`（未编码 → 乱码/not found）与 `tools/_qa_dyn_out3.txt:2-11`（编码 → 正常）
- 证据：
  ```
  [person] /api/person?name=短线作手老王 -> name: "ç¿­çº¿ä½æèç"  posts n=0
  [person-短线作手老王] /api/person?name=%E7%9F%AD... -> name: "短线作手老王"  posts n=2
  ```
- 建议修法：文档注明"非 ASCII 参数必须百分号编码"；或在 handler 内对 `path` 尝试 `latin1→utf8` 重解码兜底。

---

## 四、未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| 全部 POST 写接口（`/api/login_check`、`/api/fetch`、`/api/demo`、`/api/overseas`、`/api/sync_names`） | 只读约束（会写库/联网抓取），仅静态核对了 `_do_POST` 637-687 的路由、写锁与异常兜底逻辑（这 4 个写接口**有** try/except 返回 500 JSON，与 GET 形成鲜明对比） |
| `HEAD` / `OPTIONS` / `PUT` 等非 GET 方法 | 只读约束（约定只发 GET）。静态判定：`Handler` 未实现 `do_HEAD`/`do_OPTIONS`，`BaseHTTPRequestHandler` 会返回 **501** 英文错误页 |
| `/api/detail?kind=news` 的真实命中样例 | 示例库 `news` 表 3 行但 `id=1..3` 均 not found（id 非从 1 起），未穷举 id 空间以免产生无意义请求；`kind=news` 的 SQL（`server.py:529`）为 `SELECT * FROM news`，字段随表结构 |
| 服务端 stderr 的 traceback 文本 | 无法读取运行实例（pid 26300）的控制台/stdout。P1-1 的结论基于**静态**（`do_GET` 无 try/except）+ **客户端零字节**双重证据，未直接取得 traceback 文本 |
| 并发/压测/长稳（SQLite 写锁、内存增长） | 属性能专项，本轮只做功能与正确性；未制造并发写（写库） |
| 浏览器端真实渲染与交互 | 属 UI 回归线（任务 #8），本轮只做接口层与前端字段对账 |
| 直接读 `viewer/app.db` 校验 | 主动回避（避免任何写入/WAL 副产物风险），全部数值以接口返回为准 |

---

## 五、优化建议（生产环境典型问题视角）

1. **可观测性几乎为零（最高优先级）**：`log_message` 被整体静音（`server.py:689-690`），既无访问日志也无异常落盘；再叠加 P1-1 的"零字节响应"，线上排障只能靠猜。建议：结构化访问日志（method/path/status/耗时/UA）+ `logging.exception` + 请求 ID 回传到响应头。
2. **统一错误处理层**：GET 与 POST 的错误兜底完全不对称（POST 有 try/except→500 JSON，GET 什么都没有）。建议在 `Handler` 层加统一 `try/except` + 参数校验装饰器，把 P1-1、P3-1、P3-2 一并解决。
3. **响应体积与缓存**：单次刷新前端要拉 `/api/overseas_hotspots`（78.5KB）、`/api/players`（55.6KB）、`/api/consensus`（44.9KB）、`/api/posts`（无限长可达 38KB+），合计约 270KB 且每次全量重算。建议加 `ETag`/`Last-Modified` + 条件请求，并给 `/api/positions`、`/api/stock_names`、`/api/login_status` 这类死接口做减法。
4. **分页与上限口径统一**：`persons` LIMIT 100（前端再切 80）、`operating` LIMIT 200、`topics` LIMIT 30、`sentiment.map` 前 30、`posts` 无上限、`consensus` 无上限——建议统一为 `limit/offset`（默认值不变，上限 200），并在响应里回 `total/has_more`，避免 P1-2 这类"静默截断 + 界面承诺不遗漏"的冲突。
5. **数据增长预案**：`overseas_news` 已 707 行、`overseas_sentiment_daily` 77 行、`posts`/`stock_mentions` 各 185 行；`overseas_sentiment` 一次返回 news 40 + source_mix 35 + daily 77 全量聚合，随天数线性增长。建议对 `(day, region, source_name)` 建索引并把 `daily` 默认收敛到近 30 天（带 `days` 参数）。
6. **字段语义固化**：`hold_since`/`hold_days`（P2-1）、`holders`（P3-3）这类"同名不同算"的字段建议在接口层统一并在 UI 文案中标注口径，避免多页面同一名词两个数字。
7. **安全收口（与安全线 #6 联动）**：P0-1 的静态目录穿越必须优先修；另外 `_send_json` 对所有响应写入 `Access-Control-Allow-Origin: *`（`server.py:542`），任意网页在用户本机可读取本地库数据，建议收紧为同源或加一次性 token。
8. **本地服务韧性**：虽然实例绑定 `127.0.0.1`，但异常导致连接被硬关会让前端"整块面板空白且无提示"。前端 `api()`（`app.js:12`）建议加错误分支（`r.ok` 判断 + `catch`），后端异常时至少给一个可读错误。

---

## 六、结论摘要

- 路由覆盖：19 个 GET 路由 + 3 个静态入口 + 边界用例共 76 条，**72 条通过**；4 类请求返回零字节（P1-1）。
- 数据自洽：18 项交叉校验中 **16 项通过**，2 项不一致（`persons` 截断、`000333` 共同持仓口径）。
- 字段对账：前端读取字段**仅 1 处缺失**（`/api/person.positions[].channel`）。
- 前后端字段冲突：`overseas_sources.status` 前端判定与实际取值不匹配（P2-4），`示例模式` 分支为死代码。
- 问题合计 **14 条**：P0 × 1、P1 × 2、P2 × 5、P3 × 6。全部为实测复现，无"可能有问题"式条目。
