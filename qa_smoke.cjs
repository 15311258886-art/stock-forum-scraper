// 启动验证 / 冒烟测试：验证"能不能点开" + 点个股能否看 K线/PE/ROE
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BASE = 'http://127.0.0.1:8765/';
const OUT = 'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/qa_run';
const fs = require('fs');
fs.mkdirSync(OUT, { recursive: true });
(async () => {
  const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  const page = await browser.newPage();
  const pageErrors = [], consoleErrors = [];
  page.on('pageerror', e => pageErrors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await new Promise(r => setTimeout(r, 3000));
  const navCount = await page.evaluate(() => document.querySelectorAll('.nav-item').length);
  // 尝试真实点击一个股票入口
  let realSym = await page.evaluate(() => {
    const el = document.querySelector('[data-sym],a[data-code-open],.kline-chip[data-sym]');
    return el ? (el.getAttribute('data-sym') || el.getAttribute('data-code-open') || '') : '';
  });
  let clickedReal = false;
  if (realSym) {
    await page.evaluate(s => { const el = document.querySelector(`[data-sym="${s}"],a[data-code-open="${s}"]`); if (el) el.click(); }, realSym);
    await new Promise(r => setTimeout(r, 3000));
    clickedReal = await page.evaluate(() => !!document.querySelector('#drawer.show'));
  }
  // 兜底：直接调用 openStock 验证抽屉三段渲染
  if (!clickedReal || !(await page.evaluate(() => !!document.querySelector('#drawer.show')))) {
    await page.evaluate(() => { if (typeof openStock === 'function') openStock('600519'); });
    await new Promise(r => setTimeout(r, 3500));
  }
  const drawerOK = await page.evaluate(() => {
    const d = document.querySelector('#drawer.show'); if (!d) return false;
    return (d.innerText || '').trim().length > 200;
  });
  const dtext = await page.evaluate(() => {
    const d = document.querySelector('#drawer.show'); return d ? (d.innerText || '').slice(0, 300) : '';
  });
  const hasPE = /市盈率|PE|净资产收益率|ROE|K线|主营/.test(dtext);
  await page.screenshot({ path: OUT + '/qa_smoke.png' });
  const result = {
    navCount, realSym, clickedReal, drawerOK, hasPE, drawerTextLen: dtext.length,
    pageErrors: pageErrors.length, consoleErrors: consoleErrors.length,
    sampleErrors: pageErrors.slice(0, 4), sampleConsole: consoleErrors.slice(0, 4),
    drawerHead: dtext.slice(0, 80)
  };
  console.log('SMOKE_RESULT ' + JSON.stringify(result));
  fs.writeFileSync(OUT + '/qa_smoke.json', JSON.stringify(result, null, 2));
  await browser.close();
})().catch(e => { console.error('SMOKE_FATAL', e); process.exit(2); });
