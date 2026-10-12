// 视觉复核：viewport 级截图（captureBeyondViewport=false），用于逐段目检学习板块排版。
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
// 端口/配置目录都取"每次运行唯一"：复用同一个 --user-data-dir 会命中上一次残留的
// headless 实例（edge.kill() 只杀启动器，不杀渲染/GPU 子进程），那个实例空闲后
// 合成器停止出帧 → Page.captureScreenshot 返回**最后一帧旧画面**，而 DOM 读数却是新的。
// 实测后果：深色主题截图与浅色版逐字节相同（假图）。用全新临时目录可彻底规避。
const CDP_PORT = 9300 + (process.pid % 300);
const BASE = "http://127.0.0.1:8765/";
const OUT = process.argv[2] || ".";
fs.mkdirSync(OUT, { recursive: true });
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "edge_shot_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (buf) => createHash("sha256").update(buf).digest("hex").slice(0, 12);

const edge = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--mute-audio",
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${PROFILE}`,
  "--window-size=1600,1000", "about:blank",
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 80; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const p = l.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (p) return p.webSocketDebuggerUrl;
    } catch {}
    await sleep(300);
  }
  throw new Error("no cdp");
}
const ws = new WebSocket(await target());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0; const w = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && w.has(m.id)) { w.get(m.id)(m); w.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const id = ++seq; w.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (x) => (await send("Runtime.evaluate", { returnByValue: true, expression: x })).result?.result?.value;
const shot = async (n) => {
  // bringToFront：让该 target 成为前台可见页，避免合成器因"不可见"而停止出帧
  await send("Page.bringToFront");
  await sleep(120);
  const r = await send("Page.captureScreenshot", { format: "png" });
  const buf = Buffer.from(r.result.data, "base64");
  fs.writeFileSync(path.join(OUT, n), buf);
  console.log("saved", n, "sha=" + sha(buf), "bytes=" + buf.length);
  return buf;
};

await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.bringToFront");

// 学习板块：按区块逐段取景
await send("Page.navigate", { url: BASE + "#learn/learn_zdx_stock" });
await sleep(6000);
await ev("window.scrollTo(0,0)"); await sleep(300);
await shot("kb_v1_head.png");
for (const [id, name] of [["kb-sec-conclusions", "kb_v2_conclusions"], ["kb-sec-schools", "kb_v3_schools"],
                          ["kb-sec-compare", "kb_v4_compare"], ["kb-sec-pitfalls", "kb_v5_pitfalls"],
                          ["kb-sec-backtests", "kb_v6_backtests"], ["kb-sec-sources", "kb_v7_sources"],
                          ["kb-sec-risks", "kb_v8_risks"]]) {
  await ev(`(() => { const e = document.getElementById('${id}');
    window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 60); })()`);
  await sleep(320);
  await shot(name + ".png");
}
// 知识获取三块
await send("Page.navigate", { url: BASE + "#acquire/sources" });
await sleep(2500); await ev("window.scrollTo(0,0)"); await sleep(300);
await shot("acq_v1_sources.png");
await ev(`document.querySelector('#sidenav .nav-sub .sub-item[data-tab="archive"]').click()`);
await sleep(2200); await ev("window.scrollTo(0,0)"); await sleep(300);
await shot("acq_v2_archive.png");
await ev(`document.querySelector('#sidenav .nav-sub .sub-item[data-tab="fetchlog"]').click()`);
await sleep(1500); await ev("window.scrollTo(0,0)"); await sleep(300);
await shot("acq_v3_fetchlog.png");
await send("Page.navigate", { url: BASE + "#learn/learn_tracks" });
await sleep(2500); await shot("learn_v_tracks.png");

// 深色主题截图不在这里做。
// 原因（实测踩坑，务必别改回来）：本脚本用了 setDeviceMetricsOverride + 长时闲置的同一实例，
// 切主题后 DOM 读数明明是深色（href=dark-dashboard.css、body 底色 rgb(11,14,20)），
// 但 Page.captureScreenshot 返回的仍是**上一次的浅色帧**，产出与浅色版逐字节相同的假图。
// 深色截图改由 tools/ui_probe_themes.mjs 产出（新临时 profile、无 metrics override、
// bringToFront 后取帧，已验证能拿到真深色像素），并用 tools/png_pixel.py 解码像素复核。

ws.close(); edge.kill(); process.exit(0);
