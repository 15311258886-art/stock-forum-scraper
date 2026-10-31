# UI 端到端门禁（tests/e2e/）

单一入口：`tests/e2e/run.cjs`。取代已废弃的 `tests/ui_full_regression.js`。

```
node tests/e2e/run.cjs              # 全量：全部 tab × 3 视口 + 单位 + 自选 + 空/错态（上限 6 分钟）
node tests/e2e/run.cjs --quick      # 快速门禁：3 个 tab + 单位 + 健康 + 三视口溢出（实测 ~19s）
node tests/e2e/run.cjs --json       # 机读：stdout 只有一个 JSON 对象
node tests/e2e/run.cjs --no-write   # 只读环境：跳过自选增删
node tests/e2e/run.cjs --port 8765  # 指定端口（默认 8765）
```

**退出码：`0` 全过 / `1` 有用例失败 / `2` 环境不可用（连不上服务、浏览器起不来）。**
"环境坏了"和"产品坏了"必须分得开 —— 混在一起的后果是红了没人看。

三处刻意把"环境"从"产品"里摘出来的设计：

1. **服务和浏览器都起不来 → EXIT=2**，并且不写 `EXIT=1`（不是产品缺陷，不该触发修 bug 的流程）。
2. **服务在跑的过程中被重启**（别人重启 viewer）→ 报告里 `env.restarted_mid_run` 记录
   `pid 前→后`，并把「接口无失败」从 fail 降级为 warn，摘要里明写
   *"期间的接口连接失败属重启窗口，请复跑定性"*。实测踩过：跑第 3 分钟时 pid 5852 → 17064，
   151 条 `ERR_CONNECTION_REFUSED`、viewport 阶段从 120s 涨到 163s —— 不区分就会被记成"接口全挂"。
3. **两个时间闸**：`deadline`（1.0× 预算）只用于"如实报告是否达标"；
   `hardDeadline`（1.5× 预算）才真正中止后续阶段。否则会出现"预算到点直接 break，
   剩下两个视口一个都没测"，把"被预算掐断"报成产品缺陷。
   服务刚重启（`started_at` 距今 < 3 分钟，缓存全冷）时超预算只记 warn 并注明冷启动。

报告落在 `tests/e2e/_report.json`（已 gitignore），含每条断言的 `why`、分阶段耗时、
接口失败明细、`needs_testid` / `blocked` / `not_covered` 清单。

---

## 一、为什么是「动态枚举」而不是写死 tab 列表

旧版 `tests/ui_full_regression.js` 把 10 个 tab 名和 `.tab[data-tab="..."]` 写死。
导航改版后（`viewer/static/app.js` 的 `paintSubnav`）DOM 变成两级：

```
#sidenav .nav-item[data-cat]          一级分类
#subnav  .sub-tab[data-tab]           当前分类下的子板块（只渲染当前分类）
#p-<tab>                              每个 tab 的面板容器（懒创建）
```

于是 `page.click('.tab[data-tab="summary"]')` 直接抛 `No node found for selector`，
**一份回归在第一个 tab 就死掉，一条断言都没跑到** —— 这就是测评里"自动化测试与真实 UI 已漂移"。

新版做法：

1. 读 `#sidenav .nav-item[data-cat]` 得到分类全集（本次实测 7 个）；
2. 逐个点开分类（`ensureCat`），等二级导航稳定后读 `#subnav .sub-tab[data-tab]`；
3. 把 `{cat, tab, label, href}` 摊平成一张表（本次实测 **53 个 tab**，旧文档写的 43 个已过时）；
4. 遍历这张表逐个点。

**代码里不出现任何具体 tab 名。** 连 `--quick` 的"3 个 tab"也是取枚举顺序的前 3 个
（第一个恰好是「今日决策」，所以"含 today"这条自然满足，而不用在代码里写 `"today"`）。

多写一句为什么必须是动态的：2026-11-01 凌晨这门禁写就的同时，另一位同事正在重排导航
（`overview/money/info/opinion/watch/research/learn` → `today/mine/market/stock/strategy/system`）。
同一份门禁代码，半小时内先后跑出 **43 / 53 / 58** 三种 tab 数 —— 写死名单的测试在这半小时里
会连续红三次（且每次都指向不同的"错"），而动态枚举只是如实地把变化报出来。
报告里因此额外记录 `app.js / app.css / index.html` 的 `sha1 + size + mtime`
（`env.app_js`），用于把"结果"和"当时那一版前端代码"绑在一起；没有这个，
两次跑出不同结论时没人能证明是文件变了而不是测试不稳。

### 三个必须知道的坑（都踩过了，别重犯）

- **子板块只在"它所属分类"被选中时存在于 DOM 里。** 遍历到第 20 个 tab 时，二级导航还停在
  上一个分类，直接按 `data-tab` 查元素会得到"元素不存在"。所以每次点击前要先 `ensureCat(cat)`。
  第一版门禁就是因为这个把 3 个 tab 全判失败。
- **等就绪不能只看数量。** 切分类的瞬间列表还是上一个分类的（数量看着稳定），
  必须同时校验"首个 tab 的 `href` 已属于目标分类"**并且**"没有在飞的 `/api/` 请求"才算稳定。
  少这一条会出现"同一份代码一次枚举到 53 个 tab、另一次只有 43 个"（差的是异步到达的动态轨道），
  于是"43/43 全绿"和"53/53 全绿"是两个含义完全不同的绿灯。门禁自带
  `[nav] 导航枚举可复现` 断言专门抓这件事。
- **横条里有「折叠区」，默认收起。** 2026-11-01 起 `#subnav` 把一部分子板块收进
  `<details class="sub-fold"><summary>研究工具（N）</summary>`。收起时里面的
  `<a data-tab>` **仍在 DOM 里**（所以枚举得到、`textContent` 读得到标签），但**不可见也不可点**：
  真实鼠标点击会落到横条或下方面板上，表现为"点了没反应、hash 不变"。
  实测 53 个 tab 里有 **33 个在折叠区**，不展开就跑会得到 `20/53 通过` 的假红。
  处理方式（不写死 class）：每次点击前找 `#subnav details:not([open])`，
  优先真实点击它的 `<summary>`，点不到才退回改 `open` 属性。
  注意横条每次重绘都会重建 `<details>`，所以**展开必须在每次点击之前做**，不能只在切分类时做一次。
  顺带一个教训：`<details>` 未展开时子项的 `innerText` 是空字符串，
  读标签必须用 `textContent`，否则报告里全是 `[today/executability] ——`（标签为空，查不动）。

---

## 二、每个 tab 检查什么（四件事 + 两项软指标）

| # | 断言 | 判据 | 为什么 |
|---|---|---|---|
| ① | 深链接可用 | `location.hash === "#<cat>/<tab>"` | hash 是分享/回退/自动化的唯一入口，坏了等于不能深链 |
| ② | 无 JS 异常 | 点击窗口内无新增 `pageerror` / `console.error` | JS 崩了通常紧接着整块面板不工作 |
| ③ | 无接口失败 | `/api/` 前缀的 4xx/5xx 增量 = 0（`304` 不计） | 接口烂了用户看到的就是空面板 |
| ④ | 无脏字符 | `document.body.innerText` 里无 `undefined` / `NaN` / `Invalid Date` / `[object Object]`（`null` 见下） | 把内部值直接糊到用户脸上是最典型的前端事故 |

脏字符的两个防假阳性处理：

- 扫描前剔除 `pre / code / script / style / textarea` 的文本 —— 代码示例里出现 `null` 是合法的；
- 用**词边界**正则（`\bundefined\b`），不会把 `nullable` / `nulls` 这类合法英文单词误判。

`null` 单列 **warn** 而不是 fail：中文 UI 理论上不该出现独立 `null`，但它偶尔来自
合法示例载荷；把它当硬失败会让门禁常红，常红的门禁等于没有门禁。出现时会连同上下文一起打印。

软指标（记录但不红）：

- **面板是否有内容**：`#p-<tab>` 的 `innerText` 长度。空的可能是"当天确实没数据"，直接判 fail 会假红；
  报告里 `nav.empty_panels` 会列出来供人判断。
- **点击是否被遮挡**：点击前做 `elementFromPoint` 命中测试。被盖住时真实鼠标点不到，
  但 `element.click()` 仍可能"成功"——不单独暴露就会变成"断言莫名失败"。

---

## 三、必测清单与判据

### 1) 启动 / 健康
- `GET /api/version` 的 `build_id` 存在且长度 ≥ 6；
- `GET /api/health` 的 `leases.items` 是**数组**（断言结构而不是文案：结构变了「系统健康」页会静默空白）。

### 2) 个股详情 + K 线 + 单位一致性（P0-1 端到端守门）

事故原型：同一只 600519，抽屉「行情区」写 `5.99 万手`（对），「K 线区」写 `599.27 万手`（错 100 倍）。

断法：

1. 走**真实用户路径**打开抽屉：`Ctrl+K` → 输入 `600519` → `Enter`（命令面板，`#cmdInput`）；
   命令面板不可用时才兜底 `window.openStock()`，并在 `needs_testid` 里记一笔（不静默降级）。
2. 分别在 `#stkQuote`（行情区）与 `#klineSnap`（K 线区）里找汉字「成交量」那一格，读文本。
3. 断言三者：
   - 两处**单位标注必须不同**（一个「手」一个「股」）；
   - `手 × 100` 与 `股` 的相对误差 **< 1%**；
   - 任一处显示 `—（单位未标注）` 或读不到 → **FAIL**（600519 日线一定有量）。

不写死数值：换数据源/换日期数值就变，写死等于埋一颗定时红。

### 3) 自选增删（可回收的临时数据）

- 先读 `/api/watch_list`，从候选表里挑一个**池中没有**的标的；
- 动态发现「关注池」面板：哪个 tab 的面板里有 `#wtAdd` 就是它（不写死 tab 名）；
- 点 `#wtAdd` → 填 `#wtF-symbol` → 点 `#wtF-save` → 断言行出现；
- 从行上取 `data-wt-del` 的 id → 真实点击 → 接受 `confirm` → 断言行消失；
- `finally` 里兜底清理 + 再查一次 `/api/watch_list` 复核确实不在池中（自证清理干净）。

`--no-write` 跳过；**`--quick` 也跳过**（写链路要等页面拉行情，塞进 quick 会把 19s 拖成 50s+）。
`quick` 输出里会明确写「自选增删 = 未执行」，**不要**把"quick 绿了"理解成写链路没问题。

### 4) 全量导航无 404/5xx
遍历全部 tab，汇总 `/api/` 前缀的 4xx/5xx。过滤两类噪音：

- `304`（走缓存，不是失败）；
- `net::ERR_ABORTED`（前端切 tab 会主动 abort 上一只票的请求，属正常设计）。

### 5) 空态 / 错误态 / 停更态
`page.setRequestInterception` 在**测试页**里构造，不动服务端：

| 用例 | 构造 | 断言 |
|---|---|---|
| 空态 | 所有 `/api/` 返回 `[]` | 正文非白屏（≥40 字）+ 出现中文提示词 + 无脏字符 + 无 JS 异常 |
| 错误态 | 所有 `/api/` 返回 500 + HTML | 同上 |
| 错误态 | 所有 `/api/` 返回 200 但非 JSON | 同上 |

白屏判据用 `document.body.innerText` 长度；"人话"判据用
`暂无|无数据|没有|尚未|失败|错误|无法|请稍后|重试|加载` 这类中文词。

### 6) 三档视口 1024 / 1440 / 1920
- **无横向溢出**：`document.documentElement.scrollWidth <= innerWidth + 1`（逐 tab 测）；
- **可点元素不被裁**：面板内 `button / a[href] / input / select` 的 `getBoundingClientRect`
  不得整体落在视口外；在可横向滚动容器（`overflow-x: auto|scroll`）里的不算；
- **顶栏按钮 `elementFromPoint` 可命中**：按钮清单**动态枚举**（`#btnDemo` 这类按钮带 `hidden`，
  写死 id 就会假红），逐个用 `elementFromPoint(中心点)` 验证命中的是它自己或其后代。

---

## 四、`stale_build` —— 为什么"没重启"不能被误判成"改坏了"

服务跑的是启动那一刻的代码快照。`/api/version` 的 `build_id` 是**进程启动时的代码指纹**，
而磁盘指纹随时会变。本门禁在 Node 里按 `viewer/build_id.py::of_disk` 的算法**重算一遍磁盘指纹**
（对 `KEY_FILES` 取 `(mtime, size)` 串 sha1 前 12 位，已与 Python 交叉验证同值），然后：

- 不一致 → `env.stale_build = true`，`[health]` 记一条 **warn**（不红），并在报告与摘要里同时打印
  服务 `build_id` 与磁盘 `build_id`；
- 此时若「单位一致性」FAIL，报告里会追加一句：*这条很可能是「没重启」而不是「改坏了」*。

门禁**不会**自己重启服务（多人共用 8765），也不会为了让测试变绿去改断言。

---

## 五、非退出码的登记项

### `needs_testid`（建议后续统一加稳定标识）

1. **`.sub-tab` 缺 `data-cat`** —— 现在只能从 `href="#cat/tab"` 反解分类。
   `paintSubnav` 里已有 `cat.key`，加一个 `data-cat` 是一行的事，分类断言会更直接（影响全部 53 个 tab）。
2. **面板缺统一的「已就绪」标记** —— 现在用 `#p-<tab>` 的 `innerText` 长度猜是否渲染完。
   建议给面板容器加 `data-state="loading|ready|empty|error"`，门禁就能精确等就绪，而不是靠 sleep。
3. **抽屉成交量缺 `data-testid`** —— 现在靠 `#stkQuote`/`#klineSnap` 里 `.si-item`/`.ks-grid` 的
   内部结构找「成交量」。这是 P0-1 的核心守门项，版式一改就再也测不到，建议
   `data-testid="vol-quote"` / `data-testid="vol-kline"`。
4. **「加入自选」弹窗缺 `data-testid`** —— `#wtF-symbol` / `#wtF-save` 是 id，够用但脆弱。

> 本轮**没有改** `viewer/static/app.js` / `index.html` / `viewer/*.py`（任务约束），
> 所以以上只登记，不用。

### 首次访问的 `#firstRunMask`（一个容易把门禁带偏的东西）

干净浏览器 profile 首次访问会弹「开始使用」引导。它的 `#firstRunMask` 是
`position: fixed; inset: 0; z-index: 9100` 的**全屏挡板**（`hidden` 属性被 `.modal-mask.show`
的 `display:block` 盖掉）。不关掉它，**所有真实鼠标点击都会落到挡板上**，任何 tab 都点不动。

所以门禁在 `goto` 之后先做一件事：真实点击 `#frDone` 关掉引导，并单独断言"它确实被点掉了
且挡板不再拦截"。这不是 bug（本来就该由用户点掉），但门禁不知道这件事就会把"点不动"误报成产品缺陷。

---

## 七、本轮实测发现的真实缺陷（不是测试问题，需要改 app.js）

### 缺陷 1：二级横条里 5 个子板块被渲染两遍（用户能看到两个一模一样的入口）

现象（`node tests/e2e/run.cjs` 的 `tab 标识不重复` 断言）：

```
FAIL tab 标识不重复（同一条横条里没有重复入口） —— 重复 5 个 / 原始 58 个:
  lowbox_bottom(mine|#mine/lowbox_bottom, mine|#mine/lowbox_bottom)
  lowbox_radar(mine|#mine/lowbox_radar, mine|#mine/lowbox_radar)
  sentiment(market|#market/sentiment, market|#market/sentiment)
  overseas_sentiment(market|#market/overseas_sentiment, market|#market/overseas_sentiment)
  geo_events(market|#market/geo_events, market|#market/geo_events)
```

DOM 结构（实测）：两份都在「研究工具（N）」折叠区里，一份是
`<a class="sub-tab sub-child">`（挂在 main 的 `sub-cluster` 里），另一份是
`<a class="sub-tab">`（挂在 `sub-fold-body` 里）。

根因在 `viewer/static/app.js` 的 `paintSubnav → renderList()`：

- `if (i.child && !inFold) continue;`（约 14127 行）只在**平铺区**跳过子项；
- 折叠区里，lookahead 循环（约 14131–14138 行）已经把 lead 后面同 `mg` 的子项渲染进
  `sub-cluster`；
- 外层循环随后又遍历到同一个子项，因为 `inFold` 为真而没有 `continue`，
  落到 `else` 分支（约 14141 行）**再渲染一次**。

修法建议（一行）：内层循环把已渲染的子项记进一个集合，外层 `continue`，
例如 `if (i.child && (emitted.has(i.tab) || !inFold)) continue;`。

> 本轮约束禁止改 `viewer/static/app.js`，故只登记。门禁会一直红到它被修掉 —— 这是故意的。

### 观察 2：`strategy/fundmasters` 面板渲染为空

全量跑完 `nav.empty_panels = ["strategy/fundmasters"]`（面板存在、`innerText` 长度为 0）。
空面板在某些情况下是合法的（当天没数据），所以门禁只记录不判失败；
但如果它每次都是空的，那就是"点进去什么都没有"，建议顺手看一眼。

### 观察 3：服务跑的是旧代码快照（stale_build）

`/api/version` 的 `build_id` 与磁盘指纹不一致（本轮实测 `d2728a1c88e8` vs `ebc7922c6cc9`）。
静态资源（`app.js`/`app.css`/`index.html`）是**每次请求现读磁盘**的，
所以浏览器拿到的是新前端、后端却是旧逻辑 —— 这类"半个新版"最容易出诡异问题。
门禁把两个版本号都打出来，但不重启服务（多人共用）。**改完后端 Python 一定要重启再复跑。**

---

## 八、本轮实测输出（2026-11-01 03:49，app.js sha1=a19ca3f6 size=1254521 mtime=19:45:30）

`node tests/e2e/run.cjs --quick` → **EXIT=1**，23.0s（唯一的失败项就是上面那个「重复入口」缺陷）：

```
UI 端到端门禁 · 模式=quick · 端口=8765
服务 build_id=d2728a1c88e8 / 磁盘 build_id=0a2ef49a3d61  ⚠ stale_build=true
产物版本：app.js sha1=a19ca3f6 size=1254521
PASS  [env] 服务可用
PASS  [health] /api/version 有 build_id
PASS  [health] /api/health 有 leases.items 数组
[nav] 3/3 可打开，失败 0（DOM 动态枚举到 53 个 tab，原始 58 含重复；其中 33 个在折叠区，已帮用户展开 3 次）
      各分类: today=13 mine=11 market=15 stock=3 strategy=15 system=1
  FAIL tab 标识不重复（同一条横条里没有重复入口） —— 重复 5 个 / 原始 58 个: ...
[api] 4xx/5xx 0 条
[unit] 600519 行情 5.99 万手 / K线 599.27 万股 → 一致（误差 0.0451%）
[viewport] 1024/1440/1920 无溢出
断言 25/28 通过，失败 1，告警 2，耗时 23.0s
EXIT=1
```

`node tests/e2e/run.cjs`（全量）→ **EXIT=1**，214.7s（预算 360s，未超时）：

```
[nav] 53/53 可打开，失败 0
      空面板 1 个: strategy/fundmasters
[api] 4xx/5xx 0 条
[unit] 600519 行情 5.99 万手 / K线 599.27 万股 → 一致（误差 0.0451%）
[watch] 临时标的 000001 加入=OK 清理=OK
[states] 空态/错误态 3/3 通过
[viewport] 1024/1440/1920 无溢出
[phases] goto+boot=3.7s enumerate-nav=8.1s nav-tabs=30.4s unit=1.4s watch=40.3s states=8.0s viewport=121.2s
断言 82/85 通过，失败 1，告警 2，耗时 214.7s
EXIT=1        ← 唯一失败项是「重复入口」缺陷
```

三个视口下顶栏 4 个可见按钮全部 `elementFromPoint` 命中；53 个 tab × 3 视口均无横向溢出、
无可点元素被裁。环境不可用时（`--port 59999`）正确返回 **EXIT=2** 并打印原因，不与产品失败混淆。

---

## 九、新旧覆盖对照（`ui_full_regression.js` → `run.cjs`）

### 新版覆盖了（且更稳）

| 旧版检查项 | 新版状态 | 差异 |
|---|---|---|
| 10 个 tab 渲染 + 无脏字符 + 无 JS 报错 | ✅ 覆盖，扩到 **53 个** | 动态枚举，不再写死名单 |
| 空态（接口返回 `[]`） | ✅ 覆盖 | 3 个接口变体合并为"全 `[]`" |
| 错误态（500 / 非 JSON） | ✅ 覆盖 | 500+HTML、200+非 JSON 两种 |
| 响应式无横向溢出 | ✅ 覆盖，扩到 **1024/1440/1920 三档 × 全部 tab** | 旧版只在 2 档 × 1 个 tab 上测 |
| 顶栏按钮 `elementFromPoint` 可命中 | ✅ 覆盖，按钮清单动态枚举 | 旧版写死 4 个 id，其中 `#btnDemo` 已 `hidden`，实测永远是假红 |
| （新增）启动健康：`build_id` / `leases.items` | ✅ 新增 | 旧版没有 |
| （新增）单位一致性 / P0-1 端到端守门 | ✅ 新增 | 旧版没有 |
| （新增）自选增删 + 自证清理 | ✅ 新增 | 旧版明确"不点任何写接口按钮" |
| （新增）深链接 `hash` 校验 | ✅ 新增 | 旧版没有 |
| （新增）`stale_build` 区分"没重启"与"改坏了" | ✅ 新增 | 旧版没有 |
| （新增）退出码 0/1/2 三态分离 | ✅ 新增 | 旧版只有 0/1/2 但 2 混用了 |
| （新增）分阶段耗时 / 单 tab 超时判失败 | ✅ 新增 | 旧版没有 |

### ⚠️ 新版**没有**覆盖的（别当成已覆盖）

1. **四主题（light-tech / dark-dashboard / minimal-report / card-warm）的对比度检测**
   —— 旧版逐个切主题算 WCAG 对比度、比元素数量、比面板高度一致性。新版没做。
2. **抽屉拖拽 / 右下角拉伸 / 拖到极限后手柄仍在视口内** —— 旧版 3 种抽屉 × 6 条断言。新版没做。
3. **抽屉四条关闭入口**（✕ / Esc / 点面板外 / 底部按钮）—— 新版只用 Esc 与 JS 收尾。
4. **抽屉横向不遮挡所点行** —— 旧版用行矩形 vs 抽屉矩形求交。新版没做。
5. **长文本压力**（超长中文标题、超长无空格 ASCII、公司全称列换行）—— 新版没做。
6. **错误态 8 种细粒度变体** —— 旧版还有：网络层 abort、500+合法 JSON、`/api/summary` 缺 `counts`、
   `/api/topics` 条目缺 `weight`、`/api/persons` 缺字段、`/api/sentiment` 返回数组（类型不符）。
   新版只做 3 种粗粒度（`[]` / 500+HTML / 非 JSON），**缺字段兜底未覆盖**。
7. **"元素数量与基线一致（未消失）"** —— 旧版对 10 个 tab 比 `cards/boxes` 计数基线。
   新版只断言面板有内容，不比计数。
8. **主题切换后元素不消失 / 面板高度一致** —— 新版没做。

上面 8 条旧版用例在被废弃的文件里仍可查阅（文件头已标注废弃原因），
要恢复的话建议按新版的"动态枚举 + 语义属性"风格重写，而不是解除旧文件的废弃状态。

### 其它

- 旧的几十个一次性探针（`tests/_probe_*.js`、`tools/ui_probe*.mjs`、`tests/shots_*.js`）
  **不要往它们上面继续加**。它们硬编码坐标/端口/选择器，改一次 UI 就要改一批。
  要加检查项请改 `run.cjs`，并保持"不写死 tab 名 / 不用坐标断言状态"这条底线。
- `run.cjs` 不引任何新依赖：浏览器用机器上已有的 Chromium（playwright 缓存路径优先），
  HTTP 用 Node 内置 `http`，指纹用内置 `crypto`。项目无 npm 出网权限，也不新建 `package.json`。
