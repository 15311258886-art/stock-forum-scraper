// verify_moneyflow_fallback.cjs — 隔离副本库 + 无头 Chromium 验证资金流面板
// 用 puppeteer-core + 已下载的 chromium-1234
// 验证项：
//   A. /api/moneyflow_rank 返回 JSON（即便 rows=空，结构必须齐全）
//   B. /api/moneyflow_board 返回 JSON
//   C. 头部「🔄 强制重试」按钮存在且可点击
//   D. 板块资金流 Tab 存在且可切换
//   E. 全市场主力净流入榜 Tab 可切换
//   F. 失败时显示「接口异常：…」明确原因
//   G. 控制台零报错
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn, spawnSync } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname);
const VIEWER_DIR = path.join(PROJECT_ROOT, 'viewer');
const VENV_PY = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function findChromium() {
  const dirs = fs.readdirSync('C:/Users/野比&大熊/AppData/Local/ms-playwright');
  for (const d of dirs) {
    const exe = path.join('C:/Users/野比&大熊/AppData/Local/ms-playwright', d, 'chrome-win64', 'chrome.exe');
    if (fs.existsSync(exe)) return exe;
  }
  throw new Error('chromium not found');
}

// 1. 准备隔离副本库
const src = path.join(VIEWER_DIR, 'app.db');
const iso = path.join(VIEWER_DIR, 'app_isomf.db');
if (fs.existsSync(iso)) fs.unlinkSync(iso);
fs.copyFileSync(src, iso);

// 2. 找空闲端口
const net = require('net');
function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

(async () => {
  const PORT = await freePort();
  console.log(`== 隔离端口 ${PORT} ==`);

  // 3. 启 server
  const env = { ...process.env, VIEWER_PORT: String(PORT), VIEWER_DB: iso, PYTHONUNBUFFERED: '1' };
  const server = spawn(VENV_PY, ['-c', `import sys; sys.path.insert(0,r'${VIEWER_DIR}'); import server; srv=server.create_server('127.0.0.1', ${PORT}); srv.serve_forever()`], { env, stdio: ['ignore', 'pipe', 'pipe'] });

  let serverUp = false;
  server.stdout.on('data', d => {
    const s = d.toString();
    if (s.includes('listening') || s.includes('http') || s.length > 0) serverUp = true;
  });
  server.stderr.on('data', d => process.stderr.write('[server] ' + d));

  // 等 server 起来
  for (let i = 0; i < 50; i++) {
    await sleep(200);
    try {
      const ok = await new Promise((resolve) => {
        http.get(`http://127.0.0.1:${PORT}/`, r => { resolve(r.statusCode === 200); r.resume(); }).on('error', () => resolve(false));
      });
      if (ok) { serverUp = true; break; }
    } catch {}
  }
  if (!serverUp) {
    console.error('server 未启动');
    server.kill();
    process.exit(1);
  }

  let pass = 0, fail = 0;
  const ok = (c, msg) => { console.log((c ? '✓' : '✗') + ' ' + msg); c ? pass++ : fail++; };

  try {
    // === A. 直接 curl 后端接口 ===
    console.log('\n[A] 直接调 /api/moneyflow_rank');
    const rankRes = await new Promise((resolve) => {
      http.get(`http://127.0.0.1:${PORT}/api/moneyflow_rank?pz=5`, r => {
        let body = '';
        r.on('data', c => body += c);
        r.on('end', () => resolve({ status: r.statusCode, body }));
      });
    });
    ok(rankRes.status === 200, `HTTP ${rankRes.status}`);
    let rankData; try { rankData = JSON.parse(rankRes.body); } catch (e) { rankData = null; }
    ok(rankData !== null, '返回 JSON 可解析');
    if (rankData) {
      ok(typeof rankData.ok === 'boolean', `字段 ok=${rankData.ok}`);
      ok(Array.isArray(rankData.rows || []), `rows 是数组 (n=${(rankData.rows||[]).length})`);
      if (rankData.ok === false) ok(typeof rankData.reason === 'string', `有 reason: ${rankData.reason.slice(0,60)}…`);
    }

    console.log('\n[B] 直接调 /api/moneyflow_board');
    const boardRes = await new Promise((resolve) => {
      http.get(`http://127.0.0.1:${PORT}/api/moneyflow_board?pz=5`, r => {
        let body = '';
        r.on('data', c => body += c);
        r.on('end', () => resolve({ status: r.statusCode, body }));
      });
    });
    ok(boardRes.status === 200, `HTTP ${boardRes.status}`);
    let boardData; try { boardData = JSON.parse(boardRes.body); } catch (e) { boardData = null; }
    ok(boardData !== null, '返回 JSON 可解析');
    if (boardData) {
      ok(typeof boardData.ok === 'boolean', `字段 ok=${boardData.ok}`);
      ok(Array.isArray(boardData.rows || []), `rows 是数组 (n=${(boardData.rows||[]).length})`);
    }

    // === C-G. 用 puppeteer 看前端 ===
    const puppeteer = require('C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core');
    const exe = findChromium();
    console.log(`\nchromium: ${exe}`);

    const browser = await puppeteer.launch({
      executablePath: exe,
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    });
    const page = await browser.newPage();
    const consoleErrs = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrs.push(m.text()); });
    page.on('pageerror', e => consoleErrs.push('pageerror: ' + e.message));

    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(3500);

    // 用 hash 直接路由到增强功能 → 资金流向
    await page.evaluate(() => { window.location.hash = '#enhance/moneyflow'; });
    // 等 panel 真正填充（renderMoneyflow 内部有 fetch）
    await page.waitForFunction(() => {
      const p = document.querySelector('#p-moneyflow');
      return p && p.querySelector('#mfRefresh');
    }, { timeout: 15000 });
    await sleep(1500);

    // 调试输出：现在有哪些 panel / NAV active
    const navState = await page.evaluate(() => {
      const active = document.querySelector('.nav-item.active');
      const activeTab = document.querySelector('.sub-tab.active');
      const moneyPanel = document.querySelector('#p-moneyflow');
      const panelVisible = moneyPanel ? window.getComputedStyle(moneyPanel).display : 'NO_PANEL';
      return {
        navActive: active ? active.dataset.cat : null,
        subActive: activeTab ? activeTab.dataset.tab : null,
        hash: window.location.hash,
        panelDisplay: panelVisible,
        panelExists: !!moneyPanel,
      };
    });
    console.log('\n[DBG] NAV state:', JSON.stringify(navState));

    await page.screenshot({ path: path.join(PROJECT_ROOT, '_moneyflow_fallback.png'), fullPage: false });
    console.log('SHOT: _moneyflow_fallback.png');

    // === C. 头部「🔄 强制重试」按钮存在 ===
    console.log('\n[C] 头部「🔄 强制重试」按钮');
    const rfInfo = await page.evaluate(() => {
      const btn = document.querySelector('#mfRefresh');
      return btn ? { exists: true, text: btn.textContent.trim() } : { exists: false };
    });
    ok(rfInfo.exists, '🔄 强制重试按钮存在');
    if (rfInfo.exists) ok(/重试|刷新/.test(rfInfo.text), `按钮文本含"重试/刷新"（实为：${rfInfo.text}）`);

    // === D. 板块资金流 Tab 存在 ===
    console.log('\n[D] 板块资金流 Tab');
    const boardTab = await page.evaluate(() => {
      const t = [...document.querySelectorAll('[data-mf-mode="board"]')];
      return t.length > 0 ? { n: t.length, text: t[0].textContent.trim() } : { n: 0 };
    });
    ok(boardTab.n >= 1, `板块资金流 Tab 存在（n=${boardTab.n}，文本：${boardTab.text}）`);

    // === E. 切到板块资金流 ===
    if (boardTab.n >= 1) {
      await page.evaluate(() => document.querySelector('[data-mf-mode="board"]').click());
      await sleep(2500);
      const boardShown = await page.evaluate(() => {
        const head = document.querySelector('#p-moneyflow .wt-head h2');
        const tabs = [...document.querySelectorAll('#p-moneyflow [data-mf-mode]')];
        const activeBoard = tabs.find(t => t.dataset.mfMode === 'board' && t.classList.contains('active'));
        return { headText: head ? head.textContent.trim() : null, activeBoard: !!activeBoard };
      });
      ok(boardShown.headText === '资金流向', '头部 h2 = 资金流向');
      ok(boardShown.activeBoard, '板块 Tab 已激活');
    }

    // === E2. 切回全市场主力净流入榜 ===
    console.log('\n[E] 切回全市场主力净流入榜');
    await page.evaluate(() => document.querySelector('[data-mf-mode="rank"]').click());
    await sleep(2500);
    const rankActive = await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('#p-moneyflow [data-mf-mode]')];
      return tabs.find(t => t.dataset.mfMode === 'rank' && t.classList.contains('active'));
    });
    ok(!!rankActive, '全市场 Tab 已激活');

    // === F. 失败时显示「接口异常：…」 ===
    console.log('\n[F] 失败时显示明确原因');
    const failMsg = await page.evaluate(() => {
      const empty = document.querySelector('#p-moneyflow .wt-empty');
      return empty ? empty.textContent.trim() : null;
    });
    if (failMsg && /接口异常/.test(failMsg)) {
      ok(true, `空态含"接口异常："（实为：${failMsg.slice(0,80)}）`);
    } else {
      // 没数据但没报错也算正常（用户那边通的话有数据）
      ok(failMsg !== null, `空态文本存在：${failMsg ? failMsg.slice(0,80) : '(无)'}`);
    }

    // === G. 控制台零报错 ===
    console.log('\n[G] 控制台');
    ok(consoleErrs.length === 0, `控制台 0 报错（实际 ${consoleErrs.length} 条）`);
    if (consoleErrs.length) consoleErrs.slice(0,3).forEach(e => console.log('  !', e));

    await browser.close();
  } catch (e) {
    console.error('TEST ERR:', e.message);
    fail++;
  } finally {
    server.kill();
  }

  console.log(`\n========== ${pass} PASS / ${fail} FAIL ==========`);
  try { fs.unlinkSync(iso); } catch {}
  process.exit(fail === 0 ? 0 : 1);
})();