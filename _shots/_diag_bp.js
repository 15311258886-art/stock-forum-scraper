/* 2026-10-05 诊断：手填「最值得入手价」失焦后，提示条为什么不出现。
   监听 buypoint 的网络响应 + 页面错误，轮询看 .rate-msg 到底有没有内容。 */
const puppeteer = require("puppeteer-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  const log = [];
  page.on("pageerror", (e) => log.push("PAGEERR " + String(e).slice(0, 200)));
  page.on("console", (m) => { if (m.type() === "error") log.push("CONSOLE " + m.text().slice(0, 200)); });
  page.on("response", (r) => {
    if (String(r.url()).indexOf("buypoint") >= 0) {
      log.push("RESP " + r.status() + " " + r.url());
    }
  });
  page.on("requestfailed", (r) => {
    if (String(r.url()).indexOf("buypoint") >= 0) log.push("REQFAILED " + r.url() + " " + r.failure().errorText);
  });

  await page.goto("http://127.0.0.1:8765/#watch/rate", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 1200));
  await page.evaluate(() => { const n = document.querySelector('.nav-item[data-cat="watch"]'); if (n) n.click(); });
  await new Promise((r) => setTimeout(r, 500));
  await page.evaluate(() => { const t = document.querySelector('#subnav .sub-tab[data-tab="rate"]'); if (t) t.click(); });
  // 等榜单出（轮询而非固定 sleep）
  for (let i = 0; i < 40; i++) {
    const n = await page.evaluate(() => document.querySelectorAll("#p-rate tbody tr .bp-in").length);
    if (n) break;
    await new Promise((r) => setTimeout(r, 700));
  }

  const res = await page.evaluate(async () => {
    const el = document.querySelector("#p-rate tbody tr .bp-in");
    if (!el) return { err: "没有 bp-in 输入框" };
    const code = el.dataset.bpCode;
    el.value = "13.99";
    el.dispatchEvent(new Event("blur"));
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 250));
      const m = document.querySelector("#p-rate .rate-msg");
      if (m && String(m.innerText || "").trim()) {
        return { code: code, msg: m.innerText.trim(), at_ms: (i + 1) * 250, still_same_el: document.querySelector("#p-rate tbody tr .bp-in") === el };
      }
    }
    const m2 = document.querySelector("#p-rate .rate-msg");
    return { code: code, msg: m2 ? m2.innerText : "(没有 .rate-msg 元素)", html: m2 ? m2.outerHTML.slice(0, 200) : "", same: document.querySelector("#p-rate tbody tr .bp-in") === el };
  });

  console.log("RESULT " + JSON.stringify(res, null, 1));
  console.log("NETWORK/ERR:\n" + log.join("\n"));
  await browser.close();
})().catch((e) => { console.log("FATAL " + e); process.exit(1); });
