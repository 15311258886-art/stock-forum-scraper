// FAIL-D headless confirmation + regression sweep (2026-10-10).
//   node tools/_holdfix/ui_probe_bots.mjs
// 1) consensus & cohold DOM must contain NO bot account names (default + Tier A toggle).
// 2) other main panels (今日总览/集合竞价 · 每日预测 · 博主荐股 · 个股快讯) render with zero JS errors.
// Isolated instance runs the current on-disk code; live :8765 untouched.
import { spawn } from "node:child_process";
import { existsSync, readdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const WS = "C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/";
const require = createRequire(path.join(WS, "index.js"));
const puppeteer = require("puppeteer-core");

const PORT = Number(process.env.HOLDFIX_PORT || 8797);
const BASE = `http://127.0.0.1:${PORT}`;
const log = [];
const say = (...a) => { const s = a.join(" "); console.log(s); log.push(s); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Bot names the QA flagged + the 8 confirmed list.
const BOT_NAMES = ["平安银行资讯", "宁德时代资讯", "中国平安资讯", "中信证券资讯",
  "工业富联资讯", "贵州茅台资讯", "新浪财经客户端", "新浪财经客", "Sina财经产", "神光财经",
  "新浪财经芝", "看财经官博", "汽车营销分", "机工战略", "EC品牌观察"];

function findChrome() {
  const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
  for (const d of readdirSync(base)) {
    if (d.startsWith("chromium-")) {
      const c = path.join(base, d, "chrome-win64", "chrome.exe");
      if (existsSync(c)) return c;
    }
  }
  throw new Error("chromium not found");
}
async function waitServer(url, tries = 80) {
  for (let i = 0; i < tries; i++) {
    try { const r = await fetch(url); if (r.ok) return true; } catch (e) { /* retry */ }
    await sleep(300);
  }
  throw new Error("server not up: " + url);
}

async function main() {
  const env = { ...process.env, HOLDFIX_PORT: String(PORT) };
  const srv = spawn(path.join(ROOT, ".venv", "Scripts", "python.exe"),
    [path.join("tools", "_holdfix", "serve_bg.py")], { cwd: ROOT, env, stdio: "ignore" });
  let browser;
  try {
    await waitServer(BASE + "/api/consensus?min_holders=2");
    browser = await puppeteer.launch({ executablePath: findChrome(), headless: "new",
      args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1200"] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1200 });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text().slice(0, 160)); });

    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 60000 });
    await sleep(2500);

    async function botIn(scopeSel) {
      return page.evaluate((sel, names) => {
        const el = document.querySelector(sel);
        if (!el) return { missing: true, hits: [], len: 0, suspicious: [] };
        const t = el.innerText || "";
        // exact-token match (avoid substring false positives like 新浪财经客户端 ⊃ 新浪财经客)
        const toks = new Set(t.split(/[\s·,，、|()（）]+/).filter(Boolean));
        const hits = names.filter((n) => toks.has(n));
        const suspicious = [...toks].filter((x) => /资讯|播报|快讯$/.test(x) || x.includes("新浪财经"));
        return { missing: false, hits, len: t.length, suspicious };
      }, scopeSel, BOT_NAMES);
    }

    // ---- consensus (default) ----
    await page.evaluate(() => window.showTab && window.showTab("consensus"));
    await page.waitForFunction(() => document.querySelectorAll("#p-consensus tbody tr").length > 0, { timeout: 30000 });
    let r = await botIn("#p-consensus");
    say(`CONSENSUS default: bot hits = ${JSON.stringify(r.hits)} (panel len ${r.len}) suspicious=${JSON.stringify(r.suspicious)}`);

    // ---- consensus Tier A ----
    await page.click("#p-consensus .q-toggle input");
    await sleep(1600);
    r = await botIn("#p-consensus");
    say(`CONSENSUS TierA  : bot hits = ${JSON.stringify(r.hits)} (panel len ${r.len}) suspicious=${JSON.stringify(r.suspicious)}`);

    // ---- cohold ----
    await page.evaluate(() => window.showTab && window.showTab("cohold"));
    await page.waitForFunction(() => document.querySelectorAll("#p-cohold h3.coh-h").length >= 3, { timeout: 20000 });
    r = await botIn("#p-cohold");
    say(`COHOLD           : bot hits = ${JSON.stringify(r.hits)} (panel len ${r.len}) suspicious=${JSON.stringify(r.suspicious)}`);
    await page.screenshot({ path: path.join(HERE, "shot_cohold_nobots.png") });

    // ---- regression sweep: other main panels ----
    const sweep = [
      ["dashboard", "#p-dashboard", "集合竞价"],
      ["prediction", "#p-prediction", null],
      ["blogger", "#p-blogger", null],
      ["news", "#p-news", null],
    ];
    for (const [tab, sel, mustText] of sweep) {
      await page.evaluate((t) => window.showTab && window.showTab(t), tab);
      await sleep(2000);
      const info = await page.evaluate((s) => {
        const el = document.querySelector(s);
        const t = el ? (el.innerText || "") : "";
        return { exists: !!el, len: t.length, head: t.slice(0, 60).replace(/\s+/g, " ") };
      }, sel);
      const has = mustText ? info.head.includes(mustText) : null;
      say(`PANEL ${tab.padEnd(10)} ${sel.padEnd(14)} exists=${info.exists} len=${info.len}` +
        (mustText ? ` contains("${mustText}")=${has}` : "") + ` head="${info.head}"`);
    }

    say("");
    say("JS errors during load + tab switches: " + errors.length);
    errors.slice(0, 10).forEach((e) => say("   " + e));
    say("VERDICT bots-in-DOM: " + (log.filter((l) => l.startsWith("CONSENSUS") || l.startsWith("COHOLD"))
      .every((l) => l.includes("[]")) ? "NONE (PASS)" : "FOUND (FAIL)"));
    say("VERDICT js-errors: " + (errors.length === 0 ? "ZERO (PASS)" : "PRESENT (check)"));
  } finally {
    try { if (browser) await browser.close(); } catch (e) { /* ignore */ }
    try { srv.kill(); } catch (e) { /* ignore */ }
  }
  writeFileSync(path.join(HERE, "ui_probe_bots_report.txt"), log.join("\n"), "utf-8");
}
main().catch((e) => {
  log.push("ERROR: " + (e && e.stack || e));
  try { writeFileSync(path.join(HERE, "ui_probe_bots_report.txt"), log.join("\n"), "utf-8"); } catch (_) {}
  console.error(e); process.exit(1);
});
