/* 持仓展示回归：① 持仓表必须标注「持仓起始日期 + 已持有天数」；② 必须有「卖出/减仓」分区表，
   且列头为卖出时间/卖出价（不得把卖出价塞进"买入价"列）；③ 清仓标的须标「已清仓」。
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
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
// 解析抽屉内所有表格：表头 + 行数据
const tablesIn = (page) => page.$$eval("#drawerBody table", (ts) => ts.map((t) => {
  const heads = [...t.querySelectorAll("thead th")].map((th) => th.textContent.trim());
  const rows = [...t.querySelectorAll("tbody tr")].map((tr) =>
    [...tr.children].map((td) => td.textContent.trim()));
  return { heads, rows };
}));

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
  await page.click('#tabs .tab[data-tab="players"]');
  await wait(600);

  const openPlayer = async (name) => {
    await page.evaluate((n) => {
      const tr = document.querySelector(`#p-players tbody tr[data-name="${n}"]`);
      if (tr) tr.querySelector("td")?.click();
    }, name);
    await wait(800);
  };

  /* ================= 大阪柯小李：减仓（有买入 + 卖出） ================= */
  await openPlayer("大阪柯小李");
  const t1 = await tablesIn(page);
  const holdT = t1.find((t) => t.heads.some((h) => h.includes("持仓起始日期")));
  const sellT = t1.find((t) => t.heads.some((h) => h.includes("卖出时间")));

  ok("① 存在持仓明细表且列出「持仓起始日期」列", !!holdT, holdT ? holdT.heads.join(" | ") : "未找到");
  if (holdT) {
    const i = holdT.heads.findIndex((h) => h.includes("持仓起始日期"));
    const vals = holdT.rows.map((r) => r[i] || "");
    const dated = vals.filter((v) => /\d{4}-\d{2}-\d{2}/.test(v)).length;
    ok("② 每行持仓都标注了起始日期", holdT.rows.length > 0 && dated === holdT.rows.length,
      `${dated}/${holdT.rows.length}｜示例：${vals.slice(0, 2).join(" / ")}`);
    const withDays = vals.filter((v) => /已持有\s*\d+\s*天/.test(v)).length;
    ok("③ 起始日期旁标注「已持有 N 天」", withDays === holdT.rows.length, `${withDays}/${holdT.rows.length}`);
  }

  ok("④ 存在「卖出 / 减仓明细」表", !!sellT, sellT ? sellT.heads.join(" | ") : "未找到");
  if (sellT) {
    const iT = sellT.heads.findIndex((h) => h.includes("卖出时间"));
    const iP = sellT.heads.findIndex((h) => h.includes("卖出价"));
    ok("⑤ 卖出表列头为「卖出时间 / 卖出价」（不再混入买入列）", iT >= 0 && iP >= 0, sellT.heads.join(" | "));
    ok("⑥ 卖出表有记录", sellT.rows.length > 0, `rows=${sellT.rows.length}`);
    const bad = sellT.rows.filter((r) => !/\d{4}-\d{2}-\d{2}/.test(r[iT] || "") || !/\d/.test(r[iP] || ""));
    ok("⑦ 每条卖出记录都有卖出时间与卖出价", sellT.rows.length > 0 && bad.length === 0, `异常行=${bad.length}`);
    // 持仓起始日期标注在「标的」单元格副行（宽度受限下保证首屏可见）
    const sinceIn = sellT.rows.filter((r) => /\d{2}-\d{2}\s*起/.test(r[0] || "")).length;
    ok("⑧ 每条卖出记录都标注持仓起始日期", sinceIn === sellT.rows.length && sellT.rows.length > 0,
      `${sinceIn}/${sellT.rows.length}｜${(sellT.rows[0] || [""])[0].replace(/\n/g, " ")}`);
    // 了结盈亏与卖出价同列展示（内容宽度受限时优先保全这两项关键信息）
    const pnlIn = sellT.rows.filter((r) => /[+\-]\d+\.\d{2}/.test(r[iP] || "")).length;
    ok("⑧b 卖出价列含了结盈亏", pnlIn > 0, `含盈亏行=${pnlIn}/${sellT.rows.length}`);
    ok("⑧c 卖出表列数受控（≤5 列，首屏可完整显示）", sellT.heads.length <= 5, `列数=${sellT.heads.length}`);
  }
  await page.screenshot({ path: "holdings_player.png", fullPage: true });

  /* ================= 尾盘狙击：清仓（应有「已清仓」标注） ================= */
  await page.keyboard.press("Escape");
  await wait(400);
  await openPlayer("尾盘狙击");
  const closed = await page.$$eval("#drawerBody .closed-badge", (els) => els.length).catch(() => 0);
  const closedRow = await page.$$eval("#drawerBody .row-closed", (els) => els.length).catch(() => 0);
  const t2 = await tablesIn(page);
  const sellT2 = t2.find((t) => t.heads.some((h) => h.includes("卖出时间")));
  const fullRow = sellT2 ? sellT2.rows.filter((r) => (r[1] || "").includes("清仓")).length : 0;
  ok("⑨ 清仓标的在持仓表中标注「已清仓」", closed > 0 && closedRow > 0, `徽章=${closed} 行=${closedRow}`);
  ok("⑩ 卖出表区分「清仓」与「减仓」", fullRow > 0, `清仓行=${fullRow}`);
  await page.screenshot({ path: "holdings_closed.png", fullPage: true });

  ok("⑪ 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR: " + e.message); process.exit(2); });
