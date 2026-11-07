/* 「会清空」确认框取证 —— 原生 confirm 无法用 page.screenshot 截图，只能带桌面截屏。
 * 做法：headed 启动 → 挂 dialog 监听（不立即关闭）→ 用 PowerShell 抓整屏 → 再 dismiss。
 * 用法：node tools/qa_r3_confirm_shot.cjs
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const puppeteer = require(path.join(__dirname, "..", "node_modules", "puppeteer-core"));

const ROOT = path.join(__dirname, "..");
const LOGS = path.join(ROOT, "viewer", "logs");
const BASE = "http://127.0.0.1:8765";
const EXE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: false,
    protocolTimeout: 600000,
    defaultViewport: null,
    args: ["--no-sandbox", "--window-size=1500,950", "--window-position=0,0"],
  });
  const page = (await browser.pages())[0];
  await page.goto(BASE + "/#toolbox/toolbox", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.evaluate(() => {
    const m = document.querySelector("#firstRunMask"); if (m) m.style.display = "none";
    const f = document.querySelector("#firstRunModal"); if (f) f.style.display = "none";
  });
  await page.waitForFunction(() => {
    const tb = window.TB; if (!tb || !tb.counted) return false;
    const ks = Object.keys(tb.counted);
    return ks.length > 5 && ks.every((k) => tb.counted[k] !== null && tb.counted[k] !== undefined);
  }, { timeout: 60000, polling: 100 });
  // 先点一刀箱体谷底，让池子变成 699（危险卡才出现）
  await page.evaluate(() => document.querySelector('[data-tbx-card="box_bottom"]').click());
  await page.waitForFunction(() => {
    const L = document.getElementById("tbxLane");
    return L && [...L.querySelectorAll(".tbx-node .tbx-nN")].some((x) => x.textContent.trim() === "699");
  }, { timeout: 60000, polling: 50 });
  await sleep(1200);
  await page.bringToFront();
  await sleep(500);

  const res = { dialog: null, shots: [] };
  page.once("dialog", async (d) => {
    res.dialog = { type: d.type(), message: d.message() };
    try {
      execSync('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; ' +
        '$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; ' +
        '$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; ' +
        '$g=[System.Drawing.Graphics]::FromImage($bmp); ' +
        '$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); ' +
        "$bmp.Save($env:TEMP+'\\qa_confirm_shot.png'); $g.Dispose(); $bmp.Dispose()\"",
        { stdio: "inherit", timeout: 30000 });
      const src = path.join(process.env.TEMP || process.env.TMP || ".", "qa_confirm_shot.png");
      const dst = path.join(LOGS, "qa_r3_13_confirm_dialog.png");
      fs.copyFileSync(src, dst);
      res.shots.push(dst);
    } catch (e) {
      res.shotError = String((e && e.message) || e);
    }
    try { await d.dismiss(); } catch (e) { /* ignore */ }
  });

  // 找一张「会清空」的卡并点它
  const key = await page.evaluate(() => {
    const c = document.querySelector(".tbx-card.danger");
    return c ? c.getAttribute("data-tbx-card") : null;
  });
  res.dangerKey = key;
  if (key) {
    await page.evaluate((k) => document.querySelector('[data-tbx-card="' + k + '"]').click(), key);
    await sleep(3000);
  }
  await sleep(1500);
  await browser.close();
  console.log(JSON.stringify(res, null, 2));
})().catch((e) => {
  console.log(JSON.stringify({ fatal: String((e && e.stack) || e) }));
  process.exit(1);
});
