/* 全站排版体检 + 本次修复回归（2026-10-30）
   目的：① 断言「信号战绩 / 分市况」两表真的变成 grid（不再竖排、右侧不再空）；
        ② 全站 40 个面板扫一遍同类问题：
           A 类硬 bug：inline style 写了 grid-template 但 computed display 不是 grid（会塌成竖排）
           B 类疑似：面板右侧大面积留白（文本元素右边界 < 面板宽 55% 且面板够高）
   用法：node verify_layout_scan.cjs  → 输出 JSON + 截图
*/
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8765/?skipguide=1";
const OUT = __dirname;

const scanFn = () => {
  const out = { gridBug: [], rightBlank: [], tableOverflow: [], warn: [] };
  // ---------- A 类：inline grid-template 但 display 不是 grid ----------
  const all = document.querySelectorAll("[style]");
  const seen = new Set();
  all.forEach((el) => {
    const st = el.getAttribute("style") || "";
    if (st.indexOf("grid-template") < 0) return;
    const d = getComputedStyle(el).display;
    if (d.indexOf("grid") < 0) {
      if (seen.has(el)) return;
      seen.add(el);
      out.gridBug.push({
        tag: el.tagName,
        cls: el.className && el.className.toString().slice(0, 60),
        display: d,
        style: st.slice(0, 120),
        text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60),
      });
    }
  });
  // ---------- C 类：表格比容器还宽（横向溢出）----------
  // 2026-10-30 新增：关注池「战法信号」列曾被 white-space:nowrap 顶到 1612px，
  // 同一面板换个分组表格宽就从 1274 跳到 2428 —— 用户看到的是"两张完全不同的表"。
  // 判据：表所在滚动容器 scrollWidth > clientWidth + 4。lowbox 是 23 列，横向滚动属设计如此，白名单跳过。
  document.querySelectorAll(".panel").forEach((panel) => {
    if (panel.offsetParent === null) return;
    panel.querySelectorAll("table").forEach((t) => {
      const wrap = t.closest(".wt-tw") || t.parentElement;
      if (!wrap) return;
      const over = wrap.scrollWidth - wrap.clientWidth;
      if (over <= 4) return;
      const cls = (t.className || "").toString();
      // 2026-10-30 第二轮：低位突破已砍到 18 列并改固定列宽，不再需要白名单，这里恢复真检查
      const ths = [].slice.call(t.querySelectorAll("thead th")).map((th) => ({
        txt: (th.textContent || "").trim().slice(0, 10),
        w: Math.round(th.getBoundingClientRect().width),
      }));
      const widest = ths.sort((x, y) => y.w - x.w)[0] || {};
      out.tableOverflow.push({ panel: panel.id, over: Math.round(over), cols: ths.length, widest: widest, cls: cls.slice(0, 40) });
    });
  });

  // ---------- B 类：面板右侧大面积留白 ----------
  document.querySelectorAll(".panel").forEach((panel) => {
    if (panel.offsetParent === null) return;          // 只看当前可见面板
    const pr = panel.getBoundingClientRect();
    if (pr.width < 700 || pr.height < 260) return;
    let maxRight = 0;
    panel.querySelectorAll("*").forEach((el) => {
      if (el.children.length) return;                 // 只取文本叶子
      const t = (el.textContent || "").trim();
      if (!t) return;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      if (r.right > maxRight) maxRight = r.right;
    });
    if (!maxRight) return;
    const used = (maxRight - pr.left) / pr.width;      // 内容实际用掉的比例
    if (used < 0.5) {
      out.rightBlank.push({
        panel: panel.id,
        panelWidth: Math.round(pr.width),
        contentWidth: Math.round(maxRight - pr.left),
        usedRatio: Number(used.toFixed(2)),
        height: Math.round(pr.height),
      });
    }
  });
  return out;
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--proxy-bypass-list=*"],
  });
  const res = { ts: new Date().toISOString(), pages: [], errors: [], asserts: [] };
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 980 });
  page.on("pageerror", (e) => res.errors.push("pageerror: " + String(e).slice(0, 200)));
  page.on("console", (m) => {
    if (m.type() === "error") res.errors.push("console: " + m.text().slice(0, 200));
  });

  let ok = true;
  const assert = (cond, msg) => {
    res.asserts.push((cond ? "PASS " : "FAIL ") + msg);
    if (!cond) ok = false;
  };

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction(() => typeof selectNav === "function", { timeout: 40000 });

  // ===== 0. 扫描器反向自检：故意造一个"漏 display 的 grid"，必须被抓到 =====
  const selfTest = await page.evaluate(() => {
    const d = document.createElement("div");
    d.id = "_selftest_grid";
    d.setAttribute("style", "grid-template-columns:1fr 1fr;gap:6px");
    d.textContent = "selftest";
    document.body.appendChild(d);
    const hit = getComputedStyle(d).display !== "grid";
    return hit;
  });
  assert(selfTest, "体检器自检：故意漏 display 的 grid 能被识别（否则是假绿灯）");
  await page.evaluate(() => {
    const d = document.getElementById("_selftest_grid");
    if (d) d.remove();
  });

  // ===== 1. 本次修复的两个表：先把它跑出来 =====
  await page.evaluate(() => selectNav("overview", "prediction", false));
  await page.waitForFunction(() => {
    const p = document.getElementById("sig-ev-body");
    return p && p.querySelector(".dtb");
  }, { timeout: 120000 }).catch(() => {});

  const tbl = await page.evaluate(() => {
    const p = document.getElementById("sig-ev-body");
    if (!p) return null;
    const dtb = p.querySelector(".dtb");
    if (!dtb) return { found: false };
    const hd = dtb.querySelector(".dtb-hd");
    const row = dtb.querySelector(".dtb-row");
    const pr = dtb.getBoundingClientRect();
    const rr = row ? row.getBoundingClientRect() : null;
    const cells = row ? Array.from(row.children).map((c) => {
      const r = c.getBoundingClientRect();
      return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), t: c.textContent.trim().slice(0, 12) };
    }) : [];
    return {
      found: true,
      tables: document.querySelectorAll("#p-prediction .dtb").length,
      caps: Array.from(document.querySelectorAll("#p-prediction .dtb-cap")).map((x) => x.textContent.trim().slice(0, 30)),
      hdDisplay: hd ? getComputedStyle(hd).display : null,
      rowDisplay: row ? getComputedStyle(row).display : null,
      cols: hd ? getComputedStyle(hd).gridTemplateColumns : null,
      dtbW: Math.round(pr.width),
      rowW: rr ? Math.round(rr.width) : 0,
      rowH: rr ? Math.round(rr.height) : 0,
      cells,
      rowsInFirst: dtb.querySelectorAll(".dtb-row").length,
    };
  });
  assert(!!tbl && tbl.found, "信号战绩表已渲染（.dtb 存在）");
  if (tbl && tbl.found) {
    assert(tbl.hdDisplay === "grid", "表头 display=grid（实际 " + tbl.hdDisplay + "）");
    assert(tbl.rowDisplay === "grid", "数据行 display=grid（实际 " + tbl.rowDisplay + "）");
    assert(tbl.rowW >= tbl.dtbW - 4, "数据行铺满容器宽（行 " + tbl.rowW + " / 表 " + tbl.dtbW + "）");
    assert(tbl.rowH <= 60, "单行高度正常（" + tbl.rowH + "px，竖排会 >150）");
    // 同一行 7 个格子的 y 应基本一致（同一水平线）
    const ys = tbl.cells.map((c) => c.y);
    assert(ys.length === 7 && Math.max(...ys) - Math.min(...ys) <= 4,
      "信号战绩 7 列在同一行（列数 " + ys.length + "，y 差 " + (ys.length ? Math.max(...ys) - Math.min(...ys) : "-") + "px）");
    assert(tbl.tables >= 2, "买入段 + 分市况段都成了表（" + tbl.tables + " 个 .dtb）");
    console.log("   表标题：" + JSON.stringify(tbl.caps));
  }

  // 截图（元素句柄截图，clip 在本项目会崩）
  try {
    const el = await page.$("#p-prediction");
    await page.evaluate(() => {
      const p = document.getElementById("p-prediction");
      const s = p.querySelector(".dtb-wrap");
      if (s) s.scrollIntoView({ block: "start" });
      window.scrollBy(0, -110);
    });
    await new Promise((r) => setTimeout(r, 500));
    await el.screenshot({ path: path.join(OUT, "_layout_sig_table.png") });
  } catch (e) {
    res.errors.push("shot: " + String(e).slice(0, 150));
  }

  // ===== 2. 全站逐面板扫描 =====
  const nav = await page.evaluate(() =>
    NAV.map((c) => ({ key: c.key, label: c.label, items: (c.items || []).map((i) => ({ tab: i.tab, label: i.label })) }))
  );
  const total = nav.reduce((n, c) => n + c.items.length, 0);
  let done = 0;
  for (const cat of nav) {
    for (const it of cat.items) {
      try {
        await page.evaluate((k, t) => selectNav(k, t, false), cat.key, it.tab);
      } catch (e) {
        res.errors.push("selectNav " + cat.key + "/" + it.tab + ": " + String(e).slice(0, 120));
        continue;
      }
      await new Promise((r) => setTimeout(r, 1100));
      let r = null;
      try {
        r = await page.evaluate(scanFn);
      } catch (e) {
        res.errors.push("scan " + it.tab + ": " + String(e).slice(0, 120));
        continue;
      }
      res.pages.push({ cat: cat.label, tab: it.tab, label: it.label, gridBug: r.gridBug, rightBlank: r.rightBlank, tableOverflow: r.tableOverflow });
      done++;
    }
  }
  res.scanned = done;
  res.total = total;
  res.ok = ok;
  res.gridBugTotal = res.pages.reduce((n, p) => n + p.gridBug.length, 0);
  res.rightBlankTotal = res.pages.reduce((n, p) => n + p.rightBlank.length, 0);
  res.tableOverflowTotal = res.pages.reduce((n, p) => n + (p.tableOverflow || []).length, 0);

  fs.writeFileSync(path.join(OUT, "_layout_scan.json"), JSON.stringify(res, null, 2), "utf8");
  console.log("=== 断言 ===");
  res.asserts.forEach((a) => console.log(a));
  console.log("=== 扫了 " + done + "/" + total + " 个面板 ===");
  console.log("A类 grid 塌陷（真 bug）：" + res.gridBugTotal);
  res.pages.forEach((p) => {
    p.gridBug.forEach((b) => console.log("  [" + p.tab + "] " + b.tag + "." + b.cls + " display=" + b.display + " :: " + b.text));
  });
  console.log("B类 右侧留白（疑似）：" + res.rightBlankTotal);
  res.pages.forEach((p) => {
    p.rightBlank.forEach((b) => console.log("  [" + p.tab + "] " + b.panel + " 用了 " + Math.round(b.usedRatio * 100) + "% 宽（" + b.contentWidth + "/" + b.panelWidth + "px, 高 " + b.height + "）"));
  });
  console.log("C类 表格横向溢出（同一面板列宽会随数据跳）：" + res.tableOverflowTotal);
  res.pages.forEach((p) => {
    (p.tableOverflow || []).forEach((b) => console.log("  [" + p.tab + "] 溢出 " + b.over + "px，共 " + b.cols + " 列，最宽列=「" + b.widest.txt + "」" + b.widest.w + "px"));
  });
  console.log("=== 控制台错误 " + res.errors.length + " 条 ===");
  res.errors.slice(0, 12).forEach((e) => console.log("  " + e));
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
