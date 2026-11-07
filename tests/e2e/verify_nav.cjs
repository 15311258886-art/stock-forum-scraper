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
  page.on("console", (m) => {
    // 只排除 browser 对 <meta> 投递 CSP 的固有提示（与 JS 逻辑无关），其余照收
    if (m.type() === "error" && !/frame-ancestors/.test(m.text())) errors.push("console.error: " + m.text());
  });

  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 3500));

  // ② + ③ 结构体检
  const cats = await page.evaluate(() => NAV.map((c) => {
    const items = itemsOfCat(c);
    return {
      key: c.key, label: c.label, n: items.length, tabs: items.map((i) => i.tab),
      // 平铺/折叠拆分：收敛是否生效看这两个数，不看总数
      main: items.filter((i) => !i.fold && !i.child).length,
      fold: items.filter((i) => i.fold).length,
      // 静态项（NAV 里写死的）与运行时动态项（知识轨道 / KB 固定子板块）必须分开数：
      // 「43 个 tab 一个不少」说的是静态项；动态项取决于 kb_sources.yaml，不能当锚点。
      staticN: c.items.length,
    };
  }));

  // 2026-10-12（P1 部分）：复盘成长是**盘后**动作 → 左栏用「盘后」分隔线把它与盘中项隔开
  // 2026-11-01（P1-2 导航收敛 7 类 → 6 组）：分隔线现在标的是 `pinned` 的「数据与系统」，
  // 不再是 learn（复盘成长）—— 前者是「盘后/出问题时才看」，后者已并入「策略与实证」。
  const sepOk = await page.evaluate(() => {
    const items = [...document.querySelectorAll("#sidenav .nav-item")];
    const sep = document.querySelector("#sidenav .rail-sep");
    return !!sep && sep.textContent.trim() === "盘后" &&
      items.length > 0 && items[items.length - 1].dataset.cat === "system" &&
      items.every((it) => it.dataset.cat !== "system" || it === items[items.length - 1]);
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
  // 2026-11-01（P1-2）：期望值不再手写 —— 7 类并成 6 组后，任何一张手抄的「旧→新」表都会过期，
  // 而且过期时给出的红是假红（面板其实到对了，只是类名换了）。改为**现算**：
  // 旧类名 + tab 拼出来的 hash，最终必须落在 `#<TAB_HOME[tab]>/<tab>`，且**tab 一字不变**。
  // 这样"落错面板"照样抓得到，但"类名搬家"不会再被误报。
  const mig = await page.evaluate(async () => {
    const OLD = ["overview", "money", "info", "opinion", "watch", "research", "learn", "trade", "extref", "enhance"];
    const tabs = Object.keys(TAB_HOME);
    const cases = [];
    for (const tab of tabs) for (const old of OLD) cases.push(["#" + old + "/" + tab, "#" + TAB_HOME[tab] + "/" + tab]);
    const out = [];
    for (const [from, expect] of cases) {
      location.hash = from;
      await new Promise((r) => setTimeout(r, 40));
      out.push({ from, got: location.hash, expect, ok: location.hash === expect });
    }
    // 复位到默认页
    location.hash = "#today/today";
    await new Promise((r) => setTimeout(r, 200));
    return out;
  });

  // ⑥ 动态知识轨道 learn_*：等 kb_sources.yaml 的轨道加载后逐个点开，确认容器存在且不串
  const dyn = await page.evaluate(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    for (let i = 0; i < 40 && !(typeof KB !== "undefined" && KB.tracks); i++) await wait(500);
    const loaded = typeof KB !== "undefined" && !!KB.tracks;
    // 2026-11-01：知识轨道所在组已从 learn 改名 strategy（DYN_CATS 的 key）；从 DYN_CATS 现取，别再写死。
    const dynCat = NAV.find((c) => DYN_CATS[c.key]);
    const items = itemsOfCat(dynCat);
    const dynTabs = items.filter((i) => /^learn_/.test(i.tab) && i.tab !== "learn_tracks");
    const out = [];
    for (const it of dynTabs) {
      selectNav(dynCat.key, it.tab, false);
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
  await cold.goto(URL + "#research/lhb", { waitUntil: "domcontentloaded", timeout: 30000 });
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
  // 2026-11-01（P1-2 导航收敛）：一级导航由 7 类改为 6 组，且**每组的项数不再手抄**。
  // 手抄项数是这张表以前最容易腐烂的地方：往 NAV 加一个 tab，这里就红一次，
  // 而红的原因和"导航坏了"完全无关。故只固定「组 key + 组名 + 顺序」，项数交给 TOTAL。
  const EXPECT = [
    ["today", "今日交易计划"],
    ["mine", "我的持仓与自选"],
    ["market", "市场与题材"],
    ["stock", "个股研究"],
    ["strategy", "策略与实证"],
    ["system", "数据与系统"],
  ];
  const TOTAL = 43;   // 静态 tab 总数锚点（少一个就是少一个面板）
  const totalNow = cats.reduce((s, c) => s + c.staticN, 0);   // 只数静态项（锚点 43）
  const totalAll = cats.reduce((s, c) => s + c.n, 0);          // 含动态轨道，仅供参考
  const structOk = cats.length === EXPECT.length &&
    EXPECT.every((e, i) => cats[i].key === e[0] && cats[i].label === e[1]) &&
    totalNow === TOTAL;
  // 每组「平铺 ≤4 个主项」是收敛生效的判据（system 只有 1 项，无平铺压力）
  const sizeOk = cats.every((c) => c.n === 1 || (c.main >= 1 && c.main <= 4));
  const panelsOk = panels.length === flat.length && panels.length > 0 &&
    panels.every((p) => p.exists && p.activeId === "p-" + p.tab && p.activeCount === 1);
  const migOk = mig.length > 0 && mig.every((m) => m.ok);
  const dynOk = dyn.loaded ? (dyn.n > 0 && dyn.out.every((d) => d.exists && d.activeId === "p-" + d.tab)) : true;
  // 冷启动（旧书签直接打开）：hash 会被扶正到 tab 的新归属组，面板必须是同一个
  // 冷启动（旧书签直接打开）：hash 被扶正到 tab 的新归属组，**面板必须是同一个**。
  // 用 lhb / screener 两个仍然在导航里的 tab 做样本（原样本 topics 已于 2026-10 停止抓取、撤下导航）。
  const coldOk = coldRes.activeId === "p-lhb" && /^#(today|mine|market|stock|strategy|system)\/lhb$/.test(coldRes.hash) &&
    coldRes2.activeId === "p-screener" && /^#(today|mine|market|stock|strategy|system)\/screener$/.test(coldRes2.hash) &&
    coldErrors.length === 0;
  const pass = errors.length === 0 && structOk && sizeOk && sepOk && panelsOk && migOk && dynOk && coldOk;
  console.log("CHECKS=" + JSON.stringify({ structOk, sizeOk, sepOk, panelsOk, migOk, dynOk, coldOk,
    noErrors: errors.length === 0, tabCount: flat.length, totalNow, totalAll, dynTrackCount: dyn.n, dynLoaded: dyn.loaded }));
  console.log(pass ? "NAV_PASS" : "NAV_FAIL");
  process.exit(pass ? 0 : 2);
})().catch((e) => { console.log("CRASH: " + (e && e.message)); process.exit(1); });
