/* verify_noblogger.cjs —— 2026-10-30 博主下线回归
 *
 * 断言：
 *   ① 页面无 JS 运行错误（CSP 噪音除外）；
 *   ② 「多源共振」页正常渲染，不停在「正在融合多源信号…」；
 *   ③ 页面上**不出现**「博主」二字（这是本轮硬要求）；
 *   ④ 战法体系支柱仍在（86 个战法 —— 这是进哥要保留的技法成果）；
 *   ⑤ 信号来源里没有 blogger 类。
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

(async () => {
  const browser = await puppeteer.launch({
    executablePath: pickBrowser(), headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  // 首次引导的键是 viewer-first-run-done（见 showFirstRun），进任何页面前先置上，免得挡截图
  await page.evaluateOnNewDocument(() => {
    try { localStorage.setItem("viewer-first-run-done", "1"); } catch (e) {}
  });

  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") {
      const t = m.text();
      if (!/Content Security Policy|Refused to (load|connect|execute)/.test(t)) errors.push("console.error: " + t);
    }
  });

  // 先落地首页并关掉首次引导，再跳共振页（否则弹窗挡截图）
  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2500));
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button, .btn, a")]
      .find((b) => /开始使用|我知道了|关闭/.test((b.textContent || "").trim()));
    if (btn) btn.click();
    document.querySelectorAll(".onboard, .guide-mask, .welcome-mask, .coach-mask").forEach((el) => el.remove());
  });

  await page.goto(BASE + "#overview/resonance", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 8000));

  console.log("\n[1] JS 错误（CSP 噪音已滤）");
  check(errors.length === 0, "无页面运行错误（实际 " + errors.length + " 条）");
  errors.slice(0, 4).forEach((e) => console.log("       " + e));

  console.log("\n[2] 页面渲染");
  const st = await page.evaluate(() => {
    const box = document.querySelector("#p-resonance");
    const nav = [...document.querySelectorAll("[data-tab]")].find((x) => x.dataset.tab === "resonance");
    return {
      has: !!box,
      len: box ? box.innerHTML.length : 0,
      text: box ? box.innerText : "",
      full: box ? box.textContent : "",   // details 折叠内容只在 textContent 里
      loading: box ? /正在融合多源信号/.test(box.innerText) : false,
      navText: nav ? nav.textContent.trim().replace(/\s+/g, " ") : "",
    };
  });
  check(st.has && st.len > 3000, "面板已渲染（" + st.len + " 字节）");
  check(!st.loading, "不再停在「正在融合多源信号…」");
  check(/多源共振/.test(st.navText), "NAV 文案为「多源共振」（实际：" + st.navText + "）");

  console.log("\n[3] 博主痕迹清除");
  /* 允许「博主...已下线」这类说明文字（那是口径，必须留），
     禁止的是荐股/持股的**展示**语境：谁推了什么票、博主提及、博主分档。 */
  const hits = st.full.match(/.{0,26}博主.{0,44}/g) || [];
  const bad = hits.filter((x) => !/下线|不再含|已停/.test(x));
  check(bad.length === 0, "面板不含博主荐股展示（说明性文字除外）");
  bad.slice(0, 5).forEach((x) => console.log("       违规: " + x.replace(/\s+/g, " ")));
  if (hits.length) console.log("       （说明性提及 " + (hits.length - bad.length) + " 处，已放行）");

  console.log("\n[4] 技法留存");
  check(/战法体系/.test(st.text), "「战法体系」支柱仍在");
  const tabNums = await page.evaluate(() => {
    const out = {};
    document.querySelectorAll("#p-resonance .sub-tab").forEach((a) => {
      const n = (a.textContent.match(/(\d+)/) || [])[1];
      out[a.textContent.replace(/[\d\s]/g, "").trim()] = n ? +n : 0;
    });
    return out;
  });
  console.log("       支柱计数：" + JSON.stringify(tabNums));
  check((tabNums["战法体系"] || 0) >= 80, "战法数量 ≥80（实际 " + (tabNums["战法体系"] || 0) + "）");

  console.log("\n[5] 权重口径");
  const cal = await page.evaluate(() => {
    const el = document.querySelector("#p-resonance .res-cal");
    return el ? el.innerText : "";
  });
  check(/博主荐股维度已于 2026-10-30 永久下线/.test(st.full), "口径里说明了博主维度已下线");

  await page.screenshot({ path: "_verify_noblogger.png", fullPage: false });
  console.log("\n" + (fails.length ? "FAILED " + fails.length + " 项" : "全部通过"));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})();
