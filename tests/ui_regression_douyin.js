/* ============================================================================
 * 豆包语料前端 · 只读回归断言（web-ui-regression 技能）
 * ----------------------------------------------------------------------------
 * 真实浏览器（系统 Edge）+ puppeteer-core，全部为只读 GET 验证，绝不点写库按钮。
 * 验证目标（进哥 2026-10-20 拍板）：
 *   ① 情绪面板 #p-sentiment 顶部「豆包舆情」独立框（标来源、不混算）
 *   ② 新建「技术方法」面板 #p-tech 渲染 / 分页 / 搜索 / 详情展开
 *   ③ 零 JS 运行时错误
 * 端口通过命令行传入，默认 8765。
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

async function waitFor(page, fn, timeout = 20000, label = "", arg = null) {
  try {
    await page.waitForFunction(fn, { timeout, polling: 200 }, arg);
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
    // 等首屏与 loadAll 异步请求落地（含 /api/sentiment → renderSentiment → DOUYIN 框）
    await sleep(2500);

    // 先激活 learn 大类，让「整合图谱」子项进入 DOM（子项仅所属大类激活时才懒渲染，初始默认大类不是 learn）
    await page.evaluate(() => selectNav("learn"));
    await sleep(700);

    /* ---------- 0. 导航入口与整合图谱可见性（严格：用户能看见并点开） ---------- */
    const navIntegrate = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('a,button,.nav-item,li,span,[data-tab]'));
      return els.some((e) => (e.textContent || "").includes("整合图谱"));
    });
    rec("导航栏存在「整合图谱」文字入口", navIntegrate);

    const navClicked = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('a,button,.nav-item,li,span,[data-tab]'));
      const t = els.find((e) => (e.textContent || "").includes("整合图谱"));
      if (t) { t.click(); return true; }
      return false;
    });
    await sleep(1200);
    const integVisible = await page.evaluate(() => {
      const p = document.querySelector("#p-integrate");
      if (!p) return false;
      const r = p.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    rec("点击「整合图谱」导航后面板可见", navClicked && integVisible);

    /* ---------- ① 情绪面板：豆包舆情框在顶部、标来源 ---------- */
    console.log("\n== ① 情绪面板（money / sentiment）==");
    await page.evaluate(() => selectNav("money", "sentiment"));
    await sleep(800);

    const sentMounted = await waitFor(
      page,
      () => !!document.querySelector('#dy-sent-mount .dy-box[data-dy="sentiment"]'),
      20000,
      "豆包舆情框挂载"
    );
    rec("情绪框已挂载 #dy-sent-mount .dy-box[data-dy=sentiment]", sentMounted);

    const sentTitle = await page.$eval(
      '#dy-sent-mount .dy-title',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("情绪框标题 = 豆包舆情", sentTitle === "豆包舆情", "实际: " + sentTitle);

    const sentSrc = await page.$eval(
      '#dy-sent-mount .dy-src',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("情绪框来源徽标 = 豆包整理", sentSrc === "豆包整理", "实际: " + sentSrc);

    const legendOk = await page.evaluate(() =>
      !!document.querySelector('#p-sentiment .dy-src-legend')
    );
    rec("情绪面板含「来源说明条」.dy-src-legend", legendOk);

    // 顶部定位：#dy-sent-mount 在「情绪地图」h3 之上
    const topOk = await page.evaluate(() => {
      const mount = document.querySelector('#dy-sent-mount');
      const h3 = Array.from(document.querySelectorAll('#p-sentiment h3'))
        .find((h) => h.textContent.includes('情绪地图'));
      if (!mount || !h3) return false;
      const a = mount.getBoundingClientRect();
      const b = h3.getBoundingClientRect();
      return a.top < b.top;
    });
    rec("豆包舆情框位于「情绪地图」上方（顶部独立成框）", topOk);

    const sentItemsOk = await waitFor(
      page,
      () => document.querySelectorAll('#dy-sent-mount .dy-item').length >= 1,
      25000,
      "豆包舆情框拉到数据"
    );
    const sentItems = await page.evaluate(
      () => document.querySelectorAll('#dy-sent-mount .dy-item').length
    );
    rec("豆包舆情框已拉到数据（≥1 条）", sentItemsOk, "条数: " + sentItems);

    await page.screenshot({ path: OUT + "/shot_sentiment.png", fullPage: false });
    console.log("    [截图] shot_sentiment.png");

    /* ---------- ② 技术方法面板：渲染 / 分页 / 搜索 / 详情 ---------- */
    console.log("\n== ② 技术方法面板（learn / tech）==");
    await page.evaluate(() => selectNav("learn", "tech"));
    await sleep(600);

    const techMounted = await waitFor(
      page,
      () => !!document.querySelector('#p-tech .dy-box[data-dy="tech"]'),
      20000,
      "技术面板挂载"
    );
    rec("技术面板已挂载 #p-tech .dy-box[data-dy=tech]", techMounted);

    const techTitle = await page.$eval(
      '#p-tech .dy-title',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("技术面板标题 = 豆包技术方法", techTitle === "豆包技术方法", "实际: " + techTitle);

    // 以「计数文案含 2701」作为首屏已加载的稳信号（含首屏 30 条渲染），限时放宽到 35s
    const techLoaded = await waitFor(
      page,
      () => {
        const c = document.querySelector('#p-tech [data-dy-count]');
        const items = document.querySelectorAll('#p-tech .dy-item').length;
        return (c && c.textContent.includes("2701")) || items >= 1;
      },
      60000,
      "技术面板首屏计数/数据"
    );
    rec("技术面板首屏已拉到数据（计数含 2701 或 ≥1 条）", techLoaded);
    await sleep(500);

    const techItems1 = await page.evaluate(
      () => document.querySelectorAll('#p-tech .dy-item').length
    );
    rec("技术面板首屏已渲染条目（≥1 条）", techItems1 >= 1, "首屏条数: " + techItems1);

    // 计数文案应含 2701（全量）
    const techCountTxt = await page.$eval(
      '#p-tech [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    rec("技术面板计数含全量 2701 条", techCountTxt.includes("2701"), "实际: " + techCountTxt);

    // 分页：点「加载更多」
    const beforeMore = await page.evaluate(
      () => document.querySelectorAll('#p-tech .dy-item').length
    );
    await page.evaluate(() => {
      const btn = document.querySelector('#p-tech [data-dy-more]');
      if (btn) btn.click();
    });
    let afterMore = beforeMore;
    for (let i = 0; i < 30; i++) {
      await sleep(400);
      afterMore = await page.evaluate(
        () => document.querySelectorAll('#p-tech .dy-item').length
      );
      if (afterMore > beforeMore) break;
    }
    rec("「加载更多」分页生效（条数增加）", afterMore > beforeMore, beforeMore + " → " + afterMore);

    // 搜索：均线
    await page.evaluate(() => {
      const inp = document.querySelector('#p-tech [data-dy-q]');
      inp.value = "均线";
      inp.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await sleep(900);
    const searchCountTxt = await page.$eval(
      '#p-tech [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    const searchItems = await page.evaluate(
      () => document.querySelectorAll('#p-tech .dy-item').length
    );
    const searchTotalMatch = (searchCountTxt.match(/共\s*(\d+)\s*条/) || [])[1];
    const searchTotal = searchTotalMatch ? parseInt(searchTotalMatch, 10) : -1;
    rec(
      "搜索「均线」命中且总数 < 2701（真实过滤）",
      searchTotal > 0 && searchTotal < 2701,
      "总数: " + searchTotal + "，首屏条数: " + searchItems
    );

    // 清空搜索恢复全量
    await page.evaluate(() => {
      const inp = document.querySelector('#p-tech [data-dy-q]');
      inp.value = "";
      inp.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await sleep(900);
    const clearTxt = await page.$eval(
      '#p-tech [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    rec("清空搜索恢复全量 2701", clearTxt.includes("2701"), "实际: " + clearTxt);

    // 详情展开
    await page.evaluate(() => {
      const btn = document.querySelector('#p-tech .dy-tbtn');
      if (btn) btn.click();
    });
    const detailOk = await waitFor(
      page,
      () => {
        const f = document.querySelector('#p-tech .dy-full');
        return f && f.textContent.trim().length > 0;
      },
      10000,
      "详情展开"
    );
    rec("点标题展开全文摘要（.dy-full 有内容）", detailOk);

    await page.screenshot({ path: OUT + "/shot_tech.png", fullPage: false });
    console.log("    [截图] shot_tech.png");

    /* ---------- ⑥ 零 JS 运行时错误 ---------- */
    /* ---------- ③ 整合图谱面板：板块联动 + 博主分域群像 ---------- */
    console.log("\n== ③ 整合图谱面板（learn / integrate）==");
    await page.evaluate(() => selectNav("learn", "integrate"));
    await sleep(1000);

    const integMounted = await waitFor(
      page,
      () => !!document.querySelector('#p-integrate .integ-hd .dy-src'),
      20000,
      "整合图谱面板挂载"
    );
    rec("整合图谱面板已挂载 #p-integrate .integ-hd .dy-src", integMounted);

    const integTitle = await page.$eval(
      '#p-integrate .integ-hd .dy-src',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("整合图谱标题徽标 = 整合图谱", integTitle === "整合图谱", "实际: " + integTitle);

    const integHdTitle = await page.$eval(
      '#p-integrate .integ-hd .dy-title',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("整合图谱主标题含「跨域整合」", integHdTitle.includes("跨域整合"), "实际: " + integHdTitle);

    const hotChips = await page.evaluate(
      () => document.querySelectorAll('#p-integrate #integ-hot .dy-chip[data-plate]').length
    );
    rec("热门板块 chip 已渲染（≥1）", hotChips >= 1, "个数: " + hotChips);

    const blogCols = await waitFor(
      page,
      () => document.querySelectorAll('#p-integrate #integ-blog .integ-col').length === 2
        && document.querySelectorAll('#p-integrate #integ-blog .integ-blog-card').length >= 1,
      25000,
      "博主分域群像两列卡片"
    );
    const blogCards = await page.evaluate(
      () => document.querySelectorAll('#p-integrate #integ-blog .integ-blog-card').length
    );
    rec("博主分域群像：技术派+观点派两列均已渲染（≥1 卡）", blogCols, "卡片数: " + blogCards);

    // 点热门板块 AI → 跨域共振三列
    const plateClicked = await page.evaluate(() => {
      const c = document.querySelector('#p-integrate #integ-hot .dy-chip[data-plate="AI"]');
      if (!c) return false;
      c.click();
      return true;
    });
    const plateRes = await waitFor(
      page,
      () => document.querySelectorAll('#p-integrate #integ-plate-res .integ-pcol').length === 3,
      20000,
      "板块联动三列"
    );
    const pcolText = await page.evaluate(() => {
      const cols = Array.from(document.querySelectorAll('#p-integrate #integ-plate-res .integ-pcol'));
      return cols.map((c) => c.querySelector('.ipc-h') ? c.querySelector('.ipc-h').textContent.trim() : '');
    });
    rec("点「AI」出跨域共振三列（技术/情绪/博主）", plateClicked && plateRes, pcolText.join(" | "));

    const aiTechN = await page.evaluate(() => {
      const h = document.querySelector('#p-integrate #integ-plate-res .integ-pcol .ipc-h');
      if (!h) return -1;
      const m = (h.textContent || '').match(/(\d+)\s*条/);
      return m ? parseInt(m[1], 10) : -1;
    });
    rec("AI 板块联动：技术方法条数 > 0（跨域化学反应有料）", aiTechN > 0, "技术方法条数: " + aiTechN);

    await page.screenshot({ path: OUT + "/shot_integrate.png", fullPage: false });
    console.log("    [截图] shot_integrate.png");

    /* ---------- ④ 技术主题轨道筛选 ---------- */
    console.log("\n== ④ 技术主题轨道筛选（learn / tech）==");
    await page.evaluate(() => selectNav("learn", "tech"));
    await sleep(1000);

    const trackChips = await waitFor(
      page,
      () => document.querySelectorAll('#p-tech .dy-tracks .dy-chip[data-track]').length >= 2,
      20000,
      "技术轨道 chip"
    );
    const trackCount = await page.evaluate(
      () => document.querySelectorAll('#p-tech .dy-tracks .dy-chip[data-track]').length
    );
    rec("技术面板含主题轨道 chip（≥2：全部+轨道）", trackChips, "chip 数: " + trackCount);

    const techFullTxt = await page.$eval(
      '#p-tech [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    rec("技术全量计数存在（含 2701）", techFullTxt.includes("2701"), "实际: " + techFullTxt);

    const pickedTrack = await page.evaluate(() => {
      const chips = Array.from(document.querySelectorAll('#p-tech .dy-tracks .dy-chip[data-track]'));
      const t = chips.find((c) => c.getAttribute('data-track'));
      if (!t) return null;
      t.click();
      return t.getAttribute('data-track');
    });
    const trackFiltered = await waitFor(
      page,
      () => {
        const c = document.querySelector('#p-tech [data-dy-count]');
        const m = (c ? c.textContent : '').match(/共\s*(\d+)\s*条/);
        return m && parseInt(m[1], 10) < 2701;
      },
      20000,
      "轨道筛选生效"
    );
    const trackFilteredTxt = await page.$eval(
      '#p-tech [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    rec("选主题轨道后列表真实过滤（总数 < 2701）", trackFiltered, "轨道=" + pickedTrack + " 实际: " + trackFilteredTxt);

    await page.evaluate(() => {
      const all = document.querySelector('#p-tech .dy-tracks .dy-chip[data-track=""]');
      if (all) all.click();
    });
    await sleep(800);

    /* ---------- ⑤ 情绪观点类型筛选 ---------- */
    console.log("\n== ⑤ 情绪观点类型筛选（money / sentiment）==");
    await page.evaluate(() => selectNav("money", "sentiment"));
    await sleep(1200);

    const stanceChips = await waitFor(
      page,
      () => document.querySelectorAll('#dy-sent-mount .dy-stance .dy-chip[data-stance]').length >= 2,
      20000,
      "观点类型 chip"
    );
    const stanceCount = await page.evaluate(
      () => document.querySelectorAll('#dy-sent-mount .dy-stance .dy-chip[data-stance]').length
    );
    rec("情绪面板含观点类型 chip（≥2：全部+类型）", stanceChips, "chip 数: " + stanceCount);

    const sentFullTxt = await page.$eval(
      '#dy-sent-mount [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    const sentFull = (sentFullTxt.match(/共\s*(\d+)\s*条/) || [])[1];
    rec("情绪全量计数存在", sentFull && parseInt(sentFull, 10) > 0, "实际: " + sentFullTxt);

    const pickedStance = await page.evaluate(() => {
      const chips = Array.from(document.querySelectorAll('#dy-sent-mount .dy-stance .dy-chip[data-stance]'));
      const s = chips.find((c) => c.getAttribute('data-stance'));
      if (!s) return null;
      s.click();
      return s.getAttribute('data-stance');
    });
    const stanceFiltered = await waitFor(
      page,
      (full) => {
        const c = document.querySelector('#dy-sent-mount [data-dy-count]');
        const m = (c ? c.textContent : '').match(/共\s*(\d+)\s*条/);
        return m && parseInt(m[1], 10) < full;
      },
      20000,
      "观点类型筛选生效",
      sentFull ? parseInt(sentFull, 10) : 999
    );
    const stanceFilteredTxt = await page.$eval(
      '#dy-sent-mount [data-dy-count]',
      (el) => el.textContent
    ).catch(() => "");
    rec("选观点类型后列表真实过滤（总数 < 全量 " + (sentFull || "?") + "）",
        stanceFiltered, "类型=" + pickedStance + " 实际: " + stanceFilteredTxt);

    await page.evaluate(() => {
      const all = document.querySelector('#dy-sent-mount .dy-stance .dy-chip[data-stance=""]');
      if (all) all.click();
    });
    await sleep(600);

    /* ---------- ⑥ 豆包×主力共振面板（learn / resonance）---------- */
    console.log("\n== ⑥ 豆包×主力共振面板（learn / resonance）==");
    await page.evaluate(() => selectNav("learn", "dy_reso"));
    await sleep(1500);

    const resoMounted = await waitFor(
      page,
      () => !!document.querySelector('#p-dy_reso .integ-hd .dy-src'),
      20000,
      "共振面板挂载"
    );
    rec("共振面板已挂载 #p-dy_reso .integ-hd .dy-src", resoMounted);

    const resoTitle = await page.$eval(
      '#p-dy_reso .integ-hd .dy-src',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("共振面板标题徽标 = 豆包×主力共振", resoTitle === "豆包×主力共振", "实际: " + resoTitle);

    const resoRows = await waitFor(
      page,
      () => document.querySelectorAll('#p-dy_reso #reso-list .reso-row').length >= 1,
      30000,
      "共振标的渲染"
    );
    const resoRowN = await page.evaluate(
      () => document.querySelectorAll('#p-dy_reso #reso-list .reso-row').length
    );
    rec("共振标的已渲染（≥1 行）", resoRows, "行数: " + resoRowN);

    const resoMeta = await page.$eval(
      '#p-dy_reso #reso-meta',
      (el) => el.textContent.trim()
    ).catch(() => "");
    rec("共振面板含统计（点名→共振）", /共振/.test(resoMeta) && /点名/.test(resoMeta), "实际: " + resoMeta);

    const resoStrong = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('#p-dy_reso #reso-list .reso-row'));
      return rows.some((r) => r.textContent.includes('强共振'));
    });
    rec("共振列表含「强共振」高信服标的", resoStrong);

    await page.screenshot({ path: OUT + "/shot_resonance.png", fullPage: false });
    console.log("    [截图] shot_resonance.png");


    rec("零 pageerror（JS 运行时错误）", pageErrors.length === 0, pageErrors.join(" | "));
    rec("零 console.error", consoleErrors.length === 0, consoleErrors.slice(0, 5).join(" | "));
  } catch (e) {
    rec("脚本执行异常", false, String(e && e.stack ? e.stack : e));
  } finally {
    await browser.close();
  }

  const passed = results.filter((r) => r.pass).length;
  const total = results.length;
  console.log("\n==================== 回归结果 ====================");
  console.log("通过 " + passed + " / " + total);
  if (pageErrors.length) {
    console.log("\n[pageerror 明细]");
    pageErrors.forEach((e) => console.log("  - " + e));
  }
  if (consoleErrors.length) {
    console.log("\n[console.error 明细]");
    consoleErrors.slice(0, 10).forEach((e) => console.log("  - " + e));
  }
  process.exit(passed === total ? 0 : 1);
})();
