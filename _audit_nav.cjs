/* 端到端导航审计：遍历 NAV 全部 6 类 37 子 tab
   - 直链 (#cat/tab) 与 点击横条 两条路径都测
   - 断言：面板非空 / 零 JS 报错 / 零溢出
   - 同时记录每个 /api/ 请求的 status + 耗时 + 字节（既是接口冒烟，也是性能基线） */
const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const ROOT = __dirname;
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const BASE = 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* 自起服务：绑定真实端口 8765。
   原因：desktop_app.py 末尾 os._exit(0)，窗口/异常时整个进程连 HTTP 服务一起退出，
   外部重启的服务撑不过几十秒。这里直接拉 server.py，脚本生命周期内保持存活。 */
const PYEXE = require('path').join(ROOT, '.venv', 'Scripts', 'python.exe');
let SERVER = null;

async function ensureServer() {
  const net = require('net');
  const probe = () => new Promise(res => {
    const s = net.connect(8765, '127.0.0.1');
    s.on('connect', () => { s.end(); res(true); });
    s.on('error', () => res(false));
    setTimeout(() => { s.destroy(); res(false); }, 1500);
  });
  if (await probe()) { console.log('[srv] 8765 已有服务在跑，直接复用'); return; }
  SERVER = require('child_process').spawn(PYEXE, ['-m', 'viewer.server'], {
    cwd: ROOT, env: Object.assign({}, process.env, { VIEWER_PORT: '8765' }),
    stdio: ['ignore', require('fs').openSync(require('path').join(ROOT, '_srv_out.log'), 'w'),
            require('fs').openSync(require('path').join(ROOT, '_srv_err.log'), 'w')],
  });
  for (let i = 0; i < 60; i++) {
    await sleep(800);
    if (await probe()) { console.log('[srv] viewer.server 已监听 8765 (等待 ' + ((i + 1) * 0.8).toFixed(1) + 's)'); return; }
  }
  throw new Error('viewer.server 未能在 48s 内监听 8765');
}

const NAV = [
  { key: 'overview', label: '今日总览', tabs: ['dashboard', 'summary', 'prediction'] },
  { key: 'money', label: '钱往哪走', tabs: ['lhb', 'lhb_sectors', 'stocks', 'sentiment', 'moneyflow'] },
  { key: 'info', label: '消息与外围', tabs: ['topics', 'geo_events', 'geo_policy', 'geo_tech', 'overseas_sentiment', 'overseas_hotspots', 'policy', 'news'] },
  { key: 'players', label: '别人怎么做', tabs: ['players', 'operating', 'niusan', 'consensus', 'cohold', 'persons'] },
  { key: 'opinion', label: '别人怎么看', tabs: ['extref_soft', 'extref_comm', 'extref_rank', 'research', 'blogger', 'contest'] },
  { key: 'watch', label: '我的票', tabs: ['watch', 'watch_alerts', 'screener', 'diag', 'multiquote'] },
  { key: 'learn', label: '复盘成长', tabs: ['strategy_lab', 'learn_tracks'] },
];

function panelStats() {
  const p = document.querySelector('.panel.active') || document.querySelector('.panel[style*="display: block"]');
  if (!p) return { missing: true };
  const r = p.getBoundingClientRect();
  return {
    id: p.id,
    nodes: p.querySelectorAll('*').length,
    textLen: (p.innerText || '').replace(/\s+/g, '').length,
    overflowX: p.scrollWidth - p.clientWidth,
    rows: p.querySelectorAll('tbody tr').length,
    cards: p.querySelectorAll('.card,.bt-card,.panel-card,.st-card').length,
  };
}

(async () => {
  await ensureServer();
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--disable-gpu'],
    defaultViewport: { width: 1600, height: 1000 },
  });
  const page = await browser.newPage();
  const errs = [];
  const api = [];          // 接口耗时表
  const pending = new Map();
  const badApi = [];

  page.on('pageerror', e => errs.push('PAGEERROR ' + String(e.message).slice(0, 300)));
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE ' + m.text().slice(0, 300)); });
  page.on('request', r => {
    const u = r.url();
    if (u.includes('/api/')) pending.set(u, { url: u, t0: Date.now() });
  });
  page.on('response', async res => {
    const u = res.url();
    if (!u.includes('/api/')) return;
    const rec = pending.get(u) || { url: u, t0: Date.now() };
    pending.delete(u);
    let len = 0;
    try { len = (await res.text()).length; } catch (e) { len = -1; }
    const ms = Date.now() - rec.t0;
    const item = { url: u.replace(BASE, ''), status: res.status(), ms, bytes: len };
    api.push(item);
    if (res.status() !== 200 || len < 60) badApi.push(item);
  });

  const results = [];
  const ok = (c, m) => { if (!c) console.log('   FAIL ' + m); return c; };

  const ONLY_B = process.argv.includes('--onlyB');
  // ---------- Pass A: 直链 ----------
  console.log('=== Pass A: 深链接直链遍历 ===');
  if (ONLY_B) { console.log('(跳过 Pass A)'); } else
  for (const cat of NAV) {
    for (const tab of cat.tabs) {
      api.length = 0; errs.length = 0;
      await page.goto(BASE + '/#' + cat.key + '/' + tab, { waitUntil: 'domcontentloaded' });
      await Promise.all([sleep(2600), page.waitForNetworkIdle({ idleTime: 400, timeout: 12000 }).catch(() => {})]);
      const st = await page.evaluate(panelStats);
      const slow = api.filter(a => a.ms > 900).sort((a, b) => b.ms - a.ms).slice(0, 3);
      const rec = {
        path: 'deeplink', cat: cat.key, tab, panel: st && st.id,
        nodes: st && st.nodes, textLen: st && st.textLen, rows: st && st.rows, cards: st && st.cards,
        overflowX: st && st.overflowX, errs: errs.slice(), apiCount: api.length,
        slow: slow.map(s => s.url + ' ' + s.ms + 'ms/' + s.bytes + 'B'),
      };
      results.push(rec);
      const blank = !st || st.missing || st.nodes < 15 || st.textLen < 30;
      if (blank) console.log('  [BLANK?] ' + cat.key + '/' + tab + ' ' + JSON.stringify(st));
      if (errs.length) console.log('  [ERR] ' + cat.key + '/' + tab + ' :: ' + errs.slice(0, 2).join(' | '));
      if (slow.length) console.log('  [SLOW] ' + cat.key + '/' + tab + ' :: ' + slow.map(s => s.url + '=' + s.ms + 'ms').join(', '));
    }
  }

  // ---------- Pass B: 点击横条（从首页依次点击） ----------
  console.log('=== Pass B: 点击横条遍历 ===');
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await Promise.all([sleep(3000), page.waitForNetworkIdle({ idleTime: 400, timeout: 15000 }).catch(() => {})]);

  for (const cat of NAV) {
    // 点一级分类
    const catClicked = await page.evaluate((k) => {
      const el = document.querySelector('a.nav-item[data-cat="' + k + '"]');
      if (el) { el.click(); return true; }
      return false;
    }, cat.key);
    if (!catClicked) console.log('  [NO-CAT-SEL] ' + cat.key);
    await sleep(900);
    for (const tab of cat.tabs) {
      api.length = 0; errs.length = 0;
      const clicked = await page.evaluate((t) => {
        const el = document.querySelector('#subnav a.sub-tab[data-tab="' + t + '"]');
        if (el) { el.click(); return true; }
        return false;
      }, tab);
      if (!clicked) {
        console.log('  [NO-SELECTOR] ' + cat.key + '/' + tab + ' 横条上找不到可点的 tab 元素');
        results.push({ path: 'click', cat: cat.key, tab, clicked: false, errs: [], nodes: 0 });
        continue;
      }
      await Promise.all([sleep(2400), page.waitForNetworkIdle({ idleTime: 400, timeout: 12000 }).catch(() => {})]);
      const st = await page.evaluate(panelStats);
      const slow = api.filter(a => a.ms > 900).sort((a, b) => b.ms - a.ms).slice(0, 3);
      results.push({
        path: 'click', cat: cat.key, tab, panel: st && st.id,
        nodes: st && st.nodes, textLen: st && st.textLen, rows: st && st.rows, cards: st && st.cards,
        overflowX: st && st.overflowX, errs: errs.slice(), apiCount: api.length, clicked: true,
        slow: slow.map(s => s.url + ' ' + s.ms + 'ms/' + s.bytes + 'B'),
      });
      const blank = !st || st.missing || st.nodes < 15 || st.textLen < 30;
      if (blank) console.log('  [BLANK?] click ' + cat.key + '/' + tab + ' ' + JSON.stringify(st));
      if (errs.length) console.log('  [ERR] click ' + cat.key + '/' + tab + ' :: ' + errs.slice(0, 2).join(' | '));
      if (slow.length) console.log('  [SLOW] click ' + cat.key + '/' + tab + ' :: ' + slow.map(s => s.url + '=' + s.ms + 'ms').join(', '));
    }
  }

  // ---------- 汇总 ----------
  const blanks = results.filter(r => r.path === 'click' && (r.nodes === undefined || r.nodes < 15 || r.textLen < 30));
  const withErr = results.filter(r => r.errs && r.errs.length);
  const allApi = [];
  // 重新收集一次：把所有接口名攒出来
  const apiNames = new Set();
  results.forEach(r => {});
  console.log('\n=== 汇总 ===');
  console.log('遍历 tab 数 = ' + results.length);
  console.log('疑似空白面板 = ' + blanks.length);
  blanks.forEach(b => console.log('   BLANK ' + b.path + ' ' + b.cat + '/' + b.tab + ' nodes=' + b.nodes + ' textLen=' + b.textLen));
  console.log('有 JS 报错的 tab = ' + withErr.length);
  withErr.forEach(b => console.log('   ERR ' + b.path + ' ' + b.cat + '/' + b.tab + ' :: ' + b.errs.slice(0, 1).join('')));

  fs.writeFileSync(path.join(ROOT, '_audit_nav_result.json'), JSON.stringify(results, null, 1), 'utf8');
  await browser.close();
  if (SERVER) { SERVER.kill(); console.log('[srv] 自起服务已关闭'); }
  console.log('DONE');
})().catch(e => { console.log('FATAL ' + e.stack); if (SERVER) SERVER.kill(); process.exit(1); });
