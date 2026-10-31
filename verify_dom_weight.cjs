/* verify_dom_weight.cjs —— 首页 DOM 节点数称重（2026-11-01 P1-2 第 3 项）
 *
 * 目的：DOM 瘦身必须**先测量再动手**。此脚本只测量、不修改任何代码，
 *       输出三张表：
 *         ① 总量：document.querySelectorAll('*').length
 *         ② 按容器分组（导航区 / 顶栏 / 横条 / 各面板骨架 / 弹层 / 隐藏节点）
 *         ③ Top 10 容器明细（子树节点数降序）—— 优化只针对这里的前 2~3 名
 *
 * 之所以要"按容器分组"而不是只看总数：总数 2 万时，是"一个面板 1.5 万"还是
 * "40 个面板各 500"决定完全不同的改法。前者该延迟渲染，后者该改骨架。
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

/* 页面内执行：全部用原生 DOM，避免依赖 app.js 的内部状态。 */
function measure() {
  const all = document.querySelectorAll("*");
  const total = all.length;

  const count = (root) => (root ? root.querySelectorAll("*").length + 1 : 0);

  // 可见性：自身不可见且祖先链上有一个 display:none → 算隐藏
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

  // ② 固定区域分组
  const regions = [];
  const push = (name, sel) => {
    const el = document.querySelector(sel);
    if (el) regions.push({ name, sel, nodes: count(el) });
  };
  push("左栏导航", "#sidenav");
  push("顶栏", "#topbar");
  push("顶栏(alt)", "header");
  push("二级横条", "#subnav");
  push("页面外壳", "#app");
  push("主内容区", "#main");
  push("主内容区(alt)", "main");

  // 面板骨架：所有 #p-* 容器
  const panels = [];
  document.querySelectorAll('[id^="p-"]').forEach((el) => {
    panels.push({ id: el.id, nodes: count(el), active: el.classList.contains("active"), hidden: isHidden(el) });
  });
  panels.sort((a, b) => b.nodes - a.nodes);

  // 弹层 / 浮层（.modal / .drawer / [id*="modal"] / .ov-*）
  const overlays = [];
  document.querySelectorAll('.modal, .drawer, [id*="modal"], [id*="Modal"], .ov-mask').forEach((el) => {
    if (el.__seen) return;
    el.__seen = true;
    overlays.push({ id: el.id || el.className, nodes: count(el) });
  });
  all.forEach((el) => { delete el.__seen; });
  overlays.sort((a, b) => b.nodes - a.nodes);

  // ③ Top 10 容器：以「有 id 的后代整体」为候选，避免把每个 <div> 都列一遍。
  //    取候选 = body > * 的整棵树 + 每个 #p-* 面板 + 每个有 id 且子树 > 200 的节点。
  const cands = [];
  const seen = new Set();
  const addCand = (el) => {
    if (!el || seen.has(el) || el === document.body) return;
    seen.add(el);
    const n = count(el);
    if (n < 150) return;
    cands.push({
      id: el.id || "(no-id)",
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || "").slice(0, 40),
      nodes: n,
      hidden: isHidden(el),
    });
  };
  Array.from(document.body.children).forEach(addCand);
  document.querySelectorAll('[id^="p-"]').forEach(addCand);
  document.querySelectorAll("[id]").forEach(addCand);
  cands.sort((a, b) => b.nodes - a.nodes);

  // 去重：子容器被父容器完全包含时不重复列（父节点数 ≥ 子节点数且 id 前缀相同的不算）
  const top = [];
  for (const c of cands) {
    if (top.length >= 10) break;
    top.push(c);
  }

  return {
    total,
    visible,
    hidden,
    regions,
    panels,
    panelSum: panels.reduce((s, p) => s + p.nodes, 0),
    panelCount: panels.length,
    overlays,
    overlaySum: overlays.reduce((s, o) => s + o.nodes, 0),
    top,
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
    executablePath: exe,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.goto(URL + HASH, { waitUntil: "domcontentloaded", timeout: 60000 });
  // 等首页五要素渲染完（td-status / td-health 挂载后被填充），最多等 12s 不抛错
  try { await page.waitForFunction(() => {
    const el = document.querySelector("#td-status");
    return el && el.children.length > 0;
  }, { timeout: 12000 }); } catch (e) { /* 允许超时，仍测量当前状态 */ }
  try { await new Promise((r) => setTimeout(r, 2500)); } catch (e) {}

  const m = await page.evaluate(measure);

  const L = [];
  L.push("==== DOM 称重 @ " + m.hash + " ====");
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
  L.push("---- 面板明细（降序，前 15） ----");
  m.panels.slice(0, 15).forEach((p) => L.push("  " + p.id.padEnd(26, " ") +
    String(p.nodes).padStart(6) + (p.active ? "  [active]" : "") + (p.hidden ? "  [隐藏]" : "")));
  L.push("");
  L.push("---- ③ Top 10 容器 ----");
  m.top.forEach((c, i) => L.push("  " + String(i + 1).padStart(2) + ". " +
    (c.id !== "(no-id)" ? "#" + c.id : c.tag + "." + c.cls).padEnd(30, " ") +
    String(c.nodes).padStart(6) + (c.hidden ? "  [隐藏]" : "")));
  L.push("");
  L.push("JS_ERRORS=" + (errs.length ? errs.join(" | ") : "none"));
  const out = L.join("\n");
  console.log(out);

  const jsonAt = process.argv.indexOf("--json");
  if (jsonAt > 0 && process.argv[jsonAt + 1]) {
    fs.writeFileSync(process.argv[jsonAt + 1], JSON.stringify({ ...m, jsErrors: errs }, null, 2), "utf8");
    console.log("\nJSON → " + process.argv[jsonAt + 1]);
  }
  await browser.close();
  process.exit(0);
})();
