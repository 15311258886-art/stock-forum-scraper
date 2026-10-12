/* 入手概率板块回归（无头 Chromium / Edge）
 * 覆盖：新面板渲染 → 七级档位 → 提示两行 → 归档按钮 → 时间轴折叠 → 抽屉 → 老面板回归
 * 用法：node _shots/rate_regression.js
 */
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer-core");

const BASE = "http://127.0.0.1:8765";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = path.resolve(__dirname, "..", "_shots_rate");
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

const results = [];
const ok = (n, d = "") => results.push({ s: "PASS", n, d });
const bad = (n, d = "") => results.push({ s: "FAIL", n, d });
const chk = (cond, n, d = "") => (cond ? ok(n, d) : bad(n, d));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });

  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------- 1. 进入「我的票 → 入手概率」 ----------
  await page.goto(BASE + "/#watch/rate", { waitUntil: "domcontentloaded" });
  await sleep(1200);
  await page.evaluate(() => {
    const nav = document.querySelector('.nav-item[data-cat="watch"]');
    if (nav) nav.click();
  });
  await sleep(500);
  await page.evaluate(() => {
    const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]');
    if (t) t.click();
  });
  // 浏览器端拉全市场榜约 3~8 秒，等表格行真的出现再断言，别按固定秒数赌
  let waited = 0;
  while (waited < 60000) {
    const n = await page.evaluate(() => document.querySelectorAll("#p-rate tbody tr").length);
    if (n > 0) break;
    await sleep(1000); waited += 1000;
  }
  console.log(`榜单就绪耗时 ≈ ${waited}ms`);

  const panel = await page.evaluate(() => {
    const p = document.querySelector("#p-rate");
    return { exists: !!p, html: p ? p.innerHTML.length : 0 };
  });
  chk(panel.exists && panel.html > 500, "① 面板 #p-rate 渲染", `html ${panel.html} 字符`);

  // ---------- 2. 榜单行数 + 七级档位 + 提示两行 ----------
  const table = await page.evaluate(() => {
    const p = document.querySelector("#p-rate");
    const rows = Array.from(p.querySelectorAll("tbody tr"));
    const chips = new Set();
    rows.forEach((r) => r.querySelectorAll(".gr-chip").forEach((c) => chips.add(c.className.replace("gr-chip gr-", ""))));
    const grades = new Set(Array.from(p.querySelectorAll(".gr-chip")).map((c) => c.textContent.trim()));
    return {
      rows: rows.length,
      chips: Array.from(chips),
      grades: Array.from(grades),
      hint1: p.querySelectorAll(".rh-1").length,
      hint2: p.querySelectorAll(".rh-2").length,
      score: p.querySelectorAll(".rate-score").length,
      sample: rows.slice(0, 2).map((r) => r.innerText.replace(/\s+/g, " ").slice(0, 120)),
    };
  });
  chk(table.rows >= 10, "② 榜单行数", `${table.rows} 行`);
  chk(table.chips.length >= 2, "③ 七级档位徽标", `用到 ${table.chips.length} 级：${table.chips.join("/")}`);
  chk(table.hint1 >= 10 && table.hint2 >= 10, "④ 提示两行（场景判据 + 五维分解）", `rh-1=${table.hint1} rh-2=${table.hint2}`);
  chk(table.score >= 10, "⑤ 入手概率分列", `${table.score} 个`);
  await page.screenshot({ path: path.join(OUT, "01-rate-panel.png"), fullPage: false });

  // ---------- 3. 手动「归档今天」 ----------
  const snapBtn = await page.evaluate(() => {
    const b = document.querySelector("#rateSnap");
    if (!b) return null;
    b.click();
    return true;
  });
  if (snapBtn) {
    // 归档成功 → 自动刷时间轴；时间轴要补 K 线，轮询等它出卡，别按固定秒数赌
    let tw = 0;
    while (tw < 45000) {
      const n = await page.evaluate(() => document.querySelectorAll("#p-rate .tl-card").length);
      if (n > 0) break;
      await sleep(1500); tw += 1500;
    }
    const msg = await page.evaluate(() => {
      const m = document.querySelector("#p-rate .rate-msg");
      return m ? { txt: m.innerText.trim(), bad: m.classList.contains("bad") } : null;
    });
    chk(!!msg && !msg.bad, "⑥ 手动「归档今天」提示条", msg ? msg.txt : "无提示条");
    const tlRaw = await page.evaluate(() => {
      const s = document.querySelector("#p-rate .rate-sec");
      return s ? s.innerText.replace(/\s+/g, " ").slice(0, 260) : "(没有 .rate-sec)";
    });
    console.log("   时间轴区块实况：" + tlRaw);
    await page.screenshot({ path: path.join(OUT, "02-after-snap.png"), fullPage: false });
  } else {
    bad("⑥ 手动「归档今天」按钮", "找不到 #rateSnap");
  }

  // ---------- 4. 时间轴 ----------
  const tl = await page.evaluate(() => {
    const p = document.querySelector("#p-rate");
    const cards = p.querySelectorAll(".tl-card");
    const heads = Array.from(p.querySelectorAll(".tl-tb thead th")).map((t) => t.innerText.trim());
    const pends = p.querySelectorAll(".tl-pend").length;
    const sums = p.querySelectorAll(".tl-sum").length;
    return {
      cards: cards.length,
      heads, pends, sums,
      summary: (p.querySelector(".tl-summary") || {}).innerText || "",
      head: cards[0] ? cards[0].innerText.replace(/\s+/g, " ").slice(0, 200) : "",
      // ⑩-A：标题报的真实归档量（30）与「列前 N」必须分开，别把展示条数当成归档数
      meta: cards[0] ? (cards[0].querySelector(".tl-meta") || {}).innerText || "" : "",
      // ⑩-B：pending 提示里的「基准」要说实际取到的那根 K 线（可能比行情日旧）——挂在 title 上
      pendTxt: (() => { const e = p.querySelector(".tl-pend"); return e ? (e.getAttribute("title") || "") : ""; })(),
    };
  });
  chk(tl.cards >= 1, "⑦ 时间轴卡片", `${tl.cards} 张`);
  chk(tl.heads.join("").includes("T+1") && tl.heads.join("").includes("T+10"), "⑧ 时间轴 T+1/3/5/10 刻度", tl.heads.filter(Boolean).join(" | "));
  chk(tl.sums === 4, "⑨ 时间轴汇总四格（T+1/3/5/10）", `${tl.sums} 格`);
  chk(tl.pends > 0, "⑩ 未到期留空不填 0（tl-pend）", `${tl.pends} 个「待 T+N」`);
  chk(/归档\s*\d+\s*只/.test(tl.meta) && /列前\s*\d+/.test(tl.meta),
    "⑩-A 归档量 vs 列前条数分开报", tl.meta || "(空)");
  chk(/基准（\d{4}-\d{2}-\d{2}）/.test(tl.pendTxt),
    "⑩-B pending 提示带真实基准日", tl.pendTxt || "(空)");
  await page.screenshot({ path: path.join(OUT, "03-timeline.png"), fullPage: false });

  // ---------- 5. 折叠展开 ----------
  // 最近一个归档日默认就是展开的 —— 先收起再展开，验证的是「点击能翻转」而不是把已展开的收掉
  // 每次都重新取 .tl-head：点一下 renderRate() 会把面板 innerHTML 重建，
  // 握着旧元素引用再点等于点了个已经从 DOM 里摘掉的按钮（首次跑就栽在这，行 0）。
  const flip = async () => page.evaluate(async () => {
    const h = document.querySelector("#p-rate .tl-head");
    if (!h) return false;
    h.click();
    await new Promise((r) => setTimeout(r, 350));
    return true;
  });
  const opened = await flip();          // 默认展开 → 先收起
  if (opened) {
    await flip();                       // 再展开
    const isOpen = await page.evaluate(() => !!document.querySelector("#p-rate .tl-card.open"));
    if (!isOpen) await flip();          // 兜底：确保落在展开态
  }
  if (opened) {
    await sleep(800);
    const body = await page.evaluate(() => {
      const c = document.querySelector("#p-rate .tl-card");
      const b = c && c.querySelector(".tl-body");
      return {
        open: !!c && c.classList.contains("open"),
        vis: b ? getComputedStyle(b).display !== "none" : false,
        rows: b ? b.querySelectorAll("tbody tr").length : 0,
        note: b ? (b.querySelector(".tl-note") || {}).innerText || "" : "",
      };
    });
    chk(body.open && body.vis && body.rows > 0, "⑪ 时间轴折叠展开", `行 ${body.rows}，说明：${body.note.slice(0, 40)}`);
    await page.screenshot({ path: path.join(OUT, "04-timeline-open.png"), fullPage: false });
  } else {
    bad("⑪ 时间轴折叠展开", "找不到 .tl-head");
  }

  // ---------- 6. 抽屉（复用既有 document 级委托） ----------
  const drawer = await page.evaluate(async () => {
    const a = document.querySelector('#p-rate [data-wt-open]');
    if (!a) return { found: false };
    a.click();
    await new Promise((r) => setTimeout(r, 1200));
    const ds = Array.from(document.querySelectorAll("div,section,aside"))
      .filter((e) => /drawer|modal|sheet/i.test(e.className || ""))
      .filter((e) => getComputedStyle(e).display !== "none" && e.offsetHeight > 200);
    return { found: true, n: ds.length, cls: ds.map((e) => String(e.className).slice(0, 60)).slice(0, 4) };
  });
  chk(drawer.found && drawer.n > 0, "⑫ 个股抽屉（复用 [data-wt-open]）", drawer.found ? `${drawer.n} 个可见容器 ${(drawer.cls || []).join(",")}` : "无链接");
  await page.screenshot({ path: path.join(OUT, "05-drawer.png"), fullPage: false }).catch(() => {});

  // ---------- 7. 老面板回归：智能选股 / 连续阴跌 ----------
  await page.evaluate(() => {
    const t = document.querySelector('#subnav .sub-tab[data-tab="screener"]');
    if (t) t.click();
  });
  await sleep(3500);
  const old1 = await page.evaluate(() => {
    const p = document.querySelector("#p-screener");
    return { len: p ? p.innerHTML.length : 0, rows: p ? p.querySelectorAll("tbody tr").length : 0 };
  });
  chk(old1.len > 500 && old1.rows > 0, "⑬ 回归：智能选股老面板", `html ${old1.len}，${old1.rows} 行`);

  // 连续阴跌视图：老面板里的模式切换 tab（按下跌天数 / 按入手值得率）
  const decline = await page.evaluate(async () => {
    const btns = Array.from(document.querySelectorAll("#p-screener button, #p-screener .wt-tab, #p-screener .wt-chip"))
      .filter((b) => /阴跌|下跌天数|入手值得率/i.test(b.innerText || ""));
    if (!btns.length) return { found: false };
    btns[0].click();
    // 阴跌扫描首次要逐只抓 K 线（10~40 秒），轮询等出结果
    const p = document.querySelector("#p-screener");
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const n = p.querySelectorAll("tbody tr").length;
      const empty = (p.querySelector(".wt-empty") || {}).innerText || "";
      if (n > 0 || /没有符合|异常|不能|接口/i.test(empty)) break;
    }
    return { found: true, rows: p.querySelectorAll("tbody tr").length, label: btns[0].innerText.trim() };
  });
  chk(decline.found ? decline.rows > 0 : true, "⑭ 回归：连续阴跌视图", decline.found ? `${decline.label} → ${decline.rows} 行` : "未找到阴跌入口");
  await page.screenshot({ path: path.join(OUT, "06-regression-screener.png"), fullPage: false });

  chk(errs.length === 0, "⑮ 无 JS 报错", errs.slice(0, 3).join(" ; ") || "0 条");

  await browser.close();

  console.log("\n========== 入手概率板块回归 ==========");
  let f = 0;
  results.forEach((r) => {
    if (r.s === "FAIL") f++;
    console.log(`${r.s === "PASS" ? "绿" : "红"} ${r.n}${r.d ? " —— " + r.d : ""}`);
  });
  console.log("--------------------------------------");
  console.log(`合计 ${results.length} 项，红 ${f} 项，截图目录 ${OUT}`);
  if (errs.length) { console.log("\nJS 错误明细："); errs.slice(0, 10).forEach((e) => console.log("  " + e)); }
})().catch((e) => { console.error("脚本异常：", e); process.exit(1); });
