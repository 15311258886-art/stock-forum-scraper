/* tools/probe_dom_timeline.cjs —— DOM 节点数随时间的收敛曲线（2026-11-01 复核用）
 *
 * 为什么需要它：verify_dom_weight.cjs 固定等 2.5s，而 #p-watch（关注池）是一个
 * 比 2.5s 更慢的接口 —— 同一份代码在 2.5s 和 8s 量，能差出 1000+ 个节点。
 * 改前/改后如果落在不同的采样时刻，比较就没有意义。故这里一次会话连续采样，
 * 给出"什么时候稳下来"，让 12840 → N 这个 N 是可复现的（取相邻两次差 <1% 的收敛值）。
 */
const puppeteer = require("puppeteer-core");
const URL = process.env.VIEWER_URL || "http://127.0.0.1:8765/";
const HASH = process.env.VIEWER_HASH || "#today/today";
const exe = ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe"].find((p) => require("fs").existsSync(p));

(async () => {
  if (!exe) { console.log("NO_BROWSER"); process.exit(3); }
  const b = await puppeteer.launch({ executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--window-size=1440,1200"] });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 1200 });
  await p.goto(URL + HASH, { waitUntil: "domcontentloaded", timeout: 60000 });
  const series = [];
  for (let i = 1; i <= 24; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const n = await p.evaluate(() => document.querySelectorAll("*").length);
    series.push({ t: (i * 0.5).toFixed(1), n });
  }
  const last = series[series.length - 1].n;
  // 收敛点：从后往前找第一个与 last 相差 <1% 的时刻
  let settled = series[series.length - 1];
  for (let i = series.length - 1; i >= 0; i--) {
    if (Math.abs(series[i].n - last) / Math.max(1, last) < 0.01) settled = series[i]; else break;
  }
  const brk = await p.evaluate(() => {
    const c = (id) => { const el = document.getElementById(id); return el ? el.querySelectorAll("*").length : -1; };
    return { total: document.querySelectorAll("*").length,
      visible: [...document.querySelectorAll("*")].filter((el) => el.offsetParent !== null).length,
      p_today: c("p-today"), p_watch: c("p-watch"), p_summary: c("p-summary"),
      p_overseas: c("p-overseas_sentiment"), p_lhb: c("p-lhb"), p_geo: c("p-geo_events") };
  });
  console.log("SERIES=" + series.map((s) => s.t + "s:" + s.n).join(" "));
  console.log("SETTLED@" + settled.t + "s = " + settled.n);
  console.log("BREAKDOWN=" + JSON.stringify(brk));
  await b.close();
})();
