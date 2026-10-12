/* verify_topbar.cjs —— 顶栏控件「职责分组+命名统一+状态牌可点击」回归（2026-09-29）
 *
 * 覆盖：① 页面无 JS 运行错误；② 开关/状态牌命名统一（「盘中轮询」）且每个控件带悬停说明；
 *      ③ 顶栏按「数据抓取 | 页面刷新 | 手动操作」三组分隔（tb-sep ≥2）；
 *      ④ 状态牌 #pagePollState 点击 = 开关（与复选框等效，双向同步）；
 *      ⑤ 底部提示条更新为职责说明文案。
 *
 * 运行（需查看器已在 127.0.0.1:8765 运行）：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" verify_topbar.cjs
 * 退出码：0=PASS，2=FAIL，3=服务未启动（跳过）。
 */
const fs = require("fs");
const http = require("http");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

function probe(url, cb) {
  const req = http.get(url, (res) => { res.resume(); cb(true); });
  req.on("error", () => cb(false));
  req.setTimeout(3000, () => { req.destroy(); cb(false); });
}

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

probe(URL, (up) => {
  if (!up) { console.log("SERVER_DOWN: 查看器未在 8765 运行，跳过（非失败）"); process.exit(3); }
  (async () => {
    const exe = pickBrowser();
    if (!exe) { console.log("NO_BROWSER: 未找到 Edge/Chrome/Chromium，跳过"); process.exit(3); }
    console.log("BROWSER=" + exe);

    const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
      args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1000"] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1000 });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });

    await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await new Promise((r) => setTimeout(r, 3500));

    const ui = await page.evaluate(() => {
      const top = document.querySelector(".topbar");
      const lbl = (id) => {
        const el = document.querySelector(id);
        if (!el) return null;
        const l = el.closest("label");
        return { checked: el.checked, label: l ? l.textContent.trim() : "", title: el.title || (l ? l.title : "") };
      };
      const btn = (id) => { const b = document.querySelector(id); return b ? b.title || "" : null; };
      return {
        realtime: lbl("#realtime"), pagePoll: lbl("#pagePoll"), schedAuto: lbl("#schedAuto"),
        pillText: (document.querySelector("#pagePollState") || {}).textContent || null,
        pillTitle: (document.querySelector("#pagePollState") || {}).title || "",
        pillCursor: (document.querySelector("#pagePollState") || {}).style ? document.querySelector("#pagePollState").style.cursor : "",
        schedText: (document.querySelector("#schedText") || {}).textContent || null,
        seps: top ? top.querySelectorAll(".tb-sep").length : 0,
        btnLoginTitle: btn("#btnLogin"), btnRefreshTitle: btn("#btnRefreshNow"),
        btnFetchTitle: btn("#btnFetch"),
        hintText: (document.querySelector("#statusHint") || {}).textContent || "",
      };
    });

    /* 状态牌点击开/关（用真实 click 事件走 UI 路径） */
    const clickToggle = await page.evaluate(async () => {
      const pill = document.querySelector("#pagePollState");
      const cb = document.querySelector("#pagePoll");
      try { localStorage.removeItem("pagePollEnabled"); } catch (e) {}
      cb.checked = false; cb.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 250));
      pill.click();                                    // 关 → 开
      await new Promise((r) => setTimeout(r, 300));
      const on = { enabled: PAGE_POLL.enabled, cbChecked: cb.checked, pill: pill.textContent };
      pill.click();                                    // 开 → 关
      await new Promise((r) => setTimeout(r, 300));
      const off = { enabled: PAGE_POLL.enabled, cbChecked: cb.checked, pill: pill.textContent };
      try { localStorage.removeItem("pagePollEnabled"); } catch (e) {}
      return { on, off };
    });

    console.log("UI=" + JSON.stringify(ui));
    console.log("CLICK=" + JSON.stringify(clickToggle));
    console.log("JS_ERRORS=" + JSON.stringify(errors));
    await browser.close();

    const nameOk = ui.pagePoll && ui.pagePoll.label.includes("盘中轮询") &&
      ui.pillText && ui.pillText.includes("盘中轮询") &&
      ui.schedText && (ui.schedText.includes("调度状态") || ui.schedText.includes("自动调度"));
    const tipOk = !!ui.realtime.title && !!ui.pagePoll.title && !!ui.schedAuto.title &&
      !!ui.pillTitle && ui.btnLoginTitle !== null && !!ui.btnRefreshTitle && !!ui.btnFetchTitle;
    const groupOk = ui.seps >= 2;
    const clickOk = clickToggle.on.enabled === true && clickToggle.on.cbChecked === true &&
      clickToggle.on.pill.includes("盘中轮询：") && !clickToggle.on.pill.includes("关闭") &&
      clickToggle.off.enabled === false && clickToggle.off.cbChecked === false &&
      clickToggle.off.pill.includes("关闭");
    const hintOk = ui.hintText.includes("盘中轮询") && ui.hintText.includes("数据抓取");
    const pass = errors.length === 0 && nameOk && tipOk && groupOk && clickOk && hintOk;
    console.log("CHECKS=" + JSON.stringify({ nameOk, tipOk, groupOk, clickOk, hintOk, noErrors: errors.length === 0 }));
    console.log(pass ? "TOPBAR_PASS" : "TOPBAR_FAIL");
    process.exit(pass ? 0 : 2);
  })().catch((e) => { console.log("CRASH: " + (e && e.message)); process.exit(1); });
});
