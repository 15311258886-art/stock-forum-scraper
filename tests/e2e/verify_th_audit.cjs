/* 表头口径审计（2026-10-30）
   目的：进哥要求"表头细节不一致"一律抹平。先测量、后改，不靠猜。
   采集：逐面板遍历所有 thead th / .dtb-hd，读 computed font-size / background-color /
        color / font-weight / padding / text-align，按 (字号+底色+字重) 聚类，
        并列出每个面板的具体取值 → 输出 _th_audit.json
   用法：node verify_th_audit.cjs
*/
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8765/?skipguide=1";
const OUT = __dirname;

const scanFn = () => {
  const out = [];
  const visible = (el) => el.offsetParent !== null || getComputedStyle(el).position === "fixed";
  document.querySelectorAll(".panel").forEach((panel) => {
    if (!visible(panel)) return;
    const heads = [];
    // 传统 table 表头
    panel.querySelectorAll("table").forEach((t) => {
      const ths = Array.prototype.slice.call(t.querySelectorAll(":scope > thead > tr > th"));
      if (!ths.length) return;
      const th = ths[0];
      const cs = getComputedStyle(th);
      const bgEl = th;
      heads.push({
        kind: "table",
        tblCls: (t.className || "").toString().slice(0, 48),
        cols: ths.length,
        fontSize: cs.fontSize,
        background: getComputedStyle(bgEl).backgroundColor,
        trBg: getComputedStyle(th.parentElement).backgroundColor,
        color: cs.color,
        fontWeight: cs.fontWeight,
        padding: cs.padding,
        textAlign: cs.textAlign,
        sample: ths.slice(0, 4).map((x) => (x.textContent || "").trim().slice(0, 8)).join("|"),
      });
    });
    // .dtb 组件表头
    panel.querySelectorAll(".dtb-hd").forEach((hd) => {
      const cs = getComputedStyle(hd);
      heads.push({
        kind: "dtb",
        tblCls: "dtb",
        cols: hd.children.length,
        fontSize: cs.fontSize,
        background: cs.backgroundColor,
        trBg: cs.backgroundColor,
        color: cs.color,
        fontWeight: cs.fontWeight,
        padding: cs.padding,
        textAlign: cs.textAlign,
        sample: Array.prototype.slice.call(hd.children, 0, 4).map((x) => (x.textContent || "").trim().slice(0, 8)).join("|"),
      });
    });
    if (heads.length) out.push({ panel: panel.id, heads });
  });
  return out;
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--proxy-bypass-list=*"],
  });
  const res = { ts: new Date().toISOString(), pages: [], errors: [] };
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 980 });
  page.on("pageerror", (e) => res.errors.push("pageerror: " + String(e).slice(0, 160)));
  page.on("console", (m) => { if (m.type() === "error") res.errors.push("console: " + m.text().slice(0, 160)); });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction(() => typeof selectNav === "function", { timeout: 40000 });

  const nav = await page.evaluate(() =>
    NAV.map((c) => ({ key: c.key, label: c.label, items: (c.items || []).map((i) => ({ tab: i.tab, label: i.label })) }))
  );
  const total = nav.reduce((n, c) => n + c.items.length, 0);
  let done = 0;
  for (const cat of nav) {
    for (const it of cat.items) {
      try {
        await page.evaluate((k, t) => selectNav(k, t, false), cat.key, it.tab);
      } catch (e) { res.errors.push("nav " + it.tab + ": " + String(e).slice(0, 100)); continue; }
      await new Promise((r) => setTimeout(r, 1200));
      try {
        const r = await page.evaluate(scanFn);
        r.forEach((p) => res.pages.push({ tab: it.tab, cat: cat.label, panel: p.panel, heads: p.heads }));
      } catch (e) { res.errors.push("scan " + it.tab + ": " + String(e).slice(0, 100)); }
      done++;
    }
  }
  res.scanned = done; res.total = total;

  // 聚类：字号 | 底色 | 字重
  const tally = {};
  res.pages.forEach((p) => p.heads.forEach((h) => {
    const k = h.fontSize + " | bg=" + h.background + " | w=" + h.fontWeight;
    (tally[k] = tally[k] || []).push(p.panel + "." + h.tblCls + "(" + h.cols + "c)");
  }));
  res.tally = Object.keys(tally).sort((a, b) => tally[b].length - tally[a].length)
    .map((k) => ({ key: k, n: tally[k].length, where: tally[k].slice(0, 8) }));

  fs.writeFileSync(path.join(OUT, "_th_audit.json"), JSON.stringify(res, null, 2), "utf8");
  console.log("扫了 " + done + "/" + total + " 个面板，表头样本 " +
    res.pages.reduce((n, p) => n + p.heads.length, 0) + " 个\n");
  console.log("=== 口径聚类（按出现次数降序）===");
  res.tally.forEach((t) => console.log(String(t.n).padStart(3) + "  " + t.key + "\n      " + t.where.join(", ")));
  console.log("\n=== 控制台错误 " + res.errors.length + " ===");
  res.errors.slice(0, 10).forEach((e) => console.log("  " + e));
  await browser.close();
})();
