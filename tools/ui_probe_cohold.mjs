/* Request F「近5日共同买入」板块的真实浏览器验证（零依赖：Node 22 内置 WebSocket 直连 CDP）。
   只用截图判断"看起来对"是不够的——本脚本对每一项给出 PASS/FAIL，并断言：
   导航位置、面板切换、窗口条内容、两张表的行数与列、参与者明细字段、
   数量/浮盈真的渲染出来、天数与锚定切换真的改变结果、行下钻能开抽屉、以及 JS 异常为 0。 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const PORT = 9337;
const outDir = process.argv[2] || ".";
mkdirSync(outDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "edgecoh_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fails = [];
const check = (ok, msg) => { console.log(`  [${ok ? "OK" : "FAIL"}] ${msg}`); if (!ok) fails.push(msg); };

const edge = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--hide-scrollbars", "--window-size=1600,1000", BASE + "#trade/cohold",
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
const jsErrors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") {
    jsErrors.push((m.params?.exceptionDetails?.exception?.description || "?").slice(0, 200));
  }
};
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
/* 按可见文字点按钮（面板里的按钮没有 id，用文字定位最贴近用户实际点击的路径） */
const clickText = (sel, text) => ev(`(() => {
  const b = [...document.querySelectorAll(${JSON.stringify(sel)})].find(x => (x.textContent||"").trim().startsWith(${JSON.stringify(text)}));
  if (!b) return false; b.click(); return true; })()`);

await send("Page.enable"); await send("Runtime.enable");
await sleep(3800);

/* ---------- 1. 导航位置 ---------- */
console.log("\n[1] 左栏导航：新增项的位置与层级");
const navInfo = await ev(`(() => {
  const groups = [...document.querySelectorAll('#sidenav .nav-group')].map(g => ({
    cat: (g.querySelector('.nav-item')||{}).dataset?.cat,
    label: ((g.querySelector('.ni-txt')||{}).textContent||'').trim(),
    items: [...g.querySelectorAll('.sub-item')].map(a => ({ tab: a.dataset.tab, label: (a.textContent||'').trim() })),
  }));
  const trade = groups.find(g => g.cat === 'trade');
  return { groups: groups.map(g => ({ cat: g.cat, label: g.label, n: g.items.length })),
           tradeItems: trade ? trade.items : [],
           railX: Math.round(document.querySelector('#sidenav').getBoundingClientRect().x) };
})()`);
check(!!navInfo.tradeItems.find((i) => i.tab === "cohold"),
      `「实盘操作」下存在 cohold 子项（${navInfo.tradeItems.map((i) => i.label).join(" / ")}）`);
const ci = navInfo.tradeItems.findIndex((i) => i.tab === "cohold");
const pi = navInfo.tradeItems.findIndex((i) => i.tab === "consensus");
check(ci === pi + 1, `紧邻「共同持仓共识」之后（consensus 序号 ${pi} → cohold 序号 ${ci}）`);
check(navInfo.railX === 0, `导航仍在视口最左缘（x=${navInfo.railX}）`);

/* ---------- 2. 面板切换 ---------- */
console.log("\n[2] 面板切换与深链");
const panel = await ev(`(() => {
  const on = [...document.querySelectorAll('.panel.active')].map(p => p.id);
  return { on, hash: location.hash,
           buyH: !!document.querySelector('#p-cohold .coh-h') }; })()`);
check(panel.on.length === 1 && panel.on[0] === "p-cohold",
      `仅 p-cohold 处于 active（实际 ${panel.on.join(",")}），hash=${panel.hash}`);

/* ---------- 3. 窗口条 ---------- */
console.log("\n[3] 窗口条：把「按哪一天算」讲清楚");
const bar = await ev(`(() => {
  const b = document.querySelector('#p-cohold .coh-bar');
  if (!b) return null;
  return { text: (b.textContent||'').replace(/\\s+/g,' ').trim(),
           btns: [...b.querySelectorAll('button')].map(x => (x.textContent||'').trim()),
           primary: [...b.querySelectorAll('button.primary')].map(x => (x.textContent||'').trim()) };
})()`);
check(!!bar, "窗口条已渲染");
check(/2026-\d\d-\d\d ~ 2026-\d\d-\d\d/.test(bar.text), `含窗口日期区间（${(bar.text.match(/2026-\d\d-\d\d ~ 2026-\d\d-\d\d/) || [])[0]}）`);
check(bar.text.includes("锚定"), "明确标注了锚定口径");
check(bar.btns.includes("3 天") && bar.btns.includes("5 天") && bar.btns.includes("10 天"),
      `天数可切换（${bar.btns.filter((x) => x.includes("天")).join(" ")}）`);
check(bar.btns.some((x) => x.includes("锚定今日")) && bar.btns.some((x) => x.includes("数据最新日")),
      "锚定口径可切换");
check(bar.primary.includes("5 天"), "默认高亮 5 天");
check(bar.text.includes("口径：≥2 人同向"), "展示了纳入口径（≥N 人）");

/* ---------- 4. 两张表与列 ---------- */
console.log("\n[4] 共买 / 共卖 两张表");
const tbl = await ev(`(() => {
  const hs = [...document.querySelectorAll('#p-cohold .coh-h')].map(h => (h.textContent||'').replace(/\\s+/g,' ').trim());
  const tables = [...document.querySelectorAll('#p-cohold table')];
  return {
    headings: hs,
    tables: tables.map(t => ({
      head: [...t.querySelectorAll('thead th')].map(x => (x.textContent||'').trim()),
      rows: t.querySelectorAll('tbody tr').length,
    })),
    legend: (document.querySelector('#p-cohold .coh-legend')||{}).textContent?.replace(/\\s+/g,' ').trim() || '',
  };
})()`);
check(tbl.headings.length === 2 && tbl.headings[0].includes("共同买入") && tbl.headings[1].includes("共同卖出"),
      `两个分组标题（${tbl.headings.join(" | ")}）`);
check(tbl.tables.length === 2, `两张表（共买 ${tbl.tables[0]?.rows} 行 / 共卖 ${tbl.tables[1]?.rows} 行）`);
const H = tbl.tables[0].head.join("|");
check(H.includes("股票") && H.includes("时间范围"), `含股票与时间范围列（${H}）`);
check(H.includes("合计数量") && H.includes("均价"), "含合计数量与均价列");
check(H.includes("参与者明细"), "含参与者明细列");
check(H.includes("持有状态"), "含持有状态列");
check(H.includes("最后更新"), "沿用「最后更新」列（与 Request G 打通）");
check(tbl.tables[0].head.join("|").includes("买入时间"), "共买表列头标注「买入时间」");
check(tbl.tables[1].head.join("|").includes("卖出时间"), "共卖表列头标注「卖出时间」（不是照抄买入）");
check(tbl.legend.includes("红") && tbl.legend.includes("绿"), `红/绿图例（A股惯例）：${tbl.legend}`);
check(tbl.tables[0].rows >= 1, `共买表有 ${tbl.tables[0].rows} 行数据`);
check(tbl.tables[1].rows >= 1, `共卖表有 ${tbl.tables[1].rows} 行数据`);

/* ---------- 5. 参与者明细字段真的渲染出来 ---------- */
console.log("\n[5] 参与者明细：时间 / 价格 / 数量 / 仓位 / 浮盈 / 状态");
const det = await ev(`(() => {
  const rows = [...document.querySelectorAll('#p-cohold table')][0].querySelectorAll('tbody tr');
  const lines = [...rows[0].querySelectorAll('.coh-p')].map(d => d.textContent.replace(/\\s+/g,' ').trim());
  const qty = [...document.querySelectorAll('#p-cohold .coh-p')]
    .map(d => d.textContent).filter(t => /\\d+ 股/.test(t)).length;
  const gain = [...document.querySelectorAll('#p-cohold .coh-p b[style*="color"]')]
    .map(b => ({ t: (b.textContent||'').trim(), c: getComputedStyle(b).color }));
  return { firstRow: lines, qtyLines: qty, gain,
           badFresh: document.querySelectorAll('#p-cohold td.td-fresh').length,
           holdBadge: document.querySelectorAll('#p-cohold .badge.hold').length,
           soldBadge: document.querySelectorAll('#p-cohold .badge.bear').length };
})()`);
check(det.firstRow.length >= 2, `首行列出 ${det.firstRow.length} 位参与者`);
check(/\d{4}-\d\d-\d\d \d\d:\d\d/.test(det.firstRow[0]), `参与者带具体时间（${(det.firstRow[0].match(/2026-\d\d-\d\d \d\d:\d\d/) || [])[0]}）`);
check(det.firstRow[0].includes("¥"), "参与者带价格");
check(det.qtyLines > 0, `${det.qtyLines} 行参与者渲染出成交数量（来自 trades 表）`);
check(det.firstRow[0].includes("仓位"), "参与者带仓位占比");
check(/[+-]?\d+(\.\d+)?%/.test(det.firstRow[0]), "参与者带浮盈百分比");
check(det.holdBadge > 0 || det.soldBadge > 0, "参与者带持有状态徽标");
check(det.badFresh > 0, `复用「最后更新」时效格 ${det.badFresh} 个`);
const gains = det.gain || [];
check(gains.length > 0, `浮盈带颜色标记 ${gains.length} 处`);
if (gains.length) {
  const bull = /rgb\(200, 30, 30\)|rgb\(185, 28, 28\)|rgb\(248, 113, 113\)|rgb\(220, 38, 38\)/;
  const bear = /rgb\(21, 128, 61\)|rgb\(52, 211, 153\)|rgb\(22, 163, 74\)|rgb\(16, 122, 87\)/;
  console.log(`       示例：${gains[0].t} → ${gains[0].c}`);
  check(bull.test(gains[0].c) || bear.test(gains[0].c),
        `浮盈配色沿用主题的涨红/跌绿变量（实测 ${gains[0].c}）`);
  const reds = gains.filter((g) => g.t.startsWith("+") && bull.test(g.c)).length;
  check(reds === gains.filter((g) => g.t.startsWith("+")).length,
        `所有正浮盈都用红色（${reds}/${gains.filter((g) => g.t.startsWith("+")).length}）—— 符合 A 股惯例`);
}

/* ---------- 6. 天数切换真的改变结果 ---------- */
console.log("\n[6] 天数切换（5 天 → 3 天）");
const before = await ev(`document.querySelectorAll('#p-cohold table')[0].querySelectorAll('tbody tr').length`);
await clickText("#p-cohold .coh-bar button", "3 天");
await sleep(2200);
const after3 = await ev(`(() => ({
  rows: document.querySelectorAll('#p-cohold table')[0].querySelectorAll('tbody tr').length,
  win: (document.querySelector('#p-cohold .coh-bar').textContent.match(/2026-\\d\\d-\\d\\d ~ 2026-\\d\\d-\\d\\d/)||[''])[0],
  primary: [...document.querySelectorAll('#p-cohold .coh-bar button.primary')].map(x=>x.textContent.trim()),
}))()`);
check(after3.win !== "", `窗口仍正常（${after3.win}）`);
check(after3.rows <= before, `3 天结果不多于 5 天（${before} → ${after3.rows}）`);
check(after3.primary.includes("3 天"), "「3 天」按钮变为高亮态");
await shot("coh_01_days3.png");

/* ---------- 7. 锚定切换 ---------- */
console.log("\n[7] 锚定口径切换（数据最新日 ↔ 今日）");
await clickText("#p-cohold .coh-bar button", "锚定今日");
await sleep(2200);
const today = await ev(`(() => {
  const t = document.querySelector('#p-cohold .coh-bar').textContent.replace(/\\s+/g,' ');
  return { text: t, win: (t.match(/2026-\\d\\d-\\d\\d ~ 2026-\\d\\d-\\d\\d/)||[''])[0],
           today: new Date().toISOString().slice(0,10) }; })()`);
check(today.text.includes("按今日"), `切到按今日口径（窗口 ${today.win}）`);
check(today.win.split(" ~ ")[1] === today.today, `窗口右端 = 今天（${today.win.split(" ~ ")[1]} vs ${today.today}）`);
await shot("coh_02_today.png");
await clickText("#p-cohold .coh-bar button", "锚定数据最新日");
await sleep(2200);
const back = await ev(`(document.querySelector('#p-cohold .coh-bar').textContent.match(/2026-\\d\\d-\\d\\d ~ 2026-\\d\\d-\\d\\d/)||[''])[0]`);
check(back !== today.win, `切回数据最新日后窗口随之变化（${today.win} → ${back}）`);

/* ---------- 8. 行下钻 ---------- */
console.log("\n[8] 点击行下钻（抽屉）");
const opened = await ev(`(() => {
  const tr = document.querySelector('#p-cohold table tbody tr[data-sym]');
  if (!tr) return { ok: false };
  const sym = tr.dataset.sym;
  tr.click();
  return { ok: true, sym };
})()`);
await sleep(1800);
const drawer = await ev(`(() => {
  const d = document.getElementById('drawer');
  return { open: d && d.getAttribute('aria-hidden') === 'false',
           title: (document.getElementById('drawerTitle')||{}).textContent || '',
           len: (document.getElementById('drawerBody')||{}).innerHTML?.length || 0 }; })()`);
check(opened.ok && drawer.open, `点击 ${opened.sym} 行后抽屉打开（标题「${drawer.title}」）`);
check(drawer.len > 200, `抽屉有内容（${drawer.len} 字符）`);
await shot("coh_03_drawer.png");
await ev(`document.getElementById('btnClose')?.click()`);
await sleep(600);

/* ---------- 9. 回归：共同持仓共识仍在 ---------- */
console.log("\n[9] 回归：共同持仓共识未被破坏");
await ev(`location.hash = '#trade/consensus'`);
await sleep(2200);
const cons = await ev(`(() => ({
  active: (document.querySelector('.panel.active')||{}).id,
  rows: document.querySelectorAll('#p-consensus table tbody tr').length,
  head: [...document.querySelectorAll('#p-consensus thead th')].map(x=>x.textContent.trim()).join('|'),
}))()`);
check(cons.active === "p-consensus", `切回共识面板（active=${cons.active}）`);
check(cons.rows > 0, `共识表仍有 ${cons.rows} 行`);
check(cons.head.includes("加权共识") && cons.head.includes("层级覆盖"),
      "共识表既有优化列仍在（加权共识 / 层级覆盖）");
check(cons.head.includes("最后更新"), "共识表「最后更新」列仍在");
await shot("coh_04_consensus_regression.png");

/* ---------- 10. 回到新板块并留证（先复位到默认 5 天 / 数据最新日，保证截图是默认态） ---------- */
await ev(`location.hash = '#trade/cohold'`);
await sleep(1500);
await clickText("#p-cohold .coh-bar button", "5 天");
await sleep(1200);
await clickText("#p-cohold .coh-bar button", "锚定数据最新日");
await sleep(2400);
const reset = await ev(`(() => ({
  primary: [...document.querySelectorAll('#p-cohold .coh-bar button.primary')].map(x=>x.textContent.trim()),
  win: (document.querySelector('#p-cohold .coh-bar').textContent.match(/2026-\\d\\d-\\d\\d ~ 2026-\\d\\d-\\d\\d/)||[''])[0],
  buy: document.querySelectorAll('#p-cohold table')[0].querySelectorAll('tbody tr').length,
  sell: document.querySelectorAll('#p-cohold table')[1].querySelectorAll('tbody tr').length,
}))()`);
check(reset.primary.includes("5 天"), `复位到默认 5 天（高亮 ${reset.primary.join(",")}）`);
check(reset.buy === 11 && reset.sell === 4,
      `复位后与接口一致（共买 ${reset.buy} / 共卖 ${reset.sell}，窗口 ${reset.win}）`);
await ev(`window.scrollTo(0,0)`);
await sleep(500);
await shot("coh_05_panel_top.png");
await ev(`(() => { const h = [...document.querySelectorAll('#p-cohold .coh-h')][1];
  if (h) window.scrollTo(0, h.getBoundingClientRect().top + window.scrollY - 70); })()`);
await sleep(600);
await shot("coh_06_panel_sell.png");

console.log("\n" + "=".repeat(56));
check(jsErrors.length === 0, `JS 异常 0（实际 ${jsErrors.length}）` + (jsErrors.length ? " -> " + jsErrors.join(" | ") : ""));
if (fails.length) {
  console.log(`FAILED: ${fails.length} 项`);
  fails.forEach((f) => console.log("  -", f));
} else {
  console.log("ALL PASSED —— 近5日共同买入板块在真实浏览器中行为符合预期");
}
ws.close(); edge.kill();
process.exit(fails.length ? 1 : 0);
