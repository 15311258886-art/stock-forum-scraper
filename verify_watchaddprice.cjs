/* 2026-09-29 关注池新增「加入时价 / 当前价」两列回归。
 * 验证：① 表头两列存在且相邻；② mock 行渲染 12 列、加入时价有值/空值两种显示；
 * ③ 空池 colspan=12；④ 零 JS 报错。不污染数据库（mock 只在页面内存里）。 */
const fs = require("fs"), path = require("path"), puppeteer = require("puppeteer-core");
const BASE = "http://127.0.0.1:8765";

function findBrowser() {
  for (const p of [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ]) { try { if (fs.existsSync(p)) return p; } catch (e) {} }
  return null;
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: findBrowser(), headless: "new",
    args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 950 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("CONSOLE: " + m.text().slice(0, 200)); });

  await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
  await new Promise((r) => setTimeout(r, 1200));

  // ① 真实渲染（池子可能为空）→ 检查表头相邻与 colspan
  const real = await page.evaluate(() => {
    selectNav("watch", "watch", false);
    const ths = Array.from(document.querySelectorAll("#p-watch thead th")).map((t) => t.textContent.trim());
    const iAdd = ths.indexOf("加入时价"), iCur = ths.indexOf("当前价");
    const empty = document.querySelector("#p-watch td.wt-empty");
    return { ths, adjacent: iAdd >= 0 && iCur === iAdd + 1,
      colspan: empty ? (parseInt(empty.getAttribute("colspan") || "0", 10)) : null };
  });

  // ② mock 一条有价 + 一条无价 → 断言行结构
  const mock = await page.evaluate(() => {
    WATCH.data = { groups: [], alerts_unread: 0, checked_at: "mock",
      items: [
        { id: 1, symbol: "600519", name: "贵州茅台", kind: "stock", price: 1500.5, pct: 1.23,
          price_at_add: 1420.1, target_price: 1600, to_target_pct: 6.63, attention: 3,
          sector: "白酒", days_watched: 12, since_add_pct: 5.65, note: "" },
        { id: 2, symbol: "000651", name: "格力电器", kind: "stock", price: 41.2, pct: -0.8,
          price_at_add: null, target_price: null, to_target_pct: null, attention: 2,
          sector: "", days_watched: 3, since_add_pct: null, note: "" }] };
    WATCH.selGroup = "all";
    renderWatch();
    const rows = Array.from(document.querySelectorAll("#p-watch tbody tr"));
    const cells = rows.map((tr) => Array.from(tr.querySelectorAll("td")).map((td) => td.textContent.trim()));
    return { nRows: rows.length,
      nCells: cells.map((c) => c.length),
      addPrice1: cells[0] ? cells[0][1] : null, cur1: cells[0] ? cells[0][2] : null,
      addPrice2: cells[1] ? cells[1][1] : null, cur2: cells[1] ? cells[1][2] : null };
  });

  const SHOT = path.join(__dirname, "artifacts", "watch_addprice_20260929.png");
  try { await page.screenshot({ path: SHOT }); } catch (e) { /* 截图失败不致命 */ }

  console.log("REAL=" + JSON.stringify(real));
  console.log("MOCK=" + JSON.stringify(mock));
  console.log("JS_ERRORS=" + JSON.stringify(errors.slice(0, 6)));
  await browser.close();

  const hdrOk = real.adjacent && real.ths.length === 12;
  const emptyOk = real.colspan === 12 || real.colspan === null; // 池子有数据时无空行
  const mockOk = mock.nRows === 2 && mock.nCells.every((n) => n === 12) &&
    mock.addPrice1 === "1420.10" && mock.cur1 === "1500.50" &&
    mock.addPrice2 === "—" && mock.cur2 === "41.20";
  const pass = errors.length === 0 && hdrOk && emptyOk && mockOk;
  console.log("CHECKS=" + JSON.stringify({ hdrOk, emptyOk, mockOk, noErrors: errors.length === 0 }));
  console.log(pass ? "WATCH_ADDPRICE_PASS" : "WATCH_ADDPRICE_FAIL");
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.log("FAIL", e && e.message); process.exit(1); });
