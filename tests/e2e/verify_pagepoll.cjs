/* verify_pagepoll.cjs —— P3「盘中全页轮询」回归（2026-09-29）
 *
 * 覆盖：① 页面无 JS 运行错误；② 顶栏 #pagePoll 开关 + #pagePollState 药丸默认态正确；
 *      ③ isTradingNow() 交易时段边界（工作时段/午休/周末）判定正确；
 *      ④ 开关 on/off 在非交易时段只改状态、不起表；
 *      ⑤ 伪造盘中（覆写 window.isTradingNow）→ 每秒 tick 的 pagePollAutoManage 会「自动起表」，
 *         恢复休市后「自动停表」——这是唯一能在非交易时段验证起/停表的办法。
 *
 * 运行（需查看器已在 127.0.0.1:8765 运行；puppeteer-core 在受管 node workspace）：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" "F:/.../verify_pagepoll.cjs"
 * 退出码：0=PASS，2=FAIL，3=无可用浏览器（跳过）。
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

function pickBrowser() {
  const cands = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ];
  // 技能记录的 Chromium（版本号目录可能变化，扫描取最新）
  try {
    const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
    if (fs.existsSync(base)) {
      fs.readdirSync(base).filter((d) => /^chromium-/.test(d)).sort().reverse()
        .forEach((d) => cands.push(base + "/" + d + "/chrome-win64/chrome.exe"));
    }
  } catch (e) { /* 忽略 */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER: 未找到 Edge/Chrome/Chromium，跳过（非失败）"); process.exit(3); }
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

  const init = await page.evaluate(() => {
    const cb = document.querySelector("#pagePoll");
    const pill = document.querySelector("#pagePollState");
    const nav = document.querySelector("#sidenav");
    return { hasCheckbox: !!cb, pillText: pill && pill.textContent,
      hasIsTradingNow: typeof isTradingNow === "function",
      hasPAGE_POLL: typeof PAGE_POLL === "object" && !!PAGE_POLL,
      navChildren: nav ? nav.children.length : 0 };
  });

  const th = await page.evaluate(() => {
    const mk = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi, 0);
    return {
      Mon_1000: isTradingNow(mk(2026, 9, 28, 10, 0)), Mon_0930: isTradingNow(mk(2026, 9, 28, 9, 30)),
      Mon_1130: isTradingNow(mk(2026, 9, 28, 11, 30)), Mon_1131: isTradingNow(mk(2026, 9, 28, 11, 31)),
      Mon_1200: isTradingNow(mk(2026, 9, 28, 12, 0)), Mon_1400: isTradingNow(mk(2026, 9, 28, 14, 0)),
      Mon_1501: isTradingNow(mk(2026, 9, 28, 15, 1)), Sat_1000: isTradingNow(mk(2026, 9, 26, 10, 0)),
      Sun_1000: isTradingNow(mk(2026, 9, 27, 10, 0)),
    };
  });

  const toggle = await page.evaluate(async () => {
    const cb = document.querySelector("#pagePoll"); const pill = document.querySelector("#pagePollState");
    cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 250));
    const on = { text: pill.textContent, enabled: PAGE_POLL.enabled, running: PAGE_POLL.running, hasTimer: !!PAGE_POLL.timer };
    const stored = (() => { try { return localStorage.getItem("pagePollEnabled"); } catch (e) { return "n/a"; } })();
    cb.checked = false; cb.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 250));
    const off = { text: pill.textContent, enabled: PAGE_POLL.enabled, hasTimer: !!PAGE_POLL.timer };
    return { on, off, stored };
  });

  const forced = await page.evaluate(async () => {
    const cb = document.querySelector("#pagePoll"); const orig = window.isTradingNow;
    cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true }));
    window.isTradingNow = () => true;
    await new Promise((r) => setTimeout(r, 1600));
    const started = { running: PAGE_POLL.running, hasTimer: !!PAGE_POLL.timer,
      cls: document.querySelector("#pagePollState").className };
    window.isTradingNow = () => false;
    await new Promise((r) => setTimeout(r, 1600));
    const stopped = { running: PAGE_POLL.running, hasTimer: !!PAGE_POLL.timer };
    window.isTradingNow = orig;
    cb.checked = false; cb.dispatchEvent(new Event("change", { bubbles: true }));
    try { localStorage.removeItem("pagePollEnabled"); } catch (e) {}
    return { started, stopped };
  });

  console.log("INIT=" + JSON.stringify(init));
  console.log("TRADING=" + JSON.stringify(th));
  console.log("TOGGLE=" + JSON.stringify(toggle));
  console.log("FORCED=" + JSON.stringify(forced));
  console.log("JS_ERRORS=" + JSON.stringify(errors));
  await browser.close();

  const thOk = th.Mon_1000 && th.Mon_0930 && th.Mon_1130 && !th.Mon_1131 && !th.Mon_1200 &&
    th.Mon_1400 && !th.Mon_1501 && !th.Sat_1000 && !th.Sun_1000;
  const initOk = init.hasCheckbox && init.hasIsTradingNow && init.hasPAGE_POLL &&
    init.pillText === "盘中轮询：关闭" && init.navChildren > 0;
  const toggleOk = toggle.on.enabled === true && toggle.on.text === "盘中轮询：非交易时段" &&
    toggle.on.hasTimer === false && toggle.off.enabled === false &&
    toggle.off.text === "盘中轮询：关闭" && toggle.off.hasTimer === false && toggle.stored === "1";
  const forcedOk = forced.started.running === true && forced.started.hasTimer === true &&
    forced.started.cls === "rb-poll on" && forced.stopped.running === false && forced.stopped.hasTimer === false;
  const pass = errors.length === 0 && thOk && initOk && toggleOk && forcedOk;
  console.log("CHECKS=" + JSON.stringify({ thOk, initOk, toggleOk, forcedOk, noErrors: errors.length === 0 }));
  console.log(pass ? "PAGE_POLL_PASS" : "PAGE_POLL_FAIL");
  process.exit(pass ? 0 : 2);
})().catch((e) => { console.log("CRASH: " + (e && e.message)); process.exit(1); });
