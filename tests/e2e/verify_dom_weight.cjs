/* verify_dom_weight.cjs —— 首屏 DOM 节点称重（2026-11-01 P1-2 第 3 项；2026-11-01 二次修正）
 *
 * ⚠️ 为什么有「收敛采样」而不是 `sleep(2500)` 然后数一次：
 *    同一天同一份代码，固定 2.5s 采样量出过 3,441，等 8s 再量是 4,509 —— 差的 1,068 个节点
 *    是 `#p-watch`（关注池）还没画完。也就是说「等固定时间再数」这个测法本身就是假数字的来源：
 *    代码越慢/越冷，数越小，看起来像"优化得更好了"。这种指标比不测更糟。
 *    所以本脚本改成：每 400ms 采样一次，**连续 5 次（2 秒）节点数不变**才认为收敛，
 *    上限 30 秒；输出 settled@Xs 与采样曲线，任何人都能复核那个数是怎么来的。
 *    只有一条测量路径（不保留"快速模式"）：留两条，早晚有人只跑快的那个，然后拿到假数字。
 *
 * 输出三张表：
 *   ① 总量（settled）：document.querySelectorAll('*').length
 *   ② 按容器分组（导航区 / 顶栏 / 横条 / 各面板 / 弹层 / 隐藏节点）
 *   ③ Top 10 容器（子树节点数降序）
 *
 * 2026-11-01 口径备注（写在这里，否则下次有人看到 4,474 → 4,509 会去查"是不是回涨了"）：
 *   P0-2 证据门禁上线后，「③ 今日可执行机会」由 6 张短卡片变成空态（4 行理由 + 折叠区标题），
 *   节点数 **+35**（#p-today 245 → 279）。这是"把理由写清楚"的代价，不是瘦身回涨；
 *   真正的瘦身项（海外舆情 5164 / 龙虎榜 1958 / 国际局势 1237 三个隐藏面板）首屏仍是 0 节点。
 *
 * 运行：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   node verify_dom_weight.cjs            # 当前 hash（默认 #today/today）
 *   node verify_dom_weight.cjs --json out.json
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";
const HASH = process.env.VIEWER_HASH || "#today/today";

const SAMPLE_MS = 400;      // 采样间隔
const STABLE_N = 5;         // 连续多少次不变算收敛（5 × 400ms = 2s）
const MAX_MS = 30000;       // 上限

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
  } catch (e) { /* ignore */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

/* 页面内执行：全部用原生 DOM，不依赖 app.js 内部状态。 */
function measure() {
  const all = document.querySelectorAll("*");
  const count = (root) => (root ? root.querySelectorAll("*").length + 1 : 0);

  const isHidden = (el) => {
    if (!el.isConnected) return true;
    const st = getComputedStyle(el);
    if (st.display === "none") return true;
    if (st.visibility === "hidden") return true;
    return el.offsetParent === null && st.position !== "fixed";
  };
  let visible = 0;
  let hidden = 0;
  all.forEach((el) => (isHidden(el) ? hidden++ : visible++));

  const regions = [];
  const push = (name, sel) => {
    const el = document.querySelector(sel);
    if (el) regions.push({ name, sel, nodes: count(el) });
  };
  push("左栏导航", "#sidenav");
  push("顶栏", "#topbar");
  push("二级横条", "#subnav");
  push("页面外壳", "#app");

  const panels = [];
  document.querySelectorAll('[id^="p-"]').forEach((el) => {
    panels.push({ id: el.id, nodes: count(el), active: el.classList.contains("active"),
      ready: el.getAttribute("data-ready") === "1", hidden: isHidden(el) });
  });
  panels.sort((a, b) => b.nodes - a.nodes);

  const overlays = [];
  document.querySelectorAll('.modal, .drawer, [id*="modal"], [id*="Modal"], .ov-mask').forEach((el) => {
    overlays.push({ id: el.id || el.className, nodes: count(el) });
  });
  overlays.sort((a, b) => b.nodes - a.nodes);

  const cands = [];
  const seen = new Set();
  const addCand = (el) => {
    if (!el || seen.has(el) || el === document.body) return;
    seen.add(el);
    const n = count(el);
    if (n < 150) return;
    cands.push({ id: el.id || "(no-id)", tag: el.tagName.toLowerCase(),
      cls: String(el.className || "").slice(0, 40), nodes: n, hidden: isHidden(el) });
  };
  Array.from(document.body.children).forEach(addCand);
  document.querySelectorAll('[id^="p-"]').forEach(addCand);
  document.querySelectorAll("[id]").forEach(addCand);
  cands.sort((a, b) => b.nodes - a.nodes);

  return {
    total: all.length,
    visible, hidden,
    regions,
    panels,
    panelSum: panels.reduce((s, p) => s + p.nodes, 0),
    panelCount: panels.length,
    overlays,
    overlaySum: overlays.reduce((s, o) => s + o.nodes, 0),
    top: cands.slice(0, 10),
    bodyChildren: document.body.children.length,
    scripts: document.querySelectorAll("script").length,
    styles: document.querySelectorAll("style,link[rel=stylesheet]").length,
    hash: location.hash,
  };
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("NO_BROWSER: 未找到 Edge/Chrome/Chromium，跳过（非失败）"); process.exit(3); }
  const browser = await puppeteer.launch({
    executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.goto(URL + HASH, { waitUntil: "domcontentloaded", timeout: 60000 });

  /* 收敛采样：直到「连续 STABLE_N 次相同」或超时 */
  const series = [];
  let stable = 0;
  let lastN = -1;
  const t0 = Date.now();
  while (Date.now() - t0 < MAX_MS) {
    const n = await page.evaluate(() => document.querySelectorAll("*").length);
    series.push({ t: Math.round(Date.now() - t0), n });
    if (n === lastN) stable++; else { stable = 0; lastN = n; }
    if (stable >= STABLE_N - 1) break;
    await new Promise((r) => setTimeout(r, SAMPLE_MS));
  }
  const settled = series[series.length - 1];

  const m = await page.evaluate(measure);

  const L = [];
  L.push("==== DOM 称重（收敛采样）@ " + m.hash + " ====");
  L.push("采样：" + series.map((s) => s.t / 1000 + "s:" + s.n).join(" "));
  L.push("SETTLED@" + (settled.t / 1000).toFixed(1) + "s = " + settled.n +
    "（连续 " + STABLE_N + " 次不变，样本 " + series.length + " 个）");
  L.push("总节点数 querySelectorAll('*').length = " + m.total);
  L.push("  可见 " + m.visible + " / 隐藏 " + m.hidden + "（隐藏占比 " +
    ((m.hidden / m.total) * 100).toFixed(1) + "%）");
  L.push("  body 直接子节点 " + m.bodyChildren + " · script " + m.scripts + " · style/link " + m.styles);
  L.push("");
  L.push("---- ② 按容器分组 ----");
  m.regions.forEach((r) => L.push("  " + r.name.padEnd(10, " ") + " " + String(r.nodes).padStart(6) + "   " + r.sel));
  L.push("  面板容器    " + String(m.panelSum).padStart(6) + "   #p-* 共 " + m.panelCount + " 个");
  L.push("  弹层/浮层   " + String(m.overlaySum).padStart(6) + "   " + m.overlays.length + " 个");
  L.push("");
  L.push("---- 面板明细（降序，前 15）----");
  m.panels.slice(0, 15).forEach((p) => L.push("  " + p.id.padEnd(26, " ") +
    String(p.nodes).padStart(6) + (p.active ? "  [active]" : "") +
    (p.ready ? "  [ready]" : "") + (p.hidden ? "  [隐藏]" : "")));
  L.push("");
  L.push("---- ③ Top 10 容器 ----");
  m.top.forEach((c, i) => L.push("  " + String(i + 1).padStart(2) + ". " +
    (c.id !== "(no-id)" ? "#" + c.id : c.tag + "." + c.cls).padEnd(30, " ") +
    String(c.nodes).padStart(6) + (c.hidden ? "  [隐藏]" : "")));
  L.push("");
  L.push("JS_ERRORS=" + (errs.length ? errs.join(" | ") : "none"));
  console.log(L.join("\n"));

  const jsonAt = process.argv.indexOf("--json");
  if (jsonAt > 0 && process.argv[jsonAt + 1]) {
    fs.writeFileSync(process.argv[jsonAt + 1],
      JSON.stringify({ ...m, series, settled, jsErrors: errs }, null, 2), "utf8");
    console.log("\nJSON → " + process.argv[jsonAt + 1]);
  }
  await browser.close();
  process.exit(0);
})();
