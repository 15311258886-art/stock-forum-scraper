/* 前端版本自检回归（2026-10-02）
 *
 * 背景（这条回归就是在防它复发）：网页里的 JS 不会自己重新加载，跑的永远是「打开那一刻那份代码」。
 * 页面只定时拉 /api/dashboard 刷数据，不重新拉 index.html / app.js。于是后端改完前端，
 * 已经开着的旧窗口还是旧代码 —— 接口把新数据递过去，旧 JS 也渲染不出来。
 * 2026-10-01 就是这么回事：集合竞价做完了，王总桌面窗口里就是看不到（重开窗口才出现）。
 *
 * 防复发手段：每 60s 跟 /api/version 比一次版本，不一致就顶部挂一条「点这一条即可换上新代码」。
 * 本脚本验四件事：
 *   ① 接口返回的版本 == index.html 里 <meta name="app-version">（唯一真源，不许写两份）
 *   ② 版本一致时不打扰（不挂提示条）
 *   ③ 版本不一致时挂出提示条，写明「旧 → 新」，且点了会 reload
 *   ④ 竞价卡片在首屏内、且排在主线研判之上（改版后别又挪到看不见的地方）
 */
const puppeteer = require("puppeteer-core");

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765";
let pass = 0, fail = 0;
const say = (ok, msg) => { ok ? pass++ : fail++; console.log((ok ? "PASS  " : "FAIL  ") + msg); };

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new", args: ["--no-sandbox", "--disable-gpu"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 860 });
  await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(".auc-card", { timeout: 60000 });
  await new Promise(r => setTimeout(r, 1500));

  // ① 接口版本 == 页面 meta
  const rst = await page.evaluate(async () => {
    const meta = document.querySelector('meta[name="app-version"]');
    const j = await fetch("/api/version").then(r => r.json());
    return { meta: meta ? meta.content : "", api: j.version };
  });
  say(!!rst.api && rst.api === rst.meta, `① 前后端版本同源（meta=${rst.meta} / api=${rst.api}）`);

  // ② 版本一致 → 不挂提示条
  const none = await page.evaluate(() => !!document.getElementById("ver-notice"));
  say(none === false, "② 版本一致时不弹提示条（不打扰看盘）");

  // ③ 模拟「页面跑旧版」→ 应挂出提示条，文案带旧→新，点击触发 reload
  const shot = await page.evaluate(() => {
    renderVersionNotice("20261001-boardfix", "20261002-updatenotice");
    const bar = document.getElementById("ver-notice");
    if (!bar) return { ok: false };
    const cs = getComputedStyle(bar);
    return {
      ok: true,
      text: bar.textContent,
      title: bar.title || "",
      cursor: cs.cursor,
      position: cs.position,
      isFirst: document.body.firstElementChild === bar,
      z: cs.zIndex,
      bar: bar.getBoundingClientRect().top,
    };
  });
  say(shot.ok, "③ 版本不一致时挂出提示条");
  if (shot.ok) {
    say(/20261001-boardfix/.test(shot.text) && /20261002-updatenotice/.test(shot.text),
      `③ 文案写明「旧 → 新」：${(shot.text || "").slice(0, 60)}`);
    say(shot.cursor === "pointer" && shot.position === "sticky",
      `③ 提示条可点、吸顶（cursor=${shot.cursor} / position=${shot.position}）`);
    say(shot.isFirst, "③ 提示条贴在 body 最顶部，不会被内容盖住");
  }

  // ④ 竞价卡位置：在首屏内 + 排在主线研判之上
  const pos = await page.evaluate(() => {
    const c = document.querySelector(".auc-card");
    const h = [...document.querySelectorAll(".hint")].find(e => e.textContent.includes("主线研判"));
    return {
      cardTop: c ? Math.round(c.getBoundingClientRect().top + window.scrollY) : null,
      cardH: c ? Math.round(c.getBoundingClientRect().height) : null,
      hintTop: h ? Math.round(h.getBoundingClientRect().top + window.scrollY) : null,
      vh: window.innerHeight,
    };
  });
  say(pos.cardTop !== null && pos.cardTop < 400, `④ 竞价卡在首屏内（top=${pos.cardTop}px，视口高 ${pos.vh}）`);
  say(pos.hintTop !== null && pos.cardTop < pos.hintTop,
    `④ 竞价卡排在主线研判之上（卡 ${pos.cardTop} < 主线 ${pos.hintTop}）`);

  // ⑤ 真点一下（真实 click → 真实 reload）：reload 会把页面里的临时标记冲掉，据此判定「点了确实换代码」
  //    注意：reload 会销毁执行上下文，所以「挂标记 + 点击」和「事后查标记」必须分两步，不能塞进同一个 evaluate。
  await page.evaluate(() => {
    renderVersionNotice("20261001-boardfix", "20261002-updatenotice");
    window.__probe = "alive";
    const bar = document.getElementById("ver-notice");
    if (bar) bar.click();
  });
  await new Promise(r => setTimeout(r, 3000));
  const probe = await page.evaluate(() => (window.__probe || null));
  say(probe === null, `⑤ 点提示条真的重新加载了页面（页内标记已冲掉 = ${probe === null}）`);

  // reload 后页面恢复正常：版本一致 → 不该再有提示条
  await page.waitForSelector(".auc-card", { timeout: 60000 });
  await new Promise(r => setTimeout(r, 1200));
  const after = await page.evaluate(() => ({
    notice: !!document.getElementById("ver-notice"),
    card: !!document.querySelector(".auc-card"),
  }));
  say(after.card && !after.notice,
    "⑤ 重新加载后竞价卡照常显示、提示条自己撤掉（换到新版且不再打扰）");

  console.log(`\n结果：${pass}/${pass + fail} 通过`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("ERR", e.stack || e.message); process.exit(1); });
