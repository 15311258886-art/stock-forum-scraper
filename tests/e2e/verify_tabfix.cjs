const puppeteer = require('puppeteer-core');
const { spawn, execSync } = require('child_process');
const ROOT = 'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper';
const PY = ROOT + '/.venv/Scripts/python.exe';
const DB = ROOT + '/.qa_iso/tabfix.db';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const tagBar = (page) => page.evaluate(() =>
  [...document.querySelectorAll('#p-watch .wt-tab:not(.wt-tab-add)')]
    .map(a => a.textContent.trim().replace(/\s*\d+\s*✎?\s*$/, '').trim()));

(async () => {
  let PASS = 0, FAIL = 0; const errors = [];
  const ok = (c, m) => { c ? PASS++ : FAIL++; console.log((c ? 'PASS' : 'FAIL') + ' ' + m); };
  const srv = spawn(PY, ['-m', 'viewer.server'], {
    cwd: ROOT, env: { ...process.env, VIEWER_DB: DB, VIEWER_PORT: '8796' }, stdio: 'ignore'
  });
  try {
    for (let i = 0; i < 30; i++) { try { await fetch('http://localhost:8796/api/version'); break; } catch { await sleep(500); } }
    const browser = await puppeteer.launch({
      executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
      headless: 'new', args: ['--no-sandbox']
    });
    const page = await browser.newPage();
    page.on('pageerror', e => errors.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    await page.evaluateOnNewDocument(() => { localStorage['extref.autoBackfillAt'] = String(Date.now()); });
    await page.goto('http://localhost:8796/#watch/watch', { waitUntil: 'domcontentloaded' });
    let tabs = [];
    for (let i = 0; i < 40; i++) { await sleep(1000); tabs = await tagBar(page); if (tabs.length > 1) break; }
    ok(tabs.length >= 2, '关注池标签条渲染出来：' + JSON.stringify(tabs));
    const before = tabs.filter(t => t !== '全部');
    await page.evaluate(() => document.querySelector('#wtGroupMgr').click());
    await page.waitForSelector('.wt-glist [data-gdn]', { timeout: 10000 });
    ok(true, '分组管理弹窗打开，↑/↓ 按钮在');
    // 点第一行 ↓
    await page.evaluate(() => document.querySelector('.wt-glist [data-gdn]').click());
    // 关键断言：1.5 秒内标签条前两名互换（乐观更新，不等慢接口）
    await sleep(1500);
    const after1 = (await tagBar(page)).filter(t => t !== '全部');
    ok(after1[0] === before[1] && after1[1] === before[0],
      '点↓后 1.5s 内标签条前两名互换：' + JSON.stringify(before) + ' -> ' + JSON.stringify(after1));
    // 服务端落库校验（等最多 25s）
    let names = [];
    for (let i = 0; i < 25; i++) {
      await sleep(1000);
      names = execSync('"' + PY + '" -c "import sqlite3;c=sqlite3.connect(r\'' + DB + '\');print(\',\'.join(r[0] for r in c.execute(\'SELECT name FROM watch_groups ORDER BY sort_order\')))"').toString().trim().split(',');
      if (names[0] === before[1] && names[1] === before[0]) break;
    }
    ok(names[0] === before[1] && names[1] === before[0], '服务端已落库新顺序：' + names.join(','));
    // 还原：点第二行的 ↑（原第一组现在在第二位）
    const dbg = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wt-glist .wt-grow')];
      return rows.map((r, i) => ({
        i: i + 1,
        name: r.querySelector('.wt-gname') ? r.querySelector('.wt-gname').value : '?',
        gup: r.querySelector('[data-gup]') ? r.querySelector('[data-gup]').dataset.gup : null,
        dis: r.querySelector('[data-gup]') ? r.querySelector('[data-gup]').className : ''
      }));
    });
    console.log('DBG rows:', JSON.stringify(dbg));
    const clicked = await page.evaluate(() => {
      const el = document.querySelector('.wt-glist .wt-grow:nth-child(2) [data-gup]');
      if (!el) return 'NOT_FOUND';
      el.click(); return 'CLICKED:' + el.dataset.gup;
    });
    console.log('DBG click:', clicked);
    await sleep(1500);
    const after2 = (await tagBar(page)).filter(t => t !== '全部');
    ok(JSON.stringify(after2) === JSON.stringify(before), '点↑还原，标签条回到原始顺序：' + JSON.stringify(after2));
    ok(errors.length === 0, '控制台零报错' + (errors.length ? '：' + errors.join(' | ') : ''));
    await browser.close();
  } finally { srv.kill(); }
  console.log(FAIL === 0 ? 'TABFIX_PASS' : 'TABFIX_FAIL');
  process.exit(FAIL === 0 ? 0 : 1);
})().catch(e => { console.error('ERR', e); process.exit(1); });
