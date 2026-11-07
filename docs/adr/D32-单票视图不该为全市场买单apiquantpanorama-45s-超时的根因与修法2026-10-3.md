## D32 单票视图不该为全市场买单：`/api/quant/panorama` 45s 超时的根因与修法（2026-10-30）

**一句话**：`factor_panorama()` 第一行是 `_universe_bars()` —— 为了看**一只票**，把全市场
5239 只 × 最近 260 根 K 线读进内存再逐只过滤。用户在「量化板块 → 因子全景」输入 6 位代码点查询，
请求就挂在那儿（线上实测 **>120s 不返回**，45s 上限的两次测试全超时）。同一处漏网的
`/api/quant/select` 也是 45s 超时。

### 一、根因：一个「取数函数」被当成了「面板函数」

`viewer/server.py` 里这四条路由的分工原本是清楚的：

| 路由 | 旧实现 | 代价 |
|---|---|---|
| `/api/quant` | `_heavy_cached_payload("/api/quant")`（读落盘缓存） | ms 级 |
| `/api/quant/panorama` | 直调 `quant.factor_panorama(code, con=DB.conn)` | **>120s** |
| `/api/quant/select` | 直调 `quant.multi_factor_select(...)` | **45s 超时** |
| `/api/quant/backtest` | 直调 `quant.factor_backtest(...)` | 同样重（本轮未改，见第五节） |

`quant.py` 侧的问题更本质：`_universe_bars()` 的 docstring 自己写着「耗时从分钟级降到几十秒」——
它是个**全池**取数函数，却被 `factor_panorama()` 当成「拿一只票的 K 线」的通用入口。
而 `factor_panorama` 下一行就是 `rows = bars_map.get(code)` —— 前面 5239 只票的数据全白读了。

### 二、为什么 2026-10-29 那次改造漏了这两个子接口

那次（见 `_HEAVY_CACHE` 注释）修的是「**面板级**接口」：`/api/quant`、`/api/etf`、`/api/kronos`、
`/api/hotspots` —— 判据是「这个面板整页要不要等几十秒」。`/api/quant/panorama` 带着 `?code=`
参数、只返回一只票的 7 个因子、响应体几 KB，**看起来完全不像重活**，所以没进 `_HEAVY_CACHE` 名单。
漏的不是「忘了加缓存」，是**没人量过它的取数路径**：它轻，是因为它的输出轻，不是因为它的计算轻。
教训写在这：*给面板做性能分级时，判据必须是「实际执行路径的复杂度」，不是「响应体大小 / 参数个数」。*

### 三、改法（① `viewer/quant.py`）

新增 `_single_bars(code)`：一次 `code = ?` 的 SQL 拿一只票，与 `_universe_bars()` 对同一 code
的输出**严格同构**（准入 → 窗口 → 清洗三步照抄）：

1. **准入**：`_is_ashare(code)` 且该票在 K 线底座里确有 bar（等价于原来的 `sources` 过滤）。
2. **窗口**：同样以 `base.distinct_dates()[-_SERIES_TAIL]` 作 `min_date`，再截最近 `_SERIES_TAIL`(260) 根。
3. **清洗**：同样剔掉 `date`/`close` 为空的 bar，同样要求 `>= MIN_BARS`(130) 根才认。

`factor_panorama()` 只改一行取数（`_universe_bars()` → `_single_bars(code)`），其余一字未动。

**改的过程中踩到三个坑，全部记录在此（都是"看起来等价其实不等价"）**：

- **坑 1｜`KBaseBars.__bool__` 会走 `__len__`**：`kc.get("bars") or {}` 会触发
  `SELECT COUNT(DISTINCT code) FROM kline`（1364 万行，实测 **0.54s**/次）。
  `_universe_bars` 里写这句无所谓（一次调用只付一次），单票视图里就是白丢 0.54s。改成显式判 `None`。
- **坑 2｜`ohlcv_bulk(...) or None` 在单票语义下会翻车**（这个是**全量比对抓出来的真 bug**）：
  对单票，`ohlcv_bulk` 返回空 dict 有两种可能 —— ①该票在窗口内确实没有 bar；②批量通道坏了。
  `or None` 把两者都当成 ② → 走「逐只读取」回退分支 → **回退分支不带 `min_date` 过滤**，
  于是 7 只退市票（600677 / 603555 / 603603 / 603963 / 603996 / 688086 / 688555，末根早于窗口起点）
  从「无数据」被误判成「有 260 根」，`factor_panorama` 会对退市票吐出因子分。
  全池版为什么没事：5239 只里这一只的空结果**不会**让整批 dict 变成空，所以从不触发回退。
  修法：用 `base.last_date(code)` 探针把 ①（合法空，不回退）与 ②（通道异常，回退）分开。
  **这个差异 21 只抽样样本一个都没覆盖到** —— 只有「对全部 5239 只逐只比对」才暴露。教训：
  *改取数路径时，抽样比对不算验证，全量比对才算。*
- **坑 3｜口径里的既有副作用必须原样保留**：`min_date` 是「**全市场**最近 260 个交易日」，
  所以长期停更票（835670 末根 2025-09-30，窗口内只剩 3 根）会被判「无数据」。
  这看着像 bug，但它是**改造前就有的行为**，且同时影响 `factor_backtest` / `multi_factor_select`
  的宇宙。本轮**只搬不动**：要改必须单独立项（改宇宙 = 改所有权重与回测结论）。

### 四、改法（② `viewer/server.py`：`/api/quant/select`）

新增 `_quant_select_cached(top_n)`，路由改为读 `/api/quant` 的**同一份落盘缓存**里的 `"select"` 字段。

为什么不去加 TTL 缓存、也不给 `_universe_bars` 加进程内缓存：**这份结果早就被算过了**。
`quant.payload()`（`tools/sched_job.py` 的 `quant` 任务在**子进程**里跑，写
`viewer/data/quant_payload.json`）第 3 步就是 `sel = multi_factor_select(con=con)`，落在
payload 的 `"select"` 字段里 —— 量化面板读的正是它。所以只需「读同一份落盘 + 按 `top_n` 切片」：
不新增计算、不新增落盘文件、不新增子进程任务，且与量化面板显示的内容天然一致（不会再出现
「面板说 30 只、子接口说另外 30 只」的口径分裂）。

**不选「给 `_universe_bars` 加 TTL 缓存」的理由**（写清，避免下次又有人想这条路）：
① 它治不了首调用 —— 缓存冷的时候第一个请求照样几十秒，照样超时；
② 它把几百 MB 的全池 bars 常驻**服务进程**内存 —— 2026-10-29 那次「内存冲到 16.7GB 被系统杀掉」
   就是全池常驻的后果，把同样的风险搬回服务进程是倒退；
③ `_universe_bars` 被 backtest/etf/self-learn 多处调用，加 TTL 等于给**所有**消费方引入数据陈旧窗口，
   而「陈旧到什么程度会误导用户」这件事没人能给出依据 —— 现有落盘缓存有 `at` 时间戳 + 前端展示，
   TTL 缓存没有这个交代能力。

**诚实标注的取舍**：
- **清单长度**：缓存清单是 `TOP_N_SELECT`(30) 只。`top_n < 30` 正常切片；`top_n > 30`
  只能给到 30 只并在 `note` 里写明原因 —— 不为「多要几只票」把请求线程拖死。
- **数据新鲜度（语义变了，写清）**：改前是「每次现算（慢但最新）」，改后是「读落盘缓存」，
  `_HEAVY_CACHE["/api/quant"]` 的 TTL 是 **36 小时**。响应里保留了 `data_date`，调用方能看出榜单是哪天的；
  但**最坏可能读到 36 小时前的榜单，跨周末还会更长**。该接口**当前无前端入口**
  （QA 已 grep 全量 `static/*.js` 复核：只有 `/api/quant`(4 处) 与 `/api/quant/panorama`(app.js:17436)），
  所以今天无用户影响；**将来若有外部/手工调用方，必须自己看 `data_date`** —— 别把它当"实时榜单"。

### 五、`/api/quant/backtest` 本轮**没改**（不是漏了）

它同样直调 `factor_backtest(con)`（全池 + 120 个截面日算 IC/T+5/10/20 衰减），也是几十秒级。
但它**与 `/api/quant` 的 payload 完全重叠**：`quant.payload()` 第 4 步就调 `factor_backtest()` 并落在
`"backtest"` 字段里，前端回测面板读的是 `/api/quant` 的缓存。所以正确修法与 `select` 一模一样
（读 `_quant_select_cached` 的同款缓存切片），但**本轮不做**：任务边界只划到 panorama/select，
而这块的收益是「一个无前端入口的接口」，风险是「动到回测口径的展示」。已登记为待办，
下次要动就照 `_quant_select_cached` 抄，别新发明。

### 六、验证方法（三条，都能复现）

1. **21 只抽样逐字段 diff = 0**：从 `git show HEAD:viewer/quant.py` 取旧版（**先取旧版再改代码**，
   否则就是自证），对 14 只真实票 + 1 只不存在(999999) + 2 只 K 线不足(301718/601091) +
   1 只非 A 股前缀(113001) + 空串 + 带空格("600519 ") 各 dump 完整 JSON，递归逐字段比对
   （键集合/列表顺序/数值 repr），**差异 0**。
2. **全量 5239 只逐只等价**：`_single_bars(code) == _universe_bars()[0].get(code)` 全库跑一遍
   （有数据 5185 只 / 无数据 54 只），**差异 0**。这条才是真验证 —— 坑 2 就是它抓到的。
   复现：`set QUANT_PARITY_ALL=1 && pytest tests/test_quant_panorama_perf.py -q`
   （实测 **135~170s**，随机器负载浮动；函数级 `_single_bars` 每只票要开两次 sqlite 连接，
   5239 只 = 上万次开关连接，慢在这里）。
   ⚠️ **该 test 必须带 `@pytest.mark.timeout(600)`**：`pytest.ini` 有全局 `timeout = 120`（P2-37 防挂死），
   不加就**必然超时被打断**——而且是「超时」不是「断言失败」，下一个人会误读成「D32 改出 bug 了」。
   QA 独立复现时按无参数命令跑直接红，加 `--timeout=900` 才绿；现已用 marker 覆写（不动 `pytest.ini`，
   免得影响别的测试），**按原始命令 `QUANT_PARITY_ALL=1 pytest tests/test_quant_panorama_perf.py -q`
   实测 `8 passed in 142.76s`**。
3. **回归测试**：`tests/test_quant_panorama_perf.py` 7 条（+1 条默认 skip 的全量比对）。
   守门方式不靠"返回 200"，而是：①不存在代码 < 1s；②把 `_universe_bars` 换成爆雷桩，
   `factor_panorama` 仍须出结果（比计时更硬，不受机器负载影响）；③单票取数与
   `kbase.bars_of` 的「窗口内最近 260 根 + 剔空 + >= MIN_BARS」逐根相等；
   ④把 `multi_factor_select` 换成爆雷桩，`_quant_select_cached` 仍须 < 1s 出结果。
4. **口径更正（QA 指出，采纳）**：`/api/quant/select` **前端零调用** —— `grep` 全量 `static/*.js`
   只有 `/api/quant`(4 处) 与 `/api/quant/panorama`(`app.js:17436`)，`quant/select` 一次都没有。
   所以 select 那半边的性质是「**消除一个能在请求线程里持 GIL 162s 的入口**」（防未来有人接前端
   / 手工调用把全站冻住），**不是修用户可见痛点**；**用户可见收益全部来自 panorama 那半边**。
   引用本决策时别把收益算给 select。

### 七、实测耗时

| 口径 | 改前 | 改后 |
|---|---|---|
| 线上 HTTP `/api/quant/panorama?code=600519` | **>120s（curl --max-time 120 超时）** | 待重启生效（本轮禁止重启服务） |
| 独立进程 `factor_panorama('600519')` cold | 3.90s / 3.96s | **0.77s / 0.75s** |
| 独立进程 `factor_panorama('600519')` warm | 3.21s / 3.20s | **0.016s** |

cold 那 0.75s 里 **0.73s 是 `distinct_dates()` 的首次 SQL**（进程内 mtime 记忆化，之后 0.0002s），
即「服务重启后第一次点查询」的代价；同一进程内后续请求都是 16ms 级。
服务进程内的绝对耗时比独立进程高（`server.py` 里 2026-10-29 的注释记过同类现象：同段纯 Python
在服务进程里 14s、独立跑 2.6s），故线上预期仍是「几十毫秒级」，不再有秒级等待。

### 八、遗留

1. **需要重启服务才生效**：改的是 `viewer/quant.py` / `viewer/server.py`，运行中的进程装的是旧模块。
   本任务明令禁止重启（公告回补在跑），故线上 45s 仍未消失，重启后才是 20ms 级。
   （前端不受影响：`/static/*` 是每次请求读盘。2026-10-30 研报评级那次改动即因此**无需重启即生效**。）
2. `/api/quant/backtest` 同款问题未修（见第五节）。
3. 可选的再加一层保险：`boot()` 时预热 `distinct_dates()`，把那 0.73s 从「重启后第一次点击」挪到启动期。
   本轮没做（不在任务边界内，且启动期已有足够多的预热项）。
4. **`factor_panorama(code)` 不传 `con` 时 `name` 静默为空**（`name = "" if con is None`）——这是**改造前
   就有的**行为，本轮没动。但它是个 footgun：QA 写复核脚本时踩了，第一次跑出 14 处 `name` 差异，
   差点误报成回归。线上 GET 路由传了 `con=DB.conn` 所以无事。**约定：调它必须传 con，否则 name 必空。**
   （可选改法：无 con 时降级自己开一次只读连接读名字。本轮不做 —— 会给「读函数」偷偷加一个 DB 连接，
   得不偿失。）
5. **`_single_bars` 的存在性检查用的是 `bars_in.get(code)`（一次该票全历史读），不是 `KBase.has(code)`**
   —— QA 提议换成 `has()` 可省一次读。**本轮不改**，理由：`bars_of(code, adj=KBASE_ADJ)` 按 `adj` 过滤，
   而 `has(code)` **不看 `adj`**；当前库里只有 `hfq` 一种 adj，两者等价，但一旦将来引入第二种复权口径，
   `has()` 会把"别的 adj 有、hfq 没有"的票放进宇宙 → 悄悄改变宇宙口径。这属于「用将来的正确性换
   今天几毫秒」，不划算（生产路径上每请求只多付约 5ms）。
6. **`pytest.ini` 的全局 `timeout = 120`** 与「分钟级验证测试」天然冲突：本轮用函数级 marker 解决，
   但**下次再加同类的分钟级测试，记得同样加 marker**，否则它会以「超时」而非「断言失败」的形式红，
   误导后来者。

---
