/* 定点核验：博主卡上的「精读 / 视频 / 仅视频」标注是否与数据一致。
   零依赖：Node 22 内置 WebSocket 直连系统 Edge 的 CDP。 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const URL_ = "http://127.0.0.1:8765/";
const PORT = 9333;
const outDir = process.argv[2] || ".";
const profile = mkdtempSync(join(tmpdir(), "edgevo_"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = [];

const edge = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--window-size=1600,1000", "--hide-scrollbars", URL_,
], { stdio: "ignore" });

async function targetWs() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const t = list.find((x) => x.type === "page" && x.webSocketDebuggerUrl);
      if (t) return t.webSocketDebuggerUrl;
    } catch {}
    await sleep(250);
  }
  throw new Error("CDP 未就绪");
}

const ws = new WebSocket(await targetWs());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pend = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
function send(method, params = {}) {
  const i = ++id;
  return new Promise((res) => { pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
}
async function evalJs(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  return r.result?.result?.value;
}
function check(ok, msg) { console.log(`  [${ok ? "OK" : "FAIL"}] ${msg}`); if (!ok) fails.push(msg); }

await send("Page.enable");
await send("Runtime.enable");
await sleep(2500);
await evalJs(`location.hash = "#learn/learn_zdx_stock"`);
await sleep(3000);

console.log("\n[核验] 博主卡标注 vs 后端数据");
const dom = await evalJs(`(() => {
  const cards = [...document.querySelectorAll(".kb-blogger")];
  const rows = cards.map((c) => {
    const txt = [...c.querySelectorAll(".kb-tag")].map(t => t.textContent.trim());
    return { name: c.querySelector(".kb-blogger-n")?.textContent.trim(),
             tags: txt,
             only: txt.includes("仅视频"),
             hasNotes: txt.some(t => t.startsWith("精读")),
             hasVideo: txt.some(t => t.startsWith("视频")) };
  });
  return { total: rows.length, rows };
})()`);

const api = await (await fetch("http://127.0.0.1:8765/api/kb/doc?key=zdx_stock")).json();
const expect = [];
for (const s of api.schools) for (const b of s.bloggers)
  expect.push({ name: b.name, n: b.phase_notes.length, v: b.phase_video.length });

check(dom.total === 50, `博主卡 50 张（实得 ${dom.total}）`);
const onlyDom = dom.rows.filter((r) => r.only).map((r) => r.name).sort();
const onlyExp = expect.filter((e) => e.n === 0 && e.v > 0).map((e) => e.name).sort();
check(onlyDom.length === onlyExp.length && onlyDom.join("|") === onlyExp.join("|"),
  `「仅视频」标注人数 = 上游无精读字段者（DOM ${onlyDom.length} vs 数据 ${onlyExp.length}）`);
check(onlyDom.length > 0, `确有仅视频博主（${onlyDom.slice(0, 4).join(" / ")}${onlyDom.length > 4 ? " …" : ""}）`);

// 逐位核对「精读 N」「视频 N」的数字
const byName = new Map(dom.rows.map((r) => [r.name, r]));
let mismatch = [];
for (const e of expect) {
  const r = byName.get(e.name);
  if (!r) { mismatch.push([e.name, "卡片缺失"]); continue; }
  const gn = (r.tags.find((t) => t.startsWith("精读")) || "").replace(/\D/g, "");
  const gv = (r.tags.find((t) => t.startsWith("视频")) || "").replace(/\D/g, "");
  if (String(e.n || "") !== gn && !(e.n === 0 && gn === "")) mismatch.push([e.name, `精读 ${gn}≠${e.n}`]);
  if (String(e.v || "") !== gv && !(e.v === 0 && gv === "")) mismatch.push([e.name, `视频 ${gv}≠${e.v}`]);
}
check(mismatch.length === 0, `50 位博主字段数逐个与后端一致（不符：${JSON.stringify(mismatch.slice(0, 5))}）`);

// 旧的冗余「▶」标记应已消失（原先 50/50 全带，属噪声）
const stale = await evalJs(`document.querySelectorAll(".kb-tag-v").length`);
check(stale === onlyExp.length, `.kb-tag-v（仅视频样式）只出现在 ${onlyExp.length} 位，不再全量出现`);

ws.close();
edge.kill();
console.log("\n" + "=".repeat(56));
if (fails.length) { console.log(`FAILED: ${fails.length} 项`); for (const f of fails) console.log("  -", f); process.exit(1); }
console.log("ALL PASSED —— 博主字段标注与上游数据逐位一致");
