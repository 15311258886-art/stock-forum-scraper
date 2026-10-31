/* 本轮验收（2026-10-30 第二轮）
   ① 表头口径：今日总览/ETF/入手概率/关注池 各面板内两张表必须同字号同底色
   ② 低位突破：18 列（原 23）、表宽 <= 容器宽（不再横向滚）、无列被压扁
   ③ 「详情」弹层：点开能出 5 个原列 + 关闭/入池按钮，无控制台报错
   用法：node verify_thead_lowbox.cjs
*/
const puppeteer = require("puppeteer-core");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8765/?skipguide=1";
const OUT = __dirname;

const headStyle = (panelId) => {
  const panel = document.getElementById(panelId);
  if (!panel) return null;
  const out = [];
  panel.querySelectorAll("table").forEach((t) => {
    const th = t.querySelector(":scope > thead > tr > th");
    if (!th) return;
    const cs = getComputedStyle(th);
    out.push({ cn: t.querySelectorAll("thead th").length, fs: cs.fontSize, bg: cs.backgroundColor, fw: cs.fontWeight, ls: cs.letterSpacing });
  });
  panel.querySelectorAll(".dtb-hd").forEach((hd) => {
    const cs = getComputedStyle(hd);
    out.push({ cn: hd.children.length, fs: cs.fontSize, bg: cs.backgroundColor, fw: cs.fontWeight, ls: cs.letterSpacing });
  });
  return out;
};

(async () => {
  const browser = await puppeteer.launch({ executablePath: EDGE, headless: "new", args: ["--no-sandbox", "--proxy-bypass-list=*"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + String(e).slice(0, 200)));
  page.on("console", (m) => { if (m.type() === "error" && !/frame-ancestors/.test(m.text())) errors.push("console: " + m.text().slice(0, 200)); });

  let ok = true;
  const A = (cond, msg) => { console.log((cond ? "PASS " : "FAIL ") + msg); if (!cond) ok = false; };

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction(() => typeof selectNav === "function", { timeout: 40000 });

  // ---------- ① 表头口径 ----------
  for (const [cat, tab, pid] of [["overview", "dashboard", "p-dashboard"], ["etf", "etf", "p-etf"], ["watch", "rate", "p-rate"], ["watch", "watch", "p-watch"]]) {
    await page.evaluate((c, t) => selectNav(c, t, false), cat, tab);
    await new Promise((r) => setTimeout(r, 2500));
    const hs = await page.evaluate(headStyle, pid);
    if (!hs || !hs.length) { console.log("SKIP " + pid + " 无表头（数据未到）"); continue; }
    const uniq = Array.from(new Set(hs.map((h) => h.fs + "/" + h.bg + "/" + h.fw + "/" + h.ls)));
    A(uniq.length === 1, pid + " 表头口径唯一（" + hs.length + " 张表 → " + uniq.length + " 种：" + uniq.join(" ; ") + "）");
  }

  // ---------- ② 低位突破 ----------
  await page.evaluate(() => selectNav("watch", "lowbox", false));
  await page.waitForFunction(() => {
    const t = document.querySelector("#p-lowbox table.lowbox-tb.lb-main");
    return t && t.querySelectorAll("thead th").length > 0;
  }, { timeout: 180000 }).catch(() => {});
  const lb = await page.evaluate(() => {
    const panel = document.getElementById("p-lowbox");
    const t = panel.querySelector("table.lowbox-tb.lb-main");
    if (!t) return { found: false };
    const wrap = t.closest(".wt-tw");
    const ths = Array.from(t.querySelectorAll("thead th")).map((th) => (th.textContent || "").trim());
    const tds = t.querySelectorAll("tbody tr td").length;
    const rows = t.querySelectorAll("tbody tr").length;
    const cols = ths.length;
    // 列宽最小值：任何一列 < 30px 视为被压扁
    const widths = Array.from(t.querySelectorAll("thead th")).map((th) => Math.round(th.getBoundingClientRect().width));
    // 内容被裁检测：单元格 scrollWidth 明显大于 clientWidth 说明装不下（字被省略号/切边吃掉）
    // 允许例外：lb-nowrap（实控人）本来就设计成省略号
    const clipped = [];
    t.querySelectorAll("tbody tr td").forEach((td, idx) => {
      if (td.classList.contains("lb-nowrap")) return;
      if (td.scrollWidth - td.clientWidth > 1) {
        clipped.push({ col: idx % cols, txt: (td.textContent || "").replace(/\s+/g, " ").trim().slice(0, 14), over: td.scrollWidth - td.clientWidth });
      }
    });
    return {
      found: true, cols, rows, ths,
      clipped: clipped.slice(0, 12), clippedTotal: clipped.length,
      tdsPerRow: rows ? tds / rows : 0,
      tableW: Math.round(t.getBoundingClientRect().width),
      wrapW: wrap.clientWidth,
      over: wrap.scrollWidth - wrap.clientWidth,
      minCol: Math.min.apply(null, widths),
      widths,
    };
  });
  A(lb.found, "低位突破表已渲染");
  if (lb.found) {
    console.log("   列头：" + lb.ths.join(" | "));
    A(lb.cols === 18, "列数 18（实际 " + lb.cols + "）");
    A(lb.tdsPerRow === 18, "每行单元格 18（实际 " + lb.tdsPerRow + "）");
    A(lb.over <= 2, "不再横向滚（表 " + lb.tableW + " / 容器 " + lb.wrapW + "，溢出 " + lb.over + "px）");
    A(lb.minCol >= 30, "最窄列 " + lb.minCol + "px（无压扁）");
    A(lb.clippedTotal === 0, "无单元格内容被裁（" + lb.clippedTotal + " 个）");
    lb.clipped.forEach((c) => console.log("   被裁 @" + c.col + " 「" + c.txt + "」超出 " + c.over + "px"));
    A(lb.ths.indexOf("PE") < 0 && lb.ths.indexOf("ROE") < 0 && lb.ths.indexOf("负债率") < 0 && lb.ths.indexOf("股息率") < 0 && lb.ths.indexOf("估值分") < 0,
      "PE/ROE/负债率/股息率/估值分 已从表头移除");
  }
  try {
    const el = await page.$("#p-lowbox");
    await page.evaluate(() => { const p = document.getElementById("p-lowbox"); const t = p.querySelector("table.lb-main"); if (t) t.scrollIntoView({ block: "center" }); });
    await new Promise((r) => setTimeout(r, 400));
    await el.screenshot({ path: path.join(OUT, "_v2_lowbox.png") });
  } catch (e) { console.log("shot lowbox fail " + String(e).slice(0, 80)); }

  // ---------- ③ 详情弹层 ----------
  const btn = await page.$("#p-lowbox [data-lb-more]");
  A(!!btn, "「详情」按钮存在");
  if (btn) {
    await btn.click();
    await new Promise((r) => setTimeout(r, 500));
    const modal = await page.evaluate(() => {
      const m = document.querySelector(".lb-modal");
      if (!m) return { found: false };
      const keys = Array.from(m.querySelectorAll(".lb-k")).map((x) => x.textContent.trim());
      const vals = Array.from(m.querySelectorAll(".lb-v")).map((x) => x.textContent.trim());
      const r = m.getBoundingClientRect();
      return {
        found: true, keys, vals,
        w: Math.round(r.width), h: Math.round(r.height),
        hasClose: !!m.querySelector("[data-lb-close]"),
        hasAdd: !!m.querySelector("[data-lb-add2]"),
        // 键值对是否重叠（同一行 k 的右边界不应超过 v 的左边界）
        overlap: (() => {
          let bad = 0;
          m.querySelectorAll(".lb-kv").forEach((kv) => {
            const k = kv.querySelector(".lb-k"), v = kv.querySelector(".lb-v");
            if (!k || !v) return;
            const kb = k.getBoundingClientRect(), vb = v.getBoundingClientRect();
            if (kb.right > vb.left + 1 && kb.bottom > vb.top && kb.top < vb.bottom) bad++;
          });
          return bad;
        })(),
      };
    });
    A(modal.found, "详情弹层已打开");
    if (modal.found) {
      console.log("   字段：" + modal.keys.join(" / "));
      A(["PE", "ROE", "负债率", "股息率", "估值分"].every((k) => modal.keys.indexOf(k) >= 0), "5 个原列都在弹层里");
      A(modal.hasClose && modal.hasAdd, "关闭 + 加入关注池 按钮齐全");
      A(modal.overlap === 0, "键值对无重叠（重叠 " + modal.overlap + " 处）");
      A(modal.w <= 700 && modal.h <= 940, "弹层尺寸合理（" + modal.w + "×" + modal.h + "）");
      const mEl = await page.$(".lb-modal");
      await mEl.screenshot({ path: path.join(OUT, "_v2_lb_modal.png") }).catch(() => {});
      await page.evaluate(() => { const b = document.querySelector(".lb-modal [data-lb-close]"); if (b) b.click(); });
      await new Promise((r) => setTimeout(r, 300));
      const gone = await page.evaluate(() => !document.querySelector(".lb-modal"));
      A(gone, "关闭按钮能正常关掉弹层");
    }
  }

  A(errors.length === 0, "无控制台/JS 报错（" + errors.length + " 条）");
  errors.slice(0, 8).forEach((e) => console.log("   " + e));
  await browser.close();
  console.log(ok ? "\n=== 全部通过 ===" : "\n=== 有失败项 ===");
  process.exit(ok ? 0 : 1);
})();
