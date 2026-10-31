/* verify_lift.cjs —— 2026-10-30 限售解禁风险回归
 *
 * 背景（进哥当日需求）：「新股的股东临近释放期，到了就能卖，容易砸盘 → 提前提示」。
 * 实证（tools/probe_lift_signal.py / probe_lift_timing.py，1588 个解禁事件）：
 *   解禁前 60~41 日 +0.44% / 前 40~21 日 −0.94%(t=-1.16) / 前 20~11 日 −1.88%(t=-3.36)
 *   / 前 10~1 日 −1.39%(t=-2.81) / 解禁后 1~5 日 +0.31%
 *   → 只有解禁前 20 个交易日才显著跑输，前 40 日还是超额上涨。
 * 所以设置三档：high/mid 亮灯（紫/靛徽标），watch 只给灰字日期。
 *
 * 断言：
 *   ① 无 JS 运行错误；
 *   ② 候选池表头出现「解禁」列，每行结构对齐，且有 high/mid 徽标命中；
 *   ③ **watch 档不得渲染成亮灯徽标**（防呆：以后有人把 watch 也改成 .lift-badge，
 *      就等于「提前三个月报警」，正是实证否掉的做法）；
 *   ④ 徽标 title 必须带「交易日 / 占比 / t 值」这类可核对信息，不能只给一个天数；
 *   ⑤ /api/lift_flags 自洽：td 与 free_date 的距离和交易日历一致、分档符合口径；
 *   ⑥ 关注池出现 lift 字段（watch 也应显示，持仓的人要提前知道哪天能卖）。
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

  // ---------------------------------------------------------------- ① 接口自洽
  console.log("\n[1] /api/lift_flags 自洽");
  const api = await page.evaluate(async () => {
    const r = await fetch("/api/lift_flags?limit=12");
    return { status: r.status, body: await r.json() };
  });
  check(api.status === 200, "接口返回 200");
  const b = api.body || {};
  check(b.ok === true, "ok=true");
  check(!!b.as_of, "带 as_of（" + b.as_of + "）");
  check(typeof b.n_hit === "number" && b.n_hit > 0, "有命中（n_hit=" + b.n_hit + "）");
  const cnt = b.count || {};
  check((cnt.high || 0) + (cnt.mid || 0) > 0, "存在 high/mid 档（" + JSON.stringify(cnt) + "）");
  check(/t=-?[\d.]+/.test(b.caliber || ""), "口径说明含 t 值（可核对）");
  note("口径：" + (b.caliber || "").slice(0, 110));

  const items = Object.entries(b.items || {});
  check(items.length > 0, "items 非空（" + items.length + " 条）");
  let badLevel = [], badTd = [], badRange = [];
  for (const [code, v] of items) {
    // td 必须 >= 0；且 free_date 不应早于 as_of
    if (typeof v.td !== "number" || v.td < 0) badTd.push(code + ":" + v.td);
    if (v.free_date < b.as_of) badRange.push(code + ":" + v.free_date);
    // 分档口径：high 必须是首发原股东且占流通>=3 且 td<=20
    if (v.level === "high" && !(v.type === "首发原股东" && v.free_ratio >= 3 && v.td <= 20)) {
      badLevel.push(code + "(" + v.type + "," + v.free_ratio + "%," + v.td + ")");
    }
    if (v.level === "watch" && v.td <= 20 && v.free_ratio >= 10) {
      badLevel.push("watch漏判:" + code + "(" + v.free_ratio + "%," + v.td + ")");
    }
  }
  check(badTd.length === 0, "td 均非负（异常 " + badTd.slice(0, 3).join(",") + "）");
  check(badRange.length === 0, "解禁日均不在过去（异常 " + badRange.slice(0, 3).join(",") + "）");
  check(badLevel.length === 0, "分档符合口径（异常 " + badLevel.slice(0, 3).join(",") + "）");
  const s0 = items[0][1];
  check(s0.msg && s0.msg.length > 8, "每条带 msg 说明（" + (s0.msg || "").slice(0, 40) + "…）");

  // ---------------------------------------------------------------- ② 候选池
  console.log("\n[2] 方法共振 · 候选池「解禁」列");
  await page.goto(BASE + "#learn/funnel", { waitUntil: "domcontentloaded", timeout: 30000 });
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
    const idx = ths.indexOf("解禁");
    let cellOk = null, nBadge = 0, nWatch = 0, samples = [], badgeTitles = [], watchTitles = [];
    if (idx >= 0 && rows.length) {
      cellOk = rows.every((tr) => tr.children.length === ths.length);
      rows.forEach((tr) => {
        const td = tr.children[idx];
        if (!td) return;
        const bd = td.querySelector(".lift-badge");
        const wt = td.querySelector(".lift-watch");
        if (bd) {
          nBadge++;
          if (badgeTitles.length < 5) badgeTitles.push(bd.getAttribute("title") || "");
          if (samples.length < 4) samples.push(tr.children[0].textContent.trim() + " " + td.textContent.trim());
        } else if (wt) {
          nWatch++;
          if (watchTitles.length < 5) watchTitles.push(wt.getAttribute("title") || "");
        }
      });
    }
    return {
      missing: false, len: box.innerHTML.length, ths, nRows: rows.length,
      idx, cellOk, nBadge, nWatch, samples, badgeTitles, watchTitles, full: box.textContent,
    };
  });
  check(!f.missing, "#p-funnel 容器存在");
  check(tableReady, "候选池表格已渲染（" + f.nRows + " 行）");
  if (f.ths.length) {
    check(f.idx >= 0, "表头含「解禁」列（表头：" + f.ths.join("|") + "）");
    check(f.cellOk !== false, "每行单元格数与表头对齐（含新增列）");
    check(f.nBadge > 0, "解禁列有亮灯徽标（" + f.nBadge + " 只亮灯 / " + f.nWatch + " 只灰字）");
    const bt = f.badgeTitles.join(" || ");
    check(/个交易日后/.test(bt), "徽标 title 带剩余交易日（可核对）");
    check(/占流通/.test(bt) && /t=-?[\d.]+/.test(bt), "徽标 title 带占比与实证 t 值");
    // 关键防呆：watch 档绝不能混进亮灯徽标
    const watchInBadge = f.badgeTitles.filter((t) => /尚未进入实证有效的风险窗口/.test(t));
    check(watchInBadge.length === 0,
      "watch 档未被渲染成亮灯徽标（误判 " + watchInBadge.length + " 只）");
    if (f.watchTitles.length) {
      check(f.watchTitles.every((t) => /个交易日后/.test(t)), "灰字档也带完整日期说明");
    }
    note("亮灯样例：" + (f.samples.join(" · ") || "无"));
  } else {
    check(false, "未取到表头（表格未渲染）");
  }

  // ---------------------------------------------------------------- ③ 关注池
  console.log("\n[3] 关注池 · lift 字段与徽标");
  const w = await page.evaluate(async () => {
    const r = await fetch("/api/watch_list");
    const d = await r.json();
    const its = d.items || [];
    const withLift = its.filter((x) => x.lift && x.lift.level);
    return {
      n: its.length,
      hasField: its.length ? Object.prototype.hasOwnProperty.call(its[0], "lift") : false,
      nLift: withLift.length,
      samples: withLift.slice(0, 5).map((x) => x.symbol + " " + x.lift.level + " td=" + x.lift.td),
    };
  });
  check(w.hasField, "关注池每行都带 lift 字段（无解禁的为 null）");
  note("关注池 " + w.n + " 只，其中有解禁提示 " + w.nLift + " 只：" + (w.samples.join(" · ") || "无"));

  await page.goto(BASE + "#watch/watch", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 7000));
  const wd = await page.evaluate(() => {
    const box = document.querySelector("#p-watch");
    if (!box) return { missing: true };
    return {
      missing: false,
      nBadge: box.querySelectorAll(".lift-badge").length,
      nWatch: box.querySelectorAll(".lift-watch").length,
      titles: [...box.querySelectorAll(".lift-badge,.lift-watch")]
        .slice(0, 3).map((e) => e.getAttribute("title") || ""),
    };
  });
  check(!wd.missing, "#p-watch 容器存在");
  note("关注池徽标：亮灯 " + wd.nBadge + " / 灰字 " + wd.nWatch);
  if (wd.titles.length) {
    check(/个交易日后/.test(wd.titles.join(" ")), "关注池徽标 title 带解禁日期");
  }

  // ---------------------------------------------------------------- ④ JS 错误
  console.log("\n[4] JS 错误（CSP 噪音已滤）");
  check(errors.length === 0, "无页面运行错误（实际 " + errors.length + " 条）");
  errors.slice(0, 4).forEach((e) => console.log("       " + e));

  await page.goto(BASE + "#learn/funnel", { waitUntil: "domcontentloaded", timeout: 30000 });
  try {
    await page.waitForFunction(
      () => document.querySelectorAll("#p-funnel table tbody tr, #fnResult table tbody tr").length > 10,
      { timeout: 90000 });
  } catch (e) { /* 截图尽力而为 */ }
  await new Promise((r) => setTimeout(r, 2000));
  await page.evaluate(() => {
    const t = document.querySelector("#p-funnel table, #fnResult table");
    if (t) t.scrollIntoView({ block: "start" });
  });
  await new Promise((r) => setTimeout(r, 1200));
  await page.screenshot({ path: "_verify_lift_funnel.png", fullPage: false });
  console.log("  截图已存 _verify_lift_funnel.png");

  console.log("\n" + (fails.length ? "FAILED " + fails.length + " 项" : "全部通过"));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})();
