/* 筛台（工具箱 · 筛台）验收脚本 —— 2026-11-05
 *
 * 用途：按 docs/工具箱筛台-实现规格.md 第五节 10 条验收标准，用真浏览器逐条取证。
 * 退出码 0 = 跑完（结论看 stdout 的 JSON），1 = 脚本自身崩溃。
 *
 * 用法：node tools/qa_sieve_verify.cjs
 * 产出：viewer/logs/qa_sieve_*.png + stdout JSON 报告
 *
 * 注意：本机有 HTTP_PROXY，node/puppeteer 走 127.0.0.1 直连不受影响；
 *      探活请用 python urllib，不要用 curl。
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const ROOT = path.join(__dirname, "..");
const LOGS = path.join(ROOT, "viewer", "logs");
const BASE = "http://127.0.0.1:8765";
const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));

const out = { t: {}, checks: [], errors: [], notes: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 对比度取样：取「已加入筛道」卡片的底色/字色 + 筛道数字，算 WCAG 对比度 */
const CONTRAST_FN = `(() => {
  function parse(c) {
    var s = String(c).trim();
    var m = s.match(/^color\\(srgb\\s+([\\d.eE+-]+)\\s+([\\d.eE+-]+)\\s+([\\d.eE+-]+)(?:\\s*\\/\\s*([\\d.]+))?\\)$/);
    if (m) return { r: parseFloat(m[1]) * 255, g: parseFloat(m[2]) * 255, b: parseFloat(m[3]) * 255,
                    a: m[4] === undefined ? 1 : parseFloat(m[4]) };
    m = s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    var p = m[1].split(",").map(function (x) { return parseFloat(x); });
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum(c) {
    var v = [c.r, c.g, c.b].map(function (x) {
      x = x / 255;
      return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function flat(fg, bg) {
    if (!fg || !bg) return fg;
    var a = fg.a == null ? 1 : fg.a;
    return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a), a: 1 };
  }
  function ratio(fgRaw, bgRaw) {
    var bg = parse(bgRaw), fg = parse(fgRaw);
    if (!bg || !fg) return null;
    fg = flat(fg, bg);
    var l1 = lum(fg), l2 = lum(bg);
    var hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
  }
  function effBg(el) {
    var p = el;
    while (p) {
      var b = getComputedStyle(p).backgroundColor;
      if (b && b !== "rgba(0, 0, 0, 0)" && b !== "transparent") return b;
      p = p.parentElement;
    }
    return "rgb(255,255,255)";
  }
  function probe(sel, textSel) {
    var el = document.querySelector(sel);
    if (!el) return null;
    var tEl = textSel ? el.querySelector(textSel) : el;
    if (!tEl) return null;
    var bg = effBg(el), fg = getComputedStyle(tEl).color;
    return { bg: bg, fg: fg, ratio: ratio(fg, bg), size: getComputedStyle(tEl).fontSize };
  }
  return {
    theme: (document.getElementById("theme-link") || {}).getAttribute ? document.getElementById("theme-link").getAttribute("href") : "",
    pageBg: getComputedStyle(document.body).backgroundColor,
    cardOn: probe(".tbx-card.on", ".tbx-cName"),
    cardOnNum: probe(".tbx-card.on", ".tbx-cNum b"),
    cardOnFoot: probe(".tbx-card.on", ".tbx-cFoot"),
    cardNormal: probe(".tbx-card:not(.on):not(.dead)", ".tbx-cName"),
    laneNum: probe(".tbx-node.out", ".tbx-nN"),
    laneLabel: probe(".tbx-node.out", ".tbx-nL"),
    cardDead: probe(".tbx-card.dead", ".tbx-cName")
  };
})()`;


/* 在页面里点一张卡，等到筛道里出现「不是 …/— 的真实数字」为止，返回毫秒数。 */
async function clickCardAndWait(page, key, expectLast, timeout = 60000) {
  return page.evaluate(
    async (key, expectLast, timeout) => {
      const t0 = performance.now();
      const card = document.querySelector('[data-tbx-card="' + key + '"]');
      if (!card) return { error: "card not found: " + key };
      card.click();
      const lane = () => document.getElementById("tbxLane");
      const read = () => {
        const L = lane();
        const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim());
        const labels = [...L.querySelectorAll(".tbx-node .tbx-nL")].map((x) => x.textContent.trim());
        const drops = [...L.querySelectorAll(".tbx-node .tbx-nD")].map((x) => x.textContent.trim());
        return { nums, labels, drops, text: (L.textContent || "").replace(/\s+/g, " ").trim() };
      };
      const ready = () => {
        const L = lane();
        if (!L) return false;
        const txt = L.textContent || "";
        if (txt.indexOf("正在算") >= 0 || txt.indexOf("…") >= 0) return false;
        const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim());
        if (nums.length < 3) return false;
        if (!L.querySelector(".tbx-node.out")) return false;
        if (expectLast != null) {
          const want = String(expectLast).replace(/,/g, "");
          if (!nums.some((n) => n.replace(/,/g, "") === want)) return false;
        }
        return true;
      };
      while (performance.now() - t0 < timeout) {
        if (ready()) return Object.assign({ ms: Math.round(performance.now() - t0) }, read());
        await new Promise((r) => setTimeout(r, 12));
      }
      return Object.assign({ error: "timeout", ms: Math.round(performance.now() - t0) }, read());
    },
    key,
    expectLast,
    timeout
  );
}

/* 点筛道节点上的 ×，等数字回到 expectLast */
async function clickCutAndWait(page, idx, expectLast, timeout = 20000) {
  return page.evaluate(
    async (idx, expectLast, timeout) => {
      const t0 = performance.now();
      const x = document.querySelector('#tbxLane .tbx-nX[data-tbx-del="' + idx + '"]');
      if (!x) return { error: "× not found at " + idx };
      x.click();
      const lane = () => document.getElementById("tbxLane");
      while (performance.now() - t0 < timeout) {
        const L = lane();
        if (L) {
          const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((n) => n.textContent.trim());
          const txt = L.textContent || "";
          const pending = txt.indexOf("正在算") >= 0 || txt.indexOf("…") >= 0;
          const want = String(expectLast).replace(/,/g, "");
          if (!pending && nums.some((n) => n.replace(/,/g, "") === want)) {
            return {
              ms: Math.round(performance.now() - t0),
              nums,
              labels: [...L.querySelectorAll(".tbx-node .tbx-nL")].map((n) => n.textContent.trim()),
              pool: (document.getElementById("tbxPoolN") || {}).textContent,
            };
          }
        }
        await new Promise((r) => setTimeout(r, 12));
      }
      return { error: "timeout", text: (lane() || {}).textContent };
    },
    idx,
    expectLast,
    timeout
  );
}

(async () => {
  fs.mkdirSync(LOGS, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1600"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });

  const consoleErrs = [];
  const pageErrs = [];
  const netStack = [];
  page.on("pageerror", (e) => pageErrs.push(String(e && e.message ? e.message : e)));
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrs.push(m.text());
  });
  page.on("response", (r) => {
    const u = r.url();
    if (u.indexOf("/api/toolbox/stack") >= 0) netStack.push({ url: u.slice(u.indexOf("/api/")), status: r.status() });
  });

  /* ---------- 1. 首屏 3 秒内出筛道 + 工具墙 ---------- */
  const tGoto = Date.now();
  await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask");
    if (m) m.style.display = "none";
    const fm = document.querySelector("#firstRunModal");
    if (fm) fm.style.display = "none";
  });
  let firstPaintMs = null;
  try {
    await page.waitForFunction(
      () => !!document.getElementById("tbxLane") && document.querySelectorAll(".tbx-card").length >= 5,
      { timeout: 20000, polling: 50 }
    );
    firstPaintMs = Date.now() - tGoto;
  } catch (e) {
    out.checks.push({ id: 1, ok: false, why: "首屏 20 秒内没出筛道/工具墙：" + e.message });
  }
  out.t.firstPaintMs = firstPaintMs;

  // 等数字都落位（预热已完成，正常 1~2 轮轮询内齐）
  try {
    await page.waitForFunction(
      () => {
        const tb = window.TB;
        if (!tb || !tb.stack) return false;
        const c = tb.counted || {};
        const ks = Object.keys(c);
        return ks.length > 5 && ks.every((k) => c[k] !== null && c[k] !== undefined);
      },
      { timeout: 30000, polling: 200 }
    );
  } catch (e) {
    out.notes.push("等待 counted 全部落位超时（可能有工具还在算）");
  }

  /* ---------- 首屏结构快照 ---------- */
  out.firstScreen = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".tbx-card")];
    const pending = cards.filter((c) => (c.textContent || "").indexOf("待算") >= 0);
    const dead = cards.filter((c) => c.classList.contains("dead"));
    return {
      title: (document.querySelector(".tbx-title") || {}).textContent,
      pool: (document.getElementById("tbxPoolN") || {}).textContent,
      laneText: (document.getElementById("tbxLane") || {}).textContent.replace(/\s+/g, " ").trim(),
      wallN: (document.getElementById("tbxWallN") || {}).textContent,
      cards: cards.length,
      pendingCards: pending.length,
      deadCards: dead.map((c) => (c.querySelector(".tbx-cName") || {}).textContent),
      laneCount: document.querySelectorAll("#tbxLane .tbx-node").length,
      warmText: (document.getElementById("tbxLaneN") || {}).textContent,
      suggested: (() => {
        const n = document.querySelector(".tbx-nextName");
        const v = document.querySelector(".tbx-nextN");
        return n && v ? { name: n.textContent.trim(), text: v.textContent.replace(/\s+/g, " ").trim() } : null;
      })(),
      resText: (document.getElementById("tbxRes") || {}).textContent.replace(/\s+/g, " ").trim().slice(0, 80),
    };
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_01_empty.png"), fullPage: true });
  out.checks.push({
    id: 1,
    ok: firstPaintMs != null && firstPaintMs <= 3000 && out.firstScreen.cards >= 20,
    detail: "首屏 " + firstPaintMs + "ms，卡 " + out.firstScreen.cards + " 张，待算卡 " + out.firstScreen.pendingCards + " 张",
  });

  /* ---------- 2. 点「箱体谷底」 ---------- */
  out.t.boxClick = await clickCardAndWait(page, "box_bottom", 699, 45000);
  out.checks.push({
    id: 2,
    ok: !out.t.boxClick.error && /699/.test((out.t.boxClick.nums || []).join(",")),
    detail: "点击后 " + out.t.boxClick.ms + "ms 出数；筛道 " + (out.t.boxClick.text || "").slice(0, 120),
  });
  out.t.cardOnState = await page.evaluate(() => {
    const c = document.querySelector('.tbx-card[data-tbx-card="box_bottom"]');
    return c ? { cls: c.className, num: (c.querySelector(".tbx-cNum") || {}).textContent, foot: (c.querySelector(".tbx-cFoot") || {}).textContent } : null;
  });

  /* ---------- 3. 再点「准入闸门」：必须 ≤500ms ---------- */
  out.t.gateClick = await clickCardAndWait(page, "gate_admit", 679, 45000);
  out.checks.push({
    id: 3,
    ok: !out.t.gateClick.error && out.t.gateClick.ms <= 500,
    detail: "点击后 " + out.t.gateClick.ms + "ms 出数（阈值 500ms）；筛道 " + (out.t.gateClick.text || "").slice(0, 140),
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_02_two_stage.png"), fullPage: true });

  /* ---------- 9. 结果表 ---------- */
  out.t.table = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".tbx-tbl tbody tr")];
    const toggle = document.querySelector(".tbx-toggle");
    return {
      head: (document.getElementById("tbxResT") || {}).textContent,
      sub: (document.getElementById("tbxResN") || {}).textContent,
      rowCount: rows.length,
      cols: document.querySelectorAll(".tbx-tbl thead th").length,
      thText: [...document.querySelectorAll(".tbx-tbl thead th")].map((x) => x.textContent.replace(/\s+/g, " ").trim()),
      toggleText: toggle ? toggle.textContent.replace(/\s+/g, " ").trim() : "",
      hasToggle: !!toggle,
      firstRow: rows[0] ? [...rows[0].children].map((td) => td.textContent.replace(/\s+/g, " ").trim()) : [],
      dots: document.querySelectorAll(".tbx-dot").length,
      okDots: document.querySelectorAll(".tbx-dot.ok").length,
      noDots: document.querySelectorAll(".tbx-dot.no").length,
    };
  });
  if (out.t.table.hasToggle) {
    await page.evaluate(() => {
      const cb = document.querySelector('.tbx-toggle input[data-tbx-act="toggle-dead"]');
      if (cb) cb.click();
    });
    await sleep(400);
  }
  out.t.tableDropped = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".tbx-tbl tbody tr")];
    const dead = rows.filter((r) => r.classList.contains("tbx-rowDead"));
    const sample = dead.find((r) => (r.textContent || "").indexOf("级淘汰") >= 0);
    const levels = [...new Set(dead.map((r) => ((r.textContent.match(/第 (\\d+) 级淘汰/) || [])[1] || "?")))];
    return {
      rowCount: rows.length,
      deadRows: dead.length,
      levels: levels,
      okDots: document.querySelectorAll(".tbx-dot.ok").length,
      noDots: document.querySelectorAll(".tbx-dot.no").length,
      sample: sample ? [...sample.children].map((td) => td.textContent.replace(/\s+/g, " ").trim()).slice(-3) : [],
      checked: !!(document.querySelector('.tbx-toggle input') || {}).checked,
    };
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_03_dropped.png"), fullPage: true });
  out.checks.push({
    id: 9,
    ok:
      out.t.table.hasToggle &&
      out.t.tableDropped.deadRows > 0 &&
      out.t.tableDropped.sample.length > 0 &&
      /第 \d+ 级淘汰：/.test((out.t.tableDropped.sample || []).join(" ")) &&
      out.t.tableDropped.okDots > 0 &&
      out.t.tableDropped.noDots > 0,
    detail: JSON.stringify(out.t.tableDropped.sample),
  });
  // 关掉淘汰行，回到干净状态
  await page.evaluate(() => {
    const cb = document.querySelector('.tbx-toggle input[data-tbx-act="toggle-dead"]');
    if (cb && cb.checked) cb.click();
  });
  await sleep(300);

  /* ---------- 4. 点筛道节点 ×：只撤掉这一刀 ---------- */
  out.t.cut = await page.evaluate(async () => {
    const before = document.querySelectorAll("#tbxLane .tbx-node").length;
    const t0 = performance.now();
    const x = document.querySelector('#tbxLane .tbx-nX[data-tbx-del="1"]');
    if (!x) return { error: "no ×" };
    x.click();
    while (performance.now() - t0 < 20000) {
      const nodes = [...document.querySelectorAll("#tbxLane .tbx-node")];
      const txt = (document.getElementById("tbxLane").textContent || "").replace(/\s+/g, " ").trim();
      if (nodes.length === before - 1 && txt.indexOf("679") < 0 && !/正在算|…/.test(txt)) {
        return {
          ms: Math.round(performance.now() - t0), before: before, after: nodes.length,
          nums: nodes.map((n) => n.querySelector(".tbx-nN").textContent.trim()), text: txt,
          pool: (document.getElementById("tbxPoolN") || {}).textContent,
          chain: window.TB.chain.map((c) => c.tool),
        };
      }
      await new Promise((r) => setTimeout(r, 8));
    }
    return { error: "timeout", text: document.getElementById("tbxLane").textContent };
  });
  out.checks.push({
    id: 4,
    ok: !out.t.cut.error && out.t.cut.after === out.t.cut.before - 1 && out.t.cut.chain.length === 1,
    detail: JSON.stringify(out.t.cut),
  });

  /* ---------- 5. 再点同一张卡：撤掉，池子回到上一级 ---------- */
  out.t.cardToggleOff = await page.evaluate(async () => {
    const t0 = performance.now();
    const card = document.querySelector('[data-tbx-card="box_bottom"]');
    card.click();
    while (performance.now() - t0 < 15000) {
      const txt = (document.getElementById("tbxLane").textContent || "");
      if (txt.indexOf("还没筛") >= 0) {
        return {
          ms: Math.round(performance.now() - t0),
          lane: txt.replace(/\s+/g, " ").trim(),
          pool: (document.getElementById("tbxPoolN") || {}).textContent,
          chainLen: window.TB.chain.length,
        };
      }
      await new Promise((r) => setTimeout(r, 12));
    }
    return { error: "timeout" };
  });
  out.checks.push({
    id: 5,
    ok: !out.t.cardToggleOff.error && out.t.cardToggleOff.chainLen === 0,
    detail: JSON.stringify(out.t.cardToggleOff),
  });

  /* ---------- 6. 建议下一刀 == 手动点该卡的剩余数 ---------- */
  async function suggestVsManual(tag) {
    const sg = await page.evaluate(() => {
      const n = document.querySelector(".tbx-nextName");
      const v = document.querySelector(".tbx-nextN");
      if (!n || !v) return null;
      const txt = v.textContent.replace(/\s+/g, " ").trim();
      const mm = txt.match(/→\s*([\d,]+)/);
      const key = (() => {
        const name = n.textContent.trim();
        const t = (window.TB.data.tools || []).find((x) => x.name === name);
        return t ? t.tool_key : null;
      })();
      return { name: n.textContent.trim(), key: key, text: txt, num: mm ? Number(mm[1].replace(/,/g, "")) : null };
    });
    if (!sg || !sg.key || sg.num == null) return { tag, ok: false, why: "没读到建议" + JSON.stringify(sg) };
    const manual = await clickCardAndWait(page, sg.key, sg.num, 60000);
    // 手动点完后，最后一刀就是它：取倒数第二个节点（= 这一刀的输出）
    const nums = (manual.nums || []).map((x) => Number(String(x).replace(/,/g, "")));
    const got = nums[nums.length - 2];
    // 撤掉
    await page.evaluate((k) => {
      const c = document.querySelector('[data-tbx-card="' + k + '"]');
      if (c) c.click();
    }, sg.key);
    await sleep(900);
    return { tag, ok: got === sg.num, suggest: sg.num, manual: got, name: sg.name, key: sg.key, ms: manual.ms };
  }
  out.t.suggestEmpty = await suggestVsManual("空筛道");
  // 一级筛道下的建议
  await clickCardAndWait(page, "box_bottom", 699, 30000);
  out.t.suggestOneStage = await suggestVsManual("一级（699）");
  out.checks.push({
    id: 6,
    ok: !!(out.t.suggestEmpty.ok && out.t.suggestOneStage.ok),
    detail: JSON.stringify({ empty: out.t.suggestEmpty, one: out.t.suggestOneStage }),
  });
  // 清空
  await page.click("#tbxClear");
  await sleep(900);

  /* ---------- 7. 已淘汰工具 ---------- */
  out.t.retired = await page.evaluate(async () => {
    const dead = [...document.querySelectorAll(".tbx-card.dead")];
    const before = window.TB.chain.length;
    const info = dead.map((d) => ({
      key: d.getAttribute("data-tbx-card"),
      name: (d.querySelector(".tbx-cName") || {}).textContent,
      opac: getComputedStyle(d).opacity,
      borderStyle: getComputedStyle(d).borderTopStyle,
      badge: (d.querySelector(".tbx-dead") || {}).textContent,
      foot: (d.querySelector(".tbx-cFoot") || {}).textContent,
      num: (d.querySelector(".tbx-cNum") || {}).textContent,
    }));
    if (dead.length) dead[0].click();
    await new Promise((r) => setTimeout(r, 500));
    const box = document.querySelector(".tbx-deadBox");
    return {
      count: dead.length,
      info: info,
      chainBefore: before,
      chainAfter: window.TB.chain.length,
      drawerOpen: document.getElementById("tbxDrawer").classList.contains("open"),
      deadBoxText: box ? box.textContent.replace(/\s+/g, " ").trim().slice(0, 200) : "",
      drawerTitle: (document.querySelector(".tbx-dTop b") || {}).textContent,
    };
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_04_drawer_dead.png") });
  out.checks.push({
    id: 7,
    ok:
      out.t.retired.count === 2 &&
      out.t.retired.chainAfter === out.t.retired.chainBefore &&
      out.t.retired.drawerOpen &&
      /已淘汰/.test(out.t.retired.deadBoxText) &&
      out.t.retired.info.every((x) => x.badge === "已淘汰" && Number(x.opac) < 1),
    detail: JSON.stringify(out.t.retired),
  });
  // Esc 关闭
  await page.keyboard.press("Escape");
  await sleep(350);
  out.t.escClose1 = await page.evaluate(() => !document.getElementById("tbxDrawer").classList.contains("open"));

  /* ---------- 8. 抽屉：内容 + 点遮罩/ Esc 关闭 + 轮询不冲输入框 ---------- */
  out.t.drawer = await page.evaluate(async () => {
    const dot = document.querySelector('.tbx-i[data-tbx-info="box_bottom"]');
    const chainBefore = window.TB.chain.length;
    dot.click();
    await new Promise((r) => setTimeout(r, 300));
    const rows = [...document.querySelectorAll(".tbx-drawer .tbx-rowK")].map((x) => x.textContent.trim());
    const meta = (document.querySelector(".tbx-dMeta") || {}).textContent.replace(/\s+/g, " ").trim();
    return {
      open: document.getElementById("tbxDrawer").classList.contains("open"),
      maskOpen: document.getElementById("tbxMask").classList.contains("open"),
      chainUnchanged: window.TB.chain.length === chainBefore,
      rows: rows,
      meta: meta,
      params: [...document.querySelectorAll(".tbx-param > span")].map((x) => x.textContent.trim()),
      hasReasonInput: !!document.getElementById("tbxReason"),
      closeBtn: !!document.querySelector('.tbx-dClose[data-tbx-act="close"]'),
    };
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_05_drawer.png") });
  out.checks.push({
    id: 7.5,
    ok:
      out.t.drawer.open &&
      ["收录", "为什么收它", "作用", "实测表现", "短板"].filter((k) => !(out.t.drawer.rows.join() + out.t.drawer.meta).includes(k)).length === 0 &&
      out.t.drawer.params.length > 0 &&
      out.t.drawer.chainUnchanged,
    detail: JSON.stringify({ rows: out.t.drawer.rows, params: out.t.drawer.params, meta: out.t.drawer.meta }),
  });

  /* 8a. 改参数（谷底驻留下限 0.15 → 0.30）：抽屉保持，触发后台重算；观测轮询是否冲掉输入框 */
  out.t.paramChange = await page.evaluate(async () => {
    const inp = document.querySelector('.tbx-param input[data-tbx-pkey="min_dwell"]');
    const reason = document.getElementById("tbxReason");
    reason.value = "QA 轮询保护测试：这行字不能被冲掉";
    reason.focus();
    inp.value = "0.30";
    inp.dispatchEvent(new Event("change", { bubbles: true }));
    // 真实轮询会在这段时间里反复跑（因为 digest 变了 → pending）
    await new Promise((r) => setTimeout(r, 5000));
    const still = document.getElementById("tbxReason");
    return {
      drawerStillOpen: !!still && document.getElementById("tbxDrawer").classList.contains("open"),
      reasonValue: still ? still.value : null,
      focused: document.activeElement ? document.activeElement.id : "",
      override: JSON.parse(JSON.stringify(window.TB.overrides.box_bottom || {})),
      pending: window.TB.stack ? !!(window.TB.stack.pending || (window.TB.counted.box_bottom === null)) : null,
      laneText: (document.getElementById("tbxLane") || {}).textContent.replace(/\s+/g, " ").trim(),
    };
  });
  out.checks.push({
    id: 8.1,
    ok:
      out.t.paramChange.drawerStillOpen &&
      out.t.paramChange.reasonValue === "QA 轮询保护测试：这行字不能被冲掉" &&
      out.t.paramChange.override.min_dwell === 0.3,
    detail: JSON.stringify(out.t.paramChange),
  });
  // 点遮罩关闭
  await page.mouse.click(300, 500);
  await sleep(400);
  out.t.maskClose = await page.evaluate(() => !document.getElementById("tbxDrawer").classList.contains("open"));
  out.checks.push({
    id: "8-mask",
    ok: out.t.maskClose === true,
    detail: "点遮罩后抽屉已关闭=" + out.t.maskClose + "（规格 §2.6 要求点遮罩能关）",
  });
  out.t.sizes = await page.evaluate(() => {
    const d = document.querySelector(".tbx-i");
    const x = document.querySelector("#tbxLane .tbx-nX");
    const card = document.querySelector(".tbx-card");
    const r1 = d && d.getBoundingClientRect(), r2 = x && x.getBoundingClientRect();
    const wrapped = [...document.querySelectorAll(".tbx-card")].filter((c) => {
      const n = c.querySelector(".tbx-cNum");
      const lh = parseFloat(getComputedStyle(n).lineHeight) || 18;
      return n.getBoundingClientRect().height / lh > 1.5;
    }).map((c) => (c.querySelector(".tbx-cName") || {}).textContent);
    return {
      dotI: r1 ? Math.round(r1.width) + "x" + Math.round(r1.height) : null,
      laneX: r2 ? Math.round(r2.width) + "x" + Math.round(r2.height) : null,
      card: card ? Math.round(card.getBoundingClientRect().width) + "x" + Math.round(card.getBoundingClientRect().height) : null,
      numWrappedCards: wrapped,
      docW: document.documentElement.scrollWidth, winW: window.innerWidth,
    };
  });

  /* 8b. 用新参数下这一刀，确认数字随新参数变（缓存作废重算） */
  out.t.paramStage = await clickCardAndWait(page, "box_bottom", null, 90000);
  out.checks.push({
    id: 8,
    ok: !out.t.paramStage.error && (out.t.paramStage.nums || []).join(",").indexOf("699") < 0,
    detail:
      "新参数一关 " + out.t.paramStage.ms + "ms，结果 " + JSON.stringify((out.t.paramStage || {}).nums) + " 文案：" + out.t.paramStage.text,
  });
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_06_param.png"), fullPage: true });

  // 撤掉这一刀，参数改回默认，避免影响后面的截图
  await page.evaluate(() => {
    const c = document.querySelector('[data-tbx-card="box_bottom"]');
    if (c) c.click();
  });
  await sleep(1200);
  await page.evaluate(() => {
    window.TB.overrides = {};
  });

  /* ---------- 10. 深/浅两套主题 ---------- */
  await clickCardAndWait(page, "box_bottom", 699, 30000);
  await sleep(400);
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_07_light.png"), fullPage: true });
  out.t.contrastLight = await page.evaluate(CONTRAST_FN);
  await page.evaluate(() => {
    const chip = document.querySelector('#themePick .chip[data-id="dark-dashboard"]');
    if (chip) chip.click();
  });
  await sleep(700);
  await page.screenshot({ path: path.join(LOGS, "qa_sieve_08_dark.png"), fullPage: true });
  out.t.contrastDark = await page.evaluate(CONTRAST_FN);

  /* ---------- 汇总 ---------- */
  out.errors = { pageErrors: pageErrs, consoleErrors: consoleErrs };
  out.netStackCount = netStack.length;
  out.netStackBad = netStack.filter((x) => x.status >= 400);
  out.final = await page.evaluate(() => ({
    pool: (document.getElementById("tbxPoolN") || {}).textContent,
    lane: (document.getElementById("tbxLane") || {}).textContent.replace(/\s+/g, " ").trim(),
    theme: document.getElementById("theme-link").getAttribute("href"),
  }));

  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch((e) => {
  out.fatal = String((e && e.stack) || e);
  console.log(JSON.stringify(out, null, 2));
  process.exit(1);
});
