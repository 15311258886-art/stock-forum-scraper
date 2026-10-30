// xueqiu_cube_crawler.cjs
// 雪球「组合持仓(cube)」回填 - 浏览器抓取段（Node + puppeteer-core）。
//
// 背景：雪球被阿里云 WAF + 滑块拦截，且组合持仓需登录态(cookie)才能看到完整明细。
// 因此必须在「本机带代理的浏览器」里：注入 cookie -> 访问用户主页抠 cube_id ->
// 调 rebalancing/history.json 取调仓历史 -> 抽持仓(symbol/weight)。
//
// ============================================================================
// 2026-10-30 退役闸：雪球链路整体停用（对应 viewer/config.XUEQIU_ENABLED=False）。
//   依据 docs/论坛大V信号实证_20261030.md —— 「跟人操作」经同日配对检验后不显著。
//   Node 读不到 python 配置，故用显式环境变量开门：默认停，XUEQIU_ENABLED=1 才抓。
//   需要恢复时：config.XUEQIU_ENABLED 改回 True，并 `XUEQIU_ENABLED=1 node xueqiu_cube_crawler.cjs`。
// ============================================================================
if (process.env.XUEQIU_ENABLED !== '1') {
  console.error('[退役] 雪球组合抓取已停用（config.XUEQIU_ENABLED=False）。');
  console.error('        恢复方式：config 改回 True，并以 XUEQIU_ENABLED=1 运行本脚本。');
  process.exit(0);
}

// 输入（环境变量，均可选，有默认值）：
//   XQ_CHROME_EXE   本机 Chromium 路径（默认见下）
//   XQ_PROXY        代理 http://127.0.0.1:53269
//   XQ_NODE_PATH    node_modules 目录（puppeteer-core 所在）
//   XQ_COOKIE_FILE  雪球 cookie 的 JSON 文件路径（Netscape 数组 或 {cookies:[...]}）
//   XQ_TARGETS_FILE 目标 uid 列表 JSON：[uid, uid, ...]（缺省读 xueqiu_targets.json）
//   XQ_UIDS         “,” 分隔的 uid 字符串（与 TARGETS_FILE 二选一）
//   XQ_OUT          输出 JSON 路径（默认 xueqiu_cubes.json）
//
// 输出 xueqiu_cubes.json：
//   { "crawled_at":"...", "players": { "<uid>": { "name":"...", "cubes":[
//        {"cube_id":"...","cube_name":"...","holdings":[{"symbol":"600519",
//         "name":"贵州茅台","weight":12.3}, ...]}, ...]}, ... }
//
// 失败处理：单用户/单 cube 出错只记录到 result.errors，不中断整体。

const fs = require('fs');
const path = require('path');
const os = require('os');

const HOME = process.env.USERPROFILE || os.homedir();
const CHROME_EXE = process.env.XQ_CHROME_EXE ||
  path.join(HOME, 'AppData', 'Local', 'ms-playwright', 'chromium-1234', 'chrome-win64', 'chrome.exe');
const PROXY = process.env.XQ_PROXY || 'http://127.0.0.1:53269';
const NODE_PATH = process.env.XQ_NODE_PATH ||
  'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules';

let puppeteer;
try { puppeteer = require(path.join(NODE_PATH, 'puppeteer-core')); }
catch (e) { console.error('LOAD_PUPPETEER_ERR ' + e.message); process.exit(2); }

function resolveCookieFile() {
  const env = process.env.XQ_COOKIE_FILE;
  if (env && fs.existsSync(env)) return env;
  // 自动识别两种常见落地方式：JSON（含 xueqiu 登录态）或 Netscape 文本（浏览器导出的 .txt）
  for (const cand of ['cookies.json', 'cookies.txt', 'cookies_netscape.txt']) {
    if (fs.existsSync(cand)) return cand;
  }
  return null;
}

// 解析 Netscape HTTP Cookie File（浏览器「导出为 Netscape 格式」的文本）
function parseNetscape(text) {
  const out = [];
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const parts = t.split('\t');
    if (parts.length < 7) continue;
    const [domain, , path, secure, expires, name] = parts;
    const value = parts.slice(6).join('\t');
    if (!name || !value) continue;
    out.push({
      name, value,
      domain: domain.startsWith('.') ? domain : '.' + domain,
      path: path || '/',
      expires: expires && expires !== '0' ? Number(expires) : undefined,
      httpOnly: false,
      secure: /TRUE/i.test(secure),
    });
  }
  return out;
}

function loadCookies() {
  const f = resolveCookieFile();
  if (!f) {
    console.error('NO_COOKIE_FILE: 未找到 cookies.json / cookies.txt（雪球组合持仓需登录态）。');
    console.error('  把雪球 Cookie 存为 cookies.json(JSON) 或 cookies.txt(Netscape) 放本目录即可，无需重新登录。');
    return [];
  }
  try {
    const raw = fs.readFileSync(f, 'utf-8');
    let arr = [];
    const looksNetscape = f.endsWith('.txt') ||
      /#\s*Netscape|^\s*\.[^\t#]+\t(TRUE|FALSE)\t/i.test(raw);
    if (looksNetscape) {
      arr = parseNetscape(raw);
    } else {
      const data = JSON.parse(raw);
      arr = Array.isArray(data) ? data : (data.cookies || []);
      arr = arr.map(c => ({
        name: c.name, value: c.value, domain: c.domain || '.xueqiu.com',
        path: c.path || '/', expires: c.expirationDate || c.expires || undefined,
        httpOnly: !!c.httpOnly, secure: !!c.secure,
      }));
    }
    // 只保留 xueqiu 域名，避免无关/淘股吧 cookie 干扰注入
    arr = arr.filter(c => c.name && c.value && /xueqiu/i.test(c.domain || ''));
    if (!arr.length) console.error('COOKIE_NO_XUEQIU: 文件中未找到 xueqiu 域名 cookie');
    else console.error('COOKIE_LOADED: ' + arr.length + ' 条 xueqiu cookie（来自 ' + f + '）');
    return arr;
  } catch (e) { console.error('COOKIE_PARSE_ERR ' + e.message); return []; }
}

// 把任意 targets 输入归一化为 [{name, uid}]；uid 为空表示需按名字搜索解析。
function normalizeTargets(raw) {
  const out = [];
  if (Array.isArray(raw)) {
    for (const it of raw) {
      if (typeof it === 'string') {
        const s = it.trim();
        if (!s) continue;
        if (/^\d+$/.test(s)) out.push({ name: s, uid: s });
        else out.push({ name: s, uid: '' });
      } else if (it && typeof it === 'object') {
        const n = String(it.name || it.screen_name || '').trim();
        const u = String(it.uid || it.id || '').trim();
        if (n || u) out.push({ name: n, uid: /^\d+$/.test(u) ? u : '' });
      }
    }
  } else if (raw && typeof raw === 'object') {
    for (const [n, u] of Object.entries(raw)) {
      const uid = String(u || '').trim();
      out.push({ name: n, uid: /^\d+$/.test(uid) ? uid : '' });
    }
  }
  return out;
}

function loadTargets() {
  let raw = null;
  if (process.env.XQ_UIDS) {
    raw = process.env.XQ_UIDS.split(',').map(s => s.trim()).filter(Boolean);
  } else {
    const tf = process.env.XQ_TARGETS_FILE || 'xueqiu_targets.json';
    if (fs.existsSync(tf)) {
      try { raw = JSON.parse(fs.readFileSync(tf, 'utf-8')); }
      catch (e) { console.error('TARGETS_PARSE_ERR ' + e.message); }
    }
  }
  return normalizeTargets(raw);
}

// 按名字在雪球搜索页找 uid（best-effort）。雪球用户主页为 /u/<uid> 或老版 /<uid>。
async function resolveNameToUid(page, name) {
  const q = encodeURIComponent(name);
  try {
    await page.goto('https://xueqiu.com/k?q=' + q, { waitUntil: 'networkidle2', timeout: 25000 });
    await new Promise(r => setTimeout(r, 2500));
  } catch (e) { console.error('SEARCH_WARN ' + name + ' ' + e.message); }
  const html = await page.content();
  const uids = new Set();
  let m;
  const re1 = /\/u\/(\d+)/g;
  while ((m = re1.exec(html)) !== null) uids.add(m[1]);
  const re2 = /href=["']\/(\d{6,})["']/g;
  while ((m = re2.exec(html)) !== null) uids.add(m[1]);
  return [...uids][0] || '';  // 取第一个候选（同名时可能不准，需本机反馈校准）
}

// TCP 可达性探测（用于代理降级）
const net = require('net');
function probeTcp(host, port, timeout = 800) {
  return new Promise(res => {
    const s = net.connect({ host, port });
    const done = ok => { try { s.destroy(); } catch (e) {} res(ok); };
    s.setTimeout(timeout, () => done(false));
    s.on('connect', () => done(true));
    s.on('error', () => done(false));
  });
}

const OUT = process.env.XQ_OUT || 'xueqiu_cubes.json';
const cookies = loadCookies();
const targets = loadTargets();

console.error('INFO targets=' + targets.length + ' cookies=' + cookies.length + ' chrome=' + CHROME_EXE);

(async () => {
  const result = { crawled_at: new Date().toISOString(), players: {}, errors: [] };
  if (!targets.length) {
    result.errors.push('NO_TARGETS: 未提供雪球 uid 清单（XQ_UIDS 或 xueqiu_targets.json）');
    fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
    console.error('DONE_NO_TARGETS');
    return;
  }

  let browser;
  try {
    // 代理可达性探测：不通则降级直连（本机直连外网即可，无需代理）
    let useProxy = PROXY;
    try {
      const pu = new URL(PROXY);
      const ok = await probeTcp(pu.hostname, Number(pu.port) || 80);
      if (!ok) { console.error('PROXY_UNREACHABLE ' + PROXY + ' -> 降级直连'); useProxy = ''; }
    } catch (e) { useProxy = ''; }
    const launchArgs = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                        '--ignore-certificate-errors', '--disable-blink-features=AutomationControlled',
                        '--window-size=1920,1080'];
    if (useProxy) launchArgs.push('--proxy-server=' + useProxy);
    browser = await puppeteer.launch({
      executablePath: CHROME_EXE, headless: 'new', args: launchArgs,
    });
  } catch (e) { console.error('BROWSER_LAUNCH_ERR ' + e.message); result.errors.push('BROWSER_LAUNCH_ERR: ' + e.message); fs.writeFileSync(OUT, JSON.stringify(result, null, 2)); process.exit(2); }

  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');
  try { await page.setViewport({ width: 1920, height: 1080 }); } catch (e) {}
  if (cookies.length) {
    try { await page.setCookie(...cookies); } catch (e) { console.error('SET_COOKIE_ERR ' + e.message); }
  }
  // 预热首页过 WAF
  try { await page.goto('https://xueqiu.com/', { waitUntil: 'networkidle2', timeout: 25000 }); await new Promise(r => setTimeout(r, 3000)); }
  catch (e) { console.error('WARMUP_WARN ' + e.message); }

  for (const t of targets) {
    let uid = t.uid;
    const wantName = t.name || '';
    if (!uid) {
      // 名字模式：先搜索找 uid（best-effort，需本机反馈校准同名问题）
      console.error('RESOLVE ' + wantName + ' -> 搜索 uid ...');
      try { uid = await resolveNameToUid(page, wantName); }
      catch (e) { result.errors.push('RESOLVE_ERR ' + wantName + ': ' + e.message); console.error('RESOLVE_ERR ' + wantName); continue; }
      if (!uid) { result.errors.push('NO_UID ' + wantName + ': 搜索未找到该用户的 uid'); console.error('NO_UID ' + wantName); continue; }
      console.error('RESOLVE_OK ' + wantName + ' -> ' + uid);
    }
    try {
      // 1) 用户主页（取昵称 + cube_count 兜底）
      let name = wantName, cubeTotal = null;
      try {
        await page.goto('https://xueqiu.com/u/' + uid, { waitUntil: 'networkidle2', timeout: 25000 });
        await new Promise(r => setTimeout(r, 2000));
        const html = await page.content();
        const mm = html.match(/"cube_count":(\d+)/);
        if (mm) cubeTotal = Number(mm[1]);
        try {
          const meta = await page.evaluate(() => {
            const el = document.querySelector('a.user-name, .profile-name, h1');
            return el ? el.textContent.trim() : '';
          });
          if (meta) name = meta;
        } catch (e) { /* 取不到昵称不影响 */ }
      } catch (e) { result.errors.push('PROFILE_WARN ' + uid + ': ' + e.message); }

      // 2) 组合列表：走 cubes/list.json（页面级导航，绕开被 WAF 拦的 XHR）
      let cubes = [];
      try {
        const resp = await page.goto('https://xueqiu.com/cubes/list.json?uid=' + uid + '&count=20&page=1',
                                     { waitUntil: 'domcontentloaded', timeout: 20000 });
        const j = JSON.parse(await resp.text());
        if (typeof j.totalCount === 'number') cubeTotal = j.totalCount;
        cubes = Array.isArray(j.list) ? j.list : [];
      } catch (e) { result.errors.push('CUBELIST_ERR ' + uid + ': ' + e.message); }

      const player = { name, uid, cube_count: cubeTotal, cubes: [] };
      for (const c of cubes.slice(0, 5)) {
        const cid = String(c.id || c.cube_id || c.cubeId || '');
        const csym = String(c.symbol || c.cube_symbol || '');
        const cname = c.name || c.cube_name || csym || cid;
        if (!cid && !csym) continue;
        for (const param of [csym ? 'cube_symbol=' + csym : '', cid ? 'cube_id=' + cid : '']) {
          if (!param) continue;
          try {
            const api = 'https://xueqiu.com/cubes/rebalancing/history.json?' + param + '&count=20&page=1';
            const resp = await page.goto(api, { waitUntil: 'domcontentloaded', timeout: 20000 });
            const j = JSON.parse(await resp.text());
            const list = j.list || [];
            const holdings = [];
            for (const item of list) {
              const hs = item.holding || item.holdings || [];
              for (const h of hs) {
                const sym = h.symbol || h.stock_symbol || h.code;
                if (!sym) continue;
                holdings.push({ symbol: String(sym).replace(/^(SH|SZ|sh|sz)/, ''), name: h.name || '', weight: Number(h.weight) || 0 });
              }
            }
            if (holdings.length) {
              player.cubes.push({ cube_id: cid || csym, cube_symbol: csym, cube_name: cname, holdings });
              break;
            }
          } catch (e) { result.errors.push('CUBE_ERR ' + uid + '/' + (csym || cid) + ': ' + e.message); }
        }
      }
      result.players[uid] = player;
      console.error('OK ' + (name || uid) + ' cube_count=' + cubeTotal + ' cubes_with_holdings=' + player.cubes.length);
    } catch (e) {
      result.errors.push('USER_ERR ' + (wantName || uid) + ': ' + e.message);
      console.error('USER_ERR ' + (wantName || uid) + ' ' + e.message);
    }
  }

  try { await browser.close(); } catch (e) {}
  fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
  const totalCubes = Object.values(result.players).reduce((a, p) => a + (p.cubes || []).length, 0);
  console.error('DONE players=' + Object.keys(result.players).length + ' cubes=' + totalCubes + ' -> ' + OUT);
})().catch(e => { console.error('FATAL ' + e.message); process.exit(1); });
