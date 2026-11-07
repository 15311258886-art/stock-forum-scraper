/* verify_after_purge.cjs —— 荐股内容物理删除后的回归（2026-10-29）
 *
 * 重点验「删了之后哪里会坏」，而不是重跑旧基线：
 *   ① 页面加载无真实 JS 错误（CSP frame-ancestors 是既有噪音，过滤）；
 *   ② 三个已删页面确实不在导航里，也**没有残留面板容器**；
 *   ③ 个股抽屉能正常打开 —— 这是最可能出事的地方（原 initStockBloggerPerf 的挂载点已删，
 *      若模板里还留着 #stkBloggerPerf 或还调那一句，点开任何股票都会抛错）；
 *   ④ 关注池「博主提及」列的兜底：blogger_overlap 仍在下游用，列表不能因此空掉；
 *   ⑤ 板块热度页仍有数据。
 *
 * 退出码：0=PASS，2=FAIL，3=无浏览器
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const BASE = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

function pickBrowser() {
  const cands = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ];
  try {
    const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
    if (fs.existsSync(base)) {
      fs.readdirSync(base).filter((d) => /^chromium-/.test(d)).sort().reverse()
        .forEach((d) => cands.push(base + "/" + d + "/chrome-win64/chrome.exe"));
    }
  } catch (e) { /* 忽略 */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

const fails = [];
function check(cond, msg) { if (!cond) fails.push(msg); console.log((cond ? "  PASS " : "  FAIL ") + msg); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER"); process.exit(3); }
  const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1100"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1100 });
  const errors = [];
  const NOISE = /frame-ancestors|Content Security Policy/i;
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !NOISE.test(m.text())) errors.push("console.error: " + m.text());
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.evaluate(() => { try { localStorage.setItem("viewer-first-run-done", "1"); } catch (e) {} });
  await page.goto(BASE + "#money/plate_heat", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(7000);
  await page.evaluate(() => {
    const m = document.getElementById("firstRunModal"); if (m) m.hidden = true;
    const k = document.getElementById("firstRunMask"); if (k) k.classList.remove("show");
  });

  console.log("\n[1] 加载与 JS 错误");
  check(errors.length === 0, "无真实 JS 错误" + (errors.length ? " —— " + errors.slice(0, 3).join(" | ") : ""));

  console.log("\n[2] 已删页面不再存在");
  const gone = await page.evaluate(() => {
    const tabs = [...document.querySelectorAll("[data-tab]")].map((x) => x.dataset.tab);
    return {
      inNav: ["blogger", "contest", "extref_soft"].filter((t) => tabs.includes(t)),
      containers: ["p-blogger", "p-contest", "p-extref_soft", "p-extref"]
        .filter((id) => document.getElementById(id)),
      globals: ["loadExtref", "renderBloggerPanel", "renderContestPanel",
        "initStockBloggerPerf", "btSparkSvg"].filter((f) => typeof window[f] === "function"),
    };
  });
  check(gone.inNav.length === 0, "导航中无 blogger/contest/extref_soft（残留：" + (gone.inNav.join(",") || "无") + "）");
  check(gone.containers.length === 0, "无残留面板容器（残留：" + (gone.containers.join(",") || "无") + "）");
  check(gone.globals.length === 0, "已删函数未泄漏为全局（残留：" + (gone.globals.join(",") || "无") + "）");

  console.log("\n[3] 板块热度");
  const ph = await page.evaluate(() => {
    const p = document.querySelector("#p-plate_heat");
    return { rows: p ? p.querySelectorAll(".ph-tb tbody tr").length : 0,
             active: p ? p.classList.contains("active") : false };
  });
  check(ph.active && ph.rows > 3, "板块热度有 " + ph.rows + " 行且处于 active");

  console.log("\n[4] 个股抽屉（原荐股收益节奏的挂载点已删，最容易炸）");
  errors.length = 0;
  await page.evaluate(() => { if (typeof openStock === "function") openStock("600519"); });
  await sleep(5000);
  const drawer = await page.evaluate(() => {
    const dw = document.querySelector(".drawer, #drawer, .drw");
    const txt = dw ? dw.innerText : "";
    return {
      open: !!dw && (dw.offsetHeight > 100),
      hasOldBlock: txt.includes("收益节奏加载中") || !!document.getElementById("stkBloggerPerf"),
      len: txt.length,
      head: txt.slice(0, 80).replace(/\s+/g, " "),
    };
  });
  check(drawer.open, "个股抽屉已打开（内容 " + drawer.len + " 字）");
  check(!drawer.hasOldBlock, "抽屉内已无「荐股收益节奏」残留块");
  check(errors.length === 0, "打开抽屉未触发 JS 错误" + (errors.length ? " —— " + errors.slice(0, 2).join(" | ") : ""));

  await page.screenshot({ path: "_verify_after_purge.png" });
  await browser.close();
  console.log("\n" + (fails.length ? "RESULT=FAIL (" + fails.length + ")\n - " + fails.join("\n - ") : "RESULT=PASS"));
  process.exit(fails.length ? 2 : 0);
})().catch((e) => { console.error("EXCEPTION", e); process.exit(2); });
