/* 验证「方案 A」导航改造：左栏只留大类，子类回到内容区顶部横向平铺。
   零依赖：Node 22 内置 WebSocket 直连 Edge 的 CDP。 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const PORT = 9346;
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 必须用全新 profile：复用残留实例会截到旧帧（本项目踩过）
const profile = mkdtempSync(path.join(tmpdir(), "edge_subnav_"));
const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--window-size=1600,1000", "about:blank",
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 80; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const x = l.find((y) => y.type === "page" && y.webSocketDebuggerUrl);
      if (x) return x.webSocketDebuggerUrl;
    } catch {}
    await sleep(250);
  }
  throw new Error("CDP 不可达");
}
const ws = new WebSocket(await target());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pend = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
const send = (m, p = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
};
const shot = async (n) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, n), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + n);
};

const issues = [];
const check = (ok, msg) => { console.log(`  ${ok ? "[OK]  " : "[FAIL]"} ${msg}`); if (!ok) issues.push(msg); };

const jsErrors = [];
await send("Runtime.enable");
await send("Page.enable");
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") {
    jsErrors.push(String(m.params?.exceptionDetails?.text || "").slice(0, 160));
  }
});
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

await send("Page.navigate", { url: BASE + "#trade/consensus" });
await sleep(5200);

console.log("\n[1] 左栏：只应剩大类，不含子项");
const nav = await ev(`(() => {
  const side = document.getElementById('sidenav');
  const cats = [...side.querySelectorAll('.nav-item')];
  return {
    cats: cats.length,
    labels: cats.map(a => (a.querySelector('.ni-txt')||{}).textContent || ''),
    hasSubInRail: side.querySelectorAll('.sub-item, .nav-sub, .nav-group').length,
    railW: Math.round(side.getBoundingClientRect().width),
    railX: Math.round(side.getBoundingClientRect().left),
  };
})()`);
console.log(`  左栏 ${nav.cats} 个大类：${nav.labels.join(" / ")}`);
check(nav.hasSubInRail === 0, "左栏内不再有任何子项容器（.sub-item/.nav-sub/.nav-group 均为 0）");
check(nav.railX === 0, `左栏仍贴住视口最左缘（x=${nav.railX}）`);

console.log("\n[2] 内容区顶部：横向子项条");
const sub = await ev(`(() => {
  const bar = document.getElementById('subnav');
  if (!bar) return null;
  const items = [...bar.querySelectorAll('.sub-tab')];
  const tops = new Set(items.map(i => Math.round(i.getBoundingClientRect().top)));
  const fdir = items.length ? getComputedStyle(items[0].parentElement).flexDirection : '';
  return {
    found: true, n: items.length,
    labels: items.map(i => i.textContent.trim()),
    rows: tops.size, flex: fdir,
    active: (bar.querySelector('.sub-tab.active')||{}).textContent?.trim() || null,
    barTop: Math.round(bar.getBoundingClientRect().top),
    barH: Math.round(bar.getBoundingClientRect().height),
    contentTop: Math.round(document.querySelector('.content').getBoundingClientRect().top),
  };
})()`);
check(!!sub && sub.found, "内容区顶部存在横向子项条 #subnav");
if (sub && sub.found) {
  console.log(`  ${sub.n} 个子项（${sub.rows} 行）：${sub.labels.join(" / ")}`);
  check(sub.flex === "row", `子项为横向排列（flex-direction=${sub.flex}）`);
  check(sub.rows === 1, `子项单行平铺（实测 ${sub.rows} 行；1600px 下应当 1 行）`);
  check(sub.n >= 2, `子项数量 ≥2（实测 ${sub.n}）`);
  check(sub.active === "共同持仓共识", `当前子项高亮正确（active=${sub.active}）`);
  check(sub.barTop >= sub.contentTop, "子项条落在内容区内（不在左栏）");
}

console.log("\n[3] 逐个横项点击 → 面板切换");
for (const [want, panel] of [["正在操作股票", "p-operating"], ["实盘选手(渠道)", "p-players"], ["近5日共同买入", "p-cohold"]]) {
  const r = await ev(`(async () => {
    const a = [...document.querySelectorAll('#subnav .sub-tab')].find(x => x.textContent.trim() === ${JSON.stringify(want)});
    if (!a) return { err: 'not found' };
    a.click();
    await new Promise(r => setTimeout(r, 1500));
    return { hash: location.hash, active: document.querySelector('.panel.active')?.id || '' };
  })()`);
  if (r.err) { check(false, `点击「${want}」→ 未找到该项`); continue; }
  check(r.active === panel, `点击「${want}」→ ${r.active}（hash=${r.hash}）`);
}

console.log("\n[4] 大类切换 → 横条换一批子项");
for (const [cat, first] of [["overview", "汇总"], ["research", "当前讨论股票"], ["learn", "中短线炒股"]]) {
  const r = await ev(`(async () => {
    const a = document.querySelector('#sidenav .nav-item[data-cat="${cat}"]');
    if (!a) return { err: 'no cat' };
    a.click();
    await new Promise(r => setTimeout(r, 1800));
    const items = [...document.querySelectorAll('#subnav .sub-tab')].map(x => x.textContent.trim());
    return { items, active: document.querySelector('.sub-tab.active')?.textContent.trim() || '' };
  })()`);
  if (r.err) { check(false, `切到 ${cat} 失败`); continue; }
  check(r.items[0] === first, `切到「${cat}」→ 横条首项为「${r.items[0]}」（期望 ${first}）`);
}

console.log("\n[5] 学习类：知识库状态控件仍在（原在左栏底部）");
const kb = await ev(`(() => ({
  chip: !!document.getElementById('kbStateChip'),
  reload: !!document.getElementById('btnKbReload'),
  chipText: (document.getElementById('kbStateChip')||{}).textContent || '',
}))()`);
console.log(`  知识库 chip：${kb.chipText}`);
check(kb.chip && kb.reload, "「知识库状态」与「立即吸取」控件仍在（未随左栏改造丢失）");

console.log("\n[6] 布局：无横向溢出、内容区满宽");
const lay = await ev(`(() => {
  const c = document.querySelector('.content');
  const r = c.getBoundingClientRect();
  return { doc: document.documentElement.scrollWidth, vw: window.innerWidth,
           contentW: Math.round(r.width), contentX: Math.round(r.left),
           railW: Math.round(document.getElementById('sidenav').getBoundingClientRect().width) };
})()`);
check(lay.doc <= lay.vw + 1, `无横向溢出（${lay.doc} ≤ ${lay.vw}）`);
check(Math.abs(lay.contentX - lay.railW) <= 2, `内容区紧接左栏（x=${lay.contentX}, rail=${lay.railW}）`);

await ev(`window.scrollTo(0,0)`); await sleep(500);
await shot("nav_h_01_subnav.png");
await ev(`(() => { const a=document.querySelector('#sidenav .nav-item[data-cat="overview"]'); a && a.click(); })()`);
await sleep(1500); await ev(`window.scrollTo(0,0)`); await sleep(400);
await shot("nav_h_02_overview.png");

console.log("\n[7] JS 异常：" + jsErrors.length);
check(jsErrors.length === 0, "零 JS 异常");
for (const e of jsErrors.slice(0, 4)) console.log("   js>", e);

console.log("\n" + "=".repeat(56));
console.log(issues.length === 0 ? "ALL PASSED" : `FAILED：${issues.length} 项`);
for (const i of issues) console.log("  -", i);
ws.close(); edge.kill();
process.exit(issues.length ? 1 : 0);
