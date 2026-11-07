/* verify_plate_heat.cjs —— 板块热度榜回归（2026-10-29）
 *
 * 覆盖：
 *   ① 页面无 JS 运行错误；
 *   ② NAV 里出现「板块热度」，且落在「资金动向 · 人气」组；
 *   ③ 切到该页后面板非空、表格有行、前三是真实板块名（不是 undefined/NaN）；
 *   ④ 口径说明块存在（这是本页的硬性要求：缺陷必须照实露出）；
 *   ⑤ 页面上**不得出现**方向性/荐股性文字（买入/目标价/看多/荐股/博主）——
 *      这是本轮产品约束，回归时要把关，防止后续迭代把荐股偷偷加回来。
 *
 * 运行：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" verify_plate_heat.cjs
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

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER: 未找到浏览器，跳过（非失败）"); process.exit(3); }
  const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1100"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1100 });
  const errors = [];
  // 已知噪音：index.html 的 <meta> CSP 里写了 frame-ancestors，浏览器必定忽略并报一条
  // console.error。它是既有问题、与本页无关，若不过滤会让每次回归都假红。
  const NOISE = /frame-ancestors/i;
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !NOISE.test(m.text())) errors.push("console.error: " + m.text());
  });

  // 首次访问会弹「3 步上手」引导（showFirstRun），整页都挡住截图。
  // 它的开关是 localStorage["viewer-first-run-done"]，先写标记再进目标页最干净 ——
  // 点按钮的方法不可靠：弹窗可能在 load 之后才渲染，脚本执行时机稍早就会点空。
  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.evaluate(() => { try { localStorage.setItem("viewer-first-run-done", "1"); } catch (e) {} });
  await page.goto(BASE + "#money/plate_heat", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 6000));
  await page.evaluate(() => {
    const m = document.getElementById("firstRunModal");
    if (m) m.hidden = true;
    const mask = document.getElementById("firstRunMask");
    if (mask) mask.classList.remove("show");
  });

  console.log("\n[1] JS 错误");
  check(errors.length === 0, "页面无 JS 错误" + (errors.length ? " —— " + errors.slice(0, 3).join(" | ") : ""));

  console.log("\n[2] 导航项");
  // 子标签在内容区顶部的 subnav 里（带 data-tab），大类在左栏 sidenav（带 data-cat），
  // 两者不在同一 DOM 分支 —— 所以大类归属要读「左栏当前 active 的那一项」，不能靠 closest。
  const nav = await page.evaluate(() => {
    const tab = [...document.querySelectorAll("[data-tab]")].find((x) => x.dataset.tab === "plate_heat");
    const cat = document.querySelector("#sidenav .nav-item.active .ni-txt");
    return { exists: !!tab, text: tab ? tab.textContent.trim() : "", cat: cat ? cat.textContent.trim() : "" };
  });
  check(nav.exists, "NAV 中存在 plate_heat 子标签");
  check(/板块热度/.test(nav.text), "文案为「板块热度」（实际：" + nav.text + "）");
  check(/资金动向/.test(nav.cat), "归在「资金动向」大类下（实际：" + nav.cat + "）");

  console.log("\n[3] 面板内容");
  const ph = await page.evaluate(() => {
    const p = document.querySelector("#p-plate_heat");
    if (!p) return { missing: true };
    const rows = [...p.querySelectorAll(".ph-tb tbody tr")];
    return {
      active: p.classList.contains("active"),
      rows: rows.length,
      first: rows.slice(0, 3).map((r) => ({
        rank: (r.querySelector(".ph-rk") || {}).textContent,
        plate: (r.querySelector(".ph-plate") || {}).textContent,
        heat: (r.querySelector(".ph-heat b") || {}).textContent,
      })),
      leads: [...p.querySelectorAll(".ph-lead-p")].map((x) => x.textContent.trim()),
      foot: p.querySelectorAll(".ph-foot li").length,
      bad: /undefined|NaN|null/.test(p.innerText),
      // 反向检查只看**榜单本体**（.ph-tb / .ph-lead），
      // 底部「口径说明」本来就要出现「荐股/看多看空」这些词来解释「已下架什么」，不能一起扫。
      text: ((p.querySelector(".ph-tb") || {}).innerText || "")
        + ((p.querySelector(".ph-lead-row") || {}).innerText || ""),
    };
  });
  check(!ph.missing, "#p-plate_heat 容器存在");
  check(ph.active, "面板处于 active");
  check(ph.rows > 3, "榜单有 " + ph.rows + " 行（>3）");
  check(ph.leads.length === 3, "前三名结论条有 3 张卡");
  check((ph.first || []).every((r) => r.plate && /[\u4e00-\u9fa5A-Za-z]/.test(r.plate)),
    "前三名板块名正常：" + (ph.first || []).map((r) => r.plate + "/" + r.heat).join("、"));
  check(!ph.bad, "页面无 undefined/NaN/null 字样");
  check(ph.foot >= 5, "口径说明条目 " + ph.foot + " 条（>=5）");

  console.log("\n[4] 反向检查：不得出现荐股/方向语义");
  const banned = ["买入", "卖出", "目标价", "看多", "看空", "荐股", "推荐买", "博主推荐", "收益率"];
  const hit = banned.filter((w) => (ph.text || "").includes(w));
  check(hit.length === 0, "无方向性/荐股性文字" + (hit.length ? " —— 命中：" + hit.join("、") : ""));

  await page.screenshot({ path: "_verify_plate_heat.png", fullPage: false });
  console.log("\n截图 -> _verify_plate_heat.png");

  await browser.close();
  console.log("\n" + (fails.length ? "RESULT=FAIL (" + fails.length + ")\n - " + fails.join("\n - ")
    : "RESULT=PASS"));
  process.exit(fails.length ? 2 : 0);
})().catch((e) => { console.error("EXCEPTION", e); process.exit(2); });
