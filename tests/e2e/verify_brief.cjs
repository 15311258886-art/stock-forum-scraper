/* 回归护栏：三个「结论页」（2026-10-12 体验审计 P0-D/E/F）+ P1 导航盘后分隔 + P2 子页签释义
   用法：node verify_brief.cjs （需 8765 后端在监听）
   覆盖：#p-info_brief / #p-smart_money / #p-watch_brief 三页的结论条与分区，
        左栏「盘后」分隔线、复盘成长沉底、子页签 title 释义、控制台零报错。 */
module.paths.push("C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules");
const puppeteer = require("puppeteer-core");

const BASE = "http://127.0.0.1:8765/";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const fails = [];
const ok = (c, m) => { if (!c) fails.push(m); console.log((c ? "PASS " : "FAIL ") + m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(BASE, { waitUntil: "networkidle2", timeout: 60000 });
  await sleep(2500);

  // ---- P1：左栏「盘后」分隔线 + 复盘成长沉底 ----
  const sep = await page.$$eval("#sidenav .rail-sep", (a) => a.map((x) => x.textContent.trim()));
  ok(sep.length === 1 && sep[0] === "盘后", "左栏出现「盘后」分隔线：" + JSON.stringify(sep));
  const cats = await page.$$eval("#sidenav .nav-item", (a) => a.map((x) => x.dataset.cat));
  ok(cats[cats.length - 1] === "learn", "复盘成长沉到左栏最底部：" + cats.join(","));

  // ---- P2：子页签带释义 title ----
  const hint = await page.$eval('.sub-tab[data-tab="today"]', (e) => e.getAttribute("title") || "");
  ok(hint.length > 10 && hint.includes("一屏四卡"), "子页签「今日决策」带大白话释义");

  // ---- P0-D 消息面汇总 ----
  await page.evaluate(() => { location.hash = "#info/info_brief"; });
  await sleep(3000);
  const active1 = await page.$eval("#subnav .sub-tab.active", (e) => e.textContent.trim()).catch(() => "");
  ok(active1 === "消息面汇总", "消息与外围默认落「消息面汇总」：" + active1);
  const v1 = await page.$eval("#p-info_brief .td-v-text", (e) => e.textContent.trim()).catch(() => "");
  ok(v1.length > 8, "消息面结论条非空：" + v1.slice(0, 56));
  const infoRows = await page.$$eval("#p-info_brief .bf-card .td-row", (a) => a.length);
  ok(infoRows >= 5, "消息面 Top/板块/数据日 分区有行：" + infoRows);
  const srcTags = await page.$$eval("#p-info_brief .bf-src", (a) => a.map((x) => x.textContent.trim()));
  ok(srcTags.length >= 5, "Top 条目带来源标签：" + JSON.stringify(srcTags.slice(0, 6)));
  const fresh = await page.$$eval("#p-info_brief .bf-fresh", (a) => a.length);
  ok(fresh === 6, "各源数据日 6 项（时效自查）：" + fresh);

  // ---- P0-E 聪明钱共识 ----
  await page.evaluate(() => { location.hash = "#players/smart_money"; });
  await sleep(3000);
  const active2 = await page.$eval("#subnav .sub-tab.active", (e) => e.textContent.trim()).catch(() => "");
  ok(active2 === "聪明钱共识", "别人怎么做默认落「聪明钱共识」：" + active2);
  const v2 = await page.$eval("#p-smart_money .td-v-text", (e) => e.textContent.trim()).catch(() => "");
  ok(v2.includes("聪明钱") && v2.includes("方向"), "共识结论条含方向与人数：" + v2.slice(0, 60));
  const consRows = await page.$$eval("#p-smart_money .td-row", (a) => a.length);
  ok(consRows >= 10, "共识榜/方向/近期共买 有行：" + consRows);
  const symLinks = await page.$$eval("#p-smart_money .td-sym", (a) => a.length);
  ok(symLinks >= 5, "共识个股可点入口：" + symLinks);

  // ---- P0-F 我的票·今日提示 ----
  await page.evaluate(() => { location.hash = "#watch/watch_brief"; });
  await sleep(6000);   // 该页会走一次三源并发取价（硬上限 3s）
  const active3 = await page.$eval("#subnav .sub-tab.active", (e) => e.textContent.trim()).catch(() => "");
  ok(active3 === "今日提示", "我的票默认落「今日提示」：" + active3);
  const v3 = await page.$eval("#p-watch_brief .td-v-text", (e) => e.textContent.trim()).catch(() => "");
  ok(v3.includes("关注池"), "今日提示结论条含关注池：" + v3.slice(0, 60));
  const secTitles = await page.$$eval("#p-watch_brief .bf-card .td-hd b", (a) => a.map((x) => x.textContent.trim()));
  ok(secTitles.length >= 5, "今日提示分区 ≥5（到价/异动/接近/看空/主线）：" + JSON.stringify(secTitles));
  // 池内当日异动：应至少有取到报价的票（本机三源可用时）
  const quoteRows = await page.$$eval("#p-watch_brief .bf-card", (a) => a.length);
  ok(quoteRows >= 6, "今日提示卡片数（含分组）：" + quoteRows);
  const hasWarn = await page.$("#p-watch_brief .bf-warn");
  console.log("  （info）未设目标价提示是否出现（数据相关，不计 FAIL）:", !!hasWarn);

  await page.evaluate(() => { location.hash = "#watch/watch_brief"; });
  await sleep(1500);
  await page.screenshot({ path: "_watch_brief.png", fullPage: true });
  await page.evaluate(() => { location.hash = "#info/info_brief"; });
  await sleep(1500);
  await page.screenshot({ path: "_info_brief.png", fullPage: true });

  ok(errs.length === 0, "控制台零报错" + (errs.length ? "：" + errs.slice(0, 3).join(" || ") : ""));

  await browser.close();
  console.log(fails.length ? "\nRESULT: FAIL (" + fails.length + ")" : "\nRESULT: ALL PASS");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("RUN ERROR:", e.message); process.exit(2); });
