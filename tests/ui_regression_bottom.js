/* ============================================================================
 * 长期底部埋伏面板 · 回归断言（2026-10-21 dy10）
 * ----------------------------------------------------------------------------
 * 验证进哥 2026-10-21 需求：
 *   ① 导航「低位突破」下出现「长期底部埋伏」子项，且能点开
 *   ② 面板有真实数据（不是空白/加载中），表头含 底部时长 / 平台底 / 底部抬升
 *   ③ 分档按钮 6 个（全部/≥1/≥2/≥3/≥5/≥8 年），点击「≥5年」后所有行的时长 ≥5
 *   ④ 「状态」切到「仅刚启动」后，每行都带「已启动」字样
 *   ⑤ 涨红跌绿铁律：距峰值(负数)=绿、近1年(正数)=红，一处反用即失败
 *   ⑥ 零 JS 运行时错误 / 零 console.error
 * 真实 Chromium + puppeteer-core，只读 GET（不改任何数据）。
 * 用法：node tests/ui_regression_bottom.js [port]
 * ========================================================================== */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8765";
const BASE = "http://127.0.0.1:" + PORT;
const CHROMIUM_DIR = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

function pickChrome() {
  try {
    const dirs = fs.readdirSync(CHROMIUM_DIR).filter((d) => d.startsWith("chromium-")).sort().reverse();
    for (const d of dirs) {
      for (const sub of ["chrome-win64", "chrome-win"]) {
        const p = path.join(CHROMIUM_DIR, d, sub, "chrome.exe");
        if (fs.existsSync(p)) return p;
      }
    }
  } catch (e) { /* ignore */ }
  return EDGE;
}

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log((pass ? "  PASS " : "  FAIL ") + name + (detail ? "  → " + detail : ""));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 轮询直到条件成立：后台全市场重扫会短时独占 CPU（纯 Python 持 GIL），
// 接口响应会从 ~0.1s 拖到数秒，固定 sleep 取样会假失败。
async function waitFor(fn, ms = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if (await page.evaluate(fn)) return true; } catch (_) {}
    await sleep(400);
  }
  return false;
}

(async () => {
  const pageErrors = [];
  const consoleErrors = [];
  const browser = await puppeteer.launch({
    executablePath: pickChrome(), headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-proxy-server", "--proxy-bypass-list=*"] ,
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1760, height: 1100 });
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 200)));

  try {
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.querySelector("#sidenav") && document.querySelectorAll("#sidenav *").length > 3,
      { timeout: 30000 });

    // ① 面板入口：hash 深链路 #watch/lowbox_bottom（本工程导航为一级折叠，二级用 hash 路由进入，
    //    与 verify_lowbox.cjs 的 #watch/lowbox 一致）；同时校验渲染函数已注册
    const fnOk = await page.evaluate(() => typeof window.loadBottom === "function");
    await page.evaluate(() => { location.hash = "#watch/lowbox_bottom"; });
    await sleep(2000);
    const navOk = fnOk && await page.evaluate(() => !!document.querySelector("#p-lowbox_bottom"));
    rec("① 入口可达（#watch/lowbox_bottom + loadBottom 已注册 + 面板挂载）", navOk);
    if (!navOk) throw new Error("入口不可达，后续断言无法进行");

    // ①b 面板标题含板块名（确认是正确的面板，不是空白壳）
    const titleOk = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      return p && (p.textContent || "").indexOf("长期底部埋伏") >= 0;
    });
    rec("①b 面板标题为「长期底部埋伏」", titleOk);

    // ② 面板渲染出真实表格
    const gotTable = await page.waitForFunction(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      if (!p) return false;
      const tb = p.querySelector("table.wt-tb");
      return tb && tb.querySelectorAll("tbody tr").length > 0;
    }, { timeout: 60000, polling: 300 }).then(() => true).catch(() => false);
    rec("② 面板渲染出数据表格", gotTable);
    if (!gotTable) {
      await page.screenshot({ path: path.join(__dirname, "shot_bottom_fail.png"), fullPage: false });
      throw new Error("表格未渲染");
    }

    const head = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      return Array.from(p.querySelectorAll("table.wt-tb thead th")).map((t) => (t.textContent || "").replace(/\s+/g, " ").trim());
    });
    rec("②b 表头含关键列", ["底部时长", "平台底", "平台顶", "底部抬升", "距峰值", "箱内位置"].every((k) => head.some((h) => h.includes(k))),
      head.join(" | ").slice(0, 150));

    // ③ 分档按钮
    const tierBtns = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      return Array.from(p.querySelectorAll("[data-bt-tier]")).map((b) => b.dataset.btTier);
    });
    rec("③ 分档按钮 6 档", tierBtns.length === 6, tierBtns.join(","));

    const before = await page.evaluate(() => document.querySelectorAll("#p-lowbox_bottom table.wt-tb tbody tr").length);
    await page.evaluate(() => {
      const b = document.querySelector('#p-lowbox_bottom [data-bt-tier="5"]');
      if (b) b.click();
    });
    await waitFor(() => {
      const rows = Array.from(document.querySelectorAll("#p-lowbox_bottom table.wt-tb tbody tr"));
      return rows.length > 0 && rows.every((tr) => Number((tr.children[3] || {}).textContent || 0) >= 5);
    }, 40000);
    const tier5 = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      const rows = Array.from(p.querySelectorAll("table.wt-tb tbody tr"));
      return rows.map((tr) => Number((tr.children[3] || {}).textContent || 0));
    });
    const allGe5 = tier5.length > 0 && tier5.every((v) => v >= 5);
    rec("③b 点「≥5年」后所有行底部时长 ≥5", allGe5, "行数 " + tier5.length + "，样例 " + tier5.slice(0, 6).join(",") + "（点击前 " + before + " 行）");

    // ④ 状态筛选：仅刚启动
    await page.evaluate(() => {
      const s = document.querySelector("#btState");
      if (s) { s.value = "started"; s.dispatchEvent(new Event("change")); }
    });
    await waitFor(() => {
      const rows = Array.from(document.querySelectorAll("#p-lowbox_bottom table.wt-tb tbody tr"));
      return rows.length > 0 && rows.every((tr) => ((tr.children[15] || {}).textContent || "").indexOf("已启动") >= 0);
    }, 40000);
    const startedTxt = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      const rows = Array.from(p.querySelectorAll("table.wt-tb tbody tr"));
      return { n: rows.length, states: rows.map((tr) => ((tr.children[15] || {}).textContent || "").trim()) };
    });
    rec("④ 「仅刚启动」筛选后每行都是已启动", startedTxt.n > 0 && startedTxt.states.every((s) => s.indexOf("已启动") >= 0),
      "行数 " + startedTxt.n + "，样例 " + startedTxt.states.slice(0, 4).join(" / "));

    // 复位到全部状态，供配色检查
    await page.evaluate(() => {
      const s = document.querySelector("#btState");
      if (s) { s.value = "all"; s.dispatchEvent(new Event("change")); }
    });
    await waitFor(() => document.querySelectorAll("#p-lowbox_bottom table.wt-tb tbody tr").length > 0, 40000);
    await sleep(500);   // 等表格重渲染稳定再做配色取样

    // ⑤ 涨红跌绿：距峰值(第10列,index9)=负→绿；近1年(第14列,index13)=正→红
    const colorCheck = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      const rows = Array.from(p.querySelectorAll("table.wt-tb tbody tr"));
      const GREEN = "rgb(10, 125, 59)";
      const RED = "rgb(192, 57, 43)";
      const bad = [];
      let nDd = 0, n1y = 0;
      for (const tr of rows) {
        const ddCell = tr.children[9];
        if (ddCell) {
          const v = parseFloat((ddCell.textContent || "").replace(/[^\-0-9.]/g, ""));
          const c = getComputedStyle(ddCell).color;
          if (isFinite(v) && v < 0) {
            nDd++;
            if (c !== GREEN) bad.push("距峰值 " + v + "% 显示 " + c + "（应为绿）");
          }
        }
        const y1Cell = tr.children[13];
        if (y1Cell) {
          const v = parseFloat((y1Cell.textContent || "").replace(/[^\-0-9.]/g, ""));
          const c = getComputedStyle(y1Cell).color;
          if (isFinite(v) && v > 0) {
            n1y++;
            if (c !== RED) bad.push("近1年 " + v + "% 显示 " + c + "（应为红）");
          }
        }
      }
      return { bad: bad.slice(0, 5), nDd, n1y, nBad: bad.length };
    });
    rec("⑤ 涨红跌绿（距峰值负=绿 / 近1年正=红）", colorCheck.nBad === 0,
      "校验 距峰值 " + colorCheck.nDd + " 格 / 近1年 " + colorCheck.n1y + " 格；反用 " + colorCheck.nBad + " 处" +
      (colorCheck.bad.length ? "：" + colorCheck.bad.join("；") : ""));

    // 截图留证
    await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      if (p) p.scrollIntoView();
    });
    await sleep(500);
    const shot = path.join(__dirname, "shot_bottom_panel.png");
    await page.screenshot({ path: shot, fullPage: false });
    console.log("  [shot] " + shot);

    // ⑥ 零报错
    rec("⑥ 零 JS 运行时错误", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    rec("⑥b 零 console.error", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
  } catch (e) {
    rec("执行异常", false, String(e).slice(0, 220));
  } finally {
    await browser.close();
  }

  const pass = results.filter((r) => r.pass).length;
  console.log("\n==== " + pass + "/" + results.length + " 通过 ====");
  process.exit(pass === results.length ? 0 : 1);
})();
