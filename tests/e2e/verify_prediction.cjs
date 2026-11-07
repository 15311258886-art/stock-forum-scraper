// verify_prediction.cjs —— 「总览·每日预测」面板回归护栏
// 依赖：puppeteer-core（受管 node workspace）+ 项目 .venv 起后端
// ⚠️ 本机若未装 ms-playwright/chromium，浏览器段会自动跳过（A 接口段照常验）；装好后再跑完整护栏：
//   node verify_prediction.cjs
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8813;
const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PY   = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\.venv\\Scripts\\python.exe';

const fails = [];
function ok(cond, msg) { if (!cond) { fails.push(msg); console.log('  x ' + msg); } else { console.log('  . ' + msg); } }

function getJSON(port, pathname) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: pathname }, (res) => {
      let buf = '';
      res.on('data', (c) => buf += c);
      res.on('end', () => { try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

function findChrome() {
  const base = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
  try {
    const dirs = fs.readdirSync(base).filter(d => d.startsWith('chromium')).sort();
    for (const d of dirs) {
      const p = path.join(base, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(p)) return p;
    }
  } catch (e) {}
  return null;
}

(async () => {
  // ---- 起后端（隔离端口，不动王总正在用的 8765）----
  const env = { ...process.env, VIEWER_PORT: String(PORT) };
  ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'].forEach(k => delete env[k]);
  const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  srv.stderr.on('data', (d) => { const s = d.toString(); if (/ERROR|Traceback/.test(s)) process.stderr.write('[srv] ' + s); });

  let up = false;
  for (let i = 0; i < 40; i++) {
    try { const r = await getJSON(PORT, '/api/prediction/today'); if (r.status) { up = true; break; } } catch (e) {}
    await new Promise(r => setTimeout(r, 500));
  }
  if (!up) { console.log('后端起不来，退出'); try { srv.kill(); } catch (e) {} process.exit(1); }

  // ---- A. 三数据接口 ----
  console.log('A. 接口');
  const today = await getJSON(PORT, '/api/prediction/today');
  ok(today.status === 200, 'today 200');
  ok(today.body.has_data === true, 'today.has_data=true');
  ok(Array.isArray(today.body.items) && today.body.items.length === 20, 'today 共 20 项（实=' + (today.body.items || []).length + '）');
  const core = (today.body.items || []).filter(x => x.tier === 'core').length;
  const watch = (today.body.items || []).filter(x => x.tier === 'watch').length;
  ok(core === 10, '核心 10 只（实=' + core + '）');
  ok(watch === 10, '观察 10 只（实=' + watch + '）');
  ok((today.body.items || []).every(x => x.symbol && /^\d{6}$/.test(x.symbol)), '所有 pick 为 6 位 A 股代码');

  const scores = await getJSON(PORT, '/api/prediction/scores');
  ok(scores.status === 200, 'scores 200');
  const keys = (scores.body.signals || []).map(s => s.key);
  // 2026-10-12 修正过时断言：'fund' 是六路时代的老 key，十二路重构后资金类已细分为 lhb/inst/board；
  // 断言改为当前真实存在的 6 个主轴 key（数据见 predictor_score，缺 key 才说明引擎断了）。
  ['tech', 'sentiment', 'lhb', 'follow', 'extref', 'holders'].forEach(k => ok(keys.includes(k), 'scores 含 ' + k));

  const track = await getJSON(PORT, '/api/prediction/track');
  ok(track.status === 200, 'track 200');
  ok(track.body && typeof track.body === 'object', 'track 结构有效');

  // ---- B. 浏览器段（需 chromium）----
  const CHROME = findChrome();
  if (!CHROME) {
    console.log('\n[跳过] 本机未装 ms-playwright/chromium，浏览器段不跑；A 接口段已验。装好后重跑完整护栏。');
    try { srv.kill(); } catch (e) {}
    process.exit(fails.length ? 1 : 0);
  }
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));

  await page.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'networkidle2' });

  console.log('B. 导航与面板');
  const predTab = await page.evaluate(() => {
    const el = [...document.querySelectorAll('[data-tab="prediction"]')][0];
    if (!el) return null;
    el.click();
    return el.textContent.trim();
  });
  ok(predTab && predTab.includes('每日预测'), '总览下「每日预测」tab 可见可点（实=' + predTab + '）');

  await new Promise(r => setTimeout(r, 900)); // 等懒加载
  const panel = await page.evaluate(() => {
    const p = document.querySelector('#p-prediction');
    if (!p) return null;
    return {
      visible: p.offsetParent !== null,
      verdict: !!p.querySelector('.pred-verdict'),
      sbRows: p.querySelectorAll('.pred-sb-row').length,
      coreCards: p.querySelectorAll('.pred-card.tier-core').length,
      watchCards: p.querySelectorAll('.pred-card.tier-watch').length,
      chips: [...p.querySelectorAll('.kline-chip[data-sym]')].map(c => c.dataset.sym),
    };
  });
  ok(panel && panel.visible, '面板 #p-prediction 已渲染可见');
  ok(panel && panel.verdict, '含市场研判段（.pred-verdict）');
  ok(panel && panel.sbRows >= 6, '六路信号贡献条 >=6（实=' + (panel && panel.sbRows) + '）');
  ok(panel && panel.coreCards >= 10, '核心卡 >=10（实=' + (panel && panel.coreCards) + '）');
  ok(panel && panel.watchCards >= 10, '观察卡 >=10（实=' + (panel && panel.watchCards) + '）');
  ok(panel && panel.chips.every(s => /^\d{6}$/.test(s)), '卡片内个股入口全为 6 位 A 股');

  if (panel && panel.chips.length) {
    // openStock 是异步的（先请求 /api/stock 再 openDrawer），必须轮询等抽屉，同步读必然假失败。
    const sym0 = panel.chips[0];
    await page.evaluate((sym) => {
      const chip = document.querySelector('#p-prediction .kline-chip[data-sym="' + sym + '"]');
      if (chip) chip.click();
    }, sym0);
    let opened = null;
    for (let i = 0; i < 40 && !opened; i++) {          // 最多 ~16s（含行情慢源）
      await new Promise(r => setTimeout(r, 400));
      opened = await page.evaluate(() => {
        const dr = document.querySelector('#drawer');
        return dr && dr.classList.contains('show') ? (document.querySelector('#drawerTitle') || {}).textContent : null;
      });
    }
    ok(opened && opened.includes(sym0), '点个股 chip 打开详情抽屉（标题含代码）');
  }

  ok(consoleErrors.length === 0, '控制台零报错（实=' + consoleErrors.length + '）');
  if (consoleErrors.length) consoleErrors.slice(0, 5).forEach(e => console.log('   ERR: ' + e));

  await browser.close();
  try { srv.kill(); } catch (e) {}

  console.log('\n==== 结果：' + (fails.length ? ('FAIL ' + fails.length + ' 项') : 'ALL PASS') + ' ====');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
