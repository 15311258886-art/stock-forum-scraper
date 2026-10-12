/* 临时探针：验证「个股快讯/公告」面板点代码/点名称能否打开个股详情抽屉。
 * 通过真实导航（#info/news）进入面板，用真实鼠标点击。只读，不触发破坏性接口。
 * 运行：node tests/_probe_news_code_detail.js [port]
 */
const puppeteer = require("puppeteer-core");
const os = require("os");
const path = require("path");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8765";
const URL = `http://127.0.0.1:${PORT}`;
const TMP = os.tmpdir();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`);

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--window-size=1440,1000"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  // 真实导航进入「个股快讯/公告」
  await page.goto(URL + "#info/news", { waitUntil: "networkidle2" });
  await page.evaluate(() => applyTheme("light-tech"));
  for (let i = 0; i < 40; i++) {
    const n = await page.$$eval("#p-news a[data-code-open]", (e) => e.length).catch(() => 0);
    if (n) break;
    await wait(400);
  }

  const info = await page.evaluate(() => {
    const host = document.querySelector("#p-news");
    const links = host ? [...host.querySelectorAll("a[data-code-open]")] : [];
    const table = host ? host.querySelector("table.desk-tb") : null;
    const rows = table ? [...table.querySelectorAll("tbody tr")] : [];
    const firstRow = rows[0];
    return {
      panelExists: !!host,
      panelActive: host ? host.classList.contains("active") : false,
      panelVisible: host ? host.offsetHeight > 0 : false,
      rowCount: rows.length,
      linkCount: links.length,
      headers: table ? [...table.querySelectorAll("thead th")].map((t) => t.textContent.trim()) : [],
      firstCode: links[0] ? links[0].getAttribute("data-code-open") : null,
      firstCodeText: links[0] ? links[0].textContent : null,
      // 第一行的 名称单元格（第 2 列）内容与是否可点
      nameCellHtml: firstRow ? firstRow.children[1].innerHTML.slice(0, 120) : "",
      nameCellHasLink: firstRow ? !!firstRow.children[1].querySelector("[data-code-open]") : false,
    };
  });
  console.log("PANEL_INFO " + JSON.stringify(info));
  ok("面板 #p-news 存在且可见", info.panelExists && info.panelVisible, `active=${info.panelActive}`);
  ok("表格有数据行", info.rowCount > 0, `${info.rowCount} 行`);
  ok("表头正确", /代码/.test(info.headers.join("")) && /名称/.test(info.headers.join("")), info.headers.join("|"));
  ok("代码单元格已可点（data-code-open）", info.linkCount > 0, `${info.linkCount} 个，首个 ${info.firstCode}/${info.firstCodeText}`);
  ok("名称单元格当前是否可点", info.nameCellHasLink, `可点=${info.nameCellHasLink}，html=${info.nameCellHtml}`);

  const drawerState = async () => page.evaluate(() => {
    const d = document.querySelector("#drawer");
    const body = document.querySelector("#drawerBody");
    return {
      open: d ? d.classList.contains("show") : false,
      title: (document.querySelector("#drawerTitle") || {}).textContent || "",
      bodyLen: body && body.innerText ? body.innerText.length : 0,
      bodyHead: (body && body.innerText ? body.innerText : "").slice(0, 100),
    };
  });
  const closeDrawer = async () => { await page.keyboard.press("Escape"); await wait(350); };

  // ---- A. 点代码 ----
  if (info.linkCount > 0) {
    await page.click("#p-news table.desk-tb tbody tr:first-child td:first-child a[data-code-open]");
    await wait(1500);
    const st = await drawerState();
    ok("【点代码】抽屉打开且含个股信息", st.open && st.bodyLen > 50, `标题="${st.title}" 正文${st.bodyLen}字`);
    await page.screenshot({ path: path.join(TMP, "news_click_code.png") });
    await closeDrawer();
  }

  // ---- B. 点名称 ----
  const nameClickable = await page.evaluate(() => {
    const host = document.querySelector("#p-news");
    const row = host && host.querySelector("table.desk-tb tbody tr");
    const cell = row && row.children[1];
    return cell ? !!cell.querySelector("[data-code-open]") : false;
  });
  if (nameClickable) {
    await page.click("#p-news table.desk-tb tbody tr:first-child td:nth-child(2) a[data-code-open]");
    await wait(1500);
    const st = await drawerState();
    ok("【点名称】抽屉打开且含个股信息", st.open && st.bodyLen > 50, `标题="${st.title}"`);
    await closeDrawer();
  } else {
    out.push("INFO  【点名称】当前名称列不是链接，点击名称不会打开详情抽屉（待改造）");
  }

  // ---- C. 回归：点「非链接」单元格仍走整行选中（不破坏原有交互）----
  await page.click("#p-news table.desk-tb tbody tr:nth-child(3) td:nth-child(3)");   // 第3行「结论」列
  await wait(900);
  const selectState = await page.evaluate(() => {
    const sel = document.querySelector("#p-news table.desk-tb tbody tr.desk-sel");
    const cell = sel && sel.children[0];
    return { hasSel: !!sel, code: cell ? cell.textContent.trim() : "" };
  });
  ok("【回归】点非链接单元格仍触发整行选中（newsPick）", selectState.hasSel, `选中行=${selectState.code}`);

  // ---- D. 其它面板冒烟：逐个切过去，面板须 active 且不新增 JS 报错 ----
  const errBefore = errs.length;
  const tabsToSmoke = ["dashboard", "stocks", "sentiment", "lhb", "news", "policy", "summary"];
  const smoke = [];
  for (const t of tabsToSmoke) {
    await page.evaluate((tb) => { location.hash = "#" + (tb === "news" || tb === "policy" ? "info" : tb === "stocks" || tb === "sentiment" || tb === "lhb" ? "money" : "today") + "/" + tb; }, t);
    await wait(900);
    const vis = await page.evaluate((tb) => { const p = document.querySelector("#p-" + tb); return p ? (p.classList.contains("active") && p.offsetHeight > 0) : false; }, t);
    smoke.push(`${t}:${vis ? "ok" : "MISS"}`);
  }
  const allSmoke = smoke.every((s) => s.endsWith("ok"));
  ok("【回归】其它面板切换后均正常 active（无破坏）", allSmoke, smoke.join(" "));
  ok("【回归】上述切换未产生新的 JS 报错", errs.length === errBefore, errs.slice(errBefore, errBefore + 3).join(" | ") || "0 条");

  ok("无 JS 报错", errs.length === 0, errs.slice(0, 3).join(" | "));
  console.log(out.join("\n"));
  const fail = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n合计 ${out.length} 项，失败 ${fail} 项`);
  console.log("SHOT " + path.join(TMP, "news_click_code.png"));
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error("脚本异常：", e); process.exit(2); });
