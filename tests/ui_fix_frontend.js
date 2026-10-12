/**
 * 本轮前端缺陷修复的专项回归（真实 Chromium + 真实布局矩形）
 *
 * 覆盖 team-lead 清单里的 7 组改动：
 *   1) P1-2 加载失败必须可见：abort / 500 HTML / 500 JSON 三种失败下都要出现可关闭的错误条，
 *          且 #statusHint 也要带「失败」字样（原先整页静默空白）。
 *   2) P2-1 属性逃逸：用带 " 的载荷注入 name/channel/symbol，断言属性上下文被 escAttr 兜住、
 *          没有 onmouseover/data-evil 之类被注入、window.__xss 未被触发；javascript: 不被写进 href。
 *   3) P2-8 抽屉把手：拖到右下极限 / 左上极限 / 改变窗口尺寸后，
 *          left+width<=innerWidth、top+height<=innerHeight，右下角拉伸手柄始终在视口内。
 *   4) P2-9 / P3-6 / P3-7 缺字段与类型不符：DOM 里不得出现 undefined / NaN / Invalid Date，
 *          title 属性上也不得出现；counts 缺失要给出可读提示而非白屏；weight 缺失不渲染 NaN。
 *   5) P2-7 数据源面板：已接入=绿、已连通但无条目=中性、待激活=黄、未启用=灰、抓取失败=红；
 *          渲染 checked_at；enabled===false 时出现「已关闭」小标；「N 条」改为「抓取 N 条」并带 title。
 *   6) P2-5 人员列表文案如实描述截断。
 *
 * 全程只读：只发 GET；写接口按钮一个都不点。
 * 用法： NODE_PATH=... node tests/ui_fix_frontend.js [端口]      默认 8765
 * 截图： tools/shots_frontend/*.png      报告：tools/_qa_fix_frontend_assert.txt
 */
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8765";
const URL = `http://127.0.0.1:${PORT}/index.html`;
const EXE = "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe";
const ROOT = path.join(__dirname, "..");
const SHOT = path.join(ROOT, "tools", "shots_frontend");
const REPORT = path.join(ROOT, "tools", "_qa_fix_frontend_assert.txt");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, extra) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${extra !== undefined ? "  —— " + extra : ""}`);
const info = (n, extra) => out.push(`INFO  ${n}${extra !== undefined ? "  —— " + extra : ""}`);
const skip = (n, extra) => out.push(`SKIP  ${n}${extra !== undefined ? "  —— " + extra : ""}`);

/** 服务可能正在被另一条线重启：goto 遇到连接拒绝时重试几次 */
async function gotoRetry(page, url, tries = 6) {
  for (let i = 0; i < tries; i++) {
    try {
      return await page.goto(url, { waitUntil: "networkidle2", timeout: 40000 });
    } catch (e) {
      if (i === tries - 1) throw e;
      info("[preflight] goto 失败，等 3s 重试", `${e.message.slice(0, 80)} (第 ${i + 1} 次)`);
      await wait(3000);
    }
  }
}

const TABS = ["summary", "stocks", "operating", "players", "consensus", "persons",
  "sentiment", "topics", "overseas_sentiment", "overseas_hotspots"];

/** 统一的新页面：收集 pageerror / console error */
async function newPage(browser, w = 1440, h = 1000) {
  const p = await browser.newPage();
  await p.setViewport({ width: w, height: h });
  const errs = [];
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  p.__errs = errs;
  return p;
}

/** 在页面内扫全站脏字符 */
const SCAN = () => {
  const body = document.body;
  const txt = body.textContent || "";
  const countRe = (re) => (txt.match(re) || []).length;
  const titleBad = [...document.querySelectorAll("[title]")].filter((e) => /undefined|NaN|Invalid Date|null/.test(e.getAttribute("title") || ""));
  const undefCtx = [];
  for (let i = txt.indexOf("undefined"); i >= 0 && undefCtx.length < 6; i = txt.indexOf("undefined", i + 1)) {
    undefCtx.push(txt.slice(Math.max(0, i - 34), i + 12).replace(/\s+/g, " "));
  }
  return {
    undefined: countRe(/undefined/g),
    NaN: countRe(/NaN/g),
    invalidDate: countRe(/Invalid Date/g),
    undefCtx,
    titleBad: titleBad.length,
    titleBadSample: titleBad.slice(0, 3).map((e) => `${e.tagName}.${e.className}@${e.getAttribute("title")}`),
    barVisible: (() => {
      const b = document.getElementById("apiErrorBar");
      if (!b) return false;
      const cs = getComputedStyle(b);
      return cs.display !== "none" && b.getBoundingClientRect().height > 0 && (b.textContent || "").trim().length > 0;
    })(),
    barText: ((document.getElementById("apiErrorBar") || {}).textContent || "").trim(),
    hint: ((document.querySelector("#statusHint") || {}).textContent || "").trim(),
  };
};

(async () => {
  if (!fs.existsSync(SHOT)) fs.mkdirSync(SHOT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EXE, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });

  /* ======================================================================
   * 一、真实页面（8765，只读）：纯函数守卫 + 数据源面板 + 全站脏字符
   * ==================================================================== */
  out.push("========== 一、真实页面（只读） ==========");
  STEP = "goto real";
  const page = await newPage(browser);
  await gotoRetry(page, URL);
  await wait(1200);

  /* 1.1 正常页不该常亮错误条 */
  const healthy = await page.evaluate(SCAN);
  ok("[正常页] 无加载失败时错误条隐藏（不能常亮）", !healthy.barVisible, `barVisible=${healthy.barVisible} text="${healthy.barText.slice(0, 60)}"`);
  ok("[正常页] 全站无 undefined / NaN / Invalid Date", healthy.undefined === 0 && healthy.NaN === 0 && healthy.invalidDate === 0,
    `undefined=${healthy.undefined} NaN=${healthy.NaN} invalidDate=${healthy.invalidDate}`);
  ok("[正常页] title 属性上无 undefined / NaN", healthy.titleBad === 0, `${healthy.titleBad} 处 ${healthy.titleBadSample.join(" | ")}`);

  /* 1.2 转义 / 协议守卫（直接调页面内的纯函数） */
  const escRes = await page.evaluate(() => {
    const src = `x" onmouseover="window.__xss=1' <b>&`;
    return {
      hasEscAttr: typeof window.escAttr === "function",
      escAttrOut: window.escAttr ? window.escAttr(src) : null,
      escOut: window.esc ? window.esc(src) : null,
      jsPlaceholder: window.isPlaceholderUrl ? window.isPlaceholderUrl("javascript:alert(1)") : null,
      dataPlaceholder: window.isPlaceholderUrl ? window.isPlaceholderUrl("data:text/html;base64,PHNjcmlwdD4=") : null,
      jsHref: window.srcLink ? window.srcLink("javascript:alert(1)", "点我") : null,
      dataHref: window.srcLink ? window.srcLink("data:text/html,<script>x</script>", "点我") : null,
      realHref: window.srcLink ? window.srcLink("https://www.reuters.com/markets/x", "路透") : null,
      demoHref: window.srcLink ? window.srcLink("https://example.com/a", "占位") : null,
    };
  });
  ok("[P2-1] 已定义 escAttr()", escRes.hasEscAttr);
  ok("[P2-1] escAttr 把 \" 与 ' 一并转义",
    escRes.escAttrOut !== null && !/["']/.test(escRes.escAttrOut) && /&quot;/.test(escRes.escAttrOut) && /&#39;/.test(escRes.escAttrOut),
    `escAttr("x\\" onmouseover=...") = ${JSON.stringify(escRes.escAttrOut)}`);
  ok("[P2-1] 对照：esc() 不转义引号（说明 escAttr 是必要的）",
    escRes.escOut !== null && /"/.test(escRes.escOut), `esc() = ${JSON.stringify(escRes.escOut)}`);
  ok("[P2-1] javascript: / data: 被判定为不可跳转",
    escRes.jsPlaceholder === true && escRes.dataPlaceholder === true,
    `javascript=${escRes.jsPlaceholder} data=${escRes.dataPlaceholder}`);
  ok("[P2-1] javascript: / data: 不会生成 <a href>",
    /^<span/.test(escRes.jsHref || "xx") && /^<span/.test(escRes.dataHref || "xx") && !/href/.test((escRes.jsHref || "") + (escRes.dataHref || "")),
    `js=${(escRes.jsHref || "").slice(0, 50)} | data=${(escRes.dataHref || "").slice(0, 50)}`);
  ok("[P2-1] 真实 https 链接仍可点（保留原有能力）",
    /^<a href="https:\/\/www\.reuters\.com\/markets\/x"/.test(escRes.realHref || ""), (escRes.realHref || "").slice(0, 80));
  ok("[P2-1] 占位域名仍降级为不可点（保留原有行为）",
    /^<span/.test(escRes.demoHref || "xx") && /示例/.test(escRes.demoHref || ""), (escRes.demoHref || "").slice(0, 60));

  /* 1.3 数据源面板（P2-7） */
  await page.click('.tab[data-tab="overseas_sentiment"]');
  await wait(600);
  const srcPanel = await page.evaluate(() => {
    const box = document.querySelector("#ov-sources");
    if (!box) return { missing: true };
    const items = [...box.querySelectorAll(".ov-src-status")].map((e) => ({
      status: e.dataset.status || "",
      cls: e.className,
      badges: [...e.querySelectorAll(".ov-src-st-badge")].map((b) => b.textContent.trim()),
      closed: !!e.querySelector(".chip-closed"),
      countTxt: (e.querySelector(".ov-src-count") || {}).textContent || "",
      countTitle: (e.querySelector(".ov-src-count") || {}).getAttribute ? (e.querySelector(".ov-src-count") || {}).getAttribute("title") || "" : "",
      text: e.textContent.replace(/\s+/g, " ").trim(),
    }));
    const checked = box.querySelector("[data-checked-at]");
    return {
      n: items.length,
      items,
      checkedAt: checked ? checked.getAttribute("data-checked-at") : null,
      checkedTxt: checked ? checked.textContent.trim() : "",
      hint: (box.querySelector(".hint") || {}).textContent || "",
    };
  });
  if (srcPanel.missing || !srcPanel.n) {
    ok("[P2-7] 数据源面板已渲染", false, "未找到 .ov-src-status");
  } else {
    const stCount = {};
    srcPanel.items.forEach((i) => { stCount[i.status] = (stCount[i.status] || 0) + 1; });
    info("[P2-7] 面板状态分布", Object.keys(stCount).map((s) => `${s}×${stCount[s]}`).join(" / "));

    /* 映射规则用「纯函数」验证（不依赖运行中服务此刻恰好有哪些状态），
       再用真实渲染结果做一致性校验。 */
    const mapRes = await page.evaluate(() => {
      const f = window.srcStatusClass;
      if (typeof f !== "function") return { missing: true };
      return {
        cases: {
          "已接入": f("已接入"),
          "已连通但无条目": f("已连通但无条目"),
          "示例模式·未联网": f("示例模式·未联网"),
          "待激活（需 VPN）": f("待激活（需 VPN）"),
          "待激活（需 API Key）": f("待激活（需 API Key）"),
          "未启用": f("未启用"),
          "抓取失败": f("抓取失败"),
          "未知状态XYZ": f("未知状态XYZ"),
          "": f(""),
        },
      };
    });
    const EXPECT = {
      "已接入": "src-on",
      "已连通但无条目": "src-neutral",
      "示例模式·未联网": "src-demo",
      "待激活（需 VPN）": "src-wait",
      "待激活（需 API Key）": "src-wait",
      "未启用": "src-off",
      "抓取失败": "src-err",
    };
    if (mapRes.missing) {
      ok("[P2-7①] 找到 srcStatusClass() 纯函数", false, "未导出");
    } else {
      const wrong = Object.keys(EXPECT).filter((k) => mapRes.cases[k] !== EXPECT[k]);
      ok("[P2-7①] 六类状态映射正确（未接入/待激活/未启用/示例模式/连通无条目/失败）",
        wrong.length === 0, wrong.length ? wrong.map((k) => `${k}→${mapRes.cases[k]}(期望${EXPECT[k]})`).join(" | ") : JSON.stringify(mapRes.cases));
      ok("[P2-7④] 「已连通但无条目」不再落到红色 src-err",
        mapRes.cases["已连通但无条目"] !== "src-err" && mapRes.cases["已连通但无条目"] === "src-neutral",
        `→ ${mapRes.cases["已连通但无条目"]}`);
      ok("[P2-7④] 未知状态也不再默认标红（兜底为中性灰）",
        mapRes.cases["未知状态XYZ"] === "src-off" && mapRes.cases[""] === "src-off",
        `未知→${mapRes.cases["未知状态XYZ"]} 空→${mapRes.cases[""]}`);
    }
    // 真实渲染与状态自洽
    const inconsistent = srcPanel.items.filter((i) => !new RegExp(`\\b${EXPECT[i.status] || "src-off"}\\b`).test(i.cls));
    ok("[P2-7①] 真实渲染中每个源的配色与自身状态一致", inconsistent.length === 0,
      inconsistent.length ? inconsistent.map((i) => `${i.status}→${i.cls}`).join(" | ") : `${srcPanel.items.length} 个源全部一致`);
    const redItems = srcPanel.items.filter((i) => /\bsrc-err\b/.test(i.cls));
    ok("[P2-7①] 红色只用于真失败（除「抓取失败」外没有别的源被标红）",
      redItems.every((i) => i.status.indexOf("抓取失败") >= 0),
      redItems.map((i) => i.status).join(",") || "无红色项");
    if (srcPanel.items.some((i) => i.status.indexOf("已连通但无条目") >= 0)) {
      ok("[P2-7④] 面板里「已连通但无条目」确实按中性渲染",
        srcPanel.items.filter((i) => i.status.indexOf("已连通但无条目") >= 0).every((i) => /\bsrc-neutral\b/.test(i.cls) && !/\bsrc-err\b/.test(i.cls)),
        srcPanel.items.filter((i) => i.status.indexOf("已连通但无条目") >= 0).map((i) => i.cls).join(","));
    } else {
      skip("[P2-7④] 面板里「已连通但无条目」确实按中性渲染", "本次快照无该状态（已被纯函数用例覆盖）");
    }

    ok("[P2-7⑥] 面板标题旁渲染「数据截至：<checked_at>」",
      !!srcPanel.checkedAt && /^\d{4}-\d{2}-\d{2}/.test(srcPanel.checkedAt) && /数据截至/.test(srcPanel.checkedTxt),
      `checked_at=${srcPanel.checkedAt} 文案="${srcPanel.checkedTxt}"`);
    ok("[P2-7⑥] checked_at 不渲染成 undefined", !/undefined/.test(srcPanel.checkedTxt + srcPanel.checkedAt), srcPanel.checkedTxt);
    const noTs = await page.evaluate(() => window.latestCheckedAt && window.latestCheckedAt([{ name: "a", status: "已接入" }, { name: "b" }]));
    ok("[P2-7⑥] checked_at 全缺失时降级为空（调用方会隐藏该行，不渲染 undefined）", noTs === "", `latestCheckedAt(...)=${JSON.stringify(noTs)}`);

    const cnt = srcPanel.items.filter((i) => i.countTxt);
    if (cnt.length) {
      ok("[P2-7③] 条数口径写成「抓取 N 条」并带 title 提示去重前口径",
        cnt.every((i) => /^抓取 \d+ 条$/.test(i.countTxt) && /去重前/.test(i.countTitle) && /入库数可能更少/.test(i.countTitle)),
        `${cnt.length} 个源，样例 ${cnt[0].countTxt} / title="${cnt[0].countTitle}"`);
    } else {
      skip("[P2-7③] 条数口径写成「抓取 N 条」", "本次快照所有源 count 均为 0（缺字段页已独立覆盖）");
    }

    ok("[P2-7⑤] 图例如实描述为「上一次采集」的快照", /上一次采集/.test(srcPanel.hint), srcPanel.hint.slice(0, 60));
    ok("[P2-7⑤] 图例已删除当前实现下不会出现的「示例模式·未联网」说法",
      !/未联网时显示/.test(srcPanel.hint) && !/示例模式·未联网/.test(srcPanel.hint), srcPanel.hint.slice(0, 90));
    ok("[P2-7] 面板内无 undefined", !/undefined/.test(srcPanel.items.map((i) => i.text).join(" ") + srcPanel.hint));
  }

  /* 1.4 人员列表文案（P2-5） */
  await page.click('.tab[data-tab="persons"]');
  await wait(500);
  const personsPanel = await page.evaluate(() => {
    const p = document.querySelector("#p-persons");
    const rows = p.querySelectorAll("tbody tr").length;
    const hint = (p.querySelector(".hint") || {}).textContent || "";
    const undefCells = [...p.querySelectorAll("tbody td")].filter((td) => /undefined/.test(td.textContent)).length;
    return { rows, hint, undefCells };
  });
  ok("[P2-5] 人员列表文案不再声称「不遗漏任何一个名称」", !/不遗漏任何一个名称/.test(personsPanel.hint), personsPanel.hint.slice(0, 120));
  ok("[P2-5] 人员列表文案如实说明截断（列出前 N 位 + 接口共返回 M 位）",
    /列出前\s*\d+\s*位/.test(personsPanel.hint) && /共[^，。；]{0,10}?\d+\s*位/.test(personsPanel.hint),
    personsPanel.hint.replace(/\s+/g, " ").slice(0, 140));
  ok("[P2-5] 人员表单元格无 undefined", personsPanel.undefCells === 0, `行数=${personsPanel.rows} 脏单元格=${personsPanel.undefCells}`);

  /* 1.5 四个主题下再扫一遍脏字符（防止主题分支渲染出 NaN） */
  for (const t of ["dark-dashboard", "minimal-report", "card-warm", "light-tech"]) {
    await page.evaluate((id) => { document.querySelector("#theme-link").href = `static/themes/${id}.css`; }, t);
    await wait(300);
    const s = await page.evaluate(SCAN);
    ok(`[主题 ${t}] 无 undefined / NaN / title 脏值`,
      s.undefined === 0 && s.NaN === 0 && s.titleBad === 0,
      `undefined=${s.undefined} NaN=${s.NaN} titleBad=${s.titleBad}`);
  }
  for (const t of TABS) {
    await page.click(`.tab[data-tab="${t}"]`);
    await wait(200);
  }
  ok("[正常页] 真实数据流程无 JS 运行时错误", page.__errs.length === 0, page.__errs.slice(0, 3).join(" ; ") || "无");
  await page.screenshot({ path: path.join(SHOT, "real-page.png") });

  /* ======================================================================
   * 二、P1-2 三种失败下错误条必须可见
   * ==================================================================== */
  out.push("");
  out.push("========== 二、P1-2 加载失败可见化（abort / 500 HTML / 500 JSON） ==========");
  const failCases = [
    { name: "网络层 abort", handler: (req) => req.abort() },
    { name: "500 HTML 错误页", handler: (req) => req.respond({ status: 500, contentType: "text/html", body: "<html><body>Internal Server Error</body></html>" }) },
    { name: "500 JSON error", handler: (req) => req.respond({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "boom" }) }) },
  ];
  for (const fc of failCases) {
    STEP = "fail-case " + fc.name;
    const fp = await newPage(browser);
    await fp.setRequestInterception(true);
    fp.on("request", (req) => (/\/api\//.test(req.url()) ? fc.handler(req) : req.continue()));
    await gotoRetry(fp, URL);
    await wait(1200);
    const st = await fp.evaluate(SCAN);
    ok(`[P1-2][${fc.name}] 出现可见错误条`, st.barVisible, `barVisible=${st.barVisible} bar="${st.barText.slice(0, 70)}"`);
    ok(`[P1-2][${fc.name}] 错误条文案含「部分数据加载失败」`, /部分数据加载失败/.test(st.barText), st.barText.slice(0, 90));
    ok(`[P1-2][${fc.name}] statusHint 也变成可读报错（不再是默认引导语）`, /失败/.test(st.hint), st.hint.slice(0, 70));
    ok(`[P1-2][${fc.name}] 报错信息带状态码/原因`, /HTTP 500|Failed to fetch|net::ERR|abort|Intern/i.test(st.barText), st.barText.slice(0, 110));
    const closeable = await fp.evaluate(() => {
      const b = document.getElementById("apiErrorClose");
      if (!b) return { none: true };
      return { exists: true };
    });
    // 先留证（错误条还在），再验证可关闭
    await fp.screenshot({ path: path.join(SHOT, "err-" + fc.name.replace(/[^0-9A-Za-z\u4e00-\u9fa5]/g, "_") + ".png") });
    if (closeable.exists) {
      await fp.click("#apiErrorClose");
      await wait(200);
      const after = await fp.evaluate(() => {
        const b = document.getElementById("apiErrorBar");
        return { visible: b ? getComputedStyle(b).display !== "none" : false };
      });
      ok(`[P1-2][${fc.name}] 错误条可关闭`, !after.visible);
    } else {
      ok(`[P1-2][${fc.name}] 错误条可关闭`, false, "未找到 #apiErrorClose");
    }
    ok(`[P1-2][${fc.name}] 无未捕获的 JS 异常`, fp.__errs.filter((x) => /pageerror/.test(x)).length === 0,
      fp.__errs.slice(0, 2).join(" ; ") || "无");
    await fp.close();
  }

  /* 2b. counts 缺失（HTTP 200 但字段不全）也要有可读提示 */
  {
    STEP = "missing counts";
    const fp = await newPage(browser);
    await fp.setRequestInterception(true);
    fp.on("request", (req) => {
      if (!/\/api\//.test(req.url())) return req.continue();
      if (/\/api\/summary/.test(req.url())) {
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ login_status: [], last_ingest: "x", sites: [] }) });
      }
      return req.continue();
    });
    await gotoRetry(fp, URL);
    await wait(1000);
    const st = await fp.evaluate(SCAN);
    const cards = await fp.evaluate(() => ({
      txt: (document.querySelector("#cards") || {}).textContent || "",
      n: [...document.querySelectorAll("#cards .n")].map((e) => e.textContent.trim()),
    }));
    ok("[P2-9][缺 counts] 给出可读提示（而非静默白屏）", /失败/.test(st.hint) || st.barVisible, `hint="${st.hint.slice(0, 60)}" bar=${st.barVisible}`);
    ok("[P2-9][缺 counts] 计数降级为「—」且不渲染 undefined",
      cards.n.length === 9 && cards.n.every((x) => x === "—") && !/undefined/.test(cards.txt),
      cards.n.join(","));
    ok("[P2-9][缺 counts] 无未捕获 JS 异常", fp.__errs.filter((x) => /pageerror/.test(x)).length === 0, fp.__errs.slice(0, 2).join(" ; ") || "无");
    await fp.close();
  }

  /* ======================================================================
   * 三、P2-1 属性逃逸（注入带 " 的载荷）
   * ==================================================================== */
  out.push("");
  out.push("========== 三、P2-1 属性逃逸注入测试 ==========");
  STEP = "xss payload";
  {
    const NAME = `甲" data-evil="1`;
    const CH = `A" onmouseover="window.__xssCh=1`;
    const NAME2 = `乙" onmouseover="window.__xssPerson=1`;
    const SYM = `600519" onmouseover="window.__xssSym=1`;
    const fp = await newPage(browser);
    await fp.setRequestInterception(true);
    fp.on("request", (req) => {
      const u = req.url();
      if (!/\/api\//.test(u)) return req.continue();
      const json = (o) => req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify(o) });
      if (/\/api\/players/.test(u)) return json([{ name: NAME, channel: CH, tier: "A", source_url: "javascript:alert(1)", rank_position: 1 }]);
      if (/\/api\/persons/.test(u)) return json([{ name: NAME2, platform: "taoguba", mention_count: 3, role: "mention" }]);
      if (/\/api\/stocks/.test(u)) return json([{ symbol: SYM, symbol_name: "X", mentions: 1 }]);
      if (/\/api\/consensus/.test(u)) return json([{ symbol: SYM, symbol_name: "X", holders: 2, sources: ["javascript:alert(2)"] }]);
      return json([]);
    });
    await gotoRetry(fp, URL);
    await wait(1200);

    const res = await fp.evaluate(() => {
      const row = document.querySelector("#p-players tbody tr[data-name]");
      const pRow = document.querySelector("#p-persons tbody tr[data-name]");
      const card = document.querySelector("#p-stocks .stk-card[data-sym]");
      return {
        playerAttrs: row ? row.getAttributeNames().sort() : null,
        playerName: row ? row.getAttribute("data-name") : null,
        playerCh: row ? row.getAttribute("data-ch") : null,
        playerSuffix: (() => { const b = row && row.querySelector("td:nth-child(4) .badge"); return b ? b.className : null; })(),
        playerSuffixHasOn: (() => { const b = row && row.querySelector("td:nth-child(4) .badge"); return b ? b.hasAttribute("onmouseover") : null; })(),
        personAttrs: pRow ? pRow.getAttributeNames().sort() : null,
        personName: pRow ? pRow.getAttribute("data-name") : null,
        stockSym: card ? card.getAttribute("data-sym") : null,
        stockAttrs: card ? card.getAttributeNames().sort() : null,
        hovering: document.querySelectorAll("[onmouseover]").length,
        evilAttr: document.querySelectorAll("[data-evil]").length,
        jsHref: document.querySelectorAll('a[href^="javascript:"], a[href^="data:"]').length,
        xss: [window.__xssCh, window.__xssPerson, window.__xssSym].filter((x) => x !== undefined).length,
      };
    });

    ok("[P2-1] 选手行 data-name 原样回读（引号没有被截断成半个值）", res.playerName === NAME,
      `得到 ${JSON.stringify(res.playerName)} 期望 ${JSON.stringify(NAME)}`);
    ok("[P2-1] 选手行 data-ch 原样回读", res.playerCh === CH, `得到 ${JSON.stringify(res.playerCh)}`);
    ok("[P2-1] 选手行属性集合只有 data-name/data-ch/data-tier（未注入 data-evil）",
      JSON.stringify(res.playerAttrs) === JSON.stringify(["data-ch", "data-name", "data-tier"]), JSON.stringify(res.playerAttrs));
    ok("[P2-1] 页面上不存在任何 [onmouseover] 属性", res.hovering === 0, `节点数=${res.hovering}`);
    ok("[P2-1] 页面上不存在被注入的 [data-evil]", res.evilAttr === 0, `节点数=${res.evilAttr}`);
    ok("[P2-1] 选手渠道徽章未被引号截断成独立属性（无 onmouseover，class 仍含 ch-A）",
      !!res.playerSuffix && /^badge\s/.test(res.playerSuffix) && /ch-A/.test(res.playerSuffix) && res.playerSuffixHasOn === false,
      `className=${JSON.stringify(res.playerSuffix)} hasOnmouseover=${res.playerSuffixHasOn}`);
    ok("[P2-1] 人物行 data-name 原样回读且属性未被追加（只有 data-name + 源码里的内联 style）", res.personName === NAME2 &&
      JSON.stringify(res.personAttrs) === JSON.stringify(["data-name", "style"]),
      `${JSON.stringify(res.personName)} / ${JSON.stringify(res.personAttrs)}`);
    ok("[P2-1] 股票卡 data-sym 原样回读且属性未被追加", res.stockSym === SYM &&
      JSON.stringify(res.stockAttrs) === JSON.stringify(["class", "data-sym"]),
      `${JSON.stringify(res.stockSym)} / ${JSON.stringify(res.stockAttrs)}`);
    ok("[P2-1] javascript:/data: 没有被写进任何 <a href>", res.jsHref === 0, `命中=${res.jsHref}`);
    ok("[P2-1] 注入的 onmouseover 未被触发", res.xss === 0, `被触发=${res.xss}`);

    // 真鼠标悬停一遍，确认即使有残留 on* 也不会被触发
    await fp.click('.tab[data-tab="players"]'); await wait(250);
    const rowBox = await fp.$eval("#p-players tbody tr[data-name]", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + 40, y: r.top + r.height / 2 }; });
    await fp.mouse.move(rowBox.x, rowBox.y);
    await wait(200);
    await fp.click('.tab[data-tab="persons"]'); await wait(250);
    const pBox = await fp.$eval("#p-persons tbody tr[data-name]", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + 40, y: r.top + r.height / 2 }; });
    await fp.mouse.move(pBox.x, pBox.y);
    await wait(250);
    const afterHover = await fp.evaluate(() => [window.__xssCh, window.__xssPerson, window.__xssSym].filter((x) => x !== undefined).length);
    ok("[P2-1] 真实鼠标悬停后仍未触发任何注入脚本", afterHover === 0, `被触发=${afterHover}`);
    ok("[P2-1] 注入场景无未捕获 JS 异常", fp.__errs.filter((x) => /pageerror/.test(x)).length === 0, fp.__errs.slice(0, 2).join(" ; ") || "无");
    await fp.screenshot({ path: path.join(SHOT, "xss-injection.png") });
    await fp.close();
  }

  /* ======================================================================
   * 四、P2-8 抽屉把手永远在视口内
   * ==================================================================== */
  out.push("");
  out.push("========== 四、P2-8 抽屉把手可视性 ==========");
  STEP = "drawer clamp";
  {
    const dp = await newPage(browser);
    await gotoRetry(dp, URL);
    await wait(1000);
    await dp.click('.tab[data-tab="players"]'); await wait(400);
    await dp.click("#p-players tbody tr[data-name] td:nth-child(2)"); await wait(900);

    const geom = () => dp.evaluate(() => {
      const d = document.querySelector("#drawer");
      const r = d.getBoundingClientRect();
      const rz = document.querySelector("#drawerResize").getBoundingClientRect();
      const hd = document.querySelector("#drawerHead").getBoundingClientRect();
      return {
        shown: d.classList.contains("show"),
        vw: innerWidth, vh: innerHeight,
        l: Math.round(r.left), t: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
        r: Math.round(r.right), b: Math.round(r.bottom),
        rz: { l: Math.round(rz.left), t: Math.round(rz.top), r: Math.round(rz.right), b: Math.round(rz.bottom), w: Math.round(rz.width) },
        headTop: Math.round(hd.top),
        fitsX: r.left + r.width <= innerWidth + 1,
        fitsY: r.top + r.height <= innerHeight + 1,
        handleIn: rz.left >= -1 && rz.top >= -1 && rz.right <= innerWidth + 1 && rz.bottom <= innerHeight + 1 && rz.width > 0,
      };
    });

    const dragHead = async (dx, dy) => {
      const box = await dp.$eval("#drawerHead", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + 100, y: r.top + r.height / 2 }; });
      await dp.mouse.move(box.x, box.y);
      await dp.mouse.down();
      await dp.mouse.move(box.x + dx, box.y + dy, { steps: 12 });
      await dp.mouse.up();
      await wait(260);
    };

    let g = await geom();
    ok("[P2-8] 抽屉已打开且初始就在视口内（right/bottom 不越界）", g.shown && g.fitsX && g.fitsY && g.handleIn, JSON.stringify({ l: g.l, t: g.t, w: g.w, h: g.h, r: g.r, b: g.b }));

    // 拖到右下极限（旧实现会得到 left=1280 + width=802 → 右缘 2082 > 1440）
    await dragHead(2400, 1600);
    g = await geom();
    ok("[P2-8] 拖到右下极限后 right <= innerWidth", g.fitsX, `left=${g.l} width=${g.w} right=${g.r} vw=${g.vw}`);
    ok("[P2-8] 拖到右下极限后 bottom <= innerHeight", g.fitsY, `top=${g.t} height=${g.h} bottom=${g.b} vh=${g.vh}`);
    ok("[P2-8] 拖到右下极限后右下角拉伸手柄完整在视口内", g.handleIn, JSON.stringify(g.rz) + ` vw=${g.vw} vh=${g.vh}`);
    ok("[P2-8] 拖到右下极限后标题栏可见可拖", g.headTop >= -1, `headTop=${g.headTop}`);
    await dp.screenshot({ path: path.join(SHOT, "drawer-bottom-right-extreme.png") });

    // 拖到左上极限
    await dragHead(-2600, -1600);
    g = await geom();
    ok("[P2-8] 拖到左上极限后 right/bottom 仍不越界且手柄可见", g.fitsX && g.fitsY && g.handleIn,
      `@(${g.l},${g.t}) ${g.w}x${g.h} rz=${JSON.stringify(g.rz)}`);
    ok("[P2-8] 拖到左上极限后标题栏仍在视口内（可再次拖回）", g.headTop >= -1, `headTop=${g.headTop}`);

    // 窗口缩小：resize 后必须重新钳制
    await dp.setViewport({ width: 900, height: 620 });
    await wait(500);
    g = await geom();
    ok("[P2-8] 窗口缩到 900x620 后 right/bottom 不越界且手柄可见", g.fitsX && g.fitsY && g.handleIn,
      `@(${g.l},${g.t}) ${g.w}x${g.h} right=${g.r} bottom=${g.b} vw=${g.vw} vh=${g.vh} rz=${JSON.stringify(g.rz)}`);
    await dp.screenshot({ path: path.join(SHOT, "drawer-small-viewport.png") });

    // 恢复视口 + 拉伸到超界
    await dp.setViewport({ width: 1440, height: 1000 });
    await wait(500);
    const rz = await dp.$eval("#drawerResize", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await dp.mouse.move(rz.x, rz.y);
    await dp.mouse.down();
    await dp.mouse.move(2200, 1800, { steps: 12 });
    await dp.mouse.up();
    await wait(300);
    g = await geom();
    ok("[P2-8] 把手向右下拖出视口后仍被钳回视口内", g.fitsX && g.fitsY && g.handleIn,
      `@(${g.l},${g.t}) ${g.w}x${g.h} right=${g.r} bottom=${g.b} rz=${JSON.stringify(g.rz)}`);

    ok("[P2-8] 抽屉场景无未捕获 JS 异常", dp.__errs.filter((x) => /pageerror/.test(x)).length === 0, dp.__errs.slice(0, 2).join(" ; ") || "无");
    await dp.close();
  }

  /* ======================================================================
   * 五、P2-9 / P3-6 / P3-7 缺字段 / 类型不符批量体检
   * ==================================================================== */
  out.push("");
  out.push("========== 五、缺字段 / 类型不符（拦截构造，仅测试页生效） ==========");
  STEP = "sparse payload";
  {
    const sp = await newPage(browser);
    await sp.setRequestInterception(true);
    sp.on("request", (req) => {
      const u = req.url();
      if (!/\/api\//.test(u)) return req.continue();
      const json = (o) => req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify(o) });
      if (/\/api\/summary/.test(u)) return json({ counts: { posts: 1, trades: 2 } });
      if (/\/api\/posts/.test(u)) return json([{ id: 1 }]);
      if (/\/api\/stocks/.test(u)) return json([{ symbol: "600519" }]);
      if (/\/api\/operating/.test(u)) return json([{ id: 1, author: "甲" }]);
      if (/\/api\/players/.test(u)) return json([{ name: "甲" }]);
      if (/\/api\/consensus/.test(u)) return json([{ symbol: "600519" }]);
      if (/\/api\/persons/.test(u)) return json([{ name: "乙" }]);
      if (/\/api\/sentiment/.test(u)) return json({ map: [{ symbol: "600519" }] });
      if (/\/api\/topics/.test(u)) return json([{ topic: "无权重话题" }]);
      if (/\/api\/overseas_sentiment/.test(u)) return json({ total: 3, region_dist: { US: {} }, source_mix: [{}], symbols: [{}], news: [{}], daily: [] });
      if (/\/api\/overseas_hotspots/.test(u)) return json([{ title: "缺字段热点" }]);
      if (/\/api\/overseas_sources/.test(u)) return json({
        sources: [
          { name: "S-连通无条目", status: "已连通但无条目", region: "GLOBAL", count: 0, checked_at: "2026-09-22 21:53:46" },
          { name: "S-待激活已关闭", status: "待激活（需 VPN）", region: "US", enabled: false, count: 5, requires_vpn: 1, checked_at: "2026-09-22 21:53:46" },
        ],
      });
      return json([]);
    });
    await gotoRetry(sp, URL);
    await wait(1400);

    const scan = await sp.evaluate(SCAN);
    ok("[P2-9] 大面积缺字段时全站无 undefined / NaN / Invalid Date",
      scan.undefined === 0 && scan.NaN === 0 && scan.invalidDate === 0,
      `undefined=${scan.undefined} NaN=${scan.NaN} invalidDate=${scan.invalidDate} 上下文=${JSON.stringify(scan.undefCtx)}`);
    ok("[P2-9] title 属性上无 undefined / NaN", scan.titleBad === 0, `${scan.titleBad} 处 ${scan.titleBadSample.join(" | ")}`);
    ok("[P2-9] 空降级不抛错（无未捕获 JS 异常）", sp.__errs.filter((x) => /pageerror/.test(x)).length === 0,
      sp.__errs.slice(0, 3).join(" ; ") || "无");

    // 逐面板细看
    const detail = await sp.evaluate(() => {
      const g = (sel) => (document.querySelector(sel) || {}).textContent || "";
      return {
        cardsN: [...document.querySelectorAll("#cards .n")].map((e) => e.textContent.trim()),
        topics: g("#p-topics"),
        topicsRows: document.querySelectorAll("#p-topics .box").length,
        topicsEmpty: document.querySelectorAll("#p-topics .empty").length,
        personsCells: [...document.querySelectorAll("#p-persons tbody td")].map((e) => e.textContent.trim()),
        consensus: g("#p-consensus"),
        sentiment: g("#p-sentiment"),
        operating: g("#p-operating"),
        hotspots: g("#p-overseas_hotspots"),
        hsNum: [...document.querySelectorAll("#p-overseas_hotspots .ov-hs-dir b, #p-overseas_hotspots .ov-hs-heat")].map((e) => e.textContent.trim()),
        sourcesCls: [...document.querySelectorAll("#ov-sources .ov-src-status")].map((e) => ({ st: e.dataset.status, cls: e.className, closed: !!e.querySelector(".chip-closed"), cnt: (e.querySelector(".ov-src-count") || {}).textContent || "" })),
        sourcesChecked: !!document.querySelector("#ov-sources [data-checked-at]"),
      };
    });
    ok("[P2-9] 汇总卡：payload 给了值的显示值、缺的显示「—」，且无 undefined",
      detail.cardsN.length === 9 &&
      detail.cardsN[1] === "1" && detail.cardsN[2] === "2" &&
      detail.cardsN.filter((x) => x === "—").length === 7 &&
      !detail.cardsN.some((x) => /undefined/.test(x)),
      detail.cardsN.join(","));
    ok("[P3-6] 缺 weight 的热点被跳过，面板不出现 NaN",
      !/NaN/.test(detail.topics) && detail.topicsRows === 0 && detail.topicsEmpty > 0,
      `行数=${detail.topicsRows} 空态=${detail.topicsEmpty} 文本="${detail.topics.slice(0, 60)}"`);
    ok("[P2-9] 人物表缺 mention_count 显示「—」而非 undefined",
      detail.personsCells.includes("—") && !detail.personsCells.some((x) => /undefined/.test(x)),
      detail.personsCells.join(" | ").slice(0, 80));
    ok("[P2-9] 共同持仓缺 holders/weighted/heat 显示「—」",
      !/undefined/.test(detail.consensus) && /—/.test(detail.consensus), detail.consensus.replace(/\s+/g, " ").slice(0, 90));
    ok("[P2-9] 舆论情绪缺 distribution/name_total 不抛错且有内容",
      !/undefined/.test(detail.sentiment) && detail.sentiment.trim().length > 0, detail.sentiment.replace(/\s+/g, " ").slice(0, 90));
    ok("[P2-9] 实盘操作缺 price/quantity 显示 -", !/undefined/.test(detail.operating), detail.operating.replace(/\s+/g, " ").slice(0, 90));
    ok("[P2-9] 海外热点缺放大系数/置信/热度显示「—」",
      !/undefined/.test(detail.hotspots) && detail.hsNum.some((x) => /—/.test(x)), detail.hsNum.join(" | "));

    const neutral = detail.sourcesCls.find((s) => s.st.indexOf("已连通但无条目") >= 0);
    ok("[P2-7①/②] 已连通但无条目在缺字段页也判为中性（不是红色）",
      !!neutral && /src-neutral/.test(neutral.cls) && !/src-err/.test(neutral.cls), neutral ? neutral.cls : "未找到");
    const closedSrc = detail.sourcesCls.find((s) => s.closed);
    ok("[P2-7②] enabled===false 时出现「已关闭」小标", !!closedSrc && /src-wait/.test(closedSrc.cls), closedSrc ? closedSrc.cls : "未找到");
    const cntSrc = detail.sourcesCls.find((s) => s.cnt);
    ok("[P2-7③] count=5 渲染为「抓取 5 条」", !!cntSrc && cntSrc.cnt === "抓取 5 条", cntSrc ? cntSrc.cnt : "未找到");
    ok("[P2-7⑥] 缺字段页仍渲染 checked_at", detail.sourcesChecked);
    await sp.screenshot({ path: path.join(SHOT, "sparse-fields.png") });

    // 类型不符：数组/字符串喂给对象接口
    await sp.evaluate(() => { /* noop 保持页面 */ });
    await sp.close();
  }

  {
    STEP = "type mismatch";
    const tp = await newPage(browser);
    await tp.setRequestInterception(true);
    tp.on("request", (req) => {
      const u = req.url();
      if (!/\/api\//.test(u)) return req.continue();
      const json = (o) => req.respond({ status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify(o) });
      if (/\/api\/sentiment/.test(u)) return json([{ symbol: "600519" }]);          // 应为对象，实为数组
      if (/\/api\/summary/.test(u)) return json([]);                              // 应为对象，实为数组
      if (/\/api\/topics/.test(u)) return json({ topic: "应为数组" });             // 应为数组，实为对象
      if (/\/api\/stocks/.test(u)) return json("应为数组");                        // 应为数组，实为字符串
      if (/\/api\/overseas_sentiment/.test(u)) return json([{ total: 1 }]);        // 应为对象，实为数组
      if (/\/api\/overseas_hotspots/.test(u)) return json({ a: 1 });              // 应为数组，实为对象
      return json([]);
    });
    await gotoRetry(tp, URL);
    await wait(1400);
    const pe = tp.__errs.filter((x) => /pageerror/.test(x));
    ok("[P3-7] 类型不符（数组↔对象↔字符串）不产生 pageerror", pe.length === 0, pe.slice(0, 3).join(" ; ") || "无");
    const tm = await tp.evaluate(() => {
      const g = (sel) => (document.querySelector(sel) || {}).textContent || "";
      return {
        sentiment: g("#p-sentiment"), stocks: g("#p-stocks"), topics: g("#p-topics"),
        hs: g("#p-overseas_hotspots"), summary: g("#p-summary"), cards: g("#cards"),
      };
    });
    ok("[P3-7] /api/sentiment 返回数组时面板降级提示而非全空白",
      /格式异常|降级/.test(tm.sentiment) && tm.sentiment.trim().length > 0, tm.sentiment.replace(/\s+/g, " ").slice(0, 70));
    ok("[P2-9] /api/stocks 返回字符串时降级为空态而非抛错", tm.stocks.trim().length > 0, tm.stocks.replace(/\s+/g, " ").slice(0, 50));
    ok("[P2-9] /api/topics 返回对象时降级为空态", tm.topics.trim().length > 0, tm.topics.replace(/\s+/g, " ").slice(0, 50));
    ok("[P2-9] /api/summary 返回数组时计数降级为「—」且不抛错",
      !/undefined/.test(tm.cards) && /—/.test(tm.cards), tm.cards.replace(/\s+/g, " ").slice(0, 70));
    const scan2 = await tp.evaluate(SCAN);
    ok("[P2-9] 类型不符页无 undefined / NaN", scan2.undefined === 0 && scan2.NaN === 0,
      `undefined=${scan2.undefined} NaN=${scan2.NaN}`);
    await tp.screenshot({ path: path.join(SHOT, "type-mismatch.png") });
    await tp.close();
  }

  /* ======================================================================
   * 汇总
   * ==================================================================== */
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  const passes = out.filter((l) => l.startsWith("PASS")).length;
  out.push("");
  out.push(`=== ui_fix_frontend：${passes}/${passes + fails} 通过（FAIL ${fails}） ===`);
  fs.writeFileSync(REPORT, out.join("\n"), "utf8");
  console.log(out.join("\n"));
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch(async (e) => {
  out.push("RUNNER ERROR @ " + STEP + ": " + (e && e.stack || e));
  try { fs.writeFileSync(REPORT, out.join("\n"), "utf8"); } catch (x) { /* ignore */ }
  console.log(out.join("\n"));
  process.exit(2);
});
