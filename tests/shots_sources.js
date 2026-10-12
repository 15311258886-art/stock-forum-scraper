/**
 * 触发真实联网采集并截图（数据源接入状态 + 热点）。
 * 用法: node tests/shots_sources.js [port]
 *
 * 安全：默认只读。数据准备走 _guard.seed({soft:true})——只有「端口非 8765 且
 * ALLOW_DATA_RESET=1」才会点击 #btnOverseas（POST /api/overseas 会清空海外数据
 * 并联网重抓）；否则跳过采集、对现有数据出图，且不 exit 3。
 * 详见 tests/_guard.js。
 */
const puppeteer = require("puppeteer-core");
const { PORT, DESTRUCTIVE_OK, announce, seed } = require("./_guard");

const CHROME = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}/`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  announce();
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1.6 });
  await page.goto(URL, { waitUntil: "networkidle2" });

  if (DESTRUCTIVE_OK) {
    console.log("→ 触发联网采集（境外源实时抓取，约 30-60s）...");
  } else {
    console.log("→ 只读模式：跳过 POST /api/overseas（不联网重抓、不清空海外数据），改用现有数据出图");
  }
  const mode = await seed(page, {
    sel: "#btnOverseas",
    ready: async () => (await page.$$eval("#p-overseas_sentiment .ov-src-status", (e) => e.length).catch(() => 0)) > 0,
    timeoutMs: 200000,
    soft: true,
  });
  if (mode === "missing") {
    console.log("→ 现有数据中未见数据源接入状态卡片，仍继续出图（可能为空白图）");
  }

  const srcOn = await page.$$eval("#p-overseas_sentiment .ov-src-status.src-on", (e) => e.length).catch(() => 0);
  console.log("→ 已接入源 ≥15:", srcOn >= 15);

  await page.click('#tabs .tab[data-tab="overseas_sentiment"]');
  await wait(1800);
  await page.screenshot({ path: "full_sources_live.png", fullPage: true });

  const grid = await page.$("#p-overseas_sentiment .ov-src-grid");
  if (grid) await grid.screenshot({ path: "sources_grid.png" }).catch(() => {});

  await page.click('#tabs .tab[data-tab="overseas_hotspots"]');
  await wait(1500);
  await page.screenshot({ path: "full_hotspots_live.png", fullPage: true });

  const stats = await page.evaluate(() => ({
    on: document.querySelectorAll("#p-overseas_sentiment .ov-src-status.src-on").length,
    demo: document.querySelectorAll("#p-overseas_sentiment .ov-src-status.src-demo").length,
    wait: document.querySelectorAll("#p-overseas_sentiment .ov-src-status.src-wait").length,
    cards: document.querySelectorAll("#p-overseas_hotspots .ov-hs-card").length,
  }));
  console.log("→ 状态统计:", JSON.stringify(stats));
  console.log("→ 截图完成: full_sources_live.png / sources_grid.png / full_hotspots_live.png");
  await browser.close();
})();
