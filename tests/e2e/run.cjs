#!/usr/bin/env node
/**
 * 前端 UI 端到端门禁（唯一入口）
 * ============================================================================
 * 为什么要有这个文件（2026-11-01 重建）：
 *   `tests/ui_full_regression.js` 曾经是完整 UI 回归，但它把 tab 名字写死成
 *   `.tab[data-tab="summary"]`。导航 DOM 早已改成 `.sub-tab[data-tab="..."]`
 *   （app.js paintSubnav），于是它在第一个 tab 就抛 "No node found for selector"。
 *   后端有几百条 pytest 护着，前端导航与面板渲染却一条自动回归都没有 ——
 *   这就是测评里「自动化测试与真实 UI 已漂移」那条。
 *
 * 本文件的硬设计约束（改之前请先读完，否则会退化成又一个一次性探针）：
 *   1. **不出现任何具体 tab 名**。导航项一律从 DOM 动态枚举：
 *      先读 `#sidenav .nav-item[data-cat]` 得到分类全集，逐个点开分类，
 *      再读该分类下 `#subnav .sub-tab[data-tab]` 得到子板块全集，然后逐个点。
 *      理由：tab 会随 NAV / kb_sources.yaml 增减（本次实测 53 个，而旧文档写的是 43 个），
 *      写死数量或名字的测试，第一次导航调整就会红，红了没人看就等于没有。
 *   2. **状态判定只用语义属性**：`[data-tab=X].active`、`location.hash === "#cat/tab"`、
 *      `#p-<tab>` 是否有内容。不用坐标、不用 nth-child、不用文案匹配当断言依据。
 *   3. **"环境坏了" 与 "产品坏了" 必须分开**：连不上服务 / 浏览器起不来 → EXIT=2；
 *      断言失败 → EXIT=1。混在一起会让人以为是代码坏了，久而久之全部忽略。
 *   4. **写操作必须可回收**：自选增删用临时标的，跑完一定清理（含异常路径）。
 *   5. 每条断言都带 `why`（为什么这么测），失败时人不用猜。
 *
 * 用法：
 *   node tests/e2e/run.cjs             # 全量（默认）
 *   node tests/e2e/run.cjs --quick     # 快速门禁：3 个 tab + 单位 + 健康，< 40s
 *   node tests/e2e/run.cjs --json      # 机读：stdout 只打一个 JSON
 *   node tests/e2e/run.cjs --no-write  # 跳过自选增删（只读环境用）
 *   node tests/e2e/run.cjs --port 8765 # 指定端口（默认 8765）
 *   附加：--keep-shots 保留失败截图到 tests/e2e/_shots/
 *
 * 退出码：0 全过 / 1 有用例失败 / 2 环境不可用（服务或浏览器）
 * 报告：tests/e2e/_report.json（已 gitignore）
 */
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const crypto = require("crypto");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const REPORT_PATH = path.join(__dirname, "_report.json");
const SHOT_DIR = path.join(__dirname, "_shots");

/* ==========================================================================
 * 0. 参数
 * ======================================================================== */
const argv = process.argv.slice(2);
const CFG = {
  quick: argv.includes("--quick"),
  json: argv.includes("--json"),
  noWrite: argv.includes("--no-write"),
  keepShots: argv.includes("--keep-shots"),
  /* 自测用：故意注入一条 ok=true/level=warn 的坏断言，门禁**必须**因此变红。
     存在的理由：自检很容易写成"永远为真"的判断，那样它绿得毫无意义。
     这条开关让"自检真的有效"本身也能被验证。 */
  selftestHarness: argv.includes("--selftest-harness"),
  port: 8765,
  help: argv.includes("--help") || argv.includes("-h"),
};
{
  const i = argv.findIndex((a) => a === "--port");
  if (i >= 0 && argv[i + 1]) CFG.port = parseInt(argv[i + 1], 10) || 8765;
  const eq = argv.find((a) => a.startsWith("--port="));
  if (eq) CFG.port = parseInt(eq.split("=")[1], 10) || 8765;
}
const BASE = `http://127.0.0.1:${CFG.port}`;
const PAGE_URL = `${BASE}/index.html`;
/* 全量上限 6 分钟：超时按失败处理，并指出卡在哪个 tab —— 卡住本身就是缺陷。
   quick 目标 < 40 秒：超过 40s 记一条 FAIL（budget），提醒有人把门禁拖慢了。 */
const BUDGET_MS = CFG.quick ? 40 * 1000 : 360 * 1000;
/* 单个 tab 的上限：超过就是「点开它之后页面卡死」，按失败记。 */
const TAB_TIMEOUT_MS = 15000;

/* 三档视口。放在模块作用域（不是函数里）是有原因的：曾把它写在阶段代码里，
   结果一次编辑把声明挤到使用点之后，运行时抛 `VPS is not defined`，整个视口阶段
   连带 6 条断言一起消失，而摘要里只多了一条 runner 失败 —— 想分辨"没跑"和"跑过了"
   要多翻一次报告。声明提到顶层，这类作用域事故就不可能再吞掉一个阶段。 */
const VPS = [
  { w: 1024, h: 700 },
  { w: 1440, h: 1000 },
  { w: 1920, h: 1080 },
];

if (CFG.help) {
  process.stdout.write(
    "用法: node tests/e2e/run.cjs [--quick] [--json] [--no-write] [--port N] [--keep-shots]\n" +
    "  --quick      只跑 3 个 tab + 单位一致性 + 健康检查（<40s）\n" +
    "  --json       stdout 只输出一个 JSON 对象\n" +
    "  --no-write   跳过自选增删（只读环境）\n" +
    "  --selftest-harness  自测：注入一条坏 level 的断言，应当变红（验证自检有效）\n" +
    "退出码: 0 全过 / 1 用例失败 / 2 环境不可用\n"
  );
  process.exit(0);
}

/* ==========================================================================
 * 1. 结果收集
 * ======================================================================== */
/* 三个桶是**互斥且穷尽**的，而且 `level` 现在由 `ok` + 「是否阻断」推导出来，
   不再由调用方随手写 —— 之前 `level` 出现过 102 条 `fail` 里 102 条都是 ok=true 的情况：
   字段有值、但值是垃圾，比 null 更危险（health.leases 踩过同一个病）。
      ok === true              → level = "pass"
      ok === false && 阻断     → level = "fail"     （计入退出码）
      ok === false && 不阻断   → level = "warn"     （只记录，不计入退出码）
   调用方只能表达「阻断 / 不阻断」这个意图，不能再自己决定 level 的取值。 */
const RESULTS = [];          // 断言（进分母）
const INFO = [];             // 好消息/正常状态（**不进 checks、不进分母**、不影响判定）
const LEVEL_HINTS = [];      // 记下调用方的阻断意图，便于事后核对（"这条为什么降级了"）
const LEVEL_BAD = [];        // 违反三值口径的行（自检用；非空就必须红）

function rec(group, id, ok, detail, why, intent, extra) {
  const isOk = !!ok;
  /* intent 只允许 "fail"（阻断）或 "warn"（不阻断）；缺省按 ok 推导 */
  const blocking = intent === undefined ? !isOk : intent === "fail";
  const level = isOk ? "pass" : (blocking ? "fail" : "warn");
  /* 记录调用方原始意图，供核对；同时把"ok=true 却传了 fail/warn"这种口误抓出来 */
  LEVEL_HINTS.push({ id, ok: isOk, intent: intent === undefined ? null : intent, level });
  if (!isOk && intent !== undefined && intent !== "fail" && intent !== "warn") {
    LEVEL_BAD.push({ id, why: `ok=false 的断言传了非法 intent="${intent}"` });
  }
  RESULTS.push(Object.assign({
    group, id, ok: isOk,
    detail: detail === undefined ? "" : String(detail),
    why: why || "", level,
    intent: intent === undefined ? null : intent,
  }, extra || {}));
  return isOk;
}
const fail = (group, id, detail, why, extra) => rec(group, id, false, detail, why, "fail", extra);
/* pass 一律 level="pass"（以前标 "info" 会让"通过"混进记录桶，是算术对不上的源头之一） */
const pass = (group, id, detail) => rec(group, id, true, detail, "");
/* warn = 不阻断的未达标（长文本溢出、重启窗口内的网络失败…） */
const warn = (group, id, detail, why, extra) => rec(group, id, false, detail, why || "", "warn", extra);
/* info = 好消息/正常状态：单独一个通道，不进分母 —— 混进 warn 会让 warn 数失去意义 */
function info(group, id, detail) {
  INFO.push({ group, id, detail: detail === undefined ? "" : String(detail) });
}

const NOTES = [];            // 环境/模式说明（不进断言）
const NEEDS_TESTID = [];     // 需要加稳定标识才能更稳的地方（本轮不许改 app.js，故只登记）
const BLOCKED = [];          // 因约束无法测的点
const NOT_COVERED = [];      // 旧版有、新版没覆盖的检查项（漏掉不写 = 假装覆盖了）
/* 任务约束导致"测得不够硬"的地方必须亮出来（不写 = 假装测过了）： */
BLOCKED.push(
  "（2026-11-01 更新）app.js 已补 data-testid：个股抽屉 stk-drawer / 成交量 vol-quote·vol-kline / 自选行 watch-row，" +
    "面板 data-ready、横条 data-cat。本脚本已改为**优先用 testid**，结构探测只作兜底；" +
    "兜底命中时会记 needs_testid（降级为提示，不再当告警）。",
  "禁止重启服务（多人共用 8765）→ 只能对「进程里正跑的那份代码」做端到端验证；" +
    "后端 Python 改动必须由人重启后复跑，否则 /api/version 的 build_id 会一直是旧值（报告里标 stale_build）。"
);

/* ==========================================================================
 * 2. 小工具
 * ======================================================================== */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowMs = () => Date.now();

/** 本地写接口的令牌 Cookie（server.py 13178 行：只要设了 VIEWER_API_TOKEN，每次响应都会
 *  下发 `local_token=...`，写接口（POST）缺它就直接 401 "token required"）。
 *  浏览器侧无需关心（同源 fetch 自动带），但本门禁的 Node 侧清理走裸 http —— 必须自己接住
 *  这个 Cookie，否则「清理测试数据」会 401 失败，把用户的真实自选池留下脏数据。
 *  这里不读环境变量、不猜令牌：只做"服务器给什么就用什么"，避免权限假设。 */
let COOKIE = "";
function absorbCookie(setCookie) {
  if (!setCookie) return;
  const list = Array.isArray(setCookie) ? setCookie : [setCookie];
  for (const c of list) {
    const m = /(?:^|;\s*)local_token=([^;]+)/.exec(c);
    if (m) COOKIE = "local_token=" + m[1];
  }
}

/** 极简 HTTP GET/POST（不引依赖：项目无出网权限，也不许新建 package.json） */
function httpReq(method, p, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8");
    const req = http.request(
      {
        host: "127.0.0.1", port: CFG.port, path: p, method,
        headers: Object.assign(
          { Accept: "application/json" },
          COOKIE ? { Cookie: COOKIE } : {},
          data ? { "Content-Type": "application/json", "Content-Length": data.length } : {}
        ),
        timeout: timeoutMs || 15000,
      },
      (res) => {
        const chunks = [];
        absorbCookie(res.headers["set-cookie"]);
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let json = null;
          try { json = JSON.parse(text); } catch (_) { /* 非 JSON 时保持 null */ }
          resolve({ status: res.statusCode, text, json });
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error("timeout after " + (timeoutMs || 15000) + "ms")));
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}
const httpJson = async (p, body, t) => {
  /* 写请求第一次跑时可能还没拿到 Cookie（前面全是纯 GET）——先请求一次首页把 Cookie 接住。
     为什么是 "/" 而不是 /api/version：server.py 只在**发页面**时下发 Set-Cookie（13178 行），
     JSON 接口不下发。实测踩过：拿 /api/version 预热 → Cookie 为空 → 清理请求 401 "token required"
     → 门禁把用户自选池写脏了却没删掉。 */
  if (body !== undefined && !COOKIE) {
    try { await httpReq("GET", "/", undefined, t); } catch (_) { /* 拿不到就让服务端自己报 401 */ }
  }
  const r = await httpReq(body === undefined ? "GET" : "POST", p, body, t);
  return { status: r.status, json: r.json, text: r.text };
};

/** 磁盘侧代码指纹 —— 与 viewer/build_id.py::of_disk **逐字节等价**（2026-11-01 改内容哈希后
 *  仍需交叉验证同值：两边都算 `sha1( 各文件 "rel|size|sha1hex;" 拼接 )[0:12]`）。
 *  为什么要自己算：服务可能跑的是旧快照，/api/version 的 build_id 与磁盘不一致时，
 *  「单位一致性」这类依赖新代码的用例会 FAIL，但那是"没重启"而不是"改坏了"，
 *  必须在报告里标 stale_build 才不会误判成回归。
 *  ⚠️ 2026-11-01：原来是 `(mtime, size)`，mtime 只到秒 → 同一秒内等长替换指纹不变（漏报，
 *  致命）；touch 一下指纹就变（误报，让人习惯性忽略告警）。已改内容哈希。 */
const KEY_FILES = [
  "viewer/server.py", "viewer/quant.py", "viewer/pillars.py", "viewer/evolve.py",
  "viewer/metalearn.py", "viewer/resonance.py", "viewer/backtest.py", "viewer/selfpick.py",
  "viewer/db.py", "viewer/predictor.py", "viewer/static/index.html", "viewer/static/app.js",
  "viewer/static/app.css", "viewer/rhythm.py", "board_flow.py",
];
function diskBuildId() {
  const h = crypto.createHash("sha1");
  for (const rel of KEY_FILES) {
    try {
      const buf = fs.readFileSync(path.join(ROOT, rel));
      const digest = crypto.createHash("sha1").update(buf).digest("hex");
      h.update(`${rel}|${buf.length}|${digest};`, "utf8");
    } catch (_) {
      h.update(`${rel}|missing;`, "utf8");
    }
  }
  return h.digest("hex").slice(0, 12);
}

/** 静态资源的 (size, mtime, 内容 sha1 前 8 位) —— 给报告做归因用。
 *  场景：多人同时改 app.js 时，同一份门禁两次跑出不同 tab 数，必须能证明"是文件变了"而不是"测试不稳"。 */
function assetStamp(rel) {
  try {
    const p = path.join(ROOT, rel);
    const st = fs.statSync(p);
    const buf = fs.readFileSync(p);
    return {
      rel,
      size: st.size,
      mtime: new Date(st.mtimeMs).toISOString(),
      sha1_8: crypto.createHash("sha1").update(buf).digest("hex").slice(0, 8),
    };
  } catch (e) {
    return { rel, error: String(e.message).slice(0, 80) };
  }
}

/** 找 Chromium：不装依赖，用机器上已有的（playwright 缓存的 chromium 最稳）。 */
function findChromium() {
  const cands = [
    "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe",
    "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium_headless_shell-1234\\chrome-win64\\headless_shell.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
  ];
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch (_) { /* ignore */ } }
  return null;
}

/* ==========================================================================
 * 3. 启动前探测：服务不通就直接 EXIT=2，不去猜
 * ======================================================================== */
/** 用 Python 侧**权威实现**算一次磁盘指纹（viewer/build_id.py::of_disk）。
 *  为什么要这一层：Node 里那份 `diskBuildId()` 是"复刻"，而复刻会滞后 ——
 *  本轮就发生过一次：`of_disk` 从 `(mtime,size)` 改成了内容哈希，复刻如果不跟着改，
 *  `stale_build` 会长期误报"服务跑旧代码"，喊人重启却永远对不上。
 *  所以：能起 python 就以权威值为准，复刻只当交叉校验；起不来才退回复刻（并如实记录来源）。 */
function pythonDiskBuildId() {
  const { execFileSync } = require("child_process");
  for (const bin of ["python", "py", "python3"]) {
    try {
      const out = execFileSync(bin, ["-c", "from viewer.build_id import of_disk; print(of_disk('.'))"],
        { cwd: ROOT, timeout: 8000, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      const v = String(out || "").trim().split(/\s+/).pop();
      if (/^[0-9a-f]{6,}$/.test(v || "")) return { bin, value: v };
    } catch (_) { /* 试下一个 */ }
  }
  return null;
}

async function preflight() {
  const out = { ok: false, version: null, health: null, diskBuild: null, staleBuild: null, fpSource: null, replica: null, replicaDrift: null, err: null };
  try {
    const v = await httpJson("/api/version", undefined, 6000);
    if (v.status !== 200 || !v.json) {
      out.err = `GET /api/version → HTTP ${v.status}，body 前 120 字: ${String(v.text || "").slice(0, 120)}`;
      return out;
    }
    out.version = v.json;
    const h = await httpJson("/api/health", undefined, 20000);
    out.health = { status: h.status, json: h.json };
    out.ok = true;
    /* 权威值优先；复刻值留作交叉校验 */
    out.replica = diskBuildId();
    const py = pythonDiskBuildId();
    out.diskBuild = py ? py.value : out.replica;
    out.fpSource = py ? `python:${py.bin} viewer.build_id.of_disk` : "node:replica";
    out.replicaDrift = py ? (py.value === out.replica ? null : { python: py.value, node: out.replica }) : null;
    out.staleBuild = !!out.version.build_id && out.version.build_id !== out.diskBuild;
    return out;
  } catch (e) {
    out.err = `连不上 ${BASE}：${e.message}（服务没起 / 端口不对？本门禁不会自己拉进程）`;
    return out;
  }
}

/* ==========================================================================
 * 4. 页面侧脚本（注入到浏览器里跑的小函数，集中放这里便于审阅）
 * ======================================================================== */
/** 脏字符扫描：排除 pre/code（代码示例里出现 null/undefined 是合法的），
 *  用词边界正则避免把 "nullable"/"nulls" 这类英文单词误判。
 *  null 单列 warn：中文 UI 里合法出现 "null" 的概率虽低但确实有（示例载荷），
 *  把它当硬失败会让门禁变红却没人能改，最终整条门禁被忽略 —— 宁可降级但要亮出来。 */
function scanBadTokensInPage(scopeSel) {
  const EXCLUDE = Array.from(document.querySelectorAll("pre, code, script, style, textarea"))
    .map((e) => (e.innerText || e.textContent || ""))
    .filter((s) => s && s.trim());
  let text = document.body.innerText || "";
  for (const chunk of EXCLUDE) text = text.split(chunk).join(" ");
  const PATTERNS = [
    { k: "undefined", re: /\bundefined\b/g, hard: true },
    { k: "NaN", re: /\bNaN\b/g, hard: true },
    { k: "Invalid Date", re: /Invalid Date/g, hard: true },
    { k: "[object Object]", re: /\[object Object\]/g, hard: true },
    { k: "null", re: /\bnull\b/g, hard: false },
  ];
  const hits = [];
  for (const p of PATTERNS) {
    let m;
    p.re.lastIndex = 0;
    while ((m = p.re.exec(text)) !== null) {
      const s = Math.max(0, m.index - 40), e = Math.min(text.length, m.index + m[0].length + 40);
      hits.push({ token: p.k, hard: p.hard, context: text.slice(s, e).replace(/\s+/g, " ") });
      if (hits.length > 12) break;
    }
  }
  // 面板级文本长度：用来判断「这个 tab 打开后到底渲了东西没有」
  const panel = scopeSel ? document.querySelector(scopeSel) : null;
  return {
    hits,
    bodyLen: text.trim().length,
    panelExists: !!panel,
    panelLen: panel ? (panel.innerText || "").trim().length : 0,
    panelHead: panel ? (panel.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60) : "",
  };
}

/* 面板内容探测（iframe 感知）。
   ⚠️ 为什么必须有这一层：`#p-fundmasters` 整块是一个 `<iframe src="/static/fund_masters/index.html">`。
   `innerText` 不跨子文档，父页面里它**永远是 0 字**，于是"空面板"这个软指标会稳定误报
   `strategy/fundmasters`（本轮实测：父文档 0 字 / 子文档 127 字 / 数据在盘上 138KB）。
   这不是产品缺陷，是**度量口径无效** —— 拿 0 当"面板没内容"会把一条好路径记成坏的。
   现在的口径：面板自己有字就用自己，有 iframe 就把子文档正文也算进来，取两者较大值；
   同时把 `data-loaded`（iframe 加载成功标记）与 `data-ready` 一起带出来，
   这样上层既能判"空不空"，也能判"是不是还停在加载中"。
   不写死 tab 名、也不写死 fundmasters：任何面板套 iframe 都走同一条路。 */
function panelTextInPage(sel) {
  const p = document.querySelector(sel);
  if (!p) return { exists: false, len: 0, ownLen: 0, subLen: 0, frame: false, panelFrame: false, frameLoaded: null, ready: false, empty: true, head: "" };
  const own = (p.innerText || "").trim();
  /* 只认"声明了自己是外挂页"的 iframe（app.js 打的语义属性），不拿任意 iframe 当内容源 */
  const pf = p.querySelector("iframe[data-panel-frame]") || p.querySelector("iframe");
  const panelFrame = !!(p.querySelector("iframe[data-panel-frame]"));
  let sub = "", loaded = null;
  if (pf) {
    loaded = pf.getAttribute("data-loaded");
    try {
      const d = pf.contentDocument;
      if (d && d.body) sub = (d.body.innerText || "").trim();
    } catch (_) { sub = ""; }          /* 跨域时读不到：保持 0，但 frame=true 会在明细里体现 */
  }
  const best = own.length >= sub.length ? own : sub;
  const ready = p.getAttribute("data-ready") === "1";
  /* 「空面板」三口径（缺一不可，任一非空就不算空）：
       ① 父文档 innerText
       ② 有 iframe 时读子文档 body.innerText
       ③ 就绪标记：data-ready + iframe 的 data-loaded
     为什么要三口径：`#p-fundmasters` 整块是 iframe，①②不跨文档边界恒为 0；
     只看文本会把一个正常面板稳定判成空（这就是上一轮那条假信号）。
     反过来只看 data-ready 也不行：它可能因为别的原因没打上，
     此时文本非空说明"用户确实看得到东西"，不该判空。 */
  const empty = own.length === 0 && sub.length === 0 && !ready && loaded !== "1";
  return {
    exists: true,
    len: best.length,
    ownLen: own.length,
    subLen: sub.length,
    frame: !!pf,
    panelFrame,
    frameLoaded: loaded,
    ready,
    empty,
    head: best.replace(/\s+/g, " ").slice(0, 60),
  };
}

/* ==========================================================================
 * 5. 主流程
 * ======================================================================== */
(async () => {
  const T0 = nowMs();
  /* 两个时间闸，故意分开：
     · deadline（1.0×预算）→ 只用来"如实报告是否达标"（perf 断言）；
     · hardDeadline（1.5×预算）→ 才真的中止后续阶段。
     否则会出现"预算到点了直接 break，剩下两个视口一个都没测，却报 EXIT=1"——
     那是被预算掐断，不是产品坏了。实测：服务刚重启（缓存全冷）时 quick 跑到 41s，
     就是为了不超预算把 1440/1920 两个视口跳过了。 */
  const hardDeadline = T0 + Math.round(BUDGET_MS * 1.5);
  const timedOut = () => nowMs() > hardDeadline;
  /* 分阶段计时：门禁变慢时必须一眼看出是哪一段慢（否则"快慢"只能靠感觉）。
     结果写进 report.phases，人读摘要里也会打出来。 */
  const PHASES = [];
  let _ph = { name: "(start)", at: nowMs() };
  const phase = (name) => {
    const t = nowMs();
    /* 第一段（preflight + 起浏览器）不落一条 (start) 噪音，直接从第一次调用开始记 */
    if (_ph.name !== "(start)") PHASES.push({ name: _ph.name, ms: t - _ph.at });
    _ph = { name, at: t };
  };

  /* ---------- 5.0 环境探测（失败 = EXIT 2，不是产品缺陷） ---------- */
  const pf = await preflight();
  const report = {
    meta: {
      at: new Date().toISOString(),
      mode: CFG.quick ? "quick" : "full",
      port: CFG.port,
      base: BASE,
      no_write: CFG.noWrite,
      budget_ms: BUDGET_MS,
      node: process.version,
    },
    env: {
      server_build_id: pf.version ? pf.version.build_id : null,
      server_version: pf.version ? pf.version.version : null,
      server_pid: pf.version ? pf.version.pid : null,
      server_started_at: pf.version ? pf.version.started_at : null,
      disk_build_id: pf.diskBuild,
      disk_build_id_source: pf.fpSource,
      disk_build_id_replica: pf.replica,
      disk_build_id_replica_drift: pf.replicaDrift,
      stale_build: pf.staleBuild,
      /* 静态资源是「每次请求现读磁盘」的：server.py 可以是旧进程，app.js 却已经是新文件。
         多人同时改前端时，必须能说清「这份结果对应哪一版 app.js」，否则没法归因。 */
      app_js: assetStamp("viewer/static/app.js"),
      app_css: assetStamp("viewer/static/app.css"),
      index_html: assetStamp("viewer/static/index.html"),
    },
    nav: { total: 0, passed: 0, failed: 0, empty_panels: [], tabs: [] },
    api: { failures: [], net_failures: [], filtered_console: [] },
    unit: null,
    watch: null,
    states: [],
    viewports: [],
    checks: RESULTS,
    needs_testid: NEEDS_TESTID,
    blocked: BLOCKED,
    not_covered: NOT_COVERED,
    notes: NOTES,
    phases: PHASES,
    exit_code: null,
  };

  /* 环境中途坏掉时"这条用例没执行"的登记处。
     为什么单独建一个：跑全量时真遇到过两种中途断服务 —— 一是别人重启 viewer，
     二是浏览器开新页面时恰好撞上重启窗口（`ERR_CONNECTION_REFUSED` 出现在**文档层**，
     页面本身就是错误页，此时面板/文案断言全都不成立）。
     这种既不能记 PASS（是谎话），也不该记 FAIL（不是产品坏了），所以：
       · 登记到 ENV_SKIPS；
       · 全跑完后若有 ENV_SKIPS 且无真实失败 → 退出码 2（"环境不可用，请复跑"），
         而不是 0 —— 不给自己发一张"全过"的假证书。 */
  const ENV_SKIPS = [];
  const envSkip = (where, why) => {
    ENV_SKIPS.push({ where, why });
    warn("env", `中途不可用：${where}`, why,
      "这条用例没执行（不是通过、也不是产品缺陷）；请等服务稳定后复跑");
  };

  const finish = (code, extraNote) => {
    report.exit_code = code;
    report.duration_ms = nowMs() - T0;
    report.meta.finished_at = new Date().toISOString();
    /* 环境不可用会在任何断言跑之前就退出，此时 summary 还没算过 —— 统一在这里兜底，
       否则打印摘要会抛 "Cannot read properties of undefined"，把一个干净的 EXIT=2 弄成崩溃。
       兜底也必须用同一套三值口径，否则摘要又变成加不起来的了。 */
    if (!report.summary) {
      const passed = RESULTS.filter((r) => r.ok).length;
      const warned = RESULTS.filter((r) => !r.ok && r.level === "warn").length;
      const failed = RESULTS.filter((r) => !r.ok && r.level === "fail").length;
      report.summary = {
        checks_total: RESULTS.length,
        passed, warned, failed,
        info_total: INFO.length,
        algebra: { passed, warned, failed, sum: passed + warned + failed, total: RESULTS.length, ok: passed + warned + failed === RESULTS.length },
        level_ok: null,
        warn_split: { assertion_level: warned, phase_level: 0 },
        phase_warn_ids: [],
        aggregates: [],
      };
      report.infos = INFO;
    }
    /* ---------- 打印前的**硬断言**：摘要必须加得起来 ----------
       为什么放在 finish() 里而不是只做一条断言：
       断言能"发现"算术坏了，但只有这里能"阻止"它被当成一次合格运行 ——
       一份加不起来的摘要说明统计口径本身坏了，此时任何一个"全过"都不可信。
       不成立 → 强制 EXIT=1，并把三个数写进 notes 与 stdout（不让人去翻报告才知道）。 */
    const smx = report.summary || {};
    const closedN = (smx.passed || 0) + (smx.warned || 0) + (smx.failed || 0);
    if (closedN !== smx.checks_total) {
      smx.algebra = Object.assign({}, smx.algebra, { ok: false, closed: closedN });
      smx.algebra_ok = false;
      code = 1;
      report.exit_code = 1;
      NOTES.push(`⛔ 摘要算术不成立：通过 ${smx.passed} + 告警 ${smx.warned} + 失败 ${smx.failed} = ${closedN}` +
        ` ≠ 总数 ${smx.checks_total} → 强制 EXIT=1（这一轮不能作为验收依据）`);
    }
    try { fs.writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2), "utf8"); } catch (e) { console.error("写报告失败:", e.message); }
    if (extraNote) NOTES.push(extraNote);
    if (CFG.json) {
      process.stdout.write(JSON.stringify(report, null, 2) + "\n");
    } else {
      printHuman(report);
    }
    process.exit(code);
  };

  if (!pf.ok) {
    fail("env", "服务可用", pf.err, "门禁只验证「跑着的服务」；服务不通时任何 UI 断言都无意义，必须与产品缺陷区分开");
    finish(2, "服务不可用 → EXIT=2，请先启动 viewer（本门禁不会自行拉起进程）");
    return;
  }
  pass("env", "服务可用", `GET /api/version 200，build_id=${pf.version.build_id}`);
  NOTES.push(`服务 build_id=${pf.version.build_id} / 磁盘 build_id=${pf.diskBuild}` +
    (pf.staleBuild ? " —— 不一致：服务跑的是旧代码快照，依赖新代码的用例可能 FAIL（标记 stale_build）" : " —— 一致"));

  /* ---------- 5.1 健康 / 单实例 ---------- */
  rec("health", "/api/version 有 build_id",
    !!(pf.version && typeof pf.version.build_id === "string" && pf.version.build_id.length >= 6),
    `build_id=${pf.version && pf.version.build_id}`,
    "build_id 是「正在监听的进程是不是当前磁盘代码」的唯一依据；缺失则无法判断重启是否生效", "fail");
  const H = (pf.health && pf.health.json) || null;
  const leasesOk = !!(H && H.leases && Array.isArray(H.leases.items));
  rec("health", "/api/health 有 leases.items 数组", leasesOk,
    H ? `status=${pf.health.status} leases.items=${H.leases && H.leases.items ? (Array.isArray(H.leases.items) ? "array(" + H.leases.items.length + ")" : typeof H.leases.items) : "缺失"}` : "health 请求失败",
    "leases 是任务租约表：结构变了前端「系统健康」页会静默空白，故断言结构而不是文案", "fail");
  if (H && H.leases && H.leases.quota) {
    NOTES.push(`leases 配额: compute=${H.leases.quota.compute} fetch=${H.leases.quota.fetch} light=${H.leases.quota.light}，运行中=${H.leases.running_count}`);
  }
  if (pf.staleBuild) {
    warn("health", "服务与磁盘代码一致（stale_build）",
      `服务 build_id=${pf.version.build_id} ≠ 磁盘 ${pf.diskBuild}（指纹来源: ${pf.fpSource}）`,
      "不重启服务就换不掉旧代码；本项只告警，因为门禁无权重启（多人共用）");
  } else {
    pass("health", "服务与磁盘代码一致（build_id 相同）",
      `build_id=${pf.version.build_id}，指纹来源: ${pf.fpSource}${pf.replicaDrift ? "（Node 复刻已与权威值不一致，见 warn）" : ""}`);
  }
  /* 复刻漂移自检：Node 里那份 replica 一旦滞后，stale_build 会长期误报"服务跑旧代码"。
     现在以 Python 权威实现为准，复刻只做交叉校验；两者不一致就亮出来（不是产品问题，是我的复刻过期了）。 */
  if (pf.replicaDrift) {
    warn("health", "Node 复刻指纹与 Python 权威值一致（防止 stale_build 误报）",
      `python=${pf.replicaDrift.python} / node=${pf.replicaDrift.node} —— 复刻已滞后，已改用权威值`,
      "复刻滞后会让 stale_build 常年误报，喊人重启却永远对不上；以权威实现为准、复刻降级为交叉校验");
  }

  /* ---------- 5.2 起浏览器（起不来 = EXIT 2） ---------- */
  let puppeteer;
  try { puppeteer = require("puppeteer-core"); } catch (e) {
    fail("env", "puppeteer-core 可加载", e.message, "没有浏览器驱动就测不了 UI；这是环境问题不是产品问题");
    finish(2, "puppeteer-core 不可用 → EXIT=2");
    return;
  }
  const EXE = findChromium();
  if (!EXE) {
    fail("env", "找到可用 Chromium", "候选路径都不存在", "浏览器不可用属环境问题");
    finish(2, "找不到 Chromium → EXIT=2");
    return;
  }
  NOTES.push("浏览器: " + EXE);

  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: EXE,
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1440,1000"],
      protocolTimeout: 180000,
    });
  } catch (e) {
    fail("env", "浏览器可启动", e.message, "浏览器起不来属环境问题");
    finish(2, "Chromium 启动失败 → EXIT=2");
    return;
  }

  /* ---------- 5.3 主页面：错误/接口 4xx 收集器 ---------- */
  const page = await browser.newPage();
  page.setDefaultTimeout(TAB_TIMEOUT_MS);
  page.setDefaultNavigationTimeout(45000);
  await page.setViewport({ width: 1440, height: 1000 });

  let CURRENT_TAB = "(boot)";
  const consoleErrs = [];       // 原始 console error
  const consoleFiltered = [];   // 被判定为噪音而忽略的
  const pageErrs = [];
  const apiFails = [];          // /api/ 4xx 5xx
  const apiNetFails = [];       // 非 abort 的网络失败
  /* 在飞的 /api 请求数。用途：判断「二级导航是否已经稳定」——
     学习中心（learn）的子项由 kb_sources.yaml 动态生成、异步到达，
     只看数量稳定会数到一个"只有静态项"的中间态（实测同一份代码，
     一次枚举到 53 个 tab、另一次只有 43 个 —— 差的就是 10 个动态轨道）。
     枚举结果必须可复现，否则"43/43 全过"和"53/53 全过"就是两个含义不同的绿灯。 */
  let inflight = 0;

  /* 噪音过滤：这些不是产品缺陷，混进来会让门禁常红
     - CSP frame-ancestors 通过 <meta> 下发时浏览器必然打的警告（index.html 有意为之）
     - "Failed to load resource: 4xx" 与接口 4xx 是同一件事，已在 [api] 里单独统计 */
  const NOISE = [
    /frame-ancestors/i,
    /* 「Failed to load resource」是浏览器对每个失败请求自动打的一条日志，不是前端代码抛的错。
       4xx/5xx 与 net::ERR_* 都属这一类，且已经在 [api] 的失败统计里单独计过一次；
       留着它只会让「接口全挂」的场景刷出几十条 console error（实测全 500 时 50 条），
       属于"把同一个信号数两遍"，会让门禁看起来比实际严重。真正的信号是 pageerror。 */
    /Failed to load resource/i,
  ];
  const isNoise = (t) => NOISE.some((r) => r.test(t));

  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const t = m.text();
    if (isNoise(t)) { consoleFiltered.push({ text: t.slice(0, 200), tab: CURRENT_TAB }); return; }
    consoleErrs.push({ text: t.slice(0, 300), tab: CURRENT_TAB });
  });
  page.on("request", (r) => { if (r.url().indexOf("/api/") >= 0) inflight++; });
  page.on("pageerror", (e) => pageErrs.push({ text: String(e && e.message || e).slice(0, 300), tab: CURRENT_TAB }));
  page.on("response", (r) => {
    const u = r.url();
    if (u.indexOf("/api/") < 0) return;
    if (inflight > 0) inflight--;
    const st = r.status();
    if (st === 304) return;                       // 304 不是失败（走缓存）
    if (st >= 400) apiFails.push({ status: st, url: u.replace(BASE, ""), tab: CURRENT_TAB });
  });
  page.on("requestfailed", (r) => {
    const u = r.url();
    if (u.indexOf("/api/") < 0) return;
    if (inflight > 0) inflight--;                 // 与 request 计数配平
    const why = (r.failure() && r.failure().errorText) || "";
    if (/ERR_ABORTED/i.test(why)) return;         // 前端切 tab 会主动 abort 上一只票的请求，属正常
    apiNetFails.push({ url: u.replace(BASE, ""), why, tab: CURRENT_TAB });
  });

  /* ---------- 5.4 打开页面 + 关掉首启引导 ----------
   * 首次访问（干净 profile）会弹「开始使用」引导，它的 #firstRunMask 是
   * position:fixed;inset:0;z-index:9100 的全屏挡板 —— 不关掉，鼠标点击会全部落到挡板上，
   * 任何 tab 点击都点不动。这不是 bug（本来就要用户点「开始使用」），但如果门禁不知道这件事，
   * 就会把"点不动"误报成产品缺陷。所以：先真实点掉它，并单独断言它确实被点掉了。 */
  const dismissFirstRun = async (p) => {
    const st = await p.evaluate(() => {
      const btn = document.querySelector("#frDone");
      const mask = document.querySelector("#firstRunMask");
      const modal = document.querySelector("#firstRunModal");
      const visible = (el) => !!el && !el.hidden && el.getBoundingClientRect().height > 0;
      return {
        hasBtn: !!btn, btnVisible: visible(btn),
        maskShows: !!mask && mask.classList.contains("show"),
        modalVisible: visible(modal),
      };
    }).catch(() => null);
    if (!st || !st.hasBtn || !st.btnVisible) return { shown: false, st };
    /* 用真实鼠标点：顺便验证按钮没有被自己的挡板挡住（挡板挡按钮是常见事故） */
    const box = await p.evaluate(() => {
      const b = document.querySelector("#frDone").getBoundingClientRect();
      const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
      const btn = document.querySelector("#frDone");
      return { x: b.left + b.width / 2, y: b.top + b.height / 2, hitOk: !!hit && (hit === btn || btn.contains(hit)) };
    });
    if (!box.hitOk) { await p.evaluate(() => { const b = document.querySelector("#frDone"); if (b) b.click(); }); }
    else await p.mouse.click(box.x, box.y);
    await sleep(400);
    const after = await p.evaluate(() => {
      /* 注意：不能判 #frDone.hidden —— 引导关掉后按钮本身仍留在 DOM 里，
         真正隐藏的是它的祖先 #firstRunModal。判可见性要看渲染盒。 */
      const btn = document.querySelector("#frDone");
      const modal = document.querySelector("#firstRunModal");
      const mask = document.querySelector("#firstRunMask");
      const vis = (el) => !!el && !el.hidden && el.getBoundingClientRect().height > 0;
      return {
        btnVisible: vis(btn),
        modalVisible: vis(modal),
        maskShows: !!mask && mask.className.indexOf("show") >= 0,
      };
    });
    return { shown: true, hitOk: box.hitOk, after };
  };

  let nav = [];
  /* 折叠区展开统计：让"门禁帮用户点开了 N 次折叠"这件事可见，而不是悄悄发生 */
  const FOLD_STATS = { opened: 0 };
  try {
    phase("goto+boot");
    CURRENT_TAB = "(goto)";
    await page.goto(PAGE_URL, { waitUntil: "networkidle2", timeout: 45000 });
    await sleep(900);

    const fr = await dismissFirstRun(page);
    if (fr.shown) {
      const closed = fr.hitOk && fr.after && !fr.after.btnVisible && !fr.after.modalVisible && !fr.after.maskShows;
      rec("boot", "首启引导可被真实点击关闭", closed,
        `hitOk=${fr.hitOk} after=${JSON.stringify(fr.after)}`,
        "干净浏览器首次访问会弹引导，其 #firstRunMask 是全屏挡板；它关不掉就意味着新用户第一屏点不动任何东西", "fail");
    } else {
      NOTES.push("未出现首启引导（localStorage 已有 viewer-first-run-done），跳过该项");
    }

    phase("enumerate-nav");
    /* ---------- 5.5 动态枚举导航 ----------
     * 两级：分类（#sidenav .nav-item[data-cat]）→ 子板块（#subnav .sub-tab[data-tab]）。
     * 子板块只渲染「当前分类」下的，所以必须先逐个点开分类才能看全。
     * 学习中心（learn）的子项由 kb_sources.yaml 动态生成，异步到达 ——
     * 因此子项数量要"连续两次相同"才认为稳定，否则会把 8 个当全部、漏掉后到的 4 个。 */
    const cats = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#sidenav .nav-item[data-cat]")).map((a) => ({
        cat: a.dataset.cat,
        label: (a.querySelector(".ni-txt") || a).innerText.trim(),
      }))
    );
    rec("nav", "一级分类可枚举", cats.length > 0, `分类 ${cats.length} 个: ${cats.map((c) => c.cat).join(",")}`,
      "一条分类都读不到说明导航区整体没渲染，后面所有 tab 断言都不必跑", "fail");

    /* 等二级导航就绪：必须同时满足
       ① 首个 tab 的 href 已属于目标分类（切分类瞬间列表还是上一个分类的）
       ② 数量连续两次不变
       ③ 没有在飞的 /api 请求（动态轨道还在拉数据时，数量也会"稳定"在只有静态项的中间态）
       ④ 知识库状态 chip 不再是「读取中」（DYN 分类的显式就绪信号）
       只看数量会被坑：这正是第一版门禁把 3 个 tab 全判失败、以及枚举出 43/53 两种结果的原因。 */
    const settleSubnav = async (catKey) => {
      let prev = -1, stable = 0;
      const maxRounds = CFG.quick ? 26 : 40;
      for (let i = 0; i < maxRounds; i++) {
        await sleep(280);
        const st = await page.evaluate((cat) => {
          const list = Array.from(document.querySelectorAll("#subnav .sub-tab[data-tab]"));
          return {
            n: list.length,
            firstHref: list.length ? (list[0].getAttribute("href") || "") : "",
            want: "#" + cat + "/",
            navActive: (() => { const a = document.querySelector(`#sidenav .nav-item[data-cat="${cat}"]`); return !!a && a.className.indexOf("active") >= 0; })(),
            kbBusy: (() => { const c = document.querySelector("#kbStateChip"); return !!c && /读取中|加载中/.test(c.textContent || ""); })(),
          };
        }, catKey);
        const inCat = st.n > 0 && st.firstHref.indexOf(st.want) === 0;
        const ready = inCat && !st.kbBusy && inflight === 0 && i > 1;   // i>1: 至少等两轮，避免"刚点完还是空的就宣布稳定"
        if (ready && st.n === prev && st.navActive) { if (++stable >= 2) return st.n; } else stable = 0;
        prev = st.n;
      }
      return prev;
    };

    /* 切到某个一级分类（已在该分类则不重复点，避免无谓重渲染） */
    let currentCat = null;
    const ensureCat = async (catKey) => {
      if (currentCat === catKey) return;
      await page.evaluate((cat) => {
        const a = document.querySelector(`#sidenav .nav-item[data-cat="${cat}"]`);
        if (a) a.click();
      }, catKey);
      await settleSubnav(catKey);
      currentCat = catKey;
    };

    /* 展开二级横条里的「折叠区」。为什么必须有这一步：
       2026-11-01 起横条把一部分子板块收进 <details class="sub-fold">「研究工具（N）」，
       默认收起 —— 收起时里面的 <a data-tab> 仍存在于 DOM（所以枚举得到），但**不可见也不可点**：
       真实鼠标点击会落到横条或下方面板上，表现为"点了没反应"。
       实测：53 个 tab 里有 33 个在折叠区里，不展开就跑，会得到 20/53 通过的假红。
       做法不写死 class：找 #subnav 里所有未展开的 <details>，优先真实点击它的 <summary>
       （用户也是这么做的），点不到（被遮挡）才退回改 open 属性。 */
    const openSubnavFolds = async () => {
      try {
        return await page.evaluate(() => {
          const closed = Array.from(document.querySelectorAll("#subnav details:not([open])"));
          let opened = 0, byClick = 0;
          for (const d of closed) {
            const s = d.querySelector("summary");
            if (s) {
              const r = s.getBoundingClientRect();
              const hit = r.width > 2 ? document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) : null;
              if (hit && (hit === s || s.contains(hit))) { s.click(); opened++; byClick++; continue; }
            }
            d.open = true; opened++;
          }
          return { opened, byClick };
        });
      } catch (_) { return { opened: 0, byClick: 0 }; }
    };

    /* 统一的「点某个子板块」动作：先切分类 → 再展开折叠区 → 再点。
       横条每次重绘都会重建 <details>（并且只在"当前 tab 就在折叠区里"时才自动 open），
       所以展开动作必须放在**每次点击之前**，不能只在切分类时做一次。 */
    const clickSubTab = async (item) => {
      await ensureCat(item.cat);
      const fold = await openSubnavFolds();
      if (fold && fold.opened) FOLD_STATS.opened += fold.opened;
      await page.click(`#subnav .sub-tab[data-tab="${item.tab}"]`);
      return fold;
    };

    /* 按「面板里有没有某个元素」反查 tab —— 用来在不写死 tab 名的前提下定位特定功能面板。
       例：关注池 → 有 #wtAdd；也用于抽屉测试的行。找不到返回 null（调用方自己判失败）。 */
    const findTabHaving = async (innerSelector) => {
      for (const it of nav) {
        await clickSubTab(it).catch(() => {});
        await sleep(240);
        const has = await page
          .evaluate(([t, s]) => !!document.querySelector(`#p-${t} ${s}`), [it.tab, innerSelector])
          .catch(() => false);
        if (has) return it;
      }
      return null;
    };
    const drawerOpen = () => page.evaluate(() => !!document.querySelector("#drawer.show") && document.querySelector("#drawer").getBoundingClientRect().height > 40);

    for (const c of cats) {
      await ensureCat(c.cat);
      const subs = await page.evaluate((cat) =>
        Array.from(document.querySelectorAll("#subnav .sub-tab[data-tab]")).map((a) => ({
          tab: a.dataset.tab,
          /* 用 textContent 而不是 innerText：折叠区（<details> 未展开）里的项 innerText 为空，
             用它会把 33 个 tab 的标签全部读成空字符串，报告里就只剩 "today/executability  ——"。 */
          label: (a.textContent || "").trim(),
          href: a.getAttribute("href") || "",
          /* 子项没有 data-cat 属性，分类只能从 href="#cat/tab" 里解析 —— 见 needs_testid */
          hrefCat: (a.getAttribute("href") || "").replace(/^#/, "").split("/")[0] || "",
          hasDataCat: a.hasAttribute("data-cat"),
          /* 2026-11-01：分类有独立属性了，优先用它；href 只作兜底（href 换成 JS 跳转时也能判分类） */
          dataCat: a.getAttribute("data-cat") || "",
          /* 是否在「研究工具（N）」折叠区里：这些项默认收起，点击前必须先展开 */
          folded: !!(a.closest && a.closest("details")),
          active: a.className.indexOf("active") >= 0,
        })), c.cat);
      for (const s of subs) nav.push({ cat: c.cat, catLabel: c.label, ...s });
      /* quick 只需要"前 3 个 tab（含 today）"，把 6 个分类全枚举一遍纯属浪费 ——
         实测这一步在冷服务上能吃掉 20s，把 quick 顶到 39s，离 40s 预算只剩 1s 余量。
         枚举到够用就停，并在 notes 里写明"quick 的 by_cat 是部分结果"，避免被当成全量口径。 */
      if (CFG.quick && nav.length >= 3) {
        NOTES.push(`quick 模式：枚举到 ${nav.length} 个 tab 即停（够 3 个就够用），classified=${cats.length} 个分类中只走了 ` +
          `${cats.findIndex((x) => x.cat === c.cat) + 1} 个 —— **quick 的 by_cat 不是全量口径**`);
        break;
      }
    }

    /* 枚举自一致性校验：把最后一个分类再点一次，数量必须与刚才记录的一致。
       动态轨道（learn）异步到达时，"数量稳定"可能停在只有静态项的中间态 ——
       实测同一份代码一次数到 53、一次数到 43（差的就是 10 个动态轨道）。
       枚举结果不可复现的话，"53/53 全过"与"43/43 全过"是两个含义不同的绿灯，必须抓出来。 */
    const byCat = {};
    for (const x of nav) byCat[x.cat] = (byCat[x.cat] || 0) + 1;
    let recheck = null;
    if (cats.length && nav.length) {
      /* 复查要挑**真的枚举过**的分类：quick 模式只走了一两个分类就停，
         拿最后一个分类（可能根本没访问）来复查，会得到 "首次 undefined / 复查 1 个" 的假红。
         这里取"已枚举分类里的最后一个"，两种模式都成立。 */
      const visited = cats.map((c) => c.cat).filter((k) => byCat[k] > 0);
      const lastCat = visited[visited.length - 1];
      currentCat = null;                                  // 强制重点，不吃缓存
      await ensureCat(lastCat);
      const n2 = await page.evaluate(() => document.querySelectorAll("#subnav .sub-tab[data-tab]").length);
      recheck = { cat: lastCat, first: byCat[lastCat], second: n2 };
      rec("nav", "导航枚举可复现（同一分类两次枚举数量一致）", n2 === byCat[lastCat],
        `${lastCat}: 首次 ${byCat[lastCat]} 个 / 复查 ${n2} 个；各分类 ${JSON.stringify(byCat)}`,
        "动态轨道异步到达会让枚举少算一批 tab，于是「全过」可能只是「只测了静态项」", "fail");
    }

    /* 枚举质量：重名 / 缺 href / 缺 data-cat / 数量
     * 这四项任一坏了，后面的点击断言就会"看起来绿实际测了个空"。 */
    const tabs = nav.map((x) => x.tab);
    const dups = tabs.filter((v, i) => tabs.indexOf(v) !== i);
    const noHref = nav.filter((x) => !x.href || x.href.indexOf("#") !== 0);
    const noDataCat = nav.filter((x) => !x.hasDataCat);
    const catNoTab = nav.filter((x) => (x.hasDataCat ? x.dataCat !== x.cat : x.hrefCat !== x.cat));
    const rawCount = nav.length;
    /* 重复 tab：真缺陷（同一条横条里同一个入口渲染了两遍），但要按 (cat,tab) 去重后再跑，
       否则同一个面板会被点两遍，纯浪费预算。原始条数仍写进报告。 */
    const dupDetail = dups.map((d) => {
      const occ = nav.filter((x) => x.tab === d).map((x) => x.cat + "|" + x.href);
      return d + "(" + occ.join(",") + ")";
    });
    const seenKey = new Set();
    nav = nav.filter((x) => {
      const k = x.cat + "/" + x.tab;
      if (seenKey.has(k)) return false;
      seenKey.add(k);
      return true;
    });

    rec("nav", "tab 标识不重复（同一条横条里没有重复入口）", dups.length === 0,
      `重复 ${dups.length} 个 / 原始 ${rawCount} 个: ${dupDetail.slice(0, 6).join(" ")}`,
      "同一个 tab 在同一横条里出现两次＝用户看到两个一模一样的入口，且会让「点第几个」类断言指错元素", "fail");
    rec("nav", "每个 tab 都有 #cat/tab 深链接", noHref.length === 0, `缺 href: ${noHref.length} 个 ${JSON.stringify(noHref.slice(0, 5).map((x) => x.tab))}`,
      "深链接可用性是回归重点（旧版就是靠 hash 判断的），没有 href 就没法验证深链", "fail");
    rec("nav", "href 里的分类与所属分类一致", catNoTab.length === 0,
      `不一致: ${JSON.stringify(catNoTab.slice(0, 5).map((x) => x.tab + "→" + x.hrefCat))}`,
      "分类与深链不一致＝点进去 hash 会跳到另一个分类，属于导航归属错误", "fail");
    if (noDataCat.length) {
      /* 2026-11-01：data-cat 已加。这里只会在「改版把属性弄丢」时命中，属真缺陷但不红 ——
         分类断言还有 href 可用（catNoTab 仍按 fail 计），所以降级为提示。 */
      warn("nav", ".sub-tab 的 data-cat 属性", `缺 ${noDataCat.length} 个（分类断言已退回解析 href）`,
        "data-cat 是分类断言的直接依据；丢了它就只能从 href 反解，href 改成 JS 跳转时会静默失效");
      NEEDS_TESTID.push("[nav] .sub-tab 缺 data-cat：" + noDataCat.length + " 个 tab 退回解析 href。");
    }
    /* 2026-11-01：面板已由 app.js 的 initPanelReadyWatcher 打 data-ready="1"。
       这里现场抽一个当前激活面板确认标记真的在工作；没有才降级回 innerText 轮询并登记。 */
    const readyProbe = await page.evaluate(() => {
      const p = document.querySelector(".panel.active");
      return { id: p ? p.id : null, ready: !!(p && p.getAttribute("data-ready") === "1"),
        anyReady: document.querySelectorAll('.panel[data-ready="1"]').length };
    });
    NOTES.push(`面板就绪标记 data-ready：当前激活面板 ${readyProbe.id} ready=${readyProbe.ready}；` +
      `全站已标记 ${readyProbe.anyReady} 个`);
    if (!readyProbe.anyReady) {
      warn("nav", "面板 data-ready 标记可用", "一个面板都没有 data-ready",
        "没有就绪标记时，门禁只能靠 sleep + 文本长度猜「画完了没有」，既慢又会误判空态");
      NEEDS_TESTID.push("[nav] 面板缺 data-ready 标记（app.js initPanelReadyWatcher 失效？）—— 已退回 innerText 轮询。");
    }
    /* 2026-11-01：抽屉/成交量/自选行的 testid 都已在 app.js 落地，这段"建议加 testid"的文案随之作废。
       改为**在 unit 阶段实测**（打开抽屉后验证 stk-drawer / vol-quote / vol-kline 是否真的在），
       测不到才登记 needs_testid —— 静态地在这里喊一句"建议加"，等加完了就变成永久的假告警。 */
    NOTES.push("抽屉/成交量/自选行已补 data-testid（stk-drawer / vol-quote / vol-kline / watch-row），实际可用性在 unit 与 watch 阶段实测。");
    /* 2026-11-01：自选行已有 data-testid="watch-row" + data-symbol，行定位改用 testid（曾用 [data-wt-open] 反查）。
       这里只探一下当前页面里是否真的有（watch 阶段会再实测增删），没有才登记。
       弹窗 #wtF-symbol / #wtF-save 仍是 id 选择器：它们不在本轮 testid 范围内，留一条提示。 */
    { const wr = await page.evaluate(() => document.querySelectorAll('[data-testid="watch-row"]').length);
      if (!wr) {
        NEEDS_TESTID.push("[watch] 当前面板没有 [data-testid=\"watch-row\"]（可能还没打开关注池，或属性丢了）—— 行定位会退回 [data-wt-open]。");
      } }

    NOTES.push(`导航枚举结果：${cats.length} 个分类 / ${nav.length} 个 tab` +
      (pf.staleBuild ? "（服务为旧快照，若分类数少于磁盘版本属正常）" : ""));

    /* ---------- 5.6 逐 tab 检查（四件事） ----------
     * 每个 tab 断言：
     *   ① 点击后 hash 变成 #cat/tab（深链接可用）
     *   ② 无新增 console error / pageerror
     *   ③ 无新增 /api/ 4xx/5xx
     *   ④ 可见文本无 undefined/NaN/null/Invalid Date/[object Object]
     * 另外顺手记两项不作为硬失败的：面板是否有内容（空的可能只是当天没数据）、点击是否被遮挡。
     * quick 模式只跑前 3 个（自然覆盖 today，因为它是第一个）—— 不写死名字。 */
    const navList = CFG.quick ? nav.slice(0, 3) : nav;
    if (CFG.quick) NOTES.push(`quick 模式：只遍历前 3 个 tab（${navList.map((x) => x.cat + "/" + x.tab).join("、")}）`);

    phase("nav-tabs");
    let navPassed = 0;
    for (const item of navList) {
      const tStart = nowMs();
      if (timedOut()) {
        fail("nav", `预算内跑完（卡在 ${item.cat}/${item.tab}）`,
          `已用 ${Math.round((nowMs() - T0) / 1000)}s > 预算 ${Math.round(BUDGET_MS / 1000)}s`,
          "卡住本身就是缺陷：某个 tab 点开后一直不返回，用户就是这么被卡死的");
        break;
      }
      CURRENT_TAB = `${item.cat}/${item.tab}`;
      const cErr0 = consoleErrs.length, pErr0 = pageErrs.length, api0 = apiFails.length;

      /* 关键：子板块只渲染在「它所属分类」被选中时。遍历到第 20 个 tab 时二级导航还停在上一个分类，
         直接按 data-tab 查元素会「元素不存在」—— 所以每次点击前先把分类切回去。
         紧接着展开折叠区（否则折叠区里的 tab 不可见不可点，真实点击会落到别处）。 */
      let catSwitch = true;
      try { await ensureCat(item.cat); } catch (e) { catSwitch = false; }
      let fold = { opened: 0 };
      try { fold = await openSubnavFolds(); } catch (e) { /* 展开失败照样试点击，失败会被下面的断言抓到 */ }
      if (fold && fold.opened) FOLD_STATS.opened += fold.opened;

      /* 点击前先做命中测试：如果 tab 被别的元素盖住，点击会落到盖住它的东西上，
         断言就会"莫名失败"，而真正的原因是布局/挡板问题 —— 分开报才查得动。 */
      let hit = { ok: true, detail: "" };
      try {
        hit = await page.evaluate((tab) => {
          const a = document.querySelector(`#subnav .sub-tab[data-tab="${tab}"]`);
          if (!a) return { ok: false, detail: "元素不存在" };
          a.scrollIntoView({ block: "nearest", inline: "nearest" });
          const r = a.getBoundingClientRect();
          if (r.width < 4 || r.height < 4) return { ok: false, detail: `尺寸 ${Math.round(r.width)}x${Math.round(r.height)}` };
          const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          const ok = !!el && (el === a || a.contains(el));
          return { ok, detail: ok ? "" : `被 ${el ? el.tagName + "." + String(el.className).split(" ")[0] : "空"} 遮挡` };
        }, item.tab);
      } catch (e) { hit = { ok: false, detail: e.message }; }

      let clicked = true;
      try {
        await page.click(`#subnav .sub-tab[data-tab="${item.tab}"]`);
      } catch (e) {
        clicked = false;
        fail("nav", `[${CURRENT_TAB}] 可点击`, e.message.slice(0, 160),
          "点击都失败就不可能验证渲染；常见原因是选择器不存在、在折叠区里没展开、或被遮挡");
      }

      /* 等 hash 与面板就绪（不 sleep 死等：轮询，命中即走）。
         分两段等，别混在一起等：
           ① 等「hash + 选中态」—— 这是导航是否生效的判据，最多 5s；
           ② 等「面板有内容」—— 空面板是合法状态（当天没数据），最多再等 3s。
         第一版把两者塞进同一个 9s 循环里，遇到 33 个空面板的 tab 就变成 33×9s=297s，
         nav 阶段直接吃掉全量预算 —— 门禁自己把自己跑死了。 */
      let hashOk = false, panel = { panelExists: false, panelLen: 0, ready: false };
      /* 用字符串形式的 evaluate 把 panelTextInPage 注进页面（与 scanBadTokensInPage 同一手法）：
         面板可能是 iframe（见 panelTextInPage 注释），直接读父文档 innerText 会得到 0。 */
      const readTabState = (x) => page.evaluate(
        `(function(){
           var t = (${panelTextInPage.toString()})(${JSON.stringify("#p-" + x.tab)});
           var a = document.querySelector(${JSON.stringify('#subnav .sub-tab[data-tab="' + x.tab + '"]')});
           return {
             hash: location.hash,
             panelExists: t.exists, panelLen: t.len, ownLen: t.ownLen, subLen: t.subLen,
             frame: t.frame, frameLoaded: t.frameLoaded, ready: t.ready, head: t.head,
             /* 选中态：横条上该项是否带 .active —— 不参与 hard fail（hash 才是深链接判据），
                但要记录，因为"hash 对了但横条高亮没跟上"是用户能一眼看出来的错位 */
             active: !!a && a.className.indexOf("active") >= 0
           };
         })()`
      ).catch(() => null);

      const tNav = nowMs() + (CFG.quick ? 4000 : 5000);
      while (nowMs() < tNav) {
        await sleep(130);
        const st = await readTabState(item);
        if (!st) break;
        panel = st;
        hashOk = st.hash === `#${item.cat}/${item.tab}`;
        if (hashOk && st.active) break;
      }
      if (hashOk) {
        const tContent = nowMs() + 3000;
        /* 有 data-ready 就以它为准（内容为空也算"画完了"，空态合法）；没有才退回文本长度。 */
        while (!panel.ready && panel.panelLen === 0 && nowMs() < tContent) {
          await sleep(200);
          const st = await readTabState(item);
          if (!st) break;
          panel = st;
        }
      }

      const cErr = consoleErrs.length - cErr0;
      const pErr = pageErrs.length - pErr0;
      const apiNew = apiFails.slice(api0);
      const scan = await page
        .evaluate(`(${scanBadTokensInPage.toString()})(${JSON.stringify("#p-" + item.tab)})`)
        .catch(() => null);

      const hardBad = scan ? scan.hits.filter((h) => h.hard) : [];
      const softBad = scan ? scan.hits.filter((h) => !h.hard) : [];
      const tabOk = clicked && hashOk && cErr === 0 && pErr === 0 && apiNew.length === 0 && hardBad.length === 0;

      item.result = {
        ok: tabOk,
        clicked,
        hit: hit.ok ? "ok" : hit.detail,
        hash: panel.hash,
        hash_ok: hashOk,
        active: panel.active,
        panel_exists: panel.panelExists,
        panel_len: panel.panelLen,
        /* 三口径原样落盘，便于别人复核"空/非空"是怎么判的 */
        panel_own_len: panel.ownLen,
        panel_sub_len: panel.subLen,
        panel_frame: !!panel.frame,
        panel_frame_loaded: panel.frameLoaded === undefined ? null : panel.frameLoaded,
        panel_ready: !!panel.ready,
        panel_empty: !!panel.empty,
        new_console_err: cErr,
        new_page_err: pErr,
        new_api_fails: apiNew,
        bad_tokens: scan ? scan.hits : [],
        ms: nowMs() - tStart,
      };

      if (tabOk) navPassed++;
      const detail = [
        `hash=${panel.hash}${hashOk ? "" : " ✗"}`,
        `面板${panel.panelExists ? "存在" : "缺失"}/${panel.panelLen}字` +
          (panel.frame ? `(iframe 子文档 ${panel.subLen}字${panel.frameLoaded === "1" ? ",已加载" : ",未打 data-loaded"})` : ""),
        cErr ? `console错 ${cErr}` : "console净",
        pErr ? `pageerror ${pErr}` : "无pageerror",
        apiNew.length ? `接口失败 ${apiNew.length}(${apiNew.slice(0, 2).map((f) => f.status + " " + f.url).join(" ")})` : "接口净",
        hardBad.length ? `脏字符 ${JSON.stringify(hardBad.map((h) => h.token))}` : "无脏字符",
        hit.ok ? "" : `点击位置:${hit.detail}`,
        `${nowMs() - tStart}ms`,
      ].filter(Boolean).join(" | ");
      rec("nav", `[${CURRENT_TAB}] ${item.label}`, tabOk, detail,
        "这是用户最常做的一件事：点导航看面板。hash/报错/接口/脏字符四项任一坏掉，都是用户能直接看到的故障", "fail");

      if (!tabOk) {
        /* 接口明细统一在 finally 里汇总（那里有全量 apiFails），此处只补上下文，避免重复计数 */
        if (hardBad.length) {
          item.result.bad_context = hardBad.slice(0, 3).map((h) => h.token + " :: " + h.context);
        }
      }
      /* 空面板口径 = 三口径全空（见 panelTextInPage 注释：父文本 / 子文档文本 / data-ready+data-loaded）。
         只读父文档会把 iframe 面板稳定误报成空 —— 上一轮就是这样把 fundmasters 记进了 empty_panels。 */
      if (panel.panelExists && panel.empty) {
        report.nav.empty_panels.push(CURRENT_TAB);
        report.nav.empty_detail = report.nav.empty_detail || {};
        report.nav.empty_detail[CURRENT_TAB] = {
          own: panel.ownLen, sub: panel.subLen, frame: !!panel.frame,
          frame_loaded: panel.frameLoaded === undefined ? null : panel.frameLoaded, ready: !!panel.ready,
        };
      }
      if (softBad.length) {
        warn("nav", `[${CURRENT_TAB}] 出现独立 null 文本`, softBad.map((h) => h.context).join(" ／ ").slice(0, 300),
          "null 用词边界匹配、并排除了 pre/code，理论上中文 UI 不该出现；降级为 warn 是因为它偶尔来自合法示例文本");
      }
      if (hit.ok === false) {
        warn("nav", `[${CURRENT_TAB}] 点击命中未被遮挡`, hit.detail,
          "被遮挡时真实用户点不到该 tab（但 JS click 仍可能成功），必须单独暴露");
      }
    }

    report.nav.tabs = navList.map((x) => ({
      cat: x.cat, tab: x.tab, label: x.label, href: x.href, has_data_cat: x.hasDataCat,
      result: x.result || null,
    }));
    report.nav.total = navList.length;
    report.nav.passed = navPassed;
    report.nav.failed = navList.length - navPassed;
    report.nav.enumerated_total = nav.length;
    report.nav.enumerated_raw = rawCount;
    report.nav.duplicate_detail = dupDetail;
    report.nav.folded_tabs = nav.filter((x) => x.folded).map((x) => x.cat + "/" + x.tab);
    report.nav.fold_opens = FOLD_STATS.opened;
    report.nav.by_cat = byCat;
    report.nav.recheck = recheck;
    report.nav.duplicates = dups;
    report.nav.missing_href = noHref.map((x) => x.tab);
    report.nav.href_cat_mismatch = catNoTab.map((x) => x.tab);

    phase("deep");
    /* ---------- 5.65 深链接冷启动（点出来的绿 ≠ 直接打开的绿） ----------
     * 为什么单独做这一段（这是本轮补的最有价值的一条）：
     * 上面的导航遍历全部是「点」，而「点」天然绕过了**首屏时序**问题 ——
     * 2026-11-01 实测过一个真 bug：app.js 的启动段在本文件被解析时就执行，
     * 而 `douyin_corpus.js / funnel.js / industry_leaders.js / bottleneck.js / screener_extra.js`
     * 都排在 app.js 之后。于是首屏落在依赖这些脚本的面板上时，`window.DOUYIN` 还是 undefined，
     * `if (tab === "dy_reso" && window.DOUYIN)` 这类守卫**静默跳过**，面板永远停在「正在加载数据…」；
     * 点别的 tab 再点回来就正常。**点击型回归永远抓不到它**，只有"直接带 hash 冷启动"能抓到。
     *
     * 取样分**两组**（缺任一组都不算覆盖）：
     *   A. 通用取样：枚举结果的第一个 / 中间 / 最后一个 —— 不写死 tab 名，守"这一类"问题；
     *   B. 已知缺陷回归：`dy_reso / funnel / tech / integrate / sentiment / industry-leaders /
     *      bottleneck / screener / overseas_sentiment / geo_events / lowbox_bottom / lowbox_radar`
     *      **写死名单**逐个冷启动 —— 守"这一个"缺陷确实修好了。理由见下方 B 段注释：
     *      只跑 A，3 个取样很可能一个都没落在这批面板上，"3/3 通过"证明的就不是那个 bug。
     * 判据三条（两组共用同一个 coldStart，保证尺子只有一把）：
     *   ① 面板有内容（iframe 感知）—— 冷启动渲了东西；
     *   ② 不仍停在「正在加载数据」占位 —— 有字但只是一句"正在加载"等于没渲；
     *   ③ 冷启动期间无 pageerror。
     * 等就绪用轮询（data-ready / 有内容 / 超时），不 sleep 死等。
     */
    if (!timedOut()) {
      /* 冷启动探一个面板：goto(#cat/tab) → 轮询到「有内容且不再是占位」→ 三条判据。
         抽成函数是因为下面**两组**用例（通用取样 / 已知缺陷回归）必须用同一把尺子量；
         各写一遍的话，迟早有一处悄悄少一条判据，那时两条绿断言证明的就不是同一件事了。 */
      const coldStart = async (cat, tab, pollMs) => {
        CURRENT_TAB = `(deep) ${cat}/${tab}`;
        const pErrs0 = pageErrs.length;
        const url = `${PAGE_URL}#${cat}/${tab}`;
        let gotoErr = null;
        try { await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 }); } catch (e) { gotoErr = e.message; }
        /* 首启引导可能在新文档里再弹一次（不同 profile 状态下），不处理它不影响读文本，只影响点击 */
        let st = null;
        const tEnd = nowMs() + (pollMs || 20000);
        while (nowMs() < tEnd) {
          await sleep(300);
          st = await page.evaluate(
            `(function(){ return (${panelTextInPage.toString()})(${JSON.stringify("#p-" + tab)}); })()`
          ).catch(() => null);
          if (!st) break;
          if ((st.ready || st.len > 0) && !/正在加载数据/.test(st.head)) break;
        }
        const stuck = !!st && /正在加载数据/.test(st.head);
        const pErr = pageErrs.length - pErrs0;
        const ok = !!st && st.exists && st.len > 0 && !stuck && pErr === 0 && !gotoErr;
        return {
          tab: cat + "/" + tab, ok, url, gotoErr,
          exists: !!st && st.exists, len: st ? st.len : 0, ready: !!st && st.ready,
          frame: !!st && st.frame, stuck, pageerr: pErr, head: st ? st.head : "",
        };
      };
      /* 冷启动失败时先分环境：服务在这一次 goto 前后掉线 → 浏览器错误页，不是产品缺陷。
         返回 true = 已记成「未执行」，调用方不要再记一条红。 */
      const coldEnvSkip = async (label) => {
        const reach = await httpJson("/api/version", undefined, 5000).catch(() => null);
        if (!reach || reach.status !== 200) {
          envSkip(label, "打开页面时服务不可达（" + (reach ? "HTTP " + reach.status : "连接失败") + "），本例未执行");
          return true;
        }
        return false;
      };
      const deepDetail = (r) =>
        `goto${r.gotoErr ? "失败:" + r.gotoErr.slice(0, 80) : "OK"} | 面板${r.exists ? "存在" : "缺失"}/${r.len}字` +
        (r.frame ? "(iframe)" : "") + ` | ready=${r.ready} | 停在「正在加载」=${r.stuck} | pageerror ${r.pageerr}` +
        ` | 首屏「${r.head}」`;
      const DEEP_WHY = "「点出来正常」不等于「直接打开正常」：依赖后置脚本的面板在首屏会静默停在加载中，" +
        "而分享链接/刷新/收藏夹全是冷启动路径 —— 点击型回归永远漏这一档";

      /* ----- A. 通用取样：枚举结果的第一个 / 中间 / 最后一个 -----
         不写死任何 tab 名，也不挑"看起来容易坏"的。这一组的价值在于覆盖面：
         哪天别处又坏一个，随机取样有机会撞上。 */
      const DEEP = [];
      const picks = [];
      if (nav.length) {
        const idx = Array.from(new Set([0, Math.floor(nav.length / 2), nav.length - 1]));
        for (const i of idx) picks.push(nav[i]);
      }
      for (const it of picks) {
        if (timedOut()) break;
        const r = await coldStart(it.cat, it.tab);
        if (!r.ok && await coldEnvSkip(`深链接冷启动 #${r.tab}`)) {
          DEEP.push({ ...r, ok: null, env_skip: true });
          continue;
        }
        DEEP.push(r);
        rec("deep", `深链接冷启动（通用取样）#${r.tab} 直接打开就有内容`, r.ok, deepDetail(r), DEEP_WHY, "fail");
      }
      report.deep = DEEP;

      /* ----- B. 已知缺陷回归：后置脚本面板冷启动（**写死名单**） -----
       * 为什么这里必须写死（与"不许写死 tab 名单去遍历导航"不矛盾，这是两件事）：
       *   A 组守的是「这一类」问题能被动撞见 —— 价值在覆盖面；
       *   B 组守的是「这一个」问题确实修好了 —— 价值在确定性。
       *   只留 A 组时，3 个取样很可能一个都没落在这批面板上，于是"3/3 冷启动正常"
       *   证明的是别的面板正常。这是最难发现的那类测试：**通过了，但测的不是那个东西**。
       *
       * 名单来源（按现盘核对，勿凭记忆改）。viewer/static/index.html 里排在
       * app.js(:326) **之后**还有 5 个 <script>，它们提供的正是 app.js 早就引用过的全局：
       *   screener_extra.js   → window.SC_EXTRA.init                  ← app.js:12019（renderScreener 末尾钩子）
       *   industry_leaders.js → window.loadIndustryLeaders            ← app.js:14474
       *   bottleneck.js       → window.loadBottleneck                 ← app.js:14476
       *   douyin_corpus.js    → window.DOUYIN.renderTech / .renderIntegration
       *                         / .renderResonance / .renderSentimentBox
       *                                                               ← app.js:14485 / 14486 / 14487 / 3317
       *   funnel.js           → window.DOUYIN.renderFunnel            ← app.js:14488
       * 这些调用点全是 `if (window.X)` 形状的**静默守卫**：脚本还没到就跳过，不报错、不进 console。
       * ⇒ 今后 index.html 若再加一个排在 app.js 之后的 <script>，**请往这张表补一条**，
       *   否则那个面板的冷启动就重新回到"没人测"的状态。
       * 另外 4 条是 2026-11-01 导航收敛时新拆出来的子面板（overseas_sentiment / geo_events /
       * lowbox_bottom / lowbox_radar）：首绘走 deferPaint + ensureOverseasShell，与后置脚本
       * 同属"首屏时序"这一类，一并钉住。
       * `cat` 是兜底归属（枚举没给出时用）：有 NAV 静态项的 tab 会被页面自己的 TAB_HOME 扶正，
       * KB_FIXED_TABS 那 4 个（tech/integrate/dy_reso/funnel）不在 NAV 里，必须写对（strategy）。 */
      const POST_SCRIPT_TABS = [
        { tab: "dy_reso", cat: "strategy", script: "douyin_corpus.js", global: "window.DOUYIN.renderResonance", at: "app.js:14487" },
        { tab: "funnel", cat: "strategy", script: "funnel.js", global: "window.DOUYIN.renderFunnel", at: "app.js:14488" },
        { tab: "tech", cat: "strategy", script: "douyin_corpus.js", global: "window.DOUYIN.renderTech", at: "app.js:14485" },
        { tab: "integrate", cat: "strategy", script: "douyin_corpus.js", global: "window.DOUYIN.renderIntegration", at: "app.js:14486" },
        { tab: "sentiment", cat: "market", script: "douyin_corpus.js", global: "window.DOUYIN.renderSentimentBox", at: "app.js:3317" },
        { tab: "industry-leaders", cat: "market", script: "industry_leaders.js", global: "window.loadIndustryLeaders", at: "app.js:14474" },
        { tab: "bottleneck", cat: "mine", script: "bottleneck.js", global: "window.loadBottleneck", at: "app.js:14476" },
        { tab: "screener", cat: "mine", script: "screener_extra.js", global: "window.SC_EXTRA.init", at: "app.js:12019" },
        { tab: "overseas_sentiment", cat: "market", script: "（导航收敛新子面板）", global: "deferPaint(overseas_sentiment)", at: "app.js:7979" },
        { tab: "geo_events", cat: "market", script: "（导航收敛新子面板）", global: "deferPaint(geo_events)", at: "app.js:7984" },
        { tab: "lowbox_bottom", cat: "mine", script: "（导航收敛新子面板）", global: "loadBottom", at: "app.js:14445" },
        { tab: "lowbox_radar", cat: "mine", script: "（导航收敛新子面板）", global: "loadLowboxRadar", at: "app.js:14450" },
      ];
      if (CFG.quick) {
        /* quick 只枚举 3 个 tab，用它去"找不到 ⇒ 名单失效"会误报；整段跳过并写明未覆盖。 */
        NOTES.push("quick 模式：跳过后置脚本面板回归（A 组通用取样照跑）—— 这一档只有全量才算数");
        report.deep_regression = [];
      } else {
        const REG = [];
        const regMissing = [];
        for (const e of POST_SCRIPT_TABS) {
          if (timedOut()) break;
          const hit = nav.find((x) => x.tab === e.tab);
          const cat = (hit && hit.cat) || e.cat;
          /* 轮询上限 15s：真·缺陷（停在「正在加载数据…」）会**立刻**停在那里，等再久也不会好；
             这里的余量只是给"接口慢但正常"的面板留的（lowbox/bottleneck 这类要现算）。 */
          const r = await coldStart(cat, e.tab, 15000);
          if (!r.ok && await coldEnvSkip(`后置脚本回归 #${e.tab}`)) {
            REG.push({ ...e, ...r, ok: null, env_skip: true });
            continue;
          }
          if (!r.exists) {
            /* 面板压根不存在 = 这条名单已经失效（tab 改名/下线），交给下面那条断言统一报红，
               避免"面板缺失"被拆成一条失败 + 一条名单失效、两处都看着像产品缺陷。 */
            regMissing.push(e.tab);
            REG.push({ ...e, ...r, ok: null, missing: true });
            continue;
          }
          REG.push({ ...e, ...r });
          rec("deep", `冷启动回归 #${r.tab}（依赖后置脚本 ${e.script} → ${e.global}）直接打开就有内容`, r.ok,
            deepDetail(r), DEEP_WHY + `；该面板的渲染入口是 ${e.at} 的 \`if (window.X)\` 静默守卫`, "fail");
        }
        report.deep_regression = REG;
        /* 名单失效必须红：一条永远"找不到所以跳过"的回归清单，等于没有这段测试。 */
        rec("deep", "后置脚本回归名单未失效（每条名单都真能打开出对应面板）", regMissing.length === 0,
          `名单 ${POST_SCRIPT_TABS.length} 条，取不到面板 ${regMissing.length} 条` +
          (regMissing.length ? `：${regMissing.join("、")}（可能已改名/下线，请更新 run.cjs 名单）` : ""),
          "回归清单写死是有意的；但写死的清单若整体失效又没人发现，就会静默地什么都不测", "fail");
        const regRan = REG.filter((x) => x.ok !== null);
        rec("deep", "后置脚本回归确有执行（不许整段变空）", regRan.length > 0,
          `实跑 ${regRan.length}/${POST_SCRIPT_TABS.length} 例` +
          (REG.length > regRan.length ? `（未执行 ${REG.length - regRan.length} 例：环境不可达/名单失效）` : ""),
          "「没跑」和「跑过且通过」在报告里必须长得不一样，否则空白会被读成绿灯", "fail");
      }
      /* 收尾：回到干净首屏，后面的阶段（单位/抽屉）默认在"已经跑热"的页面上做 */
      try { await page.goto(PAGE_URL, { waitUntil: "domcontentloaded", timeout: 30000 }); await sleep(800); await dismissFirstRun(page); } catch (_) { /* ignore */ }
    } else {
      NOTES.push("预算不足：跳过深链接冷启动（这一档不能当已覆盖）");
    }

    phase("unit");
    /* ---------- 5.7 个股详情 + K线 + 单位一致性（P0-1 端到端守门） ----------
     * 事故原型：600519 行情区写「5.99 万手」（对）而 K 线区写「599.27 万手」（错 100 倍）。
     * 断言方式：两个区域都读出「成交量」文本 → 单位必须不同（一个手一个股）→
     *          手×100 与 股 必须一致（相对误差 <1%）。
     * 为什么不写死数字：换数据源/换日期数值就变，写死等于给自己埋定时红。
     * 打开方式走真实用户路径 Ctrl+K 命令面板（不依赖任何 tab 名，也不点具体坐标）。
     */
    if (!timedOut()) {
      CURRENT_TAB = "(stock 600519)";
      const SYM = "600519";
      let opened = { ok: false, how: "" };
      try {
        await page.evaluate(() => { const d = document.querySelector("#drawer"); if (d) d.classList.remove("show"); });
        await page.keyboard.down("Control");
        await page.keyboard.press("k");
        await page.keyboard.up("Control");
        await page.waitForSelector("#cmdInput", { visible: true, timeout: 6000 });
        await page.click("#cmdInput");
        await page.type("#cmdInput", SYM, { delay: 20 });
        await sleep(700);
        await page.keyboard.press("Enter");
        opened = { ok: true, how: "Ctrl+K 命令面板 → 输入 600519 → Enter" };
      } catch (e) {
        /* 兜底：命令面板若是新功能/被改动，直接用 app.js 暴露的同一入口函数打开抽屉。
           记录为 needs_testid，不静默降级。 */
        opened = { ok: false, how: "命令面板不可用: " + e.message.slice(0, 100) };
        NEEDS_TESTID.push("[drawer] Ctrl+K 命令面板路径打不开个股抽屉（" + e.message.slice(0, 80) +
          "）；当前兜底用 window.openStock()。建议保留一条稳定的「按代码打开个股」UI 入口给自动化用。");
        await page.evaluate((s) => { if (typeof window.openStock === "function") window.openStock(s); }, SYM);
        opened.how += " → 已用 window.openStock 兜底";
      }
      /* 2026-11-01：抽屉是否打开，除了 .show 类还应有一个稳定标识；
         有 data-testid 之后就不必再靠 #drawer.show + 结构去猜。 */
      const drawerProbe = await page.evaluate(() => {
        const d = document.querySelector("#drawer");
        return { shown: !!(d && /(^|\s)show(\s|$)/.test(d.className || "")),
          tid: d ? d.getAttribute("data-testid") : null,
          matched: !!document.querySelector('[data-testid="stk-drawer"]') };
      });
      /* testid 就位是**好消息**，走 info 通道（不进分母）。
         只有"该有却没有、退回结构探测"才是不阻断的告警。以前这两种状态都写进 warn 桶，
         于是 warn 数被"好消息"灌水，读者无法从"告警 6 条"反推出任何东西。 */
      if (drawerProbe.matched) {
        info("unit", "个股抽屉 testid 可用", `data-testid=stk-drawer（shown=${drawerProbe.shown}）`);
      } else {
        warn("unit", "个股抽屉带稳定标识 data-testid=stk-drawer", false,
          `shown=${drawerProbe.shown} data-testid=${drawerProbe.tid || "（无）"}`,
          "抽屉是个股详情唯一入口；只靠 #drawer.show 判断，改版把类名一换门禁就静默失效");
        NEEDS_TESTID.push("[drawer] 抽屉没有 data-testid=stk-drawer（openStock 未挂上）—— 已退回 #drawer.show 判断。");
      }

      /* 这条断言的是「抽屉真的开了」——用抽屉自身的可见态判定，而不是"我调用了某个入口函数"。
         旧写法 `opened.ok || true` 恒为真，等于把断言写没了（也是"看起来绿"的一种）。 */
      rec("unit", "个股抽屉可打开（真实 UI 路径）", drawerProbe.shown, opened.how,
        "抽屉都打不开就没法验证单位；路径写进 detail 便于判断是产品回归还是测试手段退化", "fail");

      /* 读两个成交量：结构是 .si-item / .ks-grid>div 里 <span>成交量</span><b>数值</b> */
      const readVol = () => page.evaluate(() => {
        /* 2026-11-01：优先按 testid 取（app.js 给两格挂了 vol-quote / vol-kline）。
           结构探测（.si-item / .ks-grid > div 里找「成交量」文本）只作兜底 ——
           兜底一旦被用上，说明 testid 丢了，会记 needs_testid 而不是静默通过。 */
        const byTid = (tid) => {
          const el = document.querySelector(`[data-testid="${tid}"]`);
          return el ? (((el.querySelector("b") || {}).textContent || "").trim() || null) : null;
        };
        const pick = (rootSel) => {
          const root = document.querySelector(rootSel);
          if (!root) return null;
          const cells = Array.from(root.querySelectorAll(".si-item, .ks-grid > div"));
          for (const c of cells) {
            const k = ((c.querySelector("span") || {}).textContent || "").trim();
            if (k === "成交量") return ((c.querySelector("b") || {}).textContent || "").trim();
          }
          return null;
        };
        const vq = byTid("vol-quote"), vk = byTid("vol-kline");
        return {
          viaTestid: !!(vq && vk),
          quote: vq || pick("#stkQuote"),
          kline: vk || pick("#klineSnap"),
          name: (document.querySelector("#stkName") || {}).textContent || "",
          drawerShown: /(^|\s)show(\s|$)/.test((document.querySelector("#drawer") || {}).className || ""),
          klineSrc: ((document.querySelector("#klineSnap .ks-src") || {}).innerText || "").replace(/\s+/g, " ").slice(0, 160),
          quoteWhen: ((document.querySelector("#stkQuote .si-when") || {}).textContent || "").trim(),
        };
      });

      let vol = { quote: null, kline: null };
      const volDeadline = nowMs() + 21000;   // K 线首次要抓日K，给足；超时就是性能缺陷
      while (nowMs() < volDeadline) {
        vol = await readVol().catch(() => vol);
        if (vol && vol.quote && vol.kline) break;
        await sleep(400);
      }

      /* 文本 → {数值, 单位}；单位只认 手 / 股，"—（单位未标注）" 单独识别。 */
      const parseVol = (t) => {
        if (!t) return { n: null, unit: null, raw: t, reason: "空" };
        if (/单位未标注/.test(t)) return { n: null, unit: "unlabeled", raw: t, reason: "显示「单位未标注」" };
        const m = /([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?\s*(手|股)/.exec(t);
        if (!m) return { n: null, unit: null, raw: t, reason: "无法解析出数值+单位" };
        const mult = m[2] === "亿" ? 1e8 : m[2] === "万" ? 1e4 : 1;
        return { n: parseFloat(m[1]) * mult, unit: m[3] === "手" ? "hand" : "share", raw: t, mult };
      };
      const q = parseVol(vol.quote), k = parseVol(vol.kline);
      /* 600519 日线一定有量：任一为空/未标注，直接判 FAIL（这正是 P0-1 要防的） */
      const bothNum = q.n !== null && k.n !== null;
      const unitDiff = q.unit !== k.unit && q.unit !== "unlabeled" && k.unit !== "unlabeled";
      let relErr = null;
      if (bothNum) {
        const inShares = q.unit === "hand" ? q.n * 100 : q.n;
        const kShares = k.unit === "hand" ? k.n * 100 : k.n;
        const denom = Math.max(Math.abs(inShares), Math.abs(kShares)) || 1;
        relErr = Math.abs(inShares - kShares) / denom;
      }
      const unitOk = bothNum && unitDiff && relErr !== null && relErr < 0.01;
      report.unit = {
        symbol: SYM,
        name: vol.name,
        quote_text: vol.quote, kline_text: vol.kline,
        quote: q, kline: k,
        quote_when: vol.quoteWhen,
        kline_src: vol.klineSrc,
        rel_err: relErr,
        stale_build: pf.staleBuild,
        ok: unitOk,
      };
      const unitDetail = [
        `行情「${vol.quote || "未读到"}」/ K线「${vol.kline || "未读到"}」`,
        bothNum ? `换算后误差 ${(relErr * 100).toFixed(4)}%` : `无法换算（行情:${q.reason || "ok"} / K线:${k.reason || "ok"}）`,
        unitDiff ? "单位标注不同" : "单位标注相同或缺失",
        `数据日=${vol.quoteWhen || "?"}`,
      ].join("；");
      rec("unit", "600519 行情区与K线区成交量单位一致", unitOk, unitDetail,
        "同一只票同一口径的量，两个区域差 100 倍就是测评里的 P0-1；单位标注不同是必须的（一个手一个股），数值换算后必须一致", "fail");
      if (pf.staleBuild && !unitOk) {
        NOTES.push("⚠ 单位一致性 FAIL 且 stale_build=true：服务跑的是旧代码快照（服务 " + pf.version.build_id +
          " / 磁盘 " + pf.diskBuild + "），这条很可能是「没重启」而不是「改坏了」。请重启服务后复跑再定性。");
      }
      if (vol.viaTestid) {
        info("unit", "成交量两格按 testid 定位", `vol-quote=${vol.quote || "空"} / vol-kline=${vol.kline || "空"}`);
      } else {
        warn("unit", "成交量两格可用 testid 定位", false,
          `vol-quote=${vol.quote || "空"} / vol-kline=${vol.kline || "空"} / viaTestid=false`,
          "testid 丢了就只能靠版式结构找「成交量」，改版即静默失守（P0-1 单位一致性的守门项）");
        NEEDS_TESTID.push("[drawer] 成交量两格没有 testid（vol-quote / vol-kline），已退回结构探测" +
          "（行情「" + (vol.quote || "空") + "」K线「" + (vol.kline || "空") + "」）。");
      }
      await page.evaluate(() => { const d = document.querySelector("#drawer"); if (d) d.classList.remove("show"); });
      await page.keyboard.press("Escape");
    }

    phase("watch");
    /* ---------- 5.8 自选增删改（写操作，可 --no-write 跳过） ----------
     * 必须用可回收的临时数据：先读现有池子，挑一个「池里没有」的标的加进去，
     * 断言出现后立刻删掉，断言消失，最后再用 API 复核一次确实不在了。
     * 任一步抛异常都在 finally 里清理 —— 门禁绝不能给用户留下脏数据。 */
    if (CFG.noWrite) {
      NOTES.push("--no-write：跳过自选增删（只读环境）");
      report.watch = { skipped: true, reason: "--no-write" };
      warn("watch", "自选增删（已被 --no-write 跳过）", "skipped",
        "写操作被跳过，故本轮未验证自选面板的增删（只读环境必须能用 --no-write 跑通）");
    } else if (CFG.quick) {
      /* quick 的定义是「改完一次代码就能立刻看结果」：3 个 tab + 单位 + 健康。
         自选写链路要等页面拉完行情（数十秒），放进来会把 quick 拖到 1 分钟以上，
         直接违背 <40s 的目标 —— 所以 quick 不跑，全量必跑。 */
      NOTES.push("quick 模式：跳过自选增删（写链路要等页面行情刷新，全量模式才跑）");
      report.watch = { skipped: true, reason: "quick" };
      warn("watch", "自选增删（quick 模式跳过）", "skipped",
        "quick 不含写操作；请用全量模式验证自选增删，不要把「quick 绿了」当成写链路没问题");
    } else if (!timedOut()) {
      CURRENT_TAB = "(watch)";
      const W = { ok: false, steps: [], symbol: null, id: null, cleaned: null };
      const CANDIDATES = ["000001", "600036", "601398", "000002", "600030", "601888", "600276", "000651"];
      let watchTab = null, pool = [], testSym = null;
      try {
        const before = await httpJson("/api/watch_list", undefined, 30000);
        pool = (before.json && (before.json.items || before.json.data || before.json.bonds)) || [];
        if (!Array.isArray(pool)) pool = [];
        const have = new Set(pool.map((x) => String(x.symbol || x.code || "")));
        testSym = CANDIDATES.find((s) => !have.has(s)) || null;
        if (!testSym) throw new Error("候选临时标的都已在池中，为免污染用户数据不执行增删");
        W.symbol = testSym;

        /* 找「关注池」面板：动态发现 —— 哪个 tab 的面板里有 #wtAdd 就是它（不写死 tab 名） */
        for (const it of nav) {
          await clickSubTab(it).catch(() => {});
          await sleep(260);
          const has = await page.evaluate((t) => !!document.querySelector(`#p-${t} #wtAdd`), it.tab).catch(() => false);
          if (has) { watchTab = it; break; }
        }
        if (!watchTab) throw new Error("在全部 tab 里都没找到含 #wtAdd 的面板（关注池不见了？）");
        W.steps.push("找到关注池面板: " + watchTab.cat + "/" + watchTab.tab);
        pass("watch", "关注池面板可定位（动态发现含 #wtAdd 的 tab）", watchTab.cat + "/" + watchTab.tab);

        await ensureCat(watchTab.cat);
        await page.waitForSelector(`#p-${watchTab.tab} #wtAdd`, { timeout: 8000 });
        await page.click(`#p-${watchTab.tab} #wtAdd`);
        await page.waitForSelector("#wtF-symbol", { visible: true, timeout: 8000 });
        await page.click("#wtF-symbol");
        await page.type("#wtF-symbol", testSym, { delay: 25 });
        await page.click("#wtF-save");
        /* 反馈一：弹窗关掉。这是"点了有反应"的最低标准，单独记一条 ——
           否则「行没出现」会同时混进"没写进去"和"写进去了但列表没刷新"两种原因。 */
        let modalClosed = false;
        for (let i = 0; i < 40; i++) {
          await sleep(250);
          const open = await page.evaluate(() => !!document.querySelector("#wtF-symbol")).catch(() => true);
          if (!open) { modalClosed = true; break; }
        }
        rec("watch", "点「加入」后弹窗关闭（点击有反馈）", modalClosed,
          modalClosed ? "弹窗已移除" : "10s 内弹窗仍在（wtF-save 没走到 overlay.remove()）",
          "弹窗不关＝用户不知道自己点成功没有，会重复点、重复加", "fail");

        /* 反馈二：新行出现在列表里。
           loadWatch() 会重新拉 /api/watch_list（带行情 + 策略命中，池子 30+ 条时很慢），
           实测 18s 不够、45s 够 —— 所以给 45s 并在超时时用接口区分两种原因：
             · 接口里也没有 → 写入链路断了（真 bug）；
             · 接口里有、DOM 没有 → 列表刷新太慢/没刷新（也是体验问题，但性质不同）。
           分开报是为了下一个人不用重新跑一遍才知道该找谁修。 */
        let appeared = false;
        const tRow0 = nowMs();
        while (nowMs() - tRow0 < 45000 && !timedOut()) {
          await sleep(400);
          appeared = await page.evaluate((sym) => !!(
            document.querySelector(`#p-watch tr[data-testid="watch-row"][data-symbol="${sym}"]`) ||
            document.querySelector(`#p-watch [data-wt-open="${sym}"]`)), testSym).catch(() => false);
          if (appeared) break;
        }
        let apiHas = null;
        if (!appeared) {
          try {
            const chk = await httpJson("/api/watch_list", undefined, 30000);
            const arr = (chk.json && (chk.json.items || chk.json.data)) || [];
            apiHas = Array.isArray(arr) && arr.some((x) => String(x.symbol || x.code || "") === testSym);
          } catch (_) { apiHas = null; }
        }
        rec("watch", `加入自选后出现该行（${testSym}）`, appeared,
          appeared ? `行已出现（${((nowMs() - tRow0) / 1000).toFixed(1)}s）`
            : `45s 内 DOM 没有该行；接口侧${apiHas === null ? "无法确认" : (apiHas ? "已写入（列表刷新太慢）" : "也没有（写入失败）")}`,
          "自选池是用户最私有的数据面，增删链路一断就等于「点了没反应」", "fail");
        W.steps.push("add modalClosed=" + modalClosed + " appeared=" + appeared + " apiHas=" + apiHas);

        /* 删除：从行上取 data-wt-del 的 id，真实点击并接受 confirm。
           行没出现时不能"就跳过不测"——那样会漏掉删除链路已断的情况却显示全绿，
           所以照样记一条 fail，并在明细里写清"上游没出现行，UI 删除未验证"。 */
        let removed = false;
        if (appeared) {
          const delId = await page.evaluate((sym) => {
            const tr = document.querySelector(`#p-watch tr[data-testid="watch-row"][data-symbol="${sym}"]`);
            const row = tr || document.querySelector(`#p-watch [data-wt-open="${sym}"]`);
            const holder = row ? row.closest("tr") || row.parentElement : null;
            const btn = holder ? holder.querySelector("[data-wt-del]") : null;
            return btn ? btn.dataset.wtDel : null;
          }, testSym);
          W.id = delId;
          if (delId) {
            const handler = (d) => d.accept().catch(() => {});
            page.on("dialog", handler);
            await page.evaluate((id) => {
              const b = document.querySelector(`#p-watch [data-wt-del="${id}"]`);
              if (b) b.click();
            }, delId);
            for (let i = 0; i < 50; i++) {
              await sleep(300);
              const gone = await page.evaluate((sym) => !(
                document.querySelector(`#p-watch tr[data-testid="watch-row"][data-symbol="${sym}"]`) ||
                document.querySelector(`#p-watch [data-wt-open="${sym}"]`)), testSym);
              if (gone) { removed = true; break; }
            }
            page.off("dialog", handler);
          } else {
            W.steps.push("行上没有 data-wt-del 按钮（找不到删除入口）");
          }
          rec("watch", `删除后该行消失（${testSym}）`, removed, removed ? "行已消失" : "点了删除但行还在",
            "删除不生效＝用户删不掉自己的标的，且会连带影响下一次添加（已在池中）", "fail");
        } else {
          rec("watch", `删除后该行消失（${testSym}）`, false,
            "上游：加入后 45s 内列表没出现该行，UI 删除链路无法验证（兜底用接口清理）",
            "删除不生效＝用户删不掉自己的标的；这里如实记失败，不回退成「跳过」", "fail");
        }
        W.ok = appeared && removed;
      } catch (e) {
        fail("watch", "自选增删链路", false, e.message.slice(0, 200),
          "写链路异常必须暴露，但更关键的是不能留下脏数据（见 finally 清理）");
      } finally {
        /* 兜底清理：不管上面怎么挂的，只要临时标的还可能在池里就删掉，并复核。
           2026-11-01 踩坑记录：写接口需要 local_token Cookie（server.py 13047 行），
           而 Cookie 只在**发 HTML 页面**时下发 —— 早期版本直接裸 POST，收 401
           "token required"，于是"清理"静默失败、真把 000001 留在了用户的池子里。
           现在：① httpJson 会先 GET "/" 接 Cookie；② 逐条检查删除响应，失败原因写进明细；
           ③ 最后一定用接口复核"确实不在池里"，不靠"我以为删了"。 */
        try {
          if (testSym) {
            const after = await httpJson("/api/watch_list", undefined, 30000);
            const arr = Array.isArray(after.json && (after.json.items || after.json.data)) ? (after.json.items || after.json.data) : [];
            let leftover = arr.filter((x) => String(x.symbol || x.code || "") === testSym);
            const delNotes = [];
            for (const it of leftover) {
              const rr = await httpJson("/api/watch_item_remove", { id: it.id }, 20000);
              delNotes.push(`id=${it.id}→HTTP ${rr.status}${(rr.json && rr.json.ok) ? "" : " " + String((rr.json && (rr.json.error || rr.json.hint)) || rr.text || "").slice(0, 60)}`);
            }
            const verify = await httpJson("/api/watch_list", undefined, 30000);
            const arr2 = Array.isArray(verify.json && (verify.json.items || verify.json.data)) ? (verify.json.items || verify.json.data) : [];
            const still = arr2.filter((x) => String(x.symbol || x.code || "") === testSym);
            W.cleaned = still.length === 0;
            rec("watch", "测试数据已清理（临时标的不留在池中）", W.cleaned,
              `临时标的 ${testSym}，残留 ${still.length} 条` + (delNotes.length ? `；删除响应 ${delNotes.join(" ")}` : "（UI 删除已生效，无需兜底）"),
              "门禁往用户真实数据里写脏数据比不测更糟；必须自证清理干净（且要检查删除响应，不能只看'我发过请求'）", "fail");
          }
        } catch (e2) {
          W.cleaned = false;
          fail("watch", "测试数据已清理（临时标的不留在池中）", false, "清理过程异常: " + e2.message.slice(0, 160),
            "清理失败意味着门禁污染了用户数据，必须人工介入");
        }
        report.watch = W;
        await page.evaluate(() => { const d = document.querySelector("#drawer"); if (d) d.classList.remove("show"); });
      }
    }

    phase("drawer");
    /* ---------- 5.9 详情抽屉：四条关闭入口 ----------
     * 为什么必测：打开抽屉是用户最高频动作，四个关闭入口少一个就有一类用户被卡住
     * （只用键盘的人靠 Esc、鼠标党靠叉号、习惯点空白处的人靠"点面板外"、底部按钮是滚动到
     *  长内容末尾时的顺手出口）。少一条，就有人关不掉面板、只能刷新页面。
     *
     * 打开方式：动态找"有可点行"的面板 —— 关注池的行带 data-wt-open（语义属性，不是 tab 名）。
     * 注意一个坑（实测）：面板在 DOM 里一直存在（display:none），所以「#p-watch 有行」不代表
     * 当前可见可点；必须先把该 tab 点成 active，否则 puppeteer 会报 "Node is not clickable"。
     *
     * 关于第四条"再点同一行"：实测语义**不是关闭**，而是"保持打开并重新载入"——
     * 关闭入口 #4 的监听挂在 document 的 pointerdown 上（app.js 7533 行附近），
     * pointerdown 先关、随后的 click 又让行的 onclick 重新打开，净效果=开着。
     * 所以这里断言的是"再点同一行仍然开着且标题有内容"（这是设计行为，也保证点别的行能顺畅切换），
     * 而不是"点了会关"。写清楚是因为把它当关闭入口会是错的。 */
    if (!timedOut() && !CFG.quick) {
      CURRENT_TAB = "(drawer)";
      const D = { opened: false, opener: null, entries: {} };
      try {
        const watchTab = await findTabHaving("#wtAdd");
        if (!watchTab) throw new Error("找不到含 #wtAdd 的面板（关注池不见了？抽屉测试没有可点的行）");
        await clickSubTab(watchTab);
        await sleep(1200);
        /* 选行：优先带 data-wt-open 的行（关注池行）；没有再退化到任何 data-code-open */
        const openerSel = await page.evaluate(() => {
          if (document.querySelector("#p-watch [data-wt-open]")) return "#p-watch [data-wt-open]";
          if (document.querySelector("[data-code-open]")) return "[data-code-open]";
          return null;
        });
        if (!openerSel) throw new Error("关注池里没有任何可点的行（池子是空的？）");
        D.opener = openerSel;

        await page.click(openerSel);
        await sleep(1400);
        D.opened = await drawerOpen();
        const title = await page.evaluate(() => (document.querySelector("#drawerTitle") || {}).textContent || "");
        rec("drawer", "点行可打开详情抽屉且标题非空", D.opened && title.trim().length > 0,
          `opener=${openerSel} open=${D.opened} title="${title.slice(0, 40)}"`,
          "抽屉打不开就别谈后面四个关闭入口；标题为空说明 openDrawer 没拿到数据（tab=undefined 之类）", "fail");

        /* 四个关闭入口：每条都"重新打开 → 关闭 → 断言已关" */
        const reopen = async () => {
          if (!(await drawerOpen())) { await page.click(openerSel); await sleep(1200); }
          return drawerOpen();
        };
        const entries = [
          { key: "✕ 关闭按钮", why: "鼠标党最短路径；#btnClose", run: async () => { await page.click("#btnClose"); } },
          { key: "底部按钮", why: "长内容滚到末尾时的顺手出口；#btnClose2", run: async () => { await page.click("#btnClose2"); } },
          { key: "Esc 键", why: "纯键盘用户唯一入口", run: async () => { await page.keyboard.press("Escape"); } },
          {
            key: "点面板外（遮罩/空白区）", why: "文档级 pointerdown 关闭（app.js:7533 附近）",
            run: async () => {
              /* 点右下角远处：先确认落点不在抽屉矩形里，避免"其实点在抽屉上"的假绿 */
              const box = await page.evaluate(() => {
                const r = document.querySelector("#drawer").getBoundingClientRect();
                const x = Math.min(innerWidth - 6, Math.max(6, r.right + 40));
                const y = Math.min(innerHeight - 6, Math.max(6, r.bottom + 40));
                const outside = x < r.left || x > r.right || y < r.top || y > r.bottom;
                const hit = document.elementFromPoint(x, y);
                const inDrawer = !!(hit && hit.closest && hit.closest("#drawer"));
                return { x, y, outside, inDrawer, hit: hit ? hit.tagName + "#" + (hit.id || "") : null };
              });
              if (!box.outside || box.inDrawer) throw new Error("找不到抽屉外的落点（抽屉几乎占满视口）: " + JSON.stringify(box));
              await page.mouse.click(box.x, box.y);
            },
          },
        ];
        for (const e of entries) {
          const opened = await reopen();
          let clickedOk = true, err = "";
          try { await e.run(); } catch (ex) { clickedOk = false; err = String(ex.message).slice(0, 120); }
          await sleep(500);
          const stillOpen = await drawerOpen();
          D.entries[e.key] = { reopen: opened, clicked: clickedOk, closed: opened && !stillOpen, err };
          rec("drawer", `关闭入口：${e.key}`, opened && clickedOk && !stillOpen,
            `重新打开=${opened} 触发=${clickedOk}${err ? "(" + err + ")" : ""} 关闭后仍打开=${stillOpen}`,
            `${e.why}；少这一条就有一类用户关不掉面板只能刷新页面`, "fail");
        }

        /* 再点同一行：断言"保持打开"（设计行为，见上方注释），并确认标题有内容 */
        const reopened = await reopen();
        await page.click(openerSel);
        await sleep(1200);
        const stillOpen2 = await drawerOpen();
        const title2 = await page.evaluate(() => (document.querySelector("#drawerTitle") || {}).textContent || "");
        D.sameRow = { before: reopened, after: stillOpen2, title: title2.slice(0, 40) };
        rec("drawer", "点同一行 = 保持打开并刷新（不是关闭入口）", reopened && stillOpen2 && title2.trim().length > 0,
          `点前=${reopened} 点后=${stillOpen2} title="${title2.slice(0, 40)}"`,
          "app.js 的关闭入口 #4 挂在 document pointerdown 上：pointerdown 先关、click 又重开，净效果是开着。" +
          "锁住这个语义，才能保证「点别的行切换股票」不会被误关", "fail");

        /* 抽屉正文无硬脏字符（顺带守一下：抽屉是最容易把裸字段糊上去的地方） */
        const dupCheck = await page.evaluate(() => {
          const body = document.querySelector("#drawerBody");
          if (!body) return null;
          const ex = Array.from(body.querySelectorAll("pre, code")).map((e) => e.innerText || "").filter((s) => s.trim());
          let t = body.innerText || "";
          for (const c of ex) t = t.split(c).join(" ");
          return {
            len: t.trim().length,
            hard: (t.match(/\bundefined\b/g) || []).length + (t.match(/\bNaN\b/g) || []).length + (t.match(/Invalid Date/g) || []).length,
          };
        });
        if (dupCheck) {
          rec("drawer", "抽屉正文无 undefined/NaN/Invalid Date", dupCheck.hard === 0,
            `正文 ${dupCheck.len} 字，硬脏字符 ${dupCheck.hard} 处`,
            "抽屉里字段最多、最容易把裸值糊给用户看", "fail");
        }
      } catch (e) {
        fail("drawer", "详情抽屉四条关闭入口", false, String(e.message).slice(0, 200),
          "打不开抽屉就无法验证关闭入口；这条失败要么是关注池/行选择器漂移，要么是抽屉本身坏了");
      } finally {
        /* 收尾：确保抽屉关掉，别影响后续视口/长文本阶段 */
        try {
          await page.keyboard.press("Escape");
          await page.evaluate(() => { const d = document.querySelector("#drawer"); if (d) d.classList.remove("show"); });
        } catch (_) { /* ignore */ }
        report.drawer = D;
      }
    } else if (CFG.quick) {
      NOTES.push("quick 模式：跳过抽屉四条关闭入口（要开关注池并等行情，全量才跑）");
    }

    phase("states");
    /* ---------- 5.10 空态 / 错误态（6 种载荷变体） ----------
     * 用请求拦截在"测试页"里构造，不动服务端。为什么必须做：本仓库反复踩的坑就是
     * 「接口超时/返回空/返回 500 时前端是不是白板」—— 数据正常到达的路径永远测不出这个问题。
     *
     * 6 种变体覆盖的是"后端能怎么烂"的全部基本形态：
     *   500+JSON / 500+HTML / 网络层失败(超时) / 200+{} / 200+[] / 200+null
     * 后三种最容易把 `undefined` 直接渲染到页面上（字段缺失），所以它们既是"能不能活"的检查，
     * 也是脏字符检查的最强触发场景。
     *
     * 断言口径（先实测确认"正确实现能过"才写进来，避免又造一个假红）：
     *   · 非白屏：正文长度 > 0；
     *   · 抽查面板非空：前 3 个 tab 的面板 innerText 长度 > 0（不是空 div）；
     *   · 有中文人话（空态/错误/加载/重试任一）；
     *   · 5xx 与网络失败必须出现"错误/重试"类文案（实测：底部有「服务端 500，2 秒后自动重试」）；
     *   · 无未捕获 pageerror、无 undefined/NaN/Invalid Date/[object Object]。
     * 注意：不断言"面板里必须是错误文案"——实测前端的设计是「底部错误条 + 面板保持加载中并自动重试」，
     * 那是合理 UX，硬要求面板换成错误文案会误伤。 */
    if (!timedOut() && !CFG.quick) {
      CURRENT_TAB = "(states)";
      const CASES = [
        { name: "接口 500 + JSON {error}", kind: "500json", needErrCopy: true },
        { name: "接口 500 + HTML 错误页", kind: "500html", needErrCopy: true },
        { name: "接口网络层失败（等价超时/断网）", kind: "timedout", needErrCopy: true },
        { name: "接口 200 + {} 空对象（字段全缺）", kind: "emptyobj", needErrCopy: false },
        { name: "接口 200 + [] 空数组（类型不符）", kind: "emptyarr", needErrCopy: false },
        { name: "接口 200 + null", kind: "nullbody", needErrCopy: false },
      ];
      for (const cs of CASES) {
        /* 开工前先确认服务还活着：跑全量时别人重启 viewer，这里会撞上
           "文档层 ERR_CONNECTION_REFUSED" —— 那样页面本身就是浏览器的错误页，
           后面所有"面板有没有内容 / 有没有错误文案"的断言都不成立。
           此时把这一例登记为"没执行"，绝不算通过。 */
        const reach = await httpJson("/api/version", undefined, 5000).catch(() => null);
        if (!reach || reach.status !== 200) {
          envSkip(`空态/错误态「${cs.name}」`, "开新页前服务不可达（" +
            (reach ? "HTTP " + reach.status : "连接失败") + "），本例未执行");
          report.states.push({ name: cs.name, kind: cs.kind, ok: null, env_skip: true });
          continue;
        }
        const mp = await browser.newPage();
        await mp.setViewport({ width: 1440, height: 1000 });
        const mErrs = [];
        mp.on("pageerror", (e) => mErrs.push(String(e && e.message || e).slice(0, 200)));
        mp.on("console", (m) => { if (m.type() === "error" && !isNoise(m.text())) mErrs.push("console:" + m.text().slice(0, 200)); });
        await mp.setRequestInterception(true);
        mp.on("request", (req) => {
          const u = req.url();
          if (u.indexOf("/api/") < 0) return req.continue();
          const json = (body) => req.respond({ status: 200, contentType: "application/json; charset=utf-8", body });
          try {
            if (cs.kind === "500json") return req.respond({ status: 500, contentType: "application/json; charset=utf-8", body: JSON.stringify({ error: "boom" }) });
            if (cs.kind === "500html") return req.respond({ status: 500, contentType: "text/html; charset=utf-8", body: "<html><body>Internal Server Error</body></html>" });
            if (cs.kind === "timedout") return req.abort("timedout");   // 浏览器侧等价 net::ERR_TIMED_OUT
            if (cs.kind === "emptyobj") return json("{}");
            if (cs.kind === "emptyarr") return json("[]");
            return json("null");
          } catch (_) { return req.continue(); }
        });
        let navErr = null;
        try { await mp.goto(PAGE_URL, { waitUntil: "networkidle2", timeout: 35000 }); } catch (e) { navErr = e.message; }
        /* 服务在"刚探活成功"和"打开页面"之间被重启了：文档层连接失败 → 浏览器错误页。
           这不是产品缺陷，登记"未执行"并把页面关掉，别用错误页的文案去判产品。 */
        if (navErr && /ERR_CONNECTION_REFUSED|ERR_EMPTY_RESPONSE|ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED/.test(navErr)) {
          envSkip(`空态/错误态「${cs.name}」`, "打开页面时服务掉线：" + navErr.slice(0, 100));
          report.states.push({ name: cs.name, kind: cs.kind, ok: null, env_skip: true });
          await mp.close().catch(() => {});
          continue;
        }
        await sleep(1300);
        await dismissFirstRun(mp);
        await sleep(300);

        /* 抽查前 3 个 tab：点开看面板是不是空 div。
           注意面板元素一直存在于 DOM（只是 display:none），所以必须"点开再读"，不能直接读。 */
        const sampleTabs = await mp
          .evaluate(() => Array.from(document.querySelectorAll("#subnav .sub-tab[data-tab]")).slice(0, 3).map((a) => a.dataset.tab))
          .catch(() => []);
        const perTab = [];
        for (const t of sampleTabs) {
          await mp.evaluate((tab) => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${tab}"]`); if (a) a.click(); }, t).catch(() => {});
          await sleep(650);
          /* 与主导航同一口径：iframe 面板读子文档，否则父文档 innerText 永远是 0（会把好面板判成空） */
          perTab.push(await mp.evaluate(
            `(function(){ var r = (${panelTextInPage.toString()})(${JSON.stringify("#p-")} + ${JSON.stringify(t)});
               return { tab: ${JSON.stringify(t)}, exists: r.exists, len: r.len, head: r.head.slice(0, 50) }; })()`
          ).catch(() => ({ tab: t, exists: false, len: 0, head: "" })));
        }

        const st = await mp.evaluate(() => {
          /* 与主页面同一套脏字符口径：排除 pre/code，用词边界 */
          const ex = Array.from(document.querySelectorAll("pre, code, script, style, textarea")).map((e) => e.innerText || "").filter((s) => s.trim());
          let txt = document.body.innerText || "";
          for (const c of ex) txt = txt.split(c).join(" ");
          const flat = txt.replace(/\s+/g, " ").trim();
          return {
            bodyLen: flat.length,
            hasCn: /暂无|无数据|没有|尚未|未检出|无记录|失败|错误|出错|异常|无法|请稍后|重试|加载/.test(flat),
            hasErrCopy: /失败|错误|出错|异常|无法|请稍后|重试|\b5\d\d\b|服务端/.test(flat),
            errHead: (flat.match(/.{0,24}(?:失败|错误|重试|异常|无法|服务端).{0,40}/) || [""])[0].slice(0, 80),
            hard: {
              undefined: (flat.match(/\bundefined\b/g) || []).length,
              NaN: (flat.match(/\bNaN\b/g) || []).length,
              invalidDate: (flat.match(/Invalid Date/g) || []).length,
              objObj: (flat.match(/\[object Object\]/g) || []).length,
            },
            head: flat.slice(0, 90),
          };
        });
        const emptyPanels = perTab.filter((x) => !x.exists || x.len === 0).map((x) => x.tab);
        const hardSum = Object.values(st.hard).reduce((a, n) => a + n, 0);
        const ok = st.bodyLen > 0 && emptyPanels.length === 0 && st.hasCn && hardSum === 0 && mErrs.length === 0 && !navErr &&
          (!cs.needErrCopy || st.hasErrCopy);
        rec("states", cs.name, ok,
          `正文 ${st.bodyLen} 字 | 中文=${st.hasCn} 错误文案=${st.hasErrCopy}「${st.errHead}」| 空面板=${JSON.stringify(emptyPanels)} | 脏字符=${JSON.stringify(st.hard)} | JS异常 ${mErrs.length} | gotoErr=${navErr ? "有" : "无"} | 首屏「${st.head}」` +
          (perTab.length ? ` | 面板字数=${perTab.map((x) => x.tab + ":" + x.len).join(",")}` : ""),
          "后端超时/空/500/类型不符时前端不能白板、不能把 undefined 渲染出来；用拦截构造才能与真实数据解耦（数据正常时永远测不到这条路径）", "fail");
        report.states.push({ name: cs.name, kind: cs.kind, ok, st, panels: perTab, errs: mErrs.slice(0, 5) });
        await mp.close();
      }
    } else if (CFG.quick) {
      NOTES.push("quick 模式：跳过空态/错误态 6 种变体（每例要开新页面，全量才跑）");
    }


    phase("longtext");
    /* ---------- 5.11 长文本压力（超长中文 / 超长无空格 ASCII） ----------
     * 为什么这么测：真实数据里"长"是常态而不是边界 —— 帖子标题、公司全称、备注、理由都会很长。
     * 注入到**当前面板里文本最长的那些叶子元素**（也就是长文本真实落点），而不是随便挑几个 div：
     * 否则测的是装饰元素，等于没测。注入后必须还原，不能污染后续断言。
     *
     * 两档含义不同，严重度也不同（实测依据见 README）：
     *   · 超长中文（可换行的真实输入）→ 断言行不溢出，硬失败；
     *   · 超长无空格 ASCII（300 字符连写，极限输入）→ 抽查的 tab 全部把页面撑到 2381~3892px，
     *     说明这些文本节点没有 word-break。它是真问题，但属于"全站 CSS 补齐"的活、不是本门禁这一轮
     *     能修的范围，所以记 **warn** 并把具体宽度报出来，由负责人决定是否提升为硬失败。
     */
    if (!timedOut()) {
      CURRENT_TAB = "(longtext)";
      const LT = { tabs: [] };
      const ltTabs = (CFG.quick ? navList.slice(0, 1) : nav.filter((x) => !x.folded).slice(0, 3));
      for (const it of ltTabs) {
        if (timedOut()) break;
        CURRENT_TAB = "(longtext " + it.cat + "/" + it.tab + ")";
        try {
          await clickSubTab(it);
          await sleep(500);
          const r = await page.evaluate((tab) => {
            const panel = document.querySelector("#p-" + tab);
            if (!panel) return { tab, err: "无面板" };
            /* iframe 面板不能这么注入：往里写 textContent 等于把 <iframe> 自己替换掉，
               测的就不是布局而是"我有没有把别人的面板搞坏"。跳过并如实记 warn。 */
            if (panel.querySelector("iframe")) return { tab, skip: "iframe" };
            const width = () => document.documentElement.scrollWidth;
            const before = width();
            const LONG_CJK = "这是一段极其冗长的中文文本用于测试布局在极端文本长度下是否会被撑破换行".repeat(5);
            const LONG_ASCII = "A".repeat(300);
            /* 目标：叶子元素里当前文本最长的前 5 个（长文本真实落点） */
            const cands = Array.from(panel.querySelectorAll("*"))
              .filter((e) => e.children.length === 0 && (e.innerText || "").trim().length > 0)
              .sort((a, b) => (b.innerText || "").length - (a.innerText || "").length)
              .slice(0, 5);
            const orig = cands.map((e) => e.textContent);
            cands.forEach((e) => { e.textContent = LONG_CJK; });
            const afterCJK = width();
            cands.forEach((e) => { e.textContent = LONG_ASCII; });
            const afterASCII = width();
            cands.forEach((e, i) => { e.textContent = orig[i]; });
            const restored = width();
            return { tab, n: cands.length, before, afterCJK, afterASCII, restored, innerW: innerWidth };
          }, it.tab);
          LT.tabs.push(r);
          if (r.err) {
            fail("longtext", `[${it.cat}/${it.tab}] 注入长文本`, r.err, "找不到面板说明渲染还没就绪；这项等于没测", "fail");
            continue;
          }
          if (r.skip === "iframe") {
            warn("longtext", `[${it.cat}/${it.tab}] 跳过（面板是 iframe）`, "注入会替换掉 iframe 本身，测不出布局问题",
              "跨文档的长文本压力要在子文档里做；本门禁不越界改别人的页面，如实记跳过而不是记通过");
            continue;
          }
          /* 前置条件：真的注入到了东西。注入 0 个还报"通过"就是典型的"测了个空"。 */
          rec("longtext", `[${it.cat}/${it.tab}] 找到可注入的长文本落点`, r.n > 0, `注入 ${r.n} 个叶子元素`,
            "如果目标选择器漂移到找不到元素，这条断言会先红，避免下面的'无溢出'是假绿", "fail");
          if (r.n > 0) {
            rec("longtext", `[${it.cat}/${it.tab}] 超长中文不产生横向溢出`, r.afterCJK <= r.innerW + 1,
              `scrollWidth ${r.before}→${r.afterCJK}（视口 ${r.innerW}）`,
              "帖子标题/公司全称/备注都是长中文，换行失败就会把整页撑出横向滚动条", "fail");
            if (r.afterASCII > r.innerW + 1) {
              warn("longtext", `[${it.cat}/${it.tab}] 超长无空格 ASCII 撑出横向溢出（极限输入）`,
                `scrollWidth ${r.before}→${r.afterASCII}（视口 ${r.innerW}，超出 ${r.afterASCII - r.innerW}px）`,
                "这些文本节点没有 word-break；300 字符连写的 ASCII（如长 URL 粘进备注）会让整页横向滚动。" +
                "本轮抽查的 3 个 tab 全部复现（1440 视口撑到 2381~3892px），属全站 CSS 补齐项，故记 warn 而非 fail —— 要升级为硬失败改一个词即可");
            } else {
              pass("longtext", `[${it.cat}/${it.tab}] 超长无空格 ASCII 也不溢出`, `scrollWidth ${r.afterASCII}`);
            }
            rec("longtext", `[${it.cat}/${it.tab}] 注入后已还原（不污染后续断言）`, r.restored <= r.innerW + 1,
              `还原后 scrollWidth=${r.restored}`, "测完必须还原 DOM，否则后面的视口检查会读到被自己撑坏的状态", "fail");
          }
        } catch (e) {
          fail("longtext", `[${it.cat}/${it.tab}] 长文本压力`, String(e.message).slice(0, 160),
            "注入过程中抛异常说明面板结构超出预期，需要人工看一眼");
        }
      }
      report.longtext = LT;
    }

    phase("viewport");
    /* ---------- 5.12 三档视口 ----------
     * 1024 / 1440 / 1920：查横向溢出、可点元素是否被裁、顶栏按钮能否被 elementFromPoint 命中。
     * 按钮清单动态枚举（不写死 id）：顶栏里有 hidden 的按钮（如 #btnDemo）写死就会假红。
     * 效率取舍：不是"每个视口把全部 tab 点一遍"（3×53 次切换 ≈ 130s），而是
     * 「每个 tab 点一次，就地切三档量三次」—— 覆盖完全一样，切换次数降到 1/3。
     * 三档常量 VPS 在模块作用域（见文件头声明处注释：写在这里曾被编辑挤掉、吞了整个阶段）。 */
    const vpTabs = CFG.quick ? [navList[0]].filter(Boolean) : nav;
    /* 每个 tab 只切换一次，就地量三档视口 —— 覆盖不变，切换次数从 159 降到 53。 */
    const perVp = VPS.map((vp) => ({ w: vp.w, h: vp.h, overflow_tabs: [], clipped: [] }));
    for (const it of vpTabs) {
      if (timedOut()) break;
      CURRENT_TAB = `(viewport) ${it.cat}/${it.tab}`;
      await clickSubTab(it).catch(() => {});
      await sleep(CFG.quick ? 300 : 360);
      for (let vi = 0; vi < VPS.length; vi++) {
        const vp = VPS[vi];
        await page.setViewport({ width: vp.w, height: vp.h });
        await sleep(160);
        const m = await page.evaluate((tab) => {
          const doc = document.documentElement;
          const overflow = doc.scrollWidth - innerWidth;
          const panel = document.querySelector("#p-" + tab);
          /* 只挑「可点的东西」判断被裁：按钮/链接/输入框。纯装饰元素允许超出（常见于背景层） */
          const clickables = panel ? Array.from(panel.querySelectorAll("button, a[href], input, select")) : [];
          const inScroller = (el) => {
            let n = el.parentElement;
            while (n && n !== document.body) {
              const cs = getComputedStyle(n);
              if (/(auto|scroll)/.test(cs.overflowX)) return true;
              n = n.parentElement;
            }
            return false;
          };
          const out = clickables
            .filter((el) => {
              const r = el.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) return false;      // 隐藏元素不算
              return (r.right > innerWidth + 1 || r.left < -1) && !inScroller(el);
            })
            .map((el) => (el.innerText || el.value || el.getAttribute("aria-label") || el.tagName).trim().slice(0, 24));
          return { overflow, out };
        }, it.tab).catch(() => ({ overflow: 0, out: [] }));
        if (m.overflow > 1) perVp[vi].overflow_tabs.push(it.cat + "/" + it.tab + "(" + m.overflow + "px)");
        if (m.out.length) perVp[vi].clipped.push(it.cat + "/" + it.tab + ":" + m.out.slice(0, 3).join(","));
      }
    }

    /* 顶栏按钮命中测试：每档视口各测一次（按钮清单动态枚举，不写死 id —— 顶栏里有 hidden 按钮） */
    for (let vi = 0; vi < VPS.length; vi++) {
      const vp = VPS[vi];
      if (timedOut()) break;
      CURRENT_TAB = `(viewport ${vp.w} topbar)`;
      await page.setViewport({ width: vp.w, height: vp.h });
      await sleep(320);
      const top = await page.evaluate(() => {
        const bar = document.querySelector(".topbar");
        if (!bar) return { exists: false, btns: [] };
        const btns = Array.from(bar.querySelectorAll("button, .btn, a[href]")).filter((b) => {
          const r = b.getBoundingClientRect();
          const cs = getComputedStyle(b);
          return r.width > 4 && r.height > 4 && cs.visibility !== "hidden" && cs.display !== "none" && !b.hidden;
        });
        const res = btns.map((b, i) => {
          const r = b.getBoundingClientRect();
          const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          const hitOk = !!el && (el === b || b.contains(el) || (el.closest && el.closest("button,a,.btn") === b));
          return {
            name: (b.innerText || b.getAttribute("aria-label") || b.id || "btn#" + i).trim().slice(0, 18),
            hitOk,
            rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          };
        });
        return { exists: true, btns: res };
      });

      const overflowTabs = perVp[vi].overflow_tabs, clipped = perVp[vi].clipped;
      const overflowOk = overflowTabs.length === 0;
      rec("viewport", `[${vp.w}x${vp.h}] 无横向溢出`, overflowOk,
        overflowOk ? `${vpTabs.length} 个 tab 均 scrollWidth<=innerWidth+1` : `溢出: ${overflowTabs.slice(0, 6).join(" ")}`,
        "横向溢出是移动/窄屏最直观的破相，且会连带顶栏按钮被推出屏幕点不到", "fail");
      const clippedOk = clipped.length === 0;
      rec("viewport", `[${vp.w}x${vp.h}] 可点元素未被裁到视口外`, clippedOk,
        clippedOk ? "无" : clipped.slice(0, 6).join(" | "),
        "按钮在视口外＝用户永远点不到（不是样式小瑕疵）", "fail");
      if (top.exists) {
        const bad = top.btns.filter((b) => !b.hitOk);
        rec("viewport", `[${vp.w}x${vp.h}] 顶栏按钮 elementFromPoint 可命中（${top.btns.length} 个）`,
          bad.length === 0, bad.length ? bad.map((b) => `${b.name}@${b.rect}`).join(" ") : "全部可命中",
          "elementFromPoint 命中是最接近真实鼠标的判据；被弹层/遮挡盖住的按钮点下去不会触发", "fail");
      } else {
        warn("viewport", `[${vp.w}x${vp.h}] 找到顶栏容器`, "未找到 .topbar",
          "顶栏容器 class 变了会让遮挡类缺陷无法被检出");
      }
      report.viewports.push({ w: vp.w, h: vp.h, overflow_tabs: overflowTabs, clipped, topbar: top });
    }
    /* 视口阶段一条都没落地（超预算 / 异常早退）时必须显式红：把"没跑"当"通过"，
       是门禁里最危险的一种谎话 —— 上一轮就是它让 3×3 条视口断言凭空消失，
       而摘要里只看到"少了几条"。 */
    if (!report.viewports.length) {
      fail("viewport", "三档视口检查已执行", false,
        `0 档完成（已用 ${Math.round((nowMs() - T0) / 1000)}s，卡在 ${CURRENT_TAB}）`,
        "把「没跑」当「通过」是门禁最危险的谎话；要么跑，要么红");
    }
    await page.setViewport({ width: 1440, height: 1000 });

  } catch (e) {
    /* 跑到这里说明是门禁自身/页面级异常（比如导航到一半浏览器崩了）。
       这仍然可能是产品把浏览器搞崩，所以记 fail 而不是 exit 2，但把堆栈留全。 */
    fail("runner", "门禁整体执行", false, `@${CURRENT_TAB}: ${String(e && e.stack || e).slice(0, 600)}`,
      "运行时异常必须留堆栈，否则下一次没人知道死在哪");
  } finally {
    phase("summary");
    /* ---------- 5.11 汇总 ---------- */
    /* 先复核「服务是不是在我们跑的过程中被重启了」。
       实测踩过：全量跑到第 3 分钟时有人重启了 viewer（pid 5852 → 17064），
       于是 151 条 /api/ 请求报 net::ERR_CONNECTION_REFUSED、viewport 阶段从 120s 涨到 163s。
       那不是产品缺陷，是"环境在脚下换了地板"。不区分的话，门禁会把同事的一次重启
       记成"接口大面积不可用"，又一次变成红了没人看。
       判据用 pid 变化（比 build_id 更能反映"进程真的换了"）。 */
    let restartedMidRun = null;
    try {
      const after = await httpJson("/api/version", undefined, 6000);
      const pid0 = pf.version && pf.version.pid, pid1 = after.json && after.json.pid;
      if (after.json && pid0 && pid1 && pid0 !== pid1) {
        restartedMidRun = {
          pid_before: pid0, pid_after: pid1,
          build_id_before: pf.version.build_id, build_id_after: after.json.build_id,
          started_at_after: after.json.started_at,
        };
        NOTES.push(`⚠ 服务在本次运行期间被重启（pid ${pid0} → ${pid1}，新 build_id=${after.json.build_id}）。` +
          "期间出现的接口连接失败属于重启窗口，不是产品缺陷；请复跑一次再定性。");
      }
      report.env.restarted_mid_run = restartedMidRun;
      report.env.server_build_id_after = after.json ? after.json.build_id : null;
    } catch (e) { /* 服务在跑完后不可达：不额外判失败，前面已有相关断言 */ }

    report.api.failures = report.api.failures.concat(apiFails.map((f) => ({ ...f })));
    /* 去重（同一 URL+status 多次出现只留一条，附次数） */
    const seen = new Map();
    for (const f of report.api.failures) {
      const k = f.status + " " + f.url;
      if (seen.has(k)) seen.get(k).count = (seen.get(k).count || 1) + 1;
      else seen.set(k, { ...f, count: 1 });
    }
    report.api.failures = Array.from(seen.values());
    report.api.net_failures = apiNetFails;
    report.api.filtered_console = consoleFiltered;
    report.api.console_errors = consoleErrs.slice(0, 40);
    report.api.page_errors = pageErrs.slice(0, 40);

    const apiFailCount = report.api.failures.length + apiNetFails.length;
    const apiDetail = apiFailCount
      ? report.api.failures.slice(0, 8).map((f) => `${f.status} ${f.url} @${f.tab}${f.count > 1 ? " x" + f.count : ""}`).join(" | ") +
        (apiNetFails.length ? ` ／ 网络失败 ${apiNetFails.length} 条（如 ${apiNetFails[0].url} ${apiNetFails[0].why}）` : "")
      : "0 条（304 不计）";
    rec("api", "全量导航期间无 /api/ 4xx/5xx", apiFailCount === 0, apiDetail,
      "接口 4xx/5xx 是用户能直接看到的空面板/报错；只在 /api/ 前缀上计数，避免把静态资源 404 混进来",
      /* 服务在运行期间被重启时，连接失败属于重启窗口 —— 降级为 warn，并要求复跑，
         否则会把"同事重启了一次服务"误判成"接口全挂"。 */
      restartedMidRun ? "warn" : "fail",
      /* 聚合信息：这条把 N 次网络失败合并成 1 条断言。不写清 N，下一次没人知道
         "1 条告警"是 1 次还是 200 次（这正是摘要读不出来的老问题）。 */
      {
        agg: {
          n_failures: apiNetFails.length,
          n_http: report.api.failures.length,
          what: restartedMidRun ? "重启窗口内的网络失败" : "网络失败（非重启窗口，需人工看一眼）",
          window: restartedMidRun ? `pid ${restartedMidRun.pid_before}→${restartedMidRun.pid_after}` : null,
        },
      });
    rec("console", "无未捕获 pageerror", pageErrs.length === 0,
      pageErrs.length ? pageErrs.slice(0, 5).map((e) => e.tab + ": " + e.text).join(" | ") : "0 条",
      "pageerror = JS 崩了，通常紧接着就是整块面板不工作", "fail");
    if (consoleErrs.length) {
      warn("console", "console.error 数量", `${consoleErrs.length} 条: ` + consoleErrs.slice(0, 4).map((e) => e.tab + ": " + e.text).join(" | "),
        "console.error 常是前端自己报的降级信息，硬失败会让门禁常红；单独列出供人判断");
    }
    if (consoleFiltered.length) {
      NOTES.push(`已过滤 ${consoleFiltered.length} 条噪音 console.error（CSP meta 警告 / 与接口 4xx 重复的 Failed to load resource）`);
    }

    const elapsed = nowMs() - T0;
    const budgetOk = elapsed <= BUDGET_MS;
    /* 服务刚重启（缓存全冷）时会自然慢一截 —— 那种慢不是门禁变差，也不是产品变差。
       判据：进程启动时间距今 < 3 分钟，就把"超预算"降级为 warn 并注明，避免假红。 */
    const startedAt = pf.version && pf.version.started_at ? Date.parse(String(pf.version.started_at).replace(" ", "T")) : NaN;
    const coldService = !isNaN(startedAt) && (nowMs() - startedAt) < 180 * 1000;
    rec("perf", `${CFG.quick ? "quick" : "全量"} 在预算内完成（${Math.round(BUDGET_MS / 1000)}s）`, budgetOk,
      `实际 ${(elapsed / 1000).toFixed(1)}s` + (budgetOk ? "" : coldService ? "（服务刚重启，缓存全冷，见 warn）" : ""),
      "门禁太慢就没人跑；超预算本身要亮出来（卡住/超时也在这里体现）",
      budgetOk ? "info" : (coldService ? "warn" : "fail"));

    /* 未覆盖清单：旧版有、新版没做 —— 写出来，避免「假装覆盖了」 */
    NOT_COVERED.push(
      "四种主题（light-tech / dark-dashboard / minimal-report / card-warm）的文字-背景对比度检测：新版未做。",
      "抽屉拖拽 / 右下角拉伸 / 拉到极限后手柄仍在视口内：新版未做（旧版 3 种抽屉 × 6 条断言）。" +
        "（抽屉的「四条关闭入口」本轮已补：✕ / 底部按钮 / Esc / 点面板外）",
      "超长无空格 ASCII 的横向溢出目前只记 warn（本轮抽查的 3 个 tab 全部复现，撑到 2381~3892px，" +
        "属全站 CSS word-break 补齐项）；超长中文的溢出已是硬断言。",
      "细粒度「缺字段兜底」（summary 缺 counts / topics 条目缺 weight / persons 缺字段 / sentiment 返回数组）：" +
        "本轮只做 6 种整体载荷变体（500+JSON、500+HTML、网络失败、{}、[]、null），单接口/单字段级别的缺字段未逐个覆盖。",
      "10 个旧 tab 的「元素数量与基线一致（未消失）」：新版只断言面板有内容，不比元素计数基线。",
      "抽屉横向不遮挡所点行：新版未做。",
      "主题切换后元素不消失/面板高度一致：新版未做。"
    );

    /* ------------------------------------------------------------------
     * 摘要必须自洽，而且 `level` 不能是垃圾字段。
     *
     * 踩过的坑（第三次才修对）：
     *   ① `level` 以前由调用方随手写，出现过 102 条 `level="fail"` 里 102 条都是 ok=true ——
     *      **一个字段有值、但值是垃圾，比 null 更危险**（health.leases 同一个病）。
     *   ② `summary` 按 `level` 计数而不是按 `ok`，于是"通过但被标成 warn"的记录同时进两个桶，
     *      出现过 107 + 6 = 113 ≠ 111。
     * 现在：
     *   · `level` 一律由 `ok` + 阻断意图推导（rec 里做，见文件头），三值只有 pass/warn/fail；
     *   · 自检 ① 扫 RESULTS：**ok=true 却 level≠pass** 的行数必须为 0（钉死垃圾 level）；
     *   · 自检 ② 算术：passed + warned + failed === checks_total，不成立就红；
     *   · 自检 ③ `--selftest-harness` 会故意注入一条 ok=true/level=warn 的坏行，
     *      门禁必须因此变红（证明自检 ① 真的有效，而不是写了个永远为真的判断）。
     * ------------------------------------------------------------------ */
    if (CFG.selftestHarness) {
      /* 故意绕过 rec() 注入坏行：模拟"某个新增断言把 level 写错" */
      RESULTS.push({ group: "harness", id: "（自测注入）ok=true 但 level=warn", ok: true,
        detail: "自测用：这条不该存在", why: "", level: "warn", intent: "warn" });
      NOTES.push("⚠ 运行在 --selftest-harness 模式下：已注入一条 ok=true/level=warn 的坏行，本轮**必须**红");
    }
    const levelBad = RESULTS.filter((r) => (r.ok && r.level !== "pass") || (!r.ok && r.level !== "warn" && r.level !== "fail"));
    const levelOk = levelBad.length === 0 && LEVEL_BAD.length === 0;
    rec("harness", "level 三值自洽（ok=true 必须是 pass，ok=false 只能是 warn/fail）", levelOk,
      levelBad.length === 0 && LEVEL_BAD.length === 0
        ? `0 条违规（pass=${RESULTS.filter((r) => r.level === "pass").length} warn=${RESULTS.filter((r) => r.level === "warn").length} fail=${RESULTS.filter((r) => r.level === "fail").length}）`
        : `违规 ${levelBad.length + LEVEL_BAD.length} 条: ` +
          levelBad.slice(0, 5).map((r) => `${r.group}/${r.id}(ok=${r.ok},level=${r.level})`)
            .concat(LEVEL_BAD.slice(0, 3).map((x) => x.id + "(" + x.why + ")")).join("; "),
      "level 有值但值是垃圾比 null 更危险：它会让按 level 统计的摘要静默算错", "fail");

    const tally = (rows) => {
      const passed = rows.filter((r) => r.ok);
      const warned = rows.filter((r) => !r.ok && r.level === "warn");
      const failed = rows.filter((r) => !r.ok && r.level === "fail");
      return { passed, warned, failed, sum: passed.length + warned.length + failed.length, total: rows.length };
    };
    const pre = tally(RESULTS);
    const sumOk = pre.sum === pre.total;
    /* 先记自检这一条（它本身也是一条断言），再对全量结果做最终分桶 —— 分母含它、算术仍自洽 */
    rec("harness", "摘要算术自洽（通过+告警+失败 === 总数）", sumOk,
      `${pre.passed.length}+${pre.warned.length}+${pre.failed.length} = ${pre.sum} vs 总数 ${pre.total}`,
      "加不起来的摘要没法被人核对；数字自洽是这份门禁能被信任的最低条件", "fail");
    const T = tally(RESULTS);
    const hardFails = T.failed;
    /* 告警再分成两层，打印时分开列（两层都在分母里，加起来仍然等于总数）：
       · 断言级：某条具体断言没过（例如长文本 ASCII 溢出）
       · 阶段级：这一轮的环境/预算状态（stale_build、超预算、导航二次枚举不一致、中途重启…） */
    const PHASE_LEVEL_IDS = /服务与磁盘代码一致|在预算内完成|导航枚举可复现|中途不可用|重启窗口|重启后|复刻/;
    const phaseWarns = T.warned.filter((r) => PHASE_LEVEL_IDS.test(r.id));
    const assertWarns = T.warned.filter((r) => phaseWarns.indexOf(r) < 0);
    /* 聚合类：把 N 次同类失败合并成 1 条断言的那些（目前只有"网络失败"）。
       只展示**真的聚合了东西**的（n>0）；0 次就别列，否则摘要里全是"0 次 → 1 条"的噪音。 */
    const aggs = RESULTS.filter((r) => r.agg && ((r.agg.n_failures || 0) + (r.agg.n_http || 0)) > 0).map((r) => ({
      id: r.id, what: r.agg.what, n: r.agg.n_failures, n_http: r.agg.n_http, window: r.agg.window,
      level: r.level, ok: r.ok,
    }));

    report.summary = {
      checks_total: T.total,
      passed: T.passed.length,
      failed: T.failed.length,
      warned: T.warned.length,
      info_total: INFO.length,
      algebra: {
        passed: T.passed.length, warned: T.warned.length, failed: T.failed.length,
        sum: T.sum, total: T.total, ok: sumOk,
      },
      level_ok: levelOk,
      level_counts: {
        pass: RESULTS.filter((r) => r.level === "pass").length,
        warn: RESULTS.filter((r) => r.level === "warn").length,
        fail: RESULTS.filter((r) => r.level === "fail").length,
      },
      warn_split: { assertion_level: assertWarns.length, phase_level: phaseWarns.length },
      phase_warn_ids: phaseWarns.map((r) => r.id),
      aggregates: aggs,
    };
    report.infos = INFO;

    let code = hardFails.length > 0 ? 1 : (ENV_SKIPS.length > 0 ? 2 : 0);
    report.env_skips = ENV_SKIPS;
    if (ENV_SKIPS.length) {
      NOTES.push(`有 ${ENV_SKIPS.length} 处因服务中途不可达未执行（不是通过、也不是产品缺陷）` +
        `：${ENV_SKIPS.map((s) => s.where).join("；")} —— 请等服务稳定后复跑`);
      if (hardFails.length === 0) NOTES.push("本次退出码 2（而不是 0）：有用例没跑成，不能发「全过」的结论");
    }
    phase("(done)");
    try { await browser.close(); } catch (_) { /* ignore */ }
    finish(code);
  }
})().catch((e) => {
  /* 最外层兜底：未预期的崩溃。默认当环境问题（2），但把堆栈打出来便于区分。 */
  console.error("RUNNER FATAL:", e && e.stack || e);
  try {
    fs.writeFileSync(REPORT_PATH, JSON.stringify({ fatal: String(e && e.stack || e), checks: RESULTS }, null, 2), "utf8");
  } catch (_) { /* ignore */ }
  process.exit(2);
});

/* ==========================================================================
 * 6. 人读摘要
 * ======================================================================== */
function printHuman(R) {
  const L = [];
  const push = (s) => L.push(s);
  const m = R.meta || {};
  push("──────────────────────────────────────────────────────────────");
  push(`UI 端到端门禁 · 模式=${m.mode} · 端口=${m.port} · ${m.at}`);
  /* 服务不可达时两个 build_id 都是 null —— 这时**不能**打"✓ 一致"，
     那会让人以为"版本对得上"，其实根本没比过。 */
  if (R.env.server_build_id == null) {
    push(`服务 build_id=（不可达）/ 磁盘 build_id=${R.env.disk_build_id == null ? "（未计算）" : R.env.disk_build_id}  ⚠ 未比对指纹`);
  } else {
    push(`服务 build_id=${R.env.server_build_id} / 磁盘 build_id=${R.env.disk_build_id}` +
      `（指纹来源 ${R.env.disk_build_id_source || "node:replica"}）` +
      (R.env.stale_build ? "  ⚠ stale_build=true（服务跑的是旧代码快照，需重启后复跑才能定性）" : "  ✓ 一致"));
  }
  if (R.env.app_js) {
    push(`产物版本（静态资源每次请求现读磁盘，可与服务版本不同）：app.js sha1=${R.env.app_js.sha1_8} size=${R.env.app_js.size} mtime=${R.env.app_js.mtime}`);
  }
  if (R.env.restarted_mid_run) {
    push(`⚠ 服务在本次运行期间被重启：pid ${R.env.restarted_mid_run.pid_before} → ${R.env.restarted_mid_run.pid_after}` +
      `（新 build_id=${R.env.restarted_mid_run.build_id_after}）—— 期间的接口连接失败属重启窗口，请复跑定性`);
  }
  push("──────────────────────────────────────────────────────────────");

  /* 环境/健康 */
  for (const c of R.checks.filter((x) => x.group === "env" || x.group === "health")) {
    push(`${c.ok ? "PASS" : (c.level === "warn" ? "WARN" : "FAIL")}  [${c.group}] ${c.id} —— ${c.detail}`);
  }

  /* 导航 */
  const n = R.nav || {};
  push(`[nav] ${n.passed || 0}/${n.total || 0} 可打开，失败 ${n.failed || 0}` +
    (n.enumerated_total ? `（DOM 动态枚举到 ${n.enumerated_total} 个 tab` +
      (n.enumerated_raw && n.enumerated_raw !== n.enumerated_total ? `，原始 ${n.enumerated_raw} 含重复` : "") +
      `；其中 ${(n.folded_tabs || []).length} 个在折叠区，已帮用户展开 ${n.fold_opens || 0} 次）` : ""));
  if (n.by_cat) push(`      各分类: ${Object.entries(n.by_cat).map(([k, v]) => k + "=" + v).join(" ")}`);
  if (n.empty_panels && n.empty_panels.length) push(`      空面板 ${n.empty_panels.length} 个: ${n.empty_panels.join(", ")}`);
  const navFails = R.checks.filter((x) => x.group === "nav" && !x.ok && x.level === "fail");
  for (const f of navFails.slice(0, 15)) push(`  FAIL ${f.id} —— ${f.detail}`);
  if (navFails.length > 15) push(`  ...另有 ${navFails.length - 15} 条导航失败，详见报告`);

  /* 接口 */
  const af = (R.api && R.api.failures) || [];
  push(`[api] 4xx/5xx ${af.length} 条` + (af.length ? "：" : ""));
  for (const f of af.slice(0, 10)) push(`      ${f.status} ${f.url} @${f.tab}${f.count > 1 ? " x" + f.count : ""}`);
  if ((R.api && R.api.net_failures && R.api.net_failures.length)) {
    push(`[api] 网络失败 ${R.api.net_failures.length} 条：` + R.api.net_failures.slice(0, 5).map((x) => `${x.url}(${x.why})`).join(" "));
  }

  /* 单位 */
  if (R.unit) {
    const u = R.unit;
    const err = u.rel_err === null || u.rel_err === undefined ? "—" : (u.rel_err * 100).toFixed(4) + "%";
    push(`[unit] ${u.symbol} 行情 ${u.quote_text || "?"} / K线 ${u.kline_text || "?"} → ` +
      (u.ok ? `一致（误差 ${err}）` : `不一致或无法换算（误差 ${err}）${u.stale_build ? "  [stale_build]" : ""}`));
  } else {
    push("[unit] 未执行（预算不足或页面不可用）");
  }

  /* 自选 */
  if (R.watch && R.watch.skipped) {
    /* 跳过 ≠ 失败：必须和「跑了但失败」分开打印，否则 quick/--no-write 会假红 */
    push(`[watch] 跳过（${R.watch.reason}）`);
  } else if (R.watch) {
    push(`[watch] 临时标的 ${R.watch.symbol || "?"} 加入=${R.watch.ok ? "OK" : "FAIL"} 清理=${R.watch.cleaned === null ? "未执行" : (R.watch.cleaned ? "OK" : "FAIL")}`);
  } else {
    push("[watch] 未执行（--no-write 或预算不足）");
  }

  /* 深链接冷启动（两组分开打印：通用取样 / 后置脚本面板回归） */
  if (R.deep && R.deep.length) {
    const ran = R.deep.filter((d) => d.ok !== null);
    const bad = ran.filter((d) => !d.ok);
    const sk = R.deep.length - ran.length;
    push(`[deep] 通用取样 ${ran.length - bad.length}/${ran.length} 直接打开就有内容` +
      (sk ? `（${sk} 例未执行=环境中途不可达）` : "") +
      (bad.length ? `（问题: ${bad.map((d) => d.tab + (d.stuck ? " 停在加载中" : "")).join("、")}）` : ""));
  } else {
    push("[deep] 通用取样未执行（预算不足）");
  }
  if (R.deep_regression && R.deep_regression.length) {
    const ran = R.deep_regression.filter((d) => d.ok !== null);
    const bad = ran.filter((d) => !d.ok);
    push(`[deep] 后置脚本面板回归 ${ran.length - bad.length}/${ran.length} 直接打开就有内容（名单 ${R.deep_regression.length} 条，含 5 个后置脚本：` +
      `douyin_corpus / funnel / industry_leaders / bottleneck / screener_extra）` +
      (R.deep_regression.length - ran.length ? `（${R.deep_regression.length - ran.length} 例未执行）` : "") +
      (bad.length ? `（问题: ${bad.map((d) => d.tab + (d.stuck ? " 停在加载中" : "")).join("、")}）` : ""));
  } else if (CFG.quick) {
    push("[deep] 后置脚本面板回归：quick 模式跳过（只有全量才算覆盖）");
  } else {
    push("[deep] 后置脚本面板回归未执行（预算不足）");
  }

  /* 环境中途不可用：单列一行，避免被读成"某条产品断言失败" */
  if (R.env_skips && R.env_skips.length) {
    push(`[env] 中途不可用 ${R.env_skips.length} 处（未执行，不是通过也不是产品缺陷）：`);
    for (const s of R.env_skips.slice(0, 6)) push(`      · ${s.where} —— ${s.why}`);
  }

  /* 状态页 */
  if (R.states && R.states.length) {
    const sk = R.states.filter((s) => s.ok === null).length;
    push(`[states] 空态/错误态 ${R.states.filter((s) => s.ok === true).length}/${R.states.length} 通过` +
      (sk ? `（${sk} 例未执行=环境中途不可达）` : ""));
    for (const s of R.states) {
      const tag = s.ok === null ? "SKIP" : (s.ok ? "PASS" : "FAIL");
      push(`      ${tag} ${s.name}` + (s.ok === null ? " —— 环境不可达，未执行"
        : (s.st && s.st.errHead ? ` —— 错误文案「${String(s.st.errHead).slice(0, 50)}」` : "")));
    }
  }

  /* 抽屉 */
  if (R.drawer) {
    const d = R.drawer;
    const ents = Object.entries(d.entries || {});
    const badEnt = ents.filter(([, v]) => !v.closed).map(([k]) => k);
    push(`[drawer] 打开=${d.opened ? "OK" : "FAIL"}，关闭入口 ${ents.length - badEnt.length}/${ents.length} 生效` +
      (badEnt.length ? `（失效: ${badEnt.join("、")}）` : "") +
      (d.sameRow ? `；再点同一行=保持打开(${d.sameRow.after ? "是" : "否"})` : ""));
  } else {
    /* 跳过 ≠ 失败，而且要区分原因：quick 是主动跳过，其余是预算/环境问题 */
    push("[drawer] " + (R.watch && R.watch.reason === "quick" ? "跳过（quick 模式）" : "未执行（预算不足或页面不可用）"));
  }

  /* 长文本 */
  if (R.longtext && R.longtext.tabs && R.longtext.tabs.length) {
    const bad = R.longtext.tabs.filter((t) => t.err || (t.n > 0 && t.afterCJK > t.innerW + 1));
    const asciiBad = R.longtext.tabs.filter((t) => t.n > 0 && t.afterASCII > t.innerW + 1);
    push(`[longtext] ${R.longtext.tabs.length} 个 tab 注入长文本：中文溢出 ${bad.length} 个` +
      (bad.length ? `（${bad.map((t) => t.tab).join(",")}）` : "") +
      `；无空格 ASCII 溢出 ${asciiBad.length} 个（极限输入，见 warn）`);
  } else {
    push("[longtext] 未执行");
  }

  /* 视口 */
  if (R.viewports && R.viewports.length) {
    const bad = R.viewports.filter((v) => (v.overflow_tabs && v.overflow_tabs.length) || (v.clipped && v.clipped.length));
    push(`[viewport] ${R.viewports.map((v) => v.w).join("/")} ` + (bad.length ? `有溢出/裁切的档位: ${bad.map((v) => v.w).join(",")}` : "无溢出"));
  }

  /* 告警与待办。
     口径与 summary 完全一致：只有「没过且标了 warn」才算告警 ——
     否则"通过了但被标成 warn"的记录会同时出现在 PASS 行和这里，读者一加就对不上。 */
  const warns = R.checks.filter((c) => !c.ok && c.level === "warn");
  if (warns.length) {
    push(`[warn] ${warns.length} 条（不计入退出码）：`);
    for (const w of warns.slice(0, 10)) push(`      ${w.id} —— ${w.detail}`);
  }
  if (R.needs_testid && R.needs_testid.length) {
    push(`[needs_testid] ${R.needs_testid.length} 条：`);
    for (const x of R.needs_testid) push("      · " + x);
  }
  if (R.not_covered && R.not_covered.length) {
    push(`[not_covered] 旧版有、新版没做（${R.not_covered.length} 条，别当成已覆盖）：`);
    for (const x of R.not_covered) push("      · " + x);
  }
  if (R.notes && R.notes.length) {
    push("[notes]");
    for (const x of R.notes) push("      · " + x);
  }
  if (R.phases && R.phases.length) {
    push("[phases] " + R.phases.map((p) => `${p.name}=${(p.ms / 1000).toFixed(1)}s`).join("  "));
  }

  push("──────────────────────────────────────────────────────────────");
  /* 摘要分三段，**三段各自可核对**：
       断言：通过 + 告警 + 失败 === 总数    （告警/失败都在分母里）
       信息：好消息，不进分母、不影响判定
       聚合：N 次同类失败合并成 1 条断言的地方，把 N 原样写出来
     为什么不写"107/111 通过，告警 6"：这种写法读者无法反推，也就等于没有摘要。 */
  const s = R.summary || {};
  const alg = s.algebra || {};
  const closed = s.passed + s.warned + s.failed;
  push(`断言 ${s.checks_total}：通过 ${s.passed} · 告警 ${s.warned} · 失败 ${s.failed}` +
    `        （${s.passed}+${s.warned}+${s.failed}=${closed}` +
    `${closed === s.checks_total && alg.ok !== false ? " ✓" : " ✗ 与总数 " + s.checks_total + " 不符"}）`);
  if (s.warn_split) {
    const ids = (s.phase_warn_ids || []).slice(0, 3).join(" / ");
    push(`      告警分层：断言级 ${s.warn_split.assertion_level} · 阶段级 ${s.warn_split.phase_level}` +
      (ids ? `（阶段级=环境/预算：${ids}）` : ""));
  }
  if (s.info_total) {
    push(`信息 ${s.info_total}：` + ((R.infos || []).slice(0, 3).map((x) => x.id).join(" · ")) +
      `        （不影响判定，不计入断言分母）`);
  }
  if (s.aggregates && s.aggregates.length) {
    for (const a of s.aggregates) {
      push(`聚合 ${s.aggregates.length}：${a.what} ` +
        `${a.n} 次 → 合并为 1 条${a.ok ? "（已通过）" : "告警"}` + (a.window ? `（窗口 pid ${a.window}）` : ""));
    }
  }
  if (s.level_ok === false) push("      ⚠ level 三值自检未通过：见 [harness] 断言明细（这一轮结果不可作为验收依据）");
  push(`耗时 ${(R.duration_ms / 1000).toFixed(1)}s`);
  push(`报告: ${REPORT_PATH}`);
  push(`EXIT=${R.exit_code}`);
  process.stdout.write(L.join("\n") + "\n");
}
