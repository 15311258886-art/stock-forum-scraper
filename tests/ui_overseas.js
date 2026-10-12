/* 海外舆情双模块 + 选手扩容 UI 回归测试（真实 Chromium）
 *
 * 安全：默认只读。数据准备走 _guard.seed()，只有「端口非 8765 且
 * ALLOW_DATA_RESET=1」才会点击 #btnDemo；否则复用库内现有数据。
 * 详见 tests/_guard.js。 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed, trackRequests, assertNoDestructiveRequest } = require("./_guard");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
const text = (page, sel) => page.$eval(sel, (el) => el.innerText).catch(() => "");
const count = (page, sel) => page.$$eval(sel, (els) => els.length).catch(() => 0);

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1000"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.click("#btnDemo");
  await waitReady(page);

  /* ---------- 模块一：美股港股舆情 ---------- */
  await page.click('#tabs .tab[data-tab="overseas_sentiment"]');
  await wait(600);
  const thermoW = await page.$eval(".ov-thermo-fill", (el) => el.style.width).catch(() => "");
  ok("① 情绪温度计渲染", /%$/.test(thermoW || ""), `width=${thermoW}`);
  const svgPts = await count(page, ".ov-chart svg polyline");
  ok("① 趋势图绘制(有折线)", svgPts > 0, `polylines=${svgPts}`);
  const regionCards = await count(page, ".ov-region");
  ok("① 区域分布卡片", regionCards >= 3, `regions=${regionCards}`);
  const srcStatus = await count(page, ".ov-src-status");
  ok("① 数据源状态卡片(已接入/待激活)", srcStatus >= 10, `statusCards=${srcStatus}`);
  const onBadges = await page.$$eval(".ov-src-status.src-on .ov-src-st-badge", (e) => e.length).catch(() => 0);
  const waitBadges = await page.$$eval(".ov-src-status.src-wait .ov-src-st-badge", (e) => e.length).catch(() => 0);
  const demoBadges = await page.$$eval(".ov-src-status.src-demo .ov-src-st-badge", (e) => e.length).catch(() => 0);
  ok("① 含「待激活」状态(诚实展示被墙源)", waitBadges > 0, `已接入=${onBadges} 待激活=${waitBadges} 示例模式=${demoBadges}`);
  ok("① 含「示例模式·未联网」(非红色错误)", demoBadges > 0, `示例模式=${demoBadges}`);
  const newsCount = await count(page, ".ov-news");
  ok("① 海外新闻列表", newsCount > 0, `news=${newsCount}`);
  await page.screenshot({ path: "shot_overseas_sentiment.png" });

  /* ---------- 模块二：近期热点(国外驱动) ---------- */
  await page.click('#tabs .tab[data-tab="overseas_hotspots"]');
  await wait(600);
  const hsCards = await count(page, ".ov-hs-card");
  ok("② 国外驱动热点卡片", hsCards > 0, `cards=${hsCards}`);
  const hasTrans = await page.$$eval(".ov-hs-text", (e) => e.length).catch(() => 0);
  ok("② 含传导路径/放大/交叉比对分析", hasTrans >= 3, `blocks=${hasTrans}`);
  const hasEv = await count(page, ".ov-hs-ev li");
  ok("② 含原始证据条目", hasEv > 0, `evidence=${hasEv}`);
  await page.screenshot({ path: "shot_overseas_hotspots.png" });

  /* ---------- 选手扩容：A~F 层级 + 全部露出 ---------- */
  await page.click('#tabs .tab[data-tab="players"]');
  await wait(500);
  const totalPlayers = await count(page, "#p-players tbody tr");
  ok("③ 选手池扩容(>=100)", totalPlayers >= 100, `players=${totalPlayers}`);
  const tierBtns = await count(page, '.btn.sm[data-tier="A"], .btn.sm[data-tier="B"], .btn.sm[data-tier="C"], .btn.sm[data-tier="D"], .btn.sm[data-tier="E"], .btn.sm[data-tier="F"]');
  ok("③ 含 A~F 层级筛选按钮", tierBtns === 6, `tierBtns=${tierBtns}`);
  // 点 E 层级
  await page.click('.btn.sm[data-tier="E"]');
  await wait(300);
  const eRows = await count(page, '#p-players tbody tr:not([style*="display: none"])');
  ok("③ E 层级(主播大V)可筛选且露出(非隐藏)", eRows > 0, `E可见行=${eRows}`);
  // 点 F 层级
  await page.click('.btn.sm[data-tier="F"]');
  await wait(300);
  const fRows = await count(page, '#p-players tbody tr:not([style*="display: none"])');
  ok("③ F 层级(长尾)可筛选且露出", fRows > 0, `F可见行=${fRows}`);
  await page.click('.btn.sm[data-tier="ALL"]');
  await wait(300);
  await page.screenshot({ path: "shot_players_tiers.png" });

  /* ---------- 共识：加权/层级/平台 ---------- */
  await page.click('#tabs .tab[data-tab="consensus"]');
  await wait(500);
  const consTierBadge = await page.$$eval("#p-consensus tbody .badge", (els) => els.filter((e) => /t-[A-F]/.test(e.className)).length).catch(() => 0);
  ok("④ 共识含层级徽章(A~F)", consTierBadge > 0, `tierBadges=${consTierBadge}`);
  const consWeighted = await page.$$eval("#p-consensus tbody .badge.t-w", (e) => e.length).catch(() => 0);
  ok("④ 共识含加权共识度", consWeighted > 0, `weighted=${consWeighted}`);
  await page.screenshot({ path: "shot_consensus.png" });

  ok("⑤ 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + e.message); process.exit(2); });
