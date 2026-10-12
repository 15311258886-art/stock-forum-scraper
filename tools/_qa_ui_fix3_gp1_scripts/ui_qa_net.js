/* 网络健康度：全导航扫一遍，记录所有 HTTP >= 400 的响应与失败请求 */
"use strict";
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8765";
const BASE = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluateOnNewDocument(() => { window.confirm = () => false; });
  await page.setRequestInterception(true);
  const bad = [], failed = [], all = [];
  page.on("request", (req) => {
    const u = req.url();
    all.push(req.method() + " " + u);
    if (/\/api\/(demo|overseas|fetch)\b/.test(u)) return req.abort().catch(() => {});
    req.continue().catch(() => {});
  });
  page.on("response", (res) => {
    const s = res.status();
    if (s >= 400) bad.push(s + " " + res.url());
  });
  page.on("requestfailed", (req) => {
    failed.push((req.failure() || {}).errorText + " " + req.url());
  });
  page.on("dialog", (d) => d.dismiss().catch(() => {}));

  await page.goto(BASE, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);

  const cats = await page.evaluate(() => [...document.querySelectorAll("#sidenav .nav-item")].map((a) => a.dataset.cat));
  const visited = [];
  for (const c of cats) {
    await page.evaluate((k) => document.querySelector(`#sidenav .nav-item[data-cat="${k}"]`).click(), c);
    await wait(600);
    const subs = await page.evaluate(() => [...document.querySelectorAll("#subnav .sub-tab")].map((a) => a.dataset.tab));
    for (const t of subs) {
      await page.evaluate((x) => { const a = document.querySelector(`#subnav .sub-tab[data-tab="${x}"]`); if (a) a.click(); }, t);
      await wait(900);
      visited.push(c + "/" + t);
    }
  }
  // 刷新首页一次，确认无 4xx
  await page.reload({ waitUntil: "networkidle2" });
  await wait(1500);

  const out = { visitedTabs: visited.length, tabs: visited, http4xx5xx: bad, requestFailed: failed, totalRequests: all.length };
  fs.writeFileSync(path.join(__dirname, "net.json"), JSON.stringify(out, null, 2), "utf-8");
  console.log("tabs=" + visited.length + " bad=" + bad.length + " failed=" + failed.length + " totalReq=" + all.length);
  if (bad.length) console.log(bad.join("\n"));
  if (failed.length) console.log(failed.join("\n"));
  await browser.close();
})().catch((e) => { console.log("ERR " + (e && e.stack || e)); process.exit(2); });
