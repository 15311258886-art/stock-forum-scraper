/* 高清局部截图：相关人物 / 正在操作股票 的渠道标注（取表格前 14 行）
 *
 * 安全：默认只读。数据准备走 _guard.seed({soft:true})——截图脚本缺数据时不 exit 3，
 * 只打印提示并继续出图。详见 tests/_guard.js。 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed } = require("./_guard");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const SHOTS = [
  ["persons", "channels_persons_top.png"],
  ["operating", "channels_operating_top.png"],
];

(async () => {
  announce();
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 1000, deviceScaleFactor: 2 });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: "#p-persons tbody tr", timeoutMs: 30000, soft: true });
  for (const [tab, file] of SHOTS) {
    await page.click(`#tabs .tab[data-tab="${tab}"]`);
    await wait(700);
    const box = await page.$eval(`#p-${tab}`, (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y }; });
    await page.screenshot({ path: file, clip: { x: box.x, y: Math.max(0, box.y - 40), width: 1400, height: 660 } });
    console.log("saved", file);
  }
  await browser.close();
  console.log("done");
})().catch((e) => { console.log("ERR: " + e.message); process.exit(1); });
