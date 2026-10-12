/* 截取「近期热点」前两张卡的可读特写（非全页），用于人工核对三段结构与涨跌配色
 *
 * 安全：默认只读。数据准备走 _guard.seed({soft:true})——截图脚本缺数据时不 exit 3，
 * 只打印提示并继续出（占位）图。详见 tests/_guard.js。 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed } = require("./_guard");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  announce();
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1500,1250"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1250, deviceScaleFactor: 1.4 });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: ".ov-hs-card", timeoutMs: 25000, soft: true });
  await page.click('#tabs .tab[data-tab="overseas_hotspots"]');
  await wait(1200);

  // 第一屏：前两张卡
  const cards = await page.$$(".ov-hs-card");
  console.log("cards:", cards.length);
  if (!cards.length) {
    const info = await page.evaluate(() => ({
      panelLen: (document.querySelector("#p-overseas_hotspots") || {}).innerHTML?.length || 0,
      panelText: (document.querySelector("#p-overseas_hotspots") || {}).innerText?.slice(0, 160) || "",
    }));
    console.log("DEBUG", JSON.stringify(info));
    // 截图脚本的职责是出图：没数据就出一张占位图并说明，不视为失败
    await page.screenshot({ path: "chain_card1.png", fullPage: true });
    console.log("[shots_chain] 无热点卡片，已输出整页占位图 chain_card1.png；如需卡片特写请在隔离实例（非 8765 + ALLOW_DATA_RESET=1）载入示例数据后重跑");
    await browser.close();
    return;
  }

  // 用卡片自身 bbox 拼接裁剪：第 1 张
  const b1 = await cards[0].boundingBox();
  await page.screenshot({ path: "chain_card1.png", clip: { x: b1.x, y: b1.y, width: b1.width, height: b1.height } });
  // 第 2 张
  if (cards[1]) {
    const b2 = await cards[1].boundingBox();
    await page.screenshot({ path: "chain_card2.png", clip: { x: b2.x, y: b2.y, width: b2.width, height: b2.height } });
  }
  // 继续找「粮食歉收」卡（含 CBOT 签名）
  for (let i = 0; i < cards.length; i++) {
    const t = await cards[i].evaluate((el) => el.innerText);
    if (t.indexOf("CBOT") >= 0) {
      const bb = await cards[i].boundingBox();
      await page.screenshot({ path: "chain_card_food.png", clip: { x: bb.x, y: bb.y, width: bb.width, height: bb.height } });
      console.log("food card index:", i);
      break;
    }
  }
  await browser.close();
})().catch((e) => { console.log("ERR " + e.message); process.exit(2); });
