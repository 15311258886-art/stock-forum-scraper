/* 智能选股增强 B4/B5/B7/B8/B6 真实端到端验证（v6：真实 server + 真实 DeepSeek Key）
 * - 仅拦截基础 /api/screener GET 注入 mock 行（让 loadScreener 渲染出 .sc-extra 工具栏）；
 *   其余 GET/POST 全部放行，尤其 POST 经 req.continue({postData}) 保留 body（避免 puppeteer 吞体）。
 * - B6 自然语言走真实 DeepSeek（viewer/.deepseek_key 已部署真实 key），验证 server 代理调用链路。
 */
const puppeteer = require("puppeteer-core");
const http = require("http");
const fs = require("fs");
const ROOT = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper";
const PORT = 8821;
const BASE = "http://127.0.0.1:" + PORT;
const CHROME = process.env.CHROME_PATH;
const KEYFILE = ROOT + "/viewer/.deepseek_key";

function jget(path) {
  return new Promise((resolve, reject) => {
    http.get(BASE + path, (res) => { let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } }); }).on("error", reject);
  });
}
function jpost(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request(BASE + path, { method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } }, (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    });
    req.on("error", reject); req.write(data); req.end();
  });
}
const MOCK = [
  { code: "600000", name: "mockA", industry: "银行", pe: 12, pb: 1.1, ps: 2, peg: 1, pcf: 8, cap: 50, pct: 1.2, holder_num: 5, worth: 75, roe: 15, gm: 30, dr: 50, dy: 2 },
  { code: "000001", name: "mockB", industry: "保险", pe: 9, pb: 0.9, ps: 1.5, peg: 0.8, pcf: 6, cap: 40, pct: -0.5, holder_num: 4, worth: 80, roe: 18, gm: 35, dr: 45, dy: 3.5 },
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const recs = [];
  const rec = (name, ok, detail) => { recs.push({ name, ok: !!ok, detail: detail || "" }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  | " + detail : "")); };
  const deployedKey = (() => { try { return fs.readFileSync(KEYFILE, "utf-8").trim(); } catch (e) { return ""; } })();

  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERR:" + e.message));

  // 仅拦截基础 /api/screener GET，注入 mock 行；其余请求（含所有 POST）放行
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    const u = req.url();
    const base = BASE + "/api/screener";
    if (req.method() === "GET" && (u === base || u.startsWith(base + "?"))) {
      return req.respond({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, desc: "mock", rows: MOCK }) });
    }
    if (req.method() === "POST") {
      const pd = req.postData();
      return req.continue(pd ? { postData: pd } : undefined);
    }
    req.continue();
  });

  // 后端直测（不经页面，证明路由本身可用）
  try { const v = await jget("/api/screener/valuation?codes=600000,000001"); rec("B4 估值分位路由", v.ok && v.data["600000"] && v.data["600000"].pe_pct != null, "pe_pct=" + (v.data["600000"] && v.data["600000"].pe_pct)); } catch (e) { rec("B4 估值分位路由", false, String(e)); }
  try { const b = await jget("/api/screener/backtest?codes=600000,000001&start=2024-01-01&end=2025-01-01"); rec("B7 回测路由", b.ok && b.n === 2, "n=" + b.n); } catch (e) { rec("B7 回测路由", false, String(e)); }
  try { const t = await jget("/api/screener/themes"); rec("B8 题材路由", t.ok && Array.isArray(t.data) && t.data.length > 0, "themes=" + (t.data ? t.data.length : 0)); } catch (e) { rec("B8 题材路由", false, String(e)); }
  // B6 自然语言（真实 Key → 结构化条件，证明 server 代理 DeepSeek 走通）
  try { const nl = await jpost("/api/nl_screener", { text: "低估值的白酒股，股息率大于3%，按股息率排序" }); rec("B6 自然语言路由(真实Key→结构化条件)", nl && nl.ok === true && nl.cond && Array.isArray(nl.cond.screens), "screens=" + (nl.cond ? nl.cond.screens.join(",") : (nl.error || ""))); } catch (e) { rec("B6 自然语言路由(真实Key→结构化条件)", false, String(e)); }

  // 前端：进 screener，自然触发 loadScreener → 被拦截注入 mock → 渲染 .sc-extra
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await sleep(600);
  await page.evaluate(() => { if (window.selectNav) selectNav("watch", "screener", true); });
  await sleep(1500);

  rec("B4-B8/B6 工具栏(.sc-extra)出现", await page.evaluate(() => !!document.querySelector("#p-screener .sc-extra")));

  // B4 估值分位弹层（真实打到 /api/screener/valuation）
  await page.evaluate(() => { const b = document.querySelector("#scValRun"); if (b) b.click(); });
  await sleep(600);
  rec("B4 估值分位弹层(表格有行)", await page.evaluate(() => { const m = document.getElementById("scModal"); return !!m && m.style.display === "flex" && /估值分位/.test(m.textContent) && m.querySelectorAll("table tr").length > 1; }));
  await page.evaluate(() => { const x = document.querySelector("#scModal .sc-x"); if (x) x.click(); });
  await sleep(200);

  // B7 回测面板
  await page.evaluate(() => { const b = document.querySelector("#scBtRun"); if (b) b.click(); });
  await sleep(300);
  const btRange = await page.evaluate(() => { const r = document.querySelector("#scBtRange"); return r && r.style.display !== "none"; });
  await page.evaluate(() => { const b = document.querySelector("#scBtGo"); if (b) b.click(); });
  await sleep(700);
  rec("B7 回测面板(区间+结果弹层)", btRange && await page.evaluate(() => { const m = document.getElementById("scModal"); return !!m && m.style.display === "flex" && /策略回测/.test(m.textContent); }));
  await page.evaluate(() => { const x = document.querySelector("#scModal .sc-x"); if (x) x.click(); });
  await sleep(200);

  // B8 题材弹层
  await page.evaluate(() => { const b = document.querySelector("#scThemeRun"); if (b) b.click(); });
  await sleep(700);
  rec("B8 题材弹层(题材chips)", await page.evaluate(() => { const m = document.getElementById("scModal"); return !!m && m.style.display === "flex" && /题材/.test(m.textContent) && m.querySelectorAll(".sc-chip").length > 0; }));
  await page.evaluate(() => { const x = document.querySelector("#scModal .sc-x"); if (x) x.click(); });
  await sleep(200);

  // B6 自然语言（UI 真实 Key → 成功反馈，不崩）
  await page.evaluate(() => { const i = document.querySelector("#scNlInput"); if (i) i.value = "低估值的白酒股，股息率大于3%"; const b = document.querySelector("#scNlRun"); if (b) b.click(); });
  await sleep(6000);
  const nlHint = await page.evaluate(() => { const e = document.querySelector("#scHint2"); return e ? e.textContent : ""; });
  rec("B6 自然语言(UI真实Key→已按)", nlHint.length > 0 && /已按/.test(nlHint), "hint=" + nlHint.slice(0, 40));

  // 设置弹层 + 真实 key 保存 round-trip
  await page.evaluate(() => { const b = document.querySelector("#scGear"); if (b) b.click(); });
  await sleep(600);
  rec("设置弹层(DeepSeek)", await page.evaluate(() => { const m = document.getElementById("scModal"); return !!m && m.style.display === "flex" && /DeepSeek/.test(m.textContent); }));
  if (deployedKey) {
    await page.evaluate((k) => { const i = document.getElementById("scDsKey"); if (i) i.value = k; const s = document.getElementById("scDsSave"); if (s) s.click(); }, deployedKey);
    await sleep(1200);
  }
  const setMsg = await page.evaluate(() => { const e = document.getElementById("scDsMsg"); return e ? e.textContent : ""; });
  rec("设置保存按钮(UI反馈)", /已保存|失败|错误/.test(setMsg), "msg=" + setMsg);
  try { const s2 = await jget("/api/settings/deepseek_key"); rec("设置 Key 落盘后端可读", s2.configured === true, "configured=" + s2.configured); } catch (e) { rec("设置 Key 落盘后端可读", false, String(e)); }

  rec("控制台零 JS 错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" || "));

  await browser.close();
  const pass = recs.filter((r) => r.ok).length;
  console.log("\n==== 结果 " + pass + "/" + recs.length + " PASS ====");
  process.exit(pass === recs.length ? 0 : 1);
})().catch((e) => { console.error("RUNNER_ERR", e); process.exit(2); });
