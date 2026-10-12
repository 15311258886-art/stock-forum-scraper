/* =============================================================================
 * tests/_guard.js —— UI 测试脚本的「默认安全」闸门（CommonJS）
 *
 * 背景（真实事故，不是假设）
 *   本仓库 12 个 UI 测试/截图脚本原先一上来就 page.click("#btnDemo") 或
 *   page.click("#btnOverseas")。这两个按钮会打到破坏性接口：
 *     POST /api/demo     → viewer/demo_data.load_demo() 先 DELETE 掉 14 张业务表
 *                          （posts/trades/news/raw_pages/stock_mentions/persons/
 *                           topics/players/positions/overseas_news/
 *                           overseas_mentions/overseas_sentiment_daily/
 *                           overseas_hotspots/overseas_sources_status）再重灌示例，
 *                          并调用 collect(include_seed=True, reset=True)
 *     POST /api/overseas → 清空海外侧全部数据并联网重抓
 *     POST /api/fetch    → 写库（不清空，但同样属于「动数据」）
 *   这些脚本此前一直被对着**生产实例 8765** 运行，于是每跑一次就把真实抓取的
 *   数据清空重灌示例。最近一次（2026-09-22 23:47:03）把海外新闻从 707 条清成
 *   41 条、HK 区从 102 行剩 2 行，只能靠快照回滚到 713 条。
 *
 * 策略：默认只读；破坏性操作要「双钥匙」
 *   1) 端口必须不是生产口径（PORT !== "8765"，即视为隔离实例）；
 *   2) 并且必须显式设置环境变量 ALLOW_DATA_RESET=1。
 *   两者同时满足才允许点击破坏性按钮（DESTRUCTIVE_OK）。
 *   只满足其一时 seed() **绝不调用 page.click()**，改为复用库里现有数据继续测。
 *
 * 怎么在隔离实例里放开
 *   # 1. 只读复制生产库到临时隔离目录（生产库不动）
 *   copy viewer\app.db %TEMP%\qa_iso\app.db
 *   # 2. 服务端指向隔离库 + 监听非 8765 端口
 *   $env:VIEWER_DB   = "$env:TEMP\qa_iso\app.db"
 *   $env:VIEWER_PORT = "8799"
 *   python -m viewer.server
 *   # 3. 另开终端：显式声明允许重置，并把端口作为参数传给脚本
 *   $env:ALLOW_DATA_RESET = "1"
 *   node tests/ui_regression.js 8799
 *   （注意：脚本端口取自命令行第一个非 -- 开头的参数，与 VIEWER_PORT 无关，
 *     所以这里必须写出 8799，不能省。）
 *
 * 自证
 *   每次运行开头打印 announce() 策略行；ui_* 脚本结束时调用
 *   assertNoDestructiveRequest()，断言本次运行**没有**发出任何
 *   POST /api/demo|/api/overseas|/api/fetch，让运行日志本身成为「我没动数据」
 *   的证据。
 * ========================================================================== */
"use strict";

const path = require("path");

/* ---------- 端口 / 隔离判定 ---------- */

// 命令行第一个非 "--" 开头的参数；缺省 8765（生产口径）
const PORT = process.argv.slice(2).find((a) => a && !a.startsWith("--")) || "8765";

// 8765 是生产口径，视为不可破坏；其余端口视为隔离实例
const ISOLATED = PORT !== "8765";

// 双钥匙：隔离实例 + 显式授权
const DESTRUCTIVE_OK = ISOLATED && process.env.ALLOW_DATA_RESET === "1";

/* ---------- 工具 ---------- */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const SCRIPT = (() => {
  try { return path.basename(process.argv[1] || "script.js"); } catch (e) { return "script.js"; }
})();

function fmtReady(ready) {
  if (!ready) return "(未指定)";
  if (Array.isArray(ready)) return ready.join(" + ");
  if (typeof ready === "function") return "自定义谓词";
  return ready;
}

function announce() {
  const policy = DESTRUCTIVE_OK
    ? "允许（隔离实例 + ALLOW_DATA_RESET=1）"
    : "禁止（默认只读，绝不点击 #btnDemo / #btnOverseas）";
  console.log(
    `[guard] 脚本=${SCRIPT} 端口=${PORT} 隔离实例=${ISOLATED ? "是" : "否"} 破坏性操作=${policy}`
  );
}

/* ---------- 就绪轮询（替代「点击后固定 sleep」） ---------- */

async function isReady(page, ready) {
  if (!ready) return true;
  if (typeof ready === "function") return !!(await ready(page));
  const list = Array.isArray(ready) ? ready : [ready];
  if (!list.length) return true;
  for (const sel of list) {
    const n = await page.$$eval(sel, (els) => els.length).catch(() => 0);
    if (!n) return false;
  }
  return true;
}

async function waitReady(page, ready, timeoutMs) {
  const t0 = Date.now();
  for (;;) {
    let hit = false;
    try { hit = await isReady(page, ready); } catch (e) { hit = false; }
    if (hit) return true;
    if (Date.now() - t0 >= timeoutMs) return false;
    await sleep(300);
  }
}

function isolationHelp(sel, ready) {
  return [
    `[guard] 中止：期望的就绪数据未出现（选择器 ${fmtReady(ready)}）。`,
    `        本脚本需要示例数据才能成立，而端口 ${PORT} 未被授权执行破坏性操作，`,
    `        因此不会点击 ${sel}。请在隔离实例中运行（不要对着生产 8765 跑）：`,
    "          copy viewer\\app.db %TEMP%\\qa_iso\\app.db",
    "          $env:VIEWER_DB = \"$env:TEMP\\qa_iso\\app.db\"; $env:VIEWER_PORT = \"8799\"",
    "          python -m viewer.server            # 另开终端",
    "          $env:ALLOW_DATA_RESET = \"1\"",
    `          node tests/${SCRIPT} 8799`,
    "        退出码 3 = 缺少示例数据且未授权重置数据。",
  ].join("\n");
}

/* ---------- 核心：非破坏性「准备数据」 ---------- */

/**
 * 准备页面数据。
 *   DESTRUCTIVE_OK 时：点击 sel 触发重置，再轮询等 ready 出现 → 返回 "reset"
 *   否则：**绝不点击**，直接轮询 ready：
 *     数据已在  → 打印跳过提示                       → 返回 "kept"
 *     超时无数据 → 打印隔离指引；soft=false 时 exit 3 → 返回 "missing"
 *
 * @param {object} page  puppeteer Page
 * @param {object} opts
 * @param {string} [opts.sel="#btnDemo"]      破坏性按钮选择器
 * @param {string|string[]|Function} [opts.ready] 就绪信号（选择器 / 选择器数组 / 谓词）
 * @param {number} [opts.timeoutMs=20000]     就绪超时
 * @param {boolean} [opts.soft=false]         true=缺数据也继续（截图脚本用），不 exit 3
 * @returns {Promise<"reset"|"kept"|"missing">}
 */
async function seed(page, opts = {}) {
  const { sel = "#btnDemo", ready = null, timeoutMs = 20000, soft = false } = opts;

  if (DESTRUCTIVE_OK) {
    await page.click(sel);
    const hit = await waitReady(page, ready, timeoutMs);
    console.log(
      `[guard] RESET 已执行 ${sel}（端口 ${PORT} 隔离 + ALLOW_DATA_RESET=1）` +
      (hit ? "，数据已就绪" : `，警告：就绪信号 ${fmtReady(ready)} 超时`)
    );
    return "reset";
  }

  // 未授权：绝不点击，只等现有数据
  const hit = await waitReady(page, ready, timeoutMs);
  if (hit) {
    console.log(`[guard] SKIP 破坏性操作 ${sel}（端口 ${PORT} 非隔离，改用现有数据）`);
    return "kept";
  }
  console.log(isolationHelp(sel, ready));
  if (soft) {
    console.log(`[guard] SKIP(soft) 缺数据但继续出图：${SCRIPT}（不会 exit 3）`);
    return "missing";
  }
  process.exit(3);
}

/* ---------- 自证：本次运行没发出破坏性请求 ---------- */

const DESTRUCTIVE_API = [
  ["POST", "/api/demo"],
  ["POST", "/api/overseas"],
  ["POST", "/api/fetch"],
];

/** 挂上请求收集器，返回数组；配合 assertNoDestructiveRequest 使用 */
function trackRequests(page) {
  const seen = [];
  page.on("request", (req) => seen.push({ method: req.method(), url: req.url() }));
  return seen;
}

function normalize(entry) {
  if (!entry) return { method: "", url: "" };
  const method = typeof entry.method === "function" ? entry.method() : entry.method;
  const url = typeof entry.url === "function" ? entry.url() : entry.url;
  return { method: String(method || "").toUpperCase(), url: String(url || "") };
}

/**
 * 断言本次运行没有发出任何 POST /api/demo|/api/overseas|/api/fetch。
 * 既是守卫也是自证：日志里会留下「本次共 N 条请求、破坏性 0 条」的记录。
 * 若处于授权重置模式（隔离实例 + ALLOW_DATA_RESET=1），破坏性请求本就是预期
 * 行为，此时不做只读断言，直接返回 true。
 * @returns {boolean} true=干净（或本次为授权重置模式）
 */
function assertNoDestructiveRequest(page, seen) {
  const list = seen || [];
  if (DESTRUCTIVE_OK) {
    console.log(
      `[guard] N/A 本次为授权重置模式（端口 ${PORT} 隔离 + ALLOW_DATA_RESET=1），` +
      `破坏性请求属预期行为，跳过只读自证`
    );
    return true;
  }
  const hits = [];
  for (const entry of list) {
    const { method, url } = normalize(entry);
    let p = url.split("?")[0].split("#")[0];
    try { p = new URL(url).pathname; } catch (e) { /* 相对地址就按原样比 */ }
    p = p.replace(/\/+$/, "");
    for (const [m, want] of DESTRUCTIVE_API) {
      if (method === m && p === want) hits.push(`${m} ${want}`);
    }
  }
  if (hits.length) {
    console.log(
      `[guard] FAIL 检测到破坏性请求：${[...new Set(hits)].join("、")}` +
      `（本次共 ${list.length} 条请求）—— 生产数据可能已被改动，请立即核查`
    );
    return false;
  }
  console.log(
    `[guard] OK 本次运行未发出任何 POST /api/demo|/api/overseas|/api/fetch` +
    `（共 ${list.length} 条请求）`
  );
  return true;
}

module.exports = {
  PORT,
  ISOLATED,
  DESTRUCTIVE_OK,
  announce,
  seed,
  waitReady,
  trackRequests,
  assertNoDestructiveRequest,
};
