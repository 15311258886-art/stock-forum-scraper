/* ============================================================================
 * 行业龙头徽章（金/银/铜牌）· 回归断言（2026-10-21 dy11）
 * ----------------------------------------------------------------------------
 * 进哥 2026-10-21 需求：
 *   ① 按行业标准判定龙头，分金/银/铜三档
 *   ② 徽章不止在「长期底部埋伏」，而是**点开每一只股票都要能明显看到**
 * 本回归验证：
 *   ① /api/dragon_map 可用且有牌数量合理（>500 只）
 *   ② 长期底部埋伏表格里真的渲染出徽章（不是只有接口有数据）
 *   ③ 同一行不会重复挂两枚（codeLink 对"代码+名称"只挂一次）
 *   ④ 点开个股（六位数代码按钮）后，抽屉里 #stkDragon 有徽章
 *   ⑤ 直接调 openStock('600519') 必须是金牌（dg-3）——定标：茅台=白酒Ⅱ第1名
 *   ⑥ 徽章有真实背景色（不是透明占位）
 *   ⑦ 零 JS 运行时错误 / 零 console.error
 * 只读，不改任何数据。用法：node tests/ui_regression_dragon.js [port]
 * ========================================================================== */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8765";
const BASE = "http://127.0.0.1:" + PORT;
const CHROMIUM_DIR = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

function pickChrome() {
  try {
    const dirs = fs.readdirSync(CHROMIUM_DIR).filter((d) => d.startsWith("chromium-")).sort().reverse();
    for (const d of dirs) {
      for (const sub of ["chrome-win64", "chrome-win"]) {
        const p = path.join(CHROMIUM_DIR, d, sub, "chrome.exe");
        if (fs.existsSync(p)) return p;
      }
    }
  } catch (e) { /* ignore */ }
  return EDGE;
}

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log((pass ? "  PASS " : "  FAIL ") + name + (detail ? "  → " + detail : ""));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const pageErrors = [];
  const consoleErrors = [];
  const browser = await puppeteer.launch({
    executablePath: pickChrome(), headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-proxy-server", "--proxy-bypass-list=*"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1760, height: 1100 });
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 200)));

  async function waitFor(fn, ms = 30000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      try { if (await page.evaluate(fn)) return true; } catch (_) {}
      await sleep(400);
    }
    return false;
  }

  try {
    // 先真打开页面（否则 page.evaluate 里的 fetch 是在 about:blank 上跑，必然 Failed to fetch）
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 30000 });

    // ① 接口层
    const mapRes = await page.evaluate(async (base) => {
      const r = await fetch(base + "/api/dragon_map");
      const d = await r.json();
      return { ok: !!d.ok, n: Object.keys(d.m || {}).length, meta: d.meta || {} };
    }, BASE);
    rec("① /api/dragon_map 可用且有牌 >500 只", mapRes.ok && mapRes.n > 500,
      "有牌 " + mapRes.n + " 只 · 口径 " + ((mapRes.meta || {}).period_label || "?") +
      " · 行业 " + ((mapRes.meta || {}).industries || "?"));

    await page.goto(BASE + "/#watch/lowbox_bottom", { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(() => typeof loadBottom === "function", { timeout: 30000 });
    await waitFor(() => document.querySelectorAll("#p-lowbox_bottom table.wt-tb tbody tr").length > 0, 60000);
    await waitFor(() => document.querySelectorAll("#p-lowbox_bottom .dragon-badge").length > 0, 20000);

    // ② 表格里真有徽章
    const tbl = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      const badges = Array.from(p.querySelectorAll(".dragon-badge"));
      const rows = Array.from(p.querySelectorAll("table.wt-tb tbody tr"));
      const perRow = {};
      for (const b of badges) {
        const tr = b.closest("tr");
        if (!tr) continue;
        const k = Array.from(rows).indexOf(tr);
        perRow[k] = (perRow[k] || 0) + 1;
      }
      const maxPerRow = Math.max(0, ...Object.values(perRow));
      const cls = badges.map((b) => b.className).join(" ");
      return {
        n: badges.length, rows: rows.length, maxPerRow,
        gold: badges.filter((b) => b.classList.contains("dg-3")).length,
        silver: badges.filter((b) => b.classList.contains("dg-2")).length,
        bronze: badges.filter((b) => b.classList.contains("dg-1")).length,
        sample: badges.slice(0, 3).map((b) => b.textContent.trim()),
        bg: badges.length ? getComputedStyle(badges[0]).backgroundImage.slice(0, 40) : "",
        cls,
      };
    });
    rec("② 底部埋伏表格渲染出徽章", tbl.n > 0,
      "徽章 " + tbl.n + " 枚 / " + tbl.rows + " 行（金" + tbl.gold + " 银" + tbl.silver + " 铜" + tbl.bronze + "）样例 " + tbl.sample.join("|"));
    rec("③ 同一行不重复挂牌", tbl.maxPerRow <= 1, "单行最多 " + tbl.maxPerRow + " 枚");
    rec("⑥ 徽章有真实背景（非透明）", /gradient|rgb/.test(tbl.bg), tbl.bg);

    // ④ 点开「表格里带牌的那只票」→ 抽屉里必须出现同档徽章
    //    （底部表格的代码单元格是 <a class="wt-code" data-wt-open="...">，走 document 级委托打开抽屉）
    const picked = await page.evaluate(() => {
      const p = document.querySelector("#p-lowbox_bottom");
      const b = p.querySelector(".dragon-badge[data-dg-for]");
      if (!b) return null;
      const code = b.getAttribute("data-dg-for");
      const el = p.querySelector('[data-wt-open="' + code + '"]') || p.querySelector('[data-code-open="' + code + '"]');
      if (!el) return null;
      el.click();
      const m = /dg-([123])/.exec(b.className);
      return { code: code, tier: m ? Number(m[1]) : 0 };
    });
    await waitFor(() => {
      const d = document.querySelector("#stkDragon");
      return !!(d && d.querySelector(".dragon-badge"));
    }, 20000);
    const drawer = await page.evaluate(() => {
      const d = document.querySelector("#stkDragon");
      const b = d && d.querySelector(".dragon-badge");
      return { txt: d ? d.textContent.trim() : "", cls: b ? b.className : "" };
    });
    const dgTier = (/dg-([123])/.exec(drawer.cls) || [])[1];
    rec("④ 点开个股后抽屉显示同档龙头牌", !!picked && !!dgTier && Number(dgTier) === picked.tier,
      (picked ? ("点了 " + picked.code + "（第" + picked.tier + "档）") : "表格里没找到带牌的行") +
      " · 抽屉「" + drawer.txt + "」class=" + (drawer.cls || "(空)"));

    // ⑤ 定标：茅台必须是金牌
    await page.evaluate(() => { try { openDrawer && null; openStock("600519"); } catch (e) {} });
    await waitFor(() => {
      const d = document.querySelector("#stkDragon");
      return !!(d && d.querySelector(".dragon-badge"));
    }, 20000);
    const mt = await page.evaluate(() => {
      const d = document.querySelector("#stkDragon");
      const b = d && d.querySelector(".dragon-badge");
      return { html: d ? d.textContent.trim() : "", cls: b ? b.className : "", color: b ? getComputedStyle(b).color : "" };
    });
    rec("⑤ 贵州茅台=金牌龙头（定标）", /dg-3/.test(mt.cls) && /金牌/.test(mt.html),
      "文案「" + mt.html + "」class=" + mt.cls + " color=" + mt.color);

    // ⑦ 零报错
    rec("⑦ 零 JS 运行时错误", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | ") || "无");
    rec("⑦b 零 console.error", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | ") || "无");

    const shot = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/tests/shot_dragon_modal.png";
    await page.screenshot({ path: shot });
    console.log("  [shot] " + shot);
  } catch (e) {
    rec("执行异常", false, String(e).slice(0, 200));
  } finally {
    await browser.close();
  }

  const ok = results.filter((r) => r.pass).length;
  console.log("\n==== " + ok + "/" + results.length + " 通过 ====\n");
  process.exit(ok === results.length ? 0 : 1);
})();
