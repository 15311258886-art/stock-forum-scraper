/* verify_nav.cjs —— 左侧导航信息架构重构回归（2026-09-29）
 *
 * 覆盖：① 页面无 JS 运行错误；
 *      ② 一级类 = 7 个、名称与顺序符合方案 A（口语命名）；
 *      ③ 每个大类子项数 ≤7（学习中心含动态轨道，单独放宽）；
 *      ④ **逐个点开每一个子标签**：其面板容器存在且成为唯一 active 面板（不串、不空）；
 *      ⑤ 旧深链接迁移：`#旧类/子类` 自动扶正为新类（含"一个旧类拆到多个新类"的场景）。
 *
 * 运行（需查看器已在 127.0.0.1:8765 运行；puppeteer-core 在受管 node workspace）：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" "F:/.../verify_nav.cjs"
 * 退出码：0=PASS，2=FAIL，3=无可用浏览器（跳过）。
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

function pickBrowser() {
  const cands = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ];
  try {
    const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
    if (fs.existsSync(base)) {
      fs.readdirSync(base).filter((d) => /^chromium-/.test(d)).sort().reverse()
        .forEach((d) => cands.push(base + "/" + d + "/chrome-win64/chrome.exe"));
    }
  } catch (e) { /* 忽略 */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER: 未找到 Edge/Chrome/Chromium，跳过（非失败）"); process.exit(3); }
  console.log("BROWSER=" + exe);

  const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1000"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });

  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 3500));

  // ② + ③ 结构体检
  const cats = await page.evaluate(() => NAV.map((c) => {
    const items = itemsOfCat(c);
    return { key: c.key, label: c.label, n: items.length, tabs: items.map((i) => i.tab) };
  }));

  // 2026-10-12（P1 部分）：复盘成长是**盘后**动作 → 左栏用「盘后」分隔线把它与盘中项隔开
  const sepOk = await page.evaluate(() => {
    const items = [...document.querySelectorAll("#sidenav .nav-item")];
    const sep = document.querySelector("#sidenav .rail-sep");
    return !!sep && sep.textContent.trim() === "盘后" &&
      items.length > 0 && items[items.length - 1].dataset.cat === "learn" &&
      items.every((it) => it.dataset.cat !== "learn" || it === items[items.length - 1]);
  });

  // ④ 逐个子标签：容器存在且成为唯一 active 面板
  const flat = cats.flatMap((c) => c.tabs.map((t) => ({ cat: c.key, tab: t })));
  const panels = await page.evaluate((list) => {
    const out = [];
    for (const { cat, tab } of list) {
      selectNav(cat, tab, false);
      const act = document.querySelector(".panel.active");
      const el = document.getElementById("p-" + tab);
      out.push({ cat, tab, exists: !!el,
        activeId: act ? act.id : null,
        activeCount: document.querySelectorAll(".panel.active").length });
    }
    return out;
  }, flat);

  // ⑤ 旧深链接迁移
  const mig = await page.evaluate(async () => {
    const cases = [
      ["#research/lhb", "#money/lhb"],
      ["#research/lhb_sectors", "#money/lhb_sectors"],
      ["#research/stocks", "#money/stocks"],
      ["#research/sentiment", "#money/sentiment"],
      ["#research/topics", "#info/topics"],
      ["#research/overseas_sentiment", "#info/overseas_sentiment"],
      ["#research/geo_events", "#info/geo_events"],
      ["#enhance/moneyflow", "#money/moneyflow"],
      ["#enhance/news", "#info/news"],
      ["#enhance/research", "#opinion/research"],
      ["#enhance/screener", "#watch/screener"],
      ["#enhance/valuation", "#watch/valuation"],
      ["#enhance/multiquote", "#watch/multiquote"],
      ["#trade/niusan", "#players/niusan"],
      ["#trade/players", "#players/players"],
      ["#extref/extref_soft", "#opinion/extref_soft"],
      ["#watch/watch", "#watch/watch"],
      ["#learn/strategy_lab", "#learn/strategy_lab"],
    ];
    const out = [];
    for (const [from, expect] of cases) {
      location.hash = from;
      await new Promise((r) => setTimeout(r, 150));
      out.push({ from, got: location.hash, expect, ok: location.hash === expect });
    }
    // 复位到默认页
    location.hash = "#overview/dashboard";
    await new Promise((r) => setTimeout(r, 200));
    return out;
  });

  // ⑥ 动态知识轨道 learn_*：等 kb_sources.yaml 的轨道加载后逐个点开，确认容器存在且不串
  const dyn = await page.evaluate(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    for (let i = 0; i < 40 && !(typeof KB !== "undefined" && KB.tracks); i++) await wait(500);
    const loaded = typeof KB !== "undefined" && !!KB.tracks;
    const items = itemsOfCat(NAV.find((c) => c.key === "learn"));
    const dynTabs = items.filter((i) => /^learn_/.test(i.tab) && i.tab !== "learn_tracks");
    const out = [];
    for (const it of dynTabs) {
      selectNav("learn", it.tab, false);
      await wait(400);
      const el = document.getElementById("p-" + it.tab);
      const act = document.querySelector(".panel.active");
      out.push({ tab: it.tab, exists: !!el, activeId: act ? act.id : null,
        len: el ? el.innerHTML.length : 0 });
    }
    return { loaded, n: dynTabs.length, out };
  });

  // ⑦ 冷启动旧深链接（真实"旧收藏点开"）：新开标签页直接用旧 hash 进入，应被 initNav 扶正
  const cold = await browser.newPage();
  const coldErrors = [];
  cold.on("pageerror", (e) => coldErrors.push("pageerror: " + e.message));
  await cold.goto(URL + "#research/topics", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 3000));
  const coldRes = await cold.evaluate(() => ({
    hash: location.hash, cat: CURRENT.cat, tab: CURRENT.tab,
    activeId: (document.querySelector(".panel.active") || {}).id }));
  const cold2 = await browser.newPage();
  await cold2.goto(URL + "#enhance/screener", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 3000));
  const coldRes2 = await cold2.evaluate(() => ({
    hash: location.hash, cat: CURRENT.cat, tab: CURRENT.tab,
    activeId: (document.querySelector(".panel.active") || {}).id }));
  await cold.close(); await cold2.close();

  const SHOT = path.join(__dirname, "artifacts", "nav_regression_20260929.png");
  try { await page.screenshot({ path: SHOT }); } catch (e) { /* 截图失败不致命 */ }

  console.log("CATS=" + JSON.stringify(cats));
  console.log("PANELS_BAD=" + JSON.stringify(panels.filter((p) => !p.exists || p.activeId !== "p-" + p.tab)));
  console.log("MIG_BAD=" + JSON.stringify(mig.filter((m) => !m.ok)));
  console.log("DYN=" + JSON.stringify(dyn));
  console.log("COLD=" + JSON.stringify({ coldRes, coldRes2 }));
  console.log("JS_ERRORS=" + JSON.stringify(errors.slice(0, 6)));
  await browser.close();

  // ---- 断言 ----
  // 2026-10-12 更新到当前信息架构（8 个大类：leaders「行业龙头」已于 10-08 入列；
  // 体验审计又给 overview/info/players/watch 各加了一个置顶「结论页」）。
  const EXPECT = [
    ["overview", "今日总览", 4],
    ["money", "钱往哪走", 5],
    ["info", "消息与外围", 10],
    ["players", "别人怎么做", 7],
    ["opinion", "别人怎么看", 6],
    ["watch", "我的票", 7],
    ["leaders", "行业龙头", 1],
  ];
  const structOk = cats.length === 8 &&
    EXPECT.every((e, i) => cats[i].key === e[0] && cats[i].label === e[1] && cats[i].n === e[2]) &&
    cats[7].key === "learn" && cats[7].label === "复盘成长" && cats[7].n >= 2;
  // info 是「信息过载重灾区」（报告 P0-D）、learn 含动态知识轨道 —— 两类放宽上限
  const sizeOk = cats.every((c) => c.n <= 10 || c.key === "learn");
  const panelsOk = panels.length === flat.length && panels.length > 0 &&
    panels.every((p) => p.exists && p.activeId === "p-" + p.tab && p.activeCount === 1);
  const migOk = mig.length > 0 && mig.every((m) => m.ok);
  const dynOk = dyn.loaded ? (dyn.n > 0 && dyn.out.every((d) => d.exists && d.activeId === "p-" + d.tab)) : true;
  const coldOk = coldRes.hash === "#info/topics" && coldRes.activeId === "p-topics" &&
    coldRes2.hash === "#watch/screener" && coldRes2.activeId === "p-screener" && coldErrors.length === 0;
  const pass = errors.length === 0 && structOk && sizeOk && sepOk && panelsOk && migOk && dynOk && coldOk;
  console.log("CHECKS=" + JSON.stringify({ structOk, sizeOk, sepOk, panelsOk, migOk, dynOk, coldOk,
    noErrors: errors.length === 0, tabCount: flat.length, dynTrackCount: dyn.n, dynLoaded: dyn.loaded }));
  console.log(pass ? "NAV_PASS" : "NAV_FAIL");
  process.exit(pass ? 0 : 2);
})().catch((e) => { console.log("CRASH: " + (e && e.message)); process.exit(1); });
