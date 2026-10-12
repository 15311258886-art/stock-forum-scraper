/* 来源渠道标注回归：相关人物 / 正在操作股票 —— 每个名称都必须有渠道徽章，且不遗漏 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${process.argv[2] || "8765"}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
const count = (page, sel) => page.$$eval(sel, (els) => els.length).catch(() => 0);
// 演示数据加载随数据量增长变慢，改为「轮询等就绪」而非固定睡眠，避免误判
const waitReady = async (page, timeout = 25000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if ((await count(page, "#p-persons tbody tr")) > 0) return true;
    await wait(400);
  }
  return false;
};

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1100"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1100 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.click("#btnDemo");
  await waitReady(page);

  /* ---------- 相关人物 ---------- */
  await page.click('#tabs .tab[data-tab="persons"]');
  await wait(700);
  const pTotal = await count(page, "#p-persons tbody tr");
  const pPlat = await page.$$eval("#p-persons tbody tr", (trs) =>
    trs.filter((tr) => tr.querySelector("td:nth-child(2) .badge.p-taoguba, td:nth-child(2) .badge.p-eastmoney, td:nth-child(2) .badge.p-xueqiu, td:nth-child(2) .badge.p-ths")).length).catch(() => 0);
  const pTier = await page.$$eval("#p-persons tbody tr", (trs) =>
    trs.filter((tr) => { const c = tr.querySelector("td:nth-child(2)"); return c && /t-[A-F]/.test(c.innerHTML); }).length).catch(() => 0);
  ok("① 相关人物·每个名称都有平台渠道徽章", pTotal > 0 && pPlat === pTotal, `${pPlat}/${pTotal}`);
  ok("① 相关人物·覆盖平台徽章样式(4平台)", await count(page, "#p-persons .badge.p-taoguba") + await count(page, "#p-persons .badge.p-eastmoney") + await count(page, "#p-persons .badge.p-xueqiu") + await count(page, "#p-persons .badge.p-ths") > 0, "");
  ok("① 相关人物·含所属层级 A~F 徽章", pTier > 0, `tier=${pTier}`);
  await page.screenshot({ path: "channels_persons.png", fullPage: true });

  /* ---------- 正在操作股票 ---------- */
  await page.click('#tabs .tab[data-tab="operating"]');
  await wait(700);
  const oTotal = await count(page, "#p-operating tbody tr");
  const oPlat = await page.$$eval("#p-operating tbody tr", (trs) =>
    trs.filter((tr) => tr.querySelector("td:nth-child(1) .badge.p-taoguba, td:nth-child(1) .badge.p-eastmoney, td:nth-child(1) .badge.p-xueqiu, td:nth-child(1) .badge.p-ths")).length).catch(() => 0);
  const oTier = await page.$$eval("#p-operating tbody tr", (trs) =>
    trs.filter((tr) => { const c = tr.querySelector("td:nth-child(1)"); return c && /t-[A-F]/.test(c.innerHTML); }).length).catch(() => 0);
  ok("② 正在操作·每个选手都有平台渠道徽章", oTotal > 0 && oPlat === oTotal, `${oPlat}/${oTotal}`);
  ok("② 正在操作·含所属层级 A~F 徽章", oTier > 0, `tier=${oTier}`);
  await page.screenshot({ path: "channels_operating.png", fullPage: true });

  ok("③ 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  const guardOk = assertNoDestructiveRequest(page, seen);
  await browser.close();
  process.exit(fails || !guardOk ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR: " + e.message); process.exit(2); });
