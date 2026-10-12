/* 回归护栏：今日决策首页（2026-10-12 体验审计 P0-A）+ 人气/看好三态（P0-C）+ 预测三件套（P0-B）
   用法：node verify_today.cjs   （需 8765 后端在监听）
   断言：默认落地=今日决策 / 一句话结论非空 / 四卡齐全 / 顶部数据量卡默认折叠 /
        预测三件套 3 项 / 讨论股票三态说明条 / 控制台零报错 */
module.paths.push("C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules");
const puppeteer = require("puppeteer-core");

const BASE = "http://127.0.0.1:8765/";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const fails = [];
const ok = (c, m) => { if (!c) fails.push(m); console.log((c ? "PASS " : "FAIL ") + m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(BASE, { waitUntil: "networkidle2", timeout: 60000 });
  await sleep(2500);

  // 0) app.js 没崩
  const navN = await page.$$eval("#sidenav .nav-item", (a) => a.length).catch(() => 0);
  ok(navN >= 7, "左栏渲染出 7 个大类（app.js 未崩）：" + navN);

  // 1) 默认落地 = 今日决策
  const active = await page.$eval("#subnav .sub-tab.active", (e) => e.textContent.trim()).catch(() => "");
  ok(active === "今日决策", "默认落在「今日决策」子页签：" + active);
  const todayActive = await page.$eval("#p-today", (e) => e.classList.contains("active")).catch(() => false);
  ok(todayActive, "#p-today 面板为 active");

  // 2) 一句话结论 + 四卡
  const verdict = await page.$eval("#p-today .td-verdict .td-v-text", (e) => e.textContent.trim()).catch(() => "");
  ok(verdict.length > 8, "一句话结论非空：" + verdict.slice(0, 60));
  const cardHeads = await page.$$eval("#p-today .td-card .td-hd b", (a) => a.map((x) => x.textContent.trim()));
  ok(cardHeads.length === 4, "四卡齐全：" + JSON.stringify(cardHeads));
  ok(cardHeads.join("").includes("今日方向") && cardHeads.join("").includes("今日机会")
    && cardHeads.join("").includes("今日风险") && cardHeads.join("").includes("我的票"), "四卡标题为 方向/机会/风险/我的票");
  const dirRows = await page.$$eval("#p-today .td-c-dir .td-row", (a) => a.length);
  ok(dirRows >= 1, "方向卡有真实条目：" + dirRows);
  const basis = await page.$eval("#p-today .td-basis .td-basistxt", (e) => e.textContent.trim()).catch(() => "");
  ok(basis.length > 20, "口径说明已在：长度 " + basis.length);

  // 3) 顶部数据量卡默认折叠（#cards 内部包一个 details.sys-status）
  const isDetails = await page.$eval("#cards details.sys-status", (e) => e.tagName.toLowerCase() === "details").catch(() => false);
  ok(isDetails, "#cards 内已生成折叠「系统状态」details");
  const cardsOpen = await page.$eval("#cards details.sys-status", (e) => e.open).catch(() => true);
  ok(cardsOpen === false, "系统状态默认收起（不占首屏）");
  const sumTxt = await page.$eval("#cards details.sys-status > summary", (e) => e.textContent.trim()).catch(() => "");
  ok(sumTxt.includes("系统状态"), "折叠标题为「系统状态 · 数据量」：" + sumTxt);
  const sysCards = await page.$$eval("#cards .sys-grid .card", (a) => a.length).catch(() => 0);
  ok(sysCards >= 9, "折叠内仍保留全部数据量卡：" + sysCards);

  // 4) 每日预测三件套
  await page.evaluate(() => { location.hash = "#overview/prediction"; });
  await sleep(3500);
  const trio = await page.$$eval("#p-prediction .pred-trio .pred-trio-item", (a) => a.map((x) => x.textContent));
  ok(trio.length === 3, "预测三件套为 3 项：" + trio.length);
  const trioTxt = trio.join(" | ");
  ok(trioTxt.includes("历史命中率"), "含历史命中率");
  ok(trioTxt.includes("样本 n="), "含样本量 n");
  ok(trioTxt.includes("该信几分"), "含「该信几分」判读");
  ok(trioTxt.includes("三层框架"), "含生成依据（三层框架）");

  // 5) 当前讨论股票三态说明条
  await page.evaluate(() => { location.hash = "#money/stocks"; });
  await sleep(3000);
  const ph = await page.$eval("#p-stocks .stk-posture-hint", (e) => e.textContent.trim()).catch(() => "");
  ok(ph.includes("人气") && ph.includes("被看好"), "讨论股票有人气≠看好的三态说明条");
  const badges = await page.$$eval("#p-stocks .stk-card .badge", (a) => a.map((x) => x.textContent.trim()));
  ok(badges.some((b) => ["看好", "看空", "分歧"].includes(b)), "卡片上出现 看好/看空/分歧 三态徽章：" + JSON.stringify(badges.slice(0, 8)));

  ok(errs.length === 0, "控制台零报错" + (errs.length ? "：" + errs.slice(0, 3).join(" || ") : ""));

  // 截图产物（今日决策首页全页）
  await page.evaluate(() => { location.hash = "#overview/today"; });
  await sleep(2200);
  await page.screenshot({ path: "_today_home.png", fullPage: true });

  await browser.close();
  console.log(fails.length ? "\nRESULT: FAIL (" + fails.length + ")" : "\nRESULT: ALL PASS");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("RUN ERROR:", e.message); process.exit(2); });
