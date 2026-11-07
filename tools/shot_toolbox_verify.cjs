/* 工具包验收截图（2026-11-03 进哥需求）。
 *
 * 必须"真渲染出来看到"才算数的四件事：
 *   ① 一级入口在决策台旁边，能点进去；
 *   ② 能力卡上有收录时间 / 为什么收 / 作用 / 实测表现；
 *   ③ 已淘汰的工具在架上，带「已淘汰」徽章 + 原因；
 *   ④ 漏斗能跑出结果，且每只票逐级通过情况可分辨（通过 / 第几级淘汰）。
 *
 * 用法：node tools/shot_toolbox_verify.cjs
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => fs.existsSync(p));

const BASE = "http://127.0.0.1:8765";
const ROOT = path.join(__dirname, "..");

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1600,1500"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1500, deviceScaleFactor: 1 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));

  await page.goto(BASE + "/#desk/desk_hot", { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask");
    if (m) m.style.display = "none";
    const fm = document.querySelector("#firstRunModal");
    if (fm) fm.style.display = "none";
  });

  // ① 一级入口：左栏里必须有「工具包」，且紧挨着决策台
  const nav = await page.evaluate(() => {
    const items = [...document.querySelectorAll("#sidenav [data-cat]")]
      .map((x) => ({ cat: x.getAttribute("data-cat"), label: (x.textContent || "").trim() }));
    const idx = items.findIndex((x) => x.label.includes("工具包"));
    return {
      labels: items.map((x) => x.label).slice(0, 6),
      hasToolbox: idx >= 0,
      prev: idx > 0 ? items[idx - 1].label : "",
      nextToDesk: idx > 0 && items[idx - 1].label.includes("决策台"),
    };
  });

  // 进工具包页
  await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "networkidle2", timeout: 60000 });
  try {
    await page.waitForSelector(".tb-cardBody", { timeout: 30000 });
  } catch (e) { errs.push("等待能力卡超时"); }
  await new Promise((r) => setTimeout(r, 1200));

  // ② 能力卡字段
  const card = await page.evaluate(() => {
    const heads = [...document.querySelectorAll(".tb-rowK")].map((x) => (x.textContent || "").trim());
    const meta = (document.querySelector(".tb-cardMeta") || {}).textContent || "";
    return {
      rows: heads,
      hasAdopted: meta.includes("收录"),
      meta: meta.slice(0, 120),
      hasWhy: heads.includes("为什么收它"),
      hasWhat: heads.includes("作用"),
      hasEvidence: heads.includes("实测表现"),
      listCount: document.querySelectorAll(".tb-item").length,
      title: (document.querySelector(".tb-title") || {}).textContent || "",
    };
  });

  // ③ 已淘汰工具：徽章 + 原因
  const retired = await page.evaluate(() => {
    const items = [...document.querySelectorAll(".tb-item")];
    const dead = items.filter((x) => (x.textContent || "").includes("已淘汰"));
    return {
      deadCount: dead.length,
      firstDead: dead.length ? (dead[0].textContent || "").trim().slice(0, 60) : "",
    };
  });
  // 点开第一个已淘汰工具，确认卡片上有原因
  const deadCard = await page.evaluate(() => {
    const items = [...document.querySelectorAll(".tb-item")];
    const dead = items.find((x) => (x.textContent || "").includes("已淘汰"));
    if (!dead) return { ok: false };
    dead.click();
    return { ok: true };
  });
  await new Promise((r) => setTimeout(r, 500));
  const deadDetail = await page.evaluate(() => {
    const box = document.querySelector(".tb-deadBox");
    const dead = document.querySelector(".tb-cardHead.dead");
    return {
      hasDeadBox: !!box,
      deadText: box ? (box.textContent || "").trim().slice(0, 160) : "",
      headMarked: !!dead,
    };
  });
  await page.screenshot({ path: path.join(ROOT, "_tb_card.png"), fullPage: true });

  // ④ 跑一套预置漏斗（趋势跟随：均线多头 → 放量突破，两级都秒级）
  const ran = await page.evaluate(() => {
    const btn = [...document.querySelectorAll(".tb-preset")]
      .find((b) => (b.textContent || "").includes("趋势跟随"));
    if (btn) { btn.click(); return true; }
    return false;
  });
  await new Promise((r) => setTimeout(r, 1500));
  // 等结果表出现（最多 45 秒：可能要走离进程重算）
  try {
    await page.waitForFunction(
      () => document.querySelectorAll(".tb-tbl tbody tr").length > 0, { timeout: 45000 });
  } catch (e) { errs.push("等待漏斗结果超时"); }
  await new Promise((r) => setTimeout(r, 1500));

  const result = await page.evaluate(() => {
    const nodes = [...document.querySelectorAll(".tb-node")].map((n) => (n.textContent || "").trim());
    const row0 = document.querySelector(".tb-tbl tbody tr");
    return {
      presetClicked: true,
      nodes,
      hasStepNotes: document.querySelectorAll(".tb-note").length,
      rows: document.querySelectorAll(".tb-tbl tbody tr").length,
      dots: document.querySelectorAll(".tb-dot").length,
      firstRow: row0 ? [...row0.children].map((td) => (td.textContent || "").trim()).slice(0, 8) : [],
      toggleShown: !!document.querySelector(".tb-toggle"),
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
    };
  });
  // 打开"显示被淘汰的"，确认能看到死在第几级
  await page.evaluate(() => {
    const cb = document.querySelector(".tb-toggle input");
    if (cb && !cb.checked) cb.click();
  });
  await new Promise((r) => setTimeout(r, 800));
  const dropped = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".tb-tbl tbody tr.tb-rowDead")];
    const one = rows.find((r) => (r.textContent || "").includes("淘汰"));
    return {
      deadRows: rows.length,
      sample: one ? (one.textContent || "").trim().slice(0, 120) : "",
    };
  });

  await page.screenshot({ path: path.join(ROOT, "_tb_funnel.png"), fullPage: true });
  console.log(JSON.stringify({ nav, card, retired, deadCard, deadDetail, ran, result, dropped,
    errors: errs }, null, 2));
  await browser.close();
})().catch((e) => {
  console.error("FATAL", e.message);
  process.exit(1);
});
