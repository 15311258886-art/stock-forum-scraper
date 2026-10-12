/**
 * UI 回归：境外信源扩展 —— 验证新数据源已接入注册表并在界面诚实呈现。
 * 用法: node tests/ui_sources.js [port]
 *
 * 安全：默认只读。数据准备走 _guard.seed()，只有「端口非 8765 且
 * ALLOW_DATA_RESET=1」才会点击 #btnDemo；否则复用库内现有数据。
 * 详见 tests/_guard.js。
 */
const puppeteer = require("puppeteer-core");
const { PORT, announce, seed, trackRequests, assertNoDestructiveRequest } = require("./_guard");

const CHROME = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const URL = `http://127.0.0.1:${PORT}/`;

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`  ✅ ${name}${extra ? "  (" + extra + ")" : ""}`); }
  else { fail++; console.log(`  ❌ ${name}${extra ? "  (" + extra + ")" : ""}`); }
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const count = (page, sel) => page.$$eval(sel, (els) => els.length).catch(() => 0);

(async () => {
  announce();
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 160)));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text().slice(0, 160)); });
  const seen = trackRequests(page);

  console.log("\n===== 境外信源扩展 · UI 回归 =====\n");
  await page.goto(URL, { waitUntil: "networkidle2" });

  // 数据准备：默认只读（见 _guard.js）。就绪信号=数据源接入状态卡片已渲染
  await seed(page, {
    sel: "#btnDemo",
    ready: async () => (await count(page, "#p-overseas_sentiment .ov-src-status")) > 0,
    timeoutMs: 40000,
  });

  await page.click('#tabs .tab[data-tab="overseas_sentiment"]');
  await wait(1200);

  // ---------- ① 源注册表规模 ----------
  const srcCards = await count(page, "#p-overseas_sentiment .ov-src-status");
  ok("① 数据源注册表已扩容(≥35 个)", srcCards >= 35, `srcCards=${srcCards}`);

  const body = await page.$eval("#p-overseas_sentiment", (e) => e.innerText).catch(() => "");

  // ---------- ② 新增境外源确实在列 ----------
  const newSources = [
    ["Hacker News 科技社区", /hacker news/i],
    ["TechCrunch", /techcrunch/i],
    ["Nature 科研", /nature/i],
    ["PeerTube 视频平台", /peertube/i],
    ["GitHub 趋势", /github/i],
    ["联合早报 亚太视角", /联合早报/],
    ["Wired", /wired/i],
    ["Phys.org", /phys\.org/i],
    ["Fortune", /fortune/i],
  ];
  const missing = newSources.filter(([, re]) => !re.test(body)).map(([n]) => n);
  ok("② 新增境外源全部在列", missing.length === 0,
    missing.length ? "缺少: " + missing.join("、") : `${newSources.length} 项全在`);

  // ---------- ③ 诚实状态分级 ----------
  const demoCards = await count(page, "#p-overseas_sentiment .ov-src-status.src-demo");
  const waitCards = await count(page, "#p-overseas_sentiment .ov-src-status.src-wait");
  ok("③ 示例模式与被墙源状态分色展示", demoCards > 0 && waitCards > 0,
    `示例模式=${demoCards} 待激活=${waitCards}`);

  // ---------- ④ 被墙源诚实标注待激活 ----------
  const hasVpn = /待激活（需 VPN）/.test(body);
  const hasKey = /待激活（需 API Key）/.test(body);
  ok("④ 被墙源标注「待激活」（不伪装成已接入）", hasVpn && hasKey,
    `VPN=${hasVpn} Key=${hasKey}`);

  // ---------- ⑤ 新增区域的样式类已定义 ----------
  const rgSg = await page.evaluate(() => {
    const s = getComputedStyle(document.documentElement);
    const d = document.createElement("span");
    d.className = "badge rg-sg";
    document.body.appendChild(d);
    const bg = getComputedStyle(d).backgroundColor;
    d.remove();
    return bg;
  });
  ok("⑤ 新区域「新加坡/东南亚」样式已定义", rgSg && rgSg !== "rgba(0, 0, 0, 0)", `bg=${rgSg}`);

  // ---------- ⑥ 无 JS 报错 ----------
  ok("⑥ 无 JS 运行时错误", errors.length === 0, errors.length ? errors[0] : "clean");

  await page.screenshot({ path: "shot_sources_expanded.png", fullPage: false });
  console.log(`\n结果: ${pass} 通过 / ${fail} 失败\n`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
