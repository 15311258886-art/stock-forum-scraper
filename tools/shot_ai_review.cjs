/* 截图 AI 复盘页（2026-11-02 改版验收用）。
 *
 * 为什么要单独一个脚本：改的是「首屏是什么」这件事 —— 光看 DOM 节点数、
 * 跑接口断言都证明不了"他打开就看到票单"。必须真的渲染出来看一眼。
 *
 * 用法：node tools/shot_ai_review.cjs [out.png]
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => fs.existsSync(p));

const OUT = process.argv[2] || path.join(__dirname, "..", "_shot_ai_review.png");
const BASE = "http://127.0.0.1:8765";

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1200"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(BASE + "/#decision/ai_review", { waitUntil: "networkidle2", timeout: 60000 });
  // 首屏遮罩 / 引导层（旧界面踩过的坑：不隐藏就截到一片糊）
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask");
    if (m) m.style.display = "none";
    // 首次引导是「modal」不是「mask 里的内容」—— 只藏 mask 会截到一个大白板压在正中间。
    const fm = document.querySelector("#firstRunModal");
    if (fm) fm.style.display = "none";
    document.querySelectorAll(".modal-mask.show").forEach((x) => x.classList.remove("show"));
    document.querySelectorAll("[class*=welcome],[id*=welcome]").forEach((x) => { x.style.display = "none"; });
  });
  // 等票单真的画出来（不是等固定时长：表格没出来就截图等于自欺）
  try {
    await page.waitForSelector(".air-picks tbody tr, .air-kpis", { timeout: 45000 });
  } catch (e) {
    errs.push("wait: 票单/成绩单未在 45s 内出现");
  }
  await new Promise((r) => setTimeout(r, 1200));

  const info = await page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    return {
      hasPicksTable: !!q(".air-picks"),
      pickRows: document.querySelectorAll(".air-picks tbody tr").length,
      kpis: document.querySelectorAll(".air-kpi").length,
      fold: !!q(".air-fold"),
      dateSel: !!q(".air-date"),
      docW: document.documentElement.scrollWidth,
      bodyH: document.body.scrollHeight,
      firstCardTitle: (q(".air-card-hd b") || {}).textContent || "",
      chips: Array.from(document.querySelectorAll(".air-chip")).slice(0, 6).map((x) => x.textContent.trim()),
    };
  });

  await page.screenshot({ path: OUT, fullPage: false });
  await page.screenshot({ path: OUT.replace(/\.png$/, "_full.png"), fullPage: true });
  console.log(JSON.stringify({ out: OUT, info, errs }, null, 1));
  await browser.close();
})().catch((e) => {
  console.error("FAILED:", e && e.stack || e);
  process.exit(1);
});
