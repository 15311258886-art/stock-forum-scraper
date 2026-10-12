/* 2026-10-05 验收：入手概率表新增「最值得入手」+「跟踪（锚点→今）」两列，
   并验证换列后「当日涨跌/换手率」确实没了；外加时间轴 vs 买点 与手填买点落库。
   走 hash 直跳 + 轮询等待（固定 sleep 会误判，全项目已踩过三次）。 */
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer-core");

const OUT = path.join(__dirname, "..", "_shots_rate");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
let pass = 0, fail = 0;
const chk = (ok, name, extra) => {
  if (ok) { pass++; console.log("  ok   " + name + (extra ? " | " + extra : "")); }
  else { fail++; console.log("  FAIL " + name + (extra ? " | " + extra : "")); }
};

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e).slice(0, 140)));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text().slice(0, 140)); });
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 1 });

  await page.goto("http://127.0.0.1:8765/#watch/rate", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 1200));
  await page.evaluate(() => { const n = document.querySelector('.nav-item[data-cat="watch"]'); if (n) n.click(); });
  await new Promise((r) => setTimeout(r, 500));
  await page.evaluate(() => { const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]'); if (t) t.click(); });

  const rows = await page.evaluate(async () => {
    for (let i = 0; i < 60; i++) {
      const n = document.querySelectorAll("#p-rate tbody tr").length;
      if (n) return n;
      await new Promise((r) => setTimeout(r, 700));
    }
    return 0;
  });
  chk(rows > 0, "① 榜单渲染出来", rows + " 行");

  const th = await page.evaluate(() =>
    Array.from(document.querySelectorAll("#p-rate thead th")).map((e) => e.innerText.trim()));
  chk(th.some((t) => /最值得入手/.test(t)), "② 表头有「最值得入手」", th.join(" / "));
  chk(th.some((t) => /跟踪/.test(t)), "③ 表头有「跟踪（锚点→今）」", th.join(" / "));
  chk(!th.some((t) => /当日涨跌/.test(t)) && !th.some((t) => /换手率/.test(t)),
    "④ 旧列「当日涨跌 / 换手率」已删", th.join(" / "));

  const cells = await page.evaluate(() => Array.from(document.querySelectorAll("#p-rate tbody tr")).slice(0, 6).map((tr) => ({
    price: ((tr.children[3] || {}).innerText || "").trim(),
    bp: (tr.querySelector(".bp-in") || {}).value || "",
    bpd: ((tr.querySelector(".bp-d") || {}).innerText || "").trim(),
    bpCls: ((tr.querySelector(".bp-d") || {}).className || "").replace("bp-d ", ""),
    tk: ((tr.querySelector(".tk") || {}).innerText || "").replace(/\s+/g, " ").trim(),
    tknone: !!tr.querySelector(".tk-none"),
  })));
  chk(cells.length && cells.every((c) => c.bp !== ""), "⑤ 每行都有算法给出的买点价",
    cells.slice(0, 3).map((c) => c.price + "→" + c.bp).join(" , "));
  chk(cells.every((c) => c.bpd.length > 0), "⑥ 买点下方有距离文案",
    cells.slice(0, 3).map((c) => c.bpd).join(" ; "));
  chk(cells.every((c) => /bp-(at|near|wait)/.test(c.bpCls)), "⑦ 买点三态染色生效",
    cells.slice(0, 3).map((c) => c.bpCls).join(" , "));
  chk(cells.every((c) => !c.tknone), "⑧ 跟踪列已挂上锚点（非「未标锚」）",
    cells.slice(0, 3).map((c) => c.tk).join(" ; "));

  // 手填买点：改成 12.34 失焦 → 应提示已设。
  // 提示是 POST 回来才渲染的（实测要 3.7s），固定 sleep 1s 会误判成「没存」——这里轮询等 12s。
  // 顺手把原值填回去：测试值不能留在王总的真实库里。
  const edit = await page.evaluate(async () => {
    const el = document.querySelector("#p-rate tbody tr .bp-in");
    if (!el) return null;
    const code = el.dataset.bpCode;
    const old = el.value;
    const put = async (v) => {
      el.value = v;
      el.dispatchEvent(new Event("blur"));
      for (let i = 0; i < 48; i++) {
        await new Promise((r) => setTimeout(r, 250));
        const m = document.querySelector("#p-rate .rate-msg");
        if (m && String(m.innerText || "").trim()) return String(m.innerText).trim();
      }
      return "";
    };
    const msg = await put("12.34");
    const back = await put(old);
    return { code: code, msg: msg, restored: back };
  });
  chk(edit && /已设为/.test(edit.msg) && /12\.34/.test(edit.msg), "⑨ 手填买点失焦即存（提示异步，轮询等 12s）",
    edit ? edit.code + " → " + edit.msg + " ｜ 已还原：" + edit.restored : "没找到输入框");

  // 时间轴 vs 买点
  await page.evaluate(async () => {
    for (let i = 0; i < 60; i++) {
      if (document.querySelectorAll("#p-rate .tl-card").length) return;
      await new Promise((r) => setTimeout(r, 800));
    }
  });
  const vs = await page.evaluate(() => ({
    cards: document.querySelectorAll("#p-rate .tl-card").length,
    vs: document.querySelectorAll("#p-rate .tl-vs").length,
    sample: ((document.querySelector("#p-rate .tl-vs") || {}).innerText || "").trim(),
  }));
  chk(vs.cards > 0, "⑩ 时间轴卡片还在（老功能没被打坏）", vs.cards + " 张");
  chk(vs.vs > 0, "⑪ 时间轴有「vs 买点」小字", vs.vs + " 格 · 样例：" + vs.sample);

  // 排版：表格不能横向溢出 / 单元格不能有叠字式挤压
  const box = await page.evaluate(() => {
    const tb = document.querySelector("#p-rate .sc-dn-tb");
    if (!tb) return null;
    const wrap = tb.parentElement;
    return { tableW: Math.round(tb.getBoundingClientRect().width), wrapW: Math.round(wrap.clientWidth),
             overflow: tb.scrollWidth - wrap.clientWidth };
  });
  chk(box && box.overflow <= 2, "⑫ 榜单不横向溢出", JSON.stringify(box));

  // 截图：整表 + 买点/跟踪列特写。
  // 面板会防抖重渲染，元素句柄随时可能被甩成 detached（旧坑），统一走「重取 + 失败退整页」。
  const shot = async (sel, file) => {
    try {
      const h = await page.$(sel);
      if (!h) return false;
      await h.evaluate((el) => el.scrollIntoView({ block: "start" }));
      await new Promise((r) => setTimeout(r, 400));
      await h.screenshot({ path: path.join(OUT, file) });
      console.log("  shot " + file);
      return true;
    } catch (e) {
      await page.screenshot({ path: path.join(OUT, file.replace(/\.png$/, "-fallback.png")) });
      console.log("  (元素截图失败退整页：" + file + " —— " + String(e).slice(0, 60) + ")");
      return false;
    }
  };
  await shot("#p-rate .sc-dn-tb", "D-buy-point.png");
  await shot("#p-rate tbody tr", "E-row-closeup.png");

  chk(errs.length === 0, "⑬ 无 JS 报错", errs.slice(0, 2).join(" | ") || "0 个");
  console.log("——— " + pass + " passed / " + fail + " failed ———");
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error("ERR", e); process.exit(2); });
