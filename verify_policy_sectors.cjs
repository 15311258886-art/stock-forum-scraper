/* 回归验证：政策风向「板块形势研判」代表股（2026-10-01）
   需求（王总原话）：「每一个子板块，比如什么传媒啊，旅游酒店啊等等的这些代表性股份可以
   再多列几个，当然了，还是按照这个知名度等等的这个排序，越靠前的越排前面。」

   改动口径：代表股改走 sector_leaders.py 人工名单（按知名度/总市值降序，每板块 8 只），
   实时涨跌幅走新浪批量行情；静态名单里没有的板块才回退新浪节点。

   隔离：复制生产库（app.db 三件套）到临时目录，VIEWER_DB 指向副本、VIEWER_PORT=8807；
   本脚本只读，不写生产数据。

   用法：node verify_policy_sectors.cjs   （截图 _pol_sectors_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8807;
const PY = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\.venv\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_pol_sectors_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;

const ISO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'vw_pol_iso_'));
const ISO_DB = path.join(ISO_DIR, 'app.db');
for (const suf of ['', '-wal', '-shm']) {
  const src = path.join(BASE, 'viewer', 'app.db' + suf);
  if (fs.existsSync(src)) fs.copyFileSync(src, ISO_DB + suf);
}
console.log('ISO_DB =', ISO_DB);

const PW = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
let CHROME = null;
if (fs.existsSync(PW)) {
  for (const d of fs.readdirSync(PW)) {
    if (d.startsWith('chromium-')) {
      const p = path.join(PW, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(p)) { CHROME = p; break; }
    }
  }
}
console.log('CHROME =', CHROME);

const env = { ...process.env, VIEWER_PORT: String(PORT), VIEWER_DB: ISO_DB };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];
const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', d => srvLog += d.toString());
srv.stderr.on('data', d => srvLog += d.toString());
const sleep = ms => new Promise(r => setTimeout(r, ms));

const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) fails.push(msg); };

(async () => {
  try {
    for (let i = 0; i < 80; i++) {
      try { const r = await fetch(ROOT + '/api/summary'); if (r.status === 200) break; } catch (e) { /* retry */ }
      await sleep(500);
    }

    console.log('\n=== 1. 接口层（隔离副本） ===');
    const pol = await (await fetch(ROOT + '/api/policy_desk?num=80')).json();
    ok(pol && pol.ok === true, '/api/policy_desk 返回正常');
    const tops = (pol.verdict && pol.verdict.top) || [];
    ok(tops.length >= 4, `研判板块数 ${tops.length}`);

    let minMembers = 999;
    tops.forEach(t => { minMembers = Math.min(minMembers, (t.members || []).length); });
    console.log('  每板块代表股数 = ', tops.map(t => `${t.sector}:${(t.members || []).length}`).join(' '));
    ok(minMembers >= 6, `每个研判板块至少 6 只代表股（最少 ${minMembers} 只）`);

    // 顺序：名单第一只必须排最前（知名度优先）
    const expect = {
      '传媒': '东方财富', '旅游酒店': '中国中免', '农业': '牧原股份', '影视': '光线传媒',
      '零售': '永辉超市', '家电': '美的集团', '食品饮料': '贵州茅台', '体育产业': '中体产业',
    };
    let orderBad = [];
    tops.forEach(t => {
      const want = expect[t.sector];
      const m = (t.members || [])[0] || {};
      if (want && (m.name || '') !== want) orderBad.push(`${t.sector} 首位=${m.name} 期望=${want}`);
    });
    ok(orderBad.length === 0, `代表股按知名度降序（越靠前越前）${orderBad.length ? ' :: ' + orderBad.join(' | ') : ''}`);

    // 串板块检查：新浪节点口径错乱的典型症状必须消失
    const forbidden = [
      ['旅游酒店', ['巨人网络', '藏格矿业', '工商银行', '东方财富']],
      ['零售', ['藏格矿业', '江西铜业', '贵州茅台']],
      ['农业', ['通威股份', '紫金矿业']],
      ['影视', ['中国中免']],
      ['券商', ['工商银行', '建设银行']],
    ];
    let cross = [];
    tops.forEach(t => {
      const rule = forbidden.find(f => f[0] === t.sector);
      if (!rule) return;
      (t.members || []).forEach(m => { if (rule[1].includes(m.name)) cross.push(`${t.sector}←${m.name}`); });
    });
    ok(cross.length === 0, `无串板块个股${cross.length ? ' :: ' + cross.join(' | ') : ''}`);

    // 实时价：pct 必须带回来（不是 null）
    const withPct = tops.reduce((n, t) => n + (t.members || []).filter(m => m.pct != null).length, 0);
    const totalM = tops.reduce((n, t) => n + (t.members || []).length, 0);
    ok(totalM > 0 && withPct === totalM, `全部代表股带当日涨跌幅（${withPct}/${totalM}）`);

    console.log('\n=== 2. 前端层（无头 Chromium） ===');
    const browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'],
      defaultViewport: { width: 1440, height: 1000 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
    page.on('response', r => { if (r.status() >= 400) errs.push(`HTTP ${r.status()} ${r.url()}`); });

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#sidenav .nav-item', { timeout: 20000 });
    await sleep(800);
    await page.click('#sidenav .nav-item[data-cat="info"]');
    await sleep(1000);
    // 子 tab 里的「政策风向」（按文字匹配，不写死 data-tab 值）
    const tabInfo = await page.evaluate(() => {
      const tabs = Array.from(document.querySelectorAll('#subnav .sub-tab'));
      const hit = tabs.find(a => /政策风向/.test(a.textContent || ''));
      return hit ? { tab: hit.dataset.tab, n: tabs.length } : null;
    });
    console.log('  subnav =', JSON.stringify(tabInfo));
    ok(!!tabInfo, `找到「政策风向」子 tab（共 ${tabInfo ? tabInfo.n : 0} 个）`);
    const subSel = `#subnav .sub-tab[data-tab="${tabInfo ? tabInfo.tab : ''}"]`;
    await page.click(subSel);
    await page.waitForSelector('#p-policy .pol-judge', { timeout: 20000 });
    await sleep(1200);

    const panel = await page.$eval('#p-policy', el => ({
      cards: el.querySelectorAll('.pol-judge').length,
      chips: el.querySelectorAll('.pol-judge .pol-jm .pol-stk').length,
      firstCard: (el.querySelector('.pol-judge .pol-jm') || {}).innerText || '',
      secs: Array.from(el.querySelectorAll('.pol-judge .pol-jsec')).map(x => x.innerText),
    }));
    console.log('  panel =', JSON.stringify(panel, null, 2));
    ok(panel.cards >= 4, `研判卡片渲染 ${panel.cards} 张`);
    ok(panel.chips >= 24, `代表股 chip 共 ${panel.chips} 个（每卡 ≥6）`);
    ok(panel.firstCard.split('\n').length >= 6, `单卡代表股 ≥6 只（首卡 ${panel.firstCard.split('\n').length} 行）`);

    const overflow = await page.evaluate(() => {
      const host = document.querySelector('#p-policy');
      if (!host) return { err: 'no panel' };
      const hostRect = host.getBoundingClientRect();
      const scrollable = (el) => {
        let p = el;
        while (p && p !== document.documentElement) {
          const cs = getComputedStyle(p);
          if (/auto|scroll/.test(cs.overflowX) || /auto|scroll/.test(cs.overflow)) return true;
          p = p.parentElement;
        }
        return false;
      };
      const bad = [];
      host.querySelectorAll('*').forEach(el => {
        if (scrollable(el)) return;
        const cs = getComputedStyle(el);
        if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
          bad.push({ cls: (el.className || '').toString().slice(0, 40), tag: el.tagName, sw: el.scrollWidth, cw: el.clientWidth });
        }
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > hostRect.right + 2) {
          bad.push({ cls: (el.className || '').toString().slice(0, 40), tag: el.tagName, over: Math.round(r.right - hostRect.right) });
        }
      });
      // 代表股区实际占几行（衡量卡片是否因此被拉高）
      const jm = host.querySelector('.pol-judge .pol-jm');
      const rows = jm ? new Set(Array.from(jm.querySelectorAll('.pol-stk')).map(a => Math.round(a.getBoundingClientRect().top))).size : 0;
      return { bad: bad.slice(0, 12), total: bad.length, chipRows: rows };
    });
    console.log('  overflow =', JSON.stringify(overflow));
    ok(overflow.total === 0, `面板内无真溢出 / 越界元素（${overflow.total} 处）`);
    ok(overflow.chipRows <= 4, `代表股区行数 ${overflow.chipRows} 行（≤4，卡片不被拉变形）`);

    await page.screenshot({ path: SHOT, fullPage: false });
    // 卡片整体（含代表股区）再截一张
    const card = await page.$('#p-policy .pol-judges');
    if (card) { try { await card.screenshot({ path: path.join(BASE, '_pol_sectors_cards.png') }); } catch (e) { /* ignore */ } }
    console.log('  SHOT =', SHOT);
    ok(errs.length === 0, `无控制台报错 / 4xx（${errs.length} 条）${errs.length ? ' :: ' + errs.slice(0, 3).join(' | ') : ''}`);

    await browser.close();
    srv.kill();

    console.log('\n==================== 汇总 ====================');
    if (fails.length) { console.log('FAIL ' + fails.length + ' 项：'); fails.forEach(f => console.log('  - ' + f)); }
    else console.log('全部 PASS（' + '接口 + 前端 + 排版 + 串板块检查' + '）');
    process.exit(fails.length ? 1 : 0);
  } catch (e) {
    console.log('EXCEPTION =', e && e.stack ? e.stack : e);
    console.log('--- server log ---\n' + srvLog.slice(-2000));
    try { srv.kill(); } catch (_) { /* ignore */ }
    process.exit(2);
  }
})();
