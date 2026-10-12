/* 截图取证：① 共同持仓抽屉（你截图的那处）修复后的「来源」呈现；② 海外新闻的真实可点链接 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${process.argv[2] || "8765"}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const count = (page, sel) => page.$$eval(sel, (e) => e.length).catch(() => 0);

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1100"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1100 });
  await page.goto(URL, { waitUntil: "networkidle2" });

  // —— ① 共同持仓抽屉 ——
  await page.waitForFunction(() => document.querySelectorAll("#p-consensus tr[data-sym]").length > 0, { timeout: 30000 }).catch(() => {});
  await page.click('#tabs .tab[data-tab="consensus"]');
  await wait(700);
  await page.evaluate(() => { const tr = document.querySelector("#p-consensus tr[data-sym]"); if (tr) tr.querySelector("td").click(); });
  await wait(900);
  await page.evaluate(() => {
    const b = document.querySelector("#drawerBody");
    if (b) { const kv = [...b.querySelectorAll(".kv")].find((k) => /来源网址/.test(k.textContent)); if (kv) kv.scrollIntoView({ block: "center" }); }
  });
  await wait(400);
  const drawer = await page.$("#drawerBody").catch(() => null);
  if (drawer) await drawer.screenshot({ path: "shot_src_consensus.png" }).catch(() => {});
  console.log("shot_src_consensus.png 已生成");

  // —— ② 海外新闻 ——
  await page.evaluate(() => { const b = document.querySelector("#btnClose"); if (b) b.click(); });
  await wait(400);
  await page.click('#tabs .tab[data-tab="overseas_sentiment"]');
  await wait(1600);
  const list = await page.$(".ov-news-list").catch(() => null);
  if (list) {
    await page.evaluate(() => { const l = document.querySelector(".ov-news-list"); if (l) l.scrollIntoView({ block: "start" }); });
    await wait(300);
    await list.screenshot({ path: "shot_src_overseas.png" }).catch(() => {});
  }
  console.log("shot_src_overseas.png 已生成");
  await browser.close();
})();
