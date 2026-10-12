const puppeteer = require("puppeteer-core");
const path = require("path");
const fs = require("fs");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = path.resolve(__dirname, "..", "_shots_rate");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: "new", args: ["--no-sandbox"] });
  const p = await b.newPage();
  await p.setViewport({ width: 1600, height: 1000 });
  await p.goto("http://127.0.0.1:8765/#watch/rate", { waitUntil: "domcontentloaded" });
  await sleep(1500);
  await p.evaluate(() => document.querySelector('.nav-item[data-cat="watch"]').click());
  await sleep(400);
  await p.evaluate(() => document.querySelector('#subnav .sub-tab[data-tab="rate"]').click());

  for (let i = 0; i < 40; i++) {
    if (await p.evaluate(() => document.querySelectorAll("#p-rate tbody tr").length > 0)) break;
    await sleep(1000);
  }
  await sleep(1000);
  // 榜单头部 + 前 5 行
  const tbl = await p.$("#p-rate .wt-tw") || await p.$("#p-rate table");
  if (tbl) { await tbl.screenshot({ path: path.join(OUT, "A-table.png") }); console.log("A-table ok"); }

  for (let i = 0; i < 30; i++) {
    if (await p.evaluate(() => document.querySelectorAll("#p-rate .tl-card").length > 0)) break;
    await sleep(1500);
  }
  await p.evaluate(() => {
    const c = document.querySelector("#p-rate .tl-card");
    if (c && !c.classList.contains("open")) c.querySelector(".tl-head").click();
  });
  await sleep(900);
  const sec = await p.$("#p-rate .rate-sec");
  if (sec) { await sec.screenshot({ path: path.join(OUT, "B-timeline.png") }); console.log("B-timeline ok"); }
  await b.close();
})();
