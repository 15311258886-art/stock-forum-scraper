// Headless-Chromium probe for the HOLD/BUY split + 「仅看入围选手」toggle (2026-10-10).
//   node tools/_holdfix/ui_probe_holdfix.mjs
// Spawns an isolated viewer instance (new on-disk code) on HOLDFIX_PORT, drives the real page,
// saves screenshots + a DOM-text report, then kills the server. The live :8765 is untouched.
import { spawn } from "node:child_process";
import { existsSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const WS = "C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/";
const require = createRequire(path.join(WS, "index.js"));
const puppeteer = require("puppeteer-core");

const PORT = Number(process.env.HOLDFIX_PORT || 8793);
const BASE = `http://127.0.0.1:${PORT}`;
const OUT = HERE;
const log = [];
const say = (...a) => { const s = a.join(" "); console.log(s); log.push(s); };

function findChrome() {
  const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
  const cands = [];
  try {
    for (const d of readdirSync(base)) {
      if (d.startsWith("chromium-")) cands.push(path.join(base, d, "chrome-win64", "chrome.exe"));
    }
  } catch (e) { /* ignore */ }
  for (const c of cands) if (existsSync(c)) return c;
  throw new Error("chromium not found in " + base);
}

async function waitServer(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return true;
    } catch (e) { /* retry */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error("server did not come up: " + url);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function rowsInfo(page) {
  return page.$$eval("#p-consensus tbody tr", (trs) =>
    trs.slice(0, 6).map((tr) => {
      const tds = tr.querySelectorAll("td");
      return {
        stock: tds[0] ? tds[0].innerText.replace(/\s+/g, " ").trim() : "",
        split: tds[1] ? tds[1].innerText.replace(/\s+/g, " ").trim() : "",
        weighted: tds[2] ? tds[2].innerText.replace(/\s+/g, " ").trim() : "",
      };
    }));
}

async function main() {
  const env = { ...process.env, HOLDFIX_PORT: String(PORT) };
  const srv = spawn(path.join(ROOT, ".venv", "Scripts", "python.exe"),
    [path.join("tools", "_holdfix", "serve_bg.py")], { cwd: ROOT, env, stdio: "ignore" });
  let browser;
  try {
    await waitServer(BASE + "/api/consensus?min_holders=2");
    say("server up on " + BASE);

    browser = await puppeteer.launch({
      executablePath: findChrome(),
      headless: "new",
      args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1200"],
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1200 });
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 60000 });

    await page.evaluate(() => window.showTab && window.showTab("consensus"));
    await page.waitForFunction(
      () => document.querySelectorAll("#p-consensus tbody tr").length > 0,
      { timeout: 30000 });

    const hdr = await page.$eval("#p-consensus table thead tr", (tr) => tr.innerText.replace(/\s+/g, " ").trim());
    say("CONSENSUS header: " + hdr);

    const hasToggle = await page.$("#p-consensus .q-toggle input");
    say("CONSENSUS toggle present: " + !!hasToggle);

    const splitBefore = await rowsInfo(page);
    say("CONSENSUS rows BEFORE toggle (count=" +
      (await page.$$eval("#p-consensus tbody tr", (t) => t.length)) + "):");
    for (const r of splitBefore) say("   " + JSON.stringify(r));
    await page.screenshot({ path: path.join(OUT, "shot_consensus_all.png"), fullPage: false });

    // Toggle 「仅看入围选手(Tier A)」
    await page.click("#p-consensus .q-toggle input");
    await sleep(1500);
    await page.waitForFunction(
      () => document.querySelector("#p-consensus .q-toggle input") &&
            document.querySelector("#p-consensus .q-toggle input").checked,
      { timeout: 10000 });
    const splitAfter = await rowsInfo(page);
    say("CONSENSUS rows AFTER toggle (only Tier A, count=" +
      (await page.$$eval("#p-consensus tbody tr", (t) => t.length)) + "):");
    for (const r of splitAfter) say("   " + JSON.stringify(r));
    await page.screenshot({ path: path.join(OUT, "shot_consensus_tierA.png"), fullPage: false });

    // ---- cohold panel ----
    await page.evaluate(() => window.showTab && window.showTab("cohold"));
    await page.waitForFunction(
      () => document.querySelectorAll("#p-cohold h3.coh-h").length >= 3,
      { timeout: 20000 });
    const heads = await page.$$eval("#p-cohold h3.coh-h", (hs) => hs.map((h) => h.innerText.replace(/\s+/g, " ").trim()));
    say("COHOLD group headings: " + JSON.stringify(heads));
    const holdRows = await page.$$eval("#p-cohold table", (tables) =>
      tables.map((t) => t.querySelectorAll("tbody tr").length));
    say("COHOLD table row counts (buy/sell/hold): " + JSON.stringify(holdRows));
    const coholdToggle = await page.$("#p-cohold .q-toggle input");
    say("COHOLD toggle present: " + !!coholdToggle);
    await page.screenshot({ path: path.join(OUT, "shot_cohold.png"), fullPage: false });

    // 单独截「共同持有(HOLD)」组，坐实 HOLD 已独立成栏
    await page.evaluate(() => {
      const h = [...document.querySelectorAll("#p-cohold h3.coh-h")]
        .find((x) => x.innerText.includes("共同持有"));
      if (h) h.scrollIntoView({ block: "start" });
    });
    await sleep(600);
    const holdFirst = await page.evaluate(() => {
      const h = [...document.querySelectorAll("#p-cohold h3.coh-h")]
        .find((x) => x.innerText.includes("共同持有"));
      const tbl = h && h.nextElementSibling;
      const tr = tbl && tbl.querySelector("tbody tr");
      return tr ? tr.innerText.replace(/\s+/g, " ").trim().slice(0, 200) : "(none)";
    });
    say("COHOLD 共同持有 first row: " + holdFirst);
    await page.screenshot({ path: path.join(OUT, "shot_cohold_hold.png"), fullPage: false });

    say("OK");
  } finally {
    try { if (browser) await browser.close(); } catch (e) { /* ignore */ }
    try { srv.kill(); } catch (e) { /* ignore */ }
  }
  writeFileSync(path.join(OUT, "ui_probe_report.txt"), log.join("\n"), "utf-8");
}

main().catch((e) => {
  log.push("ERROR: " + (e && e.stack || e));
  try { writeFileSync(path.join(OUT, "ui_probe_report.txt"), log.join("\n"), "utf-8"); } catch (_) {}
  console.error(e);
  process.exit(1);
});
