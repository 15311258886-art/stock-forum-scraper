/* 破坏性按钮二次确认 回归（真实 Chromium）
   验证「载入示例 / 刷新海外舆情」：取消 → 一个请求都不发、提示"已取消"；
   确认 → 才发请求且 body 带 confirm；载入后统计确实变化。
   ⚠️ 仅在**隔离实例**上跑（本脚本会真的执行一次载入示例）。 */
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8799";
const URL = `http://127.0.0.1:${PORT}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, e) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${e ? "  —— " + e : ""}`);

(async () => {
  const b = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await b.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const posts = [];
  page.on("request", (r) => { if (r.method() === "POST") posts.push({ url: r.url(), body: r.postData() || "" }); });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  STEP = "goto";
  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.waitForSelector("#cards .card.clickable");

  /* 1) 取消「载入示例」→ 不发请求 */
  STEP = "dismiss-demo";
  page.once("dialog", async (d) => { await d.dismiss(); });
  posts.length = 0;
  await page.click("#btnDemo");
  await wait(800);
  const demoReqs1 = posts.filter((p) => /\/api\/demo$/.test(p.url)).length;
  const hint1 = await page.$eval("#statusHint", (e) => e.textContent);
  ok("1.1 取消「载入示例」→ 一个 /api/demo 请求都不发", demoReqs1 === 0, `POST 次数=${demoReqs1}`);
  ok("1.2 取消后提示「已取消：未改动任何数据」", /已取消/.test(hint1), hint1.slice(0, 50));

  /* 2) 确认「载入示例」→ 发请求且带 confirm:true，统计确实变化 */
  STEP = "accept-demo";
  const before = await (await fetch(URL + "/api/summary")).json();
  page.once("dialog", async (d) => { await d.accept(); });
  posts.length = 0;
  await page.click("#btnDemo");
  await wait(6000);
  const demoReqs2 = posts.filter((p) => /\/api\/demo$/.test(p.url));
  ok("2.1 确认后发出 /api/demo 且 body 带 confirm",
     demoReqs2.length > 0 && /"confirm":\s*true/.test(demoReqs2[0].body),
     demoReqs2.length ? demoReqs2[0].body.slice(0, 40) : "无请求");
  const after = await (await fetch(URL + "/api/summary")).json();
  const changed = before.counts.players !== after.counts.players
    || before.counts.posts !== after.counts.posts || before.counts.trades !== after.counts.trades;
  ok("2.2 确认后数据确实被载入（统计变化）", changed,
     `players ${before.counts.players}→${after.counts.players}, posts ${before.counts.posts}→${after.counts.posts}`);

  /* 3) 取消「刷新海外舆情」→ 不发请求（不触发联网抓取） */
  STEP = "dismiss-overseas";
  page.once("dialog", async (d) => { await d.dismiss(); });
  posts.length = 0;
  await page.click("#btnOverseas");
  await wait(800);
  const ovReqs = posts.filter((p) => /\/api\/overseas$/.test(p.url)).length;
  const hint3 = await page.$eval("#statusHint", (e) => e.textContent);
  ok("3.1 取消「刷新海外舆情」→ 一个 /api/overseas 请求都不发", ovReqs === 0, `POST 次数=${ovReqs}`);
  ok("3.2 取消后提示「已取消」", /已取消/.test(hint3), hint3.slice(0, 50));

  ok("4. 无 JS 运行时错误", errs.length === 0, errs.slice(0, 3).join(" ; ") || "无");

  console.log(out.join("\n"));
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(`\n=== ${out.length - fails}/${out.length} 通过 ===`);
  await b.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + e.message); process.exit(2); });
