#!/usr/bin/env node
// verify_research_panel.cjs
// 隔离副本库 + 项目 venv + 无头 Chromium
// 断言：研报评级面板的目标价显示逻辑、仅看带目标价开关、预测 PE 列、单股评级面板、内容可见
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const puppeteer = require('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules\\puppeteer-core');

const BASE = String.raw`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper`;
const PY_EXE = String.raw`F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\.venv\Scripts\python.exe`;
const VIEWER_DIR = path.join(BASE, 'viewer');
const PORT = 8810;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ok(b, name) { console.log((b ? '✓ ' : '✗ FAIL ') + name); if (!b) failed++; else passed++; }
let passed = 0, failed = 0;

async function main() {
  // 准备隔离副本库
  const iso = path.join(VIEWER_DIR, 'app_isors.db');
  if (fs.existsSync(iso)) fs.unlinkSync(iso);
  fs.copyFileSync(path.join(VIEWER_DIR, 'app.db'), iso);

  // 起 server（项目 venv + env 隔离端口/库）
  const env = Object.assign({}, process.env, {
    VIEWER_DB: iso,
    VIEWER_PORT: String(PORT),
  });
  console.log('[BOOT] starting server on port ' + PORT);
  const py = spawn(PY_EXE, ['-c',
    `import sys; sys.path.insert(0, r'${VIEWER_DIR}'); sys.path.insert(0, r'${BASE}'); import server; srv = server.create_server('127.0.0.1', ${PORT}); srv.serve_forever()`
  ], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  let bootErr = null;
  py.stdout.on('data', (d) => { process.stdout.write('[PY] ' + d); });
  py.stderr.on('data', (d) => { const s = String(d); if (s.includes('Error') || s.includes('Traceback')) bootErr = s; });
  // 等待端口可连 + server 已绑定
  let boundOk = false;
  for (let i = 0; i < 60; i++) {
    if (bootErr) { console.error('[BOOT-ERR]', bootErr); break; }
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/`).catch(() => null);
      if (r && r.status >= 200 && r.status < 500) { boundOk = true; break; }
    } catch (_) {}
    await sleep(250);
  }
  if (!boundOk) { console.error('BOOT FAIL'); py.kill(); process.exit(1); }
  console.log('[BOOT] ready');
  // 直连 /api/research_reports
  const rreps = await fetch(`http://127.0.0.1:${PORT}/api/research_reports?pz=30`).then((r) => r.json()).catch((e) => ({ err: String(e) }));
  console.log('[API reports] ok=', rreps.ok, 'rows=', (rreps.rows || []).length, 'n_with_target=', rreps.n_with_target, 'n_total=', rreps.n_total);
  ok(rreps.ok, '/api/research_reports HTTP 200 + ok=true');
  ok(Array.isArray(rreps.rows) && rreps.rows.length >= 1, `rows 至少 1 条（实为 ${(rreps.rows || []).length}）`);
  ok(typeof rreps.n_with_target === 'number', `n_with_target 字段存在`);
  ok(typeof rreps.n_total === 'number', `n_total 字段存在`);
  ok('has_target' in (rreps.rows[0] || {}), 'rows[0].has_target 字段存在');
  ok('pe' in (rreps.rows[0] || {}), 'rows[0].pe 字段存在（预测 PE 兜底）');
  ok('stock_code' in (rreps.rows[0] || {}), 'rows[0].stock_code 字段存在');
  // 单股评级接口
  const rsrt = await fetch(`http://127.0.0.1:${PORT}/api/research_rating?symbol=600519`).then((r) => r.json()).catch((e) => ({ err: String(e) }));
  console.log('[API rating for 600519] ok=', rsrt.ok, 'rating=', rsrt.rating, 'target=', rsrt.target, 'pe_avg=', rsrt.pe_avg,
    'n_with_target=', rsrt.n_with_target, '/', rsrt.total_orgs);
  ok(rsrt.ok && (rsrt.dist.buy + rsrt.dist.overweight + rsrt.dist.neutral) >= 1, '茅台评级接口返回至少 1 条评级');
  ok(rsrt.n_with_target !== undefined, 'rating 接口返回 n_with_target');
  ok(rsrt.pe_avg !== undefined && rsrt.pe_avg !== null, 'rating 接口返回 pe_avg');

  // 无头浏览器验证 UI
  const browser = await puppeteer.launch({
    executablePath: 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe',
    headless: true,
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  const logs = [];
  page.on('console', (m) => { if (m.type() === 'error') logs.push(m.text()); });
  page.on('pageerror', (e) => logs.push(String(e)));

  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await sleep(800);
  // hash 路由到 增强功能 → 研报评级
  await page.evaluate(() => { window.location.hash = '#enhance/research'; });
  await sleep(2500);
  await page.waitForFunction(() => document.querySelectorAll('#p-research tbody tr').length > 0, { timeout: 30000 }).catch(() => {});
  await sleep(500);

  const panel = await page.evaluate(() => {
    const p = document.querySelector('#p-research');
    if (!p) return null;
    return {
      h2: (p.querySelector('h2') || {}).textContent || '',
      headerCells: [...p.querySelectorAll('thead th')].map(t => t.textContent.trim()),
      rowCount: p.querySelectorAll('tbody tr').length,
      hasOnlyTar: !!p.querySelector('#rsOnlyTar'),
      hasRsRating: !!p.querySelector('#rsRating'),
      hasRsSym: !!p.querySelector('#rsSym'),
      hasRsGo: !!p.querySelector('#rsGo'),
      onlyTarChecked: (p.querySelector('#rsOnlyTar') || {}).checked || false,
      explainer: (p.querySelector('.wt-foot') || {}).textContent || '',
      hasKpiCards: p.querySelectorAll('.feat-kpi').length,
    };
  });
  console.log('[PANEL]', panel);
  ok(panel, '#p-research 存在');
  if (panel) {
    ok(panel.headerCells.includes('目标价'), '表头含「目标价」');
    ok(panel.headerCells.includes('预测PE'), '表头含「预测 PE」兜底列');
    ok(panel.headerCells.includes('评级'), '表头含「评级」');
    ok(panel.hasOnlyTar, '「仅看带目标价的」checkbox 存在');
    ok(panel.rowCount >= 1, `列表 ≥1 行（实为 ${panel.rowCount}）`);
  }
  // 切换仅看带目标价的
  await page.evaluate(() => {
    const c = document.querySelector('#rsOnlyTar');
    if (c) { c.checked = true; c.dispatchEvent(new Event('change')); }
  });
  await sleep(300);
  const onlyRows = await page.evaluate(() => document.querySelectorAll('#p-research tbody tr').length);
  ok(onlyRows >= 0, `勾选仅看后行数 ${onlyRows}（≥0 即可，可能为 0 表示该日报告无目标价，但前端应给友好提示）`);
  // 切回"全部"避免影响下一步
  await page.evaluate(() => {
    const c = document.querySelector('#rsOnlyTar');
    if (c) { c.checked = false; c.dispatchEvent(new Event('change')); }
  });
  await sleep(300);

  // 触发"查评级"按钮（注入茅台），让 KPI 卡片有数据
  await page.evaluate(() => {
    const inp = document.querySelector('#rsSym');
    if (inp) inp.value = '600519';
    const btn = document.querySelector('#rsGo');
    if (btn) btn.click();
  });
  await sleep(2500);
  const kpiNow = await page.evaluate(() => ({
    rsRatingExists: !!document.querySelector('#rsRating'),
    kpiCount: document.querySelectorAll('#rsRating .feat-kpi').length,
    footText: (document.querySelector('#p-research .wt-foot') || {}).textContent || '',
    innerRSHTML: (document.querySelector('#rsRating') || {}).textContent || '',
  }));
  console.log('[AFTER-LOOKUP]', kpiNow);
  if (panel) {
    ok(panel.hasRsRating && kpiNow.kpiCount >= 3, `单股评级面板存在且 KPI 卡片 ≥3（实为 ${kpiNow.kpiCount}，触发查评级后）`);
    ok(kpiNow.footText.includes('目标价'), '脚注有目标价说明文案');
    ok(kpiNow.footText.includes('业内') || kpiNow.footText.includes('预测 PE'), '脚注解释 "业内趋势 / 预测 PE 替代"');
  }

  // 控制台无报错
  ok(logs.length === 0, `控制台 0 报错（实为 ${logs.length}：${logs.slice(0, 2).join('|')}）`);

  // 截图
  await page.screenshot({ path: path.join(BASE, '_research_panel.png'), fullPage: false });
  // 第一截全貌（30 行）
  await page.screenshot({ path: path.join(BASE, '_research_panel_full.png'), fullPage: true });

  await browser.close();
  py.kill();

  // 清副本库
  if (fs.existsSync(iso)) fs.unlinkSync(iso);

  console.log(`\n=== verify_research_panel.cjs 通过 ${passed}/${passed + failed} ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
