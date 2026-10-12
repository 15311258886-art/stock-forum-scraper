/* 近期热点（国外驱动）结构化传导分析回归：
   每条热点必须输出 ① 事件概述 → ② 关键传导变量 → ③ 板块涨跌预判，
   并覆盖用户指定的四类事件：地缘冲突 / 自然灾害 / 科研论文 / 粮食歉收。 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${process.argv[2] || "8765"}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
const count = (page, sel) => page.$$eval(sel, (els) => els.length).catch(() => 0);
// 轮询等就绪，避免因演示加载变慢而误判
const waitReady = async (page, timeout = 25000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if ((await count(page, ".ov-hs-card")) > 0) return true;
    await wait(400);
  }
  return false;
};

(async () => {
  announce();
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1500,1200"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1200 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  const seen = trackRequests(page);
  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: ".ov-hs-card", timeoutMs: 25000 });

  /* ---------- 近期热点（国外驱动） ---------- */
  await page.click('#tabs .tab[data-tab="overseas_hotspots"]');
  await wait(800);

  const cards = await count(page, ".ov-hs-card");
  ok("① 热点卡片已渲染", cards > 0, `cards=${cards}`);

  // ① 事件概述：每张卡都要有，且文字非空
  const ovInfo = await page.$$eval(".ov-hs-card", (els) => els.map((el) => {
    const subs = [...el.querySelectorAll(".ov-hs-sub")].map((s) => s.textContent.trim());
    const has = subs.some((s) => s.indexOf("①") >= 0 && s.indexOf("事件概述") >= 0);
    const blk = [...el.querySelectorAll(".ov-hs-block")].find((b) => b.textContent.indexOf("事件概述") >= 0);
    const txt = blk ? (blk.querySelector(".ov-hs-text") || {}).textContent || "" : "";
    return { has, len: txt.trim().length };
  })).catch(() => []);
  const ovHas = ovInfo.filter((x) => x.has).length;
  const ovNonEmpty = ovInfo.filter((x) => x.len > 10).length;
  ok("② 每张卡都有「① 事件概述」且文字非空", cards > 0 && ovHas === cards && ovNonEmpty === cards,
     `有概述=${ovHas}/${cards} 非空=${ovNonEmpty}/${cards}`);

  // ② 关键传导变量
  const varCards = await page.$$eval(".ov-hs-card", (els) => els.filter((el) => {
    const b = [...el.querySelectorAll(".ov-hs-block")].find((x) => x.textContent.indexOf("关键传导变量") >= 0);
    return b && b.querySelectorAll(".badge.var").length > 0;
  }).length).catch(() => 0);
  const varTotal = await count(page, ".ov-hs-card .badge.var");
  ok("③ 每张卡都有「② 关键传导变量」徽章", cards > 0 && varCards === cards, `带变量卡=${varCards}/${cards} 变量徽章共=${varTotal}`);

  // ③ 板块涨跌预判
  const fcCards = await page.$$eval(".ov-hs-card", (els) => els.filter((el) => {
    const b = [...el.querySelectorAll(".ov-hs-block")].find((x) => x.textContent.indexOf("板块涨跌预判") >= 0);
    if (!b) return false;
    const up = b.querySelector(".ov-hs-fc-lbl.up");
    const dn = b.querySelector(".ov-hs-fc-lbl.down");
    return !!up && !!dn;
  }).length).catch(() => 0);
  ok("④ 每张卡都有「③ 板块涨跌预判」(上涨/下跌两行)", cards > 0 && fcCards === cards, `带预判卡=${fcCards}/${cards}`);

  const upB = await count(page, ".ov-hs-forecast .ov-hs-fc-lbl.up + .ov-hs-fc-bdgs .badge.bull");
  const dnB = await count(page, ".ov-hs-forecast .ov-hs-fc-lbl.down + .ov-hs-fc-bdgs .badge.bear");
  ok("⑤ 涨跌板块用 A 股配色（红=涨 / 绿=跌）", upB > 0 && dnB > 0, `上涨徽章=${upB} 下跌徽章=${dnB}`);

  // 四类重点事件是否都出现
  const html = await page.$eval("#p-overseas_hotspots", (el) => el.innerText).catch(() => "");
  const sig = {
    "地缘冲突与战争": "油轮/集装箱运费",
    "洪涝等自然灾害": "水利建设",
    "重大科研论文发布": "顶刊",
    "粮食歉收与减产": "CBOT",
  };
  for (const [name, token] of Object.entries(sig)) {
    ok(`⑥ 覆盖重点事件「${name}」`, html.indexOf(token) >= 0, token);
  }

  // 证据链仍在
  const ev = await count(page, ".ov-hs-card .ov-hs-ev li");
  ok("⑦ 每条热点仍附原始证据(可追溯)", ev > 0, `evidence=${ev}`);

  await page.screenshot({ path: "chain_hotspots.png", fullPage: true });

  ok("⑧ 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  const guardOk = assertNoDestructiveRequest(page, seen);
  await browser.close();
  process.exit(fails || !guardOk ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR: " + e.message); process.exit(2); });
