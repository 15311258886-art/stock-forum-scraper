/* 统计卡片 → 明细列表 联动回归（真实 Chromium，只读）
   验证：9 个统计卡片可点、点击弹出明细抽屉、条数与卡片数字一致、行可下钻、多入口关闭。
   ⚠️ 只读：全过程只发 GET（/api/summary、/api/detail_list、/api/stock…），
      绝不点击 #btnDemo / #btnFetch / #btnOverseas 等写按钮。 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8799";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);

const KINDS = ["raw_pages", "posts", "trades", "news", "players",
               "positions", "persons", "stock_mentions", "topics"];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true,
    args: ["--no-sandbox", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  STEP = "goto";
  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.waitForSelector("#cards .card.clickable", { timeout: 8000 });

  const drawer = () => page.evaluate(() => {
    const d = document.querySelector("#drawer");
    const body = document.querySelector("#drawerBody");
    const rows = body.querySelectorAll("table.tbl-detail tbody tr").length;
    const empty = !!body.querySelector("td.empty");
    const tds = [...body.querySelectorAll("table.tbl-detail tbody tr:first-child td")].map((t) => t.innerText.trim());
    return {
      shown: d.classList.contains("show"),
      title: document.querySelector("#drawerTitle").textContent,
      // 「共 N 条」在抽屉正文的提示条里（标题只放「标签 · 明细」）
      note: (body.querySelector(".hint") || {}).innerText || "",
      rows, empty, tds,
      hasTable: !!body.querySelector("table.tbl-detail"),
    };
  });

  /* ---------- 1. 9 张卡片都带 kind 且可点 ---------- */
  STEP = "cards";
  const cards = await page.$$eval("#cards .card.clickable", (els) =>
    els.map((e) => ({ kind: e.dataset.kind, n: (e.querySelector(".n") || {}).textContent,
                      more: !!(e.querySelector(".more") && /明细/.test(e.querySelector(".more").textContent)) })));
  ok("1.1 卡片数量 = 9", cards.length === 9, `实际 ${cards.length}`);
  ok("1.2 每张卡片都带 data-kind", KINDS.every((k) => cards.some((c) => c.kind === k)),
     cards.map((c) => c.kind).join(","));
  ok("1.3 每张卡片都有「查看明细」入口", cards.every((c) => c.more));

  /* ---------- 2. 命中测试：卡片真的能被点到（未被遮挡） ---------- */
  STEP = "hit";
  const hit = await page.evaluate(() => {
    const el = document.querySelector('#cards .card[data-kind="positions"]');
    const r = el.getBoundingClientRect();
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { blocked: !(at === el || el.contains(at)), at: at ? (at.className || at.tagName) : "null" };
  });
  ok("2. 卡片可命中（未被遮挡）", !hit.blocked, "命中=" + hit.at);

  /* ---------- 3. 逐张点击 → 抽屉弹出，条数与卡片数字一致 ---------- */
  STEP = "click-each";
  let mismatches = [], notOpened = [], noTable = [];
  for (const k of KINDS) {
    // 先关掉上一个
    await page.keyboard.press("Escape"); await wait(120);
    const cardNum = await page.$eval(`#cards .card[data-kind="${k}"] .n`, (e) => e.textContent.trim());
    await page.click(`#cards .card[data-kind="${k}"]`);
    await wait(650);
    const s = await drawer();
    if (!s.shown || !s.hasTable) { notOpened.push(k); continue; }
    if (!/明细/.test(s.title) || !/共\s*\d+\s*条/.test(s.note)) noTable.push(`${k}:"${s.title}" note="${s.note}"`);
    const n = parseInt(cardNum.replace(/[^\d]/g, ""), 10);
    if (!(s.rows === n || s.empty)) mismatches.push(`${k} 卡=${n} 列=${s.rows}`);
  }
  ok("3.1 每张卡片点击后都弹出明细抽屉", notOpened.length === 0, notOpened.join(",") || "9/9 均弹出");
  ok("3.2 抽屉标题含「明细 … 共 N 条」", noTable.length === 0, noTable.join(" | ") || "ok");
  ok("3.3 明细条数 == 卡片数字", mismatches.length === 0, mismatches.join(" | ") || "9/9 一致");

  /* ---------- 4. 内容可读性：不是空壳 ---------- */
  STEP = "content";
  await page.keyboard.press("Escape"); await wait(120);
  await page.click('#cards .card[data-kind="trades"]'); await wait(650);
  let s = await drawer();
  const tradesHeader = await page.$eval("#drawerBody table.tbl-detail thead", (e) => e.innerText.replace(/\s+/g, " ").trim());
  ok("4.1 实盘交易明细含「代码/股票」列", /代码/.test(tradesHeader) && /股票/.test(tradesHeader), tradesHeader);
  ok("4.2 实盘交易首行非空且有内容", s.tds.filter((t) => t && t !== "—").length >= 3, s.tds.join(" | ").slice(0, 110));

  await page.keyboard.press("Escape"); await wait(120);
  await page.click('#cards .card[data-kind="positions"]'); await wait(650);
  s = await drawer();
  const posHeader = await page.$eval("#drawerBody table.tbl-detail thead", (e) => e.innerText.replace(/\s+/g, " ").trim());
  ok("4.3 持仓记录明细含「选手/代码/仓位」列", /选手/.test(posHeader) && /代码/.test(posHeader) && /仓位/.test(posHeader), posHeader);

  /* ---------- 5. 行下钻：点持仓行 → 打开该股详情（标题变「股票 …」） ---------- */
  STEP = "drill";
  const before = s.title;
  await page.click("#drawerBody table.tbl-detail tbody tr.rowlink:first-child");
  await wait(900);
  const s2 = await drawer();
  ok("5. 点击明细行可下钻（标题切换为股票详情）",
     /^股票/.test(s2.title) && s2.title !== before, `"${before}" → "${s2.title}"`);

  /* ---------- 6. 关闭入口：Esc + 关闭按钮 ---------- */
  STEP = "close";
  await page.keyboard.press("Escape"); await wait(250);
  let closed = await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show"));
  ok("6.1 Esc 可关闭明细抽屉", closed);
  await page.click('#cards .card[data-kind="persons"]'); await wait(600);
  await page.click("#btnClose"); await wait(250);
  closed = await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show"));
  ok("6.2 关闭按钮可关闭明细抽屉", closed);

  /* ---------- 7. 键盘可达（Enter 打开） ---------- */
  STEP = "keyboard";
  await page.$eval('#cards .card[data-kind="topics"]', (e) => e.focus());
  await page.keyboard.press("Enter"); await wait(600);
  const sk = await drawer();
  ok("7. 卡片支持键盘 Enter 打开", sk.shown && sk.hasTable, sk.title);

  /* ---------- 8. 无 JS 运行时错误 ---------- */
  ok("8. 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  await page.keyboard.press("Escape"); await wait(150);
  await page.screenshot({ path: "stats_cards.png", clip: await (async () => {
    const r = await page.$eval("#cards", (e) => { const b = e.getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height }; });
    return r;
  })() });
  await page.click('#cards .card[data-kind="positions"]'); await wait(700);
  const dr = await page.$eval("#drawer", (e) => { const b = e.getBoundingClientRect(); return { x: Math.max(b.x, 0), y: Math.max(b.y, 0), width: b.width, height: b.height }; });
  await page.screenshot({ path: "stats_detail_drawer.png", clip: dr });

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + e.message); process.exit(2); });
