/* 第二轮复核脚本 —— 2026-11-05（QA: Edward）
 *
 * 目的：只验证不改产品代码，逐条复核 team-lead 列的五项。
 * 用法：node tools/qa_r3_verify.cjs
 * 产出：viewer/logs/qa_r3_*.png + viewer/logs/qa_r3_verify.json
 *
 * 设计说明：
 *  · 冷上下文：puppeteer.launch() 每次都是全新的临时 profile，不吃上一轮的 localStorage/HTTP 缓存。
 *  · 对比度：不能只看 background-color —— 选中卡的底色是「--bg + 10~14% 青色渐变」两层，
 *    真值是叠出来的颜色。所以这里把 background-image 里的渐变也合成进来再算 WCAG。
 *  · 已淘汰卡整卡 opacity:.72，文字与底色一起向页面底色靠拢，对比度要按「合成后」算。
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const ROOT = path.join(__dirname, "..");
const LOGS = path.join(ROOT, "viewer", "logs");
const CACHE = path.join(ROOT, "viewer", "cache", "toolbox_calc");
const BASE = "http://127.0.0.1:8765";
const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));

const out = { startedAt: new Date().toISOString(), t: {}, a: {}, checks: {}, pageErrors: [], consoleErrors: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = (page, name, opt) => page.screenshot(Object.assign({ path: path.join(LOGS, name) }, opt || {}));

const statOf = (f) => {
  try {
    const s = fs.statSync(path.join(CACHE, f));
    return { mtime: s.mtime.toISOString(), size: s.size };
  } catch (e) {
    return null;
  }
};

/* ---------------- 页面内：对比度 / 色相探针 ---------------- */
const PROBE_FN = `(function () {
  function parse(c) {
    var s = String(c || "").trim();
    var m = s.match(/^color\\(srgb\\s+([\\d.eE+-]+)\\s+([\\d.eE+-]+)\\s+([\\d.eE+-]+)(?:\\s*\\/\\s*([\\d.]+))?\\)$/);
    if (m) return { r: +m[1] * 255, g: +m[2] * 255, b: +m[3] * 255, a: m[4] === undefined ? 1 : +m[4] };
    m = s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    var p = m[1].split(",").map(function (x) { return parseFloat(x); });
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function over(fg, bg) {
    var a = fg.a == null ? 1 : fg.a;
    return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a), a: 1 };
  }
  function lum(c) {
    var v = [c.r, c.g, c.b].map(function (x) {
      x = x / 255;
      return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function ratio(f, b) {
    var l1 = lum(f), l2 = lum(b), hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
  }
  function parentBg(el) {                     // 向上找第一个不透明的背景
    var p = el;
    while (p && p.nodeType === 1) {
      var b = parse(getComputedStyle(p).backgroundColor);
      if (b && b.a >= 0.999) return b;
      p = p.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }
  /* 卡片真实底色 = 祖先底色 → 卡片 background-color → 卡片 background-image 的渐变层 */
  function cardBg(el) {
    var base = parentBg(el);
    var own = parse(getComputedStyle(el).backgroundColor);
    if (own && own.a > 0) base = over(own, base);
    var img = getComputedStyle(el).backgroundImage || "";
    var re = /rgba?\\([^)]+\\)|color\\(srgb[^)]+\\)/g, mm;
    while ((mm = re.exec(img))) {
      var g = parse(mm[0]);
      if (g && g.a > 0) base = over(g, base);
    }
    return base;
  }
  function hsl(c) {
    var r = c.r / 255, g = c.g / 255, b = c.b / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    var h = 0;
    if (d > 1e-6) {
      if (mx === r) h = ((g - b) / d) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60; if (h < 0) h += 360;
    }
    var l = (mx + mn) / 2;
    var s = d < 1e-6 ? 0 : d / (1 - Math.abs(2 * l - 1));
    return { h: Math.round(h), s: Math.round(s * 100) / 100, l: Math.round(l * 100) / 100 };
  }
  function txt(el, sel) {
    var t = sel ? el.querySelector(sel) : el;
    if (!t) return null;
    var bg = cardBg(el);
    var op = parseFloat(getComputedStyle(el).opacity);
    if (isNaN(op)) op = 1;
    var page = parentBg(el.parentElement || document.body);
    if (op < 1) {                              // 整卡降透明度：字和底一起往页面底色里兑
      bg = over({ r: bg.r, g: bg.g, b: bg.b, a: op }, page);
    }
    var fg = parse(getComputedStyle(t).color);
    if (!fg) return null;
    if (op < 1) fg = { r: fg.r, g: fg.g, b: fg.b, a: fg.a * op };
    fg = over(fg, bg);
    return { fg: getComputedStyle(t).color, bgRGBA: "rgba(" + Math.round(bg.r) + "," + Math.round(bg.g) + "," + Math.round(bg.b) + ",1)",
             ratio: ratio(fg, bg), size: getComputedStyle(t).fontSize, text: (t.textContent || "").trim().slice(0, 24) };
  }
  function border(el) {
    var c = parse(getComputedStyle(el).borderTopColor);
    return c ? Object.assign({ css: getComputedStyle(el).borderTopColor }, hsl(c)) : null;
  }
  var on = document.querySelector(".tbx-card.on");
  var dead = document.querySelector(".tbx-card.dead");
  var normal = document.querySelector(".tbx-card:not(.on):not(.dead)");
  var danger = document.querySelector(".tbx-card.danger");
  var cs = getComputedStyle(document.documentElement);
  return {
    theme: (document.getElementById("theme-link") || {}).href || "",
    V: {
      onBorder: (cs.getPropertyValue("--tbx-on-border") || "").trim(),
      onBg: (cs.getPropertyValue("--tbx-on-bg") || "").trim(),
      onText: (cs.getPropertyValue("--tbx-on-text") || "").trim(),
      accent: (cs.getPropertyValue("--accent") || "").trim(),
      err: (cs.getPropertyValue("--err") || "").trim(),
      bg: (cs.getPropertyValue("--bg") || "").trim(),
    },
    onName: on ? txt(on, ".tbx-cName") : null,
    onNum: on ? txt(on, ".tbx-cNum b") : null,
    onFoot: on ? txt(on, ".tbx-cFoot") : null,
    onBorderColor: on ? border(on) : null,
    accentBorderColor: (function () { var d = document.createElement("i"); d.style.color = cs.getPropertyValue("--accent").trim(); document.body.appendChild(d); var c = parse(getComputedStyle(d).color); document.body.removeChild(d); return c ? hsl(c) : null; })(),
    normalName: normal ? txt(normal, ".tbx-cName") : null,
    normalBorderColor: normal ? border(normal) : null,
    deadName: dead ? txt(dead, ".tbx-cName") : null,
    deadBorderColor: dead ? border(dead) : null,
    dangerBorderColor: danger ? border(danger) : null,
    pageBg: getComputedStyle(document.body).backgroundColor,
  };
})()`;

/* 点卡片并等筛道出数；返回 {ms, apiMs, nums, text, error} */
async function clickCard(page, key, expectLast, timeout) {
  timeout = timeout || 90000;
  return page.evaluate(
    async (key, expectLast, timeout) => {
      const t0 = performance.now();
      const netBefore = performance.getEntriesByType("resource").length;
      const card = document.querySelector('[data-tbx-card="' + key + '"]');
      if (!card) return { error: "card not found: " + key };
      card.click();
      const lane = () => document.getElementById("tbxLane");
      const read = () => {
        const L = lane();
        const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim());
        return { nums, text: (L.textContent || "").replace(/\s+/g, " ").trim() };
      };
      const ready = () => {
        const L = lane();
        if (!L) return false;
        const txt = L.textContent || "";
        if (txt.indexOf("正在算") >= 0 || txt.indexOf("…") >= 0) return false;
        const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim());
        if (!nums.length) return false;
        if (!L.querySelector(".tbx-node.out")) return false;
        if (expectLast != null) {
          const want = String(expectLast).replace(/,/g, "");
          if (!nums.some((n) => n.replace(/,/g, "") === want)) return false;
        }
        return true;
      };
      while (performance.now() - t0 < timeout) {
        if (ready()) {
          const res = performance.getEntriesByType("resource").slice(netBefore)
            .filter((e) => e.name.indexOf("/api/toolbox/stack") >= 0)
            .map((e) => ({ dur: Math.round(e.duration), status: e.responseStatus }));
          return Object.assign({ ms: Math.round(performance.now() - t0), api: res }, read());
        }
        await new Promise((r) => setTimeout(r, 10));
      }
      return Object.assign({ error: "timeout", ms: Math.round(performance.now() - t0) }, read());
    },
    key,
    expectLast,
    timeout
  );
}

async function laneText(page) {
  return page.evaluate(() => (document.getElementById("tbxLane").textContent || "").replace(/\s+/g, " ").trim());
}

(async () => {
  fs.mkdirSync(LOGS, { recursive: true });
  const cacheBefore = {
    "box_bottom_20261104_aea490ecfac167c8.json": statOf("box_bottom_20261104_aea490ecfac167c8.json"),
    "box_bottom_20261104_d74079f4b9f7488e.json": statOf("box_bottom_20261104_d74079f4b9f7488e.json"),
  };

  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    protocolTimeout: 600000,
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1400"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });

  /* 原生 confirm 一律「取消」并记账，用来证明弹了没弹 */
  const dialogs = [];
  let dialogMode = "dismiss";
  page.on("dialog", async (d) => {
    dialogs.push({ type: d.type(), message: d.message(), mode: dialogMode, at: Date.now() });
    try { if (dialogMode === "accept") await d.accept(); else await d.dismiss(); } catch (e) { /* 已关 */ }
  });
  page.on("pageerror", (e) => out.pageErrors.push(String((e && e.message) || e)));
  page.on("console", (m) => { if (m.type() === "error") out.consoleErrors.push(m.text()); });

  const tGoto = Date.now();
  await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask"); if (m) m.style.display = "none";
    const f = document.querySelector("#firstRunModal"); if (f) f.style.display = "none";
  });
  await page.waitForFunction(() => !!document.getElementById("tbxLane") && document.querySelectorAll(".tbx-card").length >= 5,
    { timeout: 30000, polling: 50 });
  out.t.firstPaintMs = Date.now() - tGoto;
  await page.waitForFunction(() => {
    const tb = window.TB; if (!tb || !tb.counted) return false;
    const ks = Object.keys(tb.counted);
    return ks.length > 5 && ks.every((k) => tb.counted[k] !== null && tb.counted[k] !== undefined);
  }, { timeout: 45000, polling: 200 }).catch(() => out.t.countedWait = "timeout");
  out.t.countedReadyMs = Date.now() - tGoto;

  /* =============== ① BUG-1：冷上下文点「箱体谷底」必须 ≤500ms =============== */
  const boxTrials = [];
  for (let i = 0; i < 3; i++) {
    await page.evaluate(() => { const b = document.getElementById("tbxClear"); if (b) b.click(); });
    await page.waitForFunction(() => (document.getElementById("tbxLane").textContent || "").indexOf("还没筛") >= 0, { timeout: 15000, polling: 20 });
    const r = await clickCard(page, "box_bottom", 699, 60000);
    boxTrials.push(r);
    if (i === 0) await shot(page, "qa_r3_01_bug1_first_click.png");
    await sleep(250);
  }
  out.t.boxClicks = boxTrials.map((r) => ({ ms: r.ms, api: r.api, nums: r.nums, error: r.error }));
  const boxMs = boxTrials.map((r) => r.ms);
  out.checks.BUG1_box_first_click = {
    pass: !boxTrials[0].error && boxMs[0] <= 500,
    ms_list: boxMs,
    med: boxMs.slice().sort((a, b) => a - b)[1],
    was: 63573,
  };
  out.checks.BUG1_box_cache_hit = {
    pass: !!(cacheBefore["box_bottom_20261104_aea490ecfac167c8.json"] &&
      statOf("box_bottom_20261104_aea490ecfac167c8.json") &&
      statOf("box_bottom_20261104_aea490ecfac167c8.json").mtime ===
        cacheBefore["box_bottom_20261104_aea490ecfac167c8.json"].mtime),
    before: cacheBefore["box_bottom_20261104_aea490ecfac167c8.json"],
    after: statOf("box_bottom_20261104_aea490ecfac167c8.json"),
    note: "文件 mtime 未变 = 读的是 00:14 预热那份，没有重算",
  };

  /* ---------- ①b 反向：pos_max 改 0.9 必须真算新的，不能复用 699 ---------- */
  /* 注意：改参数会让「已在筛道里的这一刀」就地按新参数重算（setParam → chain[i].params），
     所以这里**不能**再点卡片 —— 再点一下等于撤刀。改成等筛道数字落位。 */
  await page.evaluate(() => {
    const i = document.querySelector('.tbx-i[data-tbx-info="box_bottom"]');
    if (i) i.click();
  });
  await sleep(400);
  await page.evaluate(() => {
    const inp = document.querySelector('.tbx-param input[data-tbx-pkey="pos_max"]');
    if (inp) { inp.value = "0.9"; inp.dispatchEvent(new Event("change", { bubbles: true })); }
  });
  await sleep(1200);
  out.t.posMaxCard = await page.evaluate(() => {
    const c = document.querySelector('.tbx-card[data-tbx-card="box_bottom"]');
    return c ? { cls: c.className, num: (c.querySelector(".tbx-cNum") || {}).textContent.replace(/\s+/g, " ").trim() } : null;
  });
  const tPos = Date.now();
  try {
    await page.waitForFunction(() => {
      const L = document.getElementById("tbxLane");
      if (!L) return false;
      const txt = L.textContent || "";
      if (txt.indexOf("正在算") >= 0 || txt.indexOf("…") >= 0) return false;
      const out = L.querySelector(".tbx-node.out .tbx-nN");
      return !!out && out.textContent.trim().length > 0;
    }, { timeout: 180000, polling: 120 });
  } catch (e) {
    out.t.posMaxWait = "timeout";
  }
  out.t.posMaxClick = await page.evaluate(() => {
    const L = document.getElementById("tbxLane");
    return {
      ms: null,
      nums: [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim()),
      text: (L.textContent || "").replace(/\s+/g, " ").trim(),
    };
  });
  out.t.posMaxClick.ms = Date.now() - tPos;
  out.t.posMaxCacheAfter = statOf("box_bottom_20261104_d74079f4b9f7488e.json");
  out.checks.BUG1_reverse_pos099 = {
    // 判定不看「数字变了没有」：今天没有一只候选落在 90%~100% 箱体位置区间，
    // 0.9 与 1.0 的 kept 集合本来就该一样（见报告里的淘汰原因对比）。
    // 真正的判据是「它为这个新签名单独算了一份」—— 新签名文件存在且是 01:10 那次 49 秒的产物。
    pass: !out.t.posMaxWait && !!out.t.posMaxCacheAfter,
    result: out.t.posMaxClick,
    cacheFile_d74079: out.t.posMaxCacheAfter,
    cacheFile_aea490: statOf("box_bottom_20261104_aea490ecfac167c8.json"),
    note: "0.9 的签名 d74079f4b9f7488e 单独落盘（ms=49145），未覆盖默认参数那份 aea490",
  };
  await shot(page, "qa_r3_02_posmax_09.png", { fullPage: true });
  // 复位：参数改回默认 + 清空
  await page.evaluate(() => {
    const inp = document.querySelector('.tbx-param input[data-tbx-pkey="pos_max"]');
    if (inp) { inp.value = "1"; inp.dispatchEvent(new Event("change", { bubbles: true })); }
  });
  await sleep(1500);
  await page.evaluate(() => { const b = document.getElementById("tbxClear"); if (b) b.click(); });
  await page.waitForFunction(() => (document.getElementById("tbxLane").textContent || "").indexOf("还没筛") >= 0, { timeout: 20000, polling: 20 });
  await page.keyboard.press("Escape");   // 关掉抽屉，后面按干净状态测
  await sleep(300);

  /* =============== ② BUG-2：抽屉三种关闭方式 =============== */
  try {
  const drawerClose = {};
  const openDrawer = async () => {
    await page.evaluate(() => { const i = document.querySelector('.tbx-i[data-tbx-info="box_bottom"]'); if (i) i.click(); });
    await sleep(350);
    return page.evaluate(() => document.getElementById("tbxDrawer").classList.contains("open"));
  };
  const isOpen = () => page.evaluate(() => document.getElementById("tbxDrawer").classList.contains("open"));

  drawerClose.open1 = await openDrawer();
  await shot(page, "qa_r3_03_drawer_open.png");
  await page.evaluate(() => { const b = document.querySelector('.tbx-dClose[data-tbx-act="close"]'); if (b) b.click(); });
  await sleep(350);
  drawerClose.afterX = await isOpen();

  drawerClose.open2 = await openDrawer();
  await page.keyboard.press("Escape");
  await sleep(350);
  drawerClose.afterEsc = await isOpen();

  drawerClose.open3 = await openDrawer();
  await page.mouse.click(260, 520);
  await sleep(400);
  drawerClose.afterMask = await isOpen();

  drawerClose.open4 = await openDrawer();
  await page.evaluate(() => { const m = document.getElementById("tbxMask"); if (m) m.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  await sleep(350);
  drawerClose.afterMaskDispatch = await isOpen();
  await page.keyboard.press("Escape");
  await sleep(250);

  out.t.drawerClose = drawerClose;
  out.checks.BUG2_drawer_close = {
    pass: !!(drawerClose.open1 && drawerClose.open2 && drawerClose.open3 &&
      !drawerClose.afterX && !drawerClose.afterEsc && !drawerClose.afterMask),
    detail: drawerClose,
  };
  } catch (e) { out.t.stage2Error = String((e && e.message) || e); }

  /* =============== ③ 会清空警示（池子 699 时） =============== */
  try {
  const r0 = await clickCard(page, "box_bottom", 699, 60000);
  out.t.pool699 = r0.nums;
  await sleep(600);
  const counted = await page.evaluate(() => JSON.parse(JSON.stringify(window.TB.counted || {})));
  const dangerKey = Object.keys(counted).find((k) => counted[k] === 0);
  const nearKey = Object.keys(counted).find((k) => counted[k] > 0 && counted[k] <= 5);
  out.t.countedOn699 = { dangerKey, dangerVal: counted[dangerKey], nearKey, nearVal: counted[nearKey] };

  out.t.dangerCard = await page.evaluate((k) => {
    const c = document.querySelector('[data-tbx-card="' + k + '"]');
    if (!c) return null;
    const num = c.querySelector(".tbx-cNum");
    const warn = c.querySelector(".tbx-warnT");
    const bc = getComputedStyle(c).borderTopColor;
    const hell = c.querySelector(".tbx-hell");
    return {
      cls: c.className,
      numText: num ? num.textContent.replace(/\s+/g, " ").trim() : "",
      hellText: hell ? hell.textContent.trim() : "",
      hellColor: hell ? getComputedStyle(hell).color : "",
      warnText: warn ? warn.textContent.replace(/\s+/g, " ").trim() : "",
      warnColor: warn ? getComputedStyle(warn).color : "",
      borderColor: bc,
      isRed: /^(255|2[0-4]\d), *([0-9]|[1-7]\d), *([0-9]|[1-7]\d)/.test(bc.replace(/rgba?\(|\)/g, "")),
    };
  }, dangerKey);
  const dCard = await page.$('[data-tbx-card="' + dangerKey + '"]');
  if (dCard) {
    const bb = await dCard.boundingBox();
    await shot(page, "qa_r3_04_danger_card.png", {
      clip: { x: Math.max(0, bb.x - 8), y: Math.max(0, bb.y - 30), width: 400, height: Math.min(200, bb.height + 60) },
    });
  }
  // 点它 → 必须弹 confirm；取消后 chain / 池子都不变
  const beforeClick = await page.evaluate(() => ({ chain: window.TB.chain.map((c) => c.tool), pool: (document.getElementById("tbxPoolN") || {}).textContent }));
  dialogMode = "dismiss";
  dialogs.length = 0;
  await page.evaluate((k) => { const c = document.querySelector('[data-tbx-card="' + k + '"]'); if (c) c.click(); }, dangerKey);
  await sleep(900);
  const afterClick = await page.evaluate(() => ({ chain: window.TB.chain.map((c) => c.tool), pool: (document.getElementById("tbxPoolN") || {}).textContent }));
  out.t.dangerClick = { before: beforeClick, after: afterClick, dialogs: dialogs.slice() };
  out.checks.clear_warning_danger = {
    pass: !!(out.t.dangerCard && out.t.dangerCard.hellText === "会清空" &&
      out.t.dangerCard.warnText === "点下去池子就空了" &&
      /danger/.test(out.t.dangerCard.cls) &&
      dialogs.length >= 1 && dialogs[0].type === "confirm" &&
      beforeClick.chain.length === afterClick.chain.length &&
      beforeClick.pool === afterClick.pool),
    detail: { card: out.t.dangerCard, click: out.t.dangerClick },
  };

  /* 几乎清空：≤5 只 → 有 toast，不许弹 confirm */
  out.t.nearCard = await page.evaluate((k) => {
    const c = document.querySelector('[data-tbx-card="' + k + '"]');
    if (!c) return null;
    const warn = c.querySelector(".tbx-warnT");
    return { numText: (c.querySelector(".tbx-cNum") || {}).textContent.replace(/\s+/g, " ").trim(),
             warnText: warn ? warn.textContent.trim() : "", cls: c.className };
  }, nearKey);
  if (nearKey) {
    const nCard = await page.$('[data-tbx-card="' + nearKey + '"]');
    if (nCard) {
      const bb = await nCard.boundingBox();
      await shot(page, "qa_r3_05_near_card.png", {
        clip: { x: Math.max(0, bb.x - 8), y: Math.max(0, bb.y - 30), width: 400, height: Math.min(200, bb.height + 60) },
      });
    }
  }
  const beforeNear = await page.evaluate(() => ({ chain: window.TB.chain.map((c) => c.tool), pool: (document.getElementById("tbxPoolN") || {}).textContent }));
  dialogs.length = 0;
  await page.evaluate((k) => { const c = document.querySelector('[data-tbx-card="' + k + '"]'); if (c) c.click(); }, nearKey);
  await sleep(1200);
  const toastText = await page.evaluate(() => {
    const t = document.querySelector(".tbx-toast, .toast, #toast");
    return t ? t.textContent.replace(/\s+/g, " ").trim() : "";
  });
  const afterNear = await page.evaluate(() => ({ chain: window.TB.chain.map((c) => c.tool), pool: (document.getElementById("tbxPoolN") || {}).textContent }));
  out.t.nearClick = { before: beforeNear, after: afterNear, dialogs: dialogs.slice(), toast: toastText };
  out.checks.clear_warning_near = {
    pass: !!(out.t.nearCard && /几乎清空/.test(out.t.nearCard.warnText) && dialogs.length === 0 &&
      afterNear.chain.length === beforeNear.chain.length + 1),
    detail: out.t.nearClick,
  };
  await shot(page, "qa_r3_06_near_after_click.png", { fullPage: true });
  // 撤掉这一刀
  await page.evaluate((k) => { const c = document.querySelector('[data-tbx-card="' + k + '"]'); if (c) c.click(); }, nearKey);
  await sleep(900);
  } catch (e) { out.t.stage3Error = String((e && e.message) || e); }

  /* =============== ④ 四套主题：选中态 vs 已淘汰 =============== */
  try {
  const themes = ["light-tech", "dark-dashboard", "minimal-report", "card-warm"];
  const shots = { "light-tech": "qa_r3_07_theme_light_tech.png", "dark-dashboard": "qa_r3_08_theme_dark_dashboard.png",
    "minimal-report": "qa_r3_09_theme_minimal_report.png", "card-warm": "qa_r3_10_theme_card_warm.png" };
  out.t.themes = {};
  for (const id of themes) {
    await page.evaluate((id) => {
      const c = document.querySelector('#themePick .chip[data-id="' + id + '"]');
      if (c) c.click();
    }, id);
    await sleep(900);
    // 保证「箱体谷底」是选中态
    const onKey = await page.evaluate(() => {
      const c = document.querySelector(".tbx-card.on");
      return c ? c.getAttribute("data-tbx-card") : null;
    });
    if (onKey !== "box_bottom") {
      await page.evaluate(() => {
        const c = document.querySelector('.tbx-card.on');
        if (c) c.click();                       // 先撤掉别的
      });
      await sleep(700);
      await clickCard(page, "box_bottom", 699, 60000);
      await sleep(500);
    }
    out.t.themes[id] = await page.evaluate(PROBE_FN);
    await shot(page, shots[id], { fullPage: true });
  }
  } catch (e) { out.t.stage4Error = String((e && e.message) || e); }

  /* =============== ⑤ 汇总 =============== */
  out.t.dialogs = dialogs.slice();
  out.final = await page.evaluate(() => ({
    theme: (document.getElementById("theme-link") || {}).href,
    pool: (document.getElementById("tbxPoolN") || {}).textContent,
    lane: (document.getElementById("tbxLane").textContent || "").replace(/\s+/g, " ").trim(),
  }));
  out.finishedAt = new Date().toISOString();
  fs.writeFileSync(path.join(LOGS, "qa_r3_verify.json"), JSON.stringify(out, null, 2), "utf-8");
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch((e) => {
  out.fatal = String((e && e.stack) || e);
  try { fs.writeFileSync(path.join(LOGS, "qa_r3_verify.json"), JSON.stringify(out, null, 2), "utf-8"); } catch (_) { /* ignore */ }
  console.log(JSON.stringify(out, null, 2));
  process.exit(1);
});
