/**
 * 龙虎榜板块 UI 端到端验证（2026-09-25）
 *
 * 背景：用户反馈「游资龙虎榜点开没内容」。根因之一是运行中的查看器是旧进程（无 /api/lhb 路由）。
 * 本次在扩展了 4 维度（个股+席位+营业部+机构）后，用真实 Chromium 打开临时端口的页面，
 * 点击「游资龙虎榜」，把渲染结果抓出来核对 —— 不看代码、只看实际渲染。
 *
 * 用法：先起临时服务（VIEWER_PORT=8899），再
 *   NODE_PATH=<workspace/node_modules> node _verify_lhb_ui.cjs
 */
const puppeteer = require('puppeteer-core');
const CHROME = process.env.XQ_CHROME_EXE ||
  'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const URL = process.env.VERIFY_URL || 'http://127.0.0.1:8899/';

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--window-size=1600,1000'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  const badResp = [];
  page.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) badResp.push(r.status() + ' ' + r.url()); });

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise(r => setTimeout(r, 1200));

  // 导航是两级结构：左栏大类 + 内容区顶部子标签（.sub-tab[data-tab]）；
  // 「游资龙虎榜」仅在「实盘操作」大类下渲染，故直接用 hash 导航最稳（hashchange 会驱动切换）。
  const clicked = await page.evaluate(() => {
    location.hash = "#trade/lhb";
    return true;
  });
  await new Promise(r => setTimeout(r, 5000));

  const out = await page.evaluate(() => {
    const box = document.querySelector('#p-lhb');
    if (!box) return { found: false, text: '(no #p-lhb)' };
    const tables = Array.from(box.querySelectorAll('table'));
    const tblInfo = tables.map((t, i) => {
      const head = Array.from(t.querySelectorAll('thead th')).map(th => th.textContent.trim());
      const bodyRows = t.querySelectorAll('tbody tr').length;
      const firstRow = t.querySelector('tbody tr');
      const cells = firstRow ? Array.from(firstRow.querySelectorAll('td')).map(td => td.textContent.trim()) : [];
      return { i, head, bodyRows, cells };
    });
    // 涨跌色对账（取一个已知下跌/上涨单元格）
    const colors = Array.from(box.querySelectorAll('td.bull,td.bear')).slice(0, 6).map(td => ({
      cls: td.className, txt: td.textContent.trim(),
      color: getComputedStyle(td).color,
    }));
    const subs = Array.from(box.querySelectorAll('.detail-sub')).map(e => e.textContent.trim().slice(0, 70));
    return {
      found: true,
      textLen: box.innerText.length,
      tableCount: tables.length,
      tblInfo,
      colors,
      subs,
      empty: !!box.querySelector('.empty'),
    };
  });

  console.log('CLICKED_NAV:', clicked);
  console.log('PANEL_FOUND:', out.found, '| textLen:', out.textLen, '| empty:', out.empty);
  console.log('SUBTITLES:', out.subs);
  console.log('TABLES:', out.tableCount);
  for (const t of out.tblInfo || []) {
    console.log(`  [表${t.i}] 行数=${t.bodyRows} 表头=${JSON.stringify(t.head)}`);
    if (t.cells.length) console.log(`         首行=${JSON.stringify(t.cells)}`);
  }
  console.log('涨跌色对账:', out.colors);
  console.log('前端报错:', errors.length ? errors.slice(0, 5) : '无');
  console.log('非2xx请求:', badResp.length ? [...new Set(badResp)].slice(0, 6) : '无');

  await page.screenshot({ path: '_lhb_shot.png', fullPage: false });
  console.log('SCREENSHOT -> _lhb_shot.png');

  // 日期切换验证：切到 2026-09-16（其后已走完 5 个交易日），榜后 D1/D5 应当有值
  await page.evaluate(() => {
    const el = document.querySelector('#p-lhb [data-lhb-date="2026-09-16"]');
    if (el) el.click();
  });
  await new Promise(r => setTimeout(r, 4500));
  const d16 = await page.evaluate(() => {
    const t = document.querySelectorAll('#p-lhb table')[0];
    if (!t) return [];
    return Array.from(t.querySelectorAll('tbody tr')).slice(0, 3)
      .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim()));
  });
  console.log('切到 2026-09-16 后首 3 行（末列前两位是榜后D1/D5）:');
  for (const r of d16) console.log('   ', JSON.stringify(r));
  console.log('09-16 榜后表现有值:', (d16 || []).some(r => r[8] !== '—' || r[9] !== '—'));

  // 滚动到「游资席位榜」区截图，便于目检排版
  await page.evaluate(() => {
    const e = Array.from(document.querySelectorAll('#p-lhb .detail-sub'))
      .find(x => x.textContent.includes('游资席位榜'));
    if (e) e.scrollIntoView({ block: 'start' });
  });
  await new Promise(r => setTimeout(r, 800));
  await page.screenshot({ path: '_lhb_seats.png' });
  console.log('SCREENSHOT -> _lhb_seats.png');
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
