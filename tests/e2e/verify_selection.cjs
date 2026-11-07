/* verify_selection.cjs —— 选股方向面板回归（2026-10-30）
 *
 * 覆盖：① 深链 #overview/selection 直达不报错（与 harvest/oss 同一条 TDZ 路径）；
 *      ② 面板唯一 active、四块内容齐全（结论卡/短线表/中长期对照/低波池）；
 *      ③ 最关键的一条：trap 组必须显示「最差」标记 —— 这是本次结论的核心，
 *         如果前端漏掉，用户看到的就成了「横盘股也有机会」的错误暗示；
 *      ④ 控制台零 error（含 404 —— 版本号没同步会白屏）。
 *
 * 运行：NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" node verify_selection.cjs
 * 退出码：0=PASS，2=FAIL，3=无可用浏览器。
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
  ];
  try {
    const base = "C:/Users/野比&大熊/AppData/Local/ms-playwright";
    if (fs.existsSync(base)) {
      fs.readdirSync(base).filter((d) => /^chromium-/.test(d)).sort().reverse()
        .forEach((d) => cands.push(base + "/" + d + "/chrome-win64/chrome.exe"));
    }
  } catch (e) { /* 忽略 */ }
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

const fails = [];
function ck(cond, name, detail) {
  console.log((cond ? "  PASS " : "  FAIL ") + name + (detail ? "  → " + detail : ""));
  if (!cond) fails.push(name);
}

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("SKIP 无可用浏览器"); process.exit(3); }

  const browser = await puppeteer.launch({
    executablePath: exe, headless: true, args: ["--no-sandbox", "--disable-gpu"],
    defaultViewport: { width: 1500, height: 1100 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  page.on("requestfailed", (r) => errs.push("reqfail: " + r.url().slice(-60)));

  await page.goto(URL + "#overview/selection", { waitUntil: "networkidle2", timeout: 60000 });
  let ok = true;
  try {
    await page.waitForFunction(() => {
      const b = document.querySelector("#p-selection");
      return b && b.innerHTML.length > 500;
    }, { timeout: 40000 });
  } catch (e) { ok = false; }
  ck(ok, "深链 #overview/selection 渲染出内容");

  const closed = await page.evaluate(() => {
    const b = document.getElementById("frDone");
    if (b) { b.click(); return true; }
    return false;
  }).catch(() => false);
  if (closed) await new Promise((r) => setTimeout(r, 700));
  await page.evaluate(() => {
    ["firstRunModal", "firstRunMask"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
  }).catch(() => {});

  const r = await page.evaluate(() => {
    const box = document.querySelector("#p-selection");
    const tbls = box ? Array.from(box.querySelectorAll("table")) : [];
    return {
      len: box ? box.innerHTML.length : 0,
      tables: tbls.length,
      rows: tbls.map((t) => t.querySelectorAll("tr").length),
      active: box ? box.classList.contains("active") : false,
      actives: Array.from(document.querySelectorAll(".panel.active")).map((x) => x.id),
      text: box ? box.innerText : "",
      navHas: !!document.querySelector('[data-tab="selection"], [href*="/selection"]'),
      overflowX: box ? box.scrollWidth > box.clientWidth + 2 : false,
    };
  });

  ck(r.active && r.actives.length === 1 && r.actives[0] === "p-selection",
    "面板唯一 active", JSON.stringify(r.actives));
  ck(r.tables === 3, "三个表（短线 / 中长期 / 低波池）", "实际 " + r.tables);
  ck(r.rows[0] === 9, "短线表 8 条形态 + 表头", "实际 " + r.rows[0]);
  ck(r.rows[1] === 5, "中长期表 4 组 + 表头", "实际 " + r.rows[1]);
  // 2026-10-30 改版：低波池默认渲染 60 行 + 表头（原 40 行），避免「slice(0,40) 却说本页全部列出」的假披露
  ck(r.rows[2] === 61, "低波池表 60 行 + 表头", "实际 " + r.rows[2]);
  ck(/最差/.test(r.text), "trap 组「最差」标记出现");
  ck(/曾活跃后沉寂/.test(r.text), "反直觉结论（曾活跃后沉寂）已展示");
  ck(/一直低波/.test(r.text), "正向结论（一直低波）已展示");
  ck(/不是买入信号/.test(r.text), "定位声明（不是买入信号）出现");
  ck(/不是筛选出错/.test(r.text), "银行股解释（不是筛选出错）出现");
  ck(/正超额日占比/.test(r.text), "「正超额日占比」标签已澄清语义");
  ck(/幸存者偏差/.test(r.text), "口径边界已披露");
  ck(r.navHas, "导航中存在 selection 入口");
  ck(!r.overflowX, "面板无横向溢出");

  // 切走再切回（缓存重绘路径）
  await page.evaluate(() => { location.hash = "#overview/harvest"; });
  await page.waitForFunction(() => {
    const b = document.querySelector("#p-harvest");
    return b && b.classList.contains("active");
  }, { timeout: 20000 }).catch(() => {});
  await page.evaluate(() => { location.hash = "#overview/selection"; });
  await page.waitForFunction(() => {
    const b = document.querySelector("#p-selection");
    return b && b.classList.contains("active");
  }, { timeout: 20000 }).catch(() => {});
  const back = await page.evaluate(() => {
    const b = document.querySelector("#p-selection");
    return { active: b && b.classList.contains("active"), len: b ? b.innerHTML.length : 0 };
  });
  ck(back.active && back.len > 500, "切走再切回仍正常（缓存重绘）");

  await page.screenshot({ path: "shots/verify_selection.png", fullPage: true });
  // 面板很长，整页图在目检时会被压缩到看不清 —— 按三段各截一张视口图
  const shot = async (name, fn) => {
    await page.evaluate(fn).catch(() => {});
    await new Promise((r) => setTimeout(r, 450));
    await page.screenshot({ path: "shots/" + name, fullPage: false });
  };
  await shot("sel_top.png", () => {
    const box = document.querySelector("#p-selection");
    if (box) window.scrollTo(0, box.offsetTop - 40);
  });
  await shot("sel_mid.png", () => {
    const t = document.querySelectorAll("#p-selection table")[1];
    if (t) t.scrollIntoView({ block: "start" });
  });
  await shot("sel_bottom.png", () => {
    const box = document.querySelector("#p-selection");
    if (box && box.lastElementChild) box.lastElementChild.scrollIntoView({ block: "end" });
  });
  fs.copyFileSync("shots/sel_bottom.png", "shots/verify_selection_bottom.png");
  console.log("  截图 → shots/sel_top.png + sel_mid.png + sel_bottom.png（另有整页 verify_selection.png）");

  const KNOWN_NOISE = [/frame-ancestors/i];
  const hard = errs.filter((e) => !KNOWN_NOISE.some((re) => re.test(e)));
  ck(hard.length === 0, "控制台零运行时错误 / 零请求失败",
    hard.length ? hard.slice(0, 3).join(" | ") : "");

  await browser.close();
  console.log(fails.length ? "\nFAIL " + fails.length + " 项" : "\nALL PASS");
  process.exit(fails.length ? 2 : 0);
})();
