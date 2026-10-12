/* 「横向留白过大 / 单列纵向排列」通用检测器（2026-09-23）
   诉求：凡单列纵向排列、右侧大片留空的板块，统一改为多列并排。
   做法不靠肉眼：逐个面板走一遍，找出「同类子元素 >=4 个、且被排成单列、且自身宽度明显小于可用宽度」的容器。
   这类容器就是可以改成多列的目标。输出「区块选择器 / 子元素数 / 自身宽 / 可用宽 / 占比 / 当前列数」。
   用法：node tools/ui_audit_columns.mjs <截图输出目录>            */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const PORT = 9341;
const outDir = process.argv[2] || ".";
mkdirSync(outDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "edgcol_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 与 app.js 的 NAV 配置一致（cat/tab）
const TABS = [
  ["overview", "summary", "总览·汇总"],
  ["acquire", "sources", "知识获取·数据源"],
  ["acquire", "archive", "知识获取·原始存档"],
  ["acquire", "fetchlog", "知识获取·抓取与登录记录"],
  ["research", "stocks", "行情研判·当前讨论股票"],
  ["research", "sentiment", "行情研判·舆论情绪"],
  ["research", "topics", "行情研判·近期热点"],
  ["research", "overseas_sentiment", "行情研判·美股港股舆情"],
  ["research", "overseas_hotspots", "行情研判·国外驱动热点"],
  ["trade", "operating", "实盘操作·正在操作股票"],
  ["trade", "players", "实盘操作·实盘选手"],
  ["trade", "consensus", "实盘操作·共同持仓共识"],
  ["trade", "cohold", "实盘操作·近5日共同买入"],
  ["trade", "persons", "实盘操作·相关人物"],
  ["learn", "learn_zdx_stock", "跟我学习·中短线炒股"],
  ["learn", "learn_tracks", "跟我学习·学习轨道管理"],
];

const edge = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--hide-scrollbars", "--window-size=1600,1000", BASE,
], { stdio: "ignore" });

async function targetWs() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const t = list.find((x) => x.type === "page" && x.webSocketDebuggerUrl);
      if (t) return t.webSocketDebuggerUrl;
    } catch {}
    await sleep(250);
  }
  throw new Error("CDP 未就绪");
}
const ws = new WebSocket(await targetWs());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pend = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
async function ev(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, name), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + name);
}
await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(3400);

/* 检测逻辑：只看当前激活面板。
   「可多列化」候选 = 同类子元素 >=4 个 + 内容排成单列 + 自身宽度 < 可用宽度 * 0.72 */
const DETECT = `(() => {
  const panel = document.querySelector('.panel.active');
  if (!panel) return null;
  const pr = panel.getBoundingClientRect();
  const W = pr.width;
  const pathOf = (el) => {
    const parts = []; let cur = el, d = 0;
    while (cur && cur !== panel && d < 5) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) s += '#' + cur.id;
      else if (typeof cur.className === 'string' && cur.className.trim()) {
        s += '.' + cur.className.trim().split(/\\s+/).slice(0, 2).join('.');
      }
      parts.unshift(s); cur = cur.parentElement; d++;
    }
    return parts.join(' > ');
  };
  const vis = (e) => getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 1;
  const out = [];
  for (const el of panel.querySelectorAll('*')) {
    const tag = el.tagName;
    if (tag === 'THEAD' || tag === 'TBODY' || tag === 'TR' || tag === 'TABLE') continue;
    if (!vis(el)) continue;
    const kids = [...el.children].filter(vis);
    if (kids.length < 4) continue;
    // 同类兄弟占比 >= 0.7（避免把「标题+正文」这种混排当成列表）
    const cnt = {}; kids.forEach(k => { cnt[k.tagName + '|' + (k.className || '')] = (cnt[k.tagName + '|' + (k.className || '')] || 0) + 1; });
    const top = Math.max(...Object.values(cnt));
    if (top / kids.length < 0.7) continue;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const krs = kids.map(k => k.getBoundingClientRect());
    const xs = new Set(krs.map(k => Math.round(k.x / 8)));      // 桶化，容 8px 误差
    const ys = new Set(krs.map(k => Math.round(k.y / 8)));
    const singleCol = xs.size <= 2 && ys.size >= Math.ceil(kids.length * 0.7);
    const gtc = (cs.gridTemplateColumns || '').split(' ').filter(Boolean).length;
    out.push({
      sel: pathOf(el), tag: tag.toLowerCase(), kids: kids.length,
      w: Math.round(r.width), avail: Math.round(W), ratio: +(r.width / W).toFixed(3),
      display: cs.display, gtc, singleCol,
      kidW: Math.round(krs[0].width),
      rightGap: Math.round(W - (r.right - pr.left)),
      txt: (el.textContent || '').replace(/\\s+/g, ' ').slice(0, 46),
    });
  }
  // 同一选择器去重；按「占比」升序（越小＝浪费越多）
  const m = new Map();
  for (const o of out) if (!m.has(o.sel)) m.set(o.sel, o);
  return { panelId: panel.id, W: Math.round(W), blocks: [...m.values()].sort((a, b) => a.ratio - b.ratio) };
})()`;

console.log("=========== 「单列 + 横向留白过大」候选区块 ===========");
console.log("判定：同类子元素 ≥4 + 排成单列 + 自身宽 < 可用宽 × 0.72\n");
const offenders = [];
for (const [cat, tab, label] of TABS) {
  await ev(`location.hash = '#${cat}/${tab}'`);
  await sleep(1800);
  const d = await ev(DETECT);
  if (!d) { console.log(`- ${label}: 面板未渲染，跳过`); continue; }
  const hits = (d.blocks || []).filter((b) => b.singleCol && b.ratio < 0.72);
  console.log(`--- ${label}  (可用宽 ${d.W}px，候选 ${hits.length}) ---`);
  if (!hits.length) { console.log("      无（横向空间已用满）"); continue; }
  for (const h of hits.slice(0, 6)) {
    console.log(`      ratio=${String(h.ratio).padEnd(5)} ${String(h.w).padStart(4)}/${h.avail}px  ` +
      `${String(h.kids).padStart(2)}项×${h.kidW}px  display=${h.display} 列=${h.gtc}  gap=${h.rightGap}px`);
    console.log(`             ${h.sel}`);
    console.log(`             「${h.txt}…」`);
    offenders.push({ label, ...h });
  }
  // 取该面板最严重的两个拍图留证
  for (const h of hits.slice(0, 2)) {
    await ev(`(() => { const el = document.querySelector('.panel.active'); if (el) el.scrollIntoView({block:'start'}); window.scrollTo(0, 0); })()`);
    await sleep(300);
  }
}
console.log(`\n合计候选区块：${offenders.length} 个（跨 ${new Set(offenders.map(o => o.label)).size} 个面板）`);

// 给最严重的 3 处拍图
const top = offenders.slice(0, 3);
for (let i = 0; i < top.length; i++) {
  const o = top[i];
  const [cat, tab] = TABS.find(t => t[2] === o.label) || ["overview", "summary"];
  await ev(`location.hash = '#${cat}/${tab}'`);
  await sleep(2000);
  await ev(`(() => {
    const panel = document.querySelector('.panel.active');
    const el = [...panel.querySelectorAll('*')].find(e => {
      const rs = [...e.children].filter(c => getComputedStyle(c).display !== 'none');
      return rs.length >= 4 && Math.round(e.getBoundingClientRect().width) === ${o.w};
    });
    if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 150);
    else window.scrollTo(0, 0);
  })()`);
  await sleep(500);
  await shot(`col_${String(i + 1).padStart(2, "0")}_${o.label.split("·")[1] || tab}.png`);
}
ws.close(); edge.kill(); process.exit(0);
