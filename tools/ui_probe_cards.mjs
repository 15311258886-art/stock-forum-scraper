/* 顶部统计卡片区（#cards）专项体检：单排不换行 + 内容随容器等比例缩放。
   改前基线（2026-09-23 实测）：9 张卡在 1000~1400px 内容宽下折成 2 行（7+2）；
   .n/.l/.more 字号为固定 26/12/11px，容器缩放时文字不跟着变。
   用法：node tools/ui_probe_cards.mjs <截图输出目录> */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const PORT = 9341;
const outDir = process.argv[2] || ".";
mkdirSync(outDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "edgcards_"));
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
await sleep(3500);

/* 取 #cards 的几何 + 排版指标。以 offsetTop 聚类判断"行数"，不靠列数推断。 */
const MEASURE = `(() => {
  const box = document.getElementById('cards');
  if (!box) return { err: 'no #cards' };
  const R = (e) => { const r = e.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height) }; };
  const cards = [...box.querySelectorAll('.card')];
  const tops = [...new Set(cards.map(c => Math.round(c.getBoundingClientRect().top)))].sort((a,b)=>a-b);
  const cs = getComputedStyle(box);
  const one = cards[0];
  const cso = one ? getComputedStyle(one) : {};
  const nEl = one && one.querySelector('.n'), lEl = one && one.querySelector('.l'), mEl = one && one.querySelector('.more');
  const fs = (e) => e ? parseFloat(getComputedStyle(e).fontSize) : null;
  // 标签是否被裁掉（单排时卡变窄，要确认文字没被切）
  const labels = cards.map(c => {
    const l = c.querySelector('.l');
    return { t: l ? l.textContent : '', clipped: l ? (l.scrollWidth > l.clientWidth + 1) : false };
  });
  return {
    boxW: R(box).w, boxScrollW: box.scrollWidth, boxClientW: box.clientWidth,
    cols: cs.gridTemplateColumns.split(' ').filter(Boolean).length,
    gridCols: cs.gridTemplateColumns,
    flow: cs.gridAutoFlow,
    n: cards.length, rows: tops.length,
    cardW: one ? R(one).w : 0, cardH: one ? R(one).h : 0,
    padT: cso.paddingTop, fontN: fs(nEl), fontL: fs(lEl), fontM: fs(mEl),
    overflowing: box.scrollWidth > box.clientWidth + 1,
    clippedLabels: labels.filter(x => x.clipped).map(x => x.t),
    labels: labels.map(x => x.t),
    contentW: document.querySelector('.content') ? R(document.querySelector('.content')).w : 0,
    docScrollW: document.documentElement.scrollWidth, vw: innerWidth,
  };
})()`;

const widths = [2560, 1920, 1680, 1440, 1280, 1100];
const seen = [];
for (const w of widths) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: 1000, deviceScaleFactor: 1, mobile: false });
  await sleep(900);
  const m = await ev(MEASURE);
  seen.push([w, m]);
  console.log(`\n===== 视口 ${w} =====`);
  console.log(`  #cards 宽 ${m.boxW} ｜ 内容区宽 ${m.contentW}`);
  console.log(`  ${m.n} 张卡 → ${m.cols} 列 × ${m.cardW}px ｜ 实际占 ${m.rows} 行 ｜ 卡高 ${m.cardH}px`);
  console.log(`  字号 .n=${m.fontN} / .l=${m.fontL} / .more=${m.fontM} ｜ 卡内边距上 ${m.padT}`);
  console.log(`  grid-auto-flow=${m.flow} ｜ 横向溢出=${m.overflowing}`);
  if (m.clippedLabels.length) console.log(`  被裁标签：${m.clippedLabels.join('、')}`);

  check(m.rows === 1, `${w}：统计卡单排不换行（实际 ${m.rows} 行：${m.cols} 列 × ${m.cardW}px）`);
  check(!m.overflowing, `${w}：#cards 无横向溢出（scrollW=${m.boxScrollW} ≤ clientW=${m.boxClientW}）`);
  check(m.clippedLabels.length === 0, `${w}：无标签被裁剪${m.clippedLabels.length ? "（" + m.clippedLabels.join("、") + "）" : ""}`);
}

/* 等比例缩放：容器变窄时字号必须跟着变小（固定 px 就会一模一样） */
console.log("\n===== 等比例缩放判定（字号随容器宽度变化）=====");
const w2 = seen.map(([w, m]) => [w, m.fontN, m.fontL]);
console.log("  视口 → .n 字号：" + w2.map(([w, f]) => `${w}:${f}px`).join("  "));
console.log("  视口 → .l 字号：" + seen.map(([w, m]) => `${w}:${m.fontL}px`).join("  "));
const uniqN = new Set(w2.map(x => x[1])).size;
const uniqL = new Set(seen.map(([, m]) => m.fontL)).size;
check(uniqN >= 3, `.n 字号随容器变化（出现 ${uniqN} 种取值，固定 px 只会是 1 种）`);
check(uniqL >= 2, `.l 字号随容器变化（出现 ${uniqL} 种取值）`);
const wide = seen.find(([w]) => w === 2560)[1], narrow = seen.find(([w]) => w === 1100)[1];
check(narrow.fontN < wide.fontN, `.n 在窄容器下更小（2560:${wide.fontN}px → 1100:${narrow.fontN}px）`);
check(narrow.fontL <= wide.fontL, `.l 在窄容器下不变大（2560:${wide.fontL}px → 1100:${narrow.fontL}px）`);
check(Number(narrow.cardW) < Number(wide.cardW), `卡片宽度随容器缩放（2560:${wide.cardW}px → 1100:${narrow.cardW}px）`);

/* 「等比例」的硬判据：字号/容器宽 的比值在未被 clamp 兜住的区间内应基本恒定。
   输入输出都算得出来，不靠"看着差不多"。 */
console.log("\n===== 比例恒定判定（fontN ÷ #cards宽，未触底时应恒定）=====");
const ratios = seen.map(([w, m]) => ({ w, f: m.fontN, bw: m.boxW, r: m.boxW ? m.fontN / m.boxW : 0 }));
for (const x of ratios) console.log(`  ${x.w}: ${x.f}px ÷ ${x.bw}px = ${x.r.toFixed(5)}`);
const free = ratios.filter(x => x.f > 15.2);   // 排除被 clamp 下限 15px 兜住的档
if (free.length >= 2) {
  const rs = free.map(x => x.r);
  const spread = (Math.max(...rs) - Math.min(...rs)) / Math.max(...rs);
  check(spread < 0.05, `未触底的 ${free.length} 档中，字号/容器宽 比值最大偏差 ${(spread * 100).toFixed(2)}% < 5%（即线性等比）`);
} else {
  check(false, `可验证等比的档位不足（仅 ${free.length} 档未触底），无法证明等比例`);
}
check(ratios.every(x => x.f >= 15), `字号下限生效，任何宽度下 .n 均 ≥15px（最小 ${Math.min(...ratios.map(x => x.f))}px）`);
check(seen.every(([, m]) => m.fontM >= 9.5), `「查看明细」字号不低于 9.5px（最小 ${Math.min(...seen.map(([, m]) => m.fontM))}px）`);
const cardRatio = seen.map(([, m]) => m.cardW / m.boxW);
check(Math.max(...cardRatio) - Math.min(...cardRatio) < 0.06,
  `卡片宽/容器宽 比值稳定（${cardRatio.map(r => r.toFixed(3)).join(" / ")}）`);

/* 附带回归：其它面板的通用 .card（结构 .t/.v/.s）不能被 #cards 的收窄改坏。
   本次差点踩坑：把通用 `.card { background/border/padding }` 一并收窄成 `#cards .card`，
   会让「汇总-登录态 / 数据源 / 原始存档 / 抓取记录」这 4 处共 9 张卡瞬间失去底色与内边距。
   这里对**非 #cards 的 .card** 实测三项底样式，防止再次静默回归。 */
console.log("\n===== 附带回归：其它面板的通用 .card 未被牵连 =====");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(800);
const others = await ev(`(() => {
  const list = [...document.querySelectorAll('.card')].filter(c => !c.closest('#cards'));
  const sample = list[0];
  if (!sample) return { n: 0 };
  const cs = getComputedStyle(sample);
  return { n: list.length, bg: cs.backgroundColor, bw: cs.borderTopWidth, br: cs.borderTopLeftRadius, pad: cs.paddingTop };
})()`);
console.log(`  非 #cards 的 .card 共 ${others.n} 张；取样 ${others.bg} / 边框${others.bw} / 圆角${others.br} / 内边距${others.pad}`);
check(others.n >= 1, `其它面板仍有通用 .card 在用（${others.n} 张），故底样式必须保留`);
check(others.bg && others.bg !== "rgba(0, 0, 0, 0)", `通用 .card 仍有底色（${others.bg}）`);
check(parseFloat(others.pad) >= 8, `通用 .card 仍有内边距（${others.pad}）——未被 #cards 的收窄规则波及`);

/* 主题继承：统计条的底色/边框/文字必须全部来自主题变量，深色下要真的变深 */
console.log("\n===== 主题继承（深色下统计条不得沿用浅色）=====");
const lightBg = await ev(`getComputedStyle(document.querySelector('#cards .card')).backgroundColor`);
// 走真实代码路径：点主题 chip（= applyTheme），而非手动改 <link>
const clicked = await ev(`(() => {
  const c = [...document.querySelectorAll('#themePick .chip')].find(x => x.dataset.id === 'dark-dashboard');
  if (!c) return false; c.click(); return true;
})()`);
check(clicked, "找到并点击了「深色数据大屏」主题 chip");
await sleep(800);
const dark = await ev(`(() => {
  const c = document.querySelector('#cards .card'); const cs = getComputedStyle(c);
  const lum = (rgb => { const m = (rgb.match(/\\d+/g)||[0,0,0]).map(Number);
    return Math.round(0.2126*m[0] + 0.7152*m[1] + 0.0722*m[2]); })(cs.backgroundColor);
  const nEl = c.querySelector('.n');
  return { bg: cs.backgroundColor, lum, nColor: getComputedStyle(nEl).color,
           nFont: parseFloat(getComputedStyle(nEl).fontSize),
           href: (document.getElementById('theme-link')||{}).href || '' };
})()`);
console.log(`  实际生效样式表 ${String(dark.href).split("/").pop()}`);
console.log(`  浅色卡底 ${lightBg} → 深色卡底 ${dark.bg}（亮度 ${dark.lum}）｜数字色 ${dark.nColor}｜字号 ${dark.nFont}px`);
check(String(dark.href).includes("dark-dashboard"), `样式表确已切到深色（${String(dark.href).split("/").pop()}）`);
check(dark.bg !== lightBg, `深色主题下统计卡底色确实改变（${lightBg} → ${dark.bg}）`);
check(dark.lum < 90, `深色卡底确实为深色（亮度 ${dark.lum} < 90）`);
check(dark.nFont >= 15, `深色下字号下限仍生效（${dark.nFont}px）`);
// 复位到浅色（走同一 applyTheme 路径），避免影响后续手动查看
await ev(`(() => { const c = [...document.querySelectorAll('#themePick .chip')].find(x => x.dataset.id === 'light-tech');
  if (c) c.click(); })()`);
await sleep(400);

/* 截图留证 */
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(900);
await ev(`(() => { const e = document.getElementById('cards'); window.scrollTo(0, e.getBoundingClientRect().top + scrollY - 90); })()`);
await sleep(500);
await shot("cards_1600.png");
await send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(1200);
await ev(`(() => { const e = document.getElementById('cards'); window.scrollTo(0, e.getBoundingClientRect().top + scrollY - 90); })()`);
await sleep(500);
await shot("cards_1100.png");

console.log("\n" + (fails.length ? `FAILED：${fails.length} 项` : "ALL PASSED"));
for (const f of fails) console.log("   - " + f);
ws.close(); edge.kill();
process.exit(fails.length ? 1 : 0);
