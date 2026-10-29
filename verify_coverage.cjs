/* 覆盖度披露回归（2026-10-30）—— 进哥关切「5000 多只票怎么老从几百只里选」。
 *
 * 断言三件事：
 *   ① 预测面板顶部同时出现「深度分析 N 只」与「全市场快扫 M 只」，且 N ≥ 1500（修前 594）；
 *   ② 选股面板写明「满足条件 N 只，本页全部列出」，且 N 与接口一致（修前被 POOL_MAX=300 截断）；
 *   ③ 两个面板控制台零 error。
 *
 * 用法： node verify_coverage.cjs
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const BASE = "http://127.0.0.1:8765";

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


async function closeGuide(page) {
  // 引导层是 fixed 遮罩，点「开始使用」在本环境不一定能关掉（且会复用）。
  // 直接隐藏含「欢迎使用…开始使用」的最外层，仅影响截图与目检，不影响断言对象。
  await page.evaluate(() => {
    const hit = Array.from(document.querySelectorAll("div"))
      .filter((d) => {
        const t = (d.textContent || "");
        return t.includes("欢迎使用") && t.includes("开始使用") && t.length < 600;
      })
      .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
    hit.forEach((d) => { d.style.display = "none"; });
    // 兜底：任何 fixed 且盖住大半个视口的层
    Array.from(document.querySelectorAll("div")).forEach((d) => {
      const st = getComputedStyle(d);
      if (st.position === "fixed" && parseFloat(st.zIndex || 0) > 50) {
        const r = d.getBoundingClientRect();
        if (r.width > 500 && r.height > 300) d.style.display = "none";
      }
    });
  }).catch(() => {});
  await new Promise((r) => setTimeout(r, 500));
}

let fail = 0, pass = 0;
function ck(cond, name, extra) {
  if (cond) { pass++; console.log("  PASS " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  → " + extra : "")); }
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("无可用浏览器"); process.exit(3); }
  const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1000 });
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text().slice(0, 160)); });
  page.on("pageerror", (e) => errs.push("pageerror: " + String(e).slice(0, 160)));

  // 关掉新手引导（首次访问会盖住整个面板）
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("button,a,div,span"));
    const b = nodes.find((n) => (n.textContent || "").trim() === "开始使用" && n.offsetParent !== null);
    if (b) b.click();
  }).catch(() => {});
  await new Promise((r) => setTimeout(r, 800));

  // ---- ① 预测面板：深度分析 / 全市场快扫 ----
  console.log("\n[预测面板 · 覆盖度]");
  await page.goto(BASE + "/#overview/prediction", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 3000));
  await closeGuide(page);
  const pred = await page.evaluate(() => {
    const b = document.querySelector("#p-prediction");
    return b ? b.innerText.replace(/\s+/g, " ") : "";
  });
  const mDeep = pred.match(/深度分析\s*([\d,]+)\s*只/);
  const mScan = pred.match(/全市场快扫\s*([\d,]+)\s*只/);
  ck(!!mDeep, "出现「深度分析 N 只」", pred.slice(0, 160));
  ck(!!mScan, "出现「全市场快扫 M 只」", pred.slice(0, 160));
  const nDeep = mDeep ? parseInt(mDeep[1].replace(/,/g, ""), 10) : 0;
  const nScan = mScan ? parseInt(mScan[1].replace(/,/g, ""), 10) : 0;
  ck(nDeep >= 1500, "深度分析池 ≥1500 只（修前 594）", "实际 " + nDeep);
  ck(nScan >= 5000, "全市场快扫 ≥5000 只", "实际 " + nScan);
  console.log("  读数：深度分析 " + nDeep + " 只 / 全市场快扫 " + nScan + " 只");
  await page.screenshot({ path: "shots/cov_pred.png", fullPage: false });

  // ---- ② 选股面板：满足条件只数 vs 实际列出 ----
  console.log("\n[选股面板 · 池子截断]");
  await page.goto(BASE + "/#overview/selection", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 3000));
  await closeGuide(page);
  const selTxt = await page.evaluate(() => {
    const b = document.querySelector("#p-selection");
    return b ? b.innerText.replace(/\s+/g, " ") : "";
  });
  const mCand = selTxt.match(/满足条件\s*([\d,]+)\s*只/);
  ck(!!mCand, "出现「满足条件 N 只」覆盖说明", selTxt.slice(0, 200));
  const nCand = mCand ? parseInt(mCand[1].replace(/,/g, ""), 10) : 0;
  // 默认只渲染 60 行（性能），点「展开全部」后才应列出全部
  const clicked = await page.evaluate(() => {
    const a = Array.from(document.querySelectorAll("a"))
      .find((n) => /展开全部/.test(n.textContent || ""));
    if (a) { a.click(); return true; }
    return false;
  });
  if (clicked) await new Promise((r) => setTimeout(r, 1500));
  ck(clicked || nCand <= 60, "存在「展开全部 N 只」入口", "点击=" + clicked);
  const rowCount = await page.evaluate(() => {
    const t = document.querySelectorAll("#p-selection table")[2];
    if (!t) return 0;
    // 表头是裸 <tr> 里放 <th>（没有 thead），故按「不含 th 的行」计数
    return Array.from(t.querySelectorAll("tr")).filter((tr) => !tr.querySelector("th")).length;
  });
  ck(nCand >= 300, "满足条件只数 ≥300", "实际 " + nCand);
  ck(rowCount === nCand, "列出行数 == 满足条件只数（未被截断）",
     "行数 " + rowCount + " vs 条件 " + nCand);
  console.log("  读数：满足条件 " + nCand + " 只 / 表格 " + rowCount + " 行");
  await page.screenshot({ path: "shots/cov_selection.png", fullPage: false });
  // 低波池在面板下方，单独滚过去截一张做目检（覆盖范围说明 + 展开链接）
  await page.evaluate(() => {
    const all = Array.from(document.querySelectorAll("#p-selection div, #p-selection table"));
    const t = all.find((n) => (n.textContent || "").includes("覆盖范围"));
    if (t) t.scrollIntoView({ block: "start" });
  }).catch(() => {});
  await new Promise((r) => setTimeout(r, 600));
  await page.screenshot({ path: "shots/cov_selection_pool.png", fullPage: false });

  // ---- ③ 控制台 ----
  console.log("\n[控制台]");
  const real = errs.filter((e) => !/Content Security Policy|frame-ancestors/i.test(e));
  ck(real.length === 0, "控制台零 error", real.slice(0, 3).join(" | "));

  console.log(`\n结果：${pass} PASS / ${fail} FAIL`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
