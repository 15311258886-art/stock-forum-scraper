/* 2026-09-28 版面实测：板块资金流排行「代表股」列改成 8 只（行业龙头口径）后，
   真实浏览器里到底排几行、有没有横向溢出、右侧还剩多少空档。
   用真实 Chromium 打开真实服务，走真实导航（增强功能 → 资金流向 → 板块资金流/行业板块），
   量测每行 .bf-mem 的 offsetTop 分组数（=排了几行），并截图。
   用法：node verify_bf_members_layout.cjs [viewportWidth]   默认 1600 */
module.paths.push("C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules");
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");
const CHROME = "C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe";
const URL = process.env.VURL || "http://127.0.0.1:8765";
const W = parseInt(process.argv[2] || "1600", 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const fails = [];
  const ok = (c, msg) => { if (!c) fails.push("FAIL  " + msg); else console.log("PASS  " + msg); };

  const b = await puppeteer.launch({
    executablePath: CHROME, headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
      "--ignore-certificate-errors", "--proxy-server=direct://", "--proxy-bypass-list=*"],
  });
  const p = await b.newPage();
  await p.setViewport({ width: W, height: 1000, deviceScaleFactor: 2 });
  const cerrs = [];
  p.on("console", (m) => { if (m.type() === "error") cerrs.push(m.text()); });
  p.on("pageerror", (e) => cerrs.push("pageerror: " + e.message));

  await p.goto(URL + "/", { waitUntil: "networkidle2", timeout: 60000 });
  await sleep(1500);

  // 真实导航（按 data 属性精确点，别用文本兜底——兜底会把"点不到"伪装成 PASS）
  // 左栏一级分类 a.nav-item[data-cat="enhance"] → 横条子项 a.sub-tab[data-tab="moneyflow"]
  const nav1 = await p.evaluate(() => {
    const el = document.querySelector('a.nav-item[data-cat="enhance"]');
    if (!el) return "NOT_FOUND";
    el.click(); return "CLICKED enhance";
  });
  console.log("NAV1 =", nav1);
  await sleep(1200);
  const nav2 = await p.evaluate(() => {
    const el = document.querySelector('a.sub-tab[data-tab="moneyflow"]');
    if (!el) return "NOT_FOUND";
    el.click(); return "CLICKED moneyflow";
  });
  console.log("NAV2 =", nav2, "(null/NOT_FOUND 就要警觉)");
  await p.waitForSelector("#p-moneyflow", { timeout: 20000 }).catch(() => {});
  await sleep(2500);

  // 确保停在「板块资金流」+「行业板块」
  await p.evaluate(() => {
    const t = document.querySelector('#p-moneyflow [data-mf-mode="board"]'); if (t) t.click();
  });
  await sleep(600);
  await p.evaluate(() => {
    const t = document.querySelector('#p-moneyflow [data-bf-kind="industry"]'); if (t) t.click();
  });
  await sleep(3000);

  const m = await p.evaluate(() => {
    const tb = document.querySelector("#p-moneyflow table.bf-tb");
    if (!tb) return { error: "找不到 #p-moneyflow table.bf-tb" };
    const wrap = tb.closest(".wt-tw") || tb.parentElement;
    const ths = [...tb.querySelectorAll("thead th")].map((th) => ({
      t: th.textContent.trim(), w: Math.round(th.getBoundingClientRect().width),
    }));
    const trs = [...tb.querySelectorAll("tbody tr")];
    const lineCounts = trs.map((tr) => {
      const mems = [...tr.querySelectorAll("a.bf-mem")];
      if (!mems.length) return 0;
      return new Set(mems.map((a) => Math.round(a.getBoundingClientRect().top / 5))).size;
    });
    const cnts = trs.map((tr) => tr.querySelectorAll("a.bf-mem").length);
    const firstTr = trs[0];
    const memTd = firstTr.querySelector("td.bf-mem-td");
    const tdr = memTd.getBoundingClientRect();
    const mems = [...firstTr.querySelectorAll("a.bf-mem")];
    const maxRight = mems.length ? Math.max(...mems.map((a) => a.getBoundingClientRect().right)) : null;
    return {
      dataRows: trs.length,
      ths,
      memCountFirst: mems.length,
      memCountMin: Math.min(...cnts), memCountMax: Math.max(...cnts),
      maxLinesPerRow: Math.max(...lineCounts),
      rowsWith2PlusLines: lineCounts.filter((n) => n >= 2).length,
      memTdW: Math.round(tdr.width),
      maxLineWidthPx: mems.length ? Math.round(maxRight - Math.min(...mems.map((a) => a.getBoundingClientRect().left))) : null,
      slackPx: maxRight == null ? null : Math.round(tdr.right - maxRight),
      rowHeightPx: Math.round(firstTr.getBoundingClientRect().height),
      // 精确算「差多少」：可用宽 vs 8 只实际占宽（含 gap）
      availPx: Math.round(memTd.clientWidth - 20),
      itemsSumPx: mems.length ? Math.round(mems.reduce((s, a) => s + a.getBoundingClientRect().width, 0)) : null,
      gapPx: mems.length > 1 ? Math.round(mems[1].getBoundingClientRect().left - mems[0].getBoundingClientRect().right) : null,
      tableW: Math.round(tb.getBoundingClientRect().width),
      wrapOverflowX: wrap.scrollWidth > wrap.clientWidth + 1,
      wrapScrollW: wrap.scrollWidth, wrapClientW: wrap.clientWidth,
      firstRowMemText: mems.map((a) => a.textContent.trim()),
    };
  });
  console.log("\n=== 量测 @viewport " + W + "px ===\n" + JSON.stringify(m, null, 1) + "\n");

  if (m.error) { fails.push("FAIL  " + m.error); }
  else {
    ok(m.dataRows > 0, `数据行渲染出来了（${m.dataRows} 行）`);
    ok(m.memCountMax <= 12, `每行代表股 ≤ 12 只（最大 ${m.memCountMax}）`);
    ok(m.memCountFirst === 12, `首行 12 只（实际 ${m.memCountFirst}）`);
    ok(m.maxLinesPerRow === 1, `代表股一行排下、没有换行（最多 ${m.maxLinesPerRow} 行；换行行数 ${m.rowsWith2PlusLines}）`);
    ok(!m.wrapOverflowX, `表格没有横向溢出（scrollW=${m.wrapScrollW} clientW=${m.wrapClientW}）`);
    ok(m.slackPx != null && m.slackPx >= 0, `代表股没超出单元格（右侧余量 ${m.slackPx}px）`);
    ok(cerrs.length === 0, `控制台零报错（${cerrs.length}）`);
  }

  // 截图证据：整页 + 仅排行表
  const shotAll = path.join(__dirname, `_bf_layout_${W}.png`);
  await p.screenshot({ path: shotAll });
  const el = await p.$("#p-moneyflow");
  if (el) {
    const shotPanel = path.join(__dirname, `_bf_layout_panel_${W}.png`);
    await el.screenshot({ path: shotPanel });
    console.log("截图: " + shotAll + "\n截图: " + shotPanel);
  } else console.log("截图: " + shotAll);

  // 高清证据：只截「排行表」前若干行（2x），便于肉眼核对 12 只龙头是否一行排下
  const tw = await p.$("#p-moneyflow .wt-tw");
  if (tw) {
    // 面板内部是独立滚动容器，scrollIntoView+clip 会取错原点；直接对元素截图最稳。
    // 临时限高，只留前 ~14 行（够看 12 只龙头是否一行排下）。
    await p.evaluate(() => {
      const el = document.querySelector("#p-moneyflow .wt-tw");
      if (el) { el.dataset._oh = el.style.maxHeight || ""; el.dataset._oo = el.style.overflow || "";
                el.style.maxHeight = "420px"; el.style.overflow = "hidden"; }
    });
    await sleep(300);
    const shotZoom = path.join(__dirname, `_bf_layout_zoom_${W}.png`);
    await tw.screenshot({ path: shotZoom });
    console.log("高清截图: " + shotZoom);
    await p.evaluate(() => {
      const el = document.querySelector("#p-moneyflow .wt-tw");
      if (el) { el.style.maxHeight = el.dataset._oh || ""; el.style.overflow = el.dataset._oo || ""; }
    });
  }

  if (cerrs.length) console.log("控制台报错样本:", cerrs.slice(0, 5));

  await b.close();
  console.log(fails.length ? "\n" + fails.join("\n") + `\n\n合计：${fails.length} FAIL` : "\n全部 PASS");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("FATAL", e.message); process.exit(1); });
