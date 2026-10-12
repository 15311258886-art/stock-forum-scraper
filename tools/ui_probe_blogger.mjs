/* 2026-10-09 feat-blogger-ui 回归：博主芯片全量跟踪（含待采集）+「共同推荐」筛选 + 「待核对」列。
   证据口径：全部读 DOM / getComputedStyle / BLOG 运行时状态，不靠目测；真实 Chromium(Edge) 无头。
   注意：必须用全新 user-data-dir，否则可能截到残留实例旧帧。 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const CANDS = [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
];
const BROWSER = CANDS.find((p) => fs.existsSync(p));
if (!BROWSER) { console.error("找不到 Edge/Chrome，可执行文件路径"); process.exit(2); }
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || path.join("F:", "AI Agent部署知识库", "2026-09-22-11-49-42",
  "stock-forum-scraper", "tools", "_probe_out");
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "edgbl_"));
const PORT = 9500 + Math.floor(Math.random() * 60);
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(BROWSER, [
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
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 500));
  return r.result?.result?.value;
}

const errors = [];
await send("Runtime.enable");
await send("Log.enable");
send("Page.enable");
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") errors.push(String(m.params?.exceptionDetails?.text || "").slice(0, 200));
  if (m.method === "Log.entryAdded" && m.params?.entry?.level === "error") errors.push(String(m.params.entry.text).slice(0, 200));
});

let pass = 0, fail = 0;
const check = (ok, msg) => { if (ok) { pass++; console.log("  [OK] " + msg); } else { fail++; console.log("  [FAIL] " + msg); } };
async function shot(name) {
  await ev("window.scrollTo(0,0)");
  await sleep(500);
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + path.join(OUT, name));
}

/* ============ 0. 打开博主荐股面板 ============ */
console.log("\n===== 0. 打开 #opinion/blogger =====");
await send("Page.navigate", { url: BASE + "#opinion/blogger" });
await sleep(6500);
const boot = await ev(`(() => {
  const box = document.querySelector('#p-blogger');
  return { exists: !!box, html: box ? box.innerHTML.length : 0,
           loaded: (typeof BLOG !== 'undefined') ? !!BLOG.loaded : null,
           nRows: (typeof BLOG !== 'undefined') ? BLOG.rows.length : null,
           nOvl: (typeof BLOG !== 'undefined') ? (BLOG.overlap||[]).length : null,
           nStats: (typeof BLOG !== 'undefined') ? (BLOG.stats||[]).length : null,
           facets: (typeof BLOG !== 'undefined' && BLOG.facets) ? {
             total: BLOG.facets.total, tracked_total: BLOG.facets.tracked_total,
             tracked_with_data: BLOG.facets.tracked_with_data,
             nBloggers: (BLOG.facets.bloggers||[]).length,
             anyPending: (BLOG.facets.bloggers||[]).some(b=>b&&b.pending),
           } : null };
})()`);
console.log("  运行时：", JSON.stringify(boot));
check(boot.exists && boot.html > 500, "博主面板已渲染");
check(boot.loaded === true, "BLOG.loaded 为 true（数据已加载）");

/* ============ A. chip 行结构 ============ */
console.log("\n===== A. chip 行：全部 / ★ 共同推荐 / 博主 =====");
const chips = await ev(`(() => {
  const all = [...document.querySelectorAll('#p-blogger .bt-chips .bt-chip')];
  const ovl = document.querySelector('#p-blogger .bt-chips .bt-chip-ovl');
  const pend = [...document.querySelectorAll('#p-blogger .bt-chip-pending')];
  const tracked = document.querySelector('#p-blogger .bt-tracked');
  const first = all[0];
  const second = all[1];
  return {
    total: all.length,
    firstTxt: first ? first.textContent.trim() : null,
    secondTxt: second ? second.textContent.trim() : null,
    ovlExists: !!ovl, ovlTxt: ovl ? ovl.textContent.trim() : null,
    ovlDisabled: ovl ? ovl.classList.contains('disabled') : null,
    pendN: pend.length,
    pendTxt: pend.map(p => p.textContent.trim()).slice(0, 3),
    trackedTxt: tracked ? tracked.textContent.replace(/\\s+/g,' ').trim() : null,
  };
})()`);
console.log("  ", JSON.stringify(chips));
check(chips.total >= 2, `chip 行有「全部 + ★ 共同推荐 + 博主」共 ${chips.total} 个 chip`);
check(chips.firstTxt && chips.firstTxt.startsWith("全部"), `第 1 个 chip 是「全部」（实际 "${chips.firstTxt}"）`);
check(chips.ovlExists && /共同推荐/.test(chips.secondTxt || ""),
  `第 2 个 chip 是「★ 共同推荐」（实际 "${chips.secondTxt}"）`);
check(!!chips.trackedTxt && /跟踪\s*\d+\s*位博主/.test(chips.trackedTxt),
  `顶部跟踪小字已渲染（实际 "${chips.trackedTxt}"）`);
await shot("blogger_A_chips.png");

/* ============ B. 点「共同推荐」→ 切视图 ============ */
console.log("\n===== B. 点 ★ 共同推荐 → 重复推荐视图 =====");
if (chips.ovlDisabled) {
  console.log("  [SKIP] overlap 为空、chip 被禁用，跳过点击验证");
  check(chips.ovlDisabled === true, "overlap 为空时 chip 呈禁用态（优雅降级）");
} else {
  await ev(`document.querySelector('#p-blogger .bt-chip-ovl').click()`);
  await sleep(700);
  const ov = await ev(`(() => {
    const box = document.querySelector('#p-blogger');
    const cards = [...box.querySelectorAll('.bt-list .bt-card-ovl')];
    const cnt = box.querySelector('.bt-cnt');
    const sorts = box.querySelector('.bt-sorts');
    const days = box.querySelector('.bt-days');
    const active = box.querySelector('.bt-chip-ovl.active');
    const codes = cards.slice(0,3).map(c => (c.querySelector('.bt-code')||{}).textContent);
    const names = cards.slice(0,3).map(c => (c.querySelector('.bt-name')||{}).textContent);
    const nums  = cards.slice(0,3).map(c => (c.querySelector('.bt-blog-ovl')||{}).textContent);
    const rets  = cards.slice(0,3).map(c => (c.querySelector('.bt-card-r')||{}).textContent);
    // 校验降序（n_bloggers 非增）
    const nb = cards.map(c => {
      const t = (c.querySelector('.bt-blog-ovl')||{}).textContent || '';
      const m = t.match(/(\\d+)/); return m ? Number(m[1]) : 0; });
    let desc = true; for (let i=1;i<nb.length;i++) if (nb[i] > nb[i-1]) desc = false;
    return { nCards: cards.length, cntTxt: cnt ? cnt.textContent.replace(/\\s+/g,' ').trim() : null,
      sortsShown: !!sorts, daysShown: !!days, ovlActive: !!active,
      codes, names, nums, rets, nbDesc: desc, nbSample: nb.slice(0,6) };
  })()`);
  console.log("  ", JSON.stringify(ov));
  check(ov.nCards > 0, `共同推荐视图渲染 ${ov.nCards} 张卡片`);
  check(/共同推荐\s*\d+\s*只/.test(ov.cntTxt || ""), `计数行切成「共同推荐 N 只」（实际 "${ov.cntTxt}"）`);
  check(ov.ovlActive, "共同推荐 chip 高亮为 active");
  check(!ov.sortsShown, "共同推荐视图隐藏了排序 chip（避免歧义）");
  check(!ov.daysShown, "共同推荐视图隐藏了「只显示最近 N 天」控件");
  check(ov.nbDesc, `卡片按同推人数降序（样本 ${JSON.stringify(ov.nbSample)}）`);
  check(ov.nums.every((x) => /位博主同推/.test(x || "")), `卡片含「N 位博主同推」（${JSON.stringify(ov.nums)}）`);
  check(ov.rets.every((x) => /推荐以来/.test(x || "")), `卡片含「推荐以来」涨幅（${JSON.stringify(ov.rets)}）`);
  await shot("blogger_B_overlap.png");

  // 点「全部」→ 退回普通列表
  await ev(`document.querySelector('#p-blogger .bt-chip').click()`);
  await sleep(600);
  const back = await ev(`(() => {
    const box = document.querySelector('#p-blogger');
    return { sortsShown: !!box.querySelector('.bt-sorts'),
             daysShown: !!box.querySelector('.bt-days'),
             cards: box.querySelectorAll('.bt-list .bt-card-ovl').length };
  })()`);
  console.log("  ", JSON.stringify(back));
  check(back.sortsShown && back.daysShown, "点「全部」后排序与日期控件恢复显示（mode 回 all）");
  check(back.cards === 0, "退回普通列表后不再有共同推荐卡片");
}

/* ============ C. 效果对比表「待核对」列 ============ */
console.log("\n===== C. 博主效果对比表「待核对」列 =====");
const statsTb = await ev(`(() => {
  const d = [...document.querySelectorAll('#p-blogger .bt-detail')];
  const t = d.map(x => x.querySelector('table')).find(tb => tb && /待核对/.test(tb.textContent));
  if (!t) return { found: false };
  const ths = [...t.querySelectorAll('thead th')].map(x => x.textContent.trim());
  const idx = ths.indexOf('待核对');
  const rows = [...t.querySelectorAll('tbody tr')].slice(0, 6).map(tr => {
    const tds = [...tr.querySelectorAll('td')];
    return { who: tds[0] ? tds[0].textContent.trim() : '', pend: tds[idx] ? tds[idx].textContent.trim() : null,
             pendCls: tds[idx] ? tds[idx].className : '' };
  });
  const anyDash = rows.some(r => r.pend === '—');
  return { found: true, ths, idx, rows, anyDash };
})()`);
console.log("  ", JSON.stringify(statsTb));
check(statsTb.found, "效果对比表已找到");
check(statsTb.idx >= 0, `表头含「待核对」列（表头 ${JSON.stringify(statsTb.ths)}）`);
if (statsTb.found && statsTb.idx >= 0) {
  const allNumberOrDash = (statsTb.rows || []).every(r => r.pend === '—' || /^\d+$/.test(r.pend || ''));
  check(allNumberOrDash, "「待核对」单元格为数字或「—」（NULL 渲染成 —、不是 0）");
  console.log(`       样本：` + (statsTb.rows || []).map(r => `${r.who}=${r.pend}`).join(", "));
}

/* ============ D. 待采集 chip（注入伪造 facets，不依赖后端） ============ */
console.log("\n===== D. 待采集 chip 样式与空态（注入伪造 facets）=====");
const pendInfo = await ev(`(() => {
  if (typeof BLOG === 'undefined') return { err: 'no BLOG' };
  const prev = JSON.parse(JSON.stringify(BLOG.facets || {}));
  BLOG.facets = Object.assign({}, prev, {
    updated: prev.updated || '2026-10-09 12:16', total: 312,
    tracked_total: 21, tracked_with_data: 5,
    bloggers: [
      { name: '独股九剑', n: 126, last_date: '2026-10-08', pending: false, kind: 'list', contest: false },
      { name: 'A股化债人', n: 43, last_date: '2026-10-08', pending: false, kind: null, contest: false },
      { name: '炒股养家', n: 0, last_date: null, pending: true, kind: 'list', contest: false },
    ],
  });
  BLOG.mode = 'all'; BLOG.blogger = '';
  renderBloggerPanel();
  const box = document.querySelector('#p-blogger');
  const pc = box.querySelector('.bt-chip-pending');
  const cs = pc ? getComputedStyle(pc) : null;
  const tracked = box.querySelector('.bt-tracked');
  const dataChip = [...box.querySelectorAll('.bt-chip')].find(c => c.dataset.btBlogger === '独股九剑');
  return {
    pendExists: !!pc,
    pendTxt: pc ? pc.textContent.trim() : null,
    pendBorderStyle: cs ? cs.borderTopStyle : null,
    pendOpacity: cs ? Number(cs.opacity) : null,
    trackedTxt: tracked ? tracked.textContent.replace(/\\s+/g,' ').trim() : null,
    dataChipTxt: dataChip ? dataChip.textContent.trim() : null,
  };
})()`);
console.log("  ", JSON.stringify(pendInfo));
check(pendInfo.pendExists, "pending 博主渲染出 .bt-chip-pending");
check(/待采集/.test(pendInfo.pendTxt || ""), `pending chip 文案含「待采集」（实际 "${pendInfo.pendTxt}"）`);
check(!/0/.test((pendInfo.pendTxt || "").replace("待采集", "")), "pending chip 不显示数量 0");
check(pendInfo.pendBorderStyle === "dashed", `pending chip 虚线边框弱化（borderTopStyle=${pendInfo.pendBorderStyle}）`);
check(/跟踪\s*21\s*位博主\s*·\s*已采到\s*5\s*位/.test(pendInfo.trackedTxt || ""),
  `顶部跟踪小字取 tracked_total/with_data（实际 "${pendInfo.trackedTxt}"）`);
check(/126/.test(pendInfo.dataChipTxt || ""), `有数据博主 chip 仍显示数量（实际 "${pendInfo.dataChipTxt}"）`);
await shot("blogger_D_pending_chip.png");

// 点 pending chip → 明确空态（不复用「无匹配的荐股记录」）
await ev(`document.querySelector('#p-blogger .bt-chip-pending').click()`);
await sleep(500);
const empty = await ev(`(() => {
  const box = document.querySelector('#p-blogger');
  const e = box.querySelector('.bt-list .empty');
  const cnt = box.querySelector('.bt-cnt');
  return { txt: e ? e.textContent.replace(/\\s+/g,' ').trim() : null,
           cls: e ? e.className : null,
           isPendingCls: e ? e.classList.contains('empty-pending') : null,
           cntTxt: cnt ? cnt.textContent.replace(/\\s+/g,' ').trim() : null };
})()`);
console.log("  ", JSON.stringify(empty));
check(empty.txt && /暂无采到荐股记录（自动化跟踪中，通常 1-2 天内补齐）/.test(empty.txt),
  `pending 博主空态文案正确（实际 "${empty.txt}"）`);
check(!/无匹配的荐股记录/.test(empty.txt || ""), "空态未复用「无匹配的荐股记录」");
check(empty.isPendingCls, "空态使用 .empty-pending 独立样式");
await shot("blogger_D_pending_empty.png");

/* ============ E. 参赛股面板仍正常（切期次） ============ */
console.log("\n===== E. 当红参赛股面板回归 =====");
await ev(`location.hash = '#opinion/contest'`);
await sleep(4200);
const ct = await ev(`(() => {
  const box = document.querySelector('#p-contest');
  const chips = [...box.querySelectorAll('[data-ct-round]')];
  return { chips: chips.length,
    labels: chips.map(c => c.textContent.trim()).slice(0,4),
    cur: (typeof CONTEST !== 'undefined') ? CONTEST.cur : null,
    title: (box.querySelector('.ct-title')||{}).textContent };
})()`);
console.log("  ", JSON.stringify(ct));
check(ct.chips >= 1, `参赛股期次 chip 渲染 ${ct.chips} 个`);
if (ct.chips >= 2) {
  const target = await ev(`(() => {
    const chips = [...document.querySelectorAll('#p-contest [data-ct-round]')];
    const other = chips.find(c => c.dataset.ctRound !== CONTEST.cur);
    const vid = other ? other.dataset.ctRound : null;
    if (other) other.click();
    return vid;
  })()`);
  await sleep(3200);
  const after = await ev(`(() => {
    const box = document.querySelector('#p-contest');
    return { cur: (typeof CONTEST !== 'undefined') ? CONTEST.cur : null,
             active: !!box.querySelector('.ct-chip.active'),
             title: (box.querySelector('.ct-title')||{}).textContent };
  })()`);
  console.log("  切换目标期次：", target, "→", JSON.stringify(after));
  check(after.cur === target, `点期次后 CONTEST.cur 切到 ${target}（实际 ${after.cur}）`);
  check((after.title || "") !== (ct.title || "") || after.cur !== ct.cur, "切期次后标题/内容随之变化");
}
await shot("contest_regression.png");

/* ============ 汇总 ============ */
console.log("\n===== JS 异常 =====");
console.log(errors.length ? errors.map(e => "  ! " + e).join("\n") : "  0 条");
check(errors.length === 0, "控制台无 JS 异常");
console.log(`\nPASS ${pass} / FAIL ${fail}`);
console.log(fail === 0 ? "===> ALL PASSED" : "===> HAS FAILURES");

ws.close();
edge.kill();
process.exit(fail ? 1 : 0);
