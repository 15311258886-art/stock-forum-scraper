/* 布局体检（2026-09-23 改造后版）：量化「左右空白是否被用起来」「导航是否贴左」「筛选栏是否竖排」。
   改前实测基线：.wrap 居中 1280 → 1920 视口两侧各空 320px（33%）、卡片恒为 164px、sidenav x=342。
   本脚本对每一项给出 PASS/FAIL，截图只作辅助，不靠肉眼判断。 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const PORT = 9336;
const outDir = process.argv[2] || ".";
mkdirSync(outDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "edgely_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fails = [];
const check = (ok, msg) => { console.log(`  [${ok ? "OK" : "FAIL"}] ${msg}`); if (!ok) fails.push(msg); };

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

await send("Page.enable"); await send("Runtime.enable");
await sleep(3200);

const MEASURE = `(() => {
  const R = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.x), w: Math.round(r.width), right: Math.round(r.right),
             h: Math.round(r.height), y: Math.round(r.y) }; };
  const CS = (s, p) => { const e = document.querySelector(s); return e ? getComputedStyle(e)[p] : null; };
  const cols = (CS("#cards","gridTemplateColumns") || "").split(" ").filter(Boolean);
  return {
    vw: window.innerWidth,
    docScrollW: document.documentElement.scrollWidth,
    shell: R(".shell"), shellCols: CS(".shell","gridTemplateColumns"),
    sidenav: R(".sidenav"), sidenavPos: CS(".sidenav","position"),
    content: R(".content"),
    // 注意必须限定 #cards：知识获取三个面板内部也各有一组 .cards/.card，
    // 用文档级 ".cards > .card" 会把隐藏面板里的卡片一起数进来（本次就误报成 15 张）。
    cards: R("#cards"), cardCount: document.querySelectorAll("#cards > .card").length,
    cardLabels: [...document.querySelectorAll("#cards > .card")].map(e => (e.querySelector(".l") || {}).textContent || ""),
    cardCols: cols.length, cardW: cols.length ? Math.round(parseFloat(cols[0])) : 0,
    navGroups: document.querySelectorAll(".sidenav .nav-group").length,
    navItems: document.querySelectorAll(".sidenav .nav-item").length,
    openGroups: document.querySelectorAll(".sidenav .nav-group .nav-sub").length,
    subItems: [...document.querySelectorAll(".sidenav .sub-item")].map(e => e.textContent.trim()),
    subnavGone: !document.querySelector("#subnav"),
    railFoot: !!document.querySelector(".rail-foot"),
    topbarH: Math.round((document.querySelector(".topbar")||{getBoundingClientRect:()=>({height:0})}).getBoundingClientRect().height),
  };
})()`;

console.log("========== A. 宽屏：空白是否被用起来、导航是否贴左 ==========");
const wide = [[1280, false], [1440, false], [1680, false], [1920, false], [2560, false]];
for (const [w] of wide) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: 950, deviceScaleFactor: 1, mobile: false });
  await sleep(700);
  const m = await ev(MEASURE);
  const railX = m.sidenav.x;
  const rightGap = m.vw - m.content.right;
  console.log(`\n[视口 ${w}]`);
  console.log(`  .shell 列=${m.shellCols}   .sidenav x=${m.sidenav.x} 宽=${m.sidenav.w} (${m.sidenavPos})`);
  console.log(`  .content x=${m.content.x} 宽=${m.content.w} 右留白=${rightGap}px`);
  console.log(`  #cards ${m.cardCount} 张 → ${m.cardCols} 列 × ${m.cardW}px  [${m.cardLabels.slice(0, 3).join(",")}…]`);
  console.log(`  导航：${m.navGroups} 个大类 / ${m.navItems} 项，当前展开 ${m.openGroups} 组，子类 ${m.subItems.length} 个`);
  check(m.docScrollW <= m.vw + 1, `${w}：无横向溢出（docScrollW=${m.docScrollW} ≤ vw=${m.vw}）`);
  check(railX === 0, `${w}：导航栏贴住视口最左缘（x=${railX}）`);
  check(Math.abs(m.sidenav.w - 236) <= 2, `${w}：导航栏宽 236px（实测 ${m.sidenav.w}）`);
  // 内容区右缘只应剩内边距（24px），改前两侧各空 320px（1920 视口）
  check(rightGap <= 26, `${w}：内容区铺满剩余宽度，右留白 ${rightGap}px（改前 1920 视口各空 320px）`);
  check(m.content.x === m.sidenav.w, `${w}：内容区紧接导航栏右侧（x=${m.content.x}）`);
  /* 统计条宽度指标的变更说明（2026-09-23 二次改造）：
     原断言是「卡片宽 ≥190px」，那是「放大卡片」诉求在**允许换行**前提下的指标。
     后来新增硬要求「单排不换行」——9 张卡恒定 1 行 → 卡宽必然由 9 列均分决定
     （103~240px），两条要求**互斥**，不能同时满足。
     取舍：以「单排不换行」为准（用户后提的、更明确），可读性改由
     ①字号下限 clamp(15px,…) ②无标签被裁剪 —— 两项均在 tools/ui_probe_cards.mjs 中严格断言。
     这里保留「不能被压到不可用」的下限 + 「列数必须等于卡片数」，防未来静默压扁或又折行。 */
  check(m.cardCols === m.cardCount,
    `${w}：统计卡列数=卡片数，即恒定单排（${m.cardCols} 列 / ${m.cardCount} 张）`);
  check(m.cardW >= 80, `${w}：统计卡宽 ≥80px 可用下限（实测 ${m.cardW}；字号与裁剪另见 ui_probe_cards）`);
  check(m.subnavGone, `${w}：内容区不再有横向子类条（#subnav 已移除）`);
  check(m.openGroups === 1, `${w}：左栏手风琴只展开当前大类（展开 ${m.openGroups} 组）`);
  check(m.subItems.length >= 1, `${w}：当前大类的子类已在左栏竖向列出（${m.subItems.length} 项）`);
}

console.log("\n========== B. 各子板块仍可达（大类 → 子类切换）===========");
await send("Emulation.setDeviceMetricsOverride", { width: 1680, height: 950, deviceScaleFactor: 1, mobile: false });
await sleep(600);
await send("Page.navigate", { url: BASE });   // 回到无 hash 初始态
await sleep(3200);
const cats = await ev(`[...document.querySelectorAll('.sidenav .nav-item')].map(e => ({ k: e.dataset.cat, t: e.textContent.trim().replace(/\\s+/g,' ') }))`);
console.log("  左栏大类：", cats.map((c) => c.t).join(" ｜ "));
for (const c of cats) {
  await ev(`document.querySelector('.sidenav .nav-item[data-cat="${c.k}"]').click()`);
  await sleep(900);
  const subs = await ev(`[...document.querySelectorAll('.sidenav .nav-group .nav-sub .sub-item')].map(e => e.dataset.tab)`);
  const active = await ev(`(document.querySelector('.panel.active')||{}).id || ''`);
  let okAll = true;
  for (const s of subs) {
    await ev(`document.querySelector('.sidenav .sub-item[data-tab="${s}"]').click()`);
    await sleep(800);
    const st = await ev(`({ panel: (document.querySelector('.panel.active')||{}).id || '',
      hash: location.hash, railActive: (document.querySelector('.sidenav .sub-item.active')||{}).textContent || '' })`);
    if (st.panel !== "p-" + s) { okAll = false; console.log(`       ✗ ${s} → 面板 ${st.panel}，hash=${st.hash}`); }
  }
  console.log(`  ${c.t}：${subs.length} 个子类，panel 切换 ${okAll ? "全对" : "有错"}（进入时面板 ${active}）`);
  check(subs.length >= 1 && okAll, `${c.t}：${subs.length} 个子类全部可切换且面板对应正确`);
}

console.log("\n========== C. 筛选栏：横排 → 左列竖排 ==========");
await ev(`location.hash = '#trade/players'`);
await sleep(2200);
const f = await ev(`(() => {
  const col = document.querySelector("#p-players .filter-col");
  if (!col) return { none: true };
  const btns = [...col.querySelectorAll(".fbtn")].map(b => {
    const r = b.getBoundingClientRect();
    return { t: b.textContent.trim().replace(/\\s+/g,' '), x: Math.round(r.x), y: Math.round(r.y),
             w: Math.round(r.width), h: Math.round(r.height) };
  });
  const rowsTotal = document.querySelectorAll("#p-players tbody tr").length;
  const colR = col.getBoundingClientRect();
  return { none: false, btns, rowsTotal,
    colW: Math.round(colR.width), colX: Math.round(colR.x),
    tableX: Math.round(document.querySelector("#p-players table").getBoundingClientRect().x),
    tierRow: !!document.querySelector("#p-players tbody tr[data-tier]") };
})()`);
if (f.none) {
  check(false, "实盘选手面板存在竖向筛选列 .filter-col");
} else {
  console.log(`  筛选列 x=${f.colX} 宽=${f.colW}，共 ${f.btns.length} 项；表格 x=${f.tableX}（筛选列在表格左侧）`);
  const ys = [...new Set(f.btns.map((b) => b.y))];
  const minH = Math.min(...f.btns.map((b) => b.h));
  const minW = Math.min(...f.btns.map((b) => b.w));
  console.log(`  每项尺寸：宽 ${minW}~${Math.max(...f.btns.map((b) => b.w))}px，高 ${minH}~${Math.max(...f.btns.map((b) => b.h))}px；不同纵向位置 ${ys.length} 个（共 ${f.btns.length} 项）`);
  check(f.btns.length >= 11, `筛选项完整（实测 ${f.btns.length} 项：全部层级+6 层级+全部渠道+4 渠道）`);
  check(ys.length === f.btns.length, `每项独占一行（${ys.length} 个不同 y / ${f.btns.length} 项）`);
  check(minH >= 32, `单项可点高度 ≥32px（实测最小 ${minH}px，改前约 27px）`);
  check(minW >= 150, `单项可点宽度 ≥150px（实测最小 ${minW}px，改前约 60px）`);
  check(f.tableX > f.colX + f.colW - 4, `筛选列与表格左右并排（筛选列 ${f.colX}+${f.colW} < 表格 ${f.tableX}）`);

  // 交互：点一个层级筛选项，行必须真的被过滤且高亮落到该项
  const one = await ev(`(() => {
    const b = document.querySelector('#p-players .filter-col .fbtn[data-tier="A"]');
    if (!b) return null;
    const before = document.querySelectorAll("#p-players tbody tr").length;
    b.click();
    const vis = [...document.querySelectorAll("#p-players tbody tr")].filter(t => t.style.display !== "none").length;
    return { before, vis,
      active: (document.querySelector("#p-players .filter-col .fbtn.active")||{}).textContent || "",
      activeAll: document.querySelectorAll("#p-players .filter-col .fbtn.active").length };
  })()`);
  if (one) {
    console.log(`  点「${one.active}」后：可见行 ${one.vis} / 共 ${one.before} 行，高亮项 ${one.activeAll} 个`);
    check(one.vis > 0 && one.vis < one.before, `点击筛选项真的过滤了行（${one.vis} < ${one.before}）`);
    check(one.activeAll === 2, `两级筛选各保留一个高亮（层级 1 + 渠道 1 = ${one.activeAll}）`);
  }
  await ev(`document.querySelector('#p-players .filter-col .fbtn[data-tier="ALL"]').click()`);
  await sleep(400);
  const restored = await ev(`[...document.querySelectorAll("#p-players tbody tr")].filter(t => t.style.display !== "none").length`);
  check(restored === f.rowsTotal, `点「全部层级」后行数复原（${restored}/${f.rowsTotal}）`);
}

console.log("\n========== D. 窄屏（≤1080）：左栏折回顶部横向 ==========");
for (const w of [1080, 880, 640]) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(900);
  const m = await ev(MEASURE);
  const flexDir = await ev(`getComputedStyle(document.querySelector(".sidenav")).flexDirection`);
  console.log(`\n[视口 ${w}] 左栏 flex-direction=${flexDir} x=${m.sidenav.x} 宽=${m.sidenav.w} 高=${m.sidenav.h}`);
  console.log(`  .shell 列=${m.shellCols}  .content 宽=${m.content.w}  .cards ${m.cardCols} 列 × ${m.cardW}px`);
  check(m.docScrollW <= m.vw + 1, `${w}：无横向溢出`);
  check(flexDir === "row", `${w}：左栏折叠为横向（flex-direction=row）`);
  check(m.vw - m.content.right <= 18, `${w}：内容区仍铺满宽度（右留白 ${m.vw - m.content.right}px）`);
}

console.log("\n========== E. 截图 ==========");
await send("Emulation.setDeviceMetricsOverride", { width: 1680, height: 950, deviceScaleFactor: 1, mobile: false });
await ev(`location.hash = '#overview/summary'`);
await sleep(2600);
await shot("lay_01_summary_1680.png");
await ev(`location.hash = '#trade/players'`);
await sleep(2600);
await shot("lay_02_players_1680.png");
await send("Emulation.setDeviceMetricsOverride", { width: 2560, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(1600);
await ev(`location.hash = '#overview/summary'`);
await sleep(2200);
await shot("lay_03_summary_2560.png");
await send("Emulation.setDeviceMetricsOverride", { width: 880, height: 950, deviceScaleFactor: 1, mobile: false });
await sleep(1600);
await ev(`location.hash = '#learn/learn_zdx_stock'`);
await sleep(2600);
await shot("lay_04_narrow_880.png");

edge.kill();
console.log(`\n========== 结果：${fails.length ? fails.length + " 项 FAIL" : "全部通过"} ==========`);
for (const f of fails) console.log("  FAIL " + f);
