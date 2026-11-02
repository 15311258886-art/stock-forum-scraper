/* 决策台表格对齐诊断：把每一列表头与单元格的实际左/右边界、text-align 打出来。
   对齐问题的量化定义：同一列内，th 与 td 的「内容边」不一致（差 > 4px 即视为错位）。 */
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const CH = process.argv[2] || "hot";
const TAB = "desk_" + CH;

(async () => {
  const browser = await puppeteer.launch({
    executablePath: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    headless: "new",
    args: ["--no-sandbox", "--window-size=1600,1000"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
  await page.goto("http://127.0.0.1:8765/", { waitUntil: "networkidle2", timeout: 60000 });
  // 直接调 showTab：只改 location.hash 时，hashchange 不一定在面板未挂载时生效，
  // 结果截图里还是上一个 tab（实测 dip/mid/long 都截成了 hot）。
  await page.evaluate((tab) => {
    if (typeof showTab === "function") showTab(tab);
    else location.hash = "#desk/" + tab.replace("desk_", "");
  }, TAB);
  await page.waitForFunction(
    (id) => { const p = document.querySelector("#p-" + id); return p && p.querySelector("table"); },
    { timeout: 60000 }, TAB
  ).catch(() => {});
  await new Promise((r) => setTimeout(r, 2500));
  // 首次引导层会盖住整个面板（"欢迎使用·3 步快速上手"），截图前必须关掉，
  // 否则看到的永远是引导页而不是要检查的表格。
  await page.evaluate(() => {
    // 首次引导是 #firstRunModal（hidden=false）+ #firstRunMask，两个都要关。
    const ids = ["#firstRunMask", "#firstRunModal", "#settingsMask", "#settingsModal"];
    ids.forEach((s) => document.querySelectorAll(s).forEach((x) => { x.hidden = true; x.style.display = "none"; }));
    document.querySelectorAll(".modal-mask.show").forEach((x) => x.classList.remove("show"));
    document.querySelectorAll(".modal.show").forEach((x) => { x.style.display = "none"; });
  });

  const out = await page.evaluate((id) => {
    const panel = document.querySelector("#p-" + id);
    if (!panel) return { err: "no panel" };
    const tbl = panel.querySelector("table");
    if (!tbl) return { err: "no table" };
    const scroller = tbl.parentElement;
    const ths = [...tbl.querySelectorAll("thead th")];
    const r0 = tbl.querySelector("tbody tr");
    const tds = r0 ? [...r0.children] : [];
    const rect = (el) => { const r = el.getBoundingClientRect(); return { l: +r.left.toFixed(1), r: +r.right.toFixed(1), w: +r.width.toFixed(1) }; };
    const cs = (el) => getComputedStyle(el);
    const cols = ths.map((th, i) => {
      const td = tds[i];
      return {
        i,
        head: th.textContent.trim(),
        th: rect(th),
        thAlign: cs(th).textAlign,
        thPadR: cs(th).paddingRight,
        td: td ? rect(td) : null,
        tdAlign: td ? cs(td).textAlign : null,
        tdText: td ? td.textContent.trim().slice(0, 26) : "",
        // 内容右边界：td 里最后一个可见元素/文本的右缘，用于判断"数字是否真贴到列的右边"
        contentR: td ? (() => {
          const range = document.createRange();
          range.selectNodeContents(td);
          const rr = range.getBoundingClientRect();
          return +rr.right.toFixed(1);
        })() : null,
      };
    });
    return {
      tableW: +tbl.getBoundingClientRect().width.toFixed(1),
      scrollerW: scroller ? +scroller.clientWidth.toFixed(1) : null,
      scrollW: scroller ? scroller.scrollWidth : null,
      overflowX: scroller ? getComputedStyle(scroller).overflowX : null,
      cols,
      rows: tbl.querySelectorAll("tbody tr").length,
    };
  }, TAB);
  console.log(JSON.stringify(out, null, 1));
  const SHOT = path.join(__dirname, "..", `_diag_desk_${CH}_${Date.now()}.png`);
  await page.screenshot({ path: SHOT, fullPage: true });
  console.log("SHOT:", SHOT);

  /* 点一下第一行的名称，确认真的能打开个股详情抽屉（进哥第三条反馈）。
     判据不是"点了没报错"，而是 #drawer 真的 show 了、标题带上了那只票的代码。 */
  const clickRes = await page.evaluate(async (panelId) => {
    const panel = document.querySelector("#" + panelId);
    const lnk = panel && panel.querySelector('tbody [data-code-open], tbody .dk-lnk');
    if (!lnk) return { ok: false, why: "表格里没有可点链接" };
    const sym = lnk.getAttribute("data-code-open") || lnk.getAttribute("data-sym");
    lnk.click();
    await new Promise((r) => setTimeout(r, 1200));
    const dw = document.querySelector("#drawer");
    return {
      ok: !!(dw && dw.classList.contains("show")),
      sym,
      title: (document.querySelector("#drawerTitle") || {}).textContent || "",
      hasBody: !!(document.querySelector("#drawerBody") && document.querySelector("#drawerBody").textContent.trim().length > 20),
    };
  }, "p-" + TAB);
  console.log("CLICK:", JSON.stringify(clickRes));
  await browser.close();
})();
