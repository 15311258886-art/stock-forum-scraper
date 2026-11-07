/* 换手率策略 × 入手值得率 回归护栏（2026-09-30）
 * 断言：智能选股→连续阴跌 12 列（换手率/入手值得率/提示）、评分chip分档渲染、
 * 按值得率排序切换、口径 details、个股入口仍可点、控制台零报错。 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const { spawn } = require('child_process');
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8815;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const env = { ...process.env, VIEWER_PORT: String(PORT) };
['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY'].forEach(k => delete env[k]);

function findChrome() {
  const cands = [];
  const mpl = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  try { for (const d of fs.readdirSync(mpl)) if (d.startsWith('chromium-'))
    cands.push(path.join(mpl, d, 'chrome-win64', 'chrome.exe')); } catch (e) {}
  cands.push('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe');
  return cands.find(p => fs.existsSync(p));
}

const fails = [];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails.push(m); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  const chrome = findChrome();
  let browser;
  try {
    // 等后端就绪
    let up = false;
    for (let i = 0; i < 30 && !up; i++) {
      await sleep(1000);
      try { up = (await fetch(`http://127.0.0.1:${PORT}/api/kb/list`)).ok; } catch (e) {}
    }
    ok(up, '后端在临时端口 8815 就绪');

    browser = await puppeteer.launch({ executablePath: chrome, headless: 'new', args: ['--no-sandbox'] });
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(2000);
    // 导航到 我的票→智能选股，再点 连续阴跌
    await page.evaluate(() => selectNav('watch', 'screener', true));
    await sleep(800);
    await page.click('[data-feat-screen="decline"]');
    await sleep(800);
    // 默认 30/25 常 0 命中，显式设 30天/20跌 再筛
    await page.evaluate(() => {
      document.querySelector('#scDnDays').value = '30';
      document.querySelector('#scDnDown').value = '20';
    });
    await page.click('#scDnGo');
    // 阴跌扫描可能需抓K线（全池387只，首次约1~2分钟），轮询等真实数据行（含个股入口）渲染，最多 180s
    let rowCount = 0, headerTxt = '';
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      const r = await page.evaluate(() => {
        const tb = document.querySelector('#p-screener .sc-dn-tb tbody');
        const th = document.querySelector('#p-screener .sc-dn-tb thead');
        const dataRows = tb ? tb.querySelectorAll('tr [data-wt-open]').length : 0;
        return { n: tb ? tb.querySelectorAll('tr').length : 0, dn: dataRows, h: th ? th.textContent : '' };
      });
      rowCount = r.n; headerTxt = r.h;
      if (r.dn > 0 && headerTxt.includes('换手率')) break;
    }
    ok(headerTxt.includes('换手率') && headerTxt.includes('入手值得率') && headerTxt.includes('提示'),
       '表头含 换手率/入手值得率/提示（实际列头=' + headerTxt.slice(0, 60) + '…）');
    ok(rowCount > 0, '阴跌表渲染出行（rows=' + rowCount + '）');

    const cell = await page.evaluate(() => {
      const tb = document.querySelector('#p-screener .sc-dn-tb tbody');
      if (!tb) return null;
      const tr = tb.querySelector('tr'); if (!tr) return null;
      const tds = tr.querySelectorAll('td');
      return { n: tds.length, to: tds[9] ? tds[9].textContent : '', score: tds[10] ? tds[10].textContent : '',
               hint: tds[11] ? tds[11].textContent : '', hasChip: !!(tds[10] && tds[10].querySelector('.sc-score')),
               sym: !!(tds[1] && tds[1].querySelector('[data-wt-open]')) };
    });
    ok(cell && cell.n === 12, '首行 12 列（实际 ' + (cell ? cell.n : 0) + '）');
    ok(cell && cell.hasChip, '值得率列渲染 .sc-score 分档chip（' + (cell ? cell.score.trim() : '') + '）');
    ok(cell && cell.sym, '代码列保留 data-wt-open 个股入口');
    ok(cell && (cell.hint.trim().length > 0 || cell.to.includes('—')), '提示列有文案或换手缺失显示 —');

    // 排序切换：点「按入手值得率」→ 数据重取后行内分数单调不增
    await page.click('[data-sc-dsort="score"]');
    let sorted = false;
    for (let i = 0; i < 60; i++) {
      await sleep(2000);
      const s = await page.evaluate(() => {
        const active = document.querySelector('#p-screener [data-sc-dsort="score"]');
        if (!active || !active.classList.contains('active')) return null;
        const tb = document.querySelector('#p-screener .sc-dn-tb tbody');
        if (!tb) return null;
        const scores = [...tb.querySelectorAll('tr')].map(tr => {
          const chip = tr.querySelector('.sc-score b');
          return chip ? parseInt(chip.textContent, 10) : null;   // 占位行给 null，不进断言
        }).filter(v => v !== null);
        if (!scores.length) return null;
        return { scores, mono: scores.every((v, i) => i === 0 || scores[i - 1] >= v) };
      });
      if (s && s.scores.length) { sorted = s.mono; break; }
    }
    ok(sorted, '切「按入手值得率」后分数单调不增');

    // 口径 details
    const basis = await page.evaluate(() => {
      const d = document.querySelector('#p-screener .sc-dn-basis');
      if (!d) return null;
      return { exists: true, summary: d.querySelector('summary').textContent,
               text: d.textContent, open: d.open };
    });
    ok(!!basis, '口径 details 存在');
    ok(basis && basis.text.includes('五维评分') && basis.text.includes('吸筹') && basis.text.includes('不构成投资建议'),
       '口径含五维评分/四场景/免责声明');
    await page.evaluate(() => { const d = document.querySelector('#p-screener .sc-dn-basis'); if (d) d.open = true; });
    ok(await page.evaluate(() => document.querySelector('#p-screener .sc-dn-basis').open), '口径可展开');

    ok(errors.length === 0, '控制台零报错（' + errors.slice(0, 3).join(' | ') + '）');
  } catch (e) {
    ok(false, '脚本异常：' + e.message);
  } finally {
    if (browser) await browser.close();
    srv.kill();
  }
  console.log(fails.length ? ('FAIL: ' + fails.length + ' 项') : 'ALL PASS');
  process.exit(fails.length ? 1 : 0);
})();
