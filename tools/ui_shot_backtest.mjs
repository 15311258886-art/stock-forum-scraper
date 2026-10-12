/* 回测区专项取证（2026-09-23 UI 整理 P0/P4）
 *
 * 为什么单独一个脚本：ui_probe_ux.mjs 里那一版按 id 滚动的逻辑没滚到位，
 * 产出的 ux_bt_*.png 实际拍的是 KB 面板顶部 —— 截图"看起来正常"但内容不对，
 * 这类"假证据"必须避免。这里改成：滚到「回测表所在元素上沿」再拍，
 * 并同时读回**计算样式**（text-align / color）做断言，不靠肉眼判断。
 *
 * 用法：node tools/ui_shot_backtest.mjs [输出目录]
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CDP_PORT = 9412;
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 每次都用全新 profile，避免复用残留实例 → 合成器停帧 → 截到上一帧旧画面
const profile = path.join(OUT, "_edgeprofile_bt_" + Date.now());
const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${profile}`,
  "--window-size=1440,1000", "about:blank",
], { stdio: "ignore" });

let ws, seq = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});

async function connect() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* 浏览器还没起来 */ }
    await sleep(250);
  }
  throw new Error("CDP 连接超时");
}

const wsUrl = await connect();
ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};

async function ev(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  return r.result?.result?.value;
}
async function shot(name) {
  // 截图前强制回一次布局：滚动位置要按当前值重算，否则 sticky 元素会拍进奇怪位置
  await ev("window.dispatchEvent(new Event('resize'))");
  await sleep(260);
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("saved", name);
}

const issues = [];
function check(ok, label) {
  if (ok) console.log("  [OK]   " + label);
  else { console.log("  [FAIL] " + label); issues.push(label); }
}

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
const jsErrors = [];
await send("Log.enable").catch(() => {});
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") jsErrors.push(m.params?.exceptionDetails?.text);
});

await send("Page.navigate", { url: BASE + "#learn/learn_zdx_stock" });
await sleep(5200);

console.log("\n[1] 定位回测表");
const info = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests');
  const tables = [...document.querySelectorAll('#p-learn_zdx_stock .kb-bt')];
  return {
    hasSection: !!sec,
    sectionTop: sec ? Math.round(sec.getBoundingClientRect().top + window.scrollY) : null,
    titles: [...document.querySelectorAll('#p-learn_zdx_stock .kb-bt-h')].map(e => e.textContent.trim().slice(0, 40)),
    n: tables.length,
    firstCols: tables[0] ? [...tables[0].querySelectorAll('thead th')].map(e => e.textContent.trim()) : [],
    wrapped: tables.every(t => !!t.querySelector('.kb-scroll')),
    wideTip: document.querySelectorAll('#p-learn_zdx_stock .kb-wide-tip').length,
  };
})()`);
console.log(`  回测区块存在=${info.hasSection}，表 ${info.n} 张：`);
for (const t of info.titles) console.log("     - " + t);
console.log(`  首表列名：${info.firstCols.join(" | ")}`);
check(info.n >= 3, `回测表数量 ≥3（实测 ${info.n}）`);
check(info.wrapped, "每张回测表都被 .kb-scroll 包住（宽表可横向滚动，不撑破布局）");

console.log("\n[2] 数字列对齐（P0 修复项：此前右对齐率 0/28）");
const align = await ev(`(() => {
  const bt = document.querySelector('#p-learn_zdx_stock .kb-bt');
  if (!bt) return null;
  const cells = [...bt.querySelectorAll('tbody tr')].flatMap(tr => [...tr.children]);
  const numeric = cells.filter(td => /^-?[\\d,]+(\\.\\d+)?%?$/.test(td.textContent.trim()));
  const right = numeric.filter(td => getComputedStyle(td).textAlign === 'right');
  const mono = numeric.filter(td => /tabular-nums/.test(getComputedStyle(td).fontVariantNumeric));
  const heads = [...bt.querySelectorAll('thead th')].filter(th => getComputedStyle(th).textAlign === 'right');
  return { total: cells.length, numeric: numeric.length, right: right.length,
           mono: mono.length, rightHeads: heads.length,
           sample: numeric.slice(0, 4).map(td => td.textContent.trim()) };
})()`);
console.log(`  数值单元格 ${align.numeric}/${align.total}，其中右对齐 ${align.right}、等宽数字 ${align.mono}`);
console.log(`  右对齐表头 ${align.rightHeads} 个 | 样例值 ${align.sample.join(", ")}`);
check(align.numeric >= 10, `确有数值单元格可判定（${align.numeric} 个）`);
check(align.right === align.numeric, `全部数值单元格右对齐（${align.right}/${align.numeric}）`);
check(align.mono === align.numeric, `全部数值单元格用等宽数字 tabular-nums（${align.mono}/${align.numeric}）`);

console.log("\n[3] 着色语义（P0 修复项：亏损/回撤不得按「正数=上涨」染色）");
// 注意：语义 class 落在 <td> 自身（不是内部 span），故按 td 取
const sem = await ev(`(() => {
  const bt = document.querySelector('#p-learn_zdx_stock .kb-bt');
  if (!bt) return [];
  const cols = [...bt.querySelectorAll('thead th')].map(e => e.textContent.trim());
  const isNeu = (c) => /胜率|样本|次数|条数|数量|天数|持仓|占比|评级|排名|名称|博主|盈亏比/.test(c);
  const isNeg = (c) => /亏损|回撤|跌幅|风险|失败|成本/.test(c);
  const out = [];
  [...bt.querySelectorAll('tbody tr')].forEach(tr => {
    [...tr.children].forEach((td, ci) => {
      const v = td.textContent.trim();
      if (!/^[+-]?[\\d,]+(\\.\\d+)?%$/.test(v)) return;
      const c = cols[ci] || '';
      out.push({ col: c, v, cls: td.className || '', color: getComputedStyle(td).color,
                 neutral: isNeu(c), negative: isNeg(c) });
    });
  });
  return out;
})()`);
const red = /rgb\(200, 30, 30\)|rgb\(185, 28, 28\)|rgb\(248, 113, 113\)|rgb\(153, 27, 27\)|rgb\(220, 38, 38\)/;
const green = /rgb\(21, 128, 61\)|rgb\(52, 211, 153\)|rgb\(22, 163, 74\)|rgb\(21, 128, 61\)/;
console.log(`  带色百分比单元格 ${sem.length} 个`);
const seenCols = new Set();
for (const s of sem) {
  if (seenCols.has(s.col)) continue;
  seenCols.add(s.col);
  const tag = s.negative ? "亏损类" : (s.neutral ? "中性类" : "收益类");
  const col = red.test(s.color) ? "红" : (green.test(s.color) ? "绿" : "无色(" + s.color + ")");
  console.log(`     ${s.col}=${s.v}  ${tag}  class="${s.cls}"  → ${col}`);
}
const negRed = sem.filter(s => s.negative && parseFloat(s.v) >= 0 && red.test(s.color));
const posGreen = sem.filter(s => !s.negative && !s.neutral && parseFloat(s.v) > 0 && green.test(s.color));
const neuColored = sem.filter(s => s.neutral && (red.test(s.color) || green.test(s.color)));
check(negRed.length === 0,
      `无「亏损/回撤为正数却被染红」（检查 ${sem.filter(s => s.negative).length} 个亏损类，违规 ${negRed.length}）`);
check(posGreen.length === 0,
      `无「盈利为正数却被染绿」（检查 ${sem.filter(s => !s.negative && !s.neutral).length} 个收益类，违规 ${posGreen.length}）`);
check(neuColored.length === 0,
      `中性列（胜率/样本/评级等）不上涨跌色（违规 ${neuColored.length}）`);
check(sem.length >= 10, `确有带色单元格可判定（${sem.length} 个）`);

console.log("\n[4] 截图留证");
await ev(`(() => { const e = document.getElementById('kb-sec-backtests');
  if (e) window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 60); })()`);
await sleep(700);
await shot("bt_1440_tables.png");
// 再拍一张：第二、三张表
await ev(`(() => { const b = [...document.querySelectorAll('#p-learn_zdx_stock .kb-bt')][1];
  if (b) window.scrollTo(0, b.getBoundingClientRect().top + window.scrollY - 60); })()`);
await sleep(700);
await shot("bt_1440_tables2.png");
// 校验"拍到的是不是想拍的东西"：按**刚才滚动到的第 2 张表**判定，
// 不能用第 1 张（它已被滚到视口上方，top 为负是正常的）
const where = await ev(`(() => {
  const b = [...document.querySelectorAll('#p-learn_zdx_stock .kb-bt')][1];
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return { top: Math.round(r.top), visible: r.top >= -40 && r.top <= window.innerHeight };
})()`);
check(where && where.visible, `截图时目标回测表确实在视口内（top=${where ? where.top : "?"}）`);
const whereTop = await ev(`(() => {
  const b = document.querySelector('#p-learn_zdx_stock .kb-bt');
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return { top: Math.round(r.top) };
})()`);
console.log(`  （第 1 张表此时 top=${whereTop ? whereTop.top : "?"}，负值是正常的——它在上方）`);

console.log("\n[5] 窄屏 1100px：宽表是否给「可左右滑」提示");
await send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(1400);
await ev(`(() => { const e = document.getElementById('kb-sec-backtests');
  if (e) window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 50); })()`);
await sleep(700);
const narrow = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests');
  const bts = [...document.querySelectorAll('#p-learn_zdx_stock .kb-bt')];
  return bts.map((b, i) => {
    const sc = b.querySelector('.kb-scroll');
    const tip = b.querySelector('.kb-wide-tip');
    return { i: i + 1, sw: sc ? sc.scrollWidth : 0, cw: sc ? sc.clientWidth : 0,
             overflow: sc ? sc.scrollWidth > sc.clientWidth + 4 : false,
             tipExists: !!tip, tipShown: tip ? !tip.hidden : false };
  });
})()`);
for (const t of narrow) {
  console.log(`  表${t.i}: 内容 ${t.sw} / 视窗 ${t.cw}  溢出=${t.overflow}  提示条存在=${t.tipExists} 显示=${t.tipShown}`);
}
const overflowing = narrow.filter(t => t.overflow);
check(overflowing.length >= 1, `1100px 下确有回测表需要横向滚动（${overflowing.length} 张）`);
check(overflowing.every(t => t.tipShown), `需要滚动的表都显示了「可左右滑」提示（${overflowing.filter(t => t.tipShown).length}/${overflowing.length}）`);
check(narrow.filter(t => !t.overflow).every(t => !t.tipShown), "放得下的窄表不显示多余提示");
await shot("bt_1100_widetip.png");

console.log("\n[6] JS 异常：" + jsErrors.length);
check(jsErrors.length === 0, "零 JS 异常");
check(issues.length === 0, `合计 ${issues.length} 项失败`);

ws.close();
edge.kill();
setTimeout(() => {
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  process.exit(issues.length ? 1 : 0);
}, 700);
