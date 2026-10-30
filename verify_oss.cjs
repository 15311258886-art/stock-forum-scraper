/* verify_oss.cjs —— 开源情报面板回归（2026-10-30）
 *
 * 覆盖：① 深链 #overview/oss 直达不报错（这条最容易踩 TDZ）；
 *      ② 面板唯一 active、内容非空、表格行数与接口一致（11 条规则）；
 *      ③ 三处"必须显示出来"的关键信息：样本边界、状态标签、长样本同向标记；
 *      ④ 控制台零 error（含 404 —— 版本号没同步时 app.js 会 404 白屏）。
 *
 * 运行（需查看器已在 127.0.0.1:8765 运行）：
 *   NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" verify_oss.cjs
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

  // ① 深链直达
  await page.goto(URL + "#overview/oss", { waitUntil: "networkidle2", timeout: 60000 });
  let ok = true;
  try {
    await page.waitForFunction(() => {
      const b = document.querySelector("#p-oss");
      return b && b.innerHTML.length > 300;
    }, { timeout: 40000 });
  } catch (e) { ok = false; }
  ck(ok, "深链 #overview/oss 渲染出内容");

  // 关掉新手引导（首次访问会盖住整个面板，截图与人工目检都会被挡）。
  // 注意：不能按 offsetParent 判可见性 —— #firstRunModal 是 position:fixed，
  // 其 offsetParent 恒为 null，用它过滤会永远找不到按钮（第一版就踩了这个坑）。
  const closed = await page.evaluate(() => {
    const b = document.getElementById("frDone");
    if (b) { b.click(); return true; }
    return false;
  }).catch(() => false);
  if (closed) await new Promise((r) => setTimeout(r, 700));
  // 兜底：即便按钮逻辑没生效，也把遮罩摘掉，保证截图能反映真实面板
  await page.evaluate(() => {
    ["firstRunModal", "firstRunMask"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
  }).catch(() => {});

  const r = await page.evaluate(() => {
    const box = document.querySelector("#p-oss");
    const tbl = box ? box.querySelector("table") : null;
    return {
      len: box ? box.innerHTML.length : 0,
      rows: tbl ? tbl.querySelectorAll("tbody tr").length : 0,
      active: box ? box.classList.contains("active") : false,
      actives: Array.from(document.querySelectorAll(".panel.active")).map((x) => x.id),
      text: box ? box.innerText : "",
      navHasOss: !!document.querySelector('[data-tab="oss"], [href*="/oss"]'),
    };
  });

  ck(r.active && r.actives.length === 1 && r.actives[0] === "p-oss",
    "面板唯一 active", JSON.stringify(r.actives));
  ck(r.rows === 11, "规则表 11 行", "实际 " + r.rows);
  // 2026-10-31：因子表回补到 2016 后，样本起点不再是 2025-08-18。
  // 断言改成「必须披露样本边界 + 年数动态计算」，避免写死日期后一动数据就误报。
  ck(/主结果样本[:：]/.test(r.text) && /\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}/.test(r.text),
    "样本边界已披露（起止日期）");
  ck(/约\s*\d+(\.\d+)?\s*年/.test(r.text), "样本长度按实际交易日换算成年");
  ck(/已推翻/.test(r.text), "状态标签「已推翻」出现");
  ck(/同向/.test(r.text), "长样本「同向」标记出现");
  ck(/Stockbee/.test(r.text), "头条规则出现");
  ck(r.navHasOss, "导航中存在 oss 入口");

  // ② 再点一次导航（已加载态重绘路径）
  await page.evaluate(() => { location.hash = "#overview/harvest"; });
  await page.waitForFunction(() => {
    const b = document.querySelector("#p-harvest");
    return b && b.classList.contains("active");
  }, { timeout: 20000 }).catch(() => {});
  await page.evaluate(() => { location.hash = "#overview/oss"; });
  await page.waitForFunction(() => {
    const b = document.querySelector("#p-oss");
    return b && b.classList.contains("active");
  }, { timeout: 20000 }).catch(() => {});
  const back = await page.evaluate(() => {
    const b = document.querySelector("#p-oss");
    return { active: b && b.classList.contains("active"), len: b ? b.innerHTML.length : 0 };
  });
  ck(back.active && back.len > 300, "切走再切回仍正常（缓存重绘）");

  // 既有告警与真错误的区分：`frame-ancestors` 写在 <meta> 里按 CSP 规范无效，
  // 浏览器每次都会报这条 —— 它是 index.html 的老问题（服务端也没发 X-Frame-Options），
  // 与本次改动无关。不静默吞掉，单列成 NOTE 打出来，但不计入 FAIL。
  const KNOWN_NOISE = [/frame-ancestors/i];
  const hard = errs.filter((e) => !KNOWN_NOISE.some((re) => re.test(e)));
  const known = errs.filter((e) => KNOWN_NOISE.some((re) => re.test(e)));
  ck(hard.length === 0, "控制台零运行时错误 / 零请求失败",
    hard.length ? hard.slice(0, 3).join(" | ") : "");
  if (known.length) {
    console.log("  NOTE 既有告警（CSP meta 不支持 frame-ancestors，非本次引入）："
      + known.length + " 条，建议后续改为响应头下发");
  }

  try {
    fs.mkdirSync("shots", { recursive: true });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: "shots/verify_oss.png", fullPage: false });
    // 整页另存一张：仓库清单在折叠线以下，只看首屏会漏检底部排版
    await page.screenshot({ path: "shots/verify_oss_full.png", fullPage: true });
    console.log("  截图 → shots/verify_oss.png / verify_oss_full.png");
  } catch (e) { /* 截图失败不影响判定 */ }

  await browser.close();
  console.log(fails.length ? "\n结果 FAIL：" + fails.join("；") : "\n结果 PASS（全部检查通过）");
  process.exit(fails.length ? 2 : 0);
})().catch((e) => { console.error("异常：", e && e.message); process.exit(2); });
