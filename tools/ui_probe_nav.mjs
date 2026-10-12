// 用本机 Edge（无头）+ CDP 驱动真实页面，验证「二级导航（左侧大类 + 顶部子类）」
// 与「跟我学习 → 中短线炒股」知识库板块。只依赖 Node 22 内置 fetch / WebSocket。
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CDP_PORT = 9223;
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
fs.mkdirSync(OUT, { recursive: true });
const PROFILE = path.join(OUT, "_edgeprofile_nav");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
const check = (cond, msg) => {
  console.log((cond ? "  [OK] " : "  [FAIL] ") + msg);
  if (!cond) fails.push(msg);
};

const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${PROFILE}`,
  "--window-size=1600,1400", "about:blank",
], { stdio: "ignore" });

async function cdpTarget() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const p = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (p) return p.webSocketDebuggerUrl;
    } catch { /* 未就绪 */ }
    await sleep(300);
  }
  throw new Error("CDP target 不可达");
}

const ws = new WebSocket(await cdpTarget());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let seq = 0;
const waiters = new Map();
const jsIssues = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails || {};
    jsIssues.push("EXCEPTION: " + (d.exception?.description || d.text));
  }
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) {
    jsIssues.push(m.params.type.toUpperCase() + ": " +
      m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq; waiters.set(id, res); ws.send(JSON.stringify({ id, method, params }));
});
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { returnByValue: true, awaitPromise: true, expression: expr });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
};
const shot = async (name) => {
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("saved", name);
};

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
  { width: 1600, height: 1400, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: BASE });
await sleep(4500);

console.log("\n[1] 导航骨架（左栏贴左 + 手风琴展开）");
const nav = await evalJs(`(() => {
  const items = [...document.querySelectorAll('#sidenav .nav-item')];
  return {
    catCount: items.length,
    labels: items.map((a) => a.querySelector('.ni-txt').textContent),
    counts: items.map((a) => a.querySelector('.ni-n').textContent),
    activeCat: document.querySelector('#sidenav .nav-item.active')?.dataset.cat ?? null,
    subCat: document.querySelector('#sidenav .nav-item.active .ni-txt')?.textContent ?? null,
    subItems: [...document.querySelectorAll('#sidenav .nav-sub .sub-item')].map((a) => a.textContent),
    activeSub: document.querySelector('#sidenav .nav-sub .sub-item.active')?.dataset.tab ?? null,
    activePanel: document.querySelector('.panel.active')?.id ?? null,
    hash: location.hash,
  };
})()`);
check(nav.catCount === 5, `左侧大类 ${nav.catCount} 个（期望 5）：${nav.labels.join(" / ")}`);
check(nav.labels.join(",") === "总览,知识获取,行情研判,实盘操作,跟我学习",
  `大类名称与规划一致：${nav.labels.join(" / ")}`);
check(nav.subCat === "总览" && nav.subItems.length === 1 && nav.activeSub === "summary",
  `默认落在「总览 → 汇总」（子类 ${JSON.stringify(nav.subItems)}）`);
check(nav.activePanel === "p-summary", `默认面板 ${nav.activePanel}`);
check(nav.hash === "#overview/summary", `默认深链接 ${nav.hash}`);
await shot("nav_01_overview.png");

console.log("\n[2] 逐大类切换：左栏子类随大类展开");
const cats = [
  ["acquire", "知识获取", ["数据源", "原始存档", "抓取与登录记录"]],
  ["research", "行情研判", ["当前讨论股票", "舆论情绪", "近期热点", "美股港股舆情", "国外驱动热点"]],
  ["trade", "实盘操作", ["正在操作股票", "实盘选手(渠道)", "共同持仓共识", "近5日共同买入", "相关人物"]],
  ["learn", "跟我学习", ["中短线炒股", "学习轨道管理"]],
];
for (const [key, label, expect] of cats) {
  await evalJs(`document.querySelector('#sidenav .nav-item[data-cat="${key}"]').click()`);
  await sleep(700);
  const got = await evalJs(`(() => ({
    subCat: document.querySelector('#sidenav .nav-item.active .ni-txt')?.textContent ?? null,
    items: [...document.querySelectorAll('#sidenav .nav-sub .sub-item')].map((a) => a.textContent),
    panel: document.querySelector('.panel.active')?.id ?? null,
    hash: location.hash,
  }))()`);
  check(got.subCat === label && got.items.join(",") === expect.join(","),
    `${label} → ${got.items.join(" / ")}`);
  check(got.panel && got.panel.startsWith("p-"),
    `${label} 面板已切换（${got.panel}，hash=${got.hash}）`);
}
await shot("nav_02_research.png");

console.log("\n[3] 知识获取三块内容");
await evalJs(`document.querySelector('#sidenav .nav-item[data-cat="acquire"]').click()`);
await sleep(600);
for (const [tab, need] of [["sources", "#p-sources table"], ["archive", "#p-archive table"], ["fetchlog", "#p-fetchlog"]]) {
  await evalJs(`document.querySelector('#sidenav .nav-sub .sub-item[data-tab="${tab}"]').click()`);
  await sleep(1800);
  const has = await evalJs(`!!document.querySelector('#p-${tab}').innerHTML.trim().length`);
  const tbl = await evalJs(`document.querySelectorAll('#p-${tab} table').length`);
  check(has, `${tab} 面板已渲染内容`);
  check(tbl >= 1, `${tab} 含 ${tbl} 张表（期望 ≥1）`);
  await shot(`nav_03_${tab}.png`);
}

console.log("\n[4] 跟我学习 → 中短线炒股（知识库）");
await send("Page.navigate", { url: BASE + "#learn/learn_zdx_stock" });
await sleep(6000);
const kb = await evalJs(`(() => {
  const p = document.querySelector('#p-learn_zdx_stock');
  return {
    hasHead: !!p.querySelector('.kb-head'),
    title: p.querySelector('.kb-head-l h2')?.textContent ?? null,
    meta: [...p.querySelectorAll('.kb-head-meta .chip-s')].map((c) => c.textContent),
    toc: p.querySelectorAll('.kb-toc-item').length,
    schools: p.querySelectorAll('.kb-school').length,
    bloggers: p.querySelectorAll('.kb-blogger').length,
    backtestTables: p.querySelectorAll('.kb-bt .kb-table').length,
    backtestRows: p.querySelectorAll('.kb-bt tbody tr').length,
    riskItems: p.querySelectorAll('.kb-risk').length,
    sourceRows: document.querySelectorAll('#kb-sec-sources tbody tr').length,
    empty: !!p.querySelector('.empty'),
    chip: document.querySelector('#kbStateChip')?.textContent ?? null,
    scrollAnchor: !!document.getElementById('kb-sec-risks'),
    secs: [...p.querySelectorAll('.kb-sec')].map((s) => s.id),
  };
})()`);
check(!kb.empty, "知识库面板非空状态（已吸取）");
check(kb.schools === 8, `渲染 ${kb.schools} 个流派卡片（期望 8）`);
check(kb.bloggers === 50, `渲染 ${kb.bloggers} 个博主入口（期望 50）`);
check(kb.backtestRows === 33, `回测表共 ${kb.backtestRows} 行（期望 33）`);
check(kb.backtestTables === 3, `回测分 ${kb.backtestTables} 张表（期望 3）`);
check(kb.riskItems === 5, `风险提示 ${kb.riskItems} 条（期望 5）`);
check(kb.sourceRows === 50, `信息来源 ${kb.sourceRows} 行（期望 50）`);
check(kb.toc >= 10, `目录 ${kb.toc} 项`);
check(/v5\.0/.test(kb.chip || ""), `横向栏状态chip：${kb.chip}`);
check(/v5\.0/.test((kb.meta || []).join(" ")), `头部元信息：${(kb.meta || []).join(" | ")}`);
console.log("   sections:", (kb.secs || []).join(", "));
await shot("nav_04_learn_kb.png");
await evalJs(`document.getElementById('kb-sec-schools').scrollIntoView()`);
await sleep(500);
await shot("nav_05_learn_schools.png");
await evalJs(`document.getElementById('kb-sec-backtests').scrollIntoView()`);
await sleep(500);
await shot("nav_06_learn_backtests.png");
await evalJs(`document.getElementById('kb-sec-risks').scrollIntoView()`);
await sleep(500);
await shot("nav_07_learn_risks.png");

console.log("\n[5] 博主档案抽屉");
await evalJs(`document.querySelector('#p-learn_zdx_stock .kb-blogger').click()`);
await sleep(900);
const dr = await evalJs(`(() => ({
  open: document.querySelector('#drawer').classList.contains('show'),
  title: document.querySelector('#drawerTitle').textContent,
  secs: [...document.querySelectorAll('#drawerBody .op-sec-h')].map((e) => e.textContent),
  hasVideo: !!document.querySelector('#drawerBody .kb-note-block'),
}))()`);
check(dr.open, `抽屉已打开（标题：${dr.title}）`);
check((dr.secs || []).length >= 2, `抽屉分段：${(dr.secs || []).join(" / ")}`);
check(!!dr.hasVideo, "抽屉含字段块（精读/视频笔记）");
await shot("nav_08_blogger_drawer.png");
await evalJs(`document.querySelector('#btnClose').click()`);
await sleep(400);
check(!(await evalJs(`document.querySelector('#drawer').classList.contains('show')`)), "抽屉可正常关闭");

console.log("\n[6] 学习轨道管理");
await evalJs(`document.querySelector('#sidenav .nav-sub .sub-item[data-tab="learn_tracks"]').click()`);
await sleep(1500);
const tr = await evalJs(`(() => {
  const p = document.querySelector('#p-learn_tracks');
  return {
    rows: p.querySelectorAll('tbody tr').length,
    headers: [...p.querySelectorAll('thead th')].map((t) => t.textContent),
    subItems: [...document.querySelectorAll('#sidenav .nav-sub .sub-item')].map((a) => a.textContent),
    body: p.innerText.slice(0, 200),
  };
})()`);
check(tr.rows >= 1, `轨道表 ${tr.rows} 行（列：${tr.headers.join(" / ")}）`);
check(tr.subItems.join(",") === "中短线炒股,学习轨道管理",
  `左栏子类由配置驱动：${tr.subItems.join(" / ")}`);
await shot("nav_09_learn_tracks.png");

console.log("\n[7] 响应式：1080px 断点（左栏折回顶部横向）");
await send("Emulation.setDeviceMetricsOverride",
  { width: 880, height: 1200, deviceScaleFactor: 1, mobile: false });
await sleep(900);
const resp = await evalJs(`(() => {
  const nav = document.querySelector('#sidenav');
  const items = [...document.querySelectorAll('#sidenav .nav-item')];
  const tops = items.map((a) => Math.round(a.getBoundingClientRect().top));
  return {
    flexDir: getComputedStyle(nav).flexDirection,
    sameRow: new Set(tops).size === 1,
    cols: getComputedStyle(document.querySelector('.shell')).gridTemplateColumns,
    railX: Math.round(nav.getBoundingClientRect().left),
    navWidth: Math.round(nav.getBoundingClientRect().width),
    docScrollW: document.documentElement.scrollWidth,
    winW: window.innerWidth,
  };
})()`);
check(resp.flexDir === "row", `左栏在窄屏转为横排（flex-direction=${resp.flexDir}）`);
check(resp.sameRow, "大类项落在同一行（顶部坐标一致）");
check(resp.railX === 0, `左栏贴住视口左缘（x=${resp.railX}）`);
check(resp.docScrollW <= resp.winW + 2, `无横向溢出（scrollWidth ${resp.docScrollW} ≤ ${resp.winW}）`);
await shot("nav_10_responsive_880.png");
await send("Emulation.setDeviceMetricsOverride",
  { width: 1600, height: 1400, deviceScaleFactor: 1, mobile: false });

console.log("\n[8] 旧单排 Tab 已移除、旧功能无回归");
const legacy = await evalJs(`(() => ({
  oldTabs: document.querySelectorAll('#tabs .tab').length,
  panels: document.querySelectorAll('.panel').length,
  // 2026-09-23：独立的 #refreshBar 容器已并入 .topbar（首屏占用 107px → 48px），
  // 故断言改为「刷新状态四件套是否都还在，且都在顶栏内」——查容器 id 会假失败。
  refreshBar: !!document.querySelector('#refreshBar'),
  refreshUiInTopbar: ['rsDot', 'rsText', 'rsCountdown', 'btnRefreshNow']
    .every((id) => !!document.querySelector('.topbar #' + id)),
  demoBtn: !!document.querySelector('#btnDemo'),
  panelsHaveContent: [...document.querySelectorAll('.panel')]
    .filter((p) => p.innerHTML.trim().length > 0).map((p) => p.id),
}))()`);
check(legacy.oldTabs === 0, "旧单排 #tabs 已移除");
check(legacy.panels >= 15, `面板容器 ${legacy.panels} 个（旧 10 + 知识获取 3 + 学习 2）`);
check(legacy.refreshUiInTopbar && !legacy.demoBtn, "Request G 的刷新状态四件套仍在（已并入顶栏）、载入示例仍为移除状态");
console.log("   有内容的面板：", (legacy.panelsHaveContent || []).join(", "));

console.log("\nJS_ISSUES " + JSON.stringify(jsIssues, null, 2));
ws.close();
edge.kill();
console.log("\n" + "=".repeat(56));
if (fails.length) { console.log(`FAILED: ${fails.length} 项`); fails.forEach((f) => console.log("  -", f)); }
else console.log("ALL PASSED —— 二级导航与知识库板块在真实浏览器中表现符合预期");
process.exit(fails.length || jsIssues.some((s) => s.startsWith("EXCEPTION")) ? 1 : 0);
