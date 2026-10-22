# 股票论坛可视化查看器（Stock Forum Viewer）

> ⚠️ **免责声明（P2-36，钉死）**：本工具为**个人研究用途**，所有数据、信号与结论仅供学习参考，
> **不构成任何投资建议**。据此操作风险自负。**请勿外发。**

本地 Web 服务 + SQLite 的数据可视化查看器：聚合股票论坛（雪球 / 东财 / 微博等）舆情、龙虎榜、
板块资金流、博主荐股、政策风向、共振研判等，提供一个本地只读的研究面板。

## 启动

- 双击 `start_viewer.bat`（Windows），或：
  ```bash
  .venv/Scripts/python.exe -m viewer.server
  ```
- 浏览器打开 http://127.0.0.1:8765

## 工程化收口说明（2026-10-22）

本轮收口聚焦「后端 / 测试 / 工程化」，关键改动：

- **P0-05 启动副作用收口**：`import viewer.server` 不再有任何模块顶层副作用
  （不再写库 / 不拉调度线程 / 不预热）。所有启动动作收口到 `viewer.server.boot()`，
  仅由 `main()` / desktop_app 在真正启动服务时调用。
- **P1-45 写接口令牌**：经 `main()` 启动默认启用随机令牌（HMAC 恒定时间比较，防子串绕过），
  落盘于 `config/viewer_token.txt`；非浏览器 / 测试客户端保持关闭（向后兼容）。
- **P1-39 重写入并发**：`/api/fetch`、`/api/login_*`、`/api/edge/restart` 等重写入走空上下文，
  不再阻塞加自选 / 改设置 / 归档；互斥由 `INGEST_LOCK` + `busy_timeout` 保证。
- **P3-06 HTTP/1.1**：启用 HTTP/1.1，避免并发请求全量重连。
- **P3-19 版本缓存**：`/api/version` 的 `app-version` 用 `st_mtime` 缓存，避免 60s 轮询每次重解析。
- **P1-50 / P1-51 配置与运维端点**：`/api/config/get`、`/api/config/set`、`/api/open_dir`、
  `/api/backup`、`/api/backup/restore`、`/api/lhb/backfill`、`/api/shutdown`（前端设置面板已接调用）。
- **数据迁移（P1-36）**：`migrate.py` 基于 `PRAGMA user_version` 的有序、幂等迁移，迁移前自动快照。
- **基础设施（infra/）**：`fmt`（数值格式化）、`httpclient`（带超时/退避的 HTTP 客户端）、
  `cal`（交易日历 / 交易时段）、`cache`（单调时钟 TTL 缓存）。

## 红线（绝对不动）

WAL + `busy_timeout=15000`、Origin/Host/Content-Type 三道写防护、`build_id` 代码指纹、
缺陷探针（`defect_probe.py`）防护登记、诚实 UI（无伪造数据）、`api()` 统一出口、
TTL 缓存 / gzip / 慢任务进程化、离线降级行为、指标层不重写。

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q
```

门禁：全量回归 0 失败（超时由 `pytest.ini` 的 `timeout = 120` 守护，防挂死）。
