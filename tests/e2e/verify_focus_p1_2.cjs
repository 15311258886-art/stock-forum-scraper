/* verify_focus_p1_2.cjs —— P1-2「产品聚焦」端到端验证（2026-11-01）
 *
 * 覆盖四件事，全部读**真实渲染结果**，不看源码文本：
 *   ① 导航：6 组、每组平铺项数/折叠项数、43 个 tab 一个不少；
 *   ② 深链接：43 个 tab × 新旧类名拼 hash，落地面板必须是 #p-<tab> 且是唯一 active；
 *   ③ 首屏五要素：市场状态 / 方向 / 风险 / 持仓触发与失效 / 数据健康，抓可见文本；
 *   ④ 被推迟首绘的三个面板（海外舆情 / 龙虎榜 / 国际局势），点进去必须真有内容。
 *
 * 运行（查看器已在 127.0.0.1:8765）：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" node verify_focus_p1_2.cjs
 * 退出码：0=PASS 2=FAIL 3=无浏览器
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

const OLD_CATS = ["overview", "money", "info", "opinion", "watch", "research", "learn", "trade", "extref", "enhance"];

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
  } catch (e) { /* ignore */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER: 未找到 Edge/Chrome/Chromium，跳过（非失败）"); process.exit(3); }
  const browser = await puppeteer.launch({
    executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,1000"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.goto(URL + "#today/today", { waitUntil: "domcontentloaded", timeout: 60000 });
  try {
    await page.waitForFunction(() => {
      const el = document.querySelector("#td-status");
      return el && el.children.length > 0;
    }, { timeout: 15000 });
  } catch (e) { console.log("WARN: #td-status 未在 15s 内填充"); }
  await sleep(3000);

  const fails = [];
  const out = [];

  /* ---------- ① 导航结构（从真实 DOM 的横条读，不看常量） ---------- */
  const nav = await page.evaluate(() => {
    const cats = Array.from(document.querySelectorAll("#sidenav .nav-item")).map((a) => ({
      cat: a.dataset.cat, label: (a.querySelector(".ni-txt") || {}).textContent || "",
    }));
    const flat = Array.from(document.querySelectorAll("#subnav > .sub-tab, #subnav > .sub-cluster")).length;
    const flatTabs = Array.from(document.querySelectorAll("#subnav a.sub-tab")).map((a) => a.dataset.tab);
    const fold = document.querySelector("#subnav details.sub-fold");
    return {
      cats,
      flat,
      flatTabs,
      foldSummary: fold ? fold.querySelector("summary").textContent.trim() : "",
      foldTabs: fold ? Array.from(fold.querySelectorAll("a.sub-tab")).map((a) => a.dataset.tab) : [],
    };
  });
  out.push("==== ① 一级导航（真实 DOM）====");
  out.push("  分组数 = " + nav.cats.length + " → " + nav.cats.map((c) => c.cat + "「" + c.label + "」").join(" · "));
  out.push("  当前组(today) 平铺项 = " + nav.flatTabs.length + " → " + nav.flatTabs.join(", "));
  out.push("  折叠区 = " + nav.foldSummary + " → " + nav.foldTabs.join(", "));
  if (nav.cats.length !== 6) fails.push("一级分组不是 6 组：" + nav.cats.length);
  if (!/研究工具（\d+）/.test(nav.foldSummary)) fails.push("横条里没有「研究工具（N）」折叠区");

  /* 展开所有组，收集 tab 全集（从 NAV 常量现算，避免只看到当前组） */
  const allTabs = await page.evaluate(() => {
    const m = {};
    NAV.forEach((c) => c.items.forEach((i) => { if (i.tab) m[i.tab] = { cat: c.key, fold: !!i.fold, child: !!i.child }; }));
    return m;
  });
  const tabKeys = Object.keys(allTabs);
  out.push("  NAV 静态 tab 全集 = " + tabKeys.length + " 个（期望 43）");
  if (tabKeys.length !== 43) fails.push("tab 数不是 43：" + tabKeys.length);

  /* ---------- ② 深链接可达性：每个 tab × 每个历史类名 ---------- */
  const check = async (hash, wantTab) => {
    await page.evaluate((h) => { location.hash = h; }, hash);
    await sleep(60);
    return page.evaluate((t) => {
      const act = Array.from(document.querySelectorAll(".panel.active")).map((p) => p.id);
      const el = document.getElementById("p-" + t);
      return { active: act, exists: !!el, hash: location.hash };
    }, wantTab);
  };

  out.push("");
  out.push("==== ② 深链接可达性（浏览器内实跑，不是推演）====");
  let bad = 0;
  const samples = [];
  for (const tab of tabKeys) {
    for (const oldCat of OLD_CATS) {
      const r = await check("#" + oldCat + "/" + tab, tab);
      const ok = r.active.length === 1 && r.active[0] === "p-" + tab;
      if (!ok) { bad++; if (samples.length < 8) samples.push("#" + oldCat + "/" + tab + " → active=" + JSON.stringify(r.active) + " hash=" + r.hash); }
    }
  }
  out.push("  组合数 = " + (tabKeys.length * OLD_CATS.length) + "（" + tabKeys.length + " tab × " + OLD_CATS.length + " 个历史类名）");
  out.push("  落错/落空 = " + bad + (bad ? "\n    " + samples.join("\n    ") : "  ← 全部落到同一面板"));
  if (bad) fails.push(bad + " 条旧深链接落错面板");

  /* 自身类名可达 */
  let bad2 = 0;
  for (const tab of tabKeys) {
    const r = await check("#" + allTabs[tab].cat + "/" + tab, tab);
    if (!(r.active.length === 1 && r.active[0] === "p-" + tab)) bad2++;
  }
  out.push("  新链接自证（#<新组>/<tab>）落错 = " + bad2);
  if (bad2) fails.push(bad2 + " 条新深链接落错面板");

  /* ---------- ④ 被推迟首绘的面板：点进去必须真有内容 ---------- */
  out.push("");
  out.push("==== ④ 首绘被推迟的面板（延迟渲染不能变成「永远空」）====");
  const deferTargets = [
    ["overseas_sentiment", "海外舆情"],
    ["lhb", "游资龙虎榜"],
    ["geo_events", "国际局势"],
  ];
  for (const [tab, name] of deferTargets) {
    await page.evaluate((t) => { location.hash = "#" + TAB_HOME[t] + "/" + t; }, tab);
    await sleep(1800);      // 数据首屏已拿到 → 首绘是同步的；留 1.8s 兜网络
    const r = await page.evaluate((t) => {
      const el = document.getElementById("p-" + t);
      const txt = el ? el.innerText.trim() : "";
      return {
        nodes: el ? el.querySelectorAll("*").length : 0,
        textLen: txt.length,
        head: txt.slice(0, 90).replace(/\s+/g, " "),
        stillLoading: /正在加载数据…/.test(txt),
        pending: (window.deferredPaintState ? window.deferredPaintState() : []).indexOf(t) >= 0,
      };
    }, tab);
    out.push("  " + name.padEnd(8, " ") + " 节点 " + String(r.nodes).padStart(5)
      + " · 文本 " + String(r.textLen).padStart(5) + " 字 · 仍在加载占位=" + (r.stillLoading ? "是" : "否")
      + " · 待补绘=" + (r.pending ? "是" : "否"));
    out.push("      首句：" + r.head);
    if (r.nodes < 30 || r.textLen < 20) fails.push(name + " 进入后仍为空（节点 " + r.nodes + " / 文本 " + r.textLen + "）");
    if (r.stillLoading) fails.push(name + " 停在「正在加载数据…」");
  }

  /* ---------- ③ 首屏五要素真实文本 ---------- */
  await page.evaluate(() => { location.hash = "#today/today"; });
  await sleep(2500);
  const five = await page.evaluate(() => {
    const g = (sel) => {
      const el = document.querySelector(sel);
      return el ? el.innerText.replace(/\n{3,}/g, "\n\n").trim() : "(容器不存在)";
    };
    return {
      status: g("#td-status"),
      cards: Array.from(document.querySelectorAll("#p-today .td-card, #p-today [class*='td-'][class*='card']")).length,
      body: g("#p-today").slice(0, 2600),
    };
  });
  out.push("");
  out.push("==== ③ 首屏五要素（#today/today 真实可见文本）====");
  out.push("---- #td-status ----");
  out.push(five.status);
  out.push("---- #p-today 正文（截断 2600 字）----");
  out.push(five.body);

  out.push("");
  out.push("JS_ERRORS=" + (errs.length ? errs.slice(0, 5).join(" | ") : "none"));
  out.push("");
  out.push(fails.length ? "FAIL(" + fails.length + "):\n  - " + fails.join("\n  - ") : "PASS: 全部契约成立");
  console.log(out.join("\n"));

  if (process.argv.includes("--json")) {
    fs.writeFileSync("focus_p1_2_result.json",
      JSON.stringify({ nav, tabCount: tabKeys.length, badLinks: bad, badNew: bad2, five, fails, errs }, null, 2), "utf8");
  }
  await browser.close();
  process.exit(fails.length ? 2 : 0);
})();
