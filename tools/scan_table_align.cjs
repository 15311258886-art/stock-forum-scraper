/* 全站表格「表头 vs 单元格」对齐扫描（2026-11-02）。
 *
 * 为什么需要它：进哥反馈决策台「名称/代码/得分/来自没对齐」，根因是同一列的
 * <th> 与 <td> 用了不同的 text-align（`.dk-tbl th{left}` vs `.dk-tbl .c{right}`）。
 * 这个毛病肉眼要一列一列看才发现，但机器判起来是确定的：
 *     同列 th.align !== td.align，且两者内容右缘相差 > 24px  → 视觉错位。
 * 于是把全站每个 tab 都跑一遍，给出清单 —— 有则一并修，无则拿数据回答"别的板块没有"。
 *
 * 用法：node tools/scan_table_align.cjs [--verbose]
 */
const path = require("path");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => require("fs").existsSync(p));
const BASE = "http://127.0.0.1:8765";

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: "new",
    args: ["--no-sandbox", "--window-size=1600,1000"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask"); if (m) m.style.display = "none";
  });

  // 侧栏所有 #cat/tab 链接（含折叠区）—— 直接读 DOM，比硬编 NAV 更不会漏
  const targets = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('a[href^="#"]').forEach((a) => {
      const h = a.getAttribute("href") || "";
      if (/^#[^/]+\/[^/]+$/.test(h) && !out.includes(h)) out.push(h);
    });
    return out;
  });

  const bad = [];
  const seen = new Set();
  for (const h of targets) {
    await page.evaluate((x) => { location.hash = x; }, h).catch(() => {});
    await new Promise((r) => setTimeout(r, 1100));
    const res = await page.evaluate((hash) => {
      const isShown = (el) => el && el.offsetParent !== null;
      const panel = [...document.querySelectorAll(".panel")].find((p) => isShown(p) && p.querySelector("table"));
      if (!panel) return [];
      const rows = [];
      panel.querySelectorAll("table").forEach((t, ti) => {
        if (!isShown(t)) return;
        const ths = [...t.querySelectorAll("thead th")];
        const tr0 = t.querySelector("tbody tr");
        if (!ths.length || !tr0) return;
        const tds = [...tr0.children];
        const items = [];
        ths.forEach((th, i) => {
          const td = tds[i];
          if (!td || td.tagName !== "TD") return;
          const a1 = getComputedStyle(th).textAlign;
          const a2 = getComputedStyle(td).textAlign;
          if (a1 === a2) return;
          const r1 = th.getBoundingClientRect(), r2 = td.getBoundingClientRect();
          // 内容边缘差：左对齐 vs 右对齐时，差 ≈ 列宽 - 两侧 padding - 文字宽
          const rg = document.createRange(); rg.selectNodeContents(td);
          const rr = rg.getBoundingClientRect();
          const gap = Math.abs(rr.right - (r2.right - parseFloat(getComputedStyle(td).paddingRight)));
          const gapL = Math.abs(rr.left - (r2.left + parseFloat(getComputedStyle(td).paddingLeft)));
          items.push({ col: (th.textContent || "").trim().slice(0, 10), thAlign: a1, tdAlign: a2,
                       colW: +r1.width.toFixed(0), innerGap: +Math.max(gap, gapL).toFixed(0) });
        });
        if (items.length) {
          rows.push({ table: ti, cls: t.className || "(no class)", nRows: t.querySelectorAll("tbody tr").length, items });
        }
      });
      return rows;
    }, h);

    for (const r of res) {
      const key = h + "|" + r.cls + "|" + r.table;
      if (seen.has(key)) continue;
      seen.add(key);
      // 只报「真的看得出来」的：列宽 >= 40px 且行数 >= 2（单行/极窄列排除，属显微镜级别）
      const hard = r.items.filter((x) => x.colW >= 40);
      if (hard.length && r.nRows >= 2) bad.push({ hash: h, cls: r.cls, nRows: r.nRows, cols: hard });
    }
  }

  console.log("\n=== 扫描页数:", targets.length, " 发现错位表格:", bad.length, "===");
  for (const b of bad) {
    console.log(`\n${b.hash}  table.${b.cls}  (${b.nRows} 行)`);
    for (const c of b.cols) {
      console.log(`   · ${c.col.padEnd(8)} th=${c.thAlign} td=${c.tdAlign}  列宽${c.colW}px 内缘差${c.innerGap}px`);
    }
  }
  if (!bad.length) console.log("没有发现「同列表头与单元格对齐方式不一致」的表格。");
  await browser.close();
})();
