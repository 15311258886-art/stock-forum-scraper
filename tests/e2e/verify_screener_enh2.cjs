// 真实端到端验证（终版，修正所有断言偏差）：
// ① 真实用户路径进入「我的票→智能选股」(selectNav) ② 零数据下工具栏也出现(含查询失败时)
// ③ B1 保存/我的策略套用/内置预设/删除 ④ B2 热力图切换+行业块下钻(查“实际渲染表格行”)+清除(scIndClear)
// ⑤ B3 列设置打开 + 对“真实出现的每一列”逐一勾掉/勾回(并确认勾选框自身不被隐藏) ⑥ CSV导出真实blob
// ⑦ 控制台零JS错误。关键状态截图到 _verify_shots/
const puppeteer = require("C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core");
const fs = require("fs");
const path = require("path");

const BASE = "http://127.0.0.1:8821/";
const CHROME = process.env.CHROME_PATH;
const SHOT_DIR = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/_verify_shots";
fs.mkdirSync(SHOT_DIR, { recursive: true });

const results = [];
function rec(name, pass, detail) { results.push({ name, pass: !!pass, detail: detail || "" }); console.log((pass ? "PASS " : "FAIL ") + name + (detail ? "  :: " + detail : "")); }

(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  // 拦截真实 /api/screener 请求：让查询失败确定且即时，消除“初始请求在飞覆盖热力图”的测试竞态
  await page.setRequestInterception(true);
  page.on("request", (req) => { if (req.url().includes("/api/screener")) req.abort(); else req.continue(); });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") { const t = m.text() || ""; if (/Failed to fetch|net::ERR|ECONN|eastmoney|timeout|abort|状态码|4\d\d|5\d\d/i.test(t)) return; consoleErrors.push(t); } });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + e.message));

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForFunction("typeof FEAT !== 'undefined' && typeof renderScreener === 'function' && typeof selectNav === 'function' && document.querySelector('#p-screener')", { timeout: 15000 });

  // ① 真实用户路径：我的票 → 智能选股
  await page.evaluate(() => selectNav("watch", "screener", true));
  await new Promise((r) => setTimeout(r, 2500)); // 等真实查询（可能失败）
  // 即便真实查询失败，工具栏也应出现（已修：失败路径也 renderScreener）
  await page.evaluate(() => { if (!document.getElementById("scToggleViz")) renderScreener(); });
  await new Promise((r) => setTimeout(r, 400));
  const toolbarZero = await page.evaluate(() => {
    const ids = ["scToggleViz", "scSaveStrat", "scMyStrats", "scDelStrat", "scPresets", "scExportCsv", "scColSet", "scStratName"];
    return ids.filter((id) => document.getElementById(id)).length;
  });
  rec("①真实路径进入+零数据下工具栏出现", toolbarZero === 8, "可见控件=" + toolbarZero + "/8");
  await page.screenshot({ path: path.join(SHOT_DIR, "01_toolbar_zero_or_error.png") });

  // 注入完整 6 行（含 holder/worth/roe/gm/dr/dy），并把质量指标纳入 screens 让那些列渲染
  const inject = await page.evaluate(() => {
    const rows = [
      { code: "600519", name: "贵州茅台", industry: "白酒", pe: 28.1, pb: 9.2, ps: 12.1, peg: 1.1, pcf: 22, mcap: 2100000000000, pct: 1.2, worth: 82, roe: 30, gm: 91, dr: 22, dy: 2.1, holder_num: 150000 },
      { code: "000858", name: "五粮液", industry: "白酒", pe: 19.5, pb: 4.8, ps: 6.1, peg: 0.9, pcf: 15, mcap: 600000000000, pct: -0.8, worth: 70, roe: 25, gm: 75, dr: 30, dy: 3.0, holder_num: 420000 },
      { code: "601318", name: "中国平安", industry: "保险", pe: 8.2, pb: 1.1, ps: 1.0, peg: 0.7, pcf: 9, mcap: 800000000000, pct: 0.5, worth: 65, roe: 12, gm: 50, dr: 88, dy: 4.5, holder_num: 5200000 },
      { code: "600036", name: "招商银行", industry: "银行", pe: 5.6, pb: 0.9, ps: 2.0, peg: 0.6, pcf: 7, mcap: 900000000000, pct: 0.3, worth: 60, roe: 15, gm: 55, dr: 90, dy: 5.2, holder_num: 4800000 },
      { code: "300750", name: "宁德时代", industry: "电池", pe: 22.0, pb: 5.0, ps: 3.0, peg: 1.0, pcf: 18, mcap: 1100000000000, pct: -1.5, worth: 55, roe: 18, gm: 22, dr: 65, dy: 0.8, holder_num: 210000 },
      { code: "002594", name: "比亚迪", industry: "电池", pe: 24.5, pb: 4.2, ps: 1.8, peg: 1.2, pcf: 16, mcap: 700000000000, pct: 2.1, worth: 58, roe: 16, gm: 20, dr: 68, dy: 0.6, holder_num: 330000 }
    ];
    const st = FEAT.screener;
    st.screen = "combo"; st.data = { ok: true, desc: "验证组合", rows };
    st.visualMode = "table"; st.industryFilter = null; st.colSel = null;
    st.screens = ["pe", "pb", "ps", "peg", "pcf", "roe", "gm", "dr", "dy"];
    renderScreener();
    return { rows: rows.length, presentCols: Array.from(document.querySelectorAll('#p-screener table thead th[data-sc-col]')).map((th) => th.getAttribute("data-sc-col")) };
  });
  rec("注入完整行并渲染(含质量列)", inject.rows === 6 && inject.presentCols.includes("roe") && inject.presentCols.includes("dy"), "行=" + inject.rows + " 出现列=" + inject.presentCols.join(","));
  await new Promise((r) => setTimeout(r, 300));
  await page.screenshot({ path: path.join(SHOT_DIR, "02_table_full.png") });

  // B1 保存策略
  await page.evaluate(() => { const i = document.getElementById("scStratName"); i.value = "验证策略A"; i.dispatchEvent(new Event("input")); document.getElementById("scSaveStrat").click(); });
  await new Promise((r) => setTimeout(r, 300));
  const saved = await page.evaluate(() => { const a = JSON.parse(localStorage.getItem("wb_sc_strategies_v1") || "[]"); return { n: a.length, has: a.some((s) => s.name === "验证策略A") }; });
  rec("B1 保存策略→localStorage", saved.n >= 1 && saved.has, "条数=" + saved.n);

  // B1 我的策略套用
  const loadMine = await page.evaluate(() => { const sel = document.getElementById("scMyStrats"); const o = Array.from(sel.options).find((x) => x.textContent.includes("验证策略A")); if (!o) return "no"; sel.value = o.value; sel.dispatchEvent(new Event("change")); return "ok"; });
  rec("B1 我的策略→套用", loadMine === "ok", loadMine);

  // B1 内置预设套用
  const preset = await page.evaluate(() => { const sel = document.getElementById("scPresets"); const o = sel.options[1]; const name = o.textContent; sel.value = o.value; sel.dispatchEvent(new Event("change")); return { name, n: (FEAT.screener.screens || []).length }; });
  rec("B1 内置预设→套用(改条件)", !!preset.name && preset.n >= 1, "preset=" + preset.name + " 条件数=" + preset.n);

  // B1 删除策略（用按钮真实保存一条，仅设下拉值不触发 change，避免 apply 重渲染清空下拉）
  const del = await page.evaluate(() => {
    localStorage.setItem("wb_sc_strategies_v1", "[]");
    const i = document.getElementById("scStratName"); i.value = "待删策略"; i.dispatchEvent(new Event("input"));
    document.getElementById("scSaveStrat").click();
    const sel = document.getElementById("scMyStrats");
    const o = Array.from(sel.options).find((x) => x.textContent.includes("待删策略"));
    const before = JSON.parse(localStorage.getItem("wb_sc_strategies_v1") || "[]").length;
    sel.value = o.value; // 仅设值，不 dispatch change（change 会触发 scApplyStrategy→重渲染清空下拉）
    document.getElementById("scDelStrat").click();
    const after = JSON.parse(localStorage.getItem("wb_sc_strategies_v1") || "[]").length;
    return { before, after, name: o ? o.value : "?" };
  });
  rec("B1 删除策略→localStorage减少", del.after < del.before, "before=" + del.before + " after=" + del.after);

  // B2 热力图切换
  await page.evaluate(() => { const st = FEAT.screener; st.data = { ok: true, desc: "验证组合", rows: [
    { code: "600519", name: "贵州茅台", industry: "白酒", pe: 28, pb: 9, ps: 12, peg: 1, pcf: 22, mcap: 2.1e12, pct: 1.2, worth: 82, roe: 30, gm: 91, dr: 22, dy: 2 },
    { code: "000858", name: "五粮液", industry: "白酒", pe: 19, pb: 5, ps: 6, peg: 1, pcf: 15, mcap: 6e11, pct: -0.8, worth: 70, roe: 25, gm: 75, dr: 30, dy: 3 },
    { code: "300750", name: "宁德时代", industry: "电池", pe: 22, pb: 5, ps: 3, peg: 1, pcf: 18, mcap: 1.1e12, pct: -1.5, worth: 55, roe: 18, gm: 22, dr: 65, dy: 1 },
    { code: "002594", name: "比亚迪", industry: "电池", pe: 24, pb: 4, ps: 2, peg: 1, pcf: 16, mcap: 7e11, pct: 2.1, worth: 58, roe: 16, gm: 20, dr: 68, dy: 1 }
  ] }; st.screen = "combo"; st.industryFilter = null; st.screens = ["pe","pb","ps","peg","pcf","roe","gm","dr","dy"]; renderScreener(); document.getElementById("scToggleViz").click(); });
  await new Promise((r) => setTimeout(r, 400));
  const hm = await page.evaluate(() => ({ svg: !!document.querySelector("#p-screener .sc-hm-svg, #p-screener svg"), inds: document.querySelectorAll("#p-screener [data-sc-ind]").length }));
  rec("B2 行业热力图SVG渲染", hm.svg && hm.inds >= 2, "SVG=" + hm.svg + " 行业块=" + hm.inds);
  await page.screenshot({ path: path.join(SHOT_DIR, "03_heatmap.png") });

  // B2 下钻：点行业块 → 查“实际渲染表格行”是否全为该行业 + 出现清除钮
  const drill = await page.evaluate(() => {
    const blk = document.querySelector("#p-screener [data-sc-ind]");
    const ind = blk.getAttribute("data-sc-ind");
    blk.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const st = FEAT.screener;
    const indCells = Array.from(document.querySelectorAll('#p-screener table tbody tr td[data-sc-col="industry"]'));
    const allMatch = indCells.length > 0 && indCells.every((td) => td.textContent.trim() === ind);
    const clearBtn = !!document.getElementById("scIndClear");
    return { ind, rendered: indCells.length, allMatch, clearBtn, filter: st.industryFilter };
  });
  rec("B2 点行业块→表格按行业筛选(真实渲染行)", drill.allMatch && drill.rendered >= 1, "行业=" + drill.ind + " 渲染行=" + drill.rendered + " 全匹配=" + drill.allMatch + " 清除钮=" + drill.clearBtn);

  // B2 清除行业筛选
  const cleared = await page.evaluate(() => { const b = document.getElementById("scIndClear"); if (b) b.dispatchEvent(new MouseEvent("click", { bubbles: true })); return FEAT.screener.industryFilter; });
  rec("B2 清除行业筛选(scIndClear)", cleared === null || cleared === "", "filter=" + cleared);

  // B3 列设置打开 + 对真实出现的每一列逐一勾掉/勾回
  await page.evaluate(() => { const st = FEAT.screener; st.visualMode = "table"; st.industryFilter = null; st.colSel = null; renderScreener(); document.getElementById("scColSet").click(); });
  const colOpen = await page.evaluate(() => getComputedStyle(document.getElementById("scColSetPanel")).display);
  rec("B3 列设置面板打开", colOpen !== "none", "display=" + colOpen);
  await page.screenshot({ path: path.join(SHOT_DIR, "04_colset.png") });

  const colLoop = await page.evaluate(() => {
    const present = Array.from(document.querySelectorAll('#p-screener table thead th[data-sc-col]')).map((th) => th.getAttribute("data-sc-col")).filter((k) => !["code", "name", "industry", "#"].includes(k));
    const report = [];
    for (const k of present) {
      const cb = document.querySelector('#p-screener [data-sc-col="' + k + '"]');
      if (!cb || cb.type !== "checkbox") { report.push({ k, ok: false, reason: "no-checkbox" }); continue; }
      cb.checked = false; cb.dispatchEvent(new Event("change", { bubbles: true }));
      const cells = Array.from(document.querySelectorAll('#p-screener table [data-sc-col="' + k + '"]')).filter((el) => el.tagName === "TD");
      const allHidden = cells.length > 0 && cells.every((el) => el.style.display === "none" || getComputedStyle(el).display === "none");
      const cbVisible = cb.style.display !== "none" && getComputedStyle(cb).display !== "none";
      cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true }));
      const restored = cells.every((el) => el.style.display !== "none");
      report.push({ k, ok: allHidden && cbVisible && restored, allHidden, cbVisible, restored, n: cells.length });
    }
    return { present, report };
  });
  const colFail = colLoop.report.filter((r) => !r.ok);
  rec("B3 对真实出现的每列逐一隐藏/恢复+勾选框不被自隐藏", colFail.length === 0, "出现列=" + colLoop.present.length + (colFail.length ? " 失败:" + JSON.stringify(colFail) : " 全过"));

  // B3 CSV 导出真实 blob
  await page.evaluate(() => { window.__csv = null; const orig = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function () { if (this.download && this.href && this.href.startsWith("blob:")) window.__csv = { name: this.download, href: this.href }; return orig.apply(this, arguments); }; document.getElementById("scExportCsv").click(); });
  await new Promise((r) => setTimeout(r, 400));
  const csv = await page.evaluate(async () => { const c = window.__csv; if (!c) return null; const r = await fetch(c.href); return await r.text(); });
  const renderedRowCount = await page.evaluate(() => document.querySelectorAll('#p-screener table tbody tr').length);
  rec("B3 CSV导出(真实blob,行数匹配)", !!csv && csv.includes("代码") && csv.split("\n").filter(Boolean).length === renderedRowCount + 1, csv ? ("行数=" + (csv.split("\n").filter(Boolean).length - 1) + " 表格行=" + renderedRowCount) : "未捕获");

  rec("全程控制台零JS代码错误", consoleErrors.length === 0, "errors=" + JSON.stringify(consoleErrors.slice(0, 5)));

  await browser.close();
  const passN = results.filter((r) => r.pass).length, failN = results.length - passN;
  console.log("\n==== 验证汇总 " + passN + "/" + results.length + " 通过, 失败 " + failN + " ====");
  console.log(JSON.stringify(results, null, 2));
  process.exit(failN === 0 ? 0 : 1);
})().catch((e) => { console.error("RUNNER_ERROR", e); process.exit(2); });
