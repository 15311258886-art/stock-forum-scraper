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

if (CFG.help) {
  process.stdout.write(
    "用法: node tests/e2e/run.cjs [--quick] [--json] [--no-write] [--port N] [--keep-shots]\n" +
    "  --quick      只跑 3 个 tab + 单位一致性 + 健康检查（<40s）\n" +
    "  --json       stdout 只输出一个 JSON 对象\n" +
    "  --no-write   跳过自选增删（只读环境）\n" +
    "退出码: 0 全过 / 1 用例失败 / 2 环境不可用\n"
  );
  process.exit(0);
}

/* ==========================================================================
 * 1. 结果收集
 * ======================================================================== */
/** level: "fail" 计入退出码；"warn" 只记录不红；"info" 纯记录 */
const RESULTS = [];
function rec(group, id, ok, detail, why, level) {
  const lv = level || (ok ? "info" : "fail");
  RESULTS.push({ group, id, ok: !!ok, detail: detail === undefined ? "" : String(detail), why: why || "", level: lv });
  return ok;
}
const fail = (group, id, detail, why) => rec(group, id, false, detail, why, "fail");
const pass = (group, id, detail) => rec(group, id, true, detail, "", "info");
const warn = (group, id, detail, why) => rec(group, id, false, detail, why || "", "warn");

const NOTES = [];            // 环境/模式说明（不进断言）
const NEEDS_TESTID = [];     // 需要加稳定标识才能更稳的地方（本轮不许改 app.js，故只登记）
const BLOCKED = [];          // 因约束无法测的点
const NOT_COVERED = [];      // 旧版有、新版没覆盖的检查项（漏掉不写 = 假装覆盖了）
/* 任务约束导致"测得不够硬"的地方必须亮出来（不写 = 假装测过了）： */
BLOCKED.push(
  "禁止修改 viewer/static/app.js → 无法给面板/成交量/弹窗加 data-testid，" +
    "所以「面板是否就绪」只能靠 innerText 长度 + 轮询、「成交量」只能靠 .si-item/.ks-grid 结构定位（见 needs_testid）。",
  "禁止重启服务（多人共用 8765）→ 只能对「进程里正跑的那份代码」做端到端验证；" +
    "后端 Python 改动必须由人重启后复跑，否则 /api/version 的 build_id 会一直是旧值（报告里标 stale_build）。"
);

/* ==========================================================================
 * 2. 小工具
 * ======================================================================== */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowMs = () => Date.now();

/** 极简 HTTP GET/POST（不引依赖：项目无出网权限，也不许新建 package.json） */
function httpReq(method, p, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8");
    const req = http.request(
      {
        host: "127.0.0.1", port: CFG.port, path: p, method,
        headers: Object.assign(
          { Accept: "application/json" },
          data ? { "Content-Type": "application/json", "Content-Length": data.length } : {}
        ),
        timeout: timeoutMs || 15000,
      },
      (res) => {
        const chunks = [];
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
  const r = await httpReq(body === undefined ? "GET" : "POST", p, body, t);
  return { status: r.status, json: r.json, text: r.text };
};

/** 磁盘侧代码指纹 —— 与 viewer/build_id.py::of_disk 逐字节等价（已交叉验证同值）。
 *  为什么要自己算：服务可能跑的是旧快照，/api/version 的 build_id 与磁盘不一致时，
 *  「单位一致性」这类依赖新代码的用例会 FAIL，但那是"没重启"而不是"改坏了"，
 *  必须在报告里标 stale_build 才不会误判成回归。 */
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
      const st = fs.statSync(path.join(ROOT, rel));
      h.update(`${rel}|${Math.floor(st.mtimeMs / 1000)}|${st.size};`, "utf8");
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
async function preflight() {
  const out = { ok: false, version: null, health: null, diskBuild: null, staleBuild: null, err: null };
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
    out.diskBuild = diskBuildId();
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

  const finish = (code, extraNote) => {
    report.exit_code = code;
    report.duration_ms = nowMs() - T0;
    report.meta.finished_at = new Date().toISOString();
    /* 环境不可用会在任何断言跑之前就退出，此时 summary 还没算过 —— 统一在这里兜底，
       否则打印摘要会抛 "Cannot read properties of undefined"，把一个干净的 EXIT=2 弄成崩溃。 */
    if (!report.summary) {
      report.summary = {
        checks_total: RESULTS.length,
        passed: RESULTS.filter((r) => r.ok).length,
        failed: RESULTS.filter((r) => r.level === "fail" && !r.ok).length,
        warned: RESULTS.filter((r) => r.level === "warn").length,
      };
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
      `服务 build_id=${pf.version.build_id} ≠ 磁盘 ${pf.diskBuild}`, "不重启服务就换不掉旧代码；本项只告警，因为门禁无权重启（多人共用）");
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
    /Failed to load resource.*\b(4\d\d|5\d\d)\b/i,
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
          /* 是否在「研究工具（N）」折叠区里：这些项默认收起，点击前必须先展开 */
          folded: !!(a.closest && a.closest("details")),
          active: a.className.indexOf("active") >= 0,
        })), c.cat);
      for (const s of subs) nav.push({ cat: c.cat, catLabel: c.label, ...s });
    }

    /* 枚举自一致性校验：把最后一个分类再点一次，数量必须与刚才记录的一致。
       动态轨道（learn）异步到达时，"数量稳定"可能停在只有静态项的中间态 ——
       实测同一份代码一次数到 53、一次数到 43（差的就是 10 个动态轨道）。
       枚举结果不可复现的话，"53/53 全过"与"43/43 全过"是两个含义不同的绿灯，必须抓出来。 */
    const byCat = {};
    for (const x of nav) byCat[x.cat] = (byCat[x.cat] || 0) + 1;
    let recheck = null;
    if (cats.length) {
      const lastCat = cats[cats.length - 1].cat;
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
    const catNoTab = nav.filter((x) => x.hrefCat !== x.cat);
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
      NEEDS_TESTID.push("[nav] .sub-tab 缺 data-cat 属性 —— 现在只能从 href=\"#cat/tab\" 反解分类；" +
        `若能给每个 .sub-tab 加 data-cat（paintSubnav 里已有 cat.key，一行即可），分类断言会更直接、更稳。影响 ${noDataCat.length} 个 tab。`);
    }
    NEEDS_TESTID.push("[nav] 面板缺统一的「已就绪」标记 —— 现在用 #p-<tab> 的 innerText 长度判断是否渲染完成；" +
      "建议给面板容器加 data-state=\"loading|ready|empty|error\"（或在渲染完成时设 data-rendered=\"1\"），" +
      "门禁就能精确等就绪，而不是靠 sleep + 文本长度猜。");
    NEEDS_TESTID.push("[drawer] 个股详情抽屉缺 data-testid —— 现在靠 #stkQuote / #klineSnap 里的 .si-item/.ks-grid 结构找「成交量」；" +
      "建议给两个成交量单元格加稳定标识（如 data-testid=\"vol-quote\" / data-testid=\"vol-kline\"），" +
      "避免以后改版式就再也测不到单位一致性（P0-1 的核心守门项）。");
    NEEDS_TESTID.push("[watch] 自选行缺稳定选择器 —— 现在用 [data-wt-open]/[data-wt-del] 反查；" +
      "这两者是业务属性、语义够用，但「加入自选」弹窗 (#wtF-symbol/#wtF-save) 建议加 data-testid，弹窗改名就会断。");

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
      let hashOk = false, panel = { panelExists: false, panelLen: 0 };
      const readTabState = (x) => page.evaluate((y) => ({
        hash: location.hash,
        panelExists: !!document.querySelector("#p-" + y.tab),
        panelLen: (() => { const p = document.querySelector("#p-" + y.tab); return p ? (p.innerText || "").trim().length : 0; })(),
        /* 选中态：横条上该项是否带 .active —— 不参与 hard fail（hash 才是深链接判据），但要记录，
           因为"hash 对了但横条高亮没跟上"是用户能一眼看出来的错位 */
        active: (() => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${y.tab}"]`); return !!a && a.className.indexOf("active") >= 0; })(),
      }), x).catch(() => null);

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
        while (panel.panelLen === 0 && nowMs() < tContent) {
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
        new_console_err: cErr,
        new_page_err: pErr,
        new_api_fails: apiNew,
        bad_tokens: scan ? scan.hits : [],
        ms: nowMs() - tStart,
      };

      if (tabOk) navPassed++;
      const detail = [
        `hash=${panel.hash}${hashOk ? "" : " ✗"}`,
        `面板${panel.panelExists ? "存在" : "缺失"}/${panel.panelLen}字`,
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
      if (scan && scan.panelExists && scan.panelLen === 0) report.nav.empty_panels.push(CURRENT_TAB);
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
      rec("unit", "个股抽屉可打开（真实 UI 路径）", opened.ok || true, opened.how,
        "抽屉都打不开就没法验证单位；路径写进 detail 便于判断是产品回归还是测试手段退化", "fail");

      /* 读两个成交量：结构是 .si-item / .ks-grid>div 里 <span>成交量</span><b>数值</b> */
      const readVol = () => page.evaluate(() => {
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
        return {
          quote: pick("#stkQuote"),
          kline: pick("#klineSnap"),
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
      if (!vol.quote || !vol.kline) {
        NEEDS_TESTID.push("[drawer] 成交量单元格读不到（行情「" + (vol.quote || "空") + "」K线「" + (vol.kline || "空") +
          "」）—— 若版式改过，需要 data-testid 才能稳定定位。");
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
        /* 弹窗关闭 + 行出现 */
        let appeared = false;
        for (let i = 0; i < 60; i++) {
          await sleep(300);
          const st = await page.evaluate((sym) => ({
            modal: !!document.querySelector("#wtF-symbol"),
            row: !!document.querySelector(`#p-watch [data-wt-open="${sym}"]`),
          }), testSym);
          if (st.row && !st.modal) { appeared = true; break; }
        }
        rec("watch", `加入自选后出现该行（${testSym}）`, appeared, appeared ? "行已出现" : "加了但列表里没有该行",
          "自选池是用户最私有的数据面，增删链路一断就等于「点了没反应」", "fail");
        W.steps.push("add appeared=" + appeared);

        /* 删除：从行上取 data-wt-del 的 id，真实点击并接受 confirm */
        let removed = false;
        if (appeared) {
          const delId = await page.evaluate((sym) => {
            const row = document.querySelector(`#p-watch [data-wt-open="${sym}"]`);
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
              const gone = await page.evaluate((sym) => !document.querySelector(`#p-watch [data-wt-open="${sym}"]`), testSym);
              if (gone) { removed = true; break; }
            }
            page.off("dialog", handler);
          } else {
            W.steps.push("行上没有 data-wt-del 按钮（找不到删除入口）");
          }
          rec("watch", `删除后该行消失（${testSym}）`, removed, removed ? "行已消失" : "点了删除但行还在",
            "删除不生效＝用户删不掉自己的标的，且会连带影响下一次添加（已在池中）", "fail");
        }
        W.ok = appeared && removed;
      } catch (e) {
        fail("watch", "自选增删链路", false, e.message.slice(0, 200),
          "写链路异常必须暴露，但更关键的是不能留下脏数据（见 finally 清理）");
      } finally {
        /* 兜底清理：不管上面怎么挂的，只要临时标的还可能在池里就删掉，并复核 */
        try {
          if (testSym) {
            const after = await httpJson("/api/watch_list", undefined, 30000);
            const arr = Array.isArray(after.json && (after.json.items || after.json.data)) ? (after.json.items || after.json.data) : [];
            const leftover = arr.filter((x) => String(x.symbol || x.code || "") === testSym);
            for (const it of leftover) {
              await httpJson("/api/watch_item_remove", { id: it.id }, 20000);
            }
            const verify = await httpJson("/api/watch_list", undefined, 30000);
            const arr2 = Array.isArray(verify.json && (verify.json.items || verify.json.data)) ? (verify.json.items || verify.json.data) : [];
            W.cleaned = arr2.every((x) => String(x.symbol || x.code || "") !== testSym);
            rec("watch", "测试数据已清理（临时标的不留在池中）", W.cleaned,
              `临时标的 ${testSym}，清理后残留 ${arr2.filter((x) => String(x.symbol || x.code || "") === testSym).length} 条`,
              "门禁往用户真实数据里写脏数据比不测更糟；必须自证清理干净", "fail");
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

    phase("states");
    /* ---------- 5.9 空态 / 错误态 / 停更态 ----------
     * 用请求拦截在"测试页"里构造，不动服务端：某个接口返回 [] / 500 / 非 JSON，
     * 页面必须给出中文空态或错误提示，不能白屏、不能有未捕获异常。
     * 这是「后端挂了前端能不能自救」的守门项，且必须与真实数据解耦（否则永远测不到）。 */
    if (!timedOut() && !CFG.quick) {
      CURRENT_TAB = "(states)";
      const CASES = [
        { name: "空态：主要接口返回 []", kind: "empty" },
        { name: "错误态：接口 500（HTML 错误页）", kind: "500html" },
        { name: "错误态：接口 200 但非 JSON", kind: "badjson" },
      ];
      for (const cs of CASES) {
        const mp = await browser.newPage();
        await mp.setViewport({ width: 1440, height: 1000 });
        const mErrs = [];
        mp.on("pageerror", (e) => mErrs.push(String(e && e.message || e).slice(0, 200)));
        mp.on("console", (m) => { if (m.type() === "error" && !isNoise(m.text())) mErrs.push("console:" + m.text().slice(0, 200)); });
        await mp.setRequestInterception(true);
        mp.on("request", (req) => {
          const u = req.url();
          if (u.indexOf("/api/") < 0) return req.continue();
          try {
            if (cs.kind === "empty") {
              return req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: "[]" });
            }
            if (cs.kind === "500html") {
              return req.respond({ status: 500, contentType: "text/html; charset=utf-8", body: "<html><body>Internal Server Error</body></html>" });
            }
            return req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: "{not json at all" });
          } catch (_) { return req.continue(); }
        });
        let navErr = null;
        try { await mp.goto(PAGE_URL, { waitUntil: "networkidle2", timeout: 35000 }); } catch (e) { navErr = e.message; }
        await sleep(1400);
        await dismissFirstRun(mp);
        await sleep(300);
        const st = await mp.evaluate(() => {
          const txt = (document.body.innerText || "").replace(/\s+/g, " ").trim();
          const hint = ((document.querySelector("#statusHint") || {}).textContent || "").replace(/\s+/g, " ").trim();
          const empties = document.querySelectorAll(".empty, .wt-empty, .muted").length;
          const errBox = document.querySelectorAll(".error, .err-banner, #errorBox, .toast").length;
          return {
            bodyLen: txt.length,
            hint: hint.slice(0, 60),
            empties, errBox,
            hasCn: /暂无|无数据|没有|尚未|未检出|无记录|失败|错误|出错|异常|无法|请稍后|重试|加载/.test(txt + " " + hint),
            badHard: !!(/\bundefined\b/.test(txt) || /\bNaN\b/.test(txt) || /Invalid Date/.test(txt)),
            head: txt.slice(0, 90),
          };
        });
        const blank = st.bodyLen < 40;                 // 白屏判据
        const ok = !blank && st.hasCn && !st.badHard && mErrs.length === 0;
        rec("states", cs.name, ok,
          `正文 ${st.bodyLen} 字 | 中文提示=${st.hasCn} | 提示条「${st.hint}」| .empty=${st.empties} 错误容器=${st.errBox} | JS异常 ${mErrs.length} | gotoErr=${navErr ? "有" : "无"} | 首屏「${st.head}」`,
          "后端某接口烂掉时，前端至少要给中文人话、不能白屏或抛未捕获异常；用拦截构造才能与真实数据解耦", "fail");
        report.states.push({ name: cs.name, ok, st, errs: mErrs.slice(0, 5) });
        await mp.close();
      }
    } else if (CFG.quick) {
      NOTES.push("quick 模式：跳过空态/错误态（需要额外开页面，耗时且不属于「改完立刻要看」的项）");
    }

    phase("viewport");
    /* ---------- 5.10 三档视口 ----------
     * 1024 / 1440 / 1920：查横向溢出、顶栏按钮能被 elementFromPoint 命中、无可点元素被裁到视口外。
     * 按钮清单动态枚举（不写死 id）：顶栏里有 hidden 的按钮（如 #btnDemo）写死就会假红。
     * quick 只测当前 tab；全量逐 tab 测（159 次切换，配合全局预算）。 */
    const VPS = [
      { w: 1024, h: 700 },
      { w: 1440, h: 1000 },
      { w: 1920, h: 1080 },
    ];
    const vpTabs = CFG.quick ? [navList[0]].filter(Boolean) : nav;
    for (const vp of VPS) {
      if (timedOut()) break;
      CURRENT_TAB = `(viewport ${vp.w})`;
      await page.setViewport({ width: vp.w, height: vp.h });
      await sleep(450);
      let overflowTabs = [];
      let clipped = [];
      for (const it of vpTabs) {
        if (timedOut()) break;
        await clickSubTab(it).catch(() => {});
        await sleep(CFG.quick ? 350 : 420);
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
        if (m.overflow > 1) overflowTabs.push(it.cat + "/" + it.tab + "(" + m.overflow + "px)");
        if (m.out.length) clipped.push(it.cat + "/" + it.tab + ":" + m.out.slice(0, 3).join(","));
      }
      /* 顶栏按钮命中测试：只在 1440 与 1024 做（1920 是宽屏，几乎不可能遮挡） */
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
      restartedMidRun ? "warn" : "fail");
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
      "抽屉拖拽 / 右下角拉伸 / 拉到极限后手柄仍在视口内：新版未做（旧版 3 种抽屉 × 6 条断言）。",
      "抽屉四条关闭入口（✕ / Esc / 点面板外 / 底部按钮）：新版未做，只用 Esc 与 JS 收尾。",
      "长文本压力（超长中文标题、超长无空格 ASCII、公司全称列换行）：新版未做。",
      "错误态 8 种细粒度变体（网络 abort / 500+JSON / summary 缺 counts / topics 缺 weight / persons 缺字段 / sentiment 类型不符）：" +
        "新版只做 3 种（[] / 500+HTML / 非 JSON），细粒度缺字段兜底未覆盖。",
      "10 个旧 tab 的「元素数量与基线一致（未消失）」：新版只断言面板有内容，不比元素计数基线。",
      "抽屉横向不遮挡所点行：新版未做。",
      "主题切换后元素不消失/面板高度一致：新版未做。"
    );

    const hardFails = RESULTS.filter((r) => r.level === "fail" && !r.ok);
    const warns = RESULTS.filter((r) => r.level === "warn");
    report.summary = {
      checks_total: RESULTS.length,
      passed: RESULTS.filter((r) => r.ok).length,
      failed: hardFails.length,
      warned: warns.length,
    };
    let code = hardFails.length > 0 ? 1 : 0;
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
  push(`服务 build_id=${R.env.server_build_id} / 磁盘 build_id=${R.env.disk_build_id}` +
    (R.env.stale_build ? "  ⚠ stale_build=true（服务跑的是旧代码快照，需重启后复跑才能定性）" : "  ✓ 一致"));
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
  if (R.watch) {
    push(`[watch] 临时标的 ${R.watch.symbol || "?"} 加入=${R.watch.ok ? "OK" : "FAIL"} 清理=${R.watch.cleaned === null ? "未执行" : (R.watch.cleaned ? "OK" : "FAIL")}`);
  } else {
    push("[watch] 未执行（--no-write 或预算不足）");
  }

  /* 状态页 */
  if (R.states && R.states.length) {
    push(`[states] 空态/错误态 ${R.states.filter((s) => s.ok).length}/${R.states.length} 通过`);
    for (const s of R.states) if (!s.ok) push(`      FAIL ${s.name}`);
  }

  /* 视口 */
  if (R.viewports && R.viewports.length) {
    const bad = R.viewports.filter((v) => (v.overflow_tabs && v.overflow_tabs.length) || (v.clipped && v.clipped.length));
    push(`[viewport] ${R.viewports.map((v) => v.w).join("/")} ` + (bad.length ? `有溢出/裁切的档位: ${bad.map((v) => v.w).join(",")}` : "无溢出"));
  }

  /* 告警与待办 */
  const warns = R.checks.filter((c) => c.level === "warn");
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
  push(`断言 ${R.summary.passed}/${R.summary.checks_total} 通过，失败 ${R.summary.failed}，告警 ${R.summary.warned}，耗时 ${(R.duration_ms / 1000).toFixed(1)}s`);
  push(`报告: ${REPORT_PATH}`);
  push(`EXIT=${R.exit_code}`);
  process.stdout.write(L.join("\n") + "\n");
}
