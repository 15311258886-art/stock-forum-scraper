/* UI 可用性实测（UI 重构取证用）：5 档视口下量取导航点击区域、内容宽度、
   表格横向溢出、回测表渲染，并给出截图。
   证据口径：全部读 getComputedStyle / getBoundingClientRect，不靠目测。
   注意：必须用全新 user-data-dir，否则可能截到残留实例的旧帧。 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "edgux_"));
const PORT = 9401 + Math.floor(Math.random() * 60);
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  "--window-size=1600,1000", "about:blank",
], { stdio: "ignore" });

async function cdpTarget() {
  for (let i = 0; i < 90; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const x = l.find((y) => y.type === "page" && y.webSocketDebuggerUrl);
      if (x) return x.webSocketDebuggerUrl;
    } catch { /* 还没起来 */ }
    await sleep(250);
  }
  throw new Error("CDP 未就绪");
}

const ws = new WebSocket(await cdpTarget());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let mid = 0;
const pend = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
const send = (method, params = {}) => new Promise((r) => {
  const i = ++mid; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params }));
});
async function ev(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
}
const errors = [];
await send("Runtime.enable");
await send("Log.enable");
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") errors.push(String(m.params?.exceptionDetails?.text || "").slice(0, 160));
  if (m.method === "Log.entryAdded" && m.params?.entry?.level === "error") errors.push(String(m.params.entry.text).slice(0, 160));
});

let pass = 0, fail = 0;
const check = (ok, msg) => { if (ok) { pass++; console.log("  [OK] " + msg); } else { fail++; console.log("  [FAIL] " + msg); } };

async function shot(name) {
  // 必须先回顶：topbar 是 position:sticky，页面停在上一段的滚动位置时
  // 截图会「看起来正常」（顶栏还在），但其实内容已被滚掉一大截——
  // 本次就这样误判过「统计条不见了」。回顶 + 等重绘再截。
  await ev("window.scrollTo(0,0)");
  await sleep(500);
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + name);
}
const setVp = (w, h) => send("Emulation.setDeviceMetricsOverride",
  { width: w, height: h, deviceScaleFactor: 1, mobile: false });

await send("Page.enable");
await send("Page.navigate", { url: BASE + "#overview/summary" });
await sleep(5200);

/* ============ 1. 布局与导航：5 档视口 ============ */
console.log("\n===== 1. 布局 / 导航 点击区域（5 档实测）=====");
const VIEWPORTS = [2560, 1920, 1680, 1440, 1100];
const rows = [];
for (const w of VIEWPORTS) {
  await setVp(w, 1000);
  await sleep(900);
  const m = await ev(`(() => {
    const R = (s) => { const e = document.querySelector(s); if (!e) return null;
      const r = e.getBoundingClientRect();
      return { x: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height),
               top: Math.round(r.top), right: Math.round(r.right) }; };
    const rail = document.querySelector('.rail') || document.querySelector('.sidenav');
    const content = document.querySelector('.content') || document.querySelector('.main');
    const items = [...document.querySelectorAll('.nav-item')];
    const subs = [...document.querySelectorAll('.nav-sub a, .nav-sub .nav-sub-item, .li-sub a')];
    const first = items[0];
    const cs = first ? getComputedStyle(first) : null;
    // 表格横向溢出盘点
    const tbls = [...document.querySelectorAll('#p-summary table')];
    const over = tbls.map(t => ({ sw: t.scrollWidth, cw: t.clientWidth }))
                     .filter(x => x.sw > x.cw + 2);
    // 当前面板里的表
    const cur = document.querySelector('.panel.active');
    const curTbl = cur ? [...cur.querySelectorAll('table')] : [];
    const curOver = curTbl.map(t => ({ sw: t.scrollWidth, cw: t.clientWidth, over: t.scrollWidth > t.clientWidth + 2 }));
    // 内容区一行中文字符数：以**真正承载文字的块**为准。
    // 注意 .hint 是满宽的告示条（带底色），它的宽度反映的是**容器**而不是**行宽**；
    // 限制阅读长度的是折叠后的 .hint-body（max-width: 84ch）。两者分开量，避免自欺。
    const hint = document.querySelector('.panel.active .hint');
    const hb = document.querySelector('.panel.active .hint-fold .hint-body') || hint;
    let cpl = null, measureBox = 0, boxBox = 0;
    if (hb) {
      const hcs = getComputedStyle(hb);
      const fs2 = parseFloat(hcs.fontSize) || 12;
      measureBox = Math.round(hb.getBoundingClientRect().width
        - parseFloat(hcs.paddingLeft || 0) - parseFloat(hcs.paddingRight || 0));
      cpl = Math.round(measureBox / fs2);
    }
    if (hint) boxBox = Math.round(hint.getBoundingClientRect().width);
    return {
      vw: window.innerWidth,
      docScrollW: document.documentElement.scrollWidth,
      rail: rail ? { x: Math.round(rail.getBoundingClientRect().left), w: Math.round(rail.getBoundingClientRect().width) } : null,
      contentW: content ? Math.round(content.getBoundingClientRect().width) : null,
      contentPadR: content ? Math.round(parseFloat(getComputedStyle(content).paddingRight)) : null,
      navCount: items.length, subCount: subs.length,
      navItemH: first ? Math.round(first.getBoundingClientRect().height) : 0,
      navItemW: first ? Math.round(first.getBoundingClientRect().width) : 0,
      navFont: cs ? parseFloat(cs.fontSize) : 0,
      navActiveBg: cs ? cs.backgroundColor : "",
      navActiveColor: cs ? cs.color : "",
      subH: subs[0] ? Math.round(subs[0].getBoundingClientRect().height) : 0,
      cardCols: (getComputedStyle(document.querySelector('#cards')).gridTemplateColumns || "").split(" ").filter(Boolean).length,
      topbarH: (document.querySelector('.topbar') || {getBoundingClientRect:()=>({height:0})}).getBoundingClientRect().height,
      // 2026-09-23：独立刷新条（#refreshBar，43px）已并入顶栏，不再单独计量；
      // 改为测「顶栏是否仍是单行」。注意**不能**按 offsetTop 去重——顶栏是
      // align-items:center，不同高度的子项本来就有不同 offsetTop，单行也会被算成 7 行
      // （本次就这样误报过）。正解：按**垂直中心**聚类，相邻中心差 >12px 才算换行。
      tbRows: (() => {
        const tb = document.querySelector('.topbar'); if (!tb) return 0;
        const cs = [...tb.children].filter(c => c.offsetParent)
          .map(c => { const r = c.getBoundingClientRect(); return r.top + r.height / 2; })
          .sort((a, b) => a - b);
        if (!cs.length) return 0;
        let n = 1;
        for (let i = 1; i < cs.length; i++) if (cs[i] - cs[i - 1] > 12) n++;
        return n;
      })(),
      hintCharsPerLine: cpl,
      hintMeasureBox: measureBox, hintBoxW: boxBox,
      sumTables: tbls.length, sumOver: over.length,
      curTables: curTbl.length, curOver: curOver.filter(x => x.over),
    };
  })()`);
  rows.push(m);
  console.log(`\n  --- 视口 ${w} (docScrollW=${m.docScrollW}) ---`);
  console.log(`      rail x=${m.rail?.x} w=${m.rail?.w} | content w=${m.contentW} padR=${m.contentPadR}`);
  console.log(`      大类 ${m.navCount} 项 / 子类 ${m.subCount} 项 | 大类点击区 ${m.navItemW}×${m.navItemH || "横排"} px | 字号 ${m.navFont}px`);
  console.log(`      子类项高 ${m.subH}px | 顶栏 ${Math.round(m.topbarH)}px（${m.tbRows} 行，刷新状态已并入此栏）`);
  console.log(`      统计条 ${m.cardCols} 列 | 告示条容器 ${m.hintBoxW}px（≈${m.hintCharsPerLine} 字，**容器非行宽**）| 当前面板表 ${m.curTables} 张（溢出 ${m.curOver.length}）`);
  check(m.docScrollW <= m.vw + 1, `${w}：无横向溢出`);
}

/* 阅读行宽：必须切到**确实有长说明**的面板才能量到折叠后的正文块。
   踩过的坑：默认停靠的「汇总」面板提示只有 39 字、低于折叠阈值（110），
   于是查不到 .hint-fold .hint-body，代码回退到量 .hint 本身 —— 量到的是**告示条容器宽度**
   （2560px 下 2239px ≈ 187 字），于是报出一条**假失败**。容器宽度不是行宽。 */
console.log("\n===== 1.5 说明文字行宽（切到长说明面板实测）=====");
const cplRows = [];
for (const w of VIEWPORTS) {
  await setVp(w, 1000);
  await ev(`location.hash = '#trade/consensus'`);   // 该面板说明 236 字，必定被折叠
  await sleep(1300);
  const r = await ev(`(() => {
    const b = document.querySelector('#p-consensus .hint-fold .hint-body');
    if (!b) return null;
    const cs = getComputedStyle(b);
    const fs = parseFloat(cs.fontSize) || 12;
    const r2 = b.getBoundingClientRect();
    const inner = r2.width - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0);
    const bar = b.closest('.hint').getBoundingClientRect().width;
    return { box: Math.round(r2.width), cpl: Math.round(inner / fs), fs: Math.round(fs * 10) / 10,
             bar: Math.round(bar), clamp: cs.getPropertyValue('-webkit-line-clamp').trim(),
             bodyH: Math.round(r2.height) };
  })()`);
  cplRows.push([w, r]);
  if (!r) { check(false, `${w}：未找到共识面板的折叠正文块（.hint-fold .hint-body）`); continue; }
  console.log(`  ${w}px → 正文块 ${r.box}px ≈ ${r.cpl} 字/行（字号 ${r.fs}px，line-clamp=${r.clamp}）｜告示条满宽 ${r.bar}px`);
  check(r.cpl >= 20 && r.cpl <= 62, `${w}：说明正文字宽 ${r.cpl} 字/行（中文舒适区 30~45，上限 62）`);
  check(r.bar > r.box + 20, `${w}：告示条满宽 ${r.bar}px 明显宽于正文块 ${r.box}px（限宽只作用于正文，容器不被限宽）`);
}
await ev(`location.hash = '#overview/summary'`);
await sleep(900);
await setVp(1440, 1000);
await sleep(900);

/* 导航判别：点击区高度 */
const navH = rows.map(r => r.navItemH).filter(Boolean);
if (navH.length) {
  console.log(`\n  大类项高度：` + rows.map((r, i) => `${VIEWPORTS[i]}:${r.navItemH || "-"}`).join("  "));
  check(Math.min(...navH) >= 32, `大类项点击高度 ≥32px（最小 ${Math.min(...navH)}px）`);
}
const subH = rows.map(r => r.subH).filter(Boolean);
if (subH.length) {
  console.log(`  子类项高度：` + rows.map((r, i) => `${VIEWPORTS[i]}:${r.subH || "-"}`).join("  "));
  check(Math.min(...subH) >= 26, `子类项点击高度 ≥26px（最小 ${Math.min(...subH)}px）`);
}
// 首屏顶部占用：改造前 = topbar 64px + 独立刷新条 43px = 107px；
// 现在合并为一栏，占用应显著下降。
console.log(`  首屏顶部占用：` + rows.map((r, i) => `${VIEWPORTS[i]}:${Math.round(r.topbarH)}px`).join("  "));
const tbH = rows.map(r => Math.round(r.topbarH));
check(Math.max(...tbH) <= 88, `顶栏占用 ≤88px（最大 ${Math.max(...tbH)}px，改造前 topbar+刷新条合计 107px）`);
check(rows.every(r => r.tbRows <= 1), `顶栏在所有档位都是单行（实测行数 ${rows.map(r => r.tbRows).join("/")}）`);

/* ============ 2. 逐面板：表格横向溢出 / 提示语冗余 ============ */
console.log("\n===== 2. 逐面板体检（1440px）=====");
const TABS = ["summary", "sources", "archive", "fetchlog", "stocks", "sentiment", "topics",
  "overseas_sentiment", "overseas_hotspots", "operating", "players", "consensus", "cohold",
  "persons", "learn_zdx_stock", "learn_tracks"];
const panelFacts = [];
for (const tab of TABS) {
  await ev(`location.hash = ${JSON.stringify("#" + (tab.startsWith("learn_") ? "learn" : tab === "summary" ? "overview"
    : ["sources", "archive", "fetchlog"].includes(tab) ? "acquire"
    : ["stocks", "sentiment", "topics", "overseas_sentiment", "overseas_hotspots"].includes(tab) ? "research" : "trade") + "/" + tab)}`);
  await sleep(1700);
  const f = await ev(`(() => {
    const box = document.querySelector('.panel.active');
    if (!box) return { tab: ${JSON.stringify(tab)}, missing: true };
    const tbls = [...box.querySelectorAll('table')].map(t => ({
      sw: t.scrollWidth, cw: t.clientWidth, th: t.querySelectorAll('thead th').length,
      rows: t.querySelectorAll('tbody tr').length,
      over: t.scrollWidth > t.clientWidth + 2,
    }));
    const hints = [...box.querySelectorAll('.hint')];
    const hintChars = hints.reduce((a, h) => a + (h.textContent || "").length, 0);
    const heads = [...box.querySelectorAll('h2,h3,h4')].map(h => h.textContent.trim().slice(0, 40));
    const chips = box.querySelectorAll('.chip-s, .badge').length;
    const btns = box.querySelectorAll('button').length;
    const noteBlocks = box.querySelectorAll('.kb-note, .lg-title, .legend').length;
    return { tab: ${JSON.stringify(tab)},
      textLen: (box.textContent || "").length,
      tables: tbls.length, over: tbls.filter(t => t.over).length, tblsDetail: tbls,
      hintCount: hints.length, hintChars, heads: heads.slice(0, 5), chips, btns, noteBlocks,
      scrollW: box.scrollWidth, clientW: box.clientWidth };
  })()`);
  panelFacts.push(f);
  const ov = f.over ? ` ⚠ ${f.over} 表溢出` : "";
  console.log(`  ${tab.padEnd(20)} 文本 ${String(f.textLen).padStart(6)} 字 | 表 ${f.tables}${ov} | 提示 ${f.hintCount} 段/${f.hintChars} 字 | badge ${f.chips} | 按钮 ${f.btns}`);
}
const totalHints = panelFacts.reduce((a, f) => a + (f.hintCount || 0), 0);
const totalHintChars = panelFacts.reduce((a, f) => a + (f.hintChars || 0), 0);
console.log(`\n  合计：提示语 ${totalHints} 段 / ${totalHintChars} 字`);
check(panelFacts.every(f => f.scrollW <= f.clientW + 2), "所有面板均无整体横向溢出");
const overPanels = panelFacts.filter(f => f.over);
console.log(`  有表格横向溢出的面板：` + (overPanels.length ? overPanels.map(f => `${f.tab}(${f.over})`).join(", ") : "无"));

/* ============ 3. 回测专项 ============ */
console.log("\n===== 3. 回测专项（宽表 + 主题）=====");
await ev(`location.hash = '#learn/learn_zdx_stock'`);
await sleep(4200);
const bt = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests');
  if (!sec) return { missing: true };
  const blocks = [...sec.querySelectorAll('.kb-bt')];
  const scrollers = [...sec.querySelectorAll('.kb-scroll')];
  const info = blocks.map((b, i) => {
    const t = b.querySelector('table');
    const sc = b.querySelector('.kb-scroll');
    const ths = [...b.querySelectorAll('thead th')];
    const firstRow = b.querySelector('tbody tr');
    const tds = firstRow ? [...firstRow.querySelectorAll('td')] : [];
    // 数字列是否右对齐
    const numTds = tds.filter(td => /^-?\\d+(\\.\\d+)?%?$/.test((td.textContent||'').trim()));
    const rightAligned = numTds.filter(td => getComputedStyle(td).textAlign === 'right').length;
    return {
      idx: i, cols: ths.length, rows: b.querySelectorAll('tbody tr').length,
      title: (b.querySelector('.kb-bt-h')?.textContent || '').trim().slice(0, 60),
      tblSW: t.scrollWidth, tblCW: t.clientWidth,
      scSW: sc.scrollWidth, scCW: sc.clientWidth,
      overflow: t.scrollWidth > sc.clientWidth + 2,
      numCells: numTds.length, rightAligned,
      thFont: ths[0] ? parseFloat(getComputedStyle(ths[0]).fontSize) : null,
      tdFont: tds[0] ? parseFloat(getComputedStyle(tds[0]).fontSize) : null,
    };
  });
  return { blocks: info.length, info,
    scrollerOverflowX: getComputedStyle(scrollers[0] || document.body).overflowX };
})()`);
if (bt.missing) {
  check(false, "未找到回测区块 #kb-sec-backtests");
} else {
  bt.info.forEach(t => {
    console.log(`  表${t.idx + 1} ${t.cols} 列 × ${t.rows} 行 | 表宽 ${t.tblSW} vs 视窗 ${t.tblCW} | 滚动容器 ${t.scSW}/${t.scCW} | ${t.overflow ? "⚠ 溢出" : "不溢出"} | 数字格 ${t.numCells}（右对齐 ${t.rightAligned}）`);
    console.log(`       标题：${t.title}`);
  });
  const wide = bt.info.filter(t => t.cols >= 12);
  console.log(`  列数 ≥12 的表：${wide.length} 张（最宽 ${Math.max(...bt.info.map(t => t.cols))} 列）`);
  // 判据修正：不是「每张表都必须溢出」（窄表放得下是好事），
  // 而是「每张表都在 .kb-scroll 滚动容器里」+「放不下的表确实可滚而非被裁剪」。
  check(bt.blocks === bt.info.length, `每张回测表都在 .kb-scroll 容器内（共 ${bt.blocks} 张，overflowX=${bt.scrollerOverflowX}）`);
  check(bt.info.some(t => t.overflow), `至少有一张表宽过容器、依赖横向滚动（${bt.info.filter(t => t.overflow).length} 张）`);
  const cells = bt.info.reduce((a, t) => a + t.numCells, 0);
  const ral = bt.info.reduce((a, t) => a + t.rightAligned, 0);
  console.log(`  数字列右对齐率：${ral}/${cells}`);
  check(ral > 0, `数字列有右对齐（实测 ${ral}/${cells}${ral === 0 ? " —— 全部左对齐，是真实可读性缺陷" : ""}）`);
}
await ev(`(() => { const e = document.getElementById('kb-sec-backtests');
  if (e) window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 70); })()`);
await sleep(700);
await shot("ux_bt_1440.png");

// 窄屏回测
await setVp(1100, 1000);
await sleep(900);
const btN = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests'); if (!sec) return null;
  return [...sec.querySelectorAll('.kb-bt')].map(b => {
    const sc = b.querySelector('.kb-scroll');
    return { cols: b.querySelectorAll('thead th').length,
             scSW: sc.scrollWidth, scCW: sc.clientWidth,
             ox: getComputedStyle(sc).overflowX };
  });
})()`);
if (btN) {
  console.log(`  1100px 下：` + btN.map((t, i) => `表${i + 1} ${t.cols}列 内容${t.scSW}/视窗${t.scCW}`).join(" | "));
  const need = btN.filter((t) => t.scSW > t.scCW);
  const fits = btN.filter((t) => t.scSW <= t.scCW);
  console.log(`      → 需横向滚动 ${need.length} 张，放得下 ${fits.length} 张`);
  // 判据修正：放得下的表不必滚（这是好事，不是缺陷）；放不下的必须**可滚**而不是被裁掉。
  // 旧断言要求「每张表都必须溢出」，窄表会被误判为失败。
  check(need.length >= 1, `1100px 下有宽表确实需要横向滚动（${need.length} 张）`);
  check(btN.every((t) => t.scSW <= t.scCW || t.ox === "auto" || t.ox === "scroll"),
    `1100px 下放不下的表都可横向滚动、未被裁剪（overflowX=${[...new Set(btN.map((t) => t.ox))].join("/")}）`);
}
await shot("ux_bt_1100.png");

/* ============ 3.5 P0 整理批次专项验收 ============
   验收对象（本次改动）：① 后端原始 dict 不再上屏 ② 超长提示折叠 ③ 数字右对齐
   ④ 回测配色语义修正 ⑤ 宽表可滚动提示 */
console.log("\n===== 3.5 P0 专项（原始 dict / 提示折叠 / 数字右对齐 / 配色语义 / 宽表提示）=====");

// (a) 回测配色语义：负向列（亏损/回撤）不得用"上涨红"；中性列（胜率/样本）不得上色
const colorSem = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests');
  if (!sec) return { missing: true };
  const NEG = /亏损|回撤|跌幅|风险|失败|成本/;
  const NEU = /胜率|样本|次数|条数|数量|天数|持仓|占比|评级|排名|名称|博主/;
  const bad = [], ok = [];
  [...sec.querySelectorAll('table.kb-table')].forEach((tb) => {
    const ths = [...tb.querySelectorAll('thead th')].map(t => (t.textContent||'').trim());
    [...tb.querySelectorAll('tbody tr')].forEach((tr) => {
      [...tr.querySelectorAll('td')].forEach((td, ci) => {
        const name = ths[ci] || '';
        const t = (td.textContent||'').trim();
        if (!/^-?[\\d,]+(\\.\\d+)?%$/.test(t)) return;
        const red = td.classList.contains('kb-pct-up');
        const grn = td.classList.contains('kb-pct-dn');
        if (NEG.test(name)) {
          const msg = name + ' ' + t + ' → ' + (red ? '红(错)' : (grn ? '绿' : '无色'));
          (red ? bad : ok).push(msg);
        }
        if (NEU.test(name)) {
          const msg = name + ' ' + t + ' → ' + (red ? '红(错)' : (grn ? '绿(错)' : '无色'));
          (red || grn ? bad : ok).push(msg);
        }
      });
    });
  });
  return { badN: bad.length, okN: ok.length, bad: bad.slice(0, 6), sample: ok.slice(0, 4) };
})()`);
if (colorSem.missing) {
  check(false, "未找到回测区块，配色语义无法校验");
} else {
  console.log(`  语义合规 ${colorSem.okN} 处` + (colorSem.sample.length ? `，例：${colorSem.sample.join(" | ")}` : ""));
  check(colorSem.badN === 0, `回测配色语义正确：亏损/回撤列未染"上涨红"、胜率等中性列未上色（违规 ${colorSem.badN} 处）`);
  colorSem.bad.forEach((b) => console.log("       ⚠ " + b));
}

// (b) 数字右对齐（主表 td.num/th.num + 回测 .kb-table）
const align = await ev(`(() => {
  const nums = [...document.querySelectorAll('td.num')];
  const heads = [...document.querySelectorAll('th.num')];
  const badTd = nums.filter(td => getComputedStyle(td).textAlign !== 'right');
  const badTh = heads.filter(th => getComputedStyle(th).textAlign !== 'right');
  return { n: nums.length, bad: badTd.length, hn: heads.length, badH: badTh.length };
})()`);
console.log(`  td.num ${align.n} 个（未右对齐 ${align.bad}）｜ th.num ${align.hn} 个（未右对齐 ${align.badH}）`);
check(align.n > 0 && align.bad === 0, `数字单元格全部右对齐（${align.n} 个）`);
check(align.hn === 0 || align.badH === 0, `数字表头也右对齐（${align.hn} 个）`);

// (c) 宽表提示：只在该表确实溢出时显示
const tip = await ev(`(() => {
  const sec = document.getElementById('kb-sec-backtests'); if (!sec) return null;
  return [...sec.querySelectorAll('.kb-bt')].map(b => {
    const sc = b.querySelector('.kb-scroll'), t = b.querySelector('.kb-wide-tip');
    return { over: sc ? sc.scrollWidth > sc.clientWidth + 4 : false, shown: t ? !t.hidden : null };
  });
})()`);
if (tip && tip.length) {
  const need = tip.filter((x) => x.over);
  const shownOk = need.filter((x) => x.shown).length;
  const quietOk = tip.filter((x) => !x.over).filter((x) => x.shown === false).length;
  console.log(`  共 ${tip.length} 张表：${need.length} 张需横向滚动（显示提示 ${shownOk}）、${tip.length - need.length} 张不需（正确隐藏 ${quietOk}）`);
  check(shownOk === need.length, `溢出的表都显示了「可左右滚动」提示（${shownOk}/${need.length}）`);
  check(quietOk === tip.length - need.length, `不溢出的表不显示多余提示（${quietOk}/${tip.length - need.length}）`);
} else {
  check(false, "未取到回测表宽度，宽表提示无法校验");
}

// (d) 汇总面板：调试 dict 已清除 + 超长提示已折叠
await setVp(1440, 1000);
await ev(`location.hash = '#overview/summary'`);
await sleep(2400);
const sum = await ev(`(() => {
  const box = document.getElementById('p-summary');
  const txt = box ? (box.textContent || '') : '';
  const folded = [...document.querySelectorAll('.hint-fold')];
  const btns = [...document.querySelectorAll('.hint-more')];
  const allHints = [...document.querySelectorAll('.hint')];
  return {
    dictHit: /'site'\\s*:|'raw_pages'\\s*:|'trades'\\s*:/.test(txt),
    dictSample: (txt.match(/\\{[^{}]{0,90}\\}/) || [''])[0],
    foldedN: folded.length, btnN: btns.length,
    bodyOk: folded.every(h => !!h.querySelector('.hint-body')),
    totalHints: allHints.length,
    foldWorking: folded.every(h => h.querySelector('.hint-body') && getComputedStyle(h.querySelector('.hint-body')).display !== 'none'),
    hasLastFetch: /最后抓取/.test(txt),
  };
})()`);
console.log(`  汇总面板：dict 残留=${sum.dictHit}${sum.dictHit ? " → " + sum.dictSample : ""}｜提示共 ${sum.totalHints} 段，折叠 ${sum.foldedN} 段（含 body ${sum.bodyOk}、按钮 ${sum.btnN}）｜保留「最后抓取」=${sum.hasLastFetch}`);
check(!sum.dictHit, "汇总面板不再输出后端原始 dict（调试泄漏已清除）");
check(sum.foldedN > 0 && sum.bodyOk && sum.btnN === sum.foldedN,
  `超长提示已折叠且每段都带展开按钮（${sum.foldedN} 段）`);
check(sum.hasLastFetch, "移除调试 dict 后仍保留可读的「最后抓取」时刻");
await shot("ux_p0_summary.png");

/* ============ 4. 总览页截图 ============ */
await setVp(1440, 1000);
await ev(`location.hash = '#overview/summary'`);
await sleep(2600);
await shot("ux_1440_summary.png");
await setVp(1100, 1000);
await sleep(1400);
await shot("ux_1100_summary.png");
await setVp(1920, 1080);
await sleep(1400);
await shot("ux_1920_summary.png");

/* ============ 汇总 ============ */
console.log("\n===== JS 异常 =====");
console.log(errors.length ? errors.map(e => "  ! " + e).join("\n") : "  0 条");
check(errors.length === 0, "控制台无 JS 异常");
console.log(`\n===== ALL PASSED（${pass} 项）=====`.replace("ALL PASSED", fail === 0 ? "ALL PASSED" : "FAILED"));
console.log(`PASS ${pass} / FAIL ${fail}`);

ws.close();
edge.kill();
process.exit(fail ? 1 : 0);
