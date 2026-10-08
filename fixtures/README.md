# fixtures/ — 真实行情离线夹具（阶段2）

本目录存放**真实历史行情夹具**，用于在沙箱 / CI（无网）环境做「Oracle 真值对照」，
取代任何合成 / 假数据。红线：本目录下的 `*.json` 必须是真实数据，且带
`"is_real_data": true` 与内嵌 `sha256`；`load_fixtures.py` 加载时校验，伪造一律拒绝。

## 文件说明

| 文件 | 作用 |
|------|------|
| `collect_real_quotes.py` | **王总本机·有网·一次性**采集脚本，生成 `oracle_<symbol>.json` |
| `load_fixtures.py` | 离线加载框架：sha256 + is_real_data 校验；缺失/伪造 → 抛异常（测试据此 skip） |
| `_schema_template.json` | 夹具 JSON Schema 模板（**非真实数据**，仅描述结构） |
| `.gitkeep` | 占位，保留空目录纳入 git |
| `oracle_*.json` | 真实夹具（由采集脚本生成后入库；涉密则按 Q7 仅留 `.gitkeep`） |

## 采集步骤（王总本机，需先 `pip install akshare`）

```bash
# 进入项目根
cd stock-forum-scraper

# 1) 常规标的（建议覆盖 2~3 只代表性标的）
.venv/Scripts/python.exe fixtures/collect_real_quotes.py \
    --symbol 600000.SH --start 2023-01-01 --end 2023-12-31 --source akshare

.venv/Scripts/python.exe fixtures/collect_real_quotes.py \
    --symbol 000001.SZ --start 2023-01-01 --end 2023-12-31 --source akshare

# 2) 含 1 只曾退市/ST 样本（触发幸存者偏差 / 强平探针 P2）
.venv/Scripts/python.exe fixtures/collect_real_quotes.py \
    --symbol 某退市.SH --type delisted --start 2022-01-01 --end 2023-12-31 --source akshare

# 或一次采集多只
.venv/Scripts/python.exe fixtures/collect_real_quotes.py \
    --symbols 600000.SH,000001.SZ --start 2023-01-01 --end 2023-12-31
```

## 校验（沙箱 / CI / 本机通用，离线）

```bash
.venv/Scripts/python.exe fixtures/load_fixtures.py
# 输出：已加载 N 个真实夹具 / 或「诚实降级: 未找到...」（沙箱无网时）
```

## 数据源（Q3）

默认 **akshare**（免费、免 token）。切换 `--source tushare` 需在 `viewer/.tushare_token`
写入 token。Oracle 真值在采集机独立核算后冻结进 `expected`，消费端不依赖源。

## 涉密处理（Q7）

若数据涉密不宜入库：仅提交 `_schema_template.json` + `README.md` + `.gitkeep`，
真实 `oracle_*.json` 本地保留，并在仓库 `.gitignore` 追加 `fixtures/*.json` 排除。
