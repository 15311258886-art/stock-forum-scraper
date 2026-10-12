const puppeteer = require("puppeteer-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8765";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: "new", args: ["--no-sandbox"] });
  const p = await b.newPage();
  p.on("pageerror", (e) => console.log("PAGEERROR:", e.message));
  p.on("console", (m) => { if (m.type() === "error") console.log("CONSOLE-ERR:", m.text()); });
  p.on("requestfailed", (r) => console.log("REQFAIL:", r.url().slice(0, 120), r.failure() && r.failure().errorText));

  await p.goto(BASE + "/#watch/rate", { waitUntil: "domcontentloaded" });
  await sleep(1500);
  await p.evaluate(() => { const n = document.querySelector('.nav-item[data-cat="watch"]'); if (n) n.click(); });
  await sleep(400);
  await p.evaluate(() => { const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]'); if (t) t.click(); });

  for (const s of [3, 8, 15, 25]) {
    await sleep(s === 3 ? 3000 : 5000);
    const st = await p.evaluate(() => {
      const f = (window.FEAT && window.FEAT.rate) || null;
      return {
        hasFEAT: !!f,
        loading: f && f.loading, error: f && f.error,
        dataOk: f && f.data && f.data.ok, rows: f && f.data && (f.data.rows || []).length,
        snap: f && f.snap, tlLoading: f && f.tlLoading, tlHas: f && f.tl && f.tl.has_snap,
        bodyTxt: (document.querySelector("#p-rate .wt-empty") || {}).innerText || "",
      };
    });
    console.log(`@${s}s`, JSON.stringify(st));
  }

  // 直接打接口计时
  const api = await p.evaluate(async () => {
    const t0 = performance.now();
    try {
      const r = await fetch("/api/screener_turnover_top?days=30&limit=30");
      const j = await r.json();
      return { ms: Math.round(performance.now() - t0), ok: j.ok, rows: (j.rows || []).length, reason: j.reason };
    } catch (e) { return { ms: Math.round(performance.now() - t0), err: String(e) }; }
  });
  console.log("direct API:", JSON.stringify(api));

  await b.close();
})();
