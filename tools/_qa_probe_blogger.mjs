/* QA 独立验证探针（qa-blogger-align）——全用**真实后端数据**、不做任何注入。
   v2：修正 v1 中探针自身的两处缺陷（正则过度转义 \s、卡片点击/参赛人数选择器写错）。
   真实 Chromium(Edge) 无头 + CDP。截图写入 tools/_probe_out/qa_*.png。 */
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
if (!BROWSER) { console.error("找不到 Edge/Chrome"); process.exit(2); }
const BASE = "http://127.0.0.1:8765/";
const OUT = "F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\tools\\_probe_out";
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "edgqa2_"));
const PORT = 9800 + Math.floor(Math.random() * 60);
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(BROWSER, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio", "--no-proxy-server",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  "--window-size=1600,1100", "about:blank",
], { stdio: "ignore" });

async function cdpTarget() {
  for (let i = 0; i < 120; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const x = l.find((y) => y.type === "page" && y.webSocketDebuggerUrl);
      if (x) return x.webSocketDebuggerUrl;
    } catch { /* not up */ }
    await sleep(250);
  }
  throw new Error("CDP 未就绪");
}

const ws = new WebSocket(await cdpTarget());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let mid = 0;
const pend = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((r) => { const i = ++mid; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
async function ev(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
}

const errors = [];
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") errors.push("EXC " + String(m.params?.exceptionDetails?.text || "").slice(0, 200));
  if (m.method === "Log.entryAdded" && m.params?.entry?.level === "error") errors.push("LOG " + String(m.params.entry.text).slice(0, 200));
});

let pass = 0, fail = 0;
const check = (ok, msg) => { if (ok) { pass++; console.log("  [PASS] " + msg); } else { fail++; console.log("  [FAIL] " + msg); } };
async function shot(name) {
  await ev("window.scrollTo(0,0)");
  await sleep(400);
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + path.join(OUT, name));
}

/* ============ 0. 打开博主荐股面板（真实数据） ============ */
console.log("\n===== 0. 打开 #opinion/blogger（真实后端）=====");
await send("Page.navigate", { url: BASE + "#opinion/blogger" });
await sleep(8000);
const boot = await ev(`(() => {
  const b = (typeof BLOG!=='undefined') ? BLOG : null;
  return { hasBLOG: !!b, loaded: b?!!b.loaded:null,
    rows: b?(b.rows||[]).length:null, ovl: b?(b.overlap||[]).length:null, stats: b?(b.stats||[]).length:null,
    facets: b&&b.facets?{ total:b.facets.total, tracked_total:b.facets.tracked_total,
      tracked_with_data:b.facets.tracked_with_data, nBloggers:(b.facets.bloggers||[]).length } : null };
})()`);
console.log("  运行态：", JSON.stringify(boot));
check(boot.hasBLOG && boot.loaded === true, "博主面板数据已加载 (BLOG.loaded=true)");
check(boot.facets && boot.facets.nBloggers === 22, `facets.bloggers 前端=22（实际 ${boot.facets && boot.facets.nBloggers}）`);
check(boot.ovl === 22, `前端 overlap=22（实际 ${boot.ovl}）`);

/* ============ C8. chip 行（真实） ============ */
console.log("\n===== C8. chip 行结构（真实） =====");
const chips = await ev(`(() => {
  const box = document.querySelector('#p-blogger');
  const all = [...box.querySelectorAll('.bt-chips .bt-chip')];
  const ovl = box.querySelector('.bt-chip-ovl');
  const pend = [...box.querySelectorAll('.bt-chip-pending')];
  const dataChips = all.filter(c => c.dataset.btBlogger && !c.classList.contains('bt-chip-pending'));
  const tracked = box.querySelector('.bt-tracked');
  return {
    nChips: all.length,
    firstTxt: all[0]?all[0].textContent.trim():null,
    ovlTxt: ovl?ovl.textContent.trim():null, ovlDisabled: ovl?ovl.classList.contains('disabled'):null,
    nPend: pend.length, pendSample: pend.slice(0,3).map(p=>p.textContent.trim()),
    nData: dataChips.length, dataSample: dataChips.slice(0,3).map(c=>c.textContent.trim()),
    trackedTxt: tracked?tracked.textContent.replace(/\s+/g,' ').trim():null,
  };
})()`);
console.log("  ", JSON.stringify(chips));
check(chips.nChips === 24, `chip 总数=24（全部+共同推荐+22博主，实际 ${chips.nChips}）`);
check(/^全部/.test(chips.firstTxt||""), `首个 chip 是「全部」（实际 "${chips.firstTxt}"）`);
check(/共同推荐/.test(chips.ovlTxt||"") && /22/.test(chips.ovlTxt||""), `「★ 共同推荐」chip 计数=22（实际 "${chips.ovlTxt}"）`);
check(chips.ovlDisabled === false, "「共同推荐」chip 未禁用（overlap=22）");
check(chips.nData === 5, `有数据博主 chip=5（实际 ${chips.nData}）`);
check(chips.nPend === 17, `「待采集」chip=17（实际 ${chips.nPend}）`);
check(chips.pendSample.every(t=>/待采集/.test(t)), `待采集 chip 文案形如「X 待采集」（${JSON.stringify(chips.pendSample)}）`);
check(chips.pendSample.every(t=>!/\d/.test(t.replace('待采集',''))), "待采集 chip 不显示数量（无数字）");
check(/跟踪\s*21\s*位博主\s*·\s*已采到\s*5\s*位/.test(chips.trackedTxt||""), `顶部提示「跟踪 21 位博主 · 已采到 5 位」（实际 "${chips.trackedTxt}"）`);
await shot("qa_blogger_C8_chips.png");

/* ============ C8b. 点「★ 共同推荐」→ 22 卡视图 ============ */
console.log("\n===== C8b. 共同推荐视图（真实 22 卡） =====");
await ev(`document.querySelector('#p-blogger .bt-chip-ovl').click()`);
await sleep(900);
const ov = await ev(`(() => {
  const box = document.querySelector('#p-blogger');
  const cards = [...box.querySelectorAll('.bt-list .bt-card-ovl')];
  const cnt = box.querySelector('.bt-cnt');
  const bound = cards.filter(c => typeof c.onclick === 'function' && /^\\d{6}$/.test(c.dataset.btSym||''));
  const nb = cards.map(c=>{const t=(c.querySelector('.bt-blog-ovl')||{}).textContent||'';const m=t.match(/(\\d+)/);return m?Number(m[1]):0;});
  let desc=true; for(let i=1;i<nb.length;i++) if(nb[i]>nb[i-1]) desc=false;
  return { nCards: cards.length, sortsShown: !!box.querySelector('.bt-sorts'), daysShown: !!box.querySelector('.bt-days'),
    cntTxt: cnt?cnt.textContent.replace(/\s+/g,' ').trim():null, nbDesc: desc, nbSample: nb.slice(0,5),
    allHaveSame: cards.every(c=>/位博主同推/.test((c.querySelector('.bt-blog-ovl')||{}).textContent||'')),
    nBound: bound.length, firstSym: cards[0]?cards[0].dataset.btSym:null };
})()`);
console.log("  ", JSON.stringify(ov));
check(ov.nCards === 22, `共同推荐视图 22 张卡（实际 ${ov.nCards}）`);
check(/共同推荐\s*22\s*只/.test(ov.cntTxt||""), `计数行「共同推荐 22 只」（实际 "${ov.cntTxt}"）`);
check(ov.allHaveSame, "每卡含「N 位博主同推」");
check(ov.nbDesc, `卡片按同推人数降序（样本 ${JSON.stringify(ov.nbSample)}）`);
check(!ov.sortsShown && !ov.daysShown, "共同推荐视图隐藏排序与「最近 N 天」控件");
check(ov.nBound === 22, `22 张卡均绑定个股点击(data-bt-sym+onclick，实际 ${ov.nBound})`);
/* 真实点第一张卡 → 开个股抽屉 */
const opened = await ev(`(() => {
  const c=document.querySelector('#p-blogger .bt-card-ovl'); const sym=c?c.dataset.btSym:null;
  if(c) c.click();
  return sym;
})()`);
await sleep(2500);
const drawer = await ev(`(() => {
  const d=document.querySelector('#drawer');
  const open=d?d.classList.contains('show'):null;
  const body=(document.querySelector('#drawerBody')||{}).innerText||'';
  return { open, bodyLen:body.length, hasSym:body.includes(${JSON.stringify(opened)}) };
})()`);
console.log("  点卡：", opened, "→ 抽屉", JSON.stringify(drawer));
check(drawer.open === true && drawer.bodyLen > 20, `点共同推荐卡可打开个股抽屉（open=${drawer.open}, len=${drawer.bodyLen}）`);
await shot("qa_blogger_C8b_overlap.png");
/* 关抽屉 */
await ev(`(()=>{const b=document.querySelector('#btnClose'); if(b)b.click();})()`);
await sleep(500);

/* 点「全部」恢复 */
await ev(`document.querySelector('#p-blogger .bt-chip').click()`);
await sleep(600);
const back = await ev(`(() => { const box=document.querySelector('#p-blogger');
  return { sorts:!!box.querySelector('.bt-sorts'), days:!!box.querySelector('.bt-days'),
    cards:box.querySelectorAll('.bt-card-ovl').length }; })()`);
check(back.sorts && back.days && back.cards === 0, "点「全部」恢复常规列表（排序/N天控件回来、无共同推荐卡）");

/* ============ C8c. 点真实「待采集」chip → 空态 ============ */
console.log("\n===== C8c. 点真实待采集 chip → 明确空态 =====");
const pendName = await ev(`(() => { const p=document.querySelector('#p-blogger .bt-chip-pending');
  const n=p?p.dataset.btBlogger:null; if(p)p.click(); return n; })()`);
await sleep(600);
const empty = await ev(`(() => { const box=document.querySelector('#p-blogger');
  const e=box.querySelector('.bt-list .empty');
  return { txt:e?e.textContent.replace(/\s+/g,' ').trim():null,
    isPending:e?e.classList.contains('empty-pending'):null }; })()`);
console.log("  待采集博主：", pendName, "→", JSON.stringify(empty));
check(!!pendName, `点到真实待采集博主（${pendName}）`);
check((empty.txt||"").includes(pendName||"\u0000"), "空态文案含该博主名");
check(/自动化跟踪中/.test(empty.txt||""), `空态含「自动化跟踪中」（实际 "${empty.txt}"）`);
check(!/无匹配的荐股记录/.test(empty.txt||""), "空态未复用「无匹配的荐股记录」");
check(empty.isPending === true, "空态用 .empty-pending 独立样式");
await shot("qa_blogger_C8c_pending_empty.png");

/* ============ C8d. 效果对比表「待核对」列（真实 n_pending） ============ */
console.log("\n===== C8d. 效果对比表「待核对」列（真实） =====");
const statsTb = await ev(`(() => {
  const d=[...document.querySelectorAll('#p-blogger .bt-detail')];
  const t=d.map(x=>x.querySelector('table')).find(tb=>tb && /待核对/.test(tb.textContent));
  if(!t) return {found:false};
  const ths=[...t.querySelectorAll('thead th')].map(x=>x.textContent.trim());
  const idx=ths.indexOf('待核对');
  const rows=[...t.querySelectorAll('tbody tr')].map(tr=>{const tds=[...tr.querySelectorAll('td')];
    const who=tds[0]?tds[0].textContent.trim():'';
    return {who, n_stock:tds[1]?tds[1].textContent.trim():null, pend:tds[idx]?tds[idx].textContent.trim():null};});
  return {found:true, ths, idx, rows};
})()`);
console.log("  ", JSON.stringify(statsTb));
check(statsTb.found && statsTb.idx>=0, `效果对比表含「待核对」列（表头 ${JSON.stringify(statsTb.ths)}）`);
const djj = (statsTb.rows||[]).find(r=>r.who==='独股九剑');
check(djj && djj.pend === '73', `独股九剑「待核对」=73（实际 ${djj?djj.pend:null}）`);
const sumr = (statsTb.rows||[]).find(r=>/合计/.test(r.who));
check(sumr && sumr.pend === '73', `合计行「待核对」=73（实际 ${sumr?sumr.pend:null}）`);
await shot("qa_blogger_C8d_stats.png");

/* ============ C10. 前端数字 ↔ 接口/DB 抽样对数 ============ */
console.log("\n===== C10. 前端数字 ↔ 接口 (5 抽样) =====");
const be = await (await fetch(BASE + "api/blogger/overlap")).json();
const bf = await (await fetch(BASE + "api/blogger/facets")).json();
const bs = await (await fetch(BASE + "api/blogger/stats")).json();
const cr = await (await fetch(BASE + "api/contest/rounds")).json();
check(ov.nCards === be.rows.length, `[1] 前端共同推荐卡数(${ov.nCards}) == 接口 overlap 行数(${be.rows.length})`);
check(Number((chips.ovlTxt||'').replace(/\D/g,'')) === be.rows.length, `[2] 共同推荐 chip 计数(${(chips.ovlTxt||'').replace(/\D/g,'')}) == overlap 行数(${be.rows.length})`);
check(boot.facets.total === bf.total && boot.facets.total === 312, `[3] 全部 chip 计数(${boot.facets.total}) == facets.total(${bf.total})`);
check(boot.facets.tracked_total === 21 && boot.facets.tracked_with_data === 5, `[4] 跟踪口径(${boot.facets.tracked_total}/${boot.facets.tracked_with_data}) == 21/5`);
check(cr.rows.length === 2, `[5] 参赛期数(${cr.rows.length}) == 2`);
const djjStat = (bs.rows.find(r=>r.blogger==='独股九剑')||{});
check(djj && Number(djj.pend) === djjStat.n_pending, `[额外] 独股九剑前端待核对(${djj?djj.pend:null}) == 接口 n_pending(${djjStat.n_pending})`);

/* ============ C9. 参赛股面板（真实切期） ============ */
console.log("\n===== C9. 当红参赛股面板 =====");
await ev(`location.hash = '#opinion/contest'`);
await sleep(5000);
const ct = await ev(`(() => {
  const box=document.querySelector('#p-contest');
  const chips=[...box.querySelectorAll('[data-ct-round]')];
  const cur=(typeof CONTEST!=='undefined')?CONTEST.cur:null;
  const detailRows=(typeof CONTEST!=='undefined'&&CONTEST.detail&&CONTEST.detail.rows)?CONTEST.detail.rows.length:null;
  const rankDom=box.querySelectorAll('table.ct-tb tbody tr').length;
  const trackDom=box.querySelectorAll('table.ct-tb2 tbody tr').length;
  const dates=(typeof CONTEST!=='undefined'&&CONTEST.detail&&CONTEST.detail.dates)?CONTEST.detail.dates.length:0;
  return { nChips:chips.length, labels:chips.map(c=>c.textContent.trim()), cur, detailRows, rankDom, trackDom, dates,
    title:(box.querySelector('.ct-title')||{}).textContent };
})()`);
console.log("  ", JSON.stringify(ct));
check(ct.nChips === 2, `参赛期次 chip=2（实际 ${ct.nChips}：${JSON.stringify(ct.labels)}）`);
check(ct.cur === '7690527564850302251', `默认最新期=9-28（actual ${ct.cur}）`);
check(ct.detailRows === 12 && ct.rankDom === 12, `默认期 12 名参赛者（detail=${ct.detailRows}, 名单表=${ct.rankDom}）`);
check(ct.trackDom === 12 && ct.dates >= 1, `逐日追踪网格 12 行 × ${ct.dates} 日`);
/* 切到 9-19 */
const switched = await ev(`(() => {
  const chips=[...document.querySelectorAll('#p-contest [data-ct-round]')];
  const other=chips.find(c=>c.dataset.ctRound!==CONTEST.cur);
  const vid=other?other.dataset.ctRound:null; if(other)other.click(); return vid; })()`);
await sleep(3500);
const after = await ev(`(() => { const box=document.querySelector('#p-contest');
  return { cur:(typeof CONTEST!=='undefined')?CONTEST.cur:null, active:!!box.querySelector('.ct-chip.active'),
    detailRows:(typeof CONTEST!=='undefined'&&CONTEST.detail&&CONTEST.detail.rows)?CONTEST.detail.rows.length:null,
    title:(box.querySelector('.ct-title')||{}).textContent }; })()`);
console.log("  切期次：", switched, "→", JSON.stringify(after));
check(after.cur === switched, `点 9-19 后切期成功（${switched}）`);
check(after.active && after.detailRows === 12, "切换后 chip 高亮 + 该期 12 人");
await shot("qa_contest_round.png");

/* ============ E14. 其它面板回归（切 4 个，看可见面板自身内容） ============ */
console.log("\n===== E14. 其它面板回归 =====");
const routes = ["#market/overview", "#opinion/cohold", "#data/diag", "#watch/list"];
const panelResults = [];
for (const p of routes) {
  await ev(`location.hash = ${JSON.stringify(p)}`);
  await sleep(2600);
  const r = await ev(`(() => {
    const vis=[...document.querySelectorAll('[id^="p-"]')].filter(x=>x.offsetParent!==null);
    const one=vis[0]||null;
    return { hash:location.hash, visCount:vis.length, visId: one?one.id:null,
      len: one?one.innerText.length:0 };
  })()`);
  panelResults.push(r);
  console.log("  ", JSON.stringify(r));
}
check(panelResults.every(r=>r.visCount === 1 && r.len > 40), "每次恰 1 个面板可见且有实质内容（未白屏）");
check(new Set(panelResults.map(r=>r.visId)).size >= 3, `4 个路由落到 ≥3 个不同面板（${JSON.stringify(panelResults.map(r=>r.visId))}）`);
await shot("qa_other_panel.png");

/* ============ E13. console 异常 ============ */
console.log("\n===== E13. JS 异常 =====");
console.log(errors.length ? errors.map(e=>"  ! "+e).join("\n") : "  0 条");
check(errors.length === 0, `控制台无 JS 异常（${errors.length} 条）`);

console.log(`\nPASS ${pass} / FAIL ${fail}`);
console.log(fail === 0 ? "===> ALL PASSED" : "===> HAS FAILURES");
ws.close(); edge.kill();
process.exit(fail ? 1 : 0);
