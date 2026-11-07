/* 三期因子准入门禁验收（2026-11-03）。
 *
 * 必须真的渲染出东西才算数的四件事：
 *   ① 工具包页面上有「外来因子 · 准入门禁」这一节，四条硬线写在标题上；
 *   ② 已有记录按状态上色（影子期 / 未通过），**未通过的必须把原因写出来**；
 *   ③ 输入框 + 「过闸」按钮真的在，且缺 why 时会被前端挡下（不能静默提交）；
 *   ④ 页面没有横向溢出、没有 pageerror。
 *
 * 用法：node tools/shot_factor_gate_verify.cjs
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const EXE = [
  "C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe",
  "C://Program Files//Microsoft//Edge//Application//msedge.exe",
].find((p) => fs.existsSync(p));

const BASE = "http://127.0.0.1:8765";
const ROOT = path.join(__dirname, "..");
const out = [];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1400"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1400, deviceScaleFactor: 1 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));

  await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask");
    if (m) m.style.display = "none";
    const fm = document.querySelector("#firstRunModal");
    if (fm) fm.style.display = "none";
  });

  // 走一级入口进工具包
  const navOk = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll(".navGroup, .nav-item, [data-tab]"));
    const t = btns.find((b) => (b.textContent || "").indexOf("工具包") >= 0);
    if (t) { t.click(); return true; }
    if (window.showTab) { window.showTab("toolbox"); return true; }
    return false;
  });
  out.push(["点进工具包", navOk]);
  await new Promise((r) => setTimeout(r, 6000));

  const sec = await page.evaluate(() => {
    const heads = Array.from(document.querySelectorAll(".tb-secH"));
    const h = heads.find((x) => (x.textContent || "").indexOf("准入门禁") >= 0);
    if (!h) return { ok: false, reason: "没找到「外来因子 · 准入门禁」标题" };
    return { ok: true, headText: (h.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160) };
  });
  out.push(["门禁小节存在", sec.ok, sec.headText || sec.reason]);

  const rows = await page.evaluate(() => {
    const tbl = document.querySelector("#tbFac table");
    if (!tbl) return null;
    return Array.from(tbl.querySelectorAll("tbody tr")).map((tr) => ({
      name: (tr.children[0] || {}).textContent || "",
      stat: (tr.children[1] || {}).textContent || "",
      note: (tr.children[6] || {}).textContent || "",
      statColor: (tr.children[1] && tr.children[1].firstElementChild)
        ? getComputedStyle(tr.children[1].firstElementChild).color : "",
    }));
  });
  out.push(["因子表有行", !!(rows && rows.length), rows ? rows.length + " 行" : "无表"]);
  const hasReason = !!(rows || []).some((r) => r.stat.indexOf("未通过") >= 0 && r.note.length > 10);
  out.push(["未通过的写了原因", hasReason]);
  const hasShadow = !!(rows || []).some((r) => r.stat.indexOf("影子") >= 0);
  out.push(["影子期可见", hasShadow]);
  out.push(["状态徽章有独立配色", !!(rows || []).some((r) => r.statColor && r.statColor !== "rgb(0, 0, 0)")]);

  // 缺 why 时前端应挡住，不发请求
  const guard = await page.evaluate(async () => {
    const ex = document.getElementById("tbFacExpr");
    const wh = document.getElementById("tbFacWhy");
    const msg = document.getElementById("tbFacMsg");
    if (!ex || !wh || !msg) return { ok: false, reason: "输入框或提示位缺失" };
    let called = 0;
    const old = window.fetch;
    window.fetch = function () { called++; return old.apply(this, arguments); };
    ex.value = "close/ma(close,20)-1";
    wh.value = "";
    window.TB.admit();
    await new Promise((r) => setTimeout(r, 400));
    window.fetch = old;
    return { ok: called === 0 && msg.textContent.indexOf("必填") >= 0,
             called, text: msg.textContent.slice(0, 80) };
  });
  out.push(["缺 why 被前端挡下", guard.ok, guard.reason || ("fetch 调用 " + guard.called + " 次")]);

  // 来源待办区（三期·发现）
  const src = await page.evaluate(() => {
    const box = document.getElementById("tbSrc");
    if (!box) return { ok: false, reason: "没有 tbSrc 容器" };
    const tbl = box.querySelector("table");
    const rows = tbl ? tbl.querySelectorAll("tbody tr").length : 0;
    const head = (box.querySelector(".tb-secH") || {}).textContent || "";
    const links = box.querySelectorAll("a.tb-srcLink").length;
    const btns = box.querySelectorAll("button.tb-miniBtn").length;
    return { ok: rows > 0 && links > 0 && btns > 0, rows, links, btns,
             head: head.replace(/\s+/g, " ").trim().slice(0, 90) };
  });
  out.push(["来源待办有清单", src.ok, "行 " + src.rows + " 链接 " + src.links + " 按钮 " + src.btns]);
  out.push(["来源区标题说明", (src.head || "").indexOf("上次") >= 0, src.head]);

  const geo = await page.evaluate(() => ({
    docW: document.documentElement.scrollWidth,
    winW: window.innerWidth,
  }));
  out.push(["无横向溢出", geo.docW <= geo.winW + 1, "docW=" + geo.docW + " winW=" + geo.winW]);
  out.push(["零 pageerror", errs.length === 0, errs.slice(0, 3).join(" | ")]);

  // 滚到因子节再截图
  await page.evaluate(() => {
    const el = document.getElementById("tbFac");
    if (el) el.scrollIntoView({ block: "start" });
  });
  await new Promise((r) => setTimeout(r, 800));
  const shot = path.join(ROOT, "docs", "shot_factor_gate_20261103.png");
  await page.screenshot({ path: shot });
  await browser.close();

  let bad = 0;
  for (const [name, ok, extra] of out) {
    if (!ok) bad++;
    console.log((ok ? "  [PASS] " : "  [FAIL] ") + name + (extra ? "  -> " + extra : ""));
  }
  console.log("\n截图: " + shot);
  console.log(bad === 0 ? "FACTOR_GATE_OK" : "FACTOR_GATE_FAIL=" + bad);
  process.exit(bad === 0 ? 0 : 1);
})();
