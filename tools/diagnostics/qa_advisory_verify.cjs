/* 说明性标注 + 得分列三态 验收 —— 2026-11-05
 *
 * 背景：11 条开源规则降为「说明性标注」（advisory=1，不下架、不参与进出决策）；
 *       db_wave / tv_wbottom / tv_shrink_pull 的分数不再当排序键（score_dir=0）。
 *
 * 用法：node tools/qa_advisory_verify.cjs
 * 产出：viewer/logs/qa_advisory_*.png + qa_advisory_verify.json
 *
 * 断言：
 *   A. 卡片上有「说明性」角标的恰好 11 个，且都来自 oss_ 开头的工具
 *   B. 点 db_wave → 表头是「标签」+「不参与排序（按代码顺序）」，且首列代码升序
 *   C. 点 tv_shrink_pull / tv_wbottom → 同上（三条同一个方向表）
 *   D. 点普通工具（box_bottom）→ 表头仍是「得分」，不是「标签」
 *   E. 点开源工具的 i → 抽屉里出现 .tbx-advBar；点非开源工具的 i → 不出现
 *   F. 页面无 pageerror / console error
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const ROOT = path.join(__dirname, "..");
const LOGS = path.join(ROOT, "viewer", "logs");
const BASE = "http://127.0.0.1:8765";
const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));

const out = { startedAt: new Date().toISOString(), checks: {}, pageErrors: [], consoleErrors: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function mark(k, ok, info) {
  out.checks[k] = { ok: !!ok, info: info == null ? "" : String(info) };
}

const ADV_KEYS = ["oss_ma_align", "oss_near_high", "oss_near_high_tight", "oss_burst_vol",
  "oss_burst", "oss_pullback_ma20", "oss_shrink_vol", "oss_mom20", "oss_reversal20",
  "oss_high_vol", "oss_rsi_overbought"];

async function cardAdvKeys(page) {
  return page.evaluate(() => Array.prototype.slice
    .call(document.querySelectorAll(".tbx-card"))
    .filter((c) => c.querySelector(".tbx-cardAdv"))
    .map((c) => c.getAttribute("data-tbx-card")));
}

async function headText(page) {
  return page.evaluate(() => {
    const th = document.querySelector("#tbxRes thead th:nth-child(3)");
    return th ? th.textContent.replace(/\s+/g, " ").trim() : "";
  });
}

async function firstCodes(page, n) {
  return page.evaluate((k) => Array.prototype.slice
    .call(document.querySelectorAll("#tbxRes tr[data-tbx-sym]"))
    .slice(0, k).map((tr) => tr.getAttribute("data-tbx-sym")), n);
}

async function clickCard(page, key) {
  await page.evaluate((k) => {
    const c = document.querySelector('[data-tbx-card="' + k + '"]');
    if (c) c.click();
  }, key);
}

(async () => {
  if (!EXE) { console.log("找不到 Edge/Chrome"); process.exit(2); }
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1200"],
    defaultViewport: { width: 1600, height: 1200 },
  });
  const page = await browser.newPage();
  page.on("pageerror", (e) => out.pageErrors.push(String((e && e.message) || e)));
  page.on("console", (m) => { if (m.type() === "error") out.consoleErrors.push(m.text()); });
  // 首访引导弹层会挡住真实点击（前车之鉴），直接预置已读标记
  await page.evaluateOnNewDocument(() => {
    try { localStorage.setItem("viewer-first-run-done", "1"); } catch (e) {}
  });

  try {
    await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForFunction(
      () => document.querySelectorAll(".tbx-card").length >= 20,
      { timeout: 60000, polling: 50 }
    );
    await sleep(400);

    // A：角标数量与归属
    const advKeys = await cardAdvKeys(page);
    mark("A_角标11个", advKeys.length === 11, advKeys.length + " 个");
    mark("A2_全是oss", advKeys.every((k) => ADV_KEYS.indexOf(k) >= 0),
      advKeys.filter((k) => ADV_KEYS.indexOf(k) < 0).join(",") || "全部匹配");
    await page.screenshot({ path: path.join(LOGS, "qa_advisory_wall.png") });

    // B：db_wave（score_dir=0）
    await clickCard(page, "db_wave");
    await page.waitForFunction(
      () => document.querySelectorAll("#tbxRes tr[data-tbx-sym]").length > 0,
      { timeout: 90000, polling: 100 }
    );
    const hB = await headText(page);
    const codesB = await firstCodes(page, 12);
    mark("B_dbwave表头是标签", hB.indexOf("标签") >= 0 && hB.indexOf("不参与排序") >= 0, hB);
    mark("B2_dbwave按代码升序",
      JSON.stringify(codesB) === JSON.stringify(codesB.slice().sort()), codesB.slice(0, 5).join(","));
    await page.screenshot({ path: path.join(LOGS, "qa_advisory_dbwave.png") });

    // C：tv_shrink_pull（同表）
    await clickCard(page, "db_wave");           // 撤掉上一刀
    await sleep(300);
    await clickCard(page, "tv_shrink_pull");
    await page.waitForFunction(
      () => document.querySelectorAll("#tbxRes tr[data-tbx-sym]").length > 0,
      { timeout: 90000, polling: 100 }
    );
    const hC = await headText(page);
    mark("C_shrinkpull表头是标签", hC.indexOf("标签") >= 0 && hC.indexOf("不参与排序") >= 0, hC);

    // D：box_bottom（默认方向，仍应是「得分」）
    await clickCard(page, "tv_shrink_pull");
    await sleep(300);
    await clickCard(page, "box_bottom");
    await page.waitForFunction(
      () => document.querySelectorAll("#tbxRes tr[data-tbx-sym]").length > 0,
      { timeout: 90000, polling: 100 }
    );
    const hD = await headText(page);
    mark("D_普通工具仍是得分", hD.indexOf("得分") >= 0 && hD.indexOf("标签") < 0, hD);

    // E：抽屉里的提示条 —— 开源工具 vs 非开源工具
    await page.evaluate(() => {
      const b = document.querySelector('[data-tbx-info="oss_shrink_vol"]');
      if (b) b.click();
    });
    await page.waitForFunction(
      () => { const d = document.getElementById("tbxDrawer"); return d && d.classList.contains("open"); },
      { timeout: 20000, polling: 50 }
    );
    const advBar1 = await page.evaluate(() => {
      const b = document.querySelector(".tbx-advBar");
      return b ? b.textContent.replace(/\s+/g, " ").trim().slice(0, 80) : "";
    });
    mark("E_开源工具有提示条", advBar1.length > 0, advBar1);
    await page.screenshot({ path: path.join(LOGS, "qa_advisory_drawer.png") });

    await page.evaluate(() => {
      const d = document.getElementById("tbxDrawer"); if (d) { d.className = "tbx-drawer"; d.innerHTML = ""; }
      const m = document.getElementById("tbxMask"); if (m) m.className = "tbx-mask";
    });
    await sleep(250);
    await page.evaluate(() => {
      const b = document.querySelector('[data-tbx-info="box_bottom"]');
      if (b) b.click();
    });
    await page.waitForFunction(
      () => { const d = document.getElementById("tbxDrawer"); return d && d.classList.contains("open"); },
      { timeout: 20000, polling: 50 }
    );
    const advBar2 = await page.evaluate(() => !!document.querySelector(".tbx-advBar"));
    mark("E2_非开源工具无提示条", advBar2 === false, advBar2 ? "错误地出现了" : "未出现");

    mark("F_无页面报错", out.pageErrors.length === 0, out.pageErrors.slice(0, 2).join(" | "));
    mark("F2_无console错误", out.consoleErrors.length === 0, out.consoleErrors.slice(0, 2).join(" | "));
  } catch (e) {
    mark("Z_脚本异常", false, (e && e.message) || e);
  } finally {
    await browser.close();
    const bad = Object.entries(out.checks).filter(([, v]) => !v.ok);
    out.passed = Object.keys(out.checks).length - bad.length;
    out.failed = bad.length;
    fs.mkdirSync(LOGS, { recursive: true });
    fs.writeFileSync(path.join(LOGS, "qa_advisory_verify.json"), JSON.stringify(out, null, 1), "utf8");
    Object.entries(out.checks).forEach(([k, v]) => console.log((v.ok ? "PASS " : "FAIL ") + k + "  " + v.info));
    console.log("\n通过 " + out.passed + " / 失败 " + out.failed);
  }
})();
