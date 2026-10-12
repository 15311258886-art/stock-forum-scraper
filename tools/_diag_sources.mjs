/* 一次性诊断：数据源面板的真实 DOM 树 + 每个节点的布局，用来定位「为什么卡片挤成一列」。 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/#acquire/sources";
const PORT = 9342;
const OUT = process.argv[2] || ".";
const profile = mkdtempSync(join(tmpdir(), "edgdiag2_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--disable-gpu",
  "--window-size=1600,1000", BASE,
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
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 500));
  return r.result?.result?.value;
}
await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(4200);
// 确保停在数据源面板
await ev(`location.hash = '#acquire/sources'`);
await sleep(2200);

const tree = await ev(`(() => {
  const panel = document.querySelector('#p-sources');
  if (!panel) return { err: 'no #p-sources' };
  const rows = [];
  const walk = (el, d) => {
    if (d > 3) return;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    rows.push({
      d,
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 42),
      x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height),
      kids: el.children.length,
      disp: cs.display,
      cols: (cs.gridTemplateColumns || '').split(' ').filter(Boolean).length,
      flexDir: cs.flexDirection,
    });
    [...el.children].forEach(c => walk(c, d + 1));
  };
  walk(panel, 0);
  return {
    panelW: Math.round(panel.getBoundingClientRect().width),
    ovSrcGridCount: panel.querySelectorAll('.ov-src-grid').length,
    ovStatusCount: panel.querySelectorAll('.ov-src-status').length,
    rows,
  };
})()`);

console.log(JSON.stringify(tree, null, 1).slice(0, 9000));
ws.close(); edge.kill(); process.exit(0);
