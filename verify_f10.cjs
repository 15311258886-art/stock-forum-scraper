// verify_f10.cjs —— 个股 F10 简介 + 持股结构（机构/散户/股东户数）回归护栏
// 桌面入口：抽屉(openStock) + 个股诊断页(renderDiag) 两处接线；后端 /api/stock_f10
// 跑法：node verify_f10.cjs
const path = require('path');
const { spawn } = require('child_process');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8794;
const PY   = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\.venv\\Scripts\\python.exe';
const CHROME = 'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const URL  = `http://127.0.0.1:${PORT}/`;

const fails = [];
function ok(cond, msg) { if (cond) console.log('  PASS', msg); else { console.log('  FAIL', msg); fails.push(msg); } }

async function waitTextNot(page, sel, txt, timeout = 20000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const t = await page.evaluate((s) => { const e = document.querySelector(s); return e ? e.textContent : null; }, sel);
    if (t !== null && !t.includes(txt)) return t;
    await new Promise(r => setTimeout(r, 300));
  }
  return null;
}

(async () => {
  const env = { ...process.env, VIEWER_PORT: String(PORT) };
  ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'].forEach(k => delete env[k]);
  const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  srv.stderr.on('data', d => { const s = d.toString(); if (s.includes('ERROR') || s.includes('Traceback')) console.log('  [srv]', s.trim().split('\n')[0]); });

  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => consoleErrors.push('PAGEERROR ' + e.message));

  try {
    await page.goto(URL, { waitUntil: 'networkidle2', timeout: 30000 });
    await page.waitForSelector('#sidenav', { timeout: 15000 });

    // 0) app.js 确实是改后的版本（接口层保证）
    const served = await page.evaluate(async () => {
      const r = await fetch('/static/app.js', { cache: 'no-store' });
      return await r.text();
    });
    ok(served.includes('initStockF10'), 'served app.js 含 initStockF10（命中新静态文件）');
    ok(served.includes('renderF10Holders'), 'served app.js 含 renderF10Holders');

    // 1) 抽屉：openStock(000902) → 简介卡 + 持股结构块
    await page.evaluate(() => openStock('000902'));
    await page.waitForSelector('#drawer.show', { timeout: 8000 }).catch(() => {});
    const prof = await waitTextNot(page, '#stkProfile', '加载中');
    ok(prof !== null, '抽屉 #stkProfile 已脱离「加载中」');
    ok(prof && prof.includes('新洋丰'), '抽屉简介卡显示公司名「新洋丰」');
    ok(prof && prof.includes('主营'), '抽屉简介卡含「主营」字段');

    const hold = await waitTextNot(page, '#stkHolders', '加载中');
    ok(hold !== null, '抽屉 #stkHolders 已脱离「加载中」');
    ok(hold && hold.includes('机构'), '抽屉持股结构含「机构」分类');
    ok(hold && hold.includes('散户'), '抽屉持股结构含「散户」占比（估算）');
    const holderRows = await page.evaluate(() => document.querySelectorAll('#stkHolders .sh-tbl tr').length);
    ok(holderRows >= 3, `抽屉持股结构股东表有 ${holderRows} 行（≥3）`);
    const segCount = await page.evaluate(() => document.querySelectorAll('#stkHolders .sh-seg').length);
    ok(segCount >= 2, `抽屉持股结构堆叠条分段数=${segCount}（机构/散户）`);

    // 2) 个股诊断页：导航 → 我的票/个股诊断 → 输入 000902 → 开始诊断 → 简介+持股
    await page.evaluate(() => { if (typeof selectNav === 'function') selectNav('watch', 'diag', true); });
    await page.waitForSelector('#p-diag #dgSym', { timeout: 10000 }).catch(() => {});
    const hasDiagInput = await page.evaluate(() => !!document.querySelector('#p-diag #dgSym'));
    ok(hasDiagInput, '个股诊断页输入控件 #dgSym 存在');
    if (hasDiagInput) {
      await page.evaluate(() => {
        const i = document.querySelector('#p-diag #dgSym');
        i.value = '000902';
        i.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await page.evaluate(() => { const b = document.querySelector('#p-diag #dgGo'); if (b) b.click(); });
      // 等诊断主数据先回来
      await waitTextNot(page, '#p-diag #dgSym', '诊断中').catch(() => {});
      const dprof = await waitTextNot(page, '#p-diag #stkProfile', '加载中', 25000);
      ok(dprof !== null, '诊断页 #stkProfile 已脱离「加载中」');
      ok(dprof && dprof.includes('新洋丰'), '诊断页简介卡显示「新洋丰」');
      const dhold = await waitTextNot(page, '#p-diag #stkHolders', '加载中', 25000);
      ok(dhold !== null, '诊断页 #stkHolders 已脱离「加载中」');
      ok(dhold && dhold.includes('机构'), '诊断页持股结构含「机构」分类');
    }

    ok(consoleErrors.length === 0, `控制台零报错（实际 ${consoleErrors.length} 条）` + (consoleErrors.length ? ' :: ' + consoleErrors.slice(0, 3).join(' | ') : ''));
  } catch (e) {
    console.log('  EXCEPTION', e.message);
    fails.push('EXCEPTION ' + e.message);
  } finally {
    await browser.close();
    srv.kill();
  }

  console.log('\n========== verify_f10 ==========');
  if (fails.length === 0) { console.log('ALL PASS'); process.exit(0); }
  else { console.log('FAILS:', fails.length); fails.forEach(f => console.log(' -', f)); process.exit(1); }
})();
