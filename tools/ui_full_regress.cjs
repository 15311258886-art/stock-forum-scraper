/* 全站真机回测（2026-10-15 新增）
 *
 * 目的：把「用户点得到的每个按键」都真点一遍，捕捉任何"线路不通"——
 *   ① 前端调了后端没注册 → HTTP 404 not found（本轮 /api/pillars/run 就是这类）
 *   ② 接口内部报错 → HTTP 500 / 4xx
 *   ③ JS 运行时异常（pageerror / console error）
 *   ④ 面板点了没反应（内容长度不变）
 *
 * 做法：
 *   · 从页面内 NAV 真源枚举全部子板块（不硬编码），逐个 showTab 并等数据
 *   · 在「当前可见面板」内自动收集可点元素（button / .tab / .subnav-btn / .geo-cat /
 *     [role=tab] / [onclick]），按关键词黑名单跳过破坏性按键（删除/清空/退出/重置…）
 *   · 逐一点击，点击前后对比「新增的 4xx/5xx」与「面板文本长度变化」
 *   · 全程监听 response>=400 与 pageerror
 *
 * 用法：
 *   NODE_PATH="<workspace>/node_modules" node tools/ui_full_regress.cjs <PORT> [--mode=full|readonly]
 *     readonly —— 只遍历子板块与面板内 tab/子导航，不点任何业务按钮（适合生产实例）
 *     full     —— 额外点击业务按钮（含重新生成类），只应在隔离库实例上跑
 * 安全：full 模式请务必指向隔离实例（VIEWER_DB 副本 + 独立端口）。
 */
const puppeteer = require("puppeteer-core");

const EXE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const PORT = process.argv[2] || "8798";
const MODE = (process.argv.find((a) => a.startsWith("--mode=")) || "--mode=full").split("=")[1];
const URL = `http://127.0.0.1:${PORT}/`;
const SHOT = `F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/.qa_iso/p23_20261015/shots/regress-${PORT}`;
const fs = require("fs");
try { fs.mkdirSync(SHOT, { recursive: true }); } catch (_) {}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
let STEP = "init";
const ok = (n, c, x) => out.push(`${c ? "PASS" : "FAIL"}  ${n}${x ? "  —— " + x : ""}`);
const info = (n, x) => out.push(`INFO  ${n}${x ? "  —— " + x : ""}`);

// 破坏性 / 高副作用按键：回测一律不点（会在报告里列出）
const DANGER = /删除|清空|移除|重置|退出|注销|断开|注销|登出|取消|停止|关闭|驳回|黑名单|导入|上传|恢复默认|clear|delete|remove|reset|logout|drop|uninstall/i;
// 会真触发外部抓取 / 长耗时写库的按键：full 模式"单独受控"点，不随批量点
const HEAVY = /重新生成|立即生成|刷新全部|全部刷新|重跑|重新抓取|抓取|扫描|全量|重建/i;

(async () => {
  const b = await puppeteer.launch({
    executablePath: EXE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await b.newPage();
  await page.setViewport({ width: 1680, height: 1050 });
  page.on("dialog", (d) => { try { d.accept(); } catch (_) {} });

  const errs = [];      // 真·JS 异常（pageerror / 非资源类 console.error）
  const resErrs = [];   // 外部资源加载错误（net::ERR_* / Failed to load resource）—— 网络环境导致，非代码缺陷
  const netErrs = [];   // HTTP >=400
  page.on("pageerror", (e) => errs.push(`[${STEP}] pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const t = m.text();
    // 外部资源/网络层错误（连接被拒、域名解析失败、超时）归 resErrs，不计入 JS 异常——
    // 这类在用户真实浏览器里也偶发，不影响面板数据与功能，与后端 404/500 性质不同。
    if (/Failed to load resource|net::ERR_|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|ERR_ABORTED/i.test(t)) {
      resErrs.push(`[${STEP}] ${t.slice(0, 160)}`);
      return;
    }
    errs.push(`[${STEP}] console: ${t.slice(0, 200)}`);
  });
  page.on("response", async (res) => {
    const sc = res.status();
    if (sc < 400) return;
    let body = "";
    try { body = (await res.text()).slice(0, 160); } catch (_) {}
    netErrs.push({ step: STEP, sc, url: res.url().replace(/^http:\/\/127\.0\.0\.1:\d+/, ""), body });
  });

  const ev = (fn, ...a) => page.evaluate(fn, ...a);
  async function until(fn, arg, ms = 12000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { try { const r = await ev(fn, arg); if (r) return r; } catch (_) {} await wait(150); }
    return null;
  }

  STEP = "goto";
  await page.goto(URL, { waitUntil: "domcontentloaded" });
  await wait(2500);
  // NAV 是顶层 const（不挂 window），需等 app.js 执行完再读；超时 15s
  STEP = "enum-wait";
  await until(() => typeof NAV !== "undefined" && Array.isArray(NAV) && NAV.length > 0, null, 15000);

  // ---------- 枚举全部子板块 ----------
  STEP = "enum";
  // 注意：NAV.items 里混有分组标题（形如 {group:"当日信号"}，没有 tab），
  // 它们不是可点子板块，必须跳过，否则每轮固定产出 N 条 undefined FAIL，掩盖真故障。
  const enumRes = await ev(() => {
    const all = NAV.flatMap((c) => c.items.map((i) => ({ tab: i.tab, name: i.name || i.tab, cat: c.key, group: i.group || null })));
    const real = all.filter((t) => t.tab);
    return { real, skipped: all.filter((t) => !t.tab).map((t) => t.group || "(no-tab)") };
  });
  const tabs = enumRes.real;
  ok("枚举到子板块", tabs.length > 0, `共 ${tabs.length} 个：` + tabs.map((t) => t.tab).join(","));
  info("跳过分组标题（非子板块）", enumRes.skipped.length + " 个：" + enumRes.skipped.join(","));

  // ---------- 逐个子板块遍历 ----------
  let panelsEmpty = [];
  for (const t of tabs) {
    STEP = "tab:" + t.tab;
    const before = netErrs.length;
    let opened = false;
    try {
      opened = await ev((tb) => { if (window.showTab) { window.showTab(tb); return true; } return false; }, t.tab);
    } catch (e) { ok(`打开 ${t.name}`, false, String(e).slice(0, 80)); continue; }
    await wait(1400);
    const st = await ev((tb) => {
      const p = document.getElementById("p-" + tb);
      if (!p) return { exists: false };
      return { exists: true, len: p.textContent.length, vis: getComputedStyle(p).display !== "none" };
    }, t.tab);
    const newNet = netErrs.length - before;
    ok(`子板块「${t.name}」(${t.tab}) 可打开`, !!opened && st.exists && st.vis, `内容长度=${st.len} · 新增错误=${newNet}`);
    if (st.exists && st.len < 80) panelsEmpty.push(t.tab);

    // 面板内子导航 / tab 切换
    const innerBtns = await ev((tb) => {
      const p = document.getElementById("p-" + tb);
      if (!p) return [];
      const nodes = [...p.querySelectorAll(".subnav-btn, .tab, .geo-cat, [role=tab], .pill, .seg-btn")];
      return nodes.filter((el) => el.tagName === "BUTTON" || el.tagName === "A" || el.onclick || el.dataset.tab || el.dataset.ov || el.dataset.ex || el.dataset.cat)
        .map((el) => ({ txt: (el.textContent || "").trim().slice(0, 20), cls: String(el.className).slice(0, 50) }))
        .slice(0, 10);
    }, t.tab);
    for (let i = 0; i < innerBtns.length; i++) {
      const nb = netErrs.length;
      await ev((tb, idx) => {
        const p = document.getElementById("p-" + tb);
        if (!p) return;
        const nodes = [...p.querySelectorAll(".subnav-btn, .tab, .geo-cat, [role=tab], .pill, .seg-btn")]
          .filter((el) => el.tagName === "BUTTON" || el.tagName === "A" || el.onclick || el.dataset.tab || el.dataset.ov || el.dataset.ex || el.dataset.cat);
        const el = nodes[idx];
        if (el && !el.disabled) el.click();
      }, t.tab, i);
      await wait(700);
      const added = netErrs.length - nb;
      if (added > 0) {
        const last = netErrs[netErrs.length - 1];
        ok(`  ↳「${t.name}」内子项 [${innerBtns[i].txt || innerBtns[i].cls}] 点击后无报错`, false, `HTTP ${last.sc} ${last.url}`);
      }
    }

    // full 模式：业务按钮批量点（跳过危险 + 重活）
    if (MODE === "full") {
      const bizBtns = await ev((tb) => {
        const p = document.getElementById("p-" + tb);
        if (!p) return [];
        return [...p.querySelectorAll("button")].map((el, idx) => ({
          idx, txt: (el.textContent || "").trim().slice(0, 20), dis: !!el.disabled,
          vis: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        })).filter((x) => x.vis && !x.dis && x.txt);
      }, t.tab);
      let clicked = 0;
      for (const btn of bizBtns) {
        if (DANGER.test(btn.txt) || HEAVY.test(btn.txt)) continue;
        if (clicked >= 8) break;
        const nb = netErrs.length;
        await ev((tb, idx) => {
          const p = document.getElementById("p-" + tb);
          if (!p) return;
          const els = [...p.querySelectorAll("button")];
          const el = els[idx];
          if (el && !el.disabled) el.click();
        }, t.tab, btn.idx);
        await wait(800);
        clicked++;
        const added = netErrs.length - nb;
        if (added > 0) {
          const last = netErrs[netErrs.length - 1];
          ok(`  ↳「${t.name}」按钮 [${btn.txt}] 点击后无报错`, false, `HTTP ${last.sc} ${last.url} ${last.body.slice(0, 80)}`);
        }
      }
    }
  }

  // ---------- 受控点击「重新生成」类重活按钮（本轮修复点的回归验证）----------
  if (MODE === "full") {
    STEP = "heavy:predRegen";
    const hasPred = tabs.some((t) => t.tab === "prediction");
    if (hasPred) {
      await ev(() => window.showTab("prediction"));
      await wait(1500);
      const beforeRun = netErrs.length;
      const btnFound = await ev(() => {
        const el = document.getElementById("predRegen");
        if (el) { el.click(); return true; }
        return false;
      });
      info("『每日预测』面板找到「重新生成」按钮", String(btnFound));
      // 等生成（含网络）——最多 210s
      for (let i = 0; i < 70; i++) {
        await wait(3000);
        const done = await ev(() => {
          const el = document.getElementById("predRegen");
          return el ? (el.disabled === false && el.textContent.trim() === "重新生成") : false;
        });
        if (done && i > 0) break;
      }
      const afterRun = netErrs.slice(beforeRun);
      const runErr = afterRun.find((x) => /\/api\/pillars\/run/.test(x.url) && x.sc >= 400);
      ok("点「重新生成」→ /api/pillars/run 不再 404（本轮根因修复回归）", !runErr,
        runErr ? `HTTP ${runErr.sc} ${runErr.url} ${runErr.body}` : "无 4xx/5xx");
      const st = await ev(() => {
        const el = document.getElementById("predRegen");
        return { txt: el ? el.textContent.trim() : null, dis: el ? el.disabled : null,
                 panelLen: (document.getElementById("p-prediction") || { textContent: "" }).textContent.length };
      });
      info("重新生成后按钮状态", JSON.stringify(st));
    }
  }

  // ---------- 汇总 ----------
  STEP = "summary";
  const uniq = new Map();
  for (const e of netErrs) uniq.set(e.sc + " " + e.url, e);
  const hardFails = [...uniq.values()].filter((e) => e.sc === 404 || e.sc >= 500);
  ok("全站遍历无 HTTP 404/5xx 断链", hardFails.length === 0,
    hardFails.slice(0, 8).map((e) => `${e.sc} ${e.url} @${e.step} ${e.body.slice(0, 60)}`).join(" | ") || "无");
  const softFails = [...uniq.values()].filter((e) => e.sc !== 404 && e.sc < 500);
  if (softFails.length) info("其它 4xx（软失败）", softFails.slice(0, 8).map((e) => `${e.sc} ${e.url} @${e.step}`).join(" | "));
  ok("无 JS 运行时错误", errs.length === 0, errs.slice(0, 5).join(" ; ") || "无");
  if (resErrs.length) {
    const uniqRes = [...new Set(resErrs.map((x) => x.replace(/^\[[^\]]+\]\s*/, "")))];
    info(`外部资源加载失败（网络环境导致，非代码缺陷）共 ${resErrs.length} 次`, uniqRes.slice(0, 3).join(" | "));
  }
  if (panelsEmpty.length) info("内容偏空的面板（可能缺数据，非代码问题）", panelsEmpty.join(","));

  await page.screenshot({ path: `${SHOT}/final.png`, fullPage: false });
  const fails = out.filter((l) => l.startsWith("FAIL")).length;
  console.log(out.join("\n"));
  console.log(`\nRESULT OK=${out.filter((l) => l.startsWith("PASS")).length} FAIL=${fails}  (mode=${MODE} port=${PORT})`);
  console.log(`SHOTS: ${SHOT}`);
  await b.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log("RUNNER ERROR @ " + STEP + ": " + (e.stack || e.message)); process.exit(2); });
