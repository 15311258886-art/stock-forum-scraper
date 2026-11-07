/* 2026-10-01 验证「只显示最近 N 天」收起/展开：
   默认 7 天、超阈值的组收起成一行；点组头/全部展开/全部收起；切 3/30/全部。 */
const puppeteer = require("puppeteer-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const URL = "http://127.0.0.1:8765/";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const errs = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1560, height: 1000 });
    page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
    page.on("pageerror", (e) => errs.push("PAGEERROR: " + e.message));
    await page.goto(URL + "?v=fold" + Date.now(), { waitUntil: "networkidle2", timeout: 60000 });
    await page.evaluate(() => { location.hash = "#opinion/blogger"; });
    await sleep(3000);

    const snap = () => page.evaluate(() => {
      const host = document.querySelector("#p-blogger");
      return {
        daysChips: [...host.querySelectorAll(".bt-day")].map((a) => a.textContent.trim() + (a.classList.contains("active") ? "*" : "")),
        groups: host.querySelectorAll(".bt-dgrp").length,
        folded: host.querySelectorAll(".bt-dgrp-fold").length,
        cards: host.querySelectorAll(".bt-card").length,
        cnt: (host.querySelector(".bt-cnt") || {}).innerText || "",
        foldBtn: host.querySelectorAll("[data-bt-fold]").length,
        unfoldAll: !!host.querySelector("[data-bt-unfold]"),
        foldAll: !!host.querySelector("[data-bt-foldall]"),
        firstGroups: [...host.querySelectorAll(".bt-dgrp")].slice(0, 4).map((g) => g.innerText.replace(/\s+/g, " ").slice(0, 60)),
      };
    });

    const a = await snap();
    console.log("[1] 默认(7天):", JSON.stringify(a, null, 1));

    // 点第一个收起组头
    let ok = false;
    if (a.foldBtn) {
      await page.evaluate(() => document.querySelector("#p-blogger [data-bt-fold]").click());
      await sleep(700);
      const b = await snap();
      ok = b.cards > a.cards && b.folded === a.folded - 1;
      console.log("[2] 点组头展开:", "cards", a.cards, "->", b.cards, "folded", a.folded, "->", b.folded, ok ? "PASS" : "FAIL");
      // 全部展开
      if (b.unfoldAll) {
        await page.evaluate(() => document.querySelector("#p-blogger [data-bt-unfold]").click());
        await sleep(800);
        const c = await snap();
        console.log("[3] 全部展开:", "folded", c.folded, "foldAll存在", c.foldAll, (c.folded === 0 && c.foldAll) ? "PASS" : "FAIL");
        // 全部收起
        await page.evaluate(() => document.querySelector("#p-blogger [data-bt-foldall]").click());
        await sleep(800);
        const d = await snap();
        console.log("[4] 全部收起:", "folded", d.folded, "cards", d.cards, (d.folded === a.folded && d.cards === a.cards) ? "PASS" : "FAIL");
      } else console.log("[3][4] 无全部展开按钮 FAIL");
    } else console.log("[2] 无收起组头（可能无超 7 天数据）FAIL");

    // 切档
    for (const [v, label] of [[3, "3天"], [30, "30天"], [0, "全部"]]) {
      await page.evaluate((v) => document.querySelector(`#p-blogger [data-bt-days="${v}"]`).click(), v);
      await sleep(700);
      const s = await snap();
      console.log(`[5] 切到 ${label}:`, "groups", s.groups, "folded", s.folded, "cards", s.cards,
        "active", s.daysChips.filter((x) => x.endsWith("*")), (s.daysChips.includes(label + "*")) ? "PASS" : "FAIL");
    }
    // 回到 7 天，切到涨幅排序（days 行应消失）
    await page.evaluate(() => document.querySelector('#p-blogger [data-bt-days="7"]').click());
    await sleep(500);
    await page.evaluate(() => document.querySelector('#p-blogger [data-bt-sort="ret"]').click());
    await sleep(700);
    const g = await snap();
    console.log("[6] 切涨幅排序: days行存在", g.daysChips.length > 0 ? "FAIL(应隐藏)" : "PASS(hidden)");

    await page.evaluate(() => { location.hash = "#opinion/blogger"; });
    await sleep(1200);
    await page.evaluate(() => document.querySelector('#p-blogger [data-bt-sort="date"]').click());
    await sleep(800);
    await page.screenshot({ path: "shot_blogfold.png", fullPage: false });
    console.log("截图 shot_blogfold.png");
    console.log("[7] 控制台错误:", errs.length ? errs.slice(0, 5) : "无");
  } catch (e) {
    console.log("ERROR:", e.message);
  } finally {
    await browser.close();
  }
})();
