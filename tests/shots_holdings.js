/* 局部截图：持仓分区（含持仓起始日期）与卖出分区（含卖出价/了结盈亏/清仓标注）
 *
 * 安全：默认只读。数据准备走 _guard.seed({soft:true})——截图脚本缺数据时不 exit 3，
 * 只打印提示并继续出图。详见 tests/_guard.js。 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed } = require("./_guard");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  announce();
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1180, height: 900, deviceScaleFactor: 2 });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: "#p-players tbody tr", timeoutMs: 30000, soft: true });
  await page.click('#tabs .tab[data-tab="players"]');
  await wait(600);

  const open = async (name) => {
    await page.evaluate((n) => {
      const tr = document.querySelector(`#p-players tbody tr[data-name="${n}"]`);
      if (tr) tr.querySelector("td")?.click();
    }, name);
    await wait(800);
  };
  const shot = async (path) => {
    const el = await page.$("#drawer");
    if (el) await el.screenshot({ path });
  };
  const scrollToTable = async (idx) => {
    await page.evaluate((i) => {
      const b = document.querySelector("#drawerBody");
      const t = [...b.querySelectorAll("table")][i];
      if (t) t.scrollIntoView({ block: "start" });
    }, idx);
    await wait(400);
  };

  // 大阪柯小李：持仓（含起始日期 + 已持有天数）+ 卖出（减仓）
  await open("大阪柯小李");
  await shot("shot_hold_buy.png");
  await scrollToTable(1);
  await shot("shot_hold_sell.png");

  // 尾盘狙击：清仓标注
  await page.keyboard.press("Escape");
  await wait(400);
  await open("尾盘狙击");
  await scrollToTable(0);
  await shot("shot_hold_closed.png");

  console.log("截图完成：shot_hold_buy.png / shot_hold_sell.png / shot_hold_closed.png");
  await browser.close();
})().catch((e) => { console.log("ERROR: " + e.message); process.exit(2); });
