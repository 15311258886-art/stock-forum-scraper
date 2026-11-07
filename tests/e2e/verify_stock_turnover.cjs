/* 2026-09-30 个股详情·换手率实战块 回归护栏
   断言：① openStock 抽屉内 #stkTurnover 渲染出分数 chip（sc-score + 数字）
        ② 场景判定 / 换手率 / 位置分位 / 五维明细文案齐全
        ③ 接口 /api/stock_turnover 200
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

  // ① 接口直测
  const apiResp = await page.evaluate(async () => {
    const r = await fetch('/api/stock_turnover?symbol=688121');
    return { status: r.status, body: await r.json() };
  });
  ok('接口 /api/stock_turnover 返回 200', apiResp.status === 200);
  ok('接口返回 ok=true 且带分数', apiResp.body.ok === true && apiResp.body.score != null);

  // ② 打开个股详情抽屉（直接调 openStock，覆盖全局唯一入口）
  await page.evaluate(() => openStock('688121'));
  let tHtml = '';
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    tHtml = await page.evaluate(() => {
      const h = document.querySelector('#stkTurnover');
      return h ? h.innerHTML : '';
    });
    if (tHtml.includes('sc-score')) break;
  }
  ok('抽屉内渲染出入手值得率分数 chip', /sc-score[^>]*><b>\d+<\/b>/.test(tHtml));
  ok('场景判定文案渲染', tHtml.includes('场景判定'));
  ok('换手率数值渲染（含%）', /换手率 <b>[\d.]+%<\/b>/.test(tHtml));
  ok('位置分位渲染', tHtml.includes('位置分位'));
  ok('五维明细渲染', tHtml.includes('五维：'));

  // ③ 换一只票（主板，验证标定路径不影响其他板块）
  await page.evaluate(() => closeDrawer());
  await sleep(500);
  await page.evaluate(() => openStock('603823'));
  let t2 = '';
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    t2 = await page.evaluate(() => {
      const h = document.querySelector('#stkTurnover');
      return h ? h.innerHTML : '';
    });
    if (t2.includes('sc-score')) break;
  }
  ok('主板票(603823)同样渲染分数 chip', /sc-score[^>]*><b>\d+<\/b>/.test(t2));

  ok('页面零 JS 报错', jsErrors.length === 0);
  console.log(results.map(([s, n]) => `${s}  ${n}`).join('\n'));
  console.log(jsErrors.length ? 'JS_ERRORS:\n' + jsErrors.join('\n') : '');
  await browser.close();
  process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
