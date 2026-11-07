#!/usr/bin/env node
/* 估值诊股「最近诊过」池 + 弹窗「估值诊股」按钮 无头回归（2026-09-29）
   覆盖：① 池子容量/去重/FIFO；② 估值诊股下拉 datalist + 清空按钮；
        ③ 弹窗按钮 E2E：openStock → 点「估值诊股」→ 切到「我的票/估值诊股」→ 自动评分 → 池子入账。
   运行：NODE_PATH=<workspace>/node_modules node verify_valpool.cjs  （需 8765 在线） */
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer-core");

const BASE = "http://127.0.0.1:8765";
const SHOT = path.join(__dirname, "artifacts", "valpool_20260929.png");

function findBrowser() {
  const cands = [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  ];
  for (const p of cands) { try { if (fs.existsSync(p)) return p; } catch (e) {} }
  return null;
}

(async () => {
  const exe = findBrowser();
  if (!exe) { console.log("NO_BROWSER"); process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: exe, headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1500,950"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 950 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

  await page.goto(BASE + "/#watch/valuation", { waitUntil: "networkidle2", timeout: 60000 });
  await page.evaluate(() => { try { localStorage.removeItem("wb_val_pool_v1"); } catch (e) {} });
  await new Promise((r) => setTimeout(r, 800));

  // ① 池子逻辑：容量 20 / FIFO 清最旧 / 去重置顶
  const poolLogic = await page.evaluate(() => {
    for (let i = 1; i <= 25; i++) valPoolPush("6" + String(i).padStart(5, "0"), "票" + i);
    const a = valPoolGet();
    const len25 = a.length;                                  // 应为 20
    const oldestGone = !a.some((x) => x.code === "600001");  // 最旧的 1-5 应被清
    const newestKept = a[0].code === "600025";               // 置顶（6 位码，与上同规则）
    valPoolPush("600010", "票10再看");                        // 去重：已存在 → 提到最前
    const b = valPoolGet();
    const dedupTop = b[0].code === "600010" && b[0].name === "票10再看" && b.length === 20;
    valPoolClear();
    const emptyAfterClear = valPoolGet().length === 0;
    return { len25, oldestGone, newestKept, dedupTop, emptyAfterClear };
  });

  // ② 面板渲染：预置 2 条 → renderValuation() → datalist 选项 / 清空按钮
  const dlCheck = await page.evaluate(async () => {
    valPoolPush("600519", "贵州茅台");
    valPoolPush("000651", "格力电器");
    VAL.symbol = "600519"; loadValuation("600519");
    await new Promise((r) => setTimeout(r, 2500));           // 等 /api/valuation 回来
    const host = document.querySelector("#p-valuation");
    const dl = document.querySelector("#valPoolList");
    const opts = dl ? Array.from(dl.querySelectorAll("option")).map((o) => o.getAttribute("value")) : [];
    const inp = document.querySelector("#valSym");
    const clearBtn = document.querySelector("#valPoolClear");
    const clearDisabled = clearBtn ? clearBtn.disabled : null;
    let clearWorks = null;
    if (clearBtn) { clearBtn.click(); await new Promise((r) => setTimeout(r, 100)); }
    clearWorks = valPoolGet().length === 0 && !document.querySelector("#valPoolList");
    // 恢复 600519 供 ③ 用
    valPoolPush("600519", "贵州茅台");
    return {
      panelActive: !!(host && host.classList.contains("active")),
      hasListAttr: !!(inp && inp.getAttribute("list") === "valPoolList"),
      optCount: opts.length, has519: opts.includes("600519"), has651: opts.includes("000651"),
      clearEnabled: clearDisabled === false, clearWorks,
      scoreShown: !!document.querySelector("#p-valuation .feat-score"),
    };
  });

  // ③ E2E：openStock → 弹窗按钮 → 切到估值诊股并评分
  const e2e = await page.evaluate(async () => {
    openStock("000651");
    for (let i = 0; i < 40; i++) {                            // 等抽屉弹出（最多 8s）
      const d = document.querySelector("#drawer");
      if (d && d.classList.contains("show")) break;
      await new Promise((r) => setTimeout(r, 200));
    }
    const btn = document.querySelector("#btnValuation");
    if (!btn) return { ok: false, why: "btn missing" };
    if (btn.hidden) return { ok: false, why: "btn hidden" };
    btn.click();
    await new Promise((r) => setTimeout(r, 3000));            // 等 selectNav + /api/valuation
    const d = document.querySelector("#drawer");
    const active = document.querySelector(".panel.active");
    const rec = valPoolGet().find((x) => x.code === "000651");
    return {
      ok: true,
      hash: location.hash,
      drawerClosed: !(d && d.classList.contains("show")),
      activePanel: active ? active.id : null,
      valSymbol: VAL.symbol,
      poolRec: rec ? { code: rec.code, name: rec.name } : null,
      scoreShown: !!document.querySelector("#p-valuation .feat-score"),
      inDatalist: !!(document.querySelector('#valPoolList option[value="000651"]')),
    };
  });

  await page.screenshot({ path: SHOT }).catch(() => {});
  console.log("POOL=" + JSON.stringify(poolLogic));
  console.log("DL=" + JSON.stringify(dlCheck));
  console.log("E2E=" + JSON.stringify(e2e));
  console.log("JS_ERRORS=" + JSON.stringify(errors.slice(0, 5)));
  await browser.close();

  const poolOk = Object.values(poolLogic).every(Boolean);
  const dlOk = dlCheck.panelActive && dlCheck.hasListAttr && dlCheck.optCount === 2 &&
    dlCheck.has519 && dlCheck.has651 && dlCheck.clearEnabled && dlCheck.clearWorks;
  const e2eOk = e2e.ok && e2e.hash === "#watch/valuation" && e2e.drawerClosed &&
    e2e.activePanel === "p-valuation" && e2e.valSymbol === "000651" &&
    e2e.poolRec && e2e.poolRec.name && e2e.inDatalist;
  const pass = poolOk && dlOk && e2eOk && errors.length === 0;
  console.log("CHECKS=" + JSON.stringify({ poolOk, dlOk, e2eOk, noErrors: errors.length === 0 }));
  console.log(pass ? "VAL_POOL_PASS" : "VAL_POOL_FAIL");
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.log("HARNESS_FAIL", e && e.message); process.exit(1); });
