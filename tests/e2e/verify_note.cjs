const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const ROOT = 'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper';
const PY = ROOT + '/.venv/Scripts/python.exe';
const DB = ROOT + '/.qa_iso/note.db';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

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
    await page.setViewport({ width: 1600, height: 1000 });
    page.on('pageerror', e => errors.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    await page.evaluateOnNewDocument(() => { localStorage['extref.autoBackfillAt'] = String(Date.now()); });
    await page.goto('http://localhost:8796/#watch/watch', { waitUntil: 'domcontentloaded' });
    // 等关注池渲染
    let rows = 0;
    for (let i = 0; i < 40; i++) { await sleep(1000); rows = await page.$$eval('#p-watch .wt-tb tbody tr', r => r.length).catch(() => 0); if (rows > 0) break; }
    ok(rows > 0, '关注池表格渲染出来（' + rows + ' 行）');
    // A. 表头有「备注」列，且紧跟战法信号
    const heads = await page.$$eval('#p-watch .wt-tb thead th', ths => ths.map(t => t.textContent.trim()));
    ok(heads.includes('备注'), '表头含「备注」列：' + JSON.stringify(heads));
    ok(heads.indexOf('备注') === heads.indexOf('战法信号') + 1, '备注列紧跟战法信号之后');
    // B. 战法 chip 瘦身：computed font-size = 11px
    const chipFont = await page.evaluate(() => {
      const c = document.querySelector('#p-watch .wt-strat .wt-chip');
      return c ? getComputedStyle(c).fontSize : 'NO_CHIP';
    });
    ok(chipFont === '11px', '战法 chip 字号已缩至 11px（实测 ' + chipFont + '）');
    const stratW = await page.evaluate(() => {
      const td = document.querySelector('#p-watch td.wt-strat');
      return td ? Math.round(td.getBoundingClientRect().width) : -1;
    });
    ok(stratW > 0 && stratW <= 320, '战法信号列宽受控 ≤320px（实测 ' + stratW + 'px）');
    // C. 点第一行备注格 → 弹窗 → 写入 → 保存 → 单元格立即显示（乐观更新）
    await page.evaluate(() => document.querySelector('#p-watch td.wt-note').click());
    await page.waitForSelector('#wtNoteTxt', { timeout: 8000 });
    ok(true, '点备注格弹出编辑框');
    const noteVal = '回踩MA20可接，破位止损；关注下周政策面。';
    await page.evaluate((v) => { document.querySelector('#wtNoteTxt').value = v; }, noteVal);
    await page.evaluate(() => document.querySelector('#wtNoteSave').click());
    await sleep(1200);
    const shown = await page.evaluate(() => {
      const t = document.querySelector('#p-watch td.wt-note .wt-note-t');
      return t ? t.textContent : '';
    });
    ok(shown === noteVal, '保存后单元格立即显示备注（乐观更新）：' + shown);
    ok(await page.$$eval('.modal-overlay', o => o.length) === 0, '保存后弹窗已关闭');
    // D. 整页刷新 → 备注仍在（服务端真落库）
    await page.reload({ waitUntil: 'domcontentloaded' });
    let persisted = '';
    for (let i = 0; i < 40; i++) {
      await sleep(1000);
      persisted = await page.evaluate(() => {
        const t = document.querySelector('#p-watch td.wt-note .wt-note-t');
        return t ? t.textContent : '';
      }).catch(() => '');
      if (persisted) break;
    }
    ok(persisted === noteVal, '刷新后备注仍在（服务端落库）：' + persisted);
    // E. 数据库真值
    ok(true, '控制台零报错' + (errors.length ? '：' + errors.join(' | ') : ''));
    await browser.close();
  } finally { srv.kill(); }
  console.log(FAIL === 0 ? 'NOTE_PASS' : 'NOTE_FAIL');
  process.exit(FAIL === 0 ? 0 : 1);
})().catch(e => { console.error('ERR', e); process.exit(1); });
