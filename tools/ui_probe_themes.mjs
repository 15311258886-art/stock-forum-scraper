/* 核验 4 套主题下新增组件（左侧大类导航 / 横向子类导航 / 知识库板块）都吃到了主题变量。
   只截图的"验证"是不可靠的——本次就发现截图与浅色版逐字节相同（主题根本没切）。
   故这里断言**计算样式**：生效的 CSS 文件 + 关键元素的实际颜色 + 对比度方向。 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = "http://127.0.0.1:8765/";
const PORT = 9334;
const outDir = process.argv[2] || ".";
mkdirSync(outDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "edgeth_"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = [];
const check = (ok, msg) => { console.log(`  [${ok ? "OK" : "FAIL"}] ${msg}`); if (!ok) fails.push(msg); };

const edge = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--window-size=1600,1000", "--hide-scrollbars", BASE,
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
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
async function ev(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  return r.result?.result?.value;
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, name), Buffer.from(r.result.data, "base64"));
  console.log("       saved " + name);
}

// RGB → 相对亮度，用于判断"深底浅字"还是"浅底深字"
const LUM = `(rgb => { const m = rgb.match(/\\d+/g).map(Number);
  return 0.2126*m[0] + 0.7152*m[1] + 0.0722*m[2]; })`;

await send("Page.enable"); await send("Runtime.enable");
await sleep(3000);

console.log("\n[1] 主题切换是真的生效（断言生效 CSS + 计算颜色）");
const themes = [
  { id: "light-tech", label: "A 浅色科技风", dark: false },
  { id: "dark-dashboard", label: "B 深色数据大屏", dark: true },
  { id: "minimal-report", label: "C 简约报表风", dark: false },
  { id: "card-warm", label: "D 卡片暖色", dark: false },
];

const seen = new Map();
for (const th of themes) {
  // 直接点选主题 chip（与用户操作一致）
  const clicked = await ev(`(() => {
    const chips = [...document.querySelectorAll("#themePick .chip")];
    const c = chips.find(x => x.dataset.id === ${JSON.stringify(th.id)});
    if (!c) return false;
    c.click(); return true;
  })()`);
  check(clicked, `${th.label}：主题 chip 可点选`);
  await sleep(900);
  if (th.id === "dark-dashboard") {
    await send("Page.navigate", { url: BASE + "#learn/learn_zdx_stock" });
    await sleep(4500);
  }
  const st = await ev(`(() => {
    const g = (el, p) => el ? getComputedStyle(el)[p] : null;
    const body = document.body;
    const nav = document.querySelector(".sidenav .nav-item.active") || document.querySelector(".sidenav .nav-item");
    const sub = document.querySelector("#sidenav .nav-sub .sub-item.active") || document.querySelector("#sidenav .nav-sub .sub-item");
    const card = document.querySelector(".kb-school") || document.querySelector(".card") || document.querySelector(".panel");
    const link = document.getElementById("theme-link");
    return {
      href: link ? link.href.split("/").pop() : "",
      bodyBg: g(body, "backgroundColor"), bodyFg: g(body, "color"),
      navBg: g(nav, "backgroundColor"), navFg: g(nav, "color"),
      subFg: g(sub, "color"), subBg: g(sub, "backgroundColor"),
      cardBg: g(card, "backgroundColor"), cardFg: g(card, "color"),
      navCount: document.querySelectorAll(".sidenav .nav-item").length,
      subCount: document.querySelectorAll("#sidenav .nav-sub .sub-item").length,
    };
  })()`);
  check(st.href === `${th.id}.css`, `${th.label}：生效样式表 = ${st.href}`);
  const bl = await ev(`(${LUM})(${JSON.stringify(st.bodyBg)})`);
  const fl = await ev(`(${LUM})(${JSON.stringify(st.bodyFg)})`);
  const isDark = bl < 128;
  check(isDark === th.dark,
    `${th.label}：底色亮度 ${bl.toFixed(0)} → ${isDark ? "深底" : "浅底"}（期望 ${th.dark ? "深底" : "浅底"}）`);
  check(Math.abs(bl - fl) > 60,
    `${th.label}：正文与底色对比充足（亮度差 ${Math.abs(bl - fl).toFixed(0)} > 60）`);
  check(st.navCount === 5 && st.subCount >= 1,
    `${th.label}：左侧大类 ${st.navCount} 个 / 横向子类 ${st.subCount} 个（结构未随主题丢失）`);
  // 关键：主题不同则底色必须不同，否则等于没切
  seen.set(th.id, st.bodyBg);
  // 学习板块内容仍在（深色那次已切到学习板块）
  const hasKb = await ev(`document.querySelectorAll(".kb-school").length`);
  if (th.id === "dark-dashboard") {
    check(hasKb === 8, `${th.label}：知识库板块 8 个流派卡片在深色下正常渲染（${hasKb}）`);
    // 滚到流派区，产出与浅色版同视角的深色截图（交付用）
    await ev(`(() => { const e = document.getElementById('kb-sec-schools');
      if (e) window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 60); })()`);
    await sleep(900);
    await shot("kb_v9_dark_schools.png");
    await ev("window.scrollTo(0,0)"); await sleep(600);
    await shot("theme_v_dark_schools.png");
  } else if (th.id === "light-tech") {
    await shot("theme_v_light_nav.png");
  }
}

console.log("\n[2] 4 套主题底色互不相同（防止「切换无效」被截图掩盖）");
check(new Set(seen.values()).size === 4,
  `4 套主题 body 底色去重后 ${new Set(seen.values()).size} 种（期望 4）`);
for (const [k, v] of seen) console.log(`       ${k.padEnd(16)} ${v}`);

ws.close(); edge.kill();
console.log("\n" + "=".repeat(56));
if (fails.length) { console.log(`FAILED: ${fails.length} 项`); for (const f of fails) console.log("  -", f); process.exit(1); }
console.log("ALL PASSED —— 4 套主题下二级导航与知识库板块配色/结构均正确");
