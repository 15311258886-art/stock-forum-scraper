/* 复核补充：① 冷上下文首点复测（3 次独立浏览器）② 四套主题工具墙特写
 * 用法：node tools/qa_r3_extra.cjs
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { coldClick: [], walls: {} };

async function boot() {
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: "new", protocolTimeout: 600000,
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1400"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String((e && e.message) || e)));
  const t0 = Date.now();
  await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask"); if (m) m.style.display = "none";
    const f = document.querySelector("#firstRunModal"); if (f) f.style.display = "none";
  });
  await page.waitForFunction(() => {
    const tb = window.TB; if (!tb || !tb.counted) return false;
    const ks = Object.keys(tb.counted);
    return ks.length > 5 && ks.every((k) => tb.counted[k] !== null && tb.counted[k] !== undefined);
  }, { timeout: 60000, polling: 100 });
  return { browser, page, errs, bootMs: Date.now() - t0 };
}

(async () => {
  /* ---- ① 冷上下文首点 ×3（每次全新 profile） ---- */
  for (let i = 0; i < 3; i++) {
    const s = await boot();
    const r = await s.page.evaluate(async () => {
      const t0 = performance.now();
      const before = performance.getEntriesByType("resource").length;
      document.querySelector('[data-tbx-card="box_bottom"]').click();
      const lane = () => document.getElementById("tbxLane");
      while (performance.now() - t0 < 90000) {
        const L = lane();
        const txt = L ? L.textContent || "" : "";
        const nums = [...L.querySelectorAll(".tbx-node .tbx-nN")].map((x) => x.textContent.trim());
        if (L && txt.indexOf("正在算") < 0 && txt.indexOf("…") < 0 && L.querySelector(".tbx-node.out") &&
            nums.some((n) => n.replace(/,/g, "") === "699")) {
          const api = performance.getEntriesByType("resource").slice(before)
            .filter((e) => e.name.indexOf("/api/toolbox/stack") >= 0).map((e) => Math.round(e.duration));
          return { ms: Math.round(performance.now() - t0), api, nums, ok: true };
        }
        await new Promise((r) => setTimeout(r, 10));
      }
      return { ms: Math.round(performance.now() - t0), ok: false };
    });
    out.coldClick.push(Object.assign({ trial: i + 1, bootMs: s.bootMs, pageErrors: s.errs }, r));
    if (i === 0) await s.page.screenshot({ path: path.join(LOGS, "qa_r3_11_cold_first_click.png") });
    await s.browser.close();
    await sleep(600);
  }

  /* ---- ② 四套主题的工具墙特写（选中卡 + 已淘汰卡 + 会清空卡同框） ---- */
  const s = await boot();
  await s.page.evaluate(() => {
    document.querySelector('[data-tbx-card="box_bottom"]').click();
  });
  await s.page.waitForFunction(() => {
    const L = document.getElementById("tbxLane");
    return L && [...L.querySelectorAll(".tbx-node .tbx-nN")].some((x) => x.textContent.trim() === "699");
  }, { timeout: 60000, polling: 50 });
  await sleep(800);
  const themes = ["light-tech", "dark-dashboard", "minimal-report", "card-warm"];
  for (const id of themes) {
    await s.page.evaluate((id) => {
      const c = document.querySelector('#themePick .chip[data-id="' + id + '"]');
      if (c) c.click();
    }, id);
    await sleep(900);
    const el = await s.page.$("#tbxWall") || await s.page.$(".tbx-wall");
    if (el) {
      const f = path.join(LOGS, "qa_r3_12wall_" + id + ".png");
      await el.screenshot({ path: f });
      out.walls[id] = { file: path.basename(f), ok: true };
    } else {
      out.walls[id] = { ok: false };
    }
    // 卡片上的字色/边框色，逐套记账
    out.walls[id] = Object.assign(out.walls[id] || {}, await s.page.evaluate(() => {
      const pick = (sel) => {
        const e = document.querySelector(sel);
        if (!e) return null;
        return { text: (e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 18),
                 color: getComputedStyle(e).color, border: getComputedStyle(e).borderTopColor };
      };
      return {
        on: pick(".tbx-card.on"), onName: pick(".tbx-card.on .tbx-cName"),
        dead: pick(".tbx-card.dead"), danger: pick(".tbx-card.danger"),
        dangerHell: pick(".tbx-card.danger .tbx-hell"),
        accentDot: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
      };
    }));
  }
  await s.browser.close();
  fs.writeFileSync(path.join(LOGS, "qa_r3_extra.json"), JSON.stringify(out, null, 2), "utf-8");
  console.log(JSON.stringify(out, null, 2));
})().catch((e) => {
  out.fatal = String((e && e.stack) || e);
  try { fs.writeFileSync(path.join(LOGS, "qa_r3_extra.json"), JSON.stringify(out, null, 2), "utf-8"); } catch (_) {}
  console.log(JSON.stringify(out, null, 2));
  process.exit(1);
});
