/* 回归护栏：入手概率「最值得入手」格的口径改造（2026-10-12 进哥拍板 A+B）
   用法：node verify_rate_buy.cjs （需 8765 后端在监听）

   断言的核心是**逻辑等价**，不是某几个数字：
     A) 旧文案「已到/跌破买点，可分批买入」必须消失；改为「已到测算买点 / 低于测算买点 X%」
     B) 出现「可分批」的行 ⟺ 该行档位 ∈ {A, A+} 且 dist ≤ 0（双确认）
     并检查 title 摊开了依据（提到"不含"）+ 图例存在 + 零控制台报错。 */
module.paths.push("C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules");
const puppeteer = require("puppeteer-core");

const BASE = "http://127.0.0.1:8765/";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const fails = [];
const ok = (c, m) => { if (!c) fails.push(m); console.log((c ? "PASS " : "FAIL ") + m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new", args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1700, height: 1100 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(BASE + "#watch/rate", { waitUntil: "networkidle2", timeout: 90000 });
  await sleep(14000);   // 该页首跑要取全市场 K 线（有磁盘缓存后较快）

  const data = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("#p-rate .wt-tb tbody tr")];
    return rows.map((tr) => {
      const tds = [...tr.children];
      // 档位取自 .gr-chip 里的 <b>（渲染为「<b>A</b>积极关注」，中间没有空格，
      // 早先按 textContent.split(/\s+/)[0] 取会得到 "A积极关注" —— 护栏自身的坑，已改）
      const gb = tr.querySelector(".gr-chip b");
      const cell = tr.querySelector(".bp-d");
      return {
        grade: gb ? gb.textContent.trim() : "",
        tip: cell ? cell.textContent.trim() : "",
        buy: !!(cell && cell.classList.contains("bp-buy")),
        at: !!(cell && cell.classList.contains("bp-at")),
        title: cell ? (cell.getAttribute("title") || "") : "",
      };
    });
  });

  const txt = await page.$eval("#p-rate", (e) => e.textContent);
  ok(data.length > 0, "入手概率表已渲染，行数=" + data.length);
  ok(!txt.includes("可分批买入"), "旧文案「已到/跌破买点，可分批买入」已消失");
  ok(!txt.includes("已到/跌破买点"), "旧措辞「已到/跌破买点」已消失");

  const reached = data.filter((r) => /已到测算买点|低于测算买点/.test(r.tip));
  ok(reached.length > 0, "出现新措辞「已到测算买点 / 低于测算买点 X%」，共 " + reached.length + " 行");
  ok(data.every((r) => !r.tip.includes("可分批") || r.buy), "凡带「可分批」的行都挂了双确认样式 bp-buy");

  // B 的核心：双确认 ⟺ 档位∈{A,A+} 且 已到；逐行核对，不容例外
  const bad = data.filter((r) => {
    const strong = r.grade === "A" || r.grade === "A+";
    const isReached = /已到测算买点|低于测算买点/.test(r.tip);
    return r.buy !== (strong && isReached);
  });
  ok(bad.length === 0, "双确认判定逐行一致（档位≥A 且 价格到位 ⟺ 可分批）" +
    (bad.length ? "；反例 " + JSON.stringify(bad.slice(0, 3)) : ""));

  const buys = data.filter((r) => r.buy);
  console.log(`  （info）双确认「可分批」${buys.length} 行 / 价格到位未双确认 ${reached.length - buys.length} 行 / 共 ${data.length} 行`);

  // A：依据摊开（title 必须说明"不含"什么，避免被当成综合结论）
  const withTitle = data.filter((r) => r.title.length > 20);
  ok(withTitle.length === data.length, "每格 title 都摊开了依据（" + withTitle.length + "/" + data.length + "）");
  ok(data.some((r) => /不含换手率/.test(r.title)), "title 里明写「不含换手率/量能/资金流/情绪/板块」");
  ok(data.some((r) => /近20日振幅|近 20 日振幅/.test(r.title)), "title 里写明了买点算法（近20日振幅…）");

  const legend = await page.$eval("#p-rate .rate-bp-legend", (e) => e.textContent.trim()).catch(() => "");
  ok(legend.includes("双确认") && legend.includes("不含"), "表下口径说明条已出（含「双确认」「不含…」）");

  // 手填覆盖不能被我改坏
  const hasInput = await page.$$eval("#p-rate .bp-in", (a) => a.length);
  ok(hasInput === data.length, "每行仍有手填覆盖输入框（" + hasInput + "）");

  ok(errs.length === 0, "控制台零报错" + (errs.length ? "：" + errs.slice(0, 3).join(" || ") : ""));

  await page.screenshot({ path: "_rate_buy.png", fullPage: false });
  await browser.close();
  console.log(fails.length ? "\nRESULT: FAIL (" + fails.length + ")" : "\nRESULT: ALL PASS");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("RUN ERROR:", e.message); process.exit(2); });
