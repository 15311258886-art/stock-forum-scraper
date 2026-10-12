// 确定性探针：锁定「diag→multiquote 面板为空」的根因。
// 通过 wrap api / loadMultiquote / selectNav + hashchange 日志，判断是否：
//   (a) selectNav 在切到 multiquote 时根本没跑到 8127（hashchange 未触发或中途抛错）
//   (b) loadMultiquote 被调用但 api('/api/multiquote_preset') 没发出
//   (c) api 发了但渲染时序问题
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function inject(page) {
  await page.evaluate(() => {
    window.__log = [];
    window.__pe = [];
    const _api = window.api;
    window.api = function (path, opts) {
      window.__log.push('API ' + ((opts && opts.method) || 'GET') + ' ' + path);
      return _api.apply(this, arguments);
    };
    const _lm = window.loadMultiquote;
    window.loadMultiquote = function () { window.__log.push('>> loadMultiquote()'); return _lm.apply(this, arguments); };
    const _sn = window.selectNav;
    window.selectNav = function (c, t, p) { window.__log.push('>> selectNav(' + c + ',' + t + ',' + p + ')'); return _sn.apply(this, arguments); };
    window.addEventListener('hashchange', () => window.__log.push('HASHCHANGE -> ' + location.hash));
    window.addEventListener('error', (e) => window.__pe.push('ERR ' + (e.message || e.error)));
  });
}

async function panelState(page, tab) {
  return await page.evaluate((t) => {
    const box = document.querySelector('#p-' + t);
    const txt = box ? (box.textContent || '').replace(/\s+/g, ' ').trim() : null;
    return {
      found: !!box,
      active: box ? box.classList.contains('active') : false,
      len: txt ? txt.length : 0,
      head: txt ? txt.slice(0, 80) : null,
      mqData: (window.FEAT && window.FEAT.multiquote) ? {
        hasData: !!window.FEAT.multiquote.data,
        rows: window.FEAT.multiquote.data ? (window.FEAT.multiquote.data.rows || []).length : -1,
      } : null,
    };
  }, tab);
}

async function getLog(page) {
  return await page.evaluate(() => ({ log: window.__log || [], pe: window.__pe || [] }));
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--ignore-certificate-errors', '--disable-blink-features=AutomationControlled',
      '--proxy-server=direct://', '--proxy-bypass-list=*', '--window-size=1600,1000'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  page.on('pageerror', (e) => console.log('  [PAGEERROR]', e.message.slice(0, 200)));
  page.on('response', (r) => { if (r.url().includes('/api/')) console.log('  [NET]', r.status(), r.url().replace(URL, '')); });

  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(2500);
  await inject(page);

  // ===== SEQ A: 干净状态直接进 multiquote =====
  console.log('\n===== SEQ A: 直接进 multiquote =====');
  await page.evaluate(() => { location.hash = '#watch/multiquote'; });
  await sleep(4500);
  let st = await panelState(page, 'multiquote');
  let lg = await getLog(page);
  console.log('  panel:', JSON.stringify(st));
  console.log('  log:', JSON.stringify(lg.log));
  console.log('  pageerrors:', JSON.stringify(lg.pe));

  // ===== SEQ B: diag → multiquote（复刻审计顺序）=====
  console.log('\n===== SEQ B: diag(填600519→点Go,9s) → multiquote =====');
  await page.evaluate(() => { location.hash = '#watch/diag'; });
  await sleep(2600);
  await page.evaluate(() => {
    const inp = document.querySelector('#dgSym');
    if (inp) { inp.value = '600519'; inp.dispatchEvent(new Event('input', { bubbles: true })); }
    const btn = document.querySelector('#dgGo'); if (btn) btn.click();
  });
  await sleep(9000);
  const diagSt = await page.evaluate(() => {
    const box = document.querySelector('#p-diag');
    return { len: box ? (box.textContent || '').replace(/\s+/g, ' ').trim().length : 0,
             diagSymbol: window.DIAG ? window.DIAG.symbol : null,
             diagLoading: window.DIAG ? window.DIAG.loading : null,
             diagHasData: window.DIAG && window.DIAG.data ? !!window.DIAG.data.ok : false };
  });
  console.log('  diag after 9s:', JSON.stringify(diagSt));

  // 切到 multiquote（审计用的同一种 location.hash 方式）
  await page.evaluate(() => { location.hash = '#watch/multiquote'; });
  await sleep(4500);
  st = await panelState(page, 'multiquote');
  lg = await getLog(page);
  console.log('  panel:', JSON.stringify(st));
  console.log('  log (filtered):', JSON.stringify(lg.log.filter((x) => x.includes('multiquote') || x.includes('selectNav') || x.includes('HASHCHANGE') || x.includes('API'))));
  console.log('  pageerrors:', JSON.stringify(lg.pe));

  // 额外：若 SEQ B 失败，再给 5s 复测（防渲染抖动）
  if (st.len < 40) {
    await sleep(5000);
    st = await panelState(page, 'multiquote');
    lg = await getLog(page);
    console.log('  [复测+5s] panel:', JSON.stringify(st));
    console.log('  [复测+5s] log:', JSON.stringify(lg.log.filter((x) => x.includes('multiquote') || x.includes('selectNav') || x.includes('HASHCHANGE'))));
  }

  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.stack || e.message); process.exit(2); });
