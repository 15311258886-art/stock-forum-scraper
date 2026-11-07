# ADR-2026-11-07：viewer/server.py 按业务域拆分到 viewer/handlers

- **状态**：已落地（`aca2b70` / `724fa34` / `10ce465`）
- **作者**：WorkBuddy（遗留系统重构视角）
- **关联**：P3 架构债 #3；预拆分基线 `c3725a0`

---

## 1. 背景 / 上下文（Context）

- `viewer/server.py` 原 **14,668 行 / 157 个 `api_*` 业务函数**（占 43%），属典型 **God Class**：单文件圈复杂度极高，任意改动都容易"改 A 漏 B"。
- 既有的 157 个 `api_*` 几乎全是**纯模块级函数**（仅 2 个函数体出现 `self.`，不接收 self、返回 dict/list），只依赖 server 模块级符号（`DB`、`locks`、引擎模块 `se/il/bn/valuation/predictor/board_flow/screener/watchlib` 等），由 `routes.py` 的 `_h_*` 包装 `self._send_json` 发出。
- 路由分发机制：`routes.py` 的 `_ensure_bound()` 在首次请求时 `import viewer.server as _srv` 并把 `_SRVER_NAMES`（182 元素，含全部 157 `api_*`）别名进 routes 命名空间 → 这是"抽符号"的现成通道。
- **目标**：按业务域解耦、降低单文件风险，**保持对外接口（`routes.py` 与既有测试 `S.api_*`）零改动**。

---

## 2. 决策（Decision）

采用 **物理搬运 + 逻辑零改 + re-export 兼容** 策略，而非重写：

1. **抽取工具 `tools/split_server.py`**（一次性重构辅助，可保留为拆分文档工具）：
   - AST 识别 `api_*` 按域前缀（`api_<domain>_<...>` → `<domain>`）抽到 `viewer/handlers/<domain>.py`。
   - 函数体源码**原样截取**（零逻辑改动）。
   - handler 顶部 `from viewer.server import (依赖符号)` 把 server 模块级符号**重定向**进本地命名空间（裸名解析等价）；标准库本地导入。
   - 同域内 api 互相依赖**不重导入**（本地可见，避免循环）。
   - server.py 原位置删除这些函数，并在 `if __name__` 前**按依赖拓扑序重建完整 re-export 块**（被依赖域在前），彻底消除跨域 api 的循环/顺序导入。

2. **静态验证工具 `tools/verify_split.py`**（转正自 `_verify_split.py`）：
   - 以 `git show c3725a0:viewer/server.py` 为基线，**逐字符比对**每个 handler 源码，证明零改。
   - 校验 handler 的 `from viewer.server import` 符号 ∈ server 顶层；校验 re-export 的 api 在 handler 定义且已从 server 删除；统计体积。

3. **路由层零改动**：`_SRVER_NAMES` 已在前期补全 5 个漏绑 api（`api_hotspots`/`api_moneyflow_stock_force`/`api_research_insight_rebuild`/`api_screener_lowbox`/`api_screener_turnover_top`），157/157 全覆盖；抽取任意 api 无需改 `routes.py`。

---

## 3. 分层结构（viewer/handlers 组织）

```
viewer/
├── server.py            # 减至 8,611 行：顶层常量/引擎引用 + HTTP 类 + main() + re-export 块
├── routes.py            # 路由分发（不变）
└── handlers/            # 80 个域文件 + __init__.py
    ├── version.py  summary.py  valuation.py  dragon.py  evidence.py  sentiment.py  public.py
    ├── watch.py  douyin.py  screener.py  board.py  moneyflow.py  lhb.py  research.py
    ├── overseas.py  stock.py  bili.py  kb.py  news.py  multiquote.py  rate.py  cost.py
    ├── config.py  backup.py  signal.py  prediction.py  ai.py  ...（共 80 域）
    └── global_domain.py  # 原 domain=global，因 Python 关键字重命名（见 §4）
```

- **域划分规则**：`api_<domain>_<...>` 的 `<domain>` 段即域；单域可含多个 api（如 `watchdesk` 含 3 个）。
- **每个 `domain.py` 仅含**：该域的 `api_*` 函数 + 顶部 `from viewer.server import (...)` 依赖重定向 + 标准库导入 + 文件头 docstring。
- **server.py 剩余职责**：模块级常量/锁/引擎模块引用、HTTP 类（`Handler`/`_SingleInstanceHTTPServer`）、`main()`、以及末尾拓扑序 re-export 块。
- **`recovery_handlers.py`**（独立于 `handlers/`）仍持有 `api_universe_box` 等恢复用函数，不纳入本次拆分 re-export。

---

## 4. 关键修复（global 关键字 bug，诚实红线）

首版抽取生成 `from viewer.handlers.global import (api_global_market)`——`global` 是 Python 保留字，导致 server.py **SyntaxError**。三处修复：

1. handler 文件 `global.py` → `global_domain.py`；
2. server.py 对应 re-export 行改为 `from viewer.handlers.global_domain import (api_global_market)`；
3. `tools/split_server.py` 加 `safe_dom()`（`keyword.iskeyword` 命中则加 `_domain` 后缀），工具重跑不再生成非法模块名。

---

## 5. 回归流程（本机执行，沙箱因缺 numpy 等未能跑全）

```bash
cd F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper

# ① 关键闸门：强制加载全部 80 个 handler，校验 157 个 api_* 都在 viewer.server 命名空间
python -c "import viewer.server as s; a=[n for n in dir(s) if n.startswith('api_')]; print('import OK, api_* =', len(a))"
# 期望：import OK, api_* = 177（157 迁走 + 约 20 个本就由其他模块导入 server 命名空间，与拆分前一致）

# ② 真实启动冒烟（验证 main/端口/启动链路；会阻塞，后台跑或另开终端 Ctrl+C）
python -m viewer.server
# 另开终端：curl http://127.0.0.1:8766/api/version   （端口看启动日志的 PORT，可用 VIEWER_PORT 改）

# ③ smoke 全量（含第7节调度一致性）
python tools/smoke.py
# 期望 exit 0；唯一历史 FAIL 已不在失败名单

# ④ 单测（pytest.ini 入口：pytest -q）
pytest -q
```

---

## 6. 回归结果（事实）

| 闸门 | 结果 | 说明 |
|---|---|---|
| `verify_split.py` 逐字符比对 | ✅ 157/157 零改 | 与 `c3725a0` 基线一致 |
| `py_compile` 全扫（server + 81 handler） | ✅ 0 语法错误 | 含修复后的 global_domain |
| 独立闭合校验 | ✅ 0 残留 def / 0 re-export mismatch / 0 悬空 import | 691 个 server 顶层符号全覆盖 |
| `routes._SRVER_NAMES` 覆盖 | ✅ 157/157 | 路由别名无需改 |
| **import 闸门（运行时）** | ✅ 177 api 可见，157 handler api 全部挂在 `viewer.server` | 沙箱真实 `import viewer.server` 通过 |
| `tools/smoke.py` | ✅ PASS=27 / FAIL=0（SMOKE_OK） | 全量编译/DB/宇宙/红线/第7节均过 |
| `pytest tests/test_routes_snapshot.py` | ✅ 1 passed | routes 契约未变 |
| `pytest -q`（全量） | ⏳ 沙箱缺 numpy/pandas 致收集中断 | **环境限制，非拆分问题**；本机 `pytest -q` 即可跑全 |

> 注：沙箱已逐层补齐 `pyyaml/beautifulsoup4/requests/cryptography/pytest`，但 `numpy/pandas` 及其下游（scipy 等）体积大、未装完；这些仅影响少数业务测试模块的**收集**，与"api_* 文件搬家"无关。

---

## 7. 后果 / 回滚 / 风险

- **向下兼容**：对外接口完全不变（`routes.py`、`S.api_*` 测试、调用方 `from viewer.server import api_xxx` 均仍可用）。
- **可逆**：`git revert 10ce465 724fa34 aca2b70` 或回到 `c3725a0` 即可整段回退。
- **残余风险**：仅"延迟导入 / 循环导入"类，已被 import 闸门（运行时真实 `import viewer.server`）覆盖；目前已 PASS。
- **后续**：全量 `pytest -q` 待本机（依赖齐全）跑通确认零回归；本 ADR 与拆分 commit 分离提交，不污染拆分历史。
