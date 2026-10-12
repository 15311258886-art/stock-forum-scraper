# 股票论坛可视化查看器 · 项目记忆

本地 SQLite + HTTP API + 单页前端项目。路径 `F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`。
桌面快捷方式 `股票论坛可视化查看器.lnk` → 双击开原生窗口（pywebview 内嵌 127.0.0.1:8765）。

## 项目结构要点
- 数据层 `geo/`：`store.py`(geo_events 表 建表/幂等导入/查询/统计)、`seed_events.json`(研判事件种子)、`taxonomy.py`(七类口径)。
- 后端 `viewer/server.py`：所有 `/api/*`；`DB=Database()` 实例才有 `.conn`。
- 前端 `viewer/static/`：`app.js`(NAV 是唯一事实来源，新增板块三步：NAV 加项→index.html 加 `#p-<tab>` 容器→loadAll 加 fetch)、`index.html`、`app.css`。
- 重启：`restart_viewer.py`（仅杀 8765 上的 python 后拉 `desktop_app.py`）。

## 本机测试必读坑位（每次都踩）
1. 代理 `127.0.0.1:59378` 不可达 → 测本地服务须绕过代理：urllib `build_opener(ProxyHandler({}))` 或 `NO_PROXY=127.0.0.1,localhost`。curl 默认走代理会空响应。
2. `desktop_app.py` 的服务在守护线程、随窗口 `os._exit` 退出；无头环境测后端请用 `python -m viewer.server`（后台）。
3. Bash 须前缀 `export PATH="/usr/bin:/bin:/mingw64/bin:$PATH"`。
4. node --check 用 `cd`+相对路径（原生 node 会把 `/f/...` 解析成 `f:\f\...`）。
5. Bash 里的 `curl` 是 **Windows 原生** `C:\Windows\system32\curl.exe`，不认 MSYS 的 `/tmp/...` 路径（会当字面量写到 `C:\tmp\...`，该目录不存在 → `-o` 静默丢输出）。取接口返回体请**直接管道给 python** 解析，或写工作目录下的相对路径。
6. 定时抓取接口在 **8765**（不是 5000）。`/api/login_check` 约 10s，`/api/fetch` 全站约 2.5 分钟，`-m` 分别给 60 / 300。
7. **跑 main.py 必须用 `./.venv/Scripts/python.exe`**（项目 venv，含 bs4）。默认 `python` 是 `.workbuddy` 托管解释器 3.13，**无 bs4**，直接跑会 `ModuleNotFoundError: No module named 'bs4'`。
8. `main.py` CLI 只写 `./data/*.json` + `archive/股票论坛抓取历史存档.xlsx`，**不写 viewer/app.db**；看板数据在服务跑 `/api/fetch` 时才更新。服务死掉时 CLI 兜底能出数，但 app.db 不会长。
9. 服务常驻性：`.server.pid` 里的 PID 若已不存在 + 8765 无监听 = 服务已随桌面窗口关闭而退出（`desktop_app.py` 用 `os._exit`）。

## /api/fetch 计数口径（写报告必读）
- 返回 `results[]` 里每站字段 `raw_pages/news/trades/posts/players/positions` 是**解析条数**（按页固定量，非净新增）；upsert 幂等，同一页面重复抓取不新增行。
- `refresh_status` 里 taoguba 的 `item_count` = 上述整数字段之和（如 3+5+2+20+14+0 = 44），可用它反查某轮是否跑过。
- 判断"是否真有新数据入库"要对比 `/api/summary` 的 `counts`（COUNT(*) 口径）：本轮 posts 2478 / trades 180 / news 1599 在两次 fetch 间**未变**，说明 11:03 那轮已入库完毕。

## 国外信源策略
WebFetch 直连国外站被代理拦截(502/fetch failed)；唯一可用国外源采集通道是 **WebSearch 服务端联网**，结果沉淀为带 URL 的可点外链（见 geo_events 的 sources/cross_check）。东财行情接口同样被代理关闭，个股代码无法联网复核时按知名度标准命名并标注"待复核"。
