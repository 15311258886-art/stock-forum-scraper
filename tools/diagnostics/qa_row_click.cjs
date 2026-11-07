/* 结果表「点股票能打开详情」验证 —— 2026-11-05
 *
 * 背景：王总截图反馈「这里股票点不开」—— 工具箱筛台的最终名单只是一张静态表，
 *       行上没有 data-sym、没有点击委托，点名称/代码毫无反应。
 * 改法：renderRes() 给每行加 data-tbx-sym，bindOnce 的 document 委托里调 window.openStock。
 *
 * 用法：node tools/qa_row_click.cjs
 * 产出：viewer/logs/qa_row_*.png + viewer/logs/qa_row_click.json
 *
 * 断言：
 *   A. 表里每行都有 data-tbx-sym（6 位代码）
 *   B. 真实鼠标点击「名称」格 → 全局抽屉 #drawer 显示、标题为「股票 · <code>」、正文含该代码
 *   C. 点击「原因」格（表格最右列）同样能打开 —— 整行可点，不是只有名称能点
 *   D. 勾选「显示被淘汰的 N 只」后，点淘汰行也能打开（且抽屉不会因 tbx 抽屉而打架）
 *   E. 拖选文字（有 selection）时不抢点击 —— 不弹个股抽屉
 *   F. pageerror / console error 为空
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const ROOT = path.join(__dirname, "..");
const LOGS = path.join(ROOT, "viewer", "logs");
const BASE = "http://127.0.0.1:8765";
const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));

const out = { startedAt: new Date().toISOString(), checks: {}, notes: [], pageErrors: [], consoleErrors: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function mark(k, ok, info) {
  out.checks[k] = { ok: !!ok, info: info == null ? "" : String(info) };
  if (info != null) out.notes.push(k + ": " + info);
}

async function drawerState(page) {
  return page.evaluate(() => {
    const d = document.getElementById("drawer");
    if (!d) return { shown: false, title: "", body: "" };
    return {
      shown: d.classList.contains("show"),
      title: (document.getElementById("drawerTitle") || {}).textContent || "",
      body: ((document.getElementById("drawerBody") || {}).textContent || "").slice(0, 400),
    };
  });
}

async function closeDrawer(page) {
  await page.evaluate(() => {
    const d = document.getElementById("drawer");
    if (d) d.classList.remove("show");
    const m = document.getElementById("mask");
    if (m) m.classList.remove("show");
  });
  await sleep(150);
}

(async () => {
  if (!EXE) { console.log("找不到 Edge/Chrome"); process.exit(2); }
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1200"],
    defaultViewport: { width: 1600, height: 1200 },
  });
  const page = await browser.newPage();
  page.on("pageerror", (e) => out.pageErrors.push(String(e && e.message || e)));
  page.on("console", (m) => { if (m.type() === "error") out.consoleErrors.push(m.text()); });
  // 全新 profile 首访会弹「首次启动引导」弹层（#firstRunMask 是全屏 mask），
  // 它的遮罩会让真实鼠标点击打在 .modal-mask 上 —— 真实用户早就点过「开始使用」，
  // 这里直接预置标记跳过，否则测的不是产品行为而是引导弹窗。
  await page.evaluateOnNewDocument(() => {
    try { localStorage.setItem("viewer-first-run-done", "1"); } catch (e) {}
  });

  try {
    await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForFunction(
      () => !!document.getElementById("tbxLane") && document.querySelectorAll(".tbx-card").length >= 5,
      { timeout: 60000, polling: 50 }
    );

    mark("A0_openStock存在", await page.evaluate(() => typeof window.openStock === "function"),
      await page.evaluate(() => typeof window.openStock));

    // 下第一刀（命中预热缓存，应 ~250ms）
    await page.evaluate(() => {
      const c = document.querySelector('[data-tbx-card="box_bottom"]');
      if (c) c.click();
    });
    await page.waitForFunction(
      () => document.querySelectorAll("#tbxRes tr[data-tbx-sym]").length > 0,
      { timeout: 60000, polling: 50 }
    );

    const rows = await page.evaluate(() =>
      Array.prototype.slice.call(document.querySelectorAll("#tbxRes tr[data-tbx-sym]")).map((tr) => ({
        sym: tr.getAttribute("data-tbx-sym"),
        cells: tr.children.length,
        dead: tr.classList.contains("tbx-rowDead"),
        cursor: getComputedStyle(tr).cursor,
        underline: getComputedStyle(tr.children[0]).textDecorationLine,
      }))
    );
    out.rowsSample = rows.slice(0, 3);
    mark("A_每行带代码", rows.length > 0 && rows.every((r) => /^\d{6}$/.test(r.sym || "")), rows.length + " 行 / 首行 " + (rows[0] || {}).sym);
    mark("A2_手型光标", rows[0] && rows[0].cursor === "pointer", rows[0] && rows[0].cursor);
    mark("A3_名称带下划线", rows[0] && rows[0].underline.indexOf("underline") >= 0, rows[0] && rows[0].underline);

    const first = rows[0].sym;

    // B：真实鼠标点「名称」格（先滚到视口内 —— 结果表在页面下半部分，不滚会点到视口外）
    const box = await page.evaluate(() => {
      const tr = document.querySelector("#tbxRes tr[data-tbx-sym]");
      tr.scrollIntoView({ block: "center" });
      const td = tr.children[0];
      const r = td.getBoundingClientRect();
      const x = r.left + Math.min(24, r.width / 2);
      const y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      return { x, y, w: r.width, h: r.height, hit: hit ? (hit.tagName + "." + hit.className).slice(0, 40) : "null" };
    });
    out.clickProbe = box;
    await page.mouse.click(box.x, box.y);
    await page.waitForFunction(
      () => { const d = document.getElementById("drawer"); return d && d.classList.contains("show"); },
      { timeout: 20000, polling: 50 }
    );
    await sleep(600);
    let d = await drawerState(page);
    mark("B_点名称开抽屉", d.shown && d.title.indexOf("股票") >= 0 && d.body.indexOf(first) >= 0,
      "标题=" + d.title + " 含代码=" + (d.body.indexOf(first) >= 0));
    await page.screenshot({ path: path.join(LOGS, "qa_row_01_click_name.png") });
    await closeDrawer(page);

    // C：点最右「原因」格（整行可点）
    const box2 = await page.evaluate(() => {
      const tr = document.querySelector("#tbxRes tr[data-tbx-sym]");
      const td = tr.children[tr.children.length - 1];
      const r = td.getBoundingClientRect();
      return { x: r.left + Math.min(60, r.width / 2), y: r.top + r.height / 2 };
    });
    await page.mouse.click(box2.x, box2.y);
    await sleep(900);
    d = await drawerState(page);
    mark("C_点原因格也开", d.shown && d.title.indexOf("股票") >= 0, "标题=" + d.title);
    await closeDrawer(page);

    // E：拖选文字时不抢点击（有 selection 就不开抽屉）
    await page.evaluate(() => {
      const tr = document.querySelector("#tbxRes tr[data-tbx-sym]");
      const td = tr.children[tr.children.length - 1];
      const r = document.createRange();
      r.selectNodeContents(td);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    });
    const box3 = await page.evaluate(() => {
      const tr = document.querySelector("#tbxRes tr[data-tbx-sym]");
      const td = tr.children[tr.children.length - 1];
      const r = td.getBoundingClientRect();
      return { x: r.left + Math.min(40, r.width / 2), y: r.top + r.height / 2 };
    });
    await page.mouse.click(box3.x, box3.y);
    await sleep(700);
    d = await drawerState(page);
    mark("E_选中文字不误触", !d.shown, d.shown ? "误开抽屉：" + d.title : "未开抽屉（正确）");
    await page.evaluate(() => { const s = window.getSelection(); if (s) s.removeAllRanges(); });
    await closeDrawer(page);

    // D：勾选淘汰行 → 点淘汰行
    const hasDead = await page.evaluate(() => {
      const cb = document.querySelector('[data-tbx-act="toggle-dead"]');
      if (!cb) return false;
      cb.click();
      return true;
    });
    if (hasDead) {
      await page.waitForFunction(
        () => document.querySelectorAll("#tbxRes tr.tbx-rowDead[data-tbx-sym]").length > 0,
        { timeout: 20000, polling: 50 }
      );
      const deadSym = await page.evaluate(() => {
        const tr = document.querySelector("#tbxRes tr.tbx-rowDead[data-tbx-sym]");
        tr.scrollIntoView({ block: "center" });
        const r = tr.children[1].getBoundingClientRect();
        return { sym: tr.getAttribute("data-tbx-sym"), x: r.left + 20, y: r.top + Math.min(8, r.height / 2) };
      });
      await page.mouse.click(deadSym.x, deadSym.y);
      await sleep(900);
      d = await drawerState(page);
      mark("D_淘汰行也能开", d.shown && d.body.indexOf(deadSym.sym) >= 0, "代码 " + deadSym.sym + " 标题=" + d.title);
      await page.screenshot({ path: path.join(LOGS, "qa_row_02_click_dead.png") });
      await closeDrawer(page);
    } else {
      out.checks["D_淘汰行也能开"] = { ok: null, info: "没有淘汰勾选框（本筛道 n_failed=0）" };
    }

    // 顺带确认工具说明抽屉（tbx 自己那个）没被这行点击串到
    const tbxOpen = await page.evaluate(() => {
      const el = document.getElementById("tbxDrawer");
      return !!(el && el.classList.contains("open"));
    });
    mark("F_tbx抽屉未被串开", !tbxOpen, tbxOpen ? "tbxDrawer 被误开" : "未开（正确）");

    mark("G_pageerror为空", out.pageErrors.length === 0, out.pageErrors.join(" | "));
    const realErr = out.consoleErrors.filter((s) => s.indexOf("frame-ancestors") < 0);
    mark("H_consoleError为空", realErr.length === 0, realErr.join(" | "));

    await page.screenshot({ path: path.join(LOGS, "qa_row_03_after.png") });
  } catch (e) {
    out.fatal = String(e && e.stack || e);
  } finally {
    out.finishedAt = new Date().toISOString();
    fs.writeFileSync(path.join(LOGS, "qa_row_click.json"), JSON.stringify(out, null, 1), "utf8");
    const bad = Object.keys(out.checks).filter((k) => out.checks[k].ok === false);
    console.log("=== 结果表点击验证 ===");
    Object.keys(out.checks).forEach((k) => {
      const c = out.checks[k];
      console.log((c.ok === true ? "PASS " : c.ok === false ? "FAIL " : "SKIP ") + k + "  " + c.info);
    });
    if (out.fatal) console.log("FATAL: " + out.fatal);
    console.log(bad.length ? "=> 有 " + bad.length + " 项失败" : "=> 全部通过");
    await browser.close();
  }
})();
