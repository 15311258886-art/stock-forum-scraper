/* 来源链接可点性回归（A 路线）：
   ① 示例（demo/seed）来源不得做成可点击跳转，须降级为文本 +「示例」标记；
   ② 真实来源仍须可点（target=_blank + rel=noopener）；
   ③ 全页不得残留任何指向占位域名（example.local 等）的可点链接。 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${process.argv[2] || "8765"}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
const count = (page, sel) => page.$$eval(sel, (els) => els.length).catch(() => 0);
const waitReady = async (page, timeout = 30000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if ((await count(page, "#p-players tbody tr")) > 0) return true;
    await wait(400);
  }
  return false;
};
const closeDrawer = async (page) => {
  await page.evaluate(() => { const b = document.querySelector("#btnClose"); if (b) b.click(); });
  await wait(400);
};

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1100"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1100 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(URL, { waitUntil: "networkidle2" });
  await waitReady(page);   // 注意：不点「载入示例」，避免覆盖已联网抓取的真实数据
  await wait(600);

  // ---------- 1. 全局：不得残留指向占位域名的可点链接 ----------
  const phLinks = await page.evaluate(() =>
    [...document.querySelectorAll('a[href]')]
      .map((a) => a.getAttribute("href"))
      .filter((h) => /example\.(local|com|org|net)|localhost|127\.0\.0\.1/i.test(h || "")));
  ok("① 全页无指向占位域名的可点链接", phLinks.length === 0, phLinks.length ? phLinks.slice(0, 3).join(" ; ") : "0 条");

  const chipDemo = await count(page, ".chip-demo");
  ok("② 示例来源已带「示例」标记", chipDemo > 0, `chip-demo=${chipDemo}`);

  // ---------- 2. 共同持仓抽屉（你截图的那一处） ----------
  await page.click('#tabs .tab[data-tab="consensus"]');
  await wait(700);
  const rowN = await count(page, "#p-consensus tr[data-sym]");
  await page.evaluate(() => { const tr = document.querySelector("#p-consensus tr[data-sym]"); if (tr) tr.querySelector("td").click(); });
  await wait(900);
  const dHtml = await page.evaluate(() => { const b = document.querySelector("#drawerBody"); return b ? b.innerHTML : ""; });
  const dHasPhLink = /<a[^>]+href="[^"]*example\./i.test(dHtml);
  ok("③ 抽屉「来源网址」非可点占位链接", dHtml.length > 0 && !dHasPhLink, dHasPhLink ? "仍存在 <a href=example.*>" : "无");
  ok("③b 抽屉「来源/来源网址」带「示例」标记", /chip-demo/.test(dHtml), `chip-demo 出现 ${(dHtml.match(/chip-demo/g) || []).length} 次`);
  const metaOk = /持仓选手明细/.test(dHtml);
  ok("③c 抽屉含「持仓选手明细」区块（截图处）", metaOk, "");
  await closeDrawer(page);

  // ---------- 3. 选手表「来源」列 ----------
  await page.click('#tabs .tab[data-tab="players"]');
  await wait(600);
  const srcCells = await page.$$eval("#p-players tbody tr", (trs) =>
    trs.map((tr) => tr.children[7] ? tr.children[7].innerHTML : ""));
  const cellsWithPhLink = srcCells.filter((h) => /<a[^>]+href="[^"]*example\./i.test(h));
  ok("④ 选手表「来源」列无占位可点链接", srcCells.length > 0 && cellsWithPhLink.length === 0,
    `检查 ${srcCells.length} 行，违规 ${cellsWithPhLink.length} 行`);
  ok("④b 选手表「来源」列显示「示例」标记", srcCells.some((h) => /chip-demo/.test(h)),
    `含标记行 ${srcCells.filter((h) => /chip-demo/.test(h)).length}/${srcCells.length}`);

  // ---------- 4. 选手详情抽屉 ----------
  await page.evaluate(() => {
    const tr = document.querySelector("#p-players tbody tr");
    if (tr) tr.querySelector("td").click();
  });
  await wait(900);
  const pHtml = await page.evaluate(() => { const b = document.querySelector("#drawerBody"); return b ? b.innerHTML : ""; });
  ok("⑤ 选手详情「来源」无占位可点链接", !/<a[^>]+href="[^"]*example\./i.test(pHtml),
    pHtml ? "OK" : "抽屉为空");
  await closeDrawer(page);

  // ---------- 5. 海外舆情：真实链接须可点 ----------
  await page.click('#tabs .tab[data-tab="overseas_sentiment"]');
  await wait(1400);
  const ov = await page.evaluate(() => {
    const news = [...document.querySelectorAll(".ov-news-title")];
    const links = news.map((d) => {
      const a = d.querySelector("a");
      return a ? { href: a.getAttribute("href"), tgt: a.getAttribute("target"), rel: a.getAttribute("rel") || "" } : null;
    });
    const phInNews = links.filter((l) => l && /example\./i.test(l.href || "")).length;
    const realInNews = links.filter((l) => l && /^https?:\/\//i.test(l.href || "") && !/example\./i.test(l.href)).length;
    return { total: news.length, linked: links.filter(Boolean).length, phInNews, realInNews, sample: (links.find(Boolean) || {}).href || "" };
  });
  ok("⑥ 海外新闻存在真实可点链接", ov.realInNews > 0, `共 ${ov.total} 条，真实链接 ${ov.realInNews} 条，示例链接 ${ov.phInNews} 条`);
  ok("⑥b 海外新闻无占位链接", ov.phInNews === 0, `示例链接 ${ov.phInNews} 条`);
  const ovRelOk = await page.evaluate(() =>
    [...document.querySelectorAll('.ov-news-title a')].every((a) => a.getAttribute("target") === "_blank" && (a.getAttribute("rel") || "").includes("noopener")));
  ok("⑥c 真实链接为安全新窗口（_blank + noopener）", ov.realInNews > 0 && ovRelOk, ovRelOk ? "全部合规" : "存在不合规链接");
  ok("⑥d 样本链接域名", ov.realInNews > 0, ov.sample.slice(0, 68));

  // ---------- 6. 热点证据链接 ----------
  await page.click('#tabs .tab[data-tab="overseas_hotspots"]');
  await wait(1200);
  const hsPh = await page.evaluate(() =>
    [...document.querySelectorAll("#p-overseas_hotspots a[href]")].filter((a) => /example\./i.test(a.getAttribute("href") || "")).length);
  ok("⑦ 热点证据链接无占位域名", hsPh === 0, `违规 ${hsPh} 条`);

  ok("⑧ 无 JS 运行错误", errs.length === 0, errs.slice(0, 2).join(" | ") || "0");

  const pass = out.filter((l) => l.startsWith("PASS")).length;
  console.log(out.join("\n"));
  console.log(`\n=== 来源链接回归：${pass}/${out.length} 通过 ===`);
  await browser.close();
  process.exit(pass === out.length ? 0 : 1);
})();
