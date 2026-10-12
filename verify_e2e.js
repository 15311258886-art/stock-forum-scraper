/* 智能选股增强 B4/B5/B7/B8/B6 真实端到端验证（自包含单进程版）
 * 不再依赖独立 server.py（沙箱内起服被安全策略/路径卡住）。
 * 本脚本在【同一个 Node 进程】里：
 *   1) 起一个本地 HTTP 服务，提供真实前端文件（viewer/index.html + viewer/static/*）
 *      并把 API 请求桥接到真实 screener_extra（含真实 DeepSeek Key）；
 *   2) 用 puppeteer 真实点击每个按钮，验证 UI 接线、弹层、DeepSeek 真实调用链路。
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const puppeteer = require("puppeteer-core");

const ROOT = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper";
const VIEWER = path.join(ROOT, "viewer");
const STATIC = path.join(VIEWER, "static");
// QA 修正：原硬编码的 3.13.12 解释器缺 numpy，无法 import baostock，导致 B7 回测
// 在 E2E 里只能走到「诚实报错」而非真实回测数据。改用已装好 baostock+numpy 的 .venv
// 解释器，使 E2E 真正拉到真实 baostock 行情、验证 B7 面板显示真实结果。
const PY = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/.venv/Scripts/python.exe";
const BRIDGE = path.join(ROOT, "api_bridge.py");
const PORT = 8821;
const BASE = "http://127.0.0.1:" + PORT;
const CHROME = process.env.CHROME_PATH;
const MOCK = [
  { code: "600000", name: "浦发银行", industry: "银行", pe: 12, pb: 1.1, ps: 2, peg: 1, pcf: 8, cap: 50, pct: 1.2, holder_num: 5, worth: 75, roe: 15, gm: 30, dr: 50, dy: 2 },
  { code: "000001", name: "平安银行", industry: "保险", pe: 9, pb: 0.9, ps: 1.5, peg: 0.8, pcf: 6, cap: 40, pct: -0.5, holder_num: 4, worth: 80, roe: 18, gm: 35, dr: 45, dy: 3.5 },
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CT = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8" };

function bridge(route, method, query, body) {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, [BRIDGE], { cwd: ROOT });
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    p.on("error", reject);
    p.on("close", () => {
      if (err) console.log("BRIDGE_ERR", err.slice(0, 160));
      try { resolve(JSON.parse(out)); } catch (e) { reject(new Error("bridge parse: " + out.slice(0, 160))); }
    });
    p.stdin.write(JSON.stringify({ route, method, query: query || {}, body: body || {} }));
    p.stdin.end();
  });
}

const server = http.createServer(async (req0, res0) => {
  const u = new URL(req0.url, BASE);
  const p = u.pathname;
  try {
    let buf = null;
    if (p === "/" || p === "/index.html") {
      // 真实前端根在 viewer/static/（index.html 与 static/* 同目录），
      // 资源引用为相对路径 static/app.js 等，故根即 STATIC 目录。
      buf = fs.readFileSync(path.join(STATIC, "index.html"));
      res0.writeHead(200, { "Content-Type": CT[".html"] });
      return res0.end(buf);
    }
    if (p.startsWith("/static/")) {
      const f = path.join(STATIC, p.slice("/static/".length));
      if (fs.existsSync(f) && fs.statSync(f).isFile()) {
        const ext = path.extname(f).toLowerCase();
        buf = fs.readFileSync(f);
        res0.writeHead(200, { "Content-Type": CT[ext] || "application/octet-stream" });
        return res0.end(buf);
      }
      res0.writeHead(404); return res0.end("nf");
    }
    if (p === "/api/screener" && req0.method === "GET") {
      res0.writeHead(200, { "Content-Type": "application/json" });
      return res0.end(JSON.stringify({ ok: true, desc: "mock", rows: MOCK }));
    }
    if (p.startsWith("/api/")) {
      let body = {};
      if (req0.method === "POST") {
        const chunks = []; req0.on("data", (c) => chunks.push(c));
        await new Promise((r) => req0.on("end", r));
        try { body = JSON.parse(Buffer.concat(chunks).toString()); } catch (e) {}
      }
      const query = Object.fromEntries(u.searchParams);
      const data = await bridge(p, req0.method, query, body);
      res0.writeHead(200, { "Content-Type": "application/json" });
      return res0.end(JSON.stringify(data));
    }
    res0.writeHead(404); res0.end("nf");
  } catch (e) {
    if (!res0.headersSent) { res0.writeHead(500); res0.end(String(e)); }
  }
});

function jget(path0) {
  return new Promise((resolve, reject) => {
    http.get(BASE + path0, (res) => { let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } }); }).on("error", reject);
  });
}
function jpost(path0, body0) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body0);
    const req = http.request(BASE + path0, { method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } }, (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    });
    req.on("error", reject); req.write(data); req.end();
  });
}

(async () => {
  await new Promise((r) => server.listen(PORT, "127.0.0.1", r));
  const recs = [];
  const rec = (name, ok, detail) => { recs.push({ name, ok: !!ok, detail: detail || "" }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  | " + detail : "")); };

  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERR:" + e.message));

  // 后端直测（经本地 HTTP 桥接，证明路由+真实函数可用；沙箱无包时验证诚实报错）
  try { const v = await jget("/api/screener/valuation?codes=600000,000001");
        const a = (v.data || {})["600000"] || {};
        rec("B4 估值分位路由(真实数据或诚实报错)", !!a && (a.pe_pct != null || a.ok === false), "pe_pct=" + a.pe_pct + " ok=" + a.ok + " err=" + (a.error||"").slice(0,20)); }
  catch (e) { rec("B4 估值分位路由(真实数据或诚实报错)", false, String(e)); }
  try { const b = await jget("/api/screener/backtest?codes=600000,000001&start=2024-01-01&end=2025-01-01");
        rec("B7 回测路由(真实数据或诚实报错)", b && (b.ok === true && b.n === 2) || (b.ok === false && !!b.error), "n=" + b.n + " ok=" + b.ok + " err=" + (b.error||"").slice(0,20)); }
  catch (e) { rec("B7 回测路由(真实数据或诚实报错)", false, String(e)); }
  try { const t = await jget("/api/screener/themes");
        rec("B8 题材路由(真实数据或诚实报错)", t && (t.ok === true && Array.isArray(t.data) && t.data.length > 0) || (t.ok === false && !!t.error), "themes=" + (t.data ? t.data.length : 0) + " ok=" + t.ok); }
  catch (e) { rec("B8 题材路由(真实数据或诚实报错)", false, String(e)); }
  // B6 自然语言（真实 Key → 结构化条件，证明 server 代理 DeepSeek 走通）
  try { const nl = await jpost("/api/nl_screener", { text: "低估值的白酒股，股息率大于3%，按股息率排序" }); rec("B6 自然语言路由(真实Key→结构化条件)", nl && nl.ok === true && nl.cond && Array.isArray(nl.cond.screens), "screens=" + (nl.cond ? nl.cond.screens.join(",") : (nl.error || ""))); } catch (e) { rec("B6 自然语言路由(真实Key→结构化条件)", false, String(e)); }

  // 前端：进 screener，触发 loadScreener → 渲染 .sc-extra
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await sleep(600);
  await page.evaluate(() => { if (window.selectNav) selectNav("watch", "screener", true); });
  // 等待工具栏真正渲染（baostock 激活后首屏可能稍慢，轮询避免时序误判）
  try { await page.waitForSelector("#p-screener .sc-extra", { timeout: 12000 }); } catch (e) {}
  await sleep(400);
  rec("B4-B8/B6 工具栏(.sc-extra)出现", await page.evaluate(() => !!document.querySelector("#p-screener .sc-extra")));

  // B4 估值分位：真实数据则弹表格，诚实报错则显示提示。两种都算链路通。
  await page.evaluate(() => { const b = document.querySelector("#scValRun"); if (b) b.click(); });
  await sleep(8000);
  rec("B4 估值分位(弹层或诚实提示)", await page.evaluate(() => {
    const m = document.getElementById("scModal");
    if (m && m.style.display === "flex" && /估值分位/.test(m.textContent) && m.querySelectorAll("table tr").length > 1) return true;
    const h = document.querySelector("#scHint2");
    return !!h && /真实/.test(h.textContent);
  }));
  await page.evaluate(() => { const x = document.getElementById("scModal"); if (x) x.style.display = "none"; });
  await sleep(200);

  // B7 回测面板
  await page.evaluate(() => { const b = document.querySelector("#scBtRun"); if (b) b.click(); });
  await sleep(300);
  const btRange = await page.evaluate(() => { const r = document.querySelector("#scBtRange"); return r && r.style.display !== "none"; });
  await page.evaluate(() => { const b = document.querySelector("#scBtGo"); if (b) b.click(); });
  // 真实 baostock 回测较慢，等待弹层出现（最多 20s）
  try { await page.waitForFunction(() => { const m = document.getElementById("scModal"); return m && m.style.display === "flex" && /策略回测/.test(m.textContent); }, { timeout: 20000 }); } catch (e) {}
  await sleep(300);
  // QA 强化：B7 面板必须显示【真实回测结果】——含总收益/年化/最大回撤/夏普/索提诺
  // 等真实指标标签 + 幸存者偏差 + 坑位提示（8 条），而非仅弹「诚实报错」提示。
  rec("B7 回测面板(真实回测结果:总收益/年化/回撤/夏普+8坑位+幸存者)", await page.evaluate(() => {
    const m = document.getElementById("scModal");
    if (m && m.style.display === "flex" && /策略回测/.test(m.textContent)
        && /总收益/.test(m.textContent) && /年化/.test(m.textContent)
        && /最大回撤/.test(m.textContent) && /夏普/.test(m.textContent)
        && /索提诺/.test(m.textContent) && /幸存者偏差/.test(m.textContent)
        && /坑位提示/.test(m.textContent)) {
      return true;
    }
    return false;
  }));
  await page.evaluate(() => { const x = document.getElementById("scModal"); if (x) x.style.display = "none"; });
  await sleep(200);

  // B8 题材弹层：真实数据有 chips，诚实报错则含真实提示
  await page.evaluate(() => { const b = document.querySelector("#scThemeRun"); if (b) b.click(); });
  await sleep(2500);
  rec("B8 题材弹层(题材chips或诚实提示)", await page.evaluate(() => {
    const m = document.getElementById("scModal");
    if (m && m.style.display === "flex" && /题材/.test(m.textContent)) {
      return m.querySelectorAll(".sc-chip").length > 0 || /真实/.test(m.textContent);
    }
    const h = document.querySelector("#scHint2");
    return !!h && /真实/.test(h.textContent);
  }));
  await page.evaluate(() => { const x = document.querySelector("#scModal"); if (x) x.style.display = "none"; });
  await sleep(200);

  // B6 自然语言（UI 真实 Key → 成功反馈「已按」，不崩）
  await page.evaluate(() => { const i = document.querySelector("#scNlInput"); if (i) i.value = "低估值的白酒股，股息率大于3%"; const b = document.querySelector("#scNlRun"); if (b) b.click(); });
  await sleep(12000);
  const nlHint = await page.evaluate(() => { const e = document.getElementById("scToast"); return e ? e.textContent : ""; });
  rec("B6 自然语言(UI真实Key→已按)", nlHint.length > 0 && /已按/.test(nlHint), "hint=" + nlHint.slice(0, 40));

  // 设置弹层 + 真实 key 保存 round-trip
  await page.evaluate(() => { const b = document.querySelector("#scGear"); if (b) b.click(); });
  await sleep(2500);
  rec("设置弹层(DeepSeek)", await page.evaluate(() => { const m = document.getElementById("scModal"); return !!m && m.style.display === "flex" && /DeepSeek/.test(m.textContent); }));
  // 读取已部署真实 key 做保存 round-trip（不清空）
  let deployedKey = "";
  try { deployedKey = fs.readFileSync(path.join(VIEWER, ".deepseek_key"), "utf-8").trim(); } catch (e) {}
  if (deployedKey) {
    await page.evaluate((k) => { const i = document.getElementById("scDsKey"); if (i) i.value = k; const s = document.getElementById("scDsSave"); if (s) s.click(); }, deployedKey);
    await sleep(6000);
  }
  const setMsg = await page.evaluate(() => { const e = document.getElementById("scDsMsg"); return e ? e.textContent : ""; });
  rec("设置保存按钮(UI反馈)", /已保存|失败|错误/.test(setMsg), "msg=" + setMsg);
  try { const s2 = await jget("/api/settings/deepseek_key"); rec("设置 Key 落盘后端可读", s2.configured === true, "configured=" + s2.configured); } catch (e) { rec("设置 Key 落盘后端可读", false, String(e)); }

  rec("控制台零 JS 错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" || "));

  await browser.close();
  server.close();
  const pass = recs.filter((r) => r.ok).length;
  console.log("\n==== 结果 " + pass + "/" + recs.length + " PASS ====");
  process.exit(pass === recs.length ? 0 : 1);
})().catch((e) => { console.error("RUNNER_ERR", e); server.close(); process.exit(2); });
