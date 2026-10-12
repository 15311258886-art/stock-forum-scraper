# 自动化执行记忆 — 股票论坛舆情查看器 / KB 重吸取

## 任务
执行 `tools/kb_ingest.py --json`，按 content_hash + parser_version 双条件判断是否重建
doc_key=zdx_stock（track=中短线炒股）的知识库内容。

## 运行环境备注
- `.venv/Scripts/python.exe` 存在且可用，无需回退到系统 python。
- 首次执行时本记忆文件不存在，无历史对比基线。

## 执行记录

### 2026-09-25 09:00
- 结果：skipped=true, changed=false（内容未变，未重建）。
- 上次吸取时间：2026-09-23 22:03:17；reload_count=2。
- 库内基线：version=v5.0, node_count=1004, parser_version=2。
- 上游文件 mtime：2026-09-22 11:48:24（与上次吸取时一致）。
- 无错误、无异常，未做任何写操作。

### 2026-09-26 09:07
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21（本任务首次记录到该来源）；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32。
- 无错误、无异常，未做任何写操作。

### 2026-09-27 10:42
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32（未变）。
- 无错误、无异常，未做任何写操作。

### 2026-09-28 09:01
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32（未变）。
- 无错误、无异常，未做任何写操作。

### 2026-09-29 09:44
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32（未变）。
- 无错误、无异常，未做任何写操作。

### 2026-09-30 10:02
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32（未变）。
- 无错误、无异常，未做任何写操作。

### 2026-10-01 13:39
- 结果：两个来源均 skipped=true, changed=false（内容未变，未重建）。
- zdx_stock（中短线炒股）：上次吸取 2026-09-23 22:03:17；version=v5.0, node_count=1004, parser_version=2, reload_count=2；上游 mtime 2026-09-22 11:48:24（未变）。
- value_basics（基础常识）：上次吸取 2026-09-26 02:14:21；version=v1.1, node_count=106, parser_version=2, reload_count=3；上游 mtime 2026-09-26 02:11:32（未变）。
- 无错误、无异常，未做任何写操作。连续第 7 次双来源 skip。

## 后续判断要点
- 知识点基线 1004 条；若后续 node_count 较此减少 >10%（<904），需提示"上游改版导致解析器记号失配，建议检查"。
- 上游文件 mtime 自 2026-09-22 11:48:24 起未变动，若某次出现 changed=true 且 parser_bumped 不为真，即为上游新版本导出。
- 脚本返回 status=error 且提示文件找不到时，处理方向：修改 config/kb_sources.yaml 中该来源的 path_glob。
- 第二个来源 value_basics（基础常识）基线 node_count=106（v1.1）；减少 >10%（<96）同样提示上游改版。
- 上游文件已连续 8 天（09-23 起）零变动，属正常待更状态，无需干预。
- 09-25 至 09-30 连续 6 次执行结果完全一致（双来源 skip），无一次重建。
