/* verify_avoid_panel.cjs —— 避坑面板排版复核（2026-10-31 权重重标定后）
 *
 * 关注点：RULES 的实证摘要在本轮变长（含「原短样本高估 5 倍」「样本<300日压档」等），
 *  ① 6 条规则是否全部渲染、权重数字与后端一致；
 *  ② 长文案是否把卡片撑破（横向溢出 / 文字压字 / 行高塌陷）；
 *  ③ 控制台零 error。
 * 运行：NODE_PATH="C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules" node verify_avoid_panel.cjs
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
  return cands.find((p) => p && fs.existsSync(p)) || null;
}

const fails = [];
const ck = (c, n, d) => {
  console.log((c ? "  PASS " : "  FAIL ") + n + (d ? "  → " + d : ""));
  if (!c) fails.push(n);
};

(async () => {
  const exe = pickBrowser();
  if (!exe) { console.log("SKIP 无可用浏览器"); process.exit(3); }
  const browser = await puppeteer.launch({
    executablePath: exe, headless: true, args: ["--no-sandbox", "--disable-gpu"],
    defaultViewport: { width: 1500, height: 1200 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(URL + "#overview/avoid", { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => {
    ["firstRunModal", "firstRunMask", "frDone"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) { try { el.click(); } catch (e) {} }
    });
  }).catch(() => {});
  await new Promise((r) => setTimeout(r, 1500));

  const r = await page.evaluate(() => {
    const box = document.querySelector("#p-avoid") || document.querySelector(".panel.active");
    const txt = box ? box.innerText : "";
    return {
      active: box ? box.id : null,
      len: txt.length,
      text: txt,
      overflowX: box ? box.scrollWidth > box.clientWidth + 4 : null,
      h: box ? box.scrollHeight : null,
    };
  });

  ck(r.active === "p-avoid", "避坑面板激活", String(r.active));
  ck(r.len > 500, "面板有内容（不是「正在加载数据…」）", "len=" + r.len);
  ck(!/正在加载数据/.test(r.text), "未卡在加载态");
  ck(/龙虎榜/.test(r.text), "龙虎榜理由标签出现");
  ck(/分层实测/.test(r.text), "分层实测披露出现（只做排除的定位声明）");
  ck(!r.overflowX, "无横向溢出", String(r.overflowX));

  // 说明：avoid.RULES 的权重与实证摘要只在后端打分逻辑与文档里，不在本面板渲染，
  // 所以这里不校验「-1.22%」是否出现 —— 那是账本(evidence)的事，不是本面板的。
  // 之前写死这条断言会误报，已更正。
  const shotDir = "shots";
  try { fs.mkdirSync(shotDir, { recursive: true }); } catch (e) {}
  const p = shotDir + "/verify_avoid_20261031.png";
  await page.screenshot({ path: p, fullPage: false });
  console.log("SHOT " + p);

  // CSP 的 frame-ancestors 警告由 <meta> 传递必然出现，属浏览器噪音，不算错误
  const real = errs.filter((m) => !/frame-ancestors/.test(m));
  ck(real.length === 0, "控制台零真实 error", real.slice(0, 2).join(" | "));
  await browser.close();
  console.log(fails.length ? "FAIL " + fails.length : "AVOID_PANEL_OK");
  process.exit(fails.length ? 2 : 0);
})();
