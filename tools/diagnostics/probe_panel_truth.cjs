/* tools/probe_panel_truth.cjs —— 面板「有没有真的画出内容」的探针（2026-11-01）
 *
 * 为什么单独有这个脚本
 * --------------------
 * 判断一个面板是不是白板，`panel.innerText.length` 这个口径对**外挂整页（iframe）面板是无效的**：
 * innerText / textContent 都不跨子文档，父文档里读到的永远是 0 —— 于是"渲染失败"和"内容在 iframe 里"
 * 会得到同一个数字。本探针同时给三个口径，任何面板都判得清：
 *     ① 父文档 innerText 长度            —— 内联渲染的面板看这个
 *     ② 子文档（同源 iframe）body 文本长度 —— 外挂页面的面板看这个
 *     ③ data-ready / data-loaded 状态     —— 就绪判定（app.js 唯一设置点 markPanelReady）
 * 另外扫一遍可见文本里的字面量 `null` / `undefined` / `NaN`：模板把缺值拼进字符串时
 * 用户真的会看到 "合成-/null"，这是本轮抓到的真实缺陷，必须能在探针里复现。
 *
 * 用法：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/probe_panel_truth.cjs                  # 默认探 dy_reso + fundmasters
 *   node tools/probe_panel_truth.cjs strategy/dy_reso  # 探指定 hash
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");

const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";
const HASHES = process.argv.slice(2).length ? process.argv.slice(2)
  : ["strategy/dy_reso", "strategy/fundmasters"];

function pickBrowser() {
  const cands = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ];
  try {
    const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
    if (fs.existsSync(base)) {
      fs.readdirSync(base).filter((d) => /^chromium-/.test(d)).sort().reverse()
        .forEach((d) => cands.push(base + "/" + d + "/chrome-win64/chrome.exe"));
    }
  } catch (e) { /* ignore */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

/* 页面内执行：读一个面板的三口径 + 脏字面量。 */
function inspectPanel(tab) {
  const panel = document.querySelector("#p-" + tab);
  const out = {
    tab: tab,
    exists: !!panel,
    parentText: panel ? (panel.innerText || "") : "",
    frameText: "",
    frameSrc: "",
    ready: panel ? panel.getAttribute("data-ready") : null,
    frameLoaded: null,
    fallbackVisible: false,
    dirty: [],
  };
  if (!panel) return out;
  const frame = panel.querySelector("iframe[data-panel-frame]");
  if (frame) {
    out.frameSrc = frame.getAttribute("src") || "";
    out.frameLoaded = frame.dataset.loaded || null;
    try {
      const doc = frame.contentDocument;
      out.frameText = doc && doc.body ? (doc.body.innerText || doc.body.textContent || "") : "";
    } catch (e) {
      out.frameText = "";
    }
  }
  const fb = panel.querySelector(".fm-fallback");
  out.fallbackVisible = !!(fb && !fb.hidden);

  /* 脏字面量：只扫可见文本，扫源码会把注释里的 null 也算进去 */
  const txt = (out.frameText || out.parentText || "").replace(/\s+/g, " ");
  ["null", "undefined", "NaN"].forEach((w) => {
    const re = new RegExp("(^|[^A-Za-z0-9_$])" + w + "([^A-Za-z0-9_$]|$)");
    if (re.test(txt)) {
      const i = txt.search(re);
      out.dirty.push({ word: w, at: txt.slice(Math.max(0, i - 40), i + 40) });
    }
  });
  return out;
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.error("没有可用浏览器（Edge/Chrome/playwright chromium 都没找到）"); process.exit(1); }
  const browser = await puppeteer.launch({ executablePath: exe, headless: "new", args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  let bad = 0;
  for (const h of HASHES) {
    await page.goto(URL + "#" + h, { waitUntil: "domcontentloaded" });
    /* 等「有内容」而不是 sleep 固定时间：父文档有字 / iframe 有字 / 面板标了 ready，任一成立即可。
       5s 上限只是防死等 —— 超时就按"空"报，让调用方自己判断。 */
    const tab = h.split("/").pop();
    await page.waitForFunction((t) => {
      const p = document.querySelector("#p-" + t);
      if (!p) return false;
      if (p.getAttribute("data-ready") === "1") return true;
      const txt = (p.innerText || "").replace(/\s+/g, " ").trim();
      /* 占位文案不算"有内容"：只等 innerText 非空会把「正在加载数据…」当成已渲染，
         探针就会在数据到达前给出 OK —— 那正是"绿得毫无意义"。 */
      if (txt && !/^(正在加载数据…|加载中…|正在加载|—)$/.test(txt)) return true;
      const f = p.querySelector("iframe[data-panel-frame]");
      if (f) {
        try { return !!(f.contentDocument && f.contentDocument.body
          && (f.contentDocument.body.innerText || "").trim().length > 0); } catch (e) { return false; }
      }
      return false;
    }, { timeout: 12000 }, tab).catch(() => {});
    const r = await page.evaluate(inspectPanel, tab);
    const parentLen = r.parentText.trim().length;
    const frameLen = r.frameText.trim().length;
    const shown = Math.max(parentLen, frameLen);
    const status = !r.exists ? "MISSING"
      : (shown > 0 ? "OK" : (r.fallbackVisible ? "EMPTY(已显性空态)" : "EMPTY(白板!)"));
    if (status !== "OK") bad++;
    console.log("\n=== #" + h + " ===");
    console.log("  panel=" + (r.exists ? "#p-" + tab : "缺失")
      + "  data-ready=" + r.ready
      + "  父文档文本=" + parentLen + " 字"
      + (r.frameSrc ? "  iframe文本=" + frameLen + " 字 data-loaded=" + r.frameLoaded : "")
      + "  空态可见=" + r.fallbackVisible);
    console.log("  判定：" + status);
    if (r.parentText.trim()) console.log("  正文首行：" + r.parentText.replace(/\s+/g, " ").trim().slice(0, 110));
    if (r.frameText.trim()) console.log("  子页首行：" + r.frameText.replace(/\s+/g, " ").trim().slice(0, 110));
    if (r.dirty.length) {
      console.log("  ⚠ 脏字面量 " + r.dirty.length + " 处：");
      r.dirty.slice(0, 4).forEach((d) => console.log("     " + d.word + " ← …" + d.at + "…"));
    } else {
      console.log("  脏字面量：无（null/undefined/NaN 都没出现在可见文本里）");
    }
  }
  await browser.close();
  console.log("\n" + (bad ? "PROBE_FAIL " + bad + " 个面板" : "PROBE_OK"));
  process.exit(bad ? 1 : 0);
})();
