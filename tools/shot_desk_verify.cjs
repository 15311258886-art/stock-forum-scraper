/* 决策台改造验收截图（2026-11-02）。
 *
 * 验两件事，都必须"真渲染出来看到"才算数：
 *   ① 热点页多了一块「AI 独立票单」，且里面真有票（不是空壳）；
 *   ② 超跌页的理由已从「60日分位+末日收阳」换成「阴跌 N 日 + 企稳档」。
 *
 * 用法：node tools/shot_desk_verify.cjs
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => fs.existsSync(p));

const BASE = "http://127.0.0.1:8765";
/* 决策台四个面板的 DOM 是同时存在的（只切显示），document.querySelector(".dk-hero")
   拿到的是第一个（热点页的）。验收必须取"当前可见"那一个，否则每页都在验热点。
   这个取法内联在下面每个 page.evaluate 里（不能引用 Node 侧变量）。 */
const ROOT = path.join(__dirname, "..");

async function shot(page, hash, out, waitSel) {
  const errs = [];
  page.removeAllListeners("pageerror");
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  await page.goto(BASE + "/#" + hash, { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask");
    if (m) m.style.display = "none";
    const fm = document.querySelector("#firstRunModal");
    if (fm) fm.style.display = "none";
    document.querySelectorAll(".modal-mask.show").forEach((x) => x.classList.remove("show"));
  });
  try {
    await page.waitForSelector(waitSel, { timeout: 60000 });
  } catch (e) {
    errs.push("wait 超时: " + waitSel);
  }
  await new Promise((r) => setTimeout(r, 2500));
  await page.screenshot({ path: path.join(ROOT, out), fullPage: true });
  return errs;
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1400"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1400, deviceScaleFactor: 1 });

  // —— 热点页：AI 独立票单 ——
  const e1 = await shot(page, "decision/desk_hot", "_verify_hot.png", ".dk-sec");
  const hot = await page.evaluate(() => {
    const secs = [...document.querySelectorAll(".dk-sec")];
    const ai = secs.find((s) => (s.textContent || "").includes("AI 独立票单"));
    return {
      sections: secs.map((s) => (s.querySelector(".dk-sec-h") || {}).textContent || "").slice(0, 8),
      hasAiBlock: !!ai,
      aiRows: ai ? ai.querySelectorAll("tbody tr").length : 0,
      aiText: ai ? (ai.textContent || "").slice(0, 260) : "",
      heroTitle: (() => { const hs = [...document.querySelectorAll('.dk-hero')].filter((h) => h.offsetParent !== null); return hs.length ? (hs[hs.length - 1].textContent || '') : ((document.querySelector('.dk-hero') || {}).textContent || ''); })(),
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
    };
  });

  // —— 超跌页：新口径 ——
  const e2 = await shot(page, "decision/desk_dip", "_verify_dip.png", ".dk-sec");
  const dip = await page.evaluate(() => {
    const t = document.body.textContent || "";
    return {
      hasYinDie: t.includes("阴跌"),
      hasLevel: t.includes("企稳档"),
      hasOldText: t.includes("末日收阳"),
      heroTitle: (() => { const hs = [...document.querySelectorAll('.dk-hero')].filter((h) => h.offsetParent !== null); return hs.length ? (hs[hs.length - 1].textContent || '') : ((document.querySelector('.dk-hero') || {}).textContent || ''); })(),
      rows: document.querySelectorAll(".dk-tbl tbody tr").length,
    };
  });

  // —— 中线页：全市场谷底磨盘（2026-11-02 D65）——
  const e3 = await shot(page, "decision/desk_mid", "_verify_mid.png", ".dk-sec");
  const mid = await page.evaluate(() => {
    const t = document.body.textContent || "";
    const secs = [...document.querySelectorAll(".dk-sec")];
    const box = secs.find((s) => (s.textContent || "").includes("谷底磨盘"));
    const heads = box
      ? [...box.querySelectorAll("thead th")].map((th) => (th.textContent || "").trim())
      : [];
    const rowCount = box ? box.querySelectorAll("tbody tr").length : 0;
    const firstRow = box && box.querySelector("tbody tr")
      ? [...box.querySelector("tbody tr").children].map((td) => (td.textContent || "").trim())
      : [];
    return {
      sections: secs.map((s) => (s.querySelector(".dk-sec-h") || {}).textContent || "").slice(0, 8),
      hasBoxBlock: !!box,
      heads,
      rowCount,
      firstRow,
      hasDwell: t.includes("谷底驻留"),
      hasRun: t.includes("最长连续"),
      saysWholeMarket: t.includes("全市场"),
      hasOldOnly: !t.includes("低位突破"),
      heroTitle: (() => { const hs = [...document.querySelectorAll('.dk-hero')].filter((h) => h.offsetParent !== null); return hs.length ? (hs[hs.length - 1].textContent || '') : ((document.querySelector('.dk-hero') || {}).textContent || ''); })(),
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
    };
  });

  console.log(JSON.stringify({ hot, dip, mid, errors: [...e1, ...e2, ...e3] }, null, 2));
  await browser.close();
})().catch((e) => {
  console.error("FATAL", e.message);
  process.exit(1);
});
