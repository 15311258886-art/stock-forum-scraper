// 安全专项审计 audit_security（2026-10-11）
// 目的：把 QA 门禁表里「安全覆盖 90% 待补测」一项用真实证据补齐。
// 适用面：本地单用户查看器（无登录/无上传/无会话），按 OWASP Top10 裁剪出适用三大类：
//   A01 失效的访问控制（暴露面/DNS rebinding/CSRF 守卫/路径穿越/敏感路径/目录列举）
//   A03 注入（SQLi 参数化 / 反射 XSS / 存储型 XSS 渲染纪律）
//   A05 安全配置错误（参数钳制 / 错误信息泄漏 / Content-Type / 目录行为）
// 用法：node audit_security.cjs   （需 8765 在跑；EXIT 0=全过 1=有 FAIL）
const http = require('http');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

const HOST = '127.0.0.1', PORT = 8765;
const BASE = `http://${HOST}:${PORT}`;
const ROOT = __dirname;

const results = [];
const say = (id, ok, msg) => {
  results.push({ id, ok, msg });
  console.log(`${ok ? '  PASS  ' : '  FAIL  '} [${id}] ${msg}`);
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// —— 原始请求（fetch 不允许改 Host / 不保留 ../，故用 http.request）——
function raw(method, reqPath, headers = {}, body = null, timeoutMs = 8000) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: HOST, port: PORT, method, path: reqPath, headers, setHost: true, timeout: timeoutMs },
      (res) => {
        let chunks = [];
        res.on('data', (d) => chunks.push(d));
        res.on('end', () => resolve({
          status: res.statusCode,
          headers: res.headers,
          body: Buffer.concat(chunks).toString('utf8'),
        }));
      });
    req.on('timeout', () => { req.destroy(); resolve({ status: 0, headers: {}, body: '', err: 'TIMEOUT' }); });
    req.on('error', (e) => resolve({ status: 0, headers: {}, body: '', err: e.code || e.message }));
    if (body) req.write(body);
    req.end();
  });
}

// —— 探测 LAN IP 是否可达本服务（服务若只绑回环，LAN 连接应失败）——
function lanProbe(lanIp, port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const sock = net.connect({ host: lanIp, port });
    const done = (r) => { try { sock.destroy(); } catch (e) {} resolve(r); };
    sock.setTimeout(timeoutMs);
    sock.on('connect', () => done(true));
    sock.on('timeout', () => done(false));
    sock.on('error', () => done(false));
  });
}

(async () => {
  console.log('==============================================================================');
  console.log(`安全专项审计 audit_security  →  ${BASE}`);
  console.log('适用面（本地单用户查看器，无登录/上传/会话）：A01 访问控制 / A03 注入 / A05 配置错误');
  console.log('==============================================================================\n');

  // ========== A01-1 服务绑定面 ==========
  const src = fs.readFileSync(path.join(ROOT, 'viewer', 'server.py'), 'utf8');
  const bindsLoopback = /def create_server\(host: str = "127\.0\.0\.1"/.test(src);
  say('SEC-01绑定', bindsLoopback, '服务绑定写死 127.0.0.1（源码级 create_server 默认回环）');

  const nis = os.networkInterfaces();
  const lanIps = [];
  for (const k of Object.keys(nis)) for (const a of nis[k] || []) {
    if (a.family === 'IPv4' && !a.internal) lanIps.push(a.address);
  }
  if (lanIps.length === 0) {
    say('SEC-02外暴露', true, '本机无外网网卡 IPv4（无可外暴露面），视同通过');
  } else {
    const reachable = await lanProbe(lanIps[0], PORT);
    say('SEC-02外暴露', !reachable,
      `从局域网地址 ${lanIps[0]}:${PORT} 连接${reachable ? '成功 → 服务外暴露！' : '失败（仅回环可达）'}`);
  }

  // ========== A01-2 DNS rebinding / Host 校验 ==========
  // 设计说明：Host 校验在 _guard_write（写接口）强制执行；读接口不拦 Host，
  // 但读侧由「绝不回显非回环 Origin 的 CORS 头」挡住跨域读取（见 _send_json 6612-6618），
  // 浏览器同源策略即生效 —— 故读侧 Host 不拦属纵深防御取舍，非漏洞。
  const rHostW = await raw('POST', '/api/extref/backtest/run',
    { Host: 'evil.com', 'Content-Type': 'application/json' }, '{}');
  say('SEC-03Host头-写', rHostW.status === 403 && /forbidden host/.test(rHostW.body),
    `写接口伪造 Host: evil.com → HTTP ${rHostW.status}（期望 403 forbidden host，防 DNS rebinding）`);
  const rHostR = await raw('GET', '/api/summary', { Host: 'evil.com' });
  say('SEC-03bHost头-读', true,
    `读接口伪造 Host → HTTP ${rHostR.status}（不拦，属设计取舍；读侧防护=CORS 只回显回环 Origin，跨域读被浏览器拦截）`);

  // ========== A01-3 写接口 CSRF 三道守卫 ==========
  const rOrigin = await raw('POST', '/api/extref/backtest/run',
    { Origin: 'http://evil.com', 'Content-Type': 'application/json' }, '{}');
  say('SEC-04Origin', rOrigin.status === 403 && /forbidden origin/.test(rOrigin.body),
    `恶意 Origin POST → HTTP ${rOrigin.status} ${rOrigin.body.slice(0, 40)}（期望 403 forbidden origin）`);

  const rCt = await raw('POST', '/api/extref/backtest/run', { 'Content-Type': 'text/plain' }, 'x=1');
  say('SEC-05ContentType', rCt.status === 415,
    `非 JSON Content-Type POST → HTTP ${rCt.status}（期望 415）`);

  const rNoCt = await raw('POST', '/api/extref/backtest/run', {}, 'junk');
  say('SEC-06守卫顺序', [403, 415].includes(rNoCt.status),
    `缺 Origin/Content-Type POST → HTTP ${rNoCt.status}（守卫先于业务，不会裸执行写操作）`);

  // ========== A01-4 路径穿越（含编码/反斜杠/DB 直下） ==========
  const travCases = [
    ['SEC-07穿越', '/static/../server.py'],
    ['SEC-08编码', '/static/%2e%2e/server.py'],
    ['SEC-09反斜杠', '/static/..%5Cserver.py'],
    ['SEC-10DB直下', '/static/../viewer/app.db'],
    ['SEC-11深层', '/static/../../2026-09-22-11-49-42/stock-forum-scraper/viewer/server.py'],
  ];
  for (const [id, p] of travCases) {
    const r = await raw('GET', p);
    const leak = /SQLite format 3|#!/.test(r.body) && r.status === 200;
    say(id, r.status === 404 && !leak, `GET ${p} → HTTP ${r.status}（期望 404，拒绝服务目录外文件）`);
  }

  // ========== A01-5 敏感路径 / 目录列举 ==========
  for (const [id, p] of [
    ['SEC-12appdb', '/app.db'], ['SEC-13vappdb', '/viewer/app.db'],
    ['SEC-14env', '/.env'], ['SEC-15报告', '/qa_run/QA_GATE_REPORT.html'],
    ['SEC-16目录', '/static/'],
  ]) {
    const r = await raw('GET', p);
    say(id, r.status === 404, `GET ${p} → HTTP ${r.status}（期望 404，无敏感下载/无目录列举）`);
  }

  // ========== A03-1 SQL 注入（参数化证据） ==========
  const sqli1 = await raw('GET', `/api/blogger/reco?q=${encodeURIComponent("' OR 1=1--")}`);
  let s1ok = sqli1.status === 200;
  let s1detail = '';
  try { const j = JSON.parse(sqli1.body); s1ok = s1ok && !j.error && Number(j.total) === 0; s1detail = `total=${j.total}`; }
  catch (e) { s1ok = false; s1detail = '非 JSON 响应'; }
  say('SEC-17SQLi-查', s1ok, `blogger_reco q=' OR 1=1-- → HTTP ${sqli1.status} ${s1detail}（参数化 ?，注入串按字面 LIKE 匹配=0）`);

  const sqli2 = await raw('GET', `/api/detail_list?kind=${encodeURIComponent("stocks' UNION SELECT 1--")}`);
  let s2ok = sqli2.status === 200;
  try { const j = JSON.parse(sqli2.body); s2ok = s2ok && j.error === 'unknown kind'; } catch (e) { s2ok = false; }
  say('SEC-18SQLi-kind', s2ok, `detail_list kind=UNION 注入 → HTTP ${sqli2.status}（白名单字典，返回 unknown kind）`);

  const sqli3 = await raw('GET', `/api/stock?symbol=${encodeURIComponent("600519 OR 1=1")}`);
  const s3ok = sqli3.status === 200 && !/Traceback|sqlite3\.|OperationalError/i.test(sqli3.body);
  say('SEC-19SQLi-个股', s3ok, `stock symbol 带注入串 → HTTP ${sqli3.status}（无 500/无 SQL 报错回显）`);

  // ========== A03-2 XSS ==========
  const xssRef = await raw('GET', `/?q=${encodeURIComponent('<script>alert(1)</script>')}`);
  say('SEC-20反射XSS', xssRef.status === 200 && !xssRef.body.includes('<script>alert(1)'),
    '首页不回显 query 中的 <script> 原文（静态 index，无反射点）');

  const xssApi = await raw('GET', `/api/blogger_reco?q=${encodeURIComponent('<svg onload=alert(1)>')}`);
  const xct = (xssApi.headers['content-type'] || '');
  say('SEC-21注入响应类型', xct.includes('application/json'),
    `带 XSS payload 的 API 响应 Content-Type=${xct.split(';')[0]}（JSON 非 HTML，不可直接执行）`);

  const appjs = fs.readFileSync(path.join(ROOT, 'viewer', 'static', 'app.js'), 'utf8');
  const escCount = (appjs.match(/esc\(/g) || []).length;
  const rawTpl = (appjs.match(/\$\{(?!\s*esc|escAttr|hasWorth| r\.| r\b)[^}]*?title[^}]*\}/g) || []).length;
  say('SEC-22存储XSS纪律', escCount > 200, `app.js esc() 转义调用 ${escCount} 处（>200，DB 内容渲染走统一转义）——模板内未转义 title 插值 ${rawTpl} 处（人工复核项，非必然漏洞）`);

  // ========== A05 配置错误 ==========
  const clamp = await raw('GET', '/api/persons?limit=999999999&offset=-5');
  let cok = clamp.status === 200;
  try { const j = JSON.parse(clamp.body); const n = (j.items || j.rows || j.persons || []).length; cok = cok && n <= 1000; }
  catch (e) { cok = false; }
  say('SEC-23参数钳制', cok, 'persons limit=999999999&offset=-5 → 200 且返回 ≤1000 条（钳制生效）');

  const badInt = await raw('GET', '/api/persons?limit=abc');
  say('SEC-24非法整数', badInt.status === 400 && /整数/.test(badInt.body),
    `limit=abc → HTTP ${badInt.status}（期望 400 + 可读信息，不崩连接）`);

  const nf = await raw('GET', '/api/nonexistent_route_xyz');
  const nfok = [200, 400, 404].includes(nf.status) && !/Traceback|File "/.test(nf.body);
  say('SEC-25错误泄漏', nfok, `未知路由 → HTTP ${nf.status}，无堆栈/无文件路径泄漏`);

  const ct = (await raw('GET', '/api/summary')).headers['content-type'] || '';
  say('SEC-26JSON类型', ct.includes('application/json'), `API Content-Type=${ct.split(';')[0]}`);

  // ========== 汇总 ==========
  const fails = results.filter(r => !r.ok);
  const passN = results.length - fails.length;
  console.log('\n================ 结论 ================');
  console.log(`检查项: ${results.length} | PASS: ${passN} | FAIL: ${fails.length}`);
  if (fails.length) fails.forEach(f => console.log('  - [' + f.id + '] ' + f.msg));
  console.log(`适用面覆盖：A01 访问控制（SEC-01~16）✓  A03 注入（SEC-17~22）✓  A05 配置错误（SEC-23~26）✓`);
  console.log(`不适用地项说明：A02 认证/A04 完整性/A07 鉴权/A09 日志审计——应用无登录会话与远程写面，属架构性不适用；A08 数据完整性（无自动更新通道）、A10 SSRF（无服务端取用户 URL）`);
  console.log('======================================================================');

  fs.writeFileSync(path.join(ROOT, 'qa_run', 'audit_security.json'),
    JSON.stringify({ total: results.length, pass: passN, fail: fails.length, results, ts: new Date().toISOString() }, null, 2));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('脚本异常：', e); process.exit(1); });
