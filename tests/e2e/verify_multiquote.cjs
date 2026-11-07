#!/usr/bin/env node
// verify_multiquote.cjs —— 多资产面板真机端到端 + 真网络 fallback 验证
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const puppeteer = require('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules\\puppeteer-core');

const BASE = String.raw`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`;
const PY_EXE = String.raw`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\.venv\Scripts\python.exe`;
const VIEWER_DIR = path.join(BASE, 'viewer');
const PORT = 8812;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let passed = 0, failed = 0;
function ok(b, name) { console.log((b ? '✓ ' : '✗ FAIL ') + name); if (b) passed++; else failed++; }

async function main() {
  const iso = path.join(VIEWER_DIR, 'app_isomq.db');
  if (fs.existsSync(iso)) fs.unlinkSync(iso);
  fs.copyFileSync(path.join(VIEWER_DIR, 'app.db'), iso);

  const env = Object.assign({}, process.env, { VIEWER_DB: iso, VIEWER_PORT: String(PORT) });
  console.log('[BOOT] starting server on port ' + PORT);
  const py = spawn(PY_EXE, ['-c',
    `import sys; sys.path.insert(0, r'${VIEWER_DIR}'); sys.path.insert(0, r'${BASE}'); import server; srv = server.create_server('127.0.0.1', ${PORT}); srv.serve_forever()`
  ], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  let bootErr = null;
  py.stdout.on('data', (d) => process.stdout.write('[PY] ' + d));
  py.stderr.on('data', (d) => { const s = String(d); if (s.includes('Traceback')) bootErr = s; });
  for (let i = 0; i < 80; i++) {
    if (bootErr) { console.error('[BOOT-ERR]', bootErr); break; }
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/`).catch(() => null);
      if (r && r.status >= 200 && r.status < 500) { console.log('[BOOT] ready'); break; }
    } catch (_) {}
    await sleep(250);
  }
  if (bootErr) { py.kill(); process.exit(1); }

  // 1. API 真网络取值
  const rp = await fetch(`http://127.0.0.1:${PORT}/api/multiquote_preset`).then((r) => r.json()).catch((e) => ({ err: String(e) }));
  console.log('[API preset]', 'ok=', rp.ok, 'rows=', (rp.rows || []).length, 'src=', rp.src, 'n_ok=', rp.n_ok, 'reason=', rp.reason);
  ok(rp.ok, '多资产 preset 返回 ok=true（说明总有 fallback 数据源命中）');
  ok(Array.isArray(rp.rows) && rp.rows.length >= 8, `preset 至少有 8 行（实为 ${(rp.rows || []).length}）`);
  ok((rp.rows || []).some(r => r.src === 'tencent') || (rp.rows || []).some(r => r.src === 'push2'),
     '至少有一只是 push2/tencent 取到（非"— 断"）');
  ok(typeof rp.src === 'string' && ['push2', 'tencent', '—'].includes(rp.src), `src 在约定值集合中（实为 ${rp.src}）`);
  // 2. lookup 接口
  for (const sym of ['usAAPL', '510300']) {
    const r = await fetch(`http://127.0.0.1:${PORT}/api/multiquote_lookup?symbol=${sym}`).then((r) => r.json());
    console.log(`[API lookup ${sym}]`, r);
    ok(r.ok, `lookup(${sym}) ok`);
    ok(typeof r.price === 'number' && r.price > 0, `lookup(${sym}).price > 0`);
  }

  // 3. UI 验证
  const browser = await puppeteer.launch({
    executablePath: 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe',
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  const logs = [];
  page.on('console', (m) => { if (m.type() === 'error') logs.push(m.text()); });
  page.on('pageerror', (e) => logs.push(String(e)));
  await page.setViewport({ width: 1440, height: 1100 });
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('[data-cat]', { timeout: 30000 });
  await sleep(400);
  // 真实点击：展开「增强功能」大类，再点击「多资产」
  await page.evaluate(() => {
    const cat = document.querySelector('[data-cat="enhance"]');
    if (cat) cat.click();
  });
  await sleep(400);
  await page.waitForSelector('[data-tab="multiquote"]', { timeout: 10000 });
  await page.evaluate(() => {
    const t = document.querySelector('[data-tab="multiquote"]');
    if (t) t.click();
  });
  // 等面板真正有 render 出 h2
  await page.waitForFunction(
    () => !!document.querySelector('#p-multiquote h2'),
    { timeout: 30000 }
  );
  await sleep(1500);  // 让懒加载的 API 收尾
  const panel = await page.evaluate(() => {
    const p = document.querySelector('#p-multiquote');
    if (!p) return null;
    const rows = [...p.querySelectorAll('tbody tr')].map((tr) => ({
      code: (tr.querySelector('td:first-child') || {}).textContent || '',
      name: (tr.querySelector('td:nth-child(2)') || {}).textContent || '',
      price: (tr.querySelector('td:nth-child(3)') || {}).textContent || '',
      pct: (tr.querySelector('td:nth-child(4)') || {}).textContent || '',
      src: (tr.querySelector('td:nth-child(5) .src-tag') || {}).textContent || '',
    }));
    return {
      h2: (p.querySelector('h2') || {}).textContent || '',
      headerCells: [...p.querySelectorAll('thead th')].map((t) => t.textContent.trim()),
      rowCount: p.querySelectorAll('tbody tr').length,
      hasRefresh: !!p.querySelector('#mqRefresh'),
      hasGo: !!p.querySelector('#mqGo'),
      hasAddInput: !!p.querySelector('#mqAdd'),
      segTabs: [...p.querySelectorAll('[data-mq-seg]')].map((t) => t.textContent.trim()),
      rows: rows,
      nWithPrice: rows.filter((r) => r.price && r.price !== '—' && r.price.includes('.')).length,
      footText: (p.querySelector('.wt-foot') || {}).textContent || '',
    };
  });
  console.log('[PANEL]', JSON.stringify({ ...panel, rows: panel.rows.slice(0, 5) }, null, 2));
  ok(panel, '#p-multiquote 存在');
  if (panel) {
    ok(panel.headerCells.includes('数据源'), '表头含「数据源」列');
    ok(panel.headerCells.length === 5, `表头 5 列（实为 ${panel.headerCells.length}）`);
    ok(panel.hasRefresh, '🔄 重试按钮存在');
    ok(panel.hasGo && panel.hasAddInput, '添加输入框 + 「添加」按钮存在');
    ok(panel.segTabs.length >= 2, `板块 Tab ≥2（实为 ${panel.segTabs.length}：${panel.segTabs.join(',')}）`);
    ok(panel.rowCount >= 8, `列表 ≥8 行（实为 ${panel.rowCount}）`);
    ok(panel.nWithPrice >= 4, `≥4 行有真实价格（实为 ${panel.nWithPrice}）`);
    ok(panel.footText.includes('push2') && panel.footText.includes('tencent'), '脚注解释 push2 → push2his → 腾讯 fallback 链');
  }
  // 切板块
  await page.evaluate(() => {
    const t = [...document.querySelectorAll('[data-mq-seg]')].find((x) => x.textContent.trim() === 'ETF');
    if (t) t.click();
  });
  await sleep(500);
  const etfRows = await page.evaluate(() => document.querySelectorAll('#p-multiquote tbody tr').length);
  ok(etfRows > 0 && etfRows < (panel?.rowCount || 999), `切 ETF 后行数 ${etfRows}（小于默认 ${panel?.rowCount}）`);
  // 先切回"全部"再添加 usAAPL（避免被前一步的 ETF 筛选过滤掉）
  await page.evaluate(() => {
    const t = [...document.querySelectorAll('[data-mq-seg]')].find((x) => x.textContent.trim() === '全部');
    if (t) t.click();
  });
  await sleep(400);
  // 添加 usAAPL
  await page.evaluate(() => {
    const inp = document.querySelector('#mqAdd'); if (inp) inp.value = 'usAAPL';
    const btn = document.querySelector('#mqGo'); if (btn) btn.click();
  });
  await sleep(1500);
  const addedCells = await page.evaluate(() => [...document.querySelectorAll('#p-multiquote tbody td:first-child')].map((t) => t.textContent.trim()));
  console.log('[AFTER-ADD codes]', addedCells);
  ok(addedCells.some((c) => c.toUpperCase().includes('AAPL')), '添加 usAAPL 后出现 AAPL');
  // 重试按钮存在 + 点击不报错
  await page.evaluate(() => { const b = document.querySelector('#mqRefresh'); if (b) b.click(); });
  await sleep(2000);
  // 控制台 0 报错
  ok(logs.length === 0, `控制台 0 报错（实为 ${logs.length}：${logs.slice(0, 2).join('|')}）`);

  await page.screenshot({ path: path.join(BASE, '_multiquote_full.png'), fullPage: true });
  await page.screenshot({ path: path.join(BASE, '_multiquote_view.png'), fullPage: false });

  await browser.close();
  py.kill();
  // db 暂留，让 server 释放后再清
  setTimeout(() => { try { if (fs.existsSync(iso)) fs.unlinkSync(iso); } catch (_) {} }, 1500);

  console.log(`\n=== verify_multiquote.cjs 通过 ${passed}/${passed + failed} ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
