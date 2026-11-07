// 现场验证：连已运行的生产后端 8765（不另启、不杀），真用无头 Chromium 点开页面验证两个新页签
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const PORT = 8765;
const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const env = { ...process.env };
['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'].forEach(k => delete env[k]);

const fails = [];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails.push(m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function findChrome() {
  const cand = [];
  try {
    for (const d of fs.readdirSync(process.env.LOCALAPPDATA + '\\ms-playwright')) {
      if (d.startsWith('chromium-')) cand.push(process.env.LOCALAPPDATA + '\\ms-playwright\\' + d + '\\chrome-win64\\chrome.exe');
    }
  } catch (e) {}
  cand.push('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe');
  for (const p of cand) { try { if (fs.existsSync(p)) return p; } catch (e) {} }
  return null;
}

(async () => {
  const CHROME = findChrome();
  let browser = null;
  try {
    // 等服务就绪（连已运行的生产 8765）
    let up = false;
    for (let i = 0; i < 30; i++) {
      try { await fetch(`http://127.0.0.1:${PORT}/api/screener_boards?kind=industry&pz=5`); up = true; break; }
      catch (e) { await sleep(1000); }
    }
    ok(up, '后端就绪（8765 含新接口路由）');

    browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
    const page = await browser.newPage();
    const errs = [];
    page.on('pageerror', e => errs.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(2500);

    const clicked = await page.evaluate(() => {
      const nav = document.querySelector('.nav-item[data-cat="watch"]');
      if (nav) nav.click();
      setTimeout(() => { const st = document.querySelector('.sub-tab[data-tab="screener"]'); if (st) st.click(); }, 700);
      return nav ? nav.textContent.trim() : null;
    });
    ok(!!clicked, '导航点击我的票大类（NAV=' + clicked + '）');
    await sleep(3000);
    const onSc = await page.evaluate(() =>
      !!document.querySelector('#p-screener .wt-tab') && document.querySelector('#p-screener [data-feat-screen="value"]') !== null);
    ok(onSc, '智能选股面板已渲染');

    const tabs = await page.evaluate(() =>
      [...document.querySelectorAll('#p-screener [data-feat-screen]')].map(e => e.dataset.featScreen));
    ok(tabs.includes('boards'), '页签含「板块分类」');
    ok(tabs.includes('decline'), '页签含「连续阴跌」');

    // ===== 板块分类 =====
    await page.evaluate(() => { document.querySelector('#p-screener [data-feat-screen="boards"]').click(); });
    await sleep(2500);
    let nBoards = await page.evaluate(() => document.querySelectorAll('#p-screener .sc-board').length);
    ok(nBoards >= 20, `板块清单 chips≥20（实际 ${nBoards}）`);

    await page.evaluate(() => { document.querySelector('#p-screener .sc-board').click(); });
    let nRows = 0, hasSym = 0;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      nRows = await page.evaluate(() => document.querySelectorAll('#p-screener .wt-tb tbody tr').length);
      hasSym = await page.evaluate(() => document.querySelectorAll('#p-screener [data-wt-open]').length);
      if (nRows > 0) break;
    }
    ok(nRows >= 5, `点板块后成分股表渲染（rows=${nRows}）`);
    ok(hasSym >= 5, `成分股代码可点入口（data-wt-open=${hasSym}）`);
    const crumb = await page.evaluate(() => {
      const c = document.querySelector('#p-screener .sc-crumb');
      return c ? c.textContent.includes('返回板块列表') : false;
    });
    ok(crumb, '面包屑「返回板块列表」存在');

    const firstBefore = await page.evaluate(() =>
      (document.querySelector('#p-screener .wt-tb tbody tr td:nth-child(2)') || {}).textContent || '');
    await page.evaluate(() => { const el = document.querySelector('#p-screener [data-sc-bsort="pct"]'); if (el) el.click(); });
    await sleep(400);
    const firstAfter = await page.evaluate(() =>
      (document.querySelector('#p-screener .wt-tb tbody tr td:nth-child(2)') || {}).textContent || '');
    ok(firstBefore !== firstAfter, `排序切换生效（${firstBefore.trim()} → ${firstAfter.trim()}）`);

    await page.evaluate(() => { const el = document.querySelector('#p-screener [data-wt-open]'); if (el) el.click(); });
    let drawerShown = false;
    for (let i = 0; i < 16; i++) { await sleep(500); drawerShown = await page.evaluate(() => document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show')); if (drawerShown) break; }
    ok(drawerShown, '点成分股代码打开个股抽屉');
    await page.keyboard.press('Escape');
    await sleep(400);

    // ===== 连续阴跌 =====
    await page.evaluate(() => { document.querySelector('#p-screener [data-feat-screen="decline"]').click(); });
    await sleep(800);
    const hasCtl = await page.evaluate(() => !!document.querySelector('#scDnDays') && !!document.querySelector('#scDnGo'));
    ok(hasCtl, '阴跌控制条（窗口/下跌输入 + 筛选按钮）渲染');
    const nPreset = await page.evaluate(() => document.querySelectorAll('#p-screener [data-sc-dn]').length);
    ok(nPreset === 4, `便捷按钮 4 个（实际 ${nPreset}）`);

    await page.evaluate(() => { const el = document.querySelector('#p-screener [data-sc-dn="20,15"]'); if (el) el.click(); });
    let dnState = '';
    for (let i = 0; i < 60; i++) {
      await sleep(1000);
      dnState = await page.evaluate(() => {
        const h = document.querySelector('#p-screener');
        if (!h) return '';
        const empty = h.querySelector('.wt-tb tbody tr .wt-empty');
        const real = [...h.querySelectorAll('.wt-tb tbody tr')].filter(r => !r.querySelector('.wt-empty'));
        return JSON.stringify({ loading: h.textContent.includes('扫描中'), real: real.length,
          empty: !!empty, foot: h.textContent.includes('扫描范围') });
      });
      const s = JSON.parse(dnState);
      if (!s.loading && (s.real > 0 || s.empty)) break;
    }
    const s = JSON.parse(dnState);
    ok(s.foot, '口径说明（扫描范围）在页面上');
    ok(s.real > 0 || s.empty, `便捷按钮扫描完成（真实数据行=${s.real}，空态=${s.empty}）`);
    if (s.real > 0) {
      const rowOk = await page.evaluate(() =>
        /\d+\s*\/\s*20\s*天/.test(document.querySelector('#p-screener').textContent));
      ok(rowOk, '下跌天数展示为 N / 20 天口径');
    }

    await page.evaluate(() => {
      document.querySelector('#scDnDays').value = '30';
      document.querySelector('#scDnDown').value = '10';
      document.querySelector('#scDnGo').click();
    });
    let dnRows2 = 0;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      const t = await page.evaluate(() => (document.querySelector('#p-screener') || {}).textContent || '');
      dnRows2 = await page.evaluate(() =>
        [...document.querySelectorAll('#p-screener .wt-tb tbody tr')].filter(r => !r.querySelector('.wt-empty')).length);
      if (dnRows2 > 0 && !t.includes('扫描中')) break;
    }
    ok(dnRows2 >= 10, `自由输入 30天/10跌 出真实结果（rows=${dnRows2}）`);

    ok(errs.length === 0, '控制台零报错' + (errs.length ? ' → ' + errs.slice(0, 3).join(' | ') : ''));
    await page.screenshot({ path: BASE + '\\_sclive_shot.png' });
  } catch (e) {
    ok(false, '脚本异常: ' + e.message);
  } finally {
    if (browser) await browser.close();
  }
  console.log(fails.length ? `\nFAIL (${fails.length})` : '\nALL PASS');
  process.exit(fails.length ? 1 : 0);
})();
