/**
 * verify_api_retry.cjs —— 校验 app.js 里 api() 的「网络层自动重试」行为。
 *
 * 为什么要有这个脚本：
 *   2026-09-28 王总反馈个股抽屉 K 线区报「行情获取失败：Failed to fetch」。
 *   根因是后端重启的端口空档期，前端 fetch 连不上（后端有全局兜底，异常会回 500 JSON，
 *   所以 Failed to fetch 一定不是业务错误，而是连接层失败）。
 *   修法：给 api() 加连接类错误自动重试（0.4s / 1.2s 两次），仍失败则给中文提示。
 *   api() 是所有请求的公共入口，改它必须回归。
 *
 * 断言（直接从真实 app.js 抽取源码执行，不复制逻辑）：
 *   1) 抽取到的源码里含中文提示常量、重试分支、且只对 TypeError 重试
 *   2) 连接错误 2 次后第 3 次成功 → resolve，且 fetch 恰好被调 3 次
 *   3) 连接错误一直失败 → reject，文案是中文提示（不含裸 "Failed to fetch"），共 3 次
 *   4) HTTP 500 → 直接 reject（不重试，1 次），文案含 HTTP 500
 *   5) JSON 解析失败（SyntaxError）→ 直接 reject，不重试
 *   6) 正常 200 → resolve 出 JSON，1 次请求
 */
const fs = require("fs");
const path = require("path");

const APP = path.join(__dirname, "viewer", "static", "app.js");
const src = fs.readFileSync(APP, "utf8");

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? " — " + extra : ""}`); }
};

// ---- 从 app.js 里按「大括号配平」抽出 const _API_NET_HINT ... function api(){...} ----
const hintAt = src.indexOf("const _API_NET_HINT");
if (hintAt < 0) { console.error("FAIL: app.js 中找不到 _API_NET_HINT"); process.exit(1); }
const fnAt = src.indexOf("function api(path, opts)", hintAt);
if (fnAt < 0) { console.error("FAIL: app.js 中找不到 function api(path, opts)"); process.exit(1); }
let i = src.indexOf("{", fnAt), depth = 0, end = -1;
for (; i < src.length; i++) {
  const c = src[i];
  if (c === "{") depth++;
  else if (c === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
}
if (end < 0) { console.error("FAIL: api() 大括号未配平"); process.exit(1); }
const snippet = src.slice(hintAt, end);
const mod = new Function(snippet + "\nreturn { api, HINT: _API_NET_HINT };")();
const api = mod.api;

console.log("== api() 网络重试回归 ==");
ok("源码含中文提示常量", /连接中断/.test(mod.HINT), mod.HINT);
ok("源码含重试分支", /n < 2/.test(snippet));
ok("源码只对 TypeError 重试", /name === "TypeError"/.test(snippet));

(async () => {
  // 2) 连接错误 2 次后第 3 次成功
  {
    let calls = 0;
    global.fetch = () => {
      calls++;
      if (calls < 3) return Promise.reject(new TypeError("Failed to fetch"));
      return Promise.resolve({ ok: true, json: async () => ({ hits: 42 }) });
    };
    const d = await api("/api/kline?symbol=002582");
    ok("连接错误 2 次后成功 → resolve 出数据", d && d.hits === 42, JSON.stringify(d));
    ok("成功路径 fetch 恰好 3 次", calls === 3, "calls=" + calls);
  }

  // 3) 一直连接失败 → 中文提示
  {
    let calls = 0;
    global.fetch = () => { calls++; return Promise.reject(new TypeError("Failed to fetch")); };
    let msg = "";
    try { await api("/api/kline?symbol=002582"); } catch (e) { msg = e.message; }
    ok("持续连接失败 → 抛出中文提示", /连接中断/.test(msg), msg);
    ok("提示中不再出现裸 Failed to fetch", !/Failed to fetch/.test(msg), msg);
    ok("重试上限 = 3 次请求", calls === 3, "calls=" + calls);
  }

  // 4) HTTP 500 不重试
  {
    let calls = 0;
    global.fetch = () => {
      calls++;
      return Promise.resolve({ ok: false, status: 500, statusText: "Internal Server Error", text: async () => '{"error":"internal error"}' });
    };
    let msg = "";
    try { await api("/api/kline?symbol=002582"); } catch (e) { msg = e.message; }
    ok("HTTP 500 → 直接抛错", /HTTP 500/.test(msg), msg);
    ok("HTTP 500 不重试（1 次）", calls === 1, "calls=" + calls);
  }

  // 5) JSON 解析失败不重试
  {
    let calls = 0;
    global.fetch = () => { calls++; return Promise.resolve({ ok: true, json: async () => { throw new SyntaxError("bad json"); } }); };
    let name = "";
    try { await api("/api/x"); } catch (e) { name = e.name; }
    ok("JSON 解析失败 → 抛 SyntaxError", name === "SyntaxError", name);
    ok("解析失败不重试（1 次）", calls === 1, "calls=" + calls);
  }

  // 6) 正常路径
  {
    let calls = 0;
    global.fetch = () => { calls++; return Promise.resolve({ ok: true, json: async () => ({ ok: 1 }) }); };
    const d = await api("/api/stocks");
    ok("正常 200 → resolve", d && d.ok === 1);
    ok("正常路径 1 次请求", calls === 1, "calls=" + calls);
  }

  console.log(`\n== 结果：${pass} PASS / ${fail} FAIL ==`);
  process.exit(fail ? 1 : 0);
})();
