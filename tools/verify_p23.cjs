/* P2-3 收口后真机校验：浅色零变化 + 暗色自动修复为令牌值。
 * 只读，不改库。用法： NODE_PATH="<ws>/node_modules" node tools/verify_p23.cjs 8765
 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const PORT = process.argv[2] || "8765";
const URL = `http://127.0.0.1:${PORT}/`;
const SHOT = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/.qa_iso/p23_20261015/shots";
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function hexRgb(h) { h = h.replace("#", ""); if (h.length === 3) h = h.split("").map(c => c + c).join(""); const n = parseInt(h, 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
function parseRgb(s) { if (!s) return null; const m = s.match(/\d+/g); return m ? m.slice(0, 3).map(Number) : null; }
const eq = (a, b) => a && b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2];

// 期望计算色（与 light-tech / dark-dashboard 令牌值一致；light 即旧硬编码值，dark 即修复后值）
const SEL = {
  ".ov-src-status.src-on .ov-src-st-badge":  { light: { bg: "#DCFCE7", fg: "#15803D" }, dark: { bg: "#122820", fg: "#6FB892" } },
  ".ov-src-status.src-demo .ov-src-st-badge": { light: { bg: "#DBEAFE", fg: "#1D4ED8" }, dark: { bg: "#19222E", fg: "#7E94AB" } },
  ".ov-src-status.src-err .ov-src-st-badge":  { light: { bg: "#FEE2E2", fg: "#B91C1C" }, dark: { bg: "#2A1816", fg: "#F2544C" } },
  ".ov-src-status.src-wait .ov-src-st-badge": { light: { bg: "#FEF3C7", fg: "#B45309" }, dark: { bg: "#2A2110", fg: "#E0A33E" } },
};

(async () => {
  const b = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] });
  const page = await b.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await page.goto(URL, { waitUntil: "networkidle2" });
  await wait(800);
  // 进海外舆情面板（含全部源状态徽标色族）
  await page.evaluate(() => { const el = document.querySelector('#sidenav .nav-item[data-cat="overseas_sentiment"]'); if (el) el.click(); });
  await wait(900);

  for (const theme of ["light-tech", "dark-dashboard"]) {
    await page.evaluate((t) => applyTheme(t), theme);
    await wait(700);
    console.log("\n===== 主题 " + theme + " =====");
    for (const sel of Object.keys(SEL)) {
      const r = await page.evaluate((s) => {
        const el = document.querySelector(s);
        if (!el) return null;
        const cs = getComputedStyle(el);
        return { bg: cs.backgroundColor, fg: cs.color };
      }, sel);
      const exp = SEL[sel][theme === "light-tech" ? "light" : "dark"];
      if (!r) { console.log("  " + sel + "  —— 元素不在 DOM（跳过）"); continue; }
      const bgOk = eq(parseRgb(r.bg), hexRgb(exp.bg));
      const fgOk = eq(parseRgb(r.fg), hexRgb(exp.fg));
      console.log(`  ${sel}\n     bg=${r.bg} 期望${exp.bg} ${bgOk ? "OK" : "X"}\n     fg=${r.fg} 期望${exp.fg} ${fgOk ? "OK" : "X"}`);
    }
    await page.screenshot({ path: `${SHOT}/p23-${theme}.png`, fullPage: false });
    console.log("  shot -> p23-" + theme + ".png");
  }
  console.log("\nJS错误:", errs.length ? errs.join(" | ") : "无");
  await b.close();
})();
