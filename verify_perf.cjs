const puppeteer = require('puppeteer-core');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  let PASS = 0, FAIL = 0; const errors = [];
  const ok = (c, m) => { c ? PASS++ : FAIL++; console.log((c ? 'PASS' : 'FAIL') + ' ' + m); };
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: 'new', args: ['--no-sandbox']
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await page.evaluateOnNewDocument(() => { localStorage['extref.autoBackfillAt'] = String(Date.now()); });

  const t0 = Date.now();
  await page.goto('http://localhost:8765/#watch/watch', { waitUntil: 'domcontentloaded' });
  // 等关注池表格渲染
  let rows = 0;
  for (let i = 0; i < 50; i++) { await sleep(1000); rows = await page.$$eval('#p-watch .wt-tb tbody tr', r => r.length).catch(() => 0); if (rows > 0) break; }
  const tRender = (Date.now() - t0) / 1000;
  ok(rows > 0, `关注池表格渲染出来（${rows} 行），首屏到渲染约 ${tRender.toFixed(1)}s`);

  // 关键：不应出现「部分数据加载失败」横幅
  const banner = await page.evaluate(() => {
    const b = document.getElementById('apiErrorBar');
    return b ? b.textContent.trim() : '';
  });
  ok(banner.indexOf('部分数据加载失败') === -1, '无「部分数据加载失败」横幅' + (banner ? '（现有文案：' + banner.slice(0,40) + '）' : ''));

  // 备注列存在且紧跟战法信号
  const heads = await page.$$eval('#p-watch .wt-tb thead th', ths => ths.map(t => t.textContent.trim()));
  ok(heads.includes('备注') && heads.indexOf('备注') === heads.indexOf('战法信号') + 1, '备注列存在且紧跟战法信号：' + JSON.stringify(heads));

  // 战法列宽度受控（之前用户投诉太大）
  const stratW = await page.evaluate(() => { const td = document.querySelector('#p-watch td.wt-strat'); return td ? Math.round(td.getBoundingClientRect().width) : -1; });
  ok(stratW > 0 && stratW <= 320, '战法信号列宽受控 ≤320px（实测 ' + stratW + 'px）');

  // 控制台零报错
  ok(errors.length === 0, '控制台零报错' + (errors.length ? '：' + errors.join(' | ') : ''));

  await browser.close();
  console.log(FAIL === 0 ? 'PERF_PASS' : 'PERF_FAIL');
  process.exit(FAIL === 0 ? 0 : 1);
})().catch(e => { console.error('ERR', e); process.exit(1); });
