/* verify_kline_fullhistory.cjs —— 「K 线看全历史」回归护栏（2026-10-12 进哥要求）
 *
 * 问题：日K/周K/月K 缩放到最小，看不到「上市首日 → 今天」的整体走势。
 * 根因：滚轮缩放的上限被写成「当前已加载根数」，而前端默认只拉 250 根
 *       （日K≈1年、周K≈5年），所以怎么缩都够不到上市首日。
 *
 * 修复：
 *   ① 滚轮/捏合缩到「已加载的全部」且后端还有更早历史 → 自动拉全历史并全览；
 *   ② 工具条新增「全历史」一键按钮（#klineFull）；
 *   ③ 切周期时保持「全览」意图。
 *
 * 断言：
 *   ① 首屏默认仍只拉 250 根、可见 120 根（不能因修全历史把首屏拖慢）；
 *   ② 点「全历史」→ 根数 > 250（拉到全量）、可见 = 全部、offset = 0、range 标「已显示全部」；
 *   ③ 切到周K → 仍保持全览（继承 all）；
 *   ④ 另起一只票：滚轮往下滚到底 → 自动拉全量并全览（无需点按钮）；
 *   ⑤ 控制台零报错。
 *
 * 运行（需后端在 8765）：
 *   NODE_PATH=".../node_modules" node verify_kline_fullhistory.cjs
 * 退出码：0=PASS，2=FAIL，3=无可用浏览器（跳过）。
 */
const fs = require("fs");
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";

function pickBrowser() {
  const cands = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch (e) {} }
  return null;
}

const fails = [];
function ok(cond, msg) {
  if (cond) console.log("PASS " + msg);
  else { console.log("FAIL " + msg); fails.push(msg); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 打开个股并等 K 线视口就绪 */
async function openStockAndWait(page, symbol) {
  await page.evaluate((s) => openStock(s), symbol);
  for (let i = 0; i < 45; i++) {
    await sleep(1000);
    const ready = await page.evaluate((s) => {
      return !!(typeof _kv !== "undefined" && _kv && _kv.symbol === s && _kv.rows && _kv.rows.length);
    }, symbol);
    if (ready) return true;
  }
  return false;
}

const kvState = (page) => page.evaluate(() => {
  const k = (typeof _kv !== "undefined") ? _kv : null;
  const r = document.querySelector("#klineRange");
  return k ? {
    symbol: k.symbol, period: k.period, rows: k.rows.length, count: k.count,
    offset: k.offset, all: !!k.all, hasMore: !!k.hasMore,
    first: k.rows[0].date, last: k.rows[k.rows.length - 1].date,
    range: r ? r.textContent.replace(/\s+/g, " ").trim() : "",
  } : null;
});

/* 等“全览”生效：rows 变多（或已全量）且 count == rows */
async function waitFullView(page, prevRows, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < (timeoutMs || 45000)) {
    await sleep(800);
    const s = await kvState(page);
    if (s && s.all && s.count === s.rows && s.rows > prevRows) return s;
  }
  return await kvState(page);
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("SKIP 无可用浏览器"); process.exit(3); }
  const browser = await puppeteer.launch({
    executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
           "--ignore-certificate-errors", "--window-size=1440,900"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("CONSOLE " + m.text()); });

  try {
    await page.goto(URL, { waitUntil: "networkidle2", timeout: 60000 });
    await sleep(1200);

    /* ---------- 场景一：老票（000001 平安银行，1991 上市）点「全历史」 ---------- */
    const readyA = await openStockAndWait(page, "000001");
    ok(readyA, "打开 000001 且 K 线视口就绪");
    if (!readyA) throw new Error("K 线未就绪");

    const s1 = await kvState(page);
    console.log("  初始视口:", JSON.stringify(s1));
    // ① 首屏默认仍只拉 250 根、可见 120 根（不能因为修全历史把首屏拖慢）
    ok(s1 && s1.rows === 250 && s1.count === 120 && s1.hasMore === true,
       `首屏默认：${s1 && s1.rows} 根已加载 / 可见 ${s1 && s1.count} / has_more=${s1 && s1.hasMore}`);

    // ② 点「全历史」
    const hasBtn = await page.evaluate(() => !!document.querySelector("#klineFull"));
    ok(hasBtn, "工具条存在「全历史」按钮（#klineFull）");
    await page.evaluate(() => document.querySelector("#klineFull").click());
    const s2 = await waitFullView(page, s1 ? s1.rows : 250, 45000);
    console.log("  点全历史后:", JSON.stringify(s2));
    ok(s2 && s2.rows > 250, `拉到全历史（${s2 && s2.rows} 根 > 250）`);
    ok(s2 && s2.count === s2.rows && s2.offset === 0, "全览：可见 = 全部 且 从第 0 根起");
    ok(s2 && s2.all === true, "_kv.all = true（标记为全览）");
    ok(s2 && /已显示全部/.test(s2.range), "range 明确标注「已显示全部」（" + (s2 && s2.range) + "）");
    ok(s2 && s2.first <= "1995-01-01", `最早一根回溯到上市初期（${s2 && s2.first}）`);

    // ③ 切周K → 保持全览
    await page.evaluate(() => {
      const b = document.querySelector('#klineWrap .kbtn[data-p="week"]');
      if (b) b.click();
    });
    const s3 = await waitFullView(page, s2 ? s2.rows : 250, 45000);
    console.log("  切周K后:", JSON.stringify(s3));
    ok(s3 && s3.period === "week" && s3.all === true && s3.count === s3.rows,
       `切周K仍保持全览（周 ${s3 && s3.rows} 根，可见全部）`);

    /* ---------- 场景二：另一只票（600519 茅台），滚轮缩到底自动加载 ---------- */
    const readyB = await openStockAndWait(page, "600519");
    ok(readyB, "打开 600519 且 K 线视口就绪");
    const b1 = await kvState(page);
    console.log("  600519 初始:", JSON.stringify(b1));
    ok(b1 && b1.rows === 250 && b1.hasMore === true, "600519 首屏同为 250 根 / has_more=true");

    // 把画布滚到视口内，取中心坐标
    await page.evaluate(() => {
      const c = document.querySelector("#klineCanvas");
      if (c) c.scrollIntoView({ block: "center" });
    });
    await sleep(500);
    const box = await page.evaluate(() => {
      const c = document.querySelector("#klineCanvas");
      const r = c.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    const cx = box.x + box.w / 2, cy = box.y + box.h / 2;

    // 反复向下滚（缩小）直到缩到底 —— 应自动触发全历史加载
    for (let i = 0; i < 12; i++) {
      await page.evaluate((x, y) => {
        const c = document.querySelector("#klineCanvas");
        c.dispatchEvent(new WheelEvent("wheel", {
          deltaY: 120, clientX: x, clientY: y, bubbles: true, cancelable: true,
        }));
      }, cx, cy);
      await sleep(250);
      const cur = await kvState(page);
      if (cur && cur.all) break;      // 已自动全览
    }
    const b2 = await waitFullView(page, b1 ? b1.rows : 250, 45000);
    console.log("  缩到底后:", JSON.stringify(b2));
    ok(b2 && b2.rows > 250 && b2.all === true && b2.count === b2.rows,
       `滚轮缩到底自动拉全历史并全览（${b2 && b2.rows} 根）`);
    ok(b2 && /已显示全部/.test(b2.range), "缩到底后 range 标「已显示全部」");

    /* ---------- 控制台零报错 ---------- */
    ok(errors.length === 0, "控制台无 JS 报错" + (errors.length ? "：" + errors.slice(0, 2).join(" | ") : ""));
  } catch (e) {
    ok(false, "异常中断：" + e.message);
  } finally {
    await browser.close();
  }

  console.log("\n==== " + (fails.length ? ("FAILED " + fails.length + " 项") : "ALL PASS") + " ====");
  if (fails.length) fails.forEach((f) => console.log("  · " + f));
  process.exit(fails.length ? 2 : 0);
})();
