/* 股票论坛可视化查看器 —— 只读端到端 UI 回归（真实 Edge）
 * 设计原则（web-ui-regression 技能铁律）：
 *  - 全程只读：只发 GET、只做 DOM 交互；绝不点 #btnFetch / #btnOverseas /
 *    #btnRefreshNow / #btnLogin / 同步名称 / KB 重吸 / 载入示例 等写/抓取按钮。
 *  - 被测实例为隔离库 + 隔离端口（VIEWER_DB / VIEWER_PORT），不触碰生产数据。
 *  - 覆盖：核心功能、主要用户流程、边界场景 + 四类必测（遮挡/可点、多关闭入口、
 *    落位不遮挡、拖拽拉伸边界、文字压字、列宽）。
 *
 * 用法：node tests/ui_regression.js [port]
 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const PORT = process.argv[2] || "8799";
const URL = "http://127.0.0.1:" + PORT + "/";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => {
  out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);
  if (!c) console.log("  >> FAIL [" + STEP + "] " + n + (extra ? " :: " + extra : ""));
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  page.on("requestfailed", (r) => {
    const u = r.url();
    if (!u.includes("favicon")) errs.push("reqfail: " + u + " " + ((r.failure() || {}).errorText || ""));
  });

  STEP = "goto";
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 30000 });
  await sleep(800);

  // ---------- A. 加载与全局 ----------
  STEP = "global-checks";
  const title = await page.title();
  ok("页面标题正确", title.includes("股票讨论实时查看器"), "title=" + title);

  const globals = await page.evaluate(() => ({
    btnFetch: !!document.querySelector("#btnFetch"),
    btnOverseas: !!document.querySelector("#btnOverseas"),
    btnRefreshNow: !!document.querySelector("#btnRefreshNow"),
    btnLogin: !!document.querySelector("#btnLogin"),
    realtime: !!document.querySelector("#realtime"),
    schedAuto: !!document.querySelector("#schedAuto"),
    navItems: document.querySelectorAll("#sidenav .nav-item").length,
    cards: document.querySelectorAll("#cards .card").length,
    subTabs: document.querySelectorAll("#subnav .sub-tab").length,
    themeChips: document.querySelectorAll("#themePick .chip").length,
  }));
  ok("顶栏控制按钮齐全", globals.btnFetch && globals.btnOverseas && globals.btnRefreshNow && globals.btnLogin, JSON.stringify(globals));
  ok("左侧主导航已渲染", globals.navItems > 0, "navItems=" + globals.navItems);
  ok("统计卡片已渲染", globals.cards > 0, "cards=" + globals.cards);
  ok("主题切换器已渲染(>=3套)", globals.themeChips >= 3, "themeChips=" + globals.themeChips);
  ok("默认二级导航已渲染", globals.subTabs > 0, "subTabs=" + globals.subTabs);

  const active1 = await page.evaluate(() => { const a = document.querySelector(".panel.active"); return a ? a.id : null; });
  ok("默认激活面板存在", !!active1, "active=" + active1);

  const summaryTxt = await page.evaluate(() => document.querySelector("#p-summary")?.innerText || "");
  ok("汇总面板含数据(数字)", /\d/.test(summaryTxt), "len=" + summaryTxt.length);

  // ---------- B. 导航用户流程 ----------
  STEP = "navigation";
  await page.evaluate(() => {
    const el = [...document.querySelectorAll("#sidenav .nav-item")].find((n) => n.dataset.cat === "research");
    if (el) el.click();
  });
  await sleep(300);
  const subAfter = await page.evaluate(() => document.querySelectorAll("#subnav .sub-tab").length);
  ok("切换大类后二级导航重绘", subAfter > 0, "subTabs=" + subAfter);
  await page.evaluate(() => {
    const el = [...document.querySelectorAll("#subnav .sub-tab")].find((t) => t.dataset.tab === "stocks");
    if (el) el.click();
  });
  await sleep(300);
  const active2 = await page.evaluate(() => { const a = document.querySelector(".panel.active"); return a ? a.id : null; });
  ok("点击子标签切换到 p-stocks", active2 === "p-stocks", "active=" + active2);
  const stocksTxt = await page.evaluate(() => document.querySelector("#p-stocks")?.innerText || "");
  ok("行情研判-当前讨论股票 有内容", stocksTxt.length > 0, "len=" + stocksTxt.length);

  await page.evaluate(() => {
    const el = [...document.querySelectorAll("#sidenav .nav-item")].find((n) => n.dataset.cat === "overseas");
    if (el) el.click();
  });
  await sleep(300);
  await page.evaluate(() => {
    const el = [...document.querySelectorAll("#subnav .sub-tab")].find((t) => t.dataset.tab === "overseas_sentiment");
    if (el) el.click();
  });
  await sleep(300);
  const ovTxt = await page.evaluate(() => document.querySelector("#p-overseas_sentiment")?.innerText || "");
  ok("海外舆情面板有内容", ovTxt.length > 0, "len=" + ovTxt.length);

  // ---------- C. 抽屉：打开 + 四类必测 ----------
  // 激活总览并等待异步渲染完成（汇总面板经 API 异步渲染，需留出渲染时间）
  async function activateSummary() {
    await page.evaluate(() => { const el = [...document.querySelectorAll("#sidenav .nav-item")].find((n) => n.dataset.cat === "overview"); if (el) el.click(); });
    // 汇总面板经 /api/posts 异步渲染，行存在后才可点；轮询直到 tr[data-id] 出现（最多 3s）
    for (let k = 0; k < 30; k++) {
      const has = await page.evaluate(() => !!document.querySelector("#p-summary tr[data-id]"));
      if (has) break;
      await sleep(100);
    }
  }
  // 滚动行入视 → 点指定单元格（td.click 直接触发行 onclick，不依赖坐标）
  // 过滤 0 宽/隐藏列，并按「单元格中心在哪一半」挑真实可视单元格，避免首列被 CSS 隐藏导致锚点失效
  async function clickAnchorCell(side) {
    const info = await page.evaluate((side) => {
      const tr = document.querySelector("#p-summary tr[data-id]");
      if (!tr) return null;
      tr.scrollIntoView({ block: "center" });
      const vw = innerWidth;
      const tds = [...tr.querySelectorAll("td")].filter((td) => { const r = td.getBoundingClientRect(); return r.width > 0 && r.left >= 0 && r.right <= vw + 1; });
      if (!tds.length) return null;
      const centerOf = (td) => { const r = td.getBoundingClientRect(); return r.left + r.width / 2; };
      let td;
      if (side === "first") {
        td = tds.find((x) => centerOf(x) < vw / 2) || tds[0];
      } else {
        td = [...tds].reverse().find((x) => centerOf(x) > vw / 2) || tds[tds.length - 1];
      }
      const r = td.getBoundingClientRect();
      td.click();
      return {
        count: document.querySelectorAll("#p-summary tr[data-id]").length,
        anchor: { left: r.left, right: r.right, top: r.top, bottom: r.bottom, cx: r.left + r.width / 2, cy: r.top + r.height / 2 },
      };
    }, side);
    if (info) {
      // openDetail 走异步 /api/detail，需等抽屉真正出现再返回（避免时序竞态误判为未打开）
      for (let k = 0; k < 40; k++) {
        const sh = await page.evaluate(() => document.querySelector("#drawer").classList.contains("show"));
        if (sh) break;
        await sleep(100);
      }
    }
    return info;
  }
  STEP = "drawer-open";
  await activateSummary(); const base = await clickAnchorCell("first");
  await sleep(400);
  ok("汇总表存在可点击数据行", !!base && base.count > 0, base ? "rows=" + base.count : "NO ROWS");

  if (base && base.anchor) {
    // 点左侧单元格 → 抽屉应落到右侧（不遮挡来源）
    STEP = "drawer-place-left";
    let d = await page.evaluate(() => { const el = document.querySelector("#drawer"); const r = el.getBoundingClientRect(); return { open: el.classList.contains("show"), left: r.left, right: r.right }; });
    ok("点左侧单元格→抽屉打开", d.open, JSON.stringify(d));
    ok("点左侧单元格→抽屉落右侧且不遮挡来源", d.right <= base.anchor.right || d.left >= base.anchor.right,
       `drawer[l=${Math.round(d.left)},r=${Math.round(d.right)}] cellR=${Math.round(base.anchor.right)}`);
    STEP = "drawer-close-x";
    await page.evaluate(() => document.querySelector("#btnClose").click());
    await sleep(200);
    ok("关闭入口①：叉号可关闭", await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show")));

    // 点右侧单元格 → 抽屉应落到左侧且不压住所点单元格
    STEP = "drawer-place-right";
    await activateSummary(); const right = await clickAnchorCell("last");
    await sleep(400);
    d = await page.evaluate(() => { const el = document.querySelector("#drawer"); const r = el.getBoundingClientRect(); return { open: el.classList.contains("show"), left: r.left, right: r.right }; });
    ok("点右侧单元格→抽屉打开", d.open);
    ok("点右侧单元格→抽屉落左侧且不遮挡来源", d.right <= right.anchor.left || d.left >= right.anchor.left,
       `drawer[r=${Math.round(d.right)}] cellL=${Math.round(right.anchor.left)}`);
    STEP = "drawer-close-btn2";
    await page.evaluate(() => document.querySelector("#btnClose2").click());
    await sleep(200);
    ok("关闭入口②：底部按钮可关闭", await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show")));

    // 关闭入口③：Esc
    STEP = "drawer-close-esc";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    await page.keyboard.press("Escape"); await sleep(200);
    ok("关闭入口③：Esc 可关闭", await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show")));

    // 关闭入口④：点击面板外（确定性：在面板外元素上派发 pointerdown）
    STEP = "drawer-close-outside";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    await page.evaluate(() => { const el = document.querySelector("#statusHint"); if (el) el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })); });
    await sleep(200);
    ok("关闭入口④：点击面板外可关闭", await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show")));

    // 拖拽移动（标题栏，避开工具栏按钮）+ 边界钳制
    STEP = "drawer-drag";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    const title = await page.evaluate(() => { const r = document.querySelector("#drawerTitle").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    const before = await page.evaluate(() => { const r = document.querySelector("#drawer").getBoundingClientRect(); return { left: r.left, top: r.top }; });
    await page.mouse.move(title.x, title.y); await page.mouse.down();
    await page.mouse.move(title.x - 220, title.y - 60, { steps: 8 }); await page.mouse.up();
    await sleep(200);
    const after = await page.evaluate(() => { const el = document.querySelector("#drawer"); const r = el.getBoundingClientRect(); const rz = document.querySelector("#drawerResize").getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, rzRight: rz.right, rzBottom: rz.bottom, vw: innerWidth, vh: innerHeight }; });
    const moved = Math.abs(after.left - before.left) > 30 || Math.abs(after.top - before.top) > 30;
    ok("拖拽可移动抽屉", moved, `Δleft=${Math.round(after.left - before.left)} Δtop=${Math.round(after.top - before.top)}`);
    ok("拖拽后抽屉未越界", after.right <= after.vw + 2 && after.bottom <= after.vh + 2 && after.left >= -2 && after.top >= -2, JSON.stringify({ right: Math.round(after.right), vw: after.vw, bottom: Math.round(after.bottom), vh: after.vh }));
    ok("拖拽后右下拉伸手柄仍在视口内", after.rzRight <= after.vw + 2 && after.rzBottom <= after.vh + 2, `rzRight=${Math.round(after.rzRight)} rzBottom=${Math.round(after.rzBottom)}`);
    await page.evaluate(() => document.querySelector("#btnClose").click()); await sleep(150);

    // 拉伸：先把抽屉移到左上角留出放大空间，再拉伸
    STEP = "drawer-resize";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    const title2 = await page.evaluate(() => { const r = document.querySelector("#drawerTitle").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await page.mouse.move(title2.x, title2.y); await page.mouse.down();
    await page.mouse.move(Math.max(60, title2.x - 250), Math.max(60, title2.y - 30), { steps: 8 }); await page.mouse.up(); await sleep(200);
    const rzBefore = await page.evaluate(() => { const el = document.querySelector("#drawer"); return { w: el.offsetWidth, h: el.offsetHeight }; });
    const rz = await page.evaluate(() => { const r = document.querySelector("#drawerResize").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await page.mouse.move(rz.x, rz.y); await page.mouse.down();
    await page.mouse.move(Math.min(rz.x + 200, 1438), Math.min(rz.y + 160, 898), { steps: 10 }); await page.mouse.up(); await sleep(200);
    const rs = await page.evaluate(() => { const el = document.querySelector("#drawer"); const r = el.getBoundingClientRect(); return { w: el.offsetWidth, h: el.offsetHeight, right: r.right, bottom: r.bottom, vw: innerWidth, vh: innerHeight }; });
    ok("拉伸可放大抽屉(宽或高显著增大)", rs.w > rzBefore.w + 50 || rs.h > rzBefore.h + 50, `${rzBefore.w}x${rzBefore.h} → ${rs.w}x${rs.h}`);
    ok("拉伸后不越界", rs.right <= rs.vw + 2 && rs.bottom <= rs.vh + 2, JSON.stringify({ right: Math.round(rs.right), vw: rs.vw }));
    await page.evaluate(() => document.querySelector("#btnClose").click()); await sleep(150);

    // 拉伸到极小验证最小值
    STEP = "drawer-resize-min";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    const rz2 = await page.evaluate(() => { const r = document.querySelector("#drawerResize").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await page.mouse.move(rz2.x, rz2.y); await page.mouse.down();
    await page.mouse.move(rz2.x - 600, rz2.y - 600, { steps: 10 }); await page.mouse.up(); await sleep(200);
    const rsMin = await page.evaluate(() => { const el = document.querySelector("#drawer"); return { w: el.offsetWidth, h: el.offsetHeight }; });
    ok("拉伸有最小值下限(w>=320,h>=220)", rsMin.w >= 320 && rsMin.h >= 220, JSON.stringify(rsMin));
    await page.evaluate(() => document.querySelector("#btnClose").click()); await sleep(150);

    // 最大化
    STEP = "drawer-max";
    await activateSummary(); await clickAnchorCell("first"); await sleep(300);
    await page.evaluate(() => document.querySelector("#btnMax").click());
    await sleep(200);
    const mx = await page.evaluate(() => { const el = document.querySelector("#drawer"); const r = el.getBoundingClientRect(); return { maxed: el.classList.contains("maxed"), w: Math.round(r.width), vw: innerWidth }; });
    ok("最大化按钮生效(.maxed + 近满宽)", mx.maxed && Math.abs(mx.w - (mx.vw - 24)) <= 4, JSON.stringify(mx));
    await page.evaluate(() => document.querySelector("#btnClose").click()); await sleep(150);
  }

  // ---------- D. 弹层（手动粘贴 Cookie 模态）打开/关闭 ----------
  STEP = "modal";
  const modalOk = await page.evaluate(() => {
    try {
      if (typeof openCookieImport === "function") { openCookieImport("taoguba"); return !!document.querySelector(".modal-overlay"); }
      return false;
    } catch (e) { return "ERR:" + e.message; }
  });
  ok("Cookie 导入弹层可打开", modalOk === true, "res=" + modalOk);
  if (modalOk === true) {
    await page.evaluate(() => { const b = [...document.querySelectorAll(".modal-overlay button")].find((x) => x.textContent.includes("取消")); if (b) b.click(); });
    await sleep(200);
    const gone = await page.evaluate(() => !document.querySelector(".modal-overlay"));
    ok("弹层「取消」可关闭", gone);
  }

  // ---------- E. 主题切换（计算样式验证，非仅靠截图） ----------
  STEP = "theme";
  const themeResults = await page.evaluate(async () => {
    const ids = [...document.querySelectorAll("#themePick .chip")].map((c) => c.dataset.id);
    const res = [];
    for (const id of ids) {
      const chip = document.querySelector(`#themePick .chip[data-id="${id}"]`);
      if (!chip) continue;
      chip.click();
      await new Promise((r) => setTimeout(r, 120));
      res.push({ id, href: document.querySelector("#theme-link").getAttribute("href"), bg: getComputedStyle(document.body).backgroundColor });
    }
    return res;
  });
  const hrefs = new Set(themeResults.map((r) => r.href));
  ok("切换主题后样式表随之变化(>=3种不同href)", hrefs.size >= 3, [...hrefs].join(" | "));
  const bgs = new Set(themeResults.map((r) => r.bg));
  ok("切换主题后 body 背景色确有变化(>=2种)", bgs.size >= 2, [...bgs].join(" | "));

  // ---------- F. 文字压字/溢出检查 ----------
  STEP = "overflow";
  const cardOverflow = await page.evaluate(() => {
    let bad = 0, total = 0;
    document.querySelectorAll("#cards .card").forEach((c) => { total++; if (c.scrollWidth > c.clientWidth + 2) bad++; });
    return { bad, total };
  });
  ok("统计卡片无横向溢出", cardOverflow.bad === 0, JSON.stringify(cardOverflow));

  // ---------- 截图留证（组件级） ----------
  STEP = "screenshot";
  await page.evaluate(() => { const el = [...document.querySelectorAll("#sidenav .nav-item")].find((n) => n.dataset.cat === "overview"); if (el) el.click(); });
  await sleep(300);
  await page.screenshot({ path: "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/tests/qa_iso/shot_summary.png" });
  await activateSummary(); const shotBase = await clickAnchorCell("first");
  await sleep(400);
  const dr = await page.$("#drawer");
  if (dr) await dr.screenshot({ path: "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/tests/qa_iso/shot_drawer.png" });
  await page.evaluate(() => document.querySelector("#btnClose")?.click());

  ok("全流程无 JS 运行时错误", errs.length === 0, errs.slice(0, 5).join(" ; ") || "无");

  console.log("\n" + out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== RESULT OK=${out.length - fails} FAIL=${fails} ===`);
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + e.message); console.log(out.join("\n")); process.exit(2); });
