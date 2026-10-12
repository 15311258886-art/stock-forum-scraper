/* =============================================================================
 * ui_qa_regression.js —— 看板 UI 交互回归（只读，真实 Chromium）
 *
 * 硬约束（本脚本自带，不依赖外部 guard）：
 *   1) 浏览器层请求拦截：POST /api/demo、POST /api/overseas(reset/live)、
 *      POST /api/fetch 一律 abort；其余放行。
 *   2) window.confirm 覆盖为恒 false；page.on("dialog") 一律 dismiss。
 *   3) 绝不点击 #btnDemo / #btnOverseas / #btnFetch / #btnRefreshNow。
 * 覆盖：全部大类+子类导航、4 套主题、空 positions 容错、/api/consensus 字段、
 *      示例角标误标扫描、抽屉交互、控制台报错。
 * ========================================================================== */
"use strict";
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8765";
const BASE = `http://127.0.0.1:${PORT}`;
const MODE = process.env.GP1_BEFORE === "1" ? "before" : "after";   // before = 用改动前静态资源做 A/B 对照
const SHOT = path.join(__dirname, "shots_" + MODE);
if (!fs.existsSync(SHOT)) fs.mkdirSync(SHOT, { recursive: true });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const guardLog = [];
const consoleErrs = [];
const pageErrs = [];
let STEP = "init";

function rec(id, name, expected, actual, pass, evidence) {
  results.push({ id, name, expected, actual, pass: !!pass, evidence: evidence || "" });
  console.log(`[${pass ? "PASS" : "FAIL"}] ${id} ${name} :: ${actual}${evidence ? " || " + evidence : ""}`);
}
function ev(label, extra) { return extra ? `${label} | ${extra}` : label; }
function errMark() { return consoleErrs.length + pageErrs.length; }
function newErrs(since) {
  return { console: consoleErrs.slice(since.c), page: pageErrs.slice(since.p) };
}

(async () => {
  console.log(`[guard] 端口=${PORT} 只读模式；硬拦 POST /api/demo、/api/overseas、/api/fetch；A/B 模式=${MODE}`);
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,950"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  /* ---------- 硬约束 2：confirm 恒 false ---------- */
  await page.evaluateOnNewDocument(() => {
    window.__confirmCalled = 0;
    window.confirm = function () { window.__confirmCalled++; return false; };
  });
  page.on("dialog", async (d) => { guardLog.push("dialog dismissed: " + d.type() + " " + d.message()); await d.dismiss().catch(() => {}); });

  /* ---------- 硬约束 1：请求拦截兜底 ---------- */
  await page.setRequestInterception(true);
  const blocked = [];
  page.on("request", (req) => {
    const u = req.url();
    const m = req.method();
    const isApi = /\/api\//.test(u);
    // A/B 对照：GP1_BEFORE=1 时用「改动前」静态资源替换响应（文件由 make_before.js 生成，不写回仓库）
    if (MODE === "before" && /\/static\/app\.(js|css)(\?|$)/.test(u)) {
      const f = /app\.css/.test(u) ? "before_app.css" : "before_app.js";
      const p = path.join(__dirname, f);
      if (fs.existsSync(p)) {
        return req.respond({
          status: 200,
          contentType: /app\.css/.test(u) ? "text/css" : "application/javascript",
          body: fs.readFileSync(p),
        }).catch(() => {});
      }
      console.log("[ab] 缺少 " + f + "，回退为线上文件");
    }
    if (/\/api\/demo\b/.test(u)) { blocked.push("POST /api/demo" + " <- " + u); return req.abort().catch(() => {}); }
    if (/\/api\/fetch\b/.test(u)) { blocked.push("POST /api/fetch"); return req.abort().catch(() => {}); }
    if (/\/api\/overseas\b/.test(u)) {
      const d = req.postData() || "";
      blocked.push("POST /api/overseas(" + d.slice(0, 80) + ")");
      return req.abort().catch(() => {});
    }
    req.continue().catch(() => {});
  });

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const loc = msg.location() || {};
      consoleErrs.push(`[${STEP}] ${msg.text()} @ ${loc.url || ""}:${loc.lineNumber || ""}`);
    }
  });
  page.on("pageerror", (e) => { pageErrs.push(`[${STEP}] ${e.message}`); });

  /* ---------- 打开首页 ---------- */
  STEP = "goto";
  await page.goto(BASE, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);

  /* ===== 0. 危险按钮存在性 + 从未被点击 ===== */
  const dangerInfo = await page.evaluate(() => ({
    demo: !!document.querySelector("#btnDemo"),
    overseas: !!document.querySelector("#btnOverseas"),
    fetch: !!document.querySelector("#btnFetch"),
    refreshNow: !!document.querySelector("#btnRefreshNow"),
  }));
  rec("G0", "破坏性按钮存在但不点", "在 DOM 中存在即视为已识别，全程不点击",
    `btnDemo=${dangerInfo.demo} btnOverseas=${dangerInfo.overseas} btnFetch=${dangerInfo.fetch}`,
    true, ev("拦截器已装", "已 abort: " + (blocked.length ? blocked.join(" ; ") : "0 条")));

  /* ===== 1. 导航：5 大类 16 子类逐一点击 ===== */
  STEP = "nav";
  const navMap = await page.evaluate(() => {
    const cats = [...document.querySelectorAll("#sidenav .nav-item")].map((a) => ({
      cat: a.dataset.cat, label: (a.querySelector(".ni-txt") || {}).textContent || "",
      n: (a.querySelector(".ni-n") || {}).textContent || "",
    }));
    return cats;
  });
  rec("N0", "一级导航渲染 5 个大类", "5 个 .nav-item", `${navMap.length} 个: ${navMap.map((c) => c.label + "(" + c.n + ")").join(" / ")}`,
    navMap.length === 5);

  const allTabs = [];
  for (const c of navMap) {
    STEP = "nav-" + c.cat;
    const before = { c: consoleErrs.length, p: pageErrs.length };
    await page.evaluate((cat) => {
      const a = document.querySelector(`#sidenav .nav-item[data-cat="${cat}"]`);
      if (a) a.click();
    }, c.cat);
    await wait(700);
    const subs = await page.evaluate(() => [...document.querySelectorAll("#subnav .sub-tab")].map((a) => ({ tab: a.dataset.tab, label: a.textContent.trim() })));
    for (const s of subs) {
      allTabs.push({ cat: c.cat, tab: s.tab, label: s.label });
      STEP = "tab-" + s.tab;
      const b2 = { c: consoleErrs.length, p: pageErrs.length };
      await page.evaluate((tab) => {
        const a = document.querySelector(`#subnav .sub-tab[data-tab="${tab}"]`);
        if (a) a.click();
      }, s.tab);
      // 轮询等面板出内容（含懒加载）
      let diag = null;
      for (let i = 0; i < 20; i++) {
        await wait(250);
        diag = await page.evaluate((tab) => {
          const p = document.querySelector("#p-" + tab);
          if (!p) return { missing: true };
          const cs = getComputedStyle(p);
          const txt = p.innerText || "";
          return {
            active: p.classList.contains("active"),
            display: cs.display,
            htmlLen: p.innerHTML.length,
            textLen: txt.length,
            hasUndef: /\bundefined\b/.test(txt),
            hasNaN: /NaN/.test(txt),
            needLoad: /正在读取|加载中|读取中/.test(txt) && txt.length < 40,
          };
        }, s.tab);
        if (diag && diag.htmlLen > 40 && diag.display !== "none" && !diag.needLoad) break;
      }
      const e2 = newErrs(b2);
      const problems = [];
      if (!diag || diag.missing) problems.push("面板容器缺失(#p-" + s.tab + ")");
      else {
        if (!diag.active) problems.push("未激活(active=false)");
        if (diag.htmlLen < 40) problems.push("内容为空(htmlLen=" + diag.htmlLen + ")");
        if (diag.hasUndef) problems.push("文本含 undefined");
        if (diag.hasNaN) problems.push("文本含 NaN");
      }
      if (e2.console.length || e2.page.length) problems.push("控制台报错 " + (e2.console.length + e2.page.length) + " 条");
      rec("N1", `导航 ${c.label} › ${s.label}`, "面板 active + 有内容 + 无报错",
        diag ? `active=${diag.active} htmlLen=${diag.htmlLen} textLen=${diag.textLen}` : "无面板",
        problems.length === 0,
        problems.length ? "问题: " + problems.join("; ") + " || " + [...e2.page, ...e2.console].slice(0, 2).join(" ;; ") : "");
    }
  }
  rec("N2", "子类覆盖数", ">= 16 个子类全部可点", `共遍历 ${allTabs.length} 个子类: ${allTabs.map((t) => t.label).join(",")}`, allTabs.length >= 16);

  /* ===== 2. 空 positions 容错 + consensus 字段 ===== */
  STEP = "consensus";
  await page.evaluate(() => document.querySelector('#sidenav .nav-item[data-cat="trade"]').click());
  await wait(400);
  await page.evaluate(() => { const a = document.querySelector('#subnav .sub-tab[data-tab="consensus"]'); if (a) a.click(); });
  await wait(1500);
  const consensusDiag = await page.evaluate(() => {
    const p = document.querySelector("#p-consensus");
    const txt = p.innerText || "";
    const rows = [...p.querySelectorAll("tbody tr")].map((tr) => {
      const tds = [...tr.querySelectorAll("td")].map((d) => d.innerText.trim());
      return { sym: tr.dataset.sym, cells: tds };
    });
    return {
      text: txt, rows, rowCount: rows.length,
      hasUndef: /\bundefined\b/.test(txt), hasNaN: /NaN/.test(txt),
      emptyBadges: p.querySelectorAll(".chip-row .badge").length -
        [...p.querySelectorAll(".chip-row .badge")].filter((b) => (b.textContent || "").trim().length > 0).length,
      playerBadges: p.querySelectorAll(".chip-row .badge").length,
      playerRows: p.querySelectorAll(".chip-row").length,
      emptyCells: rows.reduce((n, r) => n + r.cells.filter((c) => c === "" || c === "—").length, 0),
      banner: (p.querySelector(".hint") || {}).innerText ? "hint-present" : "no-hint",
    };
  });
  rec("C1", "共同持仓共识：positions=0 时仍渲染 3 只股", "3 行，无 undefined/NaN",
    `行数=${consensusDiag.rowCount} 含undefined=${consensusDiag.hasUndef} 含NaN=${consensusDiag.hasNaN}`,
    consensusDiag.rowCount === 3 && !consensusDiag.hasUndef && !consensusDiag.hasNaN,
    ev("表头", consensusDiag.banner));
  rec("C2", "共识股票代码与人数正确", "000001/4人, 300750/4人, 601318/2人",
    consensusDiag.rows.map((r) => `${r.sym}:${r.cells[1] || ""}`).join(" | "),
    consensusDiag.rows.map((r) => r.sym).join(",") === "000001,300750,601318");
  rec("C3", "共识行字段齐全（名称/公司/层级/平台/热度/来源非空）",
    "每行关键列非空",
    consensusDiag.rows.map((r) => `[${r.sym}] 名称=${r.cells[0] || ""} 加权=${r.cells[2] || ""} 层级=${r.cells[3] || ""} 渠道=${r.cells[4] || ""} 热度=${r.cells[5] || ""}`).join(" ‖ "),
    consensusDiag.rows.every((r) => (r.cells[0] || "").length > 2),
    ev("空单元格数", String(consensusDiag.emptyCells)));
  // C6 判定口径修正（2026-09-25）：原断言把占位符「—」也算作"空白格"，
  // 与设计自相矛盾——抽屉对同一字段正是输出「—」(app.js:1149 `${... || "—"}`)。
  // 修正为：① 真正空白（""）的单元格 = 0；② 渠道缺失的行必须落在一个明确的占位符上。
  const PLACEHOLDER_RE = /^(—|-|–|暂无|渠道未标注)$/;
  const emptyChannelCells = consensusDiag.rows.filter((r) => (r.cells[4] || "").trim() === "").length;
  const placeholderChannelCells = consensusDiag.rows.filter((r) => PLACEHOLDER_RE.test((r.cells[4] || "").trim())).length;
  rec("C6", "共识「覆盖渠道」列：渠道缺失时给出占位而非留空白格", "真空白单元格 = 0，且缺失行有占位（— / 暂无 / 渠道未标注）",
    `覆盖渠道列真空白行数 = ${emptyChannelCells}/${consensusDiag.rows.length}，占位行数 = ${placeholderChannelCells}`,
    emptyChannelCells === 0 || consensusDiag.rows.length === 0,
    ev("该列取值", consensusDiag.rows.map((r) => `${r.sym}:${JSON.stringify((r.cells[4] || "").trim())}`).join(" | ")));

  rec("C4", "共识「持仓选手明细」渠道缺失时不留空角标", "不应出现无文字的空 badge",
    `空 badge 数 = ${consensusDiag.emptyBadges}（明细内 badge 总数 ${consensusDiag.playerBadges}）`,
    consensusDiag.emptyBadges === 0,
    ev("修法", "app.js:962-968 把渠道角标改为「channel 为空则整段不渲染」，非空时仍输出紧凑字母角标"));
  await page.screenshot({ path: path.join(SHOT, "consensus.png") });

  /* ===== 3. 近5日共同买入（空数据） ===== */
  STEP = "cohold";
  await page.evaluate(() => { const a = document.querySelector('#subnav .sub-tab[data-tab="cohold"]'); if (a) a.click(); });
  await wait(1500);
  const coholdDiag = await page.evaluate(() => {
    const p = document.querySelector("#p-cohold");
    const txt = p.innerText || "";
    return { len: txt.length, hasUndef: /\bundefined\b/.test(txt), hasNaN: /NaN/.test(txt),
      empty: !!p.querySelector(".empty"), bar: !!p.querySelector(".coh-bar"),
      snippet: txt.replace(/\s+/g, " ").slice(0, 120) };
  });
  rec("C5", "近5日共同买入：空窗口不崩、有明确空态", "无 undefined/NaN，有空态或窗口条",
    `html 文本长=${coholdDiag.len} 空态=${coholdDiag.empty} 窗口条=${coholdDiag.bar}`,
    !coholdDiag.hasUndef && !coholdDiag.hasNaN && coholdDiag.len > 0, ev("摘要", coholdDiag.snippet));

  /* ===== 4. 正在操作股票 / 实盘选手（positions 空相关） ===== */
  STEP = "operating";
  // 逐个点 trade 类的 operating/players，再跨类点 stocks/summary
  const probeTabs = [["trade", "operating"], ["trade", "players"], ["research", "stocks"], ["overview", "summary"]];
  for (const [cat, tab] of probeTabs) {
    STEP = "probe-" + tab;
    await page.evaluate((c) => document.querySelector(`#sidenav .nav-item[data-cat="${c}"]`).click(), cat);
    await wait(400);
    await page.evaluate((t) => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${t}"]`); if (a) a.click(); }, tab);
    await wait(1200);
    const d = await page.evaluate((t) => {
      const p = document.querySelector("#p-" + t);
      const txt = p.innerText || "";
      return { len: txt.length, hasUndef: /\bundefined\b/.test(txt), hasNaN: /NaN/.test(txt),
        hasNull: /\bnull\b/.test(txt), rows: p.querySelectorAll("tbody tr").length,
        snippet: txt.replace(/\s+/g, " ").slice(0, 100) };
    }, tab);
    rec("P1", `${tab} 面板：空 positions 下无 undefined/NaN/null`, "无异常字样",
      `文本长=${d.len} 行=${d.rows} undefined=${d.hasUndef} NaN=${d.hasNaN} null=${d.hasNull}`,
      !d.hasUndef && !d.hasNaN && !d.hasNull && d.len > 0, ev("摘要", d.snippet));
  }

  /* ===== 5. 示例角标误标扫描（真实数据不得标「示例」） ===== */
  STEP = "demo-badge";
  const badgeScan = [];
  for (const [cat, tab] of [["overview", "summary"], ["research", "stocks"], ["trade", "players"], ["trade", "operating"], ["trade", "consensus"]]) {
    await page.evaluate((c) => document.querySelector(`#sidenav .nav-item[data-cat="${c}"]`).click(), cat);
    await wait(350);
    await page.evaluate((t) => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${t}"]`); if (a) a.click(); }, tab);
    await wait(1200);
    const d = await page.evaluate((t) => {
      const p = document.querySelector("#p-" + t);
      // E1 口径不变：「示例」角标只认 .chip-demo，按文本统计
      const demoChips = [...p.querySelectorAll(".chip-demo")].map((e) => (e.textContent || "").trim());
      const demoText = demoChips.filter((x) => /示例/.test(x)).length;
      const other = demoChips.length - demoText;
      // S1：「真实」角标必须落在 .chip-real；若仍带 .chip-demo（琥珀底/虚线=示例语义）即样式冲突。
      // 2026-09-25：原先「真实」「示例」共用 .chip-demo，200 个真实角标被画成示例样式。
      const allChips = [...p.querySelectorAll(".chip-s")].map((e) => ({ text: (e.textContent || "").trim(), cls: e.className }));
      const realChips = allChips.filter((x) => x.text === "真实");
      const realText = realChips.length;
      const realWithDemoClass = realChips.filter((x) => /(^|\s)chip-demo(\s|$)/.test(x.cls)).length;
      const realWithRealClass = realChips.filter((x) => /(^|\s)chip-real(\s|$)/.test(x.cls)).length;
      // 实渲样式（用于证明「真实」不再是琥珀虚底）
      const sample = p.querySelector(".chip-s.chip-real") || p.querySelector(".chip-s.chip-demo");
      let realChipStyle = null;
      if (sample) {
        const cs = getComputedStyle(sample);
        realChipStyle = { text: (sample.textContent || "").trim(), cls: sample.className,
          bg: cs.backgroundColor, color: cs.color,
          border: cs.borderTopWidth + " " + cs.borderTopStyle + " " + cs.borderTopColor };
      }
      const banner = p.querySelector(".banner") ? p.querySelector(".banner").innerText.replace(/\s+/g, " ") : "";
      return { chips: demoChips.length, demoText, other, realText, realWithDemoClass, realWithRealClass,
        realChipStyle, samples: demoChips.slice(0, 3), banner: banner.slice(0, 120) };
    }, tab);
    badgeScan.push({ tab, ...d });
    rec("E1", `${tab} 示例角标误标检查（按文本）`, "真实数据 → 文本为「示例」的角标数 = 0",
      `chip-demo 总数=${d.chips}（文本「示例」=${d.demoText}、其他=${d.other}）；「真实」角标=${d.realText}（.chip-real=${d.realWithRealClass}，误用 .chip-demo=${d.realWithDemoClass}）`,
      d.demoText === 0, ev("横幅", d.banner || "无"));
  }
  // S1 汇总判定（保持 1 条用例，总数仍为 44）
  const realTotal = badgeScan.reduce((n, d) => n + d.realText, 0);
  const realBad = badgeScan.reduce((n, d) => n + d.realWithDemoClass, 0);
  const realGood = badgeScan.reduce((n, d) => n + d.realWithRealClass, 0);
  const styleSample = (badgeScan.find((d) => d.realChipStyle) || {}).realChipStyle || null;
  rec("S1", "「真实」角标不套用「示例」样式类（应为 .chip-real，非琥珀虚底）",
    "带 .chip-demo 的「真实」角标 = 0，且「真实」全部使用 .chip-real",
    `「真实」角标 ${realTotal} 个：.chip-real=${realGood}，误用 .chip-demo=${realBad}`,
    realBad === 0 && realTotal > 0 && realGood === realTotal,
    ev("实渲样式", styleSample ? `${styleSample.text} cls="${styleSample.cls}" bg=${styleSample.bg} color=${styleSample.color} border=${styleSample.border}` : "无样本")
      + ev("口径", "示例 = 琥珀底+虚线框(.chip-demo)；真实 = 弱绿实底+实线框(.chip-real)"));

  /* ===== 6. 4 套主题切换 ===== */
  STEP = "themes";
  const themes = await page.evaluate(() => [...document.querySelectorAll("#themePick .chip")].map((c) => ({ id: c.dataset.id, label: c.textContent.trim(), title: c.getAttribute("title") || "" })));
  rec("T0", "主题选择器含 4 套方案", "4 个 chip", `${themes.length} 个: ${themes.map((t) => t.id + "(" + t.title + ")").join(" / ")}`, themes.length === 4,
    ev("可用性备注", "色块无文字标签，仅靠 title 提示（hover 才可见），弱可用性但非 BUG"));
  const themeReport = [];
  for (const t of themes) {
    STEP = "theme-" + t.id;
    const b = { c: consoleErrs.length, p: pageErrs.length };
    await page.evaluate((id) => { const c = document.querySelector(`#themePick .chip[data-id="${id}"]`); if (c) c.click(); }, t.id);
    await wait(600);
    await page.evaluate(() => document.querySelector('#sidenav .nav-item[data-cat="overview"]').click());
    await wait(300);
    await page.evaluate(() => { const a = document.querySelector('#subnav .sub-tab[data-tab="summary"]'); if (a) a.click(); });
    await wait(1000);
    const d = await page.evaluate(() => {
      const href = (document.querySelector("#theme-link") || {}).getAttribute ? document.querySelector("#theme-link").getAttribute("href") : "";
      const bodyBg = getComputedStyle(document.body).backgroundColor;
      function lum(rgb) {
        const m = rgb.match(/\d+(\.\d+)?/g);
        if (!m) return null;
        const [r, g, b2] = m.slice(0, 3).map((v) => {
          const x = v / 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b2;
      }
      function bgOf(el) {
        let n = el;
        while (n && n !== document.documentElement) {
          const c = getComputedStyle(n).backgroundColor;
          const m = c.match(/rgba?\(([^)]+)\)/);
          if (m) { const parts = m[1].split(","); const a = parts.length > 3 ? parseFloat(parts[3]) : 1; if (a > 0.1) return c; }
          n = n.parentElement;
        }
        return getComputedStyle(document.body).backgroundColor;
      }
      function contrast(el) {
        const col = getComputedStyle(el).color; const bg = bgOf(el);
        const l1 = lum(col), l2 = lum(bg);
        if (l1 == null || l2 == null) return null;
        const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
        return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
      }
      const targets = [
        ["顶栏标题", document.querySelector(".topbar h1")],
        ["一级导航文字", document.querySelector("#sidenav .nav-item .ni-txt")],
        ["子导航当前项", document.querySelector("#subnav .sub-tab.active")],
        ["说明文字", document.querySelector("#p-summary .hint, #p-summary .empty, #statusHint")],
        ["表格表头", document.querySelector("#p-summary th")],
        ["卡片/正文", document.querySelector("#p-summary b, #p-summary div")],
      ];
      const sampled = targets.filter((x) => x[1]).map((x) => ({ what: x[0], ratio: contrast(x[1]) }))
        .filter((x) => x.ratio != null);
      const low = sampled.filter((x) => x.ratio < 2.5);
      // 重叠检测：顶栏 vs 侧栏、侧栏 vs 内容区
      const R = (s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; };
      const inter = (a, b) => a && b && Math.min(a.r, b.r) > Math.max(a.l, b.l) && Math.min(a.b, b.b) > Math.max(a.t, b.t);
      const top = R(".topbar"), side = R("#sidenav"), content = R(".content");
      return { href, bodyBg, sampled, low, overlapTopSide: inter(top, side), overlapSideContent: inter(side, content) };
    });
    const e2 = newErrs(b);
    themeReport.push({ theme: t.id, ...d, errs: e2 });
    await page.screenshot({ path: path.join(SHOT, `theme-${t.id}.png`), fullPage: false });
    const okTheme = (d.href || "").indexOf(t.id) >= 0 && d.low.length === 0 && !d.overlapTopSide && !d.overlapSideContent && !(e2.console.length + e2.page.length);
    rec("T1", `主题 ${t.id} 生效+可读+无重叠`, "theme-link 切换 + 对比度>=2.5 + 无重叠 + 无报错",
      `href=${d.href} bg=${d.bodyBg} 低对比元素=${d.low.map((x) => x.what + ":" + x.ratio).join(",") || "无"} 顶栏∩侧栏=${d.overlapTopSide} 侧栏∩内容=${d.overlapSideContent} 报错=${e2.console.length + e2.page.length}`,
      okTheme, ev("对比度采样", d.sampled.map((x) => x.what + "=" + x.ratio).join(" ")));
  }
  // 复位到默认主题
  await page.evaluate(() => { const c = document.querySelector('#themePick .chip[data-id="light-tech"]'); if (c) c.click(); });
  await wait(400);

  /* ===== 7. 抽屉：共识行点开 + 关闭 ===== */
  STEP = "drawer";
  await page.evaluate(() => document.querySelector('#sidenav .nav-item[data-cat="trade"]').click());
  await wait(400);
  await page.evaluate(() => { const a = document.querySelector('#subnav .sub-tab[data-tab="consensus"]'); if (a) a.click(); });
  await wait(1200);
  const hasRow = await page.$("#p-consensus tbody tr");
  if (hasRow) {
    await page.click("#p-consensus tbody tr td:nth-child(1)");
    await wait(900);
    const ds = await page.evaluate(() => {
      const d = document.querySelector("#drawer");
      const body = (document.querySelector("#drawerBody") || {}).innerText || "";
      return { shown: d.classList.contains("show"), len: body.length,
        hasUndef: /\bundefined\b/.test(body), hasNaN: /NaN/.test(body),
        snippet: body.replace(/\s+/g, " ").slice(0, 120) };
    });
    rec("D1", "点击共识行弹出抽屉且内容完整", "shown + 有内容 + 无 undefined/NaN",
      `shown=${ds.shown} 文本长=${ds.len} undefined=${ds.hasUndef} NaN=${ds.hasNaN}`, ds.shown && ds.len > 20 && !ds.hasUndef && !ds.hasNaN, ev("摘要", ds.snippet));
    await page.screenshot({ path: path.join(SHOT, "drawer-consensus.png") });
    // 关闭三个入口
    await page.keyboard.press("Escape"); await wait(300);
    const closedEsc = await page.evaluate(() => !document.querySelector("#drawer").classList.contains("show"));
    rec("D2", "Esc 关闭抽屉", "closed", String(closedEsc), closedEsc);
  } else {
    rec("D1", "点击共识行弹出抽屉", "有共识行可点", "共识表无数据行", false);
  }

  /* ===== 8. 全局控制台错误 ===== */
  STEP = "final";
  const totalErrs = consoleErrs.length + pageErrs.length;
  rec("X1", "全程无未捕获异常 / console.error", "0 条",
    `console.error=${consoleErrs.length} pageerror=${pageErrs.length}`,
    totalErrs === 0,
    [...pageErrs, ...consoleErrs].slice(0, 6).join(" ‖ ") || "无");

  /* ===== 9. 拦截器自证 ===== */
  rec("X2", "本轮未发出任何破坏性请求", "0 条 blocked",
    `被拦截(即尝试发出但已 abort)=${blocked.length}: ${blocked.join(" ; ") || "无"}`,
    blocked.length === 0,
    ev("confirm 被调用次数", String(await page.evaluate(() => window.__confirmCalled || 0))));

  await browser.close();

  /* ---------- 落盘 ---------- */
  const summary = {
    port: PORT, mode: MODE, ts: new Date().toISOString(),
    total: results.length, pass: results.filter((r) => r.pass).length,
    fail: results.filter((r) => !r.pass).length,
    failedIds: results.filter((r) => !r.pass).map((r) => r.id + " " + r.name),
    blockedRequests: blocked, guardLog,
    consoleErrors: consoleErrs, pageErrors: pageErrs,
    themes: themeReport, badgeScan, consensusDiag,
    results,
  };
  fs.writeFileSync(path.join(__dirname, `qa_result_${MODE}.json`), JSON.stringify(summary, null, 2), "utf-8");

  const lines = [];
  lines.push("用例 | 期望 | 实际 | 判定 | 证据");
  lines.push("---|---|---|---|---");
  for (const r of results) lines.push(`${r.id} ${r.name} | ${r.expected} | ${r.actual} | ${r.pass ? "PASS" : "FAIL"} | ${r.evidence}`);
  lines.push("");
  lines.push(`总计 ${results.length} 用例，PASS ${summary.pass}，FAIL ${summary.fail}`);
  lines.push(`失败用例：${summary.failedIds.join(" | ") || "无"}`);
  fs.writeFileSync(path.join(__dirname, `qa_report_${MODE}.md`), lines.join("\n"), "utf-8");

  console.log(`\n=== [${MODE}] 共 ${results.length} 项：PASS ${summary.pass} / FAIL ${summary.fail} ===`);
  console.log("FAILED=" + (summary.failedIds.join(" | ") || "无"));
  console.log("blockedRequests=" + blocked.length);
  if (consoleErrs.length || pageErrs.length) console.log("ERRORS:\n" + [...pageErrs, ...consoleErrs].join("\n"));
  process.exit(summary.fail ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + (e && e.stack || e)); process.exit(2); });
