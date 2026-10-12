/* 空态引导自测（2026-10-05，报告建议7）
   1) 时间轴空态：区分「正在生成全市场归档（约1分钟）」vs「压根没归档」，前者自动重试≤3次、间隔8s；
   2) 冷启动（没发起过任务）不刷接口空转；有归档 / 重试用尽 → 停止轮询；
   3) 每日预测过期角标：用后端 stale_reason 渲染「数据截至 MM-DD」。
   做法：从 app.js 抠出相关函数体，用 new Function 注入 stub（不引浏览器环境、零依赖）。
   坑：V8 会把 `(async function(){ function f(){ await x } })()` 里的 await 判到 async 之外，
   所以三个函数一律剥掉函数壳、body 直接塞进同一个 async function 里。 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname);
const src = fs.readFileSync(path.join(ROOT, "viewer", "static", "app.js"), "utf8");

function grab(full) {                       // 完整函数源码（到顶格的 } 为止）
  const i = src.indexOf("function " + full + "(");
  if (i < 0) throw new Error("app.js 里找不到函数 " + full);
  const j = src.indexOf("\n}", i);
  return src.slice(i, j + 2);
}
function bodyOf(full) {                     // 剥掉函数壳，只要花括号里那段
  const s = grab(full);
  const i = s.indexOf("{");
  const e = s.lastIndexOf("}");
  return (e > i ? s.slice(i + 1, e) : s.slice(i + 1)).trim();
}

/* 三个函数各包一层独立函数壳 —— 不拼成一个大块：它们内部有同名局部变量（tl / st），
   同作用域会重复声明；而且带 await 的那层必须直接是 async 壳（V8 会把
   `async function(){ function f(){ await x } }` 里的 await 判到 async 之外）。 */
const STUBS = ["FEAT", "api", "renderRate", "esc", "escAttr", "apiErrText", "setTimeout", "clearTimeout",
  "RATE_TTL", "loadRateTimeline", "rateDayCard"];
const factoryOf = (name, args, isAsync) => new Function(...STUBS,
  "var __f = " + (isAsync ? "async " : "") + "function " + name + args + "{\n" + bodyOf(name) +
  "\n}; return { " + name + ": __f };\n");
const factoryBlock = factoryOf("rateTimelineBlock", "(st)", false);
const factoryMark = factoryOf("rateMarkBusy", "(ms)", false);
const factoryTl = factoryOf("loadRateTimeline", "()", true);

// ---- stub ----
let renderCalls = 0;
const timers = [];
function stubArgs(FEAT, selfRef) {
  const api = async () => FEAT.rate.tl;
  const renderRate = () => { renderCalls += 1; };
  const esc = (s) => String(s == null ? "" : s);
  const t = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  return [FEAT, api, renderRate, esc, esc, () => "err", t, () => {}, [1, 3, 5, 10], selfRef,
    () => '<div class="rate-sec">归档日卡</div>'];   // rateDayCard：只占位，不影响空态文案判定
}
const mkFEAT = (tlResult, busy) => ({ rate: {
  data: null, loading: false, error: null, tl: tlResult, tlLoading: false, snap: null,
  days: 30, limit: 30, tlWindow: 10, tlPer: 5, open: {}, msg: "", msgBad: false,
  tlBusyUntil: busy ? Date.now() + 120000 : 0, tlPoll: 0, tlPollMax: 3, tlPollTimer: null } });
function build(tlResult, busy) {
  const FEAT = mkFEAT(tlResult, busy);
  renderCalls = 0; timers.length = 0;
  const selfRef = () => timers.push({ fn: "self-ref", ms: 0 });
  const api2 = factoryTl(...stubArgs(FEAT, selfRef));
  return { FEAT, api2, timers };
}

let pass = 0, fail = 0;
function ck(name, cond, extra) {
  if (cond) { pass += 1; console.log("[ok] " + name); }
  else { fail += 1; console.log("[FAIL] " + name + (extra ? " → " + extra : "")); }
}

(async () => {
  // case1 冷启动（没发起过任务）+ 没归档 → 仍是冷提示，不轮询
  {
    const e = build({ ok: true, has_snap: false, reason: "还没有任何归档" }, false);
    const h = factoryBlock(...stubArgs(e.FEAT, () => {}));
    const html = h.rateTimelineBlock(e.FEAT.rate);
    ck("case1 冷启动显示归档起点提示", /归档是时间轴的起点/.test(html), html);
    ck("case1 冷启动文案不含「正在生成」", !/正在生成全市场归档/.test(html));
    await e.api2.loadRateTimeline();
    ck("case1 冷启动不排轮询定时器", e.timers.length === 0);
    ck("case1 冷启动 tlPoll 保持 0", e.FEAT.rate.tlPoll === 0);
  }
  // case2 刚跑过榜 + 还没归档 → 「正在生成…（1/3）」+ 排一个 8s 定时器
  {
    const e = build({ ok: true, has_snap: false, reason: "还没有任何归档" }, true);
    const h = factoryBlock(...stubArgs(e.FEAT, () => {}));
    ck("case2 未重试时先显示冷提示", /归档是时间轴的起点/.test(h.rateTimelineBlock(e.FEAT.rate)));
    await e.api2.loadRateTimeline();
    const html = h.rateTimelineBlock(e.FEAT.rate);
    ck("case2 重试后显示「正在生成全市场归档（约 1 分钟）」", /正在生成全市场归档（约 1 分钟）/.test(html), html);
    ck("case2 提示可稍后手动刷新", /可稍后手动点「刷新时间轴」/.test(html));
    ck("case2 显示已刷新 1/3", /1\/3/.test(html));
    ck("case2 tlPoll=1", e.FEAT.rate.tlPoll === 1, String(e.FEAT.rate.tlPoll));
    ck("case2 排了一个 8000ms 定时器", e.timers.length === 1 && e.timers[0].ms === 8000,
      JSON.stringify(e.timers.map((t) => t.ms)));
    ck("case2 渲染落了版", renderCalls >= 1);
  }
  // case3 连续重试：1 → 2 → 3，第 4 次不再加（上限 3）
  {
    const e = build({ ok: true, has_snap: false, reason: "还没有任何归档" }, true);
    const h = factoryBlock(...stubArgs(e.FEAT, () => {}));
    for (let i = 1; i <= 4; i++) {
      await e.api2.loadRateTimeline();
      if (i <= 3) ck("case3 第" + i + "次重试 tlPoll=" + i, e.FEAT.rate.tlPoll === i, String(e.FEAT.rate.tlPoll));
      else ck("case3 第4次不再轮询（封顶3）", e.FEAT.rate.tlPoll === 3, String(e.FEAT.rate.tlPoll));
    }
    ck("case3 重试上限显示 3/3", /3\/3/.test(h.rateTimelineBlock(e.FEAT.rate)));
  }
  // case4 有归档时不轮询、不误报
  {
    const e = build({ ok: true, has_snap: true, days: [{ snap_date: "2026-10-05" }] }, true);
    const h = factoryBlock(...stubArgs(e.FEAT, () => {}));
    await e.api2.loadRateTimeline();
    ck("case4 有归档不断轮询", e.timers.length === 0);
    ck("case4 tlPoll 归零", e.FEAT.rate.tlPoll === 0);
    ck("case4 展开最近归档日", e.FEAT.rate.open["2026-10-05"] === true);
    const html = h.rateTimelineBlock(e.FEAT.rate);
    ck("case4 正常渲染时间轴卡", /rate-sec/.test(html) && !/正在生成/.test(html));
  }
  // case5 busy 窗口过期（>2 分钟）= 早跑完了 → 不再重试
  {
    const e = build({ ok: true, has_snap: false, reason: "还没有任何归档" }, false);
    const FEAT = e.FEAT;
    FEAT.rate.tlPoll = 2;
    FEAT.rate.tlBusyUntil = Date.now() - 1000;
    await e.api2.loadRateTimeline();
    ck("case5 窗口过期不轮询", e.timers.length === 0);
    ck("case5 tlPoll 归零", FEAT.rate.tlPoll === 0);
  }
  // case6 rateMarkBusy：设窗口 + 清计数（跑榜/归档点击时调用）
  {
    const e = build({ ok: true, has_snap: false }, false);
    const hm = factoryMark(...stubArgs(e.FEAT, () => {}));
    e.FEAT.rate.tlPoll = 2;
    hm.rateMarkBusy(120000);
    ck("case6 rateMarkBusy 清计数", e.FEAT.rate.tlPoll === 0);
    ck("case6 rateMarkBusy 设窗口", e.FEAT.rate.tlBusyUntil > Date.now() + 110000);
  }
  // case7 静态签名：每日预测过期角标已在前端落地（渲染代码存在 + 用后端 reason）
  ck("case7 app.js 有 pred-stale 角标渲染", /class=\\"pred-stale\\"/.test(src) || /class="pred-stale"/.test(src));
  ck("case7 app.js 渲染后端 stale_reason", /stale_reason/.test(src));
  ck("case7 app.js 渲染 stale 角标文案", /数据截至 /.test(src));
  const css = fs.readFileSync(path.join(ROOT, "viewer", "static", "app.css"), "utf8");
  ck("case7 app.css 有过期角标样式", /\.pred-stale\s*\{/.test(css));
  ck("case7 app.css 有生成中空态样式", /\.wt-empty-busy\s*\{/.test(css));
  ck("case7 app.css 有 stale 说明条样式", /\.pred-stale-hint\s*\{/.test(css));

  console.log("==========");
  console.log(fail === 0 ? "ALL_PASS (" + pass + ")" : "FAILED " + fail + " / " + (pass + fail));
  process.exit(fail === 0 ? 0 : 1);
})();
