/* E16 边界：overlap 为空 → 「★ 共同推荐」chip 应为 disabled 不可点。
   构造方式：仅在浏览器运行时把 BLOG.overlap 置空后重绘（**不改数据库**），
   验证完刷新页面即恢复。真实 Chromium(Edge) 无头。 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const CANDS = [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
];
const BROWSER = CANDS.find((p) => fs.existsSync(p));
const BASE = "http://127.0.0.1:8765/";
const OUT = "F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\tools\\_probe_out";
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "edgqa3_"));
const PORT = 9900 + Math.floor(Math.random() * 60);
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const edge = spawn(BROWSER, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--mute-audio", "--no-proxy-server",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, "--window-size=1500,1000", "about:blank"], { stdio: "ignore" });

async function cdp() {
  for (let i = 0; i < 120; i++) {
    try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const x = l.find((y) => y.type === "page" && y.webSocketDebuggerUrl); if (x) return x.webSocketDebuggerUrl; } catch {}
    await sleep(250);
  }
  throw new Error("CDP 未就绪");
}
const ws = new WebSocket(await cdp());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let mid = 0; const pend = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((r) => { const i = ++mid; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
async function ev(x) { const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true }); if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0,300)); return r.result?.result?.value; }
await send("Runtime.enable"); await send("Page.enable");
let pass = 0, fail = 0; const check = (ok, m) => { if (ok) { pass++; console.log("  [PASS] " + m); } else { fail++; console.log("  [FAIL] " + m); } };
async function shot(n){await ev("window.scrollTo(0,0)");await sleep(400);const r=await send("Page.captureScreenshot",{format:"png"});fs.writeFileSync(path.join(OUT,n),Buffer.from(r.result.data,"base64"));console.log("       saved "+path.join(OUT,n));}

await send("Page.navigate", { url: BASE + "#opinion/blogger" });
await sleep(8000);
const before = await ev(`(typeof BLOG!=='undefined'&&BLOG.overlap)?BLOG.overlap.length:null`);
check(before === 22, `初始 overlap=22（实际 ${before}）`);

const r = await ev(`(() => {
  const box=document.querySelector('#p-blogger');
  const c0=box.querySelector('.bt-chip-ovl');
  const t0=c0?c0.textContent.trim():null, d0=c0?c0.classList.contains('disabled'):null;
  BLOG.overlap = []; BLOG.mode='all'; renderBloggerPanel();
  const c=box.querySelector('.bt-chip-ovl');
  const state={ txt:c?c.textContent.trim():null, disabled:c?c.classList.contains('disabled'):null,
    title:c?c.getAttribute('title'):null, ovlCnt:c?c.querySelector('i').textContent.trim():null };
  if(c) c.click();
  const after={ mode:BLOG.mode, cards:document.querySelectorAll('#p-blogger .bt-card-ovl').length };
  return { t0,d0,state,after };
})()`);
console.log("  ", JSON.stringify(r));
check(r.d0 === false, "初始「共同推荐」chip 未禁用（有数据）");
check(r.state.disabled === true, "overlap 置空后 chip 呈 disabled（实际 " + r.state.disabled + "）");
check(r.state.title === '暂无共同推荐数据', "禁用 chip 的 title 改为「暂无共同推荐数据」（实际 " + r.state.title + "）");
check(r.state.ovlCnt === '0', "禁用 chip 计数显示 0");
check(r.after.mode === 'all' && r.after.cards === 0, "禁用 chip 点击无效（mode 仍 all、无共同推荐卡）");
await shot("qa_blogger_E16_overlap_empty.png");

/* 刷新恢复（真实数据回来了） */
await send("Page.reload");
await sleep(8000);
const restored = await ev(`(typeof BLOG!=='undefined'&&BLOG.overlap)?BLOG.overlap.length:null`);
check(restored === 22, `刷新后恢复真实 overlap=22（实际 ${restored}）`);
console.log(`\nPASS ${pass} / FAIL ${fail}`);
console.log(fail === 0 ? "===> ALL PASSED" : "===> HAS FAILURES");
ws.close(); edge.kill();
process.exit(fail ? 1 : 0);
