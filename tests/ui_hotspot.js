/**
 * 热点卡改造的 UI 回归断言（真实 Chromium + 真实布局矩形）
 *
 * 覆盖三件事：
 *   1) 海外模块「去噪」：证据块消失、看多/看空徽章消失；
 *   2) 海外模块「增强」：涨跌预判按细分领域列出代表公司（中文名 + 6 位代码），
 *      卡头出现明确的决策结论；
 *   3) 反向守卫：国内股吧模块的多空信息**必须还在**（防误删）；
 *      外加真实排版检查（领域名文字横向溢出、文字压到公司标签）。
 *
 * 全程只读：只发 GET 请求，不点「载入示例」「抓取并更新」等写接口。
 * 用法： node tests/ui_hotspot.js [端口]     默认 8766
 */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8766";
const URL = `http://127.0.0.1:${PORT}/index.html`;
const EXE = "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe";
const ROOT = path.join(__dirname, "..");
const SHOT_DIR = path.join(ROOT, "tools", "shots");
const REPORT = path.join(ROOT, "tools", "_qa_ui.txt");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (name, cond, extra) =>
  out.push(`${cond ? "PASS" : "FAIL"}  ${name}${extra !== undefined ? "  —— " + extra : ""}`);
const skip = (name, extra) => out.push(`SKIP  ${name}${extra !== undefined ? "  —— " + extra : ""}`);
const info = (name, extra) => out.push(`INFO  ${name}${extra !== undefined ? "  —— " + extra : ""}`);

const goTab = async (page, tab) => {
  STEP = "click tab " + tab;
  await page.click(`.tab[data-tab="${tab}"]`);
  await wait(240);
};

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  STEP = "goto " + URL;
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 30000 });

  /* ---------- A. 海外热点面板 ---------- */
  await goTab(page, "overseas_hotspots");
  const nCards = await page.$$eval("#p-overseas_hotspots .ov-hs-card", (e) => e.length);
  ok("热点卡已渲染（至少 1 张）", nCards > 0, `共 ${nCards} 张`);

  const hs = await page.evaluate(() => {
    const p = document.querySelector("#p-overseas_hotspots");
    const badgeTexts = [...p.querySelectorAll(".badge")].map((b) => b.textContent.trim());
    const rows = [...p.querySelectorAll(".ov-hs-dom-row")];
    const stks = [...p.querySelectorAll(".stk")].map((s) => ({
      n: (s.querySelector(".stk-n") || {}).textContent || "",
      c: (s.querySelector(".stk-c") || {}).textContent || "",
    }));
    // 用文字实际占用矩形（Range）判压字，盒子矩形判不出来
    const textOverlap = rows.filter((r) => {
      const n = r.querySelector(".ov-hs-dom-name");
      const s = r.querySelector(".ov-hs-dom-stks");
      if (!n || !s || !n.textContent.trim()) return false;
      const rng = document.createRange();
      rng.selectNodeContents(n);
      const a = rng.getBoundingClientRect();
      const b = s.getBoundingClientRect();
      return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    }).length;
    return {
      innerText: p.innerText || "",
      hasEvNode: p.querySelectorAll(".ov-hs-ev").length,
      sentBadges: badgeTexts.filter((t) => /^(看多|看空|中性)$/.test(t)).length,
      decCards: p.querySelectorAll(".ov-hs-decision").length,
      decShort: [...p.querySelectorAll(".ov-hs-dec-txt")].filter((d) => (d.textContent || "").trim().length < 8).length,
      decSample: ((p.querySelector(".ov-hs-dec-txt") || {}).textContent || "").trim(),
      upGrp: p.querySelectorAll(".ov-hs-fc-grp.up").length,
      downGrp: p.querySelectorAll(".ov-hs-fc-grp.down").length,
      rowCount: rows.length,
      stkCount: stks.length,
      stkBad: stks.filter((s) => !/[\u4e00-\u9fa5]/.test(s.n) || !/^\d{6}$/.test(s.c)).length,
      stkSample: stks.slice(0, 5).map((s) => `${s.n}(${s.c})`),
      fallbackNote: /细分领域代表公司需重新/.test(p.innerText || ""),
      overflow: rows.filter((r) => {
        const n = r.querySelector(".ov-hs-dom-name");
        return n && n.scrollWidth > n.clientWidth + 2;
      }).length,
      textOverlap,
    };
  });

  ok("已删除「证据（原始条目）」文本", !/证据（原始条目）/.test(hs.innerText));
  ok("已删除证据区块节点 .ov-hs-ev", hs.hasEvNode === 0, `节点数=${hs.hasEvNode}`);
  ok("热点面板内无 看多/看空/中性 徽章", hs.sentBadges === 0, `残留=${hs.sentBadges}`);
  ok("模块说明已去除「附原始证据可追溯」", !/附原始证据可追溯/.test(hs.innerText));
  ok("每张卡都有决策结论块", nCards > 0 && hs.decCards === nCards, `结论块=${hs.decCards} / 卡=${nCards}`);
  ok("决策结论文字非空（>=8 字）", hs.decShort === 0, `过短=${hs.decShort}`);
  info("结论样例", hs.decSample.slice(0, 100));
  ok("涨跌预判已按细分领域分组", hs.upGrp + hs.downGrp > 0, `上涨组=${hs.upGrp} 下跌组=${hs.downGrp}`);
  ok("领域行已渲染", hs.rowCount > 0, `领域行=${hs.rowCount}`);
  ok("公司标签已渲染", hs.stkCount > 0, `公司=${hs.stkCount}`);
  ok("每家公司都是「中文名 + 6 位代码」", hs.stkCount > 0 && hs.stkBad === 0, `不合格=${hs.stkBad}`);
  info("公司样例", hs.stkSample.join(" / "));
  ok("未出现降级占位提示（数据已重算）", !hs.fallbackNote);
  ok("领域名无横向溢出", hs.overflow === 0, `溢出=${hs.overflow}`);
  ok("领域名文字未压到公司标签", hs.textOverlap === 0, `压字=${hs.textOverlap}`);

  /* ---------- B. 海外舆情面板 ---------- */
  await goTab(page, "overseas_sentiment");
  const os = await page.evaluate(() => {
    const p = document.querySelector("#p-overseas_sentiment");
    const badges = [...p.querySelectorAll(".badge")].map((b) => b.textContent.trim());
    return {
      sent: badges.filter((t) => /^(看多|看空|中性)$/.test(t)).length,
      distCards: p.querySelectorAll(".bars .bar-card").length,
      newsSent: [...p.querySelectorAll(".ov-news-top .badge")].map((b) => b.textContent.trim())
        .filter((t) => /^(看多|看空|中性)$/.test(t)).length,
      symSent: [...p.querySelectorAll(".ov-sym .badge")].map((b) => b.textContent.trim())
        .filter((t) => /^(多|空)\s*\d+$/.test(t)).length,
      newsCount: p.querySelectorAll(".ov-news").length,
      symCount: p.querySelectorAll(".ov-sym").length,
    };
  });
  ok("海外舆情面板无 看多/看空/中性 徽章", os.sent === 0, `残留=${os.sent}`);
  ok("海外舆情面板已移除情绪分布三格", os.distCards === 0, `残留=${os.distCards}`);
  ok("海外新闻流条目无情绪徽章", os.newsSent === 0, `新闻条数=${os.newsCount} 残留=${os.newsSent}`);
  ok("重点提及标的已去掉 多N/空N", os.symSent === 0, `标的数=${os.symCount} 残留=${os.symSent}`);
  info("海外面板数据量", `新闻 ${os.newsCount} 条 / 标的 ${os.symCount} 个`);

  /* ---------- C. 反向守卫：国内模块必须完好 ---------- */
  await goTab(page, "sentiment");
  const dom = await page.evaluate(() => {
    const p = document.querySelector("#p-sentiment");
    return {
      bull: p.querySelectorAll(".bar-val.bull").length,
      bear: p.querySelectorAll(".bar-val.bear").length,
      neu: p.querySelectorAll(".bar-val.neu").length,
      empty: p.querySelectorAll(".empty").length,
      // 国内模块的多空口径是「多 N / 空 N / 中性 N」（非「看多/看空」三字徽章），
      // 断言必须按它真实的口径写，否则永远为 0、变成一条无效断言。
      distBadges: [...p.querySelectorAll(".badge")].map((b) => b.textContent.trim())
        .filter((t) => /^(多|空|中性)\s*\d+$/.test(t)).length,
      exactSent: [...p.querySelectorAll(".badge")].map((b) => b.textContent.trim())
        .filter((t) => /^(看多|看空|中性)$/.test(t)).length,
    };
  });
  if (dom.empty > 0) {
    skip("国内情绪模块未被误删", "国内模块当前为空态，无法断言");
  } else {
    ok("国内情绪分布仍在（未被误删）",
      dom.bull >= 1 && dom.bear >= 1 && dom.neu >= 1,
      `bull=${dom.bull} bear=${dom.bear} neu=${dom.neu}`);
    ok("国内模块的「多N/空N」徽章保留（本次只删海外）",
      dom.distBadges > 0, `多空徽章=${dom.distBadges} ｜ 三字徽章=${dom.exactSent}`);
  }

  /* ---------- D. 版式：结论块高度与层级 ---------- */
  await goTab(page, "overseas_hotspots");
  const lay = await page.evaluate(() => {
    const c = document.querySelector("#p-overseas_hotspots .ov-hs-card");
    if (!c) return null;
    const dec = c.querySelector(".ov-hs-decision");
    const ov = c.querySelector(".ov-hs-block");
    if (!dec || !ov) return null;
    const a = dec.getBoundingClientRect(), b = ov.getBoundingClientRect();
    return { decH: Math.round(a.height), overlap: a.bottom > b.top + 1 };
  });
  if (lay) {
    ok("结论块高度正常（未被压扁）", lay.decH >= 24, `高=${lay.decH}px`);
    ok("结论块未与「① 事件概述」重叠", !lay.overlap);
  } else {
    skip("结论块版式检查", "无热点卡或结构缺失");
  }

  /* ---------- E. 四套主题配色可读性 + 截图 ---------- */
  const themes = ["light-tech", "dark-dashboard", "minimal-report", "card-warm"];
  for (const t of themes) {
    STEP = "theme " + t;
    await page.evaluate((id) => {
      document.querySelector("#theme-link").href = `static/themes/${id}.css`;
    }, t);
    await wait(420);
    await goTab(page, "overseas_hotspots");
    const cs = await page.evaluate(() => {
      const s = document.querySelector("#p-overseas_hotspots .stk");
      const d = document.querySelector("#p-overseas_hotspots .ov-hs-dec-txt");
      const k = document.querySelector("#p-overseas_hotspots .ov-hs-dec-tag");
      if (!s || !d) return null;
      const gs = getComputedStyle(s), gd = getComputedStyle(d), gk = getComputedStyle(k || s);
      return { stkBg: gs.backgroundColor, stkFg: gs.color, decFg: gd.color, tagBg: gk.backgroundColor };
    });
    if (cs) {
      ok(`[${t}] 公司标签底色≠文字色（可读）`, cs.stkBg !== cs.stkFg, `${cs.stkBg} vs ${cs.stkFg}`);
      ok(`[${t}] 结论文字与结论标签底色不同`, cs.decFg !== cs.tagBg, `${cs.decFg} vs ${cs.tagBg}`);
    } else {
      skip(`[${t}] 配色检查`, "无公司标签/结论块");
    }
    const card = await page.$("#p-overseas_hotspots .ov-hs-card");
    if (card) await card.screenshot({ path: path.join(SHOT_DIR, `hotspot-card-${t}.png`) });
  }
  await page.evaluate(() => {
    document.querySelector("#theme-link").href = "static/themes/light-tech.css";
  });
  await goTab(page, "overseas_hotspots");
  await wait(320);
  await page.screenshot({ path: path.join(SHOT_DIR, "hotspot-panel-full.png") });
  await goTab(page, "overseas_sentiment");
  await wait(320);
  await page.screenshot({ path: path.join(SHOT_DIR, "overseas-panel-full.png") });

  /* ---------- F. 运行时错误 ---------- */
  ok("无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  const passes = out.filter((l) => l.startsWith("PASS")).length;
  const skips = out.filter((l) => l.startsWith("SKIP")).length;
  out.push("");
  out.push(`=== ${passes}/${passes + fails} 通过（SKIP ${skips}） ===`);
  fs.writeFileSync(REPORT, out.join("\n"), "utf8");
  console.log(out.join("\n"));
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  out.push("RUNNER ERROR @ " + STEP + ": " + e.message + "\n" + e.stack);
  try { fs.writeFileSync(REPORT, out.join("\n"), "utf8"); } catch (x) { /* ignore */ }
  console.log(out.join("\n"));
  process.exit(2);
});
