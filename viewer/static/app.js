/* 股票讨论实时查看器 - 前端逻辑 */
const THEMES = [
  { id: "light-tech", name: "A 浅色科技风", color: "#0E7490" },
  { id: "dark-dashboard", name: "B 深色数据大屏", color: "#22D3EE" },
  { id: "minimal-report", name: "C 简约报表风", color: "#334155" },
  { id: "card-warm", name: "D 卡片暖色", color: "#D97706" },
];
/* 2026-10-15 P2-1 修：原实现只接受选择器、**静默忽略第二个 root 参数**，
   于是 `$$(sel, root)` 退化成全文档查询 —— 海外舆情 等子导航
   各自把 onclick 绑到了「对方面板」的按钮上（后绑覆盖先绑），表现为
   「点自己子导航切不动 / 另一个面板的子视图被误隐藏（soft 被设成 display:none）」。
   现支持 root 限定；不传 root 时与旧行为完全一致（仍为全文档查询），故对既有调用零影响。 */
const $ = (s, root) => (root || document).querySelector(s);
const $$ = (s, root) => (root || document).querySelectorAll(s);

/* P1-2：fetch 不会因 4xx/5xx 而 reject，旧实现直接 r.json() 会把错误页
   解析失败或静默吞掉。这里显式校验 r.ok，错误信息带上状态码与响应体前 200 字。 */
// 2026-09-28：网络层自动重试。
//   背景：后端重启（改完 server.py / board_flow.py 必须重启）期间会有 2~5 秒端口空档，
//   此刻浏览器 fetch 连不上，抛的是 TypeError: Failed to fetch（不是后端返回的业务错误——
//   后端有全局兜底，异常会回 500 JSON）。旧版把它原样显示成「行情获取失败：Failed to fetch」，
//   用户看不出发生了什么，也不知道该不该重试。这里改为：连接类错误自动重试 2 次（0.4s / 1.2s），
//   仍失败则给中文提示。只对「网络层」错误重试，HTTP 4xx/5xx 与 JSON 解析错误照旧直接抛。
/* 2026-10-06 王总反馈「点了刷新之后也没有反应」：
   旧文案「与本地服务连接中断（服务可能刚重启）。请稍候点『刷新』重试」是个**死循环指引**——
   服务**进程**已经没了，浏览器刷新/点刷新只是重开一次页面，后端并不因此回到后台，
   于是用户反复点、界面纹丝不动。现在文案说清「服务进程已退出、刷新无用」，
   并把可执行的动作放到横幅上（见 netDropBanner / restartLocalService）。 */
const _API_NET_HINT = "与本地服务连接中断：本地服务进程已退出。此处点「刷新」只会重开页面、不会让服务回来。";
const _API_TIMEOUT = 15000;   // 单次尝试的总预算（含其内部的网络轻量重试）
/* 2026-10-25（进哥：「加载失败之后就没下文了，不能就这么放着」）：
   旧行为：15 秒没响应 → 抛 TIMEOUT → 顶栏写一条「部分数据加载失败」→ 到此为止，界面永远停在失败态。
   新行为：失败不再是一次性的终点，而是一段**看得见时长的自动恢复过程**：
     ① 自动重试 3 轮，间隔 4s / 10s / 25s（约 40 秒窗口），期间顶栏显示「N 秒后自动重试（第 i/3 次）」；
     ② 重试成功 → 该条自动消失，并 toast「已恢复（第 i 次重试成功）」；
     ③ 3 轮仍失败 → 才落到「加载失败」，由调用方给可执行建议，不再只丢一句「加载失败」；
     ④ 任何时刻都能点「立即重试」跳过等待；写操作（POST 等非 GET）默认不自动重试，避免重复提交。
   分级超时：重接口（要取行情 + 战法复算，正常就是秒级偏上）用 _API_TIMEOUT_HEAVY，
   避免把「后端正常但慢」误报成故障——误报一次，用户就会开始不信这个界面。 */
const _API_TIMEOUT_HEAVY = 60000;   // 2026-10-25：dashboard 冷缓存重算实测 32s（机器被占时更久），30s 会稳定误报；如今主路径已改 SWR 秒回，这个预算只兜冷启动那一次。
const _API_RETRY_DELAYS = [4000, 10000, 25000];
/* 2026-10-29：重接口默认长预算（60s）。原来只有调用方显式传 opts.timeout 才会用 _API_TIMEOUT_HEAVY，
   实际全站没人传 → 所有接口一律 15s 就判超时；后端一旦被重活冻住（实测 auction 94.5s），
   全部误报「响应超过 15 秒」。这里给重接口按路径自动放宽，配合下方「单飞去重」。 */
const _API_HEAVY_PATHS = [
  "/api/backtest", "/api/dashboard", "/api/resonance", "/api/evolve", "/api/selfpick",
  "/api/quant", "/api/kronos", "/api/etf", "/api/pillars", "/api/pillars/status",
  "/api/lhb_sectors", "/api/lhb", "/api/board_flow_snapshot", "/api/board_flow_intraday",
  "/api/board_flow_range", "/api/board_flow_dates", "/api/bili/consensus", "/api/bili/pile",
  // 2026-10-29：实测冷态 /api/stocks 6.4s（帖子情绪三态聚合）、/api/summary 0.5s~5s，
  // 都超过 15s 就会误报「响应超过 15 秒」——给它们重接口预算，避免把"慢"判成"坏"。
  "/api/stocks", "/api/summary", "/api/hotspots", "/api/watch_list",
  // 2026-10-29：AI 复盘走重接口预算。原因不是"它慢"，而是它**首次入队时要拼信息包**
  // （读市场横截面/主线/资金/政策/情绪/持仓/知识库十余张表）。实测本机在调度重活期间
  // 拼一次包可能超过 15 秒 —— 那是"后端在干活"，不该被判成"坏了"并触发重试。
  "/api/ai_review",
];
/* 2026-10-29：GET 单飞表（key → 在飞 Promise）。同一接口同一参数已有请求在飞时，直接复用，
   不再另发——这才是「越刷越堵」的根治：实测重试风暴把后端队列撑到 17~25 分钟才回。 */
const _apiInflight = new Map();
const _API_LABELS = {
  "/api/dashboard": "今日看板", "/api/summary": "汇总计数", "/api/stocks": "当前讨论股票",
  "/api/watch_list": "自选池", "/api/watch_alerts": "到价提醒",
  "/api/lhb": "游资龙虎榜", "/api/sentiment": "舆论情绪", "/api/brief": "今日决策",
};
function _apiLabel(path, opts) {
  if (opts && opts.label) return opts.label;
  const p = String(path || "").split("?")[0];
  return _API_LABELS[p] || p.replace(/^\/api\//, "").split("/")[0] || "接口";
}

/* ---- 自动重试等待条（独立 DOM，不吃 setHint 的 textContent 覆盖）---- */
const _retryWait = new Map();     // key → { name, why, round, at, timer, fire }
let _retryTicker = null;

function _retryBarHost() {
  let host = document.getElementById("apiRetryBar");
  if (!host) {
    host = document.createElement("div");
    host.id = "apiRetryBar";
    host.className = "api-retry-bar";
    host.setAttribute("role", "status");
    const anchor = document.getElementById("statusHint");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(host, anchor.nextSibling);
    else document.body.insertBefore(host, document.body.firstChild);
  }
  return host;
}

function _paintRetryWait() {
  const host = _retryBarHost();
  if (!_retryWait.size) { host.classList.remove("show"); host.innerHTML = ""; return; }
  const now = Date.now();
  const parts = [];
  _retryWait.forEach((v, k) => {
    const left = Math.max(0, Math.ceil((v.at - now) / 1000));
    parts.push(
      `<span class="arb-item"><span class="arb-ico">⏳</span>` +
      `<b>${esc(v.name)}</b> ${esc(v.why)}，<em>${left}</em> 秒后自动重试（第 ${v.round + 1}/${_API_RETRY_DELAYS.length} 次）` +
      ` <button type="button" class="arb-now" data-k="${esc(k)}">立即重试</button></span>`
    );
  });
  host.classList.add("show");
  host.innerHTML = `<span class="arb-lead">正在自动恢复</span>` + parts.join("");
  host.querySelectorAll(".arb-now").forEach((b) => {
    b.onclick = () => { const v = _retryWait.get(b.dataset.k); if (v) { clearTimeout(v.timer); v.fire(); } };
  });
}

function _ensureRetryTicker() {
  if (_retryTicker) return;
  _retryTicker = setInterval(() => {
    if (!_retryWait.size && !_computing.size) { clearInterval(_retryTicker); _retryTicker = null; }
    _paintRetryWait();
    if (_computing.size) _paintComputing();   // 计算中进度条每秒刷新「已等 N 秒」
  }, 1000);
}

function _dropRetryWait(key) {
  const v = _retryWait.get(key);
  if (!v) return;
  clearTimeout(v.timer);
  _retryWait.delete(key);
  _paintRetryWait();
}

/** 登记一次「等 delay 毫秒后自动重试」：到点或被点「立即重试」即 resolve。 */
function _waitBeforeRetry(key, name, why, delay, round) {
  return new Promise((resolve) => {
    const entry = {
      name, why, round, at: Date.now() + delay, timer: null,
      fire: () => { _dropRetryWait(key); resolve(); },
    };
    entry.timer = setTimeout(() => { _dropRetryWait(key); resolve(); }, delay);
    _retryWait.set(key, entry);
    _ensureRetryTicker();
    _paintRetryWait();
  });
}

/* ---- 2026-10-29 多路回退（进哥要求：「先从 A 路走，A 路加载不上再走 B 路，B 路不行再走 C 路」）----
   以前的做法是**同一条路重试 3 次**（4s/10s/25s）——路本身不通时，试 100 次也不通，只会把后端越堆越堵。
   现在改成三层真实存在的通路，逐层降级，每层都有明确产出：
     A 路 实时接口      —— /api/xxx 现算最新数据（正常走这条）
     B 路 本地数据仓    —— /api/snapshot 读 viewer/data/warehouse 里最近一次成功加载的**完整数据**
     C 路 占位 + 后台算 —— 接口回 computing，前端显示「后台计算中」并按 15s 轮询，确定会回来
   命中 B 路时一律在顶栏挂「降级」提示并写明快照时间 —— 宁可让用户知道数据旧，也不能让他以为是实时行情。 */
const _degraded = new Map();          // 面板名 → 快照时间串
function _degradedCount() { return _degraded.size; }

function _paintDegraded() {
  let host = document.getElementById("apiDegradedBar");
  if (!_degraded.size) {
    if (host) { host.classList.remove("show"); host.innerHTML = ""; }
    return;
  }
  if (!host) {
    host = document.createElement("div");
    host.id = "apiDegradedBar";
    host.className = "api-retry-bar";
    host.setAttribute("role", "status");
    const anchor = document.getElementById("statusHint");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(host, anchor.nextSibling);
    else document.body.insertBefore(host, document.body.firstChild);
  }
  const items = [];
  _degraded.forEach((at, nm) => {
    items.push(`<span class="arb-item"><span class="arb-ico">🗄️</span><b>${esc(nm)}</b> ` +
      `显示本地快照 <em>${esc(at || "时间未知")}</em>（非最新）</span>`);
  });
  host.classList.add("show");
  host.innerHTML = `<span class="arb-lead">已切到本地数据仓</span>` + items.join("") +
    ` <button type="button" class="arb-now" id="apiDegradedDismiss">知道了</button>`;
  const btn = document.getElementById("apiDegradedDismiss");
  if (btn) btn.onclick = () => { _degraded.clear(); _paintDegraded(); };
}

/* ---- C 路：接口回「计算中」占位时，前端自动轮询直到拿到真数据 ----------------------
   为什么需要：量化/ETF/Kronos 这类面板是「全市场因子矩阵」，重算要几分钟。后端已改成
   子进程算 + 落盘（绝不冻住请求线程），代价是**第一次打开时还没有结果**。
   旧行为：面板显示空 → 用户以为坏了。新行为：明确显示「后台计算中，已等 N 秒」，
   前端每 15 秒问一次，算完自动把数据填进去（最多等 10 分钟；超时也留着进度条，随时点刷新）。
   这正对应进哥的要求：「加载时间长一点没关系，但你得确定它能回来」。 */
const _computing = new Map();      // 面板名 → { path, since }
const _COMPUTE_POLL_MS = 15000;
const _COMPUTE_MAX_TRIES = 40;     // 40 × 15s = 10 分钟

function _auxBarHost(id) {
  let host = document.getElementById(id);
  if (!host) {
    host = document.createElement("div");
    host.id = id;
    host.className = "api-retry-bar";
    host.setAttribute("role", "status");
    const anchor = document.getElementById("statusHint");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(host, anchor.nextSibling);
    else document.body.insertBefore(host, document.body.firstChild);
  }
  return host;
}

function _paintComputing() {
  const host = _auxBarHost("apiComputingBar");
  if (!_computing.size) { host.classList.remove("show"); host.innerHTML = ""; return; }
  const now = Date.now();
  const parts = [];
  _computing.forEach((v, nm) => {
    const sec = Math.max(0, Math.round((now - v.since) / 1000));
    parts.push(`<span class="arb-item"><span class="arb-ico">⚙️</span><b>${esc(nm)}</b> ` +
      `后台计算中（已等 <em>${sec}</em> 秒，算完自动出现）</span>`);
  });
  host.classList.add("show");
  host.innerHTML = `<span class="arb-lead">本地计算中</span>` + parts.join("");
}

/* 收到 computing 占位后：挂进度条 + 轮询，直到拿到真数据（或到 10 分钟上限）。
   2026-10-29：轮询节奏改为「前快后慢」—— 低位箱体/换手榜这类子进程任务通常 3~15 秒就算完，
   原来固定 15 秒一问，算完了还要再干等最多 15 秒才显示，用户体感就是「慢」。
   现在 2s → 3s → 5s → 之后 15s，首批快探几乎立刻拿到结果，长任务照样不放大请求量。 */
const _COMPUTE_BACKOFF = [2000, 3000, 5000];
function _pollComputing(first, path, name) {
  _computing.set(name, { path: path, since: Date.now() });
  _ensureRetryTicker();
  _paintComputing();
  let n = 0;
  const step = () => {
    if (n >= _COMPUTE_MAX_TRIES) {
      _computing.delete(name);
      _paintComputing();
      showToast(`【${name}】仍在后台计算（已等约 10 分钟），可稍后点「刷新」查看。`);
      return Promise.resolve(first);
    }
    const wait = _COMPUTE_BACKOFF[n] != null ? _COMPUTE_BACKOFF[n] : _COMPUTE_POLL_MS;
    n += 1;
    return new Promise((res) => setTimeout(res, wait))
      .then(() => fetch(path, { cache: "no-store" }))
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((d) => {
        if (d && !d.computing && d.ok !== false) {
          _computing.delete(name);
          _paintComputing();
          showToast(`【${name}】已算完，自动加载完成。`);
          return d;
        }
        return step();
      });
  };
  return step();
}

/** B 路：取该接口最近一次成功加载的落盘快照。命中返回数据（带 _degraded 标记），否则 null。 */
function _apiSnapshot(path, name) {
  const s = String(path || "");
  const i = s.indexOf("?");
  const p = i < 0 ? s : s.slice(0, i);
  const qs = i < 0 ? "" : s.slice(i + 1);
  const url = "/api/snapshot?path=" + encodeURIComponent(p) + "&q=" + encodeURIComponent(qs);
  return fetch(url, { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || !d.ok || !d.data) return null;
      const at = d._at || "";
      _degraded.set(name, at);
      _paintDegraded();
      showToast(`【${name}】实时加载失败，已自动切到本地快照（${at}）—— 数据不是最新。`);
      return Object.assign({}, d.data, { _degraded: true, _snapshot_at: at });
    })
    .catch(() => null);
}

function api(path, opts) {
  const isNetErr = (e) => !!e && e.name === "TypeError";   // fetch 未拿到响应时的唯一表现
  const isAbort = (e) => !!e && e.name === "AbortError";
  const o = Object.assign({}, opts);
  const name = _apiLabel(path, opts);
  const _basePath = String(path || "").split("?")[0];
  const _isHeavy = _API_HEAVY_PATHS.indexOf(_basePath) >= 0;
  const budget = (o.timeout && o.timeout > 0) ? o.timeout : (_isHeavy ? _API_TIMEOUT_HEAVY : _API_TIMEOUT);
  const allowRetry = (o.retry !== false) && !(o.method && String(o.method).toUpperCase() !== "GET");
  const key = name + "|" + String(path);
  // 2026-10-29：GET 单飞——同 key 已有请求在飞 → 直接复用，避免并发/重试重复轰炸后端。
  if (allowRetry) {
    const _inflight = _apiInflight.get(key);
    if (_inflight) return _inflight;
  }
  delete o.label; delete o.timeout; delete o.retry;

  // 单次请求：超时控制 + 网络层轻量重试（保持 2026-09-28 既有行为）
  function once() {
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, budget);
    const merged = Object.assign({}, o);
    if (merged.signal) {
      // 调用方自带 signal（如抽屉关闭时主动取消）：任意一方 abort 都中断本请求
      const ext = merged.signal;
      if (ext.addEventListener) ext.addEventListener("abort", () => ctrl.abort(), { once: true });
    } else {
      merged.signal = ctrl.signal;
    }
    const attempt = (n) => fetch(path, merged).then(async (r) => {
      if (!r.ok) {
        let body = "";
        try { body = await r.text(); } catch (e) { /* 读响应体失败时忽略 */ }
        const he = new Error(`HTTP ${r.status} ${r.statusText || ""} ${path} — ${String(body).slice(0, 200)}`);
        he.status = r.status;
        throw he;
      }
      const data = await r.json();
      markServiceHealthy();   // D2：只要有一个请求成功，就说明后端已恢复 → 撤横幅并提示
      return data;
    }, (e) => {
      // 超时（我们主动 abort）：标记 TIMEOUT 后 reject，让调用方 .catch 能清除「加载中…」
      if (isAbort(e) && timedOut) {
        const te = new Error(`请求超时（${Math.round(budget / 1000)} 秒未响应）`);
        te.code = "TIMEOUT"; te.isTimeout = true;
        throw te;
      }
      if (isNetErr(e) && n < 2) {
        return new Promise((res) => setTimeout(res, n === 0 ? 400 : 1200)).then(() => attempt(n + 1));
      }
      if (isNetErr(e)) { netDropBanner(); const ne = new Error(_API_NET_HINT); ne.code = "NET"; throw ne; }
      // 其余（含调用方主动取消的外部 AbortError）：原样抛出，交给调用方处理
      throw e;
    });
    return attempt(0).finally(() => clearTimeout(timer));
  }

  // 值不值得重试：超时 / 连不上 / 后端 5xx 值得；4xx、业务错误、主动取消不值得（重试无意义甚至有害）
  const retriable = (e) => !!e && !isAbort(e) && (!!e.isTimeout || e.code === "NET" || (e.status >= 500));
  const whyOf = (e) => e.isTimeout
    ? `响应超过 ${Math.round(budget / 1000)} 秒`
    : (e.code === "NET" ? "连不上本地服务" : `服务端 ${e.status || "异常"}`);

  function round(i) {
    return once().then(
      (d) => {
        // C 路：后端回「计算中」占位（重算面板首次打开）→ 挂进度条并自动轮询，算完自动填数据
        if (d && d.computing) return _pollComputing(d, path, name);
        if (i > 0) { _dropRetryWait(key); showToast(`【${name}】已恢复（第 ${i} 次重试成功）。`); }
        if (_degraded.delete(name)) _paintDegraded();   // 拿到实时数据 → 撤掉降级提示
        return d;
      },
      (e) => {
        const canRetry = allowRetry && retriable(e) && i < _API_RETRY_DELAYS.length;
        if (!canRetry) {
          const gaveUp = allowRetry && retriable(e) && i >= _API_RETRY_DELAYS.length;
          _dropRetryWait(key);
          if (gaveUp) {
            e.autoRetried = i;
            // A 路（实时）彻底不通 → 换 B 路（本地数据仓快照）。拿到就用，绝不把面板留成空白转圈。
            return _apiSnapshot(path, name).then((snap) => {
              if (snap) return snap;
              showToast(`【${name}】已自动重试 ${i} 次仍失败（${whyOf(e)}），本地也没有可用快照，可稍后点「刷新」重取。`);
              throw e;
            });
          }
          throw e;
        }
        return _waitBeforeRetry(key, name, whyOf(e), _API_RETRY_DELAYS[i], i).then(() => round(i + 1));
      }
    );
  }
  const _p = round(0);
  // 2026-10-29：登记单飞（仅 GET）。请求结束（成功/失败）即清表，不影响下一次正常请求。
  if (allowRetry) {
    _apiInflight.set(key, _p);
    const _cleanup = () => { if (_apiInflight.get(key) === _p) _apiInflight.delete(key); };
    _p.then(_cleanup, _cleanup);
  }
  return _p;
}
/* ---- 本地服务断连横幅（2026-10-06，王总：「点了刷新也没反应」） -------------
   背景：桌面版是 pywebview 原生窗口，服务跑在本地。**服务进程阵亡后窗口还开着**，
   页面所有 fetch 必然失败，而普通「刷新」按钮救不回来 —— 需要两条可执行出口：
     ① 服务还能救 → 走 pywebview 桥 _js_restart_service()，把后端重新拉起（桌面窗口才有这个桥）；
     ② 救不了 / 是浏览器打开的 → 明说「双击桌面『股票论坛可视化查看器』重开」。
   横幅用**独立 DOM**（不吃 setHint 的 textContent 覆盖），避免下一行失败提示立刻把它抹掉。 */
let _netDropAt = 0;
let _dropBannerShown = false;   // D2：当前是否处于"断连"状态（用于恢复时给一次提示）

/** 轻量 toast（D2 引入）：4 秒自动消失，右下角，不打断操作。 */
function showToast(msg) {
  try {
    let host = document.getElementById("toastHost");
    if (!host) {
      host = document.createElement("div");
      host.id = "toastHost";
      document.body.appendChild(host);
    }
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    host.appendChild(t);
    setTimeout(() => t.classList.add("toast-out"), 3400);
    setTimeout(() => t.remove(), 4000);
  } catch (e) { /* toast 失败不该影响主流程 */ }
}

/** D2：后端恢复时的收尾——撤掉断连横幅，并提示一次「已自动重启」。 */
function markServiceHealthy() {
  if (!_dropBannerShown) return;
  _dropBannerShown = false;
  const bar = document.getElementById("netDropBar");
  if (bar) bar.remove();
  showToast("本地服务已恢复（后端由常驻守护自动重启）。数据可正常刷新。");
}

function hasLocalRestartBridge() {
  return typeof window._js_restart_service === "function";
}
function netDropBanner() {
  const now = Date.now();
  _dropBannerShown = true;
  if (now - _netDropAt < 20000) return;   // 20 秒内不重复刷同一个横幅
  _netDropAt = now;
  const host = document.getElementById("netDropBar") || (() => {
    const d = document.createElement("div");
    d.id = "netDropBar";
    const hint = document.getElementById("statusHint");
    (hint && hint.parentNode || document.body).insertBefore(d, hint ? hint.nextSibling : null);
    return d;
  })();
  const canHeal = hasLocalRestartBridge();
  host.innerHTML = `<span class="banner banner-warn" style="margin:6px 0;display:flex;gap:8px;`
    + `align-items:center;flex-wrap:wrap;flex:1 1 auto">`
    + `<span>本地服务未启动：服务进程已退出，页面还开着但取不到数据 —— `
    + (canHeal
      ? `点「重启本地服务」把后端拉回来，再重新加载页面即可继续。`
      : `点「重新加载」无用，请双击桌面「股票论坛可视化查看器」重开。`)
    + `</span>`
    + (canHeal ? `<button class="btn sm primary" data-restart="1" onclick="restartLocalService()">重启本地服务</button>` : "")
    + `<button class="btn sm" onclick="location.reload()">重新加载页面</button></span>`;
}
function restartLocalService() {
  const fn = window._js_restart_service;
  if (typeof fn !== "function") {
    setHint("当前不是桌面窗口（无自愈通道），常规刷新救不回来：请双击桌面「股票论坛可视化查看器」重开。");
    return;
  }
  const bar = document.getElementById("netDropBar");
  const btn = bar && bar.querySelector('button[data-restart]');
  if (btn) { btn.disabled = true; btn.textContent = "重启中…"; }
  setHint("正在重启本地服务…");
  const done = (msg) => {
    setHint("本地服务：" + msg + " —— 点顶部「重新加载页面」或按 F5 取数。");
    if (bar) bar.remove();
  };
  Promise.resolve(fn())
    .then((r) => done((r && r.message) || "已发起重启"))
    .catch((e) => done("调用失败：" + e));
}
function el(html) {
  const d = document.createElement("div");
  d.innerHTML = html.trim();
  return d.firstElementChild;
}
function esc(s) {
  return (s ?? "").toString().replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
/* P2-1：属性上下文专用转义。esc() 不转义引号，而 " 与 ' 足以闭合属性、
   注入 on* 事件（无需 < 即可属性逃逸）。属性值一律用 escAttr()。 */
function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ========================================================================
   前端全量整改增强模块（2026-10-22）· 集中放基础工具，供下方新功能复用。
   涉及条目：P3-11 / P3-12 / P2-12 / P2-28 / P1-48 / P2-36 / P3-14 / P2-26 /
   P3-18 / P1-50 / P1-51 / P3-17 / P1-26。所有新增功能均经既有 api() 出口调用。
   ======================================================================== */

/* P3-11：百分号拼接统一。signed=true 时正数带 "+"（负数自带 "-"）。
   v 可为数字或数字字符串；非法/缺失返回 opt.empty（默认 "—"）。
   统一替换散落的 `(v>=0?"+":"")+v.toFixed(2)+"%"` 漂移写法。 */
function fmtPct(v, opt) {
  opt = opt || {};
  const d = opt.digits == null ? 2 : opt.digits;
  const n = typeof v === "number" ? v : parseFloat(v);
  if (v == null || v === "" || isNaN(n)) return opt.empty != null ? opt.empty : "—";
  const unit = opt.unit != null ? opt.unit : "%";
  if (opt.signed) return (n >= 0 ? "+" : "") + n.toFixed(d) + unit;
  return n.toFixed(d) + unit;
}

/* P3-12：信息密度（紧凑/舒适）。存 localStorage，根节点 data-density 切换，CSS 据此调整字号/间距。 */
function applyDensity(mode) {
  mode = mode || localStorage.getItem("viewer-density") || "comfortable";
  document.documentElement.setAttribute("data-density", mode);
  localStorage.setItem("viewer-density", mode);
  const btn = document.getElementById("btnDensity");
  if (btn) btn.textContent = (mode === "compact" ? "舒适" : "紧凑") + "密度";
}
function toggleDensity() { applyDensity(localStorage.getItem("viewer-density") === "compact" ? "comfortable" : "compact"); }

/* P2-28：记录当前抽屉打开的个股代码，供顶部跳转 chip 使用。 */
let DRAWER_SYMBOL = "";
/* P3-13：记录打开抽屉前的焦点元素，关闭时回焦。 */
let DRAWER_LAST_FOCUS = null;
/* 2026-10-23：个股抽屉的「请求取消器」。打开个股时新建，关闭（或再次打开另一只）时 abort，
   让仍在进行中的分区请求立刻终止，减少与首屏/轮询对连接池的争用（仍保留 api() 内部 15s 超时兜底）。 */
let _stockAbort = null;

/* P2-12：统一「catch 不应静默吞错」工具。
   reportCatch 把错误计入顶栏 apiErrorBar（与 reportLoadError 同通道），并返回友好提示串。
   emptyRetry 生成「无数据 + ↻重试」空态（占位符里塞一个重新执行的按钮）。 */
function reportCatch(label, e) {
  const msg = `【${label}】${apiErrText(e)}`;
  loadErrors.push(msg);
  renderErrorBar();
  return msg;
}
function emptyRetry(label, retryFn, extraNote) {
  const id = "rt-" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    const b = document.getElementById(id);
    if (b) b.onclick = () => { const host = b.closest(".empty"); if (host) host.innerHTML = "加载中…"; try { retryFn(); } catch (e) { reportCatch(label, e); } };
  }, 0);
  return `<div class="empty">${esc(label)}加载失败或暂无数据。${extraNote ? " " + esc(extraNote) : ""}`
    + `<button class="btn sm" id="${id}">↻ 重试</button></div>`;
}

/* P2-28：抽屉顶部跳转 chip —— 一只票一跳到龙虎榜 / 资金 / 共振 / 回测。
   仅当 DRAWER_SYMBOL 为合法 6 位代码时渲染。 */
function drawerJumpChips() {
  if (!DRAWER_SYMBOL || !/^\d{6}$/.test(DRAWER_SYMBOL)) return "";
  const c = escAttr(DRAWER_SYMBOL);
  const chip = (tab, cat, label) =>
    `<a class="dchip" href="javascript:void(0)" onclick="closeDrawer();selectNav('${cat}','${tab}',true);return false;">${esc(label)}</a>`;
  return `<div class="drawer-jump" role="navigation" aria-label="跳转">
      <span class="dchip-lbl">看 ${esc(DRAWER_SYMBOL)}：</span>
      ${chip("lhb", "money", "游资龙虎榜")}
      ${chip("moneyflow", "money", "资金流向")}
      ${chip("resonance", "overview", "多源共振")}
      ${chip("strategy_lab", "learn", "策略回测")}
      ${chip("lhb_sectors", "money", "板块动向")}
    </div>`;
}

/* =====================================================================
   新增功能实现（2026-10-22 全量整改）。所有外部调用统一走 api() 出口，
   未就绪的后端端点一律优雅降级（显示「需后端端点」而非崩溃）。
   ===================================================================== */

/* P3-12 / P2-36：密度按钮、免责声明折叠、新顶栏按钮接线。 */
function initEnhancements() {
  // 密度切换
  const bDen = document.getElementById("btnDensity");
  if (bDen) bDen.onclick = toggleDensity;
  applyDensity();
  // 命令面板
  const bCmd = document.getElementById("btnCmdPalette");
  if (bCmd) bCmd.onclick = openCmdPalette;
  // 设置 / 运维
  const bSet = document.getElementById("btnSettings");
  if (bSet) bSet.onclick = openSettings;
  // 免责声明折叠（仅本会话）
  const fb = document.getElementById("disclaimerFold");
  const bar = document.getElementById("disclaimerBar");
  if (fb && bar) fb.onclick = () => {
    const folded = bar.classList.toggle("db-folded");
    fb.textContent = folded ? "展开 ▾" : "收起 ▴";
  };
  // 设置弹窗关闭
  const sc = document.getElementById("settingsClose");
  if (sc) sc.onclick = closeSettings;
  const sm = document.getElementById("settingsMask");
  if (sm) sm.onclick = closeSettings;
  // 命令面板遮罩
  const cm = document.getElementById("cmdMask");
  if (cm) cm.onclick = closeCmdPalette;
  // 全局快捷键：Ctrl/⌘+K 命令面板；Esc 关闭命令面板/设置
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      const cp = document.getElementById("cmdPalette");
      if (cp && !cp.hidden) closeCmdPalette(); else openCmdPalette();
      return;
    }
    if (e.key === "Escape") {
      const sp = document.getElementById("settingsModal");
      if (sp && !sp.hidden) { closeSettings(); return; }
      const fr = document.getElementById("firstRunModal");
      if (fr && !fr.hidden) { /* 首启引导不响应 Esc，强制走完 */ }
    }
  });
  // 命令面板输入
  const ci = document.getElementById("cmdInput");
  if (ci) ci.oninput = (e) => renderCmdList(e.target.value);
  // 首启引导。`?skipguide=1` 是排障/截图用的开关：无头浏览器没有 localStorage 上下文，
  // 每次都会被首启弹窗盖住内容，看不清页面实际渲染成什么样。不影响正常使用（正常打开不带该参数）。
  if (!localStorage.getItem("viewer-first-run-done") && !/[?&]skipguide=1/.test(location.search)) {
    try { showFirstRun(); } catch (_) {}
  }
}

/* P1-48 命令面板：从 NAV 生成「跳面板」命令 + 常用动作，支持 6 位代码直开个股。 */
let CMD_ITEMS = [];
function buildCmdItems() {
  const items = [];
  NAV.forEach((cat) => {
    itemsOfCat(cat).forEach((it) => {
      if (!it.tab) return;
      items.push({ ico: "▸", cat: cat.key, tab: it.tab, label: it.label, group: cat.label });
    });
  });
  // 常用动作
  items.push({ ico: "⟳", action: "refresh", label: "一键刷新全部", group: "动作" });
  items.push({ ico: "⚙", action: "settings", label: "打开设置 / 运维", group: "动作" });
  items.push({ ico: "▦", action: "density", label: "切换信息密度", group: "动作" });
  items.push({ ico: "⏻", action: "shutdown", label: "退出后台服务", group: "动作" });
  items.push({ ico: "↻", action: "restart", label: "重启后台服务", group: "动作" });
  items.push({ ico: "🗋", action: "logs", label: "打开日志目录", group: "动作" });
  items.push({ ico: "💾", action: "backup", label: "备份当前数据库", group: "动作" });
  CMD_ITEMS = items;
}
function openCmdPalette() {
  const cp = document.getElementById("cmdPalette");
  const mask = document.getElementById("cmdMask");
  if (!cp) return;
  if (!CMD_ITEMS.length) buildCmdItems();
  cp.hidden = false; if (mask) mask.classList.add("show");
  const ci = document.getElementById("cmdInput");
  if (ci) { ci.value = ""; ci.focus(); }
  renderCmdList("");
}
function closeCmdPalette() {
  const cp = document.getElementById("cmdPalette");
  const mask = document.getElementById("cmdMask");
  if (cp) cp.hidden = true;
  if (mask) mask.classList.remove("show");
}
function renderCmdList(q) {
  buildCmdItems();
  q = (q || "").trim().toLowerCase();
  const list = document.getElementById("cmdList");
  if (!list) return;
  let matched = CMD_ITEMS;
  if (q) {
    // 6 位代码直开个股优先
    if (/^\d{6}$/.test(q)) {
      list.innerHTML = `<div class="cmd-item active" data-code="${q}">
        <span class="cmd-ico">🔍</span><span>打开个股 ${q}</span><span class="cmd-cat">个股</span></div>`;
      bindCmdFirst();
      return;
    }
    matched = CMD_ITEMS.filter((x) => (x.label || "").toLowerCase().includes(q) || (x.group || "").toLowerCase().includes(q));
  }
  if (!matched.length) { list.innerHTML = `<div class="cmd-item"><span class="cmd-ico">∅</span><span>无匹配项</span></div>`; return; }
  list.innerHTML = matched.slice(0, 40).map((x, i) => {
    const arg = x.tab ? `data-cat="${escAttr(x.cat)}" data-tab="${escAttr(x.tab)}"` : `data-action="${escAttr(x.action)}"`;
    return `<div class="cmd-item${i === 0 ? " active" : ""}" ${arg}>
      <span class="cmd-ico">${esc(x.ico || "•")}</span><span>${esc(x.label)}</span><span class="cmd-cat">${esc(x.group || "")}</span></div>`;
  }).join("");
  // 单击
  list.querySelectorAll(".cmd-item").forEach((el) => {
    el.onclick = () => cmdActivate(el);
  });
  // 键盘上下选择 + 回车
  bindCmdKeys();
}
function bindCmdFirst() {
  const el = document.querySelector("#cmdList .cmd-item");
  if (el) el.onclick = () => { const c = el.dataset.code; closeCmdPalette(); openStock(c, null); };
}
let _cmdKeyBound = false;
function bindCmdKeys() {
  const ci = document.getElementById("cmdInput");
  if (!ci || _cmdKeyBound) return;
  _cmdKeyBound = true;
  ci.addEventListener("keydown", (e) => {
    const items = document.querySelectorAll("#cmdList .cmd-item");
    let idx = [...items].findIndex((x) => x.classList.contains("active"));
    if (e.key === "ArrowDown") { e.preventDefault(); idx = Math.min(items.length - 1, idx + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); idx = Math.max(0, idx - 1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const first = document.querySelector("#cmdList .cmd-item[data-code]");
      if (first && /^[\d]{6}$/.test((ci.value || "").trim())) { cmdActivate(first); return; }
      if (items[idx]) cmdActivate(items[idx]);
      return;
    } else return;
    items.forEach((x) => x.classList.remove("active"));
    if (items[idx]) { items[idx].classList.add("active"); items[idx].scrollIntoView({ block: "nearest" }); }
  });
}
function cmdActivate(el) {
  if (!el) return;
  if (el.dataset.code) { const c = el.dataset.code; closeCmdPalette(); openStock(c, null); return; }
  if (el.dataset.action) { closeCmdPalette(); cmdAction(el.dataset.action); return; }
  if (el.dataset.tab) { closeCmdPalette(); selectNav(el.dataset.cat, el.dataset.tab, true); }
}
function cmdAction(a) {
  if (a === "refresh") { const b = document.getElementById("btnAllRefresh"); if (b) b.click(); }
  else if (a === "settings") openSettings();
  else if (a === "density") toggleDensity();
  else if (a === "shutdown") serviceShutdown();
  else if (a === "restart") serviceRestart();
  else if (a === "logs") openLogDir();
  else if (a === "backup") backupDb();
}

/* P2-26 / P1-51 / P3-17 / P3-18 / P1-50 设置（运维）面板 */
function openSettings() {
  const m = document.getElementById("settingsModal");
  const mask = document.getElementById("settingsMask");
  if (!m) return;
  m.hidden = false; if (mask) mask.classList.add("show");
  renderSettingsBody();
}
function closeSettings() {
  const m = document.getElementById("settingsModal");
  const mask = document.getElementById("settingsMask");
  if (m) m.hidden = true;
  if (mask) mask.classList.remove("show");
}

/* P2-26：抓取管道运维面板入口（数据源 / 原始存档 / 抓取登录日志）。这些面板原从导航撤下（看盘无用），
   但运维时仍需可查。从「设置」进入，直接渲染既有容器 #p-sources / #p-archive / #p-fetchlog，
   不改动 NAV、不污染首屏。数据随 loadAll 落 SUMMARY，这里复读缓存即可。 */
function openOps(tab) {
  closeSettings();
  const box = ensurePanel(tab);
  if (!box) { showToast("面板容器缺失：" + tab); return; }
  $$(".panel").forEach((p) => p.classList.remove("active"));
  box.classList.add("active");
  if (tab === "sources") renderSourcesPanel(SUMMARY.data);
  else if (tab === "archive") renderArchivePanel();
  else if (tab === "fetchlog") renderFetchLogPanel(SUMMARY.data);
  try { history.replaceState(null, "", "#ops/" + tab); } catch (e) {}
  const c = document.querySelector(".content"); if (c) c.scrollTo(0, 0);
  showToast("已打开运维面板：" + ({ sources: "数据源 & 抓取状态", archive: "原始存档", fetchlog: "抓取 & 登录日志" }[tab] || tab));
}
function settingsStatus(id, kind, text) {
  const el = document.getElementById(id);
  if (el) { el.className = "set-status " + kind; el.textContent = text; }
}
/* 配置读写（P3-18 / P1-50）：优先走 B 提供的 /api/config/get|set；失败则前端本地兜底（localStorage）。 */
function cfgGet(key, fallback) {
  return Promise.race([
    api("/api/config/get?key=" + encodeURIComponent(key)).then((d) => (d && d.value != null ? d.value : fallback))
      .catch(() => { const v = localStorage.getItem("cfg-" + key); return v == null ? fallback : v; }),
    new Promise((res) => setTimeout(() => res(localStorage.getItem("cfg-" + key) != null ? localStorage.getItem("cfg-" + key) : fallback), 4000)),
  ]);
}
function cfgSet(key, value) {
  localStorage.setItem("cfg-" + key, String(value));
  return api("/api/config/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, value }) })
    .then((d) => (d && d.ok !== false)).catch(() => true); // 后端未提供端点时本地已存，仍算成功
}
function renderSettingsBody() {
  const body = document.getElementById("settingsBody");
  if (!body) return;
  body.innerHTML = `
    <div class="set-sec">
      <div class="set-sec-h">后台服务可控 <span class="set-badge ok">保留常驻</span></div>
      <div class="set-row">
        <button class="btn sm" id="setRestart">↻ 重启后台服务</button>
        <button class="btn sm" id="setShutdown">⏻ 退出后台服务</button>
        <span class="set-note">退出后须双击桌面图标重开；重启走桌面桥（pywebview）。</span>
      </div>
      <div class="set-row">
        <button class="btn sm" id="setLogs">🗋 打开日志目录</button>
        <button class="btn sm" id="setBackup">💾 备份数据库</button>
        <button class="btn sm" id="setRestore">⤴ 恢复最近备份</button>
        <span id="svcStatus" class="set-status"></span>
      </div>
    </div>
    <div class="set-sec">
      <div class="set-sec-h">刷新与回填配置 <span class="set-badge need">部分需后端端点</span></div>
      <div class="set-row"><label>实时刷新间隔(分钟)</label>
        <input type="number" id="cfgRealtime" min="1" max="120" value="5" />
        <button class="btn sm" data-cfg="realtime_interval">保存</button><span id="cfgRealtimeSt" class="set-status"></span></div>
      <div class="set-row"><label>自动调度间隔(分钟)</label>
        <input type="number" id="cfgSched" min="1" max="120" value="10" />
        <button class="btn sm" data-cfg="sched_interval">保存</button><span id="cfgSchedSt" class="set-status"></span></div>
      <div class="set-row"><label>盘中轮询间隔(秒)</label>
        <input type="number" id="cfgPoll" min="10" max="600" value="60" />
        <button class="btn sm" data-cfg="page_poll_interval">保存</button><span id="cfgPollSt" class="set-status"></span></div>
      <div class="set-row">
        <button class="btn sm" id="setLhbBackfill">龙虎榜历史回填</button>
        <span class="set-note">回填 3 年龙虎榜与席位 n（需后端 /api/lhb/backfill）。</span></div>
    </div>
    <div class="set-sec">
      <div class="set-sec-h">显示</div>
      <div class="set-row">
        <label>信息密度</label>
        <button class="btn sm" id="setDensity">${localStorage.getItem("viewer-density") === "compact" ? "切到舒适" : "切到紧凑"}</button>
        <span class="set-note">紧凑：字号/间距整体收窄，4K/投影更友好。</span>
      </div>
      <div class="set-row"><span class="set-note">主题切换在顶栏右侧色块。免责声明横幅常驻，提示「数据仅供学习、不构成投资建议」。</span></div>
    <div class="set-sec">
      <div class="set-sec-h">抓取管道运维 <span class="set-badge need">留档 · 管道信息</span></div>
      <div class="set-row">
        <button class="btn sm" id="setOpsSources">数据源 &amp; 抓取状态</button>
        <button class="btn sm" id="setOpsArchive">原始存档浏览器</button>
        <button class="btn sm" id="setOpsFetchlog">抓取 &amp; 登录日志</button>
        <span class="set-note">数据源接入、原始页存档、抓取/登录日志（落库照常，仅从导航撤下，看盘不显）。</span>
      </div>
    </div>
    </div>`;
  // 拉取已存配置回填
  cfgGet("realtime_interval", 5).then((v) => { const e = document.getElementById("cfgRealtime"); if (e && v != null) e.value = v; });
  cfgGet("sched_interval", 10).then((v) => { const e = document.getElementById("cfgSched"); if (e && v != null) e.value = v; });
  cfgGet("page_poll_interval", 60).then((v) => { const e = document.getElementById("cfgPoll"); if (e && v != null) e.value = v; });
  // 绑定
  const sr = document.getElementById("setRestart"); if (sr) sr.onclick = () => { serviceRestart(); settingsStatus("svcStatus", "ok", "已发起重启"); };
  const sd = document.getElementById("setShutdown"); if (sd) sd.onclick = () => serviceShutdown();
  const sl = document.getElementById("setLogs"); if (sl) sl.onclick = () => openLogDir();
  const sb = document.getElementById("setBackup"); if (sb) sb.onclick = () => backupDb();
  const sr2 = document.getElementById("setRestore"); if (sr2) sr2.onclick = () => restoreBackup();
  const sdf = document.getElementById("setDensity"); if (sdf) sdf.onclick = () => { toggleDensity(); const b = document.getElementById("setDensity"); if (b) b.textContent = localStorage.getItem("viewer-density") === "compact" ? "切到舒适" : "切到紧凑"; };
  const sbf = document.getElementById("setLhbBackfill"); if (sbf) sbf.onclick = () => lhbBackfill();
  const so1 = document.getElementById("setOpsSources"); if (so1) so1.onclick = () => openOps("sources");
  const so2 = document.getElementById("setOpsArchive"); if (so2) so2.onclick = () => openOps("archive");
  const so3 = document.getElementById("setOpsFetchlog"); if (so3) so3.onclick = () => openOps("fetchlog");
  body.querySelectorAll("button[data-cfg]").forEach((b) => {
    b.onclick = () => {
      const k = b.dataset.cfg;
      const map = { realtime_interval: "cfgRealtime", sched_interval: "cfgSched", page_poll_interval: "cfgPoll" };
      const inp = document.getElementById(map[k]);
      const st = document.getElementById(map[k] + "St");
      const v = inp ? Number(inp.value) : null;
      if (v == null || isNaN(v)) { if (st) settingsStatus(map[k] + "St", "bad", "请输入数字"); return; }
      cfgSet(k, v).then((ok) => { if (st) settingsStatus(map[k] + "St", ok ? "ok" : "wait", ok ? "已保存（本地生效）" : "已存本地，待后端端点同步"); });
    };
  });
}

/* P1-51 后台服务可控：重启（桌面桥）/ 退出（/api/shutdown，二次确认）。退出后页面给出提示。 */
function serviceRestart() {
  const fn = window._js_restart_service;
  if (typeof fn === "function") {
    Promise.resolve(fn()).then((r) => showToast("已发起后台服务重启：" + ((r && r.message) || ""))).catch((e) => showToast("重启失败：" + e));
  } else {
    showToast("当前非桌面窗口：请双击桌面「股票论坛可视化查看器」重开。");
  }
}
function serviceShutdown() {
  if (!confirm("确认退出本地后台服务？退出后「到价提醒」等随服务停止，需双击桌面图标重开。")) return;
  api("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((d) => {
      showToast("后台服务已收到退出指令" + ((d && d.message) ? "：" + d.message : "。"));
      settingsStatus("svcStatus", "ok", "已退出");
      netDropBanner();
    })
    .catch((e) => {
      // 后端若尚未提供 /api/shutdown，提示待 B 提供端点
      showToast("退出指令未送达（" + apiErrText(e) + "）。需后端 /api/shutdown 端点。");
      settingsStatus("svcStatus", "wait", "需后端 /api/shutdown");
    });
}

/* P3-17 打开日志目录 / 备份 / 恢复（均走 B 提供端点，未提供则友好提示）。 */
function openLogDir() {
  api("/api/open_dir?dir=logs").then((d) => { if (d && d.ok) showToast("已尝试打开日志目录。"); else showToast("打开日志目录未成功（需后端 /api/open_dir）。"); })
    .catch((e) => showToast("打开日志目录失败（需后端 /api/open_dir）：" + apiErrText(e)));
}
function backupDb() {
  api("/api/backup", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((d) => { showToast((d && d.ok) ? "已备份数据库。" : "备份未成功（需后端 /api/backup）。"); settingsStatus("svcStatus", "ok", "已备份"); })
    .catch((e) => { settingsStatus("svcStatus", "wait", "需后端 /api/backup"); showToast("备份失败（需后端 /api/backup）：" + apiErrText(e)); });
}
function restoreBackup() {
  if (!confirm("确认用最近一次备份覆盖当前数据库？此操作不可撤销。")) return;
  api("/api/backup/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((d) => { showToast((d && d.ok) ? "已恢复最近备份。" : "恢复未成功（需后端 /api/backup/restore）。"); settingsStatus("svcStatus", "ok", "已恢复"); })
    .catch((e) => { settingsStatus("svcStatus", "wait", "需后端 /api/backup/restore"); showToast("恢复失败（需后端端点）：" + apiErrText(e)); });
}
function lhbBackfill() {
  api("/api/lhb/backfill", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((d) => { showToast((d && d.ok) ? "龙虎榜回填已启动。" : "回填未启动（需后端 /api/lhb/backfill）。"); })
    .catch((e) => showToast("回填失败（需后端端点）：" + apiErrText(e)));
}

/* P3-14 首次启动引导：3 步（选主题 → 导入自选 → 解释三开关），仅首次。 */
function showFirstRun() {
  const m = document.getElementById("firstRunModal");
  const mask = document.getElementById("firstRunMask");
  if (!m) return;
  m.hidden = false; if (mask) mask.classList.add("show");
  const steps = [
    { t: "① 选个主题", b: "顶栏右侧有 4 个色块（A 浅色科技 / B 深色大屏 / C 简约报表 / D 卡片暖色），<b>点一下即可切换</b>，偏好会记住。" },
    { t: "② 导入你的自选", b: "进「<b>我的票 → 关注池</b>」点「＋ 加入自选」，把你想盯的票加进去；到价提醒、入手概率都会围绕关注池展开。" },
    { t: "③ 三个开关是干嘛的", b: "顶栏三个开关：<b>实时刷新</b>(5分钟按需补抓) / <b>自动调度</b>(交易日后台主引擎) / <b>盘中轮询</b>(仅重渲当前页)。想找票？按 <b>Ctrl/⌘+K</b> 直接搜。" },
  ];
  document.getElementById("firstRunBody").innerHTML = steps.map((s, i) =>
    `<div class="fr-step"><div class="fr-num">${i + 1}</div><div class="fr-b"><b>${s.t}</b><br/>${s.b}</div></div>`).join("")
    + `<div style="text-align:center;margin-top:8px"><span class="fr-dot on"></span><span class="fr-dot on"></span><span class="fr-dot on"></span></div>`;
  const foot = document.getElementById("firstRunFoot");
  foot.innerHTML = `<button class="btn primary" id="frDone">开始使用</button>`;
  document.getElementById("frDone").onclick = () => {
    localStorage.setItem("viewer-first-run-done", "1");
    m.hidden = true; if (mask) mask.classList.remove("show");
  };
}
/* ===== 2026-10-01 王总：个股代码必须「看得到就能点开」=====
   现象：当红参赛股等表格里代码只是纯文本（只能复制），点不进「个股诊断」抽屉。
   解法：全站统一一个入口 codeLink()，任何地方渲染个股代码都走它，
   靠 data-code-open + 捕获阶段委托一次拦下，天然压住父级 onclick
   （例如盯盘总览整行是「选中该股看公告」、研报表整行是「打开研报」）。

   硬规则（与 3532 行注释同源，别小看）：
     · 只给 **6 位纯数字** 的沪深北 A 股代码挂入口。行情接口（东财 / 腾讯）只解析沪深北，
       给美股（usAAPL）、港股（00700）甚至空仓行挂上去 = 点开一个必然失败的抽屉。
     · 挂不上的显示纯文本，保持原样，绝不伪造入口。
   opt.cls 覆盖样式类（默认 wt-sym）；opt.text 自定义显示文字（如「名称 代码」里只让代码可点）。 */
/* ===== 行业龙头分级（金/银/铜牌）· 2026-10-21 进哥需求 =====
 * 口径：中证龙头企业指数六项综合排名（总市值/成交额/营收/净利润/毛利率/ROE 行业内排名求和），
 *       后端 viewer/dragon.py 按 A 股 128 个东财二级行业算好 → /api/dragon_map 只回 {code: tier}。
 * 三档（进哥定的放宽标准）：金牌=行业内前5%（真龙头口径）/ 银牌=前10% / 铜牌=前15%；
 *       小行业（不足 8 家）按名次 1金/2银/3铜；ST 不发牌。
 * 展示范围：不止「长期底部埋伏」——凡是渲染个股代码的地方都挂（codeLink 是公共入口），
 *       点开个股详情弹窗再加一枚带文字的大牌。
 */
const DRAGON = { m: null, meta: null, loading: false, err: null, pending: false, patched: false };
const DRAGON_NAME = { 3: "金牌", 2: "银牌", 1: "铜牌" };
const DRAGON_MEDAL = { 3: "金", 2: "银", 1: "铜" };

function loadDragonMap(force) {
  if (DRAGON.loading) return DRAGON.ready;
  if (!force && DRAGON.m) return Promise.resolve(DRAGON.m);
  DRAGON.loading = true;
  DRAGON.ready = api("/api/dragon_map").then((d) => {
    DRAGON.loading = false;
    DRAGON.m = (d && d.m) || {};
    setTimeout(() => { try { dragonDecorate(document); } catch (_) {} }, 30);
    DRAGON.meta = (d && d.meta) || {};
    if (d && d.ok === false) DRAGON.err = d.reason || "未生成";
    return DRAGON.m;
  }).catch((e) => {
    DRAGON.loading = false;
    DRAGON.m = {};
    DRAGON.err = apiErrText(e);
    return DRAGON.m;
  });
  return DRAGON.ready;
}

function dragonTier(code) {
  if (!DRAGON.m) return 0;
  const c = String(code == null ? "" : code).trim();
  if (!/^\d{6}$/.test(c)) return 0;
  return Number(DRAGON.m[c] || 0) || 0;
}

/* 徽章 HTML。opt.full=true → 带文字的大牌（弹窗用）；否则小铜钱牌（表格用）。
 * 注意：地图尚未到位时返回空串，并触发一次「到位后补渲染」，避免首屏永远没有徽章。 */
function dragonTag(code, opt) {
  opt = opt || {};
  const c = String(code == null ? "" : code).trim();
  if (!DRAGON.m) {
    if (!DRAGON.pending) {
      DRAGON.pending = true;
      loadDragonMap().then(() => {
        if (DRAGON.patched) return;
        DRAGON.patched = true;
        setTimeout(() => {
          try {
            if (typeof CURRENT !== "undefined" && CURRENT && CURRENT.tab && typeof showTab === "function") {
              showTab(CURRENT.tab);
            }
          } catch (_) { /* 补渲染失败不影响主流程 */ }
        }, 80);
      });
    }
    return "";
  }
  const t = dragonTier(c);
  if (!t) return "";
  const nm = DRAGON_NAME[t];
  if (opt.full) {
    return `<span class="dragon-badge dg-${t}" title="行业龙头 · ${nm}（行业综合排名前${t === 3 ? 5 : t === 2 ? 10 : 15}%，点开可看行业第几）">`
         + `<i class="dg-medal">★</i>${nm}龙头</span>`;
  }
  return `<span class="dragon-badge dg-${t} dg-sm" title="行业龙头 · ${nm}">${DRAGON_MEDAL[t]}</span>`;
}

/* 全局装饰：给每一处个股锚点补金/银/铜小牌。
 * 为什么不用"在 codeLink 里拼字符串"：本工程大量面板是自绘单元格
 * （如长期底部埋伏的 <a class="wt-code" data-wt-open="...">），根本不走 codeLink。
 * 统一扫 DOM 才能做到「以后点开每一只股票都看得到」，新加的面板也自动覆盖。 */
let _dgTick = false;
function dragonDecorate(root) {
  if (!DRAGON.m) return;
  const box = root && root.querySelectorAll ? root : document;
  // 覆盖本工程三种个股入口：
  //   data-wt-open / data-code-open —— 表格与 codeLink 的代码按钮
  //   data-feat-open               —— 特色面板的个股入口
  //   span.kline-chip[data-sym]    —— 龙虎榜等的「K线小胶囊」（2026-10-21 抽样发现）
  const SEL = "[data-wt-open],[data-code-open],[data-feat-open],span.kline-chip[data-sym]";
  const els = box.querySelectorAll(SEL);
  for (const el of els) {
    if (el.getAttribute("data-dg") === "1") continue;
    el.setAttribute("data-dg", "1");
    const code = el.getAttribute("data-wt-open") || el.getAttribute("data-code-open")
      || el.getAttribute("data-feat-open") || el.getAttribute("data-sym") || "";
    const t = dragonTier(code);
    if (!t) continue;
    if (el.parentNode && el.parentNode.querySelector(':scope > .dragon-badge[data-dg-for="' + code + '"]')) continue;
    const sp = document.createElement("span");
    sp.className = "dragon-badge dg-" + t + " dg-sm";
    sp.setAttribute("data-dg-for", code);
    sp.title = "行业龙头 · " + DRAGON_NAME[t] + "（行业综合排名前" + (t === 3 ? 5 : t === 2 ? 10 : 15) + "%）";
    sp.textContent = DRAGON_MEDAL[t];
    if (el.insertAdjacentElement) el.insertAdjacentElement("afterend", sp);
  }
}
if (typeof window !== "undefined" && typeof MutationObserver !== "undefined") {
  const obs = new MutationObserver(() => {
    if (!DRAGON.m || _dgTick) return;
    _dgTick = true;
    setTimeout(() => {
      _dgTick = false;
      try { dragonDecorate(document); } catch (_) {}
      try { avoidDecorate(document); } catch (_) {}
    }, 120);
  });
  const startObs = () => {
    if (document.body) obs.observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startObs);
  else startObs();
}

/* ============ 避雷名单 · 全站跟随风险标记（2026-10-22 进哥需求） ============
   诉求：避雷名单里的下跌风险股，跳到别的筛选条件（长期埋伏 / 价值投资 / 任何面板）
   仍会出现，但看不到风险了。这里复用 DRAGON 龙头徽章的「MutationObserver + DOM 扫描」范式，
   把风险标记做成「跟随个股代码」—— 只要某处出现 AVOID_SET 里的代码，就自动挂一枚红色风险标。
   数据来自 /api/avoid（与避雷面板同源），开站即预拉（见底部 loadAvoidSet），保证其他面板先渲染也不漏标。 */
let AVOID_SET = {};        // { "600xxx": { code,name,res_score,reasons:[...] }, ... }
let _avTick = false;

/* 避雷面板的数据缓存（2026-10-31 修复 TDZ）。
   ★ 为什么必须声明在这里：`initNav()` 在文件末尾 15213 行执行，它会 selectNav → loadAvoid，
   而 `loadAvoid` 会读写 `AVOID.data`。原实现把 `const AVOID = {...}` 放在 15869 行（initNav 之后），
   结果首屏走深链 #overview/avoid 时直接抛
     ReferenceError: Cannot access 'AVOID' before initialization
   面板永远停在「正在加载数据…」。与之前 harvest / oss / selection 是同一类问题：
   **模块级 const 在使用点之后声明 = TDZ 地雷**，凡是 initNav 链路会被调到的状态对象，
   一律前置声明。 */
const AVOID = { data: null, busy: false };

/* 同一类 TDZ 地雷的另外两个（2026-10-31 全标签扫描发现）：
   `quantRefHtml()` 在 4535 行读写 QUANT、ETF 面板在 initNav 链路上读写 ETF，
   而两者原本分别声明在 16689 / 16705 行 → 深链 #overview/quant、#overview/etf
   首屏直接抛 ReferenceError，面板永远停在「正在加载数据…」。
   扫描结论：**凡是 initNav 会碰到的状态对象，声明必须早于一切使用点。** */
const QUANT = { data: null, busy: false, qtab: "panorama", panorama: null, panoramaCode: "" };
const ETF = { data: null, busy: false };

function buildAvoidSet(d) {
  const map = {};
  const items = toArr((d && d.items) || []);
  for (const it of items) {
    const c = String((it && it.code) == null ? "" : it.code).trim();
    if (!/^\d{6}$/.test(c)) continue;
    map[c] = it;
  }
  AVOID_SET = map;
}

function avoidTooltip(it) {
  if (!it) return "避雷名单 · 下跌风险";
  const rs = (it.res_score == null ? "" : "（低分 " + it.res_score + "）");
  const reasons = toArr(it.reasons).map((r) =>
    "· " + (r && r.tag ? r.tag : "") + (r && r.why ? "：" + r.why : "")).join("\n");
  return "⚠ 避雷名单 " + escAttr(rs) + (reasons ? "\n风险标签：\n" + reasons : "");
}

function avoidDecorate(root) {
  if (!AVOID_SET || !Object.keys(AVOID_SET).length) return;
  const box = root && root.querySelectorAll ? root : document;
  // 与 DRAGON 共用同一组个股锚点选择器：表格/codeLink/特色面板/K线小胶囊全覆盖
  const SEL = "[data-wt-open],[data-code-open],[data-feat-open],span.kline-chip[data-sym]";
  const els = box.querySelectorAll(SEL);
  for (const el of els) {
    if (el.getAttribute("data-av") === "1") continue;
    el.setAttribute("data-av", "1");
    const code = el.getAttribute("data-wt-open") || el.getAttribute("data-code-open")
      || el.getAttribute("data-feat-open") || el.getAttribute("data-sym") || "";
    const it = AVOID_SET[String(code).trim()];
    if (!it) continue;
    if (el.parentNode && el.parentNode.querySelector(':scope > .avoid-badge[data-av-for="' + code + '"]')) continue;
    const sp = document.createElement("span");
    sp.className = "avoid-badge";
    sp.setAttribute("data-av-for", code);
    sp.textContent = "⚠避雷";
    sp.title = avoidTooltip(it);
    if (el.insertAdjacentElement) el.insertAdjacentElement("afterend", sp);
  }
}

/* 开站即预拉避雷 code 集合（只取数据、不渲染面板），让其他筛选条件先渲染也不漏标。
   同时把 AVOID.data 缓存下来，避免进入避雷面板时重复打接口。 */
function loadAvoidSet() {
  return api("/api/avoid")
    .then((d) => {
      if (d && d.ok !== false && !d._err) {
        AVOID.data = d;
        buildAvoidSet(d);
        setTimeout(() => { try { avoidDecorate(document); } catch (_) {} }, 30);
      }
      return d;
    })
    .catch((e) => reportCatch("避雷名单", e));
}

function codeLink(code, opt) {
  opt = opt || {};
  const c = String(code == null ? "" : code).trim();
  const txt = opt.text == null ? c : String(opt.text);
  if (!/^\d{6}$/.test(c)) return esc(txt || "—");
  return `<button type="button" class="${escAttr(opt.cls || "wt-sym")}" data-code-open="${escAttr(c)}" `
       + `title="点开「${escAttr(txt || c)}」的个股结果（重要信息 · 换手 · 持股结构 · K线 · 持仓选手），自动加载，不用再点任何按钮">${esc(txt)}</button>`;
  // 龙头徽章由 dragonDecorate() 统一装饰（见下方 MutationObserver），此处不拼字符串，
  // 否则与全局装饰重复挂两枚。
}
/* 捕获阶段委托：比目标元素的 onclick 先跑，stopPropagation 后
   同行的「选中 / 打开研报 / 跳转」等父级 handler 一律不会误触发。 */
document.addEventListener("click", (ev) => {
  const el = ev.target && ev.target.closest ? ev.target.closest("[data-code-open]") : null;
  if (!el) return;
  ev.preventDefault();
  ev.stopPropagation();
  openStock(el.getAttribute("data-code-open"), el);
}, true);
/* P2-9：接口少给字段/给错类型时一律降级为空数组，避免 .length / .map 抛错 */
function toArr(x) { return Array.isArray(x) ? x : []; }
function setHint(t) { $("#statusHint").textContent = t; }

/* ---------- 说明文字折叠（2026-09-23 UI 整理） ----------
   实测全站 16 段说明合计 2629 字、最长单段 196 字，把首屏正内容一路挤下去，几乎没人通读。
   这里统一把**超过 HINT_FOLD_AT 字**的说明折成 2 行，点「展开说明」看全文。
   覆盖 .hint（面板说明）与 .kb-note（知识库注意事项）两类——都带底色、都满宽，
   所以只折叠高度、不限容器宽度（限宽会让色块孤零零停在内容区左侧，见 app.css 注释）。
   用 MutationObserver 而不是在每个 render 里各改一遍：新增/改动的面板自动生效，不会漏。
   阈值按"中文 2 行 × 约 45 字"取，短提示（如「共 N 条」）不受影响。 */
const HINT_FOLD_AT = 110;
const HINT_FOLD_SEL = ".hint, .kb-note";
function foldLongHints() {
  document.querySelectorAll(HINT_FOLD_SEL).forEach((el) => {
    // setHint 用 textContent 覆盖内容，会连 .hint-body 与按钮一起清掉 →
    // 先复位标记，让这条提示能被重新折叠（否则会留下一个没有按钮的"半折叠"状态）
    if (el.hasAttribute("data-fold") && !el.querySelector(".hint-body")) {
      el.removeAttribute("data-fold");
      el.classList.remove("hint-fold", "open");
    }
    if (el.hasAttribute("data-fold")) return;
    if ((el.textContent || "").replace(/\s+/g, "").length <= HINT_FOLD_AT) return;
    const body = document.createElement("div");
    body.className = "hint-body";
    while (el.firstChild) body.appendChild(el.firstChild);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "hint-more";
    btn.textContent = "展开说明 ▾";
    el.setAttribute("data-fold", "1");
    el.classList.add("hint-fold");
    el.appendChild(body);
    el.appendChild(btn);
    btn.onclick = () => { btn.textContent = el.classList.toggle("open") ? "收起 ▴" : "展开说明 ▾"; };
  });
}
new MutationObserver(() => foldLongHints()).observe(document.documentElement, {
  childList: true, subtree: true,
});

/* ---------- P1-2：加载失败必须可见（不允许静默失败） ----------
   loadAll 的每个请求失败都在此登记：顶栏下方出现一条可关闭的错误条，
   同时把摘要写进 #statusHint（原先失败是整页空白且零提示）。 */
const loadErrors = [];
function apiErrText(e) {
  return ((e && e.message) ? e.message : String(e)).replace(/\s+/g, " ").trim().slice(0, 200);
}
/* 2026-10-23：分区加载兜底 —— 把「加载中…」改写为可见的失败/超时 + 重试按钮，
   彻底消除「十几分钟不回来」的永久转圈。retryFn 传入则渲染「点此重试」。 */
function stockZoneFail(hostOrHosts, label, err, retryFn) {
  let list;
  if (hostOrHosts == null) return;
  if (typeof hostOrHosts.forEach === "function") list = hostOrHosts;   // NodeList / Array
  else list = [hostOrHosts];
  const aborted = !!(err && err.name === "AbortError");   // 抽屉关闭等主动取消：静默收尾，不渲染失败态
  const timedOut = !!(err && err.isTimeout);
  const msg = timedOut ? `加载超时（${_API_TIMEOUT / 1000} 秒未响应）` : "加载失败";
  const detail = (!timedOut && err) ? `：${esc(apiErrText(err))}` : "";
  list.forEach((h) => {
    if (!h) return;
    if (aborted) { h.innerHTML = `<div class="s">—</div>`; return; }   // 主动取消：不污染已关闭的抽屉
    h.dataset.rendered = "";   // 允许后续重试重新进入加载态
    h.innerHTML = `<div class="s stk-zone-fail">${esc(label)}${msg}${detail}` +
      (retryFn ? ` <button type="button" class="retry-btn">点此重试</button>` : "") + `</div>`;
    if (retryFn) {
      const b = h.querySelector(".retry-btn");
      if (b) b.onclick = () => { h.dataset.rendered = ""; retryFn(); };
    }
  });
}
function errorBarHost() {
  let host = document.getElementById("apiErrorBar");
  if (!host) {
    host = document.createElement("div");
    host.id = "apiErrorBar";
    host.className = "api-error-bar";
    host.setAttribute("role", "alert");
    const anchor = document.getElementById("statusHint");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(host, anchor);   // 顶栏正下方
    else document.body.insertBefore(host, document.body.firstChild);
  }
  return host;
}
function renderErrorBar() {
  const host = errorBarHost();
  const uniq = [...new Set(loadErrors)];
  if (!uniq.length) { host.classList.remove("show"); host.innerHTML = ""; return; }
  host.classList.add("show");
  host.innerHTML = `<span class="api-error-txt">部分数据加载失败：${esc(uniq.join("；"))}</span>` +
    `<button class="api-error-close" id="apiErrorClose" title="关闭提示">✕</button>`;
  const b = document.getElementById("apiErrorClose");
  if (b) b.onclick = () => { loadErrors.length = 0; renderErrorBar(); };
}
function reportLoadError(label, e) {
  const msg = `【${label}】${apiErrText(e)}`;
  loadErrors.push(msg);
  renderErrorBar();
  setHint("部分数据加载失败：" + msg.slice(0, 120));
}

/* ---------- 主题切换 ---------- */
function applyTheme(id) {
  $("#theme-link").href = `static/themes/${id}.css`;
  localStorage.setItem("viewer-theme", id);
  $$("#themePick .chip").forEach((c) => c.classList.toggle("active", c.dataset.id === id));
}
function buildThemePicker() {
  const pick = $("#themePick");
  THEMES.forEach((t) => {
    const c = el(`<div class="chip" title="${t.name}" data-id="${t.id}" style="background:${t.color}"></div>`);
    c.onclick = () => applyTheme(t.id);
    pick.appendChild(c);
  });
  applyTheme(localStorage.getItem("viewer-theme") || "light-tech");
}

/* ---------- 汇总卡片：数字可点，点进去看「这个数字由哪些记录构成」 ----------
   2026-09-23 改造（回应“只罗列数字没有实际价值”）：
     卡片 →（点击）→ 明细抽屉（列出与数字一一对应的记录）→（再点行）→ 该条详情。
   数字与列表条数的一致性由后端 /api/detail_list 的 total(=同一 COUNT(*) 口径) 保证。 */
/* 2026-10-31：数据量卡片改为「仍在日更的活数据」。
   原 9 张卡里 8 张是论坛抓取产物（帖子/实盘交易/选手/持仓/人物/讨论股票/热点词/原始文档），
   论坛 2026-10-30 停抓、10-31 已清表 → 那 8 张永久显示 0，看着像"系统坏了"。
   现换成公告/龙虎榜/解禁/自选荐股/舆情语料/资讯 —— 每一项都能点开看到真实记录，
   且都是当前决策链真的在用的数据。 */
const CARD_KINDS = [
  ["全市场公告", "stock_ann"], ["龙虎榜", "lhb_stocks"], ["解禁事件", "lift_event"],
  ["自选荐股", "self_pick"], ["舆情语料", "douyin_corpus_video"], ["资讯", "news"],
];
function renderCards(c) {
  const cc = (c && typeof c === "object") ? c : {};       // P2-9：counts 整体缺失时降级为空对象
  // 单排不换行的关键：列数 = 卡片数。CSS 那边用 repeat(var(--cn), minmax(0,1fr))，
  // 这里把真实张数写进变量，卡片增删（改 CARD_KINDS）后无需再动样式。
  const box = $("#cards");
  if (!box) return;
  box.style.setProperty("--cn", String(CARD_KINDS.length));
  const cards = CARD_KINDS.map(([l, kind]) => {
    const n = cc[kind];
    // 2026-10-05（报告建议4 配置显性化）：选手榜 / 持仓采集这两项从来没配全过，
    // 但面板看着是完整的 —— 在卡片上挂个角标，别让人把行为反推出来的数据当「榜单结论」看。
    let badge = "";
    const pcfg = (SUMMARY && SUMMARY.data && SUMMARY.data.player_cfg) || null;
    if (kind === "players" && pcfg && pcfg.players_enabled === false) {
      badge = `<div class="card-badge" title="config/sites.yaml 未配置 targets.players，选手池由发帖/交易/持仓行为反推得出">未配置选手榜源，仅行为反推</div>`;
    } else if (kind === "positions" && pcfg && pcfg.positions_enabled === false) {
      badge = `<div class="card-badge" title="config/sites.yaml 未配置 player_page_tpl，持仓由行为证据反推（起始日期/建仓价为推算值）">未配置持仓页源，仅行为反推</div>`;
    }
    return `<div class="card clickable" role="button" tabindex="0" data-kind="${escAttr(kind)}"`
      + ` title="点击查看「${escAttr(l)}」的明细记录">`
      + badge
      + `<div class="n">${n == null ? "—" : esc(n)}</div><div class="l">${esc(l)}</div>`
      + `<div class="more">查看明细 ›</div></div>`;
  }).join("");
  // 2026-10-12 体验审计证据1：首屏那排「抓了多少条」是运维视角、不是决策视角 ——
  // 收进默认折叠的「系统状态」区，让首屏直接是决策内容（今日决策 / 今日看板）。
  // 折叠不删除：点一下仍能看到全部计数并下钻明细（绑定与交互零改动）。
  box.innerHTML = `<details class="sys-status"><summary>系统状态 · 数据量（${CARD_KINDS.length} 类 · 点击展开看计数与明细）</summary>`
    + `<div class="sys-grid">${cards}</div></details>`;
  bindCards();
}
function bindCards() {
  $$("#cards .card.clickable").forEach((cardEl) => {
    const open = () => openStatDetail(cardEl.dataset.kind, cardEl);
    cardEl.onclick = open;
    cardEl.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); open(); }
    };
  });
}
/* 统计卡片 → 明细抽屉 */
function openStatDetail(kind, anchorEl) {
  if (!kind) return;
  setHint("正在加载统计明细…");
  api(`/api/detail_list?kind=${encodeURIComponent(kind)}&limit=1000`).then((d) => {
    if (!d || d.error) throw new Error((d && d.error) ? d.error : "明细接口返回异常");
    openDrawer(`${d.label || kind} · 明细`, statDetailHtml(d), anchorEl);
    bindStatRows(d);                 // 必须在 openDrawer 之后绑定（此时行已渲染）
    setHint(`「${d.label || kind}」共 ${d.total} 条，已打开明细列表。`);
  }).catch((e) => reportLoadError(`统计明细(${kind})`, e));
}
function statDetailHtml(d) {
  const cols = toArr(d.columns);
  const rows = toArr(d.rows);
  const head = cols.map((c) => `<th>${esc(c.label)}</th>`).join("");
  const body = rows.map((r) => {
    const tds = cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${statCell(r, c)}</td>`).join("");
    return `<tr${d.drill ? ' class="rowlink" title="点击查看该条详情"' : ""}>${tds}</tr>`;
  }).join("");
  const shown = rows.length, total = (d.total == null ? shown : d.total);
  const note = shown < total
    ? `<div class="hint">共 ${total} 条，此处显示前 ${shown} 条（可继续分页取）。</div>`
    : `<div class="hint">共 ${total} 条，已全部列出 —— 与上方卡片数字一致。</div>`;
  return note + `<table class="tbl-detail"><thead><tr>${head}</tr></thead><tbody>`
    + (body || `<tr><td colspan="${cols.length || 1}" class="empty">暂无记录</td></tr>`)
    + `</tbody></table>`;
}
/* 单元格渲染：按列声明选用既有徽章/标签，保证与其它面板视觉一致 */
function statCell(r, c) {
  const v = r[c.key];
  if (v == null || v === "") return `<span class="s">—</span>`;
  if (c.sentiment) return sentBadge(v);
  if (c.action) return actionBadge(v);
  if (c.access) return accessBadge(v);
  if (c.cert) return Number(v) ? `<span class="cert-badge">认证</span>` : `<span class="s">—</span>`;
  if (c.tier) return tierBadge(v);
  if (c.platform) return siteBadge(v);
  if (c.site) return siteLabel(v);
  if (c.code) return codeLink(v, { cls: "mono wt-sym" });
  /* 2026-10-06 王总：统计明细表（实盘交易·明细 / 持仓记录 / 讨论股票）里的
     「代码」与「股票」两列点不动，得自己跑去查个股。
     后端列声明加了 stock / stock_name，这里挂上全站统一的 codeLink 入口：
       · 代码列 → 直接以自身代码打开个股详情抽屉；
       · 股票名列 → 取同行的 symbol（sym_key 指定的列）这段代码来开。
     两条路径都过 codeLink 的硬规则（只给 6 位沪深北 A 股挂入口），
     挂不上就退化成纯文本，绝不伪造一个点开必然失败的入口。
     注意顺序必须在 c.action / c.access 之后、c.clip 之前 —— 这两列都是文本列，
     不会被前面的徽章分支误吞，而 codeLink 内部已做 6 位数字校验。 */
  if (c.stock) return codeLink(v, { cls: "mono wt-sym" });
  if (c.stock_name) {
    const sym = r[c.sym_key || "symbol"];
    const sc = String(sym == null ? "" : sym).trim();
    return /^\d{6}$/.test(sc) ? codeLink(sc, { text: v }) : esc(v);
  }
  let s = String(v);
  if (c.clip && s.length > c.clip) s = s.slice(0, c.clip) + "…";
  return esc(s);
}
/* 明细行 → 下钻到该条详情（复用既有详情接口与抽屉） */
function bindStatRows(d) {
  if (!d || !d.drill) return;
  const dr = d.drill, rows = toArr(d.rows);
  $$("#drawerBody tr.rowlink").forEach((tr, i) => {
    const r = rows[i];
    if (!r) return;
    tr.onclick = () => drillTo(dr, r, tr);
  });
}
function drillTo(dr, r, anchorEl) {
  if (!dr) return;
  if (dr.id_key != null && r[dr.id_key] != null) {
    openDetail(dr.to, r[dr.id_key], anchorEl);          // post / trade / news 走统一详情接口
  } else if (dr.name_key && r[dr.name_key]) {
    if (dr.to === "player") openPlayer(r[dr.name_key], anchorEl);
    else if (dr.to === "person") openPerson(r[dr.name_key], anchorEl);
  } else if (dr.symbol_key && r[dr.symbol_key]) {
    openStock(r[dr.symbol_key], anchorEl);
  }
}

/* ---------- 各面板 ---------- */
function renderSummary(s) {
  const ss = (s && typeof s === "object") ? s : {};
  const STATUS_CN = { ok: "已登录", no_credential: "未登录", login_failed: "登录失败", public: "无需登录", error: "检测异常" };
  const login = toArr(ss.login_status).map((x) => {
    const dot = { ok: "dot-ok", no_credential: "dot-no", login_failed: "dot-fail", public: "dot-pub", error: "dot-err" }[x.status] || "dot-no";
    const name = x.display_name || x.site || "";
    const url = x.login_url || "";
    const needLogin = x.status !== "ok" && x.requires_login && url;
    const isPublic = x.status === "public" || (x.requires_login === false);
    // 需要登录且未登录 → 按钮有两个：
    //   ①「打开登录窗口」= 程序自己开的受控 Edge，登录成功后自动抓 cookie 存库（推荐）
    //   ②「仅打开网页」  = 在你自己的浏览器里打开，程序读不到那里的 cookie，仅用于手动查看
    // 之前只给了 ②，导致用户明明登录了、状态却永不更新——这是本轮修复的关键。
    const entry = needLogin
      ? ` <button class="btn sm login-open" data-site="${escAttr(x.site)}" ` +
        `title="程序会打开一个受控浏览器窗口，你在其中登录，登录成功后自动保存">👉 打开登录窗口</button>` +
        ` <button class="btn sm" onclick="restartEdgeForLogin('${escAttr(x.site)}')" ` +
        `title="用你原本的 Edge profile 重启并开启调试端口（登录态与标签页保留），然后自动盯盯 cookie">🔄 重启 Edge 启用调试</button>` +
        ` <button class="btn sm" onclick="openCookieImport('${escAttr(x.site)}')" ` +
        `title="你已在自己浏览器登录，从 DevTools 导出 cookie 粘进来（最稳的方式）">📋 粘贴 Cookie</button>` +
        ` <a class="login-link" href="${escAttr(url)}" target="_blank" rel="noopener" ` +
        `title="仅在自己浏览器里打开（程序读不到该窗口的登录状态）">仅打开网页</a>`
      : (isPublic ? ` <span class="s">公开访问，无需登录</span>` : "");
    return `<div class="login-row"><span class="status-dot ${dot}"></span><b>${esc(name)}</b> <span class="s">${esc(STATUS_CN[x.status] || x.status)}</span>${entry} <span class="s" style="color:var(--muted)">${esc(x.detail || "")}</span></div>`;
  }).join("");
  /* 2026-10-02 王总原话：「你把这些帖子给我看，我不想看没有用……总结完之后分析分析他们的
     情绪在哪儿，看某一只股票或者某一个板块，他们是看涨还是看跌，这种你直接给我输出结论。
     我要帖子链接没什么意义，我哪有那么多功夫看。」
     所以这里不再铺一张「作者/时间/情绪/内容」的原帖表，改成后端先归纳好的**情绪速览**：
     总论 → 按行业 → 按个股，全是「结论 + 票数 + 一句人话」；原帖折叠在最底下，想看再看。 */
  // 2026-10-12 体验审计 P1（① 汇总）：原来是「登录状态 + 数据量」的纯运维页 →
  // 把**结论（近 24h 帖子情绪速览）提到最前**，登录态/抓取时间收进默认折叠的「系统状态」。
  // 面板名保持不变（"汇总"），避免打断已有的护栏与深链接。
  $("#p-summary").innerHTML = `
      <div id="pd-box"><div class="hint">正在归纳近 24 小时帖子情绪…</div></div>
      <details class="sys-status" style="margin-top:14px">
        <summary>系统状态 · 登录态与抓取（运维信息，点击展开）</summary>
        <div style="padding:6px 0 12px">
          <div class="hint hint-slim">最后抓取：${esc(ss.last_fetch_at || "尚未抓取")} ｜ 接入站点：${toArr(ss.sites).join("、") || "—"}</div>
          <div class="card" style="margin-bottom:6px"><div class="t" style="font-weight:700;margin-bottom:8px">登录态检测</div>${login || "<span class='s'>尚未检测</span>"}</div>
        </div>
      </details>
      <div id="pillar-snapshot" class="card" style="margin-top:14px"><div class="t">四支柱每日预测 · 速览</div><div class="hint">加载中…</div></div>`;
  bindLoginOpen();
  Promise.all([
    api("/api/postdigest?hours=24").catch((e) => ({ ok: false, error: String((e && e.message) || e) })),
    api("/api/posts?limit=20").catch(() => []),
    api("/api/pillars").catch(() => ({ ok: false })),
  ]).then(([pd, posts, pillars]) => {
    const box = $("#pd-box");
    if (!box) return;
    box.innerHTML = renderPostDigest(pd, toArr(posts));
    bindRows();
    const snap = $("#pillar-snapshot");
    if (snap) snap.innerHTML = `<div class="t">四支柱每日预测 · 速览</div>` + renderPillarSnapshot(pillars);
  });
}

/* ---------- 帖子情绪速览（2026-10-02） ----------
   原则：先给结论，再给证据；没结论的（样本不足）明说没结论，不硬凑一个方向。 */
function pdBadgeCls(k) { return k === "bull" ? "bull" : k === "bear" ? "bear" : "neu"; }

function pdRenderRows(arr, maxPosts) {
  const m = Math.max(1, maxPosts || 1);
  return toArr(arr).map((x) => {
    const cls = pdBadgeCls(x.verdict_key);
    const wp = Math.round((x.pos || 0) / x.posts * 100);
    const wn = Math.round((x.neg || 0) / x.posts * 100);
    const quotes = (x.quotes || []).map((q) =>
      `<div class="pd-q"><span class="badge ${pdBadgeCls(q.label === "看多" ? "bull" : q.label === "看空" ? "bear" : "neu")}">${esc(q.label)}</span> ` +
      `<b>${esc(q.author)}</b> <span class="s">${esc(q.time || "")}</span>：${esc(q.text)}${esc((x.text_len || "") ? "" : "…")}</div>`).join("");
    // 2026-10-02 王总反馈「帖子里的股票代码点不动，我还得自己去找个股查询」。
    // 名称/代码都走全站通用的 codeLink（data-code-open → 个股详情抽屉，自动加载完所有结果，
    // 不用再点「开始查询 / 开始诊断」那一下）。
    const head = x.key
      ? `${codeLink(x.key, { text: x.name || x.key, cls: "pd-name" })}${codeLink(x.key, { cls: "pd-code" })}`
      : `<span class="pd-name">${esc(x.name || "—")}</span>`;
    return `<div class="pd-row">
      <div class="pd-head">
        ${head}
        ${x.board ? `<span class="pd-board">${esc(x.board)}</span>` : ""}
        <span class="badge ${cls} pd-vd">${esc(x.verdict)}</span>
        <span class="pd-cnt">${x.posts} 帖</span>
      </div>
      <div class="pd-bar"><span class="pd-bull" style="width:${wp}%"></span><span class="pd-bear" style="width:${wn}%"></span></div>
      <div class="pd-sum">${esc(x.summary || "")}</div>
      ${quotes ? `<div class="pd-quotes">${quotes}</div>` : ""}
    </div>`;
  }).join("");
}

function renderPostDigest(pd, posts) {
  if (!pd || pd.ok === false) {
    return `<div class="card" style="margin-bottom:14px"><div class="t">帖子情绪速览</div>
      <div class="hint">归纳没跑成：${esc((pd && pd.error) || "接口返回空")}。原帖列表在下面仍可查看。</div></div>`;
  }
  const moodCls = pd.verdict_mood_class === "bull" ? "bull" : pd.verdict_mood_class === "bear" ? "bear" : "neu";
  const boards = pd.boards || [];
  const stocks = pd.stocks || [];
  const maxPosts = Math.max(1, ...boards.map((b) => b.posts || 0), ...stocks.map((s) => s.posts || 0));
  const rows = pdRenderRows(stocks, maxPosts);
  const brows = pdRenderRows(boards, maxPosts);
  const rowsHtml = (s) => (s || "<div class='pd-none'>这一层暂时没有可归纳的样本</div>");
  const det = toArr(posts).map((p) =>
    `<tr data-kind="post" data-id="${escAttr(p.id)}"><td>${esc(p.author)}</td><td>${esc(fmtTimeText(p.post_time, "—"))}</td><td>${sentBadge(p.sentiment)}</td><td>${esc(p.text || "").slice(0, 60)}…</td></tr>`).join("");

  return `
  <div class="pd-box">
    <div class="pd-top">
      <div class="pd-t">帖子情绪速览<span class="s">（窗口 ${esc((pd.scope || {}).from || "—")} ~ ${esc((pd.scope || {}).to || "—")}，共 ${esc((pd.scope || {}).posts_total || 0)} 帖）</span></div>
      <div class="pd-verdict">${esc(pd.verdict_text || "")}</div>
      <div class="pd-mood ${moodCls}">情绪：${esc(pd.verdict || "—")}</div>
    </div>

    ${stocks.length ? `
    <div class="pd-sec">按个股（谁在被聊、聊出来是看涨还是看跌）</div>
    <div class="pd-list">${rowsHtml(rows)}</div>` : ""}

    ${boards.length ? `
    <div class="pd-sec">按行业 / 板块</div>
    <div class="pd-list">${rowsHtml(brows)}</div>` : ""}

    ${pd.general ? `<div class="pd-gen">${esc(pd.general.summary || "")}</div>` : ""}

    <details class="pd-detail">
      <summary>展开看原始帖子（${toArr(posts).length} 条，平时不用点）</summary>
      <table><thead><tr><th>作者</th><th>时间</th><th>情绪</th><th>内容</th></tr></thead><tbody>${det || "<tr><td colspan=4 class='empty'>暂无可查看的帖子，请点「抓取并更新」</td></tr>"}</tbody></table>
    </details>

    <div class="hint pd-basis">${esc(pd.basis || "")}</div>
  </div>`;
}

/* ---------- 打开受控登录窗口（2026-09-23） ----------
   为什么必须走这条：普通 <a target=_blank> 打开的是用户自己浏览器的标签页，
   登录后的 cookie 留在那个浏览器里，程序（后端 Python）**读不到**，
   于是出现「明明登录了、点登录检测却永远不更新」的死循环。
   这里改为调用后端 /api/login/open → 后端启动 tools/capture_login.py，
   那个脚本开一个带调试端口的受控 Edge，用户在该窗口登录，脚本自动抓 cookie 存库。 */
/* ---------- 重启 Edge 启用调试端口（2026-09-24） ----------
   解决 capture_login.py 用独立 profile 拿不到用户 cookie 的问题：
   1) 杀所有现有 msedge.exe
   2) 用同一 profile (--user-data-dir=%LOCALAPPDATA%\Microsoft\Edge\User Data) 重启 + --remote-debugging-port=9333
   3) 自动启动 capture_login.py 接管 9333 上的 Edge
   4) 用户在新开的 Edge 里扫码登录 → cookie 进用户 profile → capture_login 自动存库
   Edge 重启时会保留标签页与登录态（除非用户主动设置关闭）。*/
async function restartEdgeForLogin(site) {
  if (!confirm(
    `即将重启 Edge 并启用远程调试端口：\n\n` +
    `1. 关闭所有现有 Edge 窗口\n` +
    `2. 用您原本的 profile 重新启动 Edge（标签页与登录态保留）\n` +
    `3. 启动 capture_login 盯盯 cookie\n` +
    `4. 您需要在新的 Edge 里登录淘股吧\n\n` +
    `确认要继续吗？`
  )) return;
  const old = event.target;
  if (old) { old.disabled = true; old.textContent = "重启中…"; }
  try {
    const r = await api("/api/edge/restart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site, with_capture: true }),
    });
    if (!r || !r.ok) {
      setHint("Edge 重启失败：" + ((r && r.error) || "未知错误"));
      if (old) { old.disabled = false; old.textContent = "🔄 重启 Edge 启用调试"; }
      return;
    }
    setHint(r.message || "Edge 重启完成，请在新的 Edge 里登录淘股吧");
    // 开始轮询登录状态，成功后会自动收尾
    pollLoginUntilReady(site);
  } catch (e) {
    setHint("Edge 重启异常：" + apiErrText(e));
    if (old) { old.disabled = false; old.textContent = "🔄 重启 Edge 启用调试"; }
  }
}

let _loginPollUntilReadyTimer = null;
async function pollLoginUntilReady(site) {
  clearTimeout(_loginPollUntilReadyTimer);
  let ticks = 0;
  const tick = async () => {
    ticks++;
    try {
      // 调 login_check 看是否已登录
      const r = await api("/api/login_check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sites: [site] }),
      });
      const st = (r && r.results && r.results[0] && r.results[0].status) || "";
      if (st === "ok") {
        setHint(`${site} 已登录！开始抓取数据…`);
        // 自动发起一次抓取
        api("/api/fetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
          .then(() => loadAll()).catch(() => {});
        return;
      }
      if (ticks > 60) {
        setHint("已等待 3 分钟仍未检测到登录。你可以继续在 Edge 里登录，或检查 capture_login 日志（artifacts/capture_login_taoguba.log）");
        return;
      }
    } catch (e) { /* 忽略单次失败 */ }
    _loginPollUntilReadyTimer = setTimeout(tick, 3000);
  };
  tick();
}

/* ---------- 手动粘贴 Cookie（2026-09-24） ----------
   当 capture_login.py 开的窗口用户没用上（用户自己浏览器登录），
   或 taoguba SSO 会话过期，需要换新 cookie 时最快的路径：
   DevTools → Application → Cookies → 复制 JSON 数组 → 粘到下面的框里。 */
function openCookieImport(site) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-label="手动粘贴 Cookie">
      <h3 style="margin:0 0 8px">手动粘贴 Cookie（${esc(site)}）</h3>
      <div class="hint" style="margin-bottom:8px">
        <b>操作步骤：</b>
        <ol style="margin:6px 0 0 18px;padding:0;line-height:1.6">
          <li>在自己已登录的 Edge / Chrome 里按 <kbd>F12</kbd> 打开 DevTools</li>
          <li>切到 <b>Application</b> 标签 → 左侧 <b>Cookies</b> → 选 <code>https://www.tgb.cn</code></li>
          <li>用 <b>EditThisCookie</b> 或 <b>Cookie-Editor</b> 扩展导出 <b>JSON 数组</b>（每项含 name/value/domain）</li>
          <li>把 JSON 粘到下面文本框里 → 点 <b>导入并验证</b></li>
        </ol>
        也支持 <b>Netscape cookies.txt</b> 格式（curl / 浏览器扩展通用）。
      </div>
      <textarea id="cookieText" placeholder='[{"name":"tgbuser","value":"...","domain":"www.tgb.cn"}, ...]
或：
# Netscape HTTP Cookie File
.tgb.cn\tTRUE\t/\tFALSE\t9999999999\tname\tvalue'
                style="width:100%;height:160px;font-family:Menlo,Consolas,monospace;font-size:12px;padding:8px;border:1px solid #ccc;border-radius:4px"></textarea>
      <div id="cookieResult" style="margin-top:8px;min-height:24px"></div>
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" onclick="this.closest('.modal-overlay').remove()">取消</button>
        <button class="btn primary" id="cookieImportBtn">导入并验证</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const btn = overlay.querySelector("#cookieImportBtn");
  btn.onclick = async () => {
    const raw = overlay.querySelector("#cookieText").value.trim();
    if (!raw) { setHint("请粘贴 cookie 文本"); return; }
    btn.disabled = true; btn.textContent = "导入中…";
    try {
      const r = await api("/api/login/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site, cookies: raw }),
      });
      const out = overlay.querySelector("#cookieResult");
      if (r && r.ok) {
        const stat = r.login_status || "?";
        const ok = stat === "ok";
        out.innerHTML = `<div class="banner ${ok ? "" : "banner-warn"}" style="margin:0">
          已导入 ${r.imported} 条 cookie。登录状态：<b>${esc(stat)}</b><br>
          <span style="color:#555">${esc((r.login_detail || "").slice(0, 200))}</span>
        </div>`;
        if (ok) {
          setHint("Cookie 导入并校验成功，可以抓取数据了");
        }
      } else {
        out.innerHTML = `<div class="banner banner-warn" style="margin:0">
          导入失败：${esc((r && r.error) || "未知错误")}
        </div>`;
      }
    } catch (e) {
      overlay.querySelector("#cookieResult").innerHTML =
        `<div class="banner banner-warn" style="margin:0">请求失败：${esc(apiErrText(e))}</div>`;
    } finally {
      btn.disabled = false; btn.textContent = "导入并验证";
    }
  };
}

let _loginPollTimer = null;
function bindLoginOpen() {
  $$("#p-summary .login-open").forEach((b) => {
    b.onclick = async () => {
      const site = b.dataset.site || "taoguba";
      const old = b.textContent;
      b.disabled = true;
      b.textContent = "窗口打开中…";
      try {
        const r = await api("/api/login/open", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ site }),
        });
        if (!r || !r.ok) {
          setHint("打开登录窗口失败：" + (r && r.error ? r.error : "未知错误"));
          b.disabled = false; b.textContent = old;
          return;
        }
        setHint("已打开受控登录窗口：请在该窗口里完成登录（扫码要记得点「确认」），" +
                "登录成功后会自动保存。窗口在保存前请不要关闭。");
        b.textContent = "登录中…（等待保存）";
        startLoginPoll(site, b, old);
      } catch (e) {
        setHint("打开登录窗口失败：" + apiErrText(e));
        b.disabled = false; b.textContent = old;
      }
    };
  });
}

/* 打开窗口后轮询登录状态：一旦后端检测到已登录（cookie 已存且校验通过），
   自动刷新面板并停表。用户不用再手动点「登录检测」。 */
function startLoginPoll(site, btn, oldText) {
  if (_loginPollTimer) { clearInterval(_loginPollTimer); _loginPollTimer = null; }
  let waited = 0;
  _loginPollTimer = setInterval(async () => {
    waited += 5;
    try {
      const s = await api("/api/summary");
      const row = toArr(s && s.login_status).find((x) => x.site === site);
      if (row && row.status === "ok") {
        clearInterval(_loginPollTimer); _loginPollTimer = null;
        setHint(`「${row.display_name || site}」登录成功，凭据已保存并复用。`);
        if (btn) { btn.disabled = false; btn.textContent = oldText; }
        loadAll();
        return;
      }
      if (waited >= 900) {   // 15 分钟未成功则停表，避免无限轮询
        clearInterval(_loginPollTimer); _loginPollTimer = null;
        setHint("等待登录超时（15 分钟）。请在受控窗口里完成登录后再点「登录检测」。");
        if (btn) { btn.disabled = false; btn.textContent = oldText; }
      }
    } catch { /* 轮询失败不打断用户，下一轮再试 */ }
  }, 5000);
}

/* ---------- 股票卡片：把「6 位代码」翻译成看得懂的信息 ----------
   设计原则（回应"序号看不懂"的反馈）：
   1) 第一行 = 股票简称（如 贵州茅台）＋ 上市板块；这是最需要被一眼看到的。
   2) 第二行 = 代码标签（明确标注"代码"）＋ 公司全称（贵州茅台酒股份有限公司）。
   3) 第三行 = 多空/提及等数字，全部带中文标签，不留裸数字。 */
function stockCard(x, mode) {
  // 兼容两种数据源：情绪地图给 name，股票列表给 symbol_name
  const nm = x.name || x.symbol_name || "";
  const missing = !!((x.name_missing || !nm));
  let nameHtml = missing
    ? `<span class="stk-name missing">名称待补全</span>`
    : `<span class="stk-name">${esc(nm)}</span>`;
  nameHtml += dragonTag(x.symbol || x.code || "", { full: true });   // 2026-10-21 龙头徽章
  const corp = x.company
    ? `<span class="stk-corp" title="${escAttr(x.company)}">${esc(x.company)}</span>`
    : `<span class="stk-corp unknown">公司全称待补全</span>`;
  let meta;
  if (mode === "sentiment") {
    const bull = x.bull || 0, bear = x.bear || 0, neu = x.neutral || 0, total = x.mentions || (bull + bear + neu);
    meta = (bull || bear)
      ? `<span class="badge bull">多 ${bull}</span><span class="badge bear">空 ${bear}</span>` +
        (neu ? `<span class="badge neu">中性 ${neu}</span>` : "")
      : `<span class="badge neu">中性 ${neu}</span>`;
    meta += `<span class="stk-total">共 ${total} 条提及</span>`;
  } else {
    // 2026-09-26 按王总要求：被提及次数做成高亮主标（数字加粗放大），一眼可辨。
    // 2026-10-12 体验审计 P0-C：人气（被提及次数）与「人群态度」分开显示 ——
    // 被讨论得多 ≠ 被看好，两个概念不能揉成一个数字，否则人气榜会被当成推荐榜。
    const pt = x.posture;
    const pTip = `近 72 小时帖子里对该股的明确表态：看多 ${x.posture_pos || 0} / 看空 ${x.posture_neg || 0}（共 ${x.posture_posts || 0} 帖点名）`;
    const postureBadge = pt === "bull" ? `<span class="badge bull" title="${escAttr(pTip)}">看好</span>`
      : pt === "bear" ? `<span class="badge bear" title="${escAttr(pTip)}">看空</span>`
      : pt === "diverge" ? `<span class="badge neu" title="${escAttr(pTip)}">分歧</span>`
      : "";
    meta = `<span class="stk-mention" title="该股在抓取到的帖子中被提及的总次数（人气，不代表看好）">被提及 <b>${x.mentions ?? 0}</b> 次</span>` +
      postureBadge +
      (x.last_time ? `<span class="chip-s">最近 ${esc(x.last_time)}</span>` : "") +
      // 最后更新：该股讨论帖来源站点的最近一次成功刷新（Request G）
      (toArr(x.sources).length ? refChip(x.sources) : "");
  }
  return `<div class="box stk-card" data-sym="${escAttr(x.symbol)}">
    <div class="stk-top">${nameHtml}<span class="stk-board">${esc(x.market || "")}</span></div>
    <div class="stk-sub"><span class="stk-code" title="该数字为股票代码（中国内地交易所 6 位代码）">${esc(x.symbol)}</span>${corp}</div>
    <div class="stk-meta">${meta}</div>
  </div>`;
}

/* 名称解析提示（原「图例」已按王总 2026-09-26 反馈精简）：
   - 卡片含义（第一行简称 / 第二行代码+全称）一看即知，不再写图例解释，避免纯占版面；
   - 仅当存在「未识别代码」时才提示并可一键补全；全部识别时整块隐藏，减少版面空白。
   情绪地图（sentiment）保留多空口径说明——那是颜色语义，非一眼可知。 */
function stockLegend(d, mode) {
  const lg = (d && d.legend) || {};
  const covered = d ? (d.name_covered ?? 0) : 0, total = d ? (d.name_total ?? 0) : 0;
  const miss = Math.max(0, total - covered);
  const syncBtn = `<button class="btn sm" data-sync-names="1" title="对未识别的代码联网查询（东方财富）并写入本地缓存">补全股票名称</button>`;
  if (mode === "mentions") {
    if (miss <= 0) return "";
    return `<div class="legend lg-compact"><span>有 <b>${miss}</b> 个代码未识别公司名称</span>${syncBtn}</div>`;
  }
  return `<div class="legend">
    <div class="lg-row"><span class="lg-key"><span class="badge bull">多 3</span></span><span>${esc(lg.bull || "该股被 N 条看多情绪的帖子提及")}</span></div>
    <div class="lg-row"><span class="lg-key"><span class="badge bear">空 1</span></span><span>${esc(lg.bear || "该股被 N 条看空情绪的帖子提及")}</span></div>
    <div class="lg-row"><span class="lg-key"><span class="badge neu">中性 2</span></span><span>${esc(lg.neutral || "情绪词库未判出明确多空的提及条数")}（看多=红、看空=绿，与 A 股涨跌配色一致）</span></div>
    ${miss > 0 ? `<div class="lg-act"><span>有 <b>${miss}</b> 个代码未识别公司名称</span>${syncBtn}</div>` : ""}
  </div>`;
}
function bindLegendSync() {
  const b = $("#p-sentiment [data-sync-names]") || $("#p-stocks [data-sync-names]");
  if (!b) return;
  b.onclick = () => {
    b.disabled = true; b.textContent = "正在补全…";
    api("/api/sync_names", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then((r) => { setHint(`股票名称补全完成：解析 ${r.resolved} / 请求 ${r.requested}，写入缓存 ${r.persisted} 条。`); loadAll(); })
      .catch((e) => setHint("补全失败：" + apiErrText(e)))
      .finally(() => { b.disabled = false; b.textContent = "补全股票名称"; });
  };
}
// 2026-09-27：「增强功能 ✦」6 面板代码列统一接线助手
// 接 [data-feat-open="<6位A股代码>"]；跨市场/非法代码在各自 render 里被故意过滤掉，
// 这里只对纯 A 股/ETF/港股数字代码生效，保证点了不空开。
function bindFeatStockClicks(scope) {
  $$(`${scope} [data-feat-open]`).forEach((el) => {
    el.onclick = (ev) => { ev.preventDefault(); openStock(el.dataset.featOpen, el); };
  });
}

/* 2026-09-29 全局兜底（事件委托，王总反馈"板块资金流代表股点了没反应"）：
 * 各面板的直接绑定（onclick 属性）依赖"每次重渲染后重新走一遍绑定代码"，
 * 任何一次渲染路径漏绑 / 窗口跑着旧缓存 app.js，症状都是「点了完全没反应」。
 * 这里在 document 级委托 [data-wt-open] / [data-feat-open] 两类入口：
 *   · 直接绑定的 handler 先于冒泡执行并 preventDefault → 这里靠 defaultPrevented 跳过，绝不双开；
 *   · 只认 6 位纯数字代码（与各渲染处的过滤口径一致），海外/异常代码不接；
 *   · 于是无论表格被重渲染多少次、绑定循环跑没跑，点代表股/代码列必定开抽屉。 */
document.addEventListener("click", (ev) => {
  if (ev.defaultPrevented) return;
  const t = ev.target;
  // 2026-09-29 A1：板块名 → 板块详情下钻（解决"点到板块下不去"的痛点）
  const bel = t && t.closest ? t.closest("[data-bf-open]") : null;
  if (bel) {
    ev.preventDefault();
    openBoardDetail(bel.dataset.bfOpen, bel.dataset.bfKind || "industry", bel.dataset.bfName || (bel.textContent || "").trim());
    return;
  }
  const el = t && t.closest ? t.closest("[data-wt-open],[data-feat-open]") : null;
  if (!el) return;
  const sym = el.dataset.wtOpen || el.dataset.featOpen;
  if (!/^\d{6}$/.test(String(sym || ""))) return;
  ev.preventDefault();
  openStock(sym, el);
}, false);

function bindStockCards(scope) {
  $$(`${scope} .stk-card[data-sym]`).forEach((b) => { b.onclick = () => openStock(b.dataset.sym, b); });
}

function renderStocks(list) {
  const items = toArr(list);
  if (!items.length) { $("#p-stocks").innerHTML = `<div class="empty">暂无讨论股票数据</div>`; return; }
  const missing = items.filter((x) => x && (x.name_missing || !(x.name || x.symbol_name))).length;
  // 2026-09-26 按王总反馈：删去「卡片第一行是… 第二行是…」说明条与图例（一看即知、纯占版面）；
  // 卡片密度优化走 .grid-stk（更小间距、一屏更多），被提及次数在卡片内高亮。
  const anyPosture = items.some((x) => x && x.posture);
  $("#p-stocks").innerHTML =
    `<div class="hint hint-slim stk-posture-hint">「被提及 N 次」= <b>人气</b>（被讨论得多）；` +
      `<b>看好 / 看空 / 分歧</b> = 近 72 小时帖子里明确表态的方向（与「汇总 · 情绪速览」同一把尺子）。` +
      `<b>人气高 ≠ 被看好</b>，两者分开看。${anyPosture ? "" : "（当前窗口内暂无明确表态的票，故只显示人气）"}</div>` +
    stockLegend({ name_covered: items.length - missing, name_total: items.length }, "mentions") +
    `<div class="grid grid-stk">` + items.map((x) => stockCard(x, "mentions")).join("") + `</div>`;
  bindStockCards("#p-stocks");
  bindLegendSync();
  applyFreshness();
}

/* 持仓天数口径说明（2026-09-23）：person / player 两个视图的「建仓起点」不同，
   同一标的的天数可能不同，必须在页面上显式声明基准，否则同一个数字会被误读。
   集中在此一处维护，避免同一套措辞被复制到多个抽屉里各自漂移。
   basis: "person_first_build" | "player_first_build" | "symbol_first_build" */
function holdDaysNote(basis) {
  const note = (t) => `<div class="s" style="margin-top:12px;color:var(--muted)">口径说明：${t}</div>`;
  if (basis === "symbol_first_build") {
    return note(`<b>持仓起始日期</b>=该标的<b>全市场</b>最早的一笔建仓，本视图所有记录共用同一个持有天数（算到今日），个股下方的「持仓选手」表也按此基准。`);
  }
  if (basis === "person_first_build") {
    return note(`<b>持仓起始日期</b>=<b>本人</b>自身最早的一笔建仓（帖子明确披露时以披露为准，否则取本人历次买入中最早的一笔）——与个股页的「全市场最早建仓」不是同一个起点，故同一标的两侧天数不同是<b>口径差异</b>而非数据错误。<b>持有天数</b>自本人首次建仓起算：<b>open</b>=仍在持有（算到今日），<b>closed</b>=已卖出/已平仓（算到卖出日，即该笔的实际持有区间长度）。`);
  }
  return note(`<b>持仓起始日期</b>=该标的首次建仓时间（帖子明确披露时以披露为准，否则取历次买入中最早的一笔；同一标的多次加仓不会把起始日推后）。卖出记录的「卖出比例」为本次卖出占原仓位比例，累计达 100% 视为清仓，对应建仓行会标注「已清仓」。了结盈亏=卖出价−首次建仓价；当前股价来自行情源，未抓取时显示「未抓取」。受限项已单独标注，可按提示充值解锁。`);
}

// 2026-09-26：新增第 3 个可选参数 extraHtml —— 「共同持仓共识 / 近5日共同买入」两板块
// 把各自的整条信息作为「④」区块追加到本抽屉末尾（本函数仍是唯一的个股详情入口）。
// 不传时（选手与渠道等旧调用点）行为与之前完全一致，保证零回归。
/* 2026-10-15 进哥反馈「点某只股票弹出来特别慢」——根因不是这只票，
   而是抽屉原先要**等 /api/stock 返回**才 openDrawer；该接口冷态或后台繁忙时可能卡十几秒，
   于是主观感受就是「点了半天不出东西」。现改两段式：
     ① 立刻用骨架开抽屉（点下去 0 延迟）；
     ② 四段（重要信息 / K线 / 换手率 / 持股结构）各自独立异步填充；
     ③ /api/stock 到货后只回填「标题 + 代码简称公司 + 持仓选手表」。
   这样无论后端多慢，抽屉都是点下去就出。 */
function openStock(symbol, anchorEl, extraHtml) {
  // 2026-10-22 全量整改 P2-28：记录打开的个股代码供抽屉跳转 chip 使用
  DRAWER_SYMBOL = (symbol || "").toString().trim();
  // 2026-10-23：为本抽屉建立独立的请求取消器；若上一只个股的请求还在飞，先取消它，再建新的
  if (_stockAbort) { try { _stockAbort.abort(); } catch (e) {} }
  _stockAbort = new AbortController();
  // 2026-10-02：刚点开过的票记成「下一个要诊断的票」，
  // 这样从汇总页/任何列表点完代码，再切到「个股诊断」页时是**上一只那张票自动跑出来**，
  // 不用用户重新输一遍代码再点「开始诊断」。
  if (symbol && typeof DIAG !== "undefined") DIAG.symbol = symbol;
  let symName = "";
  const html = `
        <div class="kv"><div class="k">股票代码</div><div><span class="stk-code">${esc(symbol)}</span> <span class="stk-board" id="stkBoard"></span><span id="stkDragon" class="stk-dragon"></span></div></div>
        <div class="kv"><div class="k">股票简称</div><div><b id="stkName">加载中…</b><span class="s" id="stkCompany"></span></div></div>
        <div id="stkProfile" class="stk-info stk-profile-card" data-sym="${escAttr(symbol)}"><div class="s">公司简介加载中…</div></div>
        <div class="detail-sub" style="margin-top:14px">① 重要信息 —— 现价 · 估值 · 盈利 · 成长（直接铺开，不需要再点开）</div>
        <div id="stkQuote" class="stk-info" data-sym="${escAttr(symbol)}"><div class="s">重要信息加载中…</div></div>
        <div class="detail-sub">换手率实战 —— 四场景判读 · 入手值得率（0~100，技术面规则打分，不构成投资建议）</div>
        <div id="stkTurnover" class="stk-info stk-turnover" data-sym="${escAttr(symbol)}"><div class="s">换手率实战评分加载中…（首次需抓日K，约2~5秒）</div></div>
        <div class="detail-sub">持股结构 —— 机构 / 产业资本 / 自然人 / 散户（季报口径，详见说明）</div>
        <div id="stkHolders" class="stk-info stk-holders" data-sym="${escAttr(symbol)}"><div class="s">持股结构加载中…</div></div>
        <div class="detail-sub">② K 线走势 —— <b>按住拖动</b>平移（<b>向右拖 = 往回看更早</b>，可一直拖到上市首日）· <b>滚轮</b>缩放（<b>一直缩小到底 = 看「上市至今」全历史</b>，也可直接点「全历史」）· <b>悬停</b>看当日明细；也可点下方「◀ 更早 / 更晚 ▶」逐屏翻</div>
        <div id="klineWrap" data-sym="${escAttr(symbol)}" data-name="">
          <div class="kline-snap" id="klineSnap"><span class="s">行情加载中…</span></div>
          <div class="kline-canvas-wrap"><canvas id="klineCanvas"></canvas><div class="kline-tip" id="klineTip" hidden></div></div>
          <div class="kline-bar">
            <div class="kline-periods">
              <button class="kbtn active" data-p="day">日K</button>
              <button class="kbtn" data-p="week">周K</button>
              <button class="kbtn" data-p="month">月K</button>
            </div>
            <div class="kline-tools">
              <button class="btn sm" id="klineOlder" title="往回看更早的 K 线">◀ 更早</button>
              <button class="btn sm" id="klineNewer" title="往近看更新的 K 线">更晚 ▶</button>
              <span class="kline-range" id="klineRange">—</span>
              <button class="btn sm" id="klineFull" title="一次显示从上市首日到今天的全部 K 线（等同一直缩小到底）">全历史</button>
              <button class="btn sm" id="klineLatest">回到最新</button>
              <button class="btn sm" id="klineRefresh">刷新</button>
            </div>
          </div>
          <div class="kline-recent" id="klineRecent"><span class="s">—</span></div>
        </div>
        <div class="detail-sub">③ 持仓选手（含<b>持仓起始日期</b> / 建仓价 / 当前盈亏；已卖出者不在本表）</div>
        <div id="stkPositions"><span class="s">持仓记录加载中…</span></div>
        ${holdDaysNote("symbol_first_build")}
        ${extraHtml || ""}`;
  openDrawer(`股票 · ${symbol}`, html, anchorEl);
  // ② 五段各自独立加载 —— 都不依赖 /api/stock，所以任何一段慢都不会拖住抽屉
  initStockQuote(symbol);
  initStockKline(symbol);
  initStockTurnover(symbol);
  initStockF10(symbol);
  // 抽屉头两个 CTA 立刻可用（名称到达后自动带上）
  const aw = $("#btnAddWatch");
  if (aw) {
    aw.hidden = false;
    aw.onclick = (ev) => {
      ev.stopPropagation();
      openWatchAddModal(null, { symbol, name: symName });
    };
  }
  // 2026-09-29：估值诊股快捷 CTA —— 当前票计入「最近诊过」池（上限 20），
  // 关抽屉切到「我的票 → 个股诊断」并自动诊断（selectNav 的 diag 分支会按 DIAG.symbol 触发加载）
  const bv = $("#btnValuation");
  if (bv) {
    bv.hidden = false;
    bv.onclick = (ev) => {
      ev.stopPropagation();
      valPoolPush(symbol, symName);
      DIAG.symbol = symbol;
      closeDrawer();
      selectNav("watch", "diag", true);
    };
  }
  // ③ 唯一依赖 /api/stock 的部分：标题 / 简称 / 公司 / 持仓选手表（到了再填，不挡开抽屉）
  api(`/api/stock?symbol=${encodeURIComponent(symbol)}`, { signal: _stockAbort ? _stockAbort.signal : undefined }).then((d) => {
    // 防串台：期间用户若又点了别的票（klineWrap 已换成新票），这轮就不要回填了
    const wrapEl = $("#klineWrap");
    if (!wrapEl || wrapEl.dataset.sym !== String(symbol)) return;
    if (d && d.error) {
      const h0 = $("#stkPositions");
      if (h0) h0.innerHTML = `<span class="s">${esc(d.error)}</span>`;
      return;
    }
    symName = (d && d.symbol_name) || "";
    const nm = $("#stkName"); if (nm) nm.textContent = symName || "名称待补全";
    const bd = $("#stkBoard"); if (bd) bd.textContent = (d && d.market) || "";
    // 2026-10-21 龙头徽章：进哥要求"点开每一只股票，符合的就明显标出来"
    loadDragonMap().then(() => {
      const dg = $("#stkDragon");
      if (dg) dg.innerHTML = dragonTag(symbol, { full: true });
    });
    const co = $("#stkCompany"); if (co) co.textContent = (d && d.company) ? " " + d.company : "";
    const ttl = $("#drawerTitle");
    if (ttl && isDrawerOpen()) {
      ttl.textContent = `股票 · ${symName || "名称待补全"}（${symbol}）${(d && d.company) ? " · " + d.company : ""}`;
    }
    const host = $("#stkPositions");
    if (!host) return;
    // 2026-09-25 按王总要求重排抽屉：只留「重要信息 → K 线 → 持仓选手」，一屏看完。
    // 已移除：共同持仓/讨论提及/实盘交易 计数行、多空观点明细、相关实盘交易、讨论帖原文。
    const stillHolding = toArr(d.positions).filter((p) => (p.action || "").toUpperCase() !== "SELL");
    const posRows = stillHolding.map((p) => {
      const bp = p.buy_price != null ? p.buy_price : null;
      const cp = p.current_price != null ? p.current_price : null;
      const since = (p.hold_since || "").trim();
      return `<tr>
          <td><b>${esc(p.player || "")}</b></td>
          <td>${channelBadge(p.channel)}</td>
          <td class="num">${p.ratio != null ? p.ratio + "%" : "未披露"}</td>
          <td>${since ? `<b>${esc(since.slice(0, 10))}</b>${p.hold_days != null ? `<span class="hold-days">已持有 ${p.hold_days} 天</span>` : ""}` : "未披露"}</td>
          <td class="num">${bp != null ? Number(bp).toFixed(2) : "—"}</td>
          <td class="num">${cp != null ? Number(cp).toFixed(2) + plTag(bp, cp) : "—"}</td>
        </tr>`;
    }).join("");
    host.innerHTML = stillHolding.length
      ? `<table><thead><tr><th>选手</th><th>渠道</th><th>持仓比例</th><th>持仓起始日期</th><th>买入价</th><th>当前股价</th></tr></thead><tbody>${posRows}</tbody></table>`
      : "<span class='s'>暂无持仓记录</span>";
  }).catch((e) => {
    if (e && e.name === "AbortError") return;   // 抽屉关闭等主动取消：不渲染
    stockZoneFail($("#stkPositions"), "持仓记录", e, () => openStock(symbol, anchorEl));
  });
}

/* 2026-09-30：个股详情抽屉「换手率实战」块 —— 四场景判读 + 入手值得率（/api/stock_turnover）。
   与智能选股·连续阴跌共用同一评分引擎（screener._turnover_eval，官方换手率标定口径）。 */
function initStockTurnover(symbol) {
  const host = $("#stkTurnover");
  if (!host) return;
  api(`/api/stock_turnover?symbol=${encodeURIComponent(symbol)}`, { signal: _stockAbort ? _stockAbort.signal : undefined })
    .then((d) => {
      if (!d || d.ok === false) {
        host.innerHTML = `<div class="s">换手率实战评分暂不可用：${esc((d && d.reason) || "未知原因")}</div>`;
        return;
      }
      const tierCls = { 5: "sc-t5", 4: "sc-t4", 3: "sc-t3", 2: "sc-t2", 1: "sc-t1" };
      const dimTxt = d.dim ? `位置 ${d.dim.pos} ＋ 场景 ${d.dim.scene} ＋ 持续 ${d.dim.persist} ＋ 量价 ${d.dim.vp} ＋ 企稳 ${d.dim.stab}` : "";
      host.innerHTML = `
        <div class="st-row st-main">
          <span class="sc-score ${tierCls[d.tier] || "sc-t3"}" title="${escAttr(dimTxt)}"><b>${d.score != null ? d.score : "—"}</b>${esc(d.tier_label || "—")}</span>
          <span class="st-scenario">场景判定：<b>${esc(d.scenario || "—")}</b></span>
          <span class="st-hint">${esc(d.hint || "")}</span>
        </div>
        <div class="st-row st-meta">
          <span>换手率 <b>${d.to_today != null ? d.to_today + "%" : "—"}</b><i>（5日均 ${d.to_avg5 != null ? d.to_avg5 + "%" : "—"}）</i></span>
          <span>近 ${esc(d.days)} 日位置分位 <b>${d.pos_pct != null ? d.pos_pct + "%" : "—"}</b>${d.pos_pct != null ? (d.pos_pct <= 35 ? "（低位）" : d.pos_pct >= 70 ? "（高位）" : "（中位）") : ""}</span>
          <span>流通市值 <b>${d.fmv_yi ? d.fmv_yi + " 亿" : "—"}</b></span>
          ${d.partial ? `<span class="st-warn">次新股：仅 ${d.bars_used} 根K线，评分口径减弱</span>` : ""}
        </div>
        <div class="st-row st-dim">
          <span class="s">五维：${esc(dimTxt) || "—"}</span>
        </div>`;
    })
    .catch((e) => {
      if (e && e.name === "AbortError") return;   // 抽屉关闭等主动取消：不渲染
      stockZoneFail(host, "换手率实战评分", e, () => initStockTurnover(symbol));
    });
}

/* 2026-09-29 A1：板块详情页下钻 —— 解决王总"点到板块、再想往下点了解不了"的痛点。
   入口：板块资金流里的板块名（.bf-bn[data-bf-open]）点击进入。
   抽屉内：板块资金/涨跌幅 + 成分股全列表（可排序，点击下钻个股诊断）+ 板块内个股资金对比 TOP
   + 估值分位分布 + 板块内龙虎榜。数据全部来自 /api/board（成分股=新浪、资金/估值=板块内前 N 只聚合）。 */
const BD = { data: null, sort: "default", code: "", kind: "", name: "" };
function openBoardDetail(code, kind, name) {
  BD.code = code; BD.kind = kind || "industry"; BD.name = name || code; BD.sort = "default"; BD.data = null;
  openDrawer(`板块 · ${name || code}（${BD.kind === "concept" ? "概念" : "行业"}）`,
    `<div class="empty">板块详情加载中…（成分股 + 板块内资金/估值聚合，约 10 秒）</div>`, null);
  // 板块详情内容多（成分股最多 100 行），自动最大化抽屉看得更全
  const d = $("#drawer");
  if (d && !d.classList.contains("maxed") && typeof toggleMax === "function") toggleMax();
  api(`/api/board?code=${encodeURIComponent(code)}&kind=${encodeURIComponent(BD.kind)}&name=${encodeURIComponent(BD.name || "")}`)
    .then((dd) => {
      if (dd.error) { const b = $("#drawerBody"); if (b) b.innerHTML = `<p>${esc(dd.error)}</p>`; return; }
      BD.data = dd;
      renderBoardBody();
    })
    .catch((e) => { const b = $("#drawerBody"); if (b) b.innerHTML = `<p>板块详情加载失败：${esc(apiErrText(e) || e)}</p>`; });
}
function renderBoardBody() {
  const d = BD.data; if (!d) return;
  const b = d.board || {};
  const members = (d.members || []).slice();
  if (BD.sort === "pct") members.sort((a, c) => (Number(c.pct) || 0) - (Number(a.pct) || 0));
  else if (BD.sort === "main_net") members.sort((a, c) => (Number(c.main_net) == null ? -1e18 : Number(c.main_net)) - (Number(a.main_net) == null ? -1e18 : Number(a.main_net)));
  else if (BD.sort === "val") members.sort((a, c) => (Number(c.val_score) == null ? -1e18 : Number(c.val_score)) - (Number(a.val_score) == null ? -1e18 : Number(a.val_score)));
  const mrows = members.map((m) => {
    const p = m.pct == null ? null : Number(m.pct);
    const pcls = p == null ? "" : (p > 0 ? "up" : (p < 0 ? "dn" : ""));
    const mn = m.main_net == null ? "—" : (Number(m.main_net) >= 0 ? "+" : "") + (Number(m.main_net) / 1e8).toFixed(2) + " 亿";
    const mncls = m.main_net == null ? "" : (Number(m.main_net) >= 0 ? "bull" : "bear");
    const vs = m.val_score == null ? "—" : Number(m.val_score).toFixed(0);
    return `<tr>
      <td><button type="button" class="wt-sym" data-wt-open="${escAttr(m.code)}">${esc(m.code)}</button></td>
      <td><button type="button" class="wt-sym" data-wt-open="${escAttr(m.code)}">${esc(m.name || "")}</button></td>
      <td class="num">${m.price == null ? "—" : Number(m.price).toFixed(2)}</td>
      <td class="num ${pcls}">${p == null ? "—" : fmtPct(p)}</td>
      <td class="num ${mncls}">${mn}</td>
      <td class="num">${vs}</td>
    </tr>`;
  }).join("");
  const fc = (d.flow_compare || []).map((x, i) => `<tr>
      <td class="wt-c1">${i + 1}</td>
      <td><button type="button" class="wt-sym" data-wt-open="${escAttr(x.code)}">${esc(x.name || x.code)}</button></td>
      <td class="num ${Number(x.main_net) >= 0 ? "bull" : "bear"}">${(Number(x.main_net) / 1e8).toFixed(2)} 亿</td>
    </tr>`).join("");
  const v = d.valuation || {};
  const vtxt = v.count
    ? `样本 <b>${v.count}</b> 只 · 平均估值分 <b>${v.avg}</b> · 低估 <b class="bull">${v.n_overweight}</b> / 中性 ${v.n_neutral} / 偏高 <b class="bear">${v.n_low}</b>`
    : "估值分位未取到（接口限流，可稍后重试）";
  const lhb = (d.lhb && d.lhb.rows) || [];
  const lhbtxt = lhb.length ? lhb.map((x) => `<tr>
      <td><button type="button" class="wt-sym" data-wt-open="${escAttr(x.code)}">${esc(x.name || x.code)}</button></td>
      <td class="num">${x.pct == null ? "—" : fmtPct(x.pct)}</td>
      <td class="num ${Number(x.net_yi) >= 0 ? "bull" : "bear"}">${Number(x.net_yi).toFixed(2)} 亿</td>
      <td>${esc(x.reason || "")}</td>
    </tr>`).join("")
    : `<tr><td colspan="4" class="muted">当日（${esc((d.lhb || {}).date || "—")}）该板块无龙虎榜个股</td></tr>`;
  const bm = b.main_net == null ? "—" : (Number(b.main_net) / 1e8).toFixed(2) + " 亿";
  const bcls = b.main_net == null ? "" : (Number(b.main_net) >= 0 ? "bull" : "bear");
  const sortBtn = (k, lbl) => `<a class="wt-tab${BD.sort === k ? " active" : ""}" data-bd-sort="${k}">${lbl}</a>`;
  const body = $("#drawerBody");
  if (!body) return;
  body.innerHTML = `
    <div class="kv"><div class="k">板块资金净流入</div><div class="${bcls}"><b>${bm}</b></div></div>
    <div class="kv"><div class="k">板块涨跌幅</div><div>${b.pct == null ? "—" : wtPct(b.pct)}</div></div>
    <div class="detail-sub">① 成分股（共 ${d.member_total} 只，点击代码/名称下钻个股详情）· 排序：
      ${sortBtn("default", "默认")} ${sortBtn("pct", "涨幅↓")} ${sortBtn("main_net", "主力净流入↓")} ${sortBtn("val", "估值分位↓")}</div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>主力净流入</th><th>估值分位</th>
    </tr></thead><tbody>${mrows}</tbody></table></div>
    <div class="detail-sub">② 板块内个股资金对比 TOP（已取到 ${(d.flow_compare || []).length} 只）</div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr><th>#</th><th>名称</th><th>主力净流入</th></tr></thead><tbody>${fc || '<tr><td colspan="3" class="muted">未取到</td></tr>'}</tbody></table></div>
    <div class="detail-sub">③ 估值分位分布</div>
    <div class="s">${vtxt}</div>
    <div class="detail-sub">④ 板块内龙虎榜（${esc((d.lhb || {}).date || "—")}）</div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr><th>名称</th><th>涨跌幅</th><th>净买额</th><th>原因</th></tr></thead><tbody>${lhbtxt}</tbody></table></div>
    <div class="s" style="margin-top:10px;color:var(--muted)">${esc(d.note || "")}</div>
  `;
  $$("#drawerBody [data-bd-sort]").forEach((el) => {
    el.onclick = () => { BD.sort = el.dataset.bdSort; renderBoardBody(); };
  });
}

/* ---------- 个股「重要信息」快照（2026-09-25 新增） ----------
   点开一只股票 → 抽屉顶部展示：实时行情 + 估值(PE/PB/市值) + 财务(ROE/毛利率/营收/净利)。
   数据由 /api/quote 服务端代理（东财 push2 + F10；东财不可用时自动兜底腾讯行情）。
   涨跌配色遵循 A 股习惯：涨=红(--bull)、跌=绿(--bear)。 */
function siNum(v, d = 2) {
  return (v === null || v === undefined || v === "" || isNaN(v)) ? "—" : Number(v).toFixed(d);
}
function siPct(v, d = 2) {
  if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(d) + "%";
}
// 水平型百分比（换手率/振幅/ROE/毛利率/净利率）：不加正负号
function siPctPlain(v, d = 2) {
  if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
  return Number(v).toFixed(d) + "%";
}
function siMoney(v) {            // 元 → 亿 / 万亿
  if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
  const n = Number(v), a = Math.abs(n);
  if (a >= 1e12) return (n / 1e12).toFixed(2) + " 万亿";
  if (a >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (a >= 1e4) return (n / 1e4).toFixed(2) + " 万";
  return n.toFixed(2);
}
function siVol(v) {              // 手 → 万手 / 亿手
  if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
  const n = Number(v), a = Math.abs(n);
  if (a >= 1e8) return (n / 1e8).toFixed(2) + " 亿手";
  if (a >= 1e4) return (n / 1e4).toFixed(2) + " 万手";
  return n.toFixed(0) + " 手";
}
/* 2026-09-30 晚：个股 F10 块 —— 公司简介 + 持股结构（机构/散户）+ 股东户数。
   同时填充抽屉与诊断页里所有 .stk-profile-card / .stk-holders 容器（class 选择器，
   避免同页重复 id 冲突）。数据来自 /api/stock_f10（东方财富 F10，90 天缓存）。 */
function initStockF10(symbol) {
  const profHosts = $$(".stk-profile-card");
  const holdHosts = $$(".stk-holders");
  if (!profHosts.length && !holdHosts.length) return;
  profHosts.forEach((h) => { if (!h.dataset.rendered) h.innerHTML = `<div class="s">公司简介加载中…</div>`; });
  holdHosts.forEach((h) => { if (!h.dataset.rendered) h.innerHTML = `<div class="s">持股结构加载中…</div>`; });
  api(`/api/stock_f10?symbol=${encodeURIComponent(symbol)}`, { signal: _stockAbort ? _stockAbort.signal : undefined })
    .then((d) => {
      if (!d || d.ok === false) {
        const msg = `<div class="s">F10 数据暂不可用：${esc((d && d.reason) || "未知原因")}</div>`;
        profHosts.forEach((h) => { h.innerHTML = msg; h.dataset.rendered = "1"; });
        holdHosts.forEach((h) => { h.innerHTML = msg; h.dataset.rendered = "1"; });
        return;
      }
      const prof = renderF10Profile(d.profile);
      const hold = renderF10Holders(d);
      profHosts.forEach((h) => { h.innerHTML = prof; h.dataset.rendered = "1"; });
      holdHosts.forEach((h) => { h.innerHTML = hold; h.dataset.rendered = "1"; });
    })
    .catch((e) => {
      if (e && e.name === "AbortError") return;   // 抽屉关闭等主动取消：不渲染
      stockZoneFail(profHosts, "公司简介", e, () => initStockF10(symbol));
      stockZoneFail(holdHosts, "持股结构", e, () => initStockF10(symbol));
    });
}

function renderF10Profile(p) {
  if (!p || !p.name) return `<div class="s">暂无公司简介（新股或数据缺失）</div>`;
  const meta = [p.market, p.industry].filter(Boolean).join(" · ");
  const rows = [];
  if (p.org_name) rows.push(`<div class="pf-row"><span class="pf-k">全称</span><b>${esc(p.org_name)}</b></div>`);
  if (p.main_business) rows.push(`<div class="pf-biz"><span class="pf-k">主营业务</span>${esc(p.main_business)}</div>`);
  const extra = [];
  if (p.founded) extra.push(`成立 ${esc(p.founded)}`);
  if (p.listed) extra.push(`上市 ${esc(p.listed)}`);
  if (p.chairman) extra.push(`法人 ${esc(p.chairman)}`);
  if (p.emp_num) extra.push(`员工 ${Number(p.emp_num).toLocaleString("zh-CN")} 人`);
  return `<div class="pf-head"><b class="pf-name">${esc(p.name)}</b>${meta ? `<span class="pf-meta">${esc(meta)}</span>` : ""}</div>`
    + rows.join("")
    + (extra.length ? `<div class="pf-extra">${extra.map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : "");
}

// —— 筹码判词（2026-10-01 王总反馈：散户 86% 标「较分散」、散户 46% 却标「非常分散」，看不懂）——
// 东财自带的 HOLD_FOCUS 是拿「股东户数绝对人数」分档的（8 千户=非常集中、13 万户=非常分散），
// 跟持股占比完全是两把尺子，跟上方「机构/自然人/散户(估算)」色带并排摆就会反直觉。
// 这里改用「散户(估算)占比」分档，与色带同一口径；股东户数环比另外单独判趋势。
function f10FocusWord(retail) {
  const r = Number(retail);
  if (!isFinite(r)) return null;
  if (r >= 90) return "极度分散";
  if (r >= 75) return "非常分散";
  if (r >= 60) return "较分散";
  if (r >= 45) return "基本均衡";
  if (r >= 30) return "较集中";
  return "非常集中";
}
function f10TrendWord(q) {
  if (q == null || !isFinite(Number(q))) return null;
  const a = Math.abs(Number(q));
  if (a < 3) return null;
  const dir = Number(q) > 0 ? "发散" : "收拢";
  const lv = a >= 20 ? "明显" : "略有";
  return { word: lv + dir, cls: Number(q) > 0 ? "up" : "down" };
}

function renderF10Holders(d) {
  const s = d.holder_summary || {};
  const hn = d.holdernum || {};
  const org = s.org_pct || 0, person = s.person_pct || 0, retail = s.retail_pct || 0;
  const bar = `<div class="sh-bar">
      <span class="sh-seg sh-org" style="width:${org}%"></span>
      <span class="sh-seg sh-person" style="width:${person}%"></span>
      <span class="sh-seg sh-retail" style="width:${retail}%"></span>
    </div>`;
  const legend = `<div class="sh-legend">
      <span><i class="sh-dot sh-org"></i>机构/产业资本 <b>${org}%</b></span>
      <span><i class="sh-dot sh-person"></i>自然人 <b>${person}%</b></span>
      <span><i class="sh-dot sh-retail"></i>散户(估算) <b>${retail}%</b></span>
    </div>`;
  // 结构结论（人话）：集中度判词跟上方色带同口径，再补一句大白话
  const fw = f10FocusWord(retail);
  const conclTail = org >= 40 ? "机构是主导方，筹码没散在散户手里"
    : org >= 15 ? "机构有一定话语权，但还算不上主导"
    : "筹码高度散户化，机构基本缺席";
  const conclHtml = fw ? `<div class="sh-concl"><b>筹码集中度 ${esc(fw)}</b>`
    + `<span class="sh-calc">（按散户(估算)占比 ${retail}% 判，与上方色带同一口径）</span>`
    + `<span class="sh-cl">${esc(conclTail)}</span></div>` : "";
  let hnHtml = "";
  if (hn.total) {
    const q = hn.qoq == null ? null : Number(hn.qoq);
    const qoq = q == null ? "" : ` <span class="sh-qoq ${q >= 0 ? "up" : "down"}">${q >= 0 ? "▲" : "▼"}${Math.abs(q).toFixed(2)}%</span>`;
    const tw = f10TrendWord(q);
    hnHtml = `<div class="sh-hn">股东户数 <b>${Number(hn.total).toLocaleString("zh-CN")}</b>${qoq}`
      + (tw ? ` · <span class="sh-cntxt">户数趋势</span> <b class="${tw.cls === "up" ? "sh-trend-up" : "sh-trend-down"}">${esc(tw.word)}</b>` : "")
      + (hn.end_date ? ` · 截至 ${esc(hn.end_date)}` : "") + `</div>`;
  }
  const hs = (d.holders || []).slice(0, 6).map((h) => {
    const chg = h.change === "增持" ? `<span class="sh-up">增持</span>` : h.change === "减持" ? `<span class="sh-down">减持</span>` : `<span class="sh-flat">不变</span>`;
    return `<tr><td class="num">${h.rank}</td><td><b>${esc(h.name)}</b></td><td class="sh-type sh-${h.cat}">${esc(h.type)}</td><td class="num">${h.ratio}%</td><td>${chg}</td></tr>`;
  }).join("");
  const note = `<div class="sh-note">机构占比含产业资本/一般法人；散户占比 = 100% − 前十大流通合计，系季报口径估算值，滞后约 1–2 月。</div>`;
  return bar + legend + conclHtml + hnHtml
    + (hs ? `<table class="sh-tbl"><thead><tr><th>#</th><th>流通股东</th><th>性质</th><th>占流通</th><th>变动</th></tr></thead><tbody>${hs}</tbody></table>` : "")
    + note;
}

function siListDate(s) {         // 20010827 → 2001-08-27
  const t = String(s || "").trim();
  return /^\d{8}$/.test(t) ? `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}` : "—";
}

function initStockQuote(symbol) {
  if (!$("#stkQuote")) return;
  loadStockQuote(symbol, false);
}

function loadStockQuote(symbol, force) {
  const box = $("#stkQuote");
  if (!box) return;
  if (!box.dataset.rendered) box.innerHTML = `<div class="s">重要信息加载中…</div>`;
  api(`/api/quote?symbol=${encodeURIComponent(symbol)}` + (force ? "&force=1" : ""), { signal: _stockAbort ? _stockAbort.signal : undefined })
    .then((d) => {
      if (d.error) { box.innerHTML = `<div class="s" style="color:var(--bear)">${esc(d.error)}</div>`; return; }
      renderStockQuote(d);
    })
    .catch((e) => {
      if (e && e.name === "AbortError") return;   // 抽屉关闭等主动取消：不渲染
      stockZoneFail(box, "重要信息", e, () => loadStockQuote(symbol, true));
    });
}

/* ---------- 公司主营（2026-09-26 新增） ----------
   王总要求：每只股票点开后，要能直接看懂「这家公司到底是干什么的」。
   数据来自 /api/quote 的 org（东财 F10 公司概况，服务端已做进程缓存 + 落库）。
   主营分两种形态渲染：列表型铺成标签、句子型（含长句）铺原文并折叠；
   简介默认收 3 行。两处的「展开」按钮都只在文本真的溢出时出现。
   取不到数据时整块不渲染（不留空壳，也不写"暂无"占位）。 */
function siBizHtml(org) {
  if (!org || (!org.main_business && !org.profile)) return "";
  const parts = String(org.main_business || "")
    .split(/[,，、;；]/).map((s) => s.trim().replace(/[。.；;,，]+$/, "").trim()).filter(Boolean);
  // 只有「列表型」主营才拆标签：项数 3~14 且单项 ≤14 字。
  // 阈值取自库内 229 只标的实测分布（单项长度 p75=20 字、项数中位数 3）：
  //   · 2 项多半是「被逗号拆开的整句」（如 中国长城「计算产业、系统装备及其他业务」），
  //     拆开反而割裂语义，故要求 ≥3 项；
  //   · 招行 / 平安银行那类长句（20+ 项）整段原文 + 折叠更忠实。
  const listLike = parts.length >= 3 && parts.length <= 14 && parts.every((p) => p.length <= 14);
  const mainCell = listLike
    ? `<span class="si-biz-chips">${parts.map((p) => `<span class="si-biz-chip">${esc(p)}</span>`).join("")}</span>`
    : `<span class="si-biz-prof clamp prof-2" id="siBizMain">${esc(org.main_business || "")}</span>`
      + `<button class="si-biz-toggle" data-biz-toggle="siBizMain" type="button">展开</button>`;
  const inds = [...new Set([org.industry, org.industry_csrc].filter(Boolean))];
  return `
    <div class="si-sec">公司主营<span class="si-report">东方财富 F10 公司概况</span></div>
    <div class="si-biz">
      ${org.main_business ? `<div class="si-biz-row"><span class="si-biz-k">主营业务</span><span class="si-biz-v">${mainCell}</span></div>` : ""}
      ${inds.length ? `<div class="si-biz-row"><span class="si-biz-k">所属行业</span><span class="si-biz-v">${esc(inds.join(" · "))}</span></div>` : ""}
      ${org.profile ? `<div class="si-biz-row"><span class="si-biz-k">公司简介</span><span class="si-biz-v">`
        + `<span class="si-biz-prof clamp" id="siBizProf">${esc(org.profile)}</span>`
        + `<button class="si-biz-toggle" data-biz-toggle="siBizProf" type="button">展开全文</button>`
        + `</span></div>` : ""}
    </div>`;
}

function renderStockQuote(d) {
  const box = $("#stkQuote");
  if (!box) return;
  const q = d.quote || {}, val = d.valuation || {}, fin = d.finance || {};
  const has = (v) => !(v === null || v === undefined || v === "" || isNaN(v));
  const cls = has(q.pct) ? (Number(q.pct) >= 0 ? "bull" : "bear") : "";
  const vsPre = (v) => (has(v) && has(q.prev_close)) ? (Number(v) >= Number(q.prev_close) ? "bull" : "bear") : "";
  const item = (k, v, c, id) =>
    `<div class="si-item"${id ? ` id="${id}"` : ""}><span>${k}</span><b class="${c || ""}">${v}</b></div>`;
  const grp = (label, extra, items) =>
    `<div class="si-sec">${label}${extra || ""}</div><div class="si-grid">${items.filter(Boolean).join("")}</div>`;
  const chg = has(q.change) ? (Number(q.change) >= 0 ? "+" : "") + siNum(q.change) : "—";
  const bps = has(val.bps) ? val.bps : fin.bps;
  const dv = d.dividend || {};
  const dy = has(dv.yield_pct) ? Number(dv.yield_pct) : null;
  const roe = has(fin.roe) ? Number(fin.roe) : null;
  const pe = has(val.pe_ttm) ? Number(val.pe_ttm) : null;
  // 「价值标准」三盏灯（王总的筛选口径：PE<20 / ROE>10% / 股息率>3%）。
  // 只做客观对照提示，缺数据时显示「—」而不是判为不符合，避免误导。
  const lamp = (label, ok, txt) =>
    `<div class="vl-item ${ok === null ? "vl-na" : (ok ? "vl-ok" : "vl-no")}">
       <span class="vl-dot"></span><span class="vl-label">${label}</span>
       <b>${txt}</b><span class="vl-mark">${ok === null ? "无数据" : (ok ? "符合" : "不符合")}</span>
     </div>`;
  const valStd = `<div class="si-sec">价值标准<span class="si-report">按你的筛选口径自动对照</span></div>
    <div class="vl-row">
      ${lamp("PE(TTM) &lt; 20", pe === null ? null : pe < 20, pe === null ? "—" : pe.toFixed(2))}
      ${lamp("ROE &gt; 10%", roe === null ? null : roe > 10, roe === null ? "—" : roe.toFixed(2) + "%")}
      ${lamp("股息率 &gt; 3%", dy === null ? null : dy > 3, dy === null ? "—" : dy.toFixed(2) + "%")}
    </div>
    <div class="si-src-note">仅按上述三条口径做客观对照，不构成投资建议。</div>`;

  box.dataset.rendered = "1";
  box.innerHTML = `
    <div class="si-head">
      <div class="si-price ${cls}">${siNum(q.price)}</div>
      <div class="si-tags">
        <span class="${cls}">${chg}</span>
        <span class="${cls}">${siPct(q.pct)}</span>
        ${q.time ? `<span class="si-when">${esc(q.time)}</span>` : ""}
      </div>
      <button class="btn sm si-refresh" id="siRefresh">刷新</button>
    </div>
    ${grp("行情", "", [
      item("今开", siNum(q.open), vsPre(q.open)),
      item("昨收", siNum(q.prev_close)),
      item("最高", siNum(q.high), vsPre(q.high)),
      item("最低", siNum(q.low), vsPre(q.low)),
      item("成交量", siVol(q.volume)),
      item("成交额", siMoney(q.amount)),
      item("换手率", siPctPlain(q.turnover)),
      item("振幅", siPctPlain(q.amplitude)),
      item("量比", siNum(q.volume_ratio)),
    ])}
    ${grp("估值", "", [
      item("市盈率 TTM", siNum(val.pe_ttm)),
      item("市盈率(动)", siNum(val.pe_dyn)),
      item("市盈率(静)", siNum(val.pe_static)),
      item("市净率 PB", siNum(val.pb)),
      item("股息率", has(dv.yield_pct) ? siPctPlain(dv.yield_pct) : "—"),
      item("总市值", siMoney(val.total_mv)),
      item("流通市值", siMoney(val.float_mv)),
      item("每股净资产", siNum(bps)),
      item("上市日期", siListDate(q.list_date), "", "siListDate"),
    ])}
    ${dv.basis ? `<div class="si-src-note">股息率口径：${esc(dv.note || "")} → 每股派息 ${siNum(dv.per_share)} 元（${esc(dv.basis)}，${dv.items ? dv.items.length : 0} 期）</div>` : ""}
    ${grp("盈利", fin.report ? `<span class="si-report">${esc(fin.report)}</span>` : "", [
      item("ROE(加权)", siPctPlain(fin.roe)),
      item("毛利率", siPctPlain(fin.gross_margin)),
      item("净利率", siPctPlain(fin.net_margin)),
      item("总资产收益率", siPctPlain(fin.roa)),
      item("每股收益", siNum(fin.eps)),
      item("每股经营现金流", siNum(fin.ocf_ps)),
    ])}
    ${grp("成长", "", [
      item("营业总收入", siMoney(fin.revenue)),
      item("营收同比", has(fin.revenue_yoy) ? siPct(fin.revenue_yoy) : "—"),
      item("归母净利润", siMoney(fin.net_profit)),
      item("净利同比", has(fin.net_profit_yoy) ? siPct(fin.net_profit_yoy) : "—"),
    ])}
    ${valStd}
    ${siBizHtml(d.org)}
    <div class="si-foot">数据源：${esc(d.source || "—")}</div>`;
  const rf = box.querySelector("#siRefresh");
  if (rf) rf.onclick = () => loadStockQuote(box.dataset.sym || d.symbol, true);
  // 折叠/展开：按钮只在文本真的溢出时保留 —— 不足两/三行还挂个「展开全文」，
  // 点了没反应，用户会以为坏了。测量不到高度（抽屉未展示）时不误删按钮。
  box.querySelectorAll("[data-biz-toggle]").forEach((bt) => {
    const t = box.querySelector("#" + bt.dataset.bizToggle);
    if (!t || t.clientHeight === 0) return;
    if (t.scrollHeight <= t.clientHeight + 2) { t.classList.remove("clamp"); bt.remove(); return; }
    const foldedText = bt.dataset.bizToggle === "siBizMain" ? "展开" : "展开全文";
    bt.onclick = () => {
      const folded = t.classList.toggle("clamp");
      bt.textContent = folded ? foldedText : "收起";
    };
  });
}

/* ---------- 股票 K 线（2026-09-25 新增；当晚改为「可拖动 / 可缩放」视口模型） ----------
   王总要求：像同花顺那样 —— 按住左右拖动，一直能拖到上市首日；能缩放；能看当天明细。
   实现要点：
     · 后端 /api/kline 已返回「全历史」（东财前复权 / 新浪不复权，均数千根），
       所以拖动只重绘、**不再发请求** → 拖动零延迟。
     · 视口 = { offset: 首根下标, count: 可见根数 }；拖动改 offset，滚轮改 count。
     · MA5/10/20/60 基于「全量数据」计算，保证拖到左边缘时均线仍是真值（而非从可见区起算）。
     · 涨跌配色遵循 A 股口径：涨 = 红(--bull)、跌 = 绿(--bear)。
*/
const KLINE_PAD = { l: 58, r: 14, t: 18, b: 24 };
const KLINE_MA = [5, 10, 20, 60];
const KLINE_MA_COLOR = { 5: "#0E7490", 10: "#f59e0b", 20: "#a855f7", 60: "#64748b" };
let _kv = null;                 // 当前视口状态（一次只开一个抽屉，故用单例即可）
let _klineResizeBound = false;
let _bfKeysBound = false;        // 板块资金流区间回车监听的一次性守卫（2026-10-12 修泄漏）

function klineCssH(w) { return w < 460 ? 300 : 380; }

function klineMA(rows) {
  const out = {};
  for (const n of KLINE_MA) {
    const a = new Array(rows.length).fill(null);
    let s = 0;
    for (let i = 0; i < rows.length; i++) {
      s += rows[i].close;
      if (i >= n) s -= rows[i - n].close;
      if (i >= n - 1) a[i] = s / n;
    }
    out[n] = a;
  }
  return out;
}

function klineGeom() {
  const canvas = $("#klineCanvas");
  if (!canvas || !_kv) return null;
  const wrap = canvas.parentElement;
  const W = Math.max(260, Math.round((wrap && wrap.clientWidth) || 600));
  const H = klineCssH(W);
  const volH = Math.round(H * 0.20), gap = 10;
  const plotW = W - KLINE_PAD.l - KLINE_PAD.r;
  const priceH = H - KLINE_PAD.t - volH - gap - KLINE_PAD.b;
  const volTop = KLINE_PAD.t + priceH + gap;
  const slot = plotW / Math.max(1, _kv.count);
  return { canvas, W, H, volH, gap, plotW, priceH, volTop, volBot: volTop + volH,
           slot, bodyW: Math.max(1.2, slot * 0.66) };
}

function klineVisibleRows() {
  if (!_kv || !_kv.rows.length) return [];
  return _kv.rows.slice(_kv.offset, _kv.offset + _kv.count);
}

function updateKlineRange() {
  const el = $("#klineRange");
  if (!el || !_kv) return;
  const vis = klineVisibleRows();
  if (!vis.length) { el.textContent = "—"; return; }
  const adjust = _kv.adjust === "raw" ? "不复权" : "前复权";
  const atEarliest = _kv.offset <= 0;
  const atLatest = _kv.offset + _kv.count >= _kv.rows.length;
  // 视口铺满全部 ≠ 上市至今：只有后端真·全历史（fullHistory）才准标「上市至今」，
  // 否则（腾讯单页兜底）是把半年走势冒充上市行情——进哥 2026-10-13 反馈的误判根源。
  const trulyFull = _kv.fullHistory && atEarliest && atLatest;
  let edge;
  if (trulyFull) {
    edge = ` · <b class="kline-edge">已显示全部（上市至今 ${esc(_kv.rows[0].date)} 起）</b>`;
  } else if (atEarliest) {
    // 注意：hasMore 时「最早」只是**本次已加载**的最早，不是上市首日——不能误导
    edge = _kv.hasMore
      ? ` · <b class="kline-edge">已到本次加载的最早（还有更早历史 —— 继续缩小或点「全历史」）</b>`
      : (_kv.fullHistory
          ? ` · <b class="kline-edge">已到最早一根（${esc(_kv.firstDate)}）</b>`
          : ` · <b class="kline-edge" style="color:var(--warn, #f59e0b)">⚠ 数据源暂只提供最近 ${_kv.rows.length} 根（非上市至今）—— 点「全历史」或继续缩小可拉取完整行情</b>`);
  } else if (atLatest) {
    edge = ` · <b class="kline-edge">最新</b>`;
  } else {
    edge = "";
  }
  el.innerHTML = `${esc(vis[0].date)} ~ ${esc(vis[vis.length - 1].date)}`
    + ` · 可见 ${vis.length} / 共 ${_kv.rows.length} 根 · ${adjust}` + edge;
}

function initStockKline(symbol) {
  const wrap = $("#klineWrap");
  if (!wrap) return;
  _kv = null;
  const periodBtns = $$("#klineWrap .kbtn");
  periodBtns.forEach((b) => {
    b.onclick = () => {
      periodBtns.forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      loadKline(symbol, b.dataset.p, false);
    };
  });
  const rf = $("#klineRefresh");
  if (rf) rf.onclick = () => loadKline(symbol, curKlinePeriod(), true);
  // 「全历史」：一键从上市首日看到今天（等同一直缩小到底；进哥 2026-10-12 要求便捷性）
  const kf = $("#klineFull");
  if (kf) kf.onclick = () => klineShowAll();
  const lt = $("#klineLatest");
  if (lt) lt.onclick = () => {
    if (!_kv) return;
    _kv.offset = Math.max(0, _kv.rows.length - _kv.count);
    _kv.crossIdx = null;
    klinePaint();
    const tip = $("#klineTip"); if (tip) tip.hidden = true;
  };
  // 逐屏翻页（拖动之外的另一条路；同花顺也有类似按钮）
  const klineShift = (dir) => {
    if (!_kv) return;
    const maxOff = Math.max(0, _kv.rows.length - _kv.count);
    const step = Math.max(1, Math.round(_kv.count * 0.8));
    _kv.offset = Math.min(maxOff, Math.max(0, _kv.offset + dir * step));
    _kv.crossIdx = null;
    klinePaint();
    const tip = $("#klineTip"); if (tip) tip.hidden = true;
  };
  const ob = $("#klineOlder"); if (ob) ob.onclick = () => klineShift(-1);
  const nb = $("#klineNewer"); if (nb) nb.onclick = () => klineShift(1);
  if (!_klineResizeBound) {
    window.addEventListener("resize", () => { if (_kv) klinePaint(); });
    _klineResizeBound = true;
  }
  loadKline(symbol, "day", false);
}

/**
 * 「全历史」全览：从上市首日一路看到今天。
 *   · 已加载全量（或后端表明没有更早的数据）→ 直接把视口铺满全部，不重复请求；
 *   · 否则先把全量拉回来（limit=0，后端 90s 缓存，二次秒回），加载完自动铺满。
 * 2026-10-12 进哥要求：日/周/月 K 一直缩小要能看到「上市至今」的整体走势。
 */
function klineShowAll() {
  if (!_kv || _kv.loading) return;
  // 已掌握真·全历史（东财/新浪/腾讯分段）→ 直接把视口铺满全部，不重复请求；
  // 否则（腾讯单页兜底，fullHistory=false，!hasMore 会短路）→ 认真拉全历史（后端 limit=0 重试）。
  if (_kv.fullHistory) {
    _kv.count = _kv.rows.length;
    _kv.offset = 0;
    _kv.crossIdx = null;
    klinePaint();
    const tip = $("#klineTip"); if (tip) tip.hidden = true;
    return;
  }
  _kv.loading = true;
  loadKline(_kv.symbol, _kv.period, false, { all: true });
}

function curKlinePeriod() {
  const b = $("#klineWrap .kbtn.active");
  return (b && b.dataset.p) || "day";
}

// 2026-10-05 建议8：K 线一次拉几千根纯属浪费——前端本来只看最近 120 根，
// 默认向后端要最近 250 根；点「拉全部历史」才传 limit=0（后端 90s 缓存，二次秒回）。
const KLINE_DEFAULT_LIMIT = 250;
const KLINE_ZOOM_MIN = 20;      // 最多放大到看 20 根（再细就没有信息量了）
function loadKline(symbol, period, force, opts) {
  const o = opts || {};
  const snap = $("#klineSnap"), recent = $("#klineRecent");
  // 全览意图 = 显式 all，**或沿用上一次的全览状态**（切周期时不该丢掉"看全程"）。
  const wantAll = !!o.all || !!(_kv && _kv.all);
  const lim = wantAll ? 0 : (o.limit != null ? o.limit : KLINE_DEFAULT_LIMIT);
  if (snap) snap.innerHTML = `<span class="s">${wantAll ? "正在拉取全部历史 K 线…" : "行情加载中…"}</span>`;
  api(`/api/kline?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}&limit=${lim}` + (force ? "&_=" + Date.now() : ""), { signal: _stockAbort ? _stockAbort.signal : undefined })
    .then((d) => {
      if (d.error) {
        if (snap) snap.innerHTML = `<span class="s" style="color:var(--bear)">${esc(d.error)}</span>`;
        if (recent) recent.innerHTML = `<span class="s">—</span>`;
        _kv = null;
        return;
      }
      renderKlineSnap(d);
      renderKlineRecent(d);
      const rows = d.klines || [];
      if (!rows.length) { _kv = null; return; }
      // 全览：把视口铺满「上市首日 → 今天」；
      // 否则默认显示最近 120 根，切周期时沿用当前可见根数，减少"跳一下"的割裂感。
      const showAll = wantAll || !d.has_more;
      let count, offset;
      if (showAll) {
        count = rows.length;
        offset = 0;
      } else {
        const keep = (_kv && _kv.count) || 120;
        count = Math.max(KLINE_ZOOM_MIN, Math.min(keep, rows.length));
        offset = Math.max(0, rows.length - count);
      }
      _kv = {
        symbol, period, rows, count, offset,
        all: showAll,
        fullHistory: !!d.full_history,   // 后端是否真·全历史（东财/新浪/腾讯分段）；false=腾讯单页兜底
        adjust: d.adjust || "qfq", source: d.source || "",
        hasMore: !!d.has_more, earliest: d.earliest_date || d.first_date || "",
        firstDate: d.first_date || rows[0].date || "",
        ma: klineMA(rows),
        dragging: false, dragX0: 0, dragOff0: 0, crossIdx: null, loading: false,
      };
      bindKlineInteractions();
      klinePaint();
    })
    .catch((e) => {
      if (_kv) _kv.loading = false;   // 失败必须复位，否则"缩到底自动加载"再也不会触发
      if (e && e.name === "AbortError") return;   // 抽屉关闭等主动取消：不清为失败态
      stockZoneFail(snap, "行情", e, () => loadKline(symbol, period, false));
    });
}

function renderKlineSnap(d) {
  const c = d.current || {};
  const up = Number(c.pct || 0) >= 0;
  const cls = up ? "bull" : "bear";
  const sign = up ? "+" : "";
  // 周期标注：周/月 K 的「昨收 / 涨跌幅」其实是「上周收 / 周涨跌」，若不标注会与上方
  // 「重要信息」里的日涨跌看起来互相矛盾（实际口径不同）——故按周期改写标签。
  const per = d.period === "week" ? "周" : (d.period === "month" ? "月" : "");
  const prevLbl = per ? `上${per}收` : "昨收";
  const amtTxt = (d.amount_est && c.amount != null) ? "≈" + siMoney(c.amount) : siMoney(c.amount);
  const el = $("#klineSnap");
  if (!el) return;
  el.innerHTML = `
    <div class="ks-price ${cls}">${siNum(c.price)} <span class="ks-pct">${sign}${siNum(c.pct)}%</span>${per ? `<span class="ks-per">${per}涨跌</span>` : ""}</div>
    <div class="ks-grid">
      <div><span>${per ? per + "开" : "今开"}</span><b>${siNum(c.open)}</b></div>
      <div><span>${per ? per + "高" : "最高"}</span><b>${siNum(c.high)}</b></div>
      <div><span>${per ? per + "低" : "最低"}</span><b>${siNum(c.low)}</b></div>
      <div><span>${per ? per + "收" : "收/最新"}</span><b>${siNum(c.price)}</b></div>
      <div><span>${prevLbl}</span><b>${siNum(c.prev_close)}</b></div>
      <div><span>成交量</span><b>${siVol(c.volume)}</b></div>
      <div><span>成交额</span><b>${amtTxt}</b></div>
      <div><span>数据日</span><b>${esc((c.date || "").slice(0, 10))}</b></div>
    </div>
    <div class="ks-src">数据源：${esc(d.source || "—")}${d.adjust === "raw" ? " · <b>不复权</b>（长期图上的除权跳空属正常）" : " · 前复权"}${d.local_fallback ? ' · <b style="color:#d97706">⚠ 本地旧数据（非实时，详见说明）</b>' : ""}${per ? ` · 当前为 <b>${per}K</b>` : ""}${d.amount_est ? " · 成交额按「收盘价 × 成交量」估算（≈）" : ""}${(d.has_more || !d.full_history) ? ` · 本次 ${d.count} 根${d.full_history ? " / 全历史 " + d.total + " 根" : "（仅最近 " + d.total + " 根）"} <a class="ks-more" id="klineLoadAll" href="javascript:void 0">拉全部历史</a>` : ""}</div>`;
  // 上市日期兜底：部分行情源不提供该字段，用 K 线最早一根的日期补，并明确标注口径。
  // 注意用 earliest_date（全历史首日）——limit=250 时 first_date 只是「250 根之前的那天」，
  // 拿它当上市日会把 2000 年的票写成 2010 年上市（2026-10-05 建议8 的降采样坑）。
  const ld = $("#siListDate");
  const listDateSrc = d.earliest_date || d.first_date || "";
  if (ld) {
    const b = ld.querySelector("b");
    const cur = ((b && b.textContent) || "").trim();
    if (b && (cur === "—" || cur === "") && listDateSrc) {
      b.textContent = listDateSrc;
      const tag = document.createElement("i");
      tag.className = "si-est";
      tag.textContent = "按最早行情";
      ld.appendChild(tag);
    }
  }
  // 「拉全部历史」：只有本次被降采样（has_more=true）时才出现；与工具条「全历史」同一入口
  const more = $("#klineLoadAll");
  if (more) more.onclick = () => klineShowAll();
}

function renderKlineRecent(d) {
  const per = d.period === "week" ? "周" : (d.period === "month" ? "月" : "日");
  const rows = (d.klines || []).slice(-15).reverse();
  const el = $("#klineRecent");
  if (!el) return;
  if (!rows.length) { el.innerHTML = `<span class="s">—</span>`; return; }
  const trs = rows.map((r) => {
    const up = (r.close || 0) >= (r.open || 0);
    const cls = up ? "bull" : "bear";
    return `<tr>
      <td>${esc(r.date)}</td>
      <td class="num">${siNum(r.open)}</td>
      <td class="num">${siNum(r.high)}</td>
      <td class="num">${siNum(r.low)}</td>
      <td class="num">${siNum(r.close)}</td>
      <td class="num ${cls}">${siPct(r.pct)}</td>
      <td class="num">${siVol(r.volume)}</td>
    </tr>`;
  }).join("");
  el.innerHTML = `<div class="kr-title">最近 15 根 ${per}K 线明细（自上而下 = 由近及远；涨跌幅为${per}涨跌）</div>
    <table><thead><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>涨跌幅</th><th>量</th></tr></thead><tbody>${trs}</tbody></table>`;
}

/* 交互绑定：拖动平移（鼠标 / 触摸）、滚轮缩放、悬停十字光标读数。 */
function bindKlineInteractions() {
  const canvas = $("#klineCanvas");
  if (!canvas || !_kv) return;
  const tip = $("#klineTip");
  canvas.style.cursor = "crosshair";

  const clampOffset = () => {
    const maxOff = Math.max(0, _kv.rows.length - _kv.count);
    _kv.offset = Math.min(maxOff, Math.max(0, _kv.offset));
  };

  canvas.onmousedown = (e) => {
    if (e.button !== 0 || !_kv) return;
    _kv.dragging = true;
    _kv.dragX0 = e.clientX;
    _kv.dragOff0 = _kv.offset;
    _kv.crossIdx = null;
    if (tip) tip.hidden = true;
    canvas.style.cursor = "grabbing";
    e.preventDefault();
  };

  canvas.onmousemove = (e) => {
    const g = klineGeom();
    if (!g || !_kv) return;
    if (_kv.dragging) {
      const deltaBars = Math.round((e.clientX - _kv.dragX0) / g.slot);
      _kv.offset = _kv.dragOff0 - deltaBars;
      clampOffset();
      klinePaint();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.floor((mx - KLINE_PAD.l) / g.slot);
    if (mx < KLINE_PAD.l || idx < 0 || idx >= _kv.count) {
      if (_kv.crossIdx !== null) { _kv.crossIdx = null; klinePaint(); }
      if (tip) tip.hidden = true;
      return;
    }
    if (_kv.crossIdx !== idx) { _kv.crossIdx = idx; klinePaint(); }
    const vis = klineVisibleRows();
    const r = vis[idx];
    if (!r) return;
    if (tip) {
      const up = r.close >= r.open;
      const sign = (r.pct || 0) >= 0 ? "+" : "";
      tip.hidden = false;
      tip.innerHTML = `<b>${esc(r.date)}</b><br>开 ${siNum(r.open)} 高 ${siNum(r.high)}<br>`
        + `低 ${siNum(r.low)} 收 ${siNum(r.close)}<br>`
        + `<span class="${up ? "bull" : "bear"}">${sign}${siNum(r.pct)}%</span> 量 ${siVol(r.volume)}`;
      const x = KLINE_PAD.l + g.slot * idx + g.slot / 2;
      const tw = tip.offsetWidth || 160;
      let left = x + 12;
      if (left + tw > g.W) left = x - tw - 12;
      if (left < 0) left = 4;
      tip.style.left = left + "px";
      tip.style.top = (KLINE_PAD.t + 8) + "px";
    }
  };

  const endDrag = () => {
    if (!_kv) return;
    _kv.dragging = false;
    if (canvas.style.cursor === "grabbing") canvas.style.cursor = "crosshair";
  };
  canvas.onmouseup = endDrag;
  canvas.onmouseleave = () => {
    if (!_kv) return;
    _kv.dragging = false;
    _kv.crossIdx = null;
    canvas.style.cursor = "crosshair";
    klinePaint();
    if (tip) tip.hidden = true;
  };

  canvas.onwheel = (e) => {
    const g = klineGeom();
    if (!g || !_kv) return;
    e.preventDefault();
    // 「一直缩小到底」= 想看更早的历史：若后端还有全量没拉回来（hasMore），
    // 或当前只是腾讯单页兜底（fullHistory=false，看似铺满其实只有最近 N 根），
    // 都自动补全并全览。进哥 2026-10-13：此前单页兜底时 hasMore=false，缩到底也不会触发，
    // 导致半年走势被当成上市行情。
    if (e.deltaY > 0 && _kv.count >= _kv.rows.length && !_kv.loading
        && (_kv.hasMore || !_kv.fullHistory)) {
      _kv.loading = true;
      loadKline(_kv.symbol, _kv.period, false, { all: true });
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const frac = Math.min(1, Math.max(0, (mx - KLINE_PAD.l) / g.plotW));
    const anchor = _kv.offset + frac * _kv.count;
    const factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    const newCount = Math.round(Math.min(_kv.rows.length, Math.max(KLINE_ZOOM_MIN, _kv.count * factor)));
    if (newCount === _kv.count) return;
    _kv.count = newCount;
    _kv.offset = Math.round(anchor - frac * newCount);
    clampOffset();
    _kv.crossIdx = null;
    klinePaint();
  };

  // 触摸：单指拖动平移，双指捏合缩放（桌面壳一般用不到，但手机上可用）
  let pinch = null;
  const tDist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  canvas.addEventListener("touchstart", (e) => {
    if (!_kv) return;
    if (e.touches.length === 1) {
      _kv.dragging = true;
      _kv.dragX0 = e.touches[0].clientX;
      _kv.dragOff0 = _kv.offset;
    } else if (e.touches.length === 2) {
      _kv.dragging = false;
      pinch = { d: tDist(e.touches), count: _kv.count };
    }
  }, { passive: true });
  canvas.addEventListener("touchmove", (e) => {
    const g = klineGeom();
    if (!g || !_kv) return;
    if (e.touches.length === 1 && _kv.dragging) {
      const deltaBars = Math.round((e.touches[0].clientX - _kv.dragX0) / g.slot);
      _kv.offset = _kv.dragOff0 - deltaBars;
      clampOffset();
      klinePaint();
      e.preventDefault();
    } else if (e.touches.length === 2 && pinch && pinch.d > 0) {
      const ratio = pinch.d / tDist(e.touches);
      const nc = Math.round(Math.min(_kv.rows.length, Math.max(KLINE_ZOOM_MIN, pinch.count * ratio)));
      // 捏合到「已加载的全部」且还有更早的历史 → 自动拉全量并全览（与滚轮缩到底一致）
      if (nc >= _kv.rows.length && !_kv.loading
          && (_kv.hasMore || !_kv.fullHistory)) {
        _kv.loading = true;
        loadKline(_kv.symbol, _kv.period, false, { all: true });
        e.preventDefault();
        return;
      }
      _kv.count = nc;
      clampOffset();
      klinePaint();
      e.preventDefault();
    }
  }, { passive: false });
  canvas.addEventListener("touchend", () => { _kv && (_kv.dragging = false); pinch = null; }, { passive: true });
}

/* 主绘制：蜡烛 + 成交量副图 + MA5/10/20/60 + 价格/日期轴 + 十字光标。 */
function klinePaint() {
  const g = klineGeom();
  if (!g || !_kv) return;
  const { canvas } = g;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(g.W * dpr);
  canvas.height = Math.round(g.H * dpr);
  canvas.style.width = g.W + "px";
  canvas.style.height = g.H + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, g.W, g.H);

  const vis = klineVisibleRows();
  if (!vis.length) return;

  const cssVar = (n, fb) => {
    const v = getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    return v || fb;
  };
  const colUp = cssVar("--bull", "#ef4444"), colDn = cssVar("--bear", "#22c55e");
  const colGrid = cssVar("--border", "#334155"), colText = cssVar("--muted", "#94a3b8");
  const colPanel = cssVar("--panel", "#111827"), colAcct = cssVar("--accent", "#0E7490");

  const i0 = _kv.offset, i1 = _kv.offset + vis.length;
  let lo = Infinity, hi = -Infinity, vMax = 0;
  for (const r of vis) {
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
    if ((r.volume || 0) > vMax) vMax = r.volume || 0;
  }
  // 均线也参与纵轴范围，避免拖到左边缘时 MA 线跑出画布
  for (const n of KLINE_MA) {
    const a = _kv.ma[n] || [];
    for (let i = i0; i < i1; i++) {
      const v = a[i];
      if (v == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
  const mg = (hi - lo) * 0.06 || 1;
  lo -= mg; hi += mg;
  const yOf = (p) => KLINE_PAD.t + (hi - p) / (hi - lo) * g.priceH;
  const xOf = (k) => KLINE_PAD.l + g.slot * k + g.slot / 2;

  // 价格网格 + 刻度
  ctx.strokeStyle = colGrid; ctx.lineWidth = 1; ctx.fillStyle = colText;
  ctx.font = "11px ui-monospace, Consolas, monospace";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  for (let k = 0; k <= 5; k++) {
    const p = lo + (hi - lo) * k / 5, y = yOf(p);
    ctx.globalAlpha = 0.3;
    ctx.beginPath(); ctx.moveTo(KLINE_PAD.l, y); ctx.lineTo(g.W - KLINE_PAD.r, y); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(p.toFixed(p >= 1000 ? 0 : 2), 4, y);
  }
  // 成交量区底
  ctx.fillStyle = colPanel; ctx.globalAlpha = 0.35;
  ctx.fillRect(KLINE_PAD.l, g.volTop, g.plotW, g.volBot - g.volTop);
  ctx.globalAlpha = 1;

  // 蜡烛 + 量柱
  for (let k = 0; k < vis.length; k++) {
    const r = vis[k], x = xOf(k);
    const up = r.close >= r.open, col = up ? colUp : colDn;
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yOf(r.high)); ctx.lineTo(x, yOf(r.low)); ctx.stroke();
    const yO = yOf(r.open), yC = yOf(r.close);
    ctx.fillStyle = col;
    ctx.fillRect(x - g.bodyW / 2, Math.min(yO, yC), g.bodyW, Math.max(1, Math.abs(yC - yO)));
    const vh = vMax ? ((r.volume || 0) / vMax) * (g.volBot - g.volTop) : 0;
    ctx.globalAlpha = 0.55;
    ctx.fillRect(x - g.bodyW / 2, g.volBot - vh, g.bodyW, vh);
    ctx.globalAlpha = 1;
  }
  // 均线
  for (const n of KLINE_MA) {
    const a = _kv.ma[n] || [];
    ctx.strokeStyle = KLINE_MA_COLOR[n] || colAcct;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    for (let i = i0; i < i1; i++) {
      const v = a[i];
      if (v == null) continue;
      const x = xOf(i - i0), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  }
  // 日期轴
  ctx.fillStyle = colText; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const step = Math.max(1, Math.floor(vis.length / 6));
  for (let k = 0; k < vis.length; k += step) {
    const lbl = _kv.period === "day" ? vis[k].date.slice(0, 10) : vis[k].date.slice(0, 7);
    ctx.fillText(lbl, xOf(k), g.H - 11);
  }
  // 均线图例
  ctx.textAlign = "left"; ctx.font = "11px sans-serif";
  let lx = KLINE_PAD.l + 4;
  for (const n of KLINE_MA) {
    ctx.fillStyle = KLINE_MA_COLOR[n]; ctx.fillRect(lx, KLINE_PAD.t - 8, 10, 3);
    ctx.fillStyle = colText; ctx.fillText("MA" + n, lx + 13, KLINE_PAD.t - 6);
    lx += 56;
  }
  // 十字光标 + 价格标签
  if (_kv.crossIdx != null && _kv.crossIdx >= 0 && _kv.crossIdx < vis.length) {
    const r = vis[_kv.crossIdx], cx = xOf(_kv.crossIdx), cy = yOf(r.close);
    ctx.save();
    ctx.strokeStyle = colText; ctx.globalAlpha = 0.55; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(cx, KLINE_PAD.t); ctx.lineTo(cx, g.volBot); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(KLINE_PAD.l, cy); ctx.lineTo(g.W - KLINE_PAD.r, cy); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = colAcct; ctx.fillRect(2, cy - 8, 50, 16);
    ctx.fillStyle = "#ffffff"; ctx.textAlign = "center";
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.fillText(r.close.toFixed(2), 27, cy + 1);
    ctx.textAlign = "left";
  }
  updateKlineRange();
}


// 时间显示规则（2026-09-25）：源站没给时刻 → 只显示到日并标注「仅日期」；
// 源站没给时间 → 显示「未披露」。绝不用本系统的抓取时刻冒充选手操作时间。
function fmtTimeText(v, dash = "未披露") {
  const s = String(v == null ? "" : v).trim();
  if (!s) return dash;
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s + "（仅日期）";
  if (/ 00:00:00$/.test(s)) return s.slice(0, 10) + "（仅日期）";
  return s;
}

function renderOperating(list) {
  const items = toArr(list);
  if (!items.length) { $("#p-operating").innerHTML = `<div class="empty">暂无实盘操作记录</div>`; return; }
  // 2026-09-24：trades 同样区分 demo 与真实（demo URL 全是 example.local）。
  // chip "东方财富/同花顺/雪球" 来自 demo_data 给选手预设的渠道，**不代表这些平台真的被抓取**——
  // 当前 PARSER_REGISTRY 仅 taoguba，其它站需要单独写 HTML 解析器才能实时抓取。
  const isDemoTrade = (t) => /example\.local/i.test(t.post_url || "") || /^demo-/i.test(t.post_id || "");
  const demoN = items.filter(isDemoTrade).length;
  const realN = items.length - demoN;
  const sites = [...new Set(items.map(t => t.site || "?"))];
  const rows = items.map((t) => {
    const plat = t.author_platform || t.site;
    const ch = sourceChannel(plat ? [plat] : [], t.author_tier);
    const demo = isDemoTrade(t);
    return `<tr data-kind="trade" data-id="${escAttr(t.id)}" ${demo ? 'data-demo="1"' : 'data-real="1"'}>
      <td>${esc(t.author)} <span class="chip-s ${demo ? "chip-demo" : "chip-real"}">${demo ? "示例" : "真实"}</span><div class="src-line">${ch}</div></td>
      <td>${t.post_time ? esc(fmtTimeText(t.post_time)) : "<span class='s'>未披露</span>"}</td>
      <td>${actionBadge(t.action)}</td>
      <td>${t.symbol
        ? `<span class="kline-chip chip-s" data-sym="${escAttr(t.symbol)}" title="点击看该股详情与K线"><b>${esc(t.symbol_name || "名称待补全")}</b> <span class="stk-code">${esc(t.symbol)}</span></span>`
        : `<b>${esc(t.symbol_name || "名称待补全")}</b> <span class="stk-code"></span>`}</td>
      ${priceCell(t.price)}${qtyCell(t.quantity)}
      ${refCell([plat])}
    </tr>`;
  }).join("");
  const banner = realN === 0
    ? `<div class="banner banner-warn"><b>当前是示例数据</b>（${items.length} 条），trades 表里所有 URL 都是 <code>example.local/demo</code>。${sites.length === 1 && sites[0] === 'taoguba' ? `唯一已对接的真实抓取站是 <b>淘股吧</b>（需登录）；其他平台（东方财富/雪球/同花顺）尚未写解析器。` : "尚未对接任何真实抓取源。"}</div>`
    : `<div class="banner banner-info">真实抓取 ${realN} 条 / 示例 ${demoN} 条（来源：${sites.join(", ")}）</div>`;
  $("#p-operating").innerHTML = `
    ${banner}
    <div class="hint" style="margin-bottom:10px">每个选手均标注其<b>来源渠道</b>：<b>平台</b>（淘股吧/东方财富/雪球/同花顺） + <b>所属层级</b>（A~F）。标的列格式为「股票简称 + 股票代码」：代码是 6 位数字（如 600519），对应贵州茅台，不是序号。${freshLegend()}</div>
    <div class="hint" style="margin-bottom:10px">「时间」是<b>选手发帖/操作的时间</b>，不是本系统的抓取时间。源站没给出时间的显示<b>未披露</b>——不会用抓取时刻冒充，避免误导判断。</div>
    <div class="hint" style="margin-bottom:10px">「价格」「数量」只回填<b>帖子里明写了</b>的部分（如"11.38元买入2000股""津药药业6手"）。博主没写就是<b>帖中未披露</b>，不会去猜——预测价（"跌到1100附近"）一律不当事。数量统一折算为<b>股数</b>（1 手 = 100 股），万元级显示"X.X万"。</div>
    <table><thead><tr><th>选手（来源渠道）</th><th>时间</th><th>动作</th><th>标的（名称 + 代码）</th><th class="num">价格</th><th class="num">数量</th><th>最后更新</th></tr></thead><tbody>${rows}</tbody></table>`;
  bindRows();
  applyFreshness();
}

/* ---------- 实盘交易：价格 / 数量两列的渲染（2026-10-05，对应测试报告 P0-2） ----------
   此前两列为空就渲染一个 "—"，看着像「取数失败」，实际是**帖子里本来就没写**。
   空值必须说清楚是"帖中未披露"，不能让用户以为系统漏抓。
   数量统一按**股数**存库（1 手 = 100 股），显示时格式化，避免"3 万股"显示成 30000。 */
function fmtQty(n) {
  if (n == null) return "";
  const v = Number(n);
  if (!isFinite(v)) return "";
  if (Math.abs(v) >= 10000) return (v / 10000).toFixed(2).replace(/\.00$/, "") + "万股";
  return v.toLocaleString("zh-CN") + "股";
}
function priceCell(p) {
  return `<td class="num">${p == null ? '<span class="s">帖中未披露</span>' : esc(String(p))}</td>`;
}
function qtyCell(q) {
  return `<td class="num">${q == null ? '<span class="s">帖中未披露</span>' : esc(fmtQty(q))}</td>`;
}

/* ---------- 相关人物「历史战绩评分」（2026-09-25 增量） ----------
   王总反馈：原「角色」列（恒为"作者"）零信息量、「最后更新」列对人物档案没意义，
   真正想要的是**这个人的历史战绩 + 一个可排序的分数**。故：
   - 删除「角色」「最后更新」两列；
   - 新增 评分 / 战绩摘要 / 活跃 / 覆盖标的 / 证据 五列；
   - 三档口径：A 实测（满分100）> B 提及（上限60）> C 数据不足（无分，显示「数据不足」而非 0）；
   - 非自然人（资讯号/系统ID/匿名）单独归类、垫底，不与真人混排。
   注意：data-name 属性与行点击（bindRows 里 openPerson）必须保留，不能改坏。 */
const PERSON_TIER_LABEL = { A: "A档·实测", B: "B档·提及", C: "数据不足" };
const PERSON_KIND_LABEL = { "资讯号": "资讯号", "系统ID": "系统ID", "匿名": "匿名" };
const PERSON_EV_CLS = { "交割单": "ev-a", "组合": "ev-b", "晒单": "ev-c", "仅讨论": "ev-d" };

const PERSON_TIER_TIP = {
  A: "A 档·实测（满分 100）：有交割单/持仓记录，证据最强",
  B: "B 档·提及（上限 60）：无成交记录，但有带时间、点名股票的帖子",
  C: "C 档·数据不足：只有无时间、无标指向的帖子，不给分",
};

function personScoreCell(p) {
  // 非自然人（资讯号 / 系统ID / 马甲）不参与评分与排名：即便库里存有技术分，
  // 这里也一律显示「—」，否则用户扫一眼会把「中国平安资讯 78 分」误读成"这是个高分选手"。
  // 注意：只影响展示，数据库里的 score 不动（验收②「score NULL 只在 C 档」继续成立）。
  if (p.is_natural_person === 0) {
    return `<span class="s" title="非自然人，不参与评分与排名">—</span>`;
  }
  const tier = p.score_tier || "C";
  const tip = escAttr(PERSON_TIER_TIP[tier] || "评分证据档位");
  // C 档（无分）显示「数据不足」——绝不显示 0 分：0 会被误读成"表现极差"，与"没数据"是两回事。
  if (p.score == null) return `<span class="chip-s sc-none" title="${tip}">数据不足</span>`;
  const cls = "sc-" + String(tier).replace(/[^AB]/g, "");
  return `<b class="sc-val">${Number(p.score).toFixed(1)}</b> <span class="badge ${cls}" title="${tip}">${esc(PERSON_TIER_LABEL[tier] || tier)}</span>`;
}

function personRecordCell(p) {
  // 本平台实测摘要（唯一数据来源）。
  // 说明：曾有一支「优先显示公开年赛战绩」的分支，但 2026-09-26 QA 复核确认本库人物与公开榜
  // 零交集（person_scores.public_record 恒为 null），那支是**死代码**，已删除，避免后来维护者
  // 误以为这里有数据。公开榜现在单列在页面底部的「公开年赛战绩榜（外部参照）」折叠区块。
  const wr = p.win_rate_20d != null ? p.win_rate_20d : p.win_rate_5d;
  const av = p.avg_ret_20d != null ? p.avg_ret_20d : p.avg_ret_5d;
  if (wr != null || av != null) {
    const horizon = p.win_rate_20d != null ? "20日" : "5日";
    const parts = [];
    if (wr != null) parts.push(`提及后${horizon}胜率 ${Math.round(wr * 100)}%`);
    if (av != null) parts.push(`均涨 ${av >= 0 ? "+" : ""}${(av * 100).toFixed(1)}%`);
    return `<span class="rec-meas">${parts.join(" · ")}</span>`;
  }
  // 退而求其次：给出样本里最好的一笔
  if (p.best_call_ret != null && p.best_call_symbol) {
    const r = Number(p.best_call_ret);
    return `<span class="rec-meas">最佳 ${esc(p.best_call_symbol)} ${r >= 0 ? "+" : ""}${(r * 100).toFixed(1)}%</span>`;
  }
  return `<span class="s">—</span>`;
}

function personActiveCell(p) {
  if (!p.first_seen && !p.last_seen) return `<span class="s">—</span>`;
  const a = p.first_seen || "?", b = p.last_seen || "?";
  // 首末同日时只显示一次，避免「2026-08-22 ~ 2026-08-22」这种冗余
  const range = a === b ? esc(a) : `${esc(a)} ~ ${esc(b)}`;
  return `<span class="s">${range}</span><div class="s">${p.active_days ?? 0} 天</div>`;
}

function personEvidenceBadge(p) {
  const e = p.evidence_level;
  if (!e) return `<span class="s">—</span>`;
  return `<span class="badge ${PERSON_EV_CLS[e] || "ev-d"}">${esc(e)}</span>`;
}

function personRowHtml(p) {
  const plats = Array.isArray(p.sites) && p.sites.length ? p.sites : (p.platform ? [p.platform] : []);
  const isNat = p.is_natural_person === 0;
  const kindTag = (p.person_kind && p.person_kind !== "真人")
    ? `<span class="chip-s pnat-tag">${esc(PERSON_KIND_LABEL[p.person_kind] || p.person_kind)}</span>` : "";
  return `<tr data-name="${escAttr(p.name)}" style="cursor:pointer"${isNat ? ' class="pnat-row"' : ""}>
    <td>${esc(p.name)} ${kindTag}</td>
    <td>${sourceChannel(plats, p.tier)}</td>
    <td class="num">${p.post_count ?? "—"} / ${p.mention_count ?? "—"}</td>
    <td>${personScoreCell(p)}</td>
    <td>${personRecordCell(p)}</td>
    <td>${personActiveCell(p)}</td>
    <td class="num">${p.symbol_count ?? "—"}</td>
    <td>${personEvidenceBadge(p)}</td>
  </tr>`;
}

function personSection(title, arr, open) {
  if (!arr.length) return "";
  const head = `<tr><th>姓名 / 昵称</th><th>来源渠道（平台 · 层级）</th>
    <th class="num">发帖 / 被提及次数</th><th>评分</th><th>战绩摘要</th>
    <th>活跃</th><th class="num">覆盖标的</th><th>证据</th></tr>`;
  return `<details class="psec"${open ? " open" : ""}>
    <summary>${esc(title)}（${arr.length}）</summary>
    <table><thead>${head}</thead><tbody>${arr.map(personRowHtml).join("")}</tbody></table>
  </details>`;
}

function renderPersons(list) {
  const all = toArr(list);
  if (!all.length) { $("#p-persons").innerHTML = `<div class="empty">暂无人物数据</div>`; return; }
  // 排序（这是王总的核心诉求）：① 真人优先、非自然人垫底；② 有分按分从高到低；③ 无分按被提及次数降序
  const sorted = all.slice().sort((a, b) => {
    const na = a.is_natural_person === 0 ? 1 : 0, nb = b.is_natural_person === 0 ? 1 : 0;
    if (na !== nb) return na - nb;
    const sa = a.score == null ? -1 : Number(a.score), sb = b.score == null ? -1 : Number(b.score);
    if (sa !== sb) return sb - sa;
    return (Number(b.mention_count) || 0) - (Number(a.mention_count) || 0);
  });
  const scored = sorted.filter((p) => p.score != null && p.is_natural_person !== 0);
  const unscored = sorted.filter((p) => p.score == null && p.is_natural_person !== 0);
  const nonNat = sorted.filter((p) => p.is_natural_person === 0);
  const pubNote = "本库人物暂无公开战绩来源；下方「公开年赛战绩榜」为外部赛事参照。";
  $("#p-persons").innerHTML = `
    <div class="hint" style="margin-bottom:10px">
      <b>评分口径（三档，证据强度 A &gt; B &gt; C）</b>：
      <b>A 档·实测</b>（满分 100，有交割单/持仓记录）= 实测收益 40 + 记录完整度 20 + 时间跨度 15 + 来源可信 15 + 被跟随度 10；
      <b>B 档·提及</b>（上限 60，帖子带时间且点名股票、但无成交记录）= 提及后 5/20 日胜率 35 + 提及次数 15 + 活跃度 10；
      <b>C 档·数据不足</b>= 只有无时间、无标指向的帖子，<b>不给分</b>（不用活跃度凑一个假分数）。
      <br/>数据来源：本平台已抓取的帖子 / 交割单 / 持仓 + 新浪日线（不复权，5/20 日前瞻涨跌）。${pubNote}
      <br/>提示：这里的 <b>A / B / C 是「评分证据档位」</b>，与第二列「来源渠道」里的 <b>A~F 选手层级</b>不是同一套体系，别混淆。
      <br/><b>评分仅反映平台公开可核验数据，不构成任何投资建议。</b>
    </div>
    ${personSection("🏆 真人榜单（按评分从高到低，A / B 档）", scored, true)}
    ${personSection("📉 真人 · 数据不足（C 档，按被提及次数降序）", unscored, false)}
    ${personSection("🤖 非自然人（资讯号 / 系统ID / 马甲），不参与评分与排名", nonNat, false)}
    <div id="p-persons-extra"></div>`;
  bindRows();
  applyFreshness();
  loadPublicRecords();
}

/* ---------- 公开年赛战绩榜（外部参照 · 非本库人物）----------
   为什么单独成块：这是本库唯一带**真实收益**的公开战绩，正对用户「多少钱做到多少钱」的诉求；
   但它来自淘股吧官方年赛（淘实盘），与上方本库人物零交集，故单列一块、**默认折叠**、不参与本库评分排名。
   折叠交互沿用项目既有的写法（<details class="psec"> + <summary>），保持视觉一致，不自创一套。 */
function renderPublicRecords(rows) {
  const box = $("#p-persons-extra");
  if (!box) return;
  const arr = toArr(rows);
  if (!arr.length) { box.innerHTML = ""; return; }
  // 后端已按昵称去重（每人取「代表条」：优先可核验的原文那一期）并降序；前端再兜一次排序，保证顺序稳定
  const sorted = arr.slice().sort((a, b) => (Number(b.total_return) || 0) - (Number(a.total_return) || 0));
  const body = sorted.map((r) => {
    // 只有「有稳定原文链接且非搜索引擎快照」才渲染外链；快照/空链接一律如实标注，
    // 绝不造一个假链接（用户明确要求：能查到的标注来源，查不到就留空）。
    const src = (r.has_link && isSafeHttpUrl(r.source_url))
      ? `<a href="${escAttr(r.source_url)}" target="_blank" rel="noopener">淘股吧原文</a>`
      : `<span class="s" title="来自搜索引擎结果快照，未找到稳定的原文链接，故不提供跳转">搜索引擎快照 · 无稳定原文链接</span>`;
    const ret = r.total_return != null
      ? `<b class="rec-pub">${Number(r.total_return).toFixed(2)}%</b>`
      : `<span class="s">—</span>`;
    return `<tr><td>${esc(r.nickname)}</td><td>${esc(r.platform || "—")}</td><td>${esc(r.group_name || "—")}</td>`
      + `<td class="num">${ret}</td><td>${esc(r.as_of_date || "—")}</td><td>${src}</td></tr>`;
  }).join("");
  box.innerHTML = `<details class="psec">
    <summary>📊 公开年赛战绩榜（外部参照 · 非本库人物）（${sorted.length}）</summary>
    <div class="hint" style="padding:10px 12px 6px">本榜汇总淘股吧官方年赛（淘实盘）多个交易日的公开排名，每人取一条代表战绩（<b>优先可核验的原文那一期</b>，无原文时退回搜索快照）。名单来自外部赛事，与上方本库人物<b>暂无交集</b>；收益为选手自述并由平台公示，仅供参考，不构成投资建议。</div>
    <table><thead><tr><th>昵称</th><th>平台 / 赛事</th><th>组别</th><th class="num">总收益%</th><th>截至日期</th><th>来源</th></tr></thead>
      <tbody>${body}</tbody></table>
  </details>`;
  injectFreshChip("#p-persons-extra", "public_records");   // P0-3 保鲜角标
}

function loadPublicRecords() {
  // 单独拉取公开战绩：主面板接口 /api/persons 不耦合外部赛事数据；
  // 接口不可用（表缺失等）时静默留空，绝不影响相关人物主面板。
  api("/api/public_records").then(renderPublicRecords).catch(() => {
    const box = $("#p-persons-extra");
    if (box) box.innerHTML = "";
  });
}

function renderSentiment(d) {
  // P3-7：/api/sentiment 若返回数组（类型不符），旧实现会把 Array.prototype.map 当数据用而抛错、
  // 面板全空。这里先做类型校验并降级提示，不抛错。
  if (!d || typeof d !== "object" || Array.isArray(d)) {
    $("#p-sentiment").innerHTML = `<div class="empty">舆论情绪数据格式异常（接口返回类型不符），已降级显示。请检查 /api/sentiment 的返回结构。</div>`;
    return;
  }
  const dist = (d.distribution && typeof d.distribution === "object") ? d.distribution : {};
  const bars = [
    ["看多", dist["看多"] || 0, "bull"], ["看空", dist["看空"] || 0, "bear"], ["中性", dist["中性"] || 0, "neu"],
  ].map(([l, n, c]) => `<div class="bar-card"><div class="bar-val ${c}">${n}</div><div class="s" style="color:var(--muted)">${l}</div></div>`).join("");
  const map = toArr(d.map).map((m) => stockCard(m, "sentiment")).join("");
  const missing = Math.max(0, (Number(d.name_total) || 0) - (Number(d.name_covered) || 0));
  $("#p-sentiment").innerHTML = `<div id="dy-sent-mount"></div>
    <!-- 2026-10-20 进哥：情绪板块里给豆包单独立框 + 在情绪地图上标出来源。
         为什么强调「不混算」：豆包整理的这 69 条是**观点语料**（博主在讲板块/大盘），
         而下方 bars 与情绪地图是**逐帖多空标注**计数（淘股吧/东财帖子）。
         两者口径不同，合成一个数会让"看多多少"变得既不可解释也不可复现。 -->
    <div class="dy-src-legend">
      <span class="dy-src">豆包整理</span>
      <span>本页情绪有<b>两个来源</b>：上方「豆包舆情」框 = 豆包整理的博主观点；下方多空计数与情绪地图 = 论坛帖子实际看多看空。二者<b>不混算</b>。</span>
    </div>
    <div class="bars" style="margin-bottom:16px">${bars}</div>
    <h3 style="margin:6px 0 10px">情绪地图（按股票聚合多空 · 来源：论坛帖子，不含豆包整理内容）</h3>
    ${stockLegend(d)}
    ${missing > 0 ? `<div class="hint" style="margin-bottom:12px">有 <b>${missing}</b> 个代码未取到名称（多为冷门标的或网络未连通）。可点上方「补全股票名称」联网查询；也可在 <code>config/stock_names.yaml</code> 中手工补充，格式：<code>600519: [贵州茅台, 贵州茅台酒股份有限公司]</code>。</div>` : ""}
    <div class="grid">${map || "<div class='empty'>暂无情绪分布</div>"}</div>`;
  bindStockCards("#p-sentiment");
  bindLegendSync();
  // 豆包舆情框（独立模块，异步；失败只影响这一个框，不拖垮本面板其它内容）
  if (window.DOUYIN) window.DOUYIN.renderSentimentBox("#dy-sent-mount");
}

function renderHotspots(data) {
  const box = $("#p-topics");
  if (!box) return;
  const layers = toArr(data && data.layers);
  if (!layers.length) { box.innerHTML = `<div class="empty">暂无热点话题</div>`; return; }
  const m = (data && data.meta) || {};
  const metaHtml = m.title ? `<div class="hs-meta">
      <div class="hs-meta-title">${esc(m.title)}</div>
      <div class="hs-meta-row"><b>核心作用</b>${esc(m.role || "")}</div>
      <div class="hs-meta-row"><b>能带来的价值</b>${esc(m.value || "")}</div>
      <div class="hs-meta-note">${esc(m.caliber || "")}　${esc(m.legend || "")}</div>
    </div>` : "";
  const layersHtml = layers.map((L, li) =>
    `<section class="hs-layer">
      <div class="hs-layer-hd"><span class="hs-layer-idx">${li + 1}</span><span class="hs-layer-title">${esc(L.title)}</span><span class="hs-layer-sub">${esc(L.subtitle || "")}</span></div>
      <div class="grid">${toArr(L.items).map((it, ii) => hsCard(it, li * 100 + ii)).join("")}</div>
    </section>`).join("");
  box.innerHTML = metaHtml + layersHtml;
  bindHotspotCards(box);
}

function hsKindLabel(k) {
  return ({ policy: "政策", industry: "行业主题", rhythm: "交易节奏", risk: "风险预警", unknown: "未分类" }[k]) || "热词";
}

function hsSectors(list, dir) {
  const arr = toArr(list);
  if (!arr.length) return "";
  const label = dir === "up" ? "上涨机会" : "情绪回落 / 承压";
  const rows = arr.map((x) => {
    // 2026-09-26：板块下的标的补上详情入口。数据有两种形态 ——
    //   ① 纯代码字符串（"600036"）  ② 对象（{code,name}）
    // 统一归一后，凡是 6 位 A 股代码就挂 data-sym，点开看 K 线 / 估值 / 盈利。
    const syms = toArr(x.symbols).map((c) => {
      const isObj = c && typeof c === "object";
      const code = String((isObj ? c.code : c) || "").trim();
      const nm = isObj ? String(c.name || "") : "";
      const txt = nm ? `${esc(nm)} <span class="stk-mini-c">${esc(code)}</span>` : esc(code);
      return /^\d{6}$/.test(code)
        ? `<span class="stk-mini stk-click" data-sym="${escAttr(code)}" title="${escAttr((nm || code) + " —— 点开看 K 线走势 / 估值(PE·PB) / 盈利(ROE)")}">${txt}</span>`
        : `<span class="stk-mini">${txt}</span>`;
    }).join("");
    const symPart = syms ? `<span class="hs-sec-syms">${syms}</span>` : `<span class="hs-sec-syms s">待映射</span>`;
    return `<div class="hs-sec-row">
        <div class="hs-sec-name">${esc(x.sector || "")}</div>
        <div class="hs-sec-meta">核心作用：${esc(x.role || "")}　·　价值：${esc(x.value || "")}</div>
        <div class="hs-sec-syms-wrap">${symPart}</div>
      </div>`;
  }).join("");
  return `<div class="hs-block hs-fc ${dir}"><span class="hs-sub">${label}<span class="hs-legend">（${dir === "up" ? "红=预判上涨" : "绿=预判承压"}，A 股惯例）</span></span><div class="hs-sec">${rows}</div></div>`;
}

function hsCard(it, idx) {
  const ev = it.evidence || {};
  const kindBadge = it.judgment
    ? `<span class="badge hs-kind hs-kind-${escAttr(it.kind)}">${hsKindLabel(it.kind)}</span>`
    : `<span class="badge hs-kind hs-nojudge">暂无板块判断</span>`;
  const evBadge = ev.posts
    ? `<span class="badge hs-ev" title="点击查看原始讨论">证据 ${ev.posts} 帖 · ${ev.authors} 人 · 多空 ${ev.bull}:${ev.bear}</span>`
    : `<span class="badge hs-ev hs-ev-zero">证据 0 帖 · 样本少</span>`;
  const meaning = it.meaning ? `<div class="hs-meaning">${esc(it.meaning)}</div>` : "";
  const up = hsSectors(it.up, "up");
  const down = hsSectors(it.down, "down");
  const trans = it.transmission ? `<div class="hs-block"><span class="hs-sub">传导路径</span><div class="hs-text">${esc(it.transmission)}</div></div>` : "";
  const dec = it.decision ? `<div class="hs-decision"><span class="hs-dec-tag">结论</span><div class="hs-dec-txt">${esc(it.decision)}</div></div>` : "";
  const syms = toArr(ev.symbols).map((s) =>
    `<span class="stk" data-sym="${escAttr(s.code)}" title="点开看该标的详情">${esc(s.name || s.code)}<span class="stk-c">${esc(s.code)}</span></span>`).join("");
  const symRow = syms ? `<div class="hs-syms"><span class="s">涉及标的：</span>${syms}</div>` : "";
  return `<div class="hs-card ${it.judgment ? "" : "hs-nojudge"}" data-topic="${escAttr(it.topic)}">
    <div class="hs-head"><span class="hs-idx">#${idx + 1}</span><span class="hs-topic">${esc(it.topic)}</span>${kindBadge}${evBadge}</div>
    ${meaning}${up}${down}${trans}${dec}${symRow}
  </div>`;
}

function bindHotspotCards(box) {
  box.querySelectorAll(".stk[data-sym], .stk-mini[data-sym]").forEach((el) => {
    el.style.cursor = "pointer";
    el.onclick = () => openStock(el.getAttribute("data-sym"));
  });
  box.querySelectorAll(".hs-ev").forEach((el) => {
    el.style.cursor = "pointer";
    el.onclick = () => openPostsByKeyword(el.closest(".hs-card").getAttribute("data-topic"));
  });
}

function openPostsByKeyword(topic) {
  if (!topic) return;
  api("/api/posts?limit=500").then((list) => {
    const arr = toArr(list).filter((p) => (p.text || "").indexOf(topic) >= 0);
    const rows = arr.slice(0, 200).map((p) =>
      `<div class="dr"><div class="dr-top"><b>${esc(p.author || "")}</b> <span class="s">${esc(fmtTimeText(p.post_time, ""))}</span> ${sentBadge(p.sentiment)}</div><div class="dr-txt">${esc((p.text || "").slice(0, 200))}</div></div>`).join("");
    openDrawer(`讨论「${topic}」的原始帖子（命中 ${arr.length} 条）`,
      `<div class="hint">事实层：以下为命中「${esc(topic)}」的真实帖子（前 200 条）。</div>` + (rows || '<div class="s">无</div>'));
  }).catch((e) => setHint("加载失败：" + e));
}

/* ---------- 实盘选手（含渠道标注 A/B/C/D + 层级 A~F）---------- */
const CHANNEL_LABEL = {
  A: "A 实盘大赛", B: "B 直播高手", C: "C 专栏作者", D: "D 人气榜",
  G: "G 社区晒盘",
};
function channelBadge(ch) {
  // P2-9：channel 缺失时不渲染 "渠道 undefined"
  const txt = CHANNEL_LABEL[ch] || ((ch == null || ch === "") ? "渠道未标注" : ("渠道 " + ch));
  return `<span class="badge ch-${escAttr(ch || 'A')}">${esc(txt)}</span>`;
}
/* 层级体系 A~F：A/B/C/D 沿用原渠道规则；E=主播大V、F=长尾观察（低层级权重降低，
   但 is_visible 恒为 1，一律露出，不因热度低而隐藏）。 */
const TIER_LABEL = {
  A: "A 实盘大赛", B: "B 直播高手", C: "C 专栏作者",
  D: "D 人气榜", E: "E 主播大V", F: "F 长尾观察",
};
function tierBadge(t) {
  // P2-9：tier 缺失时不渲染 "层级 undefined"
  const txt = TIER_LABEL[t] || ((t == null || t === "") ? "层级未标注" : ("层级 " + t));
  return `<span class="badge t-${escAttr(t || 'D')}">${esc(txt)}</span>`;
}
/* ---------- 来源渠道（平台）标注：为每个名称标注「所属/来源的具体渠道」---------- */
const SITE_LABEL = { taoguba: "淘股吧", eastmoney: "东方财富", xueqiu: "雪球", ths: "同花顺", sina: "新浪股吧", jisilu: "集思录", moer: "摩尔金融" };
// 持仓来源性质（后端按 site 推导）：实盘 = 选手主页实盘记录；组合晒单 = 集思录/摩尔；其余 = 讨论页提及
const SRC_TYPE_LABEL = { player_page: "选手主页实盘", player_live: "选手主页实盘", community: "组合晒单", bar_post: "个股讨论页提及" };
function siteLabel(s) { return SITE_LABEL[s] || s || "未知"; }
function siteBadge(s) {
  if (!s) return "";
  return `<span class="badge p-${escAttr(s)}">${esc(siteLabel(s))}</span>`;
}
/* 来源渠道单元格：平台徽章（名称来自哪个论坛）+ 所属层级（A~F，入选选手才有） */
function sourceChannel(platforms, tier) {
  const plats = toArr(platforms).filter(Boolean);
  const parts = plats.length ? plats.map(siteBadge).join(" ") : `<span class="chip-s">来源未知</span>`;
  return parts + (tier ? " " + tierBadge(tier) : "");
}

/* ---------- 来源链接：只有真实可打开的链接才做成可点入口 ----------
   示例（demo/seed）数据的 URL 是程序内置的占位域名（example.local 等，公网 DNS 不解析），
   点开必然“网页无法显示”。此处统一降级为不可点击文本 +「示例」标记，
   避免假链接冒充真实出处；真实来源继续保持可点跳转。 */
const PLACEHOLDER_HOSTS = ["example.local", "example.com", "example.org", "example.net", "localhost", "127.0.0.1", "0.0.0.0"];
/* P2-1：显式拒绝危险协议（javascript: / data: / vbscript: / file:）——即使未来放宽
   「非 http(s) 不可点」的规则，也不会把可执行协议写进 href。 */
const DANGEROUS_URL_RE = /^\s*(?:javascript|data|vbscript|file)\s*:/i;
function isSafeHttpUrl(u) {
  const s = String(u == null ? "" : u).trim();
  if (DANGEROUS_URL_RE.test(s)) return false;
  return /^https?:\/\//i.test(s);
}
function isPlaceholderUrl(u) {
  const s = String(u == null ? "" : u).trim();
  if (!isSafeHttpUrl(s)) return true;                     // 空值 / 危险协议 / 非 http(s) 一律视为不可跳转
  try {
    const h = new URL(s).hostname.toLowerCase();
    return PLACEHOLDER_HOSTS.some((p) => h === p || h.endsWith("." + p));
  } catch (e) { return true; }
}
/* 真实链接 → 新窗口打开；示例/空 → 纯文本 +「示例」灰标（鼠标悬停有说明） */
function srcLink(url, text, opts) {
  const o = opts || {};
  const label = text != null && text !== "" ? text : (url || "");
  if (isPlaceholderUrl(url)) {
    return (label ? `<span class="src-demo-text" title="该条为程序内置示例数据，无真实出处，故不提供跳转">${esc(label)}</span>` : "")
      + `<span class="chip-demo" title="示例数据，无真实出处，不可跳转">示例</span>`;
  }
  return `<a href="${escAttr(url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${esc(label)}${o.arrow === false ? "" : "↗"}</a>`;
}

/* ---------- 来源列：站点名 + 条数（可展开原始链接） ----------
   2026-10-06 修订（王总实测反馈："来源那列全是斜向上的箭头，看不懂"）：
   原先「来源」列把每条出处渲染成一个孤零零的 ↗，一只股票十几位选手 → 十几 ↗ 挤在一格，
   既看不出出处在哪个站、也不知道有几条，视觉上就是一片无意义的斜箭头。
   改为：① 按站点归并成「淘股吧 8 / 东财 2」的站点徽章（含条数，悬停看前几条链接）；
        ② 后接「出处 N 条 ▾」按钮，点击在格内展开全部原始链接（真实可点，不做假链接）。
   调用链单向：srcCellHtml / toggleSrcList 只读写自己的 DOM，不反向触发任何渲染函数。 */
const SITE_HOSTS = [
  ["taoguba", ["tgb.cn", "taoguba.com.cn"]],
  ["eastmoney", ["eastmoney.com", "eastmoney.net"]],
  ["xueqiu", ["xueqiu.com"]],
  ["ths", ["10jqka.com.cn", "10jqka.com", "ths.com"]],
  ["jisilu", ["jisilu.cn"]],
  ["moer", ["moer.money", "moer.cn"]],
  ["sina", ["sina.com.cn", "sina.com"]],
];
/* URL → 站点 key（与 SITE_LABEL 同源，便于复用 p-* 配色与中文名） */
function siteKeyOfUrl(u) {
  let h = "";
  try { h = new URL(String(u == null ? "" : u)).hostname.toLowerCase(); } catch (e) { return ""; }
  for (const pair of SITE_HOSTS) {
    const key = pair[0];
    if (pair[1].some((p) => h === p || h.endsWith("." + p))) return key;
  }
  return h;   // 未收录站点：直接暴露主机名，至少可读（不编造站名）
}
/* 来源单元格：站点徽章组 + 展开按钮 + 折叠的链接清单 */
function srcCellHtml(urls) {
  const arr = toArr(urls).filter(Boolean);
  if (!arr.length) return `<span class="chip-s">—</span>`;
  const bySite = new Map();
  arr.forEach((u) => {
    const k = siteKeyOfUrl(u);
    if (!bySite.has(k)) bySite.set(k, []);
    bySite.get(k).push(u);
  });
  const badges = [];
  bySite.forEach((list, k) => {
    const tip = "共 " + list.length + " 条，例如：\n" +
      list.slice(0, 3).map((u) => String(u).slice(0, 110)).join("\n") + (list.length > 3 ? "\n…" : "");
    const cls = /^[A-Za-z0-9_-]+$/.test(k) ? `badge p-${escAttr(k)}` : "badge";
    badges.push(`<span class="${cls}" title="${escAttr(tip)}">${esc(siteLabel(k))} <b>${list.length}</b></span>`);
  });
  const raw = escAttr(JSON.stringify(arr));
  return `<div class="src-cell">${badges.join(" ")}</div>` +
    `<button type="button" class="src-more" data-srcs="${raw}" onclick="toggleSrcList(this)">出处 ${arr.length} 条 ▾</button>` +
    `<div class="src-list" hidden></div>`;
}
/* 展开/收起该行的原始出处清单（懒填充：第一次点才生成，避免每行都塞长 HTML） */
function toggleSrcList(btn) {
  if (!btn) return;
  const list = btn.nextElementSibling;        // 紧随其后的 .src-list
  if (!list || !list.classList.contains("src-list")) return;
  const urls = toArr(safeJsonArr(btn.dataset.srcs || ""));
  if (!list.dataset.filled) {
    list.dataset.filled = "1";
    // 每行一条、截断显示（完整 URL 放 title）：一行一条比"整条长 URL 折成两三行"容易扫得多
    list.innerHTML = urls.length
      ? urls.map((u, i) => {
        const raw = String(u == null ? "" : u);
        const lab = raw.replace(/^https?:\/\//i, "").replace(/^www\./, "") || raw;
        const short = lab.length > 62 ? lab.slice(0, 62) + "…" : lab;
        const a = isPlaceholderUrl(raw)
          ? `<span class="src-demo-text" title="程序内置示例数据，无真实出处，故不提供跳转">${esc(short)}</span>`
          : `<a href="${escAttr(raw)}" target="_blank" rel="noopener noreferrer" title="${escAttr(raw)}">${esc(short)} ↗</a>`;
        return `<div class="src-item"><span class="src-n">${i + 1}.</span> ${a}</div>`;
      }).join("")
      : `<div class="src-item chip-s">（该行无原始链接）</div>`;
  }
  const willOpen = list.hidden;
  list.hidden = !willOpen;
  btn.classList.toggle("open", willOpen);
  btn.textContent = `出处 ${urls.length} 条 ${willOpen ? "▴" : "▾"}`;
  // 行点击已由 bindCohold / bindConsensus 排除 .src-more / .src-list，此处不会再穿透触发 openStock
}
/* ================= 游资龙虎榜（东财 datacenter 免鉴权接口，T日盘后披露） ================= */
function amtYi(v) {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (a >= 1e4) return (v / 1e4).toFixed(0) + "万";
  return v.toFixed(0);
}
const LHB = { data: null, sel: null, selDept: null };
function loadLhb(date, windowDays) {
  const q = [];
  if (date) q.push("date=" + encodeURIComponent(date));
  if (windowDays) q.push("window_days=" + windowDays);
  return api("/api/lhb" + (q.length ? "?" + q.join("&") : ""))
    .then((d) => { LHB.data = d; LHB.sel = null; LHB.selDept = null; renderLhb(); });
}
/* 涨跌色：必须用 .bull/.bear —— app.css 里只有这两个是全局红/绿（A 股口径 红涨绿跌）。
   原先这里写的是 .up/.down，而 .up/.down 只对个别元素作用域生效，导致颜色一直没显示出来。*/
function lhbPct(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return `<td class="num">—</td>`;
  const n = Number(v);
  return `<td class="num ${n >= 0 ? "bull" : "bear"}">${n >= 0 ? "+" : ""}${n.toFixed(d)}%</td>`;
}
function lhbRate(v) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toFixed(0) + "%";
}
function lhbSymChip(code) {
  return `<span class="kline-chip chip-s" data-sym="${escAttr(code)}" title="点击看K线">${esc(code)}</span>`;
}
function renderLhb() {
  const box = $("#p-lhb");
  if (!box) return;
  const d = LHB.data;
  if (!d || d.empty || !(d.stocks || []).length) {
    box.innerHTML = `<div class="empty">暂无龙虎榜数据。请先运行 lhb_fetch.py 抓取（沪深交易所盘后披露，东方财富接口转发）。</div>`;
    return;
  }
  const seatsBy = {};
  (d.seats || []).forEach((s) => { (seatsBy[s.security_code] = seatsBy[s.security_code] || []).push(s); });
  const chips = dayChips(d.dates, d.date, "data-lhb-date");   // 只渲染近 10 个，其余走日期查询
  const instNet = (d.inst_buy_net || 0) - (d.inst_sell_net || 0);

  const rows = (d.stocks || []).map((s) => {
    const on = LHB.sel === s.security_code;
    const chg = s.change_rate != null ? s.change_rate.toFixed(2) : "—";
    const cls = s.change_rate != null ? (s.change_rate >= 0 ? "bull" : "bear") : "";
    return `<tr data-lhb-stock="${escAttr(s.security_code)}" style="cursor:pointer;${on ? "background:rgba(14,116,144,.12);" : ""}">
      <td>${lhbSymChip(s.security_code)}</td>
      <td><b>${esc(s.security_name || "")}</b></td>
      <td class="${cls}">${chg}%</td>
      <td class="num">${s.close_price != null ? s.close_price.toFixed(2) : "—"}</td>
      <td class="num"><b>${amtYi(s.net_amt)}</b></td>
      <td class="num">${amtYi(s.buy_amt)}</td>
      <td class="num">${amtYi(s.sell_amt)}</td>
      <td class="num">${s.turnoverrate != null ? s.turnoverrate.toFixed(1) + "%" : "—"}</td>
      ${lhbPct(s.d1_chg)}
      ${lhbPct(s.d5_chg)}
      <td class="s">${esc((s.reason || "").replace("日", ""))}</td>
    </tr>`;
  }).join("");

  let seatHtml = `<div class="s dim">点击上表任意个股，查看买卖前 5 席位明细。</div>`;
  if (LHB.sel) {
    const seats = seatsBy[LHB.sel] || [];
    const buyRows = seats.filter((s) => s.side === "BUY").sort((a, b) => (b.net || 0) - (a.net || 0)).slice(0, 5)
      .map((s, i) => `<tr><td class="num">${i + 1}</td><td>${esc(s.operate_name || "")}</td>
        <td class="num">${amtYi(s.buy)}</td><td class="num">${amtYi(s.net)}</td>
        <td class="num">${s.rise_prob_3d != null ? s.rise_prob_3d.toFixed(1) + "%" : "—"}</td></tr>`).join("");
    const sellRows = seats.filter((s) => s.side === "SELL").sort((a, b) => (b.net || 0) - (a.net || 0)).slice(0, 5)
      .map((s, i) => `<tr><td class="num">${i + 1}</td><td>${esc(s.operate_name || "")}</td>
        <td class="num">${amtYi(s.sell)}</td><td class="num">${amtYi(s.net)}</td>
        <td class="num">${s.rise_prob_3d != null ? s.rise_prob_3d.toFixed(1) + "%" : "—"}</td></tr>`).join("");
    seatHtml = `<div class="panel-split" style="margin-top:12px;gap:16px;display:grid;grid-template-columns:1fr 1fr">
      <div><b>买入前 5 席位</b><table><thead><tr><th>#</th><th>营业部/机构</th><th class="num">买入</th><th class="num">净额</th><th class="num">3日胜率</th></tr></thead>
      <tbody>${buyRows || "<tr><td colspan=5 class='empty'>无</td></tr>"}</tbody></table></div>
      <div><b>卖出前 5 席位</b><table><thead><tr><th>#</th><th>营业部/机构</th><th class="num">卖出</th><th class="num">净额</th><th class="num">3日胜率</th></tr></thead>
      <tbody>${sellRows || "<tr><td colspan=5 class='empty'>无</td></tr>"}</tbody></table></div></div>`;
  }

  // ---------- 游资席位榜（以营业部为轴，滚动窗口）----------
  const depts = d.depts_top || [];
  const deptRows = depts.map((x, i) => {
    const on = LHB.selDept === x.operate_name;
    return `<tr data-lhb-dept="${escAttr(x.operate_name)}" style="cursor:pointer;${on ? "background:rgba(14,116,144,.12);" : ""}">
      <td class="num">${i + 1}</td>
      <td>${esc(x.operate_name)}</td>
      <td class="num">${x.appear}</td>
      <td class="num">${x.stock_count}</td>
      <td class="num"><b>${amtYi(x.net)}</b></td>
      <td class="num">${lhbRate(x.win1)}</td>
      <td class="num">${lhbRate(x.win3)}</td>
      ${lhbPct(x.avg3)}
    </tr>`;
  }).join("");
  let deptDetail = "";
  if (LHB.selDept) {
    const x = depts.find((y) => y.operate_name === LHB.selDept);
    if (x) {
      const sr = (x.top_stocks || []).map((s) => `<tr>
        <td>${lhbSymChip(s.code)}</td><td>${esc(s.name || "")}</td>
        <td class="num">${amtYi(s.net)}</td><td class="num">${s.days || 1}</td>${lhbPct(s.d3_chg)}</tr>`).join("");
      deptDetail = `<div style="margin-top:8px"><b>${esc(x.operate_name)}</b>
        <span class="s dim"> · 窗口内净买前 ${(x.top_stocks || []).length} 只</span>
        <table><thead><tr><th>代码</th><th>名称</th><th class="num">净买</th><th class="num">上榜天数</th><th class="num">D3涨幅</th></tr></thead>
        <tbody>${sr || "<tr><td colspan=5 class='empty'>无</td></tr>"}</tbody></table></div>`;
    }
  }

  // ---------- 机构动向 ----------
  const inst = d.inst_top || [];
  const instRows = inst.map((x) => `<tr>
    <td>${lhbSymChip(x.security_code)}</td>
    <td><b>${esc(x.security_name || "")}</b></td>
    <td class="num"><b>${amtYi(x.net)}</b></td>
    <td class="num">${x.buy_times || 0}</td>
    <td class="num">${x.sell_times || 0}</td>
    <td class="num">${x.ratio == null ? "—" : Number(x.ratio).toFixed(2) + "%"}</td>
    ${lhbPct(x.avg3)}
  </tr>`).join("");

  const wd = d.window_days || 5;
  const wdates = d.window_dates || [];
  const winTxt = wdates.length ? `${wdates[wdates.length - 1]} ~ ${wdates[0]}` : "";

  box.innerHTML = `
    <div style="margin-bottom:10px">${dataDayBadge(d.date)}<b>交易日</b>：${chips}</div>
    <div class="s" style="margin-bottom:10px">上榜 <b>${(d.stocks || []).length}</b> 行 · 机构席位净买 <b>${amtYi(instNet)}</b> ·
      「龙虎榜净买」= 该股榜内买入合计 − 卖出合计（交易所披露口径）；「榜后D1/D5」= 上榜后 1/5 个交易日的实际涨跌幅（最新上榜日尚未走完，显示 —）；
      同一只股票若触发多个上榜标准会按原因分行列出（非重复）。点击个股看买卖前 5 席位，点击营业部看它在扫哪些股。数据源：东方财富 datacenter（免鉴权）。</div>
    <table><thead><tr><th>代码</th><th>名称</th><th>涨跌幅</th><th class="num">收盘</th>
      <th class="num">龙虎榜净买</th><th class="num">买入合计</th><th class="num">卖出合计</th>
      <th class="num">换手</th><th class="num">榜后D1</th><th class="num">榜后D5</th><th>上榜原因</th></tr></thead><tbody>${rows}</tbody></table>
    ${seatHtml}
    <div class="detail-sub" style="margin-top:18px">游资席位榜 —— 以「营业部为轴」滚动近 ${wd} 个交易日（${winTxt}），按净买额排序；
      D1/D3 胜率＝该席位窗口内买入个股在榜后的实际上涨占比</div>
    <table><thead><tr><th>#</th><th>营业部 / 机构专用席位</th><th class="num">上榜次数</th><th class="num">涉及股数</th>
      <th class="num">净买额</th><th class="num">D1胜率</th><th class="num">D3胜率</th><th class="num">D3均涨</th></tr></thead>
      <tbody>${deptRows || "<tr><td colspan=8 class='empty'>无（请先抓取 lhb_depts）</td></tr>"}</tbody></table>
    ${deptDetail}
    <div class="detail-sub" style="margin-top:18px">机构动向 —— 机构专用席位在哪些股上买/卖（滚动近 ${wd} 个交易日），按机构净买额排序</div>
    <table><thead><tr><th>代码</th><th>名称</th><th class="num">机构净买</th><th class="num">买入次数</th>
      <th class="num">卖出次数</th><th class="num">占成交比</th><th class="num">D3均涨</th></tr></thead>
      <tbody>${instRows || "<tr><td colspan=7 class='empty'>无（请先抓取 lhb_inst）</td></tr>"}</tbody></table>`;

  $$("#p-lhb [data-lhb-date]").forEach((el) => {
    el.onclick = () => loadLhb(el.dataset.lhbDate).catch((e) => setHint("龙虎榜日期切换失败：" + apiErrText(e)));
  });
  // 2026-10-12 进哥要求：交易日只显示近 10 个，其余用日期框查询（见 dayChips）。
  // 超出库内范围时给明确提示并回退，不静默失败。
  const _lhbJump = $("#p-lhb .day-jump");
  if (_lhbJump) {
    _lhbJump.onchange = () => {
      const v = _lhbJump.value;
      const days = (_lhbJump.dataset.days || "").split(",");
      if (!v || !days.includes(v)) {
        setHint("该日不是库内交易日。可选范围：" + (days[0] || "—") + " ~ " + (days[days.length - 1] || "—"));
        _lhbJump.value = d.date || "";
        return;
      }
      loadLhb(v).catch((e) => setHint("龙虎榜日期切换失败：" + apiErrText(e)));
    };
  }
  // 2026-09-28 修复「点代码没反应」（王总反馈）：代码芯片 lhbSymChip 此前**从未被绑定** ——
  // 下面行 handler 写着「让 K 线芯片自己处理」，可芯片本身没有任何 handler，
  // 于是点代码时行 handler 直接 return，什么都不会发生；而芯片样式与其他面板的
  // 可点芯片完全一致（.kline-chip.chip-s），所以"看着能点、点了不动"。
  // 口径统一为 openStock：芯片点开完整个股详情（重要信息 / K 线 / 持仓选手）。
  // stopPropagation 是必须的，否则会连带触发行选中（展开席位明细）抢走焦点。
  $$("#p-lhb .kline-chip[data-sym]").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); openStock(el.dataset.sym, el); };
  });
  $$("#p-lhb tr[data-lhb-stock]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest(".kline-chip")) return; // 已由上面的芯片 handler 接管（仅作兜底）
      LHB.sel = LHB.sel === tr.dataset.lhbStock ? null : tr.dataset.lhbStock;
      renderLhb();
    };
  });
  $$("#p-lhb tr[data-lhb-dept]").forEach((tr) => {
    tr.onclick = () => {
      LHB.selDept = LHB.selDept === tr.dataset.lhbDept ? null : tr.dataset.lhbDept;
      renderLhb();
    };
  });
}

/* ================= 龙虎榜 · 板块动向（与个股榜口径完全独立） =================
   回答的不是「哪只票被买」，而是「资金是不是成片扫某个方向」。
   板块聚集度（上榜股数 + 净买额）才有前瞻价值；扩散幅度只作方向印证，不作交易信号。
   数据由 lhb_sector_link.py 离线算出后落库（lhb_sector_focus），本模块只读、不重算。 */
/* ================= 牛散追踪 · 股东协同 =================
   对标「看究竟·股东协同」/ Dataroma 共识持仓。
   数据源：上市公司定期报告/临时公告披露的自然人十大股东（东财 F10），
   由 holders_fetch.py 抓取后落库（stock_holders）。本模块只读、不重算。
   两个视角：① 牛散榜（跨多只股建仓的自然人）② 协同股（多名牛散同持一只股）。 */
const NIUSAN = { data: null, scope: "free", minHold: 2, view: "rank" };
function loadNiusan() {
  const q = "?scope=" + encodeURIComponent(NIUSAN.scope) + "&min_hold=" + NIUSAN.minHold;
  return api("/api/niusan" + q).then((d) => { NIUSAN.data = d; renderNiusan(); });
}
/* 牛散追踪（合并「牛散榜 + 变动流」）：同一群体（跨多只股建仓的自然人），两个视角 */
function renderNiusan() {
  const box = $("#p-niusan");
  if (!box) return;
  const seg = (v, t) => `<span class="chip-s" role="button" data-ns-view="${escAttr(v)}" ` +
    `style="cursor:pointer;${v === NIUSAN.view ? "background:var(--accent,#0E7490);color:#fff;" : ""}">${esc(t)}</span>`;
  const head = `<div class="hint" style="margin-bottom:10px">
      数据源：上市公司<b>定期报告 / 临时公告</b>披露的十大股东（东财 F10，交易所强制披露，免费公开）。
      <b>已做风险隔离</b>：实控人、家族同持（父子/夫妻等）不计入牛散口径。
      <br>视角切换：${seg("rank", "① 牛散榜（谁在买）")} ${seg("moves", "② 变动流（本期买/卖了什么）")}
    </div>
    <div id="nsBody"></div>`;
  box.innerHTML = head;
  $$("#p-niusan [data-ns-view]").forEach((el) => {
    el.onclick = () => {
      NIUSAN.view = el.dataset.nsView;
      renderNiusan();
      if (NIUSAN.view === "moves" && !STARM.data) {
        loadStarMoves().catch((e) => reportLoadError("牛散动向", e));
      }
    };
  });
  const body = $("#nsBody");
  if (!body) return;
  if (NIUSAN.view === "moves") renderNsMoves(body);
  else renderNsRank(body);
}
function renderNsRank(box) {
  const d = NIUSAN.data;
  if (!d) { box.innerHTML = `<div class="empty">牛散数据加载中…</div>`; return; }
  const s = d.summary || {};
  if (!(d.investors || []).length && !(d.cohold || []).length) {
    box.innerHTML = `<div class="empty">暂无股东数据。该板块由 holders_fetch.py 抓取上市公司披露的自然人股东后生成。</div>`;
    return;
  }
  const seg = (v, t) => `<span class="chip-s" role="button" data-ns-scope="${escAttr(v)}" ` +
    `style="cursor:pointer;${v === NIUSAN.scope ? "background:var(--accent,#0E7490);color:#fff;" : ""}">${esc(t)}</span>`;
  const inv = (d.investors || []).map((p) => {
    const holds = (p.holdings || []).map((x) => {
      const tip = (x.symbol_name || "") + " " + x.symbol +
        " | 占流通 " + (x.ratio == null ? "—" : x.ratio + "%") +
        " | " + (x.change || "变动未披露") + " | 披露期 " + (x.end_date || "—");
      return `<span class="kline-chip chip-s" data-sym="${escAttr(x.symbol)}" style="margin:1px 3px 1px 0" ` +
        `title="${escAttr(tip)} 点击看详情">${esc(x.symbol_name || x.symbol)}</span>`;
    }).join("");
    return `<tr>
      <td><b>${esc(p.name)}</b></td>
      <td class="num"><b>${p.stock_count}</b> 只</td>
      <td class="num">${(p.total_ratio || 0).toFixed(2)}%</td>
      <td class="num">${p.new_cnt ? `<span class="bull">${p.new_cnt}</span>` : "—"}</td>
      <td class="num">${p.cut_cnt ? `<span class="bear">${p.cut_cnt}</span>` : "—"}</td>
      <td>${holds || "—"}</td>
    </tr>`;
  }).join("");
  const coh = (d.cohold || []).map((c) => {
    const ps = (c.people || []).map((x) =>
      `<span class="chip-s" style="margin:1px 3px 1px 0">${esc(x.name)}` +
      `${x.ratio == null ? "" : " " + x.ratio.toFixed(2) + "%"}</span>`).join("");
    return `<tr>
      <td>${lhbSymChip(c.symbol)}</td>
      <td><b>${esc(c.symbol_name || "")}</b></td>
      <td class="num"><b class="bull">${c.people_count}</b> 位</td>
      <td class="num">${(c.star_ratio_sum || 0).toFixed(2)}%</td>
      <td>${ps}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <div class="s" style="margin-bottom:10px">
      当前口径 <b>${esc(s.scope_label || "")}</b> · 最新披露期 <b>${esc(s.latest_date || "—")}</b> ·
      自然人 <b>${s.person_total || 0}</b> 名 · 覆盖 <b>${s.stock_total || 0}</b> 只股。
      <b>披露有时滞</b>（季报约 1 个月、临时公告约 1-2 日），仅作方向参考，不构成交易信号。
      <br>口径切换：${seg("free", "十大流通股东")} ${seg("total", "十大股东")}
    </div>
    <div class="detail-sub">① 牛散榜 —— 跨 ≥${s.min_hold || 2} 只股的自然人（按持股只数排序；点击股票名看详情）</div>
    ${inv ? `<table><thead><tr><th>牛散</th><th class="num">持股数</th><th class="num">合计占流通</th>
      <th class="num">新进</th><th class="num">减持</th><th>持仓明细</th></tr></thead><tbody>${inv}</tbody></table>`
      : "<span class='s'>暂无符合条件的牛散</span>"}
    <div class="detail-sub">② 股东协同 —— 多名<b>牛散</b>同时持有同一只股（已剔除家族 / 创始团队持股）</div>
    ${coh ? `<table><thead><tr><th>代码</th><th>名称</th><th class="num">牛散数</th>
      <th class="num">合计占流通</th><th>牛散名单</th></tr></thead><tbody>${coh}</tbody></table>`
      : "<span class='s'>暂无牛散协同持仓</span>"}
    <div class="ks-src" style="margin-top:10px">${esc(d.basis || "")}</div>`;
  $$("#nsBody [data-ns-scope]").forEach((el) => {
    el.onclick = () => {
      NIUSAN.scope = el.dataset.nsScope;
      loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
    };
  });
  $$("#nsBody .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}

const LBHS = { data: null };
function loadLhbSectors(date) {
  const q = date ? "?date=" + encodeURIComponent(date) : "";
  return api("/api/lhb_sectors" + q).then((d) => { LBHS.data = d; renderLhbSectors(); });
}
/* ---------- 今日看板（2026-09-25 新增）：决策入口 ----------
 * 组织维度是「今天该看什么」而非「数据从哪来」：
 *   ① 资金主线（龙虎榜最新日：个股净买 / 机构 / 营业部）
 *   ② 板块多点共振（同一天多个票上榜的题材方向 + T+1/T+3 验证）
 *   ③ 牛散共识（多位牛散同持的股）
 *   ④ 数据新鲜度（一眼看出哪条数据陈旧）
 */
const DASH = { data: null };

function loadDashboard() {
  // 2026-10-25：这里原来用默认 15s 预算，而 dashboard 冷算要 32s —— 首屏必超时。
  // 现在后端过期即回快照（d._stale + d._cache_age），正常是毫秒级；长预算只兜冷启动。
  return api("/api/dashboard", { label: "今日看板", timeout: _API_TIMEOUT_HEAVY })
    .then((d) => {
      DASH.data = d;
      renderDashboard();
      if (d && d._stale) {
        const m = Math.max(1, Math.round((d._cache_age || 0) / 60));
        showToast(`今日看板：先显示 ${m} 分钟前的快照，后台正在刷新，稍后自动更新。`);
      }
    });
}

/* ================= 今日决策首页（2026-10-12 体验审计 P0-A） =================
   报告《查看器体验审计与改造方案》第 0 步：把「今日看板」里已有的**一句话结论**写法
   推广成全站首页 —— 打开软件一屏只答四件事：① 今天该盯哪个方向 ② 哪些票能上车
   ③ 哪里在退潮/要躲 ④ 我自己的关注池今天有什么动静。不用再进二级菜单。
   数据全部来自后端 /api/today_decision（复用既有口径，不新造引擎），前端只负责摆版式。 */
const TODAY = { data: null };
// 2026-10-15：近期热点（/api/hotspots）从 loadAll 首屏挪为「进入面板才拉」后的状态与加载器。
// 状态对象与 TODAY 同级声明（早于任何 selectNav 调用，无 TDZ 风险）。
const HOTSPOTS = { data: null, loading: false };
function loadHotspots() {
  if (HOTSPOTS.loading) return Promise.resolve();
  HOTSPOTS.loading = true;
  return api("/api/hotspots")
    .then((d) => { HOTSPOTS.data = d; renderHotspots(d); })
    .catch((e) => { HOTSPOTS.data = null; throw e; })
    .finally(() => { HOTSPOTS.loading = false; });
}

function loadToday() {
  return api("/api/today_decision").then((d) => { TODAY.data = d; renderToday(); });
}
function tdPct(v) { return v == null ? "—" : fmtPct(v); }
function tdCls(v) { return v == null ? "" : (v >= 0 ? "bull" : "bear"); }

/* ============================================================================
 * 第 1 节  纯逻辑辅助函数
 * 说明：全部为函数声明 + 模板字符串，与项目既有原生写法一致；不引入任何依赖。
 * 缺失策略总纲（继承 A1）：缺什么就明示什么 —— 缺失 ≠ 0 ≠ 无；
 *   · source 缺 → 返回空串，组件渲染「来源缺失」并整块置灰；
 *   · timepoint 缺 → 返回空串，组件渲染「时点未知」，绝不冒充最新；
 *   · caliber 缺 → 返回空串，组件渲染「口径待明确」，不展示数值；
 *   · trust 缺 → 返回空串，组件渲染「可信度未知」；model 级无命中率 → 标「未验证」。
 * ========================================================================== */

/* ---- 1.1 数据日/生成时间 → 时点要素（降级：数据日缺 → 退生成时间；再缺 → 空串=时点未知） ---- */
function tdTimepointOf(d) {
  d = d || {};
  const parts = [];
  if (d.data_date) parts.push("数据日 " + d.data_date + " 收盘后");
  if (d.generated_at) parts.push("生成于 " + d.generated_at);
  // 两者皆无：返回空串 → dataUnitMeta 自动渲染「时点未知」，不写"今日"/"最新"
  return parts.join(" · ");
}

/* ---- 1.2 行/卡可信度档位判定（official=官方/交易所级；community=社区统计级；model=模型推断级） ---- */
function tdTrustOf(kind, x) {
  const note = (x && String(x.note || "")) || "";
  // 模型推断级：机会卡里由预测引擎输出的「每日预测核心」（无验证命中率 → 调用方用 trustBadge 自动补「未验证」）
  if (kind === "opportunity" && tdIsModelPrediction(x)) {
    return { trust: "model", trustNote: "预测引擎输出，无验证命中率时一律标注未验证" };
  }
  // 社区统计级（聚合结论）：基础数据是交易所披露，但排序/评分是统计规则 → 不能冒充官方结论
  if (kind === "direction" || kind === "verdict") {
    return { trust: "community", trustNote: "基础为交易所龙虎榜披露，排序/评分/聚合为统计规则" };
  }
  if (kind === "risk" && x && x.type === "stock") {
    return { trust: "community", trustNote: "龙虎榜净卖出为交易所披露，聚合为统计规则" };
  }
  if (kind === "risk") {
    return { trust: "community", trustNote: "板块资金统计与近24h帖子情绪聚合" };
  }
  if (kind === "watch") {
    return { trust: "community", trustNote: "关注池配置与行情注入，非官方推荐" };
  }
  return { trust: "community", trustNote: "" };
}

/* ---- 1.3 机会行是否「模型推断级」（补位条目：旧为“每日预测核心”，2026-10-15 起改为四支柱） ---- */
function tdIsModelPrediction(x) {
  return !!(x && /每日预测|预测·核心|预测核心|prediction|四支柱|pillar/i.test(String(x.note || "")));
}

/* ---- 1.4b 历史背景硬隔离（A2 整改，2026-10-13）----
 * 纪律：今日决策结论只允许基于「今日」可见的数据；当 data_date 落后于当前日期时，
 * 结论所在的区块必须整体标记为「历史背景」（data-historical="true"），与今日卡片
 * 严格隔离呈现 —— 历史背景区块只陈述「数据截至哪一天」，绝不冒充今日实时状态；
 * 今日结论区（data-historical="false"）只引用今日数据，二者互不混入。 */
function tdIsHistorical(d) {
  d = d || {};
  const dd = d.data_date ? String(d.data_date).slice(0, 10) : "";
  if (!dd) return false;
  const t = new Date();
  const pad = (x) => String(x).padStart(2, "0");
  const todayStr = `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
  return dd < todayStr;
}
function tdHistoryBackground(d) {
  d = d || {};
  const isHist = tdIsHistorical(d);
  if (!isHist) return "";   // 今日数据：无历史背景区块，全部按「今日」正常呈现
  const dd = d.data_date ? String(d.data_date).slice(0, 10) : "—";
  // 历史区块：仅当后端给的 data_date 落后于今天才渲染，并用 data-historical 属性标记，
  // 供审计/样式把「历史背景」与「今日」严格区分开，旧数据绝不混入今日判词。
  return `
    <section class="hist-bg" data-historical="true" aria-label="历史区块">
      <div class="hist-hd"><b>历史背景</b><span class="hist-tag">非今日数据 · 与今日结论严格隔离</span></div>
      <div class="hist-txt">本页「今日决策」基于数据日 <b>${esc(dd)}</b> 生成，早于当前日期，属历史背景。
        以下内容只描述截至 ${esc(dd)} 的存量状态，不代表今日实时状态；今日结论请等数据刷新后再看。</div>
      <div class="hist-note">已触碰 A2 新鲜度硬隔离：历史背景区块独立成区，旧数据不会混入「今日」判词，
        也不会在「数据截至」之外伪装成最新消息。</div>
    </section>`;
}

/* ---- 1.4 结论条（今日一句话）四要素 meta：全部真实字段 → 缺失时透传空串 ---- */
function tdTodayVerdictMeta(d) {
  d = d || {};
  const isEmpty = d.ok === false
    || (!toArr(d.directions).length && !toArr(d.opportunities).length && !toArr(d.risks).length);
  const t = tdTrustOf("verdict", d);
  return {
    // 后端没有任何可聚合数据 → 来源缺失（整块置灰）；否则来源是真实的聚合查询，不算缺失
    source: isEmpty
      ? ""
      : "sector_chain / lhb_sector_focus / pillar_item / lhb_stocks（后端聚合）",
    timepoint: tdTimepointOf(d),
    caliber: d.basis
      ? "方向=主线研判；机会=可推进/四支柱入选；风险=净流出+情绪；详见下方口径说明"
      : "",
    trust: t.trust,
    trustNote: t.trustNote,
  };
}

/* ---- 1.5 四张卡片（方向/机会/风险/我的票）卡头四要素 meta ---- */
function tdCardMeta(kind, d) {
  d = d || {};
  const M = {
    direction: {
      src: "sector_chain（交易所龙虎榜）",
      cal: "净买额单位：亿元；评分=资金40%+龙头30%+覆盖20%+梯队10%",
    },
    opportunity: {
      src: "sector_chain 主线梯队 / pillar_item 四支柱入选（政策漏斗/超跌修复/竞价冲浪）",
      cal: "主线可推进名单；预测核心无当日涨跌幅时以 note 说明",
    },
    risk: {
      src: "lhb_sector_focus / lhb_stocks / postdigest 近24h情绪",
      cal: "板块净流出与个股净卖出单位：亿元；情绪为近24h帖子统计",
    },
    watch: {
      src: "watchlib 关注池（本地配置 + 行情注入）",
      cal: "目标价/触发价为关注池注入，现价为行情，距离为百分比",
    },
  };
  const m = M[kind] || M.direction;
  const t = tdTrustOf(kind, null);
  return {
    source: m.src,
    timepoint: tdTimepointOf(d),
    caliber: m.cal,
    trust: t.trust,
    trustNote: t.trustNote,
  };
}

/* ---- 1.6 行级补充徽章（默认空；仅「机会卡·每日预测核心」强制追加模型推断级 + 未验证） ---- */
function tdRowMetaBadge(kind, x) {
  if (kind !== "opportunity" || !tdIsModelPrediction(x)) return "";
  // model 级且本接口未携带验证命中率 → 一律标「未验证」（A1 硬性要求，不假装已回测）
  return trustBadge("model", "未验证");
}

/* ============================================================================
 * AI 复盘（2026-10-29 进哥需求）
 * ----------------------------------------------------------------------------
 * 与「今日决策」并列，但走的是**完全不同的路**，界面上必须让这个区别一眼可见：
 *   · 今日决策 = 本地规则引擎：固定权重公式（40×净流入比 + 30/20 涨停档 + 梯队数×1.2），
 *     输出可复现、但没有推理过程，也不会告诉你它错在哪。
 *   · AI 复盘 = 真调大模型：把当日信息包（市场横截面/主线/龙虎榜/政策/情绪/持仓）
 *     + 用户自己沉淀的 9 份知识库准则 + 上一份复盘，交给 DeepSeek/混元/千问，
 *     让它输出带理由的判断，并显式回答两件规则引擎答不了的事：
 *       ① disagreements —— 我和规则打分哪里不一致、为什么
 *       ② vs_last      —— 上一份判断今天兑现了没有
 * 所以本面板把①放在显眼位置（不是塞在角落），这就是「用大脑算」的证据。
 * 数据来自 /api/ai_review（后端 viewer/ai_review.py，每次调用落库可审计）。
 * ============================================================================ */
const AIR = { data: null, provider: "", polling: null, tries: 0, hist: null, sinceId: 0 };

function airProviderName(key) {
  const p = (AIR.data && AIR.data.providers || []).find((x) => x.key === key);
  return p ? p.label : (key || "—");
}
function airPct(v) { return v == null || isNaN(v) ? "—" : Math.round(v) + "%"; }
function airList(arr) { return Array.isArray(arr) ? arr : []; }
function airToneCls(level) {
  const s = String(level || "");
  if (/高|清|退潮|普跌|减/.test(s)) return "air-tone-bad";
  if (/低|加|加速|普涨/.test(s)) return "air-tone-good";
  return "air-tone-mid";
}

function loadAiReview(opts) {
  const o = opts || {};
  const qs = [];
  if (AIR.provider) qs.push("provider=" + encodeURIComponent(AIR.provider));
  if (o.date) qs.push("date=" + encodeURIComponent(o.date));
  if (o.force) qs.push("force=1");
  // 结果水位线：点「重新分析」后后台要算 10s，这期间如果轮询只看「有没有缓存」，
  // 会立刻拿到上一份旧结果并误判成已完成（真实踩到过：库里还是旧 id，页面显示旧判断）。
  // 所以重算时记下当时的 MAX(id)，轮询带上它，只有出现更新的记录才算这一轮真出结果。
  else if (AIR.sinceId) qs.push("since_id=" + AIR.sinceId);
  const url = "/api/ai_review" + (qs.length ? "?" + qs.join("&") : "");
  // 强制重算走 POST（会写库 + 花 token）。注意必须带 Content-Type 与 body：
  // 服务端 POST 分支要求 JSON，缺 header 直接 415（本项目所有 POST 都是这个写法，见 cfgSet/serviceShutdown）。
  const p = o.force
    ? api("/api/ai_review/run?provider=" + encodeURIComponent(AIR.provider || ""),
          { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    : api(url);
  return p.then((d) => {
    AIR.data = d || {};
    if (d && d.since_id != null) AIR.sinceId = d.since_id || 0;
    if (!AIR.provider && d && d.providers) {
      const ready = d.providers.filter((x) => x.ready);
      if (ready.length) AIR.provider = ready[0].key;
    }
    renderAiReview();
    if (d && d.state === "computing") airStartPoll();
    else airStopPoll();
    return d;
  }).catch((e) => {
    // 2026-10-29（进哥截图：「一只在刷新不出来」）：
    // 旧行为：请求失败 → 只往顶栏塞一条「部分数据加载失败」→ 面板里那句
    // 「正在加载数据…」**永远不会被替换**，看起来就是死在转圈。
    // 新行为：面板自己说清失败原因与下一步，绝不留一句假的"加载中"。
    airRenderError(e);
    throw e;
  });
}

/* 面板内失败态：必须能回答「为什么」「我现在能做什么」，而不是留一句加载中。 */
function airRenderError(e) {
  const box = $("#p-ai_review");
  if (!box) return;
  // 已经有结果就别用错误覆盖掉（轮询失败不该抹掉上一次的可用结论）
  if (AIR.data && AIR.data.data && AIR.data.data.verdict) return;
  const msg = (e && (e.message || e.code)) || "未知错误";
  const isTimeout = !!(e && e.isTimeout);
  const isNet = !!(e && e.code === "NET");
  const why = isTimeout
    ? "后端在 15 秒内没有响应。通常是它正被别的重活占着（抓取/因子计算），不是这条链路坏了。"
    : isNet
      ? "连不上本地服务：服务进程可能已退出。刷新页面不会让它回来，需要用启动脚本重开。"
      : "接口返回了错误，详情见顶栏。";
  box.innerHTML = `
    <div class="air-head">
      <div class="air-hd-l"><b class="air-title">AI 复盘</b></div>
      <div class="air-hd-r">${airProviderBar()}</div>
    </div>
    <div class="air-err">
      <b>这一次没拿到结果</b>
      <div>${esc(String(msg))}</div>
      <div class="air-err-hint">${esc(why)}</div>
      <button class="btn sm" onclick="airRerun(this)">重试</button>
    </div>`;
}

function airStartPoll() {
  if (AIR.polling) return;
  AIR.tries = 0;
  // 前快后慢：模型正常 10~60s 出结果，前 30s 每 3s 问一次，之后 8s 一次，最多约 4 分钟
  const tick = () => {
    AIR.tries += 1;
    if (AIR.tries > 30) { airStopPoll(); return; }
    const wait = AIR.tries <= 10 ? 3000 : 8000;
    AIR.polling = setTimeout(() => {
      AIR.polling = null;
      loadAiReview().catch(() => { airStopPoll(); });
    }, wait);
  };
  AIR.polling = setTimeout(() => { AIR.polling = null; tick(); }, 2500);
}
function airStopPoll() {
  if (AIR.polling) { clearTimeout(AIR.polling); AIR.polling = null; }
}

function airProviderBar() {
  const provs = airList(AIR.data && AIR.data.providers);
  const used = (AIR.data && AIR.data.provider) || AIR.provider;
  const btns = provs.map((p) => {
    const on = p.key === used;
    const dis = !p.ready;
    return `<button class="air-prov${on ? " on" : ""}${dis ? " off" : ""}"
      ${dis ? 'disabled title="未配置该模型的 API Key"' : ""}
      onclick="airSwitch('${escAttr(p.key)}')">${esc(p.label)}${dis ? "（未配 Key）" : ""}</button>`;
  }).join("");
  return `<span class="air-provbar"><span class="air-provlab">用哪个大脑：</span>${btns}</span>`;
}

function airSwitch(key) {
  if (!key || key === AIR.provider) return;
  AIR.provider = key;
  AIR.sinceId = 0;   // 换模型是全新一轮，旧水位线作废
  AIR.data = null;
  airStopPoll();
  const box = $("#p-ai_review");
  if (box) box.innerHTML = `<div class="panel-loading">正在用 ${esc(airProviderName(key))} 重新分析…</div>`;
  loadAiReview({ force: true }).catch((e) => reportLoadError("AI 复盘", e));
}

function airRerun(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
  loadAiReview({ force: true }).catch((e) => reportLoadError("AI 复盘", e));
}

/* ============================================================================
 * AI 可信度卡片（2026-10-31）
 *
 * 为什么单独挂一块、不改下面的 5 个渲染分支：复盘面板的分支很多（计算中/无数据/
 * WorkBuddy 待取/正常/历史），每处都插一次迟早漏。这里用「独立容器 + 每次重绘后
 * 重新插回」的方式，零侵入。
 *
 * 它要回答的问题是：**这个 AI 说的话，到底准不准？**
 * 数据全部来自系统回填（viewer/review_score.py），不是模型自评 —— 这正是之前缺的。
 * 样本 <30 条时明确写「只是趋势」，不允许拿 6 天数据冒充准确率。
 * ========================================================================== */
let AIR_SCORE = "";
let AIR_SCORE_BUSY = false;

function buildAiScoreHtml(sc, hy, ev) {
  const p = ['<div class="asc-card">',
             '<div class="asc-hd">AI 可信度 <span class="asc-hd-sub">系统按收盘价回填，非模型自评</span></div>'];
  if (!sc || !sc.ok) {
    p.push('<div class="asc-note">成绩单暂不可用</div>');
  } else if (!sc.n_scored) {
    p.push('<div class="asc-note">还没有到期可回填的判断 —— 复盘后满 5 个交易日出第一批成绩。</div>');
  } else {
    if (!sc.min_sample_ok) {
      p.push('<div class="asc-warn">有效样本 ' + sc.n_scored + ' 条（&lt;30）：命中率只代表趋势，不是结论</div>');
    }
    p.push('<div class="asc-grid">');
    for (const g of (sc.groups || [])) {
      if (g.kind === "mainline") continue;       // 弱代理，不计入合并
      const ex = (g.ex5 == null) ? "—" : (g.ex5 > 0 ? "+" : "") + g.ex5.toFixed(2) + "%";
      const hit = (g.hit5 == null) ? "—" : g.hit5.toFixed(0) + "%";
      const cls = (g.ex5 == null) ? "" : (g.ex5 > 0 ? "up" : "down");
      p.push('<div class="asc-item"><span class="asc-lb">' + g.label + '</span>'
        + '<span class="asc-v ' + cls + '">' + ex + '</span>'
        + '<span class="asc-sub">T+5 超额 · 命中 ' + hit + ' · n=' + g.n5 + '</span></div>');
    }
    p.push('</div>');
  }
  if (hy && hy.ok && hy.counts) {
    const c = hy.counts;
    p.push('<div class="asc-line">假说回测：成立 ' + (c.confirmed || 0)
      + ' · 方向相反 ' + (c.opposite || 0)
      + ' · 幅度不足(不可用) ' + (c.marginal || 0)
      + ' · 未测出 ' + (c.unsupported || 0) + '</div>');
  }
  if (ev && ev.ok) {
    const st = ev.by_status || {};
    p.push('<div class="asc-line">实证账本 ' + (ev.asof || "") + '：可用 ' + (st.active || 0)
      + ' 条 · 已推翻 ' + (st.refuted || 0) + ' 条 · 观察 ' + (st.watch || 0)
      + ' 条 <span class="asc-dim">（每条都带 t 值与可复现脚本）</span></div>');
  }
  p.push('</div>');
  return p.join("");
}

function loadAiScore() {
  AIR_SCORE_BUSY = true;
  Promise.all([
    api("/api/review_score").catch(() => null),
    api("/api/hypothesis").catch(() => null),
    api("/api/evidence").catch(() => null),
  ]).then(([sc, hy, ev]) => {
    AIR_SCORE = buildAiScoreHtml(sc, hy, ev);
    AIR_SCORE_BUSY = false;
    paintAiScore();
  }).catch(() => { AIR_SCORE_BUSY = false; });
}

function paintAiScore() {
  const box = $("#p-ai_review");
  if (!box) return;
  if (!AIR_SCORE) {                 // 首次进入：拉一次；busy 期间不重复发
    if (!AIR_SCORE_BUSY) loadAiScore();
    return;
  }
  let el = box.querySelector(".air-score");
  if (!el) {
    el = document.createElement("div");
    el.className = "air-score";
    box.insertBefore(el, box.firstChild || null);
  }
  el.innerHTML = AIR_SCORE;
}

function renderAiReview() {
  const box = $("#p-ai_review");
  if (!box) return;
  const d = AIR.data;
  if (!d) { box.innerHTML = `<div class="panel-loading">正在加载 AI 复盘…</div>`; return; }

  // 等本次 innerHTML 写完再插回成绩单（否则会被覆盖）。microtask 比 setTimeout 更快。
  queueMicrotask(paintAiScore);

  const provBar = airProviderBar();

  // —— 还在算：必须给出明确进度，而不是白屏或死转圈
  if (d.state === "computing") {
    const sec = d.elapsed_sec || 0;
    const isWB = (d.provider || AIR.provider) === "workbuddy";
    // 2026-10-29：WorkBuddy 通道与"调外部 API"是两种等待，不能共用一套文案。
    // 外部模型：本进程正在调 API，10~60 秒。
    // WorkBuddy：任务已入队，等主代理来取包 —— 不可能是"自己在算"，必须说清怎么触发。
    const body = isWB
      ? `<b>已把 ${esc(d.review_date || "")} 的分析包放进队列（任务 #${esc(String(d.queue_id || "-"))}，${esc(String(d.brief_chars || 0))} 字）</b>
         <span>已等待 ${esc(String(sec))} 秒。这是「用 WorkBuddy 的算力」通道：查看器不调用任何外部 API，
         而是把信息包备好，由 WorkBuddy 取走后分析、再回填到这里。<br>
         去 WorkBuddy 说一句 <code>分析今天的复盘</code> 即可，回来刷新就能看到结果。</span>`
      : `<b>正在让 ${esc(airProviderName(d.provider || AIR.provider))} 读完 ${esc(d.review_date || "")} 的全部信息再判断…</b>
         <span>已等待 ${esc(String(sec))} 秒。它在读：市场横截面 / 主线板块 / 龙虎榜 / 政策与公告 / 社区情绪 / 你的持仓 / 你自己的 9 份知识库准则。通常 10~60 秒完成，可以先去别的面板，回来即好。</span>`;
    box.innerHTML = `
      <div class="air-head">
        <div class="air-hd-l"><b class="air-title">AI 复盘</b>
          <span class="air-sub">${esc(d.review_date || "")} · ${esc(airProviderName(d.provider || AIR.provider))}</span></div>
        <div class="air-hd-r">${provBar}</div>
      </div>
      <div class="air-computing">
        <div class="air-spin"></div>
        <div class="air-computing-txt">${body}</div>
      </div>
      ${d.partial ? `<div class="air-note">上一份可用结果（${esc(d.partial.review_date || "")}）：${esc((d.partial.data || {}).verdict || "—")}</div>` : ""}`;
    return;
  }

  // —— 失败：说清原因，不要假装在算
  if (d.state === "error" || d.ok === false) {
    box.innerHTML = `
      <div class="air-head">
        <div class="air-hd-l"><b class="air-title">AI 复盘</b></div>
        <div class="air-hd-r">${provBar}</div>
      </div>
      <div class="air-err"><b>这次没能出结果</b><div>${esc(d.error || d.err || "未知错误")}</div>
        <div class="air-err-hint">常见原因：该模型未配 API Key、额度用尽、或网络不通。修好后点下面重试。</div>
        <button class="btn sm" onclick="airRerun(this)">重试</button></div>`;
    return;
  }

  // —— 还没有任何一份（首份没算过）：给一个明确入口
  const o = (d && d.data) || {};
  if (!o || !o.verdict) {
    box.innerHTML = `
      <div class="air-head">
        <div class="air-hd-l"><b class="air-title">AI 复盘</b>
          <span class="air-sub">${esc(d.review_date || "")}</span></div>
        <div class="air-hd-r">${provBar}</div>
      </div>
      <div class="air-empty">
        <b>还没有这一天的 AI 复盘</b>
        <div>点下面按钮，让 ${esc(airProviderName(AIR.provider))} 把当天的市场数据、资金、消息、情绪和你自己的交易体系一起读完，给出一份带理由的判断。</div>
        <button class="btn sm primary" onclick="airRerun(this)">开始分析</button>
      </div>`;
    return;
  }

  // —— 正常渲染
  const mr = o.market_read || {};
  const tk = d.tokens || {};
  const metaLine = `${esc(d.model || "")} · 耗时 ${((d.elapsed_ms || 0) / 1000).toFixed(1)}s · 输入 ${tk.prompt || 0} + 输出 ${tk.completion || 0} tokens · 喂入 ${d.context_chars || 0} 字 · ${esc(String(d.created_at || "").slice(0, 16))}`;

  // 「它到底读了多少东西」—— 直接把读了的量摆出来，不让"到底有没有在思考"停留在信仰层面
  const cs = d.context_stats || {};
  const readBar = (cs.docs || cs.policy || cs.mainlines) ? `
    <div class="air-readbar">
      <span class="air-readlab">这次 AI 实际读了：</span>
      <span class="air-readitem"><b>${cs.sections || 0}</b> 类市场数据</span>
      <span class="air-readitem"><b>${cs.mainlines || 0}</b> 条主线</span>
      <span class="air-readitem"><b>${cs.lhb || 0}</b> 只龙虎榜个股</span>
      <span class="air-readitem"><b>${cs.policy || 0}</b> 条政策要闻</span>
      <span class="air-readitem"><b>${cs.hot_stocks || 0}</b> 只热议股</span>
      <span class="air-readitem air-readhi"><b>${cs.docs || 0}</b> 份你自己的知识库</span>
      <span class="air-readitem air-readhi"><b>${cs.rules || 0}</b> 条数据自生长规则</span>
    </div>` : "";

  // 局内人推演（2026-10-31 进哥需求）：本面板唯一"换位思考"的部分。
  // 约束写在 prompt 里：每一步必须挂在当日盘面数据（涨停/炸板/连板/陷阱名单/板块阶段）上，
  // 撑不住的要自己标「这一步是猜测」——所以这里如实展示 data_backed 标记。
  const ins = o.insider || {};
  const insHtml = (ins.where_to_lure || ins.squeeze || ins.reverse) ? `
    <div class="air-card" style="border-left:3px solid #7c3aed">
      <div class="air-card-hd"><b>局内人推演</b>
        <span class="air-card-sub">${ins.data_backed === false ? "含未验证推测" : "挂在当日盘面数据上"}</span></div>
      <div class="air-card-bd">
        ${ins.where_to_lure ? '<div class="air-logic"><b style="color:#7c3aed">在哪设套　</b>' + esc(ins.where_to_lure) + '</div>' : ""}
        ${ins.squeeze ? '<div class="air-logic"><b style="color:#7c3aed">砸谁托谁　</b>' + esc(ins.squeeze) + '</div>' : ""}
        ${ins.retail_mistake ? '<div class="air-logic"><b style="color:#dc2626">散户会犯的错　</b>' + esc(ins.retail_mistake) + '</div>' : ""}
        ${ins.reverse ? '<div class="air-logic" style="margin-top:8px;padding:9px 11px;background:rgba(124,58,237,.07);border-radius:8px"><b>反向操作　</b>' + esc(ins.reverse) + '</div>' : ""}
      </div>
    </div>` : "";

  const evHtml = airList(mr.key_evidence).length
    ? `<ul class="air-ev">${airList(mr.key_evidence).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";

  const lineRows = airList(o.mainlines).map((m) => `
    <div class="air-line">
      <div class="air-line-hd"><b>${esc(m.sector)}</b>
        <span class="air-chip ${airToneCls(m.stage)}">${esc(m.stage || "—")}</span>
        <span class="air-chip air-chip-dim">持续性 ${esc(m.sustainability || "—")}</span></div>
      <div class="air-line-logic">${esc(m.logic || "")}</div>
      ${m.risk ? `<div class="air-line-risk">⚠ ${esc(m.risk)}</div>` : ""}
    </div>`).join("");

  const oppRows = airList(o.opportunities).map((x) => `
    <div class="air-opp">
      <div class="air-opp-hd">${x.symbol ? codeLink(x.symbol, { text: x.name || x.symbol, cls: "air-sym" }) : `<b class="air-sym">${esc(x.name || "—")}</b>`}</div>
      <div class="air-opp-body">
        <div><span class="air-k">逻辑</span>${esc(x.thesis || "")}</div>
        <div><span class="air-k">动手条件</span>${esc(x.trigger || "")}</div>
        <div class="air-invalidate"><span class="air-k">证伪</span>${esc(x.invalidate || "")}</div>
      </div>
    </div>`).join("");

  const riskRows = airList(o.risks).map((x) => `
    <div class="air-risk">
      <span class="air-chip ${airToneCls(x.level)}">${esc(x.level || "—")}</span>
      <b>${esc(x.label || "")}</b><span class="air-risk-d">${esc(x.detail || "")}</span>
    </div>`).join("");

  const pfRows = airList(o.portfolio_advice).map((x) => `
    <div class="air-pf">
      <span class="air-chip ${airToneCls(x.advice)}">${esc(x.advice || "—")}</span>
      <b>${esc(x.name || "")}</b><span class="air-risk-d">${esc(x.reason || "")}</span>
    </div>`).join("");

  // 分歧区 —— 本面板的重点，单独高亮。
  // 排版取舍：初版把「规则引擎会认为 | 模型判断」做成同一行左右两段（inline），
  // 实测两列被 1fr 1fr 拉得很开、中间大片空白，读的时候要左右横跳。
  // 改成「标签在上、正文在下」的对照卡片，左弱右强，视线只需上下比。
  const disRows = airList(o.disagreements).map((x) => `
    <div class="air-dis">
      <div class="air-dis-q">${esc(x.point || "")}</div>
      <div class="air-dis-vs">
        <div class="air-dis-rule"><span class="air-k">规则引擎</span><div class="air-dis-txt">${esc(x.rule_view || "")}</div></div>
        <div class="air-dis-mine"><span class="air-k">模型判断</span><div class="air-dis-txt">${esc(x.my_view || "")}</div></div>
      </div>
      <div class="air-dis-why"><span class="air-k air-k-inline">为什么</span>${esc(x.why || "")}</div>
    </div>`).join("");

  const vl = o.vs_last || {};
  const vlHtml = vl.available
    ? `<div class="air-vs">
        ${airList(vl.hit).length ? `<div class="air-vs-row air-vs-hit"><span class="air-k">兑现</span><ul>${airList(vl.hit).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
        ${airList(vl.miss).length ? `<div class="air-vs-row air-vs-miss"><span class="air-k">没兑现</span><ul>${airList(vl.miss).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
      </div>`
    : `<div class="air-vs-empty">${esc(airList(vl.miss)[0] || "首次复盘，暂无历史可对照。明天起这里会显示「昨天说的今天兑现没有」。")}</div>`;

  const actHtml = airList(o.actions).map((x, i) => `<div class="air-act"><span class="air-act-n">${i + 1}</span>${esc(x)}</div>`).join("");

  const conf = o.confidence;
  const confBar = conf == null ? "" : `
    <div class="air-conf"><span class="air-conf-lab">置信度</span>
      <div class="air-conf-bar"><i style="width:${Math.max(2, Math.min(100, conf))}%"></i></div>
      <b>${esc(String(conf))}</b><span class="air-conf-unit">/100</span></div>`;

  box.innerHTML = `
    <div class="air-head">
      <div class="air-hd-l"><b class="air-title">AI 复盘</b>
        <span class="air-sub">${esc(d.review_date || "")} · ${esc(airProviderName(d.provider))}</span></div>
      <div class="air-hd-r">${provBar}<button class="btn sm" onclick="airRerun(this)" title="跳过缓存，重新让模型分析一次（会花 token）">重新分析</button></div>
    </div>
    <div class="air-meta">${metaLine}</div>
    ${readBar}

    <div class="air-verdict">
      <div class="air-v-tag">核心判断</div>
      <div class="air-v-text">${esc(o.verdict || "—")}</div>
      ${confBar}
    </div>

    <div class="air-grid2">
      <div class="air-card">
        <div class="air-card-hd"><b>① 市场怎么读</b><span class="air-card-sub">${esc(mr.state || "")}</span></div>
        <div class="air-card-bd">
          <div class="air-logic">${esc(mr.logic || "")}</div>
          ${evHtml}
        </div>
      </div>
      <div class="air-card">
        <div class="air-card-hd"><b>② 今天该做什么</b><span class="air-card-sub">最多 3 条</span></div>
        <div class="air-card-bd">${actHtml || `<div class="air-empty-inline">无</div>`}</div>
      </div>
    </div>

    ${insHtml}

    <div class="air-card">
      <div class="air-card-hd"><b>③ 主线与阶段</b><span class="air-card-sub">${airList(o.mainlines).length} 条</span></div>
      <div class="air-card-bd air-lines">${lineRows || `<div class="air-empty-inline">无明确主线</div>`}</div>
    </div>

    <div class="air-card">
      <div class="air-card-hd"><b>④ 机会（带动手条件与证伪条件）</b><span class="air-card-sub">不是"推荐"，是"什么条件下才算"</span></div>
      <div class="air-card-bd air-opps">${oppRows || `<div class="air-empty-inline">模型认为当前没有值得动手的机会 —— 空仓也是一种判断</div>`}</div>
    </div>

    <div class="air-grid2">
      <div class="air-card">
        <div class="air-card-hd"><b>⑤ 风险</b></div>
        <div class="air-card-bd">${riskRows || `<div class="air-empty-inline">无</div>`}</div>
      </div>
      <div class="air-card">
        <div class="air-card-hd"><b>⑥ 你的持仓怎么办</b></div>
        <div class="air-card-bd">${pfRows || `<div class="air-empty-inline">无持仓数据</div>`}</div>
      </div>
    </div>

    <div class="air-card air-card-hl">
      <div class="air-card-hd"><b>⑦ 模型与规则引擎的分歧</b><span class="air-card-sub">这一栏就是「有没有真在思考」的证据</span></div>
      <div class="air-card-bd air-diss">${disRows || `<div class="air-empty-inline">这一次模型与规则打分没有分歧</div>`}</div>
    </div>

    <div class="air-card">
      <div class="air-card-hd"><b>⑧ 上一份判断兑现了没有</b></div>
      <div class="air-card-bd">${vlHtml}</div>
    </div>

    <div class="air-foot">
      <span>本页判断由 ${esc(airProviderName(d.provider))} 基于上述数据自动生成，每次调用可审计（模型/耗时/token/喂入字数均落库）。仅供研究参考，不构成投资建议。</span>
      <button class="btn sm" onclick="airLoadHist()" title="查看历史复盘">历史复盘</button>
    </div>
    <div id="air-hist"></div>`;

  if (AIR.hist) airRenderHist();
}

function airLoadHist() {
  return api("/api/ai_review/history?limit=30").then((d) => {
    AIR.hist = d || { items: [] };
    airRenderHist();
  }).catch((e) => setHint("历史复盘加载失败：" + apiErrText(e)));
}

function airRenderHist() {
  const box = $("#air-hist");
  if (!box) return;
  const items = airList((AIR.hist || {}).items);
  if (!items.length) { box.innerHTML = `<div class="air-hist-empty">还没有历史记录。</div>`; return; }
  const rows = items.map((x) => `
    <div class="air-hist-row${x.status === "ok" ? "" : " bad"}">
      <span class="air-hist-d">${esc(x.review_date || "")}</span>
      <span class="air-hist-p">${esc(x.provider || "")}</span>
      <span class="air-hist-v">${esc(x.verdict || (x.err ? "（失败：" + x.err + "）" : "—"))}</span>
      <span class="air-hist-t">${x.elapsed_ms ? ((x.elapsed_ms / 1000).toFixed(1) + "s") : ""}</span>
    </div>`).join("");
  box.innerHTML = `<div class="air-card"><div class="air-card-hd"><b>历史复盘</b>
    <span class="air-card-sub">每天一份，可回看当时的判断</span></div>
    <div class="air-card-bd air-hist">${rows}</div></div>`;
}

function renderToday() {
  const box = $("#p-today");
  if (!box) return;
  const d = TODAY.data;
  if (!d || d.ok === false) { box.innerHTML = `<div class="empty">正在汇总今日决策…</div>`; return; }
  const dirs = toArr(d.directions), opps = toArr(d.opportunities), risks = toArr(d.risks);
  const mw = d.mywatch || { total: 0, unread: 0, alerts: [], near: [] };
  const tone = d.verdict_tone || "mixed";
  const codeA = (code, name) => codeLink(code, { text: name || code, cls: "td-sym" });

  // [改点 1] 卡片 meta 预构造：传空 meta 的 d 缺失场景由组件自动降级（来源缺失/时点未知/待明确/未知）
  const dirMeta   = dataUnitBadge(tdCardMeta("direction", d));
  const oppMeta   = dataUnitBadge(tdCardMeta("opportunity", d));
  const riskMeta  = dataUnitBadge(tdCardMeta("risk", d));
  const watchMeta = dataUnitBadge(tdCardMeta("watch", d));

  // ① 方向（资金主攻 + 龙头 + 阶段判词）
  const dirHtml = dirs.length ? dirs.map((x, i) => `
    <div class="td-row">
      <span class="td-rank lv-${escAttr(x.level || "cool")}">${i + 1}</span>
      <span class="td-main"><b>${esc(x.sector)}</b>
        <span class="td-sub">净买 <b>${x.net_yi}</b> 亿 · ${esc(x.stage)} · 上榜 ${x.onlist_n} 家</span>${x.etf_confirm ? `<span class="etf-conf-badge" style="margin-left:6px;color:${x.etf_confirm.level === '强' ? '#0a7d3b' : x.etf_confirm.level === '中' ? '#0E7490' : x.etf_confirm.level === '弱' ? '#b42318' : '#888'};font-weight:600">· ETF${esc(x.etf_confirm.level)}</span>` : ""}</span>
      ${x.leader_code ? `<span class="td-side">龙头 ${codeA(x.leader_code, x.leader_name)}</span>` : ""}
    </div>`).join("") : `<div class="td-empty">今日暂无明确主攻方向（数据日 ${esc(d.data_date || "—")}）。</div>`;

  // ② 机会（主线「可推进」名单 / 每日预测核心）—— 副行必须嵌在 td-main 内，
  //    否则它会变成 flex 行的第二个 item 被推到最右侧（与方向/风险卡不对齐）。
  const oppHtml = opps.length ? opps.map((x) => `
    <div class="td-row">
      <span class="td-main">${codeA(x.symbol, x.name)}${x.pct != null ? `<span class="td-pct ${tdCls(x.pct)}">${tdPct(x.pct)}</span>` : ""}
        <span class="td-sub">${tdRowMetaBadge("opportunity", x)}${esc(x.sector || "")}${x.sector ? " · " : ""}${esc(x.note || "")}</span></span>
    </div>`).join("") : `<div class="td-empty">主线暂无可推进名单。</div>`;

  // ③ 风险（板块资金净流出 / 龙虎榜净卖出 / 上榜次日跑输 / 情绪看空·分歧）
  const riskHtml = risks.length ? risks.map((x) => `
    <div class="td-row">
      <span class="td-main">${x.symbol ? codeA(x.symbol, x.label) : esc(x.label)}
        <span class="td-sub">${esc(x.detail || "")}</span></span>
    </div>`).join("") : `<div class="td-empty">今日无明确风险信号。</div>`;

  // ④ 我的票（到价触发优先，其次接近目标价的）
  const alertRows = toArr(mw.alerts).map((a) => `
    <div class="td-row">
      <span class="td-main">${codeA(a.symbol, a.name)}
        <span class="td-sub">${wtAlertRuleText(a.rule)}目标价 ${esc(String(a.target_price))} · 触发价 ${a.price == null ? "—" : esc(String(a.price))}</span></span>
      <span class="td-side">${esc(String(a.triggered_at || "").slice(0, 16))}</span>
    </div>`).join("");
  const nearRows = toArr(mw.near).map((n) => `
    <div class="td-row">
      <span class="td-main">${codeA(n.symbol, n.name)}
        <span class="td-sub">现价 ${esc(String(n.last_price))} · 目标 ${esc(String(n.target_price))} · 距 ${n.dist_pct >= 0 ? "+" : ""}${esc(String(n.dist_pct))}%</span></span>
    </div>`).join("");
  const watchHtml = (alertRows || nearRows || `<div class="td-empty">关注池今日无到价提醒。</div>`);

  const _tdHist = tdIsHistorical(d);   // A2 硬隔离：数据日落后于今天 → 结论整体归入历史背景
  box.innerHTML = `
    ${tdHistoryBackground(d)}
    <div class="td-verdict tone-${escAttr(tone)}" data-historical="${_tdHist ? "true" : "false"}">
<span class="td-v-tag">今日一句话</span>
      <b class="td-v-text">${esc(d.verdict || "—")}</b>
      <span class="td-v-meta">${dataUnitMeta(tdTodayVerdictMeta(d))}</span>
    </div>
    <div class="td-grid" data-historical="${_tdHist ? "true" : "false"}">
      <div class="td-card td-c-dir">
        <div class="td-hd"><b>① 今日方向</b><span class="td-hd-sub">钱在买什么</span><span class="td-hd-meta">${dirMeta}</span></div>
        <div class="td-body">${dirHtml}</div>
        <div class="td-foot"><a href="#overview/dashboard" onclick="selectNav('overview','dashboard',true);return false;">展开「今日看板」完整研判 ›</a></div>
      </div>
      <div class="td-card td-c-opp">
        <div class="td-hd"><b>② 今日机会</b><span class="td-hd-sub">能上车的</span><span class="td-hd-meta">${oppMeta}</span></div>
        <div class="td-body">${oppHtml}</div>
        <div class="td-foot"><a href="#watch/rate" onclick="selectNav('watch','rate',true);return false;">展开「入手概率」全市场榜 ›</a></div>
      </div>
      <div class="td-card td-c-risk">
        <div class="td-hd"><b>③ 今日风险</b><span class="td-hd-sub">要躲的坑</span><span class="td-hd-meta">${riskMeta}</span></div>
        <div class="td-body">${riskHtml}</div>
        <div class="td-foot"><a href="#money/lhb_sectors" onclick="selectNav('money','lhb_sectors',true);return false;">展开「板块动向」研判 ›</a></div>
      </div>
      <div class="td-card td-c-watch">
        <div class="td-hd"><b>④ 我的票提示</b><span class="td-hd-sub">关注池 ${mw.total} 只${mw.unread ? ` · 未读 ${mw.unread}` : ""}</span><span class="td-hd-meta">${watchMeta}</span></div>
        <div class="td-body">${watchHtml}</div>
        <div class="td-foot"><a href="#watch/watch_alerts" onclick="selectNav('watch','watch_alerts',true);return false;">展开「到价提醒」 ›</a></div>
      </div>
    </div>
    <div id="td-quant-ref" class="td-quant-ref"></div>
    <div id="td-etf-fusion" class="td-etf-fusion"></div>
    <details class="td-basis"><summary>口径说明（这些结论是怎么来的）</summary><div class="td-basistxt">${esc(d.basis || "")}</div></details>
  `;
  renderQuantRefMount();
  renderEtfFusion();
}

/* M6（2026-10-16）：今日决策页里把「量化信号」作为参考证据流插入（第三路信号，与四支柱/共振对称）。
 * 只引用量化因子的自我学习状态（走 metalearn 共享引擎、诚实三态徽章），不重复算、不喧宾夺主。 */
function renderQuantRefMount() {
  const box = $("#td-quant-ref");
  if (!box) return;
  const fill = (html) => { if (box) box.innerHTML = html; };
  const d = QUANT.data;
  if (d && d.self_learn) { fill(quantRefHtml(d)); return; }
  if (QUANT.busy) { fill(`<div class="empty">量化信号加载中…</div>`); return; }
  QUANT.busy = true;   // 懒拉一次（不阻塞今日页渲染）
  api("/api/quant").then((dd) => { QUANT.data = dd; QUANT.busy = false; fill(quantRefHtml(dd)); })
    .catch(() => { QUANT.busy = false; fill(`<div class="q-ref-row muted">量化信号暂不可用（详见「量化信号」页）</div>`); });
}
function quantRefHtml(d) {
  const sl = d && d.self_learn;
  if (!sl || !sl.state) {
    return `<div class="q-ref-row"><span class="q-ref-tag">量化信号</span>
      <span class="muted">自我学习尚未生成（详见「量化信号」页 → 重新学习）</span></div>`;
  }
  const st = sl.state || {};
  const stage = st.weight_stage || "等权运行中";
  const badge = `<span class="badge ${quantStageCls(stage)} sl-stage">${esc(stage)}</span>`;
  const w = st.weights || {};
  const wParts = QUANT_FACTOR_ORDER.filter((k) => w[k] != null)
    .map((k) => `${esc(QUANT_FACTOR_LABEL[k] || k)} ${(w[k] * 100).toFixed(0)}%`).join(" · ");
  const items = toArr(sl.items);
  const top = items.slice(0, 5).map((it) => codeLink(it.symbol, { text: it.name || it.symbol, cls: "td-sym" })).join(" ");
  return `<div class="q-ref-row">
      <span class="q-ref-tag">量化信号</span>${badge}
      <span class="s">${esc(sl.run_date || "")} 期 · 学到权重：${wParts}</span>
      <span class="s">Top${items.length ? "：" : "—"}${top}</span>
      <a href="#overview/quant" onclick="selectNav('overview','quant',true);return false;">量化信号页 ›</a>
    </div>`;
}

/* M7（2026-10-17）：今日决策页「ETF 融合信号」证据流（与量化信号对称）。
 * 把 ETF 板块资金流 / 跨境风险传导 / 宽基仓位 regime / 多源共振环境态 / 信息面 Top
 * 聚合成一眼可扫的证据条。数据来自 TODAY.data.etf_overlay / resonance / info_brief。 */
function renderEtfFusion() {
  const box = $("#td-etf-fusion");
  if (!box) return;
  const d = TODAY.data;
  if (!d) { box.innerHTML = ""; return; }
  const ov = d.etf_overlay || {};
  const res = d.resonance || {};
  const ib = d.info_brief || {};
  const rows = [];
  const pct = (v) => briefPct(v);
  const lvlBadge = (lv) => {
    const c = lv === "强" ? "#0a7d3b" : lv === "中" ? "#0E7490"
      : lv === "弱" ? "#b42318" : "#888";
    return `<span class="etf-conf-badge" style="color:${c};font-weight:600">ETF${esc(lv)}</span>`;
  };

  // 1) 板块资金流确认（按净申赎强度排序，取前 4）
  const sf = toArr(ov.sector_flow).slice()
    .sort((a, b) => Math.abs(b.net_subscribe_yi || 0) - Math.abs(a.net_subscribe_yi || 0))
    .slice(0, 4);
  if (sf.length) {
    const cells = sf.map((x) =>
      `${esc(x.track)} ${x.avg_pct == null ? "" : pct(x.avg_pct)}`
      + `<i class="bf-n">${x.net_subscribe_yi >= 0 ? "+" : ""}${x.net_subscribe_yi}亿</i> ${lvlBadge(x.level)}`
    ).join(" ｜ ");
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">ETF资金流</span><span class="s">${cells}</span></div>`);
  }

  // 2) 跨境风险传导（海外风险向 A 股传导的领先信号）
  const cb = ov.cross_border || {};
  if (cb.risk) {
    const flagged = toArr(cb.items).filter((x) => x.flag).slice(0, 3)
      .map((x) => `${esc(x.name || x.code)} ${x.pct == null ? "" : pct(x.pct)}<i class="bf-n">${esc(x.flag)}</i>`)
      .join(" ｜ ");
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">跨境传导</span><span class="s" style="color:#b42318">${flagged || "海外风险信号"}</span></div>`);
  } else {
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">跨境传导</span><span class="s muted">境外 ETF 平稳（无 pct≤-2% / 溢价≥5% 过热）</span></div>`);
  }

  // 3) 宽基仓位 regime（长线资金托底信号）
  const bb = ov.broad_based || {};
  if (bb.regime) {
    const regTxt = bb.regime === "risk_on" ? "长线资金净流入（risk_on）"
      : bb.regime === "risk_off" ? "长线资金净流出（risk_off）" : "中性（neutral）";
    const regColor = bb.regime === "risk_on" ? "#0a7d3b" : bb.regime === "risk_off" ? "#b42318" : "#888";
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">宽基仓位</span><span class="s"><b style="color:${regColor}">${regTxt}</b>`
      + `${bb.net_subscribe_yi != null ? ` · 净申赎 ${bb.net_subscribe_yi >= 0 ? "+" : ""}${bb.net_subscribe_yi}亿` : ""}`
      + `${bb.avg_pct != null ? " · " + pct(bb.avg_pct) : ""}</span></div>`);
  }

  // 4) 多源共振环境态
  const ms = res.market_state;
  if (ms) {
    const tops = toArr(res.top_sectors).slice(0, 5)
      .map((s) => `<span class="bf-chip">${esc(s[0])}<i>${s[1]}</i></span>`).join("");
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">多源共振</span><span class="s">环境态 <b>${esc(ms)}</b>${tops ? " · 共振板块：" + tops : ""}</span></div>`);
  }

  // 5) 信息面 Top
  const items = toArr(ib.items);
  if (items.length) {
    const tops = items.slice(0, 3).map((x) => `${esc(x.title || x.src || "")}`).join(" ｜ ");
    rows.push(`<div class="q-ref-row"><span class="q-ref-tag">信息面</span><span class="s muted">${tops}</span></div>`);
  }

  if (!rows.length) {
    box.innerHTML = `<div class="q-ref-row muted">ETF / 多源共振 / 信息面信号暂未就绪（详见对应独立页）</div>`;
    return;
  }
  const asof = ov.as_of ? `<span class="s muted" style="margin-left:8px">数据截至 ${esc(ov.as_of)}</span>` : "";
  box.innerHTML = `<div class="td-fusion-hd"><b>ETF 融合信号</b>${asof}</div>` + rows.join("");
}

/* ================= 三个「结论页」（2026-10-12 体验审计 P0-D/E/F） =================
   报告要求给「消息与外围 / 别人怎么做 / 我的票」三个大类各置顶一个汇总结论，
   替掉「6~9 个名单并列、得自己交叉比对」。三页共用 .td-* / .bf-* 版式：
   结论条 → 分区列表（可点个股）→ 口径折叠。数据来自 /api/info_brief / smart_money / watch_brief。 */
const BRIEF = { info: null, money: null, watch: null };
const BRIEF_URL = { info: "/api/info_brief", money: "/api/smart_money", watch: "/api/watch_brief" };
const BRIEF_BOX = { info: "#p-info_brief", money: "#p-smart_money", watch: "#p-watch_brief" };

function loadBrief(kind) {
  return api(BRIEF_URL[kind]).then((d) => { BRIEF[kind] = d; renderBrief(kind); });
}
function briefSym(code, name) { return codeLink(code, { text: name || code, cls: "td-sym" }); }
function briefPct(v) {
  return v == null ? `<span class="td-pct">—</span>`
    : `<span class="td-pct ${v >= 0 ? "bull" : "bear"}">${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%</span>`;
}
function briefShell(verdict, meta, body, basis) {
  return `<div class="td-verdict bf-verdict"><span class="td-v-tag">结论</span>`
    + `<b class="td-v-text">${verdict}</b><span class="td-v-meta">${meta || ""}</span></div>`
    + body
    + (basis ? `<details class="td-basis"><summary>口径说明（这些结论是怎么来的）</summary>`
        + `<div class="td-basistxt">${esc(basis)}</div></details>` : "");
}
function briefSec(title, sub, rows, empty) {
  return `<div class="td-card bf-card"><div class="td-hd"><b>${esc(title)}</b>`
    + `<span class="td-hd-sub">${esc(sub || "")}</span></div>`
    + `<div class="td-body">${rows || `<div class="td-empty">${esc(empty || "无")}</div>`}</div></div>`;
}
function briefRow(main, sub, side) {
  return `<div class="td-row"><span class="td-main">${main}`
    + (sub ? `<span class="td-sub">${sub}</span>` : "") + `</span>`
    + (side ? `<span class="td-side">${side}</span>` : "") + `</div>`;
}
function briefChips(arr, fmt) {
  return arr.length ? `<div class="bf-chips">${arr.map(fmt).join("")}</div>` : "";
}

function renderBrief(kind) {
  const box = $(BRIEF_BOX[kind]);
  if (!box) return;
  const d = BRIEF[kind];
  if (!d || d.ok === false) { box.innerHTML = `<div class="empty">正在汇总…</div>`; return; }

  if (kind === "info") {
    const items = toArr(d.items), secs = toArr(d.sectors);
    const verdict = items.length
      ? `今日消息面重心：<b>${esc((secs[0] && secs[0].sector) || "—")}</b> 等 <b>${secs.length}</b> 个方向被政策 / 公告 / 海外同时点到；Top${items.length} 条见下。`
      : "本次窗口内没有取到新的政策 / 公告 / 海外消息。";
    const rows = items.map((x) => briefRow(
      `<span class="bf-src">${esc(x.src)}</span>${esc(x.title)}`,
      (x.sectors.length ? `影响板块：${x.sectors.slice(0, 6).map((s) => esc(s)).join("、")}` : "")
        + (x.note ? (x.sectors.length ? " · " : "") + esc(x.note) : "")
        + (toArr(x.symbols).length ? " · " + toArr(x.symbols).slice(0, 4).map((s) => briefSym(s.code, s.name)).join(" ") : ""),
      esc(x.tag) + (x.date ? `<br>${esc(x.date)}` : "")
    )).join("");
    const body = (d.stale_warn ? `<div class="bf-warn">⚠️ ${esc(d.stale_warn)}</div>` : "")
      + briefSec("影响板块 · 被最多消息点到", `${secs.length} 个`,
          briefChips(secs, (s) => `<span class="bf-chip">${esc(s.sector)}<i>${s.n}</i></span>`),
          "本次窗口内没有带板块标签的消息")
      + briefSec(`今日消息面 Top${items.length}`, "政策 / 国际 / 海外 / 公告 / 快讯", rows, "本次窗口内没有新消息")
      + briefSec("各源数据日", "时效自查（陈旧源不会混进 Top）",
          `<div class="bf-freshrow">${toArr(d.freshness).map((f) => `<span class="bf-fresh">${esc(f.label)} <b>${esc(f.latest)}</b></span>`).join("")}</div>`, "—");
    box.innerHTML = briefShell(verdict, `窗口 ${d.window_hours} 小时 · 生成于 ${esc(d.generated_at)}`, body, d.basis);
    return;
  }

  if (kind === "money") {
    const cons = toArr(d.consensus), dirs = toArr(d.directions), rec = toArr(d.recent);
    const d0 = dirs[0];
    const verdict = d0
      ? `聪明钱最集中的方向是 <b>${esc(d0.sector)}</b>：${d0.n} 只共识票、合计 <b>${d0.buy_holders}</b> 人次「喊买入」。`
      : `当前没有达到门槛的多人共识（至少 ${d.min_holders} 人）。`;
    const dirRows = dirs.map((x) => briefRow(
      `<b>${esc(x.sector)}</b>`,
      x.stocks.slice(0, 10).map((s) => briefSym(s.symbol, s.name) + `<i class="bf-n">${s.buy_holders}</i>`).join(" "),
      `${x.n} 只 · ${x.buy_holders} 人次`
    )).join("");
    const consRows = cons.map((x) => briefRow(
      briefSym(x.symbol, x.name) + (x.sector ? `<span class="bf-tag">${esc(x.sector)}</span>` : ""),
      `喊买入 <b>${x.buy_holders}</b> · 喊持有 ${x.hold_holders} · 持仓快照 ${x.watch_holders}`,
      x.pct == null ? "" : briefPct(x.pct)
    )).join("");
    const recRows = rec.map((x) => briefRow(
      briefSym(x.symbol, x.name),
      `近 ${d.days} 日 <b>${x.holders}</b> 人同向买入${x.buy_time ? " · 最近 " + esc(x.buy_time) : ""}`,
      esc(x.sector || "")
    )).join("");
    const body = briefSec("共识方向", "共识票扎堆的地方", dirRows, "没有 ≥2 只共识票重合的方向")
      + briefSec("共识榜 · 多少人喊买入", `门槛 ≥${d.min_holders} 人（已剔除「喊持有/关注」，避免虚增看多）`, consRows, "无")
      + briefSec(`近 ${d.days} 日共同买入`, "看的是动作时点，与「现在还有多少人拿着」不是一回事", recRows, "窗口内无多人同向买入");
    box.innerHTML = briefShell(verdict, `主线数据日 ${esc(d.mainline_date || "—")} · 生成于 ${esc(d.generated_at)}` +
      (d.data_asof ? ` · 数据截至 ${esc(d.data_asof)}` : ""), body, d.basis);
    return;
  }

  // watch
  const alerts = toArr(d.alerts), movers = toArr(d.movers), near = toArr(d.near),
    nt = toArr(d.no_target), caut = toArr(d.caution), ml = toArr(d.on_mainline), grp = toArr(d.groups);
  const parts = [];
  if (d.unread) parts.push(`${d.unread} 条到价提醒待看`);
  if (movers.length) parts.push(`当日异动最大 ${esc(movers[0].name)} ${movers[0].pct >= 0 ? "+" : ""}${Number(movers[0].pct).toFixed(2)}%`);
  if (caut.length) parts.push(`池内 ${caut.length} 只被看空/分歧`);
  if (ml.length) parts.push(`${ml.length} 只落在今日主线`);
  if (nt.length) parts.push(`${nt.length} 只开了提醒却没设目标价`);
  const verdict = `关注池 <b>${d.total}</b> 只：` + (parts.length ? parts.join("；") + "。" : "今日无异常。");
  const body =
    (nt.length ? `<div class="bf-warn">⚠️ 有 ${nt.length} 只已开启到价提醒但<b>没有设置目标价</b> —— 这些提醒永远不会触发。`
      + `去「关注池」给它们标个目标价，提醒才会生效。</div>` : "")
    + briefSec("① 到价触发", "已触发的提醒（未读优先）",
        alerts.map((a) => briefRow(briefSym(a.symbol, a.name),
          `${wtAlertRuleText(a.rule)}目标价 ${esc(String(a.target_price))} · 触发价 ${a.price == null ? "—" : esc(String(a.price))}`,
          esc(String(a.triggered_at || "").slice(0, 16)))).join(""), "无")
    + briefSec("② 池内当日异动", `报价源：${esc(d.quote_src || "—")}`,
        movers.map((q) => briefRow(briefSym(q.symbol, q.name), `现价 ${esc(String(q.price))}`, briefPct(q.pct))).join(""),
        "未取到行情（本机行情源不可达，稍后重试）")
    + briefSec("③ 接近目标价", "按与目标价的距离排序",
        near.map((n) => briefRow(briefSym(n.symbol, n.name),
          `现价 ${esc(String(n.last_price))} · 目标 ${esc(String(n.target_price))}`,
          `距 ${n.dist_pct >= 0 ? "+" : ""}${esc(String(n.dist_pct))}%`)).join(""), "没有已设目标价的票")
    + briefSec("④ 池内被看空 / 分歧", "近 72 小时帖子情绪（只认真分歧）",
        caut.map((c) => briefRow(briefSym(c.symbol, c.name),
          `${esc(c.text)}（看多 ${c.pos} / 看空 ${c.neg}）`,
          c.tone === "bear" ? "看空" : "分歧")).join(""), "池内没有被看空或真分歧的票")
    + briefSec("⑤ 池内落在今日主线", `数据日 ${esc(d.mainline_date || "—")}`,
        ml.map((m) => briefRow(briefSym(m.symbol, m.name),
          `${esc(m.sector)}${m.forward ? " · 同题材尚未大涨（可推进）" : ""}`,
          m.pct == null ? "" : briefPct(m.pct))).join(""), "池内票都不在今日主线名单里")
    + briefSec("分组", `${grp.length} 组`, briefChips(grp, (g) => `<span class="bf-chip">${esc(g.name)}<i>${g.n}</i></span>`), "—");
  box.innerHTML = briefShell(verdict, `生成于 ${esc(d.generated_at)}`, body, d.basis);
}

/* ---------- 集合竞价区块（2026-10-01 方案 A「放在今日总览里」） ----------
 * 数据随 /api/dashboard 一起下发（d.auction），不额外发请求。
 * 三条硬口径，与后端 auction.py 一一对应，界面上原样写出来：
 *   ① 「竞价」= 9:25 集合竞价定盘价（= 开盘价）；9:25 之后量额就滚进连续竞价了，
 *      所以**只有 9:25 那一瞬抓的快照才是竞价量**，盘后/回填的历史日量额一律显示「—」
 *   ② 新股首日(N)与除权除息(XD/XR/DR)剔除，它们的开盘涨幅不是同一把尺
 *   ③ 量比要凑够前 5 天的竞价量才算得出；样本不足就**不列**，不拿别的数凑
 * 无数据时说清为什么没数据，不编、不静默留白。 */
function renderAuctionBlock(a) {
  if (!a) return "";
  if (a.status !== "ok") {
    return `<div class="auc-card">
      <div class="auc-head"><span class="auc-t">集合竞价</span>
        <span class="auc-sub">09:25 定盘后自动采一次 / 历史可用日 K 回填</span></div>
      <div class="auc-empty">${esc(a.note || "暂无竞价数据")}</div>
    </div>`;
  }
  const k = a.counts || {};
  const chip = (code, name, pct) => `<span class="kline-chip chip-s" data-sym="${escAttr(code)}" title="${escAttr((name || "") + " " + code)} 点击看详情">${esc(name || code)}</span>`;
  const rowHtml = (c) => `<tr>
    <td>${chip(c.code, c.name)}</td>
    <td class="num ${c.pct == null ? "" : (c.pct >= 0 ? "bull" : "bear")}"><b>${c.pct == null ? "—" : fmtPct(c.pct)}</b></td>
    <td class="num s">${c.amount == null ? "—" : (c.amount / 1e8).toFixed(2) + " 亿"}</td>
    <td class="num s">${c.ratio == null ? "—" : c.ratio.toFixed(2)}</td>
  </tr>`;
  // 2026-10-22 全量整改 P1-54：h.date/up/down/avg_pct/amount_yi 原是未转义直插进 title 属性，
  // 归一逻辑一旦失效即成属性逃逸注入通道。统一改 escAttr()（属性上下文专用转义）。
  const hist = (a.history || []).map((h) => `<span class="auc-h" title="${escAttr(h.date)} · 涨 ${escAttr(h.up)} / 跌 ${escAttr(h.down)} · 均值 ${escAttr(h.avg_pct)}%${h.amount_yi == null ? "" : " · 额 " + escAttr(h.amount_yi) + " 亿"}">
      <i class="auc-h-d">${esc(h.date.slice(5))}</i>
      <i class="auc-h-n">${h.up}↑/${h.down}↓</i>
      <i class="auc-h-a ${h.avg_pct >= 0 ? "bull" : "bear"}">${h.avg_pct >= 0 ? "+" : ""}${h.avg_pct}%</i>
      ${h.real ? "" : `<i class="auc-h-b" title="历史回填日：只有竞价价，没有竞价量">回填</i>`}
    </span>`).join("");
  const kpiCls = (v, inv) => (v == null ? "" : (inv ? (v < 0 ? "bull" : "bear") : (v > 0 ? "bull" : "bear")));
  return `<div class="auc-card">
    <div class="auc-head">
      <span class="auc-t">集合竞价</span>
      <span class="auc-sub">${esc(a.date || "—")}${a.is_live ? " · 当日快照" : " · 最近有数据的交易日"}</span>
      ${a.is_live ? "" : `<span class="auc-stale" title="库内最新竞价数据不是今天（今日尚未采集或行情源未刷）；下面是基于最近有数据交易日的结果，不编今日数据">非今日数据</span>`}
      <span class="auc-spacer"></span>
      <span class="auc-live">${k.limit_up == null ? "" : `涨停意图 <b class="bull">${k.limit_intent}</b> 家`}</span>
    </div>
    <div class="auc-kpis">
      <span class="auc-kpi"><i>竞价上涨</i><b class="${kpiCls(k.up)}">${k.up == null ? "—" : k.up}</b><em>/ 全市场 ${k.total == null ? "—" : k.total} 只</em></span>
      <span class="auc-kpi"><i>竞价下跌</i><b class="${kpiCls(k.down, true)}">${k.down == null ? "—" : k.down}</b><em>平盘 ${k.flat == null ? "—" : k.flat}</em></span>
      <span class="auc-kpi"><i>竞价总额</i><b>${k.amount_yi == null ? "—" : k.amount_yi + " 亿"}</b><em>${k.amount_yi != null ? "竞价量/额合计" : (a.is_live ? "竞价额需 9:15–9:25 采集" : "历史回填日无竞价额")}</em></span>
      <span class="auc-kpi"><i>竞价均值</i><b class="${kpiCls(k.avg_pct)}">${k.avg_pct == null ? "—" : (k.avg_pct >= 0 ? "+" : "") + k.avg_pct + "%"}</b><em>涨超 3% ${k.up_gt3 == null ? "—" : k.up_gt3} 家</em></span>
    </div>
    <div class="auc-grid">
      ${(a.boards || []).map((bd) => `<div class="auc-col">
        <div class="auc-col-t">${esc(bd.title || "")} <span class="auc-col-sub">${esc(bd.sub || "")}</span></div>
        <table><thead><tr><th>个股</th><th class="num">竞价涨幅</th><th class="num">竞价额</th><th class="num">量比</th></tr></thead>
          <tbody>${(bd.rows || []).map(rowHtml).join("") || '<tr><td colspan="4" class="s">—</td></tr>'}</tbody></table>
      </div>`).join("")}
    </div>
    ${(a.history || []).length > 1 ? `<div class="auc-hist-t">近 ${a.history.length} 个交易日 · 竞价涨跌家数与均值</div><div class="auc-hist">${hist}</div>` : ""}
    <div class="auc-note">${esc(a.note || "")} <b>非买卖建议</b>。</div>
  </div>`;
}

function renderDashboard() {
  const box = $("#p-dashboard");
  if (!box) return;
  const d = DASH.data;
  if (!d) { box.innerHTML = `<div class="empty">主线研判加载中…</div>`; return; }

  const yi = (v) => (v == null ? "—" : (Number(v) / 1e8).toFixed(2) + " 亿");
  const pctS = (v) => (v == null ? "—" : fmtPct(v));
  const cls = (v) => (v == null ? "" : (Number(v) >= 0 ? "bull" : "bear"));
  const sym = (code, name, extra) =>
    `<span class="kline-chip chip-s" data-sym="${escAttr(code)}" title="${escAttr((name || "") + " " + code)} 点击看详情${extra ? " · " + escAttr(extra) : ""}">${esc(name || code)}</span>`;

  const ml = d.mainlines || {};
  const rows = ml.rows || [];
  const focus = ml.focus || [];

  // ---- 重点方向（置顶，单列出来，用颜色标出） ----
  const focusHtml = focus.length ? `
    <div class="ml-focus">
      <div class="ml-focus-t">今日重点方向 · 建议优先跟踪</div>
      <div class="ml-focus-list">
        ${focus.map((f, i) => `
          <span class="ml-focus-item lv-${escAttr(f.level || "hot")}">
            <b class="ml-focus-rank">${i + 1}</b>
            <span class="ml-focus-name">${esc(f.sector)}</span>
            <span class="ml-focus-meta">净买 ${f.net_yi} 亿 · 龙头 ${esc(f.leader_name || "—")} · 评分 ${f.score}</span>
          </span>`).join("")}
      </div>
      <div class="ml-focus-note">判定依据：资金净买居前 + 龙头封板 + 同题材多点上榜 + 分支延伸度。仅为方向参考，非买卖建议。</div>
    </div>` : "";

  // ---- 每条主线一张卡 ----
  const card = (m) => {
    const lv = m.level || "cool";
    const leader = m.leader_code ? `
      <div class="ml-row">
        <span class="ml-k">龙头</span>
        <span class="ml-v">
          ${sym(m.leader_code, m.leader_name, "龙头")}
          <span class="${cls(m.leader_pct_now)} ml-num">${pctS(m.leader_pct_now)}</span>
          <span class="ml-tag ml-tag-leader">资金 ${m.leader_net_yi} 亿</span>
        </span>
      </div>` : "";
    const tiers = (m.tiers || []).length ? `
      <div class="ml-row">
        <span class="ml-k">梯队</span>
        <span class="ml-v">${(m.tiers || []).slice(0, 10).map((t) =>
          `${sym(t.code, t.name)}<span class="${cls(t.pct)} ml-num ml-num-sm">${pctS(t.pct)}</span>`).join(" ")}</span>
      </div>` : "";
    const fwd = (m.forward || []).length ? `
      <div class="ml-row ml-row-fwd">
        <span class="ml-k"><span class="ml-star">★</span>可推进</span>
        <span class="ml-v">${(m.forward || []).slice(0, 8).map((t) =>
          `${sym(t.code, t.name, "同题材尚未大涨")}<span class="${cls(t.pct)} ml-num ml-num-sm">${pctS(t.pct)}</span>`).join(" ")}</span>
      </div>` : "";
    const br = (m.branches || []).length ? `
      <div class="ml-row">
        <span class="ml-k">分支</span>
        <span class="ml-v ml-branches">${(m.branches || []).slice(0, 8).map((b) =>
          `<span class="ml-branch" role="button" tabindex="0"
             title="与主线重合 ${b.overlap} 只 · 点开看名单、谁在买、怎么判断"
             data-sector="${escAttr(m.sector)}" data-branch="${escAttr(b.name)}"
             data-date="${escAttr(ml.date || "")}">${esc(b.name)}<i>${b.overlap}</i></span>`).join("")}</span>
      </div>` : "";

    return `
      <div class="ml-card lv-${escAttr(lv)}">
        <div class="ml-head">
          <span class="ml-rating lv-${escAttr(lv)}">${esc(m.rating || "观察")}</span>
          <b class="ml-sector">${esc(m.sector)}</b>
          <span class="ml-why">${esc(m.why || "")}</span>
          <span class="ml-stats">
            净买 <b>${m.net_yi}</b> 亿 · 上榜 <b>${m.onlist_n}</b> 家 · 评分 <b>${m.score}</b>
          </span>
        </div>
        ${leader}
        ${tiers}
        ${fwd}
        ${br}
      </div>`;
  };

  // ---- 底部：其它维度（压缩保留） ----
  const con = d.consensus || {};
  const coholdRows = (con.rows || []).map((c) =>
    `<tr><td>${sym(c.symbol, c.symbol_name)}</td>
      <td class="num"><b>${c.people_count}</b> 位</td>
      <td class="s">${esc((c.people || []).map((p) => p.name).slice(0, 6).join("、"))}</td></tr>`).join("");
  const fresh = (d.freshness || []).map((f) =>
    `<span class="chip-s" style="margin:2px 4px 2px 0">${esc(f.label)} <b>${esc(f.latest)}</b></span>`).join(" ");

  // 2026-10-12 体验审计 P1（① 今日看板）：全站最会给结论的面板，却缺一句「置顶结论」→ 补上。
  const f0 = focus[0];
  const dashVerdict = f0
    ? `今日资金主攻 <b>${esc(f0.sector)}</b>（净买 ${f0.net_yi} 亿 · 龙头 ${esc(f0.leader_name || "—")}）；共 <b>${rows.length}</b> 条主线在跟。`
    : "今日暂无主线数据（请先运行 sector_chain.py 生成）。";

  box.innerHTML = `
    ${renderAuctionBlock(d.auction)}   <!-- 2026-10-01 集合竞价：开盘前第一眼看的情绪，放整屏最上面 -->
    <div class="td-verdict"><span class="td-v-tag">结论</span>
      <b class="td-v-text">${dashVerdict}</b>
      <span class="td-v-meta">数据截至 ${esc(ml.date || "—")}</span></div>
    <div class="hint" style="margin-bottom:12px">
      <b>主线研判</b> —— 不列涨停榜，只答三件事：<b>资金在哪个方向</b> · <b>谁是龙头</b> · <b>同链条里谁还没动</b>。
      ${dataDayBadge(ml.date)}<span class="dd-note">数据截至 <b>${esc(ml.date || "—")}</b>（生成于 ${esc(d.generated_at || "—")}）。</span>
      <b>方向参考，非买卖建议</b>。
    </div>
    ${focusHtml}
    ${rows.length ? rows.map(card).join("") : `<div class="empty">暂无主线数据。请先运行 sector_chain.py 生成。</div>`}

    <div class="detail-sub" style="margin-top:20px">牛散共识 —— 共 <b>${con.star_count == null ? "—" : con.star_count}</b> 位牛散（跨≥2只股票），多方同持：</div>
    ${coholdRows ? `<table><thead><tr><th>个股</th><th class="num">同持牛散</th><th>名单</th></tr></thead><tbody>${coholdRows}</tbody></table>` : `<div class="empty">暂无协同持股</div>`}

    <div class="detail-sub">数据新鲜度</div>
    <div style="margin-bottom:16px">${fresh || "<span class='s'>—</span>"}</div>
  `;

  $$("#p-dashboard .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });

  /* 分支标签点开 = 展开「都有谁 / 谁在买 / 谁在说 / 怎么判断」——
     横条上光写个「人工智能 18」等于没给信息，用户要的是能照着判断的名单。 */
  $$("#p-dashboard .ml-branch").forEach((el) => {
    const go = () => openBranchModal(el.dataset.sector, el.dataset.branch, el.dataset.date);
    el.onclick = go;
    el.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
    };
  });
}

/* 分支透视弹窗的 ESC 监听句柄（重开时先摘掉旧的，避免叠加）。 */
let _branchEsc = null;

/* 点看板上的「分支」→ 把那个数字展开成能照着判断的东西。
   数据来自后端 /api/branch_detail：成员股、龙虎榜买方席位（含该席位历史 3 日涨概率）、
   博主/选手覆盖、补涨候选、判断依据。全部读本地库与行情快照，秒回，不联网。 */
function openBranchModal(sector, branch, date) {
  const prev = document.getElementById("branchModal");
  if (prev) prev.remove();
  if (_branchEsc) { document.removeEventListener("keydown", _branchEsc); _branchEsc = null; }
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "branchModal";
  overlay.innerHTML = `
    <div class="modal-card branch-modal" role="dialog" aria-label="分支透视">
      <div class="bm-head">
        <div class="bm-head-l">
          <h3>${esc(sector)} · ${esc(branch)}</h3>
          <div class="bm-sub">正在展开这个分支…</div>
        </div>
        <div class="bm-tools">
          <button class="bm-max" title="最大化 / 还原">⛶</button>
          <button class="bm-x" title="关闭">×</button>
        </div>
      </div>
      <div class="bm-body"><div class="bm-loading">正在取该分支的成员股、买方席位与博主覆盖…</div></div>
    </div>`;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector(".bm-x").onclick = () => overlay.remove();
  const onEsc = (e) => {
    if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", onEsc); _branchEsc = null; }
  };
  _branchEsc = onEsc;
  document.addEventListener("keydown", onEsc);
  document.body.appendChild(overlay);

  /* 拖动 / 最大化（2026-09-27）：标题栏按住可拖，拖动后切 bm-floating（脱离 flex 居中、改固定定位）；
     「⛶」铺满视口并把长名单表格放到 58vh，解决「信息看不全」；两者都钳在视口内，按钮不会被拖出屏幕。 */
  const bmCard = overlay.querySelector(".branch-modal");
  const bmHead = overlay.querySelector(".bm-head");
  const bmMaxBtn = overlay.querySelector(".bm-max");
  const bmClamp = () => {
    const vw = window.innerWidth, vh = window.innerHeight;
    const w = bmCard.offsetWidth, h = bmCard.offsetHeight;
    const minL = Math.min(0, 160 - w), maxL = Math.max(0, vw - w);
    const maxT = Math.max(0, vh - h);
    bmCard.style.left = Math.round(Math.max(minL, Math.min(bmCard.offsetLeft, maxL))) + "px";
    bmCard.style.top = Math.round(Math.max(0, Math.min(bmCard.offsetTop, maxT))) + "px";
  };
  let bmSX = 0, bmSY = 0, bmL = 0, bmT = 0;
  bmHead.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button")) return;   // 点按钮不触发拖拽
    if (overlay.classList.contains("bm-maxed")) return;         // 最大化状态下先还原再拖
    const r = bmCard.getBoundingClientRect();
    bmSX = e.clientX; bmSY = e.clientY; bmL = r.left; bmT = r.top;
    bmCard.style.left = Math.round(bmL) + "px";
    bmCard.style.top = Math.round(bmT) + "px";
    overlay.classList.add("bm-floating");
    const move = (ev) => {
      bmCard.style.left = Math.round(bmL + ev.clientX - bmSX) + "px";
      bmCard.style.top = Math.round(bmT + ev.clientY - bmSY) + "px";
      bmClamp();
    };
    const up = () => {
      bmClamp();
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    e.preventDefault();
  });
  bmMaxBtn.onclick = () => {
    const on = overlay.classList.toggle("bm-maxed");
    overlay.classList.remove("bm-floating");
    bmCard.style.left = ""; bmCard.style.top = "";
    bmMaxBtn.classList.toggle("on", on);
    bmMaxBtn.textContent = on ? "❐" : "⛶";
    bmMaxBtn.title = on ? "还原" : "最大化 / 还原";
  };

  const qs = "?sector=" + encodeURIComponent(sector || "") +
             "&branch=" + encodeURIComponent(branch || "") +
             (date ? "&date=" + encodeURIComponent(date) : "");
  api("/api/branch_detail" + qs).then((d) => {
    const sub = overlay.querySelector(".bm-sub");
    if (sub && d && d.available) {
      sub.innerHTML = `与主线重合 <b>${esc(String(d.overlap))}</b> 只 · 数据日 ${esc(d.date || "—")}`;
    }
    const body = overlay.querySelector(".bm-body");
    if (body) body.innerHTML = renderBranchModalBody(d, sector, branch);
    $$("#branchModal .bm-chip[data-sym]").forEach((el) => {
      el.onclick = () => {
        // 让位于个股详情（2026-09-27）：遮罩转透明 + 不再拦点击，弹窗整层降到详情面板之下。
        // 否则 .modal-overlay 的 z-index:9999 会压住 #drawer(70) → 详情看不见、点不到、也拖不动。
        overlay.classList.add("bm-behind");
        openStock(el.dataset.sym, el);
      };
    });
  }).catch((e) => {
    const body = overlay.querySelector(".bm-body");
    if (body) body.innerHTML = `<div class="bm-warn">查询失败：${esc(String((e && e.message) || e))}</div>`;
  });
}

function renderBranchModalBody(d, sector, branch) {
  if (!d || !d.available) {
    return `<div class="bm-warn">${esc((d && d.message) || "暂无该分支数据。")}</div>`;
  }
  const j = d.judge || {};
  const pctTxt = (p) => fmtPct(p);
  const pctCls = (p) => (p == null ? "" : (p > 0 ? "bull" : (p < 0 ? "bear" : "")));
  const st = d.stocks || [];
  const by = d.buyers || [];
  const ad = d.advocates || [];
  const cd = j.candidates || [];
  let h = "";

  /* ① 结论先行：一句话说清这个分支此刻什么状态 */
  h += `<div class="bm-judge bm-${escAttr(j.tone || "cool")}">${esc(j.text || "")}</div>`;

  /* 0 这个标签怎么读 —— 把 chip 上的数字翻译成结论（要回答：写了什么、为什么下此结论） */
  {
    const ov = (d.overlap == null) ? "—" : String(d.overlap);
    const reso = (d.reso == null) ? "—" : String(d.reso);
    const wide = (d.wide == null) ? "—" : String(d.wide);
    const why = (d.overlap == null)
      ? `「${esc(branch)}」是全市场概念、未挂在某个主线板块下，故无「与主线交集」数；下面的成员名单与市场共振，按该概念的全市场归属统计。`
      : `主线「${esc(d.sector_label || sector)}」与「${esc(branch)}」概念重合 <b>${ov}</b> 只成分股；其中 <b>${reso}</b> 只当日上了龙虎榜，<b>全市场</b>同概念当日上榜 <b>${wide}</b> 只。`;
    h += `<div class="bm-read">
      <div class="bm-read-h">这个标签怎么读</div>
      <div class="bm-read-b">
        <div class="bm-read-row"><span class="bm-rk">交集 <b>${ov}</b></span><span class="bm-rv">与主线重合的成分股数量（东财 F10 单板块上限 300，属可见下限）</span></div>
        <div class="bm-read-row"><span class="bm-rk">主线内上榜 <b>${reso}</b></span><span class="bm-rv">这批重合股里，当日实际登上龙虎榜的只数</span></div>
        <div class="bm-read-row"><span class="bm-rk">市场 <b>${wide}</b></span><span class="bm-rv">全市场同概念当日上龙虎榜的只数（不限于主线）</span></div>
        <div class="bm-read-row"><span class="bm-rk">状态 <b class="bm-state">${esc(d.state || "—")}</b></span><span class="bm-rv">${esc(d.state_rule || "")}</span></div>
      </div>
      <div class="bm-read-why">${why}</div>
    </div>`;
  }

  /* ① 交集的是哪些票 —— 与主线重合的那批成员股（对应 chip 上的「交集 N」） */
  {
    const mainLabel = (d.overlap == null)
      ? `该概念全市场成员股`
      : `与「${esc(d.sector_label || sector)}」重合 ${esc(String(d.overlap))} 只`;
    h += `<div class="bm-sec">① 交集的是哪些票<span class="bm-sec-n">${mainLabel}` +
         (d.members_total > st.length ? `，列前 ${st.length} 只` : "") + `</span></div>`;
  }
  if (st.length) {
    h += `<div class="bm-tw"><table class="bm-table"><thead><tr>
      <th>个股</th><th class="num">涨幅</th><th class="num">龙虎榜净买(亿)</th><th>上榜原因</th>
    </tr></thead><tbody>` + st.map((s) => `<tr>
      <td><span class="kline-chip chip-s bm-chip" data-sym="${escAttr(s.code)}" title="点击看个股详情">${esc(s.name || s.code)}</span><span class="bm-code">${esc(s.code)}</span></td>
      <td class="num bm-p ${pctCls(s.pct)}">${pctTxt(s.pct)}</td>
      <td class="num">${s.on_lhb && s.net_yi != null ? esc(String(s.net_yi)) : '<span class="s">—</span>'}</td>
      <td class="bm-reason">${esc(s.reason || "—")}</td>
    </tr>`).join("") + `</tbody></table></div>`;
  } else {
    h += `<div class="bm-warn">该分支暂无可列出的成员股。</div>`;
  }

  /* ② 市场共振的是哪几只 —— 全市场同概念当日上龙虎榜（对应 chip 上的「市场 N」） */
  {
    const wl = d.wide_list || [];
    h += `<div class="bm-sec">② 市场共振的是哪几只<span class="bm-sec-n">全市场同概念当日上龙虎榜 <b>${esc(String(d.wide == null ? "—" : d.wide))}</b> 只</span></div>`;
    if (wl.length) {
      h += `<div class="bm-tw"><table class="bm-table"><thead><tr>
        <th>个股</th><th class="num">涨幅</th><th class="num">净买(亿)</th><th>上榜原因</th><th>是否在主线交集</th>
      </tr></thead><tbody>` + wl.map((s) => `<tr>
        <td><span class="kline-chip chip-s bm-chip" data-sym="${escAttr(s.code)}" title="点击看个股详情">${esc(s.name || s.code)}</span><span class="bm-code">${esc(s.code)}</span></td>
        <td class="num bm-p ${pctCls(s.pct)}">${pctTxt(s.pct)}</td>
        <td class="num">${s.net_yi == null ? '<span class="s">—</span>' : esc(String(s.net_yi))}</td>
        <td class="bm-reason">${esc(s.reason || "—")}</td>
        <td>${s.on_mainline ? '<span class="bm-tag bm-tag-in">主线内</span>' : '<span class="bm-tag bm-tag-out">概念内</span>'}</td>
      </tr>`).join("") + `</tbody></table></div>`;
    } else {
      h += `<div class="bm-warn">当日全市场同概念没有个股上龙虎榜——也就是这个方向「尚未启动」（主线内的重合股本身仍可看上面①）。</div>`;
    }
  }

  /* ③ 谁在买 —— 成员股的龙虎榜买方席位（这是「谁」里最硬的一路：真金白银） */
  h += `<div class="bm-sec">② 谁在买<span class="bm-sec-n">该分支成员的龙虎榜买方席位，按净买排序</span></div>`;
  if (by.length) {
    h += `<div class="bm-tw"><table class="bm-table"><thead><tr>
      <th>买方席位</th><th>标的</th><th class="num">净买(亿)</th><th class="num">该席位历史3日涨概率</th>
    </tr></thead><tbody>` + by.map((b) => `<tr>
      <td class="bm-seat">${esc(b.name)}</td>
      <td>${codeLink(b.code, { text: b.stock || b.code })}</td>
      <td class="num">${esc(String(b.net_yi))}</td>
      <td class="num">${b.rise_prob_3d == null ? "—" : Number(b.rise_prob_3d).toFixed(1) + "%"}</td>
    </tr>`).join("") + `</tbody></table></div>`;
    h += `<div class="bm-tip">「历史3日涨概率」是该营业部/机构<b>过往买入后 3 日上涨的占比</b>，属统计参照，<b>不是对个股的预测</b>。</div>`;
  } else {
    h += `<div class="bm-warn">该分支成员本期未上龙虎榜，看不到席位数据。</div>`;
  }

  /* ④ 谁在说 —— 博主 / 实盘选手提到过该概念的票（带原话与原帖） */
  h += `<div class="bm-sec">④ 谁在说<span class="bm-sec-n">博主 / 实盘选手提到过该概念的票</span></div>`;
  if (ad.length) {
    h += `<div class="bm-adv">` + ad.map((a) => `<div class="bm-adv-i">
      <span class="bm-adv-who">${esc(a.who)}</span>
      <span class="bm-adv-act">${esc(a.action || a.kind || "")}</span>
      <span class="kline-chip chip-s bm-chip" data-sym="${escAttr(a.symbol)}">${esc(a.name || a.symbol)}</span>
      ${a.on_mainline ? '<span class="bm-tag bm-tag-in">主线内</span>' : '<span class="bm-tag bm-tag-out">概念内</span>'}
      ${a.text ? `<span class="bm-adv-txt" title="${escAttr(a.text)}">“${esc(String(a.text).slice(0, 42))}”</span>` : ""}
      ${a.url ? `<a class="bm-adv-lnk" href="${escAttr(a.url)}" target="_blank" rel="noopener">原帖</a>` : ""}
    </div>`).join("") + `</div>`;
  } else {
    h += `<div class="bm-warn">该分支暂无博主/选手覆盖——本地库的持仓与讨论帖和这批票没有交集，属数据缺口，不代表没人提。</div>`;
  }

  /* ⑤ 同方向还没动的：分支已经启动，但这些票涨幅还很低（补涨观察位） */
  if (cd.length) {
    h += `<div class="bm-sec">⑤ 同方向还没动的<span class="bm-sec-n">分支已启动、个股涨幅 &lt;3%</span></div>`;
    h += `<div class="bm-cands">` + cd.map((c) => `<span class="bm-cand-i"><span class="kline-chip chip-s bm-chip" data-sym="${escAttr(c.code)}" title="点击看个股详情">${esc(c.name || c.code)}</span><span class="bm-p ${pctCls(c.pct)}">${pctTxt(c.pct)}</span></span>`).join("") + `</div>`;
  }

  /* ⑥ 对你的参考 —— 由上面事实外推，明确标注为推断 */
  h += `<div class="bm-refer"><div class="bm-refer-h">对你的参考 <span class="bm-tag-infer">推断</span></div>
    <div class="bm-refer-b">
      <div>· 资金是否已向该概念外溢：看「市场共振」里全市场同概念上榜只数——<b>${esc(String(d.wide == null ? "—" : d.wide))}</b> 只，状态「${esc(d.state || "—")}」。≥5 只属资金已扩散，1–4 只属局部共振，0 只说明仅主线内有重合、外圈还没动。</div>
      <div>· 补涨观察位：① 里<b>还没上榜且涨幅偏低</b>的重合股，是「同方向但还没被资金拉到位」的观察候选（⑤ 也单列了涨幅 &lt;3% 的）。</div>
      <div>· 口径提醒：交集数是东财 F10 概念归属的可见下限（单板块上限 300 只），并非全市场真值；本弹窗只读本地库与龙虎榜，不做任何消息面外推。</div>
    </div></div>`;

  h += `<details class="bm-src"><summary>判断依据与数据口径</summary><div>
    ${(j.basis || []).map((x) => `<div>· ${esc(x)}</div>`).join("")}
    <div>· ${esc(d.note || "仅为方向参考，非买卖建议。")}</div>
  </div></details>`;
  return h;
}

/* ---------- 牛散动向（报告期变动流：新进 / 增持 / 减持 / 退出） ---------- */
const STARM = { data: null, sel: null };
function loadStarMoves() {
  return api("/api/star_moves").then((d) => { STARM.data = d; STARM.sel = null; renderNsMoves($("#nsBody")); });
}
function renderNsMoves(box) {
  if (!box) return;
  const d = STARM.data;
  if (!d) { box.innerHTML = `<div class="empty">牛散动向加载中…</div>`; return; }
  if (d.empty || !(d.rows || []).length) {
    box.innerHTML = `<div class="empty">暂无牛散动向数据。需先抓取权威股东披露（holders_fetch.py）。</div>`;
    return;
  }
  const rows = d.rows || [];
  const sel = STARM.sel || rows[0].date;
  const cur = rows.find((r) => r.date === sel) || rows[0];
  // 报告期横排：同样限最近 10 个 + 日期查询（进哥要求全站统一）
  const chips = dayChips(rows, cur.date, "data-sm-date", {
    keyOf: (r) => r.date,
    renderOf: (r) => `${esc(r.date)} <span class="s">${r.counts.new}新/${r.counts.add}增/${r.counts.cut}减/${r.counts.exit}退</span>`,
    title: "查询任意报告期",
  });
  const badge = (k) => {
    const m = { new: ["新进", "bull"], add: ["增持", "bull"], cut: ["减持", "bear"], exit: ["退出", "bear"] };
    const [t] = m[k];
    return `<span class="src-tag ${k === "new" || k === "add" ? "src-port" : "src-comm"}">${t}</span>`;
  };
  const sec = (key, title, arr) => {
    if (!arr.length) return `<div class="view-group"><div class="vg-head">${title}（0）</div><div class="vg-empty">无</div></div>`;
    const trs = arr.map((x) => `<tr>
        <td><b>${esc(x.name)}</b></td>
        <td><span class="kline-chip chip-s" data-sym="${escAttr(x.symbol)}" title="点击看这只股的详情">${esc(x.symbol)}</span> ${esc(x.symbol_name || "")}</td>
        <td class="num">${x.ratio != null ? Number(x.ratio).toFixed(2) + "%" : "—"}</td>
        <td class="num s">${esc(x.change || "")}</td>
      </tr>`).join("");
    return `<div class="view-group">
      <div class="vg-head">${badge(key)} ${title}（${arr.length}）</div>
      <table><thead><tr><th>牛散</th><th>股票</th><th class="num">占流通股</th><th class="num">变动</th></tr></thead><tbody>${trs}</tbody></table>
    </div>`;
  };
  const s = d.summary || {};
  box.innerHTML = `
    <div style="margin-bottom:10px"><b>报告期</b>：${chips}</div>
    <div class="s" style="margin-bottom:12px">
      共 <b>${s.star_count ?? "—"}</b> 位「牛散」（跨 ≥2 家上市公司的自然人），覆盖 ${s.period_count ?? "—"} 个报告期。
      变动口径：<b>新进</b>=本期新出现在十大流通股东；<b>增持/减持</b>=持股数较上期同向变动；<b>退出</b>=上期在榜、本期已不在。
      数据源：交易所定期披露（季报／临时公告），非实时。
    </div>
    ${sec("new", "新进", cur.new || [])}
    ${sec("add", "增持", cur.add || [])}
    ${sec("cut", "减持", cur.cut || [])}
    ${sec("exit", "退出", cur.exit || [])}
    ${d.basis ? `<div class="s" style="margin-top:10px">口径：${esc(d.basis)}</div>` : ""}`;
  $$("#nsBody [data-sm-date]").forEach((el) => {
    el.onclick = () => { STARM.sel = el.dataset.smDate; renderNsMoves($("#nsBody")); };
  });
  // 报告期查询框：可直达库内任意报告期（超出范围给提示并回退）
  const _nsJump = $("#nsBody .day-jump");
  if (_nsJump) {
    _nsJump.onchange = () => {
      const v = _nsJump.value;
      const days = (_nsJump.dataset.days || "").split(",");
      if (!v || !days.includes(v)) {
        setHint("该期不是库内报告期。可选范围：" + (days[0] || "—") + " ~ " + (days[days.length - 1] || "—"));
        _nsJump.value = cur.date || "";
        return;
      }
      STARM.sel = v;
      renderNsMoves($("#nsBody"));
    };
  }
  $$("#nsBody .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}

/* ================= 方向研判报告（板块动向表格下方的分节分析，2026-09-26） =================
   需求原话：「什么原因导致这几个板块上涨？牵扯到哪些板块？预测接下来有哪些？要分开写。」
   故按三段独立成节：① 为什么涨  ② 牵涉哪些板块  ③ 接下来看什么（推断）。
   数据全部来自后端 /api/lhb_sectors 的 outlook 字段（本地库真实统计，后端已给口径）；
   前端只做展示，不重算、不补数——字段缺失就显示「—」，绝不编。 */
function outlookHtml(ol) {
  if (!ol || !(ol.sectors || []).length) return "";
  const pctS = (v) => (v == null ? "—" : fmtPct(v));
  const pctC = (v) => (v == null ? "" : (v > 0 ? "bull" : (v < 0 ? "bear" : "")));
  const sym = (c, n, p) => (c
    ? `<span class="kline-chip chip-s ol-sym" data-ol-sym="${escAttr(c)}" title="${escAttr((n || "") + " " + c)} 点击看详情">${esc(n || c)}</span>`
    : `<span class="chip-s">${esc(n || "")}</span>`)
    + (p == null ? "" : ` <span class="ol-p ${pctC(p)}">${pctS(p)}</span>`);

  // ---------- 一、为什么涨 ----------
  const whyCards = (ol.sectors || []).map((s) => {
    const forms = (s.forms || []).map((f) => `<span class="ol-form">${esc(f.k)} <b>${f.n}</b></span>`).join("");
    const c = s.capital || {};
    const ld = s.leader ? `<div class="ol-kv"><span class="ol-k">资金锚</span><span class="ol-v">龙头
      <b>${esc(s.leader.name)}</b> <span class="ol-p ${pctC(s.leader.pct)}">${pctS(s.leader.pct)}</span>
      · 单只净买 <b>${s.leader.net_yi}</b> 亿</span></div>` : "";
    const fell = (s.fell || []).length ? `<div class="ol-warn">⚠ 其中 ${s.fell.length} 只属「下跌上榜」：
      ${s.fell.map((x) => esc(x.name) + (x.pct == null ? "" : " " + pctS(x.pct))).join("、")}
      —— 这条线上已有票在被砸，分歧已出现，别把「上了龙虎榜」一概当利好。</div>` : "";
    const notes = (s.notes || []).length
      ? `<div class="ol-note">原始榜单评语（东财原文，仅作参照）：${s.notes.map((x) => esc(x)).join("；")}</div>` : "";
    return `<div class="ol-card">
      <div class="ol-card-h"><b>${esc(s.sector)}</b>
        <span class="ol-card-m">净买 ${s.net_yi} 亿 · ${s.codes_n} 只上榜 · ${esc(s.kind || "")}</span></div>
      <div class="ol-kv"><span class="ol-k">资金形态</span><span class="ol-v">${forms || "—"}</span></div>
      <div class="ol-kv"><span class="ol-k">买方席位</span><span class="ol-v">机构专用 <b>${c.inst_seats}</b> 席
        （净买 ${c.inst_net_yi} 亿）· 深沪股通 <b>${c.connect_seats}</b> 席 ·
        营业部 <b>${c.hot_seats}</b> 席（净买 ${c.hot_net_yi} 亿）</span></div>
      ${ld}
      <div class="ol-why">${esc(s.why || "")}</div>${fell}${notes}
    </div>`;
  }).join("");

  // ---------- 二、牵涉哪些板块 ----------
  const spillBlocks = (ol.sectors || []).map((s) => {
    if (!(s.spill || []).length) return "";
    const rows = s.spill.map((b) => {
      const st = b.state === "已扩散" ? "ol-st-on" : (b.state === "局部共振" ? "ol-st-mid" : "ol-st-off");
      return `<span class="ol-chip ${st} ol-click" data-ol-sector="${escAttr(s.sector)}" data-ol-branch="${escAttr(b.name)}" data-ol-date="${escAttr(ol.date)}" title="点击展开「${escAttr(b.name)}」透视报告：交集名单 / 市场共振名单 / 为什么下此结论">${esc(b.name)}<i>交集${b.overlap}·市场${b.wide}</i><b>${esc(b.state)}</b></span>`;
    }).join("");
    const on = s.spill.filter((b) => b.wide >= 1).length;
    return `<div class="ol-spill"><div class="ol-spill-h"><b>${esc(s.sector)}</b> 的外溢链
      <span class="ol-card-m">${s.spill.length} 个邻近方向，其中 ${on} 个当日已出现同概念上榜</span></div>
      <div class="ol-chips">${rows}</div></div>`;
  }).join("");

  const hubs = (ol.hubs || []).length ? `<div class="ol-spill"><div class="ol-spill-h"><b>枢纽个股</b>
    <span class="ol-card-m">同时出现在 2 个以上重点板块的上榜名单里——这几个板块正是靠它串起来的</span></div>
    <div class="ol-chips">${ol.hubs.map((h) => `<span class="ol-chip ol-st-hub ol-click" data-ol-sym="${escAttr(h.code)}" title="${escAttr((h.sectors || []).join(" / "))}：点击看个股详情">${esc(h.name || h.code)}<i>${esc((h.sectors || []).join(" / "))}</i></span>`).join("")}</div></div>` : "";

  const shared = (ol.shared_leader || []).length ? `<div class="ol-note">同一龙头被多个方向共用：${ol.shared_leader.map((x) => `${esc(x.name)}（${esc((x.sectors || []).join("、"))}）`).join("；")} —— 方向之间高度重叠，本质是同一笔资金的同一个故事。</div>` : "";

  // ---------- 三、接下来看什么 ----------
  const dirs = (ol.relay_dirs || []).length
    ? `<div class="ol-chips">${ol.relay_dirs.map((d) => `<span class="ol-chip ol-st-relay ol-click" data-ol-branch="${escAttr(d.name)}" data-ol-date="${escAttr(ol.date)}" title="点击展开「${escAttr(d.name)}」透视报告：当日全市场该概念上榜名单">${esc(d.name)}<i>${d.n} 只</i></span>`).join("")}</div>`
    : `<div class="ol-note">当日没有识别到额外的资金聚集方向。</div>`;

  const relays = (ol.sectors || []).map((s) => {
    if (!(s.relay || []).length) return "";
    return `<div class="ol-relay"><div class="ol-relay-h"><b>${esc(s.sector)}</b>
      <span class="ol-card-m">梯队内尚未涨停 ${s.relay_n} 只（龙头已封板，能接力的在这批）</span></div>
      <div class="ol-chips">${s.relay.map((x) => sym(x.code, x.name, x.pct)).join("")}</div></div>`;
  }).join("");

  const probs = (ol.sectors || []).map((s) => ({ s: s.sector, p: (s.capital || {}).seat_prob_avg })).filter((x) => x.p != null);
  const probLine = probs.length ? `<div class="ol-note">买方席位历史倾向：${probs.map((x) => `${esc(x.s)} ${x.p}%`).join(" · ")}
    —— 席位过往买入后 3 日上涨占比的历史统计，不是对个股的预测；低于 50% 说明这批席位历史上偏短线。</div>` : "";

  const basis = (ol.basis || []).map((b) => `<div>· ${esc(b)}</div>`).join("");

  return `<div class="ol-wrap">
    <div class="ol-h"><div class="ol-h-t">方向研判报告</div>
      <div class="ol-h-s">龙虎榜资金结构 + 概念交集 + 席位历史的可解释分析 · 数据日 ${esc(ol.date || "—")} ·
        <span class="ol-tag-fact">事实</span>${esc(ol.kind_note || "")}</div></div>

    <div class="ol-sec"><div class="ol-sec-t"><span class="ol-sec-n">一</span>为什么涨 —— 资金为什么选中这几个方向</div>
      <div class="ol-cards">${whyCards}</div></div>

    <div class="ol-sec"><div class="ol-sec-t"><span class="ol-sec-n">二</span>牵涉哪些板块 —— 同一笔钱还会流向哪</div>
      ${spillBlocks}${hubs}${shared}</div>

    <div class="ol-sec"><div class="ol-sec-t"><span class="ol-sec-n">三</span>接下来看什么 <span class="ol-tag-infer">推断</span></div>
      <div class="ol-relay"><div class="ol-relay-h"><b>资金已聚集、但未被本表列为重点的方向</b>
        <span class="ol-card-m">当日全市场该概念上龙虎榜的只数（已剔除风格 / 指数等统计口径）</span></div>${dirs}</div>
      ${relays}${probLine}</div>

    <details class="ol-src"><summary>判断依据与数据口径（点开看，不藏算法）</summary>${basis}</details>
  </div>`;
}

function renderLhbSectors() {
  const box = $("#p-lhb_sectors");
  if (!box) return;
  const d = LBHS.data;
  if (!d) { box.innerHTML = `<div class="empty">板块动向加载中…</div>`; return; }
  if (d.empty || !(d.rows || []).length) {
    box.innerHTML = `<div class="empty">暂无「板块动向」数据。<br>
      该板块由 lhb_sector_link.py 离线计算后落库，随每次全量更新自动跑一次。</div>`;
    return;
  }
  const s = d.summary || {};
  const chips = dayChips(d.dates, d.date, "data-lhs-date");   // 只渲染近 10 个，其余走日期查询
  const rows = (d.rows || []).map((r) => {
    const stocks = (r.onlist || []).map((o) => {
      const code = o && o.code ? o.code : "";
      const nm = o && o.name ? o.name : "";
      if (!code) return `<span class="chip-s" style="margin:1px 3px 1px 0">${esc(nm)}</span>`;
      return `<span class="kline-chip chip-s" data-sym="${escAttr(code)}" style="margin:1px 3px 1px 0"
        title="${escAttr(nm + " " + code)} 点击看详情">${esc(nm)}</span>`;
    }).join("");
    const ex1 = r.excess_t1;
    const exCls = ex1 == null ? "" : (ex1 >= 0 ? "bull" : "bear");
    return `<tr>
      <td><b>${esc(r.sector)}</b></td>
      <td class="num"><b>${r.onlist_n}</b> 只</td>
      <td class="num"><b>${amtYi(r.net_amt)}</b></td>
      <td>${stocks}</td>
      <td class="num">${r.members_n == null ? "—" : r.members_n}</td>
      <td class="num">${r.sampled == null ? "—" : r.sampled}</td>
      ${lhbPct(r.t1_avg)}
      ${lhbPct(r.t3_avg)}
      <td class="num ${exCls}">${ex1 == null ? "—" : fmtPct(ex1)}</td>
      <td class="num">${lhbRate(r.t1_win)}</td>
    </tr>`;
  }).join("");
  const exT1 = s.excess_t1, exT3 = s.excess_t3;
  const exT1H = exT1 == null ? "—" : fmtPct(exT1);
  const exT3H = exT3 == null ? "未走完" : fmtPct(exT3);
  box.innerHTML = `
    <div style="margin-bottom:10px">${dataDayBadge(d.date)}<b>交易日</b>：${chips}</div>
    <div class="hint" style="margin-bottom:10px">
      <b>这是「方向罗盘」，不是交易信号。</b>龙虎榜上的票多为游资 / 量化拉升，涨幅常已在高位，
      <b>不具备跟买的可操作性</b>；本板块只看<b>资金是否成片扫向同一个方向</b>——同一天同一板块被多只票上榜、
      且净买额放大，才说明资金形成合力。下表 T+1 / T+3 是<b>板块内没被买到的成分股</b>在该上榜日之后的平均涨幅（回看验证），
      用来判断这个方向有没有延续性，<b>不构成买卖建议</b>。与「游资龙虎榜」的个股榜是两套独立口径，不要混看。
    </div>
    <div class="s" style="margin-bottom:10px">
      当日板块 <b>${(d.rows || []).length}</b> 个 · 板块内未上榜股 T+1 平均
      <b class="${exT1 == null ? "" : (exT1 >= 0 ? "bull" : "bear")}">${exT1H}</b> · T+3 平均
      <b class="${exT3 == null ? "" : (exT3 >= 0 ? "bull" : "bear")}">${exT3H}</b>
      <span class="s">（超额＝板块平均 − 同期上证；${esc(d.basis || "")}）</span>
    </div>
    <table><thead><tr>
      <th>板块</th><th class="num">上榜股数</th><th class="num">净买额</th><th>上榜个股（点击看详情）</th>
      <th class="num">板块股数</th><th class="num">有效样本</th>
      <th class="num">T+1 均涨</th><th class="num">T+3 均涨</th><th class="num">超额 T+1</th><th class="num">T+1 上涨比</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <div class="s" style="margin-top:8px">更新时间：${esc(d.updated_at || "—")}。板块归属取自东财 F10 题材库，
      已剔除指数成分与风格 / 行为类板块（如「昨日涨停」「高送转」等统计口径）。</div>
    ${outlookHtml(d.outlook)}`;
  $$("#p-lhb_sectors [data-lhs-date]").forEach((el) => {
    el.onclick = () => loadLhbSectors(el.dataset.lhsDate)
      .catch((e) => setHint("板块动向日期切换失败：" + apiErrText(e)));
  });
  // 交易日只显示近 10 个，其余用日期框查询（与龙虎榜同一套 dayChips 逻辑）
  const _lhsJump = $("#p-lhb_sectors .day-jump");
  if (_lhsJump) {
    _lhsJump.onchange = () => {
      const v = _lhsJump.value;
      const days = (_lhsJump.dataset.days || "").split(",");
      if (!v || !days.includes(v)) {
        setHint("该日不是库内交易日。可选范围：" + (days[0] || "—") + " ~ " + (days[days.length - 1] || "—"));
        _lhsJump.value = d.date || "";
        return;
      }
      loadLhbSectors(v).catch((e) => setHint("板块动向日期切换失败：" + apiErrText(e)));
    };
  }
  $$("#p-lhb_sectors .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
  $$("#p-lhb_sectors [data-ol-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.olSym, el);
  });
  // 外溢 / 接力方向 chip：点开分支透视弹窗（外溢带主线，接力方向无主线 → sector 传空走降级）
  $$("#p-lhb_sectors [data-ol-sector]").forEach((el) => {
    el.onclick = () => openBranchModal(el.dataset.olSector, el.dataset.olBranch, el.dataset.olDate);
  });
  $$("#p-lhb_sectors [data-ol-branch]:not([data-ol-sector])").forEach((el) => {
    el.onclick = () => openBranchModal("", el.dataset.olBranch, el.dataset.olDate);
  });
}
function renderPlayers(list) {
  const all = toArr(list);
  if (!all.length) { $("#p-players").innerHTML = `<div class="empty">暂无入选实盘选手。请先点「抓取并更新」(players 模式)。</div>`; return; }
  // 2026-09-24：库中的源 URL 全部为 example.local/demo 的条目视为「示例数据」。
  // 真实抓取尚未打通（淘股吧登录态无效），整张表目前是合成数据，刷新不会改变时间戳。
  // 这里把示例/真实分开计数，并在顶部横幅明示，避免用户反复刷新以为有 bug。
  const isDemoRow = (p) => /example\.local/i.test(p.source_url || "");
  const demoN = all.filter(isDemoRow).length;
  const realN = all.length - demoN;
  const tierCount = {};
  const chCount = {};
  all.forEach((p) => {
    const t = p.tier || p.channel || "D";
    tierCount[t] = (tierCount[t] || 0) + 1;
    if (p.channel) chCount[p.channel] = (chCount[p.channel] || 0) + 1;
  });
  // 竖向筛选项：层级 A~F + 渠道 A~D 一行一项
  const tierBtns = ["A", "B", "C", "D", "E", "F"].map((t) =>
    `<button class="fbtn" data-tier="${t}" onclick="filterPlayers('tier','${t}')">` +
    `${esc(TIER_LABEL[t])}<span class="cnt">${tierCount[t] || 0}</span></button>`).join("");
  const chBtns = ["A", "B", "C", "D"].map((c) =>
    `<button class="fbtn" data-ch="${c}" onclick="filterPlayers('ch','${c}')">` +
    `${esc(CHANNEL_LABEL[c])}<span class="cnt">${chCount[c] || 0}</span></button>`).join("");
  const rows = all.map((p) => {
    const t = p.tier || p.channel || "D";
    const demo = isDemoRow(p);
    // 当前持仓 chips：最多展示 3 只，其余折进 "+N"；悬停 title 显示全量（含动作/时间/来源）
    // 2026-09-25 补：徽章内直接展示「操作时间」，不再只藏在 tooltip 里（与「共同持仓」口径一致）。
    // 源站未给时间的一律不显示时间标，绝不用抓取时刻冒充。
    const hls = toArr(p.holdings);
    // 2026-09-25：东财/同花顺为讨论区，平台无结构化真实持仓，标记为 discussion。
    // 不再显示空「—」(易被误认为漏抓)，改为诚实灰标「讨论活跃·无实盘源」。
    const isDiscussion = p.real_holding === "discussion";
    const isCommunity = p.real_holding === "community";
    const hlShown = hls.slice(0, 3).map((h) => {
      // 2026-09-25：标签按 source_type 渲染，与顶部图例保持一致。
      // 此前只渲染「实盘」一种，而图例却列出 实盘/组合/晒盘/提及 四类，属说明与实现不一致。
      const _st = h.source_type || "bar_post";
      const _isLive = _st === "player_page" || _st === "player_live";
      const _isComm = _st === "community";
      const srcTag = _isLive ? ` <span class="src-tag">实盘</span>`
                   : _isComm ? ` <span class="src-tag src-comm">晒盘</span>`
                   : ` <span class="src-tag src-mention">提及</span>`;
      const cls = "chip-s" + (h.name_only ? " chip-nm" : "");
      const title = `${h.display} ${h.action || ""} ${h.post_time || ""}` +
        ` | 来源:${SRC_TYPE_LABEL[h.source_type] || h.source_type || "个股讨论页提及"}` +
        (h.name_only ? " | 仅有股票名(待匹配代码)" : "");
      const clk = h.symbol ? ` data-sym="${escAttr(h.symbol)}"` : "";
      // 2026-09-26 王总需求：本列的**主信息是建仓/操作时间**。
      // 「关注/操作」这类笼统归类占版面却不携带区分度（本页 267/271 条都是同一个值），不再渲染。
      // 时间标签改为紧贴股票名（第一位），源站没给就明说「未提供」，绝不用抓取时刻冒充。
      const _pt = (h.post_time || "").trim();
      const _tShort = _pt.length >= 16 ? _pt.slice(5, 16)
                    : _pt.length >= 10 ? _pt.slice(5, 10) : "";
      const timeTag = _tShort
        ? ` <span class="chip-time" title="${escAttr(_pt.length >= 16 ? "操作时间 " + _pt : "操作日期 " + _pt)}">${esc(_tShort)}</span>`
        : "";   // 没有时间就留空 —— 逐条挂「未提供」是新的噪音，改由面板顶部统一交代来源
      // 仅当动作能区分状态（买入/卖出/持仓…）时才上徽章；无区分度值一律不显示。
      const _act = String(h.action || "").trim();
      const actTag = (_act && !NO_ACTION_BADGE.has(_act)) ? " " + actionBadge(_act) : "";
      return `<span class="${cls}${h.symbol ? " kline-chip" : ""}"${clk} title="${escAttr(title)}">${esc(h.display)}${timeTag}${actTag}${srcTag}</span>`;
    }).join(" ");
    const hlMore = hls.length > 3
      ? `<span class="chip-s" title="${escAttr(hls.map((h) => `${h.display} ${h.action || ""} ${h.post_time || ""}`).join("\n"))}">+${hls.length - 3}</span>`
      : "";
    const holdingsCell = isDiscussion
      ? `<span class="dim" title="东财/同花顺为讨论区，平台无结构化真实持仓，仅作人气/讨论活跃标记">讨论活跃·无实盘源</span>`
      : (hls.length ? `${hlShown} ${hlMore}` : `<span class="dim">—</span>`);
    // data-real/data-demo：CSS 用来给整行加灰底/警示底色
    return `<tr data-name="${escAttr(p.name)}" data-ch="${escAttr(p.channel || '')}" data-tier="${escAttr(t)}" ${demo ? 'data-demo="1"' : 'data-real="1"'}${isDiscussion ? ' data-noreal="1"' : ''}>
      <td class="num">${esc(p.rank_position ?? "-")}</td>
      <td><b>${esc(p.name)}</b>${demo ? ` <span class="chip-s chip-demo" title="该选手来自合成示例数据，尚未连接真实抓取">示例</span>` : ""}${isDiscussion ? ` <span class="chip-s chip-disc" title="讨论活跃牛人：东财/同花顺平台无结构化真实持仓，仅有人气/讨论度">讨论</span>` : ""}${isCommunity ? ` <span class="chip-s chip-comm" title="社区晒盘：集思录用户手动记录的实盘/持仓，非券商交割单，仅供参考">晒盘</span>` : ""}</td>
      <td>${tierBadge(t)}</td>
      <td>${channelBadge(p.channel)}</td>
      <td>${holdingsCell}</td>
      <td class="num">${p.ranking_score ?? "-"}</td>
      <td><span class="chip-s">影响 ${p.influence != null ? p.influence : "-"}</span></td>
      <td>${p.is_top_player ? "✅入选" : "—"}</td>
      <td>${srcLink(p.source_url, "主页")}</td>
      ${refCell([p.platform || p.site])}
    </tr>`;
  }).join("");
  // 顶部横幅：实时说清当前数据性质 + 真实抓取卡点（登录态）
  const banner = realN === 0
    ? `<div class="banner banner-warn">
        <b>当前展示的是示例数据</b>（${all.length} 条），库中所有选手 URL 都是 <code>example.local/demo</code>。
        真实抓取卡在<b>淘股吧登录态</b>上：登录检测返回 <b>login_failed</b>，所以抓取只能拿到公开热搜、不会更新选手库。
        <br>解决办法：点右上角「登录检测」旁的<b>「点此登录」</b>按钮，程序会自动打开浏览器、登录成功后自动抓 cookie 存库。
       </div>`
    : `<div class="banner banner-info">真实抓取 ${realN} 条 / 示例 ${demoN} 条</div>`;
  $("#p-players").innerHTML = `
    ${banner}
    <div class="hint" style="margin-bottom:12px">多层级人物覆盖：在原有 A/B/C/D 渠道基础上新增 <b>E 主播大V</b>（热度不高但影响力突出）与 <b>F 长尾观察</b> 两层。低层级权重递减，但<b>一律露出</b>（is_visible=1），可查看其观点与持仓，不因热度低而隐藏。<b>「当前持仓」</b>= 从公开帖可解析到的该选手标的（每只取最近一次动作）。<b>标的右侧的时间＝该来源给出的建仓/操作日期</b>；源站没给的一律<b>留空</b>——已逐源核实：淘股吧博主页与集思录晒单<b>都不提供逐笔建仓日</b>，所以本表多数标的时间为空，属<b>源头缺失、非抓取遗漏</b>（也绝不用抓取时刻冒充）。抓取不到持仓的显示「—」。东财/同花顺的人气牛人已明确标注为「讨论活跃·无实盘源」（平台为讨论区，无结构化真实持仓，不计入实盘覆盖）。<b>持仓性质标签</b>：<span class="src-tag">实盘</span>=淘股吧交割单（最硬真盘） / <span class="src-tag src-port">组合</span>=雪球·摩尔跟随真实调仓(带仓位%) / <span class="src-tag src-comm">晒盘</span>=集思录手动记账(非券商交割单) / <span class="src-tag src-mention">提及</span>=个股讨论页提到(非持仓)。${freshLegend()}</div>
    <div class="panel-split">
      <aside class="filter-col" aria-label="筛选选手">
        <div class="fc-h">按层级</div>
        <button class="fbtn active" data-tier="ALL" onclick="filterPlayers('tier','ALL')">全部层级<span class="cnt">${all.length}</span></button>
        ${tierBtns}
        <div class="fc-sep"></div>
        <div class="fc-h">按渠道</div>
        <button class="fbtn active" data-ch="ALL" onclick="filterPlayers('ch','ALL')">全部渠道<span class="cnt">${all.length}</span></button>
        ${chBtns}
      </aside>
      <div class="panel-body">
        <table><thead><tr><th class="num">名次</th><th>选手</th><th>层级</th><th>渠道</th><th>当前持仓</th><th class="num">评分/收益</th><th>影响力</th><th>筛选</th><th>来源</th><th>最后更新</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>
    </div>`;
  bindRows();
  applyFreshness();
}
function filterPlayers(attr, val) {
  $$("#p-players tbody tr").forEach((tr) => {
    if (val === "ALL") { tr.style.display = ""; return; }
    const key = attr === "tier" ? tr.dataset.tier : tr.dataset.ch;
    tr.style.display = (key === val) ? "" : "none";
  });
  // 竖向筛选列的高亮：层级与渠道各自保留当前选择（互不干扰）
  $$(`#p-players .filter-col .fbtn[data-${attr}]`).forEach((b) => {
    b.classList.toggle("active", (b.dataset[attr] || "") === val);
  });
}

/* ---------- 多人共同持仓共识 ---------- */
/* 2026-09-26：点击某只股票 → 统一走 openStock（完整个股详情：行情 + K 线 + 持仓选手）。
   本行已有的共识信息（人数/加权/层级/渠道/热度/来源）作为「④ 共识概况」追加到抽屉末尾。
   整条记录缓存进模块级 Map，而不是塞进行内 data-* 属性（长 JSON 易被转义破坏且拖慢 DOM）；
   取不到时优雅降级为只打开个股抽屉。每次渲染前清空，避免残留陈旧行。 */
const CONSENSUS_BY_SYM = new Map();

/* 2026-10-10 口径改革（王总批准）：把「喊买入(BUY)」与「喊持有(HOLD)/持仓关注快照(WATCH)」拆开计数。
   旧口径把 HOLD 混进「看多人数」，实测中国平安 39 人=买24+持15、平安银行 31 人=买14+持17
   （持有比买入还多），被当成「看多共识」前列。现后端已分三类返回：
     buy_holders / hold_holders / watch_holders，holders 为三类去重总数。
   前端用三种不同颜色的徽标区分，HOLD 单独一栏，不再与 BUY 笼统并列。 */

/* 共识面板状态：min + 「仅看入围选手(Tier A)」开关。
   ⚠️ 过滤走**后端参数** only_qualified（server.py → db.consensus_stocks），
   理由：只有后端能在 Join players 拿到 tier，且过滤后的「去重人数」才是真值——
   若改成前端过滤，各股 holders 仍是全量口径，会与表格里露出的明细自相矛盾。 */
const CONSENSUS = { min: 2, onlyQualified: false };

function consensusLoad() {
  return api(`/api/consensus?min_holders=${CONSENSUS.min}` +
             (CONSENSUS.onlyQualified ? "&only_qualified=1" : ""))
    .then((x) => renderConsensus(toArr(x)))
    .catch((e) => reportLoadError("共同持仓共识", e));
}
function consensusToggleQualified(el) {
  CONSENSUS.onlyQualified = !!(el && el.checked);
  consensusLoad();
}

/* 信号徽标：接口 signal ∈ {BUY, HOLD, WATCH} → 三种不同颜色，明确区分「喊买入 / 喊持有 / 持仓关注」。 */
function signalBadge(sig) {
  const m = { BUY: ["bull", "喊买入"], HOLD: ["hold", "喊持有"], WATCH: ["neu", "持仓/关注"] };
  const cls = m[sig] ? m[sig][0] : "neu";
  const txt = m[sig] ? m[sig][1] : "—";
  return `<span class="badge ${cls}">${esc(txt)}</span>`;
}

/* 「仅看入围选手」开关（共识 / 共同买卖两个面板共用）。 */
function qualifiedToggleHtml(scope) {
  const on = scope === "cohold" ? COHOLD.onlyQualified : CONSENSUS.onlyQualified;
  const fn = scope === "cohold" ? "coholdToggleQualified" : "consensusToggleQualified";
  return `<label class="q-toggle" title="仅统计入围选手（players.tier='A'）；取消勾选恢复全量（A~F/社区晒盘）">` +
    `<input type="checkbox"${on ? " checked" : ""} onchange="${fn}(this)"> <b>仅看入围选手</b>（Tier A）</label>`;
}

/* 共识「口径拆分」单元格：总数（中性）+ 买入(红) / 持有(琥珀) / 关注(灰) 三个分项。
   兼容性：若后端尚未重启（返回值里还没有 buy_holders/hold_holders），退回旧的单一「共同持仓」徽标，
   绝不在缺乏字段时渲染成「买 0 持 0」那样误导人的空口径。 */
function consensusSplitCell(x) {
  if (x.buy_holders == null && x.hold_holders == null) {
    return `<span class="badge bull" title="旧口径（BUY+HOLD 合并计数）——后端未重启，口径拆分字段缺失">${x.holders ?? "—"} 人</span>` +
      `<br/><span class="chip-s">口径拆分待后端重启生效</span>`;
  }
  const buy = x.buy_holders ?? 0;
  const hold = x.hold_holders ?? 0;
  const watch = x.watch_holders ?? 0;
  return `<span class="badge neu" title="共同持仓/关注总人数（买+持+关注，按选手去重）">${x.holders ?? "—"} 人</span>` +
    `<br/><span class="badge bull" title="明确「喊买入」(BUY) 的人数——「看多共识」应看这个">买 ${buy}</span> ` +
    `<span class="badge hold" title="明确「喊持有」(HOLD) 的人数，不与买入合并计数">持 ${hold}</span>` +
    (watch ? ` <span class="badge neu" title="持仓/关注快照（positions：关注/操作、社区晒盘），非明确喊买">关注 ${watch}</span>` : "");
}

/* 口径拆分（抽屉版）：字段缺失时整行不渲染，避免旧后端下出现「买 0 持 0」。 */
function consensusSplitKv(x) {
  if (x.buy_holders == null && x.hold_holders == null) return "";
  return `<div class="kv"><div class="k">口径拆分</div><div><span class="badge bull">喊买入 ${esc(x.buy_holders ?? 0)}</span> <span class="badge hold">喊持有 ${esc(x.hold_holders ?? 0)}</span> <span class="badge neu">持仓关注 ${esc(x.watch_holders ?? 0)}</span></div></div>`;
}

function consensusExtraHtml(x) {
  if (!x) return "";
  const w = x.weighted_holders != null ? x.weighted_holders : x.holders;
  const channels = toArr(x.channels).filter(Boolean);
  const tiers = toArr(x.tiers).map(tierBadge).join(" ") || "—";
  // 2026-10-06：抽屉内的来源也从"一坨完整 URL"改为站点名+条数，点「出处 N 条」展开原始链接
  const sources = toArr(x.sources).length ? srcCellHtml(x.sources) : `<span class="chip-s">—</span>`;
  return `<div class="detail-sub">④ 共识概况 —— 本行来自「共同持仓共识」板块</div>
    <div class="kv"><div class="k">共同持仓人数</div><div>${esc(x.holders ?? "—")} 人</div></div>
    ${consensusSplitKv(x)}
    <div class="kv"><div class="k">加权共识</div><div>${w != null ? "加权 " + esc(w) : "—"}${x.buy_weighted != null ? `（其中买入加权 ${esc(x.buy_weighted)}）` : ""}</div></div>
    <div class="kv"><div class="k">层级覆盖</div><div>${tiers}</div></div>
    <div class="kv"><div class="k">覆盖渠道</div><div>${channels.length ? channels.map(channelBadge).join(" ") : "—"}</div></div>
    <div class="kv"><div class="k">讨论热度</div><div>${esc(x.discuss_heat ?? "—")}</div></div>
    <div class="kv"><div class="k">来源网址</div><div>${sources || "—"}</div></div>`;
}

function renderConsensus(list) {
  const all = toArr(list);
  // 2026-10-15 P0-1：每行已携带 data_asof / positions_max_age_days（后端时效闸回带），
  // 取首行渲染「数据截至 YYYY-MM-DD」角标，避免把多年前的陈旧持仓快照误读为当下共识。
  const asof = (all[0] && all[0].data_asof) ? all[0].data_asof : "";
  const maxage = (all[0] && all[0].positions_max_age_days != null) ? all[0].positions_max_age_days : "";
  if (!all.length) {
    $("#p-consensus").innerHTML =
      `<div class="hint coh-bar" style="margin-bottom:10px">${qualifiedToggleHtml("consensus")}</div>` +
      `<div class="empty">暂无共同持仓数据。需先采集各选手持仓（collect_positions）。` +
      (CONSENSUS.onlyQualified ? `<br/>当前已勾选「仅看入围选手(Tier A)」——没有 Tier A 选手命中；取消勾选可恢复全量。` : "") +
      `</div>`;
    return;
  }
  CONSENSUS_BY_SYM.clear();   // 2026-09-26 每次渲染重建行缓存，防止旧行残留
  const rows = all.map((x) => {
    if (x && x.symbol) CONSENSUS_BY_SYM.set(x.symbol, x);   // 2026-09-26 供行点击时取用整条共识信息
    // channel 为空时不渲染角标：/api/consensus 的 trades 兜底源把 channel 置 NULL（db.py NULL AS channel），
    // 而 ch-* 类自带背景色（app.css:207）——空文本 + 有色背景会渲染成一个无文字色块（UI 回归 C4）。
    // 这里刻意沿用原「渠道字母」紧凑写法而非 channelBadge()：后者输出「A 实盘大赛」全称，
    // 会把每行选手明细撑长，与本区块的紧凑排版不符。
    const players = toArr(x.players).map((pl) => {
      const chBadge = pl.channel
        ? `<span class="badge ch-${escAttr(pl.channel)}">${esc(pl.channel)}</span> `
        : "";
      // 操作时间：trades 兜底源用 post_time（发帖时间≈动作时间），紧挨动作标签展示，鼠标悬停看完整时间
      const timeChip = pl.post_time
        ? ` <span class="chip-s" title="操作发帖时间 ${escAttr(pl.post_time)}">${esc(pl.post_time.slice(5, 16))}</span>`
        : "";
      return `<div class="chip-row">${chBadge}${esc(pl.player)} ${pl.signal ? signalBadge(pl.signal) : actionBadge(pl.action)}${timeChip} ${pl.ratio != null ? esc(pl.ratio) + "%" : ""}${pl.cleared ? ` <span class="badge sell-full" title="该选手对该标的累计卖出≥100%">已清仓</span>` : ""}</div>`;
    }).join("");
    // 2026-10-06：来源列改「站点名 + 条数 + 可展开原始链接」，不再堆一排无语义的 ↗
    const sources = toArr(x.sources).length ? srcCellHtml(x.sources) : `<span class="chip-s">—</span>`;
    // 覆盖渠道为空数组时给占位「—」，与抽屉渲染（app.js:1149 `${... || "—"}`）口径统一（UI 回归 C6）
    const channels = toArr(x.channels).filter(Boolean);
    // 层级加权共识度（A/B/C/D 沿用原权重，E/F 递减但仍计数）→ 比单纯"人数"更能代表真实共识强度
    const w = x.weighted_holders != null ? x.weighted_holders : x.holders;
    // 2026-10-10：主列不再是单一「看多人数」——拆成 总 / 买 / 持 / 关注，避免 HOLD 虚增看多。
    return `<tr data-sym="${escAttr(x.symbol)}">
      <td><b>${esc(x.symbol_name || "名称待补全")}</b><br/><span class="stk-code">${esc(x.symbol)}</span> <span class="stk-corp">${esc(x.company || "")}</span></td>
      <td>${consensusSplitCell(x)}</td>
      <td><span class="badge t-w" title="层级加权共识度（A~F 按权重求和，含买/持/关注）">加权 ${w ?? "—"}</span>${x.buy_weighted != null ? `<br/><span class="chip-s" title="其中「喊买入」的层级加权">买入加权 ${esc(x.buy_weighted)}</span>` : ""}</td>
      <td>${toArr(x.tiers).map(tierBadge).join(" ")}</td>
      <td>${channels.length ? channels.map(channelBadge).join(" ") : "—"}</td>
      <td><span class="chip-s">讨论热度 ${x.discuss_heat ?? "—"}</span><br/><span class="chip-s">平台 ${toArr(x.platforms).length}</span></td>
      <td>${players}</td>
      <td>${sources}</td>
      ${refCell(toArr(x.platforms))}
    </tr>`;
  }).join("");
  // 后端未重启时：/api/consensus 还会忽略 only_qualified → 勾选后行数不变。如实提示，不假装过滤成功。
  const staleBackend = CONSENSUS.onlyQualified && all.length > 0 && all[0].only_qualified === undefined;
  $("#p-consensus").innerHTML = `
    <div class="hint coh-bar" style="margin-bottom:10px">
      ${qualifiedToggleHtml("consensus")}
      ${asof ? `<span class="chip-asof" title="共识数据的最新采信时点；早于该日的陈旧持仓快照已被时效闸（默认 ${maxage} 天）剔除">📅 数据截至 ${esc(asof)}</span>` : ""}
      <span class="coh-lbl" title="口径改革：HOLD 不再并入 BUY">口径：<span class="badge bull">喊买入</span>=真实 BUY、<span class="badge hold">喊持有</span>=HOLD、<span class="badge neu">持仓/关注</span>=持仓快照（positions），三类分开计数、不合并。</span>
      ${staleBackend ? `<span class="chip-s" style="color:var(--bear)">⚠ 过滤未生效：后端仍是旧代码，请重启 viewer/server.py</span>` : ""}
      <span class="coh-grow"></span>
    </div>
    <div class="hint" style="margin-bottom:10px">按「共同持仓总人数」降序排列；共识基数已扩大到 <b>100+ 选手池</b>（覆盖淘股吧/东财/雪球/同花顺 4 平台）。<b>2026-10-10 口径改革</b>：把原先混算的 <b>HOLD（喊持有）</b> 从「看多人数」中剥离 ——「共同持仓」列现拆为 <b>总人数 / 买 N / 持 N / 关注 N</b>，<b>看多共识只看「买 N」</b>；「层级加权共识度」在 A/B/C/D 原权重基础上纳入 E/F 低层级（权重递减但仍计数），并列出<b>买入加权</b>；「层级」列展示跨 A~F 覆盖，「平台」列展示跨平台分布。已<b>一律剔除机器号</b>（资讯/播报类运营号；规则与采集端 bot_filter 对齐），避免其观点稀释真实共识。<b>「来源」列</b>=该股共识的<b>原始出处</b>（哪台哪个帖/主页），按站点归并计数（如「淘股吧 8」），点后面的<b>「出处 N 条 ▾」</b>可展开全部原始链接，逐条可点新窗口打开。${freshLegend()}</div>
    <table><thead><tr><th>股票</th><th>口径拆分（总 / 买 / 持 / 关注）</th><th>加权共识</th><th>层级覆盖</th><th>覆盖渠道</th><th>热度/平台</th><th>持仓选手明细</th><th>来源</th><th>最后更新</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  bindConsensus();
  applyFreshness();
}
/* ================= 近5日共同买入（Request F） =================
   与「共同持仓共识」并列的独立板块。两者答的不是同一个问题：
   - 共识板块：现在还有多少人**持有** → 持仓快照；
   - 本板块  ：最近 N 天里谁**同时动手**了 → 动作时点（用带时间的 buy_time/sell_time 过滤）。
   所以本板块能露出「窗口内已卖出」这类共识板块永远看不到的行。

   口径：窗口内 ≥min_holders 位选手对同一标的同向操作（默认 2 人 / 5 天）。
   跨表合并：positions（仓位占比/买价/现价）+ trades（**成交数量**、更精确成交时刻），
   同一选手同一标的合并为一条，字段取「最近一笔 + 该组内最丰富者」互补。
   锚定：默认锚定「数据最新日」——按今天算会全空（离线数据没跟上今天），看起来像坏了；
        可一键切到「按今日」。当前用哪种，界面上如实标注，不做静默替换。 */
const COHOLD = { days: 5, min: 2, anchor: "auto", data: null, onlyQualified: false };

function coholdLoad(days, anchor) {
  if (days) COHOLD.days = days;
  if (anchor) COHOLD.anchor = anchor;
  return api(`/api/cohold_recent?days=${COHOLD.days}&min_holders=${COHOLD.min}` +
             `&anchor=${encodeURIComponent(COHOLD.anchor || "auto")}` +
             (COHOLD.onlyQualified ? "&only_qualified=1" : ""))
    .then(renderCohold)
    .catch((e) => reportLoadError("近5日共同买入", e));
}
function coholdDays(n) { coholdLoad(n, null); }
function coholdAnchor(m) { coholdLoad(null, m); }
function coholdToggleQualified(el) {
  COHOLD.onlyQualified = !!(el && el.checked);
  coholdLoad(null, null);
}

/* 窗口条：明确告诉用户「这个窗口是按哪一天算的」——锚定口径不透明最容易让人误判成数据坏了 */
function coholdWindowBar(d) {
  const isToday = d.anchor_source === "today";
  const srcLabel = isToday ? `按今日（日历 ${d.days} 天）`
    : d.anchor_source === "latest_data" ? `数据最新日 ${esc(d.latest_data_date || "—")}`
      : `指定日期 ${esc(d.anchor || "—")}`;
  const offNote = (!isToday && d.latest_data_date && d.latest_data_date !== d.today)
    ? `<span class="chip-s" title="今天没有任何新数据，若按日历日算窗口会得到空结果；此处按数据最新日锚定，行尾会如实标注">今日 ${esc(d.today)} 尚无数据</span>`
    : "";
  const days = [3, 5, 10].map((n) =>
    `<button class="btn sm${n === d.days ? " primary" : ""}" onclick="coholdDays(${n})">${n} 天</button>`).join(" ");
  const anchors = [["auto", "锚定数据最新日"], ["today", "锚定今日"]]
    .map(([k, lab]) => {
      const on = (k === "auto") ? !isToday : isToday;
      return `<button class="btn sm${on ? " primary" : ""}" onclick="coholdAnchor('${k}')">${lab}</button>`;
    }).join(" ");
  return `<div class="coh-bar">
      <span>窗口 <b>${esc(d.window_start)} ~ ${esc(d.window_end)}</b></span>
      <span class="chip-s" title="窗口锚定口径">锚定：${srcLabel}</span>${offNote}
      <span class="coh-sep"></span><span class="coh-lbl">天数</span>${days}
      <span class="coh-sep"></span>${anchors}
      <span class="coh-sep"></span>${qualifiedToggleHtml("cohold")}
      ${(COHOLD.onlyQualified && d.only_qualified === undefined) ? `<span class="chip-s" style="color:var(--bear)">⚠ 过滤未生效：后端仍是旧代码</span>` : ""}
      <span class="coh-grow"></span>
      <span class="chip-s" title="同一标的被至少这么多位选手在窗口内同向操作才纳入">口径：≥${esc(d.min_holders ?? COHOLD.min)} 人同向</span>
    </div>`;
}

/* 2026-09-26：与共识板块同理——点击共同买入/卖出行的股票 → 统一走 openStock（含行情/K线）。
   整条记录按「组别::代码」缓存（买/卖可能含同一标的，故必须带组别前缀避免撞键）。
   每次渲染前清空；Map 未命中时优雅降级（只打开个股抽屉，不报错）。 */
const COHOLD_BY_KEY = new Map();

/* 组别元信息：buy / sell / hold 三组的标题、色调、动词、时间列名。
   2026-10-10：新增 hold 组（把 HOLD 从 BUY 中彻底分离出来，单独一栏显示）。 */
function cohGroupMeta(group) {
  if (group === "sell") return { tone: "bear", title: "共同卖出", verb: "卖出", tLabel: "卖出时间" };
  if (group === "hold") return { tone: "hold", title: "共同持有", verb: "表态持有", tLabel: "表态时间" };
  return { tone: "bull", title: "共同买入", verb: "买入", tLabel: "买入时间" };
}

function coholdExtraHtml(rec) {
  if (!rec || !rec.x) return "";
  const x = rec.x;
  const meta = cohGroupMeta(rec.group);
  // 2026-10-06：抽屉内的来源也从"一坨完整 URL"改为站点名+条数，点「出处 N 条」展开原始链接
  const urls = toArr(x.urls).length ? srcCellHtml(x.urls) : `<span class="chip-s">—</span>`;
  const range = (x.first_time || x.last_time)
    ? `${esc((x.first_time || "—").slice(0, 16))} → ${esc((x.last_time || "—").slice(0, 16))}`
    : "—";
  const win = (rec.windowStart && rec.windowEnd) ? `${esc(rec.windowStart)} ~ ${esc(rec.windowEnd)}` : "—";
  return `<div class="detail-sub">④ 共同买卖概况 —— 本行来自「近5日共同买入」板块</div>
    <div class="kv"><div class="k">组别</div><div><span class="badge ${meta.tone}">${meta.title}</span></div></div>
    <div class="kv"><div class="k">参与人数</div><div>${esc(x.player_count ?? "—")} 人</div></div>
    <div class="kv"><div class="k">合计数量</div><div>${x.total_quantity != null ? esc(x.total_quantity) + " 股" : "—"}</div></div>
    <div class="kv"><div class="k">均价</div><div>${x.avg_price != null ? "¥" + esc(x.avg_price) : "—"}</div></div>
    <div class="kv"><div class="k">时间范围</div><div>${range}</div></div>
    <div class="kv"><div class="k">窗口起止</div><div>${win}</div></div>
    <div class="kv"><div class="k">来源网址</div><div>${urls || "—"}</div></div>`;
}

function coholdTable(d, group) {
  const meta = cohGroupMeta(group);
  const isSell = group === "sell";
  const isHold = group === "hold";
  const list = toArr(d[group]);
  if (!list.length) {
    return `<h3 class="coh-h">${meta.title} <span class="chip-s">0 个标的</span></h3>
      <div class="empty">窗口 <b>${esc(d.window_start)} ~ ${esc(d.window_end)}</b> 内没有「≥${esc(d.min_holders ?? COHOLD.min)} 位选手同时${meta.verb}同一标的」的记录。<br/>
      ${group === "buy" ? "共买需要多位选手在 5 天内看上同一只票，比共卖更难触发，为空属正常。" : ""}
      ${COHOLD.onlyQualified ? "当前已勾选「仅看入围选手(Tier A)」——命中较少属正常，可放宽天数或取消勾选恢复全量。<br/>" : ""}
      可把天数放宽到 10 天，或切到「锚定数据最新日」再试。</div>`;
  }
  const rows = list.map((x) => {
    if (x && x.symbol) COHOLD_BY_KEY.set(group + "::" + x.symbol, { x, group, windowStart: d.window_start, windowEnd: d.window_end });   // 2026-09-26 键带组别前缀，买/卖各自独立
    const people = toArr(x.players).map((p) => {
      const ch = p.channel ? `<span class="badge ch-${escAttr(p.channel)}">${esc(p.channel)}</span> ` : "";
      const bits = [];
      if (p.price != null) bits.push(`¥${esc(p.price)}`);
      if (p.quantity != null) bits.push(`<b>${esc(p.quantity)}</b> 股`);
      if (!isSell) {
        // 买入组与持有组都展示仓位与浮盈（持有记录多半只有「持有」表态、无价无量，自然显示为空）
        if (p.ratio != null && p.ratio !== "") bits.push(`仓位 ${esc(p.ratio)}%`);
        if (p.pct_gain != null) {
          const c = p.pct_gain >= 0 ? "var(--bull)" : "var(--bear)";
          bits.push(`<b style="color:${c}">${p.pct_gain >= 0 ? "+" : ""}${esc(p.pct_gain)}%</b>`);
        }
      } else if (p.ratio != null && p.ratio !== "") {
        bits.push(`${p.ratio >= 100 ? "清仓" : "减仓"} ${esc(p.ratio)}%`);
      }
      if (p.ops > 1) bits.push(`<span class="chip-s">${p.ops} 笔</span>`);
      const st = isSell
        ? `<span class="badge bear">已卖出</span>`
        : (p.still_holding ? `<span class="badge hold">持有中</span>`
          : `<span class="badge bear">${isHold ? "已清仓" : "买入后已卖出"}</span>`);
      const from = toArr(p.evidence).join("+");
      return `<div class="coh-p">${ch}<b>${esc(p.player)}</b>` +
        `<span class="coh-t">${esc((p.last_time || "").slice(0, 16))}</span>` +
        `${bits.join(' <span class="coh-dot">·</span> ')} ${st}` +
        `<span class="coh-src" title="该条数据来自哪张表">${esc(from)}</span></div>`;
    }).join("");
    // 2026-10-06：来源列改「站点名 + 条数 + 可展开原始链接」，不再堆一排无语义的 ↗
    const urls = toArr(x.urls).length ? srcCellHtml(x.urls) : `<span class="chip-s">—</span>`;
    const status = isSell
      ? `<span class="chip-s">已卖出 ${esc(x.player_count ?? 0)} 人</span>`
      : `<span class="chip-s">持有 ${esc(x.still_holding ?? 0)} 人</span>` +
        (x.exited ? `<br/><span class="chip-s" style="color:var(--bear)">${isHold ? "已清仓" : "已卖出"} ${esc(x.exited)} 人</span>` : "");
    return `<tr data-sym="${escAttr(x.symbol)}" data-group="${escAttr(group)}">
      <td><b>${esc(x.symbol_name || "名称待补全")}</b><br/><span class="stk-code">${esc(x.symbol)}</span> <span class="stk-corp">${esc(x.company || "")}</span></td>
      <td><span class="badge ${meta.tone}">${esc(x.player_count ?? "—")} 人</span></td>
      <td><span class="chip-s">合计 ${x.total_quantity != null ? esc(x.total_quantity) + " 股" : "—"}</span><br/><span class="chip-s">均价 ${x.avg_price != null ? "¥" + esc(x.avg_price) : "—"}</span></td>
      <td><span class="chip-s">${esc((x.first_time || "").slice(0, 16))}</span><br/><span class="chip-s">→ ${esc((x.last_time || "").slice(0, 16))}</span></td>
      <td>${people}</td>
      <td>${status}</td>
      <td>${urls}</td>
      ${refCell(toArr(x.sites))}
    </tr>`;
  }).join("");
  return `<h3 class="coh-h">${meta.title} <span class="badge ${meta.tone}">${list.length} 个标的</span></h3>
    <table><thead><tr><th>股票</th><th>${meta.title}</th><th>合计数量 / 均价</th><th>时间范围</th>
      <th>参与者明细（${meta.tLabel} · 价格 · 数量 · ${isSell ? "卖出比例" : "仓位 · 浮盈"}）</th>
      <th>持有状态</th><th>来源</th><th>最后更新</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderCohold(d) {
  const box = $("#p-cohold");
  if (!box) return;
  if (!d || typeof d !== "object" || Array.isArray(d)) {
    box.innerHTML = `<div class="empty">近5日共同买入：接口返回类型不符，已降级为空。</div>`;
    return;
  }
  COHOLD.data = d;
  COHOLD_BY_KEY.clear();   // 2026-09-26 每次渲染重建行缓存（买/卖两表都会在 coholdTable 里写入）
  box.innerHTML = `
    <div class="hint" style="margin-bottom:10px">
      按「窗口内 ≥<b>${esc(d.min_holders ?? COHOLD.min)}</b> 位选手<b>同向操作</b>同一标的」聚合，<b>共买</b>（红）、<b>共卖</b>（绿）、<b>共持</b>（琥珀）分三组列出。
      与左侧「共同持仓共识」<b>口径不同</b>：共识看的是<b>现在还持有</b>（持仓快照），本板块看的是<b>最近 ${esc(d.days)} 天谁同时动手</b>（动作发生时点）——
      所以本板块能露出「窗口内已卖出」这类共识板块看不到的行。<b>2026-10-10 口径改革</b>：<b>HOLD（喊持有）单独成「共同持有」组</b>，不再并入「共同买入」——
      共买只统计真实 BUY。数据合并 <b>positions</b>（仓位占比/买价/现价）与 <b>trades</b>（<b>成交数量</b>、更精确的成交时刻）两张表。
      <b>「来源」列</b>=该标的动作的<b>原始出处</b>（帖子/主页链接），按站点归并计数，点<b>「出处 N 条 ▾」</b>展开全部原始链接。
      三组作者集合均已<b>剔除机器号</b>（资讯/播报号）。
    </div>
    ${coholdWindowBar(d)}
    <div class="coh-legend">
      <span class="badge bull">共同买入</span> <span class="coh-lbl">红 = 买（A股惯例·真实 BUY）</span>
      <span class="coh-sep"></span>
      <span class="badge bear">共同卖出</span> <span class="coh-lbl">绿 = 卖</span>
      <span class="coh-sep"></span>
      <span class="badge hold">共同持有</span> <span class="coh-lbl">琥珀 = HOLD（单独计数、不与买入合并）</span>
    </div>
    ${coholdTable(d, "buy")}
    ${coholdTable(d, "sell")}
    ${coholdTable(d, "hold")}
    <div class="hint" style="margin-top:12px">
      <b>数量</b>来自 trades 表的成交记录——positions 表没有这一列，所以只有持仓快照、没有成交明细的行会显示「—」，<b>不会用 0 或估算值顶替</b>。
      <b>浮盈</b>用同一笔的买价与现价计算，不跨笔混算。<b>持有状态</b>取「动作时点之后是否存在该选手对该标的的卖出记录」，有则记为「买入后已卖出 / 已清仓」。
      <b>共买组不含 HOLD</b>：三组按 action 精确分组（BUY / SELL / HOLD），互不重叠。${freshLegend()}
    </div>`;
  bindCohold();
  applyFreshness();
}

function bindCohold() {
  // 2026-09-26：改为打开完整个股抽屉（含行情/K线），本行共同买卖信息作为「④ 共同买卖概况」追加。
  // 键带组别前缀（buy::/sell::），因同一标的同时出现在买、卖两张表；Map 未命中则优雅降级。
  $$("#p-cohold tr[data-sym]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.tagName === "A") return;
      // 来源列的「出处 N 条」按钮/展开清单：只.toggle 自己的 DOM，不能穿透成 openStock
      if (e.target.closest && e.target.closest(".src-more, .src-list")) return;
      openStock(tr.dataset.sym, anchorOf(e, tr),
        coholdExtraHtml(COHOLD_BY_KEY.get((tr.dataset.group || "") + "::" + tr.dataset.sym)));
    };
  });
}

function bindConsensus() {
  // 2026-09-26：改为打开完整个股抽屉（含行情/K线），本行共识信息作为「④ 共识概况」追加。
  // Map 未命中时 extraHtml 为空串，openStock 会优雅降级（只展示行情/K线/持仓选手，不报错）。
  $$("#p-consensus tr[data-sym]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.tagName === "A") return;
      // 来源列的「出处 N 条」按钮/展开清单：只.toggle 自己的 DOM，不能穿透成 openStock
      if (e.target.closest && e.target.closest(".src-more, .src-list")) return;
      openStock(tr.dataset.sym, anchorOf(e, tr), consensusExtraHtml(CONSENSUS_BY_SYM.get(tr.dataset.sym)));
    };
  });
}
// 2026-09-26 起，三个面板（共同持仓共识 / 近5日共同买入 / 正在操作股票）的股票点击统一走
// openStock（含行情/K线）；本函数已无调用点，保留以防回归 —— 不要直接删除。
function openStockConsensus(sym, anchorEl) {
  api(`/api/consensus?min_holders=1&symbol=${encodeURIComponent(sym)}`).then((all) => {
    const x = toArr(all)[0];
    if (!x) return;
    const body = `
      <h3>${esc(x.symbol_name || "名称待补全")} <span class="stk-code">${esc(x.symbol)}</span></h3>
      <div class="kv"><div class="k">公司全称</div><div>${esc(x.company || "待补全")}</div></div>
      <div class="kv"><div class="k">上市板块</div><div>${esc(x.market || "—")}</div></div>
      <div class="kv"><div class="k">共同持仓人数</div><div>${x.holders ?? "—"} 人</div></div>
      ${consensusSplitKv(x)}
      <div class="kv"><div class="k">覆盖渠道</div><div>${toArr(x.channels).join("、") || "—"}</div></div>
      <div class="kv"><div class="k">讨论热度</div><div>${x.discuss_heat ?? "—"}</div></div>
      <div class="kv"><div class="k">来源网址</div><div>${toArr(x.sources).map((s) => srcLink(s, s, { arrow: false })).join("<br/>") || "—"}</div></div>
      <h4 style="margin:12px 0 6px">持仓选手明细</h4>
      ${toArr(x.players).map((pl) => `<div class="box" style="margin-bottom:8px"><div class="t">${channelBadge(pl.channel)} ${esc(pl.player)} ${pl.signal ? signalBadge(pl.signal) : actionBadge(pl.action)}</div><div class="meta">操作时间 ${esc(pl.post_time || "未披露")} ｜ 持仓比例 ${pl.ratio != null ? esc(pl.ratio) + "%" : "未披露"} ｜ ${esc(pl.site)} ｜ ${srcLink(pl.source_url, "来源")}</div></div>`).join("")}`;
    openDrawer(`共同持仓 · ${x.symbol_name || "名称待补全"}（${x.symbol}）`, body, anchorEl);
  }).catch((e) => reportLoadError("共同持仓详情", e));
}

/* ---------- 海外舆情：模块一（监控）与模块二（国外驱动热点） ---------- */
function regionLabel(r) {
  // P2-9：region 缺失时不能把 undefined 渲染进 DOM
  if (r == null || r === "") return "区域未标注";
  return { US: "美股", HK: "港股", GLOBAL: "国际", SG: "新加坡/东南亚", CN: "国内交叉比对" }[r] || String(r);
}
function regionClass(r) {
  return ({ US: "rg-us", HK: "rg-hk", GLOBAL: "rg-gl", SG: "rg-sg", CN: "rg-cn" })[r] || "rg-gl";
}
function moodTrendSVG(daily) {
  if (!daily || !daily.length) return "";
  const days = [...new Set(daily.map((d) => d.day))].sort();
  if (!days.length) return "";
  const regions = [...new Set(daily.map((d) => d.region))];
  const byReg = {};
  daily.forEach((d) => { (byReg[d.region] = byReg[d.region] || {})[d.day] = d.mood; });
  const W = 640, H = 200, padL = 34, padR = 12, padT = 14, padB = 26;
  const x = (i) => padL + (W - padL - padR) * (days.length === 1 ? 0.5 : i / (days.length - 1));
  const y = (m) => padT + (H - padT - padB) * (1 - (m + 100) / 200); // mood 映射 -100..100 → 顶部..底部
  const colors = { US: "#dc2626", HK: "#2563eb", GLOBAL: "#7c3aed", CN: "#64748b" };
  let lines = "";
  regions.forEach((reg) => {
    const pts = days.map((d, i) => (byReg[reg][d] != null ? `${x(i).toFixed(1)},${y(byReg[reg][d]).toFixed(1)}` : null)).filter(Boolean);
    if (pts.length > 1) lines += `<polyline fill="none" stroke="${colors[reg] || "#888"}" stroke-width="2" points="${pts.join(" ")}"/>`;
    days.forEach((d, i) => { if (byReg[reg][d] != null) lines += `<circle cx="${x(i).toFixed(1)}" cy="${y(byReg[reg][d]).toFixed(1)}" r="2.4" fill="${colors[reg] || "#888"}"/>`; });
  });
  const zero = y(0);
  const grid = `<line x1="${padL}" y1="${zero}" x2="${W - padR}" y2="${zero}" stroke="var(--border)" stroke-dasharray="4 4"/>` +
    `<text x="2" y="${y(100) + 4}" font-size="9" fill="var(--muted)">+100</text>` +
    `<text x="2" y="${y(-100) + 4}" font-size="9" fill="var(--muted)">-100</text>` +
    `<text x="2" y="${zero + 4}" font-size="9" fill="var(--muted)">0</text>`;
  const xlabels = days.map((d, i) => `<text x="${x(i).toFixed(1)}" y="${H - 8}" font-size="9" fill="var(--muted)" text-anchor="middle">${esc(d.slice(5))}</text>`).join("");
  const legend = regions.map((reg) => `<span class="lg-item"><i style="background:${colors[reg] || "#888"}"></i>${regionLabel(reg)}</span>`).join("");
  return `<div class="ov-chart"><svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet">${grid}${lines}</svg>
    <div class="ov-legend">${legend}</div></div>`;
}
/* ============ 海外舆情 → 决策报告（说什么 / 投向哪 / 做了什么 / 对内陆建议） ============
   诚实口径：后端 insight 的所有「权重」由 heat（关注度）加权，表示注意力分布，
   不是海外真实资金金额（库中无海外持仓 / 成交数据）。见报告末尾口径说明。 */
const OV2_PIE = ["#0E7490", "#dc2626", "#d97706", "#7c3aed", "#0f766e", "#64748b", "#be185d", "#4d7c0f", "#0369a1", "#a16207", "#155e75", "#475569"];
/* 环形图：按主题热度占比切分（纯内联 SVG，不引入图表库） */
function ov2Donut(topics) {
  const items = toArr(topics).filter((t) => t && Number(t.heat) > 0).slice(0, 12);
  const total = items.reduce((s, t) => s + Number(t.heat || 0), 0) || 1;
  const R = 54, r = 32, C = 70;
  const pt = (rad, a) => `${(C + rad * Math.cos(a)).toFixed(2)},${(C + rad * Math.sin(a)).toFixed(2)}`;
  let ang = -Math.PI / 2, arcs = "";
  items.forEach((t, i) => {
    const frac = Number(t.heat || 0) / total;
    const a2 = ang + frac * Math.PI * 2;
    const lg = frac > 0.5 ? 1 : 0;
    arcs += `<path d="M${pt(R, ang)} A${R},${R} 0 ${lg} 1 ${pt(R, a2)} L${pt(r, a2)} A${r},${r} 0 ${lg} 0 ${pt(r, ang)} Z" fill="${OV2_PIE[i % OV2_PIE.length]}" stroke="var(--panel)" stroke-width="1"/>`;
    ang = a2;
  });
  const legend = items.map((t, i) =>
    `<div class="ov2-lg"><i style="background:${OV2_PIE[i % OV2_PIE.length]}"></i><span class="ov2-lg-n">${esc(t.topic)}</span><b>${t.share}%</b><span class="ov2-lg-c">${t.n} 条</span></div>`).join("");
  const lead = items[0] || { share: 0 };
  return `<div class="ov2-pie"><svg viewBox="0 0 140 140" width="140" height="140">${arcs}
      <text x="70" y="65" text-anchor="middle" font-size="9" fill="var(--muted)">主导主题</text>
      <text x="70" y="81" text-anchor="middle" font-size="15" font-weight="800" fill="var(--text)">${lead.share}%</text></svg>
    <div class="ov2-legend">${legend || "<span class='s'>无</span>"}</div></div>`;
}
/* 横向条形：国内板块按海外提及热度排序（w = 相对榜首的百分比宽度） */
function ov2Bars(sectors) {
  const items = toArr(sectors).slice(0, 12);
  if (!items.length) return "";
  return `<div class="ov2-bars">` + items.map((s) =>
    `<div class="ov2-bar" title="热度 ${s.heat} · 被 ${s.n} 条海外条目提及 · 来源主题：${toArr(s.topics).join(" / ")}">
       <div class="ov2-bar-n">${esc(s.sector)}</div>
       <div class="ov2-bar-track"><div class="ov2-bar-fill" style="width:${Math.max(2, Number(s.w) || 0)}%"></div></div>
       <div class="ov2-bar-v">${s.heat}</div><div class="ov2-bar-k">${s.n} 条</div>
     </div>`).join("") + `</div>`;
}
function renderOverseasSentiment(d) {
  ensureOverseasShell();
  if (!d || typeof d !== "object" || Array.isArray(d) || !d.total) { $("#ov-sub-sentiment").innerHTML = `<div class="empty">暂无海外舆情数据。请点「刷新海外舆情(联网)」。</div>`; return; }
  const net = Number(d.net_mood) || 0;
  const pos = (((net + 100) / 200) * 100).toFixed(1);
  // 口径（2026-10-01 写清楚）：净情绪 =（看多条目占比 − 看空条目占比）×100，取值 −100~+100；
  // ±5 以内算「基本持平」。旧写法只给「整体偏多 / 整体偏空 / 多空均衡」三个词，既不露数值也不说 ±5 从哪来。
  const moodColor = net > 5 ? "var(--bull)" : net < -5 ? "var(--bear)" : "var(--muted)";
  const _ns = Number(net).toFixed(1);
  const _sgn = net > 0 ? "+" : "";
  const moodText = net > 5 ? `整体偏多（净情绪 ${_sgn}${_ns}）`
                 : net < -5 ? `整体偏空（净情绪 ${_ns}）`
                 : `多空基本持平（净情绪 ${_sgn}${_ns}，±5 内视为持平）`;
  const rw = (d.region_weights && typeof d.region_weights === "object") ? d.region_weights : {};
  const rdist = (d.region_dist && typeof d.region_dist === "object") ? d.region_dist : {};
  const ins = (d.insight && typeof d.insight === "object") ? d.insight : null;
  const regionCards = ["US", "HK", "GLOBAL", "CN"].filter((r) => rdist[r]).map((r) => {
    const rd = rdist[r] || {};
    return `<div class="ov-region ${regionClass(r)}">
      <div class="ov-rg-top"><span class="ov-rg-name">${regionLabel(r)}</span><span class="ov-rg-weight">权重 ${rw[r] != null ? rw[r] : "-"}</span></div>
      <div class="ov-rg-n">${rd.count ?? "—"} 条</div><div class="ov-rg-heat">热度 ${rd.heat ?? "—"}</div></div>`;
  }).join("");
  const srcRows = toArr(d.source_mix).map((s) =>
    `<div class="ov-src"><span class="ov-src-name">${esc(s.source_name)}</span><span class="badge ${regionClass(s.region)}">${regionLabel(s.region)}</span><span class="chip-s">${s.n ?? 0} 条</span></div>`).join("");
  // 重点提及标的：只保留「代码 + 名称」，不再标注多/空条数（该口径无决策增益）
  const symRows = toArr(d.symbols).slice(0, 14).map((s) =>
    `<div class="ov-sym"><b>${esc(s.symbol)}</b> ${esc(s.name || "")}</div>`).join("");
  const newsRows = toArr(d.news).map((n) =>
    `<div class="ov-news">
      <div class="ov-news-top"><span class="badge ${regionClass(n.region)}">${regionLabel(n.region)}</span> <span class="ov-news-src">${esc(n.source_name)}</span> <span class="ov-news-time">${esc((n.publish_time || "").slice(5))}</span></div>
      <div class="ov-news-title">${srcLink(n.url, n.title)}</div>
      <div class="ov-news-topic"><span class="chip-s">${esc(n.topic || "")}</span>${n.sectors ? `<span class="chip-s">板块 ${esc(n.sectors)}</span>` : ""}</div>
    </div>`).join("");
  api("/api/overseas_sources").then(renderOverseasSources).catch((e) => reportLoadError("数据源接入状态", e));

  /* ---------- 决策报告（后端 insight；旧服务进程无该字段时整段降级为空） ---------- */
  const v = (ins && ins.verdict) || {};
  const lead = ins ? `<div class="hint ov2-lead">国外视角优先：海外（美股/港股/国际）源权重更高，国内源仅作交叉比对（权重 ${rw.CN != null ? rw.CN : "-"}）。共 <b>${d.total}</b> 条海外条目。当前海外注意力最集中在 <b>${esc(v.lead_topic || "—")}</b>（热度占比 ${v.lead_share ?? "—"}%），映射到国内最密集的方向是 <b>${esc(v.lead_sector || "—")}</b>；净情绪指数 <b>${net}</b>（${moodText}）。</div>` : "";
  const topicBlocks = ins ? toArr(ins.topics).slice(0, 4).map((t) => `
    <div class="ov2-topic">
      <div class="ov2-topic-hd"><b>${esc(t.topic)}</b><span class="chip-s">${t.n} 条</span><span class="chip-s">热度 ${t.heat}</span><span class="ov2-share">${t.share}%</span></div>
      <div class="ov2-heads">${toArr(t.headlines).map((h) => `<div class="ov2-head">${sentBadge(h.sentiment || "中性")}${srcLink(h.url, h.title)}<span class="ov2-head-meta">${esc(h.source || "")} · ${esc(String(h.time || "").slice(5, 16))}</span></div>`).join("") || "<span class='s'>—</span>"}</div>
    </div>`).join("") : "";
  const moveCards = ins ? toArr(ins.moves).slice(0, 8).map((m, i) => {
    const dc = (m.direction || "").indexOf("利空") >= 0 ? "dir-bear" : (m.direction || "").indexOf("利多") >= 0 ? "dir-bull" : "dir-mix";
    const syms = toArr(m.symbols).map((s) => `<span class="chip-s">${esc(s)}</span>`).join(" ");
    const regs = toArr(m.regions).map((r) => `<span class="badge ${regionClass(r)}">${regionLabel(r)}</span>`).join("");
    const srcs = toArr(m.sources).map((s) => `<span class="ov-driver">${esc(s)}</span>`).join(" ");
    return `<div class="ov2-move ${dc}">
      <div class="ov2-move-hd"><span class="ov-hs-idx">#${i + 1}</span><b${m.title_en ? ` title="原文：${escAttr(m.title_en)}"` : ""}>${esc(m.title)}</b><span class="ov2-move-regs">${regs}</span></div>
      <div class="ov2-move-meta"><span class="ov-hs-dir-badge ${dc}">${esc(m.direction || "")}</span><span class="chip-s">热度 ${m.heat}</span>${m.confidence != null ? `<span class="chip-s">置信 ${m.confidence}</span>` : ""}${m.amp != null ? `<span class="chip-s">放大 ${m.amp}</span>` : ""}</div>
      ${syms ? `<div class="ov2-move-row"><span class="s">关联海外标的：</span>${syms}</div>` : ""}
      <div class="ov2-move-row"><span class="s">驱动源(国外)：</span>${srcs || "—"}</div>
    </div>`;
  }).join("") : "";
  const bb = (ins && ((ins.bull || []).length || (ins.bear || []).length)) ? `<div class="ov2-bb">
    <div class="ov2-bb-col up"><div class="ov2-bb-hd">主攻方向（海外热度支持）</div><div class="ov2-bb-chips">${toArr(ins.bull).map((s) => `<span class="badge bull">${esc(s)}</span>`).join(" ") || "—"}</div></div>
    <div class="ov2-bb-col down"><div class="ov2-bb-hd">阶段回避</div><div class="ov2-bb-chips">${toArr(ins.bear).map((s) => `<span class="badge bear">${esc(s)}</span>`).join(" ") || "—"}</div></div>
  </div>` : "";
  const impGrp = (arr, dir, label) => {
    const rows = toArr(arr).map((x) => {
      const stks = toArr(x.symbols).map((s) => `<span class="kline-chip chip-s ol-sym" data-ov-sym="${escAttr(s.code)}" title="${escAttr((s.name || "") + " " + (s.code || "") + " 点击看详情")}">${esc(s.name || s.code)}</span>`).join("");
      return `<div class="ov2-imp-row"><div class="ov2-imp-sec">${esc(x.sector)}</div><div class="ov2-imp-stks">${stks || "<span class='s'>暂无映射标的</span>"}</div></div>`;
    }).join("");
    return rows ? `<div class="ov2-imp-grp ${dir}"><div class="ov2-imp-grp-hd">${label}</div>${rows}</div>` : "";
  };
  const impactCards = ins ? toArr(ins.impacts).map((h, i) => `
    <div class="ov2-imp">
      <div class="ov2-imp-hd"><span class="ov-hs-idx">#${i + 1}</span><b>${esc(h.title)}</b><span class="chip-s">${esc(h.topic || "")}</span></div>
      ${h.decision ? `<div class="ov2-imp-dec">${esc(h.decision)}</div>` : ""}
      ${impGrp(h.up, "up", "可能受益")}${impGrp(h.down, "down", "可能承压")}
      ${h.transmission ? `<div class="ov2-imp-tx"><span class="s">传导路径：</span>${esc(h.transmission)}</div>` : ""}
    </div>`).join("") : "";
  const basisBox = (ins && toArr(ins.basis).length) ? `<details class="ov2-basis"><summary>口径说明：上面这些数字是怎么来的</summary>
    <ul>${toArr(ins.basis).map((x) => `<li>${esc(x).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</li>`).join("")}</ul></details>` : "";
  const report = ins ? `
    <h3 class="ov2-h"><span class="ov2-hn">一</span>他们在说什么<span class="ov2-hsub">按海外关注度（热度加权）排序</span></h3>
    ${ov2Donut(ins.topics)}
    ${topicBlocks}
    <h3 class="ov2-h"><span class="ov2-hn">二</span>注意力投向哪些国内板块<span class="ov2-hsub">海外条目自带的板块映射，热度加权</span></h3>
    ${ov2Bars(ins.sectors)}
    <h3 class="ov2-h"><span class="ov2-hn">三</span>他们做了什么（事件动向）<span class="ov2-hsub">方向 / 关联海外标的 / 驱动源</span></h3>
    <div class="ov2-moves">${moveCards || "<span class='s'>无</span>"}</div>
    <h3 class="ov2-h"><span class="ov2-hn">四</span>对内陆的影响与建议<span class="ov2-hsub">主攻 / 回避为聚合推断，代表个股用于定位观察方向</span></h3>
    ${bb}
    <div class="ov2-imps">${impactCards || "<span class='s'>无</span>"}</div>
    ${basisBox}` : "";
  const legacy = `
    <div class="ov-mood">
      <div class="ov-mood-head"><span class="ov-mood-label">海外整体情绪指数</span><span class="ov-mood-val" style="color:${moodColor}">${net} · ${moodText}</span></div>
      <div class="ov-thermo"><div class="ov-thermo-fill" style="width:${pos}%;background:${moodColor}"></div></div>
      <div class="ov-thermo-scale"><span>偏空 -100</span><span>0</span><span>偏多 +100</span></div>
    </div>
    <h3 style="margin:14px 0 10px">按日×区域情绪趋势</h3>
    ${moodTrendSVG(d.daily)}
    <div class="ov-cols">
      <div><h3 style="margin:6px 0 10px">区域分布</h3><div class="ov-regions">${regionCards || "<span class='s'>无</span>"}</div></div>
      <div><h3 style="margin:6px 0 10px">数据源构成</h3>${srcRows || "<span class='s'>无</span>"}</div>
      <div><h3 style="margin:6px 0 10px">重点提及标的</h3>${symRows || "<span class='s'>无</span>"}</div>
    </div>
    <div id="ov-sources" class="ov-sources"></div>
    <h3 style="margin:16px 0 10px">近期海外新闻（国外源优先）</h3>
    <div class="ov-news-list">${newsRows || "<span class='s'>无</span>"}</div>`;
  $("#ov-sub-sentiment").innerHTML = `${lead}${report}
    <details class="ov2-src"><summary>附：原始情绪指数 / 区域分布 / 数据源状态 / 新闻列表（点击展开）</summary><div class="ov2-src-in">${legacy}</div></details>`;
  $$("#ov-sub-sentiment [data-ov-sym]").forEach((el) => { el.onclick = () => openStock(el.dataset.ovSym, el); });
  // P0-3 保鲜角标：注入到「本子视图容器顶部」而非共用的 .subnav-slot ——
  // 舆情 / 热点 是两个不同数据域（updated_at 不同），各随自己的子视图显隐，避免两个角标同槽并列。
  injectFreshChip("#ov-sub-sentiment", "overseas_sentiment");
}
/* P2-7④：状态 → 配色。红只留给真正的「抓取失败」；
   「已连通但无条目」（如 PeerTube：通道可达、按主题过滤后当前确实无条目）是客观现状，
   不是错误，改用中性色；未知状态也不再用红色兜底。 */
function srcStatusClass(st) {
  const s = String(st || "");
  if (s.indexOf("已接入") >= 0) return "src-on";            // 绿：已接入且有数据
  if (s.indexOf("已连通但无条目") >= 0) return "src-neutral"; // 中性：连通但客观无条目
  if (s.indexOf("示例模式") >= 0) return "src-demo";         // 蓝：示例数据
  if (s.indexOf("待激活") >= 0) return "src-wait";           // 黄：需 VPN / API Key
  if (s.indexOf("未启用") >= 0) return "src-off";            // 灰：未启用
  if (s.indexOf("抓取失败") >= 0) return "src-err";          // 红：真失败
  return "src-off";
}
/* P2-7⑥：面板展示的是「上一次采集」的快照，取最新的 checked_at 作为数据时点 */
function latestCheckedAt(sources) {
  const ts = toArr(sources).map((s) => (s && s.checked_at) ? String(s.checked_at) : "").filter(Boolean).sort();
  return ts.length ? ts[ts.length - 1] : "";
}
/* 海外数据源状态卡片。sel 可指定渲染目标——「知识获取 → 数据源」面板会复用同一渲染，
   这样同一份数据源状态不必写两套视图。 */
function renderOverseasSources(d, sel) {
  const box = $(sel || "#ov-sources");
  if (!box) return;
  const list = toArr(d && d.sources);
  const items = list.map((s) => {
    const st = (s && s.status) || "";
    const cls = srcStatusClass(st);
    // P2-7②：特性探测——后端若新增 enabled 字段且为 false，补一个「已关闭」小标，
    // 不再让「待激活（需 VPN）」独自承担「已关闭」的语义。
    const hasEnabled = !!s && Object.prototype.hasOwnProperty.call(s, "enabled");
    const closed = hasEnabled && s.enabled === false;
    const cnt = Number(s && s.count) || 0;
    return `<div class="ov-src-status ${cls}" data-status="${escAttr(st)}">
      <div class="ov-src-st-top"><b>${esc(s && s.name)}</b>
        <span class="ov-src-st-badges">${closed ? '<span class="chip-s chip-closed" title="该数据源在后端被显式关闭（enabled=false）">已关闭</span>' : ""}<span class="ov-src-st-badge">${esc(st)}</span></span></div>
      <div class="ov-src-st-meta"><span class="badge ${regionClass(s && s.region)}">${regionLabel(s && s.region)}</span>${s && s.requires_vpn ? '<span class="chip-s">需VPN</span>' : ""}${s && s.requires_key ? '<span class="chip-s">需API Key</span>' : ""}${cnt ? `<span class="chip-s ov-src-count" title="为去重前的抓取条数，实际入库数可能更少">抓取 ${cnt} 条</span>` : ""}</div>
    </div>`;
  }).join("");
  const checkedAt = latestCheckedAt(list);
  // P2-7⑤：本面板不是实时探测结果，而是「上一次采集」的快照——图例如实描述，
  //        并去掉当前实现下不会出现的「未联网时显示示例模式·未联网」说法。
  box.innerHTML = `<h3 style="margin:16px 0 10px">数据源接入状态（已接入 / 已连通但无条目 / 待激活 / 未启用 / 抓取失败）${checkedAt ? `　<span class="ov-src-checked" data-checked-at="${escAttr(checkedAt)}">数据截至：${esc(checkedAt)}</span>` : ""}</h3>
    <div class="hint" style="margin-bottom:10px">本面板展示的是<b>上一次采集</b>的结果快照（数据时点见标题旁「数据截至」），不是实时探测结果。国外源（YouTube/Reddit/Google News/Reuters 等）在本机实测需 VPN 或 API Key，标记为「待激活（需 VPN/API Key）」；可接入源在「刷新海外舆情(联网)」后会实时抓取并标记「已接入」。通道可达但按主题短语过滤后当前确无相关条目的源标记为「已连通但无条目」——这是客观现状，不是抓取失败。真实条目与示例条目并存、逐条可区分。</div>
    <div class="ov-src-grid">${items || "<span class='s'>无</span>"}</div>`;
}
/** ③ 涨跌预判：按细分领域分组，逐个领域列出代表公司（名称 + 代码）。
 *  数据源 = 后端 sectors_up_detail / sectors_down_detail；
 *  若库中尚无该字段（未重算），降级为原来的「板块名徽章」，避免整块空白。 */
function hotspotForecast(h) {
  const isNone = (s) => String(s || "").indexOf("无显著") >= 0;
  // 容错：后端已 json.loads 时是数组；若拿到的是 JSON 字符串（未重启的旧服务进程）也自行解析
  const asArr = (v) => {
    if (Array.isArray(v)) return v;
    if (typeof v === "string" && v.trim()) { try { return JSON.parse(v) || []; } catch (e) { return []; } }
    return [];
  };
  const clean = (arr) => asArr(arr).filter((x) => x && !isNone(x.sector));
  const upD = clean(h.sectors_up_detail);
  const dnD = clean(h.sectors_down_detail);
  if (!upD.length && !dnD.length) {
    const up = toArr(h.sectors_up).map((s) => `<span class="badge bull">${esc(s)}</span>`).join(" ");
    const dn = toArr(h.sectors_down).map((s) => `<span class="badge ${isNone(s) ? "neu" : "bear"}">${esc(s)}</span>`).join(" ");
    if (!up && !dn) return "";
    return `<div class="ov-hs-block ov-hs-forecast"><span class="ov-hs-sub">③ 涨跌预判<span class="ov-hs-fc-legend">（红=预判上涨 / 绿=预判下跌，A 股惯例）</span></span>
      <div class="ov-hs-fc-row"><span class="ov-hs-fc-lbl up">可能上涨</span><span class="ov-hs-fc-bdgs">${up || "<span class='s'>—</span>"}</span></div>
      <div class="ov-hs-fc-row"><span class="ov-hs-fc-lbl down">可能下跌</span><span class="ov-hs-fc-bdgs">${dn || "<span class='s'>—</span>"}</span></div>
      <div class="ov-hs-fc-note">细分领域代表公司需重新「刷新海外舆情」后生成。</div></div>`;
  }
  const group = (arr, dir, label) => {
    if (!arr.length) return "";
    const rows = arr.map((x) => {
      // 2026-09-26 王总要求：代表个股要能点开看详情（K 线 / PE / ROE / 基础信息）。
      // 只有 6 位纯数字的 A 股代码才挂入口 —— 行情接口（东财/腾讯）只解析沪深北，
      // 给美股港股代码挂上去会点出一个必然失败的抽屉。
      const stks = toArr(x.symbols).map((s) => {
        const code = String(s.code || "").trim();
        const nm = esc(s.name || "");
        if (!/^\d{6}$/.test(code)) {
          return `<span class="stk"><span class="stk-n">${nm}</span><span class="stk-c">${esc(code)}</span></span>`;
        }
        return `<span class="stk stk-click" data-hp-sym="${escAttr(code)}"` +
          ` title="${escAttr((s.name || "") + " " + code + " —— 点开看 K 线走势 / 估值(PE·PB) / 盈利(ROE) / 基础信息")}">` +
          `<span class="stk-n">${nm}</span><span class="stk-c">${esc(code)}</span></span>`;
      }).join("");
      return `<div class="ov-hs-dom-row"><div class="ov-hs-dom-name">${esc(x.sector || "")}</div>` +
        `<div class="ov-hs-dom-stks">${stks || "<span class='s'>暂无映射标的</span>"}</div></div>`;
    }).join("");
    return `<div class="ov-hs-fc-grp ${dir}"><div class="ov-hs-fc-grp-hd">${label}</div><div class="ov-hs-dom">${rows}</div></div>`;
  };
  return `<div class="ov-hs-block ov-hs-forecast"><span class="ov-hs-sub">③ 涨跌预判 · 按细分领域<span class="ov-hs-fc-legend">（红=预判上涨 / 绿=预判下跌，A 股惯例；公司为各细分领域代表性标的，用于定位观察方向；<b>点公司名可查 K 线 · 估值(PE) · 盈利(ROE)</b>）</span></span>
    <div class="ov-hs-fc-groups">${group(upD, "up", "可能上涨")}${group(dnD, "down", "可能下跌")}</div></div>`;
}

function renderOverseasHotspots(list) {
  const all = toArr(list);
  ensureOverseasShell();
  if (!all.length) { $("#ov-sub-hotspots").innerHTML = `<div class="empty">暂无国外驱动热点。请点「刷新海外舆情(联网)」。</div>`; return; }
  const cards = all.map((h, i) => {
    const dirClass = (h.direction || "").indexOf("利空") >= 0 ? "dir-bear" : (h.direction || "").indexOf("利多") >= 0 ? "dir-bull" : "dir-mix";
    const secs = toArr(h.domestic_sectors).map((s) => `<span class="badge sec">${esc(s)}</span>`).join(" ");
    // 驱动源只列前 6 家（且「示例源·xx」种子已由后端剔除）。
    // 旧实现把 20+ 家全铺出来，既占版面、也稀释了真正在驱动该议题的源。
    const drvAll = toArr(h.driver_sources);
    const drivers = drvAll.slice(0, 6).map((s) => `<span class="ov-driver">${esc(s)}</span>`).join(" ")
      + (drvAll.length > 6 ? `<span class="ov-driver-more">等 ${drvAll.length} 家</span>` : "");
    const regs = toArr(h.driver_regions).map((r) => `<span class="badge ${regionClass(r)}">${regionLabel(r)}</span>`).join(" ");
    // 结论（决策导向）：直接给方向与主攻 / 回避，不再罗列证据条目
    const dec = h.decision
      ? `<div class="ov-hs-decision"><span class="ov-hs-dec-tag">结论</span><div class="ov-hs-dec-txt">${esc(h.decision)}</div></div>`
      : "";
    // ---- 结构化结论：① 事件概述 → ② 关键传导变量 → ③ 涨跌预判（按细分领域列代表公司）----
    const ov = h.overview
      ? `<div class="ov-hs-block"><span class="ov-hs-sub">① 事件概述</span><div class="ov-hs-text">${esc(h.overview)}</div></div>` : "";
    const vars = toArr(h.variables).map((v) => `<span class="badge var">${esc(v)}</span>`).join(" ");
    const fc = hotspotForecast(h);
    return `<div class="ov-hs-card ${dirClass}">
      <div class="ov-hs-head"><span class="ov-hs-idx">#${i + 1}</span><span class="ov-hs-title">${esc(h.title)}${h.title_en ? `<span class="ov-src-en" title="原文：${escAttr(h.title_en)}">原文</span>` : ""}</span>${regs}</div>
      <div class="ov-hs-dir"><span class="ov-hs-dir-badge ${dirClass}">${esc(h.direction || "")}</span>
        <span class="ov-hs-amp" title="放大系数：>1 为放大，<1 为衰减">放大系数 <b>${h.amplification_factor ?? "—"}</b></span>
        <span class="ov-hs-conf" title="置信度">置信 <b>${h.confidence ?? "—"}</b></span>
        <span class="ov-hs-heat">热度 ${h.heat ?? "—"}</span></div>
      ${dec}
      ${ov}
      ${vars ? `<div class="ov-hs-block"><span class="ov-hs-sub">② 关键传导变量</span><div class="ov-hs-vars">${vars}</div></div>` : ""}
      ${fc}
      <div class="ov-hs-sec"><span class="s">关联国内板块：</span>${secs || "<span class='s'>—</span>"}</div>
      <div class="ov-hs-drivers"><span class="s">驱动源（国外）：</span>${drivers || "<span class='s'>—</span>"}</div>
      <div class="ov-hs-block"><span class="ov-hs-sub">传导路径（提升性影响分析）</span><div class="ov-hs-text">${esc(h.transmission || "")}</div></div>
      <div class="ov-hs-block"><span class="ov-hs-sub">放大 / 衰减判断</span><div class="ov-hs-text">${esc(h.amplification || "")}</div></div>
      <div class="ov-hs-block"><span class="ov-hs-sub">与国内源交叉比对</span><div class="ov-hs-text">${esc(h.cross_check || "")}</div></div>
    </div>`;
  }).join("");
  $("#ov-sub-hotspots").innerHTML = `
    <div class="hint" style="margin-bottom:12px">模块二：由「国外新闻」驱动，自动汇总热点事件并做<b>提升性影响分析</b>。重点追踪四类事件：<b>地缘冲突与战争 / 洪涝等自然灾害 / 重大科研论文发布 / 粮食歉收与减产</b>。每条按「<b>结论</b> → ① 事件概述 → ② 关键传导变量（油价·粮价等）→ ③ 涨跌预判（按细分领域列代表性公司）」输出，并给出传导路径与放大 / 衰减判断。所列公司为各细分领域代表性标的，用于定位观察方向，不构成投资建议。</div>
    <div class="ov-hs-list">${cards}</div>`;
  // 代表个股 → 个股详情抽屉（与「当前讨论股票」卡片、近期热点「涉及标的」同一个入口，行为一致）
  $$("#ov-sub-hotspots .stk[data-hp-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.hpSym, el);
  });
  injectFreshChip("#ov-sub-hotspots", "overseas_hotspots");   // P0-3 保鲜角标（随热点子视图显隐）
}

/* ================= 国际局势（geo）：全球事件驱动 A 股影响研判 ================= */
let GEO = { data: null, cat: "all", cats: {} };

/* 方向色：含"利多"且含"利空"→ 分化(mix)；仅利空→bear；仅利多→bull；否则中性(mix)。 */
function geoDirClass(s) {
  s = s || "";
  const hasUp = s.indexOf("利多") >= 0 || s.indexOf("利好") >= 0 || s.indexOf("受益") >= 0;
  const hasDn = s.indexOf("利空") >= 0 || s.indexOf("承压") >= 0 || s.indexOf("利淡") >= 0;
  if (hasUp && hasDn) return "dir-mix";
  if (hasDn) return "dir-bear";
  if (hasUp) return "dir-bull";
  return "dir-mix";
}

function renderGeoEvents(d) {
  if (!d || typeof d !== "object") {
    $("#p-geo_events").innerHTML = `<div class="empty">国际局势数据加载失败。</div>`;
    return;
  }
  GEO.data = d;
  paintGeoInto("p-geo_events", d, { label: "国际局势", showCatBar: true, showNews: true, showRefresh: true });
}

// 面板归属 → 分类筛选状态 key
function _geoPanelKey(cid) {
  if (cid === "p-geo_policy") return "policy";
  if (cid === "p-geo_tech") return "tech";
  return "general";
}

// 通用渲染：支持不同容器、分类筛选、交叉印证、顶部印证统计
function paintGeoInto(cid, d, opts) {
  const sel = cid.startsWith("#") ? cid : "#" + cid;   // 调用点可能传裸 id（如 "p-geo_events"），统一补 #
  const host = document.querySelector(sel);
  if (!host) return;
  if (!d || typeof d !== "object") { host.innerHTML = `<div class="empty">数据加载失败。</div>`; return; }
  const o = opts || {};
  const st = d.stats || {};
  const cats = toArr(st.categories);
  const events = toArr(d.events);
  const news = toArr(d.news);
  const byCat = st.by_category || {};
  const honesty = (d.meta && d.meta.honesty) ? d.meta.honesty : "";
  const key = _geoPanelKey(cid);
  if (GEO.cats[key] == null) GEO.cats[key] = "all";

  // 本面板交叉印证统计（按事件内 cross_check 口径）
  const vloc = { "一致": 0, "分歧": 0, "待观察": 0 };
  let ccTotal = 0;
  events.forEach((e) => { const cc = e.cross_check; if (cc && typeof cc === "object") { ccTotal++; if (vloc[cc.verdict] != null) vloc[cc.verdict]++; } });

  const chips = [
    ["研判事件", st.total != null ? st.total : events.length],
    ["重大(强度≥4)", st.major_count != null ? st.major_count : "—"],
    ["交叉印证", ccTotal],
    ["涉及标的", st.symbols_n != null ? st.symbols_n : "—"],
    ["更新于", st.updated_at ? String(st.updated_at).slice(0, 16) : "—"],
  ].map(([k, v]) => `<span class="geo-chip"><b>${esc(String(v))}</b><span>${esc(k)}</span></span>`).join("");

  const verdictBar = o.showVerdict !== false ? `
    <div class="geo-verdicts">
      <span class="geo-verdict ok">✅ 国内外一致 <i>${vloc["一致"]}</i></span>
      <span class="geo-verdict warn">⚠️ 存在分歧 <i>${vloc["分歧"]}</i></span>
      <span class="geo-verdict wait">⏳ 待观察 <i>${vloc["待观察"]}</i></span>
    </div>` : "";

  const catBar = o.showCatBar ? `<div class="geo-cats">` +
    `<a class="geo-cat${GEO.cats[key] === "all" ? " active" : ""}" data-cat="all">全部 <i>${events.length}</i></a>` +
    cats.map((c) => `<a class="geo-cat${GEO.cats[key] === c.key ? " active" : ""}" data-cat="${escAttr(c.key)}" title="${escAttr(c.desc || "")}">${esc(c.icon || "")} ${esc(c.label)} <i>${byCat[c.key] || 0}</i></a>`).join("") +
    `</div>` : "";

  const list = (GEO.cats[key] === "all" ? events : events.filter((e) => e.category === GEO.cats[key]));
  const cards = list.map((e) => {
    const dc = geoDirClass(e.direction);
    const upS = toArr(e.symbols_up).map((s) => `<span class="stk" data-sym="${escAttr(s.code)}" title="点击查看 ${escAttr(s.name)}">${esc(s.name)}<em>${esc(s.code)}</em></span>`).join("");
    const dnS = toArr(e.symbols_down).map((s) => `<span class="stk down" data-sym="${escAttr(s.code)}" title="点击查看 ${escAttr(s.name)}">${esc(s.name)}<em>${esc(s.code)}</em></span>`).join("");
    const ups = toArr(e.sectors_up).map((s) => `<span class="badge sec up">${esc(s)}</span>`).join(" ");
    const dns = toArr(e.sectors_down).map((s) => `<span class="badge sec down">${esc(s)}</span>`).join(" ");
    const com = toArr(e.commodities).map((s) => `<span class="badge com">${esc(s)}</span>`).join(" ");
    const srcs = toArr(e.sources).map((s) => `<a class="geo-src" href="${escAttr(s.url || "#")}" target="_blank" rel="noopener" title="${escAttr(s.region || "")}">${esc(s.name || s.region || "来源")}${s.lang ? " · " + esc(s.lang) : ""}</a>`).join(" ");
    const catMeta = cats.find((c) => c.key === e.category) || {};
    const cc = e.cross_check;
    const ccHtml = cc && typeof cc === "object" ? `
      <div class="geo-cc">
        <div class="geo-cc-head"><span class="geo-sub">信源交叉印证</span><span class="geo-cc-verdict ${cc.verdict === "一致" ? "ok" : (cc.verdict === "分歧" ? "warn" : "wait")}">${esc(cc.verdict || "待观察")}</span></div>
        <div class="geo-cc-cols">
          <div class="geo-cc-col"><b>🌐 国外视角</b>${toArr(cc.foreign).map((f) => `<div class="geo-cc-item"><a href="${escAttr(f.url || "#")}" target="_blank" rel="noopener">${esc(f.src)}</a>：${esc(f.claim)}</div>`).join("")}</div>
          <div class="geo-cc-col"><b>🇨🇳 国内视角</b>${toArr(cc.domestic).map((f) => `<div class="geo-cc-item"><a href="${escAttr(f.url || "#")}" target="_blank" rel="noopener">${esc(f.src)}</a>：${esc(f.claim)}</div>`).join("")}</div>
        </div>
        ${cc.note ? `<div class="geo-cc-note">📌 ${esc(cc.note)}</div>` : ""}
      </div>` : "";
    return `<div class="geo-card ${dc}">
      <div class="geo-head">
        <span class="geo-cat-badge" title="${escAttr(catMeta.desc || "")}">${esc(catMeta.icon || "")} ${esc(e.cat_label || e.category)}</span>
        <span class="geo-int int-${e.intensity || 1}" title="强度 ${e.intensity}/5">${esc(e.intensity_text || "")}</span>
        <span class="geo-date">${esc(e.event_date || "")}</span>
        <span class="geo-title">${esc(e.title)}</span>
      </div>
      <div class="geo-dir">
        <span class="geo-dir-badge ${dc}">${esc(e.direction || "—")}</span>
        ${e.certainty != null ? `<span class="geo-conf" title="研判确定性">确定性 <b>${Math.round((Number(e.certainty) || 0) * 100)}%</b></span>` : ""}
      </div>
      ${e.summary ? `<div class="geo-summary">${esc(e.summary)}</div>` : ""}
      ${e.transmission ? `<div class="geo-block"><span class="geo-sub">传导逻辑</span><div class="geo-text">${esc(e.transmission)}</div></div>` : ""}
      ${ccHtml}
      ${(ups || dns) ? `<div class="geo-sec"><span class="s">板块影响：</span>${ups}${dns}</div>` : ""}
      ${com ? `<div class="geo-sec"><span class="s">涉及原材料：</span>${com}</div>` : ""}
      ${(upS || dnS) ? `<div class="geo-sec"><span class="s">代表个股：</span><span class="geo-up">${upS}</span><span class="geo-dn">${dnS}</span></div>` : ""}
      ${e.decision ? `<div class="geo-dec"><span class="geo-dec-tag">研判结论</span><div class="geo-dec-txt">${esc(e.decision)}</div></div>` : ""}
      ${e.watch ? `<div class="geo-watch">🔭 接下来看：${esc(e.watch)}</div>` : ""}
      ${srcs ? `<div class="geo-srcs"><span class="s">来源：</span>${srcs}</div>` : ""}
    </div>`;
  }).join("");

  const newsItems = news.map((n) => {
    const dc = geoDirClass(n.hint_direction);
    return `<div class="geo-news">
      <a class="geo-news-title" href="${escAttr(n.url || "#")}" target="_blank" rel="noopener">${esc(n.title)}</a>
      <div class="geo-news-meta">
        <span class="geo-cat-badge sm">${esc(n.cat_label || "")}</span>
        <span class="geo-news-src">${esc(n.source || "")}</span>
        <span class="geo-news-time">${esc(n.time || "")}</span>
        <span class="geo-news-tag" title="机器仅做分类，未做方向研判">机器初判 · 未研判</span>
        ${n.hint_direction ? `<span class="geo-dir-badge ${dc} sm">${esc(n.hint_direction)}</span>` : ""}
      </div>
    </div>`;
  }).join("");

  const bannerLabel = o.label || "国际局势";
  const newsWrap = o.showNews ? `
    <div class="geo-news-wrap">
      <div class="geo-news-head">国际新闻流 · 仅机器分类、未研判 <span class="geo-news-n">${news.length} 条</span></div>
      <div class="geo-news-list">${newsItems || `<div class="empty">近 10 日无筛选到的国际新闻。</div>`}</div>
    </div>` : "";

  host.innerHTML = `
    <div class="geo-banner hint">🌐 <b>${esc(bannerLabel)}</b> · 全球事件驱动 A 股影响研判。${esc(honesty)}</div>
    <div class="geo-topbar">
      <div class="geo-chips">${chips}${freshChip("geo")}</div>
      ${o.showRefresh ? `<button class="btn sm" id="btnGeoRefresh" title="幂等重导 seed_events.json（更新该文件后点此同步）">同步研判事件</button>` : ""}
    </div>
    ${verdictBar}
    ${catBar}
    <div class="geo-list">${cards || `<div class="empty">该分类下暂无研判事件。</div>`}</div>
    ${newsWrap}`;

  $$(sel + " .geo-cat").forEach((a) => { a.onclick = () => { GEO.cats[key] = a.dataset.cat; paintGeoInto(cid, d, o); }; });
  $$(sel + " .stk[data-sym]").forEach((el) => { el.onclick = () => openStock(el.dataset.sym, el); });
  const rb = $("#btnGeoRefresh");
  if (rb) rb.onclick = geoRefresh;
}

// 独立面板：按 panel 过滤拉取并渲染
function renderGeoPanel(panelKey, cid, label) {
  const sel = cid.startsWith("#") ? cid : "#" + cid;
  api("/api/geo_events?panel=" + encodeURIComponent(panelKey) + "&days=180")
    .then((d) => paintGeoInto(cid, d, { label: label, showCatBar: true, showNews: false }))
    .catch((e) => { const h = document.querySelector(sel); if (h) h.innerHTML = `<div class="empty">${esc(label)}加载失败：${esc(apiErrText(e))}</div>`; });
}

/* ================= 板块热度榜（2026-10-29 进哥拍板） =================
   博主/论坛的荐股 —— 谁推了什么票、看多看空、事后涨没涨 —— 从此**不再展示、不进打分**，
   只保留「注意力」这一个维度，并且在**板块**层面聚合：哪个板块被聊得多，哪个就热。

   所以这张表刻意没有方向、没有个股推荐、没有收益、没有博主名：
     · 「代表股」只给**被提及次数**，不给涨跌、不给目标价、不给推荐人；
     · 热度条用中性的金橙色，**不用红绿** —— 红绿在本站是涨跌语义，用在这儿会被读成涨跌；
     · 环比用「占全站讨论的百分点变化」，不是涨跌幅，因为这不是价格。
   后续迭代请勿把这些字段加回来 —— 加回来就等于把荐股换个皮重新上台面。 */
const PH = { data: null, loading: false, err: "" };

/* 热度分 → 条宽。0 分也留 2% 的一丝底边，让用户看得出「它在榜上但几乎没人聊」，
   而不是渲染成空白看着像加载失败。 */
function phBarW(v) {
  const n = Number(v) || 0;
  return Math.max(2, Math.min(100, n));
}

function phChg(v) {
  if (v === null || v === undefined || v === "") {
    return `<span class="ph-chg-na" title="前后窗口占比均需≥3% 才计算；样本太薄时一律显示「—」">—</span>`;
  }
  const n = Number(v);
  if (!isFinite(n) || Math.abs(n) < 0.05) return `<span class="ph-chg-flat">持平</span>`;
  // 升温用暖色实心、降温用冷色空心 —— 与「红涨绿跌」区分开：这里谈的是注意力，不是价格。
  return n > 0
    ? `<span class="ph-chg-up" title="本板块在本窗口的讨论占比，比上一窗口高 ${n.toFixed(1)} 个百分点">↑ ${n.toFixed(1)}pp</span>`
    : `<span class="ph-chg-dn" title="本板块在本窗口的讨论占比，比上一窗口低 ${Math.abs(n).toFixed(1)} 个百分点">↓ ${Math.abs(n).toFixed(1)}pp</span>`;
}

function renderPlateHeat() {
  const host = $("#p-plate_heat");
  if (!host) return;
  if (PH.err) {
    host.innerHTML = `<div class="card"><div class="empty">板块热度加载失败：${esc(PH.err)}</div></div>`;
    return;
  }
  const d = PH.data;
  if (!d) {
    host.innerHTML = `<div class="card"><div class="empty">正在统计板块热度…</div></div>`;
    return;
  }
  if (!d.ok || !toArr(d.rows).length) {
    host.innerHTML = `<div class="card"><div class="empty">${esc(d.error || "暂无板块热度数据")}
      <div class="s" style="margin-top:8px">热度需要有「能关联到帖子的股票提及」。若长期为空，
      多半是帖子表被清理后 mention 成了孤儿记录 —— 详见本页底部口径说明。</div></div></div>`;
    return;
  }
  const rows = toArr(d.rows);
  const m = d.meta || {};
  const top3 = rows.slice(0, 3);

  const body = rows.map((r) => {
    const ts = toArr(r.top_stocks);
    const tsHtml = ts.length
      ? ts.map((s) => `<span class="ph-stk" title="本窗口被提及 ${s.n} 次（只统计次数，不含涨跌与推荐人）">
            ${esc(s.name || s.code)}<i>${esc(s.n)}</i></span>`).join("")
      : `<span class="s">—</span>`;
    const breadth = (r.breadth == null) ? null : Number(r.breadth) * 100;
    return `<tr>
      <td class="ph-rk">${esc(r.rank_no)}</td>
      <td class="ph-plate">${esc(r.plate)}</td>
      <td class="ph-heat">
        <div class="ph-bar"><span style="width:${phBarW(r.heat)}%"></span></div>
        <b>${Number(r.heat).toFixed(1)}</b>
      </td>
      <td class="ph-num">${r.share == null ? "—" : Number(r.share).toFixed(1) + "%"}</td>
      <td class="ph-num">${r.n_posts == null ? "—" : esc(r.n_posts)}</td>
      <td class="ph-num">${breadth == null ? "—" : breadth.toFixed(1) + "%"}
        <span class="s">(${esc(r.n_stocks)}/${esc(r.members)})</span></td>
      <td class="ph-ts">${tsHtml}</td>
      <td class="ph-chg">${phChg(r.chg_pp)}</td></tr>`;
  }).join("");

  const head = top3.map((r, i) => `<div class="ph-lead">
      <span class="ph-lead-n">${i + 1}</span>
      <div><div class="ph-lead-p">${esc(r.plate)}</div>
        <div class="ph-lead-s">讨论占比 ${r.share == null ? "—" : Number(r.share).toFixed(1) + "%"}
          · 板块内 ${esc(r.n_stocks)} 只被提到 · ${phChg(r.chg_pp)}</div></div>
    </div>`).join("");

  host.innerHTML = `<div class="card ph-wrap">
    <div class="ph-head">
      <div>
        <h2>板块热度</h2>
        <div class="s">窗口 ${esc(m.window || "—")}（最近 ${esc(d.window || 5)} 个有数据的交易日）
          · 截至 ${esc(d.asof || m.asof || "—")}</div>
      </div>
      <div class="ph-note-tag">只谈热度 · 不谈方向</div>
    </div>
    <div class="ph-lead-row">${head}</div>
    <div class="ph-tw"><table class="ph-tb">
      <thead><tr>
        <th>#</th><th>板块</th><th>热度</th>
        <th title="本板块帖数占全站帖数的百分比（取中位日）">讨论占比</th>
        <th title="窗口内提到该板块的帖子数（同帖只算一次）">提及帖</th>
        <th title="板块内被提到的不同股票数 / 该板块成员总数">板块广度</th>
        <th>被提及最多的股票（次数）</th>
        <th title="占全站讨论比例的百分点变化，不是涨跌幅">较上一窗口</th>
      </tr></thead><tbody>${body}</tbody></table></div>
    <div class="ph-foot">
      <div class="ph-foot-t">口径与缺陷（照实说，不挑好看的显示）</div>
      <ul>
        <li><b>这张表不回答「该不该买」。</b>它只统计「哪个板块被讨论得多」——
          没有方向、没有目标价、没有历史收益，也没有推荐人。热度高不等于会涨，
          热度低也不等于会跌。<b>荐股内容本轮已全部下架</b>，此页是它们的替代口径。</li>
        <li><b>讨论占比</b>＝本板块帖数 ÷ 当日全站帖数，取窗口内的中位日。
          用占比而不用绝对帖数，是因为每日抓取量在 71~824 帖之间波动（覆盖不均），
          绝对量比出来的「降温」全是抓取节奏，不是市场。</li>
        <li><b>${esc(m.orphan_note || "论坛提及仅覆盖能关联到 posts 的部分")}</b>——
          约 88% 的历史提及指向已清理的帖子，只能计入总量、无法定位到日期，
          故本表实际只用了近两个月约 9.6 千帖。</li>
        <li><b>板块归属来自本地标签库</b>（plate_members，17 个板块 / 3131 只股票）。
          一只股票可以同时属于多个板块，所以「代表股」在不同板块里可能重复出现；
          个别归属也可能有误（例如概念板块收得过宽）。这是数据源问题，未做人工修饰。</li>
        <li><b>视频源当前${Number(m.video_weight) > 0 ? "已计入" : "未计入"}</b>：
          豆包整理的博主视频板块标签最晚到 ${esc(m.video_asof || "—")}
          （距今 ${esc(m.video_stale_days || "—")} 天）${Number(m.video_weight) > 0
            ? "，权重 " + esc(m.video_weight) : "，已超 7 天或有效板块不足 3 个，按规则整源停用、权重并入论坛"}。</li>
        <li><b>热度分是自定义口径</b>（论坛占比 ${(Number(d.weights && d.weights.forum) * 100 || 0).toFixed(0)}%
          ＋广度 ${(Number(d.weights && d.weights.breadth) * 100 || 0).toFixed(0)}%，log 后归一 ×100），
          不是交易所或任何机构的官方指标，只看相对排序、不宜跨日横向比较绝对值。</li>
        <li>${esc(m.cmp_note || "")}</li>
      </ul>
    </div></div>`;
}

function loadPlateHeat(force) {
  const host = $("#p-plate_heat");
  if (host && !PH.data) host.innerHTML = `<div class="card"><div class="empty">正在统计板块热度…</div></div>`;
  if (PH.loading) return Promise.resolve();
  PH.loading = true;
  return api("/api/plate_heat" + (force ? "?rebuild=1" : ""))
    .then((d) => { PH.data = d || {}; PH.err = ""; })
    .catch((e) => { PH.err = apiErrText(e); })
    .finally(() => { PH.loading = false; renderPlateHeat(); });
}


function geoRefresh() {
  const b = $("#btnGeoRefresh");
  if (b) { b.disabled = true; b.textContent = "同步中…"; }
  api("/api/geo_refresh").then((r) => {
    setHint("国际局势同步完成：" + JSON.stringify((r && r.imported) || {}));
    return api("/api/geo_events").then(renderGeoEvents).catch((e) => reportLoadError("国际局势", e));
  }).catch((e) => setHint("国际局势同步失败：" + apiErrText(e)))
    .finally(() => { if (b) { b.disabled = false; b.textContent = "同步研判事件"; } });
}

/* ---------- 徽章 ---------- */
function sentBadge(s) {
  const m = { 看多: "bull", 看空: "bear", 中性: "neu" };
  return `<span class="badge ${m[s] || "neu"}">${esc(s)}</span>`;
}
/* 来自源站「笼统归类」的动作值：本页 267/271 条持仓都是「关注/操作」，
   对读者不构成任何区分度 → 不渲染徽章，把版面让给建仓时间。
   （定义在文件顶层即可：渲染函数在 initNav() 之后才被调用，不存在 TDZ 问题） */
const NO_ACTION_BADGE = new Set(["关注/操作", "关注", "操作", "提及"]);

function actionBadge(a) {
  const m = { BUY: "buy", SELL: "sell", HOLD: "hold" };
  // P2-9：action 缺失时不能把 "undefined" 渲染进 DOM（原先 `${txt}` 会得到字面量 undefined）
  const fallback = (a == null || a === "") ? "—" : a;
  const txt = { BUY: "买入", SELL: "卖出", HOLD: "持仓" }[a] || fallback;
  return `<span class="badge ${m[a] || "hold"}">${esc(txt)}</span>`;
}

/* ---------- 详情面板：浮动窗口（关闭 / 拖拽 / 拉伸 / 层级） ---------- */
let drawerDim = false;   // 遮罩（仅视觉压暗，不拦截点击）
let lastRect = null;     // 最大化前的矩形，供还原

function isDrawerOpen() { return $("#drawer").classList.contains("show"); }

function loadDrawerSize() {
  try {
    const s = JSON.parse(localStorage.getItem("viewer-drawer-size") || "null");
    if (s && s.w > 320 && s.h > 240) return s;
  } catch (e) { /* ignore */ }
  // 2026-09-27：默认宽度 940/56% → 1000/64% 让 K 线 + 基本面 + 研报一次看全；高度 86% → 88%。
  // 注意：保留 ≈30% 视口宽度给左侧面板与拖动空间，避免抽屉初始紧贴右边导致指针被钳制拖不动。
  return { w: Math.min(1000, Math.round(window.innerWidth * 0.64)), h: Math.round(window.innerHeight * 0.88) };
}
function saveDrawerSize() {
  const d = $("#drawer");
  localStorage.setItem("viewer-drawer-size", JSON.stringify({ w: d.offsetWidth, h: d.offsetHeight }));
}

/** P2-8：统一把抽屉钳制在视口内。
 *  旧实现在拖拽时用 `left <= vw - 160` 兜底，面板宽 802 时拖到最右 left=1280 →
 *  右缘 2082 > 视口 1440，右下角拉伸手柄被推出视口、再也抓不到。
 *  这里统一保证：left + width <= innerWidth、top + height <= innerHeight（手柄永远可见），
 *  同时至少留 160px 标题栏在左侧可见（面板仍可拖回）。窗口 resize / 拖拽 / 拉伸结束都走这里。 */
const DRAWER_MIN_KEEP = 160;
function clampDrawerToViewport(d) {
  const vw = window.innerWidth, vh = window.innerHeight;
  let w = d.offsetWidth, h = d.offsetHeight;
  if (w > vw - 8) { w = Math.max(240, vw - 8); d.style.width = w + "px"; }
  if (h > vh - 8) { h = Math.max(160, vh - 8); d.style.height = h + "px"; }
  const maxLeft = Math.max(0, vw - w);                                   // 右缘不出界
  const minLeft = Math.min(0, DRAWER_MIN_KEEP - w);                      // 左缘最多探出到只剩 160px
  const left = Math.max(minLeft, Math.min(d.offsetLeft, maxLeft));
  const top = Math.max(0, Math.min(d.offsetTop, Math.max(0, vh - h)));    // 下缘不出界且标题栏可见
  d.style.left = Math.round(left) + "px";
  d.style.top = Math.round(top) + "px";
}

/** 智能落位：面板放到来源单元格的另一侧，且**保证不与所点单元格水平重叠**（空间允许时）。
 *  旧实现只按「单元格中心在哪一半」二分：中心在右半→面板放最左(x=18)。但面板宽 ~662px，
 *  左缘 18、右缘 680，会压住 x≥603 的所点单元格（FAIL：点右侧列→仍遮挡来源）。
 *  新逻辑：以「所点单元格」为锚，优先把面板贴到它另一侧且**留出 12px gap、不越过单元格边缘**；
 *  仅当该侧连最小宽度(320)都放不下时，才退回「中心二分 + 尽量贴边」的兜底（无法完全避免重叠）。 */
function placeDrawer(anchorEl) {
  const d = $("#drawer"), vw = window.innerWidth, vh = window.innerHeight;
  const size = loadDrawerSize();
  let w = Math.max(320, Math.min(size.w, vw - 32));
  let h = Math.max(240, Math.min(size.h, vh - 92));
  const gap = 12;
  let left;
  if (anchorEl && anchorEl.getBoundingClientRect) {
    const r = anchorEl.getBoundingClientRect();
    const center = r.left + r.width / 2;
    const rightRoom = vw - r.right - gap;   // 单元格右侧可容纳宽度
    const leftRoom = r.left - gap;           // 单元格左侧可容纳宽度
    if (center < vw / 2) {
      // 锚在左半 → 面板放右侧；宽度取「右侧空间」与期望宽度的较小值，确保抽屉右缘不越过单元格右缘
      w = Math.max(320, Math.min(w, rightRoom));
      left = Math.max(r.right + gap, vw - w - gap);
    } else {
      // 锚在右半 → 面板放左侧；宽度取「左侧空间」与期望宽度的较小值，确保抽屉右缘 <= 单元格左缘
      w = Math.max(320, Math.min(w, leftRoom));
      left = Math.min(r.left - gap - w, vw - w - gap);
    }
  } else {
    left = vw - w - 18;
  }
  d.style.right = "auto"; d.style.bottom = "auto";
  d.style.width = w + "px";
  d.style.height = h + "px";
  d.style.left = Math.max(12, Math.min(left, vw - w - 12)) + "px";
  d.style.top = Math.max(12, Math.min(76, vh - h - 16)) + "px";
  clampDrawerToViewport(d);
}

function openDrawer(title, html, anchorEl) {
  // P3-13：基础无障碍 —— 记录打开前的焦点，关闭时回焦；打开后把焦点移入抽屉。
  try { DRAWER_LAST_FOCUS = document.activeElement; } catch (e) {}
  $("#drawerTitle").textContent = title || "详情";
  const body = $("#drawerBody");
  // P2-28：抽屉顶部加一排跳转 chip（仅当当前打开的是个股时渲染；DRAWER_SYMBOL 由 openStock 写入）
  const jump = drawerJumpChips();
  body.innerHTML = (jump ? jump : "") + html;
  body.scrollTop = 0;
  const d = $("#drawer");
  d.classList.add("show");
  d.setAttribute("aria-hidden", "false");
  d.setAttribute("aria-modal", "true");   // P3-13：声明为模态对话框
  placeDrawer(anchorEl);
  $("#mask").classList.toggle("show", drawerDim);
  // 焦点移入抽屉（优先关闭按钮，便于键盘 Esc/Enter 操作）
  try {
    const focusEl = document.getElementById("btnClose") || d;
    if (focusEl && focusEl.focus) setTimeout(() => { try { focusEl.focus(); } catch (_) {} }, 0);
  } catch (e) {}
}

/** 关闭：叉号 / 底部按钮 / Esc / 点击面板外 共用同一入口 */
function closeDrawer() {
  if (!isDrawerOpen()) return;
  // 2026-10-23：关闭抽屉时取消仍未完成的个股请求，避免与首屏/轮询抢连接池
  if (_stockAbort) { try { _stockAbort.abort(); } catch (e) {} _stockAbort = null; }
  const d = $("#drawer");
  d.classList.remove("show", "ghost", "maxed");
  d.setAttribute("aria-hidden", "true");
  $("#mask").classList.remove("show");
  const g = $("#btnGhost");
  g.classList.remove("on"); g.textContent = "半透明";
  // P2-28：关闭时清除当前个股上下文，避免下一次非个股抽屉残留跳转 chip
  DRAWER_SYMBOL = "";
  // P3-13：关闭后焦点回退到打开抽屉前的元素（若仍在文档中）
  try { if (DRAWER_LAST_FOCUS && DRAWER_LAST_FOCUS.focus && document.contains(DRAWER_LAST_FOCUS)) DRAWER_LAST_FOCUS.focus(); } catch (e) {}
  // 分支弹窗若处于「让位」状态（bm-behind），详情关闭后恢复其模态遮罩与层级
  const bm = document.getElementById("branchModal");
  if (bm) bm.classList.remove("bm-behind");
}

function toggleMax() {
  const d = $("#drawer"), btn = $("#btnMax");
  if (d.classList.contains("maxed")) {
    if (lastRect) {
      d.style.left = lastRect.left + "px"; d.style.top = lastRect.top + "px";
      d.style.width = lastRect.w + "px"; d.style.height = lastRect.h + "px";
    }
    d.classList.remove("maxed"); btn.textContent = "⛶"; btn.classList.remove("on");
  } else {
    lastRect = { left: d.offsetLeft, top: d.offsetTop, w: d.offsetWidth, h: d.offsetHeight };
    d.style.left = "12px"; d.style.top = "60px";
    d.style.width = (window.innerWidth - 24) + "px";
    d.style.height = (window.innerHeight - 72) + "px";
    d.classList.add("maxed"); btn.textContent = "❐"; btn.classList.add("on");
  }
}

function initDrawer() {
  const d = $("#drawer"), head = $("#drawerHead"), rz = $("#drawerResize");
  let sx = 0, sy = 0, ox = 0, oy = 0, ow = 0, oh = 0;

  // 拖拽移动（按住标题栏）
  head.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".dbtn")) return;
    if (d.classList.contains("maxed")) { toggleMax(); }   // 最大化状态下拖动先还原
    const r = d.getBoundingClientRect();
    sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    d.style.right = "auto"; d.style.bottom = "auto";
    const move = (ev) => {
      d.style.left = (ox + ev.clientX - sx) + "px";
      d.style.top = (oy + ev.clientY - sy) + "px";
      // P2-8：统一钳制，右缘/下缘不出界（右下拉伸手柄永远可抓）
      clampDrawerToViewport(d);
    };
    const up = () => {
      clampDrawerToViewport(d);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    e.preventDefault();
  });

  // 右下角拉伸缩放
  rz.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const r = d.getBoundingClientRect();
    sx = e.clientX; sy = e.clientY; ow = r.width; oh = r.height;
    d.classList.remove("maxed");
    const move = (ev) => {
      // 高度上限同时受"面板顶部位置"约束，确保右下角手柄不会被拉出视口
      const maxH = Math.max(220, window.innerHeight - d.offsetTop - 4);
      const maxW = Math.max(320, window.innerWidth - d.offsetLeft - 4);
      d.style.width = Math.max(320, Math.min(ow + ev.clientX - sx, maxW)) + "px";
      d.style.height = Math.max(220, Math.min(oh + ev.clientY - sy, maxH)) + "px";
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      saveDrawerSize();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    e.preventDefault();
  });

  // 关闭入口 1/2/3：叉号、底部按钮、Esc
  $("#btnClose").onclick = closeDrawer;
  $("#btnClose2").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // 关闭入口 4：点击面板以外的任何区域（背景仍保持可点击，操作后自动收起）
  document.addEventListener("pointerdown", (e) => {
    if (!isDrawerOpen()) return;
    if (e.target.closest("#drawer")) return;
    // 分支透视弹窗内的操作（滚动 / 点另一只票）不应误关详情面板；要关请用叉号 / 底部按钮 / Esc
    if (e.target.closest("#branchModal")) return;
    closeDrawer();
  });

  $("#btnGhost").onclick = () => {
    const on = d.classList.toggle("ghost");
    $("#btnGhost").classList.toggle("on", on);
    $("#btnGhost").textContent = on ? "不透明" : "半透明";
  };
  $("#btnDim").onclick = () => {
    drawerDim = !drawerDim;
    $("#btnDim").classList.toggle("on", drawerDim);
    $("#mask").classList.toggle("show", drawerDim && isDrawerOpen());
  };
  $("#btnMax").onclick = toggleMax;

  // 窗口尺寸变化时把面板收回可视范围（与拖拽/拉伸共用同一钳制逻辑）
  window.addEventListener("resize", () => {
    if (!isDrawerOpen()) return;
    if (d.classList.contains("maxed")) {
      d.style.left = "12px"; d.style.top = "60px";
      d.style.width = (window.innerWidth - 24) + "px";
      d.style.height = (window.innerHeight - 72) + "px";
      return;
    }
    clampDrawerToViewport(d);
  });
}

/* ---------- 列表行绑定 ---------- */
function anchorOf(e, fallbackEl) {
  // 用「被点击的单元格/元素」作为锚点：面板落到它的另一侧，避免遮住你所点的内容
  return (e && e.target && e.target.closest && (e.target.closest("td") || e.target.closest(".box"))) || fallbackEl;
}
function bindRows() {
  $$("#p-summary tr[data-id], #p-operating tr[data-id]").forEach((tr) => {
    tr.onclick = (e) => openDetail(tr.dataset.kind, tr.dataset.id, anchorOf(e, tr));
  });
  // 相关人物、实盘选手 行：点击查看该用户/选手汇总（轻量，不加载原始页面）
  $$("#p-persons tr[data-name]").forEach((tr) => {
    tr.onclick = (e) => openPerson(tr.dataset.name, anchorOf(e, tr));
  });
  $$("#p-players tr[data-name]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.tagName === "A") return;
      const chip = e.target.closest && e.target.closest(".kline-chip");
      if (chip && chip.dataset.sym) { openStock(chip.dataset.sym, chip); return; }
      openPlayer(tr.dataset.name, anchorOf(e, tr));
    };
  });
  // 2026-09-26：「正在操作股票」标的列的股票 chip → 打开完整个股详情（含行情/K线）。
  // 必须 stopPropagation：该行 tr 的 onclick 是 openDetail（字段明细），不拦截会两个抽屉打架。
  $$("#p-operating .kline-chip[data-sym]").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); openStock(el.dataset.sym, el); };
  });
}
function openDetail(kind, id, anchorEl) {
  api(`/api/detail?kind=${kind}&id=${id}`).then((d) => {
    let html;
    if (d.error) { html = `<p>${esc(d.error)}</p>`; }
    else {
      const rows = Object.entries(d).map(([k, v]) =>
        `<div class="kv"><div class="k">${esc(k)}</div><div>${esc(v)}</div></div>`).join("");
      // 默认不渲染可能数 MB 的原始页面；仅在用户显式点击时按需加载（见 loadRawHtml）。
      html = `<div class="detail-sub" style="margin-top:0">字段明细</div>${rows}
         <button class="btn sm" id="btnRaw" style="margin-top:10px">查看原始文档（完整网页）</button>`;
    }
    openDrawer(`详情 · ${kind}`, html, anchorEl);
    const b = $("#btnRaw");
    if (b) b.onclick = () => loadRawHtml(kind, id);
  }).catch((e) => reportLoadError("字段明细", e));
}
function loadRawHtml(kind, id) {
  setHint("正在加载原始文档…");
  api(`/api/detail?kind=${kind}&id=${id}&full=1`).then((d) => {
    const raw = (d && (d.raw_html || d.html)) || "";
    const body = $("#drawerBody");
    if (!raw) {
      // 诚实的空状态：该行确实没有关联到原始存档（历史抓取未留存该页，而非"加载失败"）。
      // 旧写法会渲染成「共 0 KB」的空框，看着像坏了。
      body.insertAdjacentHTML("beforeend",
        `<h4 style="margin:12px 0 6px">原始文档</h4>
         <div class="empty">该记录没有关联的原始存档。<br/>
         原因：这一页在抓取时未留存原始 HTML（raw_pages 表无对应行），无法回看"当时抓到了什么"。
         新抓取的记录会自动建立关联。</div>`);
      setHint("");
      return;
    }
    // 原始页面可能很大：仅预览前 200KB，完整内容已在后台数据库保留。
    const preview = raw.slice(0, 200000);
    body.insertAdjacentHTML("beforeend",
      `<h4 style="margin:12px 0 6px">原始文档（预览前 200KB / 共 ${Math.round(raw.length/1024)} KB）</h4>
       <div class="raw">${esc(preview)}</div>
       <div class="s" style="color:var(--muted)">完整原始页面已存于后台数据库（raw_pages 表），仅按需预览。</div>`);
    setHint("");
  }).catch((e) => setHint("原始文档加载失败：" + apiErrText(e)));
}
/* 明细区块（人物 / 选手抽屉）里的「标的」单元格：代码与名称都要能点开个股详情。
   与统计明细表（statCell 的 c.stock / c.stock_name 分支）共用同一套 codeLink 口径：
   只给 6 位沪深北 A 股代码挂入口，挂不上就退化成纯文本，绝不伪造入口。 */
function symCell(sym, name) {
  const c = String(sym == null ? "" : sym).trim();
  const nm = (name == null ? "" : String(name)).trim();
  if (!/^\d{6}$/.test(c)) return nm ? `${esc(c)} ${esc(nm)}` : esc(c);
  return codeLink(c) + (nm && nm !== c ? " " + codeLink(c, { text: nm }) : "");
}
function openPerson(name, anchorEl) {
  api(`/api/person?name=${encodeURIComponent(name)}`).then((d) => {
    let html;
    if (d.error) { html = `<p>${esc(d.error)}</p>`; }
    else {
      const meta = (d.meta && typeof d.meta === "object") ? d.meta : {};
      const posts = toArr(d.posts).map((p) =>
        `<div class="box" style="margin-bottom:6px"><div class="t">${esc(fmtTimeText(p.post_time, "时间未披露"))} · ${sentBadge(p.sentiment)}</div><div class="meta">${esc(p.text)}</div></div>`).join("");
      const trades = toArr(d.trades).map((t) =>
        `<div class="box" style="margin-bottom:6px"><div class="t">${actionBadge(t.action)} ${symCell(t.symbol, t.symbol_name)}</div><div class="meta">${esc(fmtTimeText(t.post_time))} · 价格 ${t.price ?? "-"} · 数量 ${t.quantity ?? "-"}</div></div>`).join("");
      // 持仓徽章内标注「持仓起始日期」（title 给出完整日期与已持有天数）
      const pos = toArr(d.positions).map((p) => {
        const isSell = (p.action || "").toUpperCase() === "SELL";
        const since = (p.hold_since || "").trim();
        const tip = since ? `持仓起始日期 ${since.slice(0, 10)}${p.hold_days != null ? `，已持有 ${p.hold_days} 天` : ""}` : "起始日期未披露";
        return `<span class="badge ch-${escAttr(p.channel || 'A')}" title="${escAttr(tip)}">${symCell(p.symbol, p.symbol_name)} ${p.ratio != null ? p.ratio + '%' : ''}${since ? `<i class="hold-mini">${esc(since.slice(5, 10))} 起</i>` : ""}${isSell ? `<i class="hold-mini sell-mini">已卖出</i>` : ""}</span>`;
      }).join(" ");
      html =
        `<h3>${esc(d.name)}</h3>
         <div class="kv"><div class="k">出现次数</div><div>${meta.mention_count ?? 0}</div></div>
         <div class="kv"><div class="k">提及过股票</div><div>${toArr(d.mentioned_stocks).join("、") || "—"}</div></div>
         <h4 style="margin:12px 0 6px">持仓（${d.position_count ?? 0}）</h4>${pos || "<span class='s'>无</span>"}
         <h4 style="margin:12px 0 6px">发帖（${d.post_count ?? 0}）</h4>${posts || "<span class='s'>无</span>"}
         <h4 style="margin:12px 0 6px">实盘交易（${d.trade_count ?? 0}）</h4>${trades || "<span class='s'>无</span>"}
         ${holdDaysNote("person_first_build")}`;
    }
    openDrawer(`人物 · ${name}`, html, anchorEl);
  }).catch((e) => reportLoadError("人物详情", e));
}
function openPlayer(name, anchorEl) {
  api(`/api/player?name=${encodeURIComponent(name)}`).then((d) => {
    let html;
    if (d.error) { html = `<p>${esc(d.error)}</p>`; }
    else {
      const i = (d.info && typeof d.info === "object") ? d.info : {};
      const recentDays = d.recent_days || 5;

      // —— 受限内容汇总横幅（付费/积分/无权限）：明确提示，便于按需充值 ——
      let banner = "";
      if (d.access_count > 0) {
        const items = toArr(d.access_items).map((a) =>
          `<li>${accessBadge(a.level)} <b>${esc(a.symbol)} ${esc(a.symbol_name)}</b>（${esc(a.kind)}）— ${esc(a.hint)}</li>`).join("");
        banner = `<div class="access-banner">
          <b>⚠ 该选手存在 ${d.access_count} 项受限内容</b>（付费 / 积分 / 无权限）。如需查看完整买卖价与成本，请按提示充值或开通相应权限：
          <ul>${items}</ul></div>`;
      }

      // —— ① 持仓明细（买入 / 持有）：每笔含持仓起始日期（自建仓起持有天数）——
      const holdings = toArr(d.holdings).length ? toArr(d.holdings) : toArr(d.positions);
      const posRows = holdings.map((p) => {
        const bp = p.buy_price != null ? p.buy_price : (p.price != null ? p.price : null);
        const cp = p.current_price != null ? p.current_price : null;
        const since = (p.hold_since || "").trim();
        const days = p.hold_days != null ? p.hold_days : null;
        const sinceCell = since
          ? `<b class="hold-since" title="持仓起始日期：该标的首次建仓时间">${esc(since.slice(0, 10))}</b>
             <span class="hold-days">${days != null ? `已持有 ${days} 天` : "建仓日"}</span>`
          : `<span class="s">未披露</span>`;
        return `<tr${p.closed ? ' class="row-closed"' : ''}>
          <td>${symCell(p.symbol, p.symbol_name)}${p.closed ? ' <span class="badge closed-badge">已清仓</span>' : ""}</td>
          <td>${actionBadge(p.action)}</td>
          <td>${sinceCell}</td>
          <td class="num">${bp != null ? Number(bp).toFixed(2) : "—"}</td>
          <td class="num">${cp != null ? Number(cp).toFixed(2) + plTag(bp, cp) : "<span class='s'>未抓取</span>"}</td>
          <td>${p.ratio != null ? p.ratio + "%" : "未披露"}</td>
          <td>${accessBadge(p.access_level || "free")}</td>
        </tr>`;
      }).join("");
      const posTable = posRows
        ? `<table><thead><tr><th>标的</th><th>动作</th><th>持仓起始日期</th><th>建仓价</th><th>当前股价</th><th>持仓比例</th><th>权限</th></tr></thead><tbody>${posRows}</tbody></table>`
        : `<span class="s">无持仓记录</span>`;

      // —— ② 卖出 / 减仓明细：此前卖出记录只能挤在「买入时间/买入价」列里展示，语义错位 ——
      const sellRows = toArr(d.sells).map((s) => {
        const sp = s.sell_price != null ? s.sell_price : null;
        const cost = s.cost_price != null ? s.cost_price : null;
        const pnl = s.pnl != null ? s.pnl : (sp != null && cost != null ? sp - cost : null);
        const since = (s.hold_since || "").trim();
        const st = (s.sell_time || s.post_time || "").trim();
        const closeCls = s.close_type === "清仓" ? "sell-full" : (s.close_type === "减仓" ? "sell-part" : "sell-part");
        // 卖出价与了结盈亏合并同列，控制列数让关键信息不被横向裁切
        const priceCell = sp != null
          ? `${Number(sp).toFixed(2)}${pnl != null ? ` <span class="${pnl > 0 ? "pl-up" : (pnl < 0 ? "pl-down" : "pl-flat")}">(${pnl > 0 ? "+" : ""}${pnl.toFixed(2)})</span>` : ""}`
          : "—";
        // 「类型 + 比例」合并一列（清仓 100% / 减仓 15%），把列数压到 5 列，
        // 保证卖出起始日期与了结盈亏都能在第一屏看到（抽屉宽度有限，此前 8 列被裁切）。
        const typeTxt = `${esc(s.close_type || "卖出")}${s.ratio != null ? " " + s.ratio + "%" : ""}`;
        // 持仓起始日期作为标的的副行标注：抽屉内容区较窄，做成独立列会被横向裁切，
        // 放标的旁既能保证首屏可见，也符合「在对应股票位置标注起始日期」的要求。
        const sinceLine = since
          ? `<span class="hold-days" title="持仓起始日期 ${escAttr(since)}">${esc(since.slice(5, 10))} 起${s.hold_days != null ? ` · 持有 ${s.hold_days} 天` : ""}</span>`
          : `<span class="hold-days">起始日期未披露</span>`;
        return `<tr>
          <td>${symCell(s.symbol, s.symbol_name)}${sinceLine}</td>
          <td><span class="badge ${closeCls}">${typeTxt}</span></td>
          <td>${esc(st ? st.slice(0, 16) : "—")}</td>
          <td class="num">${priceCell}</td>
        </tr>`;
      }).join("");
      const sellTable = sellRows
        ? `<table class="tbl-sell"><thead><tr><th>标的 / 持仓起始</th><th>类型 / 比例</th><th>卖出时间</th><th>卖出价 / 盈亏</th></tr></thead><tbody>${sellRows}</tbody></table>`
        : `<span class="s">暂无卖出 / 减仓记录</span>`;

      // —— 近 N 日操作记录（时间线）——
      // 卖出流水取 sell_time/sell_price、买入流水取 buy_*，并统一称「成交价」——
      // 时间线里买卖混排，若一律标「买入价」，卖出那笔的价格就成了错误标注。
      const fxTime = (t) => ((t.action || "").toUpperCase() === "SELL"
        ? (t.sell_time || t.post_time) : (t.buy_time || t.post_time)) || "—";
      const fxPrice = (t) => ((t.action || "").toUpperCase() === "SELL"
        ? (t.sell_price != null ? t.sell_price : t.price)
        : (t.buy_price != null ? t.buy_price : t.price));

      const recentRows = toArr(d.recent).map((t) => {
        const bt = fxTime(t);
        const bp = fxPrice(t);
        const cp = t.current_price != null ? t.current_price : null;
        const isSell = (t.action || "").toUpperCase() === "SELL";
        const tag = t.access_level && t.access_level !== "free"
          ? `<span class="lock-tag">${accessText(t.access_level)}</span>`
          : `<span class="free-tag">可查看</span>`;
        return `<div class="op-row">
          <span class="t">${esc(bt)}</span>
          <span class="note">${tag}</span>
          <span class="sym">${actionBadge(t.action)} ${symCell(t.symbol, t.symbol_name)}</span>
          <span>成交价 <b>${bp != null ? Number(bp).toFixed(2) : "—"}</b>${cp != null ? " ｜ 现价 " + Number(cp).toFixed(2) + (isSell ? "" : plTag(bp, cp)) : ""}</span>
        </div>`;
      }).join("");

      // —— 全部实盘交易（完整，含超 5 日的历史）——
      const tradeRows = toArr(d.trades).map((t) => {
        const bt = fxTime(t);
        const bp = fxPrice(t);
        const cp = t.current_price != null ? t.current_price : null;
        const isSell = (t.action || "").toUpperCase() === "SELL";
        return `<tr>
          <td>${esc(bt)}</td>
          <td>${actionBadge(t.action)}</td>
          <td>${symCell(t.symbol, t.symbol_name)}</td>
          <td class="num">${bp != null ? Number(bp).toFixed(2) : "—"}</td>
          <td class="num">${cp != null ? Number(cp).toFixed(2) + (isSell ? "" : plTag(bp, cp)) : "—"}</td>
          <td>${t.quantity != null ? t.quantity : "—"}</td>
          <td>${accessBadge(t.access_level || "free")}</td>
        </tr>`;
      }).join("");
      const tradeTable = tradeRows
        ? `<table><thead><tr><th>时间</th><th>动作</th><th>标的</th><th>成交价</th><th>当前股价</th><th>数量</th><th>权限</th></tr></thead><tbody>${tradeRows}</tbody></table>`
        : `<span class="s">无交易记录</span>`;

      const certBadge = i.is_certified ? `<span class="cert-badge">✔ 官方认证/示范</span>` : "";
      html =
        `<div class="detail-sub" style="margin-top:0">选手档案${i.channel ? " · " + esc(CHANNEL_LABEL[i.channel] || ("渠道 " + i.channel)) : ""} ${certBadge}</div>
         <div class="kv"><div class="k">名次</div><div>${i.rank_position != null ? i.rank_position : "-"}</div></div>
         <div class="kv"><div class="k">评分/收益</div><div>${i.ranking_score != null ? i.ranking_score : "-"}</div></div>
         <div class="kv"><div class="k">官方认证</div><div>${i.is_certified ? "✔ 是（直接入围，不受名次限制）" : "—"}</div></div>
         <div class="kv"><div class="k">是否入选</div><div>${i.is_top_player ? "✅ 入选" : "⚠ 未入选（见下方原因）"}</div></div>
         ${i.source_url ? `<div class="kv"><div class="k">来源</div><div>${srcLink(i.source_url, "主页")}</div></div>` : ""}
         ${banner}
         <div class="detail-sub">① 持仓明细（${d.holding_count != null ? d.holding_count : (d.position_count ?? "—")}）— 每笔标注<b>持仓起始日期</b>（该标的首次建仓时间）与已持有天数</div>
         ${posTable}
         <div class="detail-sub">② 卖出 / 减仓明细（${d.sell_count ?? 0}）— 卖出时间 / 卖出价 / 卖出比例 / 了结盈亏${toArr(d.sold_out).length ? `　·　已清仓：${toArr(d.sold_out).map(esc).join("、")}` : ""}</div>
         ${sellTable}
         <div class="detail-sub">③ 近 ${recentDays} 日操作记录（${d.recent_count ?? "—"}）— 实时买卖时间线与价位</div>
         ${recentRows || "<span class='s'>近 " + recentDays + " 日无操作记录</span>"}
         <div class="detail-sub">④ 全部实盘交易（${d.trade_count ?? "—"}）</div>
         ${tradeTable}
         ${holdDaysNote("player_first_build")}`;
    }
    openDrawer(
      `实盘选手 · ${name}${d.info && d.info.channel ? "（渠道 " + d.info.channel + "）" : ""}`,
      html, anchorEl
    );
  }).catch((e) => reportLoadError("选手详情", e));
}

/* 权限徽章 */
function accessBadge(level) {
  const m = { free: ["access-free", "可查看"], paid: ["access-paid", "付费"], points: ["access-points", "积分"], locked: ["access-locked", "无权限"] };
  const [cls, txt] = m[(level || "free")] || m.free;
  return `<span class="access-badge ${cls}">${txt}</span>`;
}
function accessText(level) {
  return { free: "可查看", paid: "付费", points: "积分", locked: "无权限" }[level || "free"] || "可查看";
}
/* 盈亏标记（A股惯例：涨=红、跌=绿） */
function plTag(buy, cur) {
  if (buy == null || cur == null) return "";
  const diff = cur - buy;
  if (Math.abs(diff) < 1e-9) return ` <span class="pl-flat">(平)</span>`;
  if (diff > 0) return ` <span class="pl-up">(+${diff.toFixed(2)})</span>`;
  return ` <span class="pl-down">(${diff.toFixed(2)})</span>`;
}
/* ---------- 详情面板初始化（关闭/拖拽/拉伸/层级 统一在此注册） ---------- */
initDrawer();

/* ---------- 加载 ----------
   P1-2：10 个请求此前全部没有 .catch —— 任一失败都是整页静默空白。
   现在每个请求独立兜底：失败登记到可见的错误条 + statusHint，其余面板照常渲染。 */
const SUMMARY = { data: null };
/* 抓取管道三个子板块的数据源：均在数据到达或切换进入时按需渲染，
   不在 30s 时效心跳里重复打接口（其「最后更新」列靠 applyFreshness 自行重算）。
   注：2026-09-26 起这三块已从导航撤下，此函数仍会渲染它们（面板容器保留在 index.html），
   目的是保留「加回导航项即可复原」的能力，且开销极小。 */
function refreshAcquirePanels() {
  renderSourcesPanel(SUMMARY.data);
  renderFetchLogPanel(SUMMARY.data);
  if (CURRENT.tab === "archive") renderArchivePanel();
}

function loadAll() {
  // 与各面板并行拉取刷新状态；renderX 尾部会调用 applyFreshness() 兜底重算时效标记。
  loadRefreshStatus().then(refreshAcquirePanels);
  // 今日看板：决策入口，页面加载即拉（后端已做容错，单模块失败不拖垮整页）
  api("/api/dashboard", { label: "今日看板", timeout: _API_TIMEOUT_HEAVY })
    .then((d) => { DASH.data = d; renderDashboard(); })
    .catch((e) => reportLoadError("今日看板", e));
  api("/api/summary").then((s) => {
    if (!s || typeof s !== "object" || Array.isArray(s)) {
      s = {};
      reportLoadError("汇总接口", new Error("返回类型不符，计数已降级为空"));
    }
    if (!s.counts) reportLoadError("汇总接口", new Error("响应中缺少 counts 字段，计数已降级为空"));
    SUMMARY.data = s;
    renderCards(s.counts);
    renderSummary(s);
    refreshAcquirePanels();
  }).catch((e) => reportLoadError("汇总接口", e));
  // 2026-10-31：原首屏的 6 个论坛链路请求（/api/stocks 讨论热度、/api/operating 正在操作、
  // /api/players 选手、/api/persons 人物、consensus 共同持仓、cohold 近5日买入）全部撤除。
  // 理由：论坛抓取 2026-10-30 全停，这些接口返回的是永久停更的旧数据；且其中
  // /api/persons 响应体 932KB、/api/smart_money 单次 17s、/api/persons 6s，
  // 撤掉后首屏少 6 个请求、约 1.5MB 流量，落地更快也更诚实（不再假装有"他人操作"）。
  // 对应面板已从 NAV 撤下；后端路由与 renderX 函数保留，源恢复时一处加回。
  // 龙虎榜是**活链路**（lhb_daily 每日 18:00 更新），仍保留首屏延迟预热。
  if (!LHB.data) setTimeout(() => loadLhb().catch((e) => reportLoadError("游资龙虎榜", e)), 2500);
  api("/api/sentiment").then(renderSentiment).catch((e) => reportLoadError("舆论情绪", e));
  // 2026-10-15（性能修复 · 「面板随机卡 20~30 秒」的真根因之一）：
  // /api/hotspots 是**纯 Python 聚合热词**的重接口，实测首次要 **40 秒**，期间**持有 GIL
  // 把整个服务的 API 线程一起冻住**（与 blogger_perf 回算同一类病）。
  // 它此前在 loadAll 首屏就发 —— 首屏那几秒所有接口全被它拖死。现改为**进入「近期热点」面板才拉**
  // （见 selectNav 的 topics 分支），并给后端加 TTL 缓存 + 启动预热，让真正点进去时命中缓存。
  api("/api/overseas_sentiment").then(renderOverseasSentiment).catch((e) => reportLoadError("美股港股舆情", e));
  api("/api/overseas_hotspots").then((x) => renderOverseasHotspots(toArr(x))).catch((e) => reportLoadError("近期热点(国外驱动)", e));
  api("/api/geo_events").then(renderGeoEvents).catch((e) => reportLoadError("国际局势", e));
  // 2026-10-15 P2-1：原「全球政策雷达 / 科技前沿突破」两独立面板已合并进「国际局势」单一面板，
  // 由 paintGeoInto 的 in-panel 分类筛选条（全部/政策/科技）切换，无需再按 panel 单独请求。
  // 2026-09-27 新增一级分类「自选池」：关注池 + 到价提醒（一次拉取，两面板各自渲染）
  loadWatch();
  // 2026-09-27 新增一级分类「增强功能」：智能选股/资金流向/估值诊股/研报评级/多资产/资讯公告。
  // 采用懒加载：进入子板块（selectNav 命中下面 if 分支）才拉取，避免每次 loadAll/5分钟刷新都打外网。
  // 2026-10-22 全量整改：密度/命令面板/设置/免责声明/首启引导 等新增功能的初始化接入点
  initEnhancements();
}

/* ================= 策略回测 · 策略实验室（T04，三栏联动） =================
   数据：GET /api/strategies（一次全量 100 条 × 各窗口指标，前端本地筛选/排序）。
   形态：左=策略清单 / 中=长周期排名 / 右=现实检验（近半年 / 近一年 双窗口），三栏双向联动。
   诚实性（硬性，PM §2.6 + 设计文档 §9）：
     ① 口径横幅常驻（不可关）；
     ② 不可回测 / 口径受限 → 一律不显示数字（用 «—» 与虚线卡，**绝不写 0**）；
     ③ 样本不足显式标注；交易极频繁（>5000 笔）另加高频预警；
     ④ 择时/仓位切换类显式标注（避免误读为选股能力）；
     ⑤ 提示横幅用语义色（黄=注意 / 蓝=提示），不与涨跌红绿混淆。 */
const LAB = {
  data: null, loading: false, sel: null,
  q: "", schools: new Set(), onlyBt: false, onlyNotBt: false,
  groupByFamily: false, sort: "default",
  wl: 300, wr: 360,                     // 左右栏宽度（px，可拖拽调整）
};
const LAB_HIGH_FREQ = 5000;             // 交易笔数超过此值 → 高频预警（T04 要求）

/* 方向色（A 股习惯：正=红/负=绿/零=灰）；缺失一律 «—»，绝不写 0 */
function labPctText(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return `<span class="muted">—</span>`;
  const n = Number(v);
  const cls = n > 0 ? "bull" : (n < 0 ? "bear" : "muted");
  return `<span class="${cls}">${n > 0 ? "+" : ""}${n.toFixed(d)}%</span>`;
}
/* 风险色：最大回撤越大越不利 → 越绿 + ⚠（不靠颜色单打独斗） */
function labRiskText(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return `<span class="muted">—</span>`;
  const n = Number(v);
  return n >= 0 ? `<span class="muted">${n.toFixed(d)}%</span>` : `<span class="bear">${n.toFixed(d)}% ⚠</span>`;
}
function labNumText(v, d = 1, suffix = "%") {
  if (v === null || v === undefined || isNaN(v)) return `<span class="muted">—</span>`;
  return `${Number(v).toFixed(d)}${suffix}`;
}
/* 状态徽标：不可回测/口径受限/样本不足一律黄色「注意」色，不用红绿 */
function statusBadge(status) {
  const map = {
    ok: { cls: "neu", txt: "可回测" },
    not_backtestable: { cls: "warn", txt: "不可回测·缺数据" },
    scope_limited: { cls: "warn", txt: "口径受限" },
    insufficient: { cls: "warn", txt: "样本不足" },
    no_data: { cls: "neu", txt: "无数据" },
  };
  const m = map[status] || { cls: "neu", txt: status || "—" };
  return `<span class="lab-badge lab-badge-${m.cls}">${esc(m.txt)}</span>`;
}

function loadStrategyLab() {
  if (LAB.loading) return Promise.resolve();
  LAB.loading = true;
  const box = $("#p-strategy_lab");
  if (box && !LAB.data) box.innerHTML = `<div class="empty">正在读取策略回测数据…</div>`;
  return api("/api/strategies")
    .then((d) => { LAB.data = d; LAB.loading = false; renderStrategyLab(); })
    .catch((e) => { LAB.loading = false; reportLoadError("策略回测", e); });
}

/* 当前筛选后的策略列表（本地筛选，与 /api/persons「一次全量、前端排序」同一范式） */
function labList() {
  const arr = toArr(LAB.data && LAB.data.strategies);
  const q = (LAB.q || "").trim().toLowerCase();
  return arr.filter((s) => {
    if (LAB.onlyNotBt && s.status !== "not_backtestable") return false;
    if (LAB.onlyBt && !s.backtestable) return false;
    if (LAB.schools.size && !LAB.schools.has(s.school)) return false;
    if (q) {
      const hay = [s.name, s.key, s.family, s.desc, s.school, s.buy, s.sell].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}
function labKeyCompare(a, b) {
  return (a.school || "").localeCompare(b.school || "", "zh")
    || (a.family || "").localeCompare(b.family || "", "zh")
    || (a.key || "").localeCompare(b.key || "", "zh");
}
function labSorted(list) {
  if (LAB.sort === "default") return list.slice().sort(labKeyCompare);
  const val = (x) => {
    const m = x.mid;
    if (!m) return null;
    if (LAB.sort === "max_dd") return m.max_dd;
    return m[LAB.sort];
  };
  return list.slice().sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va === null && vb === null) return labKeyCompare(a, b);
    if (va === null) return 1;                 // 缺失值一律沉底
    if (vb === null) return -1;
    return vb - va;                            // 越大越靠前（max_dd 越接近 0 越"好"）
  });
}
function labMidRows(list) {
  const ok = list.filter((x) => x.mid);
  if (LAB.sort === "default") return ok.slice().sort((a, b) => (a.mid.rank || 9999) - (b.mid.rank || 9999));
  return labSorted(ok);
}

function renderStrategyLab() {
  const box = $("#p-strategy_lab");
  if (!box) return;
  const d = LAB.data;
  if (!d) { box.innerHTML = `<div class="empty">正在读取策略回测数据…</div>`; return; }
  const cal = d.caliber || {};
  const meta = d.meta || {};
  const bench = (cal.bench && (cal.bench.name || cal.bench.code)) || "基准";
  const fillTxt = cal.fill === "next_close" ? "信号次日收盘" : (cal.fill || "信号次日收盘");
  box.innerHTML = `
    <div class="lab-top">
      <h2 class="lab-h2">策略回测<span class="lab-h2-sub">等权单标的满仓 · 信号次日收盘成交 · 仅作相对强弱比较</span></h2>
      <div class="lab-meta">
        <span class="lab-mchip">策略总数 <b>${meta.total ?? 0}</b></span>
        <span class="lab-mchip">可回测 <b>${meta.backtestable ?? 0}</b></span>
        <span class="lab-mchip">参与排名 <b>${meta.ranked ?? 0}</b></span>
        <span class="lab-mchip lab-mchip-warn">口径受限 <b>${meta.scope_limited ?? 0}</b></span>
        <span class="lab-mchip lab-mchip-warn">不可回测 <b>${meta.not_backtestable ?? 0}</b></span>
        <span class="lab-mchip">更新于 ${esc(d.generated_at || "—")}</span>
      </div>
    </div>
    <div class="lab-caliber" role="note">
      <b>${esc(cal.title || "回测口径")}：</b>${esc(cal.text || "等权单标的满仓、信号次日收盘成交、不计手续费与滑点；用于策略相对强弱比较，不是可实现收益预测。")}
      <span class="lab-caliber-tag">成交口径：${esc(fillTxt)}</span>
      <span class="lab-caliber-tag">年化基准：${esc(cal.annual_basis || "244 交易日/年")}</span>
      <span class="lab-caliber-tag">可成交性过滤：${cal.limit_up_filter ? "已启用（一字板撤单）" : "未启用"}</span>
      <span class="lab-caliber-tag">基准：${esc(bench)}</span>
      <span class="lab-caliber-tag">取数 ${esc(cal.bars_fetch != null ? cal.bars_fetch : "")} 根</span>
      ${cal.adj ? `<span class="lab-caliber-tag lab-caliber-ok">复权：${esc(cal.adj)}</span>` : ""}
      ${cal.walk_forward ? `<span class="lab-caliber-tag lab-caliber-ok" title="${esc(cal.walk_forward.note || "")}">样本外验证：${esc(cal.walk_forward.split || "70/30")}</span>` : ""}
    </div>
    <div class="lab-toolbar">
      <input id="labQ" class="lab-search" type="search" placeholder="搜索 名称 / key / 家族 / 说明…（中英文皆可）" value="${escAttr(LAB.q)}">
      <div id="labChips" class="lab-chips"></div>
      <div id="labToggles" class="lab-toggles"></div>
      <label class="lab-sort">排序
        <select id="labSort">
          <option value="default">默认（流派·家族）</option>
          <option value="total_return">总收益</option>
          <option value="annual_return">年化</option>
          <option value="max_dd">最大回撤</option>
        </select>
      </label>
    </div>
    <div class="lab-cols" id="labCols" style="--lab-wl:${LAB.wl}px;--lab-wr:${LAB.wr}px;">
      <div class="lab-col lab-col-left">
        <div class="lab-col-head">策略清单 <span class="lab-count" id="labLeftCount"></span></div>
        <div class="lab-body" id="labLeftBody"></div>
      </div>
      <div class="lab-handle" data-side="l" title="左右拖动调整栏宽"></div>
      <div class="lab-col lab-col-mid">
        <div class="lab-col-head">长周期排名（约 2 年） <span class="lab-count" id="labMidCount"></span></div>
        <div class="lab-body" id="labMidBody"></div>
      </div>
      <div class="lab-handle" data-side="r" title="左右拖动调整栏宽"></div>
      <div class="lab-col lab-col-right">
        <div class="lab-col-head">现实检验（近半年 / 近一年）</div>
        <div class="lab-body" id="labRightBody"></div>
      </div>
    </div>`;
  // 搜索框 / 排序：只更新状态并重绘「列表 + 详情」，不重绘搜索框本身 → 保住输入焦点
  const qInput = $("#labQ");
  if (qInput) qInput.oninput = () => { LAB.q = qInput.value; refreshLab(); };
  const sortSel = $("#labSort");
  if (sortSel) { sortSel.value = LAB.sort; sortSel.onchange = () => { LAB.sort = sortSel.value; refreshLab(); }; }
  bindLabHandles();
  refreshLab();
}

function refreshLab() {
  renderLabChips();
  renderLabToggles();
  renderLabLists();
  renderLabDetail();
}

function renderLabChips() {
  const host = $("#labChips");
  if (!host) return;
  const schools = toArr(LAB.data && LAB.data.meta && LAB.data.meta.schools);
  host.innerHTML = schools.map((sc) => {
    const on = LAB.schools.has(sc);
    return `<span class="lab-chip${on ? " active" : ""}" data-school="${escAttr(sc)}">${esc(sc)}</span>`;
  }).join("");
  host.querySelectorAll(".lab-chip").forEach((c) => {
    c.onclick = () => {
      const sc = c.dataset.school;
      if (LAB.schools.has(sc)) LAB.schools.delete(sc); else LAB.schools.add(sc);
      refreshLab();
    };
  });
}

function renderLabToggles() {
  const host = $("#labToggles");
  if (!host) return;
  host.innerHTML =
    `<label class="lab-switch"><input type="checkbox" id="labOnlyBt"${LAB.onlyBt ? " checked" : ""}> 仅看可回测</label>` +
    `<label class="lab-switch"><input type="checkbox" id="labGroupFamily"${LAB.groupByFamily ? " checked" : ""}> 按家族分组</label>` +
    `<span class="lab-chip lab-chip-notbt${LAB.onlyNotBt ? " active" : ""}" id="labNotBtChip" title="只看不可回测（缺数据）的策略">不可回测</span>`;
  const b1 = $("#labOnlyBt");
  if (b1) b1.onchange = () => { LAB.onlyBt = b1.checked; refreshLab(); };
  const b2 = $("#labGroupFamily");
  if (b2) b2.onchange = () => { LAB.groupByFamily = b2.checked; refreshLab(); };
  const nb = $("#labNotBtChip");
  if (nb) nb.onclick = () => { LAB.onlyNotBt = !LAB.onlyNotBt; refreshLab(); };
}

function renderLabLists() {
  const list = labList();
  const lc = $("#labLeftCount");
  if (lc) lc.textContent = `共 ${list.length} 条`;
  const lh = $("#labLeftBody");
  if (lh) {
    if (!list.length) {
      lh.innerHTML = `<div class="empty">没有匹配的策略。调整搜索或筛选条件。</div>`;
    } else if (LAB.groupByFamily) {
      const groups = {};
      list.forEach((s) => { const f = s.family || "其他"; (groups[f] = groups[f] || []).push(s); });
      lh.innerHTML = Object.keys(groups).map((fam) => {
        const items = groups[fam].slice().sort(labKeyCompare);
        return `<div class="lab-fam"><div class="lab-fam-head">${esc(fam)} · ${items.length} 条变体</div>` +
          items.map(labLeftItem).join("") + `</div>`;
      }).join("");
    } else {
      lh.innerHTML = labSorted(list).map(labLeftItem).join("");
    }
    bindLabLeft();
  }
  renderLabMid(list);
}

function labLeftItem(s) {
  const on = LAB.sel === s.key;
  const m = s.mid;
  let kv = "";
  if (m) {
    kv = `<span class="lab-kv">总 ${labPctText(m.total_return)}</span>` +
      `<span class="lab-kv">年化 ${labPctText(m.annual_return)}</span>`;
  } else if (s.status === "not_backtestable") {
    kv = `<span class="lab-kv muted">不可回测 · 不显示数字</span>`;
  } else if (s.status === "scope_limited") {
    kv = `<span class="lab-kv muted">口径受限 · 不显示数字</span>`;
  }
  const sw = s.is_timing_switch
    ? `<span class="lab-tag-sw" title="择时/仓位切换类，非选股能力">${esc(s.switch_kind || "择时/仓位")}</span>` : "";
  return `<div class="lab-item${on ? " active" : ""}" data-key="${escAttr(s.key)}" tabindex="0">
    <div class="lab-item-top">
      <span class="lab-item-name">${esc(s.name || s.key)}</span>
      ${s.status !== "ok" ? statusBadge(s.status) : ""}
    </div>
    <div class="lab-item-sub">
      <span class="lab-school">${esc(s.school || "")}</span>
      <span class="lab-fam-tag">${esc(s.family || "")}</span>${sw}
    </div>
    <div class="lab-item-desc">${esc(s.desc || "")}</div>
    <div class="lab-item-kv">${kv}</div>
  </div>`;
}

function bindLabLeft() {
  $$("#labLeftBody .lab-item").forEach((it) => {
    it.onclick = () => onStrategyClick(it.dataset.key);
    it.onkeydown = (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const all = Array.from($$("#labLeftBody .lab-item"));
      const i = all.indexOf(it);
      const j = e.key === "ArrowDown" ? i + 1 : i - 1;
      if (j >= 0 && j < all.length) { all[j].focus(); selectStrategy(all[j].dataset.key); }
    };
  });
}

function renderLabMid(list) {
  const host = $("#labMidBody");
  if (!host) return;
  const rowsData = labMidRows(list);
  const scoped = list.filter((x) => x.status === "scope_limited");
  const nb = list.filter((x) => x.status === "not_backtestable");
  // 不可回测占位：默认不显示（PM §2.5 设计：开关默认关）；当「不可回测」筛选开启，或当前选中项
  // 恰为不可回测策略时，才列出其虚线卡（保证"被点即见"）。
  const showNb = LAB.onlyNotBt || (LAB.sel && nb.some((s) => s.key === LAB.sel));
  const cnt = $("#labMidCount");
  if (cnt) cnt.textContent = `参与排名 ${rowsData.length}`;

  let html = "";
  if (!rowsData.length) {
    html += `<div class="empty">当前筛选下没有可排名的策略。</div>`;
  } else {
    html += `<table class="lab-table"><thead><tr>
      <th class="num" title="综合分数排名，仅长周期窗口计算">#</th>
      <th>策略</th>
      <th class="num" title="窗口内等权平均总收益（池内）">总收益</th>
      <th class="num" title="按 244 交易日/年折算的年化收益">年化</th>
      <th class="num" title="最大回撤 = 窗口内净值峰谷最大跌幅（越大越不利）">最大回撤</th>
      <th class="num" title="全池胜率">胜率</th>
      <th class="num" title="全池交易笔数；低于阈值标「样本不足」，超过 ${LAB_HIGH_FREQ} 标「高频」">交易</th>
      <th class="num" title="池内跑赢基准（上证指数）的标的占比">跑赢基准</th>
      <th title="2026-10-15 P0-2：窗口按 70/30 切分（前段训/后段验），样本外只验一次">样本外验证</th>
      <th title="优秀/良好/一般/较差">评级</th>
      </tr></thead><tbody>` +
      rowsData.map(labMidRow).join("") + `</tbody></table>`;
    html += `<div class="lab-note">数值 hover 有口径说明；带 <span class="lab-star">*</span> 者为样本不足，偶然性大；无数据处显示 «—»，不写 0。</div>`;
  }
  if (scoped.length) {
    html += `<div class="lab-scope"><div class="lab-scope-head">口径受限 · 不予排名（${scoped.length}）</div>` +
      scoped.map((s) => `<div class="lab-scope-item"><b>${esc(s.name || s.key)}</b> — ` +
        `${esc(s.status_note || "与『等权满仓』口径不符，不予回测与排名")}</div>`).join("") + `</div>`;
  }
  if (showNb && nb.length) {
    html += `<div class="lab-nb"><div class="lab-nb-head">不可回测 · 缺数据（${nb.length}）</div>` +
      nb.map((s) => `<div class="lab-dashed"><span class="lab-badge lab-badge-warn">缺数据</span> ` +
        `<b>${esc(s.name || s.key)}</b><div class="lab-dashed-note">原因：` +
        `${esc(s.status_note || ("缺少" + (s.needs || "所需数据")))}</div></div>`).join("") + `</div>`;
  }
  host.innerHTML = html;
  host.querySelectorAll("tr[data-key]").forEach((tr) => { tr.onclick = () => onStrategyClick(tr.dataset.key); });
}

/* 交易笔数文案：0 笔 ≠「没交易」，而是「买了到窗口末还没了结」——写清楚，避免被误读 */
function labTradeText(trades) {
  if (trades === null || trades === undefined || isNaN(trades)) return "—";
  return Number(trades) === 0 ? "0 笔已了结" : String(trades);
}
/* 胜率文案：0 笔了结时胜率「未定义」→ «—»（绝不显示 0.0%） */
function labWinRateText(v, trades) {
  if (!trades) return `<span class="muted" title="窗口内无了结交易，胜率未定义（不是 0%）">—</span>`;
  return labNumText(v, 1, "%");
}

/* 2026-10-15 P0-2 / DEF-007：样本外验证角标（70/30 walk-forward）。 */
function labOosBadge(m) {
  if (!m) return `<span class="lab-badge lab-badge-mute">—</span>`;
  const r = (m.oos_return == null) ? null : Number(m.oos_return);
  const rr = (r == null) ? "" : `（样本外 ${r >= 0 ? "+" : ""}${r.toFixed(1)}%）`;
  if (m.oos_validated) {
    return `<span class="lab-badge lab-badge-ok" title="窗口按 70/30 切分，样本外收益${rr}；实盘通常较样本内低 30%-50%，勿直接外推">✓ 已样本外验证</span>`;
  }
  if (m.oos_status === "insufficient") {
    return `<span class="lab-badge lab-badge-mute" title="样本外交易笔数不足，未做验证">样本外未验证</span>`;
  }
  if (m.oos_status === "ok") {
    return `<span class="lab-badge lab-badge-warn" title="样本内为正但样本外转弱/转负，疑似过拟合${rr}">样本外转弱·疑似过拟合</span>`;
  }
  return `<span class="lab-badge lab-badge-mute">—</span>`;
}

function labMidRow(s) {
  const m = s.mid;
  const insuf = m.status === "insufficient";
  const star = insuf ? `<span class="lab-star" title="交易次数少，结果偶然性大">*</span>` : "";
  const on = LAB.sel === s.key;
  const dimCls = insuf ? "lab-dim" : "";
  const hf = (m.trades != null && m.trades > LAB_HIGH_FREQ);
  const sw = s.is_timing_switch
    ? ` <span class="lab-tag-sw" title="择时/仓位切换类，非选股能力">${esc(s.switch_kind || "择时")}</span>` : "";
  const hfBadge = insuf
    ? ' <span class="lab-badge lab-badge-warn">样本不足</span>'
    : (hf ? ` <span class="lab-badge lab-badge-warn" title="交易极频繁，对成本/滑点高度敏感">高频</span>` : "");
  return `<tr data-key="${escAttr(s.key)}"${on ? ' class="active"' : ''}>
    <td class="num">${m.rank ?? "—"}</td>
    <td class="lab-td-str">${esc(s.name || s.key)}${star}${sw}</td>
    <td class="num ${dimCls}">${labPctText(m.total_return)}</td>
    <td class="num ${dimCls}">${labPctText(m.annual_return)}</td>
    <td class="num">${labRiskText(m.max_dd)}</td>
    <td class="num">${labWinRateText(m.win_rate, m.trades)}</td>
    <td class="num">${labTradeText(m.trades)}${hfBadge}</td>
    <td class="num">${labNumText(m.beat_bench, 1, "%")}</td>
    <td>${labOosBadge(m)}</td>
    <td>${esc(m.rating || "—")}</td>
  </tr>`;
}

function selectStrategy(key) {
  LAB.sel = key;
  // 左栏只切高亮（整体重绘会丢焦点/滚动位置）
  $$("#labLeftBody .lab-item").forEach((it) => it.classList.toggle("active", it.dataset.key === key));
  renderLabMid(labList());     // 中栏需重绘：不可回测被点时显示其占位卡
  const lit = document.querySelector(`#labLeftBody .lab-item[data-key="${key}"]`);
  if (lit && lit.scrollIntoView) { try { lit.scrollIntoView({ block: "nearest" }); } catch (e) { /* 忽略 */ } }
  renderLabDetail();
}

/* 点击策略（左栏清单 / 中栏排名通用）：只做选中（高亮 + 右栏明细）。
   不再自动弹「关联基金」窗——用户点策略想看的是"这策略选什么/怎么解读"，
   基金查询改为明细头部的「关联基金」按钮显式触发（2026-09-29 用户反馈）。 */
function onStrategyClick(key) {
  selectStrategy(key);
}

/* 点击策略 → 弹窗展示对应的公募基金 / ETF（净口径关联，逐条标来源）。
   数据来自后端 /api/fund_strategies（引擎 fund_link.py 写库，接口只读）。 */
function openFundModal(key) {
  const prev = document.getElementById('fundModal');
  if (prev) prev.remove();
  if (LAB._fundEsc) { document.removeEventListener('keydown', LAB._fundEsc); LAB._fundEsc = null; }
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'fundModal';
  overlay.innerHTML = `
    <div class="modal-card fund-modal" role="dialog" aria-label="策略关联基金">
      <div class="fm-head">
        <div class="fm-head-l"><h3>关联基金加载中…</h3></div>
        <button class="fm-x" title="关闭" onclick="document.getElementById('fundModal').remove()">×</button>
      </div>
      <div class="fm-body"><div class="fm-loading">正在查询该策略对应的公募基金 / ETF…</div></div>
    </div>`;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  const onEsc = (e) => {
    if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', onEsc); LAB._fundEsc = null; }
  };
  LAB._fundEsc = onEsc;
  document.addEventListener('keydown', onEsc);
  document.body.appendChild(overlay);
  api('/api/fund_strategies?key=' + encodeURIComponent(key))
    .then((d) => {
      const s = (d && d.strategy) || {};
      const headL = overlay.querySelector('.fm-head-l');
      if (headL) headL.innerHTML = '<h3>' + esc(s.name || s.key || key) + '</h3>' +
        '<div class="fm-sub">' + ([s.school, s.family].filter(Boolean).join(' · ')) +
        (s.key ? ' · key: ' + escAttr(s.key) : '') + '</div>';
      const body = overlay.querySelector('.fm-body');
      window.__fundData = d;
      window.__fundTotal = (d && d.n_strategies_total) || 0;
      if (body) body.innerHTML = renderFundModalBody(d);
    })
    .catch((e) => {
      const body = overlay.querySelector('.fm-body');
      if (body) body.innerHTML = '<div class="fm-note fm-note-warn">查询失败：' +
        esc(String((e && e.message) || e)) + '</div>';
    });
}

function renderFundModalBody(d) {
  if (!d || !d.available) {
    return '<div class="fm-note fm-note-warn">' +
      esc((d && d.message) || '尚未生成基金关联数据。请在项目根目录运行 python fund_link.py。') + '</div>';
  }
  const j = d.judge || {};
  const funds = d.funds || [];
  const n = d.fund_count || 0;
  let html = '';

  /* ① 结论先行：这个策略命中哪些标的、几只 */
  if (j.status === 'not_judgeable') {
    html += '<div class="fm-judge fm-judge-no">该策略无法判定：' +
      esc(j.reason || '所需数据公募基金无法提供') + '</div>';
  } else if (j.status === 'unknown') {
    html += '<div class="fm-judge fm-judge-no">' + esc(j.reason || '该策略未纳入判定范围。') + '</div>';
  } else if (!n) {
    html += '<div class="fm-judge fm-judge-ok">最近 5 个交易日无标的触发该信号' +
      '（并非没有对应基金，而是当前无信号）</div>';
  } else {
    html += '<div class="fm-judge fm-judge-ok"><b>' + n + '</b> 只标的触发该策略' +
      (j.scope && j.scope !== '全部标的' ? ' · 判定范围：' + esc(j.scope) : '') +
      (j.caliber === 'approx' ? ' · 近似判定' : '') + '</div>';
  }

  /* ② 基金清单（主体，放最显眼位置）：9 列紧凑排版（含「策略数」共识度） */
  const fs = (d.funds || []).slice();
  if (_fundSort === 'consensus') {
    fs.sort((a, b) => ((b.n_strategies || 0) - (a.n_strategies || 0)) ||
                      ((b.ret_since || -1e9) - (a.ret_since || -1e9)));
  } else {
    fs.sort((a, b) => ((a.days_ago == null ? 1 : 0) - (b.days_ago == null ? 1 : 0)) ||
                      ((a.days_ago || 0) - (b.days_ago || 0)) ||
                      ((b.ret_since || -1e9) - (a.ret_since || -1e9)));
  }
  if (fs.length) {
    const tot = window.__fundTotal || (d.n_strategies_total || '—');
    html += '<div class="fm-table-wrap"><table class="fm-table"><thead><tr>' +
      '<th>基金</th><th>类型</th>' +
      '<th class="num" title="该基金被多少条策略共同命中（全库 ' + tot + ' 条策略）。数值越大=越多策略同时看多，共识越强">策略数</th>' +
      '<th>信号日</th><th class="num">距今</th>' +
      '<th class="num">信号日至今</th>' +
      '<th class="num" title="最新 20 个交易日涨幅">20日</th>' +
      '<th class="num" title="最新 122 个交易日涨幅（约半年）">半年</th>' +
      '<th class="num" title="最新 244 个交易日涨幅（约一年）">一年</th>' +
      '</tr></thead><tbody>' + fs.map(fmFundRow).join('') + '</tbody></table></div>';
    html += '<div class="fm-foot">排序：' +
      '<span class="fm-sort' + (_fundSort === 'fresh' ? ' on' : '') + '" onclick="fmSetSort(\'fresh\')" title="按信号新鲜度（越靠上越新）">新鲜度</span>' +
      '<span class="fm-sort' + (_fundSort === 'consensus' ? ' on' : '') + '" onclick="fmSetSort(\'consensus\')" title="按被命中策略数（共识度）从高到低">共识度</span>' +
      ' · 悬停任意一行可看该条数据来源</div>';
  }

  /* ③ 口径与来源：折叠收起，不占主视线 */
  html += '<details class="fm-src"><summary>数据来源与口径</summary>' +
    '<div>候选池：' + esc(d.pool_note || '—') + '</div>' +
    '<div>价格口径：' + esc(d.nav_basis || '—') +
    (d.nav_end ? '（截至 ' + esc(d.nav_end) + '）' : '') + '</div>' +
    '<div>生成时间：' + esc(d.generated_at || '—') + '</div>' +
    '<div>来源：' + (d.sources || []).map(esc).join('；') + '</div></details>';
  return html;
}

/* 来源长文本 → 紧凑标签（完整来源放在 title，悬停可见）。 */
function fmSrcShort(s) {
  if (!s) return '—';
  if (s.indexOf('sina.com.cn') >= 0) return '新浪财经·场内日线';
  if (s.indexOf('pingzhongdata') >= 0) return '东方财富·基金净值';
  if (s.indexOf('rankhandler') >= 0) return '东方财富·基金排行';
  if (s.indexOf('strategy_data') >= 0) return '本仓库·策略规则';
  return s.length > 14 ? s.slice(0, 14) + '…' : s;
}

function fmPct(v) {
  return fmtPct(v);
}

function fmPctCls(v) {
  return (v == null) ? '' : (v >= 0 ? 'bull' : 'bear');
}

/* 基金清单排序状态：'fresh'=信号新鲜度（默认）；'consensus'=被命中策略数（共识度） */
let _fundSort = 'fresh';
/* 弹窗当前数据 + 全库策略总数（供「策略数」列 tooltip 用） */
window.__fundData = null;
window.__fundTotal = 0;

/* 共识度分级：≥20 强共识 / 10–19 中 / ≤9 弱 */
function fmNsCls(v) {
  if (v == null) return '';
  if (v >= 20) return 'ns-hi';
  if (v >= 10) return 'ns-mid';
  return 'ns-lo';
}

/* 点击排序切换：重渲染弹窗主体（数据已在 window.__fundData，不重新请求） */
function fmSetSort(mode) {
  if (_fundSort === mode) return;
  _fundSort = mode;
  const b = document.querySelector('#fundModal .fm-body');
  if (b && window.__fundData) b.innerHTML = renderFundModalBody(window.__fundData);
}

function fmFundRow(f) {
  const cal = (f.caliber === 'approx')
    ? '<span class="fm-cal fm-cal-approx" title="近似判定（原规则需日内高低/成交量，此处用收盘价近似）">近似</span>'
    : '<span class="fm-cal fm-cal-exact" title="等价判定（数据口径与原规则一致）">精确</span>';
  const mk = (f.market === '场内')
    ? '<span class="fm-mk fm-mk-in" title="场内 ETF/LOF：有成交量，可判定量能与日内形态类策略">场内</span>'
    : '<span class="fm-mk" title="场外基金：只有净值序列，无成交量">场外</span>';
  /* 类型去掉与市场标签重复的「场内」前缀，避免同一格挤两个同义词 */
  const ty = String(f.fund_type || '').replace(/^场内/, '') || '—';
  /* 信号日只显示 月-日（年份在悬浮提示里），给新增的半年/一年列让出宽度 */
  const sd = String(f.signal_date || '');
  const sdShort = sd.length >= 10 ? sd.slice(5) : sd;
  const srcTip = '来源：' + (f.source || '—');
  return '<tr title="' + escAttr(srcTip) + '">' +
    '<td class="fm-fund"><b>' + esc(f.fund_name || '') + '</b>' +
      '<span class="fm-code">' + esc(f.fund_code || '') + '</span>' + cal + '</td>' +
    '<td class="fm-ty">' + mk + '<span class="fm-ty-t">' + esc(ty) + '</span></td>' +
    '<td class="num fm-ns ' + fmNsCls(f.n_strategies) + '" title="该基金被 ' +
      (f.n_strategies == null ? 0 : f.n_strategies) + ' 条策略共同命中（共 ' +
      (window.__fundTotal || '—') + ' 条）">' + (f.n_strategies == null ? '—' : f.n_strategies) + '</td>' +
    '<td class="fm-date" title="信号日 ' + escAttr(sd) + '">' + esc(sdShort) + '</td>' +
    '<td class="num">' + (f.days_ago == null ? '—' : f.days_ago) + '</td>' +
    '<td class="num ' + fmPctCls(f.ret_since) + '">' + fmPct(f.ret_since) + '</td>' +
    '<td class="num ' + fmPctCls(f.ret_20d) + '">' + fmPct(f.ret_20d) + '</td>' +
    '<td class="num ' + fmPctCls(f.ret_120d) + '">' + fmPct(f.ret_120d) + '</td>' +
    '<td class="num ' + fmPctCls(f.ret_1y) + '">' + fmPct(f.ret_1y) + '</td>' +
  '</tr>';
}

/* 策略解读卡：把数据里已有但此前没上 UI 的 buy/sell/risk/source/params 补出来，
   并按策略类型给一段"怎么解读"指引（2026-09-29 用户反馈：点开只看到数字，不知道策略是什么）。 */
function labHowTo(s) {
  const row = (k, v, cls) => v ? `<div class="lab-how-row${cls ? " " + cls : ""}"><span class="lab-how-k">${k}</span><span class="lab-how-v">${v}</span></div>` : "";
  const interpret = s.is_timing_switch
    ? `这是<b>择时/仓位类</b>策略：它决定「什么时候多拿权益、什么时候退避」，收益来自躲跌与踏空的取舍。解读重点看<b>最大回撤</b>（择时的本职是控回撤）与<b>交易次数</b>（信号越频繁对成本越敏感）；总收益高但回撤也大者，只是"胆子大"，不是"看得准"。`
    : `这是<b>选股/信号类</b>策略：它决定「买什么、何时进出」。解读重点看<b>超额</b>（对基准的超额收益，为正才算有用）与<b>胜率×交易次数</b>（胜率高但笔数极少 = 样本不足，偶然性大）；同时务必对照<b>最大回撤</b>，收益相近时回撤小者优先。`;
  const riskNote = s.risk
    ? row("风险提示", esc(s.risk), "lab-how-risk")
    : "";
  return `<div class="lab-howto">
    <div class="lab-how-head">这个策略是什么 · 怎么解读</div>
    ${s.desc ? `<div class="lab-how-desc">${esc(s.desc)}</div>` : ""}
    ${row("买入条件（选什么）", esc(s.buy || ""))}
    ${row("卖出/退出条件", esc(s.sell || ""))}
    ${row("关键参数", esc(s.params || ""))}
    ${row("思路出处", esc(s.source || ""))}
    ${riskNote}
    <div class="lab-how-interpret">${interpret}</div>
  </div>`;
}

function renderLabDetail() {
  const host = $("#labRightBody");
  if (!host) return;
  const list = toArr(LAB.data && LAB.data.strategies);
  const s = list.find((x) => x.key === LAB.sel);
  if (!s) {
    host.innerHTML = `<div class="empty">点击左侧清单或中栏任一行，查看该策略的「近半年 / 近一年」双窗口明细。</div>`;
    return;
  }
  const howto = labHowTo(s);   // 策略解读卡：三个分支（不可回测/口径受限/正常）都要带上
  const head = `<div class="lab-rd-head">
    <div class="lab-rd-title">${esc(s.name || s.key)}</div>
    <div class="lab-rd-meta">
      <span class="lab-school">${esc(s.school || "")}</span>
      <span class="lab-fam-tag">${esc(s.family || "")}</span>
      ${s.params ? `<span class="lab-fam-tag">${esc(s.params)}</span>` : ""}
      ${statusBadge(s.status)}
      <button class="btn sm" id="labFundBtn" title="查询该策略对应的公募基金 / ETF">关联基金</button>
    </div>
    ${s.desc ? `<div class="lab-rd-desc">${esc(s.desc)}</div>` : ""}
  </div>`;

  if (s.status === "not_backtestable") {
    host.innerHTML = head + howto + `<div class="lab-dashed lab-dashed-lg">
      <span class="lab-badge lab-badge-warn">缺数据 · 不可回测</span>
      <div class="lab-dashed-title">本策略本次不可回测</div>
      <div class="lab-dashed-note">缺什么数据：${esc(s.needs || "—")}</div>
      <div class="lab-dashed-note">${esc(s.status_note || "")}</div>
      <div class="lab-dashed-note muted">已按口径省略所有数字（不显示 0，避免被误读为「收益为 0」）。</div>
    </div>`;
    return;
  }
  if (s.status === "scope_limited") {
    host.innerHTML = head + howto + `<div class="lab-dashed lab-dashed-lg">
      <span class="lab-badge lab-badge-warn">口径受限 · 不予排名</span>
      <div class="lab-dashed-title">该策略不参与回测与排名</div>
      <div class="lab-dashed-note">${esc(s.status_note || "该策略的仓位/横截面逻辑与『等权满仓』口径不符，不予回测与排名")}</div>
      <div class="lab-dashed-note muted">已按口径省略所有数字（不显示 0）。</div>
    </div>`;
    return;
  }
  const wins = toArr(s.windows);
  const swBanner = s.is_timing_switch
    ? `<div class="lab-banner lab-banner-switch">择时/仓位切换类（${esc(s.switch_kind || "择时")}）—— 衡量的是「何时买卖 / 仓位高低」，` +
      `不是选股能力，请勿当作选股战绩解读。</div>` : "";
  const hfNote = labHighFreqNote(s);
  const winHtml = wins.length
    ? wins.map(labWindowCard).join("")
    : `<div class="empty">该策略暂无窗口数据。</div>`;
  host.innerHTML = head + howto + swBanner + hfNote +
    `<div class="lab-wins">${winHtml}</div>` + labDiffBanner(wins) + labBestWorst(s);
  const fb = $("#labFundBtn");
  if (fb) fb.onclick = () => openFundModal(s.key);
}

function labWindowCard(w) {
  const insuf = w.status === "insufficient" || w.status === "no_data";
  const hf = (w.trades != null && w.trades > LAB_HIGH_FREQ);
  // 0 笔了结（且期末仍持仓）→ 单独说明"不是没交易，是还没卖"，避免看着像策略一次没动过。
  const openNoSell = (w.trades === 0);
  const openFoot = openNoSell
    ? `<div class="lab-win-foot"><span class="lab-badge lab-badge-warn">期末仍持仓·未了结</span> ` +
      `<span class="muted">${w.open_at_end ? "窗口结束时仍持仓，收益为浮动盈亏；" : ""}` +
      `交易笔数为 0 表示尚未卖出，不是没有交易。</span></div>`
    : "";
  return `<div class="lab-win">
    <div class="lab-win-head">${esc(w.name || w.window_label)} <span class="lab-win-bars">${w.bars ?? ""} 根</span></div>
    <div class="lab-win-range">${esc(w.start || "")} ~ ${esc(w.end || "")}</div>
    <div class="lab-win-row"><span>策略收益</span><b>${labPctText(w.total_return)}</b></div>
    <div class="lab-win-row"><span>基准同期</span><b>${labPctText(w.bench_return)}</b></div>
    <div class="lab-win-row"><span>超额</span><b>${labPctText(w.excess)}</b></div>
    <div class="lab-win-row"><span>最大回撤</span><b>${labRiskText(w.max_dd)}</b></div>
    <div class="lab-win-row"><span>胜率</span><b>${labWinRateText(w.win_rate, w.trades)}</b></div>
    <div class="lab-win-row"><span>交易次数</span><b>${labTradeText(w.trades)}${hf ? ` <span class="lab-badge lab-badge-warn">高频</span>` : ""}</b></div>
    ${openFoot}
    ${insuf ? `<div class="lab-win-foot"><span class="lab-badge lab-badge-warn">样本不足</span> <span class="muted">${esc(w.sample_note || "")}</span></div>` : ""}
  </div>`;
}

/* 双窗口差异提示：语义色（蓝=提示 / 黄=注意），不用红绿 */
function labDiffBanner(wins) {
  const h1 = wins.find((w) => w.window_label === "h1");
  const y1 = wins.find((w) => w.window_label === "y1");
  if (!h1 || !y1 || h1.excess === null || h1.excess === undefined || y1.excess === null || y1.excess === undefined) return "";
  const e1 = Number(h1.excess), e2 = Number(y1.excess);
  if (e1 > 0 && e2 > 0) return `<div class="lab-banner lab-banner-info">双窗口一致优于基准（近半年与近一年超额均为正）。</div>`;
  if ((e1 > 0) !== (e2 > 0)) return `<div class="lab-banner lab-banner-warn">近期与长期表现背离：两个窗口超额一正一负，谨慎解读。</div>`;
  if (Math.abs(e1 - e2) > 15) return `<div class="lab-banner lab-banner-warn">两窗口结论不一致（超额相差 ${Math.abs(e1 - e2).toFixed(1)} 个百分点），谨慎解读。</div>`;
  return "";
}

/* 高频预警：任一窗口交易笔数超过阈值 → 提示对成本/滑点敏感（本口径不计费用） */
function labHighFreqNote(s) {
  const m = s.mid;
  const t = m && m.trades != null ? m.trades : 0;
  const wt = toArr(s.windows).map((w) => (w.trades != null ? w.trades : 0));
  const hi = Math.max(t, ...(wt.length ? wt : [0]));
  if (hi > LAB_HIGH_FREQ) {
    return `<div class="lab-banner lab-banner-warn">交易极频繁（最多 ${hi} 笔）：结果对交易成本 / 滑点高度敏感，` +
      `而本口径不计手续费与滑点，请谨慎外推。</div>`;
  }
  return "";
}

function labBestWorst(s) {
  const m = s.mid;
  if (!m || (!m.best && !m.worst)) return "";
  const one = (x, label) => x
    ? `<span class="lab-bw">${label}：${esc(x.name || x.code || "")}（${labPctText((x.ret != null ? x.ret : 0) * 100)}）</span>` : "";
  return `<div class="lab-bw-row">${one(m.best, "最佳标的")} ${one(m.worst, "最差标的")} ` +
    `<span class="muted">（单标的极值，仅供参照）</span></div>`;
}

/* 三栏宽度拖拽（≥1100px 生效；窄屏由 CSS 折叠为单列） */
function bindLabHandles() {
  const cols = $("#labCols");
  if (!cols) return;
  $$("#labCols .lab-handle").forEach((h) => {
    h.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const side = h.dataset.side;
      const startX = e.clientX;
      const base = side === "l" ? LAB.wl : LAB.wr;
      const move = (ev) => {
        let d = ev.clientX - startX;
        if (side === "r") d = -d;                       // 右栏手柄：向左拖变宽
        const v = Math.max(220, Math.min(560, base + d));
        if (side === "l") { LAB.wl = v; cols.style.setProperty("--lab-wl", v + "px"); }
        else { LAB.wr = v; cols.style.setProperty("--lab-wr", v + "px"); }
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  });
}

/* ================= 刷新状态（Request G） =================
   需求：① 数据列表新增「最后更新」列，显示每条数据最近一次更新的时刻，
         超时未更新则在对应行给出高亮/告警；② 实时刷新（每 5 分钟）的状态由
         后台记录（前端不展示其内部机制）；③ 补齐刷新状态标识、下次刷新倒计时、
         手动刷新按钮等实用入口。

   口径（与后端 /api/refresh_status 保持一致）：
   - 「最后更新」= 该行来源站点最近一次**成功**刷新完成的时刻（失败不前移）。
   - 来源未纳入当前抓取配置 → 「未纳入刷新」（中性灰，不算超时，避免噪声）。
   - 超过 stale 秒（默认 600s = 2×5min）未成功更新 → 该格/该行标红 + ⚠。
   - 多来源行：展示其中最新的一次成功刷新；只要有任一来源超时，即标记该行为超时。 */
const RS = {
  interval: 300, stale: 600, intervalMs: 300000, staleMs: 600000,
  lastOk: null, status: "never", error: "", tracked: [], sources: {},
  // 2026-10-05（建议3）：调度口径字段，后端 /api/refresh_status 回传
  schedulerLastRunAt: null, schedulerEnabled: true,
  // 2026-10-09（P0 行情新鲜度）：与「论坛帖子抓取」严格分离的行情新鲜度。
  //   后端 /api/refresh_status 新增 market 字段（老接口无此字段时降级），或独立
  //   /api/market_freshness 返回同一 market 对象。**绝不用帖子抓取时间冒充行情时间**。
  //   market = { src, quote_time, server_now, lag_seconds, is_trading }
  market: null, marketFromStatus: null, marketProbeAt: 0,
  // 2026-10-30：已退役/已冻结数据源（后端 /api/refresh_status 的 retired_feeds）。
  //   用途不是"报故障"，是**防止把冻结数据当今天的**——这些面板不会报错，只是不再更新。
  retired: [],
};
/* 盘中行情滞后超过该秒数即视为「报价不新鲜」，顶栏行情栏切警示色（2026-10-09） */
const MARKET_STALE_SEC = 120;
let RS_READY = false;
let rtTimer = null;   // 自动刷新定时器
let rtNextAt = null;  // 下一次自动刷新的时间戳（用于倒计时）

/* 后台自动调度状态（白天交易时段定时抓取 taoguba + eastmoney） */
const SCHED = { enabled: true, last_run_at: null, next_run_at: null,
                run_count: 0, errors_today: 0, in_window: false };

/* ── 盘中全页轮询（P3，2026-09-29）──────────────────────────────────
 * 交易时段内按固定周期「重渲染当前激活标签页」（与手动切标签同一入口 selectNav(cat,tab,false)），
 * 让看盘界面始终保持最新，无需手动刷新。设计要点：
 *   · 仅在交易时段（工作日 09:30–11:30 / 13:00–15:00）运行；非交易时段 / 页面隐藏 / 开关关闭 自动停表。
 *   · 只重渲染「当前页」，不拉全部面板，避免无谓上游请求；数据由各 loadX() 自带重取数保证新鲜。
 *   · 开关状态用 localStorage 持久化，刷新页面后保持。
 *   · 周期默认 60s，可按需调大（如 120s）以降低上游请求频率。 */
const PAGE_POLL = { enabled: false, periodMs: 60000, timer: null, nextAt: null, running: false };

/* 交易时段判定：工作日 + 两个连续交易区间（午休 11:30–13:00 视为非交易） */
function isTradingNow(d) {
  d = d || new Date();
  const day = d.getDay();
  if (day === 0 || day === 6) return false;                  // 周六日休市
  const mins = d.getHours() * 60 + d.getMinutes();
  const am = mins >= 9 * 60 + 30 && mins <= 11 * 60 + 30;    // 09:30–11:30
  const pm = mins >= 13 * 60 && mins <= 15 * 60;             // 13:00–15:00
  return am || pm;
}

/* 顶栏「盘中轮询」状态药丸 */
function renderPagePollState() {
  const el = $("#pagePollState");
  if (!el) return;
  if (!PAGE_POLL.enabled) { el.textContent = "盘中轮询：关闭"; el.className = "rb-poll off"; return; }
  if (!isTradingNow())     { el.textContent = "盘中轮询：非交易时段"; el.className = "rb-poll idle"; return; }
  if (document.hidden)     { el.textContent = "盘中轮询：页面隐藏暂停"; el.className = "rb-poll idle"; return; }
  const cd = PAGE_POLL.nextAt ? fmtCountdown(Math.max(0, Math.round((PAGE_POLL.nextAt - Date.now()) / 1000))) : "—";
  el.textContent = `盘中轮询：运行中 · ${cd} 后刷新当前页`;
  el.className = "rb-poll on";
}

function startPagePoll() {
  if (PAGE_POLL.timer) clearTimeout(PAGE_POLL.timer);
  PAGE_POLL.nextAt = Date.now() + PAGE_POLL.periodMs;
  PAGE_POLL.timer = setTimeout(pagePollTick, PAGE_POLL.periodMs);
  PAGE_POLL.running = true;
  renderPagePollState();
}
function stopPagePoll() {
  if (PAGE_POLL.timer) { clearTimeout(PAGE_POLL.timer); PAGE_POLL.timer = null; }
  PAGE_POLL.running = false; PAGE_POLL.nextAt = null;
  renderPagePollState();
}

/* 单次轮询：重渲染当前激活标签（selectNav 可安全重入） */
function pagePollTick() {
  // 任何一层不满足都先停表并清掉定时器，避免空转
  if (!PAGE_POLL.enabled || !isTradingNow() || document.hidden) {
    if (PAGE_POLL.timer) { clearTimeout(PAGE_POLL.timer); PAGE_POLL.timer = null; }
    PAGE_POLL.running = false;
    renderPagePollState();
    return;
  }
  try { selectNav(CURRENT.cat, CURRENT.tab, false); }
  catch (e) { /* 单标签渲染异常不应打断轮询节奏 */ }
  PAGE_POLL.nextAt = Date.now() + PAGE_POLL.periodMs;
  if (PAGE_POLL.timer) clearTimeout(PAGE_POLL.timer);
  PAGE_POLL.timer = setTimeout(pagePollTick, PAGE_POLL.periodMs);
  renderPagePollState();
}

/* 由每秒 tick 调用：交易时段开始 / 页面恢复可见时，自动拉起轮询 */
function pagePollAutoManage() {
  if (!PAGE_POLL.enabled) return;
  if (!isTradingNow() || document.hidden) {
    if (PAGE_POLL.running) {
      if (PAGE_POLL.timer) { clearTimeout(PAGE_POLL.timer); PAGE_POLL.timer = null; }
      PAGE_POLL.running = false;
    }
    return;
  }
  if (!PAGE_POLL.running && !PAGE_POLL.timer) startPagePoll();
}

function relAge(s) {
  const d = parseLocalTime(s);
  if (!d) return "—";
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  return fmtAge(Math.abs(sec));
}
function loadSchedulerStatus() {
  return api("/api/scheduler/status").then((d) => {
    if (d && typeof d === "object") {
      SCHED.enabled = !!d.enabled;
      SCHED.last_run_at = d.last_run_at || null;
      SCHED.next_run_at = d.next_run_at || null;
      SCHED.run_count = Number(d.run_count) || 0;
      SCHED.errors_today = Number(d.errors_today) || 0;
      SCHED.in_window = !!d.in_window;
      const cb = $("#schedAuto");
      if (cb && cb.checked !== SCHED.enabled) cb.checked = SCHED.enabled;
    }
    renderSchedText();
    return SCHED;
  }).catch(() => { renderSchedText(); return SCHED; });
}
function renderSchedText() {
  const el = $("#schedText");
  if (!el) return;
  if (!SCHED.enabled) {
    el.textContent = "📡 调度状态：已关闭";
    el.className = "rb-sched off";
    return;
  }
  /* fmtAge/relAge 已自带「前」字，这里不再重复拼后缀（2026-09-29 修「10 分钟前 前」叠字）；
     「下次」用 fmtIn 输出未来时态（X 秒后），而非拿 relAge 的「X 秒前」硬改「后」。 */
  const last = SCHED.last_run_at ? ("上次 " + relAge(SCHED.last_run_at)) : "上次 从未";
  const nextAt = parseLocalTime(SCHED.next_run_at);
  const next = nextAt ? ("下次 " + fmtIn(Math.max(0, Math.round((nextAt.getTime() - Date.now()) / 1000)))) : "下次 —";
  el.textContent = `📡 调度状态：${last} / ${next}`;
  el.className = "rb-sched " + (SCHED.in_window ? "on" : "idle");
}

/* 后端时间为本地时间字符串 "YYYY-MM-DD HH:MM:SS"，手工解析以避免时区歧义。 */
function parseLocalTime(s) {
  if (!s) return null;
  const m = String(s).trim().match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
}
function fmtClock(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtAge(sec) {
  if (sec == null) return "";
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}
/* 未来时态版 fmtAge：用于「下次 X 后」类倒计时展示（2026-09-29） */
function fmtIn(sec) {
  if (sec == null || sec < 0) return "—";
  if (sec < 60) return `${sec} 秒后`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟后`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时后`;
  return `${Math.floor(sec / 86400)} 天后`;
}
function fmtCountdown(sec) {
  if (sec == null || sec < 0) return "—";
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/* ================= 「数据日」徽标工具（2026-10-09 P0，可复用） =================
   背景（王总投诉）：顶栏写着「刷新在 6 分钟前」，面板里的「最新价」却是昨天（10-08）的收盘价 ——
   因为「主力净流入榜 / 龙虎榜 / 主线研判」等面板的数据源本身就是**东财日报表（T-1）**，
   盘中根本没有当天值。可面板上又完全不显示归属日，于是「界面刚刷新」被误当成「价是今天的」。
   对策：凡「日报表 / 库内日快照」口径的面板，都强制调用本函数在显著位置标出**数据日**。
   规则：
     · 盘中快照（is_intraday=true）→ 「⚡ 盘中快照 · 更新于 HH:MM」，与「日终」视觉可辨；
     · 归属日=今日 → 「📅 实时 / 当日 YYYY-MM-DD」；
     · 归属日≠今日 → 「📅 数据日 YYYY-MM-DD（日终）」，琥珀警示色；
     · NULL / 空 → 「数据日 —」中性灰，**绝不当今天**。
   返回 HTML 字符串（已转义），可直接拼进模板。 */
function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function dataDayBadge(date, opts) {
  opts = opts || {};
  const raw = (date == null ? "" : String(date)).trim();
  const dm = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  const tm = raw.match(/[ T](\d{1,2}):(\d{2})/);
  const day = dm ? `${dm[1]}-${String(dm[2]).padStart(2, "0")}-${String(dm[3]).padStart(2, "0")}` : "";
  const p2 = (n) => String(n).padStart(2, "0");
  // 句中带时间（"YYYY-MM-DD HH:MM[:SS]"）或显式 opts.time → 盘中口径优先用它
  const hhmm = opts.time ? String(opts.time).slice(0, 5) : (tm ? `${p2(tm[1])}:${tm[2]}` : "");
  if (opts.intraday === true) {
    const tt = hhmm ? `更新于 ${hhmm}` : (day ? `${day} 盘中` : "盘中");
    return `<span class="dd-badge dd-intraday" title="${escAttr("盘中快照口径：该值取自当日盘中实时采样，尚未日终结算，会随盘中变化。")}">⚡ 盘中快照 · ${esc(tt)}</span>`;
  }
  if (!day) {
    return `<span class="dd-badge dd-none" title="${escAttr("该面板数据源未提供数据日，无法判定数据归属日——请勿默认当作今日数据。")}">📅 数据日 —</span>`;
  }
  const isToday = day === (opts.today || todayStr());
  if (isToday && opts.allowToday !== false) {
    return `<span class="dd-badge dd-today" title="${escAttr("数据归属日=" + day + "（今日）")}">📅 实时 / 当日 ${esc(day)}</span>`;
  }
  return `<span class="dd-badge dd-past" title="${escAttr("数据归属日=" + day + "，非今日 —— 属日终（T-1/历史）口径，不是当前实时值。")}">📅 数据日 ${esc(day)}<i>（日终）</i></span>`;
}
function safeJsonArr(s) {
  try { const v = JSON.parse(s || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; }
}
function freshnessOf(siteKeys) {
  const keys = toArr(siteKeys).filter(Boolean);
  if (!RS_READY) return { state: "loading", names: keys };
  if (!keys.length) return { state: "unknown", names: [] };
  const now = Date.now();
  let newest = null, anyStale = false, staleName = "", okName = "";
  keys.forEach((k) => {
    const s = RS.sources[k];
    const d = s ? parseLocalTime(s.last_ok_at) : null;
    if (!d) return;
    if (!newest || d.getTime() > newest.getTime()) { newest = d; okName = s.display_name || k; }
    if (now - d.getTime() > RS.staleMs) { anyStale = true; if (!staleName) staleName = s.display_name || k; }
  });
  if (!newest) return { state: "untracked", names: keys };
  const age = Math.max(0, Math.round((now - newest.getTime()) / 1000));
  return { state: anyStale ? "stale" : "ok", time: newest, age, name: anyStale ? staleName : okName };
}
function freshCls(state) {
  return ({ ok: "fr-ok", stale: "fr-stale", untracked: "fr-untracked", unknown: "fr-unknown", loading: "fr-loading" }[state]) || "fr-unknown";
}
function freshTitle(f) {
  const staleMin = Math.round(RS.stale / 60);
  if (f.state === "ok") return `来源「${f.name}」最近一次成功刷新：${fmtClock(f.time)}（${fmtAge(f.age)}）`;
  if (f.state === "stale") return `来源「${f.name}」已超过 ${staleMin} 分钟未成功更新，该行数据可能已过期`;
  if (f.state === "untracked") return `来源「${toArr(f.names).join("、")}」未纳入当前抓取配置，无法判定更新时效`;
  if (f.state === "loading") return "正在读取刷新状态…";
  return "该行未记录来源站点，无法判定更新时效";
}
function freshText(f) {
  if (f.state === "loading") return "…";
  if (f.state === "ok") return `${fmtClock(f.time)}<span class="fresh-age">${fmtAge(f.age)}</span>`;
  if (f.state === "stale") return `⚠ ${fmtClock(f.time)}<span class="fresh-age">${fmtAge(f.age)} 未更新</span>`;
  if (f.state === "untracked") return "未纳入刷新";
  return "—";
}
const FRESH_CHIP_TEXT = { ok: "已更新", stale: "⚠ 未更新", untracked: "未纳入刷新", loading: "…", unknown: "" };
/* 各表格「最后更新」列的统一图例文案（阈值取自后端回传的 stale_seconds） */
function freshLegend() {
  return `「最后更新」列为该行<b>来源站点</b>最近一次<b>成功刷新</b>的时刻；超过 ${Math.round(RS.stale / 60)} 分钟未成功更新时，该行会标红 ⚠ 告警（灰色「未纳入刷新」表示该来源不在当前抓取配置内，不计入超时）。`;
}

/* 表格单元格：<td>，带 data-fresh 以便周期重算（无需整表重绘） */
function refCell(siteKeys) {
  const keys = toArr(siteKeys).filter(Boolean);
  const f = freshnessOf(keys);
  return `<td class="td-fresh ${freshCls(f.state)}" data-fresh="${escAttr(JSON.stringify(keys))}" title="${escAttr(freshTitle(f))}">${freshText(f)}</td>`;
}
/* 卡片角标：股票卡片等非表格场景 */
function refChip(siteKeys) {
  const keys = toArr(siteKeys).filter(Boolean);
  const f = freshnessOf(keys);
  return `<span class="chip-s chip-fresh ${freshCls(f.state)}" data-fresh="${escAttr(JSON.stringify(keys))}" title="${escAttr(freshTitle(f))}">${FRESH_CHIP_TEXT[f.state] || ""}</span>`;
}
/* 刷新状态变化后重算已渲染的时效节点（由 loadRefreshStatus 与定时器调用） */
function applyFreshness() {
  $$("td.td-fresh[data-fresh]").forEach((td) => {
    const f = freshnessOf(safeJsonArr(td.getAttribute("data-fresh")));
    td.className = "td-fresh " + freshCls(f.state);
    td.innerHTML = freshText(f);
    td.title = freshTitle(f);
  });
  $$(".chip-fresh[data-fresh]").forEach((elm) => {
    const f = freshnessOf(safeJsonArr(elm.getAttribute("data-fresh")));
    elm.className = "chip-s chip-fresh " + freshCls(f.state);
    elm.textContent = FRESH_CHIP_TEXT[f.state] || "";
    elm.title = freshTitle(f);
  });
}

/* 顶栏刷新状态标识 + 下次自动刷新倒计时
   ---------------------------------------------------------------------------
   2026-10-09（P0 拆栏）：原先把「论坛帖子抓取」的 last_ok_at 含糊地写成
   「已更新于 X 分钟前」，王总据此以为**行情**也是新的 —— 实则该时间与行情价格毫无关系
   （后台只抓帖子文本 + 板块资金流采样，不抓个股行情）。现拆成**两栏物理分离**：
     ① 帖抓取（本函数 #rsDot/#rsText）：沿用 last_ok_at，明确标注「帖抓取」；
     ② 行情（renderMarketBar #rsMktDot/#rsMarket）：用 market.quote_time + lag_seconds，
        盘中滞后 >120s 警示。两栏都在这一屏、一眼可辨，互不冒充。 */
function renderRefreshBar() {
  const dot = $("#rsDot"), txt = $("#rsText"), cd = $("#rsCountdown");
  if (!dot || !txt) return;
  const d = parseLocalTime(RS.lastOk);
  const age = d ? Math.max(0, Math.round((Date.now() - d.getTime()) / 1000)) : null;
  let cls = "rs-never", label = "帖抓取：尚未刷新";
  if (RS.status === "ok" && d) {
    const stale = age > RS.stale;
    // 2026-10-05（报告 P1 / 建议3）统一口径：
    // 后端现在每轮调度结束都会回写 __all__ 的 last_ok_at，正常情况下这里不该再误报；
    // 但一旦回写失败（或调度被关），仅凭一个陈旧的 last_ok_at 直接喊「已超时」
    // 会把用户吓一跳。这里以「调度最近是否真的跑过」做一次兜底仲裁：
    // 调度刚跑过 → 判「调度中」，不判超时。
    const schedRun = parseLocalTime(RS.schedulerLastRunAt);
    const schedFresh = schedRun && RS.schedulerEnabled
      && schedRun.getTime() > Date.now() - RS.stale * 1000;
    cls = stale ? "rs-stale" : "rs-ok";
    if (stale && schedFresh) {
      cls = "rs-ok";
      const sc = fmtAge(Math.max(0, Math.round((Date.now() - schedRun.getTime()) / 1000)));
      label = `帖抓取：调度中（${sc}前刚跑完）｜帖子截至 ${fmtClock(d)}`;
    } else {
      label = `帖抓取：${stale ? "已超时" : "已更新"} ${fmtClock(d)}（${fmtAge(age)}）`;
    }
  } else if (RS.status === "error") {
    cls = "rs-err";
    label = d ? `帖抓取：抓取失败（上次成功 ${fmtClock(d)}）` : "帖抓取：抓取失败";
  }
  dot.className = "rs-dot " + cls;
  // 2026-10-30：论坛已整体停抓。若不在这里改口径，顶栏会永远显示「尚未刷新 / 已超时」，
  //   看起来像故障，实际是主动停用 —— 两者必须分清，否则以后没人敢相信这个指示灯。
  const _rf = toArr(RS.retired).find((x) => x && x.key === "forum");
  if (_rf) {
    dot.className = "rs-dot rs-never";
    label = `帖抓取：已停用（${_rf.retired_at || "—"}）`;
    txt.title = (_rf.why || "") + (RS.error ? `\n${RS.error}` : "");
  } else {
    txt.title = RS.error
      ? RS.error
      : "「帖抓取」＝论坛帖子文本的最近一次成功抓取时间，与行情价格无关（行情另见右侧「行情」栏）";
  }
  txt.textContent = label;
  // 外层 .rb-item 的静态 title 原文是「最近一次成功刷新与下次自动刷新时刻」，拆分后已不能只指行情，
  // 这里顺手改写成明确口径（不改 index.html，避免与其他人的并行改动冲突）。
  const _item = dot.parentNode;
  if (_item && _item.classList && _item.classList.contains("rb-item")) {
    _item.title = "「帖抓取」＝论坛帖子文本的最近一次成功抓取时间；行情价格另见右侧「行情」栏（两者无关）";
  }
  if (cd) {
    const rt = $("#realtime");
    if (rt && rt.checked && rtNextAt) {
      cd.textContent = `自动刷新：${fmtCountdown(Math.max(0, Math.round((rtNextAt - Date.now()) / 1000)))} 后`;
    } else {
      cd.textContent = "自动刷新：未开启";
    }
  }
  renderMarketBar();   // 第二栏：行情新鲜度（独立于帖抓取）
}

/* ---------- 顶栏「行情」栏（2026-10-09 P0） ----------
   新 DOM 由 JS 动态插入（不动 index.html，避免与其他人的并行改动冲突）：
   位置在 #rsCountdown 之后、.spacer 之前。降级：拿不到 market 字段 → 「行情：时效未知」。 */
function ensureMarketBar() {
  let wrap = document.getElementById("rsMarketWrap");
  if (wrap) return wrap;
  const cd = document.getElementById("rsCountdown");
  if (!cd || !cd.parentNode) return null;
  wrap = document.createElement("span");
  wrap.className = "rb-item rb-market rb-market-unknown";
  wrap.id = "rsMarketWrap";
  wrap.title = "行情新鲜度：行情源最近一笔报价的时间与相对服务端的滞后秒数（与左侧「帖抓取」无关）";
  wrap.innerHTML = '<span class="rs-dot rs-never" id="rsMktDot"></span><span id="rsMarket">行情：时效未知</span>';
  cd.parentNode.insertBefore(wrap, cd.nextSibling);
  return wrap;
}
function renderMarketBar() {
  const wrap = ensureMarketBar();
  if (!wrap) return;
  const dot = document.getElementById("rsMktDot");
  const txt = document.getElementById("rsMarket");
  if (!txt) return;
  const m = RS.market;
  if (!m || typeof m !== "object") {
    // 降级路径：后端尚未提供行情字段 → 明确「时效未知」，绝不用帖子抓取时间冒充行情时间。
    wrap.className = "rb-item rb-market rb-market-unknown";
    if (dot) dot.className = "rs-dot rs-never";
    txt.textContent = "行情：时效未知";
    txt.title = "后端未提供行情新鲜度（/api/refresh_status 的 market 字段或 /api/market_freshness 均不可用）"
      + "——无法判定行情是否新鲜。注意：左侧「帖抓取」时间与行情价格无关，不能拿来推断行情。";
    return;
  }
  const qd = parseLocalTime(m.quote_time);
  const clock = qd ? fmtClock(qd) : (m.quote_time ? String(m.quote_time) : "");
  // 滞后秒数：优先用后端给的 lag_seconds；缺失则用 server_now − quote_time 现算
  let lag = (m.lag_seconds == null) ? null : Number(m.lag_seconds);
  if (lag == null || isNaN(lag)) {
    const sd = parseLocalTime(m.server_now);
    lag = (sd && qd) ? Math.max(0, Math.round((sd.getTime() - qd.getTime()) / 1000)) : null;
  }
  const trading = m.is_trading === true;
  let dotCls = "rs-never", wrapExtra = "rb-market-unknown", text = "";
  if (!clock) {
    dotCls = "rs-never"; wrapExtra = "rb-market-unknown";
    text = "行情：时间未知";
  } else if (!trading) {
    dotCls = "rs-never"; wrapExtra = "rb-market-closed";
    text = `行情 ${clock}（休市${lag == null ? "" : ` · 滞后 ${lag} 秒`}）`;
  } else if (lag != null && lag > MARKET_STALE_SEC) {
    dotCls = "rs-stale"; wrapExtra = "rb-market-stale";
    text = `行情 ${clock}（滞后 ${lag} 秒 ⚠）`;
  } else {
    dotCls = "rs-ok"; wrapExtra = "rb-market-ok";
    text = `行情 ${clock}（${lag == null ? "盘中" : `滞后 ${lag} 秒`}）`;
  }
  if (dot) dot.className = "rs-dot " + dotCls;
  txt.textContent = text;
  txt.title = [
    `行情源最近报价：${clock || "—"}`,
    lag == null ? "" : `滞后 ${lag} 秒${trading && lag > MARKET_STALE_SEC ? "（已超 " + MARKET_STALE_SEC + " 秒，报价可能不新鲜）" : ""}`,
    trading ? "盘中" : "休市",
    m.src ? `来源 ${m.src}` : "",
    m.server_now ? `服务端时间 ${m.server_now}` : "",
  ].filter(Boolean).join(" · ");
  wrap.className = "rb-item rb-market " + wrapExtra;
}

/* 拉取后台记录的刷新状态（每次抓取后由后端写入） */
function loadRefreshStatus() {
  return api("/api/refresh_status").then((d) => {
    if (d && typeof d === "object" && !Array.isArray(d)) {
      RS.interval = Number(d.interval_seconds) || RS.interval;
      RS.stale = Number(d.stale_seconds) || RS.stale;
      RS.intervalMs = RS.interval * 1000;
      RS.staleMs = RS.stale * 1000;
      RS.lastOk = d.last_ok_at || null;
      RS.status = d.status || "never";
      RS.error = d.error || "";
      RS.tracked = toArr(d.tracked_sources);
      // 建议3：调度口径一并带过来（顶栏用它做「调度中 / 已超时」仲裁）
      RS.schedulerLastRunAt = d.scheduler_last_run_at || null;
      RS.schedulerEnabled = d.scheduler_enabled !== false;
      const map = {};
      toArr(d.sources).forEach((s) => { if (s && s.source_key) map[s.source_key] = s; });
      RS.sources = map;
      // 2026-10-09（P0）：新契约 —— refresh_status 内嵌 market 字段（老接口无此字段则保持 null）
      RS.marketFromStatus = (d.market && typeof d.market === "object") ? d.market : null;
      if (RS.marketFromStatus) RS.market = RS.marketFromStatus;
      // 2026-10-30：已退役/冻结的数据源（论坛/雪球/抖音语料），供顶栏提示与面板判断。
      RS.retired = toArr(d.retired_feeds);
      RS_READY = true;
    }
    renderRefreshBar();
    applyFreshness();
    maybeProbeMarketFreshness();
    return RS;
  }).catch(() => {
    // 接口不可用时降级：不阻塞面板渲染，「最后更新」列显示「—」。
    RS_READY = true;
    renderRefreshBar();
    applyFreshness();
    return RS;
  });
}

/* 行情新鲜度双契约兼容（2026-10-09 P0）：
   优先用 /api/refresh_status 内嵌的 market；没有就（限频 5 分钟一次，避免刷 404 噪声）
   试独立接口 /api/market_freshness。两者都拿不到时，renderMarketBar 显示「时效未知」。 */
function maybeProbeMarketFreshness() {
  if (RS.marketFromStatus) { renderMarketBar(); return; }         // 已内嵌，直接用
  const now = Date.now();
  if (now - (RS.marketProbeAt || 0) < 300000) { renderMarketBar(); return; }
  RS.marketProbeAt = now;
  api("/api/market_freshness").then((m) => {
    if (m && typeof m === "object" && !m.error && (m.quote_time || m.lag_seconds != null)) {
      RS.market = m;
    }
    renderMarketBar();
  }).catch(() => { renderMarketBar(); });
}

/* 立即抓取一次并刷新全部面板 */
function refreshNow() {
  const b = $("#btnRefreshNow");
  if (b) { b.disabled = true; b.textContent = "刷新中…"; }
  setHint("正在抓取并刷新…");
  return api("/api/fetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((r) => { setHint("刷新完成：" + JSON.stringify((r && r.results) || [])); return loadAll(); })
    .catch((e) => setHint("刷新失败：" + apiErrText(e)))
    .finally(() => {
      if (b) { b.disabled = false; b.textContent = "立即刷新"; }
      if ($("#realtime") && $("#realtime").checked) { if (rtTimer) clearTimeout(rtTimer); armRealtime(); }
    });
}

/* ---------- 操作 ---------- */
function busy(b) { $("#btnFetch").disabled = b; $("#btnFetch").textContent = b ? "抓取中…" : "抓取并更新"; }
$("#btnFetch").onclick = () => {
  busy(true); setHint("正在登录检测并抓取…");
  api("/api/login_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then(() => api("/api/fetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }))
    .then((r) => {
      const errs = (r.results || []).filter(x => x.error).map(x => `${x.site}: ${x.error}`);
      if (errs.length) {
        // 2026-09-24：抓取失败时不再只放一行 hint，而是直接给一个带「点此登录」按钮的醒目横幅。
        const needLogin = errs.some(e => /登录|login_failed/i.test(e));
        const banner = needLogin
          ? `抓取失败：${errs.join("；")}。请点页面上的「点此登录」按钮完成登录后再次抓取。`
          : `抓取失败：${errs.join("；")}`;
        // 直接 innerHTML 覆盖 statusHint（不调 setHint，否则会被它 textContent 清掉）
        $("#statusHint").innerHTML = `<span class="banner banner-warn" style="margin:0">${esc(banner)} ` +
          (needLogin ? `<button class="btn sm primary" onclick="openLoginWindow('taoguba')">点此登录</button>` : "") +
          `</span>`;
      } else {
        setHint("抓取完成：" + JSON.stringify(r.results));
      }
      loadAll();
    })
    .catch((e) => setHint("抓取出错：" + apiErrText(e))).finally(() => busy(false));
};
$("#btnLogin").onclick = () => {
  setHint("正在检测各站点登录态…");
  api("/api/login_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((r) => { setHint("登录检测：" + JSON.stringify(r.results)); loadAll(); })
    .catch((e) => setHint("登录检测失败：" + apiErrText(e)));
};
/* ---------- 破坏性操作的二次确认（2026-09-23，P0-2 前端半边） ----------
   后端 reset=true 的 /api/overseas 会清空海外业务数据、且必须显式 confirm，
   否则一律 409 拒绝执行。这里在发起请求前先弹确认，既避免误点毁数据，
   也把「到底会不会动数据」讲清楚；用户取消则完全不发请求、「不碰任何数据」。
   注：原「载入示例」入口已移除（含义不清，且会清空真实数据）。 */
const DESTRUCTIVE_OVERSEAS_MSG =
  "「刷新海外舆情」会重置并清空现有海外新闻 / 热点 / 数据源状态，然后重新抓取。\n" +
  "此操作不可撤销（后端会自动做一份整库快照，可人工回滚）。\n\n确定继续？";

$("#btnOverseas").onclick = () => {
  if (!window.confirm(DESTRUCTIVE_OVERSEAS_MSG)) { setHint("已取消：未改动任何数据。"); return; }
  setHint("正在联网抓取海外源（可达源实时抓取、被墙源标记待激活）…");
  $("#btnOverseas").disabled = true; $("#btnOverseas").textContent = "抓取中…";
  api("/api/overseas", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ live: true, reset: true, confirm: true }) })
    .then((r) => {
      const res = r.result || {};
      setHint(`海外舆情已刷新：实时抓取 ${res.fetched || 0} 条、示例补充 ${res.seed_added || 0} 条、热点 ${res.hotspots || 0} 个。被墙源标记为「待激活」。`);
      loadAll();
    })
    .catch((e) => setHint("海外刷新失败：" + apiErrText(e)))
    .finally(() => { $("#btnOverseas").disabled = false; $("#btnOverseas").textContent = "刷新海外舆情(联网)"; });
};

/* ---------- 一键刷新（2026-10-20 进哥要求收敛顶栏） ----------
   把「登录检测 → 抓取新数据 → 海外舆情 → 国际局势同步」四个动作串成一个按钮，
   逐项显示进度与结果，任何一步失败不阻塞后续步骤、最后汇总报告。
   顶栏旧四按钮已隐藏（id 保留，绑定不拆），本函数成为唯一手动入口。 */
const ALLR = { running: false };
async function allRefresh() {
  if (ALLR.running) { setHint("一键刷新正在跑，请等它结束…"); return; }
  ALLR.running = true;
  const b = $("#btnAllRefresh");
  if (b) b.disabled = true;
  const notes = [];
  const tryStep = async (n, label, fn) => {
    if (b) b.textContent = `刷新中 ${n}/4 · ${label}`;
    setHint(`一键刷新 ${n}/4：${label}…`);
    try { return await fn(); }
    catch (e) { notes.push(`${label} 失败(${apiErrText(e)})，已跳过`); return null; }
  };
  await tryStep(1, "登录检测", () =>
    api("/api/login_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
  const fr = await tryStep(2, "抓取新数据", () =>
    api("/api/fetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
  if (fr) {
    const errs = ((fr && fr.results) || []).filter((x) => x.error).map((x) => `${x.site}: ${x.error}`);
    if (errs.length) notes.push("抓取部分站点失败: " + errs.join("；"));
    else notes.push("本地数据已更新");
  }
  const or = await tryStep(3, "海外舆情", () =>
    api("/api/overseas", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ live: true, reset: true, confirm: true }) }));
  if (or && or.result) notes.push(`海外抓取 ${or.result.fetched || 0} 条/热点 ${or.result.hotspots || 0}（失败会自动保留旧数据）`);
  await tryStep(4, "国际局势同步", () => api("/api/geo_refresh"));
  ALLR.running = false;
  if (b) { b.disabled = false; b.textContent = "一键刷新"; }
  setHint("✅ 一键刷新完成 → " + notes.join(" ｜ "));
  loadAll();
}
$("#btnAllRefresh").onclick = allRefresh;
/* ================= 自选 / 关注池 + 到价提醒（2026-09-27 新增一级分类） =================
   数据：GET /api/watch_list（分组 + 标的 + 最新价）、GET /api/watch_alerts（告警历史）。
   写：POST /api/watch_item_add|update|remove、/api/watch_group_add|rename|remove、
       /api/watch_check（到价检测）、/api/watch_alert_read（标记已读 / 全部已读）。
   隔离：独立模块 watchlib.py（表 watch_*），零 JOIN 既有业务表。
   诚实口径：取不到价一律 «—»，**绝不显 0**；分组名可在「分组管理」里直接改。 */
const WATCH = { data: null, alerts: null, selGroup: "all", poll: null, signalDate: null };

function wtMoney(v, d = 2) {
  if (v === null || v === undefined || v === "" || isNaN(v)) return '<span class="muted">—</span>';
  return Number(v).toFixed(d);
}
/* 提醒规则 → 文案。box_breakout/box_retest 为「长期低位·突破雷达」推入关注池的突破/回踩事件，
   需与旧版 above/below（到价提醒）共存，故集中映射，避免各渲染处硬编码漏改。 */
function wtAlertRuleText(rule) {
  if (rule === "above") return "升破";
  if (rule === "below") return "跌破";
  if (rule === "box_breakout") return "放量突破";
  if (rule === "box_retest") return "回踩确认";
  return String(rule || "—");
}
function wtAlertRuleDir(rule) {
  if (rule === "above") return "上破 ↑";
  if (rule === "below") return "下穿 ↓";
  if (rule === "box_breakout") return "突破 ↑";
  if (rule === "box_retest") return "回踩 ↓";
  return String(rule || "—");
}
/* 突破雷达状态文案 */
function wtBoStateLabel(s) {
  return ({ breakout: "放量突破", stand_top: "站上箱顶", near: "临近箱顶", in_box: "箱内震荡" })[s] || "箱内震荡";
}
function wtPct(v) {
  if (v === null || v === undefined || v === "" || isNaN(v)) return '<span class="muted">—</span>';
  const n = Number(v);
  const cls = n > 0 ? "bull" : (n < 0 ? "bear" : "muted");
  return `<span class="${cls}">${n > 0 ? "+" : ""}${n.toFixed(2)}%</span>`;
}
function wtStars(n) {
  n = Math.max(1, Math.min(5, Number(n) || 3));
  return `<span class="wt-star" title="关注度 ${n} / 5">${"★".repeat(n)}<span class="wt-star-off">${"☆".repeat(5 - n)}</span></span>`;
}
function wtFind(id) {
  const its = (WATCH.data && WATCH.data.items) || [];
  return its.find((x) => String(x.id) === String(id)) || null;
}
/* 分组多选 checkbox 列表（2026-10-14 多对多）：每组一个 checkbox + 组名 + 当前只数。
   一只标的可同时在多个分组；无分组时兜底提示（不允许把标的改成无组）。 */
function wtGroupChecks(checkedIds) {
  const gs = (WATCH.data && WATCH.data.groups) || [];
  if (!gs.length) return '<div class="muted wt-nogroup">（暂无分组——请先关掉本窗，到「分组管理」新增一个）</div>';
  const checked = new Set((checkedIds || []).map(String));
  return `<div class="wt-checks">` + gs.map((g) => `
    <label class="wt-check"><input type="checkbox" class="wt-fgroup" value="${g.id}"${checked.has(String(g.id)) ? " checked" : ""}><span class="wt-check-nm">${esc(g.name)}</span><span class="muted wt-check-n">${g.n_items || 0} 只</span></label>`).join("") + `</div>`;
}

/* 防乱序闸（2026-10-14 进哥反馈：弹窗调完分组标签条不动）：/api/watch_list 响应慢
   （现取行情+值得率评分+战法复算，秒级起步），连续触发时旧响应可能后到、把新数据
   盖回去。seq 只认最新一次调用的响应，过期的直接丢弃。 */
let _watchLoadSeq = 0;
function loadWatch(force) {
  const seq = ++_watchLoadSeq;
  const p = api("/api/watch_list" + (force ? "?refresh=1" : ""),
    { label: "自选池", timeout: _API_TIMEOUT_HEAVY })
    .then((d) => { if (seq !== _watchLoadSeq) return; WATCH.data = d || {}; WATCH.signalDate = (d && d.signal_date) || null; renderWatch(); })
    .catch((e) => { if (seq === _watchLoadSeq) reportLoadError("自选池", e); });
  const pa = api("/api/watch_alerts?limit=200", { label: "到价提醒", timeout: _API_TIMEOUT_HEAVY })
    .then((d) => { WATCH.alerts = d || {}; renderWatchAlerts(); })
    .catch((e) => reportLoadError("到价提醒", e));
  startWatchPoll();
  return Promise.all([p, pa]);
}

/* 轻量输入弹窗：Promise<string|null>。比原生 prompt 更稳（部分桌面壳会屏蔽 prompt）。 */
function wtPrompt(title, defVal) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.innerHTML = `
      <div class="modal-card" role="dialog" style="max-width:420px">
        <h3 style="margin:0 0 10px">${esc(title)}</h3>
        <input id="wtPromptInput" style="width:100%" value="${escAttr(defVal || "")}">
        <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">
          <button class="btn" data-pc="0">取消</button>
          <button class="btn primary" data-pc="1">确定</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const inp = ov.querySelector("#wtPromptInput");
    inp.focus(); if (inp.select) inp.select();
    let done = false;
    const finish = (val) => { if (done) return; done = true; ov.remove(); resolve(val); };
    ov.querySelector('[data-pc="0"]').onclick = () => finish(null);
    ov.querySelector('[data-pc="1"]').onclick = () => finish(inp.value.trim());
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(inp.value.trim()); else if (e.key === "Escape") finish(null); });
    ov.addEventListener("click", (e) => { if (e.target === ov) finish(null); });
  });
}
/* 标签行内快捷操作：随时新增分组 / 双击改名（含默认的关注重点/观察中/基金池）。 */
async function watchQuickAddGroup() {
  const nm = await wtPrompt("新增分组", "");
  if (!nm) return;
  const r = await api("/api/watch_group_add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: nm }) })
    .catch((e) => ({ ok: false, error: apiErrText(e) }));
  setHint(r.ok ? `已新增分组「${nm}」。` : ("新增失败：" + (r.error || "")));
  if (r.ok) loadWatch();
}
async function watchRenameGroup(gid, oldName) {
  const nm = await wtPrompt("修改分组名", oldName || "");
  if (!nm || nm === oldName) return;
  const r = await api("/api/watch_group_rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: gid, name: nm }) })
    .catch((e) => ({ ok: false, error: apiErrText(e) }));
  setHint(r.ok ? "分组已改名。" : ("改名失败：" + (r.error || "")));
  if (r.ok) loadWatch();
}

/* 备注编辑弹窗（2026-10-14 进哥需求④）：写/改对该标的的看法。走 /api/watch_item_update
   的 note 字段（后端本就支持）；保存后乐观更新本地 WATCH.data 并立即重渲染，不等慢接口。 */
function openWatchNoteModal(iid) {
  const it = wtFind(iid);
  if (!it) return;
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-label="备注" style="max-width:460px">
      <h3 style="margin:0 0 4px">备注 · ${esc(it.symbol)} ${esc(it.name || "")}</h3>
      <div class="hint" style="margin-bottom:8px">写你对该标的的看法 / 计划 / 风险点，随手保存，随时再改。</div>
      <textarea id="wtNoteTxt" style="width:100%;min-height:110px;resize:vertical;box-sizing:border-box">${esc(it.note || "")}</textarea>
      <div style="margin-top:10px;display:flex;justify-content:flex-end;gap:8px">
        <button class="btn" onclick="this.closest('.modal-overlay').remove()">取消</button>
        <button class="btn primary" id="wtNoteSave">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  setTimeout(() => { const t = overlay.querySelector("#wtNoteTxt"); if (t) t.focus(); }, 50);
  overlay.querySelector("#wtNoteSave").onclick = async () => {
    const btn = overlay.querySelector("#wtNoteSave");
    btn.disabled = true;
    const val = overlay.querySelector("#wtNoteTxt").value.trim();
    const r = await api("/api/watch_item_update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: it.id, note: val }) })
      .catch((e) => ({ ok: false, error: apiErrText(e) }));
    btn.disabled = false;
    if (!r.ok) { setHint("备注保存失败：" + (r.error || "")); return; }
    /* 乐观更新：本地立即生效并重渲染，慢的 watch_list 不用等 */
    if (WATCH.data && Array.isArray(WATCH.data.items)) {
      const loc = WATCH.data.items.find((x) => String(x.id) === String(it.id));
      if (loc) loc.note = val;
    }
    overlay.remove(); setHint("备注已保存。"); renderWatch();
  };
}

function renderWatch() {
  const host = $("#p-watch");
  if (!host) return;
  const d = WATCH.data || {};
  const groups = d.groups || [];
  const all = d.items || [];
  /* 多对多（2026-10-14）：组视图按关联组 groups 匹配（一只票可在多个组同时出现） */
  const items = all.filter((it) => WATCH.selGroup === "all"
    || (it.groups || []).some((g) => String(g.id) === String(WATCH.selGroup)));
  /* 组视图按「当前组的组内 sort_order」排序：reorder 改的是 watch_item_groups.sort_order，
     而服务端默认列表按主组排、前端又只做是否在组内的过滤，故此处补一道组内排序，
     保证 ↓ 按钮 / 行拖拽 调整后顺序即时可见（2026-10-14 修复 C5.3 / C6.2）。 */
  if (WATCH.selGroup !== "all" && WATCH.selGroup !== "") {
    const gk = String(WATCH.selGroup);
    items.sort((a, b) => {
      const ga = (a.groups || []).find((g) => String(g.id) === gk);
      const gb = (b.groups || []).find((g) => String(g.id) === gk);
      return ((ga ? (ga.sort_order != null ? ga.sort_order : 9999) : 9999)
              - (gb ? (gb.sort_order != null ? gb.sort_order : 9999) : 9999));
    });
  }
  const gtabs = groups.map((g) => {
    const active = String(WATCH.selGroup) === String(g.id) ? " active" : "";
    return `<a class="wt-tab${active}" data-wt-group="${g.id}" title="单击筛选">${esc(g.name)} <b>${g.n_items || 0}</b><span class="wt-tab-edit" data-wt-grename="${g.id}" data-wt-gname="${escAttr(g.name)}" title="改名">✎</span></a>`;
  });
  const tabs = [`<a class="wt-tab${WATCH.selGroup === "all" ? " active" : ""}" data-wt-group="all">全部 <b>${all.length}</b></a>`]
    .concat(gtabs).join("")
    + `<a class="wt-tab wt-tab-add" id="wtGroupAddTab" title="新增分组">＋ 新分组</a>`;
  /* 无有效行情的标的（停牌 / 未开盘 / 当日无成交）：后端一律回空，这里只把「为什么是 —」讲清楚。
     历史 bug（2026-10-09 王总带截图纠）：这类票曾在界面显示「0.00 / -100%」= 拿停牌零成交价当行情算出来的假跌幅。 */
  const noQuote = items.filter((it) => it.price === null || it.price === undefined || it.price === "").length;
  const noQuoteHint = noQuote
    ? `<div class="hint wt-noquote"><b>${noQuote} 只标的当前无有效行情</b>（停牌 / 未开盘 / 当日无成交）——当前价与涨跌一律显示 «—»，<b>不以 0 或 -100% 充数</b>；这类标的也不参与到价检测。</div>`
    : "";
  const rows = items.map(watchRow).join("") ||
    `<tr><td colspan="10" class="muted wt-empty">池子为空。点右上「＋ 加入自选」把想盯的股票 / 基金收进来。</td></tr>`;
  host.innerHTML = `
    <div class="wt-head">
      <h2>关注池 <span class="muted wt-sub">${d.checked_at ? "更新于 " + esc(d.checked_at) : ""}</span></h2>
      <div class="wt-actions">
        <button class="btn primary" id="wtAdd">＋ 加入自选</button>
        <button class="btn sm" id="wtGroupMgr">分组管理</button>
        <button class="btn sm" id="wtCheck">检测到价</button>
        <button class="btn sm" id="wtRefresh">刷新</button>
      </div>
    </div>
    <div class="wt-tabs">${tabs}</div>
    ${noQuoteHint}
    <div class="wt-tw"><table class="wt-tb wt-watch"><colgroup>
      <col><!-- 代码/名称 弹性 -->
      <col style="width:86px"><col style="width:86px"><col style="width:84px"><col style="width:82px">
      <col style="width:200px"><!-- 战法信号 锁窄 -->
      <col style="width:200px"><!-- 备注 给足 -->
      <col style="width:74px"><col style="width:90px">
      <col><!-- 操作 弹性 -->
    </colgroup><thead><tr>
      <th>代码 / 名称</th><th title="加入自选那一刻的价格快照">加入时价</th><th>当前价</th><th>涨跌</th>
      <th title="screener 入手值得率评分 0~100：≥70 值博(红) / 40~69 观望(黄) / &lt;40 偏弱(灰)">入手值得率</th>
      <th title="B站一键筛股命中的战法信号${WATCH.signalDate ? "（基于 " + esc(WATCH.signalDate) + " 日K，收盘后自动更新）" : ""}：列内 ▲看涨 / ＝中立 / ▼看跌 为各战法按流派打的方向标签">战法信号</th>
      <th title="你对该标的的看法 / 计划 / 风险点，点单元格即可写或改">备注</th>
      <th>关注天数</th><th>加入至今</th><th>操作</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="hint wt-foot">点代码看个股；「目标价 ↑/↓」= 到价方向（涨到/跌到提醒）。战法列 ▲看涨 / ＝中立 / ▼看跌：按战法流派（趋势/突破/量价/形态/动量＝看涨；均值回归/风控/择时＝中立）标注，本库战法均为做多触发型，看跌正常为 0。命中会在「到价提醒」生成未读记录（同标的同方向同目标价不重复）。</div>`;
  const add = $("#wtAdd"); if (add) add.onclick = () => openWatchAddModal(WATCH.selGroup === "all" ? null : WATCH.selGroup);
  const gm = $("#wtGroupMgr"); if (gm) gm.onclick = openWatchGroupMgrModal;
  const ck = $("#wtCheck"); if (ck) ck.onclick = () => watchCheckNow(ck);
  const rf = $("#wtRefresh"); if (rf) rf.onclick = () => loadWatch(true);
  $$("#p-watch [data-wt-group]").forEach((el) => { el.onclick = () => { WATCH.selGroup = el.dataset.wtGroup; renderWatch(); }; });
  $$("#p-watch [data-wt-grename]").forEach((el) => { el.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); watchRenameGroup(el.dataset.wtGrename, el.dataset.wtGname); }; });
  const wtGroupAddTab = $("#wtGroupAddTab"); if (wtGroupAddTab) wtGroupAddTab.onclick = watchQuickAddGroup;
  $$("#p-watch [data-wt-open]").forEach((el) => { el.onclick = () => openStock(el.dataset.wtOpen, el); });
  $$("#p-watch [data-wt-edit]").forEach((el) => { el.onclick = () => { const it = wtFind(el.dataset.wtEdit); if (it) openWatchEditModal(it); }; });
  $$("#p-watch [data-wt-del]").forEach((el) => { el.onclick = () => watchRemoveItem(el.dataset.wtDel); });
  $$("#p-watch [data-wt-note]").forEach((el) => { el.onclick = () => openWatchNoteModal(el.dataset.wtNote); });
  /* —— 组内排序（2026-10-14）：↑↓ 按钮 + HTML5 行拖拽，统一走 /api/watch_item_reorder
     提交当前组全序 ids（交换 / 拖拽后整体提交，最稳）；「移出本组」走 /api/watch_item_group_remove。
     这些控件仅组视图渲染（watchRow 内 inGroup 判定），全部视图不绑定。 */
  const wtReorder = (ids) => api("/api/watch_item_reorder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: WATCH.selGroup, ids }) })
    .then(() => loadWatch())
    .catch((e) => setHint("排序失败：" + apiErrText(e)));
  $$("#p-watch [data-wt-up]").forEach((el) => { el.onclick = () => {
    const ids = items.map((x) => x.id);
    const i = ids.findIndex((x) => String(x) === String(el.dataset.wtUp));
    if (i > 0) { const t = ids[i - 1]; ids[i - 1] = ids[i]; ids[i] = t; wtReorder(ids); }
  }; });
  $$("#p-watch [data-wt-down]").forEach((el) => { el.onclick = () => {
    const ids = items.map((x) => x.id);
    const i = ids.findIndex((x) => String(x) === String(el.dataset.wtDown));
    if (i >= 0 && i < ids.length - 1) { const t = ids[i + 1]; ids[i + 1] = ids[i]; ids[i] = t; wtReorder(ids); }
  }; });
  $$("#p-watch [data-wt-gdel-item]").forEach((el) => { el.onclick = () => watchGroupRemoveItem(el.dataset.wtGdelItem); });
  let wtDragId = null;
  $$("#p-watch tbody tr[draggable]").forEach((tr) => {
    tr.addEventListener("dragstart", (ev) => {
      wtDragId = tr.dataset.wtDrag;
      tr.classList.add("dragging");
      try { ev.dataTransfer.setData("text/plain", String(wtDragId)); ev.dataTransfer.effectAllowed = "move"; } catch (e) { /* 忽略 */ }
    });
    tr.addEventListener("dragend", () => {
      tr.classList.remove("dragging");
      $$("#p-watch tbody tr.wt-dragover").forEach((x) => x.classList.remove("wt-dragover"));
      wtDragId = null;
    });
    tr.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      try { ev.dataTransfer.dropEffect = "move"; } catch (e) { /* 忽略 */ }
      tr.classList.add("wt-dragover");
    });
    tr.addEventListener("dragleave", () => tr.classList.remove("wt-dragover"));
    tr.addEventListener("drop", (ev) => {
      ev.preventDefault();
      tr.classList.remove("wt-dragover");
      const srcId = wtDragId || (ev.dataTransfer && ev.dataTransfer.getData("text/plain"));
      const dstId = tr.dataset.wtDrag;
      if (!srcId || !dstId || String(srcId) === String(dstId)) return;
      const ids = items.map((x) => x.id);
      const from = ids.findIndex((x) => String(x) === String(srcId));
      const to = ids.findIndex((x) => String(x) === String(dstId));
      if (from < 0 || to < 0) return;
      ids.splice(to, 0, ids.splice(from, 1)[0]);   // 拖到目标行位置
      wtReorder(ids);
    });
  });
}

/* 避坑徽标（2026-10-30）：替代已下线的「博主反向警示」。
   数据来自接口里的 avoid 字段（viewer/avoid.py 实时算：当日量价 + 龙虎榜 + 近 20 日减持公告），
   **不含任何博主数据，也不读沉淀快照** —— 旧的博主警示拿 6 月的推荐绑 11 月的票，
   进哥判定「过期即失效」，故整体换源。
   hover 逐条列出触发项与实证依据，避免只给一个分数让人猜。
   2026-10-30 夜：满分 17→20（新增龙虎榜两条、删掉人事变动、减持降权），
   分值上限改由接口的 max_score 提供，避免以后再改权重还要动前端。 */
let AV_MAX = 20;
function avoidTitle(av) {
  const parts = [];
  for (const f of (av.flags || [])) parts.push(`${f.key} · ${f.ev}`);
  if (av.stale) parts.push("该票最新K线早于当日（停牌/退市），标记仅供参考");
  if (av.trade_date) parts.push(`数据日 ${av.trade_date} · 风险分 ${av.score}/${AV_MAX}`);
  return parts.join("\n");
}
function avoidBadge(av) {
  if (!av || !av.level) return '<span class="muted">—</span>';
  const high = av.level === "high";
  return `<span class="av-badge ${high ? "av-high" : "av-mid"}" title="${escAttr(avoidTitle(av))}">⚠${av.score}</span>`;
}

/* 解禁徽标（2026-10-30，进哥需求）：股东临近释放期就会卖 → 提前提示。
   数据来自接口里的 lift 字段（viewer/lift.py：东财解禁批次 × 当日交易日历现算）。

   为什么三档而不是「有解禁就报警」——实证（1588 个解禁事件，2025-08~2026-10）：
     解禁前 60~41 日  +0.44%（还在超额上涨）
     解禁前 40~21 日  −0.94%（t=-1.16，不显著）
     解禁前 20~11 日  −1.88%（t=-3.36）
     解禁前 10~1 日   −1.39%（t=-2.81）
     解禁后 1~5 日    +0.31%（靴子落地反弹）
   所以「提前三个月就亮红灯」是错的，会让人在上涨段踏空。watch 档只用灰字标出日期，
   让持仓的人心里有数，但不假装它是风险。 */
function liftTitle(li) {
  const parts = [];
  const kind = li.type ? `（${li.type}）` : "";
  parts.push(`${li.td} 个交易日后 ${li.free_date} 解禁${kind}`);
  if (li.free_ratio != null) parts.push(`解禁量占流通 ${li.free_ratio}%`);
  if (li.lift_cap != null) parts.push(`解禁金额约 ${li.lift_cap} 亿`);
  if (li.n_holder) parts.push(`涉及股东 ${li.n_holder} 家`);
  if (li.msg) parts.push(li.msg);
  if (li.level === "high") {
    parts.push("实证：首发原股东解禁且占流通≥3%的票，解禁前 20 个交易日跑输大盘 3.36%（t=-5.09）");
  } else if (li.level === "mid") {
    parts.push("实证：解禁占流通≥10% 的票，解禁前 20 个交易日跑输大盘 2.19%（t=-4.01）");
  } else {
    parts.push("提示：前 40 个交易日仍为超额上涨，尚未进入实证有效的风险窗口");
  }
  return parts.join("\n");
}
function liftBadge(li, opts) {
  const showWatch = opts && opts.watch;
  if (!li || !li.level) return '<span class="muted">—</span>';
  if (li.level === "watch") {
    if (!showWatch) return '<span class="muted">—</span>';
    // watch 档刻意不带 🔓 —— emoji 是彩色的，CSS 改不动它的颜色，
    // 带着同一个金色锁会让「仅供参考」和「要躲」看起来一样重。纯文字才排得下层级。
    return `<span class="lift-watch" title="${escAttr(liftTitle(li))}">${li.td}日后</span>`;
  }
  const high = li.level === "high";
  return `<span class="lift-badge ${high ? "lift-high" : "lift-mid"}" title="${escAttr(liftTitle(li))}">🔓${li.td}日</span>`;
}

function watchRow(it, idx, arr) {
  // 第一格（代码/名称）附加标记：基金标记+ 多分组 ×N + 到价提醒标记（🔔）。
  let c1Extra = "";
  if (it.kind === "fund") c1Extra += ' <span class="wt-kind">基</span>';
  if (it.n_groups && it.n_groups > 1) {
    const gnames = (it.groups || []).map((g) => g.name).join("、");
    c1Extra += ` <span class="wt-multi" title="同时在 ${it.n_groups} 个分组：${escAttr(gnames)}">×${it.n_groups}</span>`;
  }
  if (it.alert_on && it.target_price !== null && it.target_price !== undefined && it.target_price !== "") {
    c1Extra += ` <span class="wt-bell" title="已设到价提醒">🔔</span>`;
  }
  // 避坑徽标：只在高/中档亮灯（低档不显示，否则半张表都在报警，等于没报）。
  if (it.avoid && it.avoid.level) c1Extra += " " + avoidBadge(it.avoid);
  // 解禁徽标：关注池是持仓/准备买的地方，watch 档（60 日内）也显示，让人提前安排。
  if (it.lift && it.lift.level) c1Extra += " " + liftBadge(it.lift, { watch: true });
  // 入手值得率：数字按阈值配色（bull 红 / wt-warn 黄 / muted 灰），否则 «—»。
  const w = it.worth;
  const worthCell = (typeof w === "number" && !isNaN(w))
    ? `<span class="${w >= 70 ? "bull" : (w >= 40 ? "wt-warn" : "muted")}">${w}</span>`
    : '<span class="muted">—</span>';
  // 战法信号（2026-10-14 进哥需求：要"说清楚"——几根看涨/看跌/中立）。
  // 每行命中带 stance（bull=看涨 / neu=中立 / bear=看跌，由后端按流派打标）。
  // 统计后给三向 tally，并把每个战法 chip 按方向着色；无命中则 «—»。
  const hits = it.strat_hits;
  let stratCell;
  if (Array.isArray(hits) && hits.length) {
    let nb = 0, nn = 0, nr = 0;
    for (const h of hits) {
      const st = (h && h.stance) || "neu";
      if (st === "bull") nb++;
      else if (st === "bear") nr++;
      else nn++;
    }
    const verdict = `<span class="wt-st-verdict" title="战法方向统计：▲看涨 ${nb} / ＝中立 ${nn} / ▼看跌 ${nr}。本库战法均为做多触发型，看跌正常为 0，绝不臆造。">`
      + `<b class="bull">▲${nb}</b><span class="wt-st-sep">/</span>`
      + `<b class="wt-st-neu">＝${nn}</b><span class="wt-st-sep">/</span>`
      + `<b class="bear">▼${nr}</b></span>`;
    const chips = hits.map((h) => {
      const sname = (h && typeof h === "object") ? (h.sname || "") : h;
      const wr = (h && typeof h === "object") ? h.win_rate : null;
      const st = (h && typeof h === "object") ? (h.stance || "neu") : "neu";
      const dirTxt = st === "bull" ? "看涨" : st === "bear" ? "看跌" : "中立";
      const title = (wr !== null && wr !== undefined && wr !== "")
        ? `战法信号 · 胜率 ${wr}% · ${dirTxt}` : `战法信号 · ${dirTxt}`;
      const cls = "wt-chip" + (st === "bull" ? " st-bull" : st === "bear" ? " st-bear" : " st-neu");
      return `<span class="${cls}" title="${escAttr(title)}">${esc(sname)}</span>`;
    }).join(" ");
    stratCell = verdict + `<div class="wt-st-chips">${chips}</div>`;
  } else {
    stratCell = '<span class="muted">—</span>';
  }
  /* 备注列（2026-10-14 进哥需求：战法信号后面加一栏写自己对该标的的看法）。
     有内容显示原文（悬停看全文，超长省略号），空则给「＋ 备注」弱提示；点单元格弹编辑框。 */
  const noteTxt = String(it.note || "").trim();
  const noteCell = noteTxt
    ? `<span class="wt-note-t" title="${escAttr(noteTxt)}（点此编辑备注）">${esc(noteTxt)}</span>`
    : `<span class="wt-note-empty" title="点此写备注">＋ 备注</span>`;
  const days = it.days_watched;
  /* 组内排序 / 移出本组（2026-10-14）：仅组视图显示；全部视图不显示这两类控件。 */
  const inGroup = WATCH.selGroup !== "all" && String(WATCH.selGroup) !== "";
  const pos = typeof idx === "number" ? idx : -1;
  const total = arr ? arr.length : 0;
  let ops = `<span class="wt-op" data-wt-edit="${it.id}">编辑</span><span class="wt-op danger" data-wt-del="${it.id}">删</span>`;
  if (inGroup) {
    const first = pos <= 0, last = pos < 0 || pos >= total - 1;
    const up = first
      ? '<span class="wt-op wt-op-dis" title="已在组顶">↑</span>'
      : `<span class="wt-op" data-wt-up="${it.id}" title="上移">↑</span>`;
    const down = last
      ? '<span class="wt-op wt-op-dis" title="已在组底">↓</span>'
      : `<span class="wt-op" data-wt-down="${it.id}" title="下移">↓</span>`;
    ops = up + down
      + `<span class="wt-op" data-wt-gdel-item="${it.id}" title="仅移出本组，池中保留">移出本组</span>` + ops;
  }
  const dragAttr = inGroup ? ` draggable="true" data-wt-drag="${it.id}"` : "";
  return `<tr${dragAttr}>
    <td class="wt-c1"><button type="button" class="wt-sym" data-wt-open="${escAttr(it.symbol)}">${esc(it.symbol)}</button> <span class="wt-nm">${esc(it.name || "")}</span>${c1Extra}</td>
    <td>${wtMoney(it.price_at_add)}</td>
    <td>${wtMoney(it.price)}</td>
    <td>${wtPct(it.pct)}</td>
    <td>${worthCell}</td>
    <td class="wt-strat">${stratCell}</td>
    <td class="wt-note" data-wt-note="${it.id}">${noteCell}</td>
    <td>${(days === null || days === undefined) ? '<span class="muted">—</span>' : days + " 天"}</td>
    <td>${wtPct(it.since_add_pct)}</td>
    <td class="wt-ops">${ops}</td>
  </tr>`;
}

function renderWatchAlerts() {
  const host = $("#p-watch_alerts");
  if (!host) return;
  const d = WATCH.alerts || {};
  const list = d.alerts || [];
  const rows = list.map((a) => {
    const rule = wtAlertRuleDir(a.rule);
    return `<tr class="${a.read ? "" : "wt-unread"}">
      <td>${esc(a.triggered_at || "")}</td>
      <td class="wt-c1"><button type="button" class="wt-sym" data-wt-alert-open="${escAttr(a.symbol)}">${esc(a.symbol)}</button> <span class="wt-nm">${esc(a.name || "")}</span></td>
      <td>${rule}</td>
      <td>${wtMoney(a.target_price)}</td>
      <td>${wtMoney(a.triggered_price)}</td>
      <td>${a.read ? '<span class="muted">已读</span>' : '<span class="wt-dot"></span>未读'}</td>
      <td class="wt-ops">${a.read ? "" : `<span class="wt-op" data-wt-read="${a.id}">标记已读</span>`}</td>
    </tr>`;
  }).join("") ||
    `<tr><td colspan="7" class="muted wt-empty">暂无到价提醒。在关注池给标的设好「目标价 + 方向」后，点「立即检测」即可。</td></tr>`;
  host.innerHTML = `
    <div class="wt-head">
      <h2>到价提醒 <span class="wt-badge ${(d.unread || 0) > 0 ? "on" : ""}">${d.unread || 0} 未读</span></h2>
      <div class="wt-actions">
        <button class="btn primary" id="wtCheck2">立即检测</button>
        <button class="btn sm" id="wtReadAll">全部标记已读</button>
        <button class="btn sm" id="wtRefresh2">刷新</button>
      </div>
    </div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>触发时间</th><th>标的</th><th>规则</th><th>目标价</th><th>触发价</th><th>状态</th><th>操作</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="hint wt-foot">提醒为应用内（打开查看器即自动轮询 + 可手动检测）；浏览器通知在权限允许时弹出。微信 / 邮件推送为后续可加项。</div>`;
  const c = $("#wtCheck2"); if (c) c.onclick = () => watchCheckNow(c);
  const ra = $("#wtReadAll"); if (ra) ra.onclick = () => watchMarkAllRead();
  const rf = $("#wtRefresh2"); if (rf) rf.onclick = () => loadWatch();
  $$("#p-watch_alerts [data-wt-read]").forEach((el) => { el.onclick = () => watchMarkRead(el.dataset.wtRead); });
  $$("#p-watch_alerts [data-wt-alert-open]").forEach((el) => { el.onclick = () => openStock(el.dataset.wtAlertOpen, el); });
}

function watchCheckNow(btn) {
  const orig = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "检测中…"; }
  api("/api/watch_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((r) => {
      setHint(`到价检测完成：检测 ${r.checked || 0} 只 · 命中 ${r.triggered_n || 0} 条 · 未读 ${r.unread || 0}。`);
      if ((r.triggered_n || 0) > 0) watchNotify(r.triggered || []);
      return loadWatch();
    })
    .catch((e) => setHint("到价检测失败：" + apiErrText(e)))
    .finally(() => { if (btn) { btn.disabled = false; btn.textContent = orig; } });
}

function watchNotify(list) {
  // 浏览器通知为 best-effort：不支持 / 被拒一律静默跳过，不影响主流程。
  try {
    if (typeof Notification === "undefined") return;
    const fire = () => list.slice(0, 5).forEach((a) => {
      const dir = wtAlertRuleText(a.rule);
      new Notification(`到价提醒：${a.name || a.symbol}`, {
        body: `${a.symbol} ${dir} 目标 ${a.target_price}（现价 ${a.triggered_price}）`,
      });
    });
    if (Notification.permission === "granted") fire();
    else if (Notification.permission === "default") Notification.requestPermission().then((p) => { if (p === "granted") fire(); });
  } catch (e) { /* 忽略：通知非关键路径 */ }
}

function startWatchPoll() {
  if (WATCH.poll) return;
  // 前台轮询：每 90s 检测一次（仅页面可见时）；命中则刷新提醒 + 浏览器通知。
  WATCH.poll = setInterval(() => {
    if (document.hidden) return;
    api("/api/watch_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then((r) => { if ((r.triggered_n || 0) > 0) { watchNotify(r.triggered || []); loadWatch(); } })
      .catch(() => { /* 静默：轮询失败不打扰 */ });
  }, 90000);
}

function watchMarkRead(id) {
  api("/api/watch_alert_read", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) })
    .then(() => loadWatch()).catch((e) => setHint("操作失败：" + apiErrText(e)));
}
function watchMarkAllRead() {
  api("/api/watch_alert_read", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }) })
    .then((r) => { setHint(`已标记 ${r.marked || 0} 条为已读。`); return loadWatch(); })
    .catch((e) => setHint("操作失败：" + apiErrText(e)));
}
function watchRemoveItem(id) {
  const it = wtFind(id);
  if (!confirm(`从池中移除 ${it ? (it.name || it.symbol) : "该标的"}？`)) return;
  api("/api/watch_item_remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) })
    .then(() => { setHint("已从池中移除。"); return loadWatch(); })
    .catch((e) => setHint("移除失败：" + apiErrText(e)));
}
/* 移出当前分组（2026-10-14 多对多）：仅移出本组、池中保留；confirm 明示，避免误删全池。 */
function watchGroupRemoveItem(id) {
  const it = wtFind(id);
  if (!it) return;
  if (!confirm(`把「${it.name || it.symbol}」移出当前分组？\n仅移出本组，池中保留。`)) return;
  api("/api/watch_item_group_remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: it.id, group_id: WATCH.selGroup }) })
    .then((r) => { setHint(r.ok ? "已移出本组（池中保留）。" : ("操作失败：" + (r.error || ""))); return loadWatch(); })
    .catch((e) => setHint("操作失败：" + apiErrText(e)));
}

function watchFormHtml(row) {
  const it = row || {};
  const dirSel = (v) => (it.alert_dir === v ? " selected" : "");
  /* 分组多选（2026-10-14 多对多）：编辑态预勾 row.groups 的 id；新增态由调用方 preset 预勾。 */
  const grpIds = row ? (row.groups || []).map((g) => g.id) : [];
  return `<div class="wt-form">
    <label>代码 *<input id="wtF-symbol" value="${escAttr(it.symbol || "")}" placeholder="600519 或 sh600519" ${row ? "disabled" : ""}></label>
    <label>名称<input id="wtF-name" value="${escAttr(it.name || "")}" placeholder="留空自动补全"></label>
    <label>类型<select id="wtF-kind"><option value="stock"${it.kind !== "fund" ? " selected" : ""}>股票</option><option value="fund"${it.kind === "fund" ? " selected" : ""}>基金 / ETF</option></select></label>
    <div class="wt-full"><div class="wt-f-lb">分组（可多选，可同时在多个组）</div>${wtGroupChecks(grpIds)}</div>
    <label>目标价<input id="wtF-target" value="${it.target_price === null || it.target_price === undefined ? "" : escAttr(it.target_price)}" placeholder="如 1500（可空）"></label>
    <label>方向<select id="wtF-dir"><option value=""${dirSel("")}>（不提醒）</option><option value="above"${dirSel("above")}>上破 ↑（涨到目标提醒）</option><option value="below"${dirSel("below")}>下穿 ↓（跌到目标提醒）</option></select></label>
  </div>`;
}
function watchCollectForm(overlay) {
  const q = (s) => overlay.querySelector(s);
  /* 分组多选（2026-10-14）：返回全部勾选的 gid；group_id 保留首个（兼容旧读法）。 */
  const groups = Array.from(overlay.querySelectorAll(".wt-fgroup:checked")).map((el) => Number(el.value));
  return {
    name: q("#wtF-name").value.trim(),
    kind: q("#wtF-kind").value,
    groups,
    group_id: groups.length ? groups[0] : null,
    target_price: q("#wtF-target").value.trim() || null,
    alert_dir: q("#wtF-dir").value,
  };
}

function openWatchAddModal(presetGroup, preset) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-label="加入自选" style="max-width:560px">
      <h3 style="margin:0 0 6px">＋ 加入自选池</h3>
      <div class="hint" style="margin-bottom:8px">填代码即可（名称与加入价自动补全）。目标价 + 方向用于「到价提醒」，其余可留空。</div>
      ${watchFormHtml(null)}
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" onclick="this.closest('.modal-overlay').remove()">取消</button>
        <button class="btn primary" id="wtF-save">加入</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  /* 预设分组预勾（2026-10-14 多选 checkbox）：从组视图「＋ 加入自选」/ 个股抽屉点进来时带上 */
  if (presetGroup) {
    const box = Array.from(overlay.querySelectorAll(".wt-fgroup"))
      .find((el) => String(el.value) === String(presetGroup));
    if (box) box.checked = true;
  }
  // 2026-09-27：从个股详情抽屉的「＋加入自选池」点进来时，预填代码与名称，省去重复输入
  if (preset && preset.symbol) {
    const symInp = overlay.querySelector("#wtF-symbol"); if (symInp) symInp.value = preset.symbol;
    const nameInp = overlay.querySelector("#wtF-name"); if (nameInp && preset.name) nameInp.value = preset.name;
  }
  overlay.querySelector("#wtF-symbol").focus();
  const btn = overlay.querySelector("#wtF-save");
  btn.onclick = async () => {
    const body = watchCollectForm(overlay);
    body.symbol = overlay.querySelector("#wtF-symbol").value.trim();
    if (!body.symbol) { setHint("请填代码"); return; }
    btn.disabled = true; btn.textContent = "加入中…";
    try {
      const r = await api("/api/watch_item_add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) { setHint("加入失败：" + (r.error || "未知错误")); return; }
      overlay.remove();
      /* 同票已在池：不再报错，回显 watchlib 的 hint（「已在池中，已追加到分组X」） */
      setHint(r.existed && r.hint ? r.hint : "已加入自选池。");
      loadWatch();
    } catch (e) { setHint("加入异常：" + apiErrText(e)); }
    finally { btn.disabled = false; btn.textContent = "加入"; }
  };
}

function openWatchEditModal(item) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-label="编辑自选" style="max-width:560px">
      <h3 style="margin:0 0 6px">编辑：${esc(item.symbol)} ${esc(item.name || "")}</h3>
      <div class="hint" style="margin-bottom:8px">改目标价 / 方向后请点「保存」，并到「到价提醒」页点一次「立即检测」即生效。</div>
      ${watchFormHtml(item)}
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" onclick="this.closest('.modal-overlay').remove()">取消</button>
        <button class="btn primary" id="wtE-save">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const btn = overlay.querySelector("#wtE-save");
  btn.onclick = async () => {
    const body = watchCollectForm(overlay);
    body.id = item.id;
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      const r = await api("/api/watch_item_update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) { setHint("保存失败：" + (r.error || "未知错误")); return; }
      overlay.remove(); setHint("已保存。"); loadWatch();
    } catch (e) { setHint("保存异常：" + apiErrText(e)); }
    finally { btn.disabled = false; btn.textContent = "保存"; }
  };
}

function openWatchGroupMgrModal() {
  /* 2026-10-14 进哥反馈①：分组前后顺序调不了 —— 组表本就有 sort_order 字段，只缺入口。
     每行加 ↑/↓：点击即本地换位并 POST /api/watch_group_reorder 落库；
     标签条 / 弹窗多选列表的顺序都由 list_groups 的 sort_order 决定，保存后 loadWatch 全局生效。 */
  const gs = ((WATCH.data && WATCH.data.groups) || []).map((g) => ({ ...g }));
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-label="分组管理" style="max-width:520px">
      <h3 style="margin:0 0 6px">分组管理</h3>
      <div class="hint" style="margin-bottom:8px">组名可任意修改（改完点「改名」）；点 <b>↑ / ↓</b> 调整分组在标签条上的前后顺序（点完即存）。删除分组时，组内标的会**移到其它分组**，标的本身不删。</div>
      <div class="wt-glist"></div>
      <div class="wt-gadd">
        <input id="wtG-new" placeholder="新分组名，如：AI 主线 / 长线 / 待观察">
        <button class="btn primary" id="wtG-add">新增分组</button>
      </div>
      <div style="margin-top:12px;display:flex;justify-content:flex-end">
        <button class="btn" onclick="this.closest('.modal-overlay').remove()">关闭</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const listEl = overlay.querySelector(".wt-glist");
  const persistOrder = async () => {
    /* 乐观更新（2026-10-14 进哥反馈②：弹窗里调完顺序，标签条不动）——根因是落库后
       靠慢接口 /api/watch_list 回来才重渲染（现取行情+评分+战法，秒级起步），期间
       标签条纹丝不动、旧响应还可能盖回旧顺序。现改为：先把本地顺序立刻写回
       WATCH.data.groups 并重渲染标签条（所见即所得），落库成功后再 loadWatch 兜底
       校验；配合 _watchLoadSeq 防旧响应盖新。 */
    if (WATCH.data) WATCH.data.groups = gs.map((g) => ({ ...g }));
    renderWatch();
    const r = await api("/api/watch_group_reorder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: gs.map((g) => g.id) }) })
      .catch((e) => ({ ok: false, error: apiErrText(e) }));
    setHint(r.ok ? "分组顺序已保存。" : ("顺序保存失败：" + (r.error || "")));
    if (r.ok) loadWatch();
  };
  const moveGroup = (gid, dir) => {
    const i = gs.findIndex((g) => String(g.id) === String(gid));
    const j = i + dir;
    if (i < 0 || j < 0 || j >= gs.length) return;
    const [g] = gs.splice(i, 1);
    gs.splice(j, 0, g);
    renderRows();
    persistOrder();
  };
  const renderRows = () => {
    listEl.innerHTML = gs.map((g, i) => `
    <div class="wt-grow">
      <span class="wt-gmv${i === 0 ? " wt-op-dis" : ""}" data-gup="${g.id}" title="上移（排到前面）">↑</span>
      <span class="wt-gmv${i === gs.length - 1 ? " wt-op-dis" : ""}" data-gdn="${g.id}" title="下移（排到后面）">↓</span>
      <input class="wt-gname" data-gid="${g.id}" value="${escAttr(g.name)}">
      <span class="muted wt-gn">${g.n_items || 0} 只</span>
      <button class="btn sm" data-gsave="${g.id}">改名</button>
      <button class="btn sm" data-gdel="${g.id}">删除</button>
    </div>`).join("") || '<div class="muted">暂无分组</div>';
    overlay.querySelectorAll("[data-gup]").forEach((el) => { el.onclick = () => moveGroup(el.dataset.gup, -1); });
    overlay.querySelectorAll("[data-gdn]").forEach((el) => { el.onclick = () => moveGroup(el.dataset.gdn, +1); });
    overlay.querySelectorAll("[data-gsave]").forEach((b) => {
      b.onclick = async () => {
        const gid = b.dataset.gsave;
        const inp = overlay.querySelector(`.wt-gname[data-gid="${gid}"]`);
        const r = await api("/api/watch_group_rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: gid, name: inp.value.trim() }) })
          .catch((e) => ({ ok: false, error: apiErrText(e) }));
        setHint(r.ok ? "分组已改名。" : ("改名失败：" + (r.error || "")));
        if (r.ok) { overlay.remove(); loadWatch(); }
      };
    });
    overlay.querySelectorAll("[data-gdel]").forEach((b) => {
      b.onclick = async () => {
        const gid = b.dataset.gdel;
        if (!confirm("删除该分组？组内标的会移到其它分组（标的不会删除）。")) return;
        const r = await api("/api/watch_group_remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: gid }) })
          .catch((e) => ({ ok: false, error: apiErrText(e) }));
        setHint(r.ok ? `已删除分组，${r.moved || 0} 只标的已改挂其它分组。` : ("删除失败：" + (r.error || "")));
        if (r.ok) { overlay.remove(); loadWatch(); }
      };
    });
  };
  renderRows();
  overlay.querySelector("#wtG-add").onclick = async () => {
    const nm = overlay.querySelector("#wtG-new").value.trim();
    if (!nm) { setHint("请填分组名"); return; }
    const r = await api("/api/watch_group_add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: nm }) })
      .catch((e) => ({ ok: false, error: apiErrText(e) }));
    setHint(r.ok ? "已新增分组。" : ("新增失败：" + (r.error || "")));
    if (r.ok) { overlay.remove(); loadWatch(); }
  };
}

/* ================= 二级导航（一级竖向 + 二级横向） =================
   分类依据（2026-09-29 信息架构重构）：「这条信息是谁的」——
   市场的钱(钱往哪走) / 事(消息与外围) / 别人怎么做 / 别人怎么看 / 我自己的(我的票) / 出入口(今日总览·复盘成长)。
   旧版按「数据链路四段式」（获取→研判→执行→沉淀）划分，但该维度与"功能类型""内容主题"混用，
   导致归类不可预测（同一层混三套标准）；现收敛为单一标准，并守两条硬约束：一级类 ≤7、每类子项 ≤7。

   本配置是**唯一事实来源**：左侧大类与顶部子类都由它生成；面板容器沿用原命名
   `#p-<tab>`，因此原有 12 个 renderX 函数一行都不用改（渲染层零改动）。
   新增一块内容只需三步：
     ① 在此加一项；② index.html 里加一个空的 <div class="panel" id="p-<tab>">；
     ③ 在 loadAll() 里加一次 fetch + render（或在其首次进入时懒加载）。
   URL 采用 `#大类/子类` 深链接，刷新与浏览器前进/后退都能保持位置。 */
// 2026-10-31：HARVEST 状态机提到文件顶部 —— 深链 #overview/harvest 首次进入时
// selectNav 会在 app.js 顶层语句执行途中就调用 loadHarvest，声明放后面会直接 TDZ
// （实测报 "Cannot access 'HARVEST' before initialization"）。
const HARVEST = { data: null, busy: false };
// 2026-10-30：开源情报（GitHub 规则重测）。同样必须声明在 NAV 之前 ——
// 深链 #overview/oss 会走同一条 TDZ 路径。
const OSS = { data: null, busy: false };
// 2026-10-30：选股方向（短线证伪 + 低波池）。同样必须在 NAV 之前声明。
const SELECT = { data: null, busy: false, expandPool: false };

const NAV = [
  // 2026-10-17 架构重排（墨衡方案 v1.0）：统一命名 / 分组 / 跨类迁移；所有 tab 标识保持不变，深链接不变。
  { key: "overview", label: "今日总览", icon: "◎", items: [
    { group: "当日结论" },
    // 今日决策 为主项，今日看板/汇总/我的提示 归拢其下（视觉聚合，tab 仍平铺，深链接不变）
    { tab: "today", label: "今日决策", lead: true, mg: "decision" },
    // 2026-10-29 进哥需求：AI 复盘 —— 紧挨今日决策放，一眼能对比
    // 「规则引擎公式算的」vs「大模型读完信息包+我的知识库之后判断的」。
    { tab: "ai_review", label: "AI 复盘", child: true, mg: "decision" },
    { tab: "dashboard", label: "今日看板", child: true, mg: "decision" },
    { tab: "summary", label: "今日汇总", child: true, mg: "decision" },
    { tab: "watch_brief", label: "我的提示", child: true, mg: "decision" },
    { group: "当日信号" },
    // 2026-10-31 进哥需求：先看「局」，再选票。不预测点位，只识别阶段与必亏动作。
    { tab: "harvest", label: "大势研判" },
    // 2026-10-30 进哥需求：短线（今天买明天卖）与中长期（底部持有）两个方向。
    // 实测结果与直觉相反，面板如实呈现正反对照，不给安慰性结论。
    { tab: "selection", label: "选股方向" },
    // 2026-10-30 进哥需求：GitHub 上的量化规则在我们的数据上重测。
    // 定位是「方法论校验」，不是又一个荐股源 —— 只回答"这个说法成立吗"。
    { tab: "oss", label: "开源情报" },
    { tab: "prediction", label: "每日预测" },
    { tab: "resonance", label: "多源共振" },
    // 2026-10-21 dy9 进哥拍板：低分卡归因做成一级功能（只做排除，不做推荐）
    { tab: "avoid", label: "避雷名单" },
    { tab: "quant", label: "量化信号" },
    { tab: "etf", label: "ETF基金" },
    { group: "外部环境" },
    // 全球大盘/海外舆情/国际局势 从「消息与外围」迁入（它们是盘面/外围，不是资讯）
    { tab: "global_market", label: "全球大盘", lead: true, mg: "overseas" },
    { tab: "overseas_sentiment", label: "海外舆情", child: true, mg: "overseas" },
    { tab: "geo_events", label: "国际局势", child: true, mg: "overseas" }] },
  // ② 资金动向（原「钱往哪走」）：资金 + 人气，改名使命名语域统一（名词短语、首字可定位）
  { key: "money", label: "资金动向", icon: "¥", items: [
    { tab: "lhb", label: "游资龙虎榜" },
    { tab: "lhb_sectors", label: "板块动向" },
    { tab: "moneyflow", label: "资金流向" },
    { tab: "industry-leaders", label: "龙头全景" },
    { group: "人气" },
    // ⚠️ 2026-10-31：本组原有「讨论热度」(stocks) 与「板块热度」(plate_heat) 两项，已随论坛停抓撤下。
    //   两者 100% 依赖 posts / stock_mentions（板块热度另一路豆包语料停在 2026-10-11），
    //   源停之后它们只会把旧帖反复重算成"今天的注意力"，属于用死数据生成活结论。
    //   渲染函数与面板容器保留（不删代码，便于源恢复时一处加回）。
    { tab: "bili_sentiment", label: "市场情绪", lead: true, mg: "mood" },
    { tab: "sentiment", label: "舆论情绪", child: true, mg: "mood" }] },
  // ③ 消息政策（原「消息与外围」）：只装资讯与政策，边界清晰。外围/情绪已迁出到今日总览/资金动向。
  { key: "info", label: "消息政策", icon: "◈", items: [
    { tab: "info_brief", label: "消息面汇总" },
    // ⚠️ 2026-10-31：「近期热点」(topics) 已撤下 —— hotspots 算法是「拿词库关键词扫 posts 原文」，
    //   论坛停抓后扫不到新帖，只剩 10-29 之前的历史热度冒充"近期"。容器与 renderTopics 保留。
    { tab: "news", label: "个股快讯公告" },
    { tab: "policy", label: "政策风向" }] },
  // ④ 他人操作（原「别人怎么做」）—— 2026-10-31 整类**下线并撤出导航**。
  //   本类 7 个面板（牛散追踪/选手与渠道/相关人物/正在操作股票/资金共识/共同持仓/近5日买入）
  //   全部读论坛抓取产物：trades（选手实时成交）、positions（**2021-06-20 的集思录晒盘快照**）、
  //   persons/person_scores/public_records、posts ⋈ stock_mentions（共识/共同持仓）。
  //   论坛 2026-10-30 全停后它们永久停更；其中 positions 尤其不能留 —— 五年前别人晒的仓位
  //   被当成"选手正在操作"展示，比空白更有害。数据与表已同步清空（见 DECISIONS.md D16）。
  //   代码不删（后端 /api/players 等路由与前端 renderX 全部保留），源恢复时把本段加回即可。
  // ⑤ 他人观点（原「别人怎么看」）：观点——机构/社区/研报怎么评价。改名为与「他人操作」对仗。
  { key: "opinion", label: "他人观点", icon: "◇", items: [
    { tab: "research", label: "研报评级" },
    // 2026-10-29 进哥拍板：博主荐股 / 外部荐股验证 / 当红参赛股 三页已**物理删除**
    // （不只是摘导航，代码也删了）。荐股口径降级为「资金动向 · 板块热度」，只谈板块不谈票。
    // 本类下现仅存「研报评级」——机构研报是另一种信源，不属博主荐股，故保留。
  ] },
  // 2026-09-27 新增一级分类「自选池」：自己的关注/自选池（股票 + 基金），可分组（**组名可自定义**）、
  // 标目标价、到价提醒。数据来自独立模块 watchlib.py（表 watch_*，零 JOIN、零外部关联）。
  // ⑥ 我的票：我自己的——自选、提醒、选股、诊股（原「自选池」+ 「增强功能」的选股/诊股/多资产）
  // ⑥ 我的票：自己有的票（持仓管理）+ 还没票时找票（选股发现）。今日提示已迁到「今日总览」。
  { key: "watch", label: "我的票", icon: "★", items: [
    { group: "持仓管理" },
    { tab: "watch", label: "关注池" },
    { tab: "watch_alerts", label: "到价提醒" },
    { tab: "diag", label: "个股诊断" },
    { tab: "multiquote", label: "多资产" },
    { group: "选股发现" },
    { tab: "screener", label: "智能选股" },
    // 长期低位/突破雷达 是同一套形态筛选的两端，归拢到「低位突破」主项
    { tab: "lowbox", label: "低位突破", lead: true, mg: "lowbox" },
    { tab: "lowbox_bottom", label: "长期底部埋伏", child: true, mg: "lowbox" },
    { tab: "lowbox_radar", label: "突破雷达", child: true, mg: "lowbox" },
    { tab: "rate", label: "入手概率" },
    { group: "产业链研究" },
    { tab: "bottleneck", label: "瓶颈猎手", lead: true, mg: "chain" }] },
  // 2026-09-29：「增强功能」类已**解散**，六项按维度归位——资金流向→钱往哪走、资讯公告→消息与外围、
  // 研报评级→别人怎么看、智能选股/估值诊股/多资产→我的票。后端独立模块（emapi + moneyflow/screener/
  // valuation/research/multiquote/newsfeed）与面板容器原样保留，前端懒加载逻辑（selectNav 末尾）零改动。
  // 2026-09-26：原「知识获取」(acquire) 与「跟我学习」(learn) 两个一级分类**合并为一个**
  // （显示名 2026-09-29 由「学习中心」易名为「复盘成长」；内部 key 仍沿用 learn，深链接 learn_* 前缀不变）。
  // 原「知识获取」下的 数据源 / 原始存档 / 抓取与登录记录 三项已**从导航撤下**：
  // 它们是抓取管道的运维信息，看盘时无用；数据照常采集落库（refresh_status / raw_pages 不变），
  // 面板与渲染函数（#p-sources / #p-archive / #p-fetchlog 及 renderXPanel）一并保留，
  // 需要复原时把这三项加回下面 items 即可，后端无需改动。
  // 2026-10-12 体验审计 P1（部分）：复盘/学习是**盘后**动作，盘中用不到却占一级导航 →
  // 打上 pinned 标记，renderNav 会把它沉到左栏最底部、并用「盘后」分隔线与盘中决策项隔开。
  // 2026-10-27 P4 收尾：信号 alpha 回测实证可视化。独立一级「信号实证」最干净——
  // 非盘中决策、也非知识库动态轨道（DYN_CATS），items 原样生效，不污染首屏与其他大类。
  { key: "research", label: "信号实证", icon: "⚙", items: [
    { tab: "backtest", label: "信号回测" },
  ] },
  { key: "learn", label: "复盘成长", icon: "✎", pinned: true, items: [
    // ⚠️ 本类子项由两个来源动态生成（见 itemsOfCat 的 learn 分支），此处 items 仅为占位。
    { tab: "strategy_lab", label: "策略回测" },
    { tab: "learn_tracks", label: "轨道管理" },
  ] },
];
// 2026-10-17 架构重排：取「首个有效 tab」（跳过 group 占位与 child 子项）。
// 分组标题（group）和视觉归拢子项（child）不能当首项/默认落地点。
function firstRealTab(items) {
  const it = (items || []).find((i) => i.tab && !i.child);
  return it ? it.tab : (items && items[0] ? items[0].tab : undefined);
}
let CURRENT = { cat: NAV[0].key, tab: firstRealTab(NAV[0].items) };

function catOf(tab) { return NAV.find((c) => c.items.some((i) => i.tab === tab)); }

/* ---------- 一级导航：左栏只放大类 ----------
   两级分工（2026-09-23 晚回调）：左栏负责「大类」，稳定不跳动、一眼看全；
   子类移到内容区顶部横向平铺（见 paintSubnav），逐个点选更顺手。
   renderNav() 只绑事件、不写 hash、不切面板，故可被异步数据随时安全调用（无递归风险）。 */
function renderNav() {
  const side = $("#sidenav");
  if (!side) return;
  const navItem = (c) => {
    const open = c.key === CURRENT.cat;
    const items = itemsOfCat(c);   // 动态轨道（跟我学习 / 知识获取）在运行时由 kb_sources.yaml 决定
    return `<a class="nav-item${open ? " active" : ""}" data-cat="${escAttr(c.key)}" ` +
      `href="#${escAttr(c.key)}/${escAttr(firstRealTab(items))}" title="${escAttr(c.label)}">` +
      `<span class="ni-ico" aria-hidden="true">${esc(c.icon || "")}</span>` +
      `<span class="ni-txt">${esc(c.label)}</span>` +
      `<span class="ni-n">${items.filter((i) => i.tab && !i.child).length}</span></a>`;
  };
  // 2026-10-12（P1 部分）：盘后项（复盘成长）沉到最底部，用「盘后」分隔线与盘中决策项隔开。
  const main = NAV.filter((c) => !c.pinned);
  const pinned = NAV.filter((c) => c.pinned);
  side.innerHTML = `<div class="rail-head">导航</div>` + main.map(navItem).join("")
    + (pinned.length ? `<div class="rail-sep" title="盘后动作（复盘 / 学习），盘中一般用不到"><span>盘后</span></div>`
        + pinned.map(navItem).join("") : "");
  $$("#sidenav .nav-item").forEach((a) => {
    // 切大类时落到该类的第一个子板块；已是当前类则保持原选中项，避免"点一下跳回第一项"
    a.onclick = (e) => {
      e.preventDefault();
      const keep = a.dataset.cat === CURRENT.cat ? CURRENT.tab : null;
      selectNav(a.dataset.cat, keep, true);
    };
  });
}

/* ================= 增强功能（2026-09-27 新增，隔离增量） =================
   智能选股 / 资金流向 / 估值诊股 / 研报评级 / 多资产 / 资讯公告。
   后端：emapi + 独立模块（moneyflow/screener/valuation/research/multiquote/newsfeed），零 JOIN 既有业务表。
   前端：进入子板块才拉（selectNav 懒加载）；复用 app.css 既有 .wt-* 浅色科技风样式。 */
const FEAT = {
  screener: { screen: "", data: null, boardKind: "industry", boards: null,
    // 2026-10-07 多条件组合：screens=估值/持股/质量类多选 AND（boards/decline 仍独占视图走 screen）
    // 2026-10 修复「智能选股死按钮」：默认改为 worth（换手率值得率，数据源=腾讯/新浪日 K，
    // 东财估值报表被网络策略拦截也能出真实数据），不再默认挂死的东财估值屏。
    screens: ["worth"], thr: {},
    // 2026-10-07 提速：点过的组合结果按「keys|阈值」缓存，切回来立刻回显不再等后端
    resCache: {},
    boardSel: null, boardMembers: null, boardSort: "score",
    decline: { days: 30, down: 25, data: null, loading: false },
    // 2026-10-07 智能选股增强（B1 策略保存 / B2 行业热力图 / B3 列自定义）：纯前端状态，零后端改动
    visualMode: "table", colSel: null, industryFilter: null },
    // 入手概率（2026-10-05 独立成板块）：榜单 + 时间轴 + 各归档日展开态
  rate: { data: null, loading: false, error: null, tl: null, tlLoading: false,
          snap: null, days: 30, limit: 30, tlWindow: 10, tlPer: 5,
          open: {}, msg: "", msgBad: false,
          // 2026-10-05 空态引导：刚点过「跑全市场榜 / 归档今天」的 2 分钟内算「归档生成中」，
          // 此时 has_snap=false 不该甩一句冷冰冰的「还没有归档」，而该说「正在生成（约 1 分钟）」并自己重试。
          tlBusyUntil: 0, tlPoll: 0, tlPollTimer: null, tlPollMax: 3 },
  // 长期低位（2026-10-13 进哥需求 demo）：埋伏"便宜的好公司"等突破；板块二
  // 长期底部埋伏（2026-10-21 进哥需求）：全市场自适应底部平台，1/2/3/5/8 年分档
  lowboxBottom: { data: null, loading: false, error: null, minYears: 0, state: "all",
                  pool: "market", codes: "", limit: 100, includeST: false, poll: 0, pollTimer: null,
                  prefix: "", minScore: 0, medal: "all" },
  lowbox: { data: null, loading: false, error: null, codes: "", limit: 30, boxWindow: 750, lowPct: 30,
            quality: "off", wShape: 40, wFund: 35, wWorth: 25, radarData: null, radarLoading: false, radarError: null, boRows: [] },
  moneyflow: { mode: "board", symbol: "", data: null,
    boardKind: "industry", snap: null, curve: null, autoTimer: null,
    // 2026-10-05 王总要求「日期要看多少号到多少号之间的流入」，不再搞没有边界的「累计」：
    // single = 最新交易日单日（默认展示口径），range = 自定义起止日期区间。
    boardMode: "single", range: null, rangeStart: "", rangeEnd: "", rangeDates: [] },
  research: { symbol: "", data: null, rating: null, onlyTar: false, insight: null, riFilter: "all", riTheme: "" },
  multiquote: { added: [], data: null },
  news: { symbol: "600519", data: null },
  policy: { data: null, loading: false, retried: false },
};
const VAL = { symbol: "" };

/* ---------- 估值诊股「最近诊过」池（2026-09-29） ----------
   个股详情弹窗点「估值诊股」（或在本页手动评分）的票都会记进来，
   下拉名单可直接选。上限 VAL_POOL_MAX 条，超了自动清最旧的（FIFO）。 */
const VAL_POOL_KEY = "wb_val_pool_v1";
const VAL_POOL_MAX = 20;
function valPoolGet() {
  try {
    const a = JSON.parse(localStorage.getItem(VAL_POOL_KEY) || "[]");
    return Array.isArray(a) ? a.filter((x) => x && x.code) : [];
  } catch (e) { return []; }
}
function valPoolPush(code, name) {
  if (!code) return;
  const pool = valPoolGet().filter((x) => x.code !== code);   // 去重：再点一次提到最前
  pool.unshift({ code, name: name || "", ts: Date.now() });
  if (pool.length > VAL_POOL_MAX) pool.length = VAL_POOL_MAX; // 上限 20：到量自动清最旧
  try { localStorage.setItem(VAL_POOL_KEY, JSON.stringify(pool)); } catch (e) { /* 隐私模式等：静默降级 */ }
}
function valPoolClear() { try { localStorage.removeItem(VAL_POOL_KEY); } catch (e) {} }

function featMoney(v) {
  if (v === null || v === undefined || isNaN(v)) return '<span class="muted">—</span>';
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(2) + " 亿";
  if (a >= 1e4) return (v / 1e4).toFixed(1) + " 万";
  return v.toFixed(0);
}
/* 选股筛选项（2026-10-07 王总：阈值可调 + 合并持股档 + 质量扩充 + 两行分组 + 技术面值得率）。
   每项 key 与后端 screener.METRICS 一一对应；op=阈值方向，def=建议默认阈值，unit=单位。
   估值/规模 + 筹码为一行（AND 组合），质量为第二行（巴菲特口径，数据源当前受限），
   技术面为第三组（值得率=换手率五维评分 0~100，口径同「入手概率」页）。 */
const SCREENER_GROUPS = [
  { title: "估值 / 规模", items: [
    { k: "pe",  nm: "PE",  op: "<", def: 20, unit: "" },
    { k: "pb",  nm: "PB",  op: "<", def: 1,  unit: "" },
    { k: "ps",  nm: "PS",  op: "<", def: 2,  unit: "" },
    { k: "peg", nm: "PEG", op: "<", def: 1,  unit: "" },
    { k: "pcf", nm: "PCF", op: "<", def: 10, unit: "" },
    { k: "cap", nm: "市值", op: "<", def: 50, unit: "亿" },
  ]},
  { title: "筹码集中度", items: [
    { k: "holders", nm: "股东户数", op: "<", def: 1, unit: "万户" },
  ]},
  { title: "质量（巴菲特口径）", items: [
    { k: "roe", nm: "ROE",     op: ">", def: 15, unit: "%" },
    { k: "gm",  nm: "毛利率",  op: ">", def: 30, unit: "%" },
    { k: "dr",  nm: "负债率",  op: "<", def: 50, unit: "%" },
    { k: "dy",  nm: "股息率",  op: ">", def: 2,  unit: "%" },
  ]},
  { title: "技术面", items: [
    { k: "worth", nm: "值得率", op: "≥", def: 70, unit: "" },
  ]},
];
const SCREENER_QUAL_KEYS = ["roe", "gm", "dr", "dy"];
const SCREENER_QUAL_NAME = { roe: "ROE", gm: "毛利率", dr: "负债率", dy: "股息率" };
function scQualCell(r, k) {
  const v = r[k];
  if (v == null) return '<span class="muted">—</span>';
  return Number(v).toFixed(2) + "%";
}
/* 板块分类 / 连续阴跌 的常量（2026-09-30 新增两个子板块） */
const SCREENER_PRESETS = [["20", "15", "近20天15跌"], ["30", "25", "近30天25跌"],
  ["50", "40", "近50天40跌"], ["60", "50", "近60天50跌"]];
const SCREENER_BOARD_SORTS = [["score", "综合"], ["mktcap", "市值"],
  ["amount", "成交额"], ["pct", "涨跌幅"]];

/* ---------- 智能选股 ---------- */
function loadScreener(screen) {
  const st = FEAT.screener;
  if (screen) st.screen = screen;
  // 2026-09-30 新增两个子板块：板块分类 / 连续阴跌（走各自的加载与渲染）
  if (st.screen === "boards") { loadScreenerBoards(); return; }
  if (st.screen === "decline") { loadScreenerDecline(); return; }
  // 2026-10-07 多条件组合：估值/持股/质量类 chips 多选 AND，阈值随每个 chip 的内联输入框可调
  st.screen = "";
  if (!st.thr) st.thr = {};
  SCREENER_GROUPS.forEach((g) => g.items.forEach((it) => {
    if (st.thr[it.k] == null) st.thr[it.k] = it.def;
  }));
  const keys = st.screens.length ? st.screens : (st.screens = ["worth"]);
  // 只把选中的指标带上阈值（未选中的不传，后端用默认值也无所谓）
  const params = {};
  keys.forEach((k) => { if (st.thr[k] != null) params[k] = st.thr[k]; });
  const hasHolder = keys.includes("holders");
  const hasWorth = keys.includes("worth");
  // 2026-10-07 提速：点过的组合直接回显缓存结果（后端同日数据也是毫秒级，双保险）
  const ck = keys.join(",") + "|" + JSON.stringify(params);
  if (st.resCache[ck]) { st.data = st.resCache[ck]; renderScreener(); return; }
  if (hasHolder || hasWorth) {
    // 当日首跑要建全市场快照/拉户数/评分预热（约 10~30 秒），之后每种组合秒出；先给提示别像卡死
    st.data = { ok: true, desc: keys.join(","), rows: null, loadingHolder: true,
      loadingWorth: !hasHolder && hasWorth };
    renderScreener();
  }
  const qs = `screen=${encodeURIComponent(keys.join(","))}&limit=40&p=${encodeURIComponent(JSON.stringify(params))}`;
  api(`/api/screener?${qs}`)
    .then((d) => { st.resCache[ck] = d; st.data = d; renderScreener(); })
    .catch((e) => { st.data = { ok: false, reason: String((e && e.message) || e) }; reportLoadError("智能选股", e); renderScreener(); });
}
function loadScreenerBoards(force) {
  const st = FEAT.screener;
  if (st.boards && !force) { renderScreener(); return; }
  api(`/api/screener_boards?kind=${encodeURIComponent(st.boardKind)}&pz=90`)
    .then((d) => { st.boards = d; renderScreener(); })
    .catch((e) => { st.boards = { ok: false, reason: String((e && e.message) || e) }; renderScreener(); });
}
function loadScreenerBoardMembers(code, name) {
  const st = FEAT.screener;
  st.boardSel = { code, name };
  st.boardMembers = null;
  renderScreener();
  api(`/api/screener_board_members?code=${encodeURIComponent(code)}&num=100`)
    .then((d) => { if (st.boardSel && st.boardSel.code === code) { st.boardMembers = d; renderScreener(); } })
    .catch((e) => { if (st.boardSel && st.boardSel.code === code) {
      st.boardMembers = { ok: false, reason: String((e && e.message) || e) }; renderScreener(); } });
}
function loadScreenerDecline(days, down, sort) {
  const st = FEAT.screener, dc = st.decline;
  // 2026-09-30「返回来路」配套：无参调用 = 切页返回 / 盘中轮询，此时**保留当前视图**——
  // 之前无条件 dc.mode="decline" 会把「全市场值得率 Top30」重置回阴跌表，王总回来还得重设一遍。
  const noargs = (days == null && down == null && sort == null);
  if (noargs && dc.mode === "top") {
    const had = !!(dc.data && dc.data_mode === "top");
    dc.loading = !had;                      // 有旧数据先原样呈现，后台刷新（stale-while-revalidate）
    renderScreener();
    api("/api/screener_turnover_top?days=30&limit=30")
      .then((d) => { if (dc.mode !== "top") return; dc.data = d; dc.data_mode = "top"; dc.loading = false; renderScreener(); })
      .catch(() => { if (dc.mode !== "top") return; dc.loading = false; renderScreener(); });
    return;
  }
  dc.mode = "decline";
  if (days != null) dc.days = Math.max(5, Math.min(120, parseInt(days, 10) || 30));
  if (down != null) {
    dc.down = Math.max(1, Math.min(dc.days, parseInt(down, 10) || 25));
  }
  if (sort != null) dc.sort = (sort === "score") ? "score" : "down";
  // 无参刷新且已有阴跌数据：不打断当前表（不亮 loading），新数据到了再整体重绘
  dc.loading = !(noargs && dc.data && dc.data_mode === "decline");
  renderScreener();
  api(`/api/screener_decline?days=${dc.days}&down=${dc.down}&limit=80&sort=${encodeURIComponent(dc.sort || "down")}`)
    .then((d) => { if (dc.mode !== "decline") return; dc.data = d; dc.data_mode = "decline"; dc.loading = false; renderScreener(); })
    .catch((e) => { if (dc.mode !== "decline") return; dc.data = { ok: false, reason: String((e && e.message) || e) }; dc.data_mode = "decline"; dc.loading = false; renderScreener(); });
}
function loadScreenerTop() {
  const st = FEAT.screener, dc = st.decline;
  dc.mode = "top";
  dc.loading = true;
  renderScreener();
  api("/api/screener_turnover_top?days=30&limit=30")
    .then((d) => { if (dc.mode !== "top") return; dc.data = d; dc.data_mode = "top"; dc.loading = false; renderScreener(); })
    .catch((e) => { if (dc.mode !== "top") return; dc.data = { ok: false, reason: String((e && e.message) || e) }; dc.data_mode = "top"; dc.loading = false; renderScreener(); });
}
function screenerBoardsBody(st) {
  if (st.boardSel) {
    const bm = st.boardMembers, sortK = st.boardSort || "score";
    let inner = "";
    if (!bm) inner = `<div class="hint wt-empty">正在取板块成分股并计算综合分…</div>`;
    else if (bm.ok === false) inner = `<div class="muted wt-empty">接口异常：${esc(bm.reason || "未取到数据")}</div>`;
    else {
      const rows = (bm.rows || []).slice();
      rows.sort((a, b) => (b[sortK] == null ? -9e9 : b[sortK]) - (a[sortK] == null ? -9e9 : a[sortK]));
      const trs = rows.map((r, i) => `<tr>
        <td class="wt-c1">${i + 1}</td>
        <td><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
        <td>${esc(r.name || "")}</td>
        <td>${wtMoney(r.price)}</td><td>${wtPct(r.pct)}</td>
        <td>${r.turnover == null ? '<span class="muted">—</span>' : Number(r.turnover).toFixed(2) + "%"}</td>
        <td>${featMoney(r.mcap_yuan)}</td><td>${featMoney(r.amount)}</td>
        <td><b>${r.score == null ? "—" : r.score.toFixed(1)}</b></td>
      </tr>`).join("");
      inner = `<div class="wt-tw"><table class="wt-tb"><thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th><th>换手</th>
        <th>总市值</th><th>成交额</th><th>综合分</th>
      </tr></thead><tbody>${trs || `<tr><td colspan="9" class="muted wt-empty">该板块暂无成分股数据。</td></tr>`}</tbody></table></div>
      ${bm.total_note ? `<div class="hint wt-foot">${esc(bm.total_note)}</div>` : ""}`;
    }
    const sortPills = SCREENER_BOARD_SORTS.map(([k, lbl]) =>
      `<a class="wt-tab${sortK === k ? " active" : ""}" data-sc-bsort="${k}">${lbl}</a>`).join("");
    return `<div class="sc-crumb"><a href="javascript:void(0)" id="scBoardBack">← 返回板块列表</a>
        <b>${esc(st.boardSel.name || st.boardSel.code)}</b>
        <span class="muted">综合分 = 市值50% + 成交额30% + 换手率20%（知名度口径）</span></div>
      <div class="wt-tabs">${sortPills}</div>${inner}`;
  }
  const d = st.boards;
  const kindTabs = [["industry", "行业板块"], ["concept", "概念板块"]].map(([k, lbl]) =>
    `<a class="wt-tab${st.boardKind === k ? " active" : ""}" data-sc-bkind="${k}">${lbl}</a>`).join("");
  let grid = "";
  if (!d) grid = `<div class="hint wt-empty">正在取板块清单…</div>`;
  else if (d.ok === false || !(d.rows || []).length) {
    grid = `<div class="muted wt-empty">接口异常：${esc((d && d.reason) || "未取到板块数据")}</div>`;
  } else {
    const chips = d.rows.map((r) => `<a class="sc-board" data-sc-board="${escAttr(r.board_code)}" data-sc-name="${escAttr(r.board_name)}" href="javascript:void(0)">
      <span>${esc(r.board_name)}</span><i class="${(r.pct || 0) >= 0 ? "up" : "dn"}">${wtPct(r.pct)}</i></a>`).join("");
    grid = `<div class="sc-board-grid">${chips}</div>
      <div class="hint wt-foot">板块涨跌幅为当日板块均价口径；点板块名看成分股（按知名度综合排序）。</div>`;
  }
  return `<div class="wt-tabs">${kindTabs}</div>${grid}`;
}
function screenerDeclineBody(st) {
  const dc = st.decline;
  const mode = dc.mode || "decline";
  const presets = SCREENER_PRESETS.map(([n, m, lbl]) =>
    `<button class="btn sm${mode === "decline" && Number(dc.days) === Number(n) && Number(dc.down) === Number(m) ? " active" : ""}" data-sc-dn="${n},${m}">${lbl}</button>`).join("");
  const sortPills = [["down", "按下跌天数"], ["score", "按入手值得率"]].map(([k, lbl]) =>
    `<a class="wt-tab${mode === "decline" && (dc.sort || "down") === k ? " active" : ""}" data-sc-dsort="${k}">${lbl}</a>`).join("");
  const ctl = `<div class="sc-dn-ctl">
    <span>近</span><input id="scDnDays" type="number" min="5" max="120" value="${esc(dc.days)}"><span>个交易日中</span>
    <input id="scDnDown" type="number" min="1" max="120" value="${esc(dc.down)}"><span>天下跌</span>
    <button class="btn sm" id="scDnGo">筛选</button>
    <span class="sc-dn-sep"></span><span class="muted">便捷：</span>${presets}
    <span class="sc-dn-sep"></span><button class="btn sm${mode === "top" ? " active" : ""}" id="scDnTop">全市场值得率 Top30</button>
    ${mode === "decline" ? `<span class="sc-dn-sep"></span><span class="muted">排序：</span>${sortPills}` : ""}
  </div>`;
  const tierCls = { 5: "sc-t5", 4: "sc-t4", 3: "sc-t3", 2: "sc-t2", 1: "sc-t1" };
  const chip = (r) => r.score == null ? '<span class="muted">—</span>'
    : `<span class="sc-score ${tierCls[r.tier] || "sc-t3"}" title="${escAttr((r.hint || "") + (r.dim ? `｜位置${r.dim.pos}+场景${r.dim.scene}+持续${r.dim.persist}+量价${r.dim.vp}+企稳${r.dim.stab}` : ""))}"><b>${r.score}</b>${esc(r.tier_label || "")}</span>`;
  let inner = "";
  if (dc.loading) {
    inner = mode === "top"
      ? `<div class="hint wt-empty">全市场扫描中…约 5600 只逐只核对日 K，<b>首次约 1 分钟</b>（收盘后系统自动预热，平时点开秒出）。请勿关闭页面。</div>`
      : `<div class="hint wt-empty">扫描中…首次需逐只抓取日K（约10~40秒），之后当天调参数秒出结果。</div>`;
  } else {
    const stale = dc.data_mode && dc.data_mode !== mode;
    const d = dc.data;
    if (stale || !d) {
      inner = mode === "top"
        ? `<div class="hint wt-empty">点「全市场值得率 Top30」：全 A 股按入手值得率降序取前 30（首次扫描约 1 分钟，收盘后系统自动预热，平时点开秒出）。</div>`
        : `<div class="hint wt-empty">设置窗口天数与下跌天数后点「筛选」，或直接点便捷按钮。</div>`;
    } else if (d.ok === false) {
      inner = `<div class="muted wt-empty">接口异常：${esc(d.reason || "未取到数据")}</div>`;
    } else if (mode === "top" && dc.data_mode === "top") {
      const rows = scCurrentRows(st).map((r, i) => `<tr>
        <td class="wt-c1">${i + 1}</td>
        <td><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
        <td>${esc(r.name || "")}</td>
        <td>${wtMoney(r.price)}</td>
        <td>${wtPct(r.last_pct)}</td>
        <td>${r.to_today == null ? '<span class="muted">—</span>' : esc(Number(r.to_today).toFixed(2)) + "%" +
            (r.to_avg5 == null ? "" : ` <span class="muted">/ 5均 ${esc(Number(r.to_avg5).toFixed(2))}%</span>`)}</td>
        <td>${r.pos_pct == null ? '<span class="muted">—</span>' : esc(Number(r.pos_pct).toFixed(0)) + "%"}</td>
        <td>${chip(r)}</td>
        <td class="sc-hint" title="${escAttr(r.hint || "")}">${esc(r.hint || "")}</td>
      </tr>`).join("") || `<tr><td colspan="9" class="muted wt-empty">全市场没有可评分的股票（行情源异常？），请稍后重试。</td></tr>`;
      inner = `<div class="wt-tw"><table class="wt-tb sc-dn-tb"><thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>当日涨跌</th><th>换手率</th><th>位置分位</th><th>入手值得率</th><th>提示</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
      ${basisDetails(d)}
      <div class="hint wt-foot">扫描范围：全 A 股（新浪 hs_a 节点，共 ${d.pool_size || 0} 只，K线不足或成交量缺失已跳过 ${d.skipped_bars || 0} 只），
      按「入手值得率」降序取 Top ${d.limit || 30}；数据日 ${esc(d.trade_date || "—")}；首次全量扫描较慢，K 线当天缓存复用。
      ST 票换手形态天然异常，评分参考价值打折。不构成投资建议。点代码看个股。</div>`;
    } else {
      const rows = scCurrentRows(st).map((r, i) => `<tr>
        <td class="wt-c1">${i + 1}</td>
        <td><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
        <td>${esc(r.name || "")}</td>
        <td>${wtMoney(r.price)}</td>
        <td><b>${r.down_days}</b> / ${r.days} 天</td>
        <td>${wtPct(r.cum_pct)}</td>
        <td>${wtPct(r.avg_dn_pct)}</td>
        <td>${r.max_streak} 天</td>
        <td>${wtPct(r.last_pct)}</td>
        <td>${r.to_today == null ? '<span class="muted">—</span>' : esc(Number(r.to_today).toFixed(2)) + "%" +
            (r.to_avg5 == null ? "" : ` <span class="muted">/ 5均 ${esc(Number(r.to_avg5).toFixed(2))}%</span>`)}</td>
        <td>${chip(r)}</td>
        <td class="sc-hint" title="${escAttr(r.hint || "")}">${esc(r.hint || "")}</td>
      </tr>`).join("") || `<tr><td colspan="12" class="muted wt-empty">当前条件下没有符合的股票，试试放宽下跌天数要求。</td></tr>`;
      inner = `<div class="wt-tw"><table class="wt-tb sc-dn-tb"><thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>下跌天数</th><th>累计跌幅</th><th>日均跌幅</th><th>最长连跌</th><th>当日涨跌</th><th>换手率</th><th>入手值得率</th><th>提示</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
      ${basisDetails(d)}
      <div class="hint wt-foot">扫描范围：智能选股6策略并集 + 关注池 + 近10日龙虎榜（共 ${d.pool_size || 0} 只，K线不足 ${d.days + 1} 根的已跳过）；
      数据日 ${esc(d.trade_date || "—")}；下跌=收盘低于前一日收盘（前复权）。换手率=成交量÷流通股本（腾讯实时换算，7天缓存）。点代码看个股。</div>`;
    }
  }
  return `${ctl}${inner}`;
}
/* ================= 入手概率（2026-10-05 王总需求：从智能选股独立成板块 + 时间轴）=================
   上半：全市场入手概率榜 —— 五维换手评分（位置/场景/持续性/量价/企稳），档位拆成七级
        A+ / A / B+ / B / C+ / C / D，每级配「判据定义 + 动作建议」。
   下半：时间轴 —— 每个归档日一张卡，回看「当时建议入市价 → 后来 T+1/3/5/10 收盘价」的比值。
   归档由「跑榜成功后顺手 POST /api/rate/snapshot」完成（同日覆盖、幂等），另有「归档今天」兜底。
   ★ 纪律：未到 T+N 的格子**写「待 T+N」而不是 0** —— 0 会被读成平盘，是假信号。 */
/* ===== 「入手值得率 / 入手概率」口径说明（2026-10-05 提到顶层）=====
   原先这个函数定义在 screenerDeclineBody 内部，入手概率面板要用时作用域够不着，
   故整体上提一层：智能选股（连续阴跌视图）与入手概率两处共用同一份口径说明。 */
const basisDetails = (d) => `<details class="bm-src sc-dn-basis"><summary>「入手值得率」怎么算的（换手率四场景 + 五维权重口径）</summary>
        <div class="sc-basis-bd">
          <p><b>五维评分（满分 100）：</b>位置分位 30（现价在近${esc(d.days)}日高低区间越低分越高）＋
           场景温度 30（低位高换手=吸筹 30 / 低位低换手不创新低=洗盘 24 / 高位低换手=锁仓 18 / 高位高换手=出货 0~4 / 其余中性 12）＋
           换手持续性 15（温和抬升最佳，单日异常脉冲疑似对倒扣分）＋ 量价配合 15（下跌缩量、低点不创新低加分；放量下跌、长上影扣分）＋
           企稳信号 10（当日转红、跌势收窄、收复 5 日前收盘）。</p>
          <p><b>档位（七级，上手概率面板与连续阴跌共用）：</b>${[
            "A+ ≥92 强吸筹·可分批建仓", "A 85~91 积极关注·小仓试探", "B+ 75~84 值得持有·等放量",
            "B 62~74 中性偏多·轻仓", "C+ 45~61 中性观望", "C 30~44 偏弱回避", "D &lt;30 技术面弱势·不参与",
          ].join(" / ")}
            <span class="muted">（按技术面换手形态综合评分分档，<b>不是</b>风险事件分级）</span></p>
          ${(d.basis || []).map((b) => `<p class="muted">· ${esc(b)}</p>`).join("")}
        </div>
      </details>`;

const RATE_TTL = [1, 3, 5, 10];

/* 时间轴刻度格：主值＝对入市价的比值（沿用旧口径），
   下面再补一行「对当时理想买点价」——判断准不准，量的是"离买点跑多远"，比只量入市价更贴题。 */
/* 「vs 买点」小字（2026-10-05 王总需求）。
   两个出处：① T+N 出数后，对该票首次标注买点的涨跌；② T+0 入市价那格，
   把你标的「最值得入手价」和你当天真看到的现价摆一起比对（踏空/等到了，一眼看出）。 */
function vsTargetSpan(diff) {
  if (diff == null) return "";
  const v = Number(diff);
  if (!isFinite(v)) return "";
  const cls = v >= 0 ? "bull" : "bear";
  return `<span class="tl-vs ${cls}" title="对该票首次标注的「最值得入手价」的涨跌">vs 买点 ${v > 0 ? "+" : ""}${v.toFixed(1)}%</span>`;
}
function rateTickSub(tick) {
  if (!tick || tick.vs_target == null) return "";
  return vsTargetSpan(tick.vs_target);
}

/* 档位徽标：数字 + 等级名两件套，颜色只区分主档（A/B/C/D），
   A+ 与 A 同色靠文字区分，免得七级染色把表格搞脏。 */
function rateGradeChip(r, tip2) {
  const g = r.grade || "—";
  const cls = "gr-" + (r.grade_cls || "d");
  const tip = escAttr([r.grade_desc || "", r.advice || tip2 || ""].filter(Boolean).join(" ｜ "));
  return `<span class="gr-chip ${cls}" title="${tip}"><b>${esc(g)}</b>${esc(r.tier_label || "")}</span>`;
}
function rateDimText(r) {
  const d = r.dim || {};
  if (d.pos == null) return "";
  return `位置 ${d.pos}/30 · 场景 ${d.scene}/30 · 持续 ${d.persist}/15 · 量价 ${d.vp}/15 · 企稳 ${d.stab}/10`;
}
function rateTick(tick, n) {
  if (!tick) return '<span class="muted">—</span>';
  if (tick.price == null) {
    // 后端已算好 why（含真实基准 K 线日期与「到点自动出现」），挂 title 让悬停能看到原因，
    // 不给格子塞长句——表格列宽有限，塞进去就挤成叠字了。
    const why = tick.why ? String(tick.why).replace(/"/g, "&quot;") : `T+${n} 还没走完（该交易日数不足），留空不填 0`;
    return `<span class="tl-pend" title="${why}">待 T+${n}</span>`;
  }
  const ret = Number(tick.ret);
  return `<span class="tl-px" title="${escAttr(tick.date || "")} 收盘">${Number(tick.price).toFixed(2)}</span>` +
         `<span class="tl-rt ${ret >= 0 ? "bull" : "bear"}">${ret > 0 ? "+" : ""}${ret.toFixed(2)}%</span>`;
}
/* 「最值得入手价」单元格（2026-10-05 王总需求）。
   算法值可手填覆盖：失焦即存。dist＝现价/买点-1，正数＝还要跌这么多才够得着买点。

   2026-10-12 按进哥拍板的 A+B 方案改（原「已到/跌破买点，可分批买入」被质疑：依据不清 + 与五维混读）：
   A 文案与依据对齐：这一格是**纯价格结构单一口径**（近20日振幅回撤 + 近60日收盘20%分位支撑），
     **不含**换手率/量能/资金流/情绪/板块 —— 所以不再用"可分批买入"这种行动建议口吻陈述，
     并把"依据是什么 + 不含什么"写进 title，避免被当成"综合了五维之后的结论"。
   B 双确认：只有「五维换手档位 ≥ A」**且** dist ≤ 0（价格到位）才给「可分批」字样；
     档位不够就只报价格事实，并在 title 里说明为什么没给。
     于是「可分批」= 价格到位 且 换手形态对，两套口径才真正联动。 */
function rateIdealCell(r) {
  const v = (r.ideal_buy != null && r.ideal_buy !== "") ? Number(r.ideal_buy) : "";
  const dist = r.ideal_dist != null ? Number(r.ideal_dist) : null;
  const gap = r.ideal_gap != null ? Math.abs(Number(r.ideal_gap)) : null;
  const grade = r.grade || "";
  const strong = grade === "A+" || grade === "A";      // 五维档位 ≥ A
  const reached = dist != null && dist <= 0;           // 价格到位（dist==0＝买点被截到现价，也算到位）
  const buy = reached && strong;                       // 双确认
  const cls = dist == null ? ""
    : (buy ? "bp-buy" : (reached ? "bp-at" : (dist <= 8 ? "bp-near" : "bp-wait")));

  // A：只陈述价格事实，不喊"买入"
  let tip;
  if (dist == null) tip = "数据不足";
  else if (dist > 0) tip = `距买点还差 ${dist.toFixed(2)}%`;
  else {
    const px = dist < 0 ? `低于测算买点 ${Math.abs(dist).toFixed(2)}%` : "已到测算买点";
    tip = buy ? `${px} · 可分批` : px;
  }

  // A：摊开依据 + 明说不含什么；B：讲清"给了/没给可分批"的理由
  const algo = "测算买点 = 现价回撤"
    + (gap == null ? "（近20日振幅×3，限5%~25%）" : gap.toFixed(1) + "%（近20日振幅×3，限5%~25%）")
    + "，并与近60日收盘 20% 分位（支撑）取较高者，最高不超过现价；可手填覆盖（失焦保存）";
  const scope = "⚠ 本格仅「价格结构」单一口径 —— 不含换手率、量能、资金流、情绪、板块";
  const why = dist == null ? ""
    : buy ? `五维换手档位 ${grade}（≥A）＋ 价格到位 → 双确认，给「可分批」`
    : reached ? `五维换手档位 ${grade || "—"}（未达 A）→ 本格只报价格到位，不给「可分批」（口径 B：双确认才给）`
    : "";
  const title = [algo, scope, why].filter(Boolean).join("　｜　");

  return `<input class="bp-in ${cls}" data-bp-code="${escAttr(r.code)}" value="${v === "" ? "" : v.toFixed(2)}"
      title="${escAttr(title)}" />
    <span class="bp-d ${cls}" title="${escAttr(title)}">${esc(tip)}</span>`;
}
/* 跟踪列：锚点日（首次标注，锁死）→ 今天，标注价 → 现价，以及涨跌幅。
   红＝已越过买点（踏空/轮到别人吃肉），绿＝还在买点下方（等得到，可买）。 */
function rateTrackCell(r, bm) {
  const a = bm && bm[r.code];
  if (!a || !a.anchor_date) return `<span class="muted tk-none">未标锚</span>`;
  const d0 = String(a.anchor_date).slice(0, 10);
  const tgt = (a.manual != null && a.manual !== "") ? Number(a.manual)
    : (a.anchor_target != null ? Number(a.anchor_target) : null);
  const now = r.price != null ? Number(r.price) : null;
  let days = "";
  try {
    days = Math.max(0, Math.round((Date.now() - new Date(d0).getTime()) / 86400000)) + "天";
  } catch (e) { days = ""; }
  let cls = "", txt = "—";
  if (tgt && now) {
    const vs = (now / tgt - 1) * 100;
    cls = vs >= 0 ? "bull" : "bear";
    txt = (vs > 0 ? "+" : "") + vs.toFixed(1) + "%";
  }
  const st = a.manual != null && a.manual !== "" ? "手填" : (tgt ? "算" : "");
  return `<span class="tk" title="锚点 ${esc(d0)}（首次标注锁死）· 标注价 ${tgt == null ? "—" : tgt.toFixed(2)} · 锚点日现价 ${a.anchor_price == null ? "—" : Number(a.anchor_price).toFixed(2)}">
    <span class="tk-d">${esc(d0.slice(5))}起 ${days}</span>
    <span class="tk-v">${tgt == null ? "—" : tgt.toFixed(2)} → ${now == null ? "—" : now.toFixed(2)}</span>
    <span class="tk-p ${cls}">${txt}</span>
    ${st ? `<span class="tk-s">${st}</span>` : ""}
  </span>`;
}
function rateRow(r, i, bm) {
  return `<tr>
    <td class="wt-c1">${i + 1}</td>
    <td><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
    <td>${esc(r.name || "")}</td>
    <td>${r.price == null ? '<span class="muted">—</span>' : Number(r.price).toFixed(2)}</td>
    <td class="rate-ideal">${rateIdealCell(r)}</td>
    <td class="rate-track">${rateTrackCell(r, bm)}</td>
    <td>${r.pos_pct == null ? '<span class="muted">—</span>' : Number(r.pos_pct).toFixed(0) + "%"}</td>
    <td class="rate-score">${r.score == null ? '<span class="muted">—</span>' : `<b>${r.score}</b>`}</td>
    <td>${rateGradeChip(r)}</td>
    <td class="sc-hint rate-hint">
      <span class="rh-1">${esc(r.hint || r.scenario || "")}</span>
      <span class="rh-2">${esc(rateDimText(r))}${(r.advice && r.hint) ? " ｜ " : ""}${esc(r.advice || "")}</span>
    </td>
  </tr>`;
}
function rateTable(d) {
  const bm = d.buy_map || {};
  const body = (d.rows || []).map((r, i) => rateRow(r, i, bm)).join("") ||
    `<tr><td colspan="10" class="muted wt-empty">全市场没有可评分的股票（行情源异常？），请稍后重试。</td></tr>`;
  // 2026-10-12 A+B：把「最值得入手」这一格的两个层级讲清楚 ——
  // 免得「可分批」被读成"综合了五维/资料库之后的结论"（进哥的原始质疑）。
  const legend = `<div class="rate-bp-legend">
    <b>最值得入手</b> 这一格是<b>纯价格结构</b>算法：现价回撤（近 20 日振幅×3，限 5%~25%）与
    近 60 日收盘 20% 分位（支撑）取较高者、且不超过现价 —— 只看日K，
    <b>不含换手率 / 量能 / 资金流 / 情绪 / 板块</b>。
    <span class="lg-buy">可分批</span> = <b>双确认</b>：价格已到测算买点 <b>且</b> 右侧「档位」达到 A / A+（五维换手综合 ≥85 分）；
    档位不够时只报「已到测算买点」、不给「可分批」。鼠标停在该格上可看这只票的具体依据。
  </div>`;
  return `<div class="wt-tw"><table class="wt-tb sc-dn-tb"><thead><tr>
    <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>最值得入手</th><th>跟踪（锚点→今）</th>
    <th>位置分位</th><th>入手概率</th><th>档位</th><th>提示（场景判据 · 五维分解 · 动作建议）</th>
  </tr></thead><tbody>${body}</tbody></table></div>` + legend;
}
function rateDayCard(day, open) {
  const isToday = day.snap_date === new Date().toISOString().slice(0, 10);
  const rows = (day.items || []).map((it) => `<tr>
    <td class="wt-c1">${esc(it.rank)}</td>
    <td><button type="button" class="wt-sym" data-wt-open="${escAttr(it.code)}">${escAttr(it.code)}</button></td>
    <td>${esc(it.name || "")}</td>
    <td>${it.price == null ? '<span class="muted">—</span>' : Number(it.price).toFixed(2) + vsTargetSpan(it.track && it.track.diff)}</td>
    ${RATE_TTL.map((n) => `<td class="tl-cell">${rateTick(it.ticks && it.ticks[String(n)], n)}${rateTickSub(it.ticks && it.ticks[String(n)])}</td>`).join("")}
    <td>${rateGradeChip(it)}</td>
  </tr>`).join("");
  const top = (day.items || [])[0] || {};
  const pend = [].concat.apply([], (day.items || []).map((it) => it.pending || []));
  const uniqPend = Array.from(new Set(pend)).sort((a, b) => a - b);
  return `<div class="tl-card${open ? " open" : ""}">
    <div class="tl-head" data-rate-day="${escAttr(day.snap_date)}">
      <span class="tl-arrow">▸</span>
      <span class="tl-date">${esc(day.snap_date)}</span>
      ${isToday ? '<span class="tl-tag today">今天</span>' : ""}
      <span class="tl-meta">归档 ${day.total != null ? day.total : day.n} 只${day.total && day.n < day.total ? `（列前 ${day.n}）` : ""}${top.name ? ` · 榜首 ${esc(top.name)} ${esc(top.grade || "")}` : ""}</span>
      <span class="tl-x">${uniqPend.length ? "待到期：" + uniqPend.map((n) => "T+" + n).join("/") : "刻度已全部到期"}</span>
    </div>
    <div class="tl-body">
      <table class="tl-tb"><thead><tr>
        <th>#</th><th>代码</th><th>名称</th><th>入市价<br>（T+0）</th>
        ${RATE_TTL.map((n) => `<th>T+${n} 收盘价 / 比值</th>`).join("")}
        <th>档位</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="tl-note">基准＝归档当天的入市价；T+N 取其后第 N 个交易日收盘价（前复权）。未到期的格子留空，不是 0。</div>
      ${pendingWhy(day, uniqPend)}
    </div>
  </div>`;
}
/* 待到期说明（2026-10-05）：光看「待 T+5」王总会以为坏了，得说清卡在哪一根 K 线上。
   日线只到 09-30、行情日却写 10-02 时，基准就是 09-30 那根，告诉用户「到点自动出现」比沉默强。 */
function pendingWhy(day, uniqPend) {
  const items = day.items || [];
  if (!items.length || !uniqPend.length) return "";
  const base = items[0].base_bar && items[0].base_bar.date ? items[0].base_bar.date : "";
  const td = day.trade_date || "";
  if (!base) return "";
  return `<div class="tl-note tl-note-warn">当前 ${uniqPend.length} 个刻度待到期：日线基准是 <b>${base}</b>` +
    (td && td !== base ? `（榜单行情日 ${td}，其日线尚未发布，故基准落在 ${base}）` : "") +
    `。之后每走完 N 个交易日这格自动出现比值，<b>不会补 0</b>（0 会被读成平盘，是假信号）。</div>`;
}
function rateTimelineBlock(st) {
  if (st.tlLoading) return `<div class="hint wt-empty">时间轴计算中…需补各票日线（约几秒到半分钟）。</div>`;
  const tl = st.tl;
  if (!tl) return `<div class="hint wt-empty">时间轴：跑一次「全市场榜」会自动归档当天快照，之后这里按 T+1/3/5/10 回看「入市价 → 后来收盘价」的比值。</div>`;
  if (tl.ok === false) return `<div class="muted wt-empty">时间轴不可用：${esc(tl.reason || "未知原因")}</div>`;
  if (!tl.has_snap) {
    // 刚发起过跑榜/归档（tlPoll>0）＝归档正在生成，说「正在生成，可稍后刷新」并自动重试；
    // 冷启动压根没跑过榜 ＝ 真的还没起点，才劝「跑一次全市场榜，往后每天攒」。两种不能一种文案。
    if (st.tlPoll > 0) {
      return `<div class="hint wt-empty wt-empty-busy">正在生成全市场归档（约 1 分钟）…` +
        `已自动刷新 <b>${st.tlPoll}/${st.tlPollMax}</b> 次，<b>可稍后手动点「刷新时间轴」</b>。</div>`;
    }
    return `<div class="hint wt-empty">${esc(tl.reason || "还没有归档")} —— 归档是时间轴的起点，往后每天跑一次就攒起来。</div>`;
  }
  const sum = tl.summary || {};
  const sumRow = RATE_TTL.map((n) => {
    const s = sum[String(n)];
    if (!s) return `<span class="tl-sum off">T+${n}<i class="muted">样本待攒</i></span>`;
    return `<span class="tl-sum" title="样本 ${s.n} 只（已到期）">T+${n} <b class="${s.avg >= 0 ? "bull" : "bear"}">${s.avg > 0 ? "+" : ""}${s.avg.toFixed(2)}%</b><i>胜率 ${s.win_rate.toFixed(0)}% · ${s.win}/${s.n}</i></span>`;
  }).join("");
  const days = tl.days || [];
  const cards = days.map((d) => rateDayCard(d, !!st.open[d.snap_date])).join("");
  return `<div class="rate-sec">
    <div class="rate-sec-h"><h3>时间轴</h3><span class="muted">按归档日回看「入市价 → 后来收盘价」，每一行是你做判断那天</span></div>
    <div class="tl-summary">${sumRow}</div>
    <div class="tl-cards">${cards}</div>
    <div class="hint wt-foot">样本口径：每个归档日取前 ${esc(tl.per_day || st.tlPer)} 名、${esc(tl.days_window || st.tlWindow)} 个归档日。「胜率」＝ T+N 收盘价 ≥ 入市价的比例；「待到期」＝还没走到 N 个交易日，留空不填 0。</div>
  </div>`;
}
function renderRate() {
  const host = $("#p-rate"); if (!host) return;
  const st = FEAT.rate;
  const d = st.data || {};
  const ctl = `<div class="wt-ctl rate-ctl">
    <span class="wt-cl">评分窗口</span>
    ${[[20, "近20日"], [30, "近30日"], [60, "近60日"], [90, "近90日"], [100, "近100日"]].map(([v, l]) =>
      `<a class="wt-chip${st.days === v ? " on" : ""}" data-rate-days="${v}">${l}</a>`).join("")}
    <span class="wt-cl">榜宽</span>
    ${[[10, "Top10"], [30, "Top30"], [50, "Top50"]].map(([v, l]) =>
      `<a class="wt-chip${st.limit === v ? " on" : ""}" data-rate-limit="${v}">${l}</a>`).join("")}
    <button class="btn sm" id="rateGo"${st.loading ? " disabled" : ""}>${st.loading ? "扫描中…" : "跑全市场榜"}</button>
    <button class="btn sm" id="rateSnap">归档今天</button>
    <button class="btn sm" id="rateTlGo"${st.tlLoading ? " disabled" : ""}>刷新时间轴</button>
  </div>`;
  let body;
  if (st.loading) body = `<div class="hint wt-empty">全市场扫描中…约 5600 只逐只核对日 K，<b>首次约 1 分钟</b>（收盘后系统自动预热，平时点开秒出）。</div>`;
  else if (st.error && !d.rows) body = `<div class="muted wt-empty">加载失败：${esc(st.error)}</div>`;
  else if (!d.ok) body = `<div class="muted wt-empty">接口异常：${esc(d.reason || "未取到数据")}</div>`;
  else body = rateTable(d) + basisDetails(d);
  // 瞬时反馈（归档成功/失败）走面板内提示条，不依赖任何全局 toast：少一条依赖少一处失修
  const msg = st.msg ? `<div class="hint rate-msg${st.msgBad ? " bad" : ""}">${esc(st.msg)}</div>` : "";
  host.innerHTML = `<div class="wt-head"><h2>入手概率 <span class="muted wt-sub">全市场五维换手评分排序${d.trade_date ? " · 数据日 " + esc(d.trade_date) : ""}</span></h2></div>
    ${ctl}${msg}${body}${rateTimelineBlock(st)}
    <div class="hint wt-foot">档位七级：A+ ≥92 · A 85~91 · B+ 75~84 · B 62~74 · C+ 45~61 · C 30~44 · D &lt;30。
    评分与「智能选股·连续阴跌」同一套引擎（位置/场景/持续性/量价/企稳），这里只把档位拆细并加时间轴。
    鼠标停档位徽标上看判据与动作建议。不构成投资建议。</div>`;
  $$("#p-rate [data-rate-days]").forEach((el) => el.onclick = () => {
    st.days = Number(el.dataset.rateDays); loadRate();
  });
  $$("#p-rate [data-rate-limit]").forEach((el) => el.onclick = () => {
    st.limit = Number(el.dataset.rateLimit); loadRate();
  });
  const go = $("#rateGo"); if (go) go.onclick = () => loadRate();
  // 「最值得入手」手填覆盖（2026-10-05）：只改本地这一行 + 存锚点，绝不重跑全市场榜（那要几分钟）
  $$("#p-rate [data-bp-code]").forEach((el) => {
    const save = () => {
      const code = el.dataset.bpCode;
      const raw = String(el.value).trim();
      let v = null;
      if (raw !== "") {
        v = parseFloat(raw);
        if (!isFinite(v) || v <= 0) { st.msg = "买点价得填正数"; st.msgBad = true; renderRate(); return; }
      }
      api("/api/rate/buypoint", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code, price: v }) })
        .then(() => {
          const row = (((st.data || {}).rows) || []).find((x) => x.code === code);
          if (row) {
            row.ideal_buy = v;
            const p = Number(row.price);
            row.ideal_dist = (v && p) ? (p / v - 1) * 100 : null;
          }
          st.data.buy_map = st.data.buy_map || {};
          if (st.data.buy_map[code]) st.data.buy_map[code].manual = v;
          else st.data.buy_map[code] = { anchor_date: "", anchor_target: null,
                                         anchor_price: null, manual: v, updated_at: "" };
          st.msg = v == null ? "已清空手填，恢复算法值" : "买点已设为 " + v.toFixed(2) + "（锚点已锁）";
          st.msgBad = false;
          renderRate();
        })
        .catch((e) => { st.msg = "保存失败：" + ((e && e.message) || e); st.msgBad = true; renderRate(); });
    };
    el.onblur = save;
    el.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); el.blur(); } };
  });
  const snap = $("#rateSnap");
  if (snap) snap.onclick = () => {
    const rows = (st.data && st.data.rows) || [];
    if (!rows.length) { st.msg = st.msgBad ? st.msg : "先跑一次全市场榜，榜出来即自动归档"; renderRate(); return; }
    snap.disabled = true; snap.textContent = "归档中…";
    rateMarkBusy(120000);   // 让时间轴空态走「正在生成…」并自动重试，而不是干等或甩冷提示
    api("/api/rate/snapshot", { method: "POST", headers: { "Content-Type": "application/json" },
      // trade_date 带上：榜单接口顶层给了这批行情的数据日，往后 T+N 的数日线以它为准
      body: JSON.stringify({ rows, src: "manual",
        trade_date: (st.data && st.data.trade_date) || "" }) })
      .then((s) => {
        const k = s && s.snapshot;
        st.msg = k && k.snapped ? ("已归档 " + k.snapped + " 只（同日覆盖）") : "本次没有可归档的行";
        st.msgBad = false;
        loadRateTimeline();
      })
      .catch((e) => { st.msg = "归档失败：" + apiErrText(e); st.msgBad = true; })
      .finally(() => { snap.disabled = false; snap.textContent = "归档今天"; renderRate(); });
  };
  const tlg = $("#rateTlGo"); if (tlg) tlg.onclick = () => loadRateTimeline();
  $$("#p-rate .tl-head[data-rate-day]").forEach((el) => el.onclick = () => {
    const k = el.dataset.rateDay; st.open[k] = !st.open[k]; renderRate();
  });
  // 注意：点代码开个股抽屉不用在这里绑 —— document 级委托已统一吃 [data-wt-open]（见 671 行注释），
  // 这里再绑一次会双开（靠 defaultPrevented 兜是能压住，但多一条路径就多一处失修风险）。
}
async function loadRate() {
  const st = FEAT.rate;
  st.loading = true; st.error = null; rateMarkBusy(180000); renderRate();
  try {
    const res = await api(`/api/screener_turnover_top?days=${st.days}&limit=${st.limit}`);
    st.data = res; st.loading = false;
    if (res && res.ok && (res.rows || []).length) {
      // 拉榜即归档（同日覆盖）：榜一到手顺手存一份，时间轴才有起点，不用惦记手动归档
      api("/api/rate/snapshot", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: res.rows, src: "auto",
          trade_date: res.trade_date || "" }) })
        .then((s) => { st.snap = s; loadRateTimeline(); })
        .catch(() => { /* 归档失败不该打断看榜，下一次跑榜会再试 */ });
    }
    renderRate();
  } catch (e) {
    st.loading = false; st.error = apiErrText(e); renderRate(); reportLoadError("入手概率", e);
  }
}
/* ================= 长期低位（2026-10-13 进哥需求 demo）=================
   板块二：埋伏"便宜的好公司"等突破。后端 /api/screener_lowbox 复用全历史日K 算
   低位分位 + 箱体震荡 + 国资系股东 + 长窗worth，综合排序。PE/ROE 等质量指标为增强期，暂标 «—»。 */
async function loadLowbox() {
  const st = FEAT.lowbox;
  st.loading = true; st.error = null; renderLowbox();
  try {
    const params = new URLSearchParams({
      codes: st.codes || "", limit: String(st.limit),
      box_window: String(st.boxWindow), low_pct: String(st.lowPct),
      quality: st.quality || "off",
      w_shape: String(st.wShape), w_fund: String(st.wFund), w_worth: String(st.wWorth) });
    const res = await api(`/api/screener_lowbox?${params.toString()}`);
    st.data = res; st.loading = false; renderLowbox();
  } catch (e) {
    st.loading = false; st.error = apiErrText(e); renderLowbox(); reportLoadError("长期低位", e);
  }
}
function renderLowbox() {
  const host = $("#p-lowbox"); if (!host) return;
  const st = FEAT.lowbox;
  const d = st.data || {};
  const ctl = `<div class="wt-ctl lowbox-ctl">
    <span class="wt-cl">股票池</span>
    <input id="lbCodes" class="wt-inp" placeholder="留空=关注池+板块一Top30；可填代码逗号分隔" value="${escAttr(st.codes)}" style="width:300px">
    <span class="wt-cl">箱体时长</span>
    <select id="lbBox" class="wt-inp">
      ${[[250, "1年"], [500, "2年"], [750, "3年"], [1250, "5年"], [2000, "8年"], [0, "上市以来"]].map(([v, l]) =>
        `<option value="${v}"${st.boxWindow === v ? " selected" : ""}>${l}</option>`).join("")}
    </select>
    <span class="wt-cl">低位阈值</span>
    <select id="lbLow" class="wt-inp">
      ${[[20, "≤20%"], [30, "≤30%"], [40, "≤40%"]].map(([v]) =>
        `<option value="${v}"${st.lowPct === v ? " selected" : ""}>${v}%</option>`).join("")}
    </select>
    <span class="wt-cl">质量档</span>
    <select id="lbQ" class="wt-inp">
      ${[["off", "不限"], ["loose", "宽·ROE≥6/PE≤40/负债≤65"], ["strict", "严·ROE≥10/PE≤25/负债≤50"]].map(([v, l]) =>
        `<option value="${v}"${st.quality === v ? " selected" : ""}>${l}</option>`).join("")}
    </select>
    <span class="wt-cl">权重%</span>
    <span class="wt-cl sm">形态</span><input id="lbWS" class="wt-inp" type="number" min="0" max="100" value="${st.wShape}" style="width:50px">
    <span class="wt-cl sm">基本面</span><input id="lbWF" class="wt-inp" type="number" min="0" max="100" value="${st.wFund}" style="width:50px">
    <span class="wt-cl sm">长窗</span><input id="lbWW" class="wt-inp" type="number" min="0" max="100" value="${st.wWorth}" style="width:50px">
    <button class="btn sm primary" id="lbGo"${st.loading ? " disabled" : ""}>${st.loading ? "扫描中…" : "扫描长期低位"}</button>
  </div>`;
  let body;
  if (st.loading) body = `<div class="hint wt-empty">扫描中…首次需逐只拉全历史日K（关注池/板块一Top30，至多80只），<b>约 1~3 分钟</b>（按代码缓存，二次秒出）。</div>`;
  else if (st.error && !d.rows) body = `<div class="muted wt-empty">加载失败：${esc(st.error)}</div>`;
  else if (!d.ok) body = `<div class="muted wt-empty">${esc(d.reason || "未取到数据")}</div>`;
  else body = lowboxTable(d);
  const msg = d.params ? `<div class="hint wt-foot">${esc(d.params.note || "")} 共扫描 ${d.scanned || 0} 只 / 池 ${d.universe_size || 0} 只，耗时 ${d.elapsed || 0}s。</div>` : "";
  host.innerHTML = `<div class="wt-head"><h2>长期低位震荡 <span class="muted wt-sub">埋伏"便宜的好公司"等突破 · 达瓦斯箱体(本土化)+邱国鹭+中特估</span></h2></div>
    ${ctl}${body}${msg}`;
  const go = $("#lbGo");
  if (go) go.onclick = () => {
    st.codes = ($("#lbCodes") || {}).value || "";
    st.boxWindow = Number(($("#lbBox") || {}).value) || 250;
    st.lowPct = Number(($("#lbLow") || {}).value) || 30;
    st.quality = ($("#lbQ") || {}).value || "off";
    st.wShape = Math.max(0, Math.min(100, Number(($("#lbWS") || {}).value) || 40));
    st.wFund = Math.max(0, Math.min(100, Number(($("#lbWF") || {}).value) || 35));
    st.wWorth = Math.max(0, Math.min(100, Number(($("#lbWW") || {}).value) || 25));
    loadLowbox();
  };
  $$("#p-lowbox [data-lb-add]").forEach((el) => {
    el.onclick = () => openWatchAddModal(null, { symbol: el.dataset.lbAdd });
  });
}
function lowboxTable(d) {
  const rows = d.rows || [];
  if (!rows.length) return `<div class="muted wt-empty">没有符合条件的标的（试试放宽低位阈值、箱体时长或质量档）。</div>`;
  const trs = rows.map((r, i) => {
    // 国资系：十大股东背景(state_tag) + 实控人国资(holder_tag) 双标
    const stTag = r.state_tag ? `<span class="wt-chip on" title="十大流通股东含${esc(r.state_tag)}背景">${esc(r.state_tag)}</span>` : "";
    const hTag = r.holder_tag === "国资实控" ? `<span class="wt-chip hot" title="实际控制人系国资">国资实控</span>` : "";
    const govCell = (stTag || hTag) || "—";
    const ah = r.actual_holder ? esc(r.actual_holder) : "—";
    const ahBadge = r.holder_tag === "国资实控" ? ` <span class="wt-chip hot" title="国资实控">★</span>` : "";
    // 长窗worth 三档（250/500/750）；缺档标 «—»
    const lwParts = r.lw_parts || {};
    const lw3 = ["w250", "w500", "w750"].map((k) =>
      lwParts[k] != null ? `<b>${Math.round(lwParts[k])}</b>` : "«—»").join("/");
    const lwCls = r.long_worth != null ? (r.long_worth >= 70 ? "bull" : r.long_worth >= 40 ? "" : "muted") : "";
    // 突破状态（达瓦斯箱顶 + 量比）
    const bo = r.bo_state || "in_box";
    const vr = r.vol_ratio != null ? r.vol_ratio : null;
    const boCls = bo === "breakout" ? "bull" : bo === "stand_top" ? "bull2" : bo === "near" ? "" : "muted";
    const boTxt = wtBoStateLabel(bo) + (vr != null ? ` · 量比${vr}` : "");
    const boHot = (bo === "breakout" || (vr != null && vr >= 1.5)) ? " wt-hot" : "";
    // 质量标记
    const qf = r.quality_flag ? `<span class="wt-chip warn" title="质量过滤未通过项 / 数据缺失">${esc(r.quality_flag)}</span>` : `<span class="bull">✓</span>`;
    const pctCls = r.pct_all != null && r.pct_all <= 30 ? "bull" : "";
    const slopeCls = r.slope_yr_pct != null && Math.abs(r.slope_yr_pct) < 8 ? "muted" : "";
    const roeCls = r.roe != null ? (r.roe >= 10 ? "bull" : r.roe < 0 ? "bear" : "") : "";
    const drCls = r.dr != null && r.dr > 70 ? "bear" : "";
    const dyCls = r.dy != null && r.dy >= 2 ? "bull" : "";
    const scCls = r.score != null ? (r.score >= 70 ? "bull" : r.score >= 50 ? "" : "muted") : "";
    return `<tr>
      <td>${i + 1}</td>
      <td><a class="wt-code" data-wt-open="${esc(r.code)}">${esc(r.code)}</a><br><span class="muted">${esc(r.name || "")}</span>${r.avoid && r.avoid.level ? " " + avoidBadge(r.avoid) : ""}${r.lift && r.lift.level ? " " + liftBadge(r.lift, { watch: true }) : ""}</td>
      <td class="${scCls}"><b>${r.score != null ? r.score : "—"}</b>${(r.state_bonus ? `<br><span class="muted">+${r.state_bonus}国资</span>` : "")}</td>
      <td>${qf}</td>
      <td>${r.current != null ? r.current : "—"}</td>
      <td class="${pctCls}">${r.pct_all != null ? r.pct_all + "%" : "—"}</td>
      <td>${r.pct_3y != null ? r.pct_3y + "%" : "—"}</td>
      <td>${r.box_low != null ? r.box_low : "—"}</td>
      <td>${r.box_high != null ? r.box_high : "—"}</td>
      <td>${r.box_years != null ? r.box_years : "—"}</td>
      <td>${r.box_range_pct != null ? r.box_range_pct + "%" : "—"}</td>
      <td>${r.cv_pct != null ? r.cv_pct + "%" : "—"}</td>
      <td class="${slopeCls}">${r.slope_yr_pct != null ? r.slope_yr_pct + "%" : "—"}</td>
      <td class="${lwCls}" title="长窗worth·250/500/750日（30:40:30加权）"><span class="muted sm">250/500/750</span><br>${lw3}</td>
      <td>${r.pe != null ? r.pe : "—"}</td>
      <td class="${roeCls}">${r.roe != null ? r.roe + "%" : "—"}</td>
      <td class="${drCls}">${r.dr != null ? r.dr + "%" : "—"}</td>
      <td class="${dyCls}">${r.dy != null ? r.dy + "%" : "—"}</td>
      <td>${r.val_score != null ? r.val_score : "—"}</td>
      <td title="${esc(ah)}">${ah}${ahBadge}</td>
      <td>${govCell}</td>
      <td class="${boCls}${boHot}">${boTxt}</td>
      <td><button class="btn xs" data-lb-add="${esc(r.code)}">入池</button></td>
    </tr>`;
  }).join("");
  return `<div class="wt-tw"><table class="wt-tb lowbox-tb"><thead><tr>
    <th>#</th><th>代码/名称</th><th>综合分</th><th>质量</th><th>现价</th><th>上市分位</th><th>近3年分位</th>
    <th>箱底</th><th>箱顶</th><th>时长(年)</th><th>振幅</th><th>波动率</th><th>年化斜率</th>
    <th>长窗worth<br><span class="sm muted">250/500/750</span></th><th>PE</th><th>ROE</th><th>负债率</th>
    <th>股息率</th><th>估值分</th><th>实控人</th><th>国资系</th><th>突破状态</th><th>操作</th>
  </tr></thead><tbody>${trs}</tbody></table></div>`;
}

/* ================= 长期底部埋伏（2026-10-21 进哥需求）=================
   与「低位突破」的分工（别混）：
     低位突破 —— 近 1~8 年箱体 + 全历史分位，池子是关注池 ∪ 板块一Top30（至多 80 只）；
     长期底部埋伏 —— **全市场**扫描「长期底部平台」，挑以前出现过高点、现在在底线长期徘徊的埋伏票。
   底部基准是自适应的：压缩比(比自己历史同等时长更横盘) + 近一年止跌 + 底部抬升倍数，
   **不用全历史最低价** —— 进哥原话「早年 10 块的底不能拿去卡现在 20 块的底」。
   后端 /api/screener_bottom，算法 viewer/bottom.py，数据底座 data/kline_daily.db(前复权)。 */
const BOTTOM_TIERS = [[0, "全部"], [1, "≥1年"], [2, "≥2年"], [3, "≥3年"], [5, "≥5年"], [8, "≥8年"]];
async function loadBottom(refresh) {
  const st = FEAT.lowboxBottom;
  st.loading = true; st.error = null; renderBottom();
  try {
    const p = new URLSearchParams({
      min_years: String(st.minYears), state: st.state, pool: st.pool,
      codes: st.codes || "", limit: String(st.limit), refresh: refresh ? "1" : "0",
      include_st: st.includeST ? "1" : "0",
      prefix: st.prefix || "", min_score: String(st.minScore || 0), medal: st.medal || "all" });
    const res = await api(`/api/screener_bottom?${p.toString()}`);
    st.data = res; st.loading = false; renderBottom();
    // 后端首次扫描要 20~30s，走的是后台线程 → 这里每 3 秒来一次，最多 40 次（2 分钟）
    if (res && res.computing) {
      st.poll = (st.poll || 0) + 1;
      if (st.poll <= 40) {
        clearTimeout(st.pollTimer);
        st.pollTimer = setTimeout(() => {
          if (CURRENT && CURRENT.tab === "lowbox_bottom") loadBottom(false);
        }, 3000);
      }
    } else {
      st.poll = 0;
    }
  } catch (e) {
    st.loading = false; st.error = apiErrText(e); renderBottom();
    reportLoadError("长期底部埋伏", e);
  }
}
function renderBottom() {
  const host = $("#p-lowbox_bottom"); if (!host) return;
  const st = FEAT.lowboxBottom, d = st.data || {}, cnt = d.counts || {};
  const tierBtns = BOTTOM_TIERS.map(([v, l]) => {
    const n = v === 0 ? (d.total != null ? d.total : "") : (cnt[String(v)] != null ? cnt[String(v)] : "");
    const on = st.minYears === v;
    return `<button class="btn xs${on ? " primary" : ""}" data-bt-tier="${v}">${l}${n !== "" ? " " + n : ""}</button>`;
  }).join(" ");
  const ctl = `<div class="wt-ctl lowbox-ctl">
    <span class="wt-cl">底部时长</span>${tierBtns}
    <span class="wt-cl">状态</span>
    <select id="btState" class="wt-inp">
      ${[["all", "全部"], ["dormant", "仅埋伏中·未启动"], ["started", "仅刚启动·突破"]].map(([v, l]) =>
        `<option value="${v}"${st.state === v ? " selected" : ""}>${l}</option>`).join("")}
    </select>
    <span class="wt-cl">股票池</span>
    <select id="btPool" class="wt-inp">
      ${[["market", "全市场"], ["watch", "关注池"], ["custom", "自定义代码"]].map(([v, l]) =>
        `<option value="${v}"${st.pool === v ? " selected" : ""}>${l}</option>`).join("")}
    </select>
    ${st.pool === "custom" ? `<input id="btCodes" class="wt-inp" placeholder="代码逗号分隔" value="${escAttr(st.codes)}" style="width:220px">` : ""}
    <label class="wt-cl sm" style="cursor:pointer;user-select:none"><input type="checkbox" id="btST"${st.includeST ? " checked" : ""} style="margin-right:4px;vertical-align:-1px">含ST</label>
    <button class="btn sm primary" id="btGo"${st.loading ? " disabled" : ""}>${st.loading ? "扫描中…" : "重新扫描"}</button>
  </div>`;
  // 2026-10-21 进哥需求：奖牌 / 代码前缀 / 埋伏分 三组筛选（第二排，与底部时长并列）。
  // 数量由后端 medal_counts / prefix_counts 给（pool/状态/ST 之后统计），龙头分级未生成时数量留空、点金牌会提示稍后。
  const mc = d.medal_counts || {}, pc = d.prefix_counts || {};
  const medalBtns = [["all", "全部", ""], ["3", "金牌", mc["3"]], ["2", "银牌", mc["2"]], ["1", "铜牌", mc["1"]], ["0", "无牌", mc["0"]]]
    .map(([v, l, n]) => `<button class="btn xs${st.medal === v ? " primary" : ""}" data-bt-medal="${v}" title="行业龙头分级：金牌=行业综合排名前5%、银=前10%、铜=前15%（与代码旁徽章同一套口径）">${l}${n !== "" && n != null ? " " + n : ""}</button>`).join(" ");
  const prefixBtns = [["", "全部", ""], ["0", "0开头", pc["0"]], ["3", "3开头", pc["3"]], ["6", "6开头", pc["6"]]]
    .map(([v, l, n]) => `<button class="btn xs${(st.prefix || "") === v ? " primary" : ""}" data-bt-prefix="${v}">${l}${n != null && n !== "" ? " " + n : ""}</button>`).join(" ");
  const scoreBtns = [[0, "全部"], [60, "60分+"], [70, "70分+"], [80, "80分+"]]
    .map(([v, l]) => `<button class="btn xs${Number(st.minScore || 0) === v ? " primary" : ""}" data-bt-score="${v}">${l}</button>`).join(" ");
  const ctl2 = `<div class="wt-ctl lowbox-ctl">
    <span class="wt-cl">奖牌</span>${medalBtns}
    <span class="wt-cl">代码</span>${prefixBtns}
    <span class="wt-cl">埋伏分</span>${scoreBtns}
  </div>`;
  const hasRows = !!(d.rows && d.rows.length);
  let body;
  // 先给旧数据、后台再刷：只有「一条都还没有」时才显示扫描占位，否则照常出表
  if (d.computing && !hasRows) body = `<div class="hint wt-empty">正在后台扫描全市场前复权日K（约 20~30 秒）…页面每 3 秒自动刷新，不用手动重试。</div>`;
  else if (st.loading && !hasRows) body = `<div class="hint wt-empty">加载中…</div>`;
  else if (st.error && !d.rows) body = `<div class="muted wt-empty">加载失败：${esc(st.error)}</div>`;
  else if (!d.ok) body = `<div class="muted wt-empty">${esc(d.reason || "未取到数据")}</div>`;
  else body = bottomTable(d);
  const meta = d.ok ? `<div class="hint wt-foot">全市场通过 ${d.all_total || 0} 只 / 扫 ${d.scanned || 0} 只 · 扫描耗时 ${d.elapsed || 0}s · 数据时间 ${esc(d.ts || "")}${d.cached ? "（缓存）" : ""}${d.stale ? " · 后台刷新中，当前为上次结果" : ""} · ${esc((d.params || {}).note || "")}</div>` : "";
  host.innerHTML = `<div class="wt-head"><h2>长期底部埋伏 <span class="muted wt-sub">全市场 · 自适应底部平台 · 以前有过高峰、现在长期在底线徘徊</span></h2></div>
    ${ctl}${ctl2}${body}${meta}`;
  $$("#p-lowbox_bottom [data-bt-tier]").forEach((el) => {
    el.onclick = () => { st.minYears = Number(el.dataset.btTier) || 0; loadBottom(false); };
  });
  $$("#p-lowbox_bottom [data-bt-medal]").forEach((el) => {
    el.onclick = () => { st.medal = el.dataset.btMedal || "all"; loadBottom(false); };
  });
  $$("#p-lowbox_bottom [data-bt-prefix]").forEach((el) => {
    el.onclick = () => { st.prefix = el.dataset.btPrefix || ""; loadBottom(false); };
  });
  $$("#p-lowbox_bottom [data-bt-score]").forEach((el) => {
    el.onclick = () => { st.minScore = Number(el.dataset.btScore) || 0; loadBottom(false); };
  });
  const selS = $("#btState"); if (selS) selS.onchange = () => { st.state = selS.value; loadBottom(false); };
  const selP = $("#btPool"); if (selP) selP.onchange = () => { st.pool = selP.value; renderBottom(); };
  const cbST = $("#btST");
  if (cbST) cbST.onchange = () => { st.includeST = !!cbST.checked; loadBottom(false); };
  const go = $("#btGo");
  if (go) go.onclick = () => {
    const ci = $("#btCodes"); if (ci) st.codes = ci.value;
    loadBottom(true);
  };
  $$("#p-lowbox_bottom [data-bt-add]").forEach((el) => {
    el.onclick = () => openWatchAddModal(null, { symbol: el.dataset.btAdd });
  });
}
/* A股配色铁律：涨红跌绿，任何面板不得反用（2026-10-21 dy9d 教训，已写进回归断言）。 */
function btPctC(v) {
  if (v === null || v === undefined) return "color:#666";
  const n = Number(v);
  if (!isFinite(n)) return "color:#666";
  return n > 0 ? "color:#c0392b" : (n < 0 ? "color:#0a7d3b" : "color:#666");
}
function bottomTable(d) {
  const rows = d.rows || [];
  if (!rows.length) return `<div class="muted wt-empty">没有符合当前条件的标的（试试放宽底部时长 / 埋伏分，或把「奖牌」「代码」切回全部）。</div>`;
  const trs = rows.map((r, i) => {
    const stCls = r.state === "已启动·放量" ? "wt-chip hot" : r.state === "贴底埋伏" ? "wt-chip on" : "wt-chip";
    const pos = r.pos != null ? r.pos : 0;
    const posTxt = r.pos == null ? "—" : (r.pos > 1 ? `突破 +${Math.round((pos - 1) * 100)}%` : `${Math.round(pos * 100)}%`);
    const posCls = r.pos == null ? "" : (r.pos > 1 ? "bull" : (r.pos <= 0.3 ? "bull2" : ""));
    const lift = r.lift != null ? `${r.lift}×` : "—";
    const vr = r.vol_ratio != null ? `<br><span class="muted sm">量比 ${r.vol_ratio}</span>` : "";
    return `<tr>
      <td>${i + 1}</td>
      <td><a class="wt-code" data-wt-open="${esc(r.code)}">${esc(r.code)}</a><br><span class="muted">${esc(r.name || "")}</span></td>
      <td><b>${r.score != null ? r.score : "—"}</b></td>
      <td>${r.years != null ? r.years : "—"}</td>
      <td>${r.cur != null ? r.cur : "—"}</td>
      <td>${r.box_low != null ? r.box_low : "—"}</td>
      <td>${r.box_high != null ? r.box_high : "—"}</td>
      <td class="${posCls}">${posTxt}</td>
      <td class="muted">${r.width_pct != null ? r.width_pct + "%" : "—"}${r.compress != null ? `<br><span class="sm muted">压缩 ${r.compress}</span>` : ""}</td>
      <td style="${btPctC(r.dd)}">${r.dd != null ? r.dd + "%" : "—"}</td>
      <td>${r.peak_ago != null ? r.peak_ago + "年" : "—"}</td>
      <td title="当前平台底 ÷ 全历史5%分位价（进哥说的『底从10抬到20』就是这里）">${lift}</td>
      <td>${r.slope != null ? r.slope + "%" : "—"}</td>
      <td style="${btPctC(r.slope_1y)}">${r.slope_1y != null ? r.slope_1y + "%" : "—"}</td>
      <td class="muted">${r.cv != null ? r.cv + "%" : "—"}</td>
      <td><span class="${stCls}">${esc(r.state || "")}</span>${vr}</td>
      <td><button class="btn xs" data-bt-add="${esc(r.code)}">入池</button></td>
    </tr>`;
  }).join("");
  return `<div class="wt-tw"><table class="wt-tb lowbox-tb"><thead><tr>
    <th>#</th><th>代码/名称</th><th>埋伏分</th><th>底部时长<br><span class="sm muted">年</span></th><th>现价</th>
    <th>平台底</th><th>平台顶</th><th>箱内位置</th><th>箱体宽度</th>
    <th>距峰值</th><th>峰值距今</th><th>底部抬升</th><th>年化斜率</th><th>近1年</th>
    <th>波动率</th><th>状态</th><th>操作</th>
  </tr></thead><tbody>${trs}</tbody></table></div>`;
}

/* ================= 突破雷达（2026-10-13 进哥「一次性全做」）=================
   复评最近一次「长期低位」扫描池（data/lowbox_scan_cache.json）：放量突破(量比≥1.5) / 回踩确认。
   进入即拉；每 5 分钟自动复评（离开标签停）；触发事件落日志 + 推关注池未读提醒。 */
async function loadLowboxRadar(silent) {
  const st = FEAT.lowbox;
  if (!silent) { st.radarLoading = true; st.radarError = null; renderLowboxRadar(); }
  try {
    const res = await api(`/api/lowbox_breakouts`);
    st.radarData = res; st.radarLoading = false; st.radarError = null; renderLowboxRadar();
  } catch (e) {
    st.radarLoading = false; st.radarError = apiErrText(e); renderLowboxRadar(); reportLoadError("突破雷达", e);
  }
}
function renderLowboxRadar() {
  const host = $("#p-lowbox_radar"); if (!host) return;
  const st = FEAT.lowbox;
  const d = st.radarData || {};
  const ctl = `<div class="wt-ctl lowbox-radar-ctl">
    <span class="wt-cl">达瓦斯突破雷达</span>
    <span class="wt-cl sm">口径：收盘站上箱顶 且 量比≥1.5 = 放量突破；近15日突破过、现价回到箱顶±3% = 回踩确认</span>
    <button class="btn sm primary" id="lbRadarGo"${st.radarLoading ? " disabled" : ""}>${st.radarLoading ? "复评中…" : "重新评估"}</button>
  </div>`;
  let body;
  if (st.radarLoading) body = `<div class="hint wt-empty">复评中…逐只复拉近130日K（最多80只），<b>约 10~30 秒</b>。</div>`;
  else if (st.radarError && !d.rows) body = `<div class="muted wt-empty">加载失败：${esc(st.radarError)}</div>`;
  else if (!d.ok) body = `<div class="muted wt-empty">${esc(d.reason || "未取到数据")}。请先在「长期低位」跑一次扫描，雷达才有池子可评。</div>`;
  else body = lowboxRadarTable(d);
  const evN = (d.events || []).length;
  const pushed = d.pushed_n || 0;
  const msg = d.ts ? `<div class="hint wt-foot">最近复评 ${esc(d.ts)} · 历史事件 ${evN} 条 · 本次新增提醒 ${pushed} 条（突破/回踩已推入「我的票·到价提醒」）。</div>` : "";
  host.innerHTML = `<div class="wt-head"><h2>突破雷达 <span class="muted wt-sub">达瓦斯箱体(本土化) + 战法库 box_breakout/retest</span></h2></div>
    ${ctl}${body}${msg}`;
  const go = $("#lbRadarGo");
  if (go) go.onclick = () => loadLowboxRadar(false);
}
function lowboxRadarTable(d) {
  const rows = d.rows || [];
  if (!rows.length) return `<div class="muted wt-empty">扫描池为空（请先在「长期低位」扫描）。</div>`;
  const trs = rows.map((r) => {
    const bo = r.state || "in_box";
    const boCls = bo === "breakout" ? "bull" : bo === "stand_top" ? "bull2" : bo === "near" ? "" : "muted";
    const boHot = (bo === "breakout" || bo === "retest") ? " wt-hot" : "";
    const vr = r.vol_ratio != null ? r.vol_ratio : null;
    const bp = r.breakout_pct != null ? r.breakout_pct : null;
    return `<tr class="${boHot}">
      <td><a class="wt-code" data-wt-open="${esc(r.code)}">${esc(r.code)}</a><br><span class="muted">${esc(r.name || "")}</span></td>
      <td class="${boCls}"><b>${wtBoStateLabel(bo)}</b></td>
      <td>${r.cur != null ? r.cur : "—"}</td>
      <td>${r.box_high != null ? r.box_high : "—"}</td>
      <td>${vr != null ? vr : "—"}</td>
      <td>${bp != null ? (bp >= 0 ? "+" : "") + bp + "%" : "—"}</td>
    </tr>`;
  }).join("");
  return `<div class="wt-tw"><table class="wt-tb lowbox-radar-tb"><thead><tr>
    <th>代码/名称</th><th>状态</th><th>现价</th><th>箱顶</th><th>量比</th><th>距箱顶</th>
  </tr></thead><tbody>${trs}</tbody></table></div>`;
}

async function loadRateTimeline() {
  const st = FEAT.rate;
  st.tlLoading = true; renderRate();
  let tl;
  try {
    tl = await api(`/api/rate/timeline?days_window=${st.tlWindow}&per_day=${st.tlPer}`);
  } catch (e) {
    tl = { ok: false, reason: apiErrText(e) };
  }
  if (tl && tl.has_snap && tl.days && tl.days.length && st.open[tl.days[0].snap_date] == null) {
    st.open[tl.days[0].snap_date] = true;    // 默认展开最近一个归档日
  }
  st.tl = tl;
  st.tlLoading = false;
  renderRate();   // 先落一版（「正在生成 N/3」的提示就靠这一版）
  // 空态自愈（2026-10-05）：刚跑过榜/归档、但归档还没落库时，has_snap=false 会卡住页面让人干等。
  // 这里只在本会话刚发起过任务（tlBusyUntil 未过期）时自动重试，最多 3 次、间隔 8 秒，
  // 别把「压根没跑过榜」也拖成刷接口的空转。
  const busy = Date.now() < (st.tlBusyUntil || 0);
  if (tl && !tl.has_snap && busy && st.tlPoll < st.tlPollMax) {
    st.tlPoll += 1;
    clearTimeout(st.tlPollTimer);
    st.tlPollTimer = setTimeout(() => loadRateTimeline(), 8000);
  } else if (!(tl && !tl.has_snap && busy)) {
    st.tlPoll = 0;    // 热门路径（有归档 / 没发起过任务 / 重试次数用尽）：清掉计数，下次别误报「生成中」
  }
}
// 标记「正在生成归档」的窗口（点跑榜/归档今天时调用）。2 分钟足够归档落库，超时就当没在跑。
function rateMarkBusy(ms) {
  const st = FEAT.rate;
  st.tlBusyUntil = Date.now() + (ms || 120000);
  st.tlPoll = 0;
}

/* ================= 智能选股增强（2026-10-07，B1 策略保存 / B2 行业热力图 / B3 CSV+列自定义）=================
   纯前端实现，零后端改动、零付费调用、完全向后兼容。所有状态存 localStorage 或 FEAT.screener，不碰既有筛选逻辑。 */

/* 多条件组合视图的列定义：用于「列设置」勾选与 CSV 导出；k 与表格 data-col 一致。
   fmt 走与界面相同的渲染（wtMoney/wtPct/featMoney），保证导出内容与界面口径一致。 */
const SC_COLS = [
  { k: "code",      label: "代码",   get: (r) => r.code },
  { k: "name",      label: "名称",   get: (r) => r.name },
  { k: "industry",  label: "行业",   get: (r) => r.industry },
  { k: "pe",        label: "PE",     get: (r) => r.pe,        fmt: wtMoney },
  { k: "pb",        label: "PB",     get: (r) => r.pb,        fmt: wtMoney },
  { k: "ps",        label: "PS",     get: (r) => r.ps,        fmt: wtMoney },
  { k: "peg",       label: "PEG",    get: (r) => r.peg,       fmt: wtMoney },
  { k: "pcf",       label: "现金流", get: (r) => r.pcf,       fmt: wtMoney },
  { k: "mcap",      label: "总市值", get: (r) => r.mcap,      fmt: featMoney },
  { k: "pct",       label: "涨跌",   get: (r) => r.pct,       fmt: wtPct },
  { k: "holder_num",label: "股东户数", get: (r) => r.holder_num, fmt: (v) => (v == null ? "—" : (v / 10000).toFixed(2) + " 万户") },
  { k: "worth",     label: "值得率", get: (r) => r.worth,     fmt: (v) => (v == null ? "—" : Math.round(v)) },
  { k: "roe",       label: "ROE",    get: (r) => r.roe,       fmt: (v) => (v == null ? "—" : Number(v).toFixed(2) + "%") },
  { k: "gm",        label: "毛利率", get: (r) => r.gm,         fmt: (v) => (v == null ? "—" : Number(v).toFixed(2) + "%") },
  { k: "dr",        label: "负债率", get: (r) => r.dr,         fmt: (v) => (v == null ? "—" : Number(v).toFixed(2) + "%") },
  { k: "dy",        label: "股息率", get: (r) => r.dy,         fmt: (v) => (v == null ? "—" : Number(v).toFixed(2) + "%") },
];
const SC_LS_STRAT = "wb_sc_strategies_v1";

/* 当前视图下实际展示的个股结果行（多条件组合 / 连续阴跌 / Top30 三种来源统一出口）。
   2026-10-07 王总 B2：叠加行业筛选（点热力图行业块后只留该行业）。 */
function scCurrentRows(st) {
  let rows = [];
  if (st.screen === "decline") {
    const dc = st.decline;
    if (dc.data && dc.data.rows && dc.data.rows.length) rows = dc.data.rows;
  } else if (st.screen === "boards") {
    return [];   // 板块分类视图是板块级，无个股行业行
  } else if (st.data && st.data.rows) {
    rows = st.data.rows;
  }
  if (st.industryFilter && rows.length)
    rows = rows.filter((r) => r && r.industry === st.industryFilter);
  return rows;
}

/* ---------- B1 策略模板：保存 / 加载 / 删除 + 内置预设 ---------- */
function scStrategiesLoad() { try { return JSON.parse(localStorage.getItem(SC_LS_STRAT) || "[]"); } catch (e) { return []; } }
function scStrategiesSave(arr) { try { localStorage.setItem(SC_LS_STRAT, JSON.stringify(arr)); } catch (e) {} }
function scPresets() {
  return [
    { name: "低估值白马",   screen: "",      screens: ["pe", "pb", "ps", "roe", "gm"], thr: { pe: 20, pb: 3, ps: 2, roe: 15, gm: 30 } },
    { name: "高股息防御",   screen: "",      screens: ["dy", "pe"],                   thr: { dy: 3, pe: 15 } },
    { name: "低筹码集中",   screen: "",      screens: ["holders"],                   thr: { holders: 1 } },
    { name: "高值得率反转", screen: "",      screens: ["worth"],                     thr: { worth: 70 } },
    { name: "连续阴跌掘金", screen: "decline", screens: [],                         thr: {}, decline: { days: 30, down: 25, sort: "score" } },
  ];
}
function scApplyStrategy(s) {
  const st = FEAT.screener;
  st.screen = s.screen || "";
  st.screens = (s.screens || ["pe"]).slice();
  st.thr = Object.assign({}, s.thr || {});
  st.industryFilter = null;
  if (s.screen === "decline") {
    st.decline.days = (s.decline && s.decline.days) || 30;
    st.decline.down = (s.decline && s.decline.down) || 25;
    st.decline.sort = (s.decline && s.decline.sort) || "score";
    loadScreenerDecline();
  } else if (s.screen === "boards") {
    loadScreenerBoards(true);
  } else {
    loadScreener();
  }
}
function scSaveCurrentStrategy(name) {
  name = (name || "").trim();
  if (!name) { scHint("请先输入策略名。"); return; }
  const st = FEAT.screener;
  const s = { name, screen: st.screen || "", screens: st.screens.slice(),
    thr: Object.assign({}, st.thr || {}), decline: { days: st.decline.days, down: st.decline.down, sort: st.decline.sort } };
  const arr = scStrategiesLoad().filter((x) => x.name !== name);
  arr.push(s); scStrategiesSave(arr);
  scHint("已保存策略「" + name + "」");
  renderScreener();
}
function scDeleteStrategy(name) {
  if (!name) { scHint("先在「我的策略」里选一条再删除。"); return; }
  const arr = scStrategiesLoad().filter((x) => x.name !== name);
  scStrategiesSave(arr); scHint("已删除策略「" + name + "」");
  renderScreener();
}
function scHint(msg) { const el = $("#scHint"); if (el) { el.textContent = msg; el.style.opacity = "1";
  clearTimeout(el._t); el._t = setTimeout(() => { el.style.opacity = "0"; }, 3200); } }

/* ---------- B2 行业热力图（Squarified Treemap，纯 SVG，无图表库依赖）---------- */
function scTreemap(items, W, H) {
  // items: [{v:number>0, it}]  ->  [{x,y,w,h,it}]
  const total = items.reduce((s, i) => s + i.v, 0) || 1;
  const els = items.map((i) => ({ a: i.v / total * W * H, it: i.it }));
  const out = [];
  (function rec(row, x, y, w, h) {
    if (!row.length) return;
    if (row.length === 1) { out.push({ x, y, w, h, it: row[0].it }); return; }
    const side = Math.min(w, h);
    const worst = (rr) => { const s = rr.reduce((a, b) => a + b.a, 0); const mx = Math.max.apply(null, rr.map((r) => r.a));
      const mn = Math.min.apply(null, rr.map((r) => r.a)); const s2 = s * s, sd = side * side;
      return Math.max(sd * mx / s2, s2 / (sd * mn)); };
    let cur = [row[0]], rest = row.slice(1);
    while (rest.length && worst(cur.concat([rest[0]])) <= worst(cur)) { cur = cur.concat([rest[0]]); rest = rest.slice(1); }
    const sum = cur.reduce((a, b) => a + b.a, 0);
    if (w >= h) { const rw = sum / h; let yy = y; cur.forEach((r) => { const rh = r.a / sum * h; out.push({ x, y: yy, w: rw, h: rh, it: r.it }); yy += rh; }); rec(rest, x + rw, y, w - rw, h); }
    else { const rh = sum / w; let xx = x; cur.forEach((r) => { const rw2 = r.a / sum * w; out.push({ x: xx, y, w: rw2, h: rh, it: r.it }); xx += rw2; }); rec(rest, x, y + rh, w, h - rh); }
  })(els, 0, 0, W, H);
  return out;
}
function scHeatmapHtml(st) {
  const rows = scCurrentRows(st).filter((r) => r && r.industry);
  if (!rows.length)
    return `<div class="hint wt-empty">当前视图没有可聚合的「行业」数据（板块分类视图不含个股行业字段）。切到「多条件组合」或「连续阴跌」后即可看到行业热力图。</div>`;
  const m = {};
  rows.forEach((r) => { const k = r.industry; if (!m[k]) m[k] = { n: 0, sp: 0, sw: 0, wc: 0 };
    const o = m[k]; o.n++; o.sp += (r.pct != null ? r.pct : (r.last_pct != null ? r.last_pct : 0)); if (r.worth != null) { o.sw += r.worth; o.wc++; } });
  const items = Object.keys(m).map((k) => { const o = m[k];
    return { name: k, n: o.n, avgPct: o.sp / o.n, avgWorth: o.wc ? o.sw / o.wc : null, it: null }; });
  items.forEach((it) => { it.it = it; });
  items.sort((a, b) => b.n - a.n);
  const W = 960, H = 460;
  const cells = scTreemap(items.map((it) => ({ v: it.n, it })), W, H);
  const rects = cells.map((c) => {
    const it = c.it;
    const pctColor = it.avgPct >= 0 ? "var(--bull, #c23b3b)" : "var(--bear, #1f8a4c)";
    const pctBg = it.avgPct >= 0 ? "rgba(194,59,59,0.14)" : "rgba(31,138,76,0.14)";
    let accent = pctBg;
    if (it.avgWorth != null) { const t = Math.max(0, Math.min(1, it.avgWorth / 100)); accent = "rgba(14,116,144," + (0.10 + 0.55 * t).toFixed(3) + ")"; }
    const fs = Math.max(11, Math.min(20, 11 + Math.log2(it.n) * 3));
    const pctTxt = fmtPct(it.avgPct);
    return `<g><rect x="${c.x}" y="${c.y}" width="${c.w - 2}" height="${c.h - 2}" rx="4" fill="${accent}" stroke="${pctColor}" stroke-width="1.5"/>` +
      `<text x="${c.x + 7}" y="${c.y + fs + 2}" font-size="${fs}" font-weight="700" fill="var(--text)">${esc(it.name)}</text>` +
      `<text x="${c.x + 7}" y="${c.y + fs + 18}" font-size="11" fill="var(--muted)">${it.n}只 · ${pctTxt}</text>` +
      (it.avgWorth != null ? `<text x="${c.x + 7}" y="${c.y + fs + 33}" font-size="11" fill="var(--accent, #0e7490)">值得率 ${Math.round(it.avgWorth)}</text>` : "") +
      `<rect x="${c.x}" y="${c.y}" width="${c.w - 2}" height="${c.h - 2}" fill="transparent" style="cursor:pointer" data-sc-ind="${escAttr(it.name)}"/></g>`;
  }).join("");
  return `<div class="sc-hm">
    <div class="sc-hm-bar"><span class="muted">行业热力图（Treemap）：块面积=该行业命中股票数，颜色=平均涨跌（红涨绿跌），深青越深=平均入手值得率越高。点行业块可筛选下方表格。</span>
      ${st.industryFilter ? `<button class="btn sm" id="scHmClear">清除「${esc(st.industryFilter)}」筛选</button>` : ""}</div>
    <svg viewBox="0 0 ${W} ${H}" class="sc-hm-svg" preserveAspectRatio="xMidYMid meet">${rects}</svg>
  </div>`;
}

/* ---------- B3 CSV 导出 + 列自定义 ---------- */
function scCsvCell(s) { s = String(s == null ? "" : s).replace(/\s+/g, " "); if (/[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"'; return s; }
function scViewCols(st) {
  if (st.screen === "decline") {
    const dc = st.decline;
    if (dc.mode === "top") return [
      { label: "代码", get: (r) => r.code }, { label: "名称", get: (r) => r.name },
      { label: "现价", get: (r) => r.price, fmt: wtMoney },
      { label: "当日涨跌", get: (r) => r.last_pct, fmt: wtPct },
      { label: "换手率(今/5均)", get: (r) => (r.to_today == null ? "" : r.to_today.toFixed(2) + "%" + (r.to_avg5 == null ? "" : "/" + r.to_avg5.toFixed(2) + "%")) },
      { label: "位置分位", get: (r) => r.pos_pct == null ? "" : r.pos_pct.toFixed(0) + "%" },
      { label: "入手值得率", get: (r) => r.score == null ? "" : r.score },
      { label: "提示", get: (r) => r.hint || "" },
    ];
    return [
      { label: "代码", get: (r) => r.code }, { label: "名称", get: (r) => r.name },
      { label: "现价", get: (r) => r.price, fmt: wtMoney },
      { label: "下跌天数", get: (r) => r.down_days + "/" + r.days },
      { label: "累计跌幅", get: (r) => r.cum_pct, fmt: wtPct },
      { label: "日均跌幅", get: (r) => r.avg_dn_pct, fmt: wtPct },
      { label: "最长连跌", get: (r) => r.max_streak + "天" },
      { label: "当日涨跌", get: (r) => r.last_pct, fmt: wtPct },
      { label: "换手率(今)", get: (r) => (r.to_today == null ? "" : r.to_today.toFixed(2) + "%") },
      { label: "入手值得率", get: (r) => r.score == null ? "" : r.score },
      { label: "提示", get: (r) => r.hint || "" },
    ];
  }
  return SC_COLS;
}
function scExportCsv(st) {
  const rows = scCurrentRows(st);
  if (!rows.length) { scHint("当前没有可导出的结果。"); return; }
  let cols = scViewCols(st);
  // 多条件组合视图：套用列显隐设置（无 k 的列如阴跌视图始终导出）
  if (st.colSel) cols = cols.filter((c) => c.k == null || st.colSel[c.k] !== false);
  const header = cols.map((c) => scCsvCell(c.label)).join(",");
  const lines = rows.map((r) => cols.map((c) => {
    let v = c.get(r);
    if (v == null) return "";
    if (c.fmt) return scCsvCell(c.fmt(v));
    return scCsvCell(v);
  }).join(","));
  const csv = "﻿" + header + "\n" + lines.join("\n");
  try {
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = "智能选股_" + (st.screen || "组合") + "_" + new Date().toISOString().slice(0, 10) + ".csv";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    scHint("已导出 " + rows.length + " 行 CSV。");
  } catch (e) { scHint("导出失败：" + (e && e.message || e)); }
}
function scApplyColClasses(st, host) {
  if (!host) return;
  // 只作用于表格单元格（th/td）；工具栏里的列设置勾选框也带 data-sc-col，
  // 若一起处理会导致「取消勾选 → 勾选框自己被隐藏 → 再也勾不回来」。
  SC_COLS.forEach((c) => {
    const hide = !!(st.colSel && st.colSel[c.k] === false);
    const els = host.querySelectorAll('table [data-sc-col="' + c.k + '"]');
    els.forEach((el) => { el.style.display = hide ? "none" : ""; });
  });
}

/* 智能选股工具栏（筛选条下方新增一行）：B1 策略 + B2 可视化 + B3 导出/列设置 */
function scToolsHtml(st) {
  const mine = scStrategiesLoad();
  const mineOpts = mine.length ? mine.map((s) => `<option value="${escAttr(s.name)}">${esc(s.name)}</option>`).join("")
                                : `<option value="">（暂无保存的策略）</option>`;
  const presetOpts = scPresets().map((s) => `<option value="${escAttr(s.name)}">${esc(s.name)}</option>`).join("");
  const colChks = SC_COLS.map((c) => `<label class="wt-chk"><input type="checkbox" data-sc-col="${c.k}" ${st.colSel && st.colSel[c.k] === false ? "" : "checked"}>${esc(c.label)}</label>`).join("");
  return `<div class="sc-tools">
    <div class="sc-tools-l">
      <input id="scStratName" class="sc-in2" placeholder="给当前条件起个名…" />
      <button class="btn sm" id="scSaveStrat">保存为策略</button>
      <select id="scMyStrats" class="sc-sel"><option value="">我的策略 ▾</option>${mineOpts}</select>
      <button class="btn sm" id="scDelStrat" title="删除选中的「我的策略」">删除</button>
      <select id="scPresets" class="sc-sel"><option value="">内置策略 ▾</option>${presetOpts}</select>
      ${st.industryFilter ? `<button class="btn sm warn" id="scIndClear" title="清除行业筛选，恢复全部命中">✕ 行业：${esc(st.industryFilter)}</button>` : ""}
    </div>
    <div class="sc-tools-r">
      ${st.screen !== "boards" ? `<button class="btn sm${st.visualMode === "heatmap" ? " active" : ""}" id="scToggleViz">${st.visualMode === "heatmap" ? "返回表格" : "行业热力图"}</button>` : ""}
      <button class="btn sm" id="scExportCsv">导出 CSV</button>
      <button class="btn sm" id="scColSet">列设置 ▾</button>
      <div class="sc-colset" id="scColSetPanel" style="display:none">${colChks}</div>
      <span id="scHint" class="sc-hint-msg muted" style="opacity:0"></span>
    </div>
  </div>`;
}

function renderScreener() {
  const host = $("#p-screener"); if (!host) return;
  const st = FEAT.screener;
  // 2026-10-07 王总：筛选条件分两行分组（估值/规模 + 筹码 + 质量），每个 chip 内联可调阈值输入框
  const groupsHtml = SCREENER_GROUPS.map((g) => `
    <div class="sc-group">
      <div class="sc-gtitle">${esc(g.title)}</div>
      <div class="sc-chips">
        ${g.items.map((it) => {
          const on = st.screens.includes(it.k);
          const val = (st.thr && st.thr[it.k] != null) ? st.thr[it.k] : it.def;
          return `<div class="sc-chip${on ? " on" : ""}" data-sc-key="${it.k}" title="点击启用/停用；在输入框里改阈值（建议值：${esc(it.nm)}${esc(it.op)}${esc(String(it.def))}${esc(it.unit)}）">
            <span class="sc-nm">${esc(it.nm)}</span><span class="sc-op">${esc(it.op)}</span>
            <input class="sc-in" type="number" step="any" value="${esc(String(val))}" data-sc-thr="${it.k}">
            <span class="sc-u">${esc(it.unit)}</span>
          </div>`;
        }).join("")}
      </div>
    </div>`).join("");
  const exclTabs = `<a class="wt-tab${st.screen === "boards" ? " active" : ""}" data-feat-screen="boards">板块分类</a>
    <a class="wt-tab${st.screen === "decline" ? " active" : ""}" data-feat-screen="decline">连续阴跌</a>`;
  const tabs = `<div class="sc-bar"><div class="sc-groups">${groupsHtml}</div><div class="sc-excl">${exclTabs}</div></div>`;
  let body = "";
  let desc = "";
  // 2026-10-07 王总 B2：热力图视图（板块分类视图无个股行业行，跳过）
  if (st.visualMode === "heatmap" && st.screen !== "boards") body = scHeatmapHtml(st);
  else if (st.screen === "boards") body = screenerBoardsBody(st);
  else if (st.screen === "decline") body = screenerDeclineBody(st);
  else {
    const d = st.data || {};
    desc = d.desc || "";
    if (d.loadingHolder) {
      body = d.loadingWorth
        ? `<div class="hint wt-empty">正在按多条件组合筛选（含入手值得率，当日首跑需建全市场快照+评分预热，约 10~30 秒；之后每种组合都是秒出，点过的组合切回即现）…</div>`
        : `<div class="hint wt-empty">正在按多条件组合筛选（当日首跑需建全市场快照，约 10~30 秒；之后每种组合都是秒出，点过的组合切回即现）…</div>`;
    } else {
    const list = scCurrentRows(st);
    const hasHolder = list.some((r) => r.holder_num != null);
    const hasWorthCol = list.some((r) => r.worth != null);
    const qualKeys = SCREENER_QUAL_KEYS.filter((k) => st.screens.includes(k));
    const hasQualData = list.some((r) => r.roe != null || r.gm != null || r.dr != null || r.dy != null);
    const qualCols = hasQualData ? qualKeys : [];
    const rows = list.map((r, i) => `<tr>
      <td class="wt-c1">${i + 1}</td>
      <td data-sc-col="code"><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
      <td data-sc-col="name">${esc(r.name || "")}</td><td data-sc-col="industry">${esc(r.industry || "")}</td>
      <td data-sc-col="pe">${wtMoney(r.pe)}</td><td data-sc-col="pb">${wtMoney(r.pb)}</td><td data-sc-col="ps">${wtMoney(r.ps)}</td>
      <td data-sc-col="peg">${wtMoney(r.peg)}</td><td data-sc-col="pcf">${wtMoney(r.pcf)}</td>
      <td data-sc-col="mcap">${featMoney(r.mcap)}</td><td data-sc-col="pct">${wtPct(r.pct)}</td>
      ${hasHolder ? `<td data-sc-col="holder_num" title="披露日：${esc(r.holder_end || "—")}（各公司披露日不同，滞后 1~3 个月）">${r.holder_num != null ? (r.holder_num / 10000).toFixed(2) + " 万户" : "—"}</td>` : ""}
      ${hasWorthCol ? `<td data-sc-col="worth" title="入手值得率 0~100：换手率五维技术面评分（口径同「入手概率」页），越高越值得关注">${r.worth != null ? Math.round(r.worth) : "—"}</td>` : ""}
      ${qualCols.map((k) => `<td data-sc-col="${k}">${scQualCell(r, k)}</td>`).join("")}
    </tr>`).join("") ||
      `<tr><td colspan="${11 + (hasHolder ? 1 : 0) + (hasWorthCol ? 1 : 0) + qualCols.length}" class="muted wt-empty">${d.ok === false ? "筛选未返回结果：" + esc(d.reason || "未取到数据") : "暂无符合项。"}</td></tr>`;
    body = `    <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>#</th><th data-sc-col="code">代码</th><th data-sc-col="name">名称</th><th data-sc-col="industry">行业</th><th data-sc-col="pe">PE</th><th data-sc-col="pb">PB</th><th data-sc-col="ps">PS</th>
      <th data-sc-col="peg">PEG</th><th data-sc-col="pcf">现金流</th><th data-sc-col="mcap">总市值</th><th data-sc-col="pct">涨跌</th>${hasHolder ? `<th data-sc-col="holder_num">股东户数</th>` : ""}${hasWorthCol ? `<th data-sc-col="worth">值得率</th>` : ""}${qualCols.map((k) => `<th data-sc-col="${k}">${SCREENER_QUAL_NAME[k]}</th>`).join("")}
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="hint wt-foot">预设策略基于东财「价值分析」报表（PE/PB/PS/PEG/现金流/市值），口径透明可复现；
    交易日：${esc(d.trade_date || "—")}。${hasHolder ? `持股集中度=各公司最新披露的股东户数（阈值可调，默认非常集中&lt;1万户），披露日悬停可见，滞后 1~3 个月。` : ""}
    ${hasWorthCol ? `值得率=换手率五维技术面评分（0~100，越高越值得关注，口径同「入手概率」页）；评分缺失（日 K/换手数据不足）的票视为不满足该条件、已剔除。` : ""}
    ${qualKeys.length ? `质量指标（ROE/毛利率/负债率/股息率，巴菲特口径）当前数据源受限，启用后上述质量列自动填充真实值。` : ""}</div>`;
    }
  }
  host.innerHTML = `
    <div class="wt-head"><h2>智能选股 <span class="muted wt-sub">${esc(desc)}</span></h2>
      <div class="wt-actions"><button class="btn sm" id="featScRefresh">刷新</button></div></div>
    <div class="wt-tabs">${tabs}</div>
    ${scToolsHtml(st)}
    ${body}`;
  scApplyColClasses(st, host);
  const rf = $("#featScRefresh"); if (rf) rf.onclick = () => loadScreener();
  // 2026-10-07 分组 chips：点 chip=切换选中（AND 叠加，再点取消）；chip 内输入框改阈值=更新并即时重查
  $$("#p-screener [data-sc-key]").forEach((el) =>
    el.onclick = (e) => {
      if (e.target && e.target.classList && e.target.classList.contains("sc-in")) return; // 改阈值不触发切换
      const st = FEAT.screener, k = el.dataset.scKey;
      const i = st.screens.indexOf(k);
      if (i >= 0) { if (st.screens.length > 1) st.screens.splice(i, 1); }
      else st.screens.push(k);
      loadScreener();
    });
  $$("#p-screener [data-sc-thr]").forEach((el) => {
    el.onclick = (e) => e.stopPropagation();   // 点输入框不触发 chip 的切换
    el.onchange = (e) => {
      const st = FEAT.screener; if (!st.thr) st.thr = {};
      const v = parseFloat(e.target.value);
      st.thr[el.dataset.scThr] = isNaN(v) ? null : v;
      loadScreener();
    };
  });
  // 板块分类 / 连续阴跌（独占视图，离开组合模式，组合选择保留、切回来还在）
  $$("#p-screener [data-feat-screen]").forEach((el) =>
    el.onclick = () => { const st = FEAT.screener; st.screen = el.dataset.featScreen; loadScreener(); });
  // 板块分类交互
  $$("#p-screener [data-sc-bkind]").forEach((el) =>
    el.onclick = () => { const s = FEAT.screener; s.boardKind = el.dataset.scBkind;
      s.boards = null; s.boardSel = null; s.boardMembers = null; loadScreenerBoards(true); });
  $$("#p-screener [data-sc-board]").forEach((el) =>
    el.onclick = () => loadScreenerBoardMembers(el.dataset.scBoard, el.dataset.scName));
  const back = $("#scBoardBack");
  if (back) back.onclick = () => { FEAT.screener.boardSel = null; FEAT.screener.boardMembers = null; renderScreener(); };
  $$("#p-screener [data-sc-bsort]").forEach((el) =>
    el.onclick = () => { FEAT.screener.boardSort = el.dataset.scBsort; renderScreener(); });
  // 连续阴跌交互
  const go = $("#scDnGo");
  if (go) go.onclick = () => {
    const dv = parseInt(($("#scDnDays") || {}).value, 10) || 30;
    const nv = parseInt(($("#scDnDown") || {}).value, 10) || 25;
    loadScreenerDecline(dv, Math.min(nv, dv));
  };
  const goTop = $("#scDnTop");
  if (goTop) goTop.onclick = () => loadScreenerTop();
  $$("#p-screener [data-sc-dn]").forEach((el) =>
    el.onclick = () => { const p = String(el.dataset.scDn || "").split(",");
      loadScreenerDecline(p[0], p[1]); });
  $$("#p-screener [data-sc-dsort]").forEach((el) =>
    el.onclick = () => loadScreenerDecline(null, null, el.dataset.scDsort));
  $$("#p-screener [data-wt-open]").forEach((el) =>
    el.onclick = () => openStock(el.dataset.wtOpen, el));
  // 2026-10-07 王总 B1/B2/B3：智能选股增强工具栏交互
  const scSave = $("#scSaveStrat");
  if (scSave) scSave.onclick = () => { const nm = ($("#scStratName") || {}).value; scSaveCurrentStrategy(nm); };
  const scDel = $("#scDelStrat");
  if (scDel) scDel.onclick = () => { const s = ($("#scMyStrats") || {}).value; scDeleteStrategy(s); };
  const scMine = $("#scMyStrats");
  if (scMine) scMine.onchange = () => { const s = scStrategiesLoad().find((x) => x.name === scMine.value); if (s) scApplyStrategy(s); };
  const scPre = $("#scPresets");
  if (scPre) scPre.onchange = () => { const s = scPresets().find((x) => x.name === scPre.value); if (s) { scApplyStrategy(s); scPre.value = ""; } };
  const scViz = $("#scToggleViz");
  if (scViz) scViz.onclick = () => { const st = FEAT.screener; st.visualMode = st.visualMode === "heatmap" ? "table" : "heatmap"; renderScreener(); };
  const scCsv = $("#scExportCsv");
  if (scCsv) scCsv.onclick = () => scExportCsv(FEAT.screener);
  const scCol = $("#scColSet");
  if (scCol) scCol.onclick = () => { const p = $("#scColSetPanel"); if (p) p.style.display = p.style.display === "none" ? "block" : "none"; };
  const scIndClr = $("#scIndClear");
  if (scIndClr) scIndClr.onclick = () => { FEAT.screener.industryFilter = null; renderScreener(); };
  const scHmClr = $("#scHmClear");
  if (scHmClr) scHmClr.onclick = () => { FEAT.screener.industryFilter = null; renderScreener(); };
  $$("#p-screener [data-sc-col]").forEach((el) =>
    el.onchange = () => { const st = FEAT.screener; if (!st.colSel) st.colSel = {}; st.colSel[el.dataset.scCol] = el.checked; scApplyColClasses(st, host); });
  $$("#p-screener [data-sc-ind]").forEach((el) =>
    el.onclick = () => { const st = FEAT.screener; st.industryFilter = el.dataset.scInd; st.visualMode = "table"; renderScreener(); });
  // 2026-10-07 智能选股增强 B4-B8：驱动独立脚本 screener_extra.js（不改动本渲染逻辑）
  if (window.SC_EXTRA && window.SC_EXTRA.init) window.SC_EXTRA.init(st, host);
  // 2026-10-07 修复：暴露引擎 API 给 screener_extra.js（B6 自然语言选股依赖 loadScreener / scCurrentRows / FEAT）。
  // 此前 app.js 只调用 SC_EXTRA.init 却从未设置 window.SC_EXTRA_API，导致 nlRun() 命中 "引擎未就绪" 永远无法落地筛选条件。
  window.SC_EXTRA_API = { loadScreener: loadScreener, renderScreener: renderScreener, scCurrentRows: scCurrentRows, FEAT: FEAT };
}

/* ---------- 资金流向 ---------- */
/* 2026-10-05：区间口径单独一个 loader（起止日期由前端状态决定，默认空=后端取最新交易日）。
   单日 + 区间两条链并行，谁先回来谁先渲染，别让第一屏等接口。 */
function loadBoardRange(force) {
  const k = FEAT.moneyflow.boardKind;
  const tb = force ? ("&t=" + Date.now()) : "";
  const st = FEAT.moneyflow.rangeStart || "";
  const ed = FEAT.moneyflow.rangeEnd || "";
  return api(`/api/board_flow_range?kind=${k}&start=${encodeURIComponent(st)}`
    + `&end=${encodeURIComponent(ed)}&top=16${tb}`).then((r) => {
    if (r && r.ok) { FEAT.moneyflow.range = r; renderMoneyflow(); }
  }).catch((e) => reportCatch("板块资金流·区间", e));
}

function loadMoneyflow(force) {
  if (FEAT.moneyflow.mode === "rank") {
    // 2026-10-01：force 时同时带 force=1，让后端绕过 30 分钟失败负缓存（原先只破了前端缓存，
    // 结果每次点「强制重试」仍被负缓存挡着，白等 13 秒才看到一句"源不可达"）
    const qs = force ? "?pz=30&force=1&t=" + Date.now() : "?pz=30";
    api("/api/moneyflow_rank" + qs).then((d) => { FEAT.moneyflow.data = d; renderMoneyflow(); })
      .catch((e) => reportLoadError("资金流向", e));
  } else if (FEAT.moneyflow.mode === "board") {
    // 2026-09-28 重写：板块资金流不再依赖单一东财接口（会被风控掐连）。
    // 快照=新浪主源+东财兜底（带来源标签）；曲线=东财分钟→本机采样→库内快照三级兜底。
    const k = FEAT.moneyflow.boardKind;
    const tb = force ? ("&t=" + Date.now()) : "";
    const p1 = api(`/api/board_flow_snapshot?kind=${k}&pz=60${tb}`);
    const p2 = api(`/api/board_flow_intraday?kind=${k}&top=16${tb}`);
    // 区间（默认=最新交易日单日）与上面两条并行发出，不阻塞首屏；回来再补一次渲染。
    // 2026-10-05：默认视图改成「最新交易日单日」，标题直接写死日期，不再有含糊的「累计」。
    // 可选日期区间（后端按交易日历校正；空 = 最新交易日单日，即默认展示口径）
    const p3 = api(`/api/board_flow_dates?kind=${k}${tb}`);
    Promise.all([p1, p2]).then(([s, c]) => {
      FEAT.moneyflow.snap = s;
      FEAT.moneyflow.curve = c;
      FEAT.moneyflow.data = { ok: s.ok, rows: s.rows, reason: s.reason,
        source: s.source, source_label: s.source_label, updated_at: s.updated_at };
      renderMoneyflow();
    }).catch((e) => reportLoadError("板块资金流", e));
    loadBoardRange(force);
    Promise.resolve(p3).then((dd) => {
      if (dd && dd.ok && (dd.days || []).length) {
        FEAT.moneyflow.rangeDates = dd.days;
        renderMoneyflow();   // 只有日期范围变了（给 date 框设 min/max），数据没变，别重拉
      }
    }).catch((e) => reportCatch("板块资金流·日期范围", e));
  } else {
    const sym = FEAT.moneyflow.symbol;
    if (!sym) { renderMoneyflow(); return; }
    api(`/api/moneyflow_stock?symbol=${encodeURIComponent(sym)}`).then((d) => {
      FEAT.moneyflow.data = d; renderMoneyflow(); }).catch((e) => reportLoadError("资金流向", e));
  }
}
function renderMoneyflow() {
  const host = $("#p-moneyflow"); if (!host) return;
  const mf = FEAT.moneyflow, d = mf.data || {};
  // 2026-09-29：「个股资金流」子页签撤下，与「估值诊股」合并为「我的票 → 个股诊断」（tab: diag）。
  // stock 模式的加载/渲染代码原样保留（FEAT.moneyflow.mode 仍可 = "stock"，仅无入口）。
  const tabs = `<a class="wt-tab${mf.mode === "rank" ? " active" : ""}" data-mf-mode="rank">全市场主力净流入榜</a>
    <a class="wt-tab${mf.mode === "board" ? " active" : ""}" data-mf-mode="board">板块资金流</a>`;
  let body = "";
  if (mf.mode === "rank") {
    const rows = (d.rows || []).map((r, i) => `<tr>
      <td class="wt-c1">${i + 1}</td>
      <td><button type="button" class="wt-sym" data-wt-open="${escAttr(r.code)}">${esc(r.code)}</button></td>
      <td>${esc(r.name || "")}</td>
      <td>${wtMoney(r.price)}</td><td>${wtPct(r.pct)}</td>
      <td>${featMoney(r.main_net)}</td><td>${wtPct(r.main_pct)}</td>
      <td>${featMoney(r.huge_net)}</td><td>${featMoney(r.big_net)}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="muted wt-empty">${d.ok === false ? ("接口异常：" + esc(d.reason || "未取到数据") + (d.tip ? `<br><span class="wt-tip">提示：${esc(d.tip)}</span>` : "")) : "暂无数据。"}${
      d.negative_cache
        ? `<br><span class="wt-tip wt-neg">东财源本次不可达，已按 <b>30 分钟负缓存</b>直接返回（${esc(d.cached_at || "")} 记下，还剩约 ${esc(d.neg_min_left ?? "")} 分钟）。点「强制重试」可立刻重打一次。</span>`
        : ""}</td></tr>`;
    // 2026-10-09（P0）：主力净流入榜是**东财日终报表（T-1）**口径，表头的「最新价」其实是
    // 该数据日的收盘价。旧版本整屏没有任何日期，王总把「界面刚刷新」误当成「价是今天的」。
    // 现强制在表格上方标出数据归属日（in_intraday=true 时改标「盘中快照」）。
    const rkDay = d.data_date || d.trade_date || "";
    body = `<div class="dd-bar">${dataDayBadge(rkDay, { intraday: d.is_intraday === true, time: d.data_time })}
        <span class="dd-note">本榜为东财<b>日终报表</b>口径，「最新价」是该数据日的收盘/快照价，<b>不是当前实时价</b>。</span></div>
      <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>#</th><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th>
      <th>主力净流入</th><th>净占比</th><th>超大单</th><th>大单</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
  } else if (mf.mode === "board") {
    const k = mf.boardKind;
    const klabel = k === "industry" ? "行业板块" : "概念板块";
    const snap = mf.snap || d || {};
    const cur = mf.curve || {};
    const caxis = cur.axis || [], cser = cur.series || [];
    const lastTs = cur.last_ts || (caxis.length ? caxis[caxis.length - 1] : "");
    // 2026-10-05 王总质疑「全是流入、净流出的看不见」——旧写法就是这样算出来的：
    // nUp = 「曲线里末值 >= 0 的条数」、nDn = 曲线条数 − nUp。可曲线本身是按 main_net 降序取 Top 拼的，
    // 净流出板块压根没进图，nDn 恒等于 0，于是右上角永远「净流入 16 / 净流出 0」。
    // 现在改成**按榜单全池**数（快照行本来就含流入榜 + 流出榜）。
    const snapRowsForStat = (snap.rows) || [];
    const statUp = snapRowsForStat.filter((r) => (r.main_net || 0) > 0).length;
    const statDn = snapRowsForStat.filter((r) => (r.main_net || 0) < 0).length;
    // 2026-10-05 王总质疑「累计流入没边界、图上全是流入看不到流出」：
    // 区间数据（默认 = 最新交易日单日）回来了就接管这一屏——标题写清起止日期、
    // 统计胶囊用全池真数、下方表格改区间累计。旧那句「×月×日 累计净流入」
    // 没有区间边界，就是这次被质疑的对象，别再原样写回去。
    const rf = FEAT.moneyflow.range || null;
    const rOn = !!(rf && rf.ok);
    const rStart = (rOn && rf.start) || "";
    const rEnd = (rOn && rf.end) || "";
    const rDays = (rOn && rf.trade_days) || [];
    // 可选日期范围：优先后端给的（交易日历校正过的），没有就按 trade_days 首尾兜住
    const rdAll = (((FEAT.moneyflow.rangeDates || rf.trade_days) || []).slice()).sort();
    const rMin = (rdAll && rdAll[0]) || ((rf && rf.start) || "");
    const rMax = (rdAll && rdAll[rdAll.length - 1]) || ((rf && rf.end) || "");
    const rDlabel = rDays.length > 1 ? `${rStart} 至 ${rEnd}（${rDays.length} 个交易日）`
      : (rDays.length === 1 ? `${rEnd} 单日` : "—");
    const snapRows = ((snap.rows) || []).slice(0, 60);
    // 2026-09-28 按王总要求：代表股多列几只且可点击；上方各列收紧，宽度让给代表股。
    // 超大单 / 大单只有东财源才给（新浪源恒为空）——全空就不渲染这两列，
    // 免得两列「—」白占宽度、把代表股挤到换行。
    const hasSplit = snapRows.some((r) => r.huge_net != null || r.big_net != null);
    const ncol = hasSplit ? 8 : 6;
    // 代表股单元格：最多 12 只（后端按总市值取行业龙头），名称可点（代码由后端补，无代码则不硬造链接）
    const memCell = (r) => {
      const ms = (r.members || []).filter((m) => /^\d{6}$/.test(String(m.code || ""))).slice(0, 12);
      if (ms.length) {
        return `<span class="bf-mems">` + ms.map((m) => {
          const p = m.pct == null ? null : Number(m.pct);
          const cls = p == null ? "" : (p > 0 ? "up" : (p < 0 ? "dn" : ""));
          return `<button type="button" class="bf-mem" data-wt-open="${escAttr(m.code)}"` +
            ` title="${escAttr(m.code + " " + m.name + (m.price != null ? "  现价 " + m.price : ""))}">` +
            `${esc(m.name)}${p == null ? "" : `<i class="${cls}">${p > 0 ? "+" : ""}${p.toFixed(2)}%</i>`}</button>`;
        }).join("") + `</span>`;
      }
      return r.leader
        ? `<span class="bf-mem-plain" title="该板块接口未返回成分股代码，暂不可点">${esc(r.leader)}</span>`
        : `<span class="muted">—</span>`;
    };
    const rows = snapRows.map((r, i) => `<tr>
      <td class="wt-c1">${i + 1}</td>
      <td class="bf-bn" data-bf-open="${escAttr(r.board_code)}" data-bf-kind="${escAttr(k)}" data-bf-name="${escAttr(r.board_name || "")}" title="点击查看板块详情" style="cursor:pointer">${esc(r.board_name || r.board_code || "")}</td>
      <td>${wtPct(r.pct)}</td>
      <td>${featMoney(r.main_net)}</td><td>${wtPct(r.main_pct)}</td>
      ${hasSplit ? `<td>${featMoney(r.huge_net)}</td><td>${featMoney(r.big_net)}</td>` : ""}
      <td class="bf-mem-td">${memCell(r)}</td>
    </tr>`).join("") || `<tr><td colspan="${ncol}" class="muted wt-empty">${snap.ok === false ? "接口异常：" + esc(snap.reason || "未取到数据") : "暂无数据。"}</td></tr>`;
    // 区间表格：只有「区间累计净额 / 流入天数 / 流出天数」，代表股那几列在区间口径下没意义
    const rRows = (rOn ? (rf.pool || []).slice(0, 60) : []).map((r, i) => `<tr>
      <td class="wt-c1">${i + 1}</td>
      <td class="bf-bn" data-bf-open="${escAttr(r.code)}" data-bf-kind="${escAttr(k)}" data-bf-name="${escAttr(r.name || "")}" title="点击查看板块详情" style="cursor:pointer">${esc(r.name || r.code || "")}</td>
      <td class="${(r.total || 0) > 0 ? "up" : ((r.total || 0) < 0 ? "dn" : "")}">${(r.total >= 0 ? "+" : "")}${Number(r.total).toFixed(2)}亿</td>
      <td>${r.in_days} / ${r.out_days}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted wt-empty">${
      rf && rf.warming ? "区间数据抓取中（后台补抓日线），稍后刷新即可。" : "暂无区间数据。"}</td></tr>`;

    body = `<div class="bf-kinds">
        <span class="bf-kinds-l">板块类型：</span>
        <a class="bf-kind${k === "industry" ? " active" : ""}" data-bf-kind="industry">行业板块</a>
        <a class="bf-kind${k === "concept" ? " active" : ""}" data-bf-kind="concept">概念板块</a>
        <span class="bf-src-tag">数据来源：${esc(snap.source_label || snap.source || "—")}</span>
      </div>
      <div class="bf-curve-wrap">
        <div class="bf-curve-head">
          <span class="bf-curve-title">${rOn
            ? `板块资金流区间曲线 · ${esc(rDlabel)} 累计净额（${esc(klabel)}）`
            : `板块资金流分时曲线 · ${esc(curveDateLabel(cur))} 累计净流入（${esc(klabel)}）`}</span>
          ${!rOn && cur.stale ? `<span class="bf-date-badge" title="数据真实归属日，非当天">非当日数据</span>` : ""}
          <span class="bf-curve-meta">${rOn
            // 区间口径：用全池真统计（up/down 是**全部板块**里净流入/净流出的个数，不是图上画了几条）
            ? `<span class="bf-chip bf-chip-up">净流入 <b>${((rf.stats || {}).up) || 0}</b></span>` +
              `<span class="bf-chip bf-chip-dn">净流出 <b>${((rf.stats || {}).down) || 0}</b></span>` +
              `<span class="bf-chip">全池 <b>${((rf.stats || {}).boards) || 0}</b></span>`
            : (cser.length
              ? `<span class="bf-chip bf-chip-up">净流入 <b>${statUp}</b></span>` +
                `<span class="bf-chip bf-chip-dn">净流出 <b>${statDn}</b></span>` : "")}
            ${!rOn && lastTs ? `<span class="bf-time-badge">时间：${esc(lastTs)}</span>` : ""}
            ${!rOn && cur.market_note ? `<span class="bf-note-inline" title="${escAttr(cur.market_note)}">${esc(cur.market_note)}</span>` : ""}</span>
        </div>
        <div class="bf-range-bar">
          <a class="bf-range-pill${FEAT.moneyflow.boardMode === "single" ? " active" : ""}" data-bf-range-mode="single" title="口径=最新交易日单日，默认展示口径">单日 · 最新交易日</a>
          <a class="bf-range-pill${FEAT.moneyflow.boardMode === "range" ? " active" : ""}" data-bf-range-mode="range" title="自己指定起止日期，看区间累计净额">指定日期区间</a>
          <span class="bf-range-sep"></span>
          ${FEAT.moneyflow.boardMode === "range" ? (() => {
            const qs = [3, 5, 10, 20].map((n) =>
              `<a class="bf-range-qi" data-bf-range-quick="${n}" title="取该区间内的交易日">近${n}日</a>`).join("");
            return `<label class="bf-range-l" for="bfRangeStart">起</label>
            <input id="bfRangeStart" class="bf-range-in" type="date" value="${escAttr(FEAT.moneyflow.rangeStart || rStart || "")}" min="${escAttr(rMin || "")}" max="${escAttr(rMax || "")}">
            <label class="bf-range-l" for="bfRangeEnd">止</label>
            <input id="bfRangeEnd" class="bf-range-in" type="date" value="${escAttr(FEAT.moneyflow.rangeEnd || rEnd || "")}" min="${escAttr(rMin || "")}" max="${escAttr(rMax || "")}">
            <button class="btn sm" id="bfRangeApply" title="按上面起止日期重新拉区间数据">应用</button>
            <span class="bf-range-q">快捷 ${qs}</span>`;
          })() : `<span class="bf-range-cur">当前口径：<b>${esc(rDlabel)}</b>`
            + `${rOn && rf.warming ? " · 日线后台补抓中，稍后刷新" : ""}</span>`}
        </div>
        <div id="bfCurve" class="bf-curve"></div>
      </div>
      <div class="bf-snap-head">${rOn
        ? `板块区间资金流排行（起 ${esc(rStart)} 至 ${esc(rEnd)} · 全池 ${esc(((rf.stats || {}).boards) || 0)} 个板块）`
        : "板块资金流排行（双向：净流入 / 净流出）"}
        <span class="bf-snap-sub">${rOn
          ? "金额单位：亿元 · 「流入/流出天数」是该区间内净额为正/负的交易日数，可自己核对曲线是不是同一口径"
          : "金额单位：元 · 代表股按总市值取行业龙头，点名称看个股"}</span></div>
      <div class="wt-tw"><table class="wt-tb bf-tb"><colgroup>
        <col class="bf-c-i"><col class="bf-c-bn">${rOn ? "" : `<col class="bf-c-pct"><col class="bf-c-net"><col class="bf-c-ratio">`}
        ${rOn ? "" : (hasSplit ? `<col class="bf-c-split"><col class="bf-c-split">` : "")}${rOn ? "" : "<col>"}
        ${rOn ? `<col class="bf-c-net"><col>` : ""}
      </colgroup><thead><tr>
        ${rOn ? "<th>#</th><th>板块名称</th><th>区间累计净额</th><th>流入/流出天数</th>"
          : `<th>#</th><th>板块名称</th><th>涨跌幅</th>
        <th>主力净流入</th><th>净占比</th>${hasSplit ? "<th>超大单</th><th>大单</th>" : ""}<th>代表股·行业龙头（点击看个股）</th>`}
      </tr></thead><tbody>${rOn ? rRows : rows}</tbody></table></div>`;
  } else {
    const r = d || {};
    const cell = (v, lbl) => `<div class="feat-kpi"><div class="feat-kpi-v">${featMoney(v)}</div><div class="feat-kpi-l">${lbl}</div></div>`;
    const recent = (r.recent || []).map((x) =>
      `<span class="feat-recent"><b>${esc(x.date || "")}</b> ${featMoney(x.main_net)}</span>`).join("");
    body = `<div class="wt-form" style="grid-template-columns:1fr auto">
      <label>代码<input id="mfSym" value="${escAttr(mf.symbol)}" placeholder="如 600519"></label>
      <button class="btn primary" id="mfGo">查询</button></div>
      ${r.ok ? `<div class="feat-kpis">${cell(r.main_net, "主力净流入")}${cell(r.huge_net, "超大单")}
        ${cell(r.big_net, "大单")}${cell(r.mid_net, "中单")}${cell(r.small_net, "小单")}</div>
        <div class="dd-bar">${dataDayBadge(r.date, { intraday: r.is_intraday === true, time: r.data_time })}</div>
        <div class="hint">最新交易日：${esc(r.date || "—")}</div>
        <div class="wt-tw"><div class="feat-recent-row">近 6 日：${recent || "—"}</div></div>`
        : `<div class="hint wt-foot">${r.reason || "请输入代码查询个股主力资金净流入（超大单/大单/中单/小单）。"}</div>${r.tip ? `<div class="hint wt-foot">提示：${esc(r.tip)}</div>` : ""}`}`;
  }
  host.innerHTML = `
    <div class="wt-head"><h2>资金流向</h2>
      <div class="wt-head-actions"><button class="btn" id="mfRefresh" title="重新拉取最新数据">🔄 ${mf.mode === "board" ? "刷新" : "强制重试"}</button></div>
    </div>
    <div class="wt-tabs">${tabs}</div>${body}
    <div class="hint wt-foot">${mf.mode === "board"
      ? "板块资金流：快照来自新浪财经（主源）/ 东方财富（兜底），曲线优先东方财富当日分钟、被风控时回退本机采样累积。每 2 分钟自动刷新（非实时）；主力净流入正为流入、负为流出。"
      : `主力净流入为机构/大单净买入口径；正为净流入、负为净流出。数据为东财<b>日终报表</b>推送（非盘中实时），归属日见表格上方「数据日」标注，仅供参考。${d && d.ok === false ? "如长时间失败可点「🔄 强制重试」切换 ut，或检查代理/VPN 是否拦截 push2.eastmoney.com。" : ""}`}</div>`;
  if (mf.mode === "board") { renderBoardFlowCurve(); bindBoardFlowControls(); }
  else { stopBoardFlowAuto(); }
  $$("#p-moneyflow [data-mf-mode]").forEach((el) => el.onclick = () => {
    FEAT.moneyflow.mode = el.dataset.mfMode;
    if (FEAT.moneyflow.mode === "stock" && !FEAT.moneyflow.symbol) FEAT.moneyflow.symbol = "600519";
    loadMoneyflow();
  });
  const go = $("#mfGo"); if (go) go.onclick = () => {
    const v = $("#mfSym"); if (v && v.value.trim()) { FEAT.moneyflow.symbol = v.value.trim(); loadMoneyflow(); }
  };
  const rf = $("#mfRefresh"); if (rf) rf.onclick = () => loadMoneyflow(true);
  // 2026-09-28 修复「点代码没反应」：本面板表格的代码列渲染的是 data-wt-open，
  // 而原先只调 bindFeatStockClicks —— 它的选择器仅认 [data-feat-open]，两者不匹配，
  // 于是「全市场主力净流入榜」的代码列没有任何绑定，点了完全不动。
  $$("#p-moneyflow [data-wt-open]").forEach((el) => {
    el.onclick = (ev) => { ev.preventDefault(); openStock(el.dataset.wtOpen, el); };
  });
  bindFeatStockClicks("#p-moneyflow");
}

// 曲线日期标注（2026-10-03 王总质疑「国庆没开市哪来的资金流」）：
// 东财分钟接口在**休市日照样返回**，吐的是最近一个交易日的完整曲线；旧版标题硬写「当日累计净
// 流入」又不显示日期，于是旧数据顶着新日期。现在后端会透出真实 data_date（接口自带，
// 由 _em_minute_curve 从 "2026-09-30 09:31" 里保留下来），这里只负责显示，不自己猜日期。
function curveDateLabel(c) {
  const dd = (c && c.data_date) || "";
  return dd || "当日";
}

// ================= 2026-09-28 板块资金流「资金分布」曲线（按参考图重做）=================
// 内联手写 SVG（项目无图表库依赖）。横轴=交易时段 09:30–11:30 / 13:00–15:00；
// 纵轴=当日累计净流入（亿）；每条折线=一个板块，末端引到右侧「板块名+数值」标签；
// 红=净流入 / 绿=净流出（A 股习惯）。数据只画真有的时点，缺的一段不补、不猜。

/* 采样时点 "HH:MM" → 交易时段分钟轴（09:30=0，11:30=120，15:00=240；午间休市归到 11:30） */
function bfSessionX(ts) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(ts || ""));
  if (!m) return null;
  const t = (+m[1]) * 60 + (+m[2]);
  if (t <= 9 * 60 + 30) return 0;
  if (t <= 11 * 60 + 30) return t - (9 * 60 + 30);
  if (t < 13 * 60) return 120;
  return Math.min(240, 120 + (t - 13 * 60));
}
const BF_TICKS = [[0, "09:30"], [60, "10:30"], [120, "11:30"], [180, "14:00"], [240, "15:00"]];
const BF_RED = ["#e5484d", "#f1595c", "#ff6f61", "#ff8354", "#ff9730", "#ffa726",
  "#ffb833", "#f0c141", "#dcae2f", "#c49c26", "#ab8a2a", "#947a2e"];
const BF_GREEN = ["#1f9d55", "#28a95f", "#32b46a", "#3fbf78", "#50c98a", "#3aa39c",
  "#46ada4", "#57b8ad", "#68c2b7", "#79ccbf", "#8ad5c7", "#9bdecf"];

/* 纵轴刻度取整（1/2/2.5/5/10 × 10^n） */
function bfNiceStep(span, n) {
  const raw = Math.max(1e-6, span / Math.max(1, n));
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const k of [1, 2, 2.5, 5, 10]) if (raw <= k * mag) return k * mag;
  return 10 * mag;
}

/* 右侧标签防重叠：按 y 升序松弛排布（正向推 + 反向压 + 边界钳制），空间不足则均分 */
function bfLayoutTails(labels, top, bottom, gap) {
  const n = labels.length;
  if (!n) return;
  const span = Math.max(1, bottom - top);
  let g = gap;
  if (n > 1 && span < g * (n - 1)) g = Math.max(10, span / (n - 1));
  const pos = labels.map((l) => l.y);
  for (let it = 0; it < 8; it++) {
    for (let k = 0; k < n; k++) pos[k] = Math.max(pos[k], k === 0 ? top : pos[k - 1] + g);
    for (let k = n - 1; k >= 0; k--) pos[k] = Math.min(pos[k], k === n - 1 ? bottom : pos[k + 1] - g);
  }
  if (pos[0] < top - 0.5 || pos[n - 1] > bottom + 0.5) {
    for (let k = 0; k < n; k++) pos[k] = top + (span * k) / Math.max(1, n - 1);
  }
  labels.forEach((l, k) => { l.ty = pos[k]; });
  return g;
}

function bfFlowCurveSvg(curve) {
  const axis = (curve && curve.axis) || [];
  const series = (curve && curve.series) || [];
  if (!axis.length || !series.length) return "";
  const xi = axis.map(bfSessionX);
  const W = 1000, H = 400, padL = 62, padR = 196, padT = 20, padB = 28;
  let lo = Infinity, hi = -Infinity, n = 0;
  for (const s of series) {
    const p = s.points || [];
    for (let i = 0; i < p.length; i++) {
      const v = p[i];
      if (v == null || xi[i] == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      n++;
    }
  }
  if (!n) return "";
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = bfNiceStep(hi - lo, 5);
  const y0 = Math.floor(lo / step) * step;
  let y1 = Math.ceil(hi / step) * step;
  if (y1 - y0 < step) y1 = y0 + step;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const X = (m) => padL + (m / 240) * innerW;
  const Y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * innerH;

  let grid = "";
  for (let v = y0; v <= y1 + 1e-9; v += step) {
    const yy = Y(v), zero = Math.abs(v) < 1e-9;
    grid += `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}"`
      + ` stroke="currentColor" stroke-opacity="${zero ? 0.45 : 0.12}"/>`;
    grid += `<text x="${padL - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end" font-size="11"`
      + ` fill="currentColor" fill-opacity="${zero ? 0.85 : 0.55}">${Math.round(v)}亿</text>`;
  }
  let xticks = "";
  for (const [m, lb] of BF_TICKS) {
    xticks += `<line x1="${X(m).toFixed(1)}" y1="${padT}" x2="${X(m).toFixed(1)}"`
      + ` y2="${(H - padB).toFixed(1)}" stroke="currentColor" stroke-opacity="0.06"/>`;
    xticks += `<text x="${X(m).toFixed(1)}" y="${H - 9}" text-anchor="middle" font-size="11"`
      + ` fill="currentColor" fill-opacity="0.6">${lb}</text>`;
  }

  const paint = series.map((s, idx) => {
    const p = s.points || [];
    let last = null, lx = null;
    for (let i = 0; i < p.length; i++) if (p[i] != null && xi[i] != null) { last = p[i]; lx = X(xi[i]); }
    return { idx, s, last, lx };
  }).filter((o) => o.last != null);
  const colOf = new Map();
  paint.filter((o) => o.last >= 0).sort((a, b) => b.last - a.last)
    .forEach((o, i) => colOf.set(o.idx, BF_RED[Math.min(i, BF_RED.length - 1)]));
  paint.filter((o) => o.last < 0).sort((a, b) => a.last - b.last)
    .forEach((o, i) => colOf.set(o.idx, BF_GREEN[Math.min(i, BF_GREEN.length - 1)]));

  let paths = "";
  const labels = [];
  for (const o of paint) {
    const col = colOf.get(o.idx), p = o.s.points || [];
    let d = "", started = false;
    for (let i = 0; i < p.length; i++) {
      const v = p[i];
      if (v == null || xi[i] == null) { started = false; continue; }
      d += (started ? "L" : "M") + X(xi[i]).toFixed(1) + "," + Y(v).toFixed(1);
      started = true;
    }
    if (!d) continue;
    paths += `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.8"`
      + ` stroke-linejoin="round" stroke-linecap="round" stroke-opacity="0.95"/>`;
    labels.push({ name: o.s.name || o.s.code || "", col, y: Y(o.last), x: o.lx, v: o.last });
  }
  labels.sort((a, b) => a.y - b.y);
  const ax = W - padR + 6;
  const eg = bfLayoutTails(labels, padT + 8, H - padB - 4, 20);
  const fs = eg >= 19 ? 12 : 11;   // 空间不足时自动降字号，避免挤成一团
  let tails = "";
  for (const lb of labels) {
    const yy = lb.ty;
    tails += `<line x1="${lb.x.toFixed(1)}" y1="${lb.y.toFixed(1)}" x2="${ax.toFixed(1)}"`
      + ` y2="${yy.toFixed(1)}" stroke="${lb.col}" stroke-opacity="0.45" stroke-width="0.9"/>`;
    tails += `<circle cx="${lb.x.toFixed(1)}" cy="${lb.y.toFixed(1)}" r="2.6" fill="${lb.col}"/>`;
    tails += `<text class="bf-lbl" x="${(ax + 8).toFixed(1)}" y="${(yy + 4).toFixed(1)}" font-size="${fs}"`
      + ` fill="${lb.col}">${esc(lb.name)}${lb.v >= 0 ? "+" : ""}${lb.v.toFixed(2)}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet"`
    + ` class="bf-svg" role="img" aria-label="板块资金流分时曲线：当日累计净流入（亿）">`
    + `${grid}${xticks}${paths}${tails}</svg>`;
}

// ================= 2026-10-05 板块资金流「日期区间」曲线 =================
// 背景：王总质疑「图上全是流入、净流出的一个看不见，日期还只写了个 09-30 的累计」。
// 根因是旧曲线按 main_net 降序取 Top16 拼点，净流出板块根本没进图；这里反过来做：
// 横轴 = **交易日**（不是时段），纵轴 = 区间累计净额（亿），且 **0 轴一定落在图内**，
// 正值画在 0 轴上方（红）/ 负值画在 0 轴下方（绿）——净流出是「看得见的下半截」。
// 单日区间（起=止）时退化为「一个交易日一根竖轴 + 每板块一个点」，照样红绿分列。
function bfRangeCurveSvg(r) {
  const days = (r && r.trade_days) || [];
  const gs = (r && r.groups) || [];
  if (!days.length || !gs.length) return "";
  const W = 1000, H = 400, padL = 62, padR = 196, padT = 20, padB = 28;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const X = (i) => (days.length > 1 ? padL + (i / (days.length - 1)) * innerW : padL + innerW / 2);

  let lo = 0, hi = 0, n = 0;
  for (const g of gs) {
    for (const d of days) {
      const v = (g.points || {})[d];
      if (v == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      n++;
    }
  }
  if (!n) return "";
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = bfNiceStep(hi - lo, 5);
  const y0 = Math.floor(lo / step) * step;
  let y1 = Math.ceil(hi / step) * step;
  if (y1 - y0 < step) y1 = y0 + step;
  const Y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * innerH;

  let grid = "";
  for (let v = y0; v <= y1 + 1e-9; v += step) {
    const yy = Y(v), zero = Math.abs(v) < 1e-9;
    grid += `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}"`
      + ` stroke="currentColor" stroke-opacity="${zero ? 0.45 : 0.12}"/>`;
    grid += `<text x="${padL - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end" font-size="11"`
      + ` fill="currentColor" fill-opacity="${zero ? 0.85 : 0.55}">${Math.round(v)}亿</text>`;
  }

  const every = days.length > 10 ? Math.ceil(days.length / 8) : 1;
  let xticks = "";
  days.forEach((d, i) => {
    if (i % every !== 0 && i !== days.length - 1) return;
    xticks += `<line x1="${X(i).toFixed(1)}" y1="${padT}" x2="${X(i).toFixed(1)}"`
      + ` y2="${(H - padB).toFixed(1)}" stroke="currentColor" stroke-opacity="0.06"/>`;
    xticks += `<text x="${X(i).toFixed(1)}" y="${H - 9}" text-anchor="middle" font-size="11"`
      + ` fill="currentColor" fill-opacity="0.6">${esc(String(d).slice(5))}</text>`;
  });

  const paint = [];
  gs.forEach((g, idx) => {
    const pts = g.points || {};
    let last = null, lx = padL;
    for (let i = 0; i < days.length; i++) {
      const v = pts[days[i]];
      if (v != null) { last = v; lx = X(i); }
    }
    if (last == null) return;
    paint.push({ idx, g, last, lx, pts });
  });
  if (!paint.length) return "";

  const colOf = new Map();
  paint.filter((o) => o.last >= 0).sort((a, b) => b.last - a.last)
    .forEach((o, i) => colOf.set(o.idx, BF_RED[Math.min(i, BF_RED.length - 1)]));
  paint.filter((o) => o.last < 0).sort((a, b) => a.last - b.last)
    .forEach((o, i) => colOf.set(o.idx, BF_GREEN[Math.min(i, BF_GREEN.length - 1)]));

  let paths = "", dots = "";
  const labels = [];
  for (const o of paint) {
    const col = colOf.get(o.idx);
    let d = "", started = false;
    for (let i = 0; i < days.length; i++) {
      const v = o.pts[days[i]];
      if (v == null) { started = false; continue; }
      d += (started ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1);
      started = true;
    }
    if (!d) continue;
    paths += `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.8"`
      + ` stroke-linejoin="round" stroke-linecap="round" stroke-opacity="0.95"/>`;
    // 单日区间只有一根点，path 画不出圆点，单独补一个实心点（否则「看不出哪天」）
    if (days.length === 1) {
      dots += `<circle cx="${X(0).toFixed(1)}" cy="${Y(o.last).toFixed(1)}" r="3.6"`
        + ` fill="${col}" stroke="currentColor" stroke-opacity="0.25"/>`;
    }
    labels.push({ name: o.g.name || o.g.code || "", col, y: Y(o.last), x: o.lx, v: o.last });
  }
  if (!paths) return "";

  labels.sort((a, b) => a.y - b.y);
  const ax = W - padR + 6;
  const eg = bfLayoutTails(labels, padT + 8, H - padB - 4, 20);
  const fs = eg >= 19 ? 12 : 11;
  let tails = "";
  for (const lb of labels) {
    tails += `<line x1="${lb.x.toFixed(1)}" y1="${lb.y.toFixed(1)}" x2="${ax.toFixed(1)}"`
      + ` y2="${lb.ty.toFixed(1)}" stroke="${lb.col}" stroke-opacity="0.45" stroke-width="0.9"/>`;
    tails += `<circle cx="${lb.x.toFixed(1)}" cy="${lb.y.toFixed(1)}" r="2.6" fill="${lb.col}"/>`;
    tails += `<text class="bf-lbl" x="${(ax + 8).toFixed(1)}" y="${(lb.ty + 4).toFixed(1)}" font-size="${fs}"`
      + ` fill="${lb.col}">${esc(lb.name)}${lb.v >= 0 ? "+" : ""}${lb.v.toFixed(2)}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet"`
    + ` class="bf-svg" role="img" aria-label="板块资金流区间曲线：${esc(r.start || "")} 至 ${esc(r.end || "")} 累计净额（亿）">`
    + `${grid}${xticks}${paths}${dots}${tails}</svg>`;
}

function renderBoardFlowCurve() {
  const host = $("#bfCurve"); if (!host) return;
  // 区间口径优先：起止日期写死在标题上，0 轴在图内，流入在上（红）/ 流出在下（绿）。
  // 区间图画不出来（比如后台还在补抓日线、groups 为空）就退回原分时曲线，不白屏。
  const rf = FEAT.moneyflow.range || null;
  if (rf && rf.ok) {
    const svg = bfRangeCurveSvg(rf);
    if (svg) {
      const st = rf.stats || {};
      host.innerHTML = svg
        + `<div class="bf-src">口径 ${esc(rf.start || "—")} ~ ${esc(rf.end || "—")} · `
        + `${st.days} 个交易日 · 全池 ${st.boards} 个板块（净流入 ${st.up} / 净流出 ${st.down}） · `
        + `来源：${esc(rf.source_label || rf.source || "—")}`
        + `${(rf.warming ? " · 日线后台补抓中，稍后刷新" : "")}`
        + `${(rf.note || []).length ? " · " + (rf.note || []).map(esc).join(" / ") : ""}</div>`;
      return;
    }
  }
  const c = FEAT.moneyflow.curve || {};
  if (c.ok === false) {
    host.innerHTML = `<div class="bf-note">${esc(c.reason || "分时曲线暂不可用。")}</div>`;
    return;
  }
  if (!c.axis || !c.series || !c.series.length) {
    host.innerHTML = `<div class="bf-note">${c.warming_up ? "数据积累中：查看器保持开启即会自动采样（每 30 秒一次），交易时段稍后刷新即可看到曲线。" : "暂无曲线数据。"}</div>`;
    return;
  }
  const first = c.first_ts || (c.axis || [])[0] || "";
  const late = bfSessionX(first) > 5;
  host.innerHTML = bfFlowCurveSvg(c) +
    `<div class="bf-src">${c.data_date ? "数据日 " + esc(c.data_date) + " · " : ""}来源：${esc(c.source_label || c.source || "—")} · 更新于 ${esc(c.updated_at || "—")}`
    + `${c.sample_count ? " · 采样点 " + c.sample_count : ""}${c.cached_at ? " · 库内缓存于 " + esc(c.cached_at) : ""}`
    + `${late ? ` · 曲线自 ${esc(first)} 起（查看器当日启动后才开始采样，全天完整曲线需交易时段保持开启）` : ""}</div>`;
}

function stopBoardFlowAuto() {
  if (FEAT.moneyflow.autoTimer) { clearInterval(FEAT.moneyflow.autoTimer); FEAT.moneyflow.autoTimer = null; }
}

function bindBoardFlowControls() {
  $$("#p-moneyflow [data-bf-kind]").forEach((el) => {
    el.onclick = () => {
      const k = el.dataset.bfKind;
      if (k === FEAT.moneyflow.boardKind) return;
      FEAT.moneyflow.boardKind = k;
      loadMoneyflow();
    };
  });
  // 2026-10-05 区间口径控件：模式 pill / 起止日期 / 快捷区间
  $$("#p-moneyflow [data-bf-range-mode]").forEach((el) => {
    el.onclick = () => {
      const m = el.dataset.bfRangeMode;
      FEAT.moneyflow.boardMode = m;
      // 切回「单日」必须清掉起止日期：日期框藏起来了，但 rangeStart/End 还留着上次选的区间，
      // 结果就是点「单日」却仍按区间拉数、图一直是「补抓中 · 0/0」（2026-10-05 实测复现）
      if (m === "single") { FEAT.moneyflow.rangeStart = ""; FEAT.moneyflow.rangeEnd = ""; }
      loadMoneyflow();
    };
  });
  $$("#p-moneyflow [data-bf-range-quick]").forEach((el) => {
    el.onclick = () => {
      const ds = (FEAT.moneyflow.rangeDates || []).filter(Boolean);
      const n = +el.dataset.bfRangeQuick || 5;
      const end = ds[ds.length - 1] || "";
      const start = ds.length > 1 ? ds[Math.max(0, ds.length - n)] : "";
      FEAT.moneyflow.rangeStart = start;
      FEAT.moneyflow.rangeEnd = end;
      loadBoardRange(true);
    };
  });
  const apply = () => {
    const a = $("#bfRangeStart"), b = $("#bfRangeEnd");
    const s = a && a.value, e = b && b.value;
    if (!s || !e) return;
    // 起 > 止就对调：日期框是人敲的，别让它静默算出一个空区间
    FEAT.moneyflow.rangeStart = s <= e ? s : e;
    FEAT.moneyflow.rangeEnd = s <= e ? e : s;
    loadBoardRange(true);
  };
  const bs = $("#bfRangeApply"); if (bs) bs.onclick = apply;
  const ds = ["#bfRangeStart", "#bfRangeEnd"];
  ds.forEach((sel) => { const el = $(sel); if (el) el.onchange = apply; });
  // 2026-10-12 修复监听泄漏：bindBoardFlowControls 每次进入/刷新该面板都会调用，
  // 原实现每次都往 document 挂一个匿名 keydown 且从不移除 → 反复进出即无限累积。
  // 仿 _klineResizeBound 加一次性守卫；事件里现读 DOM，避免闭包引用过期元素。
  if (window.addEventListener && !_bfKeysBound) {
    _bfKeysBound = true;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target && /bfRange(Start|End)$/.test(e.target.id || "")) {
        const a = $("#bfRangeStart"), b = $("#bfRangeEnd");
        const s = a && a.value, en = b && b.value;
        if (!s || !en) return;
        FEAT.moneyflow.rangeStart = s <= en ? s : en;
        FEAT.moneyflow.rangeEnd = s <= en ? en : s;
        loadBoardRange(true);
      }
    });
  }

  // 每 2 分钟自动刷新曲线（非实时，与王总「几分钟一发新的」一致）
  stopBoardFlowAuto();
  FEAT.moneyflow.autoTimer = setInterval(() => {
    if (FEAT.moneyflow.mode !== "board") { stopBoardFlowAuto(); return; }
    const k = FEAT.moneyflow.boardKind;
    api(`/api/board_flow_intraday?kind=${k}&top=16`).then((c) => {
      FEAT.moneyflow.curve = c; renderBoardFlowCurve();
    }).catch(() => {});
    // 区间口径（默认单日）也一起刷，否则收盘后单日数字会一直停在盘中
    if (FEAT.moneyflow.boardMode === "range") loadBoardRange(false);
  }, 120000);
}

/* ---------- 估值诊股（规则评分） ---------- */
function loadValuation(symbol) {
  if (symbol) VAL.symbol = symbol;
  if (!VAL.symbol) { renderValuation(); return; }
  api(`/api/valuation?symbol=${encodeURIComponent(VAL.symbol)}`).then((d) => {
    FEAT.valuation = { symbol: VAL.symbol, data: d }; renderValuation(); }).catch((e) => reportLoadError("估值诊股", e));
}
/* 2026-10-30 D33：这块是**死代码，别照着它排错**。
   2026-09-29 做面板合并时，「估值诊股」独立页并入「个股诊断」，index.html 里已经没有
   id="p-valuation" 容器、底部导航也没有对应项，所以下面第一行 `if (!host) return` 必定命中，
   函数体（含 #valSym / #valGo / #valPoolClear 三个控件和整块评分渲染）永远不会执行。
   有意保留作参考实现：真要复活它，必须先恢复容器与导航项；否则改了也不会有任何可见效果。 */
function renderValuation() {
  const host = $("#p-valuation"); if (!host) return;
  const v = (FEAT.valuation && FEAT.valuation.data) || null;
  const pool = valPoolGet();
  // 「最近诊过」下拉名单：点击弹窗「估值诊股」或本页评分过的票（上限 20，自动清最旧）
  const dl = pool.length ? `<datalist id="valPoolList">${pool.map((x) => {
    const dt = x.ts ? new Date(x.ts).toLocaleDateString("zh-CN") : "";
    return `<option value="${escAttr(x.code)}">${esc(x.name || "")}${x.name && dt ? " · " : ""}${dt}</option>`;
  }).join("")}</datalist>` : "";
  const head = `<div class="wt-head"><h2>估值诊股</h2></div>
    <div class="wt-form" style="grid-template-columns:1fr auto auto">
      <label>代码<input id="valSym" list="valPoolList" value="${escAttr(VAL.symbol)}" placeholder="如 600519；下拉可选最近诊过的（存 20 条）"></label>
      <button class="btn primary" id="valGo">评分</button>
      <button class="btn sm" id="valPoolClear" title="清空「最近诊过」记录"${pool.length ? "" : " disabled"}>清空记录</button>${dl}</div>`;
  let body = "";
  if (v && v.ok) {
    const sc = v.score, cls = sc >= 75 ? "good" : (sc >= 55 ? "mid" : "low");
    const comps = (v.components || []).map((c) => `<div class="feat-comp">
      <span class="feat-comp-l">${esc(c.label)}</span>
      <span class="feat-bar"><span class="feat-bar-fill ${cls}" style="width:${c.score == null ? 0 : c.score}%"></span></span>
      <span class="feat-comp-v">${c.score == null ? "—" : c.score}</span></div>`).join("");
    body = `<div class="feat-score-wrap">
      <div class="feat-score ${cls}">${sc}<span class="feat-score-u">分</span></div>
      <div class="feat-verdict">${esc(v.verdict || "")}</div></div>
      <div class="wt-tw feat-metrics">
        <span><b>PE</b> ${wtMoney(v.pe)}</span><span><b>PB</b> ${wtMoney(v.pb)}</span>
        <span><b>PS</b> ${wtMoney(v.ps)}</span><span><b>PEG</b> ${wtMoney(v.peg)}</span>
        <span><b>现金流</b> ${wtMoney(v.pcf)}</span><span><b>涨跌</b> ${wtPct(v.pct)}</span>
        <span><b>行业</b> ${esc(v.industry || "—")}</span></div>
      <div class="feat-comps">${comps}</div>
      <div class="hint wt-foot">${esc(v.disclaimer || "")}</div>`;
  } else {
    body = `<div class="hint wt-foot">输入代码查看透明多因子规则评分（PE/PB/PS/PEG/现金流 加权）。
      ${v && v.reason ? "（" + esc(v.reason) + "）" : ""}</div>`;
  }
  host.innerHTML = head + body;
  const go = $("#valGo"); if (go) go.onclick = () => {
    const s = $("#valSym"); if (s && s.value.trim()) {
      valPoolPush(s.value.trim());          // 手动评分也计入「最近诊过」
      loadValuation(s.value.trim());
    }
  };
  const pc = $("#valPoolClear"); if (pc) pc.onclick = () => { valPoolClear(); renderValuation(); };
}

/* ---------- 个股诊断（2026-09-29 新增，合并「估值诊股 + 个股资金流」） ----------
   一次诊断 = 行情估值 + 资金流向（东财→新浪兜底）+ 财务指标/分红 + 公司概况
   + 最近公告 + 专家综合分析（后端本地规则引擎，零 LLM）。
   「最近诊过」池复用 valPool*（localStorage wb_val_pool_v1，上限 20 FIFO）。 */
const DIAG = { symbol: "", data: null, quick: null, loading: false };
let dgDebounce = 0;

/* 打开「个股诊断」页就自动跑，不再让用户多点一下「开始诊断」（2026-10-02 王总原话）。
   优先级：① 已经在跑/已有结果 → 直接刷新当前那只；② 当前代码 → 用它；
   ③ 「最近诊过」里最近一条 → 自动填上并跑；④ 都没有 → 只渲染表单 + 提示。 */
function dgEnsureAutoRun() {
  if (DIAG.loading || DIAG.data) {
    if (DIAG.symbol) loadDiag(); else renderDiag();
    return;
  }
  const pool = (typeof valPoolGet === "function" ? valPoolGet() : []) || [];
  const sym = (DIAG.symbol || "").trim() || (pool.length ? String(pool[0].code || "").trim() : "");
  if (sym) { DIAG.symbol = sym; loadDiag(sym); }
  else renderDiag();
}

/* ---------- 「返回来路」（2026-09-30，王总：诊断完关掉还要回原来的页面重新翻找，很不方便） ----------
   任何页面切进「个股诊断」时（抽屉 CTA 跳转或手动点子页签都算），在 selectNav 里记下来路页；
   诊断页头部出现「← 返回××」按钮，一键回去。离开诊断页（手动切走 / 点返回）来路即作废。
   returning 标记：点「返回」触发的 selectNav 不要把 origin 误当普通导航清掉。 */
const NAV_BACK = { origin: null, returning: false };
function navTabLabel(catKey, tabKey) {
  const c = NAV.find((x) => x.key === catKey);
  if (!c) return tabKey || "";
  const it = (c.items || []).find((i) => i.tab === tabKey);
  return it ? it.label : (c.label || tabKey || "");
}

function loadDiag(symbol, force) {
  if (symbol) DIAG.symbol = symbol;
  if (!DIAG.symbol) { DIAG.data = null; DIAG.quick = null; renderDiag(); return; }
  DIAG.loading = true; DIAG.quick = null;
  renderDiag();   // 先画骨架（含 K 线容器）
  const sym = DIAG.symbol;
  // ① 缓存优先：瞬时行情 → 立刻画价格条 + 预载日K（命中本地缓存＝毫秒级，等同原「行情速览面板」体感）
  api(`/api/market_cache/quote?symbol=${encodeURIComponent(sym)}`).then((qd) => {
    if (!DIAG.loading || DIAG.symbol !== sym) return;   // 已被新查询或慢结果覆盖
    DIAG.quick = (qd && !qd.error && qd.name !== undefined) ? qd : null;
    renderDiag();
    if (DIAG.quick) dgKlineInit(sym);   // 价格条出来即拉日K，不等慢诊断
  }).catch(() => {});
  // ② 慢全量诊断（权威兜底：市值/换手/行业/资金流/估值/财务/公告）
  api(`/api/diag?symbol=${encodeURIComponent(sym)}${force ? "&force=1" : ""}`).then((d) => {
    DIAG.data = d; DIAG.loading = false; DIAG.quick = null; renderDiag();
  }).catch((e) => {
    DIAG.loading = false;
    DIAG.data = { ok: false, reason: apiErrText(e) };
    DIAG.quick = null;
    renderDiag();
    reportLoadError("个股诊断", e);
  });
}

function dgFinRow(label, v, unit, cls) {
  return `<tr><td class="wt-c1">${label}</td>
    <td class="${cls || ""}">${v === null || v === undefined || isNaN(v) ? '<span class="muted">—</span>' : v + (unit || "")}</td></tr>`;
}

function renderDiag() {
  const host = $("#p-diag"); if (!host) return;
  const pool = valPoolGet();
  const dl = pool.length ? `<datalist id="valPoolList">${pool.map((x) => {
    const dt = x.ts ? new Date(x.ts).toLocaleDateString("zh-CN") : "";
    return `<option value="${escAttr(x.code)}">${esc(x.name || "")}${x.name && dt ? " · " : ""}${dt}</option>`;
  }).join("")}</datalist>` : "";
  // 2026-10-02 王总反馈：「我点开个股查询之后还要再点一下『开始查询/开始诊断』，多此一举」
  // → 去掉「开始诊断」按钮，改成**打开即自动跑 + 输入即自动跑**，按钮只留「重新诊断 / 清空记录」。
  const head = `<div class="wt-head"><h2>个股诊断</h2>
      <div class="wt-head-actions">${NAV_BACK.origin ? `<button class="btn" id="dgBack" title="回到进入诊断前的页面：${escAttr(NAV_BACK.origin.label)}">← 返回${esc(NAV_BACK.origin.label)}</button>` : ""}<button class="btn" id="dgRefresh" title="忽略缓存，重新拉全量数据">🔄 重新诊断</button></div></div>
    <div class="wt-form" style="grid-template-columns:1fr auto">
      <label>代码<input id="dgSym" list="valPoolList" value="${escAttr(DIAG.symbol)}" placeholder="如 600519；下拉可选最近诊过的（存 20 条）"></label>
      <button class="btn sm" id="valPoolClear" title="清空「最近诊过」记录"${pool.length ? "" : " disabled"}>清空记录</button>${dl}</div>
    <div class="hint wt-foot dg-autohint">输入代码<b>自动出结果</b>（也可以从汇总页/个股列表直接点代码进来），不用来回点「开始诊断」。</div>`;
  let body = "";
  const d = DIAG.data;
  if (DIAG.loading) {
    let lstrip = "";
    if (DIAG.quick && !DIAG.quick.error) {
      const q = DIAG.quick;
      lstrip = `<div class="dg-strip">
        <span class="dg-strip-nm">${esc(q.name || "")} <i>${esc(DIAG.symbol)}</i></span>
        <span class="dg-strip-px">${wtMoney(q.price)} <small>${wtPct(q.pct)}</small></span>
        <span class="muted">市值 / 换手 / 行业等全量数据加载中…</span></div>`;
    }
    body = lstrip
      + `<div class="hint wt-foot">诊断中：行情 / 资金流 / 估值 / 财务 / 公告并行拉取，首次约 2–4 秒，之后命中缓存秒回…</div>`
      + dgKlineShell(DIAG.symbol);
  } else if (!d) {
    body = `<div class="hint wt-foot">输入 A 股代码做一次全面体检：<b>专家综合分析</b>（短/中/长期 + 操作倾向，本地规则引擎）
      + 资金流向 + 估值评分（PE/PB/PS/PEG/现金流加权）+ 财务指标（ROE/毛利率/营收净利增速）+ 公司概况 + 最近公告。
      所有结论均标注数据依据，仅供研究参考，不构成投资建议。</div>`;
  } else if (d.ok === false) {
    body = `<div class="hint wt-foot">诊断失败：${esc(d.reason || "未知原因")}。可点右上「🔄 重新诊断」。</div>`;
  } else {
    const q = d.quote || {}, v = d.valuation || {}, m = d.moneyflow || {},
          fin = d.finance || {}, dv = d.dividend, org = d.org, ex = d.expert || {}, qv = d.qval || {};
    const num1 = (x) => (x === null || x === undefined || isNaN(x)) ? "—" : Number(x).toFixed(1);
    // —— 头部行情条 ——
    const strip = d.quote_error ? "" : `<div class="dg-strip">
      <span class="dg-strip-nm">${esc(d.name || "")} <i>${esc(d.symbol)}</i></span>
      <span class="dg-strip-px">${wtMoney(q.price)} <small>${wtPct(q.pct)}</small></span>
      <span>总市值 <b>${featMoney(qv.total_mv)}</b></span>
      <span>换手 <b>${q.turnover == null ? "—" : num1(q.turnover) + "%"}</b></span>
      <span>行业 <b>${esc(org && org.industry || v.industry || "—")}</b></span></div>`;
    // —— 核心指标行（2026-09-30 王总需求：既用了巴菲特那套，就把最关键的几项单独列一行）——
    // 阈值与《巴菲特选股策略》周报的「筛选标准表」**逐项对齐**，让同一只票在诊断页与周报
    // 看到的口径一致：ROE>15% / 毛利率>40% / 净利率>15% / 资产负债率<50% /
    // PE 0~25 / PB 0~3 / 股息率>1% / 总市值>100亿。达标＝深青高亮，未达标＝中性灰。
    const _p2 = (x) => (x == null || isNaN(x)) ? null : Number(x).toFixed(2) + "%";
    const _n2 = (x) => (x == null || isNaN(x)) ? null : Number(x).toFixed(2);
    const bfDefs = [
      { k: "ROE（TTM）", v: _p2(fin.roe), t: "> 15%", pass: fin.roe == null ? null : fin.roe > 15 },
      { k: "毛利率", v: _p2(fin.gross_margin), t: "> 40%", pass: fin.gross_margin == null ? null : fin.gross_margin > 40 },
      { k: "净利率", v: _p2(fin.net_margin), t: "> 15%", pass: fin.net_margin == null ? null : fin.net_margin > 15 },
      { k: "资产负债率", v: _p2(fin.debt_ratio), t: "< 50%", pass: fin.debt_ratio == null ? null : fin.debt_ratio < 50 },
      { k: "PE（TTM）", v: _n2(v.pe), t: "0 ~ 25", pass: v.pe == null ? null : (v.pe > 0 && v.pe < 25) },
      { k: "PB（LF）", v: _n2(v.pb), t: "0 ~ 3", pass: v.pb == null ? null : (v.pb > 0 && v.pb < 3) },
      { k: "股息率", v: dv && dv.yield_pct != null ? _p2(dv.yield_pct) : null, t: "> 1%",
        pass: dv && dv.yield_pct != null ? dv.yield_pct > 1 : null },
      { k: "总市值", v: qv.total_mv == null ? null : featMoney(qv.total_mv), t: "> 100 亿",
        pass: qv.total_mv == null ? null : qv.total_mv > 100e8 },
    ];
    const bfHit = bfDefs.filter((x) => x.pass === true).length;
    const bfKnown = bfDefs.filter((x) => x.pass !== null).length;
    const bfRow = `<div class="dg-bf-wrap">
      <div class="dg-bf-hd">核心指标（巴菲特口径）<span>与《巴菲特选股策略》周报同一套阈值 · 命中 ${bfHit}/${bfKnown} 项</span></div>
      <div class="dg-bf-row">${bfDefs.map((x) => `<div class="dg-bf-item ${x.pass === null ? "" : (x.pass ? "pass" : "fail")}">
        <span class="k">${esc(x.k)}</span><span class="v">${x.v == null ? "—" : esc(x.v)}</span><span class="t">${esc(x.t)}</span></div>`).join("")}</div>
      <div class="dg-bf-note">口径：ROE / 毛利率 / 净利率 / 资产负债率取最新报告期（${esc(fin.report || "—")}），PE / PB / 股息率 TTM；
        「总市值 > 100 亿」为流动性与抗风险门槛。本行仅陈列指标，不构成投资建议。</div></div>`;
    // —— K 线走势（2026-09-30 新增；2026-10-22 改为复用 dgKlineShell，loading 态也渲染以便缓存优先预载）——

    // —— 专家综合分析 ——
    const toneCls = { good: "dg-good", mid: "dg-mid", neutral: "dg-neu", bad: "dg-bad" }[ex.action_tone] || "dg-neu";
    const reasons = (ex.reasons || []).map((r) =>
      `<li class="${r.tone === "good" ? "dg-r-good" : r.tone === "bad" ? "dg-r-bad" : "dg-r-neu"}">${esc(r.text)}</li>`).join("");
    const hor = ex.horizon || {};
    const expert = ex.ok ? `<section class="dg-expert ${toneCls}">
      <div class="dg-expert-hd"><span class="dg-expert-t">专家综合分析</span>
        <span class="dg-score">${ex.score == null ? "—" : ex.score}<i>分</i></span>
        <span class="dg-act">${esc(ex.action || "")}</span></div>
      <div class="dg-hor">
        <div><b>短期（1–2 周）</b><p>${esc(hor.short || "—")}</p></div>
        <div><b>中期（1–3 个月）</b><p>${esc(hor.mid || "—")}</p></div>
        <div><b>长期（6 个月+）</b><p>${esc(hor.long || "—")}</p></div>
      </div>
      <ul class="dg-reasons">${reasons}</ul>
      <div class="dg-cov">${esc(ex.coverage || "")} · ${esc(ex.disclaimer || "")}</div></section>`
      : `<section class="dg-expert dg-neu"><div class="dg-cov">${esc(ex.reason || "专家分析数据不足")}</div></section>`;
    // —— 估值评分 ——
    // 2b 估值因子下钻（A2）：把总评分拆成五因子明细（数据已在 d.valuation，无需新后端）
    const _facDefs = [
      { lbl: "市盈率 PE (TTM)", raw: v.pe, w: "30%", sub: "越低越便宜·权重最高" },
      { lbl: "市净率 PB (MRQ)", raw: v.pb, w: "25%", sub: "越低越安全" },
      { lbl: "市销率 PS (TTM)", raw: v.ps, w: "15%", sub: "成长股常用" },
      { lbl: "PEG (CAR)", raw: v.peg, w: "20%", sub: "<1 更具性价比" },
      { lbl: "现金流 PCF (TTM)", raw: v.pcf, w: "10%", sub: "经营现金流折现" },
    ];
    const _facRows = _facDefs.map((f, i) => {
      const sc = v.components && v.components[i] ? v.components[i].score : null;
      const scls = sc == null ? "" : (sc >= 75 ? "bull" : (sc >= 55 ? "" : "bear"));
      const interp = sc == null ? "—" : (sc >= 75 ? "偏低·占优" : (sc >= 55 ? "合理" : "偏高·谨慎"));
      return `<tr><td>${esc(f.lbl)}</td><td class="num">${f.raw == null ? "—" : wtMoney(f.raw)}</td>`
        + `<td class="num ${scls}">${sc == null ? "—" : sc}</td><td class="num">${f.w}</td>`
        + `<td>${interp}</td><td class="muted">${esc(f.sub)}</td></tr>`;
    }).join("");
    const facDetail = `<details class="dg-valfac"><summary>估值因子拆解 ▾（点击下钻五因子明细与权重）</summary>
      <div class="wt-tw"><table class="wt-tb"><thead><tr><th>因子</th><th>原始值</th><th>单项评分</th><th>权重</th><th>解读</th><th>说明</th></tr></thead>
      <tbody>${_facRows}</tbody></table>
      <div class="hint">总评分 = Σ(单项评分×权重)，范围 0–100；分越高=估值越便宜/越具吸引力。原始值口径：PE_TTM / PB_MRQ / PS_TTM / PEG_CAR / PCF_OCF_TTM（东财价值分析报表）。<b>注意这是「五因子综合分」，跟选股页筛选项「估值低·PE<20」是两把不同的尺</b>：那边只看 PE 一个指标、卡 0&lt;PE&lt;20 单条件筛股；这边是 PE/PB/PS/PEG/PCF 五因子加权，分≥75 才算「估值便宜」，≥55 中等。别拿综合分去对照筛选结果。</div></div></details>`;
    let valSec;
    if (v.ok) {
      const sc = v.score, cls = sc >= 75 ? "good" : (sc >= 55 ? "mid" : "low");
      const comps = (v.components || []).map((c) => `<div class="feat-comp">
        <span class="feat-comp-l">${esc(c.label)}</span>
        <span class="feat-bar"><span class="feat-bar-fill ${cls}" style="width:${c.score == null ? 0 : c.score}%"></span></span>
        <span class="feat-comp-v">${c.score == null ? "—" : c.score}</span></div>`).join("");
      valSec = `<div class="feat-score-wrap"><div class="feat-score ${cls}">${sc}<span class="feat-score-u">分</span></div>
        <div class="feat-verdict">${esc(v.verdict || "")}</div></div>
        <div class="wt-tw feat-metrics"><span><b>PE</b> ${wtMoney(v.pe)}</span><span><b>PB</b> ${wtMoney(v.pb)}</span>
          <span><b>PS</b> ${wtMoney(v.ps)}</span><span><b>PEG</b> ${wtMoney(v.peg)}</span>
          <span><b>现金流</b> ${wtMoney(v.pcf)}</span><span><b>股息率</b> ${dv && dv.yield_pct != null ? num1(dv.yield_pct) + "%" : "—"}</span></div>
        <div class="feat-comps">${comps}</div>
        ${facDetail}
        <div class="hint wt-foot">${esc(v.disclaimer || "")}</div>`;
    } else {
      valSec = `<div class="hint wt-foot">估值评分暂缺：${esc(v.reason || "无数据")}</div>`;
    }
    // —— 资金流向 ——
    let mfSec;
    if (m.ok) {
      const cell = (vv, lbl) => `<div class="feat-kpi"><div class="feat-kpi-v">${featMoney(vv)}</div><div class="feat-kpi-l">${lbl}</div></div>`;
      const recent = (m.recent || []).map((x) =>
        `<span class="feat-recent"><b>${esc(x.date || "")}</b> ${featMoney(x.main_net)}</span>`).join("");
      mfSec = `<div class="feat-kpis">${cell(m.main_net, "主力净流入")}${cell(m.huge_net, "超大单")}
        ${cell(m.big_net, "大单")}${cell(m.mid_net, "中单")}${cell(m.small_net, "小单")}</div>
        <div class="hint">${dataDayBadge(m.date, { intraday: m.is_intraday === true, time: m.data_time })}最新交易日：${esc(m.date || "—")}（净占比 ${wtPct(m.main_pct)}）· 数据源：${esc(m.source_label || m.source || "—")}</div>
        <div class="wt-tw"><div class="feat-recent-row">近 ${m.recent.length} 日：${recent || "—"}</div></div>`;
    } else {
      mfSec = `<div class="hint wt-foot">资金流向暂缺：${esc(m.reason || "无数据")}</div>`;
    }
    // —— 财务指标 ——
    const finRows = [
      dgFinRow("ROE（加权）", fin.roe == null ? null : num1(fin.roe), "%", fin.roe >= 10 ? "bull" : ""),
      dgFinRow("ROE（扣除）", fin.roe_deduct == null ? null : num1(fin.roe_deduct), "%"),
      dgFinRow("毛利率", fin.gross_margin == null ? null : num1(fin.gross_margin), "%"),
      dgFinRow("净利率", fin.net_margin == null ? null : num1(fin.net_margin), "%"),
      dgFinRow("总资产收益率 ROA", fin.roa == null ? null : num1(fin.roa), "%"),
      dgFinRow("营收", featMoney(fin.revenue), "", ""),
      dgFinRow("营收同比", fin.revenue_yoy == null ? null : num1(fin.revenue_yoy), "%",
        fin.revenue_yoy > 0 ? "bull" : (fin.revenue_yoy < 0 ? "bear" : "")),
      dgFinRow("净利润", featMoney(fin.net_profit), "", ""),
      dgFinRow("净利同比", fin.net_profit_yoy == null ? null : num1(fin.net_profit_yoy), "%",
        fin.net_profit_yoy > 0 ? "bull" : (fin.net_profit_yoy < 0 ? "bear" : "")),
      dgFinRow("每股收益 EPS", fin.eps == null ? null : Number(fin.eps).toFixed(2), " 元"),
      dgFinRow("每股经营现金流", fin.ocf_ps == null ? null : Number(fin.ocf_ps).toFixed(2), " 元"),
      dgFinRow("股息率（年度口径）", dv && dv.yield_pct != null ? num1(dv.yield_pct) : null, "%"),
    ].join("");
    const finSec = fin.report || Object.keys(fin).length
      ? `<div class="wt-tw"><table class="wt-tb dg-fin-tb"><tbody>${finRows}</tbody></table></div>
         ${dv ? `<div class="hint">分红口径：${esc(dv.note || dv.basis || "")}；报告期：${esc(fin.report || "—")}</div>` : ""}`
      : `<div class="hint wt-foot">财务指标暂缺（东财 F10 未取到或被限流，2 分钟内重试不再等待）。</div>`;
    // —— 公司概况 ——
    const orgSec = org ? `<div class="dg-org">
      <p><b>主营业务：</b>${esc(org.main_business || "—")}</p>
      <p><b>公司简介：</b><span class="dg-org-prof">${esc(org.profile || "—")}</span></p>
      ${org.business_scope ? `<details class="dg-org-more"><summary>经营范围（原文）</summary><p>${esc(org.business_scope)}</p></details>` : ""}
      <p class="muted">上市日期 ${esc(org.list_date || "—")} · 员工 ${org.emp_num == null ? "—" : org.emp_num} 人
        ${org.web ? ` · <a href="${escAttr(org.web.startsWith("http") ? org.web : "https://" + org.web)}" target="_blank" rel="noopener">${esc(org.web)}</a>` : ""}</p></div>`
      : `<div class="hint wt-foot">公司概况暂缺。</div>`;
    // —— 最近公告 ——
    const annRows = (d.announcements && d.announcements.rows || []).map((r) =>
      `<li><span class="dg-ann-d">${esc(r.time || "")}</span>
        <a href="${escAttr(r.url || "javascript:void(0)")}" target="_blank" rel="noopener">${esc(r.title || "")}</a></li>`).join("");
    const annSec = annRows ? `<ul class="dg-ann">${annRows}</ul>`
      : `<div class="hint wt-foot">公告暂缺：${esc((d.announcements && d.announcements.reason) || "无数据")}</div>`;
    body = `${strip}<div id="stkProfile" class="stk-info stk-profile-card" data-sym="${escAttr(d.symbol)}"><div class="s">公司简介加载中…</div></div>
      <div class="detail-sub">持股结构 —— 机构 / 产业资本 / 自然人 / 散户（季报口径）</div>
      <div id="stkHolders" class="stk-info stk-holders" data-sym="${escAttr(d.symbol)}"><div class="s">持股结构加载中…</div></div>${bfRow}${expert}${dgKlineShell(d.symbol)}
      <section class="dg-sec"><h3>估值评分</h3>${valSec}</section>
      <section class="dg-sec"><h3>资金流向（主力口径）</h3>${mfSec}</section>
      <section class="dg-sec"><h3>财务指标</h3>${finSec}</section>
      <section class="dg-sec"><h3>公司概况</h3>${orgSec}</section>
      <section class="dg-sec"><h3>最近公告</h3>${annSec}</section>
      <div class="hint wt-foot">${esc(d.disclaimer || "")}</div>`;
  }
  host.innerHTML = head + body;
  // 输入即跑：防抖 900ms，避免边打边发请求；回车立刻跑；开关面板时事件会随 DOM 一起扔掉，无泄漏。
  const sym = $("#dgSym");
  if (sym) {
    sym.oninput = () => {
      const v = sym.value.trim();
      clearTimeout(dgDebounce);
      if (/^\d{2,}$/.test(v)) dgDebounce = setTimeout(() => { valPoolPush(v); loadDiag(v); }, 900);
    };
    sym.onkeydown = (ev) => {
      if (ev.key !== "Enter") return;
      const v = sym.value.trim();
      if (v) { valPoolPush(v); loadDiag(v); }
    };
  }
  const rf = $("#dgRefresh"); if (rf) rf.onclick = () => loadDiag(null, true);
  // 「← 返回来路」：一键回到跳转前的页面（2026-09-30）
  const gb = $("#dgBack");
  if (gb) gb.onclick = () => {
    const o = NAV_BACK.origin;
    NAV_BACK.returning = true;
    if (o) selectNav(o.cat, o.tab, true); else renderDiag();
  };
  const pc = $("#valPoolClear"); if (pc) pc.onclick = () => { valPoolClear(); renderDiag(); };
  // K 线要在 innerHTML 落地之后再初始化（renderDiag 会整体重绘，每次都要重绑）
  if (!DIAG.loading && DIAG.data && DIAG.data.ok !== false && DIAG.data.symbol) dgKlineInit(DIAG.data.symbol);
  if (!DIAG.loading && DIAG.data && DIAG.data.ok !== false && DIAG.data.symbol) initStockF10(DIAG.data.symbol);
}

/* ================= 个股诊断内置 K 线（2026-09-30 新增，独立实例） =================
   王总要求：诊断页也要能直接看到 K 线。抽屉里那套（#klineWrap + 全局单例 _kv）与抽屉
   生命周期绑定，诊断页若复用同一批 DOM id，两边会互相抢 canvas、互相覆盖视口状态。
   故这里做**独立实例**：状态挂在 _DGV，容器用 dgKline* 前缀；配色 / 均线口径 /
   .kline-* 样式类 / 拖动缩放交互与抽屉保持一致，两边互不干扰。 */
const DGK = { symbol: "", period: "day", data: null };
let _DGV = null;              // 视口状态：{rows, ma, offset, count, period, adjust, firstDate, crossIdx, dragging}
let _dgkUpBound = false;
let _dgkResizeBound = false;
const DGK_MA = [5, 10, 20, 60];
const DGK_MA_COLOR = { 5: "#0E7490", 10: "#f59e0b", 20: "#a855f7", 60: "#64748b" };

function dgKlineMA(rows) {
  const out = {};
  for (const n of DGK_MA) {
    const a = new Array(rows.length).fill(null);
    let s = 0;
    for (let i = 0; i < rows.length; i++) {
      s += rows[i].close;
      if (i >= n) s -= rows[i - n].close;
      if (i >= n - 1) a[i] = s / n;
    }
    out[n] = a;
  }
  return out;
}

function dgKlineGeom() {
  const canvas = $("#dgKlineCanvas");
  if (!canvas || !_DGV) return null;
  const wrap = canvas.parentElement;
  const W = Math.max(260, Math.round((wrap && wrap.clientWidth) || 600));
  const H = klineCssH(W);
  const volH = Math.round(H * 0.20), gap = 10;
  const plotW = W - KLINE_PAD.l - KLINE_PAD.r;
  const priceH = H - KLINE_PAD.t - volH - gap - KLINE_PAD.b;
  const volTop = KLINE_PAD.t + priceH + gap;
  const slot = plotW / Math.max(1, _DGV.count);
  return { canvas, W, H, volH, gap, plotW, priceH, volTop, volBot: volTop + volH,
           slot, bodyW: Math.max(1.2, slot * 0.66) };
}

function dgKlineVisible() {
  if (!_DGV || !_DGV.rows.length) return [];
  return _DGV.rows.slice(_DGV.offset, _DGV.offset + _DGV.count);
}

function dgKlineClamp() {
  if (!_DGV) return;
  const maxOff = Math.max(0, _DGV.rows.length - _DGV.count);
  _DGV.offset = Math.min(maxOff, Math.max(0, _DGV.offset));
}

function updateDgKlineRange() {
  const el = $("#dgKlineRange");
  if (!el || !_DGV) return;
  const vis = dgKlineVisible();
  if (!vis.length) { el.textContent = "—"; return; }
  const adjust = _DGV.adjust === "raw" ? "不复权" : "前复权";
  const atEarliest = _DGV.offset <= 0;
  const atLatest = _DGV.offset + _DGV.count >= _DGV.rows.length;
  el.innerHTML = `${esc(vis[0].date)} ~ ${esc(vis[vis.length - 1].date)}`
    + ` · 可见 ${vis.length} / 共 ${_DGV.rows.length} 根 · ${adjust}`
    + (atEarliest ? ` · <b class="kline-edge">已到最早一根（${esc(_DGV.firstDate)}）</b>` : "")
    + (!atEarliest && atLatest ? ` · <b class="kline-edge">最新</b>` : "");
}

function dgKlineSnap(d) {
  const el = $("#dgKlineSnap");
  if (!el) return;
  const c = d.current || {};
  const up = Number(c.pct || 0) >= 0, cls = up ? "bull" : "bear", sign = up ? "+" : "";
  const per = d.period === "week" ? "周" : (d.period === "month" ? "月" : "");
  el.innerHTML = `
    <div class="ks-price ${cls}">${siNum(c.price)} <span class="ks-pct">${sign}${siNum(c.pct)}%</span>${per ? `<span class="ks-per">${per}涨跌</span>` : ""}</div>
    <div class="ks-grid">
      <div><span>${per ? per + "开" : "今开"}</span><b>${siNum(c.open)}</b></div>
      <div><span>${per ? per + "高" : "最高"}</span><b>${siNum(c.high)}</b></div>
      <div><span>${per ? per + "低" : "最低"}</span><b>${siNum(c.low)}</b></div>
      <div><span>${per ? per + "收" : "收/最新"}</span><b>${siNum(c.price)}</b></div>
      <div><span>成交量</span><b>${siVol(c.volume)}</b></div>
      <div><span>数据日</span><b>${esc((c.date || "").slice(0, 10))}</b></div>
    </div>
    <div class="ks-src">数据源：${esc(d.source || "新浪")}${d.adjust === "raw" ? " · <b>新浪日线·不复权</b>（除权日跳空属正常，非复权缺口）" : " · 前复权"}</div>`;
}

/* 主绘制：蜡烛 + 成交量副图 + MA5/10/20/60 + 价格/日期轴 + 十字光标（配色口径同抽屉 K 线）。 */
function dgKlinePaint() {
  const g = dgKlineGeom();
  if (!g || !_DGV) return;
  const { canvas } = g;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(g.W * dpr);
  canvas.height = Math.round(g.H * dpr);
  canvas.style.width = g.W + "px";
  canvas.style.height = g.H + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, g.W, g.H);

  const vis = dgKlineVisible();
  if (!vis.length) return;

  const cssVar = (n, fb) => {
    const v = getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    return v || fb;
  };
  const colUp = cssVar("--bull", "#ef4444"), colDn = cssVar("--bear", "#22c55e");
  const colGrid = cssVar("--border", "#334155"), colText = cssVar("--muted", "#94a3b8");
  const colPanel = cssVar("--panel", "#111827"), colAcct = cssVar("--accent", "#0E7490");

  const i0 = _DGV.offset, i1 = _DGV.offset + vis.length;
  let lo = Infinity, hi = -Infinity, vMax = 0;
  for (const r of vis) {
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
    if ((r.volume || 0) > vMax) vMax = r.volume || 0;
  }
  for (const n of DGK_MA) {
    const a = _DGV.ma[n] || [];
    for (let i = i0; i < i1; i++) {
      const v = a[i];
      if (v == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
  const mg = (hi - lo) * 0.06 || 1;
  lo -= mg; hi += mg;
  const yOf = (p) => KLINE_PAD.t + (hi - p) / (hi - lo) * g.priceH;
  const xOf = (k) => KLINE_PAD.l + g.slot * k + g.slot / 2;

  ctx.strokeStyle = colGrid; ctx.lineWidth = 1; ctx.fillStyle = colText;
  ctx.font = "11px ui-monospace, Consolas, monospace";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  for (let k = 0; k <= 5; k++) {
    const p = lo + (hi - lo) * k / 5, y = yOf(p);
    ctx.globalAlpha = 0.3;
    ctx.beginPath(); ctx.moveTo(KLINE_PAD.l, y); ctx.lineTo(g.W - KLINE_PAD.r, y); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(p.toFixed(p >= 1000 ? 0 : 2), 4, y);
  }
  ctx.fillStyle = colPanel; ctx.globalAlpha = 0.35;
  ctx.fillRect(KLINE_PAD.l, g.volTop, g.plotW, g.volBot - g.volTop);
  ctx.globalAlpha = 1;

  for (let k = 0; k < vis.length; k++) {
    const r = vis[k], x = xOf(k);
    const up = r.close >= r.open, col = up ? colUp : colDn;
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yOf(r.high)); ctx.lineTo(x, yOf(r.low)); ctx.stroke();
    const yO = yOf(r.open), yC = yOf(r.close);
    ctx.fillStyle = col;
    ctx.fillRect(x - g.bodyW / 2, Math.min(yO, yC), g.bodyW, Math.max(1, Math.abs(yC - yO)));
    const vh = vMax ? ((r.volume || 0) / vMax) * (g.volBot - g.volTop) : 0;
    ctx.globalAlpha = 0.55;
    ctx.fillRect(x - g.bodyW / 2, g.volBot - vh, g.bodyW, vh);
    ctx.globalAlpha = 1;
  }
  for (const n of DGK_MA) {
    const a = _DGV.ma[n] || [];
    ctx.strokeStyle = DGK_MA_COLOR[n] || colAcct;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    for (let i = i0; i < i1; i++) {
      const v = a[i];
      if (v == null) continue;
      const x = xOf(i - i0), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  }
  ctx.fillStyle = colText; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const step = Math.max(1, Math.floor(vis.length / 6));
  for (let k = 0; k < vis.length; k += step) {
    const lbl = _DGV.period === "day" ? vis[k].date.slice(0, 10) : vis[k].date.slice(0, 7);
    ctx.fillText(lbl, xOf(k), g.H - 11);
  }
  ctx.textAlign = "left"; ctx.font = "11px sans-serif";
  let lx = KLINE_PAD.l + 4;
  for (const n of DGK_MA) {
    ctx.fillStyle = DGK_MA_COLOR[n]; ctx.fillRect(lx, KLINE_PAD.t - 8, 10, 3);
    ctx.fillStyle = colText; ctx.fillText("MA" + n, lx + 13, KLINE_PAD.t - 6);
    lx += 56;
  }
  if (_DGV.crossIdx != null && _DGV.crossIdx >= 0 && _DGV.crossIdx < vis.length) {
    const r = vis[_DGV.crossIdx], cx = xOf(_DGV.crossIdx), cy = yOf(r.close);
    ctx.save();
    ctx.strokeStyle = colText; ctx.globalAlpha = 0.55; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(cx, KLINE_PAD.t); ctx.lineTo(cx, g.volBot); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(KLINE_PAD.l, cy); ctx.lineTo(g.W - KLINE_PAD.r, cy); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = colAcct; ctx.fillRect(2, cy - 8, 50, 16);
    ctx.fillStyle = "#ffffff"; ctx.textAlign = "center";
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.fillText(r.close.toFixed(2), 27, cy + 1);
    ctx.textAlign = "left";
  }
  updateDgKlineRange();
}

function dgKlineShift(dir) {
  if (!_DGV) return;
  const maxOff = Math.max(0, _DGV.rows.length - _DGV.count);
  const step = Math.max(1, Math.round(_DGV.count * 0.8));
  _DGV.offset = Math.min(maxOff, Math.max(0, _DGV.offset + dir * step));
  _DGV.crossIdx = null;
  dgKlinePaint();
  const tip = $("#dgKlineTip"); if (tip) tip.hidden = true;
}

/* 交互：拖动平移 / 滚轮缩放 / 悬停十字光标（逻辑与抽屉 K 线一致，作用域独立）。 */
function dgKlineBind() {
  const canvas = $("#dgKlineCanvas"), tip = $("#dgKlineTip");
  if (!canvas || !_DGV) return;
  canvas.style.cursor = "crosshair";

  canvas.onmousedown = (e) => {
    if (e.button !== 0 || !_DGV) return;
    _DGV.dragging = true;
    _DGV.dragX0 = e.clientX;
    _DGV.dragOff0 = _DGV.offset;
    _DGV.crossIdx = null;
    if (tip) tip.hidden = true;
    canvas.style.cursor = "grabbing";
    e.preventDefault();
  };

  canvas.onmousemove = (e) => {
    const g = dgKlineGeom();
    if (!g || !_DGV) return;
    if (_DGV.dragging) {
      const deltaBars = Math.round((e.clientX - _DGV.dragX0) / g.slot);
      _DGV.offset = _DGV.dragOff0 - deltaBars;
      dgKlineClamp();
      dgKlinePaint();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.floor((mx - KLINE_PAD.l) / g.slot);
    if (mx < KLINE_PAD.l || idx < 0 || idx >= _DGV.count) {
      if (_DGV.crossIdx !== null) { _DGV.crossIdx = null; dgKlinePaint(); }
      if (tip) tip.hidden = true;
      return;
    }
    if (_DGV.crossIdx !== idx) { _DGV.crossIdx = idx; dgKlinePaint(); }
    const vis = dgKlineVisible();
    const r = vis[idx];
    if (!r) return;
    if (tip) {
      const up = r.close >= r.open;
      const sign = (r.pct || 0) >= 0 ? "+" : "";
      tip.hidden = false;
      tip.innerHTML = `<b>${esc(r.date)}</b><br>开 ${siNum(r.open)} 高 ${siNum(r.high)}<br>`
        + `低 ${siNum(r.low)} 收 ${siNum(r.close)}<br>`
        + `<span class="${up ? "bull" : "bear"}">${sign}${siNum(r.pct)}%</span> 量 ${siVol(r.volume)}`;
      const x = KLINE_PAD.l + g.slot * idx + g.slot / 2;
      const tw = tip.offsetWidth || 160;
      let left = x + 12;
      if (left + tw > g.W) left = x - tw - 12;
      if (left < 0) left = 4;
      tip.style.left = left + "px";
      tip.style.top = (KLINE_PAD.t + 8) + "px";
    }
  };

  canvas.onmouseup = () => {
    if (!_DGV) return;
    _DGV.dragging = false;
    if (canvas.style.cursor === "grabbing") canvas.style.cursor = "crosshair";
  };
  canvas.onmouseleave = () => {
    if (!_DGV) return;
    _DGV.dragging = false;
    _DGV.crossIdx = null;
    canvas.style.cursor = "crosshair";
    dgKlinePaint();
    if (tip) tip.hidden = true;
  };
  canvas.onwheel = (e) => {
    const g = dgKlineGeom();
    if (!g || !_DGV) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const frac = Math.min(1, Math.max(0, (mx - KLINE_PAD.l) / g.plotW));
    const anchor = _DGV.offset + frac * _DGV.count;
    const factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    const newCount = Math.round(Math.min(_DGV.rows.length, Math.max(20, _DGV.count * factor)));
    if (newCount === _DGV.count) return;
    _DGV.count = newCount;
    _DGV.offset = Math.round(anchor - frac * newCount);
    dgKlineClamp();
    _DGV.crossIdx = null;
    dgKlinePaint();
  };
  if (!_dgkUpBound) {
    // 拖到画布外再松手也要结束拖动（window 上只绑一次，避免 renderDiag 重复绑定堆积）
    window.addEventListener("mouseup", () => {
      if (!_DGV) return;
      _DGV.dragging = false;
      const c = $("#dgKlineCanvas"); if (c && c.style.cursor === "grabbing") c.style.cursor = "crosshair";
    });
    _dgkUpBound = true;
  }
}

function dgKlineApply(d) {
  const rows = d.klines || [];
  const snap = $("#dgKlineSnap");
  if (!rows.length) {
    _DGV = null;
    if (snap) snap.innerHTML = `<span class="s">暂无 K 线数据</span>`;
    return;
  }
  const keep = Math.min((_DGV && _DGV.count) || 120, rows.length);
  _DGV = {
    rows, ma: dgKlineMA(rows), count: keep,
    offset: Math.max(0, rows.length - keep),
    period: d.period || "day", adjust: d.adjust || "qfq",
    firstDate: d.first_date || (rows[0] && rows[0].date) || "",
    crossIdx: null, dragging: false,
  };
  dgKlineSnap(d);
  dgKlinePaint();
  dgKlineBind();
}

function dgKlineLoad(symbol, period, force) {
  const snap = $("#dgKlineSnap");
  // 已缓存「同一只票 + 同一周期」→ 直接重绘重绑（renderDiag 每次整体重绘都会调到这里）。
  // 注意：判断必须用**数据自身的 period**（DGK.data.period），不能用 DGK.period
  // ——后者会被切换按钮提前改写，导致切周/月 K 时误判成"缓存命中"而画回旧周期。
  if (!force && DGK.data && DGK.symbol === symbol && DGK.data.period === period) {
    dgKlineApply(DGK.data);
    return;
  }
  if (snap) snap.innerHTML = `<span class="s">行情加载中…</span>`;
  // 2026-10-22：日K 走缓存优先（market_cache/kline，命中本地缓存毫秒级，等同原「行情速览面板」体感）；
  // 周/月K 缓存层只存日K，仍走原 /api/kline 现抓（后端自身有缓存）。
  const useCache = period === "day";
  const url = useCache
    ? `/api/market_cache/kline?symbol=${encodeURIComponent(symbol)}`
    : `/api/kline?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}`;
  api(url)
    .then((d) => {
      if (!d || d.error || !d.klines) {
        if (snap) snap.innerHTML = `<span class="s" style="color:var(--bear)">行情获取失败：${esc((d && d.error) || "无返回")}</span>`;
        return;
      }
      if (useCache) { d.period = "day"; d.adjust = d.adjust || "qfq"; }  // 补字段，保证缓存判断一致
      DGK.data = d; DGK.symbol = symbol; DGK.period = period;
      dgKlineApply(d);
    })
    .catch((e) => {
      if (snap) snap.innerHTML = `<span class="s" style="color:var(--bear)">行情获取失败：${esc(apiErrText(e))}</span>`;
    });
}

function dgKlineShell(sym) {
  return `<section class="dg-sec"><h3>K 线走势</h3>
  <div id="dgKline" data-sym="${escAttr(sym)}">
    <div class="kline-snap" id="dgKlineSnap"><span class="s">行情加载中…</span></div>
    <div class="kline-canvas-wrap"><canvas id="dgKlineCanvas"></canvas><div class="kline-tip" id="dgKlineTip" hidden></div></div>
    <div class="kline-bar">
      <div class="kline-periods">
        <button class="kbtn active" data-p="day">日K</button>
        <button class="kbtn" data-p="week">周K</button>
        <button class="kbtn" data-p="month">月K</button>
      </div>
      <div class="kline-tools">
        <button class="btn sm" id="dgKlineOlder" title="往回看更早的 K 线">◀ 更早</button>
        <button class="btn sm" id="dgKlineNewer" title="往近看更新的 K 线">更晚 ▶</button>
        <span class="kline-range" id="dgKlineRange">—</span>
        <button class="btn sm" id="dgKlineLatest">回到最新</button>
      </div>
    </div>
    <p class="dg-kline-hint">按住拖动平移（向右拖＝往回看更早）· 滚轮缩放 · 悬停看当日明细。
      数据源：东方财富（前复权）→ 新浪 → 腾讯自动兜底。</p>
  </div></section>`;
}

function dgKlineInit(symbol) {
  const wrap = $("#dgKline");
  if (!wrap) return;
  _DGV = null;
  const periodBtns = $$("#dgKline .kbtn");
  periodBtns.forEach((b) => {
    b.onclick = () => {
      periodBtns.forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      // 不在这里预设 DGK.period（否则会污染 dgKlineLoad 的缓存判断）；加载成功后由它自己写
      dgKlineLoad(symbol, b.dataset.p, false);
    };
  });
  const ob = $("#dgKlineOlder"); if (ob) ob.onclick = () => dgKlineShift(-1);
  const nb = $("#dgKlineNewer"); if (nb) nb.onclick = () => dgKlineShift(1);
  const lt = $("#dgKlineLatest");
  if (lt) lt.onclick = () => {
    if (!_DGV) return;
    _DGV.offset = Math.max(0, _DGV.rows.length - _DGV.count);
    _DGV.crossIdx = null;
    dgKlinePaint();
    const tip = $("#dgKlineTip"); if (tip) tip.hidden = true;
  };
  if (!_dgkResizeBound) {
    window.addEventListener("resize", () => { if (_DGV) dgKlinePaint(); });
    _dgkResizeBound = true;
  }
  // 重绘后保持用户上次选的周期
  const cur = DGK.period || "day";
  periodBtns.forEach((b) => b.classList.toggle("active", b.dataset.p === cur));
  dgKlineLoad(symbol, cur, false);
}

/* ---------- 研报 / 机构评级 ---------- */
/* 2026-10-01：王总反馈——研报一大堆，要的是「结论 + 排序」，不是把研报再列一遍。
   于是面板顶部新增「研报结论」区（后端 research_insight.py 六因子打分排序），
   下面保留原始研报清单（折叠）供逐条核对。 */
let _riRetry = 0;
function loadResearch() {
  api("/api/research_reports?pz=30").then((d) => { FEAT.research.data = d; renderResearch(); })
    .catch((e) => reportLoadError("研报", e));
  loadResearchInsight();
}
function loadResearchInsight() {
  api("/api/research/insight?days=60").then((d) => {
    if (d && d.ok) { FEAT.research.insight = d; _riRetry = 0; }
    else FEAT.research.insight = d;   // 记下 warming / 失败原因，让结论区能显示「生成中」而不是整块空白
    renderResearch();
  }).catch((e) => renderResearch());
}
/* 首跑后台要抓 600 份研报（~1 分钟），期间后端返回 warming，前端轮询 */
function pollInsight() {
  _riRetry += 1;
  if (_riRetry > 25) return;
  setTimeout(() => {
    api("/api/research/insight?days=60").then((d) => {
      if (d && d.ok) { FEAT.research.insight = d; _riRetry = 0; }
      else FEAT.research.insight = d;
      renderResearch();
      if (!d || !d.ok) pollInsight();
    }).catch(() => pollInsight());
  }, 4000);
}

/* 后端少量文案里带 **加粗**，前端统一转义后再转 <b> */
function riBold(s) {
  return esc(s == null ? "" : s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

/* 结论区：总览 + 主线 + 排序名单 */
function renderInsightBlock() {
  const iv = FEAT.research.insight;
  if (!iv || !iv.ok) {
    // 一期都没拿到结论（服务刚重启 / 后台还在抓 600 份研报）时，必须给明确提示，
    // 否则这 1 分钟里整块是空白，看上去像面板坏了。
    if (iv && iv.warming) {
      return `<div class="ri-wrap"><div class="ri-loading">正在把近期研报压成结论（约需 1 分钟，页面不会卡，会自己刷新出来）…</div></div>`;
    }
    if (iv && iv.reason) return `<div class="ri-wrap"><div class="ri-loading muted">结论暂时不可用：${esc(iv.reason)}</div></div>`;
    return `<div class="ri-wrap"><div class="ri-loading">正在把近期研报压成结论（约需 1 分钟）…</div></div>`;
  }
  const f = FEAT.research.riFilter || "all";
  const th = FEAT.research.riTheme || "";
  let list = (iv.picks || []).slice();
  if (f === "d") list = list.filter((p) => p.grade === "D");
  else if (f === "s") list = list.filter((p) => p.grade === "S");
  else if (f === "a") list = list.filter((p) => p.grade === "A");
  else if (f === "b") list = list.filter((p) => p.grade === "B");
  else if (f === "c") list = list.filter((p) => p.grade === "C");
  else if (f === "short") list = list.filter((p) => (p.holdings || []).includes("短线进攻"));
  else if (f === "mid") list = list.filter((p) => (p.holdings || []).includes("中线埋伏"));
  else if (f === "long") list = list.filter((p) => (p.holdings || []).includes("长线配置"));
  if (th) list = list.filter((p) => (p.themes || []).includes(th));
  // 档内连续序号：后端 rank_in_grade 是按全量 513 只编的，前端只展示前 50 条会断号，
  // 所以这里按「当前筛选结果 + 档位」重新从 1 数，用户看到的就是这一档里的第几名。
  const seq = {};
  list = list.map((p) => { seq[p.grade] = (seq[p.grade] || 0) + 1; p._seq = seq[p.grade]; return p; });
  const chip = (k, t, on) => `<a class="ri-chip${on ? " on" : ""}" data-ri-f="${k}">${esc(t)}</a>`;
  const thChips = (iv.themes || []).slice(0, 6).map((x) =>
    `<a class="ri-chip${th === x.theme ? " on" : ""}" data-ri-th="${escAttr(x.theme)}">${esc(x.theme)} <b>${x.n}</b></a>`).join("");
  const rows = list.map((p) => {
    const up = p.upside;
    const upCls = up == null ? "ri-mut" : up >= 0 ? "up" : "down";
    const pctTxt = p.pct != null ? `${p.pct >= 0 ? "+" : ""}${p.pct.toFixed(1)}%` : "—";
    const bars = Object.entries(p.factors || {}).map(([k, v]) =>
      `<div class="ri-fc"><span class="ri-fcl">${esc(k)}</span><span class="ri-fbar"><i style="width:${Math.max(0, Math.min(100, v / 30 * 100))}%"></i></span><span class="ri-fcv">${v}</span></div>`).join("");
    const hold = (p.holdings || []).map((h) => `<span class="ri-hold h${h === "短线进攻" ? "s" : h === "中线埋伏" ? "m" : h === "长线配置" ? "l" : h === "目标价弹性" ? "e" : "w"}">${esc(h)}</span>`).join("");
    return `<div class="ri-row g${p.grade}">
      <div class="ri-rk" title="该分档内第 ${p._seq} 名（全表第 ${p.rank} 名）">${p._seq}</div>
      <div class="ri-main">
        <div class="ri-t1"><span class="ri-badge g${p.grade}">${esc(p.grade)}</span>
          <span class="ri-nm">${esc(p.name || p.code)}</span>
          <span class="ri-cd">${codeLink(p.code)}</span>
          <span class="ri-pt ${p.pct >= 0 ? "up" : "down"}" title="当日涨跌">${pctTxt}</span>
          ${p.price ? `<span class="ri-pr">${p.price}</span>` : ""}
          ${hold}
        </div>
        <div class="ri-l1">${esc(p.one_liner)}</div>
        <div class="ri-m1">
          <span>综合 <b>${p.score}</b></span>
          <span class="${upCls}">空间 ${up == null ? "—" : (up >= 0 ? "+" : "") + up.toFixed(1) + "%"}</span>
          <span>${p.n_orgs} 家 / ${p.n_reports} 份</span>
          <span>预测PE ${p.pe_mean == null ? "—" : p.pe_mean}</span>
          <span>${p.age_days} 天前(${esc(p.latest_date || "")})</span>
          ${p.theme ? `<span class="ri-th" title="${escAttr(p.themes.join(" / "))}">主线 ${esc(p.theme)}${p.theme_reliable ? "" : "（弱归类）"}</span>` : ""}
          ${p.target_stale ? `<span class="ri-warn" title="目标价发布已超 30 天，可能未随股价下修">目标价滞后</span>` : ""}
          ${p.n_orgs <= 1 ? `<span class="ri-warn" title="仅 1 家券商覆盖，单点证据">单点研报</span>` : ""}
          <a class="ri-more" data-ri-ex="${escAttr(p.code)}">依据 ▾</a>
        </div>
        <div class="ri-det" id="riDet_${escAttr(p.code)}" hidden>
          <div class="ri-fcs">${bars}</div>
          <div class="ri-vd">${esc(p.verdict)}</div>
          <div class="ri-orgs">覆盖机构：${esc((p.orgs || []).join("、") || "—")}</div>
          <div class="ri-orgs">最新研报标题：${esc(p.latest_title || "—")}</div>
        </div>
      </div>
    </div>`;
  }).join("") || `<div class="ri-empty muted">当前筛选条件下没有个股（试试切回「全部」或换条主线）。</div>`;

  return `<div class="ri-wrap">
    <div class="ri-head"><h2>研报结论 · 自动排序</h2><div class="ri-meta">${esc(iv.generated)}${iv.cached ? "" : "（刚生成）"} · 窗口 ${iv.window_days} 天 · ${iv.total_reports} 份研报 / ${iv.n_stocks} 只个股${iv.cost_sec ? " · 耗时 " + iv.cost_sec + "s" : ""}</div></div>
    <div class="ri-sum">${riBold(iv.summary_text)}</div>
    <div class="ri-line">主线</div><div class="ri-chips">${thChips}</div>
    <div class="ri-line">分档</div><div class="ri-chips">
      ${chip("all", "全部", f === "all")}
      ${chip("s", `核心机会 ${iv.dist_grade.S || 0}`, f === "s")}
      ${chip("a", `短线进攻 ${iv.dist_grade.A || 0}`, f === "a")}
      ${chip("b", `中线埋伏 ${iv.dist_grade.B || 0}`, f === "b")}
      ${chip("c", `仅观察 ${iv.dist_grade.C || 0}`, f === "c")}
      ${chip("d", `回避（研报） ${iv.dist_grade.D || 0}`, f === "d")}
      ${chip("short", "只看·短线进攻", f === "short")}
      ${chip("mid", "只看·中线埋伏", f === "mid")}
      ${chip("long", "只看·长线配置", f === "long")}
    </div>
    <div class="ri-list">${rows}</div>
    <div class="ri-foot">打分口径：一致看多 20 / 目标价空间 25 / 覆盖广度 12 / 研报新鲜度 13 / 预测估值 8 / 主线热度 10。券商研报天然偏多、负面报告极少发布，本表只比**相对强弱**，不构成投资建议。</div>
  </div>`;
}
function renderResearch() {
  const host = $("#p-research"); if (!host) return;
  const rs = FEAT.research, d = rs.data || {};
  const rt = rs.rating;
  // 仅看带目标价的研报（默认关；业内新趋势：多数报告无目标价，靠预测 PE 替代估值）
  const fOnly = '<label class="wt-chk"><input type="checkbox" id="rsOnlyTar"' +
    (rs.onlyTar ? " checked" : "") + '>仅看带目标价的</label>';
  // EPS / PE 兜底列 —— 业内新版研报多不填目标价，改用预测当年 PE 替代估值（必备）
  const rows = (d.rows || []).filter((r) => rs.onlyTar ? r.has_target : true).map((r) => `<tr>
    <td class="wt-c1"><a class="wt-nm" href="${r.url ? escAttr(r.url) : "javascript:void(0)"}" target="_blank" rel="noopener">${esc(r.title || "")}</a></td>
    <td>${esc(r.org || "")}</td><td>${esc(r.rating || "")}</td>
    <td>${r.has_target ? wtMoney(r.target) : '<span class="muted" title="该报告未给目标价（业内新版趋势）">—</span>'}</td>
    <td>${r.pe != null ? wtMoney(r.pe) : '<span class="muted">—</span>'}</td>
    <td>${esc(r.date || "")}</td>
    <td>${r.stock_code ? `<button type="button" class="wt-sym" data-feat-open="${escAttr(r.stock_code)}">${esc(r.stock_code)}</button> ${esc(r.stock || "")}` : esc(r.stock || "")}</td>
  </tr>`).join("") || `<tr><td colspan="7" class="muted wt-empty">${d.ok === false ? "接口异常：" + esc(d.reason || "未取到数据") : (rs.onlyTar ? "勾选状态下无带目标价研报。取消勾选可看全部（多数券商新版报告用预测 PE 替代目标价）。" : "暂无研报数据。")}</td></tr>`;
  const rtKpi = () => {
    const dist = rt.dist || {}, tot = rt.total_orgs || 1;
    const bar = (n, lbl, c) => `<div class="feat-comp"><span class="feat-comp-l">${lbl} ${n}</span>
      <span class="feat-bar"><span class="feat-bar-fill ${c}" style="width:${tot ? Math.round(n / tot * 100) : 0}%"></span></span></div>`;
    const tStat = (rt.target != null)
      ? `${wtMoney(rt.target)} <span class="feat-kpi-s">（${rt.n_with_target}/${rt.total_orgs} 份带）</span>`
      : `<span class="muted" title="近半年内 ${rt.total_orgs} 份评 buy 但均未给目标价（业内新版趋势）">—</span>`;
    const pStat = (rt.pe_avg != null)
      ? `${wtMoney(rt.pe_avg)} 倍`
      : '<span class="muted">—</span>';
    return `<div class="feat-kpis"><div class="feat-kpi"><div class="feat-kpi-v">${wtMoney(rt.price)}</div><div class="feat-kpi-l">现价</div></div>
      <div class="feat-kpi"><div class="feat-kpi-v">${tStat}</div><div class="feat-kpi-l">一致目标价</div></div>
      <div class="feat-kpi"><div class="feat-kpi-v">${pStat}</div><div class="feat-kpi-l">平均预测PE</div></div>
      <div class="feat-kpi"><div class="feat-kpi-v">${rt.total_orgs}</div><div class="feat-kpi-l">研报数</div></div></div>
      ${bar(dist.buy || 0, "买入", "good")}${bar(dist.overweight || 0, "增持", "good")}
      ${bar(dist.neutral || 0, "中性", "mid")}${bar(dist.underweight || 0, "减持", "low")}${bar(dist.sell || 0, "卖出", "low")}
      <div class="hint">综合评级：${esc(rt.rating || "—")}</div>`;
  };
  // 2026-10-30 D33：rt.ok=true 但 total_orgs=0（例如输入 12345：数字合法、A 股里没有这只）
  // 原来会渲染一整屏全是「—」的 KPI 卡片，看着像"数据坏了"。按覆盖数分流，两件事别混：
  //   ① total_orgs=0 → 「查无研报覆盖」一句话；② ok=false → 才是接口异常（后端原话照转）。
  const symTxt = (rt && (rt.symbol || rs.symbol)) || "该代码";
  const rtHtml = (rt && rt.ok)
    ? ((rt.total_orgs || 0) > 0 ? rtKpi()
      : `<div class="hint wt-foot">查无研报覆盖记录：${esc(symTxt)} 近半年没有机构评级——`
        + `可能不是 A 股代码，或暂无券商覆盖。</div>`)
    : (rt && rt.ok === false && rt.reason
      ? `<div class="hint wt-foot">${esc(rt.reason)}${rt.symbol ? `（当前输入：${esc(rt.symbol)}）` : ""}</div>`
      : `<div class="hint wt-foot">输入代码查询单只机构一致预测评级分布。</div>`);
  const nWith = d.n_with_target || 0, nTotal = d.n_total || 0;
  const explainer = nTotal ? `（本次 ${nTotal} 份研报中 ${nWith} 份带目标价，余下用「预测 PE」替代——业内 2024 起券商新版报告普遍不再硬填目标价。）` : "";
  host.innerHTML = `
    ${renderInsightBlock()}
    <div class="wt-head" style="margin-top:18px"><h2 style="font-size:16px">全市场最新研报（原始清单）</h2></div>
    <div class="wt-tabs"><a class="wt-tab active">全市场最新研报</a></div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>标题</th><th>机构</th><th>评级</th><th>目标价</th><th>预测PE</th><th>日期</th><th>个股</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="wt-head" style="margin-top:16px"><h2 style="font-size:16px">单只机构评级</h2></div>
    <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
      <div class="wt-form" style="flex:0 1 340px;grid-template-columns:1fr auto;margin:0">
        <label>代码<input id="rsSym" value="${escAttr(rs.symbol || "")}" placeholder="如 600519（回车即查）"
          inputmode="numeric" autocomplete="off" maxlength="8"></label>
        <button type="button" class="btn primary" id="rsGo">查询</button>
      </div>
      ${fOnly}
    </div>
    <div id="rsRating">${rtHtml}</div>
    <div class="hint wt-foot">研报/评级来自东财公开数据，仅供参考，不构成投资建议。${explainer}</div>`;
  // 2026-10-30 D33：查询动作抽成具名函数，按钮与回车共用同一份逻辑（原来只有 onclick，
  // 且控件本身从没进过 innerHTML，整块是死的）。
  const rsGo = () => {
    const s = $("#rsSym");
    const v = ((s && s.value) || "").trim();
    if (!v) {                                   // 空输入 → 就地提示，不再静默 return
      // 2026-10-30 D33：必须把 rs.symbol 一起清空。输入框的 value 取的就是 rs.symbol，
      // 不清的话重渲染会把用户刚删掉的旧代码填回框里，而提示又写着「当前输入：旧代码」，
      // 看起来像"我明明清空了它自己又变回来了"。清空后提示不再带「当前输入」后缀。
      rs.symbol = "";
      rs.rating = { ok: false, symbol: "", reason: "请输入 A 股数字代码" };
      renderResearch();
      return;
    }
    rs.symbol = v;
    api(`/api/research_rating?symbol=${encodeURIComponent(rs.symbol)}`).then((x) => { rs.rating = x; renderResearch(); })
      .catch((e) => reportLoadError("机构评级", e));
  };
  const go = $("#rsGo"); if (go) go.onclick = rsGo;
  const symInp = $("#rsSym");
  if (symInp) {
    symInp.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); rsGo(); } };
  }
  const chk = $("#rsOnlyTar"); if (chk) chk.onchange = () => { rs.onlyTar = chk.checked; renderResearch(); };
  // 结论区交互：分档 / 主线筛选 +「依据」展开
  $$("#p-research .ri-chip").forEach((el) => el.onclick = (ev) => {
    ev.preventDefault();
    if (el.dataset.riF !== undefined) rs.riFilter = el.dataset.riF;
    if (el.dataset.riTh !== undefined) rs.riTheme = el.dataset.riTh;
    renderResearch();
  });
  $$("#p-research .ri-more").forEach((el) => el.onclick = (ev) => {
    ev.preventDefault();
    const d = $("#riDet_" + CSS.escape(el.dataset.riEx || ""));
    if (d) { d.hidden = !d.hidden; el.textContent = d.hidden ? "依据 ▾" : "依据 ▴"; }
  });
  // 首跑/服务重启后第一次打开时，后端还在后台抓研报（返回 warming 甚至还没返回过），
  // 这里必须起轮询——否则结论区会一直空着（iv 为 null 时上面的 if 也拦不住）。
  const iv = FEAT.research.insight;
  if ((!iv || iv.warming) && _riRetry === 0) pollInsight();
  $$("#p-research [data-feat-open]").forEach((el) => el.onclick = (ev) => { ev.preventDefault(); openStock(el.dataset.featOpen, el); });
}

/* ---------- 多资产行情 ---------- */
function loadMultiquote() {
  api("/api/multiquote_preset").then((d) => { FEAT.multiquote.data = d; renderMultiquote(); })
    .catch((e) => reportLoadError("多资产", e));
}
function renderMultiquote() {
  const host = $("#p-multiquote"); if (!host) return;
  const mq = FEAT.multiquote, base = (mq.data && mq.data.rows) || [], added = mq.added || [];
  const data = mq.data || {};
  // 板块过滤（默认"全部"）
  const allSegs = Array.from(new Set([...base.map(r => r.market || ""), ...added.map(r => r.market || "")])).filter(Boolean);
  const filterSeg = mq.filterSeg || "全部";
  const baseFiltered = filterSeg === "全部" ? base : base.filter(r => r.market === filterSeg);
  const addedFiltered = filterSeg === "全部" ? added : added.filter(r => r.market === filterSeg);

  // 仅「A 股/基金/港股数字代码」挂详情入口；美股(usAAPL)/债券(其他前缀)→ 显示跨市场不展示标签，避免点了空开
  const mkCodeCell = (r) => {
    const c = (r.code || "").trim();
    const isA = /^\d{6}$/.test(c);                                  // A 股 / ETF / 基金
    const isHK = /^\d{4,5}$/.test(c) && Number(c) > 0 && Number(c) <= 99999;  // 港股
    if (isA || isHK) return `<button type="button" class="wt-sym" data-feat-open="${escAttr(c)}">${esc(c)}</button>`;
    if (c) return `<span class="wt-sym-disabled" title="跨市场标的暂不在详情范围（美股/债券）">${esc(c)}</span>`;
    return "—";
  };
  // 数据源小徽标（push2 / tencent / —）：让王总一眼看出哪只取了哪只没取
  const srcBadge = (src) => {
    if (src === "push2") return '<span class="src-tag src-push2">push2</span>';
    if (src === "tencent") return '<span class="src-tag src-tencent">tencent</span>';
    if (!src || src === "—") return '<span class="src-tag src-empty" title="该标的在所有接口都取不到，可能代码不存在或两源全断">— 断</span>';
    return "";
  };
  const row = (r) => `<tr>
    <td class="wt-c1">${mkCodeCell(r)}</td>
    <td>${esc(r.name || "")}<div class="muted" style="font-size:11px;margin-top:2px">${esc(r.market || "")}</div></td>
    <td>${wtMoney(r.price)}</td><td>${wtPct(r.pct)}</td>
    <td>${srcBadge(r.src)}</td></tr>`;
  const allRows = baseFiltered.concat(addedFiltered);
  const rows = allRows.map(row).join("") ||
    `<tr><td colspan="5" class="muted wt-empty">${data.ok === false ? "接口异常：" + esc(data.reason || "未取到数据") : "暂无数据。"}</td></tr>`;
  const segOpts = ['全部'].concat(allSegs);
  const segTabs = segOpts.map(s => `<a class="wt-tab${filterSeg === s ? " active" : ""}" data-mq-seg="${escAttr(s)}">${esc(s)}</a>`).join("");
  const nOk = data.n_ok != null ? `（数据源：${esc(data.src || "—")} · 命中 ${data.n_ok}/${allRows.length}）` : "";
  host.innerHTML = `
    <div class="wt-head"><h2>多资产行情 <span class="muted wt-sub">指数ETF / 黄金 / 国债 / 美股</span></h2>
      <div class="wt-actions">
        <div class="wt-form" style="grid-template-columns:auto auto">
          <label style="flex-direction:row;align-items:center;gap:6px">代码<input id="mqAdd" placeholder="如 510300 / usAAPL" style="width:130px"></label>
          <button class="btn primary" id="mqGo">添加</button>
          <button class="btn sm" id="mqRefresh" title="重试：push2 限流时隔 30~60s 再点">🔄 重试</button>
        </div>
      </div>
    </div>
    <div class="wt-tabs">${segTabs}</div>
    <div class="wt-tw"><table class="wt-tb"><thead><tr>
      <th>代码</th><th>名称 / 板块</th><th>现价</th><th>涨跌</th><th>数据源</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="hint wt-foot">覆盖 A股/ETF/债券/美股（代码如 usAAPL）；点「添加」把想盯的资产纳入此表（仅本机会话内）。
     <span class="muted">${nOk}</span> 接口顺序：push2 → push2his → 腾讯（终极 fallback），仍为"— 断"请检查代理 / VPN。</div>`;
  $$("#p-multiquote [data-mq-seg]").forEach((el) => el.onclick = () => {
    mq.filterSeg = el.dataset.mqSeg; renderMultiquote();
  });
  const go = $("#mqGo"); if (go) go.onclick = () => {
    const v = $("#mqAdd"); if (!v || !v.value.trim()) return;
    api(`/api/multiquote_lookup?symbol=${encodeURIComponent(v.value.trim())}`).then((x) => {
      if (x.ok) {
        mq.added.push({ code: x.symbol, name: x.name, price: x.price, pct: x.pct,
          market: /^\d{6}$/.test(x.symbol || "") ? "自添" : "美股", src: x.src || "tencent" });
        renderMultiquote();
      } else setHint("该代码未取到行情：" + (x.symbol || "") + " — " + (x.reason || ""));
    }).catch((e) => reportLoadError("多资产查询", e));
  };
  const rf = $("#mqRefresh"); if (rf) rf.onclick = () => loadMultiquote();
  bindFeatStockClicks("#p-multiquote");
}

/* ---------- 个股快讯 / 公告（2026-09-30 从「资讯公告」独立成并列面板）---------- */
/* 旧「全市场快讯」已由「政策风向」(#policy) 取代；本面板只保留个股口径，深链接 #info/news 不变。 */
function loadNews() {
  const n = FEAT.news;
  if (n.loading) return;
  n.loading = true;
  api("/api/watchdesk").then((d) => {
    n.loading = false;
    n.data = d;
    const rows = d.rows || [];
    if (!n.sel || !rows.some((r) => r.code === n.sel)) n.sel = rows.length ? rows[0].code : "";
    if (!n.sel) return null;
    return api("/api/watchdesk/detail?code=" + encodeURIComponent(n.sel))
      .then((dd) => { n.detail = dd; });
  }).then(() => { n.loading = false; renderNews(); })
    .catch((e) => { n.loading = false; n.data = { ok: false, reason: String((e && e.message) || e) }; renderNews(); });
}
window.newsPick = function (code) { FEAT.news.sel = code; loadNews(); };
/* 风险/机会强度小横条（0-10 分） */
function newsBar(v) {
  const p = Math.max(0, Math.min(100, Math.round((v || 0) / 10 * 100)));
  return `<span class="desk-mini"><i style="width:${p}%"></i></span><b class="desk-n">${v || 0}</b>`;
}
function deskStanceCls(s) {
  const t = String(s || "");
  if (/风险主导/.test(t)) return "st-watch";   // 棕：警示
  if (/机会窗口/.test(t)) return "st-good";    // 红：正面（A 股口径）
  return "st-mixed";
}
/* 盯盘评级（2026-10-01 王总要求「机会高的靠前」后配套增加）：机会/风险双因子 → 档位徽章。
   口径（A 股：红=正面，绿=负面）：硬风险一票否决；机会≥6 且风险≤4 才给最高档；
   机会 0 但风险也低（净分为正）给「回调可关注」，避免 0 分梯队刷屏时全是灰。 */
function deskGrade(r) {
  const o = r.opp_score || 0, k = r.risk_score || 0, net = o - k;
  const rs = (r.risks || []).map((x) => x.label || "");
  const why = `机会 ${o} 分 · 风险 ${k} 分 · 净分 ${net >= 0 ? "+" + net : net}`;
  if (rs.some((s) => /立案|退市/.test(s))) {
    return { cls: "st-bear", txt: "✕ 回避", why: `硬风险在案：${rs.slice(0, 2).join("、")} —— ${why}` };
  }
  if (k >= 7 && o <= 2) return { cls: "st-bear", txt: "✕ 回避", why: `风险过高且机会缺席 —— ${why}` };
  if (o >= 6 && k <= 4) return { cls: "st-strong", txt: "★★★ 优先跟踪", why: `机会压过风险，催化在先 —— ${why}` };
  if (o >= 4) return { cls: "st-good", txt: "★★☆ 观察等确认", why: `已有正面催化，等量价确认 —— ${why}` };
  if (net >= 2) return { cls: "st-good", txt: "★★ 回调可关注", why: `风险已出清、净分为正 —— ${why}` };
  if (o >= 1) return { cls: "st-good", txt: "★ 轻仓试错", why: `仅有弱催化 —— ${why}` };
  if (k >= 5) return { cls: "st-watch", txt: "★ 观望(风险高)", why: `机会缺席、风险偏高 —— ${why}` };
  return { cls: "st-mixed", txt: "— 观望", why: `无实质催化也无实质风险 —— ${why}` };
}
function gradeTd(r) {
  const g = deskGrade(r);
  return `<span class="pol-stance ${g.cls}" title="${escAttr(g.why)}">${esc(g.txt)}</span>`;
}
function renderNews() {
  const host = $("#p-news"); if (!host) return;
  const n = FEAT.news, d = n.data || {}, rows = d.rows || [], st = d.stats || {};
  const sel = rows.find((r) => r.code === n.sel) || null;
  const dt = n.detail || {};

  /* ---- 盯盘总览表（默认按 新公告 → 风险分 → 机会分 排，风险优先）---- */
  const trs = rows.map((r) => `<tr class="${r.code === n.sel ? "desk-sel" : ""}" onclick="window.newsPick('${escAttr(r.code)}')">
    <td class="wt-c1">${codeLink(r.code)}</td>
    <td>${codeLink(r.code, { text: r.name || "", cls: "wt-sym" })}</td>
    <td><span class="pol-stance ${deskStanceCls(r.stance)}">${esc(r.stance || "—")}</span></td>
    <td>${newsBar(r.risk_score)}</td>
    <td>${newsBar(r.opp_score)}</td>
    <td>${gradeTd(r)}</td>
    <td>${r.fresh ? `<b class="desk-fresh">+${r.fresh}</b>` : '—'}</td>
    <td class="muted">${esc(r.last_ann || "")}</td></tr>`).join("") ||
    `<tr><td colspan="8" class="muted wt-empty">${d.ok === false ? "接口异常：" + esc(d.reason || "") : "盯盘池暂无数据，点「立即扫描」跑一轮。"}</td></tr>`;

  /* ---- 选中票的研判卡：结论 + 风险/机会 + 改判触发 + 公告流水 ---- */
  /* 空态必须给文案：0 分时整块空白会让「为什么判 0」说不清楚（王总要求的「清晰明确」） */
  const listHtml = (arr, cls, empty) => (arr && arr.length)
    ? `<div class="desk-list ${cls}">` + arr.map((x) => `<div class="pol-jr">
        <b>${esc(x.label)}</b><span class="desk-w">${x.w}分</span>${x.count > 1 ? `<span class="desk-w2">${x.count}条</span>` : ""}
        <span class="desk-t">${esc((x.titles || []).map((t) => String(t).slice(0, 30)).join(" / "))}</span></div>`).join("") + `</div>`
    : `<div class="desk-list ${cls}"><div class="pol-jr desk-empty">${esc(empty)}</div></div>`;
  const trigHtml = (dt.triggers || []).map((t) => `<div class="pol-jr desk-trig">◆ ${esc(t)}</div>`).join("");
  const annHtml = (dt.anns || []).map((a) => `<tr>
    <td class="wt-c1">${esc(a.date || "")}</td>
    <td><a class="wt-nm" href="${a.url ? escAttr(a.url) : "javascript:void(0)"}" target="_blank" rel="noopener">${esc(a.title || "")}</a></td>
    <td>${a.is_new ? '<span class="desk-fresh">新</span>' : `<span class="pol-sec ns-cat ns-neutral">${esc(a.cat || "")}</span>`}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">近 30 天无公告。</td></tr>`;
  /* 空态文案（0 分时不留白，明确说明「为什么是 0」） */
  const EMPTY_RISK = "近 30 天无实质风险项 —— 判定依据只能是「公告面没有负面事件」，不等于利空已出尽。";
  const EMPTY_OPP = "近 30 天无实质机会项 —— 回购 / 增持 / 预增 / 订单 / 并购 / 股权激励均缺席，上调无依据。";
  const detailCard = dt.ok === false
    ? `<div class="empty">${esc(dt.reason || "")}</div>` : `
    <div class="pol-judge">
      <div class="pol-jh"><span class="pol-stance ${deskStanceCls(dt.stance)}">${esc(dt.stance || "—")}</span>
        <b>${esc((dt.name || "") + (dt.code ? " " + dt.code : ""))}</b>
        <span class="pol-jn desk-fin">${esc(dt.sentence || "")}</span></div>
      <div class="pol-jrs"><div class="pol-jr desk-verdict">${esc(dt.verdict || "")}</div></div>
    </div>
    <div class="desk-cols">
      <div><div class="desk-tt desk-tt-r">风险清单（${dt.risk_score || 0}分）</div>${listHtml(dt.risks || [], "desk-r", EMPTY_RISK)}
        <div class="desk-tt desk-tt-o">机会清单（${dt.opp_score || 0}分）</div>${listHtml(dt.opps || [], "desk-o", EMPTY_OPP)}</div>
      <div><div class="desk-tt">改判触发（出现什么 → 结论改成什么）</div>${trigHtml}
        <details class="pol-docs"><summary>公告流水（${(dt.anns || []).length} 条 · 近 30 天）</summary>
        <div class="wt-tw"><table class="wt-tb"><thead><tr><th>日期</th><th>标题</th><th>分类</th></tr></thead>
        <tbody>${annHtml}</tbody></table></div></details></div>
    </div>`;

  const body = "";   /* 手动查询降级为折叠区，见下 */
  const manual = `<details class="pol-docs"><summary>手动查询其它代码（补充入口，非自动盯盘）</summary>
    <div class="wt-form" style="grid-template-columns:1fr auto;margin:8px 0">
      <label>代码<input id="newsSym" value="${escAttr(n.symbol || "")}" placeholder="如 600519"></label>
      <button class="btn primary" id="newsGo">查询</button></div></details>`;
  host.innerHTML = `
    <div class="wt-head"><h2>个股快讯 / 公告</h2>
      <span class="hint">自动盯盘 ${rows.length} 只 · 上次扫描 ${esc(st.last_scan_at || "—")} · ${
        /* 2026-10-05（报告建议6 心跳自愈）：后端会在线程假活时自动补扫，并把 zombie 标出来；
           这里把「看起来在跑、实际停了」的情况直接写在状态牌上，不让人对着假状态发呆。 */
        st.running ? "扫描中…"
          : (st.zombie ? `<b style="color:#c0392b">已停止（${fmtAge(st.zombie_scan_age || 0)}未扫描）</b>`
              : (st.next_scan_at ? "下次 " + esc(st.next_scan_at) : ""))}
      · <button class="btn" id="deskScan">立即扫描</button></span></div>
    ${d.ok === false ? `<div class="empty">接口异常：${esc(d.reason || "未取到数据")}</div>` : `
    <div class="wt-tw"><table class="wt-tb desk-tb"><thead><tr>
      <th>代码</th><th>名称</th><th>结论</th><th>风险</th><th>机会</th><th>评级</th><th>新公告</th><th>最近公告</th>
      </tr></thead><tbody>${trs}</tbody></table></div>
    <div class="desk-detail">${
      sel ? detailCard
          : `<div class="empty">盯盘池暂无数据 —— 点右上角「立即扫描」跑一轮（约 30 秒）。</div>`}</div>
    <div class="hint wt-foot">排序：机会分 ↓ → 净分(机会−风险) ↓ → 风险低 ↑（机会为 0 的梯队里相对安全的排前面）。
      评级档位：机会≥6 且风险≤4 → 优先跟踪；机会≥4 → 观察等确认；净分≥2 → 回调可关注；
      硬风险(立案/退市)或风险≥7 且机会≤2 → 回避。悬停评级看判定依据。
      结论由规则引擎自动判定：基于「近 30 天公告 + 最新财报 + 龙虎榜净额」，
      先出倾向（风险主导 / 机会窗口 / 多空交织 / 信息真空），再给依据与改判触发点，非投资建议。
      点盯盘列表任意一行切换个股；${manual ? "点公告标题跳原文。" : ""}</div>
    ${manual}`}`;
  const scanBtn = $("#deskScan");
  if (scanBtn) scanBtn.onclick = () => {
    api("/api/watchdesk/scan").then(() => loadNews()).catch((e) => reportLoadError("盯盘扫描", e));
  };
  const go = $("#newsGo");
  if (go) go.onclick = () => {
    const s = $("#newsSym"); if (!s || !s.value.trim()) return;
    FEAT.news.symbol = s.value.trim();
    api(`/api/news_stock?symbol=${encodeURIComponent(FEAT.news.symbol)}`).then((dd) => {
      const v = dd.verdict || {};
      n.detail = { ok: dd.ok !== false, code: dd.code || FEAT.news.symbol, name: v.name || "",
        stance: v.stance || "手动查询", risks: [], opps: [], triggers: [],
        sentence: "", verdict: v.stance || "", anns: (dd.rows || []).map((r) => ({
          title: r.title, url: r.url || "", date: r.time || "", cat: r.type || "", is_new: 0 })) };
      renderNews();
    }).catch((e) => reportLoadError("个股快讯公告", e));
  };
  const inp = $("#newsSym");
  if (inp) inp.onkeydown = (e) => {
    if (e.key === "Enter" && inp.value.trim()) { FEAT.news.symbol = inp.value.trim(); loadNews(); }
  };
}
/* 公告标题 → (类别, 倾向)；与后端 _ANN_TITLE_RULES 同口径，仅用于明细行的分类徽章着色 */
function newsCatOf(title) {
  const t = String(title || "");
  /* 2026-10-01 口径自查：分类规则改从 viewer/newsclass.py 单一下发（window.NEWS_RULES，
     服务启动时由 /api 侧重写出 static/news_rules.js）。此前前端与后端各维护一份词表，
     同一条标题可能给出不同标签 —— 与「筹码集中度」同型的两把尺。 */
  const rules = window.NEWS_RULES || [];
  for (const r of rules) {
    try { if (new RegExp(r.pattern).test(t)) return [r.cat, r.tone]; } catch (e) { /* 坏规则跳过 */ }
  }
  return ["其他事务", "neutral"];
}
/* 立场 → 配色类名（2026-10-01 口径加固）：
   旧写法只有三条子串判断，命中不了就一律 return st-mixed —— 等于把「看不懂的文案」
   静默涂成中性色，读者以为是中性、实际可能是偏空。现在改成三层：
   ① 显式词表按长度降序（短词不抢长词，同 research 评级归一的坑）；
   ② 未收录文案按方向兜底（正面→st-strong / 负面→st-bear），不再静默降级；
   ③ 确实读不出方向（如「多空均衡」「整体无方向」）才落 st-mixed。 */
const NEWS_STANCE_CLS = [["偏正面", "st-strong"], ["中性偏正", "st-good"], ["警惕", "st-bear"]];
function newsStanceCls(stance) {
  const s = String(stance || "");
  for (const [kw, cls] of NEWS_STANCE_CLS) if (s.includes(kw)) return cls;
  if (/偏多|正面|利好|看涨|偏强|积极/.test(s)) return "st-strong";
  if (/偏空|警惕|负面|看跌|承压|偏弱|回避/.test(s)) return "st-bear";
  return "st-mixed";
}

/* ---------- 政策风向（国家级政策desk，2026-09-30 重做，替代旧「全市场快讯」）----------
   口径：① gov.cn 政策文件库（国务院文件+部委文件）② 中央级快讯（金十/东财筛选，省市地方默认过滤）
   每条带发文机关 / 重要性 / 影响板块 / 传导逻辑（规则映射，机器初判）；代表股来自板块资金流快照。 */
function loadPolicy() {
  const p = FEAT.policy;
  if (p.loading) return;
  const host = $("#p-policy");
  if (host && !p.data) host.innerHTML = `<div class="wt-head"><h2>政策风向 · 国家级</h2></div>
    <div class="hint wt-foot">抓取中国政府网政策文件库 + 中央级快讯筛选（首载约 2–5 秒）…</div>`;
  p.loading = true;
  api("/api/policy_desk?num=80").then((d) => {
    p.data = d; p.loading = false; renderPolicy();
    // 冷表首点：后端尚在后台抓取，3s 后自动补取一次（仅一次，避免空转）
    if (d.warming && !(p.retried)) { p.retried = true; setTimeout(loadPolicy, 3000); }
  })
    .catch((e) => { p.loading = false; reportLoadError("政策风向", e); });
}
function polImpBadge(imp) {
  if (imp >= 3) return `<span class="pol-imp pol-imp3" title="重磅：国务院本体/货币政策/重要会议">重磅</span>`;
  if (imp === 2) return `<span class="pol-imp pol-imp2" title="政策文件：国务院办公厅或部委发文">文件</span>`;
  return "";
}
function polStockChips(sectors, members) {
  const out = [];
  (sectors || []).forEach((s) => {
    (members[s] || []).forEach((m) => {
      if (out.some((x) => x.code === m.code)) return;
      out.push(m);
    });
  });
  return out.slice(0, 6).map((m) => {
    const cls = (m.pct == null) ? "" : (m.pct > 0 ? " up" : (m.pct < 0 ? " dn" : ""));
    const pct = (m.pct == null) ? "" : `<i class="${cls.trim()}">${m.pct > 0 ? "+" : ""}${Number(m.pct).toFixed(2)}%</i>`;
    return `<a class="pol-stk" data-wt-open="${escAttr(m.code)}" title="查看个股详情">${esc(m.name || m.code)}${pct}</a>`;
  }).join("");
}
function polItem(it, members) {
  const chips = (it.sectors || []).map((s) => `<span class="pol-sec" title="规则映射的影响板块（机器初判）">${esc(s)}</span>`).join("");
  const stocks = polStockChips(it.sectors, members);
  const time = String(it.time || "").slice(5, 16);
  return `<div class="pol-item${it.importance >= 3 ? " imp3" : it.importance === 2 ? " imp2" : ""}">
    <div class="pol-r1">
      <span class="pol-org">${esc(it.issuer || "中央动态")}</span>
      ${polImpBadge(it.importance)}
      ${it.url ? `<a class="pol-title" href="${escAttr(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>`
               : `<span class="pol-title">${esc(it.title)}</span>`}
      <span class="pol-time">${esc(time)}</span>
    </div>
    ${it.summary ? `<div class="pol-sum">${esc(String(it.summary).slice(0, 120))}</div>` : ""}
    ${chips ? `<div class="pol-r2"><span class="pol-lbl">影响板块</span>${chips}</div>` : ""}
    ${it.transmission ? `<div class="pol-r3"><span class="pol-lbl">传导逻辑</span>${esc(it.transmission)}</div>` : ""}
    ${stocks ? `<div class="pol-r4"><span class="pol-lbl">代表股</span>${stocks}</div>` : ""}
  </div>`;
}
/* 政策立场徽标（2026-10-01 口径修正）：
   stance 是**主立场**（按政策密度 × 重要度加权判强度），mixed 是**附加面**
   （该板块命中条目里压制类占比 ≥1/3，如集采/去产能/整顿）。
   旧写法把 mixed 当主立场，强利好 + 带集采压制的板块会被整块标成「分化」，
   读者只看得到一个「分化」，看不出整体其实偏多。现在两者分开显示。 */
function polStanceBadge(stance, j) {
  const map = { "强利好": ["st-strong", "政策密集支持 · 短期偏正面"],
                "利好": ["st-good", "有明确利好 · 待落地验证"],
                "分化": ["st-mixed", "方向本身就不一致 · 有支持也有压制"],
                "关注": ["st-watch", "政策提及少 · 等措施落地"] };
  const [cls, tip] = map[stance] || ["st-watch", "观察中"];
  const mixed = !!(j && j.mixed);
  const bear = (j && Number(j.bear)) || 0;
  const n = (j && Number(j.n)) || 0;
  const extra = mixed ? `｜附加面：${n} 条里 ${bear} 条为压制类（≥1/3，内部方向分化，不等于整体看空）` : "";
  const txt = stance + (mixed && stance !== "分化" ? " · 内有分化" : "");
  return `<span class="pol-stance ${cls}" title="${escAttr(tip + extra)}">${esc(txt)}</span>`;
}
function polJudgeCard(j) {
  const reasons = (j.reasons || []).map((r) => `<div class="pol-jr">· ${esc(r)}</div>`).join("");
  /* 政策时间行（2026-10-06 王总要求：研判卡必须写清政策何时发布、何时实施）：
     发布 = 该板块 N 条依据政策里的最新发布日期（逐条原文在下方折叠区，各带自己的时间）；
     实施 = 仅当政策原文出现「自X年X月X日起施行」等生效条款才给具体日期，
            意见/规划/方案类官方未标注 → 如实写「未标注」，不拿发布日期冒充。 */
  const impls = (j.impl || []);
  const implTxt = impls.length
    ? impls.map((x) => {
        const d = String(x.date || "");
        const label = /^20/.test(d) ? `${d} 起施行` : d;
        return `<span class="pol-impl" title="${escAttr(x.title || "")}">${esc(label)}</span>`;
      }).join("、")
    : `<span class="pol-impl-none">未标注明确日期（意见/规划类多为印发后分阶段落地）</span>`;
  const pubBit = j.pub_latest
    ? `<span class="pol-pub" title="该板块 ${escAttr(String(j.n))} 条依据政策中最新一条的发布日期">发布 ${esc(j.pub_latest)}${Number(j.n) > 1 ? `（${j.n} 条中最新）` : ""}</span>`
    : "";
  const timeRow = `<div class="pol-jtime"><span class="pol-lbl">政策时间</span>${pubBit}<span class="pol-impl-w">实施 ${implTxt}</span></div>`;
  const chips = (j.members || []).map((m) => {
    const cls = (m.pct == null) ? "" : (m.pct > 0 ? " up" : (m.pct < 0 ? " dn" : ""));
    const pct = (m.pct == null) ? "" : `<i class="${cls.trim()}">${m.pct > 0 ? "+" : ""}${Number(m.pct).toFixed(2)}%</i>`;
    return `<a class="pol-stk" data-wt-open="${escAttr(m.code)}" title="查看个股详情">${esc(m.name || m.code)}${pct}</a>`;
  }).join("");
  return `<div class="pol-judge">
    <div class="pol-jh">${polStanceBadge(j.stance, j)}<b class="pol-jsec">${esc(j.sector)}</b>
      <span class="pol-jn">依据 ${j.n} 条政策 · 加权 ${j.weight} · 压制类 ${Number(j.bear) || 0} 条</span></div>
    ${timeRow}
    ${reasons ? `<div class="pol-jrs">${reasons}</div>` : ""}
    ${chips ? `<div class="pol-jm"><span class="pol-lbl">代表股</span>${chips}</div>` : ""}
  </div>`;
}
/* 政策承压（利空面）卡片 —— 2026-10-05 王总要求：政策不只是利好，也要看得见利空。
   与上方「板块形势研判（利好面）」同屏对照，两者方位相反、互为校验：
   看好一个板块时，先看它有没有出现在下面这张「承压名单」里。
   口径说明（必须如实写清，不能只写「利空」两个字）：
   - level 「承压」= 标题里出现明确压制措辞；「关注」= 主题命中但强度不足以定级；
   - impacts 是压制类型（压价/需求收缩/产能约束/出口受限/合规处罚/成本抬升），
     不同类型意义不同——去产能是「龙头受益、中小出清」的分化，不是单边利空，必须分型标注；
   - age_days 是压制条款的日子距今天数：174 天的药品价格机制对新盘面的约束力
     跟 5 天前的网信办整治完全不是一回事，不给时效就等于哄人。 */
const POL_BEAR_IMPACT = {
  "压价": "压价", "需求收缩": "需求收缩", "产能约束": "产能约束",
  "出口受限": "出口受限", "合规处罚": "合规处罚", "成本抬升": "成本抬升",
};
function polBearCard(b) {
  const imps = (b.impacts || []).map((k) =>
    `<span class="pol-bear-imp" title="${escAttr(POL_BEAR_IMPACT[k] || k)}">${esc(POL_BEAR_IMPACT[k] || k)}</span>`).join("");
  // 同一条政策可能被多条规则命中，前端按标题去重，避免列里出现两行一模一样的依据
  const seen = {};
  const ev = (b.evidence || []).filter((e) => {
    if (seen[e.title]) return false;
    seen[e.title] = 1; return true;
  });
  const evHtml = ev.map((e) => `<div class="pol-bear-ev">
      <span class="pol-bear-sig">@${esc(e.signal || "约束条款")}</span>
      ${e.url ? `<a class="pol-bear-t" href="${escAttr(e.url)}" target="_blank" rel="noopener" title="查看政策原文">${esc(e.title)}</a>`
              : `<span class="pol-bear-t">${esc(e.title)}</span>`}
      <span class="pol-bear-date">${esc(e.date || "")}${Number(e.age_days) ? ` · ${e.age_days} 天前` : ""}</span>
    </div>`).join("");
  const chips = (b.members || []).map((m) => {
    const cls = (m.pct == null) ? "" : (m.pct > 0 ? " up" : (m.pct < 0 ? " dn" : ""));
    const pct = (m.pct == null) ? "" : `<i class="${cls.trim()}">${m.pct > 0 ? "+" : ""}${Number(m.pct).toFixed(2)}%</i>`;
    return `<a class="pol-stk" data-wt-open="${escAttr(m.code)}" title="查看个股详情">${esc(m.name || m.code)}${pct}</a>`;
  }).join("");
  const lvlCls = b.level === "承压" ? "bl-bearing" : "bl-watch";
  const meta = `标题级压制条款 ${Number(b.bear) || 0} 条 / 命中 ${Number(b.n) || 0} 条政策`
    + (Number(b.bear_pct) ? `（${b.bear_pct}%）` : "");
  return `<div class="pol-bear">
    <div class="pol-bh">
      <span class="pol-blv ${lvlCls}" title="${b.level === "承压"
        ? "标题出现明确压制措辞，负向传导较明确" : "主题命中压制规则，强度不足以定级为承压"}">${esc(b.level || "关注")}</span>
      <b class="pol-bsec">${esc(b.sector)}</b>
      <span class="pol-bmeta">${esc(meta)}</span>
    </div>
    <div class="pol-bim">${imps}</div>
    ${(b.notes || []).map((n) => `<div class="pol-bnote">${esc(n)}</div>`).join("")}
    ${evHtml || `<div class="pol-bempty">暂无逐条依据（首次加载现场抓取中）。</div>`}
    ${chips ? `<div class="pol-bm"><span class="pol-lbl">代表股</span>${chips}</div>` : ""}
  </div>`;
}
function renderPolicy() {
  const host = $("#p-policy"); if (!host) return;
  const d = FEAT.policy.data || {};
  const v = d.verdict || {};
  const members = {};
  (v.top || []).forEach((t) => { members[t.sector] = t.members || []; });
  const judges = (v.sectors || []).map(polJudgeCard).join("");
  const bears = (v.bear_sectors) || [];
  const bearCards = bears.map(polBearCard).join("");
  const sc = v.scan || {};
  const gov = (d.items || []).filter((x) => x.source === "gov.cn");
  const flash = (d.items || []).filter((x) => x.source !== "gov.cn");
  const basis = (d.basis || []).map((b) => `<li>${esc(b)}</li>`).join("");
  host.innerHTML = `
    <div class="wt-head"><h2>政策风向 · 国家级</h2>
      <span class="pol-upd">${esc((d.updated_at || "").slice(11))} 更新</span></div>
    <div class="pol-verdict">
      <div class="pol-verdict-line">📌 ${esc(v.line || "")}</div>
    </div>
    <div class="pol-judges">
      <div class="pol-gt">板块形势研判（利好面） <span class="pol-gtn">系统依据中央级政策密度与传导规则给出的判断，点击代表股可看个股</span></div>
      ${judges || `<div class="empty">暂无研判（首次加载现场抓取，若网络受限请稍后刷新）。</div>`}
    </div>
    <div class="pol-bears">
      <div class="pol-gt">政策承压研判（利空面）
        <span class="pol-gtn">政策不只催生利好，也会压制行业。下列板块命中约束/压制类政策，与上方利好面同屏对照——看好一个板块前，先看它是否出现在这张名单里</span></div>
      ${bearCards || (Number(sc.total)
        // scan.total 存在 = 后端已是新版但本轮确实没扫到压制条款（真空态）
        ? `<div class="empty">本轮未列出承压板块（政策库窗口内未命中标题级的压制/约束条款）。</div>`
        // scan.total 缺失 = 后端还是旧版（没重启过），不能把「没重启」说成「没有利空政策」
        : `<div class="empty">承压面板需后端新版本（当前接口未返回政策扫描口径）。请重启查看器后刷新本页。</div>`)}
      <div class="pol-bfoot">扫描口径：政策库共 ${Number(sc.total) || 0} 条中央级政策${sc.span ? `（${esc(sc.span)}）` : ""}，其中列表展示 ${Number(sc.window) || 0} 条；压制条款只看标题措辞，正文顺带提及不计入，「产能约束」类整体是分化（龙头受益、高成本产能出清），不等同于单边利空。</div>
    </div>
    <details class="pol-docs">
      <summary>政策原文依据（gov.cn ${gov.length} 条 · 中央级快讯 ${flash.length} 条 · 点击展开）</summary>
      <div class="pol-gt">政策文件 <span class="pol-gtn">gov.cn · 国务院文件 / 部委文件</span></div>
      ${gov.map((x) => polItem(x, members)).join("") || `<div class="empty">暂无文件。</div>`}
      <div class="pol-gt">央行动态与重要会议（快讯） <span class="pol-gtn">金十/东财 · 中央级筛选</span></div>
      ${flash.map((x) => polItem(x, members)).join("") || `<div class="empty">暂无中央级快讯。</div>`}
    </details>
    <details class="ol-src pol-basis"><summary>口径与说明（机器初判，务必读）</summary><ul>${basis}</ul></details>`;
}

/* ---------- 二级导航：内容区顶部的横向平铺子板块 ----------
   只重绘横条，不碰面板、不写 hash。与 selectNav 分开是为了让「学习轨道列表异步到达
   → 横条多出一项」这件事不会反向触发面板切换而形成递归。 */
/* 2026-10-12 体验审计 P2「命名偏行话」：少数真的需要一句大白话的项，悬停即见释义。
   只补确实容易误解的，不把整条横条塞满说明（报告：首次出现给一句释义即可）。 */
const TAB_HINTS = {
  stocks: "被帖子讨论最多的股票。讨论多 ≠ 看好 —— 卡片上另有「看好 / 看空 / 分歧」标注。",
  watch_brief: "你自己的关注池今天有没有事：到价 / 异动 / 被看空 / 落在主线。",
  watch_alerts: "到价提醒：给关注池的票设了目标价后，价格触及时在这里报信（没设目标价就不会响）。",
  rate: "入手概率 = 换手率等技术面规则给全市场打的分档排名，是规则的历史胜率参考，不是对个股的预测。",
  screener: "智能选股 = 按多条件（估值 / 持股 / 质量 / 值得率）在 5000+ 只里筛候选，再人工判断。",
  cohold: "近5日共同买入 = 看「最近谁同时动手了」（动作时点）；与「共同持仓共识」的「现在还有谁拿着」不是一回事。",
  consensus: "共同持仓共识 = 「现在还有多少人一起拿着」，按人数排序。",
  smart_money: "把选手 / 牛散 / 共识 / 近5日共买等多个名单合成一条结论：多人同时买的方向与个股。",
  info_brief: "把政策 / 国际 / 海外 / 公告 / 快讯压成一屏：今日消息面 Top5 与影响板块。",
  today: "一屏四卡：今日方向 / 机会 / 风险 / 我的票提示 —— 打开软件第一眼就该看这个。",
};

function paintSubnav(cat, tab) {
  const bar = $("#subnav");
  if (!bar) return;
  const items = itemsOfCat(cat);
  const out = [];
  for (let k = 0; k < items.length; k++) {
    const i = items[k];
    if (i.group) {
      // 分组标题：不可点小标题 + 左侧虚线分隔，纯视觉锚点（不计入 tab 校验）
      out.push(`<span class="sub-group" style="display:inline-flex;align-items:center;height:28px;margin:0 6px 0 2px;padding:0 10px 0 12px;color:#9aa4b2;font-size:12px;letter-spacing:1px;border-left:1px dashed rgba(255,255,255,.22);white-space:nowrap;">${esc(i.group)}</span>`);
      continue;
    }
    if (i.child) continue; // 子项随其主项（lead）一起渲染，避免重复与错位
    if (i.lead) {
      // 主项 + 紧随其后的同 mg 子项：用一个浅底簇视觉归拢，主项加粗、子项缩进挂在右侧
      out.push(`<span class="sub-cluster" style="display:inline-flex;align-items:center;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:2px 5px;margin:0 3px;">`);
      out.push(`<a class="sub-tab sub-lead${i.tab === tab ? " active" : ""}" data-tab="${escAttr(i.tab)}" title="${escAttr(TAB_HINTS[i.tab] || i.label)}" href="#${escAttr(cat.key)}/${escAttr(i.tab)}" style="font-weight:600;">${esc(i.label)}</a>`);
      for (let j = k + 1; j < items.length; j++) {
        const c = items[j];
        if (c.group || !c.child) break;          // 遇到下一个分组或主项即停止收拢
        if (c.mg === i.mg) {
          out.push(`<a class="sub-tab sub-child${c.tab === tab ? " active" : ""}" data-tab="${escAttr(c.tab)}" title="${escAttr(TAB_HINTS[c.tab] || c.label)}" href="#${escAttr(cat.key)}/${escAttr(c.tab)}" style="font-size:12px;padding-left:11px;margin-left:3px;border-left:1px solid rgba(255,255,255,.18);opacity:.95;">${esc(c.label)}</a>`);
        } else break;
      }
      out.push(`</span>`);
    } else {
      // 普通独立子项（无 lead/child/group 标记）
      out.push(`<a class="sub-tab${i.tab === tab ? " active" : ""}" data-tab="${escAttr(i.tab)}" title="${escAttr(TAB_HINTS[i.tab] || i.label)}" href="#${escAttr(cat.key)}/${escAttr(i.tab)}">${esc(i.label)}</a>`);
    }
  }
  bar.innerHTML = out.join("") +
    // 带知识库轨道的分类（学习中心）横条右端保留知识库上下文（状态 chip + 立即吸取）
    (DYN_CATS[cat.key]
      ? `<span class="subnav-spacer"></span>` +
        `<span class="chip-s chip-fresh" id="kbStateChip" title="知识库吸取状态">知识库：读取中…</span>` +
        `<button class="btn sm" id="btnKbReload" title="重新解析来源文件（内容未变则自动跳过，不做无意义重建）">立即吸取</button>`
      : "");
  $$("#subnav .sub-tab").forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); if (a.dataset.tab) selectNav(cat.key, a.dataset.tab, true); };
  });
  const rb = $("#btnKbReload");
  // 不能写 rb.onclick = kbReload：onclick 会把 PointerEvent 当第一参传入，
  // kbReload(docKey) 收到事件对象后 key=Event → ensurePanel("learn_"+Event)
  // → querySelector("#p-learn_[object PointerEvent]") 抛异常，面板被清成空白。
  if (rb) rb.onclick = () => kbReload();
  renderKbStateChip();
}

/* 切换导航。push=true 时写 hash（由 hashchange 统一驱动渲染，避免两处逻辑分叉） */
function selectNav(catKey, tabKey, push) {
  // 离开「突破雷达」时停掉其 5 分钟轮询，避免无意义复评请求
  if (CURRENT && CURRENT.tab === "lowbox_radar" && tabKey !== "lowbox_radar") {
    const _st = FEAT.lowbox;
    if (_st.radarTimer) { clearInterval(_st.radarTimer); _st.radarTimer = null; }
  }
  const cat = NAV.find((c) => c.key === catKey) || NAV[0];
  // 归属纠偏：轨道的所在分类由 kb_sources.yaml 的 group 决定，将来若再拆板块，轨道可能被挪到
  // 别的一级分类下。若深链接里的分类与轨道实际归属不符（典型：轨道换了 group 之后，
  // 旧书签仍停在原分类），以实际归属为准重定向 —— 否则会出现"面板内容对了、顶部横条
  // 还是另一个分类"的错位。轨道列表未加载时不纠（待其回来后再走一次）。
  // 注：2026-09-26 合并后只剩 learn 一个分类，本段为将来拆分的兼容保留。
  const _dk = docKeyOfTab(tabKey);
  if (/^learn_/.test(String(tabKey || "")) && tabKey !== "learn_tracks" && KB.tracks) {
    const _t = toArr(KB.tracks.docs).find((x) => x.doc_key === _dk);
    const _g = _t ? (_t.group || "learn") : null;
    if (_g && _g !== cat.key && NAV.some((c) => c.key === _g)) {
      // 把 hash 一并扶正：否则地址栏与实际视图长期不一致，一刷新又回到错的分类。
      // 用 replaceState 不触发 hashchange（避免与下面的递归调用打转）。
      try { history.replaceState(null, "", "#" + _g + "/" + tabKey); } catch (e) { /* 忽略：无 history 环境 */ }
      selectNav(_g, tabKey, false);
      return;
    }
  }
  const items = itemsOfCat(cat);
  // 动态轨道（「学习中心」下的子项）由 kb_sources.yaml 在**运行时**决定：
  // 首次深链接进入时轨道列表还没拉回来，items 里自然没有这个 tab。若一律按"未知就纠回首项"
  // 处理，用户点「基础常识」会被静默换成该分类的第一项（看着像点了没反应/跑错板块）。
  // 故 learn_* 一律放行，真正的轨道是否存在由 ensureKbDocFor 自己兜底。
  const relaxed = !!DYN_CATS[cat.key] && /^learn_/.test(String(tabKey || ""));
  const tab = (tabKey && (items.some((i) => i.tab === tabKey) || relaxed)) ? tabKey : firstRealTab(items);
  // 「点左栏大类」落到"本类首项"：tabKey 为空即"首项意图"。对动态轨道分类，若轨道还没拉回来，
  // 上面 items[0] 是占位项（策略回测）而非真实首项（基础常识）。记下待办，待 loadKbTracks
  // 把轨道拉回后兑现到真实首项；若轨道已到，则 tab 已是真实首项，清掉待办。
  if (!tabKey && DYN_CATS[cat.key]) {
    KB.pendingFirstTrack = KB.tracks ? null : cat.key;
  }
  // 「返回来路」记账（2026-09-30）：必须放在 CURRENT 被覆盖**之前**读到来路页，且在 push 早退
  // 之前完成（push 流程早退后由 hashchange 二次回调接手渲染，来路此时已记好）。进入 diag 时把
  // 来路记为 origin；离开 diag（无论切去哪）即作废；点「← 返回」时用 returning 标记放行并清账。
  {
    const _pvT = CURRENT && CURRENT.tab, _pvC = CURRENT && CURRENT.cat;
    if (NAV_BACK.returning) { NAV_BACK.returning = false; NAV_BACK.origin = null; }
    else if (tab === "diag") {
      if (_pvT && !(_pvT === "diag" && _pvC === cat.key))
        NAV_BACK.origin = { cat: _pvC, tab: _pvT, label: navTabLabel(_pvC, _pvT) };
    } else if (NAV_BACK.origin) NAV_BACK.origin = null;
  }
  CURRENT = { cat: cat.key, tab };
  if (push) {
    const h = "#" + cat.key + "/" + tab;
    if (location.hash !== h) { location.hash = h; return; }   // hashchange 会回调本函数
  }
  renderNav();            // 左栏只随「当前大类」高亮变化
  paintSubnav(cat, tab);  // 内容区顶部横条：按大类换一批子项，并高亮当前项
  $$(".panel").forEach((p) => p.classList.remove("active"));
  const panel = ensurePanel(tab);
  if (panel) {
    panel.classList.add("active");
    // 2026-10-15 全站回测评测修复（体验 · 首屏白屏）：
    // 切到一个「还没渲染过」的面板时先给「加载中」占位。大数据面板首次拉数要 3–8 秒
    // （相关人物 932KB、龙虎榜 218KB 等），此前这段是**纯白屏**、
    // 没有任何反馈，看着像「点进去坏了」。各渲染函数一律 `innerHTML =` 覆盖式写盘，
    // 占位会被数据到达后的重绘自然替换掉；已有内容的旧面板（innerHTML 非空）不注入，不闪。
    if (!panel.innerHTML.trim()) {
      panel.innerHTML = '<div class="panel-loading">正在加载数据…</div>';
    }
  }
  // 知识库正文约 250KB：只有真正进入该子板块才拉（懒加载）
  if (DYN_CATS[cat.key] && !KB.tracks) loadKbTracks();
  // 知识库轨道：任意 `learn_<doc_key>` 子标签都走通用入口 —— 新增轨道、或把轨道挪到另一个
  // 一级分类下，都只需改 kb_sources.yaml，不再需要在这里加 `if (tab === "learn_xxx")`
  // （旧写法把 doc_key 硬编码在导航里，结果配置里加了新轨道、左栏也出现了这一项，点进去却是空白）。
  // 仅「知识库轨道」(learn_<doc_key>) 走通用 KB 入口；strategy_lab / learn_tracks 是固定面板，
  // 各有专属渲染，不走 ensureKbDocFor。
  if (DYN_CATS[cat.key] && /^learn_/.test(tab) && tab !== "learn_tracks") ensureKbDocFor(docKeyOfTab(tab));
  if (tab === "learn_tracks") renderKbTracks();
  // 今日决策（2026-10-12 体验审计 P0-A）：新的默认落地页 —— 一屏四卡（方向/机会/风险/我的票）
  if (tab === "today") {
    if (TODAY.data) renderToday();
    else loadToday().catch((e) => reportLoadError("今日决策", e));
  }
  // AI 复盘（2026-10-29 进哥需求）：真调大模型的判断，与今日决策（规则引擎）并列。
  // 已有数据则纯重绘（零请求）；后台还在算的时候 renderAiReview 会自己续上轮询，
  // 所以切走再切回来不会丢进度，也不会重复起线程。
  if (tab === "ai_review") {
    if (AIR.data) {
      renderAiReview();
      if (AIR.data.state === "computing") airStartPoll();
    } else {
      loadAiReview().catch((e) => reportLoadError("AI 复盘", e));
    }
  }
  // 三个「结论页」（P0-D/E/F）：消息面汇总 / 聪明钱共识 / 我的票·今日提示
  if (tab === "info_brief" || tab === "smart_money" || tab === "watch_brief") {
    const _k = { info_brief: "info", smart_money: "money", watch_brief: "watch" }[tab];
    if (BRIEF[_k]) renderBrief(_k);
    else loadBrief(_k).catch((e) => reportLoadError(tab, e));
  }
  // 今日看板：决策入口（默认落地页）——进入时按需（重）渲染，兜住"先拿数据、后面板才建"的时序
  if (tab === "dashboard") {
    if (DASH.data) renderDashboard();
    else loadDashboard().catch((e) => reportLoadError("今日看板", e));
  }
  // 每日预测（pred-v1.0）：总览第三个 tab，进入时按需（重）渲染
  // 2026-10-15 修 TDZ：initNav 在脚本顶层执行（早于 const PRED 声明行），深链接
  // #overview/prediction 直接落此分支时 `if (PRED.data)` 会抛
  // "Cannot access 'PRED' before initialization"。改为无条件走 loadPrediction()
  // （函数声明有提升，调用发生在全量求值之后；两个本地接口毫秒级，不多花什么）。
  if (tab === "prediction") {
    loadPrediction().catch((e) => reportLoadError("每日预测", e));
  }
  // 近期热点（2026-10-15）：从 loadAll 挪到「进入本面板才拉」—— 该接口是纯 Python 重聚合
  // （首次 40 秒、持 GIL），放首屏会把其他接口一起冻住。已拿过数据则纯重绘（零请求）。
  if (tab === "topics") {
    if (HOTSPOTS.data) renderHotspots(HOTSPOTS.data);
    else loadHotspots().catch((e) => reportLoadError("近期热点", e));
  }
  // 多源共振（2026-10-15 进哥需求）：今日总览第 5 个子板块，进入时按需（重）渲染
  if (tab === "resonance") {
    loadResonance().catch((e) => reportLoadError("多源共振", e));
  }
  // 大势研判（2026-10-31）：局面 + 今日陷阱名单，进入时按需加载
  if (tab === "harvest") {
    loadHarvest().catch((e) => reportLoadError("大势研判", e));
  }
  // 开源情报（2026-10-30）：GitHub 规则重测结果，进入时按需加载
  if (tab === "oss") {
    loadOSS().catch((e) => reportLoadError("开源情报", e));
  }
  // 选股方向（2026-10-30）：短线证伪 + 低波池，进入时按需加载
  if (tab === "selection") {
    loadSelection().catch((e) => reportLoadError("选股方向", e));
  }
  // 避雷名单（2026-10-21 dy9）：低分卡归因显式面板，进入时按需加载
  if (tab === "avoid") {
    loadAvoid().catch((e) => reportLoadError("避雷名单", e));
  }
  // 量化信号（2026-10-16 进哥需求，第三维信号源）：今日总览第 6 个子板块，进入时按需（重）渲染
  if (tab === "quant") {
    loadQuant().catch((e) => reportLoadError("量化信号", e));
  }
  // ETF 基金（2026-10-17 进哥需求，与量化/共振并列的顶层 NAV）：今日总览子板块，进入时按需（重）渲染（左右两栏）
  if (tab === "etf") {
    loadEtf().catch((e) => reportLoadError("ETF基金", e));
  }
  // Kronos K线基础模型预测（2026-10-24 进哥需求）：独立顶层 tab，进入时按需（重）渲染
  if (tab === "kronos") {
    loadKronos().catch((e) => reportLoadError("Kronos K线预测", e));
  }
  // 抓取管道三块（原「知识获取」的 数据源/原始存档/抓取与登录记录）：2026-09-26 已从导航撤下，
  // 故正常点选与改 hash 都到不了这里（未知 tab 会被纠回首项）。保留这段是配合上面 NAV 的注释——
  // 把三项加回 items 即可原样复原，不必重新接线。面板容器同理保留在 index.html。
  if (tab === "sources") renderSourcesPanel(SUMMARY.data);
  if (tab === "archive") renderArchivePanel();
  if (tab === "fetchlog") renderFetchLogPanel(SUMMARY.data);
  // 游资龙虎榜：进入该子板块时按需（重）渲染 —— 双保险。
  // 面板已补进 index.html；这里再兜一层，确保「先拿到数据、后面板才创建」的时序也不会白屏。
  if (tab === "lhb") {
    if (LHB.data) renderLhb();
    else loadLhb().catch((e) => reportLoadError("游资龙虎榜", e));
  }
  // 板块动向：独立视角，进入时按需（重）渲染
  if (tab === "lhb_sectors") {
    if (LBHS.data) renderLhbSectors();
    else loadLhbSectors().catch((e) => reportLoadError("板块动向", e));
  }
  // 牛散追踪（含榜单 / 变动流双视角）：进入时按需（重）渲染
  if (tab === "niusan") {
    if (NIUSAN.data) renderNiusan();
    else loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
  }
  // 策略回测（T04）：一次全量 130KB，首进入时懒加载；已加载则按当前筛选重绘
  if (tab === "strategy_lab") {
    if (LAB.data) renderStrategyLab();
    else loadStrategyLab();
  }
  // 信号回测实证（P4 · 2026-10-27 收尾）：进入即懒加载，已加载则纯重绘（零请求）
  if (tab === "backtest") {
    if (BTK.data) renderBacktest();
    else loadBacktest();
  }
  // 入手概率（2026-10-05 新板块，与「智能选股」并列）：进入即渲染，已拿过数据就只重绘
  if (tab === "rate") {
    if (FEAT.rate.data || FEAT.rate.error) renderRate();
    else loadRate().catch((e) => reportLoadError("入手概率", e));
  }
  // 长期低位（2026-10-13 进哥「一次性全做」版）：板块二，进入即渲染，已拿过数据就只重绘
  if (tab === "lowbox") {
    if (FEAT.lowbox.data || FEAT.lowbox.error) renderLowbox();
    else loadLowbox().catch((e) => reportLoadError("长期低位", e));
  }
  // 长期底部埋伏（2026-10-21 进哥需求）：全市场自适应底部平台，进入即渲染
  if (tab === "lowbox_bottom") {
    if (FEAT.lowboxBottom.data || FEAT.lowboxBottom.error) renderBottom();
    else loadBottom(false).catch((e) => reportLoadError("长期底部埋伏", e));
  }
  // 突破雷达（2026-10-13 进哥「一次性全做」）：复评最近一次扫描池；进入起 5 分钟轮询，离开停
  if (tab === "lowbox_radar") {
    if (FEAT.lowbox.radarData || FEAT.lowbox.radarError) renderLowboxRadar();
    else loadLowboxRadar(false).catch((e) => reportLoadError("突破雷达", e));
    const _st = FEAT.lowbox;
    if (!_st.radarTimer) {
      _st.radarTimer = setInterval(() => {
        if (document.hidden) return;
        if (CURRENT && CURRENT.tab === "lowbox_radar") loadLowboxRadar(true);
      }, 300000);
    }
  }
  // 增强功能（2026-09-27 新增，隔离增量）：进入子板块才拉，渲染层零改动
  if (tab === "screener") loadScreener();
  if (tab === "moneyflow") loadMoneyflow();
  // 2026-09-29「个股诊断」：合并估值诊股 + 个股资金流 + 财务/公告/专家分析
  if (tab === "diag") dgEnsureAutoRun();   // 进页即自动诊断，不要求再点「开始诊断」
  if (tab === "research") loadResearch();
  if (tab === "multiquote") loadMultiquote();
  // 2026-10-29 进哥拍板：板块热度 —— 荐股降级为「注意力」后的替代页面，进入才拉
  if (tab === "plate_heat") loadPlateHeat();
  if (tab === "fundmasters") loadFundMasters();
  if (tab === "news") loadNews();
  if (tab === "policy") loadPolicy();
  // 2026-09 行业龙头全景（王总拍板）：进入即拉真实市值排名；沙箱 available:False 时面板只提示
  if (tab === "industry-leaders") loadIndustryLeaders();
  // 2026-10-24 供应链瓶颈猎手（进哥拍板）：进入才懒加载，纯标准库 KB 驱动
  if (tab === "bottleneck") loadBottleneck();
  // 2026-10-11 王总第三回合：B站视频级分堆三处新面板
  // ① 市场情绪（info）；③ 技术·价值教学（learn）；② B站博主分组框（learn）
  if (tab === "bili_sentiment") renderBiliPile("sentiment", "#p-bili_sentiment", true);
  // 2026-10-17 新增：全球大盘（消息与外围 · 外部市场环境）
  if (tab === "global_market") loadGlobalMarket();
  if (tab === "bili_techvalue") loadBiliMethods();
  if (tab === "bili_authors") renderBiliAuthors();
  // 2026-10-20 进哥：豆包「技术方法」面板 —— 2701 条，进入才懒加载（首屏不许拉）
  if (tab === "tech" && window.DOUYIN) window.DOUYIN.renderTech();
    if (tab === "integrate" && window.DOUYIN) window.DOUYIN.renderIntegration();
  if (tab === "dy_reso" && window.DOUYIN) window.DOUYIN.renderResonance();
  if (tab === "funnel" && window.DOUYIN) window.DOUYIN.renderFunnel();
}

/* ================= 信号回测实证（P4 · 2026-10-27 收尾） =================
   数据：/api/signal/backtest ← 后端 viewer/server.py api_signal_backtest()
   （表 signal_backtest：17 信号 × 3 前向窗口 = 51 行；口径 = 触发信号后 N 日超额收益）。
   界面打通「实证 → 当前消费权重」链路：表格按夏普降序展示各信号胜率/平均超额收益/夏普，
   底部对照 config.SIGNAL_WEIGHTS 当前实际被评分消费的权重，标出"有实证 / 无实证对照"。 */
const BTK = { data: null, loading: false, filter: "all" };
function loadBacktest() {
  const box = ensurePanel("backtest");
  if (!box) return;
  if (BTK.data) { renderBacktest(); return; }
  if (BTK.loading) return;
  BTK.loading = true;
  box.innerHTML = `<div class="empty">正在读取信号回测实证…</div>`;
  api("/api/signal/backtest")
    .then((d) => { BTK.loading = false; BTK.data = d; renderBacktest(); })
    .catch((e) => { BTK.loading = false; reportLoadError("信号回测", e); });
}
function btColor(x) {
  const n = Number(x);
  if (!isFinite(n) || n === 0) return "#888";
  return n > 0 ? "#c0392b" : "#0a7d3b";   // A股约定：盈利=红、亏损=绿
}
function btNum(x, p) {
  if (x == null || !isFinite(Number(x))) return "—";
  return Number(x).toFixed(p == null ? 2 : p);
}
function renderBacktest() {
  const box = ensurePanel("backtest");
  if (!box) return;
  const d = BTK.data;
  if (!d || !d.ok) {
    box.innerHTML = `<div class="empty">信号回测数据不可用：${esc(d && d.error ? d.error : "未知错误")}</div>`;
    return;
  }
  const rows = (d.rows || []).slice();
  const s = d.summary || {};
  const w = d.weights || {};
  const shown = BTK.filter === "all" ? rows : rows.filter((r) => String(r.fwd) === String(BTK.filter));
  const best = s.best ? `<b style="color:#c0392b">${esc(String(s.best))}</b>` : "—";
  const worst = s.worst ? `<b style="color:#0a7d3b">${esc(String(s.worst))}</b>` : "—";
  const card = (k, v, sub, col) =>
    `<div style="background:var(--panel,#111827);border:1px solid var(--border,rgba(255,255,255,.1));border-radius:8px;padding:10px 12px;min-width:0">
       <div style="font-size:12px;color:#888;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${k}</div>
       <div style="font-size:20px;font-weight:700;color:${col || "#e6e6e6"};line-height:1.1">${v}</div>
       <div style="font-size:11px;color:#777;margin-top:3px">${sub || ""}</div>
     </div>`;
  const cards = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px">
      ${card("实证样本", btNum(s.total, 0) + " 行", "17 信号 × 3 窗口")}
      ${card("正α（盈利方向）", btNum(s.positive_alpha, 0), "平均超额收益 > 0", "#c0392b")}
      ${card("负α（亏损方向）", btNum(s.negative_alpha, 0), "平均超额收益 < 0", "#0a7d3b")}
      ${card("最佳 / 最差信号", best + "<br>" + worst, "按夏普排序", "#e6e6e6")}
    </div>`;
  const fwds = ["all", "5", "10", "20"];
  const flab = { all: "全部", "5": "5日窗口", "10": "10日窗口", "20": "20日窗口" };
  const fbar = `<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:wrap">
      ${fwds.map((f) => `<button class="bt-fbtn" data-f="${f}" style="cursor:pointer;border:1px solid var(--border,rgba(255,255,255,.2));background:${BTK.filter === f ? "var(--accent,#0E7490)" : "transparent"};color:${BTK.filter === f ? "#fff" : "var(--txt,#ddd)"};border-radius:6px;padding:4px 12px;font-size:12px">${flab[f]}</button>`).join("")}
      <span style="margin-left:auto;font-size:12px;color:#777">更新于：${esc(String((rows[0] && rows[0].updated) || "—"))}</span>
    </div>`;
  const head = `<tr style="position:sticky;top:0;background:var(--panel,#111827)">
      <th style="text-align:left;padding:6px 8px">信号</th>
      <th style="text-align:left;padding:6px 8px">方向</th>
      <th style="text-align:right;padding:6px 8px">前向(日)</th>
      <th style="text-align:right;padding:6px 8px">样本数</th>
      <th style="text-align:right;padding:6px 8px">胜率</th>
      <th style="text-align:right;padding:6px 8px">平均超额收益</th>
      <th style="text-align:right;padding:6px 8px">夏普</th>
    </tr>`;
  const body = shown.map((r) => {
    const dir = r.direction === "sell"
      ? `<span style="color:#0a7d3b">卖</span>` : `<span style="color:#c0392b">买</span>`;
    const ret = Number(r.avg_ret), wr = Number(r.winrate), sh = Number(r.sharpe);
    return `<tr style="border-bottom:1px solid var(--border,rgba(255,255,255,.08))">
        <td style="padding:6px 8px;white-space:nowrap">${esc(String(r.signal))}</td>
        <td style="padding:6px 8px">${dir}</td>
        <td style="padding:6px 8px;text-align:right">${btNum(r.fwd, 0)}</td>
        <td style="padding:6px 8px;text-align:right">${btNum(r.n, 0)}</td>
        <td style="padding:6px 8px;text-align:right">${(isFinite(wr) ? (wr * 100).toFixed(1) + "%" : "—")}</td>
        <td style="padding:6px 8px;text-align:right;color:${btColor(ret)};font-weight:600">${(isFinite(ret) ? (ret * 100).toFixed(2) + "%" : "—")}</td>
        <td style="padding:6px 8px;text-align:right;color:${btColor(sh)};font-weight:600">${btNum(sh, 2)}</td>
      </tr>`;
  }).join("");
  const table = `<div style="overflow:auto;max-height:46vh;border:1px solid var(--border,rgba(255,255,255,.1));border-radius:8px;margin-bottom:16px">
      <table style="width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums">
        <thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  const wkeys = Object.keys(w);
  const wbody = wkeys.length ? wkeys.map((k) => {
    const key = String(k).toLowerCase();
    const has = rows.some((r) => r.signal && String(r.signal).toLowerCase().includes(key.slice(0, 6)));
    return `<tr style="border-bottom:1px solid var(--border,rgba(255,255,255,.08))">
        <td style="padding:5px 8px">${esc(String(k))}</td>
        <td style="padding:5px 8px;text-align:right">${btNum(w[k], 3)}</td>
        <td style="padding:5px 8px">${has ? '<span style="color:#0a7d3b">✓ 有实证对照</span>' : '<span style="color:#b8860b">· 无实证对照</span>'}</td>
      </tr>`;
  }).join("") : `<tr><td colspan="3" class="empty">当前未配置 SIGNAL_WEIGHTS</td></tr>`;
  const wsec = `<div style="background:var(--panel,#111827);border:1px solid var(--border,rgba(255,255,255,.1));border-radius:8px;padding:12px 14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#ddd">当前消费权重对照（config.SIGNAL_WEIGHTS）</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="color:#888">
          <th style="text-align:left;padding:4px 8px">信号维度</th>
          <th style="text-align:right;padding:4px 8px">权重</th>
          <th style="text-align:left;padding:4px 8px">实证对照</th>
        </tr></thead><tbody>${wbody}</tbody></table>
    </div>`;
  box.innerHTML = `
    <div style="margin:0 0 4px;display:flex;align-items:baseline;gap:10px">
      <h2 style="margin:0;font-size:18px">信号回测实证</h2>
      <span style="font-size:11px;color:#fff;background:var(--accent,#0E7490);border-radius:4px;padding:1px 7px">P4</span>
    </div>
    <div style="color:#888;font-size:12px;margin-bottom:14px">触发信号后 N 日超额收益实测，表格按夏普降序。
      <b style="color:#c0392b">红=盈利方向</b>、<b style="color:#0a7d3b">绿=亏损方向</b>（A股约定）。
      底部对照当前评分实际消费的权重，验证「实证 → 权重」链路一致。</div>
    ${cards}${fbar}${table}${wsec}`;
  $$(".bt-fbtn", box).forEach((b) => {
    b.onclick = () => { BTK.filter = b.dataset.f; renderBacktest(); };
  });
}

/* ================= 全球大盘（消息与外围 · 2026-10-17 新增） =================
   数据：/api/global_market ← 后端 viewer/global_market.py（国内免费源：新浪 hq.sinajs.cn
   + 腾讯 qt.gtimg.cn，免登录、免 cookie）。
   口径（不冒充，界面显式标注）：美股为**真实指数**；日/韩/德/英/法/中国台湾/印度/巴西等
   为**美股上市国家 ETF 代理**（含汇率与美股情绪因素）——方向可参考，点位不等同本地指数。 */
const GMKT = { data: null, loading: false };
function loadGlobalMarket() {
  const box = ensurePanel("global_market");
  if (!box) return;
  if (GMKT.data) { renderGlobalMarket(); return; }
  if (GMKT.loading) return;
  GMKT.loading = true;
  box.innerHTML = `<div class="empty">正在读取全球大盘行情…</div>`;
  api("/api/global_market")
    .then((d) => { GMKT.loading = false; GMKT.data = d; renderGlobalMarket(); })
    .catch((e) => { GMKT.loading = false; reportLoadError("全球大盘", e); });
}
function gmShortTs(t) {
  if (!t) return "—";
  const s = String(t).replace(/\//g, "-");
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/);
  return m ? m[2] + "-" + m[3] + " " + m[4] : s;
}
function gmPctNode(pct) {
  const n = Number(pct);
  if (!isFinite(n)) return `<span style="color:#888">—</span>`;
  const c = n > 0 ? "#c0392b" : (n < 0 ? "#0a7d3b" : "#666");
  return `<span style="color:${c};font-weight:600;font-variant-numeric:tabular-nums;min-width:66px;text-align:right">${n > 0 ? "+" : ""}${n.toFixed(2)}%</span>`;
}
function gmRowHtml(r) {
  const price = (r.price == null || !isFinite(Number(r.price))) ? "—"
    : Number(r.price).toLocaleString("en-US", { maximumFractionDigits: 2 });
  const tag = r.proxy
    ? `<i style="font-style:normal;font-size:10px;color:#8a6d3b;background:#fdf6e3;border:1px solid #f0e0b0;border-radius:3px;padding:0 3px;margin-left:5px;vertical-align:1px">ETF代理</i>`
    : "";
  return `<div style="display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px dashed rgba(128,128,128,.18)">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.name)}${tag}</span>
      <span style="color:#8a8a8a;font-variant-numeric:tabular-nums">${price}</span>
      ${gmPctNode(r.pct)}
    </div>`;
}
function gmCardHtml(title, sub, rows, emptyText) {
  const list = toArr(rows);
  const body = list.length ? list.map(gmRowHtml).join("")
    : `<div class="empty">${esc(emptyText || "暂无数据")}</div>`;
  return `<div style="border:1px solid rgba(128,128,128,.2);border-radius:8px;padding:10px 12px;background:rgba(128,128,128,.03)">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:4px">
        <h3 style="margin:0;font-size:13px">${esc(title)}</h3>
        ${sub ? `<span style="font-size:11px;color:#909090">${esc(sub)}</span>` : ""}
      </div>${body}</div>`;
}
function renderGlobalMarket() {
  const box = $("#p-global_market");
  if (!box) return;
  const d = GMKT.data;
  if (!d || !d.ok) {
    box.innerHTML = `<div class="empty">暂无全球行情数据${d && d.note ? "：" + esc(d.note) : "，请稍后重试"}。</div>`;
    return;
  }
  const s = d.summary || {};
  const toneMap = {
    risk_on: ["风险偏好上行", "#c0392b"],
    risk_off: ["风险偏好下行", "#0a7d3b"],
    neutral: ["中性震荡", "#666"],
  };
  const tone = toneMap[s.risk_tone] || toneMap.neutral;
  const pctTxt = (x) => ` ${esc(x.name)} <b style="color:${x.pct > 0 ? "#c0392b" : (x.pct < 0 ? "#0a7d3b" : "#666")}">${x.pct > 0 ? "+" : ""}${Number(x.pct).toFixed(2)}%</b>`;
  const top = toArr(s.sector_top), bot = toArr(s.sector_bottom);
  const lead = `<div style="border:1px solid rgba(128,128,128,.22);border-radius:8px;padding:10px 14px;margin-bottom:12px;background:rgba(128,128,128,.04)">
      <div style="font-weight:700;font-size:15px;color:${tone[1]}">${esc(tone[0])}
        <span style="font-weight:400;font-size:12px;color:#909090;margin-left:8px">美股均值 ${s.us_avg != null ? (s.us_avg > 0 ? "+" : "") + s.us_avg + "%" : "—"}</span>
      </div>
      <div style="margin-top:5px;font-size:12.5px;line-height:1.7;color:#555">
        ${top.length ? `<div>领涨板块：${top.map(pctTxt).join(" · ")}</div>` : ""}
        ${bot.length ? `<div>领跌板块：${bot.map(pctTxt).join(" · ")}</div>` : ""}
      </div>
      <div style="margin-top:4px;font-size:11px;color:#9a9a9a">行情时间：<b style="color:#555">北京时间 ${gmShortTs(d.us_time)}</b>${d.us_time_et ? ` 　｜　 美东时间 ${gmShortTs(d.us_time_et)}` : ""}${d.us_session ? ` · 美股${d.us_session}` : ""}</div>
      <div style="font-size:11px;color:#9a9a9a">数据更新：${esc(d.as_of || "—")}${d.age_min != null ? ` · 缓存 ${d.age_min} 分钟前` : ""} · 涨跌幅为上述时点的行情快照</div>
    </div>`;
  const grid = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:12px">
      ${gmCardHtml("美股大盘", "真实指数 · 美东 " + gmShortTs(d.us_time_et || d.us_time), d.indices, "美股指数暂缺")}
      ${gmCardHtml("美股行业板块", "SPDR 行业 ETF · 11 大类", d.sectors, "板块暂缺")}
      ${gmCardHtml("主要经济体", "国家 ETF 代理 · 美东 " + gmShortTs(d.us_time_et || d.us_time), d.countries, "暂无数据")}
      ${gmCardHtml("中国香港", "真实指数 · 北京 " + gmShortTs(d.hk_time), d.hk, "港股暂缺")}
      ${gmCardHtml("主题 / 跨资产", "半导体 · 中概 · 金油债汇", toArr(d.themes).concat(toArr(d.cross_asset)), "暂无数据")}
    </div>`;
  box.innerHTML = `${lead}${grid}
    <div style="margin-top:10px;font-size:11.5px;color:#9a9a9a;line-height:1.6">${esc(d.note || "")}</div>`;
}


/* ===== 2026-09-29 导航重构：旧深链接自动迁移 =====
   旧 7 类（总览/行情研判/实盘选手/外部参考/自选池/增强功能/学习中心）→ 新 7 类。
   迁移策略「按 tab 反查新归属」：tab key 与一级类解耦且全局唯一，故只要旧 hash 里的 tab 还在，
   就能用它反查出新类名——这天然覆盖了"一个旧类被拆到多个新类"的情况（如 #research/lhb → #money/lhb、
   #research/topics → #info/topics、#enhance/moneyflow → #money/moneyflow）。
   类名未变（overview/watch/learn）或本就不认识的一律原样放行，交给原逻辑处理。 */
const LEGACY_CAT = { research: "money", trade: "players", extref: "opinion", enhance: "watch" };
// 2026-09-29「个股诊断」合并：估值诊股(valuation) 与 个股资金流(stock 模式) 合并为 diag
const LEGACY_TAB = { valuation: "diag" };
function migrateHashRaw(raw) {
  if (!raw) return raw;
  const [c, t] = raw.split("/");
  if (NAV.some((x) => x.key === c)) return (t && LEGACY_TAB[t]) ? c + "/" + LEGACY_TAB[t] : raw;
  if (t) {                                                  // 旧类名 + 有子类：按 tab 反查新归属
    const nc = NAV.find((x) => x.items.some((i) => i.tab === t));
    if (nc) return nc.key + "/" + t;
  }
  if (LEGACY_CAT[c]) return LEGACY_CAT[c] + (t ? "/" + t : "");  // 兜底：旧类名无 tab（或 tab 已不存在）
  return raw;
}

/* 解析 `#大类/子类`；无 hash 时落到首项 */
function navFromHash() {
  const raw0 = (location.hash || "").replace(/^#/, "");
  const raw = migrateHashRaw(raw0);
  // 旧链接扶正后同步写回地址栏（replaceState 不触发 hashchange，避免递归）
  if (raw !== raw0) { try { history.replaceState(null, "", "#" + raw); } catch (e) { /* 无 history 环境：忽略 */ } }
  const [c, t] = raw.split("/");
  const cat = NAV.find((x) => x.key === c);
  if (!cat) return null;
  const items = itemsOfCat(cat);
  // 与 selectNav 同一套规则：`learn_<doc_key>` 的轨道由 kb_sources.yaml 运行时决定，
  // 首次深链接进入时列表还没拉回来。若这里按"未知就纠回首项"处理，会先落到「中短线炒股」，
  // 之后再无机会纠正回来（直接深链接 = 打不开目标轨道）。故 learn_* 一律放行。
  const relaxed = !!DYN_CATS[cat.key] && /^learn_/.test(String(t || ""));
  // 无 tab（仅 #大类）视为"落首项"意图，传 null 让 selectNav 统一处理：动态分类会在轨道
  // 到货后落到真实首项（基础常识），而非占位项（策略回测）。
  return { cat: cat.key, tab: (t && (items.some((i) => i.tab === t) || relaxed)) ? t : null };
}
/* ================= 数据保鲜角标（P0-3，2026-10-15） =================
   统一从 /api/freshness 取各非实时数据域的「最后更新时间」，渲染
   「🕓 数据截至 X · N 天前 · 已陈旧」角标（>3 天变灰）。
   用法：渲染函数里放 freshChip("geo") 占位；数据到达后 applyFreshChips 自动填充。 */
let FRESH = null;
function loadFreshness() {
  api("/api/freshness").then((d) => { FRESH = d; applyFreshChips(); })
    .catch(() => { /* 静默：角标不可用不影响主功能 */ });
}
function applyFreshChips() {
  if (!FRESH || !FRESH.domains) return;
  $$(".fresh-chip[data-fkey]").forEach((el) => {
    const k = el.getAttribute("data-fkey");
    if (FRESH.domains[k]) el.outerHTML = freshChipInner(k, FRESH.domains[k]);
  });
}
function freshChip(key) {
  if (!FRESH || !FRESH.domains || !FRESH.domains[key]) {
    return `<span class="fresh-chip" data-fkey="${escAttr(key)}">🕓 数据保鲜…</span>`;
  }
  return freshChipInner(key, FRESH.domains[key]);
}
function freshChipInner(key, d) {
  const days = d.days_ago;
  const cls = d.stale ? " fresh-stale" : "";
  const ago = (days == null) ? "" : (days <= 0 ? "今天" : days + " 天前");
  const label = `🕓 数据截至 ${escAttr(String(d.updated_at || "").slice(0, 10))}${ago ? " · " + escAttr(ago) : ""}${d.stale ? " · 已陈旧" : ""}`;
  // 已陈旧且定义了补齐动作：渲染为可点击按钮
  if (d.stale && d.action) {
    const hint = escAttr(d.hint || "");
    const autofill = d.autofill ? "1" : "0";
    return `<span class="fresh-chip${cls} fresh-click" data-fkey="${escAttr(key)}" data-action="${escAttr(d.action)}" data-autofill="${autofill}" data-hint="${hint}" title="${hint || "点击尝试补齐"}" style="cursor:pointer">${label} · 点击补齐</span>`;
  }
  return `<span class="fresh-chip${cls}" data-fkey="${escAttr(key)}" `
    + `title="数据来源：${escAttr(d.source || "")}｜最后更新：${escAttr(d.updated_at || "")}">`
    + `${label}</span>`;
}

// 保鲜角标「点击补齐」：能自动补的当场触发，不能的补说明原因
document.addEventListener("click", function (e) {
  const el = e.target && e.target.closest ? e.target.closest(".fresh-chip.fresh-click") : null;
  if (!el) return;
  e.preventDefault();
  const action = el.getAttribute("data-action");
  const autofill = el.getAttribute("data-autofill") === "1";
  const hint = el.getAttribute("data-hint") || "";
  if (!autofill) {
    showToast("该数据需联网调研后更新：" + hint, "warn");
    return;
  }
  const orig = el.textContent;
  el.textContent = "⏳ 正在补齐…";
  const finish = function () { loadFreshness(); };
  let p;
  if (action === "overseas_collect") {
    p = api("/api/overseas", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ live: true, reset: true, confirm: true }) })
      .then(function () { showToast("海外舆情已重新采集", "ok"); })
      .catch(function (e) { showToast("采集失败：" + (e && e.message ? e.message : e), "err"); });
  } else if (action === "geo_refresh") {
    p = api("/api/geo_refresh").then(function () { showToast("国际局势已重导", "ok"); })
      .catch(function (e) { showToast("重导失败：" + (e && e.message ? e.message : e), "err"); });
  } else {
    showToast(hint || "暂不支持自动补齐", "warn");
    finish();
    return;
  }
  Promise.resolve(p).finally(finish);
});
function injectFreshChip(sel, key) {
  try {
    const h = document.querySelector(sel);
    if (!h) return;
    // 幂等：同 fkey 已存在则先移除，避免面板重渲时重复注入角标
    const prev = h.querySelector(".fresh-chip[data-fkey='" + (window.CSS ? CSS.escape(key) : key) + "']");
    if (prev) prev.remove();
    h.insertAdjacentHTML("afterbegin", freshChip(key));
  } catch (e) {}
}

/* P2-1：海外舆情面板子导航（舆情 / 热点）——合并原「美股港股舆情」+「国外驱动热点」两个并列面板。
   两个子视图共用同一份海外数据，零额外请求；默认显示舆情，点「热点」切到热点子容器。 */
function ensureOverseasShell() {
  const root = $("#p-overseas_sentiment");
  if (!root || root.dataset.ovShell) return;
  root.dataset.ovShell = "1";
  root.innerHTML = `
    <div class="panel-subnav ov-subnav">
      <button type="button" class="subnav-btn active" data-ov="sentiment">舆情</button>
      <button type="button" class="subnav-btn" data-ov="hotspots">热点</button>
      <span class="subnav-slot"></span>
    </div>
    <div id="ov-sub-sentiment" class="ov-sub"></div>
    <div id="ov-sub-hotspots" class="ov-sub" style="display:none"></div>`;
  $$(".subnav-btn", root).forEach((b) => {
    b.onclick = () => {
      const which = b.dataset.ov;
      $$(".subnav-btn", root).forEach((x) => x.classList.toggle("active", x === b));
      const s = $("#ov-sub-sentiment"), hp = $("#ov-sub-hotspots");
      if (s) s.style.display = which === "sentiment" ? "" : "none";
      if (hp) hp.style.display = which === "hotspots" ? "" : "none";
    };
  });
}

function initNav() {
  renderNav();
  loadFreshness();   // P0-3：预取各非实时数据域保鲜状态，供角标渲染
  const h = navFromHash();
  if (h) selectNav(h.cat, h.tab, false);
  else selectNav(NAV[0].key, firstRealTab(NAV[0].items), true);
}
window.addEventListener("hashchange", () => {
  const h = navFromHash();
  if (h) selectNav(h.cat, h.tab, false);
});
/* 供面板内空状态按钮等调用：跳到某个子板块 */
function showTab(tab) {
  const c = catOf(tab);
  if (c) selectNav(c.key, tab, true);
}

/* ================= 跟我学习：知识库（中短线炒股） =================
   数据来自 /api/kb/doc（后端把本地 md 解析成结构化 JSON 后落库）。
   口径：① 单知识库以 kb_sources.yaml 的 doc_key 为准；② 内容哈希没变时后端
   会跳过重写，前端据此显示「内容未变」而不是谎报「已更新」。 */
const KB = {
  key: "zdx_stock", doc: null,
  docs: {},                 // docKey -> /api/kb/doc 返回（多学习轨道各存一份）
  loading: {},              // docKey -> 是否在拉取
  error: "",
  tracks: null, tracksLoading: false,
  // 「点左栏大类」时若轨道还没拉回来，落到的是占位项（策略回测）而非真实首项（基础常识）。
  // 这里记下"等轨道到货后请改落到真实首项"的待办，由 loadKbTracks 兑现、由 selectNav 维护。
  pendingFirstTrack: null,
  interval: 86400, stale: 172800,
};
/* 「学习中心」的横向子类 = kb_sources.yaml 声明的各条知识轨道（按 order 排序）
   + 固定的「策略回测」「学习轨道管理」。 */
/* 支持「动态轨道」的一级分类：轨道挂在哪一类下，由 kb_sources.yaml 的 group 字段决定
   （缺省 learn）。2026-09-26 合并分类后只剩 learn 一个分类，但保留这套「配置决定归属」的
   机制——将来若要再拆板块（例如把「基础常识」单独拎出去），改 yaml 的 group 一行即可，
   无须改前端代码。tab 前缀仍统一用 learn_（内部标识稳定，深链接不会因为挪了位置就失效）。 */
const DYN_CATS = { learn: "复盘成长" };

function tracksOfCat(catKey) {
  // order 由 kb_sources.yaml 提供（数值小的排前面，缺省 0，非法值按 0 处理）。
  // 为什么不直接用库内顺序：「基础常识」比「中短线炒股」晚吸取，靠库序会排到后面去，
  // 而用户明确要求「基础常识」在横条里排第一。故排序权交给配置（服务端 /api/kb/list 透出）。
  const all = toArr(KB.tracks && KB.tracks.docs)
    .filter((d) => ((d.group || "learn") === catKey));
  all.sort((a, b) => (Number(a.order) || 0) - (Number(b.order) || 0));  // V8 排序稳定：同值保持原序
  return all.map((d) => ({
    tab: "learn_" + d.doc_key,
    label: d.track || d.title || d.doc_key,
    docKey: d.doc_key,
  }));
}
/* 某个大类**当前实际**的子项列表。
   为什么需要它：NAV 里写死的只是"轨道还没加载回来时的占位项"，真实轨道来自
   kb_sources.yaml（运行时由 /api/kb/list 返回）。若校验 tabKey 时用静态 NAV.items，
   `learn_<新doc_key>` 一律校验不通过 → 点进去会被"纠正"回首项。 */
function itemsOfCat(cat) {
  if (!cat) return [];
  if (!DYN_CATS[cat.key]) return cat.items;
  // 2026-10-11 王总第三回合②：B站 UP 主（bili_*）不再逐个占横条（"一人一横条很累、不清晰"），
  // 统一收进「B站博主」分组框（renderBiliAuthors 按类聚合），故从扁平横条里剔除。
  const tracks = tracksOfCat(cat.key).filter((d) => !/^bili_/.test(d.docKey || ""));
  // 学习中心：动态知识轨道（内容优先）在前，固定子板块在后。
  // ⚠️ 陷阱（设计文档 §2 决策 5）：此分支用 tracks.concat(...) 重建子项，写在 NAV.learn.items
  // 里的静态项会被**整体丢弃**。故固定子板块必须在此**显式并回**，否则轨道一加载回来，
  // 「策略回测」「学习轨道管理」就从横条上消失（刷新后尤其明显）。轨道一条都没回来时仍用静态 cat.items 占位。
  const fixed = [
    { tab: "strategy_lab", label: "策略回测" },
    { tab: "fundmasters", label: "基金经理策略" },
    // 2026-10-11 王总第三回合②：B站 UP 主按类分组框（点名开该人知识沉淀）
    { tab: "bili_authors", label: "B站博主" },
    // 2026-10-11 王总第三回合③：技术面/价值面两堆（3306/397 条）独立入口
    { tab: "bili_techvalue", label: "技术·价值教学" },
    // 2026-10-20 进哥：豆包「技术方法」独立面板（选项 A —— 与上面 B站 面板并列，
    // 但数据源、结构、交互都不同：B站是分类堆+方法卡片，豆包是 2701 条视频语料+搜索分页）。
    { tab: "tech", label: "豆包技术方法" },
    { tab: "integrate", label: "整合图谱" },
    { tab: "dy_reso", label: "豆包×主力共振" },
    { tab: "learn_tracks", label: "学习轨道管理" },
    // 2026-10-24 进哥：Kronos K线基础模型预测（开源基础模型概率预测，独立顶层 tab，与上面并列）
    { tab: "kronos", label: "Kronos K线预测", tag: "AI基础模型" },
    { tab: "funnel", label: "方法共振" },
  ];
  // 固定子板块（策略回测 / 学习轨道管理）无论轨道是否加载回来都要在场：
  // 无轨道时直接用 fixed（其已是静态 cat.items 的超集）。
  return tracks.length ? tracks.concat(fixed) : fixed;
}
/* 动态学习轨道面板在 index.html 里没有预置容器 → 首次进入时按需补一个。
   容器由 .main 改为 .content（2026-09-23 外壳改造后，.wrap/.layout/.main 已合并为 .shell/.content）。 */
/* ================= 基金经理策略（自包含 iframe，含巴菲特作为价值一脉子项） =================
   数据：static/fund_masters/index.json + reports/<key>/<期>.html（后端 fund_masters.py 本地引擎生成）。
   整块 UI 是自包含页面，iframe 内嵌，不污染本文件渲染逻辑。 */
function loadFundMasters() {
  const box = ensurePanel("fundmasters");
  if (!box) return;
  box.innerHTML = '<iframe src="/static/fund_masters/index.html" '
    + 'style="width:100%;height:calc(100vh - 132px);border:0;background:#F5F6F8"></iframe>';
}

/* 2026-10-20：基金经理策略 iframe 内点击共识股票 → 打开个股详情抽屉（K线/换手/持股全套）。
   iframe 与本页同源，通过 postMessage 通知本页 openStock；校验 origin 防其他页面注入。 */
window.addEventListener("message", (ev) => {
  if (ev.origin !== location.origin) return;
  const d = ev.data || {};
  if (d.type === "fm-open-stock" && d.symbol) openStock(String(d.symbol));
});

function ensurePanel(tab) {
  let p = $("#p-" + tab);
  if (!p) {
    p = document.createElement("div");
    p.className = "panel";
    p.id = "p-" + tab;
    const host = document.querySelector(".content");
    if (host) host.appendChild(p); else return null;
  }
  return p;
}
/* 子标签 → 知识库 doc_key（`learn_<doc_key>`）。动态轨道在 index.html 里没有预置容器，
   统一由 ensurePanel() 按需补，渲染入口统一走 ensureKbDocFor()。 */
function docKeyOfTab(tab) { return String(tab || "").replace(/^learn_/, ""); }

function ensureKbDocFor(docKey, force) {
  docKey = docKey || KB.key;
  const box = ensurePanel("learn_" + docKey);
  if (!box) return;
  const cached = KB.docs[docKey];
  if (cached && !force) { renderKbDocFor(docKey); return; }
  if (KB.loading[docKey]) return;
  KB.loading[docKey] = true;
  if (!cached) box.innerHTML = `<div class="empty">正在读取知识库…</div>`;
  api("/api/kb/doc?key=" + encodeURIComponent(docKey))
    .then((d) => {
      KB.docs[docKey] = d;
      if (docKey === KB.key) KB.doc = d;      // 顶部状态 chip 仍以主库为准
      KB.error = "";
      renderKbDocFor(docKey);
    })
    .catch((e) => {
      KB.error = apiErrText(e);
      box.innerHTML = `<div class="empty">知识库读取失败：${esc(KB.error)}</div>`;
    })
    .finally(() => { KB.loading[docKey] = false; });
}
/* 兼容旧调用点（顶栏状态 chip / 面板内按钮 onclick="ensureKbDoc(true)"） */
function ensureKbDoc(force) { return ensureKbDocFor(KB.key, force); }

/* ---------- 「学习中心 → B站 UP 主知识沉淀」主视图（kind="article"） ----------
   主题文章型：按作者自身知识体系组织的 H2 分章 + H3 小节 + 要点/段落/表格。
   与「中短线炒股」(report) 和「基础常识」(glossary) 并列的第三种渲染；
   与后端 kb.parse_article 一一对应（2026-10-11 新增）。 */
function renderKbArticle(box, d, docKey) {
  if (!box) return;
  renderKbStateChip(docKey, d);
  if (!d) { box.innerHTML = `<div class="empty">正在读取知识库…</div>`; return; }
  if (d.status === "empty") {
    box.innerHTML = `<div class="empty">${esc(d.message || "尚未吸取知识库")}<br/><br/>` +
      `来源文件：<code>${esc(d.source_path || "未找到")}</code><br/><br/>` +
      `<button class="btn primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button> ` +
      `<button class="btn" onclick="showTab('learn_tracks')">查看学习轨道管理</button></div>`;
    return;
  }
  const m = d.meta || {};
  const f = freshnessOf([docKey]);
  const sections = toArr(d.sections);
  const metaNotes = toArr(d.meta_notes);
  const outline = toArr(d.outline);

  const toc = outline.map((o) =>
    `<a class="kb-toc-item" data-sec="kba-${escAttr(o.key)}" href="#kba-${escAttr(o.key)}">` +
    `<span>${esc(o.title)}</span><span class="kb-toc-n">${o.count || 0}</span></a>`).join("");

  // 一段正文（段落 / 列表 / 注记 / 表格）的通用渲染，章与小节复用
  const blockHtml = (blk) => {
    const ps = toArr(blk.paragraphs), its = toArr(blk.items), ns = toArr(blk.notes), tbs = toArr(blk.tables);
    let h = "";
    if (ns.length) h += `<div class="kb-note">${ns.map((x) => mdBold(x)).join("<br/>")}</div>`;
    if (ps.length) h += ps.map((p) => `<p class="kb-p">${mdBold(p)}</p>`).join("");
    if (its.length) h += `<ul class="kb-ul">${its.map((t) => `<li>${mdBold(t)}</li>`).join("")}</ul>`;
    if (tbs.length) h += tbs.map((t) => `<div class="kb-block"><div class="kb-block-h">${esc(t.title || "表格")}</div>${mdTable(t)}</div>`).join("");
    return h;
  };

  const sectionHtml = sections.map((s) => {
    const subs = toArr(s.subsections);
    return `<section class="kb-sec" id="kba-${escAttr(s.key)}">
      <div class="kb-sec-h">${esc(s.title)}
        ${subs.length ? `<span class="kb-hint">${subs.length} 个小节</span>` : ""}</div>
      ${blockHtml(s)}
      ${subs.length ? `<div class="kb-subs">${subs.map((sub) =>
        `<div class="kb-sub"><div class="kb-sub-h">${esc(sub.title)}</div>${blockHtml(sub)}</div>`).join("")}</div>` : ""}
    </section>`;
  }).join("");

  box.innerHTML = `
    <div class="kb-head">
      <div class="kb-head-l">
        <h2>${esc(d.title || m.title || "知识沉淀")}</h2>
        <div class="kb-head-meta">
          <span class="chip-s">版本 ${esc(d.version || "—")}</span>
          <span class="chip-s">${m.section_total ?? sections.length} 章 · ${m.node_total ?? d.node_count ?? 0} 个知识点</span>
          <span class="chip-s chip-fresh ${freshCls(f.state)}" title="${escAttr(freshTitle(f))}">${freshLine(f, "学习内容")}</span>
        </div>
        <div class="kb-head-src">来源文件：<code>${esc(d.source_path || "—")}</code>
          ${d.source_mtime ? `｜上游修改：${esc(d.source_mtime)}` : ""}
          ｜吸取：${esc(d.ingested_at || "—")}
        </div>
      </div>
      <div class="kb-head-r">
        <button class="btn sm" onclick="ensureKbDocFor('${escAttr(docKey)}', true)">重新读取</button>
        <button class="btn sm primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button>
      </div>
    </div>
    ${KB.error ? `<div class="hint">上次读取告警：${esc(KB.error)}</div>` : ""}
    ${metaNotes.length ? `<div class="kb-note kb-note-src">${metaNotes.map((x) => mdBold(x)).join("<br/>")}</div>` : ""}
    <div class="kb-toc">${toc}</div>
    ${sectionHtml}`;

  box.querySelectorAll(".kb-toc-item").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      const t = box.querySelector("#" + CSS.escape(a.dataset.sec));
      if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
  requestAnimationFrame(() => markWideTables(box));
}

/* 渲染总入口：按文档类型分派。
   - kind === "article"  → 「B站 UP 主知识沉淀」视图（主题文章型，按作者知识体系分章）
   - kind === "glossary" → 「基础常识 / 名词解释」视图（价值投资指标等）
   - 其它 → 原「中短线炒股」判读视图（结论/流派/回测/风险） */
function renderKbDocFor(docKey) {
  const box = ensurePanel("learn_" + docKey);
  if (!box) return;
  const d = KB.docs[docKey];
  if (d && d.kind === "article") { renderKbArticle(box, d, docKey); return; }
  if (d && d.kind === "glossary") { renderKbGlossary(box, d, docKey); return; }
  renderKbDocBody(box, d, docKey);
}
function loadKbTracks() {
  const box = ensurePanel("learn_tracks");
  if (!box) return;
  if (KB.tracks && !KB.tracksLoading) { renderKbTracks(); return; }
  if (KB.tracksLoading) return;
  KB.tracksLoading = true;
  api("/api/kb/list")
    .then((d) => {
      KB.tracks = d;
      KB.interval = d.interval_seconds || KB.interval;
      KB.stale = d.stale_seconds || KB.stale;
      // 轨道条数可能变化 → 重绘内容区顶部的横向子板块条（paintSubnav 只画不跳，无递归风险）
      // 动态轨道将来可能分挂在多个分类下（DYN_CATS 的每个 key），故逐个重绘、不只是当前类。
      Object.keys(DYN_CATS).forEach((ck) => {
        const c = NAV.find((x) => x.key === ck);
        if (!c) return;
        const keep = CURRENT.cat === ck ? CURRENT.tab : firstRealTab(itemsOfCat(c));
        paintSubnav(c, keep);
      });
      renderKbTracks();
      // 兑现"点左栏大类 → 真实首项"：用户点大类时轨道未到，落到了占位项（策略回测）。
      // 现在轨道到货，若挂着 pending 待办且当前仍在占位项，改落到真实首项（基础常识）。
      if (KB.pendingFirstTrack) {
        const ck = KB.pendingFirstTrack; KB.pendingFirstTrack = null;
        const c = NAV.find((x) => x.key === ck);
        if (c && DYN_CATS[ck]) {
          const _ft = firstRealTab(itemsOfCat(c));   // 真实首项（基础常识，order 最小）
          if (_ft && _ft !== CURRENT.tab) {
            selectNav(ck, _ft, true);          // 改写 hash + 落到真实首项（hashchange 会完成渲染）
            return;
          }
        }
      }
      // 轨道一加载完，就按"当前所处位置"重跑一次导航 —— 此刻才第一次知道每条轨道的实际归属：
      // ① 旧深链接（轨道挪过板块、地址还停在旧分类）会被纠偏到实际所属板块；
      // ② 首屏直接深链接进入某条轨道时，面板此刻才拿得到数据补渲染。
      selectNav(CURRENT.cat, CURRENT.tab, false);
    })
    .catch((e) => { box.innerHTML = `<div class="empty">学习轨道读取失败：${esc(apiErrText(e))}</div>`; })
    .finally(() => { KB.tracksLoading = false; });
}
function kbReload(docKey) {
  // 加固：docKey 必须是非空字符串才采纳。防御未来某处再把事件对象等非字符串传进来
  //（onclick=kbReload 的旧坑曾让 key=PointerEvent → 选择器拼出 "#p-learn_[object PointerEvent]"）。
  const key = (typeof docKey === "string" && docKey) ? docKey : KB.key;
  const btn = $("#btnKbReload");
  if (btn) { btn.disabled = true; btn.textContent = "吸取中…"; }
  setHint("正在重新吸取知识库…");
  api("/api/kb/reload", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) })
    .then((r) => {
      const one = toArr(r && r.results)[0] || {};
      const msg = one.status !== "ok"
        ? `知识库吸取失败：${one.error || "未知原因"}`
        : (one.skipped ? "知识库内容未变，已跳过重建（未产生无意义写入）。"
                       : `知识库已更新：${one.version || ""} · ${one.node_count ?? 0} 个节点。`);
      setHint(msg);
      KB.doc = null; KB.docs = {}; KB.tracks = null;
      loadRefreshStatus();
      ensureKbDocFor(key, true); loadKbTracks();
    })
    .catch((e) => setHint("知识库吸取失败：" + apiErrText(e)))
    .finally(() => {
      const b = $("#btnKbReload");
      if (b) { b.disabled = false; b.textContent = "立即吸取"; }
    });
}
/* 顶部横向栏里的知识库状态小 chip（版本 · 节点数 · 时效）。
   跟随「当前正在看的那条学习轨道」，而不是永远显示主库——否则切到新增轨道时
   chip 仍是另一份文档的版本号，容易被误读为「没生效」。 */
function renderKbStateChip(docKey, doc) {
  const chip = $("#kbStateChip");
  if (!chip) return;
  const key = docKey || KB.key;
  const d = doc || KB.doc;
  chip.className = "chip-s chip-fresh";
  if (!d || d.status === "empty") {
    chip.textContent = "知识库：未吸取";
    chip.title = (d && d.message) || "尚未吸取知识库";
    return;
  }
  const f = freshnessOf([key]);
  chip.className = "chip-s chip-fresh " + freshCls(f.state);
  chip.textContent = `知识库 ${d.version || ""} · ${d.node_count ?? 0} 节点`;
  chip.title = `${d.track || ""}｜${d.title || ""}\n吸取：${d.ingested_at || "—"}\n来源：${d.source_path || "—"}\n` + freshTitle(f);
}

/* 知识库正文里偶有 markdown 加粗记号（如评级 **B**），展示前去掉记号只留文字 */
function mdPlain(s) { return String(s == null ? "" : s).replace(/\*\*/g, "").replace(/\s+/g, " ").trim(); }
// 文章型渲染用：先转义再让 **粗体** / *斜体* 生效（数据来自本地可信 md，转义后安全）
function mdBold(s) { return esc(String(s == null ? "" : s)).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>"); }

/* 「跟我学习 → 中短线炒股」主视图：把整份知识库按「结论 → 流派 → 对比 → 误区 → 回测 → 来源 → 风险」铺开。
   设计口径：① 每一块都给出**来源与规模**（几位博主 / 几条策略），不做无出处的断言；
             ② 风险提示置于最底且完整保留原文，不删减；③ 博主详情放抽屉，面板本身保持可扫读。 */
function renderKbDocBody(box, d, docKey) {
  if (!box) return;
  renderKbStateChip(docKey, d);
  if (!d) { box.innerHTML = `<div class="empty">正在读取知识库…</div>`; return; }
  if (d.status === "empty") {
    box.innerHTML = `<div class="empty">${esc(d.message || "尚未吸取知识库")}<br/><br/>` +
      `来源文件：<code>${esc(d.source_path || "未找到")}</code><br/><br/>` +
      `<button class="btn primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button> ` +
      `<button class="btn" onclick="showTab('learn_tracks')">查看学习轨道管理</button></div>`;
    return;
  }
  const m = d.meta || {};
  const f = freshnessOf([docKey]);
  const outline = toArr(d.outline);

  const toc = outline.map((o) =>
    `<a class="kb-toc-item" data-sec="kb-sec-${escAttr(o.key)}" href="#kb-sec-${escAttr(o.key)}">` +
    `<span>${esc(o.title)}</span><span class="kb-toc-n">${o.count || 0}</span></a>`).join("");

  const conclusions = toArr(d.conclusions).map((c) =>
    `<div class="kb-block"><div class="kb-block-h">${esc(c.title)}</div>` +
    `<ul class="kb-ul">${toArr(c.items).map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>`).join("");

  const schools = toArr(d.schools).map((s) => {
    // 字段数分开标注「精读 / 视频」：实测有 16 位博主的公开资料只来自视频整理、
    // 没有原帖精读字段，写成含糊的「N 字段」会让人误以为每位都有精读笔记。
    const bs = toArr(s.bloggers).map((b) => {
      const nn = toArr(b.phase_notes).length, nv = toArr(b.phase_video).length;
      return `<button class="kb-blogger" data-school="${escAttr(s.name)}" data-blogger="${escAttr(b.name)}" ` +
      `title="${escAttr((b.style || "点击查看完整档案").slice(0, 160))}">` +
      `<span class="kb-blogger-n">${esc(b.name)}</span>` +
      (nn ? `<span class="kb-tag kb-tag-o" title="精读笔记字段数（来自原帖/专访）">精读 ${nn}</span>` : "") +
      (nv ? `<span class="kb-tag" title="视频笔记字段数（来自视频整理）">视频 ${nv}</span>` : "") +
      // 仅视频：无精读字段（上游就没有原帖/专访资料），一眼可辨，免得以为程序漏抓
      (nv && !nn ? `<span class="kb-tag kb-tag-v" title="该博主在上游知识库中没有原帖精读字段，方法整理全部来自视频笔记">仅视频</span>` : "") +
      `</button>`;
    }).join("");
    return `<div class="kb-school">
      <div class="kb-school-h"><b>${esc(s.name)}</b>
        <span class="chip-s">${s.actual_count ?? 0} 位博主</span>
        ${s.declared_count !== s.actual_count ? `<span class="chip-s chip-fresh chip-stale" title="正文声明人数与实得人数不一致，已按实得展示">声明 ${s.declared_count}</span>` : ""}
      </div>
      ${s.common_method ? `<div class="kb-school-common"><span class="kb-k">共性方法</span>${esc(s.common_method.slice(0, 260))}${s.common_method.length > 260 ? "…" : ""}</div>` : ""}
      <div class="kb-bloggers">${bs}</div>
    </div>`;
  }).join("");

  const compare = toArr(d.compare).map((c) => {
    const tb = toArr(c.tables).map((t) => mdTable(t)).join("");
    return `<div class="kb-block"><div class="kb-block-h">${esc(c.title)}</div>` +
      `<ul class="kb-ul">${toArr(c.items).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>${tb}</div>`;
  }).join("");

  const pitfalls = toArr(d.pitfalls).map((p, i) =>
    `<div class="kb-block"><div class="kb-block-h">${esc(p.title || ("误区 " + (i + 1)))}</div>` +
    `<ul class="kb-ul kb-ul-warn">${toArr(p.items).map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>`).join("");

  const backtests = toArr(d.backtests).map((t) => {
    const cols = toArr(t.columns);
    const ri = cols.findIndex((c) => /评级/.test(c));
    const rowArr = toArr(t.rows);
    const isNum = numericCols(cols, rowArr);
    const rows = rowArr.map((r) => {
      const rating = ri >= 0 ? mdPlain(toArr(r)[ri]) : "";
      return `<tr>${toArr(r).map((c, ci) => {
        if (ci === ri) return `<td${isNum[ci] ? ' class="num"' : ""}>${ratingBadge(rating)}</td>`;
        const txt = mdPlain(c);
        const cls = [isNum[ci] ? "num" : "", pctCellCls(cols[ci], txt)].filter(Boolean).join(" ");
        return `<td${cls ? ` class="${cls}"` : ""}>${esc(txt)}</td>`;
      }).join("")}</tr>`;
    }).join("");
    return `<div class="kb-bt"><div class="kb-bt-h">${esc(t.title)} <span class="chip-s">${rowArr.length} 条</span>` +
      `<span class="kb-wide-tip" hidden>↔ 内容较宽，可左右滚动</span></div>` +
      `<div class="kb-scroll"><table class="kb-table"><thead><tr>${cols.map((c, ci) => `<th${isNum[ci] ? ' class="num"' : ""}>${esc(c)}</th>`).join("")}</tr></thead>` +
      `<tbody>${rows}</tbody></table></div></div>`;
  }).join("");

  const srcTb = toArr(d.sources).map((t) => mdTable(t)).join("");
  const risks = toArr(d.risks).map((r) =>
    `<div class="kb-risk"><span class="kb-risk-n">${r.index ?? ""}</span><span>${esc(r.text)}</span></div>`).join("");

  box.innerHTML = `
    <div class="kb-head">
      <div class="kb-head-l">
        <h2>${esc(d.title || m.title || "学习知识库")}</h2>
        <div class="kb-head-meta">
          <span class="chip-s">版本 ${esc(d.version || "—")}</span>
          <span class="chip-s">${m.school_total ?? 0} 流派 · ${m.blogger_total ?? 0} 博主</span>
          <span class="chip-s">${m.strategy_total ?? 0} 条回测 · ${m.source_total ?? 0} 条来源</span>
          <span class="chip-s">${d.node_count ?? 0} 个知识点</span>
          <span class="chip-s chip-fresh ${freshCls(f.state)}" title="${escAttr(freshTitle(f))}">${freshLine(f, "学习内容")}</span>
        </div>
        <div class="kb-head-src">来源文件：<code>${esc(d.source_path || "—")}</code>
          ${d.source_mtime ? `｜上游修改：${esc(d.source_mtime)}` : ""}
          ｜吸取：${esc(d.ingested_at || "—")}
          ${d.prev_ingested_at ? `｜上一版吸取：${esc(d.prev_ingested_at)}` : ""}
        </div>
      </div>
      <div class="kb-head-r">
        <button class="btn sm" onclick="ensureKbDocFor('${escAttr(docKey)}', true)">重新读取</button>
        <button class="btn sm primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button>
      </div>
    </div>
    ${KB.error ? `<div class="hint">上次读取告警：${esc(KB.error)}</div>` : ""}
    <div class="kb-toc">${toc}</div>

    <div class="kb-sec" id="kb-sec-conclusions"><div class="kb-sec-h">核心结论</div>${conclusions}</div>

    <div class="kb-sec" id="kb-sec-schools"><div class="kb-sec-h">八大流派 · 50 位博主
      <span class="kb-hint">点击任意博主查看完整档案（精读笔记 / 视频笔记 / 来源）</span></div>
      <div class="kb-schools">${schools}</div></div>

    <div class="kb-sec" id="kb-sec-compare"><div class="kb-sec-h">跨流派对比</div>${compare}</div>
    <div class="kb-sec" id="kb-sec-pitfalls"><div class="kb-sec-h">常见误区</div>${pitfalls}</div>
    <div class="kb-sec" id="kb-sec-backtests"><div class="kb-sec-h">策略回测
      <span class="kb-hint">评级口径以正文为准（B 级＝可用，C 级＝偏弱/无效），此处不重新定义</span></div>
      <div class="kb-note">回测为历史统计结果，<b>不代表未来收益</b>；不同回测表列数不同（部分表无「来源博主」列），故分表展示、不强行对齐。</div>
      ${backtests}</div>
    <div class="kb-sec" id="kb-sec-sources"><div class="kb-sec-h">信息来源（按博主）</div>
      <div class="kb-note">短线游资在抖音多为仿冒/搬运号，本表逐人标注账号核验状态；<b>凡主动私信、拉群、收费荐股者一律视为诈骗</b>。</div>
      ${srcTb}</div>
    <div class="kb-sec kb-sec-risk" id="kb-sec-risks"><div class="kb-sec-h">风险提示</div>${risks}</div>`;

  // 目录跳转：滚动到对应区块（不写 hash，避免与导航深链接冲突）
  box.querySelectorAll(".kb-toc-item").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      const t = box.querySelector("#" + CSS.escape(a.dataset.sec));
      if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
  // 博主卡片 → 抽屉展示完整档案
  box.querySelectorAll(".kb-blogger").forEach((b) => {
    b.onclick = () => openBlogger(b.dataset.school, b.dataset.blogger, b);
  });
  // 宽表提示：必须等 innerHTML 落地、浏览器算出布局后再量——
  // 同一帧内读取时 scrollWidth 恰好等于 clientWidth，会恒判为"不溢出"
  requestAnimationFrame(() => markWideTables(box));
  // 2026-10-11 王总：中短线炒股主库顶部加「一键筛股」——直接给当前买点票结论
  if (docKey === "zdx_stock") injectBiliScanBar(box);
}

/* ---------- 「知识获取 → 基础常识」主视图（kind="glossary"） ----------
   与「中短线炒股」的判读视图不同：那边是"结论—流派—回测—风险"的研究报告，
   这边是**一个词条一张卡**的工具书。每个词条只回答三件事，便于横向比较：
       这是什么 → 怎么算 → 为什么要看它
   口径：内容全部来自本地 md（config/kb_sources.yaml 声明），程序只做结构化排版，
   不改写、不补充原文；参考值一律标注为"经验区间"，不写成硬性门槛。 */
const GLOSSARY_FIELD_ORDER = ["定义", "怎么算", "为什么重要"];

function renderKbGlossary(box, d, docKey) {
  if (!box) return;
  renderKbStateChip(docKey, d);
  if (!d) { box.innerHTML = `<div class="empty">正在读取知识库…</div>`; return; }
  if (d.status === "empty") {
    box.innerHTML = `<div class="empty">${esc(d.message || "尚未吸取知识库")}<br/><br/>` +
      `来源文件：<code>${esc(d.source_path || "未找到")}</code><br/><br/>` +
      `<button class="btn primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button></div>`;
    return;
  }
  const m = d.meta || {};
  const f = freshnessOf([docKey]);
  const groups = toArr(d.groups);
  const outline = toArr(d.outline);

  const toc = outline.map((o) =>
    `<a class="kb-toc-item" data-sec="kbg-${escAttr(o.key)}" href="#kbg-${escAttr(o.key)}">` +
    // 纯表格/文字节没有词条（count=0），显示 0 会让人误以为"解析失败"，故留空
    `<span>${esc(o.title)}</span><span class="kb-toc-n">${o.count ? o.count : ""}</span></a>`).join("");

  const fieldRows = (t) => {
    const fs = toArr(t.fields);
    if (!fs.length) return "";
    const idx = (k) => { const i = GLOSSARY_FIELD_ORDER.indexOf(k); return i < 0 ? 99 : i; };
    const sorted = fs.slice().sort((a, b) => idx(a.k) - idx(b.k));
    return `<dl class="kb-kv">${sorted.map((x) => {
      // 「注意/风险」型字段是提醒项，单独上色，扫读时先看到它
      const warn = /注意|警惕|风险|误区/.test(x.k || "");
      return `<div class="kb-kv-row${warn ? " kb-kv-warn" : ""}">` +
        `<dt class="kb-kv-k">${esc(x.k)}</dt><dd class="kb-kv-v">${esc(mdPlain(x.v))}</dd></div>`;
    }).join("")}</dl>`;
  };

  const termCard = (t) => {
    const items = toArr(t.items);
    return `<article class="kb-term">
      <div class="kb-term-h">
        <b class="kb-term-n">${esc(t.name)}</b>
        ${t.en ? `<span class="kb-term-en">${esc(t.en)}</span>` : ""}
        ${t.alias ? `<span class="chip-s" title="别名/俗称">${esc(t.alias)}</span>` : ""}
      </div>
      ${t.one_line ? `<div class="kb-term-one">${esc(mdPlain(t.one_line))}</div>` : ""}
      ${fieldRows(t)}
      ${items.length ? `<ul class="kb-ul">${items.map((x) => `<li>${esc(mdPlain(x))}</li>`).join("")}</ul>` : ""}
      ${t.tip ? `<div class="kb-tip">${esc(mdPlain(t.tip))}</div>` : ""}
    </article>`;
  };

  const groupHtml = groups.map((g) => {
    const terms = toArr(g.terms);
    const tables = toArr(g.tables);
    return `<section class="kb-sec" id="kbg-${escAttr(g.key)}">
      <div class="kb-sec-h">${esc(g.title)}
        <span class="kb-hint">${terms.length ? terms.length + " 个词条" : ""}
          ${g.plain ? "｜本节为文字说明，未拆成词条" : ""}</span></div>
      ${toArr(g.intro).length ? `<ul class="kb-ul kb-ul-lead">${toArr(g.intro).map((x) => `<li>${esc(mdPlain(x))}</li>`).join("")}</ul>` : ""}
      ${terms.length ? `<div class="kb-terms">${terms.map(termCard).join("")}</div>` : ""}
      ${tables.map((t) => `<div class="kb-block"><div class="kb-block-h">${esc(t.title || "速查表")}</div>${mdTable(t)}</div>`).join("")}
      ${toArr(g.notes).length ? `<div class="kb-note">${toArr(g.notes).map((x) => esc(mdPlain(x))).join("<br/>")}</div>` : ""}
    </section>`;
  }).join("");

  const srcTb = toArr(d.sources).map((t) => mdTable(t)).join("");
  const risks = toArr(d.risks).map((r) =>
    `<div class="kb-risk"><span class="kb-risk-n">${r.index ?? ""}</span><span>${esc(r.text)}</span></div>`).join("");

  box.innerHTML = `
    <div class="kb-head">
      <div class="kb-head-l">
        <h2>${esc(d.title || m.title || "基础常识")}</h2>
        <div class="kb-head-meta">
          <span class="chip-s">版本 ${esc(d.version || "—")}</span>
          <span class="chip-s">${m.group_total ?? groups.length} 节 · ${m.term_total ?? 0} 个词条</span>
          <span class="chip-s">${m.node_total ?? d.node_count ?? 0} 个知识点</span>
          <span class="chip-s chip-fresh ${freshCls(f.state)}" title="${escAttr(freshTitle(f))}">${freshLine(f, "学习内容")}</span>
        </div>
        <div class="kb-head-src">来源文件：<code>${esc(d.source_path || "—")}</code>
          ${d.source_mtime ? `｜上游修改：${esc(d.source_mtime)}` : ""}
          ｜吸取：${esc(d.ingested_at || "—")}
        </div>
      </div>
      <div class="kb-head-r">
        <button class="btn sm" onclick="ensureKbDocFor('${escAttr(docKey)}', true)">重新读取</button>
        <button class="btn sm primary" onclick="kbReload('${escAttr(docKey)}')">立即吸取</button>
      </div>
    </div>
    ${KB.error ? `<div class="hint">上次读取告警：${esc(KB.error)}</div>` : ""}
    <div class="hint" style="margin-bottom:12px">
      <b>这一节是"看懂数字"的地基</b>：把选股时最常被提到的指标逐个讲清楚——<b>它是什么、怎么算、为什么要看它</b>。
      先看懂指标再看龙虎榜与牛散持仓，才不会被"高增长""低估值"这类说法带走。<b>本页是知识整理，不构成任何买卖建议。</b>
    </div>
    <div class="kb-toc">${toc}</div>
    ${groupHtml}
    ${srcTb ? `<section class="kb-sec" id="kbg-sources"><div class="kb-sec-h">资料来源与口径</div>${srcTb}</section>` : ""}
    ${risks ? `<section class="kb-sec kb-sec-risk" id="kbg-risks"><div class="kb-sec-h">风险提示与免责</div>${risks}</section>` : ""}`;

  box.querySelectorAll(".kb-toc-item").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      const t = box.querySelector("#" + CSS.escape(a.dataset.sec));
      if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
  requestAnimationFrame(() => markWideTables(box));
}
/* 纯文本版的时效文案（表格单元格的 freshText 会返回带 <span> 的 HTML，故单列一个）。
   what＝主语，便于在「学习内容」这类非行情场景说人话。 */
function freshLine(f, what) {
  const w = what || "该内容";
  if (f.state === "ok") return `${w}已更新（${fmtAge(f.age)}）`;
  if (f.state === "stale") return `⚠ ${w}未更新（${fmtAge(f.age)}）`;
  if (f.state === "untracked") return "未纳入刷新";
  if (f.state === "loading") return "…";
  return `${w}无刷新记录`;
}
function ratingBadge(r) {
  const t = mdPlain(r);
  if (!t) return "";
  if (/^B/i.test(t)) return `<span class="badge kb-rate-b" title="B 级：可用（原文口径）">${esc(t)}</span>`;
  if (/^C/i.test(t)) return `<span class="badge kb-rate-c" title="C 级：偏弱或无效（原文口径）">${esc(t)}</span>`;
  return `<span class="badge">${esc(t)}</span>`;
}
/* 百分比正负上色：A 股惯例——正为红、负为绿 */
/* 注：旧的 pctCls()（只按正负号上色）已删除——它会把「平均亏损 1.82%」（上游以正数幅度书写）
   染成"上涨红"。现统一由 pctCellCls(列名, 文本) 按列语义判定。 */

/* 数值列判定（数据驱动，不按列名硬编码）：某列 ≥60% 的非空单元格是「可比较的数字」才算数值列。
   之所以不用列名匹配：同一份知识库里列名写法不统一（胜率/年化收益/平均亏损/最大回撤…），
   而列名一旦改动（上游改版）就会静默失效；按数据判定对上游改版更耐久。 */
/* ============ 缺失值统一渲染（2026-10-12 整改 A6：缺失 ≠ 0 ≠ 无）============
   背景：行情/资金流抓不到时，后端现在会返回 null（见 server.py 的 _json_safe：NaN/Inf → null）。
   但前端过去各家写法不一——有的 `?? "—"`、有的直接 `|| 0`、有的留空白。留空白最坏：
   使用者分不清"是 0 还是没抓到"。这里给一个唯一出口。

   约定：
     · renderMissing(reason)  → HTML 片段：灰阶 "—" + 悬停说明原因
     · missText(reason)       → 纯文本 "—"（用于 title/aria 等属性）
     · numOr(v, reason, fmt)   → 数值安全渲染：null/undefined/NaN/空串 一律走 renderMissing
   注意：**不要**用 `?? 0` 表达"没抓到"——0 是有意义的取值（如净流入 0 亿）。
   ==================================================================== */
function missText(reason) { return "—"; }
function renderMissing(reason) {
  const why = reason || "该数据本次未抓取到（非 0，也不是空值）";
  return `<span class="miss" title="${escAttr(why)}">—</span>`;
}
function isMissing(v) {
  return v === null || v === undefined || v === "" ||
    (typeof v === "number" && !isFinite(v));
}
/** 数值安全渲染：缺失 → "—"+原因；否则用 fmt 回调格式化（fmt 可省略，默认原样 esc）。 */
function numOr(v, reason, fmt) {
  if (isMissing(v)) return renderMissing(reason);
  const t = fmt ? fmt(v) : String(v);
  return isMissing(t) ? renderMissing(reason) : esc(t);
}

/* ================= 四要素统一标签（2026-10-13 整改 A1：全局数据可信度标签系统） =================
   需求原文（《股票论坛可视化查看器-顶配改造需求》A1，P0 重中之重）：
     · 全站所有数据单元统一「四要素标签」：来源 / 时点 / 口径 / 可信度，悬停或 ⓘ 点击可见；
     · 可信度三档：官方/交易所级、社区统计级、模型推断级，分别用实心圆 / 半圆 / 空心圆标识；
     · 缺失处理：无来源字段 → 整块置灰并标「来源缺失」；时点无法确认 → 标「时点未知」，
       不允许冒充最新；口径不明确 → 不展示数值、只展示「口径待明确」；
       模型推断级 → 必须同时展示该模型的验证命中率（无验证报告则标「未验证」）。

   对外接口（验收脚本 E1-4 命中关键词：trustBadge / metaBadge / 四要素 / dataUnitBadge / 可信度标签）：
     · dataUnitMeta(meta)   → 四要素标签组（推荐入口，含 ⓘ 悬停/点击明细）
     · dataUnitBadge(meta)  → dataUnitMeta 别名（全站调用统一走它）
     · metaBadge(kind, v)   → 单要素徽章：kind = source|timepoint|caliber
     · trustBadge(level,note) → 可信度徽章：level = official|community|model，note 为该档补充说明
     · trustDot(level)      → 三档圆点（实心 / 半圆 / 空心）

   用法示例：
     dataUnitMeta({
       source:    "lhb_stocks（交易所盘后披露）",
       timepoint: "2026-10-13 15:00 收盘",
       caliber:   "单位：亿元 · 口径：龙虎榜净买入",
       trust:     "community",
       trustNote: "净买入为交易所披露，聚合结论为统计规则判断",
     })
   任一字段省略时自动渲染缺失态，不编造来源、不冒充最新、不硬凑口径。
   ==================================================================== */
const TRUST_META = {
  official:  { label: "官方/交易所级", cls: "du-t-official",  note: "数据来自官方公告、交易所披露或权威接口，可信度最高" },
  community: { label: "社区统计级",    cls: "du-t-community", note: "数据来自论坛/社区抓取与统计聚合，需结合原文核对" },
  model:     { label: "模型推断级",    cls: "du-t-model",     note: "数据由模型/规则推断产生，非直接观测，必须附验证命中率" },
};
const META_KIND = {
  source:    { label: "来源", miss: "来源缺失" },
  timepoint: { label: "时点", miss: "时点未知" },
  caliber:   { label: "口径", miss: "口径待明确" },
};
/* 三档可信度圆点：官方=实心圆、社区=半圆、模型=空心圆（A1 硬性要求，勿换成其它图形） */
function trustDot(level) {
  const t = TRUST_META[level] || TRUST_META.community;
  return `<span class="trust-dot ${escAttr(t.cls)}" aria-hidden="true"></span>`;
}
/* 单枚「可信度」徽章：圆点 + 档位名；model 级未给验证命中率时按总原则 5 标「未验证」 */
function trustBadge(level, note) {
  const t = TRUST_META[level];
  if (!t) return renderMissing("可信度等级未知（应为 official/community/model）");
  let tail = "";
  let why = `${t.label}：${t.note}`;
  if (note) { tail = ` · ${note}`; why += tail; }
  else if (level === "model") {
    tail = " · 未验证";
    why += " · 未验证（无验证报告，按总原则 5 一律标注，不假装已回测）";
  }
  return `<span class="du-meta du-trust ${escAttr(t.cls)}" title="${escAttr(why)}">`
    + `${trustDot(level)}${esc(t.label)}${tail ? `<b class="du-trust-note">${esc(tail)}</b>` : ""}</span>`;
}
/* 单枚「要素」徽章：来源/时点/口径；value 缺失时渲染缺失态（置灰 + 虚线下划线 + 悬停原因） */
function metaBadge(kind, value) {
  const k = META_KIND[kind];
  if (!k) return "";
  const v = String(value == null ? "" : value).trim();
  if (!v) {
    return `<span class="du-meta du-missing" title="${escAttr(k.miss + "：该数据单元本次未提供" + k.label + "信息，不能当作存在或冒充最新")}">`
      + `${esc(k.label)}·${esc(k.miss)}</span>`;
  }
  return `<span class="du-meta du-${escAttr(kind)}" title="${escAttr(k.label + "：" + v)}">${esc(k.label)} <b>${esc(v)}</b></span>`;
}
/* 四要素统一标签组：来源 / 时点 / 口径 / 可信度 一行排布，悬停看明细，ⓘ 点击展开；
   任一要素缺失 → 整组按 A1 规则降权（置灰降透明度），缺哪项就明示哪项，不静默。 */
function dataUnitMeta(meta) {
  const m = meta || {};
  const trust = TRUST_META[m.trust]
    ? trustBadge(m.trust, m.trustNote || "")
    : `<span class="du-meta du-missing" title="${escAttr("可信度未知：该数据单元未提供可信度档位（official/community/model），不能当作已验证")}">可信度·可信度未知</span>`;
  const parts = [
    metaBadge("source", m.source),
    metaBadge("timepoint", m.timepoint),
    metaBadge("caliber", m.caliber),
    trust,
  ].filter(Boolean);
  if (!parts.length) return "";
  const missing = !m.source || !m.timepoint || !m.caliber || !m.trust;
  const lines = [
    m.source ? `来源：${m.source}` : "来源：缺失（无来源字段的数据一律按「来源缺失」整块置灰）",
    m.timepoint ? `时点：${m.timepoint}` : "时点：未知（不允许冒充最新）",
    m.caliber ? `口径：${m.caliber}` : "口径：待明确（口径不明确即不展示数值，只展示「口径待明确」）",
    `可信度：${(TRUST_META[m.trust] || {}).label || "未知"}` + (m.trust === "model" ? (m.trustNote ? `，验证命中率 ${m.trustNote}` : "，未验证") : ""),
  ];
  const info = lines.join("\n");
  return `<span class="du-meta-group${missing ? " du-unit-missing" : ""}"`
    + ` data-du-meta="${escAttr(info)}" title="${escAttr(info)}">${parts.join("")}`
    + `<button type="button" class="du-info" onclick="toggleDuMeta(this)" title="点击查看四要素明细">ⓘ</button></span>`;
}
function dataUnitBadge(meta) { return dataUnitMeta(meta); }
/* ⓘ 点击：轻量 toast 展示该数据单元的四要素明细（悬停 title 已可读；toast 复用 D2 现成能力，
   避免在 span 内插 block 元素破坏文档结构） */
function toggleDuMeta(btn) {
  if (!btn) return;
  const g = btn.closest(".du-meta-group");
  if (!g) return;
  showToast((g.getAttribute("data-du-meta") || "四要素明细：来源 / 时点 / 口径 / 可信度").replace(/\n/g, "；"));
}

/* ============ 交易日横排（2026-10-12 进哥：不能无限铺开，会拐行）============
   龙虎榜 / 板块动向的「交易日」原来是 (d.dates || []).map(...) 全量铺开 ——
   库内交易日一多就换行，把正文挤下去。
   改为：**只渲染最近 DAY_CHIP_LIMIT 个 chip**；其余走「查询」入口 ——
   一个日期选择器，可直接跳到库内任意交易日（超出库内范围的给提示，不静默失败）。
   attrName 用各面板自己的数据属性（data-lhb-date / data-lhs-date），点击绑定沿用原逻辑。
   ==================================================================== */
const DAY_CHIP_LIMIT = 10;
/**
 * 通用「日期/期次横排」：只渲染最近 DAY_CHIP_LIMIT 项 + 一个日期查询入口。
 * @param items    可迭代的项（默认每项就是日期字符串；对象数组时用 opts.keyOf 取键）
 * @param curValue 当前选中值
 * @param attrName 点击绑定用的数据属性名（如 "data-lhb-date"）
 * @param opts     { keyOf(x)->键, renderOf(x)->chip 内 HTML, title }
 *
 * ⚠️ 坑（2026-10-12 踩过）：选项名**不能叫 `valueOf`**——`Object.prototype.valueOf` 永远存在且 truthy，
 * 于是 `o.valueOf || 默认值` 永远取到原型方法，以 this=undefined 调用会抛
 * "Cannot convert undefined or null to object"，整个面板渲染失败、变空白。
 * 此处另用 typeof === "function" 双保险。
 */
function dayChips(items, curValue, attrName, opts) {
  const o = opts || {};
  const keyOf = typeof o.keyOf === "function" ? o.keyOf : ((x) => x);
  const renderOf = typeof o.renderOf === "function" ? o.renderOf : ((x) => esc(x));
  const all = toArr(items).filter((x) => keyOf(x));
  if (!all.length) return "";
  const shown = all.slice(0, DAY_CHIP_LIMIT);
  const chip = (x) => `<span class="chip-s" role="button" ${attrName}="${escAttr(keyOf(x))}" `
    + `style="cursor:pointer;${keyOf(x) === curValue ? "background:var(--accent,#0E7490);color:#fff;" : ""}">`
    + `${renderOf(x)}</span>`;
  const rest = all.length - shown.length;
  if (rest <= 0) return `<span class="day-strip">${shown.map(chip).join(" ")}</span>`;
  const vals = all.map(keyOf);
  const newest = vals[0];
  const oldest = vals[vals.length - 1];
  // 查询框**前置**（2026-10-12 进哥建议：设在「交易日」这边做查找），
  // chips 放进单行可横向滚动的 .day-strip —— 保证**永不换行**（进哥："不能无限排"）。
  return `<input type="date" class="day-jump" data-days="${escAttr(vals.join(","))}"`
    + ` value="${escAttr(curValue || "")}" min="${escAttr(oldest)}" max="${escAttr(newest)}"`
    + ` title="${escAttr(o.title || ("查询任意日期（可选 " + newest + " ~ " + oldest + "，共 " + all.length + " 个）"))}">`
    + ` <span class="day-more" title="只显示最近 ${DAY_CHIP_LIMIT} 项；更早的用左侧日期框查询">`
    + `(+${rest})</span>`
    + ` <span class="day-strip">${shown.map(chip).join(" ")}</span>`;
}

function numericCols(cols, rows) {
  return toArr(cols).map((_, ci) => {
    const vals = toArr(rows).map((r) => mdPlain(toArr(r)[ci])).filter((x) => x !== "");
    if (!vals.length) return false;
    const n = vals.filter((x) => /^-?[\d,]+(\.\d+)?%?$/.test(x.replace(/\s/g, ""))).length;
    return n / vals.length >= 0.6;
  });
}
/* 百分比单元格配色（2026-09-23 修正语义，原先只按符号上色会把亏损染成"上涨红"）：
   - 中性列（胜率/样本数/次数/持仓数…）：只读量，不上色——把 60% 胜率染成红是误导；
   - 负向列（平均亏损/最大回撤/跌幅/成本…）：一律用"跌色"绿。这类值表达损失，
     且上游常以**正数幅度**书写（如「平均亏损 1.82%」），按符号上色正好读反；
   - 其余（年化收益/夏普…）：按符号——正=红、负=绿（A 股惯例）。 */
const PCT_NEUTRAL_COL = /胜率|样本|次数|条数|数量|天数|持仓|占比|评级|排名|名称|博主/;
/* 注意：负向列只列「亏损」不写裸「亏」——否则「盈亏比」会被误判成负向列 */
const PCT_NEG_COL = /亏损|回撤|跌幅|风险|失败|成本/;
function pctCellCls(colName, txt) {
  const t = String(txt || "").trim();
  if (!/^-?[\d,]+(\.\d+)?%$/.test(t)) return "";
  const name = String(colName || "");
  if (PCT_NEUTRAL_COL.test(name)) return "";
  const v = parseFloat(t.replace(/[,%]/g, ""));
  if (!isFinite(v) || v === 0) return "";
  if (PCT_NEG_COL.test(name)) return "kb-pct-dn";
  return v > 0 ? "kb-pct-up" : "kb-pct-dn";
}
/* 宽表提示：只在该表**确实**溢出滚动容器时才显示。
   .kb-scroll 本身可横向滚动，但没有任何提示——实测 1100px 下回测表右侧 3 列完全看不到，
   用户会以为数据就这么多。渲染后按真实宽度判定，窄表不显示多余的提示。 */
function markWideTables(scope) {
  (scope || document).querySelectorAll(".kb-bt, .kb-sec, .kb-block").forEach((sec) => {
    const sc = sec.querySelector(".kb-scroll");
    const tip = sec.querySelector(".kb-wide-tip");
    if (!sc || !tip) return;
    tip.hidden = sc.scrollWidth <= sc.clientWidth + 4;
  });
}
window.addEventListener("resize", () => markWideTables(document));
function mdTable(t) {
  const cols = toArr(t && t.columns);
  const rows = toArr(t && t.rows);
  if (!cols.length || !rows.length) return "";
  const isNum = numericCols(cols, rows);   // 与回测表同一套判据，避免同页两种数字对齐风格
  return `<div class="kb-scroll"><table class="kb-table"><thead><tr>` +
    cols.map((c, ci) => `<th${isNum[ci] ? ' class="num"' : ""}>${esc(c)}</th>`).join("") + `</tr></thead><tbody>` +
    rows.map((r) => `<tr>` + toArr(r).map((c, ci) => {
      const txt = mdPlain(c);
      // A6：数值列的空单元格 → 统一"缺失"标记（不再留空白，避免被当成 0）
      if (isNum[ci] && (txt === "" || txt === "-" || txt === "—" || txt === "N/A")) {
        return `<td class="num">${renderMissing("该行此列在原表中为空（未提供或未抓取）")}</td>`;
      }
      return `<td${isNum[ci] ? ' class="num"' : ""}>${esc(txt)}</td>`;
    }).join("") + `</tr>`).join("") +
    `</tbody></table></div>`;
}
/* 博主完整档案抽屉：精读笔记 / 视频笔记 / 来源三块分别成段，不混在一起 */
function openBlogger(schoolName, bloggerName, anchorEl) {
  const d = KB.doc;
  const school = toArr(d && d.schools).find((s) => s.name === schoolName);
  const b = toArr(school && school.bloggers).find((x) => x.name === bloggerName);
  if (!b) { setHint("未找到该博主档案：" + bloggerName); return; }
  const seg = (title, recs) => {
    const rs = toArr(recs);
    if (!rs.length) return "";
    return `<div class="op-sec"><div class="op-sec-h">${esc(title)}</div>` +
      rs.map((r) => `<div class="kb-note-block"><div class="kb-note-h">${esc(r.heading)}</div>` +
        `<ul class="kb-ul">${toArr(r.items).map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>`).join("") +
      `</div>`;
  };
  const srcs = toArr(b.sources);
  const srcHtml = srcs.length
    ? `<div class="op-sec"><div class="op-sec-h">来源与核验（${srcs.length} 条）</div>` +
      `<ul class="kb-ul kb-ul-src">${srcs.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>`
    : "";
  // 16 位博主没有精读字段（其公开资料只有视频）——不写一句说明会让人以为程序漏抓了
  const notesHtml = toArr(b.phase_notes).length
    ? seg("精读笔记", b.phase_notes)
    : `<div class="op-sec"><div class="op-sec-h">精读笔记</div>` +
      `<div class="kb-note">该博主在上游知识库中<b>没有原帖精读字段</b>，其方法整理全部来自视频笔记（见下）。</div></div>`;
  openDrawer(bloggerName, `
    <div class="kb-bd-head">
      <span class="badge ch-A">${esc(schoolName)}</span>
      ${b.has_video ? `<span class="chip-s">含视频笔记</span>` : ""}
    </div>
    ${b.style ? `<div class="kb-note">${esc(b.style)}</div>` : ""}
    ${notesHtml}
    ${seg("视频笔记", b.phase_video)}
    ${srcHtml}
    <div class="kb-note">以上内容为上游知识库的原文整理，程序仅做结构化展示，未做改写或补充。</div>`, anchorEl);
}

/* 「学习中心 → 学习轨道管理」：各知识库来源的吸取状态（复用「最后更新」时效口径） */
function renderKbTracks() {
  const box = ensurePanel("learn_tracks");
  if (!box) return;
  // 左栏子类的重绘统一由 selectNav / loadKbTracks 负责，此处是纯渲染，不再重复重建导航
  const d = KB.tracks;
  if (!d) { box.innerHTML = `<div class="empty">正在读取知识轨道…</div>`; return; }
  const rows = toArr(d.docs).map((t) => {
    const f = freshnessOf([t.doc_key]);
    const st = t.status === "never"
      ? `<span class="chip-s chip-fresh chip-untracked">尚未吸取</span>`
      : `<span class="chip-s chip-fresh ${freshCls(f.state)}" title="${escAttr(freshTitle(f))}">${freshLine(f, "学习内容")}</span>`;
    const gk = t.group || "learn";
    return `<tr>
      <td><b>${esc(t.track || t.title || t.doc_key)}</b><div class="src-line">${esc(t.doc_key)}</div></td>
      <td><span class="chip-s" title="由 kb_sources.yaml 的 group 决定；在横条内的排位由 order 决定（数值小的靠前）。两者都无需重建知识库">${esc(DYN_CATS[gk] || gk)}</span></td>
      <td>${esc(t.version || "—")} ${t.changed ? `<span class="chip-s chip-fresh chip-new" title="相对上一版内容已变化">有更新</span>` : ""}</td>
      <td>${t.node_count ?? 0}</td>
      <td>${esc(t.ingested_at || "—")}${t.prev_ingested_at ? `<div class="src-line">上一版 ${esc(t.prev_ingested_at)}</div>` : ""}</td>
      <td>${st}</td>
      <td class="kb-path">${t.source_exists ? `<span class="free-tag">文件存在</span>` : `<span class="lock-tag" title="配置里声明的路径/通配都找不到文件">文件缺失</span>`}
        <div class="src-line">${esc(t.source_path || "—")}</div></td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <div class="hint" style="margin-bottom:10px">一条知识轨道 = 一个横向子板块。来源在
      <code>config/kb_sources.yaml</code> 声明；上游<b>每天覆盖写出</b>该 md 文件，
      程序按<b>内容哈希</b>判断要不要重吸取——内容没变就跳过，不做无意义重建。新增轨道只需在配置里加一条，
      横向导航会自动多出一项，无需改代码；<b>排第几由 <code>order</code> 决定</b>
      （数值小的靠前，缺省 0），改完刷新页面即生效。</div>
    <div class="toolbar">
      <span>吸取周期：${Math.round((d.interval_seconds || 86400) / 3600)} 小时 ｜ 超时阈值：${Math.round((d.stale_seconds || 172800) / 3600)} 小时</span>
      <span class="rb-spacer"></span>
      <button class="btn sm primary" onclick="kbReload()">立即吸取全部</button>
    </div>
    <table><thead><tr><th>知识轨道</th><th>所在板块</th><th>版本</th><th class="num">知识点</th><th>吸取时间</th><th>最后更新</th><th>来源文件</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">未配置任何知识轨道</td></tr>`}</tbody></table>
    <div class="kb-note">「文件缺失」表示配置里的路径与通配都找不到文件——通常是上游换了导出目录，改
      <code>kb_sources.yaml</code> 的 <code>path_glob</code> 即可。</div>`;
  setTimeout(applyFreshness, 0);
}

/* ================= 抓取管道：数据源 / 原始存档 / 抓取与登录记录 =================
   这三项把原先散落在各处（海外面板里的「数据源接入状态」、顶栏的「登录检测」、
   refresh_status 里的刷新记录）的运维信息归拢成一处，日常不干扰研判，需要排查时集中可查。

   2026-09-26：用户判定这三项「看盘时没有意义」，已从左侧导航撤下（不再出现在横向子板块条上）。
   注意这只是**去掉入口**，不是停掉功能——原始页面存档（raw_pages）、逐来源刷新留痕
   （refresh_status）、登录态检测（/api/login_check）全部照常执行与落库，只是不再占用界面；
   顶栏的「登录检测」按钮仍可用，结果通过顶部提示条反馈（明细仍写进后端）。
   要恢复界面只需把三项加回 NAV 的 learn.items。 */
function renderSourcesPanel(summary) {
  const box = ensurePanel("sources");
  if (!box) return;
  const s = summary || SUMMARY || {};
  const sites = toArr(s.sites);
  const rs = RS.sources || {};
  const trk = toArr(RS.tracked);
  const rows = Object.keys(rs).map((k) => {
    const s = rs[k];
    const tracked = !trk.length || trk.indexOf(k) >= 0;
    const f = freshnessOf([k]);
    return `<tr data-src="${escAttr(k)}">
      <td><b>${esc(s.display_name || k)}</b><div class="src-line">${esc(k)}</div></td>
      <td>${tracked ? `<span class="chip-s">已纳入</span>` : `<span class="chip-s chip-fresh chip-untracked">未纳入</span>`}</td>
      <td>${esc(s.last_ok_at || "—")}</td>
      <td>${esc(s.last_attempt_at || "—")}</td>
      <td>${s.status === "ok" ? `<span class="badge bull">正常</span>` : (s.status === "error" ? `<span class="badge bear" title="${escAttr(s.error || "")}">失败</span>` : `<span class="chip-s">未刷新</span>`)}</td>
      <td class="num">${s.item_count ?? 0}</td>
      <td>${refCell([k])}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <div class="hint" style="margin-bottom:10px">这里回答一个问题：<b>数据从哪来、来没来</b>。
      抓取配置声明的站点、各来源最近一次成功/尝试刷新的时刻与结果都在此；海外数据源（需联网/VPN/API Key）单列在下方。</div>
    <div class="cards" style="margin-bottom:14px">
      <div class="card"><div class="t">抓取配置站点</div><div class="v">${sites.length}</div><div class="s">${esc(sites.join("、") || "—")}</div></div>
      <div class="card"><div class="t">已纳入刷新来源</div><div class="v">${trk.length}</div><div class="s">含国内站点与知识库来源</div></div>
      <div class="card"><div class="t">有刷新记录的来源</div><div class="v">${Object.keys(rs).length}</div><div class="s">成功与失败都留痕</div></div>
    </div>
    <table><thead><tr><th>来源</th><th>纳入刷新</th><th>最近成功刷新</th><th>最近尝试</th><th>状态</th><th class="num">条目数</th><th>最后更新</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">暂无刷新记录。点「抓取并更新」后此处会出现各来源的刷新时刻。</td></tr>`}</tbody></table>
    <h3 style="margin:18px 0 10px">海外数据源接入状态</h3>
    <div class="ov-src-grid" id="ov-sources-srcpanel"></div>`;
  // 海外数据源状态缓存复用：面板每轮重绘（SUMMARY 更新时）不必再打一次接口
  if (OV_SOURCES) renderOverseasSources(OV_SOURCES, "#ov-sources-srcpanel");
  else if (!OV_SOURCES_LOADING) {
    OV_SOURCES_LOADING = true;
    api("/api/overseas_sources")
      .then((d) => { OV_SOURCES = d; renderOverseasSources(d, "#ov-sources-srcpanel"); })
      .catch((e) => reportLoadError("海外数据源状态", e))
      .finally(() => { OV_SOURCES_LOADING = false; });
  }
  setTimeout(applyFreshness, 0);
}
let OV_SOURCES = null, OV_SOURCES_LOADING = false;
function renderArchivePanel() {
  const box = ensurePanel("archive");
  if (!box) return;
  box.innerHTML = `<div class="empty">正在读取原始存档…</div>`;
  api("/api/raw_pages?limit=80").then((d) => {
    const pages = toArr(d && d.pages);
    const bySite = Object.keys(d && d.by_site || {}).map((k) =>
      `<span class="chip-s">${esc(siteLabel(k))} ${d.by_site[k]}</span>`).join(" ");
    const rows = pages.map((p) => `<tr>
      <td>${siteBadge(p.site)}</td>
      <td class="kb-path">${p.url ? `<a href="${escAttr(p.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${esc(String(p.url).slice(0, 90))}↗</a>` : "—"}</td>
      <td>${esc(p.fetched_at || "—")}</td>
      <td class="num">${p.http_status ?? "—"}</td>
      <td class="num">${fmtBytes(p.bytes)}</td>
      <td>${refCell([p.site])}</td>
    </tr>`).join("");
    box.innerHTML = `
      <div class="hint" style="margin-bottom:10px">抓取回来的<b>原始页面存档</b>：只展示存档元信息（来源/地址/抓取时刻/体积），
        不展示 HTML 正文。存档是后续任何解析结果的追溯依据——解析口径变了，可以回查当时抓到的是什么。</div>
      <div class="cards" style="margin-bottom:14px">
        <div class="card"><div class="t">存档页面总数</div><div class="v">${d.total ?? 0}</div><div class="s">本次展示最近 ${d.shown ?? 0} 条</div></div>
        <div class="card"><div class="t">来源分布</div><div class="s" style="margin-top:6px">${bySite || "—"}</div></div>
      </div>
      <table><thead><tr><th>来源</th><th>页面地址</th><th>抓取时刻</th><th class="num">HTTP</th><th class="num">体积</th><th>最后更新</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="6">暂无原始存档。点「抓取并更新」后此处会出现抓取到的页面。</td></tr>`}</tbody></table>`;
    setTimeout(applyFreshness, 0);
  }).catch((e) => { box.innerHTML = `<div class="empty">原始存档读取失败：${esc(apiErrText(e))}</div>`; });
}
function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / 1024 / 1024).toFixed(2) + " MB";
}
function renderFetchLogPanel(summary) {
  const box = ensurePanel("fetchlog");
  if (!box) return;
  const s = summary || SUMMARY || {};
  const lg = s.login_status;
  const lgHtml = (lg && typeof lg === "object" && !Array.isArray(lg) && Object.keys(lg).length)
    ? `<table><thead><tr><th>站点</th><th>登录态</th><th>说明</th></tr></thead><tbody>` +
      Object.keys(lg).map((k) => {
        const v = lg[k] || {};
        const ok = /ok|已登录|true/i.test(String(v.status ?? v.state ?? v));
        return `<tr><td>${siteBadge(k)}</td><td>${ok ? `<span class="badge bull">已登录</span>` : `<span class="badge bear">未登录</span>`}</td>` +
          `<td class="kb-path">${esc(String(v.note || v.message || v.detail || JSON.stringify(v))).slice(0, 160)}</td></tr>`;
      }).join("") + `</tbody></table>`
    : `<div class="empty">尚无登录检测记录。点下方「登录检测」联网核对各站点登录态。</div>`;
  const rsRows = Object.keys(RS.sources || {}).map((k) => {
    const x = RS.sources[k];
    return `<tr><td>${esc(x.display_name || k)}</td><td>${esc(x.last_attempt_at || "—")}</td>` +
      `<td>${x.status === "ok" ? `<span class="badge bull">成功</span>` : (x.status === "error" ? `<span class="badge bear">失败</span>` : `<span class="chip-s">未刷新</span>`)}</td>` +
      `<td class="kb-path" title="${escAttr(x.error || "")}">${esc((x.error || "—").slice(0, 120))}</td></tr>`;
  }).join("");
  box.innerHTML = `
    <div class="hint" style="margin-bottom:10px">抓取与登录的<b>执行记录</b>：最近一次抓取动作的时刻、逐来源的成功/失败留痕与失败原因、各站点登录态。
      失败也留痕是本页的关键——<b>一次失败不会抹掉「上次成功于何时」</b>，因此这里既能看到「什么时候试过」，也能看到「上次成功是什么时候」。</div>
    <div class="cards" style="margin-bottom:14px">
      <div class="card"><div class="t">最近抓取动作</div><div class="v" style="font-size:15px">${esc(s.last_fetch_at || "—")}</div><div class="s">写入业务数据时刻：${esc(s.last_ingest ?? "—")}</div></div>
      <div class="card"><div class="t">整轮刷新状态</div><div class="v" style="font-size:16px">${esc(RS.status === "ok" ? "正常" : RS.status === "error" ? "有失败" : "未刷新")}</div><div class="s">${esc(RS.error ? String(RS.error).slice(0, 80) : "任一来源失败即记为有失败")}</div></div>
      <div class="card"><div class="t">自动刷新</div><div class="v" style="font-size:16px">${$("#realtime") && $("#realtime").checked ? "已开启" : "已关闭"}</div><div class="s">周期 ${Math.round((RS.interval || 300) / 60)} 分钟</div></div>
    </div>
    <h3 style="margin:6px 0 10px">逐来源刷新记录</h3>
    <table><thead><tr><th>来源</th><th>最近尝试</th><th>结果</th><th>失败原因</th></tr></thead>
      <tbody>${rsRows || `<tr><td colspan="4">暂无刷新记录</td></tr>`}</tbody></table>
    <h3 style="margin:18px 0 10px">登录态</h3>
    <div class="toolbar" style="margin-bottom:10px">
      <button class="btn sm" onclick="loginCheck()">登录检测</button>
      <span class="chip-s">登录检测需联网，会逐个站点核对登录态</span>
    </div>
    ${lgHtml}`;
}
function loginCheck() {
  setHint("正在检测各站点登录态…");
  api("/api/login_check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then(() => api("/api/summary"))
    .then((s) => {
      SUMMARY.data = s;
      renderFetchLogPanel(s);              // 明细照旧写进面板（已撤出导航，作留档）
      setHint("登录检测完成 —— " + loginStateText(s));
    })
    .catch((e) => setHint("登录检测失败：" + apiErrText(e)));
}
/* 把 /api/summary 的 login_status 压成一句可读的话，供顶栏提示条用。
   2026-09-26 新增：原「抓取与登录记录」页已从导航撤下，明细没有可见入口了，
   而顶栏「登录检测」按钮仍在 —— 若点完只回一句"完成"，用户等于什么都没看到。
   故这里把逐站点的已登录/未登录直接顶到提示条上；完整明细仍在后端与（隐藏的）面板里。 */
function loginStateText(s) {
  const lg = (s || {}).login_status;
  if (!lg || typeof lg !== "object" || Array.isArray(lg)) return "各站点登录态已记录。";
  const ks = Object.keys(lg);
  if (!ks.length) return "各站点登录态已记录。";
  return ks.map((k) => {
    const v = lg[k] || {};
    const ok = /ok|已登录|true/i.test(String(v.status ?? v.state ?? v));
    return `${siteLabel(k)}：${ok ? "已登录" : "未登录"}`;
  }).join(" · ");
}


/* 实时刷新（周期由后端 /api/refresh_status 统一给出，默认 5 分钟）：
   刷新状态由后台记录，前端只负责触发抓取并展示倒计时。 */
function armRealtime() {
  const ms = RS.intervalMs || 300000;
  rtNextAt = Date.now() + ms;
  rtTimer = setTimeout(() => {
    // 2026-09-25 优化：自动刷新只抓轻量两站（淘股吧 + 东方财富），
    // 不碰雪球（需真实浏览器过 WAF，最慢）。雪球留给手动「刷新全部」触发，
    // 避免每 5 分钟自动拉一次导致界面周期性卡顿。
    api("/api/fetch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sites: ["taoguba", "eastmoney"] }),
    })
      .then(() => loadAll())
      .catch((err) => setHint("实时刷新抓取失败：" + apiErrText(err)))
      .finally(() => {
        if ($("#realtime").checked) armRealtime();
        else { rtNextAt = null; renderRefreshBar(); }
      });
  }, ms);
}
$("#realtime").onchange = (e) => {
  if (e.target.checked) {
    if (rtTimer) clearTimeout(rtTimer);
    armRealtime();
    setHint("实时刷新已开启（每 5 分钟抓取一次；刷新状态由后台记录）。");
  } else {
    if (rtTimer) clearTimeout(rtTimer);
    rtTimer = null; rtNextAt = null;
    setHint("实时刷新已关闭。");
  }
  renderRefreshBar();
};

/* 盘中全页轮询开关：localStorage 持久化，刷新页面保持 */
const pagePollEl = $("#pagePoll");
if (pagePollEl) {
  let saved = null;
  try { saved = localStorage.getItem("pagePollEnabled"); } catch (e) { /* 隐私模式无 localStorage */ }
  PAGE_POLL.enabled = saved === "1";
  pagePollEl.checked = PAGE_POLL.enabled;
  if (PAGE_POLL.enabled && isTradingNow() && !document.hidden) startPagePoll();
  else renderPagePollState();
  pagePollEl.onchange = (e) => {
    PAGE_POLL.enabled = e.target.checked;
    try { localStorage.setItem("pagePollEnabled", PAGE_POLL.enabled ? "1" : "0"); } catch (err) {}
    if (PAGE_POLL.enabled) {
      if (isTradingNow() && !document.hidden) startPagePoll();
      else renderPagePollState();   // 非交易时段先记下状态，进入交易时段由每秒 tick 自动拉起
      setHint("盘中轮询已开启：交易时段内每 " + Math.round(PAGE_POLL.periodMs / 1000) + " 秒自动重刷当前标签页（非交易时段显示待命）。");
    } else {
      stopPagePoll();
      setHint("盘中轮询已关闭。");
    }
  };
}

/* 状态牌点击 = 开关（与 #pagePoll 复选框等效，2026-09-29 补：之前只显示不可点，被误认为找不到开关） */
const pagePollStateEl = $("#pagePollState");
if (pagePollStateEl && pagePollEl) {
  pagePollStateEl.style.cursor = "pointer";
  pagePollStateEl.onclick = () => {
    pagePollEl.checked = !pagePollEl.checked;
    pagePollEl.dispatchEvent(new Event("change"));
  };
}
$("#btnRefreshNow").onclick = refreshNow;
const schedAutoEl = $("#schedAuto");
if (schedAutoEl) {
  schedAutoEl.onchange = (e) => {
    api("/api/scheduler/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked }),
    }).then(() => loadSchedulerStatus())
      .catch((err) => setHint("切换自动调度失败：" + apiErrText(err)));
  };
}

/* ---------- 每日预测 PRED 状态 + 四支柱页签定义（上移至此，TDZ 修复 2026-10-15） ----------
 * 必须在 initNav() 之前声明：深链接 #overview/prediction 时 initNav→selectNav→loadPrediction
 * 会同步访问 PRED.busy，若 PRED 仍在下方 const 声明处则命中 TDZ（Cannot access 'PRED' before
 * initialization）。故整体前置。
 */
// 2026-10-15 收敛：dates / scores / track / status / date / range 全部随旧引擎 UI 一并移除。
// 本页现由新引擎（四支柱）独占，只保留它真正用到的字段。
const PRED = { data: null, busy: false, ptab: "policy_funnel" };

// 四支柱页签短名（2026-10-15 王总：页签用方案本名，不带 A/B/C/D 字样）
const PILLAR_TABS = [
  { key: "policy_funnel", label: "政策漏斗" },
  { key: "t1_surf",       label: "竞价冲浪" },
  { key: "dip_revert",   label: "超跌修复" },
  { key: "resonance",    label: "共振风控" },
  // 2026-10-16 进哥要求：与「多源共振 → 自我提升」同理，但这里学的是**本引擎自己的实盘复盘**
  // （政策漏斗/竞价冲浪/超跌修复/共振风控 逐期逐支柱的命中率与超额），再用学出的权重给今日提名加权选票。
  { key: "self_learn",   label: "引擎自加权" },
];
function pillarShortName(pkey, pdata) {
  const t = PILLAR_TABS.find((x) => x.key === pkey);
  if (t) return t.label;
  return String((pdata && pdata.title) || pkey).replace(/^[A-D]\s*·\s*/, "");
}
/* 2026-10-16（进哥二次修正，推翻我上一版"锚点跳转"）：四个支柱按钮 = **子页签**，
   点一下**就地换页** —— 只显示该支柱那一页（它的名单 + **它自己的回测**），绝不滚动跳转。
   进哥原话："不是我点了哪一块儿你就给我移动到哪一块儿，是直接换一个页面，然后直接把这个页面
   里边儿的内容显示出来就行……你这个每一页都应该会有回测，底下。"
   所以换页时同时把回测清空重拉 —— loadBacktest() 会带上 &pillar=当前支柱。 */
function predPillarTab(k) {
  if (PRED.ptab === k) return;
  PRED.ptab = k;
  // 换页 = 换该支柱自己的回测（回到默认日期/周期，让后端按该支柱重新挑）
  BT.data = null; BT.err = null; BT.date = null; BT.period = 0; BT.periodLocked = false;
  renderPrediction();
}
/* 2026-10-16：钉住条要贴在顶栏正下方，而顶栏高度**不是常数** —— 它会随窗口宽度换行
   （实测 1560 宽时 84px；过去注释里写的 58px 是旧值）。按 58px 摆，钉住条会被顶栏压住 26px，
   再叠加顶栏 82% 半透明的背景，日期行就会"跟顶栏那行叠着透出来"（进哥截图里能看出来）。
   故：运行时量出真实高度写进 --topbar-h，CSS 侧统一引用它（含左栏导航）。 */
function syncTopbarH() {
  const tb = document.querySelector(".topbar");
  if (!tb) return;
  const h = Math.round(tb.getBoundingClientRect().height);
  if (h > 0) document.documentElement.style.setProperty("--topbar-h", h + "px");
}
if (typeof window !== "undefined") {
  window.addEventListener("resize", syncTopbarH);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", syncTopbarH);
  else syncTopbarH();
}

/* ---------- 多源共振 RES 状态 + 三支柱页签（2026-10-15，与 PRED 同款前置声明防 TDZ） ----------
 * 板块位置：今日总览 → 「多源共振」（紧跟「每日预测」之后）。
 * 数据源 /api/resonance（后端 resonance.py 三支柱产出，查看器只读）。
 * 三支柱 = 以票为中心 / 以战法为中心 / 以人为中心 —— 对应进哥拍板的「三套都做」。
 * 必须在此处声明（initNav → selectNav 会同步访问 RES），否则深链接进入时命中 TDZ。
 */
const RES = { data: null, evo: null, sp: null, busy: false, rtab: "cards" };
const RES_TABS = [
  { key: "cards",    label: "个股共振" },
  { key: "strategy", label: "战法体系" },
  { key: "source",   label: "信源画像" },
  // 2026-10-15 进哥追加：第 4 支柱 —— 从「汇聚别人的」到「长出自己的」。
  // 权重由数据学出、规则由数据生长、学完当天反哺共振卡。
  { key: "evolve",   label: "自我进化" },
];
function resTab(k) {
  RES.rtab = k;
  renderResonance();
}

/* ---------- 历史回测 BT（2026-10-15 新增，进哥要求） ----------
 * 板块位置：四支柱列表下方。数据源 /api/backtest（后端只读本地 K 线缓存，离线算）。
 *
 * 状态与 PRED 同样前置声明 —— 同一个 TDZ 坑（深链接进入时会在脚本尾部 const
 * 声明执行前就同步访问），故放这里。
 *
 * 交互设计（对应进哥 6 条要求）：
 *   ① 日期按钮 → 切预测日；② 周期按钮 → 切 T+1/3/5/10/20；
 *   ③ 左右分栏同一滚动容器内**逐行对齐**（左侧预测、右侧该标的 5 个周期矩阵），
 *      避免两个独立滚动框滚不同步；④ 下方总结区给命中率/平均收益/平均超额 + 判读。
 *   ⑤ 未到期周期显示「还差 N 日」、缺 K 线显示「—」，绝不填 0。
 *   ⑥ 周期切换纯前端（period_stats 一次带回全部周期），不重复请求。
 */
const BT = { source: "pillars", date: null, period: 0, periodLocked: false, data: null,
             loading: false, err: null, seq: 0 };
/* 本板块依赖后端接口 /api/backtest（2026-10-15 新增）。后端不重启该路由不存在，
   只刷新页面会报错——提示必须写成"可直接照做的一句话"，不能只甩一个 Failed to fetch。 */
const BT_RESTART_HINT = "若刚更新过界面：这是新板块，需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启，光刷新页面不够）。";

/* 每页回测只算当前支柱的票 —— 标题上必须写清楚是哪一根，否则看不出这串数字是谁的。
   优先用后端回的 pillar_label（唯一真源），拿不到再退回前端页签名。 */
function btPillarLabel(d) {
  if (d && d.pillar_label) return d.pillar_label;
  const key = (typeof PRED !== "undefined" && PRED.ptab) || "";
  const t = PILLAR_TABS.find((x) => x.key === key);
  return (t && t.label) || "四支柱";
}
function btPct(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const n = v * 100, d = digits === undefined ? 2 : digits;
  return (n >= 0 ? "+" : "") + n.toFixed(d) + "%";
}
function btSet(k, v) {
  if (k === "source") {
    BT.source = v; BT.date = null; BT.data = null; BT.err = null;
    if (!BT.periodLocked) BT.period = 0;
  } else if (k === "date") {
    BT.date = v; BT.data = null; BT.err = null;
    if (!BT.periodLocked) BT.period = 0;      // 未手动选过周期 → 让后端重挑该期已到期的最长周期
  } else if (k === "period") {
    BT.period = v; BT.periodLocked = true;
  }
  btPaint();
  if (k !== "period") loadBacktest();          // 周期切换本地重渲染即可，不再打接口
}
function btPaint() {
  const el = document.getElementById("bt-sec");
  if (el) el.innerHTML = renderBacktestSection();
  else renderPrediction();
}
function loadBacktest(isRetry) {
  const seq = ++BT.seq;
  BT.loading = true;
  BT.err = null;              // 重试时先清错误态，否则会一直停在失败画面上
  btPaint();
  // 2026-10-16：带上当前支柱 —— 每页底下的回测只算这一页这根支柱的票。
  // 注意：过滤在后端做、summary 仍由后端同一份 _summarize 算出，前端**不重算**任何收益/命中
  // （本项目红线：口径只有一个真源，前端重算必然漂移）。
  const qs = "/api/backtest?source=" + encodeURIComponent(BT.source) +
    "&pillar=" + encodeURIComponent((typeof PRED !== "undefined" && PRED.ptab) || "") +
    (BT.date ? "&date=" + encodeURIComponent(BT.date) : "") +
    (BT.period > 0 ? "&period=" + encodeURIComponent(BT.period) : "");
  api(qs).then((d) => {
    if (seq !== BT.seq) return;               // 防乱序：只认最新一次请求
    BT.loading = false;
    if (!d || d.ok === false) {
      BT.data = null;
      BT.err = esc((d && (d.note || d.error)) || "无回测数据") + "｜" + BT_RESTART_HINT;
    } else {
      BT.data = d; BT.err = null;
      BT.date = d.date; BT.source = d.source; BT.period = d.period;
    }
    btPaint();
  }).catch((e) => {
    if (seq !== BT.seq) return;
    BT.loading = false;
    // 首个请求可能撞上「后端刚起来、K 线缓存还没热」而被浏览器主动掐断
    // （ConnectionAbortedError 10053）→ 先自动重试一次，仍失败才报错。
    if (!isRetry) {
      setTimeout(() => { if (seq === BT.seq) loadBacktest(true); }, 1600);
      btPaint();
      return;
    }
    BT.err = esc(apiErrText(e)) + "｜" + BT_RESTART_HINT;
    btPaint();
  });
}

/* ===================== 信号战绩（2026-10-29）===========================
   干什么：把 signal_backtest 的百万样本回测结论印到界面上，每条信号一行：
     样本量 / 绝对胜率 / 相对基线的超额 / 夏普 / 裁决。

   为什么非要印"超额"：全市场前向 5 日基线胜率只有 46.12%（n≈129 万）。
   一个 48% 胜率的信号看着"过半"，实际跑输基线 —— 只印胜率就是在误导。
   所以这里固定把基线写在副标题里，超额单独一列、带正负号。

   裁决口径（与后端 kernel/evidence.py 完全一致，改一处必须改两处）：
     effective 有效 / neutral 中性 / adverse 反向 / insufficient 样本不足（<500）
   ==================================================================== */
const SIGEV = { data: null, loading: false, err: "" };

function loadSignalEvidence() {
  if (SIGEV.loading) return;
  SIGEV.loading = true;
  api("/api/signal/evidence?fwd=5").then((d) => {
    SIGEV.data = d || {};
    SIGEV.loading = false;
    const el = $("#sig-ev-body");
    if (el) el.innerHTML = renderSigEvidenceBody();
  }).catch((e) => {
    SIGEV.loading = false;
    SIGEV.err = String((e && e.message) || e || "加载失败");
    const el = $("#sig-ev-body");
    if (el) el.innerHTML = renderSigEvidenceBody();
  });
}

function renderSigEvidenceSection() {
  return `<div class="bt-sec">
    <div class="bt-head">
      <div class="bt-title">信号战绩<span class="bt-sub">全市场 250 日回测 · 超额 = 信号胜率 − 同期全样本基线</span></div>
    </div>
    <div id="sig-ev-body">${renderSigEvidenceBody()}</div>
  </div>`;
}

function renderSigEvidenceBody() {
  const d = SIGEV.data;
  if (SIGEV.err) return `<div class="empty">信号战绩加载失败：${esc(SIGEV.err)}</div>`;
  if (!d) return `<div class="empty">正在读取信号战绩…</div>`;
  if (d.ok === false) return `<div class="empty">${esc(d.err || "证据层暂无数据，先跑一次信号回测")}</div>`;
  const rows = toArr(d.signals);
  if (!rows.length) return `<div class="empty">暂无回测结果（signal_backtest 为空）</div>`;

  const b = (d.baseline || {})[String(d.fwd || 5)] || {};
  const fee = Number(d.fee || 0);
  /* 2026-10-29：界面口径统一为**税后**。
     税前的 48%~61% 胜率会让人误判"过半"，但同期全样本基线本来就是 46%，
     且双边成本 0.15% 足以把微幅正超额翻成负。税前值保留在 title 里做对照。 */
  const g2 = (v) => (v == null ? null : v);
  const bWrNet = g2(b.winrate_net) != null ? b.winrate_net : b.winrate;
  const bRetNet = g2(b.avg_ret_net) != null ? b.avg_ret_net : b.avg_ret;
  const bwr = bWrNet != null ? (bWrNet * 100).toFixed(2) : "—";
  const bwrGross = b.winrate != null ? (b.winrate * 100).toFixed(2) : "—";
  const color = { effective: "#1e7d5c", neutral: "#5b6470", adverse: "#c0392b", insufficient: "#9aa4b2" };
  const bgc = { effective: "#eefaf4", neutral: "#f3f4f6", adverse: "#fdeeec", insufficient: "#f6f7f9" };
  const GRID = "grid-template-columns:1.4fr .68fr .82fr .72fr .82fr .6fr .92fr;gap:6px;align-items:center;";

  const head = `<div style="${GRID}padding:4px 8px 6px;font-size:11.5px;color:#8b95a3;border-bottom:1px solid #e7eaef">
      <div>信号</div><div>样本</div><div title="已按双边成本扣费后的胜率">胜率</div>
      <div title="税后胜率 − 税后基线胜率">超额</div>
      <div title="已扣费的单次平均收益">税后收益</div><div>夏普</div><div>裁决</div></div>`;

  const line = (r) => {
    const wrn = r.winrate_net != null ? r.winrate_net : r.winrate;
    const wrG = ((r.winrate || 0) * 100).toFixed(1);
    const wr = ((wrn || 0) * 100).toFixed(1);
    const ed = (r.edge_wr || 0) * 100;
    const eds = (ed >= 0 ? "+" : "") + ed.toFixed(1);
    const rn = ((r.avg_ret_net != null ? r.avg_ret_net : r.avg_ret) || 0) * 100;
    const rns = (rn >= 0 ? "+" : "") + rn.toFixed(2);
    const nn = r.n >= 10000 ? (r.n / 10000).toFixed(1) + "万" : String(r.n);
    const c = color[r.verdict] || "#5b6470";
    const g = bgc[r.verdict] || "#f6f7f9";
    return `<div style="${GRID}padding:5px 8px;border-bottom:1px solid #f1f3f6;font-size:12.5px">
      <div style="font-weight:600" title="${escAttr(r.reason)}">${esc(r.reason)}</div>
      <div style="color:#8b95a3">${nn}</div>
      <div title="税前胜率 ${wrG}%">${wr}%</div>
      <div style="color:${ed >= 0 ? "#1e7d5c" : "#c0392b"};font-weight:600">${eds}pt</div>
      <div style="color:${rn >= 0 ? "#1e7d5c" : "#c0392b"}">${rns}%</div>
      <div style="color:#8b95a3">${r.sharpe_net != null ? r.sharpe_net : r.sharpe}</div>
      <div><span style="display:inline-block;padding:1px 7px;border-radius:9px;font-size:11.5px;color:${c};background:${g}">${esc(r.label)}</span></div>
    </div>`;
  };

  const buys = rows.filter((r) => r.direction === "buy");
  const sells = rows.filter((r) => r.direction === "sell");
  const block = (title, arr) => arr.length
    ? `<div class="bt-hint" style="margin:8px 0 2px;font-weight:600;color:#4a5462">${esc(title)}</div>` +
      head + arr.map(line).join("")
    : "";

  const t = d.tech || {};
  const tf = d.tech_factor != null ? Number(d.tech_factor) : 1;
  const tfTxt = Math.abs(tf - 1) < 0.005
    ? "技术面整体证据不足，权重未作修正"
    : `技术面证据已把技术类权重乘以 <b>${tf.toFixed(2)}</b>（历史整体${tf > 1 ? "有超额，升权" : "跑输基线，降权"}）`;
  const feeTxt = fee > 0
    ? `已扣双边成本 <b>${(fee * 100).toFixed(2)}%</b>（佣金+印花税+滑点），下表全部为税后口径`
    : "未经扣费（税前口径，会高估）";
  const bRetTxt = bRetNet != null ? (bRetNet * 100).toFixed(2) + "%" : "—";

  return `${block("买入方向（选股用）", buys)}${block("卖出方向（判断该不该追）", sells)}
    <div class="bt-hint" style="margin-top:9px;line-height:1.7">
      ${feeTxt}<br>
      基线：前向 ${esc(String(d.fwd || 5))} 日全样本胜率（税后）<b>${bwr}%</b>
      <span style="color:#8b95a3">（税前 ${bwrGross}%）</span>、平均收益 <b>${bRetTxt}</b>
      ｜ 技术面聚合超额 <b>${((t.edge_wr || 0) * 100).toFixed(1)}pt</b>、夏普 <b>${t.sharpe || 0}</b>
      （样本 ${t.n_total ? (t.n_total / 10000).toFixed(1) + " 万" : "—"}）<br>
      <b>失效条件：</b>样本 &lt; ${esc(String(500))} 记为"不足以判断"；税后超额转负即不再作为买入依据，
      权重层已按此自动降权（未通过大样本验证的信号权重上限收紧至 25%）。
    </div>`;
}

/* ====================================================================
   信号战绩 · 分市况（2026-10-29）

   为什么单独一块：上面的证据区块回答"这条信号整体有没有用"（全样本混算），
   这一块回答"在**当前市况**里它历史上好还是差"（同状态内比较）。实测两者会
   给出相反结论：混算时逆势类的正超额与追涨类的负超额互相抵消，看起来"都没用"；
   分开看才见到追涨类在弱势市有 -7pt 量级的稳定负超额。

   但分状态结论必须先过"前后半段"这一关，否则只是某一段行情的特征。所以每格
   带 stab：都≥+1pt=稳健有效 / 都≤-1pt=稳健有害 / 其余=未通过检验（灰）。
   没有 stab 的格子一律标"未检验"，不许让读者自行脑补成规律。
   ==================================================================== */
const SIGREG = { data: null, loading: false, err: "" };

function loadSignalRegime() {
  if (SIGREG.loading) return;
  SIGREG.loading = true;
  api("/api/signal/regime?fwd=10").then((d) => {
    SIGREG.data = d || {};
    SIGREG.loading = false;
    const el = $("#sig-reg-body");
    if (el) el.innerHTML = renderSigRegimeBody();
  }).catch((e) => {
    SIGREG.loading = false;
    SIGREG.err = String((e && e.message) || e || "加载失败");
    const el = $("#sig-reg-body");
    if (el) el.innerHTML = renderSigRegimeBody();
  });
}

function renderSigRegimeSection() {
  return `<div class="bt-sec">
    <div class="bt-head">
      <div class="bt-title">信号战绩 · 分市况<span class="bt-sub">同一市况内的超额 = 信号 vs 当日全市场平均，已扣双边成本</span></div>
    </div>
    <div id="sig-reg-body">${renderSigRegimeBody()}</div>
  </div>`;
}

function renderSigRegimeBody() {
  const d = SIGREG.data;
  if (SIGREG.err) return `<div class="empty">分市况数据加载失败：${esc(SIGREG.err)}</div>`;
  if (!d) return `<div class="empty">正在读取分市况表现…</div>`;
  if (d.ok === false) return `<div class="empty">${esc(d.err || "暂无分市况回测数据")}</div>`;

  const cur = d.current || {};
  const rows = toArr(d.signals).filter((r) => r.direction === "buy");
  if (!rows.length) return `<div class="empty">暂无分市况结果（先跑一次带 --diagnose 的回测）</div>`;

  const REGS = toArr(d.regimes);
  const curKey = cur.regime || "range";
  const byKey = {};
  rows.forEach((r) => { (byKey[r.reason] = byKey[r.reason] || {})[r.regime] = r; });
  const names = Object.keys(byKey).sort((a, b) => {
    const ea = byKey[a][curKey] ? byKey[a][curKey].edge_wr : -9;
    const eb = byKey[b][curKey] ? byKey[b][curKey].edge_wr : -9;
    return (eb || -9) - (ea || -9);
  });

  /* 稳定性标签：只有做过前后半段检验（stab 非空）才给结论，其余如实标"未检验" */
  const stabTag = (s) => {
    if (s === "ok") return { t: "稳健有效", c: "#1e7d5c", b: "#eefaf4" };
    if (s === "bad") return { t: "稳健有害", c: "#c0392b", b: "#fdeeec" };
    if (s === "unstable") return { t: "前后不一致", c: "#8a6d1f", b: "#fdf6e3" };
    return { t: "未检验", c: "#9aa4b2", b: "#f6f7f9" };
  };
  const pt = (v) => (v == null ? "—" : ((v * 100 >= 0 ? "+" : "") + (v * 100).toFixed(1)));
  const pcol = (v) => (v == null ? "#b6bec9" : (v >= 0 ? "#1e7d5c" : "#c0392b"));
  const GRID = "grid-template-columns:1.5fr .92fr .74fr .74fr .74fr .98fr;gap:6px;align-items:center;";

  const head = `<div style="${GRID}padding:4px 8px 6px;font-size:11.5px;color:#8b95a3;border-bottom:1px solid #e7eaef">
      <div>信号（买入）</div>
      <div title="当前市况下的税后超额胜率">当前·${esc(cur.regime_cn || "—")}</div>
      ${REGS.map((x) => `<div title="该市况下的税后超额胜率">${esc(x.cn)}</div>`).join("")}
      <div title="前后半段是否都成立">稳定性</div></div>`;

  const line = (nm) => {
    const m = byKey[nm] || {};
    const cr = m[curKey] || {};
    const st = stabTag(cr.stab);
    const cell = (k) => {
      const r = m[k];
      if (!r || r.edge_wr == null) return `<div style="color:#c3cad3">—</div>`;
      return `<div style="color:${pcol(r.edge_wr)};font-weight:${k === curKey ? "700" : "500"}">${pt(r.edge_wr)}</div>`;
    };
    return `<div style="${GRID}padding:5px 8px;border-bottom:1px solid #f1f3f6;font-size:12.5px">
      <div style="font-weight:600" title="${escAttr(nm)}">${esc(nm)}</div>
      <div style="color:${pcol(cr.edge_wr)};font-weight:700">${pt(cr.edge_wr)}</div>
      ${REGS.map((x) => cell(x.key)).join("")}
      <div><span style="display:inline-block;padding:1px 7px;border-radius:9px;font-size:11.5px;color:${st.c};background:${st.b}">${st.t}</span></div>
    </div>`;
  };

  const base = d.baseline || {};
  const baseTxt = REGS.map((x) => {
    const b = base[x.key];
    if (!b) return "";
    const v = (b.avg_ret_net || 0) * 100;
    return `<b style="color:${pcol(b.avg_ret_net)}">${esc(x.cn)} ${(v >= 0 ? "+" : "") + v.toFixed(2)}%</b>`;
  }).filter(Boolean).join(" ｜ ");

  const eff = d.eff_windows != null ? Number(d.eff_windows) : null;
  const gradeColor = d.grade === "可信" ? "#1e7d5c" : (d.grade === "仅供方向参考" ? "#8a6d1f" : "#c0392b");

  return `<div class="bt-hint" style="margin:0 0 8px;line-height:1.7">
      当前市况：<b>${esc(cur.regime_cn || "—")}</b>
      · 20 日等权市场收益 <b>${cur.ret20 != null ? ((cur.ret20 * 100 >= 0 ? "+" : "") + (cur.ret20 * 100).toFixed(2) + "%") : "—"}</b>
      · 当日上涨家数占比 <b>${cur.breadth != null ? Math.round(cur.breadth * 100) + "%" : "—"}</b>
      <span style="color:#8b95a3">（分档＝相对过去 ${esc(String(cur.hist || 250))} 个交易日的强弱 33/67 分位，不是绝对涨跌）</span><br>
      同市况全市场平均（税后，前向 ${esc(String(d.fwd || 10))} 日）：${baseTxt || "—"}<br>
      <span style="color:${gradeColor}">样本诚实度：${eff != null ? eff.toFixed(1) : "—"} 个有效独立窗口 → ${esc(d.grade || "—")}</span>
      <span style="color:#8b95a3">（相邻交易日前向窗口重叠，n 笔≠n 个独立观测，不能拿 n 当显著性）</span>
    </div>
    ${head}${names.map(line).join("")}
    <div class="bt-hint" style="margin-top:9px;line-height:1.7">
      <b>怎么读：</b>先看"稳定性"，标"稳健有害"的组合历史上两段都跑输同市况平均，不该作为买入依据；
      标"未检验/前后不一致"的不要当规律用。真正可落地的用法是<b>风控而非选股</b>：
      弱势市下行情的 T+5/T+10 平均收益前后两段一致为负，该做的是收仓位与收紧追涨，
      而不是换一套信号权重（实测没有信号通过分市况稳健性检验）。
    </div>`;
}

function renderBacktestSection() {
  const d = BT.data;
  const dates = (d && d.dates) || [];
  const periods = (d && d.periods) || [1, 3, 5, 10, 20];
  /* 2026-10-15 进哥：旧「主引擎预测」是上一版逻辑（问题多、已弃用），
     历史回测**只留四支柱**—— 源切换按钮与主引擎数据源一并撤掉。
     数据条数写进日期按钮 title，避免有人点了半天以为没反应。 */
  const dateRow = dates.length
    ? `<div class="bt-dates">` + dates.map((x) => {
        const on = x.date === BT.date;
        // title 里报"**这根支柱**这一天有几只"，不报四支柱合计 —— 否则切到只有 2 只的支柱会看不出差别
        const nThis = (x.n_by_pillar || {})[PRED.ptab];
        return `<a class="bt-date${on ? " active" : ""}" href="javascript:void(0)" ` +
          `title="${escAttr(x.date)}｜${escAttr(btPillarLabel(d))} ${nThis != null ? nThis : x.n_pillars} 只｜已走完 ${x.elapsed} 个交易日" ` +
          `onclick="btSet('date','${escAttr(x.date)}')">${esc(x.date.slice(5))}</a>`;
      }).join("") + `</div>`
    : `<div class="muted" style="margin:6px 0">暂无预测日可回测</div>`;
  const perRow = `<div class="bt-periods">` + periods.map((p) =>
    `<a class="bt-period${p === BT.period ? " active" : ""}" href="javascript:void(0)" ` +
    `onclick="btSet('period',${p})">T+${p}</a>`).join("") +
    `<span class="bt-hint">${d && d.auto && d.auto.period ? "已自动选该期已到期的最长周期，" : ""}` +
    `切换周期即切换右栏高亮列与下方总结</span></div>`;
  /* 2026-10-16：名单期次 ≠ 回测期次时必须点破 —— 上面写"竞价冲浪 2 只"（当期 10-15 期）、
     下面回测却是 10-14 期的 4 只，不解释就会变成"你这数又对不上"。
     原因：当期还没走完任何周期（elapsed=0），回测只能落在最近**有数据**的一期。 */
  const panelRun = (typeof PRED !== "undefined" && PRED.pillars && PRED.pillars.run_date) || "";
  const autoTip = (d && d.auto && d.auto.date)
    ? `<div class="bt-hint" style="margin:-8px 0 6px">已自动落在 <b>回测样本最全</b> 的一期` +
      `（${esc(d.date)}，走完 ${d.elapsed} 个交易日）；也可点上方的其它日期。` +
      (panelRun && panelRun !== d.date
        ? ` <b>注意：上面的名单是 ${esc(panelRun)} 期</b>（那一期还没走完任何周期，回测会是空的），` +
          `所以回测落在有数据的 ${esc(d.date)} 期。`
        : "") + `</div>`
    : "";
  return `<div class="bt-sec">
    <div class="bt-head">
      <div class="bt-title">历史回测<span class="bt-sub">${esc(btPillarLabel(d))} 预测名单 × 真实走势 · 命中＝跑赢池子同期平均</span></div>
    </div>
    <div class="bt-stickybar">${dateRow}${perRow}</div>${autoTip}
    ${renderBacktestBody()}
  </div>`;
}

function renderBacktestBody() {
  if (BT.loading && !BT.data) return `<div class="empty">正在回测…</div>`;
  if (BT.err) {
    return `<div class="empty bt-err">回测加载失败：${BT.err}
      <div style="margin-top:9px"><button class="btn sm" onclick="loadBacktest()">重新加载回测</button></div>
    </div>`;
  }
  const d = BT.data;
  if (!d) return `<div class="empty">正在回测…</div>`;
  const rows = toArr(d.rows);
  if (!rows.length) return `<div class="empty">该期没有可回测的标的</div>` + btPooledHtml();
  const p = BT.period;
  const head = `<div class="bt-grid-h">
      <div class="bt-cl-h">当日预测 · ${esc(d.date)} · ${rows.length} 只
        <span class="s">${esc(d.source_label)}</span></div>
      <div class="bt-cr-h">回测结果
        <span class="s">收益（命中＝跑赢池子）</span>
        <span class="bt-legend"><i class="lg lg-up"></i>涨·跑赢<i class="lg lg-dn"></i>跌·跑输<i class="lg lg-mix"></i>方向相反</span></div>
    </div>`;
  const body = rows.map((r, i) => {
    const tag = r.bucket_label
      ? `<span class="bt-tag">${esc(r.bucket_label)}</span>`
      : (r.tier ? `<span class="bt-tag">${r.tier === "core" ? "核心" : "观察"}</span>` : "");
    // 2026-10-16 进哥反馈：这一列股票名"点不了"（全站别处的股票名都能点开抽屉，只有这里不是链接）。
    // 改用全站统一的 codeLink（data-code-open → 捕获阶段委托打开个股抽屉），类名 .bt-nm / .bt-code 原样保留
    // （护栏与样式都按这两个类名选，改了会连带失效）。
    const left = `<div class="bt-cl">
        <span class="bt-rk">${r.rank || i + 1}</span>
        ${codeLink(r.symbol, { text: r.name || r.symbol, cls: "bt-nm" })}
        ${codeLink(r.symbol, { text: r.symbol, cls: "bt-code" })}
        ${r.score !== null && r.score !== undefined ? `<span class="bt-sc">${Math.round(r.score)}</span>` : ""}
        ${tag}
        <span class="bt-rs" title="${escAttr(r.reason || "")}">${esc(r.reason || "")}</span>
      </div>`;
    const right = `<div class="bt-cr">` + d.periods.map((x) => {
      const c = (r.cells || []).find((y) => y.p === x) || {};
      const on = x === p ? " on" : "";
      if (c.status === "pending") {
        return `<span class="bt-c bt-pend${on}" title="未到期：还差 ${c.need} 个交易日">` +
          `<em>T+${x}</em><b>${c.need}日</b></span>`;
      }
      if (c.status !== "ok") {
        return `<span class="bt-c bt-none${on}" title="该票缺 K 线（停牌/新股），无法回测">` +
          `<em>T+${x}</em><b>—</b></span>`;
      }
      // 2026-10-23 进哥要求的三色口径：涨跌方向与命中方向相反（涨但跑输 / 跌但跑赢）→ 浅黄底+黄框，
      // 文字颜色仍由 .bt-up / .bt-dn 决定（涨=红字、跌=绿字）。黄 ≠ 命中的替代语义，别混。
      const _up = c.ret > 0, _dn = c.ret < 0;
      const _mix = (c.bench !== null && c.bench !== undefined) && ((_up && !c.hit) || (_dn && c.hit));
      const cls = _up ? "bt-up" : (_dn ? "bt-dn" : "bt-fl");
      const mixCls = _mix ? " bt-mix" : "";
      const tip = `收益 ${btPct(c.ret)}｜池子同期 ${btPct(c.bench)}｜超额 ${btPct(c.alpha)}｜` +
        `${c.hit ? "跑赢池子 ✓" : "跑输池子 ✕"}` + (_mix ? "｜方向与命中相反（黄框）" : "");
      return `<span class="bt-c ${cls}${mixCls}${on}" title="${escAttr(tip)}"><em>T+${x}</em>` +
        `<b>${btPct(c.ret, 1)}</b><i class="${c.hit ? "hit" : "miss"}">${c.hit ? "✓" : "✕"}</i></span>`;
    }).join("") + `</div>`;
    return `<div class="bt-row">${left}${right}</div>`;
  }).join("");
  return `<div class="bt-grid">${head}<div class="bt-grid-body">${body}</div></div>` + btSummaryHtml() + btPooledHtml();
}

/* 跨期累计统计（2026-10-29 复查新增）
 * 为什么要有：上面「当日总结」只看一期，一期只有二三十只票，命中率 33% 还是 60% 完全在
 * 噪声区间内 —— 拿它判断「四支柱行不行」等于抛硬币。后端把全部 10 期摊平后，T+1/T+3/T+5
 * 各有 180~290 个样本，那时命中率与平均超额才谈得上统计意义。这里把总表如实摆出来，
 * 与单期结论并列，避免只看到某一期好看的数字就下判断。 */
function btPooledHtml() {
  const d = BT.data;
  const pl = d && d.pooled;
  if (!pl || !pl.periods) return "";
  const ps = pl.periods;
  const pct = (v, dd) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(dd == null ? 2 : dd) + "%";
  const body = toArr(d.periods).map((x) => {
    const t = ps[String(x)] || {};
    if (!t.n) return "";
    const hr = t.hit_rate;
    const hcol = (hr === null || hr === undefined) ? "" : (hr >= 0.5 ? "good" : "bad");
    const rcol = t.avg_ret > 0 ? "bt-up" : (t.avg_ret < 0 ? "bt-dn" : "");
    const acol = t.avg_alpha > 0 ? "bt-up" : (t.avg_alpha < 0 ? "bt-dn" : "");
    return `<tr>
      <td class="btp-p">T+${x}</td>
      <td class="btp-n">${t.n}${t.n_pending ? `<i>＋${t.n_pending} 未到期</i>` : ""}${t.n_nodata ? `<i>＋${t.n_nodata} 缺K线</i>` : ""}</td>
      <td class="btp-h ${hcol}">${pct(hr, 1)}</td>
      <td class="${rcol}">${pct(t.avg_ret)}</td>
      <td class="btp-b">${pct(t.avg_bench)}</td>
      <td class="${acol}">${pct(t.avg_alpha)}</td>
    </tr>`;
  }).join("");
  if (!body) return "";
  return `<div class="btp">
    <div class="btp-h2">跨期累计 · ${pl.n_dates} 期摊平
      <span class="s">单期只有二三十只票，命中率全落在噪声里；把各期摊平后样本才够判断策略本身行不行</span></div>
    <table class="btp-t">
      <thead><tr><th>周期</th><th>已到期样本</th><th>命中率</th><th>平均收益</th><th>池子同期</th><th>平均超额</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
    <div class="btp-v">${esc(pl.verdict || "")}</div>
  </div>`;
}

function btSummaryHtml() {
  const d = BT.data, p = BT.period;
  const st = (d.period_stats || {})[String(p)] || d.summary || {};
  const total = toArr(d.rows).length;
  const kpi = (v, k) => `<div class="bt-kpi"><span class="bt-kpi-v">${v}</span><span class="bt-kpi-k">${k}</span></div>`;
  const strip = d.periods.map((x) => {
    const t = (d.period_stats || {})[String(x)] || {};
    const r = t.hit_rate;
    const col = r === null || r === undefined ? "" : (r >= 0.5 ? "good" : "bad");
    return `<div class="bt-psum${x === p ? " on" : ""}">
        <span class="bt-psum-t">T+${x}</span>
        <span class="bt-psum-v ${col}">${r === null || r === undefined ? "未到期" : (r * 100).toFixed(0) + "%"}</span>
        <span class="bt-psum-n">${t.n || 0}/${total} 只</span>
      </div>`;
  }).join("");
  const bw = (x, lab) => x
    ? `<span class="bt-bw"><em>${lab}</em>${esc(x.name || x.symbol)} <b>${btPct(x.ret, 1)}</b></span>`
    : "";
  // 2026-10-16 进哥优化 P0：样本薄 + 显著性提示（不改口径，只提示，防误读）
  // 2026-10-29 复查修正：原本文案是**写死的**「T+3 及以上全部未到期」——数据早就不符合了
  //   （T+3 已有 79 个到期样本、T+5 有 59 个），一条过时的警告比没有警告更误导。
  //   现在按 pooled 的真实样本量动态生成，只说当前成立的话。
  const _ss = (d.source_stats || {})[d.source] || {};
  const _pd = _ss.dates || 0;
  const _n = (st.n != null ? st.n : (d.summary ? d.summary.n : 0)) || 0;
  const _pl = d.pooled || {};
  const _pooledMax = Math.max(0, ...toArr(d.periods).map((x) => {
    const t = (_pl.periods || {})[String(x)] || {};
    return t.n || 0;
  }));
  let _sigWarn = "";
  if (_pooledMax < 30) {
    _sigWarn = `<div class="bt-sigwarn" style="margin:8px 0;padding:8px 12px;border-left:3px solid #e8a33d;background:rgba(232,163,61,.12);color:#e8c07a;font-size:13px;line-height:1.6;border-radius:4px">⚠ <b>样本不足·勿外推</b>：跨期累计最多的周期也才 <b>${_pooledMax}</b> 个到期样本（共 ${_pd} 期），命中率与超额全在噪声内，任何"某柱更强"的差异都不可采信。攒够 30 个以上再说。</div>`;
  } else if (_n > 0 && _n < 50) {
    _sigWarn = `<div class="bt-sigwarn" style="margin:8px 0;padding:8px 12px;border-left:3px solid #e8a33d;background:rgba(232,163,61,.12);color:#e8c07a;font-size:13px;line-height:1.6;border-radius:4px">⚠ <b>单期样本偏薄</b>：本页这一期只有 <b>${_n}</b> 个到期样本，单期命中率上下 10% 都属正常波动；判断策略本身行不行，请以下方<b>跨期累计</b>（最多 ${_pooledMax} 个样本）为准。</div>`;
  }
  return `<div class="bt-sum">${_sigWarn}
    <div class="bt-sum-h">当日总结 · T+${p}<span class="s">${esc(d.date)} · ${esc(d.source_label)}</span></div>
    <div class="bt-kpis">
      ${kpi(st.hit_rate === null || st.hit_rate === undefined ? "—" : (st.hit_rate * 100).toFixed(1) + "%", "命中率（跑赢池子）")}
      ${kpi(btPct(st.avg_ret), "平均收益")}
      ${kpi(btPct(st.avg_alpha), "平均超额")}
      ${kpi(`${st.n || 0}/${total}`, "已到期 / 总样本")}
    </div>
    <div class="bt-verdict">${esc(st.verdict || "")}</div>
    <div class="bt-perstrip">${strip}</div>
    <div class="bt-bwrow">${bw(st.best, "最好")}${bw(st.worst, "最差")}
      <span class="bt-asof">K 线截至 ${esc(d.asof || "—")}｜已走完 ${d.elapsed} 个交易日</span></div>
    <div class="bt-foot">${esc(d.note || "")}</div>
  </div>`;
}

/* 启动：先建两级导航（决定首屏落在哪个子板块），再拉数据 */
buildThemePicker();
loadDragonMap();   // 2026-10-21：预取龙头分级映射（本地 JSON，几十 KB），保证首屏表格就带徽章
loadAvoidSet();    // 2026-10-22：预拉避雷名单 code 集合，供全站风险标记使用
initNav();
loadAll();
renderRefreshBar();
renderSchedText();
loadSchedulerStatus();
setInterval(() => { renderRefreshBar(); renderSchedText(); renderPagePollState(); pagePollAutoManage(); }, 1000);  // 倒计时 / 相对时长每秒走字
setInterval(loadRefreshStatus, 30000);  // 每 30s 同步一次后台刷新状态，并重算时效标记
setInterval(loadSchedulerStatus, 30000);  // 每 30s 同步一次后台自动调度状态

/* ---------------------------------------------------------------------------
 * 前端版本自检（2026-10-02）
 * 坑：网页里的 JS **不会自己重新加载** —— 跑的永远是打开那一刻那份代码。页面只定时拉 /api/dashboard 刷数据，
 * 所以后端把前端改了、已经打开着的窗口还是旧代码，接口把新数据递过去它也渲染不出来。
 * 2026-10-01 晚上就是这么回事：竞价卡做完了，王总桌面窗口里就是看不到（重开窗口才出现）。
 * 对策：每 60s 跟 /api/version 比一次版本，不一致就在顶部挂一条提示，点一下即换上新代码。
 * 版本号的唯一真源是 index.html 的 <meta name="app-version">：改前端递增它，两边自动对齐。
 * ------------------------------------------------------------------------- */
const FRONTEND_VERSION = (document.querySelector('meta[name="app-version"]') || {}).content || "";
function renderVersionNotice(cur, latest) {
  let bar = document.getElementById("ver-notice");
  if (!latest || cur === latest) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "ver-notice";
    bar.className = "ver-notice";
    bar.addEventListener("click", () => location.reload());
    document.body.insertBefore(bar, document.body.firstChild);
  }
  bar.textContent = "界面已更新（" + cur + " → " + latest + "），点这一条即可换上新代码";
  bar.title = "当前页面跑的是旧版 JS，后端已经是新版 —— 网页不会自己换代码，点一下重新加载";
}
async function loadVersionNotice() {
  try {
    const j = await api("/api/version");
    renderVersionNotice(FRONTEND_VERSION, j && j.version);
  } catch (_) { /* 自检失败不算错，别打扰看盘 */ }
}
loadVersionNotice();
setInterval(loadVersionNotice, 60000);

/* 全页轮询：页面可见性变化时，恢复可见即拉起（若正处于交易时段且已开启） */
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && PAGE_POLL.enabled && isTradingNow() && !PAGE_POLL.timer) startPagePoll();
  renderPagePollState();
});
renderPagePollState();   // 初始化药丸文案（开关状态已在上方 #pagePoll 初始化时读取）


function loadPrediction() {
  PRED.busy = true;
  return Promise.all([
    api("/api/pillars").catch((e) => ({ ok: false, error: String(e) })),
    api("/api/pillars/status").catch((e) => ({ ok: false, error: String(e) })),
  ]).then(([p, st]) => {
    PRED.pillars = p;
    PRED.pstatus = st;
    renderPrediction();
  });
}
function predRun(force) {
  const btn = $("#predRegen");
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  api("/api/pillars/run" + (force ? "?force=1" : ""), { method: "POST", headers: { "Content-Type": "application/json" } })
    .then(() => loadPrediction())
    .then(() => { if (btn) { btn.disabled = false; btn.textContent = "重新生成"; } })
    .catch((e) => {
      if (btn) { btn.disabled = false; btn.textContent = "重新生成"; }
      reportLoadError("每日预测生成", e);
    });
}


function pillarDetailLine(pkey, d) {
  if (!d || typeof d !== "object") return "";
  const bits = [];
  if (pkey === "policy_funnel") {
    if (d.board) bits.push("热板块 " + d.board);
    if (d.pct != null) bits.push("当日 " + (d.pct >= 0 ? "+" : "") + d.pct + "%");
    if (d.sig_n != null) bits.push("命中 " + d.sig_n + " 路信号");
  } else if (pkey === "t1_surf") {
    if (d.streak != null) bits.push(d.streak + " 连板");
    if (d.vol_ratio != null) bits.push("量比 " + d.vol_ratio);
    if (d.pct != null) bits.push("涨幅 " + (d.pct >= 0 ? "+" : "") + d.pct + "%");
  } else if (pkey === "dip_revert") {
    if (d.pos60 != null) bits.push("60日分位 " + Math.round(d.pos60 * 100) + "%");
    if (d.dd != null) bits.push("回撤 " + (d.dd * 100).toFixed(1) + "%");
  } else if (pkey === "resonance") {
    if (d.pillars) bits.push("共振 " + d.pillars.length + " 支柱");
    if (d.in_core) bits.push("主引擎核心池");
  } else if (pkey === "self_learn") {
    if (d.from) bits.push("来自 " + d.from.join(" + "));
    if (d.w_sum != null) bits.push("加权 " + d.w_sum.toFixed(2));
    bits.push(d.weights_effective ? "权重已生效" : "权重未生效（样本不足）");
  }
  return bits.length ? `<div class="pil-detail">${esc(bits.join(" ｜ "))}</div>` : "";
}

function pillarColumnCard(pkey, pdata) {
  if (!pdata) return "";
  const items = toArr(pdata.items);
  const stCls = { ok: "bull", pending_confirm: "neu", confirmed: "bull", partial: "neu" }[pdata.status] || "neu";
  const stTxt = { ok: "已生成", pending_confirm: "待竞价确认", confirmed: "已确认", partial: "部分" }[pdata.status] || pdata.status || "";
  let extra = "";
  // 2026-10-16 进哥：这排板块小标签长得像按钮、点了却没反应（"点起来不舒服"）——
  // 改成一行普通文字。信息一点没少，但不再扮演"假按钮"。
  if (pkey === "policy_funnel" && pdata.state && pdata.state.boards) {
    const _bs = toArr(pdata.state.boards);
    if (_bs.length) {
      extra = `<div class="pil-boards">覆盖热板块：<b>${_bs.map((x) => esc(x)).join("</b> · <b>")}</b></div>`;
    }
  }
  if (pkey === "resonance" && pdata.state && pdata.state.verdict) {
    extra = `<div class="pil-verdict">风控闸：${esc(pdata.state.verdict)}</div>`;
  }
  // 2026-10-16：E 自我提升 —— 把"学到了什么"如实摆在一行里（权重 + 样本数 + 是否生效），
  // 与政策漏斗那行"覆盖热板块"同一个位置、同一种写法。
  if (pkey === "self_learn" && pdata.state && pdata.state.weights) {
    const _w = pdata.state.weights || {};
    const _parts = PILLAR_TABS.filter((t) => t.key !== "self_learn" && _w[t.key] != null)
      .map((t) => `<b>${esc(t.label)} ${(_w[t.key] * 100).toFixed(0)}%</b>`).join(" · ");
    // 2026-10-16：权重生效状态徽章（三态：等权运行中 / 收缩生效中 / 样本外验证通过）。
    // 诚实呈现"当前=等权"这一事实，不夸大权重已生效。
    const _stage = pdata.state.weight_stage || "";
    let _scls = "neu";
    if (_stage.indexOf("样本外验证通过") === 0) _scls = "bull";
    else if (_stage.indexOf("收缩生效中") === 0) _scls = "ch-A";
    else _scls = "neu";
    const _badge = _stage ? `<span class="badge ${_scls} sl-stage">${esc(_stage)}</span>` : "";
    const _eff = pdata.state.weights_effective;
    extra = `<div class="pil-boards">`
      + (_badge ? `<div class="sl-stage-row">${_badge}</div>` : "")
      + `学到的支柱权重：${_parts}`
      + `（到期样本 ${pdata.state.samples_total || 0} 个`
      + (_eff ? "，已生效）" : `，未达门槛 ${pdata.state.gate} → 本期按等权 + 多支柱共振给票）`)
      + `</div>`;
  }
  const list = items.length ? items.map((it) => {
    const sc = Math.max(0, Math.min(100, Math.round(it.score || 0)));
    const topCls = (it.rank === 1 || it.rank === "1") ? " top" : "";
    // 2026-10-16 重排（进哥要求）：一张票 = 一张**紧凑卡**，观感对齐「多源共振」的 .res-card
    // （名次 + 名称 + 代码 + 得分 → 得分条 → 理由行 → 明细行），外层 .pil-list 改两列网格。
    // 字段一个没少：original rank / score / reason / pillarDetailLine 全部保留。
    return `<div class="pil-item">
      <div class="pil-row1">
        <span class="pil-rank${topCls}">${esc(it.rank)}</span>
        ${codeLink(it.symbol, { text: it.name || it.symbol, cls: "pil-name" })}
        <span class="pil-code">${esc(it.symbol)}</span>
        <span class="pil-score" title="综合得分">${sc}</span>
      </div>
      <div class="pil-bar" title="综合得分 ${sc}"><span style="width:${sc}%"></span></div>
      ${it.reason ? `<div class="pil-reason"><span class="pil-ico">⟐</span>${esc(it.reason)}</div>` : ""}
      ${pillarDetailLine(pkey, it.detail)}
    </div>`;
  }).join("") : `<div class="pil-empty">本期无入选标的</div>`;
  return `<div class="pil-col" id="pil-sec-${escAttr(pkey)}">
    <div class="pil-col-h">
      <span class="pil-title" title="${escAttr(pdata.title || pkey)}">${esc(pillarShortName(pkey, pdata))}</span>
      ${stTxt ? `<span class="badge ${stCls}">${esc(stTxt)}</span>` : ""}
      <span class="pil-cnt">${items.length} 只</span>
    </div>
    ${pdata.summary ? `<div class="pil-sum">${esc(pdata.summary)}</div>` : ""}
    ${extra}
    <div class="pil-list">${list}</div>
  </div>`;
}

function renderPillarSnapshot(d) {
  if (!d || d.ok === false) {
    return `<div class="muted">四支柱引擎尚未生成（等 16:00 主管线，或到「每日预测」页点生成）</div>`;
  }
  const ms = d.market_state || {};
  const msCls = { "结构": "bull", "震荡": "neu", "普跌": "bear" }[ms.state] || "neu";
  const cols = ["policy_funnel", "t1_surf", "dip_revert", "resonance"].map((k) => {
    const p = (d.pillars || {})[k];
    const items = toArr(p && p.items).slice(0, 3).map((it) =>
      `<div class="snap-row">${codeLink(it.symbol, { text: it.name || it.symbol, cls: "snap-name" })} <span class="s">${Math.round(it.score || 0)}</span></div>`
    ).join("") || `<div class="muted">—</div>`;
    return `<div class="snap-col"><div class="snap-h" title="${escAttr(p ? p.title : k)}">${esc(pillarShortName(k, p))}</div>${items}</div>`;
  }).join("");
  return `<div class="pillar-snap">
    <div class="pillar-snap-h">
      <span class="badge ${msCls}">市场状态：${esc(ms.state || "未知")}</span>
      <span class="s">期次 ${esc(d.run_date || "")}</span>
      <button class="btn sm" onclick="showTab('prediction')">看完整四支柱 →</button>
    </div>
    <div class="pillar-snap-cols">${cols}</div>
  </div>`;
}

function renderPrediction() {
  const box = $("#p-prediction");
  if (!box) return;
  syncTopbarH();      // 顶栏高度随宽度换行会变，进面板时再量一次（钉住条靠它定位）
  const d = PRED.pillars;
  if (!d || d.ok === false) {
    box.innerHTML = `<div class="pred-head">
      <div class="pred-title">每日预测 · 四支柱方案</div>
      <button class="btn primary" id="predRegen" onclick="predRun(true)">立即生成首期</button>
    </div>
    <div class="empty">${esc((d && d.note) || (d && d.error) || "四支柱引擎尚未生成任何一期")}</div>
    ${renderSigEvidenceSection()}
    ${renderSigRegimeSection()}
    <div id="bt-sec">${renderBacktestSection()}</div>`;
    if (!BT.data && !BT.loading) loadBacktest();
    if (!SIGEV.data && !SIGEV.loading) loadSignalEvidence();
    if (!SIGREG.data && !SIGREG.loading) loadSignalRegime();
    return;
  }
  const ms = d.market_state || {};
  const msLabel = { "结构": "结构（资金集中主线）", "震荡": "震荡（仓位中性）", "普跌": "普跌（风控收紧）" }[ms.state] || ms.state || "未知";
  const msCls = { "结构": "bull", "震荡": "neu", "普跌": "bear" }[ms.state] || "neu";
  const br = ms.breadth || {};
  const msBar = `<div class="pil-ms">
    <span class="badge ${msCls}">市场状态：${esc(msLabel)}</span>
    ${br.up_n != null ? `<span class="s">上涨 ${br.up_n}/${br.pool_n}（${((br.up_ratio || 0) * 100).toFixed(0)}%）</span>` : ""}
    ${br.median_pct != null ? `<span class="s">中位涨幅 ${br.median_pct >= 0 ? "+" : ""}${br.median_pct}%</span>` : ""}
    ${d.pool_n ? `<span class="s" title="四支柱深度分析覆盖的候选池：按近60个交易日成交额从全市场排序取前 ${d.pool_n} 只（剔 ST/北交所/无K线）；全市场其余部分由「全市场快扫」逐日覆盖">深度分析 <b>${d.pool_n}</b> 只</span>` : ""}
    ${d.scan_n ? `<span class="s" title="全市场快扫覆盖只数（stock_universe_scan，每日全量重算）">全市场快扫 <b>${d.scan_n}</b> 只</span>` : ""}
    ${(ms.rules || []).map((r) => `<span class="pil-rule">${esc(r)}</span>`).join("")}
  </div>`;
  const pillars = d.pillars || {};
  const active = PILLAR_TABS.some((t) => t.key === PRED.ptab) ? PRED.ptab : "policy_funnel";
  // 子页签行：与「今日决策/今日看板/汇总」同款 .sub-tab 横排，名称用方案本名（不带 A/B/C/D）。
  // 2026-10-16（进哥二次修正）：点一下 = **就地换页**（只显示该支柱 + 它自己的回测），不是滚动跳转。
  const tabRow = `<div class="pillar-tabs">` +
    PILLAR_TABS.map((t) => {
      const p = pillars[t.key] || {};
      const n = toArr(p.items).length;
      return `<a class="sub-tab${t.key === active ? " active" : ""}" href="javascript:void(0)" ` +
        `data-pil="${escAttr(t.key)}" ` +
        `title="${escAttr(p.title || t.label)}（${n} 只）· 点一下换到这一页（含它自己的回测）" ` +
        `onclick="predPillarTab('${t.key}')">` +
        `${esc(t.label)}${n ? ` <b>${n}</b>` : ""}</a>`;
    }).join("") + `</div>`;
  /* 2026-10-15：原此处有一行「校准：…」，数字来自旧引擎（pillars.calibration 统计
     prediction_item 历史）→ 属"界面上残留旧引擎"，按进哥要求移除。
     待四支柱自身攒够到期期次后，改为基于 pillar_item 做校准再放回来（那时口径也自洽）。 */
  const foot = `<div class="pil-foot">
    <div class="s">引擎版本 ${esc(d.engine_ver || "")} ｜ 期次 ${esc(d.run_date || "")} ｜ 基本面覆盖 ${d.fundamentals_n || 0} 只 ｜ ${esc(d.note || "")}</div>
  </div>`;
  box.innerHTML = `<div class="pred-head">
      <div class="pred-title">每日预测 · 四支柱方案</div>
      <button class="btn primary" id="predRegen" onclick="predRun(true)">重新生成</button>
    </div>
    ${msBar}
    ${tabRow}
    <div class="pil-stack">${pillarColumnCard(active, pillars[active])}</div>
    ${renderSigEvidenceSection()}
    ${renderSigRegimeSection()}
    <div id="bt-sec">${renderBacktestSection()}</div>
    ${foot}`;
  // 首次进入自动拉一次回测（已有数据/正在加载则不重复打接口）
  if (!BT.data && !BT.loading) loadBacktest();
  if (!SIGEV.data && !SIGEV.loading) loadSignalEvidence();
  if (!SIGREG.data && !SIGREG.loading) loadSignalRegime();
}

/* ===================== 多源共振（2026-10-15 进哥需求）=====================
   面板位置：今日总览 → 「多源共振」（紧跟「每日预测」）。
   三支柱（面板内 .sub-tab 切换；对应进哥拍板的「以票/以战法/以人 三套都做」）：
     A 个股共振 —— 票为中心：一只票被几路信号同时指向（战法命中 ×
                    主引擎交叉 × 舆情；博主维度 2026-10-30 已下线），龙虎榜**只作印证加成**，附完整证据链
     B 战法体系 —— 战法为中心：回测档案（年化/胜率/回撤/跑赢基准/评级/样本外）+ 当日命中 +
                    知识图谱互证（视频语 ↔ 回测结果，一致/存疑/无回测）
     C 信源画像 —— 人为中心：博主 / 龙虎榜席位 / 战法 的历史战绩与信誉分，按市场环境分档
   数据全部来自 /api/resonance（后端 resonance.py 产出），前端只做呈现，不算任何指标。
   口径（含"超额基准=全市场等权中位数""龙虎榜不作预判输入"等）由接口下发，前端原样折叠展示。 */
function loadResonance(force) {
  const box = $("#p-resonance");
  if (box && !RES.data) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">多源共振</div></div>
      <div class="empty">正在融合多源信号…</div>`;
  }
  RES.busy = true;
  const qs = force ? "?t=" + Date.now() : "";
  return Promise.all([
    api("/api/resonance" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true })),
    api("/api/evolve" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true })),
    // 2026-10-15 进哥要求「学完得有成果」：第 3 份数据 = 系统自选（用学出的权重选出的票）
    api("/api/selfpick" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true })),
  ]).then(([d, e, sp]) => {
    RES.data = d; RES.evo = e; RES.sp = sp; RES.busy = false; renderResonance();
  });
}
function resEvoRun() {
  const btn = $("#evoRegen");
  if (btn) { btn.disabled = true; btn.textContent = "进化中…"; }
  const restore = () => { const b = $("#evoRegen"); if (b) { b.disabled = false; b.textContent = "立即进化"; } };
  // 后台执行 + 轮询（learn 含战法历史回放要跑 10~20 秒，同步等待会被网关掐成 502）
  api("/api/evolve/run?force=1", { method: "POST", headers: { "Content-Type": "application/json" } })
    .then(() => {
      let n = 0;
      const t = setInterval(() => {
        n++;
        api("/api/evolve/status").then((s) => {
          const b = $("#evoRegen");
          if (b && s && s.stage) b.textContent = "进化中…" + s.stage;
          if (!s || !s.running || n > 40) { clearInterval(t); restore(); loadResonance(true); }
        }).catch(() => { clearInterval(t); restore(); });
      }, 2500);
    })
    .catch((e) => { restore(); reportLoadError("自我进化", e); });
}
function resRun(force) {
  const btn = $("#resRegen");
  if (btn) { btn.disabled = true; btn.textContent = "融合中…（约 20 秒）"; }
  api("/api/resonance/run" + (force ? "?force=1" : ""),
      { method: "POST", headers: { "Content-Type": "application/json" } })
    .then(() => loadResonance(true))
    .then(() => { const b = $("#resRegen"); if (b) { b.disabled = false; b.textContent = "重新融合"; } })
    .catch((e) => {
      const b = $("#resRegen");
      if (b) { b.disabled = false; b.textContent = "重新融合"; }
      reportLoadError("多源共振生成", e);
    });
}
function resTierCls(t) {
  return { S: "bull", A: "bull", B: "neu", C: "neu", D: "bear" }[t] || "neu";
}
function resNum(v, suffix, digits) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const d = digits === undefined ? 2 : digits;
  return (v >= 0 ? "+" : "") + Number(v).toFixed(d) + (suffix || "");
}
function resStockCard(c) {
  const tier = c.tier || "-";
  const fams = String(c.strat_families || "").split("、").filter(Boolean);
  const evs = toArr(c.evidence);
  return `<div class="res-card">
    <div class="res-card-h">
      <span class="badge ${resTierCls(tier)}">${esc(tier)}</span>
      ${codeLink(c.code, { text: c.name || c.code, cls: "res-name" })}
      <span class="res-code">${esc(c.code)}</span>
      <span class="res-score" title="共振分">${Math.round(c.res_score || 0)}</span>
    </div>
    <div class="res-bar"><span style="width:${Math.max(0, Math.min(100, Math.round(c.res_score || 0)))}%"></span></div>
    <div class="res-tags">
      ${c.n_blogger ? `<span class="res-tag">博主 ${c.n_blogger} 位</span>` : ""}
      ${c.n_strat ? `<span class="res-tag">战法 ${c.n_strat} 个</span>` : ""}
      ${fams.slice(0, 3).map((f) => `<span class="res-tag res-fam">${esc(f)}</span>`).join("")}
      ${c.engine_cross ? `<span class="res-tag res-eng">引擎交叉</span>` : ""}
      ${c.lhb_bonus ? `<span class="res-tag ${c.lhb_bonus > 0 ? "res-ok" : "res-bad"}">资金印证 ${c.lhb_bonus > 0 ? "+" : ""}${c.lhb_bonus}</span>` : ""}
    </div>
    <div class="res-verdict">${esc(c.verdict || "")}</div>
    ${evs.length ? `<details class="res-ev"><summary>证据链（${evs.length} 条原话 / 依据）</summary>
      <ul>${evs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
}
function resCardsView(d) {
  const cards = toArr(d.cards);
  if (!cards.length) return `<div class="empty">本期无共振标的（后端引擎尚未产出，或当日无多源交叉）</div>`;
  /* 2026-10-30：权重改读接口下发的 d.weights —— 博主维度已永久下线，
     后端已把它的份额按比例并入其余三项并重新归一化。
     旧写法读 RES.evo.weight.w（自我进化那一份仍是四维、blogger=13.6%），
     会在页面上显示「博主 13.6%」，等于删了又没删干净。 */
  const W = (d.weights && d.weights.strategy != null)
    ? d.weights
    : { blogger: 0, strategy: 0.5152, senti: 0.2121, engine: 0.2727 };
  const wSrc = (d.weights && d.weights.src === "learned") ? "数据学出" : "默认";
  const intro = `<div class="res-intro">
    <div class="res-intro-h">票为中心 · 一只票被几路信号同时指向</div>
    <div class="res-intro-b">共振分 = 战法命中
      <b>${(W.strategy * 100).toFixed(1)}%</b> ＋ 论坛舆情 <b>${(W.senti * 100).toFixed(1)}%</b>
      ＋ 主引擎交叉 <b>${(W.engine * 100).toFixed(1)}%</b>
      （权重<b>${wSrc}</b>，来历见「自我进化」支柱）；龙虎榜仅在 ±10 分内作<b>印证加成</b>，
      不进主干（上榜是结果不是原因）。共 ${cards.length} 只，按共振分降序。<br>
      <span class="s">博主荐股维度已于 2026-10-30 永久下线（实证为纯噪声），原份额已按比例并入以上三项。</span></div>
  </div>`;
  const div = toArr(d.divergence);
  const divHtml = div.length ? `<details class="res-div">
    <summary>分歧预警（${div.length} 条 · 内部矛盾最有价值 · 点击展开）</summary>
    <div class="res-div-list">${div.slice(0, 12).map((x) =>
      `<div class="res-div-item"><span class="res-div-kind">${esc(x.kind)}</span>
        ${codeLink(x.code, { text: x.name || x.code })}<span class="res-div-t">${esc(String(x.detail || "").slice(0, 96))}</span></div>`
    ).join("")}</div></details>` : "";
  return intro + divHtml + `<div class="res-grid">${cards.slice(0, 60).map(resStockCard).join("")}</div>`;
}
function resStrategyView(d) {
  const rows = toArr(d.strategies);
  if (!rows.length) return `<div class="empty">战法档案为空（请点「重新融合」）</div>`;
  const tb = rows.map((r) => {
    const oos = r.oos_validated
      ? `<span class="chip-s">已验证 ${resNum(r.oos_return, "%", 1)}</span>`
      : `<span class="chip-s res-warn">未通过/未验证</span>`;
    return `<tr>
      <td class="res-td-rk">${esc(r.rank)}</td>
      <td class="res-td-name"><b>${esc(r.name || r.strategy_key)}</b>
        <div class="res-td-sub">${esc(r.strategy_key)}｜${esc(r.school || "")}</div></td>
      <td>${resNum(r.annual_return, "%", 1)}</td>
      <td>${resNum(r.win_rate, "%", 1)}</td>
      <td>${resNum(r.max_dd, "%", 1)}</td>
      <td>${resNum(r.beat_bench, "%", 1)}</td>
      <td><span class="badge ${resTierCls(r.rating === "优秀" ? "A" : (r.rating === "良好" ? "B" : "D"))}">${esc(r.rating || "-")}</span></td>
      <td>${esc(r.trades || 0)}</td>
      <td>${oos}</td>
      <td>${r.hit_n ? `<span class="res-hit">${esc(r.hit_n)}</span>` : "—"}</td>
      <td>${r.kb_n ? esc(r.kb_n) : "—"}</td>
    </tr>`;
  }).join("");
  const kb = toArr(d.kb_nodes);
  const byCons = { "一致": [], "存疑": [], "待观察": [], "无回测": [] };
  kb.forEach((n) => { (byCons[n.consistency] || byCons["无回测"]).push(n); });
  const kbHtml = Object.keys(byCons).filter((k) => byCons[k].length).map((k) => `
    <div class="res-kb-g">
      <div class="res-kb-gh">${esc(k)}（${byCons[k].length}）</div>
      ${byCons[k].map((n) => `<div class="res-kb-item">
        <div class="res-kb-t">${esc(n.title)}<span class="res-kb-src">${esc(n.track || "")}</span></div>
        <div class="res-kb-v">${esc(n.verdict || "")}</div></div>`).join("")}
    </div>`).join("");
  return `<div class="res-intro">
    <div class="res-intro-h">战法为中心 · 回测档案 + 当日命中 + 知识图谱互证</div>
    <div class="res-intro-b">回测口径：等权单标的满仓、信号次日收盘成交、不计手续费与滑点、后复权，
      仅作<b>策略相对强弱</b>比较，不是可实现收益预测。样本外按 70/30 时间序切分验证；
      标「未通过」的说明样本内表现未能外推（典型过拟合特征）。
      ${d.strategy_note ? `<br><b>口径提示：</b>${esc(d.strategy_note)}` : ""}</div>
  </div>
  <div class="res-tb-wrap"><table class="res-tb">
    <thead><tr><th>#</th><th>战法</th><th>年化</th><th>胜率</th><th>最大回撤</th><th>跑赢基准</th>
      <th>评级</th><th>交易数</th><th>样本外</th><th>今日命中</th><th>图谱</th></tr></thead>
    <tbody>${tb}</tbody></table></div>
  ${kb.length ? `<details class="res-kb" open><summary>知识图谱互证（视频语 ↕ 回测结果，共 ${kb.length} 条）</summary>${kbHtml}</details>` : ""}`;
}
function resSourceView(d) {
  const rows = toArr(d.sources);
  if (!rows.length) return `<div class="empty">信源画像为空（请点「重新融合」）</div>`;
  const groups = [
    { key: "blogger", title: "荐股博主", hint: "样本＝该博主推荐过的票；超额＝相对全市场等权中位数" },
    { key: "strategy", title: "战法（回测档案）", hint: "样本＝回测交易次数；跑赢基准＝跑赢基准的交易占比" },
    { key: "lhb", title: "龙虎榜席位形态", hint: "按上榜原因分档，仅作资金印证参考" },
    { key: "sentiment", title: "论坛情绪", hint: "剔除无效源" },
    { key: "engine", title: "主引擎", hint: "" },
  ];
  const html = groups.map((g) => {
    const arr = rows.filter((r) => r.src_type === g.key);
    if (!arr.length) return "";
    const body = arr.map((r) => {
      const isStrat = r.src_type === "strategy";
      const st = (r.by_state && Object.keys(r.by_state).length)
        ? Object.keys(r.by_state).map((k) => `${k} ${resNum(r.by_state[k].avg_ex, "%", 1)}`).join("　")
        : "";
      return `<div class="res-src-row">
        <span class="badge ${resTierCls(r.tier)}">${esc(r.tier || "-")}</span>
        <div class="res-src-main">
          <div class="res-src-n">${esc(r.src_key)}
            <span class="res-src-nn">样本 ${esc(r.n_filled || 0)}${r.n_sig && r.n_sig !== r.n_filled ? " / " + esc(r.n_sig) : ""}</span></div>
          ${st ? `<div class="res-src-st">分市场环境：${esc(st)}</div>` : ""}
          ${r.note ? `<div class="res-src-note">${esc(r.note)}</div>` : ""}
        </div>
        <div class="res-src-m">
          <span class="res-src-k">胜率</span>${r.win_t5 === null || r.win_t5 === undefined ? "—" : (r.win_t5 * 100).toFixed(1) + "%"}
        </div>
        <div class="res-src-m">
          <span class="res-src-k">${isStrat ? "跑赢基准" : "平均超额(T5)"}</span>
          ${isStrat ? (r.pick_skill === null || r.pick_skill === undefined ? "—" : resNum(r.pick_skill, "%", 1))
                    : resNum(r.avg_ex_t5, "%")}
        </div>
        <div class="res-src-m"><span class="res-src-k">信誉分</span><b>${r.score === null || r.score === undefined ? "—" : r.score}</b></div>
      </div>`;
    }).join("");
    return `<div class="res-src-g"><div class="res-src-gh">${esc(g.title)}
      <span class="res-src-gn">${arr.length} 个源</span>
      ${g.hint ? `<span class="res-src-hint">${esc(g.hint)}</span>` : ""}</div>${body}</div>`;
  }).join("");
  return `<div class="res-intro">
    <div class="res-intro-h">人为中心 · 谁的话更值钱（按历史战绩给信誉分）</div>
    <div class="res-intro-b">信誉分 = 中位超额为主轴（±10% 映射 ±40 分）＋ 胜率补正（±15 分），
      再按样本量收缩（n/(n+10)）抑制小样本虚高；<b>样本不足的一律不给分、不参与排序，如实标注</b>。</div>
  </div>${html}`;
}
/* ---------- 第 4 支柱：自我提升（2026-10-15 进哥追加）----------
   这一支柱不看别人的东西，看**系统自己长出了什么**：
     ① 权重自学习：共振分的四个权重由 IC（信息系数）学出，附「默认 vs 学出」对比与 IC 一览
     ② 分市场环境权重 + 自生长规则（环境×声量×战法×舆情×龙虎榜 组合，时间序 70/30 样本外验证）
     ③ 知识内化：把结论/方法/反例写成我们自己的条目（不是别人的原话）
     ④ 信源趋势：谁在上升、谁在衰减
   数据来自 /api/evolve（后端 evolve.py 产出，查看器只读）。 */
const RES_DIML = { blogger: "博主声量", strategy: "战法命中", senti: "论坛舆情",
                   engine: "主引擎交叉", lhb: "龙虎榜验证" };
function resWBar(k, v, base) {
  const pct = Math.max(0, Math.min(100, Math.round((v || 0) * 100)));
  return `<div class="res-wrow">
    <span class="res-wname">${esc(RES_DIML[k] || k)}</span>
    <span class="res-wbar"><i style="width:${pct}%"></i></span>
    <b class="res-wv">${((v || 0) * 100).toFixed(1)}%</b>
    <span class="res-wd">默认 ${((base || 0) * 100).toFixed(0)}%</span>
  </div>`;
}
/* ===== 系统自选（2026-10-15 进哥要求：学完之后得有成果） =====
   两张榜：主榜「系统自选」= 按学出权重 + 自生长规则的综合最优；
   附榜「系统自己发现」= 博主声量 0、纯靠战法与规则挖出来的票（「不模仿」最直接的证据）。
   注意：这两张榜不是"汇总别人说了什么"，而是系统**自己的判断**。 */
function resSelfPickView() {
  const sp = RES.sp;
  const H = `<div class="res-evo-h">★ 系统自选 —— 学完之后，系统自己选出来的票</div>`;
  if (!sp) {
    return H + `<div class="hint">正在生成系统自选…（若刚更新过界面，需要重启查看器：后端引擎要重新载入）</div>`;
  }
  if (sp.ok === false || sp._err) {
    return H + `<div class="empty">${esc(sp.error || sp.note || "接口不可用")}</div>`;
  }
  const st = sp.stats || {}, main = toArr(sp.picks), own = toArr(sp.own_picks);
  const nm = (v) => (v == null ? "—" : fmtPct(v));
  const cl = (v) => (v == null ? "" : (v >= 0 ? "bull" : "bear"));
  const card = (x) => {
    // 2026-10-15 进哥指出：自选卡与共振卡「样式不一致、字小突兀」。
    // 实测病灶三条（已全部修）：① 股票名漏了 res-name 类 → 掉回按钮默认字号；
    // ② 正文行误用 .res-hit（那是「资金印证 +20」用的红色小胶囊，红底红字）；
    // ③ 缺 .res-bar 进度条、分数 16.3px 而非 17px。
    // 现改为**与 resStockCard 完全同构**：同 res-card-h / res-bar / res-tags /
    // res-verdict / res-ev 骨架，字号字重全部由同一套 CSS 决定，视觉必然一致。
    const fams = String(x.strat_families || "").split("、").filter(Boolean);
    const evs = toArr(x.evidence);
    const sc = Math.max(0, Math.min(100, Math.round(x.score || 0)));
    const dcls = x.direction === "偏多" ? "res-ok" : (x.direction === "偏空" ? "res-bad" : "");
    return `<div class="res-card">
    <div class="res-card-h">
      <span class="res-rank" title="系统自选排名">${esc(x.rank)}</span>
      <span class="badge ${resTierCls(x.tier)}">${esc(x.tier)}</span>
      ${codeLink(x.code, { text: x.name || x.code, cls: "res-name" })}
      <span class="res-code">${esc(x.code)}</span>
      <span class="res-score" title="系统自选评分">${sc}</span>
    </div>
    <div class="res-bar"><span style="width:${sc}%"></span></div>
    <div class="res-tags">
      <span class="res-tag ${dcls}">${esc(x.direction || "—")} · 置信 ${esc(x.confidence == null ? "—" : x.confidence)}</span>
      ${x.n_strat ? `<span class="res-tag">战法 ${esc(x.n_strat)} 个</span>` : ""}
      ${fams.slice(0, 3).map((f) => `<span class="res-tag res-fam">${esc(f)}</span>`).join("")}
      ${x.n_blogger ? `<span class="res-tag">博主 ${esc(x.n_blogger)} 位</span>` : `<span class="res-tag">无博主信号</span>`}
      ${x.is_own ? `<span class="res-tag res-ok" title="博主声量为 0，纯靠战法命中与规则选出来">★ 系统自己发现</span>` : ""}
      ${x.rule_hits ? `<span class="res-tag res-eng">规则命中</span>` : ""}
    </div>
    <div class="res-verdict">战法命中 ${esc(x.n_strat || 0)} 个${x.strat_families ? `（${esc(x.strat_families)}）` : ""}｜${x.n_blogger ? `博主参考 ${esc(x.n_blogger)} 位` : "无博主信号"}${x.rule_hits ? "｜命中自生长规则" : ""}</div>
    <div class="res-verdict res-sp-real">${x.fill_status === "ok"
      ? `实际（T+5）：<b class="${cl(x.ret_t5)}">${nm(x.ret_t5)}</b>｜超额 <b class="${cl(x.ex_t5)}">${nm(x.ex_t5)}</b>`
      : "实际：待结算（T+5 走完自动回填）"}</div>
    ${evs.length ? `<details class="res-ev"><summary>推荐依据（${evs.length} 条）</summary>
      <ul>${evs.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
  };
  const m = st.main || {}, hi = st.hist || {};
  const histRows = toArr(sp.history).filter((h) => h.settled > 0).slice(0, 10);
  return H + `
    <div class="res-intro-b" style="margin:4px 0 8px">这一栏是「自我进化」的<b>成果</b>：
      不看别人推荐了什么，看<b>系统按自己学出的权重、和自己长出的规则，选出了什么</b>。
      当期 ${main.length} 只自选 + ${own.length} 只「系统自己发现」。权重来源：<b>${esc(sp.weight_src || "—")}</b>。</div>
    <div class="res-sp-stat">
      <span>系统自选已结算 <b>${esc(m.settled || 0)}</b> 条：平均 <b class="${cl(m.avg_ret5)}">${nm(m.avg_ret5)}</b>
        ｜超额 <b class="${cl(m.avg_ex5)}">${nm(m.avg_ex5)}</b>
        ｜胜率 <b>${m.win5 == null ? "—" : (m.win5 * 100).toFixed(1) + "%"}</b></span>
      ${hi.settled ? `<span class="res-src-note">其中历史回算 ${esc(hi.settled)} 条：超额 ${nm(hi.avg_ex5)}｜胜率 ${hi.win5 == null ? "—" : (hi.win5 * 100).toFixed(1) + "%"}</span>` : ""}
    </div>
    ${toArr(sp.tier_stats).filter((x) => x && x.days > 0).length
      ? `<div class="res-evo-h" style="margin-top:12px">分档实测 —— 按交易日聚合，不是按票</div>
      <div class="res-intro-b" style="margin:4px 0 8px">同一期推 15~20 只票、T+5 窗口完全重叠，
        按票求均值会把有效样本量放大 20 倍，所以这里按<b>交易日</b>聚合并给 t 值。
        <b>天数不足 20 的档，它的均值不能拿来改算法方向</b>（这正是「A 档该不该反过来用」的判据）。</div>
      <div class="res-tb-wrap"><table class="res-tb">
        <thead><tr><th>档</th><th>已结算(只)</th><th>交易日</th><th>按日超额</th><th>t</th><th>日胜率</th><th>能不能下结论</th></tr></thead>
        <tbody>${toArr(sp.tier_stats).filter((x) => x && x.days > 0).map((x) => `<tr>
          <td><b>${esc(x.tier)}</b></td><td>${esc(x.n)}</td><td>${esc(x.days)}</td>
          <td class="${cl(x.avg_ex5)}">${nm(x.avg_ex5)}</td>
          <td>${x.t == null ? "—" : x.t.toFixed(2)}</td>
          <td>${x.win == null ? "—" : x.win.toFixed(1) + "%"}</td>
          <td>${x.days < 20 ? "样本不足（" + x.days + " 日）"
            : (Math.abs(x.t || 0) >= 2 ? "可参考" : "不显著")}</td>
        </tr>`).join("")}</tbody></table></div>` : ""}
    <div class="res-grid">${main.map(card).join("")}</div>
    ${own.length ? `<div class="res-evo-h" style="margin-top:12px">★ 系统自己发现 —— 博主声量 0，纯靠战法与规则挖出来的</div>
      <div class="res-intro-b" style="margin:4px 0 8px">这批票<b>没有任何博主在说</b>——是「环境 × 战法 × 规则」自己筛出来的，
        也是「不模仿、长出自己的东西」最直接的证据。（按综合分排序它们挤不进主榜，故单列成榜。）</div>
      <div class="res-grid">${own.map(card).join("")}</div>` : ""}
    ${histRows.length ? `<div class="res-evo-h" style="margin-top:12px">近 ${histRows.length} 期战绩（按真实走势回填）</div>
      <div class="res-tb-wrap"><table class="res-tb">
        <thead><tr><th>期次</th><th>类型</th><th>已结算</th><th>平均涨跌</th><th>超额</th><th>胜率</th></tr></thead>
        <tbody>${histRows.map((h) => `<tr>
          <td>${esc(h.date)}</td><td>${h.hist ? "历史回算" : "实时"}</td>
          <td>${esc(h.settled)}/${esc(h.n)}</td>
          <td class="${cl(h.avg_ret5)}">${nm(h.avg_ret5)}</td>
          <td class="${cl(h.avg_ex5)}">${nm(h.avg_ex5)}</td>
          <td>${h.win5 == null ? "—" : (h.win5 * 100).toFixed(1) + "%"}</td>
        </tr>`).join("")}</tbody></table></div>` : ""}
    ${toArr(sp.caliber).length ? `<details class="res-cal"><summary>系统自选的口径（${toArr(sp.caliber).length} 条，全部可追溯）</summary>
      <ul>${toArr(sp.caliber).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}`;
}
function resEvolveView(d) {
  if (!d || d.ok === false || d._err) {
    return `${resSelfPickView()}
      <div class="res-intro" style="margin-top:14px">
        <div class="res-intro-h">自我进化 · 从「汇聚别人的」到「长出自己的」</div>
        <div class="res-intro-b">进化引擎尚未生成（等每日调度，或直接点下面按钮）。</div>
      </div>
      <div style="margin-bottom:8px"><button class="btn primary" id="evoRegen" onclick="resEvoRun()">立即进化</button></div>
      <div class="hint">若刚更新过界面：这是新支柱，需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启，光刷新页面不够）。</div>`;
  }
  const w = d.weight || {}, wl = w.w || {}, wd = w.w_default || {}, ic = w.ic || {};
  const jr = toArr(d.journal);
  const j0 = jr[0] || {};
  const jrn = j0.learned
    ? `<div class="res-jrn"><b>最新一期进化（${esc(j0.log_date || "")}）</b><br>${esc(j0.learned)}</div>` : "";
  const wBlock = `<div class="res-evo-h">① 权重自学习 —— 共振分权重不再由人拍板</div>
    ${["blogger", "strategy", "senti", "engine"].map((k) => resWBar(k, wl[k], wd[k])).join("")}
    <div class="res-src-note" style="margin:6px 0 4px">${esc(w.note || "")}</div>
    <div class="res-src-note">IC（信息系数＝该维度取值与未来 T+5 超额 的相关系数，样本 ${esc(w.n_rows || 0)} 行）：
      ${Object.keys(ic).map((k) => `${esc(RES_DIML[k] || k)} <b>${ic[k] === null ? "样本不足" : esc(ic[k])}</b>`).join("　")}
      —— <b>负值意味着「越高反而越差」</b>；龙虎榜只做诊断，不进权重。</div>`;
  const st = w.by_state || {};
  const stBlock = Object.keys(st).length
    ? `<div class="res-evo-h">② 分市场环境学出的权重（不同环境下该信谁）</div>
      ${Object.keys(st).map((k) => `<div class="res-src-row">
        <span class="badge ${k === "结构" ? "bull" : (k === "普跌" ? "bear" : "neu")}">${esc(k)}</span>
        <div class="res-src-main"><div class="res-src-n">样本 ${esc(st[k].n || 0)} 条</div>
          <div class="res-src-note">${["blogger", "strategy", "senti", "engine"].map((x) =>
            `${esc(RES_DIML[x])} ${(((st[k].w || {})[x] || 0) * 100).toFixed(1)}%`).join("　")}</div></div>
      </div>`).join("")}` : "";
  const rules = toArr(d.rules);
  const ruleTb = rules.length
    ? `<div class="res-evo-h">③ 自生长规则（数据里自己长出来的，不是谁说的）</div>
      <div class="res-tb-wrap"><table class="res-tb">
        <thead><tr><th>状态</th><th>条件组合</th><th>样本</th><th>胜率</th>
          <th>平均超额</th><th>基线</th><th>提升</th><th>样本外</th></tr></thead>
        <tbody>${rules.map((r) => `<tr>
          <td><span class="badge ${r.status === "已采纳" ? "bull" : "neu"}">${esc(r.status)}</span></td>
          <td class="res-td-name">${esc(r.cond_text)}</td>
          <td>${esc(r.n)}</td><td>${((r.win_rate || 0) * 100).toFixed(1)}%</td>
          <td>${resNum(r.avg_ex, "%", 2)}</td><td>${resNum(r.baseline, "%", 2)}</td>
          <td><b>${resNum(r.lift, "%", 2)}</b></td>
          <td>${r.oos_n ? `${esc(r.oos_n)} 条 / ${resNum(r.oos_avg_ex, "%", 2)}` : "—"}</td>
        </tr>`).join("")}</tbody></table></div>` : "";
  const kn = toArr(d.knowledge);
  const knBlock = kn.length
    ? `<div class="res-evo-h">④ 知识内化 —— 我们自己的方法论条目（含数据证据）</div>
      ${kn.map((k) => `<div class="res-kcard">
        <div class="res-kt">${esc(k.title)}<span class="res-kb-src">${esc(k.kind)} · ${esc(k.status)}</span></div>
        <div class="res-kb">${esc(k.body || "")}</div>
        <div class="res-ke">证据：${esc(k.evidence || "")}｜来源：${esc(k.sources || "")}</div>
      </div>`).join("")}` : "";
  const tr = toArr(d.trends);
  const trUp = tr.filter((x) => x.trend === "上升").slice(0, 6);
  const trDn = tr.filter((x) => x.trend === "衰减").slice(0, 6);
  const trLine = (x) => `<div class="res-src-row">
    <span class="badge ${x.trend === "上升" ? "bull" : "bear"}">${esc(x.trend)}</span>
    <div class="res-src-main"><div class="res-src-n">${esc(x.src_key)}
      <span class="res-src-nn">${esc(x.src_type)}｜全期 ${resNum(x.ex_all, "%", 2)} → 近 20 日 ${resNum(x.ex_recent, "%", 2)}</span></div></div>
    <div class="res-src-m"><span class="res-src-k">变化</span><b>${resNum(x.delta, "", 2)}</b></div>
  </div>`;
  const trBlock = (trUp.length || trDn.length)
    ? `<div class="res-evo-h">⑤ 信源趋势 —— 谁在进步、谁在退步</div>
      ${trUp.map(trLine).join("")}${trDn.map(trLine).join("")}` : "";
  const cal = toArr(d.caliber);
  const calBlock = cal.length
    ? `<details class="res-cal"><summary>自我进化的口径（${cal.length} 条，全部可追溯）</summary>
      <ul>${cal.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "";
  return `${resSelfPickView()}
    <div class="res-intro" style="margin-top:14px">
      <div class="res-intro-h">进化过程 · 这些权重和规则是怎么长出来的</div>
      <div class="res-intro-b">这一栏不看别人说了什么，看<b>系统自己长出了什么</b>：
        权重由数据学出（不再是 34/34/14/18 的人为设定）、规则从数据里自己生长、
        学完<b>当天就反哺共振卡</b>（不是写进表里放着）。当前 ${rules.length} 条规则、
        ${kn.length} 条内化知识、${tr.length} 个信源趋势。</div>
      <div style="margin-top:8px"><button class="btn primary" id="evoRegen" onclick="resEvoRun()">立即进化</button></div>
    </div>
    ${jrn}${wBlock}${stBlock}${ruleTb}${knBlock}${trBlock}${calBlock}`;
}
/* ---------- 避雷名单 AVOID（2026-10-21 dy9，进哥拍板） ----------
 * 定位：**只做排除，不做推荐**。
 * 依据（40747 行面板，D+1 买入持 5 日、超额基准=全市场等权中位数）：
 *   · C 级（低分）实际 T+5 超额 -4.51%，全样本 +1.93%  → 低分卡避雷价值真实
 *   · S 级（最高分）实际 -0.65%                        → 头部无正向 alpha
 * 所以本面板不许出现「买入」「关注」「推荐」等字样，只呈现「为什么该避」。
 * 数据来自 /api/avoid（后端 resonance.avoid_payload 产出，前端只做呈现）。
 */
/* 状态对象 AVOID 已前置声明在文件上方（避雷跟随标记那一节）—— 见那里关于 TDZ 的说明。
   此处不要再写 const AVOID，否则重复声明会直接 SyntaxError。 */
function loadAvoid(force) {
  const box = $("#p-avoid");
  if (box && !AVOID.data) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">避雷名单</div></div>
      <div class="empty">正在统计低分卡归因…</div>`;
  }
  // 已有缓存且非强制刷新：直接渲染（开站预拉的 loadAvoidSet 已填好 AVOID.data 与 AVOID_SET）
  if (AVOID.data && !force) {
    renderAvoid();
    return Promise.resolve(AVOID.data);
  }
  AVOID.busy = true;
  const qs = force ? "?t=" + Date.now() : "";
  return api("/api/avoid" + qs)
    .catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => {
      AVOID.data = d; AVOID.busy = false;
      buildAvoidSet(d);                       // 同步给全站风险标记用
      setTimeout(() => { try { avoidDecorate(document); } catch (_) {} }, 30);
      renderAvoid();
    });
}
// ══════════════════════════════════════════════════════════════════════
// 大势研判 · 局（2026-10-31 进哥需求）
// 定位：**不预测指数点位**。实测（2019-2026，1600+ 交易日）涨停家数/炸板率/
// 连板断档对指数方向均无预测力（t 全 <1.6），装成一个"预测"是骗人。
// 本面板只回答两件能站住的事：
//   ① 这一局在哪个阶段 —— 用涨停/跌停/炸板率/连板高度的历史分位定位；
//   ② 今天什么动作必亏 —— 连板开板=派发，T+5 超额 −6.13%（t=−10.68，n=896）。
// 数据来自 /api/harvest（后端 harvest.py 读 data/kline_daily.db 增量沉淀）。
// ══════════════════════════════════════════════════════════════════════
const _HE = (t) => String(t == null ? "" : t).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 局面 → 颜色。注意：这是**风险语义**（红=危险），不是涨跌语义。
const _HPHASE_COLOR = {
  "杀跌": "#dc2626", "派发": "#dc2626", "过热": "#f59e0b",
  "健康": "#16a34a", "拉锯": "#64748b",
};

// ---------------------------------------------------------------- 开源情报（2026-10-30）
//
// 这个面板回答的不是「买什么」，而是「一个听起来很有道理的说法，在我们自己的
// 数据上到底成不成立」。所以排布顺序是：先说样本边界 → 再说结论 → 最后才是仓库。
// 全篇不出现「推荐」：这里只有证伪，没有推荐。
const _OSS_STATUS = {
  active: ["有效", "#16a34a"],
  refuted: ["已推翻", "#dc2626"],
  unstable: ["不稳定", "#f59e0b"],
  marginal: ["显著但无利", "#64748b"],
  unsupported: ["未测出", "#94a3b8"],
  invalid: ["样本不足", "#94a3b8"],
};

// 超额着色走涨跌语义（正=红、负=绿，中国习惯），与状态标签的「红=危险」分开，
// 避免同一个颜色在两张表里表示两件事。
function _ossExColor(v) {
  const n = parseFloat(v);
  if (!isFinite(n) || n === 0) return "var(--fg,#111)";
  return n > 0 ? "#dc2626" : "#16a34a";
}

// 统一小数位：同一列里 -1 / -2.47 / +0.036 混排会显得脏，且容易被读成不同精度。
function _ossNum(v, d) {
  if (v === null || v === undefined || v === "" || !isFinite(parseFloat(v))) return "—";
  return Number(v).toFixed(d === undefined ? 2 : d);
}

function loadSelection(force) {
  const box = $("#p-selection");
  if (box && !SELECT.data) {
    box.innerHTML = '<div class="pred-head"><div class="pred-title">选股方向 · 实测结论</div></div>'
      + '<div class="empty">正在读取回测结果…</div>';
  }
  if (SELECT.data && !force) { renderSelection(); return Promise.resolve(SELECT.data); }
  SELECT.busy = true;
  return api("/api/selection" + (force ? "?t=" + Date.now() : ""))
    .catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => { SELECT.data = d; SELECT.busy = false; renderSelection(); });
}

// 超额配色：负=红（策略失效）、正=绿。这是「策略质量」语义，不是涨跌语义，
// 与开源情报面板保持同一套色彩语言，避免同一屏出现两套红绿含义。
function _selEx(v) {
  if (v === null || v === undefined || isNaN(v)) return "var(--muted,#6b7280)";
  return v > 0 ? "#16a34a" : (v < 0 ? "#dc2626" : "var(--fg,#111)");
}
function _selN(v, d) {
  if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
  return Number(v).toFixed(d === undefined ? 2 : d);
}
function _selPct(v, d) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return (Number(v) * 100).toFixed(d === undefined ? 1 : d) + "%";
}
function _selCard(label, text, color) {
  return '<div style="flex:1 1 320px;border-left:3px solid ' + color + ';'
    + 'background:var(--card,#fff);border-radius:6px;padding:10px 12px">'
    + '<div style="font-size:var(--fs-2xs,10px);color:' + color + ';font-weight:600;'
    + 'letter-spacing:.04em">' + _HE(label) + '</div>'
    + '<div style="font-size:var(--fs-xs,11px);line-height:1.6;margin-top:4px;'
    + 'color:var(--fg,#111)">' + _HE(text) + '</div></div>';
}

function selTogglePool() {
  SELECT.expandPool = !SELECT.expandPool;
  renderSelection();
}

function renderSelection() {
  const box = $("#p-selection");
  if (!box) return;
  const d = SELECT.data;
  const head = '<div class="pred-head"><div class="pred-title">选股方向 · 实测结论</div>'
    + '<div class="pred-sub">短线与中长期两个方向都在全量日线上跑过；结论与直觉不一致，如实呈现</div></div>';
  if (!d || d._err || !d.ok) {
    box.innerHTML = head + '<div class="empty">读取失败：'
      + _HE((d && d.error) || "未知错误") + '</div>';
    return;
  }
  const H = [head];
  const vd = d.verdict || {};
  H.push('<div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 4px">');
  H.push(_selCard("短线 T+1 的结论", vd.short || "—", "#dc2626"));
  H.push(_selCard("中长期持有的结论", vd.hold || "—", "#16a34a"));
  H.push('</div>');

  // ① 短线：全部负超额
  const shorts = (d.short || []).filter((x) => x.excess !== null && x.excess !== undefined);
  if (shorts.length) {
    H.push('<div style="font-size:var(--fs-sm,12px);font-weight:600;margin:18px 0 4px">'
      + '① 短线（今天买明天卖）：所有形态都是负超额</div>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'line-height:1.7;margin-bottom:8px">'
      + '口径：d 日收盘出信号 → d+1 开盘买入 → d+2 开盘卖出（T+1 制度下限）；'
      + '超额 = 个股收益 − 同日全市场等权；d+1 开盘即涨停判为买不进，已剔除。'
      + '样本 5,573 只、2,505 个交易日。负超额意味着：这样买还不如当天随便买一篮子。</div>');
    H.push('<table style="width:100%;border-collapse:collapse;font-size:var(--fs-xs,11px)">');
    H.push('<tr style="color:var(--muted,#6b7280);text-align:left">'
      + '<th style="padding:6px 8px;font-weight:500">形态</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">样本数</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">超额</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">t 值</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">正超额日占比</th></tr>');
    shorts.forEach((x) => {
      H.push('<tr style="border-bottom:1px solid var(--line,#e5e7eb)">'
        + '<td style="padding:7px 8px">' + _HE(x.title) + '</td>'
        + '<td style="padding:7px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _HE(x.n) + '</td>'
        + '<td style="padding:7px 8px;text-align:right;font-weight:600;color:'
        + _selEx(x.excess) + '">' + (x.excess > 0 ? "+" : "") + _selN(x.excess, 2) + '%</td>'
        + '<td style="padding:7px 8px;text-align:right;color:' + _selEx(x.excess) + '">'
        + _selN(x.t, 2) + '</td>'
        + '<td style="padding:7px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _selN(x.win, 1) + '%</td></tr>');
    });
    H.push('</table>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'margin-top:6px">t 值在 −8 到 −17 之间，不是运气问题。'
      + '「正超额日占比」= 按日聚合后，该形态跑赢当日全市场的交易日占比，'
      + '不是个股赚钱比例。这条数据只能用来判断「不该买」，反过来当买入信号是错的。</div>');
  }

  // ② 中长期：正反对照
  const hold = d.hold || [];
  if (hold.length) {
    H.push('<div style="font-size:var(--fs-sm,12px);font-weight:600;margin:20px 0 4px">'
      + '② 中长期（持有约 2 年）：正反对照</div>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'line-height:1.7;margin-bottom:8px">'
      + '同样口径、同一批样本，只改「底部形态」的定义。'
      + '「曾活跃后沉寂」就是平时说的「底部横盘蓄势待爆发」——它在四组里最差。</div>');
    H.push('<table style="width:100%;border-collapse:collapse;font-size:var(--fs-xs,11px)">');
    H.push('<tr style="color:var(--muted,#6b7280);text-align:left">'
      + '<th style="padding:6px 8px;font-weight:500">形态</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">样本数</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">2年期末</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">超额</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">胜率</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">最大回撤</th></tr>');
    hold.forEach((x) => {
      const trap = x.key === "trap";
      const base = x.key === "hold_all";
      const bg = trap ? "background:rgba(220,38,38,.06);" : "";
      H.push('<tr style="border-bottom:1px solid var(--line,#e5e7eb);' + bg + '">'
        + '<td style="padding:7px 8px;' + (trap ? "font-weight:600;" : "")
        + (base ? "color:var(--muted,#6b7280);" : "") + '">'
        + _HE(x.title) + (trap ? ' <span style="color:#dc2626;font-size:var(--fs-2xs,10px)">← 最差</span>' : "")
        + '</td>'
        + '<td style="padding:7px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _HE(x.n) + '</td>'
        + '<td style="padding:7px 8px;text-align:right;font-weight:600;color:'
        + ((x.final_ret || 0) >= 0 ? "#16a34a" : "#dc2626") + '">'
        + ((x.final_ret || 0) > 0 ? "+" : "") + _selN(x.final_ret, 1) + '%</td>'
        + '<td style="padding:7px 8px;text-align:right;color:' + _selEx(x.excess) + '">'
        + ((x.excess || 0) > 0 ? "+" : "") + _selN(x.excess, 1) + '%</td>'
        + '<td style="padding:7px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _selN(x.win, 1) + '%</td>'
        + '<td style="padding:7px 8px;text-align:right;color:#dc2626">'
        + _selN(x.mdd, 1) + '%</td></tr>');
    });
    H.push('</table>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'margin-top:6px">注意：低波组赢在回撤和胜率，不是涨幅。'
      + '它的 2 年「曾涨≥100%」概率反而低于全市场（19.3% vs 23.2%）——'
      + '想要暴涨的形态，技术面上没有。</div>');
  }

  // ③ 今日低波池
  const st = d.stats || {};
  const pool = d.pool || [];
  H.push('<div style="font-size:var(--fs-sm,12px);font-weight:600;margin:20px 0 4px">'
    + '③ 今日低波池（' + _HE(st.pool || 0) + ' 只）</div>');
  if (!pool.length) {
    H.push('<div class="empty">今日池子为空（可能今日日线尚未补齐）</div>');
  } else {
    const _st = d.stats || {};
    const _cand = _st.candidates || _st.pool || 0;
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'line-height:1.7;margin-bottom:8px">'
      + '入池条件：前 250 日与近 250 日振幅都 ≤45%（即「一直低波」），'
      + '成交额 ≥1000 万，上市满 520 个交易日，已剔 ST 与北交所。A 级 ≤25%、B 级 ≤32%、C 级 ≤45%。'
      + '这是低回撤候选池，不是买入信号，也不预测时点。</div>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'line-height:1.7;margin-bottom:8px">'
      + '覆盖范围：全市场 5500+ 只 → 剔 ST/北交所/新股后约 5000 只全量扫描 → '
      + '满足条件 <b>' + _HE(_cand) + '</b> 只，下方表格可展开查看全部'
      + (_st.capped ? '（注意：达到上限只存了 ' + _HE(_st.pool) + ' 只）' : '') + '。'
      + '不是从几百只里挑的。</div>');
    H.push('<div style="font-size:var(--fs-2xs,10px);color:#a16207;'
      + 'line-height:1.7;margin-bottom:8px">'
      + '池子里会大量出现银行、公用事业、高速铁路——这不是筛选出错，'
      + '而是「一直低波」这个条件的必然结果：它们正是历史上胜率最高、回撤最小的那一类。'
      + '想要弹性，请自己往 B / C 级看，但胜率与回撤也会同步变差。</div>');
    H.push('<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;font-size:var(--fs-2xs,10px)">'
      + '<span style="color:#16a34a">A 级 ' + _HE(st.A || 0) + ' 只</span>'
      + '<span style="color:#2563eb">B 级 ' + _HE(st.B || 0) + ' 只</span>'
      + '<span style="color:var(--muted,#6b7280)">C 级 ' + _HE(st.C || 0) + ' 只</span>'
      + '<span style="color:var(--muted,#6b7280)">· 数据日 ' + _HE(d.asof || "—") + '</span></div>');
    H.push('<table style="width:100%;border-collapse:collapse;font-size:var(--fs-xs,11px)">');
    H.push('<tr style="color:var(--muted,#6b7280);text-align:left">'
      + '<th style="padding:6px 8px;font-weight:500">股票</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:center">级</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">250日振幅</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">区间位置</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">近1年</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">成交额</th>'
      + '<th style="padding:6px 8px;font-weight:500;text-align:right">ROE</th></tr>');
    // 2026-10-30：原写死 slice(0,40)，但上面文案却写「本页全部列出」——虚假披露。
    // 改为默认 60 行 + 「展开全部」，展开后如实渲染全部（600 行表格浏览器扛得住）。
    const _shown = SELECT.expandPool ? pool : pool.slice(0, 60);
    _shown.forEach((x) => {      const tc = x.tier === "A" ? "#16a34a" : (x.tier === "B" ? "#2563eb" : "#94a3b8");
      H.push('<tr style="border-bottom:1px solid var(--line,#e5e7eb)">'
        + '<td style="padding:6px 8px"><b>' + _HE(x.code) + '</b> '
        + '<span style="color:var(--muted,#6b7280)">' + _HE(x.name) + '</span></td>'
        + '<td style="padding:6px 8px;text-align:center;font-weight:600;color:' + tc + '">'
        + _HE(x.tier) + '</td>'
        + '<td style="padding:6px 8px;text-align:right">' + _selPct(x.amp250, 1) + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _selPct(x.pos250, 0) + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:'
        + ((x.chg_1y || 0) >= 0 ? "#dc2626" : "#16a34a") + '">'
        + ((x.chg_1y || 0) > 0 ? "+" : "") + _selPct(x.chg_1y, 1) + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + _selN((x.amt || 0) / 1e8, 2) + '亿</td>'
        + '<td style="padding:6px 8px;text-align:right;color:var(--muted,#6b7280)">'
        + (x.roe === null || x.roe === undefined ? "—" : _selN(x.roe, 1)) + '</td></tr>');
    });
    H.push('</table>');
    if (pool.length > 60) {
      H.push('<div style="margin-top:8px;font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280)">'
        + (SELECT.expandPool
          ? '已展开全部 ' + pool.length + ' 只　'
            + '<a href="javascript:void(0)" onclick="selTogglePool()" style="color:#2563eb">收起</a>'
          : '当前显示前 ' + _shown.length + ' 只（按振幅由窄到宽），共 ' + pool.length + ' 只　'
            + '<a href="javascript:void(0)" onclick="selTogglePool()" style="color:#2563eb">展开全部 ' + pool.length + ' 只</a>')
        + '</div>');
    }
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);margin-top:6px">'
      + '近1年涨跌幅按 A 股习惯：红=上涨、绿=下跌。'
      + 'ROE 取自最新报告期快照（2026-10-30 已补齐全市场 5055 只），'
      + '显示「—」表示该股无该期数据，不是亏损。</div>');
  }

  // ④ 口径边界
  if ((d.caveats || []).length) {
    H.push('<div style="margin-top:18px;padding:10px 12px;border:1px solid var(--line,#e5e7eb);'
      + 'border-radius:6px;background:rgba(234,179,8,.06)">'
      + '<div style="font-size:var(--fs-2xs,10px);font-weight:600;color:#a16207;'
      + 'margin-bottom:5px">口径边界（结论必须带着这些一起读）</div>');
    d.caveats.forEach((c) => {
      H.push('<div style="font-size:var(--fs-xs,11px);color:#57534e;'
        + 'line-height:1.8">· ' + _HE(c) + '</div>');
    });
    H.push('</div>');
  }
  if (d.tested_at) {
    H.push('<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);'
      + 'margin-top:10px">回测执行时间 ' + _HE(d.tested_at) + '　·　池子每日 16:20 更新</div>');
  }
  box.innerHTML = H.join("");
}

function loadOSS(force) {
  const box = $("#p-oss");
  if (box && !OSS.data) {
    box.innerHTML = '<div class="pred-head"><div class="pred-title">开源情报 · 规则校验</div></div>'
      + '<div class="empty">正在读取回测结果…</div>';
  }
  if (OSS.data && !force) { renderOSS(); return Promise.resolve(OSS.data); }
  OSS.busy = true;
  return api("/api/oss" + (force ? "?t=" + Date.now() : ""))
    .catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => { OSS.data = d; OSS.busy = false; renderOSS(); });
}

function renderOSS() {
  const box = $("#p-oss");
  if (!box) return;
  const d = OSS.data;
  const head = '<div class="pred-head"><div class="pred-title">开源情报 · 规则校验</div>'
    + '<div class="pred-sub">GitHub 上的量化说法 → 本系统严格口径重测（只抽规则，不抄代码）</div></div>';
  if (!d) { box.innerHTML = head + '<div class="empty">加载中…</div>'; return; }
  if (!d.ok) { box.innerHTML = head + '<div class="empty">' + _HE(d.error || "数据不可用") + '</div>'; return; }

  if (!d.available) {
    box.innerHTML = head + '<div class="empty">' + _HE(d.reason || "尚未跑过回测") + '</div>';
    return;
  }

  const st = d.stats || {}, sp = d.data_span || {};
  const H = [];

  // ① 样本边界：必须放在结论前面。缺少这一句，下面的数字就会被当成长期结论。
  //    2026-10-31：sp 现在是**规则实测窗口**（oss_rule.days 反推），不是指标表全跨度；
  //    两者不同（规则只跑最近 lookback 个交易日，为了单条 2 分钟的可调度性），必须分开说。
  if (sp.days) {
    const _yrs = (sp.days / 244).toFixed(1);
    const _old = Number(sp.days) < 600;
    const _tb = sp.table_span || {};
    H.push('<div style="border-left:3px solid #f59e0b;background:rgba(245,158,11,.08);'
      + 'padding:9px 12px;border-radius:6px;margin-bottom:12px;font-size:var(--fs-xs,11px);'
      + 'color:var(--muted,#6b7280);line-height:1.6">'
      + '规则实测窗口：<b style="color:var(--fg,#111)">' + _HE(sp.start) + ' ~ ' + _HE(sp.end) + '</b>'
      + '（' + _HE(sp.days) + ' 个交易日，约 ' + _yrs + ' 年）'
      + (_old
        ? '，<b style="color:var(--fg,#111)">未覆盖完整牛熊</b>。'
        : '，已覆盖多轮牛熊。')
      + (_tb.days
        ? '指标表全跨度 ' + _HE(_tb.start) + ' ~ ' + _HE(_tb.end) + '（' + _HE(_tb.days)
          + ' 个交易日）—— 规则只跑最近窗口，不要读成「结论用了全部历史」。'
        : '')
      + '标注「长样本」的两条另用 2020 年以来全量日线复核过。</div>');
  }

  // ② 规模
  const cards = [
    ["候选仓库", st.repos || 0, "var(--fg,#111)"],
    ["已测规则", st.rules || 0, "var(--fg,#111)"],
    // 0 个有效不能用绿色（绿色读作「良好」），这里是中性事实
    ["有效", st.active || 0, (st.active || 0) > 0 ? "#16a34a" : "#94a3b8"],
    ["已推翻", st.refuted || 0, (st.refuted || 0) > 0 ? "#dc2626" : "var(--fg,#111)"],
  ];
  H.push('<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">');
  cards.forEach(([k, v, c]) => {
    H.push('<div style="flex:1;min-width:105px;border:1px solid var(--line,#e5e7eb);border-radius:10px;'
      + 'padding:11px 13px;background:var(--panel,#fff)">'
      + '<div style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280);margin-bottom:5px">' + k + '</div>'
      + '<div style="font-size:var(--fs-xl,22px);font-weight:700;color:' + c + ';line-height:1.1">' + v + '</div>'
      + '</div>');
  });
  H.push('</div>');

  // 「有效 0 条」必须解释，否则会被读成「系统坏了」。
  // 事实相反：11 条开源流行说法里 0 条能进规则，这个零就是本轮最有价值的结论。
  if ((st.rules || 0) > 0 && (st.active || 0) === 0) {
    H.push('<div style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280);'
      + 'margin:-4px 0 14px;line-height:1.7">'
      + (st.rules || 0) + ' 条里 0 条有效 —— <b style="color:var(--fg,#111)">不是没跑通</b>，'
      + '而是这些流行说法在本市场、本口径下确实不成立，或幅度不够覆盖交易成本。'
      + '这个「零」本身就是结论：市面上讲得最响的那套技术面理由，多数经不起可执行口径的检验。</div>');
  }
  const hl = d.headline;
  if (hl) {
    H.push('<div style="border:1px solid rgba(220,38,38,.35);background:rgba(220,38,38,.06);'
      + 'border-radius:10px;padding:13px 15px;margin-bottom:14px">'
      + '<div style="font-size:var(--fs-xs,11px);color:#dc2626;font-weight:600;margin-bottom:6px">'
      + '最值得注意的一条（与流行说法相反）</div>'
      + '<div style="font-size:var(--fs-md,15px);font-weight:600;margin-bottom:5px">'
      + _HE(hl.title) + ' · ' + _HE(hl.repo || '开源') + '</div>'
      + '<div style="font-size:var(--fs-sm,12px);color:var(--muted,#6b7280);line-height:1.7">'
      + _HE(hl.rule) + ' → T+5 超额 <b style="color:' + _ossExColor(hl.excess) + '">'
      + (hl.excess > 0 ? '+' : '') + _ossNum(hl.excess) + '%</b>，t=' + _ossNum(hl.t) + '</div></div>');
  }

  // ④ 规则表
  H.push('<div style="font-size:var(--fs-sm,12px);font-weight:600;margin:0 0 8px">全部规则（按状态排序）</div>');
  H.push('<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;'
    + 'font-size:var(--fs-sm,12px)"><thead><tr style="color:var(--muted,#6b7280);'
    + 'font-weight:400;border-bottom:1px solid var(--line,#e5e7eb)">'
    + '<th style="text-align:left;padding:7px 8px">规则</th>'
    + '<th style="text-align:left;padding:7px 8px">来源</th>'
    + '<th style="text-align:right;padding:7px 8px">T+5 超额</th>'
    + '<th style="text-align:right;padding:7px 8px">t</th>'
    + '<th style="text-align:right;padding:7px 8px">长样本</th>'
    + '<th style="text-align:center;padding:7px 8px">状态</th></tr></thead><tbody>');

  (d.rules || []).forEach((r) => {
    const sm = _OSS_STATUS[r.status] || ["未知", "#94a3b8"];
    const cross = (r.cross_days && r.cross_excess !== null && r.cross_excess !== undefined)
      ? ('<span style="color:' + _ossExColor(r.cross_excess) + '">'
        + (r.cross_excess > 0 ? '+' : '') + _ossNum(r.cross_excess) + '%</span>'
        + '<span style="color:var(--muted,#6b7280);font-size:var(--fs-2xs,10px)"> / t=' + _ossNum(r.cross_t)
        + (r.cross_same ? ' 同向' : ' <b style="color:#dc2626">反向</b>') + '</span>')
      : '<span style="color:var(--muted,#6b7280)">—</span>';
    H.push('<tr style="border-bottom:1px solid var(--line,#e5e7eb)">'
      + '<td style="padding:8px;line-height:1.5"><div style="font-weight:600">' + _HE(r.title) + '</div>'
      + '<div style="color:var(--muted,#6b7280);font-size:var(--fs-2xs,10px)">'
      + _HE(r.predicate) + ' · n=' + _HE(r.n) + '/' + _HE(r.days) + '日</div></td>'
      + '<td style="padding:8px;color:var(--muted,#6b7280);font-size:var(--fs-2xs,10px)">'
      + _HE(r.repo || '开源') + '</td>'
      + '<td style="padding:8px;text-align:right;font-weight:600;color:' + _ossExColor(r.excess) + '">'
      + (r.excess > 0 ? '+' : '') + _ossNum(r.excess) + '%</td>'
      + '<td style="padding:8px;text-align:right">' + _ossNum(r.t) + '</td>'
      + '<td style="padding:8px;text-align:right">' + cross + '</td>'
      + '<td style="padding:8px;text-align:center"><span style="color:' + sm[1]
      + ';border:1px solid ' + sm[1] + '33;border-radius:20px;padding:2px 9px;white-space:nowrap;'
      + 'font-size:var(--fs-2xs,10px)">' + sm[0] + '</span></td></tr>');
  });
  H.push('</tbody></table></div>');

  // ⑤ 仓库清单（发现留痕，避免同一个仓库被反复挖）
  //   只展示 A/B 两类：C 类是明确不采用的（海外标的、纯数据源），列出来只是噪音。
  const repos = (d.repos || []).filter((x) => x.bucket === "A" || x.bucket === "B")
    .sort((a, b) => ((a.bucket === "A" ? 0 : 1) - (b.bucket === "A" ? 0 : 1))
      || ((b.stars || 0) - (a.stars || 0)));
  const nC = (d.repos || []).filter((x) => x.bucket === "C").length;
  const nQ = (d.repos || []).filter((x) => !x.bucket || x.bucket === "?").length;
  if (repos.length) {
    H.push('<div style="font-size:var(--fs-sm,12px);font-weight:600;margin:16px 0 8px">'
      + '已挖仓库（A=规则可回测 · B=结构可借鉴）'
      + '<span style="font-weight:400;color:var(--muted,#6b7280)">　共 ' + (d.stats.repos || 0)
      + ' 个候选，' + (d.stats.active || 0) + ' 条规则有效</span></div>');
    H.push('<div style="display:flex;flex-wrap:wrap;gap:7px">');
    repos.forEach((x) => {
      const bc = x.bucket === "A" ? "#16a34a" : "#64748b";
      H.push('<span title="' + _HE(x.why || x.descr || '') + '" style="border:1px solid var(--line,#e5e7eb);'
        + 'border-radius:6px;padding:4px 9px;font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280)">'
        + '<b style="color:' + bc + '">' + _HE(x.bucket) + '</b> ' + _HE(x.full_name)
        + ' <span style="color:#94a3b8">★' + _HE(x.stars) + '</span></span>');
    });
    H.push('</div>');
    if (nC || nQ) {
      H.push('<div style="margin-top:7px;font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280)">'
        + [nC ? 'C 类 ' + nC + ' 个（海外标的/纯数据源，不采用）' : "",
           nQ ? '待分级 ' + nQ + ' 个（扫描到但还没判断）' : ""]
          .filter(Boolean).join('　·　') + '</div>');
    }
  }

  H.push('<div style="margin-top:14px;font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);line-height:1.7">'
    + '口径与全站一致：事件日盘后知情 → 次日收盘建仓 → T+5 收盘平仓，超额减同日全市场等权，t 按日聚合。'
    + '「显著」≠「可用」：|超额| &lt;0.3% 即使 t 再大也归入「显著但无利」。'
    + (d.tested_at ? ' 最后回测：' + _HE(d.tested_at) : '')
    + '</div>');

  box.innerHTML = head + H.join('');
}

function loadHarvest(force) {
  const box = $("#p-harvest");
  if (box && !HARVEST.data) {
    box.innerHTML = '<div class="pred-head"><div class="pred-title">大势研判 · 局</div></div>'
      + '<div class="empty">正在读取盘面结构（涨停/炸板/连板）…</div>';
  }
  if (HARVEST.data && !force) { renderHarvest(); return Promise.resolve(HARVEST.data); }
  HARVEST.busy = true;
  return api("/api/harvest" + (force ? "?t=" + Date.now() : ""))
    .catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => { HARVEST.data = d; HARVEST.busy = false; renderHarvest(); });
}

// 统一口径：**红=危险**（风险语义，非涨跌语义）。
//   mode="high"   分位越高越危险（跌停家数、炸板率）
//   mode="low"    分位越低越危险（涨停家数：涨停少 = 赚钱效应差）
//   mode="pct"    不分位，按涨跌正负着色（涨红跌绿，中国习惯）
//   mode="none"   不分位、不着色（最高连板：高低都有解读，不给颜色误导）
const _hMd = (t) => _HE(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
function _hPctCell(label, val, q, unit, mode) {
  mode = mode || "high";
  let col = "var(--fg, #111)", foot = "";
  if (mode === "high" || mode === "low") {
    const p = Math.round((q || 0) * 100);
    const danger = mode === "high" ? p >= 85 : p <= 15;
    const safe = mode === "high" ? p <= 15 : p >= 85;
    col = danger ? "#dc2626" : (safe ? "#16a34a" : "var(--fg, #111)");
    foot = '历史 ' + p + '% 分位';
  } else if (mode === "pct") {
    const v = parseFloat(val);
    col = v > 0 ? "#dc2626" : (v < 0 ? "#16a34a" : "var(--fg, #111)");
    foot = v > 0 ? '全市场中位数上涨' : (v < 0 ? '全市场中位数下跌' : '全市场中位数持平');
  }
  return '<div style="flex:1;min-width:120px;border:1px solid var(--line,#e5e7eb);'
    + 'border-radius:10px;padding:12px 14px;background:var(--panel,#fff)">'
    + '<div style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280);margin-bottom:6px">'
    + _HE(label) + '</div>'
    + '<div style="font-size:var(--fs-xl,22px);font-weight:700;color:' + col + ';line-height:1.1">'
    + _HE(val) + '<span style="font-size:var(--fs-sm,12px);font-weight:400;color:var(--muted,#6b7280)">'
    + _HE(unit || '') + '</span></div>'
    + (foot ? '<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280);margin-top:4px">'
      + foot + '</div>' : '<div style="height:14px"></div>')
    + '</div>';
}

function renderHarvest() {
  const box = $("#p-harvest");
  if (!box) return;
  const d = HARVEST.data;
  if (!d) {
    box.innerHTML = '<div class="pred-head"><div class="pred-title">大势研判 · 局</div></div>'
      + '<div class="empty">加载中…</div>';
    return;
  }
  if (!d.ok) {
    box.innerHTML = '<div class="pred-head"><div class="pred-title">大势研判 · 局</div></div>'
      + '<div class="empty">' + _HE(d.error || "数据不可用") + '</div>';
    return;
  }

  const ph = d.phase || {}, pos = d.position || {}, raw = d.raw || {};
  const col = _HPHASE_COLOR[ph.tag] || "#64748b";
  const pq = ph.pct || {};
  const tr = d.traps || { rows: [], total: 0 };
  const tt = d.trap_trend || { series: [] };
  const ev = d.evidence || {};

  let h = '<div class="pred-head"><div class="pred-title">大势研判 · 局</div>'
    + '<div class="s">数据日 ' + _HE(d.asof) + ' ｜ 结构样本 1,651 个交易日（2020 至今）</div></div>';

  // ── ① 此刻在哪一局 ──────────────────────────────
  h += '<div style="border:1px solid var(--line,#e5e7eb);border-left:4px solid ' + col + ';'
    + 'border-radius:12px;padding:16px 18px;margin-bottom:14px;background:var(--panel,#fff)">'
    + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">'
    + '<span style="font-size:var(--fs-lg,16px);font-weight:700;color:' + col + '">'
    + _HE(ph.tag) + '</span>'
    + '<span style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280)">'
    + '强度 ' + _HE(ph.level) + '/3</span></div>'
    + '<div style="font-size:var(--fs-md,13px);line-height:1.75;margin-bottom:14px">'
    + _hMd(ph.desc) + '</div>'
    + '<div style="display:flex;gap:10px;flex-wrap:wrap">'
    + _hPctCell('涨停家数', raw.zt_n, pq.zt, ' 家', 'low')
    + _hPctCell('跌停家数', raw.dt_n, pq.dt, ' 家', 'high')
    + _hPctCell('炸板率', Math.round((raw.zb_rate || 0) * 100), pq.zb, ' %', 'high')
    + _hPctCell('最高连板', raw.max_streak, null, ' 板', 'none')
    + _hPctCell('中位涨幅', (raw.median_pct > 0 ? '+' : '') + (raw.median_pct || 0).toFixed(2), null, ' %', 'pct')
    + '</div>'
    + '<div style="margin-top:12px;font-size:var(--fs-sm,12px);color:var(--muted,#6b7280)">'
    + '位置：' + _HE(pos.note || '-') + '</div>'
    + '</div>';

  // ── ② 今日陷阱 ──────────────────────────────
  h += '<div style="border:1px solid var(--line,#e5e7eb);border-radius:12px;'
    + 'padding:16px 18px;margin-bottom:14px;background:var(--panel,#fff)">'
    + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px"'
    + '><div style="font-size:var(--fs-lg,16px);font-weight:700">今日陷阱</div>'
    + '<div class="s" style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280)">'
    + '昨日连板 ≥2、今日触及涨停却未封 → 散户终于买得进的时刻</div></div>';

  if (!tr.rows.length) {
    h += '<div style="padding:18px 0;color:var(--muted,#6b7280);font-size:var(--fs-md,13px)">'
      + '今日无「连板开板」陷阱标的。</div>';
  } else {
    h += '<table class="tbl-detail" style="margin-top:8px"><thead><tr>'
      + '<th>代码</th><th>名称</th><th>昨日板数</th><th>今日收盘</th><th>风险</th>'
      + '</tr></thead><tbody>';
    tr.rows.forEach((x) => {
      const rc = x.risk === "极高" ? "#dc2626" : (x.risk === "高" ? "#f97316" : "#64748b");
      const cc = (x.close_pct || 0) >= 0 ? "#dc2626" : "#16a34a";   // 中国习惯：涨红跌绿
      h += '<tr>'
        + '<td class="mono">' + _HE(x.code) + '</td>'
        + '<td>' + _HE(x.name) + '</td>'
        + '<td><b>' + _HE(x.prev_streak) + '</b> 板</td>'
        + '<td style="color:' + cc + ';font-weight:600">'
        + ((x.close_pct > 0 ? '+' : '') + (x.close_pct || 0).toFixed(2)) + '%</td>'
        + '<td style="color:' + rc + ';font-weight:600">' + _HE(x.risk) + '</td>'
        + '</tr>';
    });
    h += '</tbody></table>';
    h += '<div style="margin-top:8px;font-size:var(--fs-sm,12px);color:var(--muted,#6b7280)">'
      + '共 ' + _HE(tr.total) + ' 只。历史同型（≥3 板开板）T+5 超额 <b>−6.13%</b>，'
      + '4–5 板 <b>−7.21%</b>，胜率 28.3%。</div>';
  }
  if (tt.series && tt.series.length) {
    const seq = tt.series.map((x) => x.n).join(" → ");
    h += '<div style="margin-top:10px;font-size:var(--fs-sm,12px)">近 '
      + tt.series.length + ' 日陷阱数：<span class="mono">' + _HE(seq) + '</span>'
      + '（<b style="color:' + (tt.trend === "加速" ? "#dc2626" : "inherit") + '">'
      + _HE(tt.trend) + '</b>）</div>';
  }
  h += '</div>';

  // ── ③ 为什么这条能信 ──────────────────────────────
  const hv = ev.harvest || {}, ctrl = hv.control || {};
  h += '<div style="border:1px solid var(--line,#e5e7eb);border-radius:12px;'
    + 'padding:16px 18px;margin-bottom:14px;background:var(--panel,#fff)">'
    + '<div style="font-size:var(--fs-lg,16px);font-weight:700;margin-bottom:4px">'
    + '为什么这条能信：同一批强势股，只差「你买不买得进」</div>'
    + '<div class="s" style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280);margin-bottom:12px">'
    + '2023-01 ~ 2026-10 日线实测｜超额 = 个股收益 − 同日全市场等权收益｜t 值按日聚合</div>'
    + '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">'
    + '<div style="flex:1;min-width:200px;border:1px solid #bbf7d0;border-radius:10px;'
    + 'padding:12px 14px;background:rgba(22,163,74,.06)">'
    + '<div style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280)">买不进（仍封板）</div>'
    + '<div style="font-size:var(--fs-xl,22px);font-weight:700;color:#16a34a">+'
    + _HE(ctrl.excess) + '%</div>'
    + '<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280)">T+5 超额 ｜ t=+'
    + _HE(ctrl.t) + ' ｜ 胜率 ' + _HE(ctrl.win) + '% ｜ n=' + _HE(ctrl.n) + '</div></div>'
    + '<div style="flex:1;min-width:200px;border:1px solid #fecaca;border-radius:10px;'
    + 'padding:12px 14px;background:rgba(220,38,38,.06)">'
    + '<div style="font-size:var(--fs-xs,11px);color:var(--muted,#6b7280)">买得进（开板了）</div>'
    + '<div style="font-size:var(--fs-xl,22px);font-weight:700;color:#dc2626">'
    + _HE(hv.excess) + '%</div>'
    + '<div style="font-size:var(--fs-2xs,10px);color:var(--muted,#6b7280)">T+5 超额 ｜ t='
    + _HE(hv.t) + ' ｜ 胜率 ' + _HE(hv.win) + '% ｜ n=' + _HE(hv.n) + '</div></div>'
    + '</div>';

  if (ev.by_streak && ev.by_streak.length) {
    h += '<table class="tbl-detail"><thead><tr><th>封板高度</th><th>T+5 超额</th>'
      + '<th>t 值</th><th>样本</th></tr></thead><tbody>';
    ev.by_streak.forEach((x) => {
      h += '<tr><td>' + _HE(x.streak) + '</td>'
        + '<td style="color:#16a34a;font-weight:600">' + _HE(x.excess) + '%</td>'
        + '<td class="mono">' + _HE(x.t) + '</td>'
        + '<td class="s">' + _HE(x.n) + '</td></tr>';
    });
    h += '</tbody></table>';
  }
  h += '</div>';

  // ── ④ 不能做什么（同样是实测） ──────────────────────────
  h += '<div style="border:1px solid var(--line,#e5e7eb);border-radius:12px;'
    + 'padding:16px 18px;background:var(--panel,#fff)">'
    + '<div style="font-size:var(--fs-lg,16px);font-weight:700;margin-bottom:4px">'
    + '这个面板不能做什么（同样是实测结论）</div>'
    + '<div style="font-size:var(--fs-md,13px);line-height:1.8;margin-top:8px">'
    + '市场层的情绪指标<b>预测不了指数方向</b>：';
  (ev.market_useless || []).forEach((x, i) => {
    h += (i ? '、' : '') + _HE(x.cond) + ' 超额 ' + _HE(x.excess)
      + '%（t=' + _HE(x.t) + '）';
  });
  h += '，全部不显著。</div>'
    + '<div style="font-size:var(--fs-md,13px);line-height:1.8;margin-top:6px">'
    + '唯一显著的是<b>极端恐慌后的均值回复</b>：';
  (ev.mean_reversion || []).forEach((x, i) => {
    h += (i ? '；' : '') + _HE(x.cond) + ' 超额 <b>+' + _HE(x.excess)
      + '%</b>（t=' + _HE(x.t) + '）';
  });
  h += '。但扣掉双边成本后剩不下多少，只能当"情绪位置"参考，不能当买入信号。</div>'
    + '<div style="margin-top:10px;padding:10px 12px;border-radius:8px;'
    + 'background:rgba(245,158,11,.1);font-size:var(--fs-sm,12px)">'
    + '所以本面板<b>不给点位预测、不给买卖建议</b>，只说两件事：'
    + '这一局在哪个阶段、这个阶段什么动作必亏。</div>'
    + '</div>';

  box.innerHTML = h;
}

function renderAvoid() {
  const box = $("#p-avoid");
  if (!box) return;
  const d = AVOID.data;
  if (!d) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">避雷名单</div></div>
      <div class="empty">加载中…</div>`;
    return;
  }
  if (d.ok === false || d._err) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">避雷名单</div></div>
      <div class="empty">${esc(d.error || d.note || "接口不可用")}</div>
      <div class="hint">若刚更新界面：需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启）。</div>`;
    return;
  }
  const st = d.stats || {};
  const bt = st.by_tier || {};
  const items = toArr(d.items);
  // 统一两位小数（后端给的是 round(,3)，直接拼会出现 -4.704% 这种不一致精度）
  const fmt = (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!isFinite(n)) return "—";
    return (n > 0 ? "+" : "") + n.toFixed(2);
  };
  // ★ 涨红跌绿（A 股惯例，任何面板都不得反用）：正=红、负=绿、0/未到期=中性。
  // 2026-10-21 修正：此前把「实际 -7.28%」涂成红色，与"跌了该绿"的常识相反；
  // C 级/全样本均值是负数却也写死红色，同一处错误。统一收口到 pctC()，别再散写字面量。
  const PC_UP = "#c0392b";     // 涨
  const PC_DOWN = "#0a7d3b";   // 跌
  const pctC = (v) => {
    if (v === null || v === undefined) return "#666";
    const n = Number(v);
    if (!isFinite(n)) return "#666";
    return n > 0 ? PC_UP : (n < 0 ? PC_DOWN : "#666");
  };
  const cT = bt["C"] || {};
  const sT = bt["S"] || {};
  const head = `<div class="pred-head">
      <div class="pred-title">避雷名单</div>
      <button class="btn primary" onclick="loadAvoid(true)">刷新</button>
    </div>
    <div class="pil-ms">
      <span class="badge bear">只做排除 · 不做推荐</span>
      <span class="s" style="opacity:.7">样本量必须一起看：高分票多为刚进池的新票，还拿不到真实收益</span>
      <span class="s">数据日 ${esc(d.build_date || "—")}</span>
      <span class="s">全样本 ${esc(st.total || 0)} 张（已回填 ${esc(st.filled || 0)}）</span>
      <span class="s">全样本均值 <b style="color:${pctC(st.avg_t5)}">${fmt(st.avg_t5)}%</b></span>
      <span class="s">C 级 ${esc(cT.n || 0)} 张<b style="opacity:.65">（${esc(cT.n_filled || 0)} 有数据）</b>均值 <b style="color:${pctC(cT.avg_t5)}">${fmt(cT.avg_t5)}%</b></span>
      <span class="s">S 级 ${esc(sT.n || 0)} 张<b style="opacity:.65">（${esc(sT.n_filled || 0)} 有数据）</b>均值 <b style="color:${pctC(sT.avg_t5)}">${fmt(sT.avg_t5)}%</b></span>
    </div>`;
  // 一览证据：把「低分卡确实更差」这句话用真实数字摆在最上面，不做修饰
  const aT = bt["A"] || {};
  const evid = `<div class="hint" style="margin:8px 0 10px">
      分层实测（T+5 超额）：A 级 <b style="color:${pctC(aT.avg_t5)}">${fmt(aT.avg_t5)}%</b>（${esc(aT.n_filled || 0)}/${esc(aT.n || 0)} 有数据）·
      S 级 <b style="color:${pctC(sT.avg_t5)}">${fmt(sT.avg_t5)}%</b>（${esc(sT.n_filled || 0)}/${esc(sT.n || 0)}）·
      C 级 <b style="color:${pctC(cT.avg_t5)}">${fmt(cT.avg_t5)}%</b>（${esc(cT.n_filled || 0)}/${esc(cT.n || 0)}）·
      全样本 <b style="color:${pctC(st.avg_t5)}">${fmt(st.avg_t5)}%</b>（${esc(st.filled || 0)}/${esc(st.total || 0)}）。
      → 有区分度的是 <b>A 级与 C 级之间</b>；而 C 级与全样本的差距其实很小，
      所以本面板的价值是「<b>用多重负面标签挑出更该避的那批</b>」，不是「C 级一律不能碰」。
      <b>更不是买入清单</b>——请勿反向使用。
    </div>`;
  const rows = items.map((it) => {
    const tags = toArr(it.reasons).map((r) =>
      `<span title="${escAttr(r.why || "")}" style="display:inline-block;margin-right:6px;padding:1px 6px;
        border-radius:3px;background:rgba(192,57,43,.12);color:#c0392b;font-size:12px">${esc(r.tag)}</span>`
    ).join("");
    const a5 = (it.actual_t5 === null || it.actual_t5 === undefined) ? null : it.actual_t5;
    return `<li style="display:flex;gap:10px;align-items:flex-start;padding:7px 4px;
        border-bottom:1px solid rgba(128,128,128,.18)">
      <span style="min-width:46px;text-align:right;font-variant-numeric:tabular-nums;opacity:.65">${esc(it.res_score)}</span>
      <span style="min-width:132px"><b>${esc(it.code)}</b> ${esc(it.name || "")}</span>
      <span style="flex:1">${tags || `<span style="opacity:.5;font-size:12px">分数低但无显式负面标签（纯低分）</span>`}</span>
      <span style="min-width:82px;text-align:right;font-size:12px;${a5 === null ? "opacity:.6" : "color:" + pctC(a5)}">
        ${a5 === null ? "未到期" : "实际 " + fmt(a5) + "%"}</span>
    </li>`;
  }).join("");
  const list = items.length
    ? `<ul style="list-style:none;padding:0;margin:0">${rows}</ul>`
    : `<div class="empty">当前没有可列出的低分卡</div>`;
  const cal = toArr(d.caliber);
  const caliber = cal.length ? `<details class="res-cal"><summary>这个面板的口径（${cal.length} 条，全部可追溯）</summary>
    <ul>${cal.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "";
  const foot = `<div class="pil-foot"><div class="s">${esc(d.note || "")}</div></div>`;
  box.innerHTML = head + evid + list + caliber + foot;
}
function renderResonance() {
  const box = $("#p-resonance");
  if (!box) return;
  const d = RES.data;
  if (!d) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">多源共振</div></div>
      <div class="empty">加载中…</div>`;
    return;
  }
  if (d.ok === false || d._err) {
    box.innerHTML = `<div class="pred-head">
        <div class="pred-title">多源共振</div>
        <button class="btn primary" id="resRegen" onclick="resRun(true)">重新融合</button></div>
      <div class="empty">${esc(d.note || d.error || "接口不可用")}</div>
      <div class="hint">若刚更新过界面：这是新板块，需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启，光刷新页面不够）。</div>`;
    return;
  }
  const ms = d.market_state || {};
  const msCls = { "结构": "bull", "震荡": "neu", "普跌": "bear" }[ms.state] || "neu";
  const s = d.stats || {};
  const head = `<div class="pred-head">
      <div class="pred-title">多源共振</div>
      <button class="btn primary" id="resRegen" onclick="resRun(true)">重新融合</button>
    </div>
    <div class="pil-ms">
      <span class="badge ${msCls}">市场状态：${esc(ms.state || "未知")}</span>
      <span class="s">数据日 ${esc(d.build_date || "—")}</span>
      <span class="s">信号 ${esc(s.signals || 0)} 条（已回填 ${esc(s.filled || 0)}）</span>
      <span class="s">共振卡 ${esc(s.cards || 0)}</span>
      <span class="s">战法 ${esc(s.strategies || 0)}</span>
      <span class="s">信源 ${esc(s.sources || 0)}</span>
      <span class="s">环境历史 ${esc(s.state_days || 0)} 个交易日</span>
    </div>`;
  const active = RES_TABS.some((t) => t.key === RES.rtab) ? RES.rtab : "cards";
  const tabRow = `<div class="pillar-tabs">` + RES_TABS.map((t) => {
    let n = 0;
    if (t.key === "cards") n = toArr(d.cards).length;
    else if (t.key === "strategy") n = toArr(d.strategies).length;
    else if (t.key === "source") n = toArr(d.sources).length;
    else if (RES.evo && RES.evo.ok) n = toArr(RES.evo.rules).length;
    return `<a class="sub-tab${t.key === active ? " active" : ""}" href="javascript:void(0)"
      title="${escAttr(t.label)}" onclick="resTab('${t.key}')">${esc(t.label)}${n ? ` <b>${n}</b>` : ""}</a>`;
  }).join("") + `</div>`;
  let body = "";
  if (active === "cards") body = resCardsView(d);
  else if (active === "strategy") body = resStrategyView(d);
  else if (active === "source") body = resSourceView(d);
  else body = resEvolveView(RES.evo);
  const cal = toArr(d.caliber);
  const caliber = cal.length ? `<details class="res-cal"><summary>这个面板的口径（${cal.length} 条，全部可追溯）</summary>
    <ul>${cal.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "";
  const foot = `<div class="pil-foot"><div class="s">引擎版本 ${esc(d.engine_ver || "")}｜${esc(d.note || "")}</div></div>`;
  box.innerHTML = head + tabRow + body + caliber + foot;
}
/* ---------- 量化信号 QUANT 状态 + 五子页签（2026-10-16，第三维信号源） ----------
 * 板块位置：今日总览 → 「量化信号」（紧跟「多源共振」之后）。
 * 数据源 /api/quant（后端 quant.py 产出：因子全景 / 多因子选股 / 因子回测 / 菜谱预设 /
 * 量化因子的自我学习与成长；查看器只读，权重学走 metalearn 共享引擎）。
 * 五子页签对应进哥拍板的「5 个子视图」。必须在此处声明（initNav → selectNav 会同步访问
 * QUANT），防 TDZ（与 RES 同款前置声明）。
 */
const QUANT_FACTOR_ORDER = ["MA_POS", "MACD", "KDJ", "BOLL", "VOL_ABS", "MOM", "VOLA"];
const QUANT_FACTOR_LABEL = {
  MA_POS: "均线多空", MACD: "MACD动能", KDJ: "KDJ强度", BOLL: "布林位置",
  VOL_ABS: "量能异常", MOM: "动量(20日)", VOLA: "波动率(稳定)",
};
/* QUANT 状态对象已前置声明在文件上方（避雷跟随标记那一节）。此处的 const 已删除 ——
   原来写在这里，但 initNav 在更上方就执行了，等于没防住 TDZ（2026-10-31 全标签扫描复现）。 */
const QUANT_TABS = [
  { key: "panorama", label: "因子全景" },
  { key: "select",   label: "多因子选股" },
  { key: "backtest", label: "因子回测" },
  { key: "recipe",   label: "菜谱预设" },
  { key: "learn",    label: "量化因子的自我学习与成长" },
];
function quantTab(k) { QUANT.qtab = k; renderQuant(); }

/* ================= ETF 基金（2026-10-17 进哥需求） =================
 * 今日总览与「多源共振」「量化信号」并列的顶层 NAV 子项；左-右对称两栏、A/B 分离、绝不混叠。
 *   左栏 A = 量化多因子中的 ETF 候选（复用 quant 的 7 技术因子 + metalearn 权重；候选池空时诚实返回空）
 *   右栏 B = ETF 专属投顾 6 维（份额净申赎/资金流、跟踪指数与成分、溢价率/折溢价、规模、费率、跟踪误差），全部「待接入」
 * 数据源 /api/etf（后端 etf.py 产出：left=A 候选结果，right=B 6 维；查看器只读）。
 * 状态对象 ETF 已前置声明在文件上方（避雷跟随标记那一节）。
 *   原注释说「必须在此处声明防 TDZ」，但 initNav 在更上方执行，这里其实已经太晚了 ——
 *   2026-10-31 全标签深链扫描实测 #overview/etf 首屏报
 *   「Cannot access 'ETF' before initialization」，面板停在「正在加载数据…」。已上移。 */

function loadEtf(force) {
  const box = $("#p-etf");
  if (box && !ETF.data) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">ETF 基金</div></div>
      <div class="empty">正在载入 ETF 多因子候选与专属投顾维度…</div>`;
  }
  ETF.busy = true;
  const qs = force ? "?t=" + Date.now() : "";
  return api("/api/etf" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => { ETF.data = d; ETF.busy = false; renderEtf(); });
}

function etfStageCls(stage) {
  if (!stage) return "neu";
  if (stage.indexOf("样本外验证通过") === 0) return "bull";
  if (stage.indexOf("收缩生效中") === 0) return "ch-A";
  return "neu";
}

function renderEtf() {
  const box = $("#p-etf");
  if (!box) return;
  const d = ETF.data;
  if (!d) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">ETF 基金</div></div>
      <div class="empty">加载中…</div>`;
    return;
  }
  if (d.ok === false || d._err) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">ETF 基金</div></div>
      <div class="empty">${esc(d.note || d.error || "接口不可用")}</div>
      <div class="hint">若刚更新过界面：这是新板块，需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启，光刷新页面不够）。</div>`;
    return;
  }
  const head = `<div class="pred-head">
      <div class="pred-title">ETF 基金</div>
      <span class="s">引擎版本 ${esc(d.engine_ver || "")}</span>
      <span class="s">生成于 ${esc(d.generated_at || "—")}</span>
    </div>`;
  // 左 A / 右 B 对称两栏，中间用清晰分隔条分开（绝不混叠或上下堆叠）
  const body = `<div class="etf-cols">
      <div class="etf-col etf-col-left">${renderEtfLeft(d.left)}</div>
      <div class="etf-divider" title="左：量化多因子中的 ETF 候选 ｜ 右：ETF 专属投顾维度"><span>⟷</span></div>
      <div class="etf-col etf-col-right">${renderEtfRight(d.right)}</div>
    </div>`;
  box.innerHTML = head + body;
}

/* 左栏 A：量化多因子中的 ETF 候选（复用 quant 因子 + metalearn 权重） */
function renderEtfLeft(A) {
  if (!A) return `<div class="empty">无数据</div>`;
  const stage = A.weight_stage || "";
  const badge = stage ? `<span class="badge ${etfStageCls(stage)} sl-stage">${esc(stage)}</span>` : "";
  const head = `<div class="etf-col-h">
      <div class="etf-col-tag etf-tag-a">A · 量化多因子中的 ETF 候选</div>
      ${badge}
    </div>
    <div class="etf-col-sub">${esc(A.desc || "")}</div>`;
  if (A.universe_empty || (A.candidates && A.candidates.length === 0)) {
    const note = A.note || "ETF 日 K 待接入，候选池为空";
    return head + `<div class="empty etf-empty">${esc(note)}</div>`;
  }
  const w = A.weights || {};
  const wParts = QUANT_FACTOR_ORDER.filter((k) => w[k] != null)
    .map((k) => `${esc(QUANT_FACTOR_LABEL[k] || k)} ${(w[k] * 100).toFixed(0)}%`).join(" · ");
  const wLine = `<div class="q-weights">因子权重（数据学出，唯一真源 = metalearn.weights_from_ic）：${wParts}</div>`;
  const items = toArr(A.candidates).map((it) => {
    const facs = toArr(it.factor_scores);
    const facBits = QUANT_FACTOR_ORDER.map((k) => {
      const v = it.factor_scores ? it.factor_scores[k] : null;
      if (v == null) return null;
      const cls = v >= 0 ? "bull" : "bear";
      return `<span class="chip-s ${cls}">${esc(QUANT_FACTOR_LABEL[k] || k)} ${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}</span>`;
    }).filter(Boolean);
    return `<div class="res-card">
      <div class="res-card-h">
        <span class="pil-rank">${esc(it.rank)}</span>
        ${codeLink(it.symbol, { text: it.name || it.symbol, cls: "res-name" })}
        <span class="res-code">${esc(it.symbol)}</span>
        <span class="res-score" title="截面复合分">${Math.round((it.score || 0) * 1000) / 10}</span>
      </div>
      ${facBits.length ? `<div class="res-tags">${facBits.join("")}</div>` : ""}
    </div>`;
  }).join("");
  return head + wLine + `<div class="res-grid">${items || `<div class="empty">无入选标的</div>`}</div>`;
}

/* 右栏 B：ETF 专属投顾 6 维 + 集思录投顾指标（溢价率/规模/费率/真实份额净申赎，已接入进哥 cookie）+ 实时行情快照(Sina) */
function renderEtfRight(B) {
  if (!B) return `<div class="empty">无数据</div>`;
  const head = `<div class="etf-col-h">
      <div class="etf-col-tag etf-tag-b">B · ETF 专属投顾维度</div>
    </div>
    <div class="etf-col-sub">${esc(B.desc || "")}</div>`;
  const dims = toArr(B.dims);
  const rows = dims.map((x) => {
    const on = x.status && x.status.indexOf("已接入") === 0;
    const badge = on
      ? `<span class="etf-badge on" title="${escAttr(x.note || "")}">${esc(x.status)}</span>`
      : `<span class="etf-badge off" title="${escAttr(x.note || "")}">${esc(x.status || "待接入")}</span>`;
    const val = (x.value && x.value.n != null) ? `<span class="etf-dim-valnum">已对齐 ${x.value.n} 只</span>` : "";
    return `<div class="etf-dim">
      <div class="etf-dim-name"><b>${esc(x.dim)}</b> ${badge} ${val}</div>
      <div class="etf-dim-val"><span class="etf-dim-note">${esc(x.note || "")}</span></div>
    </div>`;
  }).join("");

  // 集思录投顾指标表（已接入：进哥提供的登录态 cookie 打通 jisilu.cn/data/etf/etf_list）
  const jm = B.jisilu_meta || {};
  const jl = B.jisilu || {};
  const jlCodes = toArr(Object.keys(jl));
  let jlSec;
  if (jlCodes.length) {
    const sorted = jlCodes.slice().sort((a, b) => ((jl[b] && jl[b].scale_est) || 0) - ((jl[a] && jl[a].scale_est) || 0));
    const jrows = sorted.map((c) => {
      const v = jl[c] || {};
      const prem = v.premium_rt == null ? null : v.premium_rt;
      const premTxt = prem == null ? "—" : fmtPct(prem);
      const premCls = prem == null ? "" : (prem > 0 ? "up" : (prem < 0 ? "down" : ""));
      const scale = v.scale_est == null ? "—" : v.scale_est.toFixed(2);
      const fee = v.fee == null ? "—" : v.fee.toFixed(2) + "%";
      const incr = v.amount_increase_rt == null ? null : v.amount_increase_rt;
      const incrTxt = incr == null ? "—" : fmtPct(incr);
      const incrCls = incr == null ? "" : (incr > 0 ? "up" : (incr < 0 ? "down" : ""));
      const idx = v.index_nm ? esc(v.index_nm) : "—";
      return `<tr>
        <td>${codeLink(c, { text: (v.name || c), cls: "etf-qn" })}</td>
        <td class="etf-qcode">${esc(c)}</td>
        <td class="num ${premCls}">${premTxt}</td>
        <td class="num">${scale}</td>
        <td class="num">${fee}</td>
        <td class="num ${incrCls}">${incrTxt}</td>
        <td class="etf-jl-idx" title="${idx}">${idx}</td>
      </tr>`;
    }).join("");
    jlSec = `<div class="etf-jl">
      <div class="etf-jl-h">集思录投顾指标 · 已接入 ${jlCodes.length} 只（按估算规模排序）· ${esc(jm.as_of || "")}</div>
      <div class="etf-jl-scroll">
        <table class="etf-qtab etf-jltab">
          <thead><tr>
            <th>名称</th><th>代码</th>
            <th class="num">溢价率</th>
            <th class="num">规模(亿)</th>
            <th class="num">费率</th>
            <th class="num">份额净申赎</th>
            <th>跟踪指数</th>
          </tr></thead>
          <tbody>${jrows}</tbody>
        </table>
      </div>
      <div class="etf-jl-foot">溢价率=集思录净值折溢价率(nav_discount_rt)；规模=总份额×单位净值估算；费率=管理费+托管费年化；份额净申赎=真实净申购/赎回(%)，均为集思录数据。跟踪误差=东财 F10 年化跟踪误差，场内 ETF 东财不返回者以同指数联接基金近似（标「近似值·联接基金」），均经进哥部署机跑 etf_track_error.py 接入。</div>
    </div>`;
  } else {
    jlSec = `<div class="etf-jl"><div class="etf-jl-h">集思录投顾指标<span class="demo-badge">暂未接入</span>（需运行 etf_jisilu.py 并配置 jisilu cookie 后重启查看器）。</div></div>`;
  }

  // 实时行情快照（Sina，已接入）：按成交额取活跃前 25 只，避免右栏过长
  const qm = B.quotes_meta || {};
  const all = toArr(Object.keys(B.quotes || {}));
  const top = all.slice().sort((a, b) => ((B.quotes[b] && B.quotes[b].amount) || 0) - ((B.quotes[a] && B.quotes[a].amount) || 0)).slice(0, 25);
  let snap = "";
  if (top.length) {
    const qrows = top.map((c) => {
      const v = B.quotes[c];
      const pct = (v.pct == null) ? "—" : fmtPct(v.pct);
      const pcls = v.pct == null ? "" : (v.pct >= 0 ? "up" : "down");
      const amt = (v.amount == null) ? "—" : (v.amount / 1e8).toFixed(2) + " 亿";
      return `<tr>
        <td>${codeLink(c, { text: (v.name || c), cls: "etf-qn" })}</td>
        <td class="etf-qcode">${esc(c)}</td>
        <td class="num">${v.price == null ? "—" : v.price.toFixed(3)}</td>
        <td class="num ${pcls}">${pct}</td>
        <td class="num">${amt}</td>
      </tr>`;
    }).join("");
    snap = `<div class="etf-snap">
      <div class="etf-snap-h">行情快照 · Sina 实时 · 已接入 ${all.length} 只（按成交额取前 ${top.length}）· ${esc(qm.as_of || "")}</div>
      <table class="etf-qtab">
        <thead><tr><th>名称</th><th>代码</th><th class="num">最新价</th><th class="num">涨跌幅</th><th class="num">成交额</th></tr></thead>
        <tbody>${qrows}</tbody>
      </table>
    </div>`;
  } else {
    snap = `<div class="etf-snap"><div class="etf-snap-h">行情快照：暂未接入（运行 etf_advisory_data.py 打通 Sina 实时数据）</div></div>`;
  }
  return head + `<div class="etf-dims">${rows}</div>` + jlSec + snap;
}

/* ================= Kronos K线基础模型预测（2026-10-24 进哥需求） =================
   数据：后端 kronos.py 离线推理（Kronos 开源基础模型）→ 落库 → 本页只读。
   诚实纪律：展示的是概率预测 + 有限样本回测，非买卖信号。 */
const KRONOS = { data: null, busy: false };

function loadKronos(force) {
  const box = $("#p-kronos");
  if (box && !KRONOS.data) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">Kronos K线预测</div></div>
      <div class="empty">正在载入基础模型预测与回测…</div>`;
  }
  KRONOS.busy = true;
  const qs = force ? "?t=" + Date.now() : "";
  return api("/api/kronos" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => { KRONOS.data = d; KRONOS.busy = false; renderKronos(); })
    // 面板打开时若后台正在推理（可能上次提交完就关了页面），把进度条接上，别让用户对着旧数据发呆
    .then(() => api("/api/kronos/status").catch(() => null))
    .then((st) => { if (st && st.running) kronosTrack(); });
}

const K_SORT = [["score", "评分"], ["ret", "预测收益"], ["sharpe", "回测夏普"]];
const K_PFX = [["", "全部"], ["0", "0开头"], ["3", "3开头"], ["6", "6开头"]];
const K_DIRS = [["", "全部"], ["up", "看涨"], ["down", "看跌"]];
const K_MINS = [[0, "全部"], [65, "65分+"], [80, "80分+"]];
if (!KRONOS.f) KRONOS.f = { pfx: "", dir: "", min: 0, sort: "score" };
if (!KRONOS.open) KRONOS.open = {};

function kScoreCls(s) {
  if (s == null) return "g5";
  if (s >= 80) return "g1";
  if (s >= 65) return "g2";
  if (s >= 50) return "g3";
  if (s >= 35) return "g4";
  return "g5";
}
function kNum(v) { return v == null ? -9e9 : v; }
function kFmtSec(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + " 秒";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + " 分" + (s ? " " + s + " 秒" : "");
}

/* 客户端筛选：数据已在内存，点一下即时生效，不再等接口（进哥要的是「快速看条件→结果」）。 */
function kronosRows() {
  const f = KRONOS.f, d = KRONOS.data || {};
  const out = toArr(d.items).filter((it) => {
    const p = String(it.prefix || (it.code || "?")[0]);
    if (f.pfx && p !== f.pfx) return false;
    if (f.dir === "up" && !(it.pred_return > 0)) return false;
    if (f.dir === "down" && !(it.pred_return < 0)) return false;
    if (f.min && !(it.score >= f.min)) return false;
    return true;
  });
  out.sort((a, b) => (f.sort === "ret" ? kNum(b.pred_return) - kNum(a.pred_return)
    : f.sort === "sharpe" ? kNum(b.bt_sharpe) - kNum(a.bt_sharpe)
      : kNum(b.score) - kNum(a.score)));
  return out;
}

function kProgHtml(s) {
  const pr = (s && s.progress) || {};
  const pct = pr.pct == null ? 0 : pr.pct;
  const eta = pr.eta_sec == null ? "估算中…" : "约剩 " + kFmtSec(pr.eta_sec);
  return `<div class="kf-prog-h"><b>后台推理中</b> · ${esc(s.stage || "")}</div>
    <div class="kf-prog-bar"><i style="width:${Math.max(2, Math.min(100, pct))}%"></i></div>
    <div class="kf-prog-m">${pr.done != null
      ? pr.done + " / " + pr.total + " 只 · " + pct.toFixed(0) + "% · " + eta
      : "正在装载模型与 K 线底座，稍候…"}
      <span class="muted">（可以关掉页面，回来还在跑）</span></div>`;
}

/* 后台推理的进度跟踪：面板打开时若正在跑，也要能看见，而不是一个死的「推理中」。 */
function kronosTrack() {
  if (KRONOS.track) return;
  KRONOS.track = setInterval(() => {
    api("/api/kronos/status").then((s) => {
      const el = $("#kProg");
      if (s && s.running) {
        if (el) el.innerHTML = kProgHtml(s);
        const b = $("#kronosRegen");
        if (b) { b.disabled = true; b.textContent = "推理中…"; }
      } else {
        clearInterval(KRONOS.track); KRONOS.track = null;
        const el2 = $("#kProg"); if (el2) el2.innerHTML = "";
        const b = $("#kronosRegen");
        if (b) { b.disabled = false; b.textContent = "重新生成"; }
        loadKronos(true);
      }
    }).catch(() => { clearInterval(KRONOS.track); KRONOS.track = null; });
  }, 3000);
}

function kronosRun() {
  const btn = $("#kronosRegen");
  const topN = Number(($("#kTopN") || {}).value || 60);
  const scope = ($("#kPfxScope") || {}).value || "";
  const fast = !!(($("#kFast") || {}).checked);
  if (btn) { btn.disabled = true; btn.textContent = "已提交…"; }
  const q = `force=1&top_n=${topN}&fast=${fast ? 1 : 0}` + (scope ? "&prefixes=" + scope : "");
  api("/api/kronos/run?" + q, { method: "POST", headers: { "Content-Type": "application/json" } })
    .then((d) => {
      if (d && d.ok === false) {
        reportLoadError("Kronos 推理生成", new Error(d.error || "未知错误"));
        if (btn) { btn.disabled = false; btn.textContent = "重新生成"; }
        return;
      }
      // 已有一轮在跑：直接接上进度，不重复提交
      const el = $("#kProg");
      if (el) el.innerHTML = kProgHtml(d && d.already ? { stage: d.stage, progress: d.progress } : { stage: "启动中", progress: {} });
      kronosTrack();
    })
    .catch((e) => {
      if (btn) { btn.disabled = false; btn.textContent = "重新生成"; }
      reportLoadError("Kronos 推理生成", e);
    });
}

function kDetail(it) {
  const parts = toArr(it.score_parts).map((p) => `<tr>
      <td class="k-pt-k">${esc(p.k)}</td>
      <td class="num ${p.got < 0 ? "bear" : ""}">${p.got == null ? "—" : (p.got > 0 ? "+" : "") + p.got.toFixed(1)}</td>
      <td class="num muted">${p.max ? "/ " + p.max.toFixed(0) : "扣分"}</td>
      <td class="k-why">${esc(p.why || "")}</td></tr>`).join("");
  const flags = toArr(it.flags).map((x) => `<span class="k-flag">⚠ ${esc(x)}</span>`).join(" ");
  return `<div class="k-detbox">
    <div class="k-det-h">评分明细 · 总分 <b>${it.score == null ? "—" : it.score.toFixed(1)}</b>
      （${esc(it.grade || "")}）${flags ? " " + flags : ""}</div>
    <table class="k-pt"><tbody>${parts}</tbody></table>
    <div class="k-det-note">${esc(it.grade_note || "")}${it.err ? "｜推理报错：" + esc(it.err) : ""}</div>
  </div>`;
}

function kronosCtl() {
  const d = KRONOS.data || {}, f = KRONOS.f;
  const pc = d.prefix_counts || {}, tot = toArr(d.items).length;
  const hit = kronosRows().length;
  const bP = K_PFX.map(([v, l]) => {
    const n = v ? (pc[v] != null ? pc[v] : 0) : tot;
    return `<button class="btn xs${f.pfx === v ? " primary" : ""}" data-kf-pfx="${v}">${l} <span class="cnt">${n}</span></button>`;
  }).join(" ");
  const bD = K_DIRS.map(([v, l]) => `<button class="btn xs${f.dir === v ? " primary" : ""}" data-kf-dir="${v}">${l}</button>`).join(" ");
  const bM = K_MINS.map(([v, l]) => `<button class="btn xs${Number(f.min) === v ? " primary" : ""}" data-kf-min="${v}">${l}</button>`).join(" ");
  const bS = K_SORT.map(([v, l]) => `<button class="btn xs${f.sort === v ? " primary" : ""}" data-kf-sort="${v}">${l}</button>`).join(" ");
  return `<div class="kf-ctl">
      <span class="kf-cl">板块</span>${bP}
      <span class="kf-cl">方向</span>${bD}
      <span class="kf-cl">评分</span>${bM}
      <span class="kf-cl">排序</span>${bS}
      <span class="kf-hit">命中 <b>${hit}</b> / ${tot} 只</span>
      <button class="btn xs" data-kf-reset="1">重置</button>
    </div>`;
}

function kronosConcl() {
  const d = KRONOS.data || {}, cc = d.conclusion || {};
  const f = KRONOS.f;
  const cond = [];
  if (f.pfx) cond.push("板块 " + (f.pfx + "开头"));
  if (f.dir) cond.push("方向 " + (f.dir === "up" ? "看涨" : "看跌"));
  if (f.min) cond.push("评分 ≥" + f.min);
  cond.push("排序 " + ((K_SORT.find((x) => x[0] === f.sort) || ["", "评分"])[1]));
  const chips = cond.map((c) => `<span class="kf-chip">${esc(c)}</span>`).join("");
  const br = (x) => {
    const cls = kScoreCls(x.score);
    const fl = toArr(x.flags);
    return `<span class="kf-br">
        <b>${esc(x.name || "")}</b> <span class="muted">${esc(x.code || "")}</span>
        <i class="ksc ${cls}">${x.score == null ? "—" : x.score.toFixed(0)}</i>
        <span class="muted">${esc(x.dir || "")}·${esc(x.grade || "")}</span>
        ${fl.length ? `<em class="k-flag">⚠ ${esc(fl.join("/"))}</em>` : ""}
      </span>`;
  };
  const best = toArr(cc.best).map((x) => `<li>${br(x)}</li>`).join("");
  const warn = toArr(cc.warn).map((x) => `<li>${br(x)}</li>`).join("");
  return `<div class="kf-concl">
    <div class="kf-concl-h">参考结论<span class="kf-chips">${chips}</span></div>
    <div class="kf-concl-s">${esc(cc.summary || "暂无结论，请先点「重新生成」跑一次。")}</div>
    <div class="kf-two">
      <div><div class="kf-two-h">最值得看（按评分）</div><ul>${best || '<li class="muted">无</li>'}</ul></div>
      <div><div class="kf-two-h">需回避 / 有硬伤</div><ul>${warn || '<li class="muted">本轮没有触发硬伤标记的票</li>'}</ul></div>
    </div>
  </div>`;
}

function renderKronos() {
  const box = $("#p-kronos");
  if (!box) return;
  const d = KRONOS.data;
  if (!d) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">Kronos K线预测</div></div>
      <div class="empty">加载中…</div>`;
    return;
  }
  if (d.ok === false || d._err) {
    // 2026-10-25（进哥：加载失败不能就这么放着）：说清原因、给预计恢复、自动再试。
    KRONOS.fail = (KRONOS.fail || 0) + 1;
    const why = esc(d.error || d.note || "接口不可用");
    const wait = KRONOS.fail <= 2 ? 8 * KRONOS.fail : 0;
    box.innerHTML = `<div class="pred-head"><div class="pred-title">Kronos K线预测</div>
        <button class="btn primary" onclick="loadKronos(true)">立即重试</button></div>
      <div class="empty" style="padding:18px;line-height:2;text-align:left">
        <b>这次没取到数据：</b>${why}<br>
        ${wait
          ? `<span class="hint">${wait} 秒后自动重试（第 ${KRONOS.fail}/2 次）。这个接口本身是毫秒级，通常是被「今日看板」等重接口占了连接，等它跑完就会自己回来。</span>`
          : `<span class="hint">已连续失败 ${KRONOS.fail} 次。请点「立即重试」；仍不行说明本地服务没响应，可稍后刷新页面。</span>`}
      </div>`;
    if (wait) {
      clearTimeout(KRONOS.timer);
      KRONOS.timer = setTimeout(() => loadKronos(true), wait * 1000);
    }
    return;
  }
  KRONOS.fail = 0;
  clearTimeout(KRONOS.timer);
  const ready = d.ready && d.ready.ok;
  const fl = d.filters || {};
  const modeTxt = fl.fast ? "快速" : (fl.windows ? "完整·" + fl.windows + "窗" : "—");
  const head = `<div class="pred-head">
      <div class="pred-title">Kronos K线预测</div>
      <div class="kf-run">
        <span class="kf-cl">覆盖</span>
        <select id="kTopN" class="kf-inp">
          ${[30, 60, 120].map((n) => `<option value="${n}"${(fl.top_n || 30) === n ? " selected" : ""}>${n} 只</option>`).join("")}
        </select>
        <span class="kf-cl">板块</span>
        <select id="kPfxScope" class="kf-inp">
          <option value="">全部（均衡）</option>
          <option value="0">仅 0 开头</option>
          <option value="3">仅 3 开头</option>
          <option value="6">仅 6 开头</option>
        </select>
        <label class="kf-cl" style="cursor:pointer"><input type="checkbox" id="kFast"${fl.fast === false ? "" : " checked"} style="margin-right:4px;vertical-align:-1px">快速模式</label>
        <button class="btn primary" id="kronosRegen" onclick="kronosRun()">重新生成</button>
      </div>
    </div>
    <div class="pil-ms">
      <span class="s">引擎 ${esc(d.engine_ver || "")}</span>
      <span class="s">模型 ${esc(d.model_ver || "—")}</span>
      <span class="s">数据日 ${esc(d.data_date || "—")}</span>
      <span class="s">覆盖 ${esc(d.n_stocks || 0)} 只</span>
      <span class="s">本轮模式 ${esc(modeTxt)}</span>
      <span class="s">${ready ? "推理链就绪" : "推理链未就绪"}</span>
    </div>
    <div class="kf-prog" id="kProg"></div>`;
  let body;
  if (!ready) {
    body = `<div class="empty" style="padding:18px;line-height:1.9">
        <b>推理引擎尚未就绪（页面不会崩，只是还没装）。</b><br>
        ${esc((d.ready && d.ready.hint) || "请在查看器目录运行：python setup_kronos.py")}<br>
        <span class="hint">该脚本会安装 PyTorch(CPU) + 下载 Kronos 权重（首次需联网，约数十 MB～百 MB），之后完全离线。</span>
      </div>`;
  } else {
    const rows = kronosRows();
    const trs = rows.map((it) => {
      const cls = kScoreCls(it.score);
      const pct = it.pred_return == null ? "—"
        : (it.pred_return >= 0 ? "+" : "") + (it.pred_return * 100).toFixed(2) + "%";
      const pc = it.pred_return == null ? "" : (it.pred_return >= 0 ? "bull" : "bear");
      const open = !!KRONOS.open[it.code];
      const flg = toArr(it.flags).length ? ` <span class="k-flag">⚠${toArr(it.flags).length}</span>` : "";
      const main = `<tr class="k-row${open ? " open" : ""}" data-kopen="${esc(it.code)}" title="点这行看评分怎么算的">
          <td><b>${esc(it.code)}</b> <span class="k-pfx">${esc(it.prefix || "")}</span></td>
          <td>${esc(it.name || "")}</td>
          <td class="${pc}"><b>${esc(it.dir_label || "—")}</b></td>
          <td class="num"><div class="k-scwrap"><b class="ksc ${cls}">${it.score == null ? "—" : it.score.toFixed(0)}</b>
            <div class="kbar"><i class="${cls}" style="width:${it.score == null ? 0 : Math.max(2, Math.min(100, it.score))}%"></i></div></div></td>
          <td><span class="kg ${cls}">${esc(it.grade || "—")}</span>${flg}</td>
          <td class="num">${it.last_close == null ? "—" : it.last_close.toFixed(2)}</td>
          <td class="num ${pc}">${pct}</td>
          <td class="num">${it.bt_sharpe == null ? "—" : it.bt_sharpe.toFixed(2)}</td>
          <td class="num">${it.bt_winrate == null ? "—" : (it.bt_winrate * 100).toFixed(0) + "%"}</td>
          <td class="num">${it.bt_maxdd == null ? "—" : (it.bt_maxdd * 100).toFixed(1) + "%"}</td>
          <td class="k-verdict">${esc(it.verdict || "—")}</td>
        </tr>`;
      return main + (open ? `<tr class="k-det"><td colspan="11">${kDetail(it)}</td></tr>` : "");
    }).join("");
    const empty = rows.length
      ? trs
      : `<tr><td colspan="11" class="empty">当前筛选条件下 0 只。放宽「板块 / 方向 / 评分」任一条，或点「重新生成」扩覆盖面。</td></tr>`;
    body = kronosCtl() + kronosConcl() + `<div class="tbl-wrap"><table class="dt k-table">
        <thead><tr><th>代码</th><th>名称</th><th>方向</th><th class="num">评分</th><th>评级</th>
          <th class="num">最新收</th><th class="num">预测收益</th><th class="num">回测夏普</th>
          <th class="num">胜率</th><th class="num">回撤</th><th>结论</th></tr></thead>
        <tbody>${empty}</tbody></table></div>
      <div class="hint">点任意一行可展开「这分数是怎么算的」（四维分项 + 扣分）；评分衡量的是**这个判断值不值得参考**，
        与涨跌方向分开看。预测收益 = 模型预测未来 60 交易日末收 / 最新收 - 1。</div>`;
  }
  // 表格里的行点击切换明细：先摘掉旧监听再重绑，避免多次渲染后一次点击触发多次
  box.innerHTML = head + body + (() => {
    const cal = toArr(d.caliber).map((c) => `<li>${esc(c)}</li>`).join("");
    return `<div class="caliber"><b>口径与诚实声明</b><ul>${cal}</ul></div>`;
  })();
  $$("#p-kronos [data-kf-pfx]").forEach((el) => {
    el.onclick = () => { KRONOS.f.pfx = el.dataset.kfPfx || ""; renderKronos(); };
  });
  $$("#p-kronos [data-kf-dir]").forEach((el) => {
    el.onclick = () => { KRONOS.f.dir = el.dataset.kfDir || ""; renderKronos(); };
  });
  $$("#p-kronos [data-kf-min]").forEach((el) => {
    el.onclick = () => { KRONOS.f.min = Number(el.dataset.kfMin) || 0; renderKronos(); };
  });
  $$("#p-kronos [data-kf-sort]").forEach((el) => {
    el.onclick = () => { KRONOS.f.sort = el.dataset.kfSort || "score"; renderKronos(); };
  });
  const rs = $("#p-kronos [data-kf-reset]");
  if (rs) rs.onclick = () => { KRONOS.f = { pfx: "", dir: "", min: 0, sort: "score" }; renderKronos(); };
  $$("#p-kronos [data-kopen]").forEach((el) => {
    el.onclick = () => {
      const c = el.dataset.kopen;
      KRONOS.open[c] = !KRONOS.open[c];
      renderKronos();
    };
  });
}

function loadQuant(force) {
  const box = $("#p-quant");
  if (box && !QUANT.data) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">量化信号</div></div>
      <div class="empty">正在载入因子矩阵与自我学习状态…</div>`;
  }
  QUANT.busy = true;
  const qs = force ? "?t=" + Date.now() : "";
  return api("/api/quant" + qs).catch((e) => ({ ok: false, error: apiErrText(e), _err: true }))
    .then((d) => {
      // 2026-10-29：全市场因子在子进程后台算（不再在请求线程现算拖死全站），这里轮询等它落盘。
      if (d && d.computing) {
        const box = $("#p-quant");
        if (box) box.innerHTML = `<div class="pred-head"><div class="pred-title">量化信号</div></div>
          <div class="empty">量化面板首次计算中（全市场因子矩阵，约 2–3 分钟）…算完自动出现；其间其它面板不受影响，可正常切换。</div>`;
        QUANT.busy = false;
        clearTimeout(loadQuant._poll);
        loadQuant._poll = setTimeout(() => { try { loadQuant(); } catch (e) { /* 轮询失败忽略，下次再试 */ } }, 15000);
        return;
      }
      clearTimeout(loadQuant._poll);
      QUANT.data = d; QUANT.busy = false; renderQuant();
    });
}

function quantRun() {
  const btn = $("#quantRegen");
  if (btn) { btn.disabled = true; btn.textContent = "学习中…（约数秒）"; }
  api("/api/quant/run?force=1", { method: "POST", headers: { "Content-Type": "application/json" } })
    .then((d) => {  // 2026-10-19 修复：后端失败(ok:false)不再静默，把真实错误亮出来
      if (d && d.ok === false) reportLoadError("量化因子自我学习生成", new Error(d.error || "未知错误"));
    })
    .then(() => loadQuant(true))
    .then(() => { const b = $("#quantRegen"); if (b) { b.disabled = false; b.textContent = "重新学习"; } })
    .catch((e) => {
      const b = $("#quantRegen"); if (b) { b.disabled = false; b.textContent = "重新学习"; }
      reportLoadError("量化因子自我学习生成", e);
    });
}

function quantStageCls(stage) {
  if (!stage) return "neu";
  if (stage.indexOf("样本外验证通过") === 0) return "bull";
  if (stage.indexOf("收缩生效中") === 0) return "ch-A";
  return "neu";
}

function renderQuant() {
  const box = $("#p-quant");
  if (!box) return;
  const d = QUANT.data;
  if (!d) {
    box.innerHTML = `<div class="pred-head"><div class="pred-title">量化信号</div></div>
      <div class="empty">加载中…</div>`;
    return;
  }
  if (d.ok === false || d._err) {
    box.innerHTML = `<div class="pred-head">
        <div class="pred-title">量化信号</div>
        <button class="btn primary" id="quantRegen" onclick="quantRun()">重新学习</button></div>
      <div class="empty">${esc(d.note || d.error || "接口不可用")}</div>
      <div class="hint">若刚更新过界面：这是新板块，需要<b>关掉查看器窗口、重新双击桌面图标</b>（后端要重启，光刷新页面不够）。</div>`;
    return;
  }
  const head = `<div class="pred-head">
      <div class="pred-title">量化信号</div>
      <button class="btn primary" id="quantRegen" onclick="quantRun()">重新学习</button>
    </div>
    <div class="pil-ms">
      <span class="s">引擎版本 ${esc(d.engine_ver || "")}</span>
      <span class="s">学习期次 ${esc(d.build_date || "—")}</span>
      <span class="s">宇宙 ${esc((d.select && d.select.n_universe) || (d.backtest && d.backtest.n_universe) || "—")} 只</span>
    </div>`;
  const active = QUANT_TABS.some((t) => t.key === QUANT.qtab) ? QUANT.qtab : "panorama";
  const tabRow = `<div class="pillar-tabs">` + QUANT_TABS.map((t) => {
    let n = 0;
    if (t.key === "select") n = toArr(d.select && d.select.items).length;
    else if (t.key === "backtest") n = toArr(d.backtest && d.backtest.factors).length;
    else if (t.key === "recipe") n = toArr(d.recipes).length;
    else if (t.key === "learn") n = toArr(d.self_learn && d.self_learn.items).length;
    return `<a class="sub-tab${t.key === active ? " active" : ""}" href="javascript:void(0)"
      title="${escAttr(t.label)}" onclick="quantTab('${t.key}')">${esc(t.label)}${n ? ` <b>${n}</b>` : ""}</a>`;
  }).join("") + `</div>`;
  let body = "";
  if (active === "panorama") body = quantPanoramaView();
  else if (active === "select") body = quantSelectView(d.select);
  else if (active === "backtest") body = quantBacktestView(d.backtest);
  else if (active === "recipe") body = quantRecipeView(d);
  else body = quantLearnView(d);
  const cal = toArr(d.caliber);
  const caliber = cal.length ? `<details class="res-cal"><summary>这个面板的口径（${cal.length} 条，全部可追溯）</summary>
    <ul>${cal.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "";
  const foot = `<div class="pil-foot"><div class="s">${esc(d.note || "")}</div></div>`;
  box.innerHTML = head + tabRow + body + caliber + foot;
}

/* 因子全景（M1）：单票 7 因子打分（oriented → [-1,1] 展示分 + 原始值）。纯本地 K 线离线算。 */
function quantPanoramaView() {
  const code = QUANT.panoramaCode || "";
  const d = QUANT.panorama;
  const head = `<div class="quant-pano-h">
      输入股票代码查单票技术因子全景（纯本地 K 线离线算，不依赖同花顺 / 不硬联网）：
      <input id="quantPanoCode" class="inp" value="${esc(code)}" placeholder="如 000002" maxlength="6"
        onkeydown="if(event.key==='Enter')quantPanoGo()" />
      <button class="btn sm" onclick="quantPanoGo()">查因子</button>
    </div>`;
  if (!code) {
    return head + `<div class="empty">输入 6 位 A 股代码查看其均线多空 / MACD / KDJ / 布林 / 量能 / 动量 / 波动率 七因子打分。</div>`;
  }
  if (!d) return head + `<div class="empty">加载中…</div>`;
  if (d.ok === false || d._err) {
    return head + `<div class="empty">${esc(d.reason || d.error || "无该票数据（本地 K 线不足）")}</div>`;
  }
  const bars = toArr(d.factors);
  const famOrder = ["动量", "技术形态", "量价", "质量", "估值", "成长", "情绪"];
  const byFam = {};
  bars.forEach((f) => { (byFam[f.family] = byFam[f.family] || []).push(f); });
  const famHtml = famOrder.filter((fam) => byFam[fam]).map((fam) => {
    const rows = byFam[fam].map((f) => {
      const sc = f.score == null ? 0 : Math.max(-1, Math.min(1, f.score));
      const cls = sc >= 0.2 ? "bull" : (sc <= -0.2 ? "bear" : "neu");
      return `<tr>
        <td class="q-td-name"><b>${esc(f.label)}</b><div class="q-td-sub">${esc(f.key)}</div></td>
        <td class="q-td-raw">${f.raw == null ? "—" : esc(String(f.raw))}${esc(f.unit || "")}</td>
        <td class="q-td-bar"><div class="q-bar"><span class="q-bar-mid"></span>
          <span class="q-bar-fill ${cls}" style="width:${Math.abs(sc) * 50}%"></span></div>
          <span class="q-num ${cls}">${sc >= 0 ? "+" : ""}${(sc * 100).toFixed(0)}</span></td>
      </tr>`;
    }).join("");
    return `<div class="q-fam"><div class="q-fam-h">${esc(fam)}</div>
      <table class="q-tbl"><tbody>${rows}</tbody></table></div>`;
  }).join("");
  const pending = toArr(d.pending || (QUANT.data && QUANT.data.pending_factors) || []);
  const pendHtml = pending.length ? `<div class="q-pending">待接入因子（需 akshare/联网，本沙箱不可用，不编数据、不参学）：${
    pending.map((p) => `<span class="chip-s res-warn" title="${escAttr(p.status)}">${esc(p.label)}</span>`).join(" ")
  }</div>` : "";
  return head + `<div class="quant-pano-meta">${esc(d.name || "")} ${esc(d.code)} · 数据日 ${esc(d.data_date || "—")}</div>`
    + famHtml + pendHtml;
}

function quantPanoGo() {
  const inp = $("#quantPanoCode");
  const code = (inp && inp.value || "").trim();
  if (!/^\d{6}$/.test(code)) { alert("请输入 6 位 A 股代码"); return; }
  QUANT.panoramaCode = code;
  QUANT.panorama = null;
  if (QUANT.qtab !== "panorama") QUANT.qtab = "panorama";
  api("/api/quant/panorama?code=" + encodeURIComponent(code))
    .then((dd) => { QUANT.panorama = dd; renderQuant(); })
    .catch((e) => { QUANT.panorama = { ok: false, _err: true, error: apiErrText(e) }; renderQuant(); });
}

/* 多因子选股（M2）：截面用 metalearn 默认引擎加权出清单；诚实三态徽章。 */
function quantSelectView(d) {
  if (!d || d.ok === false) {
    return `<div class="empty">${esc((d && (d.reason || d.error)) || "多因子选股不可用")}</div>`;
  }
  const w = d.weights || {};
  const wParts = QUANT_FACTOR_ORDER.filter((k) => w[k] != null)
    .map((k) => `${esc(QUANT_FACTOR_LABEL[k] || k)} ${(w[k] * 100).toFixed(0)}%`).join(" · ");
  const badge = d.weight_stage ? `<span class="badge ${quantStageCls(d.weight_stage)} sl-stage">${esc(d.weight_stage)}</span>` : "";
  const head = `<div class="quant-sel-h">
      ${badge}
      <span class="s">数据日 ${esc(d.data_date || "—")}</span>
      <span class="s">宇宙 ${esc(d.n_universe || "—")} 只</span>
      <span class="s">出 ${esc(d.top_n || "—")} 只</span>
    </div>
    <div class="q-weights">因子权重（数据学出，唯一真源 = metalearn.weights_from_ic）：${wParts}</div>`;
  const items = toArr(d.items).map((it) => {
    const facs = toArr(it.detail && it.detail.factor_pct);
    return `<div class="res-card">
      <div class="res-card-h">
        <span class="pil-rank">${esc(it.rank)}</span>
        ${codeLink(it.symbol, { text: it.name || it.symbol, cls: "res-name" })}
        <span class="res-code">${esc(it.symbol)}</span>
        <span class="res-score" title="截面复合分">${Math.round((it.score || 0) * 1000) / 10}</span>
      </div>
      <div class="res-bar"><span style="width:${Math.max(0, Math.min(100, Math.round((it.score || 0) * 50 + 50)))}%"></span></div>
      ${facs.length ? `<div class="res-tags">${facs.map((x) => `<span class="res-tag">${esc(x)}</span>`).join("")}</div>` : ""}
      <div class="res-verdict">${esc(it.reason || "")}</div>
    </div>`;
  }).join("");
  const pend = toArr(d.pending_inactive);
  const pendHtml = pend.length ? `<div class="q-pending">本清单仅用 7 个可学技术因子；以下因子待接入、未参学：${
    pend.map((x) => `<span class="chip-s res-warn">${esc(x)}</span>`).join(" ")
  }</div>` : "";
  return head + `<div class="res-grid">${items || `<div class="empty">无入选标的</div>`}</div>` + pendHtml;
}

/* 因子回测（M3）：各因子 IC / IR / 衰减（T+5/T+10/T+20）。 */
function quantBacktestView(d) {
  if (!d || d.ok === false) {
    return `<div class="empty">${esc((d && (d.reason || d.error)) || "因子回测不可用")}</div>`;
  }
  const icCls = (v) => v == null ? "" : (v >= 0.02 ? "bull" : (v <= -0.02 ? "bear" : "neu"));
  const tb = toArr(d.factors).map((f) => `<tr>
      <td class="q-td-name"><b>${esc(f.label)}</b><div class="q-td-sub">${esc(f.family)}｜${esc(f.key)}</div></td>
      <td class="${icCls(f.ic5)}">${f.ic5 == null ? "样本不足" : (f.ic5 >= 0 ? "+" : "") + f.ic5.toFixed(4)}</td>
      <td class="q-td-sub">${f.ir5 == null ? "—" : (f.ir5 >= 0 ? "+" : "") + f.ir5.toFixed(3)}</td>
      <td>${f.ic10 == null ? "—" : (f.ic10 >= 0 ? "+" : "") + f.ic10.toFixed(4)}</td>
      <td>${f.ic20 == null ? "—" : (f.ic20 >= 0 ? "+" : "") + f.ic20.toFixed(4)}</td>
      <td class="q-td-sub">${f.n5}</td>
      <td class="q-td-sub">${esc(f.status)}</td>
    </tr>`).join("");
  const famRows = `<table class="q-tbl wide"><thead><tr>
      <th>因子</th><th>IC@T+5</th><th>IR@T+5</th><th>IC@T+10</th><th>IC@T+20</th><th>样本n5</th><th>状态</th>
    </tr></thead><tbody>${tb}</tbody></table>`;
  const pend = toArr(d.pending);
  const pendHtml = pend.length ? `<div class="q-pending">待接入因子（需 akshare/联网，本沙箱不可用，不编数据、不参学）：${
    pend.map((p) => `<span class="chip-s res-warn" title="${escAttr(p.status)}">${esc(p.label)}</span>`).join(" ")
  }</div>` : "";
  return `<div class="quant-bt-meta">回测宇宙 ${esc(d.n_universe || "—")} 只 · 信号日 ${esc(d.n_dates || "—")} 个 · 末日 ${esc(d.last_date || "—")}</div>`
    + famRows + pendHtml;
}

/* 菜谱预设（M4）：进哥「私家配比」入口 + 强制报 OOS 外样 Sharpe/胜率。 */
function quantRecipeView(d) {
  const recipes = toArr(d.recipes);
  const listHtml = recipes.length ? `<div class="q-recipe-list">${recipes.map((r) => {
    const w = r.weights || {};
    const wParts = QUANT_FACTOR_ORDER.filter((k) => w[k] != null)
      .map((k) => `${esc(QUANT_FACTOR_LABEL[k] || k)} ${(w[k] * 100).toFixed(0)}%`).join(" · ");
    return `<div class="q-recipe">
      <div class="q-recipe-h"><b>${esc(r.name)}</b><span class="s">#${r.id} · ${esc(r.created_at || "")}</span></div>
      <div class="q-recipe-w">${wParts}</div>
      <div class="q-recipe-act">
        <button class="btn sm" onclick="quantRecipeApply(${r.id})">应用（报 OOS）</button>
        <span class="q-recipe-res" id="qrec-res-${r.id}"></span>
      </div>
    </div>`;
  }).join("")}</div>` : `<div class="empty">暂无菜谱，先在下方添加一个进哥私家配比。</div>`;
  const form = `<div class="q-recipe-form">
      <div class="q-recipe-form-h">新增菜谱（填部分因子即可，保存时归一化；作为引擎先验输入）</div>
      <div class="q-recipe-inputs">${QUANT_FACTOR_ORDER.map((k) => `<label class="q-inp"><span>${esc(QUANT_FACTOR_LABEL[k] || k)}</span>
        <input class="inp" id="qw-${k}" type="number" step="0.01" min="0" placeholder="0" /></label>`).join("")}</div>
      <div class="q-recipe-name"><span>名称</span><input class="inp" id="qw-name" placeholder="如 进哥稳健型" maxlength="20" /></div>
      <button class="btn primary" onclick="quantRecipeSave()">保存菜谱</button>
      <span id="qw-msg" class="s"></span>
    </div>`;
  return listHtml + form;
}

function quantRecipeSave() {
  const name = (($("#qw-name") || {}).value || "").trim();
  const weights = {};
  QUANT_FACTOR_ORDER.forEach((k) => {
    const v = parseFloat((($("#qw-" + k) || {}).value || "0"));
    if (!isNaN(v) && v > 0) weights[k] = v;
  });
  const msg = $("#qw-msg");
  if (!name) { if (msg) msg.textContent = "请填名称"; return; }
  if (!Object.keys(weights).length) { if (msg) msg.textContent = "至少填一个正权重"; return; }
  api("/api/quant/recipe", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, weights: weights }) })
    .then((r) => {
      if (msg) msg.textContent = (r && r.ok) ? "已保存，刷新列表…" : ("失败：" + ((r && r.error) || ""));
      if (r && r.ok) loadQuant(true);
    })
    .catch((e) => { if (msg) msg.textContent = "保存失败：" + apiErrText(e); });
}

function quantRecipeApply(id) {
  const res = $("#qrec-res-" + id);
  if (res) res.textContent = "计算中…";
  api("/api/quant/recipe/apply?id=" + id, { method: "POST", headers: { "Content-Type": "application/json" } })
    .then((r) => {
      if (!r || r.ok === false) { if (res) res.textContent = "失败：" + ((r && r.error) || ""); return; }
      const o = r.oos || {};
      const txt = `OOS（面板外样回溯，T+5）：n=${o.n_days || 0} 期，菜谱组合均值 ${((o.recipe_mean || 0) * 100).toFixed(2)}%`
        + `（Sharpe ${o.recipe_sharpe == null ? "—" : o.recipe_sharpe}），胜率 ${((o.recipe_win || 0) * 100).toFixed(1)}%；`
        + `等权均值 ${((o.equal_mean || 0) * 100).toFixed(2)}%，胜率 ${((o.equal_win || 0) * 100).toFixed(1)}%；`
        + `加权跑赢等权 ${((o.beat || 0) * 100).toFixed(1)}% 期。`
        + (r.oos_warning ? `<span class="res-warn"> ${esc(r.oos_warning)}</span>` : "");
      if (res) res.innerHTML = txt;
    })
    .catch((e) => { if (res) res.textContent = "失败：" + apiErrText(e); });
}

/* 量化因子的自我学习与成长（M5）：实例化 metalearn 于因子，诚实三态徽章。 */
function quantLearnView(d) {
  const sl = d.self_learn;
  if (!sl || !sl.state) {
    return `<div class="empty">量化因子的自我学习尚未生成（点上方「重新学习」，后端约数秒算完因子面板 + walk-forward）。</div>`;
  }
  const st = sl.state || {};
  const stage = st.weight_stage || "等权运行中";
  const badge = `<span class="badge ${quantStageCls(stage)} sl-stage">${esc(stage)}</span>`;
  const w = st.weights || {};
  const wParts = QUANT_FACTOR_ORDER.filter((k) => w[k] != null)
    .map((k) => `<b>${esc(QUANT_FACTOR_LABEL[k] || k)} ${(w[k] * 100).toFixed(0)}%</b>`).join(" · ");
  const oos = st.oos || {};
  const oosTxt = `${esc(sl.run_date || "")} 期 · 样本 ${st.samples_total || 0} 个（T+5 主轴）`
    + (oos && oos.n ? ` · OOS 可比 ${oos.n} 期，加权胜率 ${oos.win_rate == null ? "—" : (oos.win_rate * 100).toFixed(0) + "%"}，均值差 ${(oos.mean_diff >= 0 ? "+" : "")}${oos.mean_diff || 0}，${oos.passed ? "通过" : "未通过"}` : " · OOS 无可比期");
  const head = `<div class="quant-learn-h">
      ${badge}
      <span class="s">${esc(oosTxt)}</span>
    </div>
    <div class="q-weights">学到的因子权重（走 metalearn.weights_from_ic；IC 收缩 + 先验混合 + 单维下限）：${wParts}</div>`;
  const pf = st.per_factor || {};
  const pfRows = QUANT_FACTOR_ORDER.filter((k) => pf[k]).map((k) => {
    const p = pf[k];
    const icCls = p.ic5 == null ? "" : (p.ic5 >= 0.02 ? "bull" : (p.ic5 <= -0.02 ? "bear" : "neu"));
    return `<tr>
      <td class="q-td-name"><b>${esc(p.label || k)}</b></td>
      <td class="${icCls}">${p.ic5 == null ? "样本不足" : (p.ic5 >= 0 ? "+" : "") + p.ic5.toFixed(4)}</td>
      <td class="q-td-sub">${p.n5 || 0}</td>
      <td class="q-td-sub">${st.shrunk_ic && st.shrunk_ic[k] != null ? st.shrunk_ic[k].toFixed(4) : "—"}</td>
    </tr>`;
  }).join("");
  const pfTable = `<table class="q-tbl"><thead><tr><th>因子</th><th>IC@T+5</th><th>样本n</th><th>收缩后IC</th></tr></thead><tbody>${pfRows}</tbody></table>`;
  const items = toArr(sl.items);
  const itemsHtml = items.length ? `<div class="res-grid">${items.slice(0, 30).map((it) => `
    <div class="res-card">
      <div class="res-card-h">
        <span class="pil-rank">${esc(it.rank)}</span>
        ${codeLink(it.symbol, { text: it.name || it.symbol, cls: "res-name" })}
        <span class="res-code">${esc(it.symbol)}</span>
        <span class="res-score">${Math.round((it.score || 0) * 1000) / 10}</span>
      </div>
      <div class="res-verdict">${esc(it.reason || "")}</div>
    </div>`).join("")}</div>` : `<div class="empty">本期无入选标的</div>`;
  const note = sl.note ? `<div class="q-note">${esc(sl.note)}</div>` : "";
  return head + pfTable + note + itemsHtml;
}

function renderBiliPile(cat, mountSel, withConsensus) {
  const box = $(mountSel);
  if (!box) return;
  box.innerHTML = `<div class="empty">正在读取 B站分堆数据…</div>`;
  const consP = withConsensus
    ? api("/api/bili/consensus?cat=" + encodeURIComponent(cat) + "&days=30").catch(() => null)
    : Promise.resolve(null);
  consP
    .then((cons) => {
      const consHtml = cons && cons.ok ? buildBiliConsensus(cons) : "";
      return api("/api/bili/pile?cat=" + encodeURIComponent(cat) + "&limit=4000").then((d) => {
        const rows = (d && d.rows) || [];
        box.innerHTML = consHtml + buildBiliEvidence(rows, cat);
      });
    })
    .catch((e) => { box.innerHTML = `<div class="empty">加载失败：${esc(apiErrText(e))}</div>`; });
}

/* 结论区：板块方向 + 个股热度（2026-10-12 王总：先给结论，标题堆降级为证据） */
function buildBiliConsensus(cons) {
  const secs = toArr(cons.sectors);
  const stks = toArr(cons.stocks);
  const secHtml = secs.length
    ? secs.map((s) => `
      <div class="bc-row">
        <div class="bc-name">${esc(s.sector)} <span class="bc-tone tone-${esc(s.tone)}">${esc(s.tone_label)}</span></div>
        <div class="bc-bar"><span style="width:${s.w}%"></span></div>
        <div class="bc-meta">${s.n} 条视频 · ${s.bloggers_n} 位博主</div>
      </div>`).join("")
    : `<div class="bc-empty">近 ${cons.days} 天没有明确的板块指向</div>`;
  const stkHtml = stks.length
    ? stks.map((s) => `
      <div class="bc-row">
        <div class="bc-name">${codeLink(s.code, { text: s.name })} <span class="bc-tone tone-${esc(s.tone)}">${esc(s.tone_label)}</span></div>
        <div class="bc-bar"><span style="width:${s.w}%"></span></div>
        <div class="bc-meta">${s.n} 次点名 · ${s.bloggers_n} 位博主</div>
      </div>`).join("")
    : `<div class="bc-empty">近 ${cons.days} 天博主没点名具体个股</div>`;
  const basis = toArr(cons.basis).map((b) => `<li>${esc(b)}</li>`).join("");
  return `
    <div class="bc-wrap">
      <div class="bc-verdict"><span class="bc-flag">结论</span>${esc(cons.verdict || "")}</div>
      <div class="bc-cols">
        <div class="bc-col"><div class="bc-col-h">① 博主集中聊的板块方向</div>${secHtml}</div>
        <div class="bc-col"><div class="bc-col-h">② 点名最多的个股（可点开看诊断）</div>${stkHtml}</div>
      </div>
      <details class="bc-basis"><summary>这个结论怎么算的（口径）</summary><ul>${basis}</ul></details>
    </div>`;
}

/* 证据区：原始视频堆，按博主聚合（王总：链接本身对决策没意义，下沉为证据备查） */
function buildBiliEvidence(rows, cat) {
  if (!rows.length) {
    return `<div class="bc-ev"><div class="bc-ev-h">证据 · 原始视频堆（按博主聚合）</div><div class="empty">该分堆暂无视频</div></div>`;
  }
  const byB = {};
  rows.forEach((r) => { (byB[r.blogger] = byB[r.blogger] || []).push(r); });
  const html = Object.keys(byB).sort().map((b) => {
    const items = byB[b].map((r) => {
      const link = r.url
        ? `<a href="${escAttr(r.url)}" target="_blank" rel="noopener" style="color:var(--accent,#0E7490);text-decoration:none">${esc(r.title)}</a>`
        : esc(r.title);
      return `<li style="padding:3px 0;border-bottom:1px dashed rgba(128,128,128,.18)">` +
             `<span style="color:var(--muted,#888);font-size:12px;margin-right:8px">${esc(r.vdate || "")}</span>${link}</li>`;
    }).join("");
    return `<div style="margin:10px 0;padding:10px 12px;background:var(--panel,#fff);border:1px solid var(--line,#e5e7eb);border-radius:10px">` +
           `<div style="font-weight:600;margin-bottom:6px">@${esc(b)} <span style="color:var(--muted,#888);font-weight:400;font-size:12px">${byB[b].length} 条</span></div>` +
           `<ul style="list-style:none;margin:0;padding:0">${items}</ul></div>`;
  }).join("");
  return `<div class="bc-ev"><div class="bc-ev-h">证据 · 原始视频堆（按博主聚合，${rows.length} 条）</div>${html}</div>`;
}

/* ============ 2026-10-11 王总第三回合②：B站 UP 主按类分组框 ============
   2026-10-11 晚 重构（王总指出前后不一致）：原实现读「知识轨道注册表」（yaml+kb_doc，
   固定 7 人），删了数据的博主仍显示、新加的博主进不来。现改为**以 bili_pile 真实数据为准**：
   价值/技术/情绪三堆按博主实际视频聚合；有知识轨道的博主标「有知识库」、点名进知识沉淀，
   没有的点开看该人视频列表——注册表与数据从此不会再度脱节。 */
const BILI_BLOGGER_TRACK = {
  "土斯土耶夫斯基": "bili_tusi", "吃素的牛筋丸": "bili_niujin", "鼠柚子Aura": "bili_shuyouzi",
  "土豆看财报": "bili_tudou", "李大霄": "bili_lidaxiao", "知行图表交易": "bili_zhixing",
};
const BILI_GROUP_ORDER = [
  { key: "value", label: "价值主播" },
  { key: "technical", label: "技术主播" },
  { key: "sentiment", label: "情绪主播" },
];
function renderBiliAuthors() {
  const box = $("#p-bili_authors");
  if (!box) return;
  box.innerHTML = `<div class="empty">正在读取 B站博主数据…</div>`;
  api("/api/bili/pile?limit=5000")
    .then((d) => {
      const rows = (d && d.rows) || [];
      if (!rows.length) { box.innerHTML = `<div class="empty">后台暂无 B站视频数据（bili_pile 为空）</div>`; return; }
      const byB = {};
      rows.forEach((r) => {
        if (r.cat === "weekly") return;   // 「下周当红」不算三堆归属
        const o = (byB[r.blogger] = byB[r.blogger] || { value: 0, technical: 0, sentiment: 0, total: 0, last: "" });
        o[r.cat] = (o[r.cat] || 0) + 1;
        o.total += 1;
        if ((r.vdate || "") > o.last) o.last = r.vdate || "";
      });
      const html = BILI_GROUP_ORDER.map((g) => {
        const members = Object.keys(byB).filter((b) => byB[b][g.key] > 0)
          .sort((a, b) => (byB[b][g.key] - byB[a][g.key]) || (byB[b].total - byB[a].total));
        if (!members.length) return "";
        /* 2026-10-12 王总：原 btn sm 小药丸 + 11px 内联字太小看不清 → 改股票卡片式大卡（.ba-card，样式在 app.css） */
        const cards = members.map((b) => {
          const o = byB[b], dk = BILI_BLOGGER_TRACK[b];
          const oths = BILI_GROUP_ORDER.filter((x) => x.key !== g.key && o[x.key] > 0)
            .map((x) => `<span class="ba-oth">${esc(x.label.slice(0, 2))} <b>${o[x.key]}</b></span>`).join("");
          return `<div class="ba-card${dk ? " has-kb" : ""}" data-blogger="${escAttr(b)}" data-dk="${escAttr(dk || "")}" ` +
            `title="最近更新 ${esc(o.last)} · 三堆共 ${o.total} 条 · 点${dk ? "看知识沉淀" : "开视频列表"}">` +
            `<div class="ba-head"><span class="ba-name">${esc(b)}</span>${dk ? `<span class="ba-kb">有知识库</span>` : ""}</div>` +
            `<div class="ba-sub">最近更新 ${esc(o.last || "—")} · 三堆共 ${o.total} 条</div>` +
            `<div class="ba-stat">${esc(g.label.slice(0, 2))}视频<b>${o[g.key]}</b>条</div>` +
            (oths ? `<div class="ba-others">${oths}</div>` : `<div class="ba-others ba-none">仅本类有视频</div>`) +
            `</div>`;
        }).join("");
        return `<div class="ba-sec"><div class="ba-sec-hd">${esc(g.label)} <span>${members.length} 人</span></div>` +
          `<div class="ba-grid">${cards}</div></div>`;
      }).join("");
      box.innerHTML = `<div style="margin-bottom:10px;color:var(--muted,#888);font-size:13px">B站 UP 主按类分组 · 与后台视频数据实时同步 · 点「有知识库」的看知识沉淀，其余看视频列表</div>${html}`;
      $$("#p-bili_authors .ba-card[data-blogger]").forEach((btn) => {
        btn.onclick = () => {
          const dk = btn.dataset.dk;
          if (dk) selectNav("learn", "learn_" + dk, true);
          else openBiliBloggerVideos(btn.dataset.blogger);
        };
      });
    })
    .catch((e) => { box.innerHTML = `<div class="empty">加载失败：${esc(apiErrText(e))}</div>`; });
}
function openBiliBloggerVideos(blogger) {
  api("/api/bili/pile?blogger=" + encodeURIComponent(blogger) + "&limit=2000")
    .then((d) => {
      const rows = (d && d.rows) || [];
      const items = rows.map((r) => {
        const link = r.url
          ? `<a href="${escAttr(r.url)}" target="_blank" rel="noopener" style="color:var(--accent,#0E7490);text-decoration:none">${esc(r.title)}</a>`
          : esc(r.title);
        return `<li style="padding:3px 0;border-bottom:1px dashed rgba(128,128,128,.18)">` +
          `<span style="color:var(--muted,#888);font-size:12px;margin-right:8px">${esc(r.vdate || "")}</span>${link}</li>`;
      }).join("");
      openDrawer("@B站@" + blogger + " · 视频列表（" + rows.length + " 条）",
        rows.length ? `<ul style="list-style:none;margin:0;padding:0">${items}</ul>` : `<div class="empty">该博主暂无视频数据</div>`, null);
    })
    .catch((e) => { openDrawer("@B站@" + blogger, `<div class="empty">加载失败：${esc(apiErrText(e))}</div>`, null); });
}

/* ============ 2026-10-11 王总第三回合③：技术·价值教学「方法卡片网格」 ============
   王总痛点：旧的视频列表（谁发的/何时发的/忽悠描述）没价值；要的是一格一方法、
   方法短名+人话总结、回测胜率/收益看得见、点开看样例。数据来自 /api/bili/methods。 */
const BILI_METHODS = { data: null, error: null };

function mtRatingClass(rating) {
  if (!rating) return "mt-rat-un";
  if (rating.indexOf("优秀") >= 0) return "mt-rat-a";
  if (rating.indexOf("良好") >= 0) return "mt-rat-b";
  if (rating.indexOf("一般") >= 0) return "mt-rat-c";
  if (rating.indexOf("较差") >= 0) return "mt-rat-d";
  return "mt-rat-un";
}
function mtPct(v) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  return (n > 0 ? "+" : "") + n.toFixed(1) + "%";
}
function mtNumCls(v) { const n = Number(v); if (isNaN(n)) return ""; return n >= 0 ? "mt-num-bull" : "mt-num-bear"; }

function loadBiliMethods() {
  const box = $("#p-bili_techvalue");
  if (!box) return;
  if (BILI_METHODS.data) { renderBiliMethods(); return; }
  box.innerHTML = `<div class="empty">正在读取方法卡片数据…</div>`;
  api("/api/bili/methods?cat=technical,value")
    .then((d) => { BILI_METHODS.data = d; renderBiliMethods(); })
    .catch((e) => { BILI_METHODS.error = e; box.innerHTML = `<div class="empty">加载失败：${esc(apiErrText(e))}</div>`; });
}

function renderBiliMethods() {
  const box = $("#p-bili_techvalue");
  if (!box) return;
  const d = BILI_METHODS.data;
  if (!d || !d.methods) { box.innerHTML = `<div class="empty">暂无方法数据</div>`; return; }
  const ms = d.methods || [];
  const head =
    `<div class="mt-head"><div class="mt-head-l">共 <b>${esc(d.total || 0)}</b> 条教学视频 · 归并为 <b>${ms.length}</b> 个方法 · 未归类 <b>${esc(d.unmatched || 0)}</b> 条</div>`
    + `<div class="mt-head-r">一格一方法 · 点卡片看样例视频与回测口径</div></div>`
    + `<details class="mt-basis"><summary>数字口径（点开看，重要）</summary>`
    + (d.basis || []).map((b) => `<div class="mt-basis-row">${esc(b)}</div>`).join("")
    + `</details>`;
  const cards = ms.map((m) => {
    const b = m.best || {};
    const mapped = m.mapped;
    const back = mapped
      ? `<div class="mt-back">`
        + `<span class="mt-rat ${mtRatingClass(b.rating)}">${esc(b.rating || "—")}</span>`
        + `<span class="mt-back-num"><i>胜率</i><b>${b.win_rate != null ? Number(b.win_rate).toFixed(1) + "%" : "—"}</b></span>`
        + `<span class="mt-back-num"><i>区间收益</i><b class="${mtNumCls(b.total_return)}">${mtPct(b.total_return)}</b></span>`
        + `<span class="mt-back-num"><i>样本</i><b>${b.trades != null ? b.trades + " 笔" : "—"}</b></span>`
        + `</div>`
        + `<div class="mt-strat">回测规则：${esc(b.name || b.key || "")}</div>`
      : `<div class="mt-back mt-back-un"><span class="mt-rat mt-rat-un">未回测</span>`
        + `<span class="mt-un-note">暂无可量化选股规则，不编造数字</span></div>`;
    return `<div class="mt-card ${mapped ? "mt-card-ok" : "mt-card-un"}" data-mt-key="${escAttr(m.key)}" role="button" tabindex="0">`
      + `<div class="mt-card-head"><span class="mt-name">${esc(m.name)}</span>`
        + `<span class="mt-kind ${m.kind === "价值面" ? "mt-kind-val" : "mt-kind-tech"}">${esc(m.kind)}</span></div>`
      + `<div class="mt-sum">${esc(m.summary)}</div>`
      + `<div class="mt-meta"><span>${m.n} 条视频</span><span>${m.bloggers_n} 位博主</span>`
        + `${m.last_date ? `<span>最近 ${esc(m.last_date)}</span>` : ""}</div>`
      + back + `</div>`;
  }).join("");
  box.innerHTML = head + `<div class="mt-grid">${cards}</div>`;
  $$("#p-bili_techvalue .mt-card").forEach((el) => {
    const open = () => { const m = ms.find((x) => x.key === el.dataset.mtKey); if (m) openBiliMethodDrawer(m); };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  // 2026-10-11 王总：「技术·价值教学」页顶部也放「一键筛股」入口
  injectBiliScanBar(box);
}

function openBiliMethodDrawer(m) {
  const b = m.best || {};
  const stratRows = (m.strategies || []).map((s) =>
    `<tr><td>${esc(s.name || s.key)}</td>`
    + `<td class="mt-c-rat ${mtRatingClass(s.rating)}">${esc(s.rating || "—")}</td>`
    + `<td class="num">${s.win_rate != null ? Number(s.win_rate).toFixed(1) + "%" : "—"}</td>`
    + `<td class="num ${mtNumCls(s.total_return)}">${mtPct(s.total_return)}</td>`
    + `<td class="num">${s.trades != null ? s.trades : "—"}</td>`
    + `<td class="num">${s.rank != null ? s.rank : "—"}</td></tr>`
  ).join("");
  const samples = (m.samples || []).map((s) => {
    const link = s.url
      ? `<a href="${escAttr(s.url)}" target="_blank" rel="noopener" style="color:var(--accent,#0E7490);text-decoration:none">${esc(s.title)}</a>`
      : esc(s.title);
    return `<li><span class="mt-samp-date">${esc(s.vdate || "")}</span>${link}</li>`;
  }).join("");
  const html =
    `<div class="mt-dlg-head"><span class="mt-name">${esc(m.name)}</span>`
    + `<span class="mt-kind ${m.kind === "价值面" ? "mt-kind-val" : "mt-kind-tech"}">${esc(m.kind)}</span></div>`
    + `<p class="mt-dlg-sum">${esc(m.summary)}</p>`
    + `<div class="mt-dlg-stat"><span><b>${m.n}</b> 条视频</span><span><b>${m.bloggers_n}</b> 位博主</span>`
    + `<span>热门：${(m.top_bloggers || []).map(esc).join("、") || "—"}</span></div>`
    + (m.mapped
        ? `<h4 class="mt-dlg-h">回测表现（近两年真实回测 · 库内候选池）</h4>`
          + `<table class="mt-tbl"><thead><tr><th>规则</th><th>评级</th><th>胜率</th><th>区间收益</th><th>样本</th><th>排名</th></tr></thead>`
          + `<tbody>${stratRows}</tbody></table>`
        : `<div class="mt-dlg-un">该方法暂无可量化的选股规则，未做回测——不编造数字。</div>`)
    + `<h4 class="mt-dlg-h">样例视频（最近 ${Math.min((m.samples || []).length, 40)} 条）</h4>`
    + (samples ? `<ul class="mt-samp">${samples}</ul>` : `<div class="mt-dlg-un">无样例</div>`);
  openDrawer("方法 · " + m.name, html, null);
}

/* ========== 一键筛股（2026-10-11 王总需求） ==========
   结论为主、解释可下钻：
   - 顶部「⚡ 一键筛股」按钮 + 结果区；
   - 后端单飞扫全A股（≈2~3 分钟），前端 4s 轮询拿 warming/done；
   - 命中票卡片：股票 + 收盘价 + 涨跌幅 + 命中战法 chips（chip 显「入手值得率」+ 评级）；
   - 点战法 chip → 抽屉看该战法解释 + 该规则历史回测数字。 */
const BILI_SCAN = { polling: false };

function injectBiliScanBar(box) {
  if (!box) return;
  if (box.querySelector("#bili-scan-bar")) { loadBiliScan(false); return; }  // 重渲染：不重复插，仅刷新
  ensureBiliScanStyle();
  const bar = document.createElement("div");
  bar.className = "bili-scan-bar";
  bar.id = "bili-scan-bar";
  bar.innerHTML =
    `<div class="bili-scan-bar-l">`
    + `<button class="btn primary" id="bili-scan-btn" onclick="loadBiliScan(false)">⚡ 一键筛股（当前买点票）</button>`
    + `<span class="bili-scan-status" id="bili-scan-status">加载中…</span>`
    + `</div>`
    + `<div class="bili-scan-bar-r">`
    + `<button class="btn sm" title="清除当日缓存，重新扫一遍全A股" onclick="loadBiliScan(true)">强制重扫</button>`
    + `</div>`;
  const result = document.createElement("div");
  result.className = "bili-scan-result";
  result.id = "bili-scan-result";
  box.insertBefore(bar, box.firstChild);
  box.insertBefore(result, bar.nextSibling);
  loadBiliScan(false);   // 打开即取：有缓存直出，无缓存触发后台扫描
}

function ensureBiliScanStyle() {
  if (document.getElementById("bili-scan-style")) return;
  const s = document.createElement("style");
  s.id = "bili-scan-style";
  s.textContent = BILI_SCAN_CSS;
  document.head.appendChild(s);
}

const BILI_SCAN_CSS = `
.bili-scan-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  padding:12px 16px;margin:0 0 14px;background:color-mix(in srgb,var(--accent) 10%,var(--panel));
  border:1px solid color-mix(in srgb,var(--accent) 40%,var(--border));border-radius:12px;position:sticky;top:0;z-index:5}
.bili-scan-status{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.bili-scan-result{margin:0 0 16px}
.bili-scan-summary{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}
.bili-scan-summary .chip-s{background:var(--panel)}
.bili-scan-meth{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.bili-scan-meth .bili-meth{font-size:12px;padding:3px 9px;border:1px solid var(--border);border-radius:999px;
  background:var(--panel);color:var(--text)}
.bili-scan-meth .bili-meth b{color:var(--accent);margin-left:4px}
.bili-scan-hits{display:flex;flex-direction:column;gap:8px}
.bili-hit{border:1px solid var(--border);border-radius:10px;padding:10px 12px;background:var(--panel)}
.bili-hit-top{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.bili-hit-name{font-weight:800;font-size:15px;color:var(--text)}
.bili-hit-code{font-size:12px;color:var(--muted)}
.bili-hit-close{font-size:13px;color:var(--text);font-variant-numeric:tabular-nums}
.bili-hit-pct{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.bili-hit-pct.up{color:var(--bull)}
.bili-hit-pct.down{color:var(--bear)}
.bili-hit-n{margin-left:auto;font-size:12px;color:var(--muted)}
.bili-hit-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.bili-hit-chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:4px 9px;cursor:pointer;
  border:1px solid color-mix(in srgb,var(--accent) 45%,var(--border));border-radius:999px;
  background:color-mix(in srgb,var(--accent) 12%,var(--panel));color:var(--text)}
.bili-hit-chip:hover{filter:brightness(1.1);border-color:var(--accent)}
.bili-hit-chip .c-name{font-weight:700}
.bili-hit-chip .c-win{font-variant-numeric:tabular-nums;color:var(--accent)}
.bili-scan-basis{margin:12px 0 0}
.bili-scan-empty{padding:14px 16px;border:1px dashed var(--border);border-radius:10px;color:var(--muted);text-align:center}
.bili-scan-err{padding:14px 16px;border:1px solid var(--bear);border-radius:10px;color:var(--bear);
  background:color-mix(in srgb,var(--bear) 8%,transparent)}
`;

function loadBiliScan(force) {
  const status = document.getElementById("bili-scan-status");
  if (status && !BILI_SCAN.polling) status.innerHTML = "请求中…";
  api("/api/bili/scan" + (force ? "?force=1" : ""))
    .then((d) => {
      if (d.status === "warming") {
        if (status) status.innerHTML = `扫描中：${esc(d.note || "")}${d.total ? `（${d.done || 0}/${d.total}）` : ""} · ${d.elapsed_s || 0}s`;
        if (!BILI_SCAN.polling) {
          BILI_SCAN.polling = true;
          setTimeout(() => { BILI_SCAN.polling = false; loadBiliScan(false); }, 4000);
        }
      } else if (d.status === "done") {
        if (status) status.innerHTML = `已更新 · 数据日期 ${esc(d.data_date || d.date || "")}`;
        renderBiliScanResult(d);
        BILI_SCAN.polling = false;
      } else if (d.status === "error") {
        if (status) status.innerHTML = "扫描失败";
        renderBiliScanResult(d);
        BILI_SCAN.polling = false;
      } else {
        if (status) status.innerHTML = "";
        BILI_SCAN.polling = false;
      }
    })
    .catch((e) => {
      if (status) status.innerHTML = "请求失败：" + esc(apiErrText(e));
      BILI_SCAN.polling = false;
    });
}

function renderBiliScanResult(d) {
  const el = document.getElementById("bili-scan-result");
  if (!el) return;
  if (d.status === "error") {
    el.innerHTML = `<div class="bili-scan-err">扫描失败：${esc(String(d.error || "未知错误"))}。可稍后点「强制重扫」重试。</div>`;
    return;
  }
  const sum = `<div class="bili-scan-summary">`
    + `<span class="chip-s">数据日期 ${esc(d.data_date || d.date || "—")}</span>`
    + `<span class="chip-s">扫描范围 全A股 ${d.universe || 0} 只</span>`
    + `<span class="chip-s">实际扫描 ${d.scanned || 0} 只</span>`
    + `<span class="chip-s">命中买点 ${d.total_hits || 0} 只</span>`
    + (d.elapsed_s != null ? `<span class="chip-s">耗时 ${d.elapsed_s}s</span>` : "")
    + `</div>`;
  const meth = (d.methods && d.methods.length)
    ? `<div class="bili-scan-meth">${d.methods.map((m) =>
        `<span class="bili-meth" title="${escAttr((m.name || "") + "：命中 " + (m.n_hits || 0) + " 只")}">${esc(m.name || m.key)}<b>${m.n_hits || 0}</b></span>`).join("")}</div>`
    : "";
  const basis = (d.basis && d.basis.length)
    ? `<details class="bili-scan-basis mt-basis"><summary>口径与免责（点开看，重要）</summary>`
      + d.basis.map((b) => `<div class="mt-basis-row">${esc(b)}</div>`).join("") + `</details>`
    : "";
  let hits = "";
  if (!d.hits || !d.hits.length) {
    hits = `<div class="bili-scan-empty">本次扫描全A股，没有股票恰好落在知识库战法的买点上（市场风格不在这些战法上）。可过段时间或收盘后再看。</div>`;
  } else {
    const max = d.hits.length;
    hits = `<div class="bili-scan-hits">${d.hits.map((h) => {
      const chips = h.items.map((it) => {
        const r = it.rating || "";
        return `<button class="bili-hit-chip" title="点开看「${escAttr(it.mname)}」战法解释与该规则历史回测" `
          + `onclick="biliScanStratExplain('${escAttr(it.mkey)}','${escAttr(it.skey)}')">`
          + `<span class="c-name">${esc(it.mname)}</span>`
          + (it.win_rate != null ? `<span class="c-win">入手${Number(it.win_rate).toFixed(0)}%</span>` : "")
          + (r ? `<span class="c-rat mt-rat ${mtRatingClass(r)}">${esc(r)}</span>` : "")
          + `</button>`;
      }).join("");
      const pctCls = (h.pct == null) ? "" : (h.pct >= 0 ? "up" : "down");
      const pctTxt = (h.pct == null) ? "" : `${h.pct >= 0 ? "+" : ""}${h.pct.toFixed(2)}%`;
      return `<div class="bili-hit">`
        + `<div class="bili-hit-top">`
        + codeLink(h.code, { text: h.name || h.code, cls: "bili-hit-name wt-sym" })
        + codeLink(h.code, { cls: "bili-hit-code wt-sym" })
        + (h.close != null ? `<span class="bili-hit-close">${Number(h.close).toFixed(2)}</span>` : "")
        + (h.pct != null ? `<span class="bili-hit-pct ${pctCls}">${pctTxt}</span>` : "")
        + `<span class="bili-hit-n">命中 ${h.n} 个战法 · <b style="font-weight:600;color:var(--muted)">点股票名看个股详情</b></span>`
        + `</div>`
        + `<div class="bili-hit-chips">${chips}</div>`
        + `</div>`;
    }).join("")}</div>`
      + (max >= 200 ? `<div class="bili-scan-empty" style="border-style:solid;margin-top:8px">已达单页上限 200 只（全市场命中可能更多），优先展示命中战法多、入手值得率高的。</div>` : "");
  }
  el.innerHTML = sum + meth + hits + basis;
}

function biliScanStratExplain(mkey, skey) {
  const draw = (ms) => {
    const m = ms.find((x) => x.key === mkey) || {};
    const s = (m.strategies || []).find((x) => x.key === skey) || {};
    const html = `<div class="mt-dlg-head"><span class="mt-name">${esc(m.name || mkey)}</span>`
      + `<span class="mt-kind ${m.kind === "价值面" ? "mt-kind-val" : "mt-kind-tech"}">${esc(m.kind || "技术面")}</span></div>`
      + `<p class="mt-dlg-sum">${esc(m.summary || "（该方法暂无一句话总结）")}</p>`
      + `<h4 class="mt-dlg-h">本战法本次命中的规则（历史回测口径）</h4>`
      + `<table class="mt-tbl"><thead><tr><th>规则</th><th>评级</th><th>胜率</th><th>区间收益</th><th>样本</th><th>排名</th></tr></thead><tbody>`
      + `<tr><td>${esc(s.name || skey)}</td>`
      + `<td class="mt-c-rat ${mtRatingClass(s.rating)}">${esc(s.rating || "—")}</td>`
      + `<td class="num">${s.win_rate != null ? Number(s.win_rate).toFixed(1) + "%" : "—"}</td>`
      + `<td class="num ${mtNumCls(s.total_return)}">${mtPct(s.total_return)}</td>`
      + `<td class="num">${s.trades != null ? s.trades : "—"}</td>`
      + `<td class="num">${s.rank != null ? s.rank : "—"}</td></tr></tbody></table>`
      + `<div class="mt-dlg-un" style="margin-top:10px">「入手值得率」= 该规则近两年回测的每笔交易胜率（库内候选池），是<b>规则的历史胜率</b>，不是这只票的预测。扫描范围为全A股，与回测候选池口径不同；盘中最后一根K线未收盘，信号以收盘复核为准。</div>`;
    openDrawer("战法解释 · " + (m.name || mkey), html, null);
  };
  if (BILI_METHODS.data && BILI_METHODS.data.methods) { draw(BILI_METHODS.data.methods); return; }
  api("/api/bili/methods?cat=technical,value")
    .then((dd) => { BILI_METHODS.data = dd; draw(dd.methods || []); })
    .catch(() => draw([]));
}
