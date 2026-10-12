/* 记录统计明细抽屉的几何与整图（只读，用于人工目检排版） */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${process.argv[2] || "8799"}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await b.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.waitForSelector("#cards .card.clickable");
  await page.click('#cards .card[data-kind="positions"]');
  await wait(800);
  const geo = await page.evaluate(() => {
    const d = document.querySelector("#drawer"), r = d.getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
             vw: innerWidth, vh: innerHeight,
             inside: r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
             rows: document.querySelectorAll("#drawerBody table.tbl-detail tbody tr").length,
             header: document.querySelector("#drawerBody table.tbl-detail thead").innerText.replace(/\s+/g, " ").trim() };
  });
  console.log("drawer geo:", JSON.stringify(geo));
  await (await page.$("#drawer")).screenshot({ path: "stats_drawer_element.png" });
  await page.screenshot({ path: "stats_drawer_full.png" });
  await b.close();
})().catch((e) => { console.log("ERR", e.message); process.exit(2); });
