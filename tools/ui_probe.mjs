// 用本机 Edge（无头）+ CDP 驱动真实页面：捕获 JS 运行时错误、断言 Request G 的 DOM，
// 并逐 Tab 截图。仅依赖 Node 22 内置 fetch / WebSocket，无需安装任何依赖。
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CDP_PORT = 9222;
const PAGE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
fs.mkdirSync(OUT, { recursive: true });
const PROFILE = path.join(OUT, "_edgeprofile");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${PROFILE}`,
  "--window-size=1600,1300", "about:blank",
], { stdio: "ignore" });

async function cdpTarget() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const p = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (p) return p.webSocketDebuggerUrl;
    } catch { /* 还没起来 */ }
    await sleep(300);
  }
  throw new Error("CDP target 不可达");
}

const ws = new WebSocket(await cdpTarget());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let seq = 0;
const waiters = new Map();
const jsIssues = [];

ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails || {};
    jsIssues.push("EXCEPTION: " + (d.exception?.description || d.text));
  }
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) {
    jsIssues.push(m.params.type.toUpperCase() + ": " +
      m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq; waiters.set(id, res); ws.send(JSON.stringify({ id, method, params }));
});

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
  { width: 1600, height: 1300, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: PAGE });
await sleep(4000); // 等 loadAll 的多个 fetch 落地

async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("saved", name);
}

const probe = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `(() => {
    const q = (s) => document.querySelector(s);
    const cells = [...document.querySelectorAll('td.td-fresh')];
    const cls = (c) => cells.filter((t) => t.classList.contains(c)).length;
    return {
      refreshBar: !!q('#refreshBar'),
      statusText: q('#rsText')?.textContent ?? null,
      countdown: q('#rsCountdown')?.textContent ?? null,
      dotClass: q('#rsDot')?.className ?? null,
      manualBtn: !!q('#btnRefreshNow'),
      realtimeBox: !!q('#realtime'),
      demoBtn: !!q('#btnDemo'),
      freshCells: cells.length,
      okCells: cls('fr-ok'), staleCells: cls('fr-stale'),
      untrackedCells: cls('fr-untracked'), loadingCells: cls('fr-loading'),
      sampleStale: cells.find((t) => t.classList.contains('fr-stale'))?.textContent ?? null,
      sampleUntracked: cells.find((t) => t.classList.contains('fr-untracked'))?.textContent ?? null,
      tables: {
        operating: !!q('#p-operating table'), consensus: !!q('#p-consensus table'),
        persons: !!q('#p-persons table'), players: !!q('#p-players table'),
      },
      colHeaders: [...document.querySelectorAll('#p-operating thead th')].map((t) => t.textContent),
      stockChips: document.querySelectorAll('#p-stocks .chip-fresh').length,
    };
  })()`,
});
if (probe.error) console.log("PROBE_ERROR " + JSON.stringify(probe.error));
console.log("PROBE " + JSON.stringify(probe.result?.result?.value ?? probe.result?.value ?? null, null, 2));

await shot("ui_01_summary.png");
for (const [tab, file] of [
  ["operating", "ui_02_operating.png"],
  ["consensus", "ui_03_consensus.png"],
  ["persons", "ui_04_persons.png"],
  ["stocks", "ui_05_stocks.png"],
]) {
  await send("Runtime.evaluate",
    { expression: `document.querySelector('#tabs .tab[data-tab="${tab}"]').click()` });
  await sleep(700);
  await shot(file);
}

console.log("JS_ISSUES " + JSON.stringify(jsIssues, null, 2));
ws.close();
edge.kill();
process.exit(jsIssues.some((s) => s.startsWith("EXCEPTION")) ? 1 : 0);
