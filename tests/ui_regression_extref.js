// 外部参考（extref）三面板视觉回归：无 JS 错误 / 无横向溢出 / 卡片无纵向裁剪 / 卡片不被遮挡 /
// 关键元素可点击 / 内容真实渲染。只读，不触发任何写库按钮。
const puppeteer = require("puppeteer-core");
const EXE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => { out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  —— " + extra : ""}`); };
const URL = "http://127.0.0.1:" + (process.argv[2] || "8765");

// 各面板预期：soft 表行=A+B=26 + 红线区(无信号卡)；comm 8 张卡 + 表行 C+D=7；rank 表行=3(无信号卡)
const TABS = [
  { tab: "extref_soft", rows: 26, cards: 0, red: true },
  { tab: "extref_comm", rows: 7, cards: 8, red: false },
  { tab: "extref_rank", rows: 3, cards: 0, red: false },
];

(async () => {
  const b = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await b.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message + " || " + (e.stack || "").split("\n").slice(1, 4).join(" <- ")));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  STEP = "goto";
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 30000 });
  await wait(900); // 等 loadAll -> loadExtref 把三块都画好

  for (const t of TABS) {
    STEP = "switch " + t.tab;
    await page.evaluate((tab) => { location.hash = "#extref/" + tab; }, t.tab);
    let ready = false, st = null;
    for (let i = 0; i < 40; i++) {
      st = await page.evaluate((tab) => {
        const p = document.querySelector("#p-" + tab);
        if (!p || !p.classList.contains("active")) return { ok: false };
        return { ok: true, tb: p.querySelectorAll(".ex-tb tbody tr").length,
                 cards: p.querySelectorAll(".ex-card").length };
      }, t.tab);
      if (st.ok && ((t.rows && st.tb >= t.rows) || (t.cards && st.cards >= t.cards))) { ready = true; break; }
      await wait(100);
    }
    ok(t.tab + " 面板已激活且内容渲染", ready, ready ? "" : "tb=" + (st && st.tb) + " cards=" + (st && st.cards));
    // 归一化：切面板后可能存在残留滚动，先把窗口滚回顶部再测命中，避免 sticky topbar 误判遮挡
    await page.evaluate(() => { window.scrollTo(0, 0); });
    await wait(200);

    // —— 关键元素可点击：在滚动前（自然置顶态）做命中测试 ——
    const pre = await page.evaluate((tab) => {
      const p = document.querySelector("#p-" + tab);
      const r = { cards: p.querySelectorAll(".ex-card").length, symHit: null, refreshHit: null, refreshDiag: "" };
      const sym = p.querySelector("[data-ex-sym]");
      if (sym) { const sr = sym.getBoundingClientRect(); const se = document.elementFromPoint(sr.left + sr.width / 2, Math.min(sr.top + sr.height / 2, window.innerHeight - 2)); r.symHit = !!(se && (sym === se || sym.contains(se))); }
      const rf = p.querySelector("[data-ex-refresh]");
      if (rf) {
        const rr = rf.getBoundingClientRect();
        const re = document.elementFromPoint(rr.left + rr.width / 2, Math.min(rr.top + rr.height / 2, window.innerHeight - 2));
        r.refreshHit = !!(re && (rf === re || rf.contains(re)));
        if (!r.refreshHit && re) {
          let chain = [], n = re;
          for (let i = 0; i < 5 && n; i++) { chain.push(n.tagName + (n.id ? "#" + n.id : "") + (n.className ? "." + String(n.className).split(" ")[0] : "")); n = n.parentElement; }
          r.refreshDiag = "btnRect=" + Math.round(rr.top) + "," + Math.round(rr.left) + " cover=" + chain.join(">");
        } else {
          r.refreshDiag = "btnRect=" + Math.round(rr.top) + "," + Math.round(rr.left) + " hit=" + (re ? (re.tagName + "." + String(re.className).split(" ")[0]) : "null");
        }
      }
      return r;
    }, t.tab);

    // —— 溢出 / 裁剪 / 遮挡：逐卡片滚动到视口中央再测 ——
    const res = await page.evaluate((tab) => {
      const p = document.querySelector("#p-" + tab);
      const r = { over: 0, clipped: 0, blocked: 0, checked: 0, sampleOverflow: "", sampleClip: "", chipsText: "", hasRed: false };
      if (p.scrollWidth > p.clientWidth + 2) { r.over++; r.sampleOverflow = "panel h-scroll " + p.scrollWidth + ">" + p.clientWidth; }
      p.querySelectorAll(".ex-tb td").forEach((td) => {
        r.checked++;
        if (td.scrollWidth > td.clientWidth + 1) { r.over++; r.sampleOverflow = (r.sampleOverflow ? r.sampleOverflow + " | " : "") + "td:" + td.scrollWidth + ">" + td.clientWidth; }
      });
      p.querySelectorAll(".ex-card").forEach((c) => {
        r.checked++;
        if (c.scrollWidth > c.clientWidth + 2) { r.over++; r.sampleOverflow = (r.sampleOverflow ? r.sampleOverflow + " | " : "") + "card-h:" + c.scrollWidth + ">" + c.clientWidth; }
        if (c.scrollHeight > c.clientHeight + 2) { r.clipped++; r.sampleClip = (r.sampleClip ? r.sampleClip + " | " : "") + "card-v:" + c.scrollHeight + ">" + c.clientHeight; }
        c.scrollIntoView({ block: "center" });
        const cr = c.getBoundingClientRect();
        const el = document.elementFromPoint(cr.left + cr.width / 2, cr.top + cr.height / 2);
        if (!(el && (c === el || c.contains(el)))) r.blocked++;
      });
      const chips = p.querySelector(".ex-stats");
      r.chipsText = chips ? chips.innerText.replace(/\s+/g, " ").trim() : "";
      r.hasRed = !!p.querySelector(".ex-red");
      return r;
    }, t.tab);

    ok(t.tab + " 无横向溢出/裁剪", res.over === 0, res.sampleOverflow);
    if (t.cards) ok(t.tab + " 卡片无纵向裁剪(内容未截断)", res.clipped === 0, res.sampleClip);
    ok(t.tab + " 卡片未被其它元素遮挡(无压字)", res.blocked === 0);
    if (pre.cards > 0) ok(t.tab + " 个股入口可点击(data-ex-sym)", pre.symHit === true);
    else ok(t.tab + " 表视图无信号卡·跳过个股入口断言", true, "预期：soft/rank 为表视图");
    ok(t.tab + " 同步按钮可点击(data-ex-refresh)", pre.refreshHit === true, pre.refreshDiag);
    ok(t.tab + " 统计条含真实数字(源33/信号8/持牌21)", /33/.test(res.chipsText) && /8/.test(res.chipsText) && /21/.test(res.chipsText), res.chipsText.slice(0, 90));
    if (t.red) ok(t.tab + " 合规红线区块渲染", res.hasRed === true);
    try { const el = await page.$("#p-" + t.tab); if (el) await el.screenshot({ path: "tests/extref_" + t.tab + ".png" }); } catch (e) { ok(t.tab + " 截图", false, e.message); }
  }

  ok("全程无 JS 运行时错误/控制台错误", errs.length === 0, errs.slice(0, 5).join(" ; "));
  await b.close();
  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log("\n=== RESULT " + (out.length - fails) + "/" + out.length + " 通过 ===");
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + e.message); process.exit(2); });
