// 补拍：博主档案抽屉的视口级截图（用于交付展示）。
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CDP_PORT = 9226;
const OUT = process.argv[2] || ".";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const edge = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${path.join(OUT, "_edgeprofile_dr")}`,
  "--window-size=1500,1000", "about:blank"], { stdio: "ignore" });
async function target() {
  for (let i = 0; i < 80; i++) {
    try { const l = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const p = l.find((t) => t.type === "page" && t.webSocketDebuggerUrl); if (p) return p.webSocketDebuggerUrl;
    } catch {} await sleep(300);
  } throw new Error("no cdp");
}
const ws = new WebSocket(await target());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0; const w = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && w.has(m.id)) { w.get(m.id)(m); w.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const id = ++seq; w.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (x) => (await send("Runtime.evaluate", { returnByValue: true, expression: x })).result?.result?.value;
const shot = async (n) => { const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, n), Buffer.from(r.result.data, "base64")); console.log("saved", n); };

await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8765/#learn/learn_zdx_stock" });
await sleep(6000);
// 打开一个"有精读笔记"的博主（北京炒家）与一个"只有视频笔记"的博主（赵老哥）各截一张
for (const [name, file] of [["北京炒家", "kb_v10_drawer_notes.png"], ["赵老哥", "kb_v11_drawer_videoonly.png"]]) {
  const ok = await ev(`(() => { const b=[...document.querySelectorAll('#p-learn_zdx_stock .kb-blogger')]
    .find(x=>x.dataset.blogger==='${name}'); if(!b) return false; b.click(); return true; })()`);
  await sleep(900);
  console.log("  打开", name, ok);
  await shot(file);
  await ev(`document.querySelector('#btnClose').click()`); await sleep(400);
}
ws.close(); edge.kill(); process.exit(0);
