/* verify_avoid.cjs —— 2026-10-30 避坑标记（替代博主反向警示）回归
 *
 * 背景：旧「博主反向警示」拿 blogger_overlap 全期快照绑定个股（6 月推荐 11 月还亮灯），
 * 且 blogger_risk 字段前端从未渲染 —— 属于「写了没人读」的死功能。
 * 现改为 viewer/avoid.py：当日量价 + 近 15 日两类利空公告，实时算、不落库。
 *
 * 断言：
 *   ① 页面无 JS 运行错误（CSP 噪音除外）；
 *   ② 关注池：命中票渲染 .av-badge，且 title 里带实证依据（T+5超额 / t=）；
 *   ③ 方法共振候选池：表头出现「避坑」列，且每行该列有单元格（结构对齐）；
 *   ④ 两处面板均不出现「博主反向警示 / 博主提及 / N 位博主」这类旧文案；
 *   ⑤ /api/avoid_flags 可用且字段自洽（score/level/detail/flags 齐备）。
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
function note(msg) { console.log("  ---- " + msg); }

(async () => {
  const browser = await puppeteer.launch({
    executablePath: pickBrowser(), headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
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

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2500));
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button, .btn, a")]
      .find((b) => /开始使用|我知道了|关闭/.test((b.textContent || "").trim()));
    if (btn) btn.click();
    document.querySelectorAll(".onboard, .guide-mask, .welcome-mask, .coach-mask").forEach((el) => el.remove());
  });

  // ---------------------------------------------------------------- ① 关注池
  console.log("\n[1] 关注池 · 徽标");
  await page.goto(BASE + "#watch/watch", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 7000));

  const w = await page.evaluate(() => {
    const box = document.querySelector("#p-watch");
    if (!box) return { missing: true };
    const badges = [...box.querySelectorAll(".av-badge")];
    return {
      missing: false,
      len: box.innerHTML.length,
      rows: box.querySelectorAll("tbody tr").length,
      nBadge: badges.length,
      titles: badges.slice(0, 3).map((b) => b.getAttribute("title") || ""),
      texts: badges.slice(0, 6).map((b) => b.textContent.trim()),
      full: box.textContent,
    };
  });
  check(!w.missing, "#p-watch 容器存在");
  check(w.len > 500, "关注池面板已渲染（" + w.len + " 字节 / " + w.rows + " 行）");
  if (w.nBadge > 0) {
    check(/T\+5超额|t=/.test(w.titles.join(" ")), "徽标 hover 带实证依据（t 值/超额）");
    check(/⚠\d+/.test(w.texts.join(" ")), "徽标显示「⚠分数」（实际：" + w.texts.join(" ") + "）");
    note("命中 " + w.nBadge + "/" + w.rows + " 行");
  } else {
    note("本轮关注池无命中票（非失败：18% 量级命中率，池子小可能全不中）");
  }

  // ---------------------------------------------------------------- ② 候选池
  console.log("\n[2] 方法共振 · 候选池「避坑」列");
  await page.goto(BASE + "#learn/funnel", { waitUntil: "domcontentloaded", timeout: 30000 });
  // 接口要现算 1000+ 只票的四方法与避坑，十几秒正常；等表格真正出现再断言，
  // 固定 sleep 会时快时慢（第一次就踩过：表格没渲染完 → ths 为空）。
  let tableReady = true;
  try {
    await page.waitForFunction(
      () => document.querySelectorAll("#p-funnel table tbody tr, #fnResult table tbody tr").length > 3,
      { timeout: 90000 });
  } catch (e) { tableReady = false; }

  const f = await page.evaluate(() => {
    const box = document.querySelector("#p-funnel") || document.querySelector("#fnResult");
    if (!box) return { missing: true };
    const ths = [...box.querySelectorAll("table th")].map((t) => t.textContent.trim());
    const rows = [...box.querySelectorAll("table tbody tr")];
    const idx = ths.indexOf("避坑");
    let cellOk = null, nBadge = 0, nDash = 0, samples = [];
    if (idx >= 0 && rows.length) {
      cellOk = rows.every((tr) => tr.children.length === ths.length);
      rows.forEach((tr) => {
        const td = tr.children[idx];
        if (!td) return;
        if (td.querySelector(".av-badge")) {
          nBadge++;
          if (samples.length < 4) samples.push(tr.children[0].textContent.trim() + " " + td.textContent.trim());
        } else nDash++;
      });
    }
    return {
      missing: false, len: box.innerHTML.length,
      ths, nRows: rows.length, idx, cellOk, nBadge, nDash, samples,
      full: box.textContent,
    };
  });
  check(!f.missing, "#p-funnel 容器存在");
  check(tableReady, "候选池表格已渲染（" + f.nRows + " 行）");
  if (f.ths.length) {
    check(f.idx >= 0, "表头含「避坑」列（表头：" + f.ths.join("|") + "）");
    check(f.cellOk !== false, "每行单元格数与表头对齐（含新增列）");
    // 关键断言：整列至少有命中，否则说明前后端字段没接上（不是「恰好没命中」）
    check(f.nBadge > 0, "避坑列有命中徽标（命中 " + f.nBadge + "/" + f.nRows + "，空白 " + f.nDash + "）");
    note("命中样例：" + (f.samples.join(" · ") || "无"));
  } else {
    check(false, "未取到表头（表格未渲染）");
  }

  // ---------------------------------------------------------------- ③ 旧文案
  console.log("\n[3] 旧博主警示痕迹");
  const stale = ((w.full || "") + " " + (f.full || "")).match(/.{0,20}(博主反向警示|博主提及|位博主共振|博主密集推荐).{0,30}/g) || [];
  check(stale.length === 0, "不含博主反向警示/博主提及类旧文案");
  stale.slice(0, 5).forEach((x) => console.log("       残留: " + x.replace(/\s+/g, " ")));

  // ---------------------------------------------------------------- ④ 接口
  console.log("\n[4] /api/avoid_flags 自洽");
  const api = await page.evaluate(async () => {
    const r = await fetch("/api/avoid_flags?limit=6");
    return { status: r.status, body: await r.json() };
  });
  check(api.status === 200, "接口返回 200");
  const b = api.body || {};
  check(b.ok === true, "ok=true");
  check(typeof b.n === "number" && b.n > 1000, "覆盖全市场（n=" + b.n + "）");
  check(typeof b.n_hit === "number" && b.n_hit > 0, "有命中（n_hit=" + b.n_hit + "）");
  const first = Object.values(b.items || {})[0] || {};
  check(["high", "mid"].includes(first.level), "命中的票按档位标注（level=" + first.level + "）");
  check(Array.isArray(first.flags) && first.flags.length > 0 && !!first.flags[0].ev,
    "flags 带实证摘要（ev）");
  check(/指标加权|无博主数据/.test(b.caliber || ""), "口径说明声明无博主数据");
  note("口径：" + (b.caliber || ""));

  console.log("\n[5] JS 错误（CSP 噪音已滤）");
  check(errors.length === 0, "无页面运行错误（实际 " + errors.length + " 条）");
  errors.slice(0, 4).forEach((e) => console.log("       " + e));

  await page.goto(BASE + "#watch/watch", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 5000));
  await page.screenshot({ path: "_verify_avoid_watch.png", fullPage: false });

  console.log("\n" + (fails.length ? "FAILED " + fails.length + " 项" : "全部通过"));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})();
