/* 情绪地图 / 股票卡片可读性回归测试
 *
 * 背景：用户反馈"情绪地图上的序号看不懂"——卡片只有 600519 这类裸代码，
 * 没有股票简称和公司全称。本脚本用真实 Chromium 验证修复后的卡片：
 *   1. 每张卡片必须同时具备：股票简称（中文）、6 位代码、公司全称、多空标签
 *   2. 图例必须存在，且逐条解释代码/名称/多/空/中性的含义
 *   3. 名称解析覆盖率应为 100%（无"名称待补全"）
 *   4. 无文字溢出/裁剪，无 JS 报错
 *   5. 点击卡片能打开详情，且详情标题含股票简称而非只有代码
 *
 * 用法：node tests/ui_stock_cards.js [port]
 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8766";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);

(async () => {
  announce();
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true,
    args: ["--no-sandbox", "--window-size=1440,1000"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  const seen = trackRequests(page);

  await page.goto(URL, { waitUntil: "networkidle2" });
  await seed(page, { sel: "#btnDemo", ready: "#p-sentiment .stk-card" });
  // 固定浅色科技风，保证截图可复现
  await page.evaluate(() => applyTheme("light-tech"));

  /* ---------- 1. 情绪地图卡片信息完整性 ---------- */
  await page.click('#tabs .tab[data-tab="sentiment"]');
  await wait(600);
  const cards = await page.$$eval("#p-sentiment .stk-card", (els) => els.map((el) => ({
    sym: el.dataset.sym,
    name: (el.querySelector(".stk-name") || {}).textContent || "",
    missing: !!el.querySelector(".stk-name.missing"),
    code: (el.querySelector(".stk-code") || {}).textContent || "",
    corp: (el.querySelector(".stk-corp") || {}).textContent || "",
    board: (el.querySelector(".stk-board") || {}).textContent || "",
    chips: [...el.querySelectorAll(".badge")].map((b) => b.textContent.trim()),
    total: (el.querySelector(".stk-total") || {}).textContent || "",
  })));
  ok("① 情绪地图有卡片", cards.length > 0, `${cards.length} 张`);
  const noName = cards.filter((c) => c.missing || !/[一-龥]/.test(c.name));
  ok("① 每张卡片都有股票简称（无待补全）", noName.length === 0,
    noName.length ? "缺名：" + noName.map((c) => c.sym).join(",") : cards.map((c) => c.name).slice(0, 5).join("、"));
  const badCode = cards.filter((c) => !/^\d{6}$/.test(c.code.trim()));
  ok("① 每张卡片都有 6 位股票代码", badCode.length === 0, badCode.map((c) => c.sym).join(",") || "全部合规");
  const badCorp = cards.filter((c) => !/(有限公司|股份有限公司|集团)/.test(c.corp));
  ok("① 每张卡片都有公司全称", badCorp.length === 0, badCorp.map((c) => c.sym + ":" + c.corp).join(" | ") || "全部含公司全称");
  const badChip = cards.filter((c) => !c.chips.some((t) => /多|中性/.test(t)));
  ok("① 每张卡片都有 多/空/中性 标签", badChip.length === 0, cards[0] ? cards[0].chips.join(",") : "");
  const badBoard = cards.filter((c) => !c.board.trim());
  ok("① 每张卡片都标注上市板块", badBoard.length === 0, cards[0] ? cards[0].board : "");
  const badTotal = cards.filter((c) => !/共 \d+ 条提及/.test(c.total));
  ok("① 每个数字都有中文口径说明（共 N 条提及）", badTotal.length === 0, cards[0] ? cards[0].total : "");

  /* ---------- 2. 图例 ---------- */
  const legend = await page.$eval("#p-sentiment .legend", (el) => el.innerText);
  ok("② 图例存在并解释代码含义", /不是序号|股票代码/.test(legend), legend.split("\n").slice(0, 2).join(" / "));
  ok("② 图例覆盖 多/空/中性 三项口径", /多 \d/.test(legend) && /空 \d/.test(legend) && /中性 \d/.test(legend));
  ok("② 图例显示名称解析覆盖率", /名称解析覆盖/.test(legend), (legend.match(/名称解析覆盖[^\n]*/) || [""])[0]);

  /* ---------- 3. 文字溢出检查（名称 / 公司全称是否被裁切） ---------- */
  const overflow = await page.$$eval("#p-sentiment .stk-card", (els) => els.map((el) => {
    const bad = [];
    for (const sel of [".stk-name", ".stk-corp", ".stk-code"]) {
      const e = el.querySelector(sel);
      if (!e) continue;
      if (e.scrollWidth > e.clientWidth + 2) bad.push(sel + " 溢出 " + (e.scrollWidth - e.clientWidth) + "px");
      const r = e.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) bad.push(sel + " 尺寸为 0");
    }
    const card = el.getBoundingClientRect();
    if (card.height < 90) bad.push("卡片过矮 " + Math.round(card.height));
    return { sym: el.dataset.sym, bad };
  }).filter((x) => x.bad.length));
  ok("③ 卡片内文字无溢出/裁切", overflow.length === 0, overflow.map((x) => x.sym + ":" + x.bad.join(";")).join(" | ") || "全部正常");

  /* ---------- 4. 点击卡片打开详情（标题含简称） ---------- */
  await page.click("#p-sentiment .stk-card");
  await wait(700);
  const dTitle = await page.$eval("#drawerTitle", (el) => el.textContent);
  const dBody = await page.$eval("#drawerBody", (el) => el.innerText);
  const dOpen = await page.$eval("#drawer", (el) => el.classList.contains("show"));
  ok("④ 点击卡片打开详情面板", dOpen, `标题="${dTitle}"`);
  ok("④ 详情标题为「简称（代码）」而非裸代码", /[一-龥]+（\d{6}）/.test(dTitle), dTitle);
  ok("④ 详情含 股票代码/股票简称/公司全称 三项", /股票代码/.test(dBody) && /股票简称/.test(dBody) && /公司全称/.test(dBody));
  await page.keyboard.press("Escape");
  await wait(300);

  /* ---------- 5. 当前讨论股票 面板同样标注 ---------- */
  await page.click('#tabs .tab[data-tab="stocks"]');
  await wait(500);
  const stockCards = await page.$$eval("#p-stocks .stk-card", (els) => els.map((el) => ({
    name: (el.querySelector(".stk-name") || {}).textContent || "",
    missing: !!el.querySelector(".stk-name.missing"),
    code: (el.querySelector(".stk-code") || {}).textContent || "",
    corp: (el.querySelector(".stk-corp") || {}).textContent || "",
  })));
  // 注意：必须显式排除"名称待补全"占位（否则空名会被当成有效名称误判 PASS）
  ok("⑤ 「当前讨论股票」卡片同样含 简称+代码+公司全称",
    stockCards.length > 0 && stockCards.every((c) => !c.missing && /[一-龥]/.test(c.name) && /^\d{6}$/.test(c.code) && c.corp),
    stockCards.slice(0, 3).map((c) => `${c.name}(${c.code})`).join("、") +
    (stockCards.some((c) => c.missing) ? " ⚠ 存在未补全项" : ""));

  // 回归：曾出现"名称已补全但面板仍报 12 个未识别"的字段名不一致 bug
  const stLegend = await page.$eval("#p-stocks .legend", (el) => el.innerText);
  const cov = (stLegend.match(/名称解析覆盖：(\d+)\/(\d+)/) || []).slice(1).map(Number);
  ok("⑤ 覆盖率统计与实际一致（不误报未识别）", cov.length === 2 && cov[0] === cov[1] && cov[1] > 0,
    `名称解析覆盖：${cov[0]}/${cov[1]}`);
  const stHint = await page.$eval("#p-stocks .hint", (el) => el.innerText);
  ok("⑤ 全部识别时不再提示「N 个代码未识别名称」", !/未识别名称/.test(stHint), stHint.slice(0, 60));
  ok("⑤ 「当前讨论股票」图例改用提及次数口径（不套用多空）",
    /被提及/.test(stLegend) && !/看多情绪/.test(stLegend), stLegend.split("\n").slice(0, 4).join(" / "));

  /* ---------- 6. 截图（浅色 / 深色 / 详情） ---------- */
  const shot = async (file, sel) => {
    if (sel) {
      const h = await page.$(sel);
      await h.screenshot({ path: file });
    } else {
      await page.screenshot({ path: file, fullPage: true });
    }
  };
  await shot("shot_stocks_light.png", "#p-stocks");
  await page.click('#tabs .tab[data-tab="sentiment"]');
  await wait(400);
  await shot("shot_sentiment_light.png", "#p-sentiment");
  // 详情面板截图
  await page.click("#p-sentiment .stk-card");
  await wait(700);
  await shot("shot_stock_drawer.png");
  await page.keyboard.press("Escape");
  await wait(250);
  // 深色主题下的可读性
  await page.evaluate(() => applyTheme("dark-dashboard"));
  await wait(400);
  await shot("shot_sentiment_dark.png", "#p-sentiment");
  await page.evaluate(() => applyTheme("light-tech"));

  ok("⑥ 无 JS 报错", errs.length === 0, errs.slice(0, 3).join(" | "));

  console.log(out.join("\n"));
  const fail = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n合计 ${out.length} 项，失败 ${fail} 项`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error("脚本异常：", e); process.exit(2); });
