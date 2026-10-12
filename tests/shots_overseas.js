/* 全页截图：两个海外模块 + 选手/共识（供交付审阅）
 *
 * 安全：默认只读。数据准备走 _guard.seed({soft:true})——截图脚本缺数据时不 exit 3，
 * 只打印提示并继续出图。详见 tests/_guard.js。 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed } = require("./_guard");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const TABS = [
  ["overseas_sentiment", "full_overseas_sentiment.png"],
  ["overseas_hotspots", "full_overseas_hotspots.png"],
  ["players", "full_players_tiers.png"],
  ["consensus", "full_consensus.png"],
];

(async () => {
  announce();
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1200"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1200, deviceScaleFactor: 1 });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: "#p-overseas_sentiment .ov-src-status", timeoutMs: 25000, soft: true });
  for (const [tab, file] of TABS) {
    await page.click(`#tabs .tab[data-tab="${tab}"]`);
    await wait(600);
    await page.screenshot({ path: file, fullPage: true });
    console.log("saved", file);
  }
  await browser.close();
  console.log("done");
})().catch((e) => { console.log("ERR: " + e.message); process.exit(1); });
