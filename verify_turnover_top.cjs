/* 2026-09-30 阴跌板块·全市场值得率 Top30 回归护栏
   断言：① 接口 /api/screener_turnover_top 200 且 ok=true、rows=30、分数降序
        ② 阴跌控制条出现「全市场值得率 Top30」按钮，点击后渲染 Top 模式表头
        ③ 30 行 chip 分数单调不升；行内代码可点开个股抽屉
        ④ 页面零 JS 报错 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const BASE = 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const results = [];
  const ok = (name, cond) => results.push([cond ? 'PASS' : 'FAIL', name]);
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: 'new', args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  const jsErrors = [];
  page.on('pageerror', (e) => jsErrors.push(String(e)));
  await page.setViewport({ width: 1440, height: 950 });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);

  // ① 接口直测（应命中缓存秒回）
  const apiResp = await page.evaluate(async () => {
    const r = await fetch('/api/screener_turnover_top?days=30&limit=30');
    return { status: r.status, body: await r.json() };
  });
  ok('接口 /api/screener_turnover_top 返回 200', apiResp.status === 200);
  ok('ok=true 且 pool_size>=1000', apiResp.body.ok === true && (apiResp.body.pool_size || 0) >= 1000);
  const rows = apiResp.body.rows || [];
  ok('返回 30 行', rows.length === 30);
  const scores = rows.map((r) => (r.score == null ? -1 : r.score));
  ok('分数降序单调', scores.every((v, i) => i === 0 || scores[i - 1] >= v));

  // ② 前端：进阴跌板块 → 点全市场按钮
  await page.evaluate(() => selectNav('watch', 'screener', true));
  await sleep(800);
  await page.click('[data-feat-screen="decline"]');
  await sleep(800);
  const btn = await page.$('#scDnTop');
  ok('控制条出现「全市场值得率 Top30」按钮', !!btn);
  await page.click('#scDnTop');
  // Top 模式数据应已缓存（接口秒回），轮询等表头与真实数据行
  let headerTxt = '', nData = 0;
  for (let i = 0; i < 45; i++) {
    await sleep(2000);
    const r = await page.evaluate(() => {
      const th = document.querySelector('#p-screener .sc-dn-tb thead');
      const tb = document.querySelector('#p-screener .sc-dn-tb tbody');
      return { h: th ? th.textContent : '', n: tb ? tb.querySelectorAll('tr [data-wt-open]').length : 0 };
    });
    headerTxt = r.h; nData = r.n;
    if (nData > 0 && headerTxt.includes('位置分位')) break;
  }
  ok('Top 模式表头渲染（含位置分位列）', headerTxt.includes('位置分位') && headerTxt.includes('入手值得率'));
  ok('Top 模式渲染 30 行数据', nData === 30);

  // ③ chip 分数降序（只认真实 chip）
  const mono = await page.evaluate(() => {
    const tb = document.querySelector('#p-screener .sc-dn-tb tbody');
    const chips = [...tb.querySelectorAll('tr .sc-score b')].map((el) => parseInt(el.textContent, 10));
    return { n: chips.length, mono: chips.every((v, i) => i === 0 || chips[i - 1] >= v) };
  });
  ok('页面 chip 分数降序（30 个）', mono.n === 30 && mono.mono);

  // ④ 行内代码可点开个股抽屉
  await page.evaluate(() => { document.querySelector('#p-screener .sc-dn-tb tbody [data-wt-open]').click(); });
  let tHtml = '';
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    tHtml = await page.evaluate(() => {
      const h = document.querySelector('#stkTurnover');
      return h ? h.innerHTML : '';
    });
    if (tHtml.includes('sc-score')) break;
  }
  ok('Top 行点击可打开个股抽屉且含换手率块', /sc-score[^>]*><b>\d+<\/b>/.test(tHtml));

  ok('页面零 JS 报错', jsErrors.length === 0);
  console.log(results.map(([s, n]) => `${s}  ${n}`).join('\n'));
  console.log(jsErrors.length ? 'JS_ERRORS:\n' + jsErrors.join('\n') : '');
  await browser.close();
  process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
