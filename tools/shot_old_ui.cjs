/**
 * 改版前界面逐页截图 —— 给进哥圈选用。
 *
 * 目标：按左栏每个大类，把改版前（git 1a58511）那 43 个面板逐个拍下来，
 *      每张图里左栏、当前大类、子标签横条都在，便于"圈出想要的功能"。
 *
 * 三个必须写下来的坑（都是实测踩出来的，不是推测）：
 *  1. 旧首屏有个 `#firstRunMask`（class 带 show）盖住整页 —— 真实鼠标点击会被它
 *     全部吃掉，导航"点了没反应"。所以遍历用 evaluate 内派发事件，截图前把它隐藏。
 *  2. 不能固定 sleep 等页面画完（上一轮同一个固定 sleep 量出过两个不同数字）。
 *     改成轮询"面板容器有内容 / 明确空态"，上限 12s。
 *  3. 深链接 goto 而不是点击遍历 —— 点击会带上一个面板的残留状态。
 *
 * 用法：node tools/shot_old_ui.cjs --base http://127.0.0.1:8791/ --out shots/old_ui_20261101
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const arg = (k, d) => {
  const i = process.argv.indexOf("--" + k);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : d;
};
const BASE = arg("base", "http://127.0.0.1:8791/");
const OUT = arg("out", "shots/old_ui_20261101");
const W = 1440, H = 1000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findChromium() {
  for (const c of [
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe",
  ]) { try { if (fs.existsSync(c)) return c; } catch (_) {} }
  return null;
}

async function hideMask(page) {
  /* 旧首屏有两层浮层，必须都收掉才看得见正文：
       #firstRunMask  —— 全屏遮罩（class 带 show，会吃掉所有真实点击）
       #firstRunModal —— "欢迎使用 · 3 步快速上手"（fixed, z=9101）
     这不是"美化截图"，是消除遮挡；两个 id 都是从 DOM 实测拿到的，不是猜的。 */
  await page.evaluate(() => {
    ["firstRunMask", "firstRunModal"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) { el.style.display = "none"; el.classList.remove("show"); }
    });
    document.querySelectorAll(".modal-mask.show").forEach((m) => {
      m.style.display = "none"; m.classList.remove("show");
    });
  });
}

async function waitPanel(page, tab, ms = 12000) {
  const t0 = Date.now();
  let last = { ok: false, len: 0 };
  while (Date.now() - t0 < ms) {
    last = await page.evaluate((tb) => {
      const el = document.querySelector("#p-" + tb);
      if (!el) return { ok: false, why: "no-container" };
      const t = (el.innerText || "").trim();
      const loading = /正在加载|加载中/.test(t) && t.length < 60;
      return { ok: t.length > 30 && !loading, len: t.length, loading, head: t.slice(0, 70) };
    }, tab);
    if (last.ok) return last;
    await sleep(400);
  }
  return Object.assign({ why: "timeout" }, last);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const EXE = findChromium();
  if (!EXE) { console.error("找不到 Chromium"); process.exit(2); }
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--hide-scrollbars", `--window-size=${W},${H}`],
    protocolTimeout: 180000,
  });
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));

  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(3500);
  await hideMask(page);

  // ---- 枚举：内派发 click 遍历左栏，读每类的子标签（DOM 实测，不写死）----
  const nav = await page.$$eval("#sidenav .nav-item[data-cat]", (els) =>
    els.map((e) => ({ cat: e.dataset.cat, label: (e.querySelector(".ni-txt") || e).textContent.trim() }))
  );
  const plan = [];
  for (const c of nav) {
    await page.evaluate((cat) => {
      const a = document.querySelector(`#sidenav .nav-item[data-cat="${cat}"]`);
      if (a) a.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    }, c.cat);
    await sleep(900);
    const tabs = await page.$$eval("#subnav .sub-tab[data-tab]", (els) =>
      els.map((e) => ({ tab: e.dataset.tab, label: (e.textContent || "").trim() }))
    );
    plan.push({ cat: c.cat, catLabel: c.label, tabs });
  }
  const total = plan.reduce((a, c) => a + c.tabs.length, 0);
  console.log(`[plan] 左栏 ${plan.length} 类 · 子标签合计 ${total} 个`);
  plan.forEach((c) => console.log(`  【${c.catLabel}】(${c.cat}) ${c.tabs.map((t) => t.label + "(" + t.tab + ")").join(" · ")}`));
  if (total < 20) { console.error("枚举明显偏少，导航未重绘，终止"); await browser.close(); process.exit(3); }

  // ---- 逐页截图 ----
  const report = [];
  let n = 0;
  for (const c of plan) {
    for (const t of c.tabs) {
      n++;
      const name = `${String(n).padStart(2, "0")}-${c.cat}-${t.tab}.png`;
      const t0 = Date.now();
      await page.goto(`${BASE}#${c.cat}/${t.tab}`, { waitUntil: "domcontentloaded" });
      await sleep(300);
      await hideMask(page);
      const st = await waitPanel(page, t.tab);
      await hideMask(page);          // 有的面板加载完才会弹遮罩
      let shot = "ok";
      try {
        await page.screenshot({ path: path.join(OUT, name), fullPage: true });
      } catch (e) { shot = "shot-fail:" + String(e).slice(0, 80); }
      report.push({
        idx: n, cat: c.cat, catLabel: c.catLabel, tab: t.tab, label: t.label,
        file: name, textLen: st.len || 0, head: (st.head || "").replace(/\s+/g, " "),
        ok: !!st.ok, shot, ms: Date.now() - t0,
      });
      console.log(`[${String(n).padStart(2, "0")}] ${c.cat}/${t.tab}  ${st.ok ? "有内容" : "空/超时"} ${st.len || 0}字 ${Date.now() - t0}ms`);
    }
  }

  fs.writeFileSync(path.join(OUT, "_index.json"),
    JSON.stringify({ base: BASE, commit: "1a58511", viewport: [W, H], at: new Date().toISOString(), plan, pages: report, pageErrors: errors }, null, 1), "utf8");

  const empty = report.filter((r) => !r.ok);
  console.log(`\n[结果] 共 ${report.length} 页 · 有内容 ${report.length - empty.length} · 空/超时 ${empty.length} · pageerror ${errors.length}`);
  if (empty.length) console.log("  空白页: " + empty.map((e) => e.cat + "/" + e.tab).join(", "));
  if (errors.length) console.log("  错误: " + [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
})();
