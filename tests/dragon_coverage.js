/* 龙头徽章覆盖面抽样：逐个进入不同板块，统计该面板内徽章数量。
 * 目的：证明不是只有「长期底部埋伏」挂了牌（进哥要求"点开每一只股票都要看得到"）。
 * 用法：node tests/dragon_coverage.js [port]
 */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");
const PORT = process.argv[2] || "8765";
const BASE = "http://127.0.0.1:" + PORT;
const CHROMIUM_DIR = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
function pickChrome() {
  try {
    const dirs = fs.readdirSync(CHROMIUM_DIR).filter((d) => d.startsWith("chromium-")).sort().reverse();
    for (const d of dirs) for (const sub of ["chrome-win64", "chrome-win"]) {
      const p = path.join(CHROMIUM_DIR, d, sub, "chrome.exe");
      if (fs.existsSync(p)) return p;
    }
  } catch (e) {}
  return EDGE;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// [tab key, 面板选择器, 显示名] —— 进入方式用 selectNav(真值 cat, tab)，不猜 hash
const TARGETS = [
  ["lowbox_bottom", "#p-lowbox_bottom", "长期底部埋伏"],
  ["lowbox", "#p-lowbox", "低位突破(箱体)"],
  ["avoid", "#p-avoid", "避雷名单"],
  ["screener", "#p-screener", "智能选股"],
  ["lhb", "#p-lhb", "龙虎榜"],
  ["watch", "#p-watch", "关注池"],
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: pickChrome(), headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-proxy-server", "--proxy-bypass-list=*"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1760, height: 1100 });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e).slice(0, 120)));
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForFunction(() => typeof DRAGON !== "undefined" && DRAGON.m && Object.keys(DRAGON.m).length > 0,
    { timeout: 30000 });
  let total = 0;
  for (const [tab, sel, label] of TARGETS) {
    const key = await page.evaluate((tb) => {
      for (const c of NAV) { if (c.items.some((i) => i.tab === tb)) return c.key; }
      return null;
    }, tab);
    if (!key) { console.log("  --   " + label + "  [导航里没有 " + tab + "]"); continue; }
    await page.evaluate((h) => { location.hash = h; }, key + "/" + tab);
    await sleep(3000);
    const r = await page.evaluate((s) => {
      const p = document.querySelector(s);
      if (!p) return { found: false, n: 0, rows: 0 };
      const b = p.querySelectorAll(".dragon-badge").length;
      const anchors = p.querySelectorAll("[data-wt-open],[data-code-open]").length;
      const rows = p.querySelectorAll("table tbody tr").length;
      return { found: true, n: b, anchors, rows };
    }, sel);
    total += r.n;
    console.log(
      (r.n > 0 ? "  OK   " : "  --   ") + label.padEnd(14) +
      " 徽章 " + String(r.n).padStart(3) + " / 个股锚点 " + String(r.anchors).padStart(4) +
      " / 表格行 " + String(r.rows).padStart(3) + (r.found ? "" : "  [面板未找到]"));
  }
  console.log("\n合计徽章 " + total + " 枚 · JS 错误 " + errs.length + (errs.length ? "：" + errs.slice(0, 2).join(" | ") : ""));
  await browser.close();
  process.exit(errs.length ? 1 : 0);
})();
