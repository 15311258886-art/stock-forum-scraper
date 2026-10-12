/* 端到端验证：个股抽屉（重要信息三组 + 价值标准三盏灯）+ K 线拖动/缩放/十字光标。
   用真实 Chromium 打开页面 → 打开贵州茅台 → 真模拟鼠标拖动与滚轮 → 断言视口变化。 */
const puppeteer = require('puppeteer-core');

const URL = process.env.VERIFY_URL || 'http://127.0.0.1:8899';
const CHROME = process.env.XQ_CHROME_EXE ||
  'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--ignore-certificate-errors', '--disable-blink-features=AutomationControlled',
           '--window-size=1440,900'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  const errors = [];
  page.on('pageerror', (e) => errors.push('PAGEERROR ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('CONSOLE ' + m.text()); });
  const bad = [];
  page.on('response', (r) => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url().slice(0, 90)); });

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);

  // 直接调用应用自身的 openStock（顶层函数声明，全局可见）
  await page.evaluate(() => openStock('600519'));

  // 等 K 线视口就绪
  let ok = false;
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    ok = await page.evaluate(() => !!(typeof _kv !== 'undefined' && _kv && _kv.rows && _kv.rows.length));
    if (ok) break;
  }
  console.log('=== 1) K 线数据就绪 ===', ok ? 'YES' : 'NO');

  const snap = await page.evaluate(() => {
    const t = (s) => { const e = document.querySelector(s); return e ? e.textContent.replace(/\s+/g, ' ').trim() : null; };
    return {
      quote: t('#stkQuote'),
      range: t('#klineRange'),
      recentTitle: t('.kline-recent .kr-title'),
      drawerHasViews: /多空观点明细/.test(document.querySelector('#drawerBody')?.textContent || ''),
      drawerHasHolders: /共同持仓/.test(document.querySelector('#drawerBody')?.textContent || ''),
      drawerHasPosSec: /持仓选手/.test(document.querySelector('#drawerBody')?.textContent || ''),
      kvRows: (() => { const k = (typeof _kv !== 'undefined') ? _kv : null; return k ? {
        rows: k.rows.length, count: k.count, offset: k.offset,
        adjust: k.adjust, first: k.rows[0].date, last: k.rows[k.rows.length - 1].date,
      } : null; })(),
      canvas: (() => { const c = document.querySelector('#klineCanvas'); return c ? { w: c.width, h: c.height, cssW: c.style.width } : null; })(),
      lamps: Array.from(document.querySelectorAll('.vl-item')).map((e) => e.textContent.replace(/\s+/g, ' ').trim()),
    };
  });

  console.log('=== 2) 重要信息（前 320 字） ===');
  console.log((snap.quote || '').slice(0, 320));
  console.log('  价值标准三盏灯:', JSON.stringify(snap.lamps, null, 0));
  console.log('=== 3) 抽屉板块检查 ===');
  console.log('  多空观点明细存在(应为 false):', snap.drawerHasViews);
  console.log('  共同持仓存在(应为 false):', snap.drawerHasHolders);
  console.log('  持仓选手存在(应为 true):', snap.drawerHasPosSec);
  console.log('=== 4) 初始视口 ===');
  console.log('  ', JSON.stringify(snap.kvRows));
  console.log('  range:', snap.range);
  console.log('  canvas:', JSON.stringify(snap.canvas));
  console.log('  recent 标题:', snap.recentTitle);

  // 画布在抽屉滚动区下方，必须先滚动到可视区，否则鼠标事件落在视口外（测试前置，非应用缺陷）
  await page.evaluate(() => {
    const c = document.querySelector('#klineCanvas');
    if (c) c.scrollIntoView({ block: 'center' });
  });
  await sleep(600);

  const box = await page.evaluate(() => {
    const c = document.querySelector('#klineCanvas');
    const r = c.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  });
  console.log('=== 4b) 画布位置（滚动后） ===', JSON.stringify(box),
              '| 视口内:', box.y >= 0 && box.y + box.h <= 900);
  const cy = box.y + box.h / 2;

  // ---- 拖动平移（约定：内容跟随光标 → 向右拖 = 看更早） ----
  const off0 = await page.evaluate(() => _kv.offset);
  const x0 = box.x + 40;
  await page.mouse.move(x0, cy);
  await page.mouse.down();
  for (let i = 1; i <= 18; i++) await page.mouse.move(x0 + i * 35, cy);
  await page.mouse.up();
  await sleep(300);
  const off1 = await page.evaluate(() => _kv.offset);
  console.log('=== 5) 拖动平移 ===');
  console.log(`  offset ${off0} → ${off1}  ${off1 < off0 ? 'OK（向右拖动=看更早历史）' : 'FAIL'}`);
  console.log('  range:', await page.evaluate(() => document.querySelector('#klineRange').textContent.replace(/\s+/g, ' ')));

  // ---- 一直拖到最早一根 ----
  let iter = 0, reached = false;
  for (; iter < 80; iter++) {
    const cur = await page.evaluate(() => _kv.offset);
    if (cur <= 0) { reached = true; break; }
    const a = box.x + 20, b = box.x + box.w - 20;
    await page.mouse.move(a, cy);
    await page.mouse.down();
    for (let i = 1; i <= 18; i++) await page.mouse.move(a + (b - a) * i / 18, cy);
    await page.mouse.up();
    await sleep(50);
  }
  const rangeEarliest = await page.evaluate(() => document.querySelector('#klineRange').textContent.replace(/\s+/g, ' '));
  console.log('=== 6) 拖到最早一根 ===');
  console.log(`  拖了 ${iter} 轮 | reached=${reached}`);
  console.log('  range:', rangeEarliest);
  console.log('  含「已拖到最早一根」:', /已拖到最早一根/.test(rangeEarliest));

  // ---- 回到最新 ----
  await page.evaluate(() => document.querySelector('#klineLatest').click());
  await sleep(400);
  const offBack = await page.evaluate(() => _kv.offset);
  const maxOff = await page.evaluate(() => _kv.rows.length - _kv.count);
  console.log('=== 7) 回到最新 ===');
  console.log(`  offset=${offBack} maxOffset=${maxOff} ${offBack === maxOff ? 'OK' : 'FAIL'}`);

  // ---- 逐屏翻页按钮（◀ 更早 / 更晚 ▶） ----
  await page.evaluate(() => document.querySelector('#klineOlder').click());
  await sleep(300);
  const oOlder = await page.evaluate(() => _kv.offset);
  await page.evaluate(() => document.querySelector('#klineNewer').click());
  await sleep(300);
  const oNewer = await page.evaluate(() => _kv.offset);
  console.log('=== 7b) 逐屏翻页按钮 ===');
  console.log(`  最新(${offBack}) --「◀更早」--> ${oOlder} --「更晚▶」--> ${oNewer}  `
    + `${oOlder < offBack && oNewer > oOlder ? 'OK' : 'FAIL'}`);
  const hintTxt = await page.evaluate(() => {
    const el = document.querySelector('#drawerBody .detail-sub:nth-of-type(1)');
    const subs = Array.from(document.querySelectorAll('#drawerBody .detail-sub'));
    const k = subs.find((e) => /K 线走势/.test(e.textContent));
    return k ? k.textContent.replace(/\s+/g, ' ').trim() : null;
  });
  console.log('  K线提示语:', hintTxt);

  // ---- 滚轮缩放 ----
  const c0 = await page.evaluate(() => _kv.count);
  await page.mouse.move(box.x + box.w / 2, cy);
  await page.mouse.wheel({ deltaY: -600 });
  await sleep(400);
  const c1 = await page.evaluate(() => _kv.count);
  await page.mouse.wheel({ deltaY: 1200 });
  await sleep(400);
  const c2 = await page.evaluate(() => _kv.count);
  console.log('=== 8) 滚轮缩放 ===');
  console.log(`  可见根数 ${c0} --(放大)--> ${c1} --(缩小)--> ${c2}  ${c1 < c0 && c2 > c1 ? 'OK' : 'FAIL'}`);

  // ---- 悬停十字光标 ----
  await page.mouse.move(box.x + box.w * 0.4, cy);
  await sleep(350);
  const tip = await page.evaluate(() => {
    const t = document.querySelector('#klineTip');
    return { hidden: t.hidden, text: (t.textContent || '').replace(/\s+/g, ' ').trim(), cross: (typeof _kv !== 'undefined' && _kv) ? _kv.crossIdx : null };
  });
  console.log('=== 9) 悬停十字光标 ===');
  console.log('  tip.hidden =', tip.hidden, '| crossIdx =', tip.cross);
  console.log('  tip 文本:', tip.text);

  // ---- 周 K 切换 ----
  await page.evaluate(() => { const b = document.querySelector('#klineWrap .kbtn[data-p="week"]'); b.click(); });
  let wok = false;
  for (let i = 0; i < 25; i++) {
    await sleep(800);
    wok = await page.evaluate(() => _kv && _kv.period === 'week' && _kv.rows.length > 100);
    if (wok) break;
  }
  const wInfo = await page.evaluate(() => ({ period: _kv.period, rows: _kv.rows.length, first: _kv.rows[0].date, last: _kv.rows[_kv.rows.length - 1].date }));
  console.log('=== 10) 周 K 切换 ===', wok ? 'OK' : 'FAIL', JSON.stringify(wInfo));

  // ---- 周 K 的周期标注（避免与「重要信息」的日涨跌被误读为矛盾） ----
  const wk = await page.evaluate(() => ({
    snap: (document.querySelector('#klineSnap') || { textContent: '' }).textContent.replace(/\s+/g, ' ').trim(),
    recent: (document.querySelector('.kline-recent .kr-title') || { textContent: '' }).textContent.replace(/\s+/g, ' ').trim(),
  }));
  console.log('=== 10b) 周K 周期标注 ===');
  console.log('  快照:', wk.snap.slice(0, 220));
  console.log('  明细标题:', wk.recent);
  console.log('  周涨跌:', /周涨跌/.test(wk.snap), '| 上周收:', /上周收/.test(wk.snap),
              '| 成交额标≈:', /成交额\s*≈/.test(wk.snap), '| 明细含周K:', /周K/.test(wk.recent));

  await page.screenshot({ path: '_kline_viewport.png', fullPage: false });
  console.log('=== 11) 截图 -> _kline_viewport.png ===');
  console.log('页面报错:', errors.length ? errors.slice(0, 6) : '无');
  console.log('非 2xx 请求:', bad.length ? [...new Set(bad)].slice(0, 8) : '无');

  await browser.close();
})().catch((e) => { console.error('VERIFY_FATAL', e.stack || e.message); process.exit(1); });
