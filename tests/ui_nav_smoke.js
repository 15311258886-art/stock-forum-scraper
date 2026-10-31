/* tests/ui_nav_smoke.js —— 全导航只读冒烟 + 逐面板真实耗时（2026-10-31，v2）
 *
 * 为什么另写一个，而不是用 ui_regression.js：
 *   那个脚本写死了 `cat === "overseas"` 与 `tab === "stocks"` —— 2026-10-30/31 的导航重排
 *   （DECISIONS.md D16/D20/D35）已把这两个撤销/迁走，跑起来只会产出「元素找不到」式假失败。
 *   本脚本**从 DOM 读导航**，不写死任何 tab 名，加减面板都不用改这里。
 *
 * v2 修掉的两个自身缺陷（v1 实测暴露）：
 *   1) v1 要求每个 tab 在 index.html 里有静态 `#p-<tab>` 容器 —— 错的。
 *      `app.js` 的 `ensurePanel(tab)` 会**按需创建**并 append 到 `.content`，
 *      prediction / policy / lowbox* / learn_* 等动态 tab 本来就没有静态容器。
 *      v2 不再做这个前置判断，改为点击后看它有没有真的出现。
 *   2) v1 用「innerText 长度 > 20」判定"没跑空" —— 太松：
 *      「正在读取…」「接口尚未生效…」这类占位/降级文案也有 20~30 个字，会被当成已加载。
 *      v2 改为：内容长度达标 **且** 连续两次采样稳定 **且** 不命中占位/降级词表。
 *
 * 它回答三个问题（对应进哥反复强调的三件事）：
 *   1) 每个面板点开是不是**空**的（「不能跑空」）；
 *   2) 每个面板点开**要等多久**（「有时刷不出来 / 等特别久」）；
 *   3) 页面上有没有 **JS 报错 / 接口 4xx·5xx**（「刷失败」）。
 *
 * 铁律：只发 GET、只点导航；绝不点任何写库/抓取按钮。
 *      对已知会触发分钟级重算的面板（SLOW_TABS）跳过并如实标注 ——
 *      不为了覆盖率把服务压死。
 *
 * 用法：  node tests/ui_nav_smoke.js [port] [perTabMs]
 *        默认 port=8765（生产实例）、perTabMs=6000
 * 退出码：0 无阻塞问题；1 有空面板/报错；2 脚本自身异常
 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

const PORT = process.argv[2] || "8765";
const PER_TAB = parseInt(process.argv[3] || "6000", 10);
const ORIGIN = `http://127.0.0.1:${PORT}`;
const URL = `${ORIGIN}/?skipguide=1`;

/* 点一下就可能跑几分钟的面板，本轮不点。
   必须如实列出「跳过了什么」，不能装作全测了。 */
const SLOW_TABS = {
  backtest: "点开会触发 /api/backtest（历史观测最长 25 分钟）",
  quant: "点开会触发 /api/quant（历史观测 145 秒级）",
};

/* 占位文案：命中即认定"还没真加载出来"。
   ⚠ 只在**小文本**（< 600 字）里扫 —— v2 首跑吃过亏：市场情绪面板 12 万字正文里
   偶然出现「等待」两个字，就被判成空面板。大面板不缺内容，不需要做这个判断。 */
const PLACEHOLDER = [
  "正在读取", "正在汇总", "正在加载", "加载中", "读取中", "计算中", "扫描中",
  "暂无数据", "等待",
];
/* 降级/不可用：环境或数据源问题，不是代码 bug，单独归类 */
const DEGRADED = [
  "尚未生效", "需要重启", "请强制刷新", "脚本未加载",
  "数据源不可用", "不可用", "未安装", "无权限", "akshare", "依赖缺失",
];
const SMALL = 600;
/* 已知无害的控制台报错（不当作失败，但要单独列出来让人知道它真实存在） */
const BENIGN_CONSOLE = [
  // CSP 的 frame-ancestors 通过 <meta> 下发时浏览器会忽略 —— 这条是真问题但属既有配置，
  // 单独报，不混进"回归失败"。见下方 summary 的「已知既存问题」段。
  "frame-ancestors",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: true,
    args: ["--disable-gpu", "--no-first-run", "--no-default-browser-check"],
    defaultViewport: { width: 1500, height: 950 },
  });
  const page = await browser.newPage();

  const jsErrors = [];
  const benign = [];
  const httpBad = [];
  page.on("pageerror", (e) => jsErrors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const t = m.text().slice(0, 220);
    if (BENIGN_CONSOLE.some((b) => t.includes(b))) benign.push(t);
    else if (!/Failed to load resource/.test(t)) jsErrors.push("console: " + t);
  });
  page.on("response", (r) => {
    const s = r.status();
    if (s >= 400) httpBad.push(s + " " + r.url().replace(ORIGIN, ""));
  });

  // ── 首屏 ──────────────────────────────────────────────────
  const t0 = Date.now();
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  const domMs = Date.now() - t0;
  await page.waitForSelector("#sidenav .nav-item", { timeout: 30000 });
  const navMs = Date.now() - t0;

  const cats = await page.evaluate(() =>
    [...document.querySelectorAll("#sidenav .nav-item")].map((n) => ({
      cat: n.dataset.cat,
      label: (n.innerText || "").split("\n").map((s) => s.trim()).filter(Boolean).pop() || n.dataset.cat,
    })).filter((x) => x.cat)
  );

  /** 点击后轮询直到「够长 + 稳定 + 非占位」，返回 {len, ms, text, kind}
   *  v2 教训：占位文案稳定时**不能**提前退出 —— 提前退出等于放过了一个
   *  "等了 6 秒还没加载出来"的真问题（今日决策就是这样被漏掉的）。
   *  iframe 型面板（基金经理策略）innerText 恒为 0，要读 iframe 内部。 */
  async function settle(tab) {
    const started = Date.now();
    let lastLen = -1, stable = 0, text = "", hasFrame = false, frameLen = 0;
    while (Date.now() - started < PER_TAB) {
      const snap = await page.evaluate((t) => {
        const p = document.getElementById("p-" + t);
        if (!p) return { text: "", frameLen: -1 };
        let fl = -1;
        const fr = p.querySelector("iframe");
        if (fr) {
          try { fl = (fr.contentDocument && fr.contentDocument.body ? fr.contentDocument.body.innerText : "").trim().length; }
          catch (e) { fl = -2; }   // 跨域读不到，视为"有 iframe 就算加载"
        }
        return { text: (p.innerText || "").trim(), frameLen: fl };
      }, tab);
      text = snap.text;
      hasFrame = snap.frameLen !== -1;
      frameLen = snap.frameLen;
      const len = text.length + Math.max(0, frameLen);
      if (len === lastLen && len > 0) stable++; else stable = 0;
      lastLen = len;
      const small = text.length < SMALL;
      const isPh = small && PLACEHOLDER.some((w) => text.includes(w));
      if (len >= 120 && stable >= 2 && !isPh) break;
      await sleep(150);
    }
    const small = text.length < SMALL;
    const isPh = small && PLACEHOLDER.some((w) => text.includes(w));
    const isDeg = small && DEGRADED.some((w) => text.includes(w));
    const effective = text.length + Math.max(0, frameLen);
    return { len: effective, ms: Date.now() - started, text, isPh, isDeg, hasFrame };
  }

  const okList = [], emptyList = [], degradedList = [], slowList = [];
  let clicked = 0, skipped = 0;

  for (const c of cats) {
    await page.evaluate((cat) => {
      const el = [...document.querySelectorAll("#sidenav .nav-item")].find((n) => n.dataset.cat === cat);
      if (el) el.click();
    }, c.cat);
    await sleep(250);

    const tabs = await page.evaluate(() =>
      [...document.querySelectorAll("#subnav .sub-tab")].map((t) => ({
        tab: t.dataset.tab,
        label: (t.innerText || "").trim(),
      })).filter((x) => x.tab)
    );
    if (!tabs.length) { emptyList.push(`${c.label}：二级导航为空`); continue; }

    for (const t of tabs) {
      const name = `${c.label}/${t.label}`;
      if (SLOW_TABS[t.tab]) { skipped++; continue; }
      clicked++;

      await page.evaluate((tab) => {
        const el = [...document.querySelectorAll("#subnav .sub-tab")].find((x) => x.dataset.tab === tab);
        if (el) el.click();
      }, t.tab);

      const r = await settle(t.tab);
      const active = await page.evaluate(() => {
        const a = document.querySelector(".panel.active");
        return a ? a.id : null;
      });
      const isActive = active === "p-" + t.tab;

      if (!isActive) { emptyList.push(`${name}（面板未激活，active=${active}）`); continue; }
      const brief = `「${r.text.slice(0, 34).replace(/\s+/g, " ")}」`;
      if (r.isDeg) { degradedList.push(`${name} len=${r.len} ${r.ms}ms ${brief}`); continue; }
      if (r.isPh) { emptyList.push(`${name} 等了 ${r.ms}ms 仍是占位 ${brief}`); continue; }
      if (r.len < 120) { emptyList.push(`${name} len=${r.len} ${r.ms}ms ${brief}`); continue; }
      okList.push(`${name} ${r.ms}ms len=${r.len}`);
      if (r.ms > 2500) slowList.push(`${name} ${r.ms}ms`);
    }
  }

  await browser.close();

  // ── 报告 ─────────────────────────────────────────────────
  console.log(`首屏 DOMContentLoaded ${domMs} ms · 导航渲染完成 ${navMs} ms · 一级类 ${cats.length} 个`);
  console.log(`点过 ${clicked} 个 tab · 跳过 ${skipped} 个\n`);

  console.log(`✅ 正常加载 ${okList.length} 个`);
  okList.sort((a, b) => parseInt(b.match(/(\d+)ms/)[1]) - parseInt(a.match(/(\d+)ms/)[1]));
  console.log("   最慢 8 个：");
  okList.slice(0, 8).forEach((x) => console.log("     " + x));

  if (slowList.length) {
    console.log(`\n🐢 超过 2500ms（${slowList.length} 个）`);
    slowList.forEach((x) => console.log("     " + x));
  }
  if (degradedList.length) {
    console.log(`\n🟡 降级运行（${degradedList.length} 个，环境问题不是代码 bug）`);
    degradedList.forEach((x) => console.log("     " + x));
  }
  if (emptyList.length) {
    console.log(`\n🔴 空 / 未加载出来（${emptyList.length} 个）`);
    emptyList.forEach((x) => console.log("     " + x));
  }
  console.log(`\n跳过 ${skipped} 个：`);
  Object.entries(SLOW_TABS).forEach(([k, v]) => console.log(`     ${k} —— ${v}`));

  if (benign.length) {
    console.log(`\n⚠ 已知既存问题（不计入失败，但确实是问题）${benign.length} 条：`);
    console.log("     CSP 的 frame-ancestors 通过 <meta> 下发会被浏览器忽略 → 防 iframe 嵌套实际未生效");
  }
  if (jsErrors.length) {
    console.log(`\n❌ JS 报错 ${jsErrors.length} 条：`);
    jsErrors.slice(0, 10).forEach((x) => console.log("     " + x));
  }
  if (httpBad.length) {
    const uniq = [...new Set(httpBad)];
    console.log(`\n❌ 接口/资源 4xx·5xx ${uniq.length} 条：`);
    uniq.slice(0, 12).forEach((x) => console.log("     " + x));
  }

  const fatal = emptyList.length + jsErrors.length + httpBad.length;
  console.log(`\n结论：${fatal === 0 ? "无阻塞问题" : "有 " + fatal + " 项需处理"}` +
    (degradedList.length ? `（另有 ${degradedList.length} 个降级，需重启服务）` : ""));
  process.exit(fatal === 0 ? 0 : 1);
})().catch((e) => {
  console.error("脚本本身出错：" + e.message);
  process.exit(2);
});
