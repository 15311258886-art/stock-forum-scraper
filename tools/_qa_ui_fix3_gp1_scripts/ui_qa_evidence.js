/* =============================================================================
 * ui_qa_evidence.js —— 改后证据采集（只读，真实 Chromium）
 * 采集：① 共识「持仓选手明细」是否还有空角标（C4）
 *       ② 共识「覆盖渠道」列缺失时的占位（C6）
 *       ③「真实」角标实渲配色（应为 .chip-real：弱绿实底 + 实线框）
 *       ④「示例」角标对照：CSSOM 声明值 + 页面内注入同结构元素取计算样式
 *          （截至本次复核库中已无 example.local 出处，线上不再渲染真实「示例」角标，
 *            故第 ④ 项仅做样式层对照，不写入任何数据）
 *       ⑤ 四套主题下「真实」chip 的前景/底色、以及 chip 底 vs 行底对比度
 * 用法：node ui_qa_evidence.js [port] [输出目录]
 * ========================================================================== */
"use strict";
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8765";
const BASE = `http://127.0.0.1:${PORT}`;
const SHOT = process.argv[3] || path.join(__dirname, "shots");
if (!fs.existsSync(SHOT)) fs.mkdirSync(SHOT, { recursive: true });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function gotoPanel(page, cat, tab, ms) {
  await page.evaluate((c) => document.querySelector(`#sidenav .nav-item[data-cat="${c}"]`).click(), cat);
  await wait(350);
  await page.evaluate((t) => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${t}"]`); if (a) a.click(); }, tab);
  await wait(ms || 1600);
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });
  await page.evaluateOnNewDocument(() => { window.confirm = () => false; });
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    const u = req.url();
    if (/\/api\/(demo|overseas|fetch)\b/.test(u)) return req.abort().catch(() => {});
    req.continue().catch(() => {});
  });
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
  const consoleErrs = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrs.push("[console] " + m.text()); });
  page.on("pageerror", (e) => consoleErrs.push("[pageerror] " + e.message));

  await page.goto(BASE, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);

  /* ---------- ① ② 共识面板 ---------- */
  await gotoPanel(page, "trade", "consensus", 1800);
  const badgeInfo = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll("#p-consensus .chip-row").forEach((row) => {
      const b = row.querySelector(".badge");
      out.push({
        hasBadge: !!b,
        text: b ? (b.textContent || "").trim() : null,
        cls: b ? b.className : null,
        box: b ? (() => { const r = b.getBoundingClientRect(); return Math.round(r.width * 10) / 10 + "x" + Math.round(r.height * 10) / 10; })() : null,
        rowText: (row.innerText || "").replace(/\s+/g, " ").trim().slice(0, 42),
      });
    });
    return out;
  });
  const emptyBadges = badgeInfo.filter((b) => b.hasBadge && b.text === "");
  const channelCells = await page.evaluate(() => {
    const w = (n) => Math.round(n * 10) / 10;
    return [...document.querySelectorAll("#p-consensus tbody tr")].map((tr) => {
      const td = tr.querySelectorAll("td")[4];
      if (!td) return { sym: tr.dataset.sym, text: null };
      const r = td.getBoundingClientRect();
      return { sym: tr.dataset.sym, text: (td.innerText || "").trim(), innerHTML: td.innerHTML.trim(),
        w: w(r.width), h: w(r.height), blank: (td.innerText || "").trim() === "" };
    });
  });

  const panel = await page.$("#p-consensus");
  if (panel) await panel.screenshot({ path: path.join(SHOT, "consensus-after.png") }).catch(() => {});
  const chClip = await page.evaluate(() => {
    const els = [...document.querySelectorAll("#p-consensus thead th:nth-child(5), #p-consensus tbody td:nth-child(5)")];
    if (!els.length) return null;
    let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
    els.forEach((e) => { const x = e.getBoundingClientRect(); l = Math.min(l, x.left); t = Math.min(t, x.top); r = Math.max(r, x.right); b = Math.max(b, x.bottom); });
    return { x: Math.max(0, l - 8), y: Math.max(0, t - 8), width: Math.min(1440, r - l + 16), height: Math.min(900, b - t + 16) };
  });
  if (chClip && chClip.height > 4) await page.screenshot({ path: path.join(SHOT, "evidence-channel-col.png"), clip: chClip }).catch(() => {});
  const plClip = await page.evaluate(() => {
    const els = [...document.querySelectorAll("#p-consensus tbody td:nth-child(7)")];
    if (!els.length) return null;
    let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
    els.forEach((e) => { const x = e.getBoundingClientRect(); l = Math.min(l, x.left); t = Math.min(t, x.top); r = Math.max(r, x.right); b = Math.max(b, x.bottom); });
    return { x: Math.max(0, l - 8), y: Math.max(0, t - 8), width: Math.min(1440, r - l + 16), height: Math.min(900, b - t + 16) };
  });
  if (plClip && plClip.height > 4) await page.screenshot({ path: path.join(SHOT, "evidence-empty-badge.png"), clip: plClip }).catch(() => {});

  /* ---------- ③ 「真实」角标（operating 面板） ---------- */
  await gotoPanel(page, "trade", "operating", 1800);
  const realChip = await page.evaluate(() => {
    const read = (e) => {
      if (!e) return null;
      const cs = getComputedStyle(e), r = e.getBoundingClientRect();
      return { text: (e.textContent || "").trim(), cls: e.className,
        bg: cs.backgroundColor, color: cs.color,
        border: cs.borderTopWidth + " " + cs.borderTopStyle + " " + cs.borderTopColor,
        radius: cs.borderTopLeftRadius, fontSize: cs.fontSize,
        w: Math.round(r.width), h: Math.round(r.height) };
    };
    const real = [...document.querySelectorAll("#p-operating .chip-s.chip-real")];
    const demo = [...document.querySelectorAll("#p-operating .chip-s.chip-demo")];
    return { realCount: real.length, demoCount: demo.length, realSample: read(real[0]), demoSample: read(demo[0]),
      realTexts: [...new Set(real.map((e) => (e.textContent || "").trim()))].slice(0, 5) };
  });
  const firstCell = await page.$("#p-operating tbody tr td:nth-child(1)");
  if (firstCell) await firstCell.screenshot({ path: path.join(SHOT, "evidence-real-chip.png") }).catch(() => {});

  /* ---------- ④ 示例/真实 样式对照（CSSOM + 同结构元素注入，仅样式验证） ---------- */
  const chipStyleCompare = await page.evaluate(() => {
    // 页面内是否真的还有「示例」角标（当前数据全为真实出处 → 预期 0）
    const demoLiveCount = document.querySelectorAll(".chip-demo").length;
    const declared = { demo: [], real: [] };
    for (const ss of document.styleSheets) {
      let rules = null; try { rules = ss.cssRules; } catch (e) { continue; }
      if (!rules) continue;
      for (const r of rules) {
        if (!r.selectorText) continue;
        const sels = r.selectorText.split(",").map((s) => s.trim());
        if (sels.includes(".chip-demo")) declared.demo.push(r.cssText.replace(/\s+/g, " ").slice(0, 220));
        if (sels.includes(".chip-real")) declared.real.push(r.cssText.replace(/\s+/g, " ").slice(0, 220));
      }
    }
    // 注入两个仅用于取色的同结构元素（不入库、不改任何业务状态）
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;left:-9999px;top:0";
    host.innerHTML = '<span id="fxDemo" class="chip-s chip-demo">示例</span><span id="fxReal" class="chip-s chip-real">真实</span>';
    document.body.appendChild(host);
    const read = (id) => {
      const e = document.getElementById(id), c = getComputedStyle(e), r = e.getBoundingClientRect();
      return { bg: c.backgroundColor, color: c.color,
        border: c.borderTopWidth + " " + c.borderTopStyle + " " + c.borderTopColor,
        w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 };
    };
    const computed = { demo: read("fxDemo"), real: read("fxReal") };
    host.remove();
    return { demoLiveCount, declared, computed, note: "注入元素仅用于读取 .chip-demo / .chip-real 的计算样式，不写库" };
  });

  /* ---------- ⑤ 四套主题下的「真实」chip 对比度 ---------- */
  const chipTheme = [];
  for (const tid of ["light-tech", "dark-dashboard", "minimal-report", "card-warm"]) {
    await page.evaluate((id) => { const c = document.querySelector(`#themePick .chip[data-id="${id}"]`); if (c) c.click(); }, tid);
    await wait(500);
    await gotoPanel(page, "trade", "operating", 1400);
    const d = await page.evaluate(() => {
      const el = document.querySelector("#p-operating .chip-s.chip-real");
      if (!el) return null;
      const cs = getComputedStyle(el);
      const lum = (rgb) => { const m = rgb.match(/\d+(\.\d+)?/g); if (!m) return null;
        const [r, g, b] = m.slice(0, 3).map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
      const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b); if (l1 == null || l2 == null) return null;
        const hi = Math.max(l1, l2), lo = Math.min(l1, l2); return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100; };
      const bgOf = (e) => { let n = e.parentElement;
        while (n && n !== document.documentElement) { const c = getComputedStyle(n).backgroundColor; const m = c.match(/rgba?\(([^)]+)\)/);
          if (m) { const p = m[1].split(","); const a = p.length > 3 ? parseFloat(p[3]) : 1; if (a > 0.1) return c; } n = n.parentElement; }
        return getComputedStyle(document.body).backgroundColor; };
      const own = cs.backgroundColor, tx = cs.color, rowBg = bgOf(el);
      return { bodyBg: getComputedStyle(document.body).backgroundColor, rowBg, chipBg: own, chipColor: tx,
        textOnChip: ratio(tx, own), chipOnRow: ratio(own, rowBg),
        border: cs.borderTopWidth + " " + cs.borderTopStyle + " " + cs.borderTopColor };
    });
    const clip = await page.evaluate(() => {
      const e = document.querySelector("#p-operating tbody tr td:nth-child(1)");
      if (!e) return null;
      const r = e.getBoundingClientRect();
      return { x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.min(1440, r.width), height: Math.min(900, r.height) };
    });
    if (clip && clip.width > 4) await page.screenshot({ path: path.join(SHOT, `chip-real-${tid}.png`), clip }).catch(() => {});
    chipTheme.push({ theme: tid, ...(d || {}) });
    await page.evaluate(() => { const c = document.querySelector('#themePick .chip[data-id="light-tech"]'); if (c) c.click(); });
    await wait(300);
  }
  await page.evaluate(() => { const c = document.querySelector('#themePick .chip[data-id="light-tech"]'); if (c) c.click(); });
  await wait(400);

  const report = {
    ts: new Date().toISOString(), port: PORT,
    emptyBadgeCount: emptyBadges.length,
    playerRowCount: badgeInfo.length,
    playerRowsWithBadge: badgeInfo.filter((b) => b.hasBadge).length,
    emptyBadgeSample: emptyBadges.slice(0, 3),
    badgeRowsSample: badgeInfo.slice(0, 5),
    channelCells,
    realChip,
    chipStyleCompare,
    chipTheme,
    consoleErrors: consoleErrs,
  };
  fs.writeFileSync(path.join(__dirname, "evidence_after.json"), JSON.stringify(report, null, 2), "utf-8");
  fs.writeFileSync(path.join(SHOT, "evidence_after.json"), JSON.stringify(report, null, 2), "utf-8");

  console.log("空角标 = " + emptyBadges.length + " / 明细行数 = " + badgeInfo.length + "（其中带角标行 " + report.playerRowsWithBadge + "）");
  console.log("覆盖渠道列取值 = " + JSON.stringify(channelCells.map((c) => c.sym + ":" + c.text)));
  console.log("真实角标 = " + JSON.stringify(realChip));
  console.log("示例/真实 样式对照 = " + JSON.stringify(chipStyleCompare));
  console.log("四主题 = " + JSON.stringify(chipTheme));
  console.log("consoleErrors = " + consoleErrs.length + (consoleErrs.length ? " :: " + consoleErrs.join(" ;; ") : ""));
  await browser.close();
})().catch((e) => { console.log("ERR " + (e && e.stack || e)); process.exit(2); });
