## D53 删除无用字段：geo.certainty 与 API 外抛的 confidence（2026-10-31）

**D48 把「确定性 82%」换成按信源分档后，字段本身成了死代码**。进哥拍板「没用就删」。

| 字段 | 处理 | 理由 |
|---|---|---|
| `geo_events.certainty` | **全链路删除**（表列 DROP + 写入移除 + API 不再输出） | 13 档全是人工手填常量，前端已不读；留在库里只会诱导后来者再拿它当"模型把握度" |
| `overseas_hotspots.confidence` | **仅停止外抛**（API 两处输出删掉），库列保留 | predictor 用它做热度加权，属内部权重；界面按信源家数分档，不需要这个数 |

**迁移安全**：`ensure_schema()` 检测到 `certainty` 列时，先把 `eid/title/certainty` 导出到
`backups/geo_certainty-<时间戳>.json`，再 `ALTER TABLE ... DROP COLUMN`（SQLite 3.53 支持）。
`_shape()` 里另加 `pop("certainty")` 兜底，保证旧库快照 / 未迁移环境也不会吐出该字段。
实测：接口返回 `certainty` = False；库文件列已消失；备份 `geo_certainty_20261031-211417.json`
含 46 条记录。

---
