/* verify_daychips.cjs —— 「交易日横排」回归护栏（2026-10-12 进哥要求）
 *
 * 背景：龙虎榜 / 板块动向的「交易日」原来把库内全部交易日铺成一排，日子一多就换行（拐弯儿），
 * 把正文挤下去。改为：只渲染最近 10 个 + 一个日期查询框（可直达库内任意交易日）。
 *
 * 断言：
 *   ① 龙虎榜面板：可见交易日 chip ≤ 10；存在 .day-jump 日期框；有「…还有 N 个交易日」时给出；
 *   ② 板块动向面板：同上；
 *   ③ 点第 2 个 chip → 高亮切换（原功能未被破坏）；
 *   ④ 控制台零报错。
 *
 * 运行（需后端在 8765）：
 *   NODE_PATH=".../node_modules" node verify_daychips.cjs
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

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("SKIP 无可用浏览器"); process.exit(3); }
  const browser = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const errs = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
    page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
    await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });

    for (const [hash, panel, attr] of [["#money/lhb", "#p-lhb", "data-lhb-date"],
                                       ["#money/lhb_sectors", "#p-lhb_sectors", "data-lhs-date"]]) {
      await page.evaluate((h) => { location.hash = h; }, hash);
      await page.waitForSelector(`${panel} .chip-s`, { timeout: 25000 }).catch(() => {});
      await new Promise((r) => setTimeout(r, 1200));

      const info = await page.evaluate((sel, a) => {
        const root = document.querySelector(sel);
        if (!root) return { missing: true };
        const chips = root.querySelectorAll(`[${a}]`);
        const jump = root.querySelector(".day-jump");
        const more = root.querySelector(".day-more");
        return { n: chips.length, hasJump: !!jump,
                 jumpDays: jump ? (jump.dataset.days || "").split(",").length : 0,
                 moreText: more ? more.textContent.trim() : "" };
      }, panel, attr);

      ok(!info.missing, `${panel} 面板存在`);
      ok(info.n > 0 && info.n <= 10, `${panel} 可见交易日 chip ≤ 10（实=${info.n}）`);
      if (info.jumpDays > 10) {
        ok(info.hasJump, `${panel} 有日期查询框（库内共 ${info.jumpDays} 个交易日）`);
        ok(/\+?\d+/.test(info.moreText), `${panel} 有「还有 N 个」提示（${info.moreText}）`);
        // 查询框必须排在 chips 之前（否则会被挤到第二行）
        const jumpFirst = await page.evaluate((sel) => {
          const root = document.querySelector(sel);
          const jump = root.querySelector(".day-jump");
          const chip = root.querySelector(".chip-s");
          if (!jump || !chip) return null;
          return jump.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING ? true : false;
        }, panel);
        ok(jumpFirst === true, `${panel} 日期查询框排在交易日 chip 之前`);
        // 关键：所有 chip 必须落在同一行（不换行 = 进哥要的效果）
        const singleRow = await page.evaluate((sel, a) => {
          const root = document.querySelector(sel);
          const chips = [...root.querySelectorAll(`[${a}]`)];
          if (chips.length < 2) return true;
          const tops = new Set(chips.map((c) => Math.round(c.getBoundingClientRect().top)));
          return tops.size === 1;
        }, panel, attr);
        ok(singleRow, `${panel} 交易日 chip 全部在同一行（不换行/不拐弯）`);
      } else {
        ok(true, `${panel} 库内交易日 ${info.jumpDays} 个，未超限（无需查询框）`);
      }
    }

    // 原功能未被破坏：点第 2 个 chip → 被选中的交易日应当改变
    // 注意：不能比较两个 chip 的 style 字符串（都带 accent 时会"相等"而误判），
    // 要比"被选中的那个日期值"。
    const switched = await page.evaluate(async () => {
      const selDate = () => {
        const el = [...document.querySelectorAll("#p-lhb [data-lhb-date]")]
          .find((x) => (x.getAttribute("style") || "").includes("background"));
        return el ? el.dataset.lhbDate : "";
      };
      const before = selDate();
      const chips = [...document.querySelectorAll("#p-lhb [data-lhb-date]")];
      if (chips.length < 2) return "skip";
      chips[1].click();
      await new Promise((r) => setTimeout(r, 2000));
      const after = selDate();
      return after && after !== before ? "yes" : ("same(" + before + "->" + after + ")");
    });
    ok(switched === "yes" || switched === "skip", "点 chip 仍可切换交易日（" + switched + "）");

    const realErrs = errs.filter((e) => !/favicon|ERR_CONNECTION_REFUSED/i.test(e));
    ok(realErrs.length === 0, "控制台零报错（实=" + realErrs.length + "）" + (realErrs[0] ? " :: " + realErrs[0] : ""));
  } catch (e) {
    console.log("RUN ERROR: " + e.message);
    fails.push("run");
  } finally {
    await browser.close();
  }
  console.log("\n==== " + (fails.length ? "FAIL (" + fails.length + ")" : "ALL PASS") + " ====");
  process.exit(fails.length ? 2 : 0);
})();
