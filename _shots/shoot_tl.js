/* 2026-10-05 目检截图：只截「入手概率 → 时间轴」区域，验证tl-note-warn 待到期说明的排版
   （是否有叠字/溢出/把表格挤变形）。--no-sandbox 必要；Edge 路径见项目既有脚本。 */
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer-core");

const OUT = path.join(__dirname, "..", "_shots_rate");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 1 });
  await page.goto("http://127.0.0.1:8765/", { waitUntil: "domcontentloaded" });

  // 进「我的票 → 入手概率」：照抄回归脚本的走法（hash 直跳 + 固定选择器，按文本找会扑空）
  await page.goto("http://127.0.0.1:8765/#watch/rate", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 1200));
  await page.evaluate(() => { const n = document.querySelector('.nav-item[data-cat="watch"]'); if (n) n.click(); });
  await new Promise((r) => setTimeout(r, 500));
  await page.evaluate(() => { const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]'); if (t) t.click(); });
  const panel = await page.evaluate(async () => {
    for (let i = 0; i < 40; i++) {
      const tb = document.querySelectorAll("#p-rate tbody tr").length;
      if (tb) return tb;
      await new Promise((r) => setTimeout(r, 700));
    }
    return 0;
  });
  console.log("榜单行数", panel);

  // 等时间轴出卡片（补日线要几秒）
  const cards = await page.evaluate(async () => {
    for (let i = 0; i < 60; i++) {
      const c = document.querySelectorAll("#p-rate .tl-card").length;
      if (c) return c;
      await new Promise((r) => setTimeout(r, 800));
    }
    return 0;
  });
  console.log("时间轴卡片", cards);

  // 元素截图（elementHandle.screenshot 自己处理滚动，别手算 clip 坐标——算错就截到一片空白）
  const sec = await page.$("#p-rate .rate-sec");
  if (!sec) { console.log("NO .rate-sec"); await browser.close(); return; }
  await sec.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await new Promise((r) => setTimeout(r, 400));
  await sec.screenshot({ path: path.join(OUT, "C-timeline-note.png") });
  console.log("C-timeline-note ok");

  // 表格是否变形：量一下各列宽与是否有横向溢出
  const box = await page.evaluate(() => {
    const tb = document.querySelector("#p-rate .tl-tb");
    if (!tb) return null;
    const wrap = tb.parentElement;
    return {
      tableW: Math.round(tb.getBoundingClientRect().width),
      wrapW: Math.round(wrap.getBoundingClientRect().width),
      overflow: tb.scrollWidth - wrap.clientWidth,
      rows: tb.querySelectorAll("tbody tr").length,
      warn: (document.querySelector("#p-rate .tl-note-warn") || {}).innerText || "(无)",
    };
  });
  console.log("表格盒模型", JSON.stringify(box, null, 1));
  await browser.close();
})().catch((e) => { console.error("ERR", e); process.exit(1); });
