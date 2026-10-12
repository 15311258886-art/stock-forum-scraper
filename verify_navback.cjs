/* verify_navback.cjs —— 「返回来路」回归（2026-09-30，王总：诊断完关掉要能回到原页面）
   覆盖：
   A. 智能选股（Top30 视图）→ 点个股 → 抽屉「估值诊股」→ 跳转个股诊断
      → 诊断页出现「← 返回智能选股」→ 点它回到智能选股且 **Top30 视图不重置**
   B. 任意页（今日看板）→ 手动切到个股诊断 → 出现「← 返回今日看板」→ 点它回去
   C. 离开诊断页后 origin 作废（不会残留过期来路）
   D. 全程零 JS 错 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const { spawn } = require('child_process');
const puppeteer = require('puppeteer-core');
const http = require('http');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8816;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';

const fails = [];
const ok = (c, m) => { (c ? console.log('  PASS ' + m) : (fails.push(m), console.log('  FAIL ' + m))); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitPort = () => new Promise((res, rej) => {
  let n = 0; const t = setInterval(() => {
    http.get('http://127.0.0.1:' + PORT + '/api/stocks', (r) => { clearInterval(t); res(); })
      .on('error', () => { if (++n > 120) { clearInterval(t); rej(new Error('server timeout')); } });
  }, 500);
});

const env = { ...process.env, VIEWER_PORT: String(PORT) };
['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY'].forEach(k => delete env[k]);
const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
srv.stderr.on('data', (d) => { const s = String(d); if (/Traceback|Error/.test(s)) console.log('[srv]', s.slice(0, 300)); });

(async () => {
  await waitPort();
  console.log('server up @' + PORT);
  const browser = await puppeteer.launch({
    executablePath: 'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',
    headless: 'new', args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  const jsErr = [];
  page.on('pageerror', (e) => jsErr.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') jsErr.push('console: ' + m.text()); });

  await page.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.selectNav && document.querySelector('#sidenav'), { timeout: 20000 });
  await sleep(600);

  /* ---------- A：智能选股 Top30 → 抽屉估值诊股 → 返回 ---------- */
  console.log('A. 智能选股(Top30视图) → 抽屉估值诊股 → 诊断页返回');
  await page.evaluate(() => selectNav('watch', 'screener', true));
  await sleep(900);
  await page.click('#p-screener [data-feat-screen="decline"]');
  await page.waitForFunction(() => document.querySelector('#scDnGo'), { timeout: 15000 });
  // 灌入 Top30 视图态（mock 数据；真实 Top30 加载已有 verify_turnover_top.cjs 覆盖）
  await page.evaluate(() => {
    const dc = FEAT.screener.decline;
    dc.mode = 'top'; dc.data_mode = 'top'; dc.loading = false;
    dc.data = { ok: true, days: 30, rows: [
      { code: '600519', name: '贵州茅台', price: 1410.0, pct: 1.2, turnover: '1.2% / 1.0%', pos_pct: 12,
        score: 91, tier: 5, tier_label: '积极关注', hint: '低位量和量·吸筹特征' },
      { code: '600036', name: '招商银行', price: 39.5, pct: 0.6, turnover: '0.8% / 0.7%', pos_pct: 18,
        score: 88, tier: 5, tier_label: '积极关注', hint: '低位量和量·吸筹特征' }] };
    renderScreener();
  });
  await sleep(300);
  ok(await page.$('#p-screener.active'), 'A1 智能选股面板激活');
  ok(await page.$eval('#scDnTop', (e) => e.classList.contains('active')), 'A2 Top30 按钮处于激活态');

  // 真实点击第一行代码 → 抽屉
  await page.click('#p-screener [data-wt-open="600519"]');
  await page.waitForFunction(() => document.querySelector('#drawer').classList.contains('show'), { timeout: 40000 });
  await page.waitForFunction(() => { const b = document.querySelector('#btnValuation'); return b && !b.hidden; }, { timeout: 40000 });
  ok(true, 'A3 点代码 → 个股抽屉打开且「估值诊股」按钮可见');
  await page.click('#btnValuation');
  await page.waitForFunction(() => document.querySelector('#p-diag') && document.querySelector('#p-diag').classList.contains('active'), { timeout: 15000 });
  ok(true, 'A4 跳转到个股诊断页');
  const backTxt = await page.evaluate(() => { const b = document.querySelector('#dgBack'); return b ? b.textContent.trim() : null; });
  ok(backTxt && backTxt.indexOf('返回智能选股') >= 0, 'A5 诊断页出现「← 返回智能选股」按钮（实际：' + backTxt + '）');
  const originTab = await page.evaluate(() => NAV_BACK.origin && NAV_BACK.origin.tab);
  ok(originTab === 'screener', 'A6 来路记录= screener（实际：' + originTab + '）');

  await page.click('#dgBack');
  await sleep(700);
  ok(await page.$eval('#p-screener', (e) => e.classList.contains('active')), 'A7 点返回 → 回到智能选股面板');
  const st = await page.evaluate(() => ({ mode: FEAT.screener.decline.mode, dm: FEAT.screener.decline.data_mode,
    top: !!document.querySelector('#scDnTop.active'), row: !!document.querySelector('#p-screener [data-wt-open="600519"]'),
    ob: NAV_BACK.origin === null }));
  ok(st.mode === 'top' && st.dm === 'top', 'A8 视图模式保留 Top30（mode=' + st.mode + '）');
  ok(st.top, 'A9 Top30 按钮仍激活');
  ok(st.row, 'A10 表格仍是 Top30 内容（mock 行在，未被重置成阴跌）');
  ok(st.ob, 'A11 返回后 origin 已清账');

  /* ---------- B：今日看板 → 手动切诊断 → 返回今日看板 ---------- */
  console.log('B. 今日看板 → 手动进诊断 → 返回');
  await page.evaluate(() => selectNav('overview', 'dashboard', true));
  await sleep(600);
  await page.evaluate(() => selectNav('watch', 'diag', true));
  await sleep(500);
  const backTxt2 = await page.evaluate(() => { const b = document.querySelector('#dgBack'); return b ? b.textContent.trim() : null; });
  ok(backTxt2 && backTxt2.indexOf('返回今日看板') >= 0, 'B1 手动切进诊断也记来路（实际：' + backTxt2 + '）');
  await page.click('#dgBack');
  await sleep(600);
  ok(await page.$eval('#p-dashboard', (e) => e.classList.contains('active')), 'B2 返回到今日看板');

  /* ---------- C：离开诊断后来路作废 ---------- */
  console.log('C. 离开诊断后来路作废');
  await page.evaluate(() => selectNav('watch', 'diag', true));
  await sleep(400);
  await page.evaluate(() => selectNav('money', 'moneyflow', true));
  await sleep(800);
  const c1 = await page.evaluate(() => NAV_BACK.origin === null);
  ok(c1, 'C1 从诊断页切走 → origin 作废');
  await page.evaluate(() => selectNav('watch', 'diag', true));
  await sleep(400);
  const c2 = await page.evaluate(() => NAV_BACK.origin && NAV_BACK.origin.tab);
  ok(c2 === 'moneyflow', 'C2 再次进入诊断 → 来路更新为最近一页（' + c2 + '）');

  /* ---------- D：零 JS 错 ---------- */
  ok(jsErr.length === 0, 'D1 全程零 JS 错' + (jsErr.length ? '：' + jsErr.slice(0, 3).join(' | ') : ''));

  await browser.close();
  console.log(fails.length ? 'NAVBACK_FAIL (' + fails.length + ')' : 'NAVBACK_PASS');
  srv.kill();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e); try { srv.kill(); } catch (_) {} process.exit(1); });
