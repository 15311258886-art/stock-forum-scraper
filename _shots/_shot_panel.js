/* 2026-10-05 目检截图：入手概率面板整块（新两列 + 时间轴 vs 买点）。 */
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const OUT = path.join(__dirname, "..", "_shots_rate");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1200, deviceScaleFactor: 1 });
  await page.goto("http://127.0.0.1:8765/#watch/rate", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 1200));
  await page.evaluate(() => { const n = document.querySelector('.nav-item[data-cat="watch"]'); if (n) n.click(); });
  await new Promise((r) => setTimeout(r, 500));
  await page.evaluate(() => { const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]'); if (t) t.click(); });
  // 轮询等表格，再等时间轴（它要走 K 线）
  for (let i = 0; i < 40; i++) {
    const ok = await page.evaluate(() => document.querySelectorAll("#p-rate tbody tr .bp-in").length > 0);
    if (ok) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  for (let i = 0; i < 60; i++) {
    const ok = await page.evaluate(() => document.querySelectorAll("#p-rate .tl-card").length > 0);
    if (ok) break;
    await new Promise((r) => setTimeout(r, 800));
  }
  // 展开第一张时间轴卡片，才看得到 T+0 的「vs 买点」
  await page.evaluate(() => {
    const h = document.querySelector("#p-rate .tl-head");
    if (h && !document.querySelector("#p-rate .tl-card.open")) h.click();
  });
  await new Promise((r) => setTimeout(r, 600));
  const h = await page.$("#p-rate");
  await h.screenshot({ path: path.join(OUT, "F-rate-panel-bp.png") });
  console.log("shot F-rate-panel-bp.png");
  await browser.close();
})().catch((e) => { console.log("FATAL " + e); process.exit(1); });
