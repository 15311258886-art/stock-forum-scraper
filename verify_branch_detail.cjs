/* 回归验证：分支透视（2026-09-26）
   用户原话：「我点了之后没有反应啊，你应该把这几个点完之后，都有谁推荐的是谁，
             谁有可能涨多少，大致的判断什么的，你要给我列出来呀，你不能光给我列数字。」

   验收点：
   ① 接口 /api/branch_detail：available、成员股非空、判断文本非空、口径随包返回；
   ② 前端：点看板上的分支标签 → 弹窗出现，里面有「成员股表」（不是只有一个数字）；
   ③ 弹窗能关（× 与 ESC 两条路都测）；控制台零报错。
   用法：node verify_branch_detail.cjs   （截图 _branch_detail_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8795;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_branch_detail_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;

const PW = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
let CHROME = null;
if (fs.existsSync(PW)) {
  for (const d of fs.readdirSync(PW)) {
    if (d.startsWith('chromium-')) {
      const p = path.join(PW, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(p)) { CHROME = p; break; }
    }
  }
}

const env = { ...process.env, VIEWER_PORT: String(PORT) };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];
const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', (d) => { srvLog += d.toString(); });
srv.stderr.on('data', (d) => { srvLog += d.toString(); });

const results = [];
const ok = (name, pass, extra) => {
  results.push([name, !!pass, extra === undefined ? '' : String(extra)]);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '  → ' + extra : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitPort() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`${ROOT}/api/summary`);
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await sleep(500);
  }
  return false;
}

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪：\n' + srvLog.slice(-1500));

    /* ---------- ① 接口层 ---------- */
    const d = await (await fetch(`${ROOT}/api/branch_detail?sector=${encodeURIComponent('福建板块')}&branch=${encodeURIComponent('人工智能')}`)).json();
    ok('接口 available', d && d.available === true, d && d.message);
    ok('接口 成员股非空', (d.stocks || []).length > 0, `${(d.stocks || []).length} 只`);
    ok('接口 成员按重合口径给出数量', Number(d.overlap) > 0, `overlap=${d.overlap}`);
    ok('接口 判断文本非空', !!(d.judge && (d.judge.text || '').length > 8), (d.judge || {}).text);
    ok('接口 带口径说明（不藏算法）', Array.isArray((d.judge || {}).basis) && d.judge.basis.length >= 3);
    ok('接口 票有代码与涨幅字段', (d.stocks || []).every((s) => s.code && ('pct' in s)));
    ok('接口 缺参报错而非静默', await (async () => {
      const r = await fetch(`${ROOT}/api/branch_detail?sector=&branch=`);
      return r.status === 400;
    })(), 'sector/branch 为空应 400');

    /* ---------- ② 前端层 ---------- */
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1600,1000'],
      defaultViewport: { width: 1600, height: 1000 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#p-dashboard .ml-branch', { timeout: 30000 });
    const nBranch = await page.$$eval('#p-dashboard .ml-branch', (els) => els.length);
    ok('看板渲染出分支标签', nBranch > 0, `${nBranch} 个`);
    const branchLabel = await page.$eval('#p-dashboard .ml-branch', (el) => el.textContent.trim());
    ok('分支标签可点击（有 role=button）', await page.$eval('#p-dashboard .ml-branch', (el) => el.getAttribute('role') === 'button'));

    /* 点开第一个分支 → 弹窗 */
    await page.$eval('#p-dashboard .ml-branch', (el) => el.click());
    await page.waitForSelector('#branchModal .bm-table', { timeout: 30000 });
    const modal = await page.evaluate(() => {
      const m = document.getElementById('branchModal');
      if (!m) return null;
      const rows = m.querySelectorAll('.bm-table tbody tr').length;
      const judge = (m.querySelector('.bm-judge') || {}).textContent || '';
      const secs = [...m.querySelectorAll('.bm-sec')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
      const tables = m.querySelectorAll('.bm-table').length;
      const chips = m.querySelectorAll('.bm-chip[data-sym]').length;
      const sub = (m.querySelector('.bm-sub') || {}).textContent || '';
      return { rows, judge: judge.trim(), secs, tables, chips, sub: sub.trim(), html: m.innerHTML.length };
    });
    ok('点分支后弹窗出现且不小', modal && modal.html > 500, modal && modal.html);
    ok('弹窗里有名单表格', modal && modal.tables >= 1, modal && `tables=${modal.tables} rows=${modal.rows}`);
    ok('名单里确实列出了个股行（不是只有一个数字）', modal && modal.rows >= 1, modal && `rows=${modal.rows}`);
    ok('有成员股可点（复用个股详情）', modal && modal.chips >= 1, modal && `chips=${modal.chips}`);
    ok('结论条非空（给了判断）', modal && modal.judge.length > 10, modal && modal.judge.slice(0, 90));
    ok('分节标题齐全（都有谁/谁在买/谁在说）', modal && modal.secs.length >= 3, modal && modal.secs.join(' | ').slice(0, 120));

    await page.screenshot({ path: SHOT });

    /* 关闭：× 与 ESC */
    await page.click('#branchModal .bm-x');
    await sleep(300);
    ok('点 × 能关掉弹窗', await page.evaluate(() => !document.getElementById('branchModal')));
    await page.$eval('#p-dashboard .ml-branch', (el) => el.click());
    await page.waitForSelector('#branchModal', { timeout: 15000 });
    await page.keyboard.press('Escape');
    await sleep(300);
    ok('按 ESC 能关掉弹窗', await page.evaluate(() => !document.getElementById('branchModal')));

    const realErrs = errs.filter((t) => !/favicon|net::ERR_/i.test(t));
    ok('控制台零报错', realErrs.length === 0, realErrs.slice(0, 3).join(' ;; '));
    console.log(`\n分支标签样例：${branchLabel}`);
  } catch (e) {
    ok('执行异常', false, e.message);
  } finally {
    if (browser) { try { await browser.close(); } catch (e) { /* ignore */ } }
    srv.kill();
  }
  const bad = results.filter((r) => !r[1]);
  console.log(`\n===== ${results.length - bad.length}/${results.length} PASS =====`);
  if (bad.length) { console.log('失败项：' + bad.map((b) => b[0]).join(' / ')); process.exitCode = 1; }
  setTimeout(() => process.exit(process.exitCode || 0), 300);
})();
