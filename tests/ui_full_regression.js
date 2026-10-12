/**
 * UI 全面回归（真实 Chromium + 真实布局矩形）—— 只读，不点任何写接口按钮
 *
 * 覆盖：
 *   1) 10 个 tab 渲染 + 无 undefined/NaN/null/Invalid Date + 无 JS 报错
 *   2) 三种详情抽屉（股票 / 人物 / 选手）× 四种关闭入口 + 拖拽 + 拉伸(把手留视口) + 不遮挡所点行
 *   3) 四主题：文字色≠背景色（对比度）+ 元素不消失/不错位
 *   4) 空态（接口返回 []）与错误态（接口 500 / 非 JSON / 缺字段）—— 用请求拦截在“测试页”内构造，不动服务端
 *   5) 响应式 1024x700 / 1440x1000：无横向溢出 + 顶栏按钮 elementFromPoint 可命中
 *   6) 长文本压力
 *
 * 用法：NODE_PATH=... node tests/ui_full_regression.js [端口]
 * 产出：tools/_qa_ui_regression.txt, tools/shots_ui/*.png
 */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8765";
const URL = `http://127.0.0.1:${PORT}/index.html`;
const EXE = "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe";
const ROOT = path.join(__dirname, "..");
const SHOT = path.join(ROOT, "tools", "shots_ui");
const REPORT = path.join(ROOT, "tools", "_qa_ui_regression.txt");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra !== undefined ? "  —— " + extra : ""}`);
const info = (n, extra) => out.push(`INFO  ${n}${extra !== undefined ? "  —— " + extra : ""}`);
const skip = (n, extra) => out.push(`SKIP  ${n}${extra !== undefined ? "  —— " + extra : ""}`);

const TABS = ["summary", "stocks", "operating", "players", "consensus", "persons",
  "sentiment", "topics", "overseas_sentiment", "overseas_hotspots"];
const TAB_LABEL = { summary: "汇总", stocks: "当前讨论股票", operating: "正在操作股票", players: "实盘选手",
  consensus: "共同持仓共识", persons: "相关人物", sentiment: "舆论情绪", topics: "近期热点",
  overseas_sentiment: "美股港股舆情", overseas_hotspots: "近期热点(国外驱动)" };
const BAD_TOKENS = [/\bundefined\b/, /\bNaN\b/, /\bInvalid Date\b/, /\bnull\b/];

// 颜色对比度（WCAG）
function parseRGB(s) {
  const m = /rgba?\(([^)]+)\)/.exec(s || "");
  if (!m) return null;
  const p = m[1].split(",").map((x) => parseFloat(x));
  return [p[0], p[1], p[2]];
}
function lum(rgb) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
}
function contrast(fg, bg) {
  const a = parseRGB(fg), b = parseRGB(bg);
  if (!a || !b) return null;
  const l1 = lum(a), l2 = lum(b);
  const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
  return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
}

(async () => {
  if (!fs.existsSync(SHOT)) fs.mkdirSync(SHOT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });

  /* ========================================================================
   * 第一部分：主页面（真实数据）
   * ===================================================================== */
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  page.on("requestfailed", (r) => { if (/\/api\//.test(r.url())) errs.push("reqfailed: " + r.url()); });

  STEP = "goto";
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);

  /* ---------- 1. 各 tab 渲染 ---------- */
  out.push("========== 一、tab 切换（10 个） ==========");
  const tabRes = {};
  for (const t of TABS) {
    STEP = "tab " + t;
    const before = errs.length;
    await page.click(`.tab[data-tab="${t}"]`);
    await wait(320);
    const r = await page.evaluate((tab) => {
      const p = document.querySelector("#p-" + tab);
      const act = document.querySelector(".panel.active");
      const txt = (p && p.innerText) || "";
      return {
        isActive: act === p,
        textLen: txt.trim().length,
        nodes: p ? p.querySelectorAll("*").length : 0,
        first80: txt.replace(/\s+/g, " ").trim().slice(0, 80),
        overflow: p ? p.scrollWidth - p.clientWidth : 0,
        tabActive: (document.querySelector(`.tab[data-tab="${tab}"]`) || {}).className || "",
      };
    }, t);
    // 全文扫描（对中文 UI 无合法 "undefined" 场景）
    const fullBad = await page.evaluate((tab) => {
      const p = document.querySelector("#p-" + tab);
      const txt = (p && p.innerText) || "";
      const hits = {};
      for (const [k, re] of Object.entries({ undefined: /undefined/, NaN: /NaN/, invalidDate: /Invalid Date/, null: /\bnull\b/ })) {
        const m = txt.match(re);
        if (m) hits[k] = (txt.match(new RegExp(re.source, "g")) || []).length;
      }
      return hits;
    }, t);
    tabRes[t] = Object.assign(r, { badTokens: fullBad, newErrs: errs.length - before });
    const pass = r.isActive && r.textLen > 0 && Object.keys(fullBad).length === 0 && errs.length === before;
    ok(`[tab ${t}] ${TAB_LABEL[t]}：渲染内容/无脏字符/无报错`, pass,
      `active=${r.isActive} 文字=${r.textLen}字 节点=${r.nodes} 溢出=${r.overflow}px 脏=${JSON.stringify(fullBad)} 新增错=${errs.length - before} ｜ ${r.first80}`);
  }

  /* ---------- 2. 抽屉：三种类型 × 四条关闭入口 ---------- */
  out.push("");
  out.push("========== 二、详情抽屉（股票 / 人物 / 选手） ==========");
  const drawerState = () => page.evaluate(() => {
    const d = document.querySelector("#drawer");
    const r = d.getBoundingClientRect();
    const rz = document.querySelector("#drawerResize").getBoundingClientRect();
    return {
      shown: d.classList.contains("show"), display: getComputedStyle(d).display,
      left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom),
      w: Math.round(r.width), h: Math.round(r.height),
      title: document.querySelector("#drawerTitle").textContent,
      text: (document.querySelector("#drawerBody").innerText || "").replace(/\s+/g, " ").slice(0, 120),
      bodyLen: (document.querySelector("#drawerBody").innerText || "").trim().length,
      bad: ["undefined", "NaN", "Invalid Date"].filter((k) => (document.querySelector("#drawerBody").innerText || "").includes(k)),
      resizeHandleInView: rz.left >= 0 && rz.top >= 0 && rz.right <= innerWidth + 1 && rz.bottom <= innerHeight + 1,
      headTopInView: document.querySelector("#drawerHead").getBoundingClientRect().top >= -1,
    };
  });

  const openCases = [
    { name: "股票", tab: "stocks", sel: "#p-stocks .stk-card[data-sym]" },
    { name: "人物", tab: "persons", sel: "#p-persons tbody tr[data-name] td:nth-child(1)" },
    { name: "选手", tab: "players", sel: "#p-players tbody tr[data-name] td:nth-child(2)" },
  ];
  for (const cs of openCases) {
    STEP = "drawer open " + cs.name;
    await page.evaluate(() => document.querySelector("#drawer").classList.remove("show"));
    await page.click(`.tab[data-tab="${cs.tab}"]`); await wait(260);
    const anchorRect = await page.$eval(cs.sel, (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
    });
    await page.click(cs.sel); await wait(900);
    let s = await drawerState();
    ok(`[${cs.name}] 抽屉弹出且内容非空`, s.shown && s.display === "flex" && s.bodyLen > 20,
      `title="${s.title}" ${s.w}x${s.h} @(${s.left},${s.top}) 正文=${s.bodyLen}字`);
    ok(`[${cs.name}] 抽屉无 undefined/NaN/Invalid Date`, s.bad.length === 0, `脏=${JSON.stringify(s.bad)} ｜ ${s.text}`);
    // 遮挡判定：以所点行的矩形 vs 抽屉矩形
    const xOverlap = s.left < anchorRect.right && s.right > anchorRect.left;
    const fullOverlap = xOverlap && s.top < anchorRect.bottom && s.bottom > anchorRect.top;
    ok(`[${cs.name}] 抽屉横向不遮挡所点行`, !xOverlap,
      `所点行x[${Math.round(anchorRect.left)},${Math.round(anchorRect.right)}] 抽屉x[${s.left},${s.right}] 全矩形相交=${fullOverlap}`);

    // 关闭入口 1：✕
    await page.click("#btnClose"); await wait(260);
    ok(`[${cs.name}] 关闭入口①✕生效`, !(await drawerState()).shown);
    // 2：Esc
    await page.click(cs.sel); await wait(650);
    await page.keyboard.press("Escape"); await wait(260);
    ok(`[${cs.name}] 关闭入口②Esc生效`, !(await drawerState()).shown);
    // 3：点面板外
    await page.click(cs.sel); await wait(650);
    await page.mouse.click(30, 960); await wait(300);
    ok(`[${cs.name}] 关闭入口③点面板外生效`, !(await drawerState()).shown);
    // 4：底部按钮
    await page.click(cs.sel); await wait(650);
    await page.click("#btnClose2"); await wait(260);
    ok(`[${cs.name}] 关闭入口④底部按钮生效`, !(await drawerState()).shown);

    // 拖拽（往左上拖，给拉伸留空间）
    await page.click(cs.sel); await wait(700);
    const b0 = await drawerState();
    const head = await page.$eval("#drawerHead", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + 120, y: r.top + r.height / 2 }; });
    await page.mouse.move(head.x, head.y); await page.mouse.down();
    await page.mouse.move(head.x - 620, head.y - 40, { steps: 14 }); await page.mouse.up(); await wait(260);
    const b1 = await drawerState();
    ok(`[${cs.name}] 拖拽移动生效`, Math.abs(b1.left - b0.left) > 300, `(${b0.left},${b0.top})→(${b1.left},${b1.top})`);
    ok(`[${cs.name}] 拖拽后标题栏+拉伸手柄仍在视口内`, b1.headTopInView && b1.resizeHandleInView,
      `headTopInView=${b1.headTopInView} handleInView=${b1.resizeHandleInView}`);
    // 拉伸
    const rz = await page.$eval("#drawerResize", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await page.mouse.move(rz.x, rz.y); await page.mouse.down();
    await page.mouse.move(Math.min(rz.x + 140, 1436), Math.min(rz.y + 70, 996), { steps: 12 }); await page.mouse.up(); await wait(280);
    const b2 = await drawerState();
    ok(`[${cs.name}] 右下角拉伸生效且手柄未出视口`, b2.w > b1.w + 80 && b2.resizeHandleInView,
      `${b1.w}x${b1.h}→${b2.w}x${b2.h} handleInView=${b2.resizeHandleInView}`);
    // 拖到极端位置：把手必须仍在视口
    const head2 = await page.$eval("#drawerHead", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + 100, y: r.top + r.height / 2 }; });
    await page.mouse.move(head2.x, head2.y); await page.mouse.down();
    await page.mouse.move(2000, 1400, { steps: 10 }); await page.mouse.up(); await wait(220);
    const b3 = await drawerState();
    ok(`[${cs.name}] 拖到右下极限后手柄仍在视口`, b3.resizeHandleInView,
      `@(${b3.left},${b3.top}) ${b3.w}x${b3.h} handleInView=${b3.resizeHandleInView}`);
    // 说明：此断言若 FAIL 是真缺陷（面板宽>vw-160 时拖到最右，右下角拉伸手柄会被推出视口），
    // 不改断言。关闭改用 Esc（✕ 此时可能已在视口外，点击会抛错，本身就是同一缺陷的体现）。
    await page.keyboard.press("Escape"); await wait(300);
    // 兜底：若仍开着（说明 Esc 也失效），用 JS 收尾，避免影响后续用例
    const stillOpen = await page.evaluate(() => document.querySelector("#drawer").classList.contains("show"));
    if (stillOpen) { await page.evaluate(() => document.querySelector("#drawer").classList.remove("show")); await wait(200); }
    // 复位抽屉尺寸，避免上一轮的持久化尺寸影响后续用例
    await page.evaluate(() => localStorage.removeItem("viewer-drawer-size"));
  }
  await page.screenshot({ path: path.join(SHOT, "drawer-after-tests.png") });

  /* ---------- 3. 四主题 ---------- */
  out.push("");
  out.push("========== 三、四主题可读性 / 不消失不错位 ==========");
  const themes = ["light-tech", "dark-dashboard", "minimal-report", "card-warm"];
  await page.click('.tab[data-tab="stocks"]'); await wait(300);
  const snaps = {};
  for (const t of themes) {
    STEP = "theme " + t;
    await page.evaluate((id) => { document.querySelector("#theme-link").href = `static/themes/${id}.css`; }, t);
    await wait(520);
    const snap = await page.evaluate(() => {
      const body = getComputedStyle(document.body);
      const p = document.querySelector("#p-stocks");
      const name = document.querySelector("#p-stocks .stk-name");
      const box = document.querySelector("#p-stocks .stk-card");
      const badge = document.querySelector("#p-stocks .chip-s");
      const g = (el) => (el ? getComputedStyle(el) : null);
      return {
        bodyFg: body.color, bodyBg: body.backgroundColor,
        nameFg: g(name) ? g(name).color : null,
        boxBg: g(box) ? g(box).backgroundColor : null,
        badgeFg: g(badge) ? g(badge).color : null, badgeBg: g(badge) ? g(badge).backgroundColor : null,
        counts: {
          cards: p.querySelectorAll(".stk-card").length,
          boxes: p.querySelectorAll(".box").length,
          badges: p.querySelectorAll(".badge,.chip-s,.chip-demo").length,
          tblRows: document.querySelectorAll("#p-stocks table tbody tr").length,
        },
        panelH: Math.round(p.getBoundingClientRect().height),
        zeroSizeBlocks: [...p.querySelectorAll(".stk-card")].filter((e) => {
          const r = e.getBoundingClientRect(); return r.width < 10 || r.height < 10;
        }).length,
      };
    });
    snaps[t] = snap;
    const c1 = contrast(snap.nameFg, snap.boxBg);
    const c2 = contrast(snap.badgeFg, snap.badgeBg);
    const c3 = contrast(snap.bodyFg, snap.bodyBg);
    ok(`[${t}] 文字色≠背景色（正文/卡片名/徽章）`,
      snap.bodyFg !== snap.bodyBg && snap.nameFg !== snap.boxBg && snap.badgeFg !== snap.badgeBg,
      `正文 ${snap.bodyFg} on ${snap.bodyBg} (对比度 ${c3}) ｜ 卡名 ${snap.nameFg} on ${snap.boxBg} (${c1}) ｜ 徽章 ${snap.badgeFg} on ${snap.badgeBg} (${c2})`);
    ok(`[${t}] 关键文字对比度 ≥ 3.0`, (c1 === null || c1 >= 3) && (c3 === null || c3 >= 3),
      `卡名对比度=${c1} 正文对比度=${c3}`);
    ok(`[${t}] 元素数量与基线一致（未消失）`,
      snap.counts.cards === snaps[themes[0]].counts.cards && snap.counts.boxes === snaps[themes[0]].counts.boxes,
      `cards=${snap.counts.cards} boxes=${snap.counts.boxes} badges=${snap.counts.badges} 零尺寸块=${snap.zeroSizeBlocks}`);
    await page.screenshot({ path: path.join(SHOT, `theme-${t}-stocks.png`) });
  }
  const hs = themes.map((t) => snaps[t].panelH);
  ok("四主题下面板高度一致（未错位/塌陷）", Math.max(...hs) - Math.min(...hs) <= 8,
    themes.map((t, i) => `${t}=${hs[i]}`).join(" / "));
  await page.evaluate(() => { document.querySelector("#theme-link").href = "static/themes/light-tech.css"; });

  /* ---------- 6. 长文本压力（测试页内 DOM 注入，不改源码/数据） ---------- */
  out.push("");
  out.push("========== 四、长文本压力（DOM 注入模拟超长标题/昵称） ==========");
  STEP = "long text";
  const longRes = await page.evaluate(() => {
    const LONG_CJK = "这是一个极其冗长的帖子标题用于测试卡片在极端文本长度下是否会把布局撑破".repeat(4);
    const LONG_ASCII = "A".repeat(160);
    const p = document.querySelector("#p-stocks");
    const card = p.querySelector(".stk-card .stk-name");
    const corp = p.querySelector(".stk-card .stk-corp");
    const before = document.scrollingElement.scrollWidth;
    let afterCJK = null, afterASCII = null, cardOverflowCJK = null, cardOverflowASCII = null, corpRes = null;
    if (corp) {   // 真实可达字段（公司全称）先测：它有 word-break，应能扛住
      const origC = corp.textContent;
      corp.textContent = LONG_ASCII;
      const afterC = document.scrollingElement.scrollWidth;
      corpRes = { after: afterC, box: { scroll: corp.scrollWidth, client: corp.clientWidth } };
      corp.textContent = origC;
    }
    if (card) {
      const orig = card.textContent;
      card.textContent = LONG_CJK;
      afterCJK = document.scrollingElement.scrollWidth;
      cardOverflowCJK = { scroll: card.scrollWidth, client: card.clientWidth, cardScroll: card.closest(".stk-card").scrollWidth, cardClient: card.closest(".stk-card").clientWidth };
      card.textContent = LONG_ASCII;
      afterASCII = document.scrollingElement.scrollWidth;
      cardOverflowASCII = { scroll: card.scrollWidth, client: card.clientWidth };
      card.textContent = orig;
    }
    return { before, afterCJK, afterASCII, cardOverflowCJK, cardOverflowASCII, innerW: innerWidth, corp: corpRes };
  });
  ok("超长中文标题不产生页面横向溢出",
    longRes.afterCJK !== null && longRes.afterCJK <= longRes.innerW + 2,
    `scrollWidth ${longRes.before}→${longRes.afterCJK} (viewport ${longRes.innerW}) 卡内 scroll/client=${JSON.stringify(longRes.cardOverflowCJK)}`);
  ok("超长无空格 ASCII 串不产生页面横向溢出（.stk-name 未设 word-break，属极限输入）",
    longRes.afterASCII !== null && longRes.afterASCII <= longRes.innerW + 2,
    `scrollWidth→${longRes.afterASCII} 卡内=${JSON.stringify(longRes.cardOverflowASCII)}`);
  ok("公司全称列（.stk-corp 已设 word-break）对超长 ASCII 正常换行、不溢出",
    longRes.corp && longRes.corp.after <= longRes.innerW + 2,
    `scrollWidth→${longRes.corp ? longRes.corp.after : "n/a"} 该元素 scroll/client=${longRes.corp ? JSON.stringify(longRes.corp.box) : "n/a"}`);

  /* ---------- 5. 响应式 ---------- */
  out.push("");
  out.push("========== 五、响应式 / 窄视口 ==========");
  for (const vp of [{ w: 1024, h: 700 }, { w: 1440, h: 1000 }]) {
    STEP = `viewport ${vp.w}x${vp.h}`;
    await page.setViewport({ width: vp.w, height: vp.h });
    await wait(500);
    const r = await page.evaluate(() => {
      const doc = document.scrollingElement;
      const btns = ["#btnFetch", "#btnLogin", "#btnDemo", "#btnOverseas"].map((sel) => {
        const el = document.querySelector(sel);
        const b = el.getBoundingClientRect();
        const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
        return { sel, visible: b.width > 0 && b.height > 0 && b.top >= 0 && b.bottom <= innerHeight + 1,
          hitOk: !!(hit && (hit === el || el.contains(hit))), rect: [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)] };
      });
      const tabs = document.querySelector("#tabs").getBoundingClientRect();
      return { scrollW: doc.scrollWidth, innerW: innerWidth, overflow: doc.scrollWidth - innerWidth,
        btns, tabsBottom: Math.round(tabs.bottom), tabRows: Math.round(tabs.height) };
    });
    ok(`[${vp.w}x${vp.h}] 无横向溢出`, r.overflow <= 2, `scrollWidth=${r.scrollW} viewport=${r.innerW} 溢出=${r.overflow}px`);
    const allClickable = r.btns.every((b) => b.hitOk);
    ok(`[${vp.w}x${vp.h}] 顶栏 4 个按钮 elementFromPoint 可命中`, allClickable,
      r.btns.map((b) => `${b.sel}=${b.hitOk ? "ok" : "BLOCKED"}${b.rect}`).join(" "));
    await page.screenshot({ path: path.join(SHOT, `viewport-${vp.w}x${vp.h}.png`) });
  }
  await page.setViewport({ width: 1440, height: 1000 });

  out.push("");
  ok("主页面全程无 JS 运行时错误", errs.length === 0, errs.slice(0, 5).join(" ; ") || "无");

  /* ========================================================================
   * 第二部分：空态 / 错误态（请求拦截，只影响测试页）
   * ===================================================================== */
  out.push("");
  out.push("========== 六、空态 / 错误态（拦截构造，仅测试页生效） ==========");

  const mockPage = await browser.newPage();
  await mockPage.setViewport({ width: 1440, height: 1000 });

  // 6.1 全部数据接口返回 [] 或空对象 → 观察空态文案
  const errsEmpty = [];
  mockPage.on("pageerror", (e) => errsEmpty.push("pageerror: " + e.message));
  mockPage.on("console", (m) => { if (m.type() === "error") errsEmpty.push("console: " + m.text()); });
  await mockPage.setRequestInterception(true);
  mockPage.on("request", (req) => {
    const u = req.url();
    if (!/\/api\//.test(u)) return req.continue();
    const json = (o) => req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify(o) });
    if (/\/api\/summary/.test(u)) return json({ counts: { posts: 0, trades: 0, news: 0, raw_pages: 0, persons: 0, stock_mentions: 0, topics: 0, players: 0, positions: 0, overseas_news: 0, overseas_hotspots: 0 }, login_status: [], last_ingest: "尚未抓取", sites: ["taoguba"] });
    if (/\/api\/overseas_sentiment/.test(u)) return json({ distribution: {}, net_mood: 0, heat_total: 0, total: 0, region_dist: {}, daily: [], source_mix: [], news: [], symbols: [], region_weights: {}, topics: [] });
    // 修正（本轮）：空态下 /api/sentiment 的真实形态是「对象 + 空数组」，不是 JSON 数组。
    // 原先误喂 [] 会让 (d.map || []) 取到 Array.prototype.map，抛 "(d.map||[]).map is not a function"，
    // 那是「响应类型不符」的鲁棒性问题，不属于空态，故拆成独立用例（见下方 case 6）。此处给正确空形态。
    if (/\/api\/sentiment/.test(u)) return json({ distribution: {}, map: [], name_covered: 0, name_total: 0, legend: {} });
    if (/\/api\/overseas_sources/.test(u)) return json({ sources: [], region_weights: {}, topics: [] });
    return json([]);
  });
  STEP = "goto empty-mock";
  await mockPage.goto(URL, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);
  const emptyRes = {};
  for (const t of TABS) {
    await mockPage.click(`.tab[data-tab="${t}"]`); await wait(220);
    emptyRes[t] = await mockPage.evaluate((tab) => {
      const p = document.querySelector("#p-" + tab);
      const txt = (p.innerText || "").replace(/\s+/g, " ").trim();
      return { len: txt.length, sample: txt.slice(0, 70),
        empties: p.querySelectorAll(".empty").length,
        bad: ["undefined", "NaN", "Invalid Date"].filter((k) => txt.includes(k)) };
    }, t);
  }
  for (const t of TABS) {
    const r = emptyRes[t];
    const hasEmptyState = r.empties > 0 || /暂无|无相关|尚未|未检出|无记录|无数据/.test(r.sample);
    ok(`[空态][${t}] 显示可读空态文案且无 undefined/NaN`, r.len > 0 && hasEmptyState && r.bad.length === 0,
      `文字=${r.len}字 .empty=${r.empties} 脏=${JSON.stringify(r.bad)} ｜ ${r.sample}`);
  }
  ok("[空态] 无 JS 运行时错误", errsEmpty.length === 0, errsEmpty.slice(0, 4).join(" ; ") || "无");
  await mockPage.screenshot({ path: path.join(SHOT, "empty-state-summary.png"), fullPage: false });
  await mockPage.close();

  // 6.2 接口失败：非 JSON / 500 → 前端如何呈现？
  const failCases = [
    { name: "全部 /api 直接失败(网络层 abort)", handler: (req) => req.abort() },
    { name: "/api 返回 500 且非 JSON(HTML 错误页)", handler: (req) => req.respond({ status: 500, contentType: "text/html", body: "<html><body>Internal Server Error</body></html>" }) },
    { name: "/api 返回 500 但 body 是合法 JSON {error}", handler: (req) => req.respond({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "boom" }) }) },
    { name: "/api/summary 缺 counts 字段(其余正常)", handler: (req) => {
        if (/\/api\/summary/.test(req.url())) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ login_status: [], last_ingest: "x", sites: [] }) });
        return req.continue();
      } },
    { name: "/api/topics 条目缺 weight 字段", handler: (req) => {
        if (/\/api\/topics/.test(req.url())) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify([{ topic: "缺权重话题A" }, { topic: "缺权重话题B" }]) });
        return req.continue();
      } },
    // 本轮新增（缺失字段兜底体检）：只看某个接口少给字段时，界面会不会把 undefined 直接渲染出来
    { name: "缺字段:/api/persons 条目缺 mention_count/role", handler: (req) => {
        if (/\/api\/persons/.test(req.url())) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify([{ name: "缺字段用户A" }, { name: "缺字段用户B" }]) });
        return req.continue();
      } },
    { name: "缺字段:/api/summary.counts 少给 topics/persons/positions", handler: (req) => {
        if (/\/api\/summary/.test(req.url())) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ counts: { posts: 1, trades: 2, news: 3 }, login_status: [], last_ingest: "x", sites: ["taoguba"] }) });
        return req.continue();
      } },
    { name: "类型不符:/api/sentiment 返回 JSON 数组", handler: (req) => {
        if (/\/api\/sentiment/.test(req.url())) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify([{ symbol: "600519" }]) });
        return req.continue();
      } },
  ];
  for (const fc of failCases) {
    STEP = "fail-case " + fc.name;
    const fp = await browser.newPage();
    await fp.setViewport({ width: 1440, height: 1000 });
    const ferrs = [];
    fp.on("pageerror", (e) => ferrs.push("pageerror: " + e.message));
    fp.on("console", (m) => { if (m.type() === "error") ferrs.push("console: " + m.text()); });
    await fp.setRequestInterception(true);
    fp.on("request", (req) => { if (/\/api\//.test(req.url())) return fc.handler(req); return req.continue(); });
    let gotoErr = null;
    try { await fp.goto(URL, { waitUntil: "networkidle2", timeout: 30000 }); } catch (e) { gotoErr = e.message; }
    await wait(1500);
    const st = await fp.evaluate(() => ({
      hint: (document.querySelector("#statusHint") || {}).textContent || "",
      cardsHtml: (document.querySelector("#cards") || {}).innerText || "",
      summaryText: (document.querySelector("#p-summary") || {}).innerText || "",
      topicsText: (document.querySelector("#p-topics") || {}).innerText || "",
      personsText: (document.querySelector("#p-persons") || {}).innerText || "",
      sentimentText: (document.querySelector("#p-sentiment") || {}).innerText || "",
      // 修正（本轮）：innerText 会排除 display:none 的面板，导致隐藏面板里的 undefined 检不出来。
      // 改用 textContent（含隐藏元素）测「有没有把 undefined 写入 DOM」，避免假绿。
      cardsTextRaw: (document.querySelector("#cards") || {}).textContent || "",
      personsTextRaw: (document.querySelector("#p-persons") || {}).textContent || "",
      sentimentTextRaw: (document.querySelector("#p-sentiment") || {}).textContent || "",
      topicsTextRaw: (document.querySelector("#p-topics") || {}).textContent || "",
      bodyText: document.body.innerText || "",
      anyErrorBanner: !!document.querySelector(".error,.err-banner,.toast,#errorBox"),
      bodyLen: document.body.innerText.replace(/\s+/g, " ").trim().length,
    }));
    const userSeesError = /失败|错误|出错|异常|无法|请稍后|重试/.test(st.hint + " " + st.bodyLen);
    info(`[错误态] ${fc.name}`,
      `gotoErr=${gotoErr || "无"} 页面可见报错提示=${userSeesError} statusHint="${st.hint.slice(0, 40)}" 卡片区文字="${st.cardsHtml.slice(0, 40)}" 汇总="…${st.summaryText.replace(/\s+/g, " ").slice(-50)}" 顶层错误条=${st.anyErrorBanner} JS错误=${ferrs.length}`);
    info(`    └ JS 错误详情: ${ferrs.slice(0, 3).join(" ; ") || "无"}`);
    if (/缺 weight/.test(fc.name)) {
      ok("[错误态] 缺 weight 字段时前端未渲染出 NaN", !/NaN/.test(st.topicsTextRaw), `topics 面板 textContent="${st.topicsTextRaw.replace(/\s+/g, " ").slice(0, 80)}"`);
    }
    if (/缺 counts/.test(fc.name)) {
      ok("[错误态] /api/summary 缺 counts 时前端给出可读提示（而非静默白屏）", userSeesError, `hint="${st.hint.slice(0, 40)}" JS错误=${ferrs.length}`);
    }
    if (/^缺字段/.test(fc.name)) {
      // 修正（本轮）：用例名里本身含 "persons" 字样，不能用 /persons/ 判容器，
      // 否则「summary.counts 少给 topics/persons/positions」会误用人物面板去测，造成假绿。
      const src = /\/api\/persons/.test(fc.name) ? st.personsTextRaw : st.cardsTextRaw;
      const undef = (src.match(/undefined/g) || []).length;
      ok(`[错误态] ${fc.name} —— 前端未把 undefined 直接渲染到产物 DOM 里`, undef === 0,
        `相关容器 textContent 内 undefined ${undef} 处 ｜ "${src.replace(/\s+/g, " ").slice(0, 130)}"`);
    }
    if (/类型不符/.test(fc.name)) {
      const pe = ferrs.filter((x) => /pageerror|Uncaught/.test(x));
      ok(`[错误态] ${fc.name} —— 前端不因载荷类型不符而抛错白屏`, pe.length === 0,
        `pageerror ${pe.length} 条 ｜ 舆论情绪面板 textContent="${st.sentimentTextRaw.replace(/\s+/g, " ").slice(0, 60) || "<空>"}" ｜ ${pe.slice(0, 1).join("")}`);
    }
    await fp.screenshot({ path: path.join(SHOT, "fail-" + fc.name.replace(/[\/\\:*?"<>|()]/g, "_").slice(0, 40) + ".png") });
    await fp.close();
  }

  /* ---------- 汇总 ---------- */
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  const passes = out.filter((l) => l.startsWith("PASS")).length;
  const skips = out.filter((l) => l.startsWith("SKIP")).length;
  out.push("");
  out.push(`=== 断言 ${passes}/${passes + fails} 通过（FAIL ${fails} / SKIP ${skips}） ===`);
  fs.writeFileSync(REPORT, out.join("\n"), "utf8");
  console.log(out.join("\n"));
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  out.push("RUNNER ERROR @ " + STEP + ": " + (e && e.stack || e));
  try { fs.writeFileSync(REPORT, out.join("\n"), "utf8"); } catch (x) { /* ignore */ }
  console.log(out.join("\n"));
  process.exit(2);
});
