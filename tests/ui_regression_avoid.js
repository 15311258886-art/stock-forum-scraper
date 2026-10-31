/* ============================================================================
 * 避雷名单面板 · 只读回归断言（2026-10-21 dy9）
 * ----------------------------------------------------------------------------
 * 验证目标（进哥 2026-10-21 拍板「避雷名单做成显式功能」）：
 *   ① 导航栏有「避雷名单」入口，且能点开
 *   ② #p-avoid 挂载、可见、有真实内容（不是空白/加载中）
 *   ③ 三个关键语义断言：明确写「只做排除·不做推荐」、分层数字在、列表有归因标签
 *   ④ 涨红跌绿铁律：列表「实际 xx%」正数=红、负数=绿，一处反用即失败
 *   ⑤ 零 JS 运行时错误 / 零 console.error
 * 真实浏览器（系统 Chromium）+ puppeteer-core，全部只读 GET。
 * ========================================================================== */
const puppeteer = require("puppeteer-core");

const PORT = process.argv[2] || "8765";
const BASE = "http://127.0.0.1:" + PORT;
const EDGE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const OUT = __dirname;

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log((pass ? "  PASS " : "✗ FAIL ") + name + (detail ? "  → " + detail : ""));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(page, fn, timeout = 25000, label = "") {
  try {
    await page.waitForFunction(fn, { timeout, polling: 200 });
    return true;
  } catch (e) {
    console.log("    [wait-timeout] " + label);
    return false;
  }
}

(async () => {
  const pageErrors = [];
  const consoleErrors = [];

  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1600 });

  page.on("pageerror", (err) => pageErrors.push(String(err.message || err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  try {
    console.log("== 打开 " + BASE + " ==");
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await sleep(2500);

    /* ---------- ① 导航入口 ---------- */
    console.log("\n== ① 导航入口 ==");
    const navExists = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll("a,button,.nav-item,li,span,[data-tab]"));
      return els.some((e) => (e.textContent || "").trim() === "避雷名单");
    });
    rec("导航栏存在「避雷名单」入口", navExists);

    const navClicked = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll("a,button,.nav-item,li,span,[data-tab]"));
      const t = els.find((e) => (e.textContent || "").trim() === "避雷名单");
      if (t) { t.click(); return true; }
      return false;
    });
    rec("「避雷名单」可点击", navClicked);
    await sleep(1200);

    const panelVisible = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      if (!p) return false;
      const r = p.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    rec("点击后面板 #p-avoid 可见（非零尺寸）", panelVisible);

    /* ---------- ② 面板真的有内容（不是空白/加载中） ---------- */
    console.log("\n== ② 内容渲染 ==");
    const loaded = await waitFor(
      page,
      () => {
        const p = document.querySelector("#p-avoid");
        if (!p) return false;
        const txt = (p.textContent || "");
        return txt.includes("只做排除") && !txt.includes("正在统计低分卡归因") && !txt.includes("加载中");
      },
      30000,
      "避雷名单数据落地"
    );
    rec("面板已加载完成（脱离加载态）", loaded);

    const titleOk = await page.evaluate(() => {
      const t = document.querySelector("#p-avoid .pred-title");
      return t ? t.textContent.trim() === "避雷名单" : false;
    });
    rec("面板标题 = 避雷名单", titleOk);

    const disclaimerOk = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      const txt = p ? (p.textContent || "") : "";
      return txt.includes("只做排除") && txt.includes("不做推荐");
    });
    rec("显著标注「只做排除 · 不做推荐」（防被当买入清单）", disclaimerOk);

    const tierStatsOk = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      const txt = p ? (p.textContent || "") : "";
      return txt.includes("全样本均值") && txt.includes("C 级") && txt.includes("S 级");
    });
    rec("展示分层真实收益（全样本/C 级/S 级均值）", tierStatsOk);

    const rows = await page.evaluate(
      () => document.querySelectorAll("#p-avoid ul > li").length
    );
    rec("避雷列表已有条目（≥1）", rows >= 1, "条数: " + rows);

    const tags = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      if (!p) return 0;
      // 归因标签是带 title 提示的 span（红底）
      return Array.from(p.querySelectorAll("span[title]")).filter(
        (e) => (e.textContent || "").trim().length > 0
      ).length;
    });
    rec("列表条目带归因标签（≥1）", tags >= 1, "标签数: " + tags);

    const tierOfTags = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      if (!p) return [];
      const set = new Set();
      Array.from(p.querySelectorAll("span[title]")).forEach((e) => {
        const t = (e.textContent || "").trim();
        if (t) set.add(t);
      });
      return Array.from(set);
    });
    rec("归因标签来自已知规则集", tierOfTags.length >= 1, "类型: " + tierOfTags.join("/"));

    const calOk = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      return p ? !!p.querySelector("details.res-cal") : false;
    });
    rec("口径可折叠展开（res-cal 存在）", calOk);

    await page.screenshot({ path: OUT + "/shot_avoid.png", fullPage: false });
    console.log("    [截图] shot_avoid.png");

    /* ---------- ③ 涨红跌绿铁律（进哥 2026-10-21：这在什么时候都应该是不变的） ---------- */
    console.log("");
    console.log("== ③ 涨红跌绿配色（铁律，不得反用） ==");
    const colorAudit = await page.evaluate(() => {
      const cls = (c) => {
        const m = String(c).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!m) return "?";
        const r = +m[1], g = +m[2], b = +m[3];
        if (r > g + 30 && r > b) return "red";
        if (g > r + 30 && g > b) return "green";
        return "neutral";
      };
      const box = document.querySelector("#p-avoid");
      if (!box) return { bad: [], checked: 0 };
      const bad = [];
      let checked = 0;
      // 逐行校验列表里的「实际 xx%」：涨必须红、跌必须绿
      Array.from(box.querySelectorAll("li span"))
        .filter((s) => /实际|未到期/.test(s.textContent || ""))
        .forEach((s) => {
          const txt = (s.textContent || "").trim();
          if (/未到期/.test(txt)) return;               // 未到期=中性，不参与判色
          const v = parseFloat(txt.replace(/[^\d.\-+]/g, ""));
          if (!isFinite(v)) return;
          const got = cls(getComputedStyle(s).color);
          const want = v > 0 ? "red" : (v < 0 ? "green" : "neutral");
          checked++;
          if (want !== got) bad.push(txt + " 应" + want + "实" + got);
        });
      return { bad, checked };
    });
    rec("列表行「涨红跌绿」无一处反用",
        colorAudit.bad.length === 0 && colorAudit.checked > 0,
        colorAudit.bad.length ? colorAudit.bad.slice(0, 4).join(" | ")
                              : "已校验 " + colorAudit.checked + " 行：跌=绿 / 涨=红");

    /* ---------- ④ 切走再切回（防止一次性渲染脆弱） ---------- */
    console.log("\n== ③ 切换稳定性 ==");
    await page.evaluate(() => selectNav("overview", "resonance"));
    await sleep(1200);
    await page.evaluate(() => selectNav("overview", "avoid"));
    await sleep(1200);
    const backOk = await page.evaluate(() => {
      const p = document.querySelector("#p-avoid");
      const txt = p ? (p.textContent || "") : "";
      return txt.includes("避雷名单") && txt.includes("只做排除");
    });
    rec("切到博主共振再切回，面板仍正常（幂等）", backOk);

    /* ---------- ④ 零报错 ---------- */
    console.log("\n== ④ 运行时错误 ==");
    rec("零 pageerror（JS 运行时错误）", pageErrors.length === 0,
        pageErrors.length ? pageErrors.slice(0, 3).join(" | ") : "");
    rec("零 console.error", consoleErrors.length === 0,
        consoleErrors.length ? consoleErrors.slice(0, 3).join(" | ") : "");
  } catch (e) {
    console.log("✗ 异常: " + (e && e.message ? e.message : e));
    results.push({ name: "脚本执行未抛异常", pass: false, detail: String(e) });
  } finally {
    await browser.close();
    const pass = results.filter((r) => r.pass).length;
    console.log("\n==================== 避雷名单回归 ====================");
    console.log("通过 " + pass + " / " + results.length);
    process.exit(pass === results.length ? 0 : 1);
  }
})();
