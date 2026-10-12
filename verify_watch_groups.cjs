/* 回归验证：关注池「分组随时新增 / 随时改名」（2026-09-27）
   需求（王总原话）：「这一块儿可以随时自己增加，可以随时自己改名儿啊，我指的是重点关注、
   观察中、基金池等等的这个啊。」→ 分组（含默认三组）要能在**标签行上**直接新增 / 改名。

   设计：自选池 → 关注池（#p-watch）标签行末尾加「＋ 新分组」胶囊（#wtGroupAddTab）；
        每个分组标签内嵌铅笔 ✎（.wt-tab-edit[data-wt-grenamE]），点它弹轻量输入框改名
        （stopPropagation，不触发筛选）。

   隔离：复制生产库（app.db + -wal + -shm）到临时目录，VIEWER_DB 指向副本、VIEWER_PORT=8805；
        改名 / 新增全部只落副本，不触碰生产数据。

   用法：node verify_watch_groups.cjs   （截图 _watch_groups_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8805;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_watch_groups_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;
const STAMP = Date.now();
const NEW_GROUP = '回归新增组' + String(STAMP).slice(-4);

// —— 隔离副本库：复制生产库三件套，绝不写生产 ——
const ISO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'vw_watch_iso_'));
const ISO_DB = path.join(ISO_DIR, 'app.db');
for (const suf of ['', '-wal', '-shm']) {
  const src = path.join(BASE, 'viewer', 'app.db' + suf);
  if (fs.existsSync(src)) fs.copyFileSync(src, ISO_DB + suf);
}
console.log('ISO_DB =', ISO_DB);

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
console.log('CHROME =', CHROME);

const env = { ...process.env, VIEWER_PORT: String(PORT), VIEWER_DB: ISO_DB };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];

const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', d => srvLog += d.toString());
srv.stderr.on('data', d => srvLog += d.toString());
const sleep = ms => new Promise(r => setTimeout(r, ms));

const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) fails.push(msg); };
const api = async () => (await (await fetch(ROOT + '/api/watch_list')).json());
const groupNames = (d) => (d.groups || []).map(g => g.name);

(async () => {
  try {
    for (let i = 0; i < 60; i++) {
      try { const r = await fetch(ROOT + '/api/summary'); if (r.status === 200) break; } catch (e) { /* retry */ }
      await sleep(500);
    }

    console.log('\n=== 1. 接口层 /api/watch_list（隔离副本） ===');
    let d = await api();
    const baseGroups = groupNames(d);
    console.log('  groups =', JSON.stringify(baseGroups));
    ok(Array.isArray(d.groups) && d.groups.length >= 1, '存在分组（默认已播种）');
    ok(baseGroups.includes('重点关注') && baseGroups.includes('观察中') && baseGroups.includes('基金池'),
       '含默认三组 重点关注 / 观察中 / 基金池');

    console.log('\n=== 2. 前端层（无头 Chromium） ===');
    const browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,900'],
      defaultViewport: { width: 1440, height: 900 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
    page.on('response', r => { if (r.status() >= 400) errs.push(`HTTP ${r.status()} ${r.url()}`); });

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#sidenav .nav-item', { timeout: 20000 });
    await sleep(1000);

    await page.click('#sidenav .nav-item[data-cat="watch"]');
    await sleep(1200);
    // 精确点「关注池」子标签，并打印真的点到了什么（防止"找不到就换方式渲染"的假 PASS）
    const subSel = '#subnav .sub-tab[data-tab="watch"]';
    await page.waitForSelector(subSel, { timeout: 8000 });
    await page.click(subSel);
    await sleep(2000);
    const TAB_CLICKED = await page.$eval('#subnav .sub-tab.active', el => el.dataset.tab).catch(() => null);
    console.log('  TAB_CLICKED =', TAB_CLICKED);
    ok(TAB_CLICKED === 'watch', '真的点到了「关注池」子标签');
    const active = await page.$eval('.panel.active', el => el.id).catch(() => null);
    ok(active === 'p-watch', '「关注池」面板被激活（p-watch）');

    // 标签行：＋ 新分组 胶囊 + 每个分组一个铅笔
    await page.waitForSelector('#wtGroupAddTab', { timeout: 8000 });
    const addTxt = await page.$eval('#wtGroupAddTab', el => el.textContent.trim());
    ok(/新分组/.test(addTxt), `标签行末尾有「＋ 新分组」入口（实际="${addTxt}"）`);
    const penN = (await page.$$('#p-watch .wt-tab-edit[data-wt-grename]')).length;
    const grpTabN = (await page.$$('#p-watch .wt-tab[data-wt-group]:not([data-wt-group="all"])')).length;
    console.log(`  分组标签=${grpTabN}  铅笔=${penN}`);
    ok(grpTabN === baseGroups.length, '分组标签数 == 接口分组数');
    ok(penN === baseGroups.length, '每个分组标签都有改名铅笔');

    // —— 改名：点第 1 个铅笔，且不触发筛选（stopPropagation 生效）——
    console.log('\n=== 3. 改名（含 stopPropagation 校验） ===');
    const beforeActive = await page.$eval('#p-watch .wt-tab.active', el => el.dataset.wtGroup).catch(() => null);
    const pen0 = await page.$('#p-watch .wt-tab-edit[data-wt-grename]');
    const gid0 = await pen0.evaluate(el => el.dataset.wtGrename);
    const oldName0 = await pen0.evaluate(el => el.dataset.wtGname);
    const RENAMED = oldName0 + '·改';
    console.log(`  目标分组 id=${gid0} 旧名=${oldName0} → 新名=${RENAMED}`);
    await pen0.click();
    const hasInput = await page.waitForSelector('#wtPromptInput', { timeout: 5000 }).then(() => true).catch(() => false);
    ok(hasInput, '点铅笔弹出输入框（#wtPromptInput）');
    const midActive = await page.$eval('#p-watch .wt-tab.active', el => el.dataset.wtGroup).catch(() => null);
    ok(midActive === beforeActive, `点铅笔不触发筛选切换（active 仍=${midActive}）`);
    await page.$eval('#wtPromptInput', (el, v) => { el.value = v; }, RENAMED);
    await page.click('.modal-overlay [data-pc="1"]');
    await sleep(1500);
    let d2 = await api();
    const renamed = (d2.groups || []).find(g => String(g.id) === String(gid0));
    console.log('  改名后 groups =', JSON.stringify(groupNames(d2)));
    ok(!!renamed && renamed.name === RENAMED, `接口里分组名已改为「${RENAMED}」`);
    ok(!groupNames(d2).includes(oldName0), '旧名已不存在');
    const domHasNew = await page.evaluate((nm) => document.querySelector('#p-watch .wt-tabs').textContent.includes(nm), RENAMED);
    ok(domHasNew, '标签行上已显示新分组名');

    // —— 新增：点「＋ 新分组」——
    console.log('\n=== 4. 新增分组 ===');
    await page.click('#wtGroupAddTab');
    const hasInput2 = await page.waitForSelector('#wtPromptInput', { timeout: 5000 }).then(() => true).catch(() => false);
    ok(hasInput2, '点「＋ 新分组」弹出输入框');
    await page.$eval('#wtPromptInput', (el, v) => { el.value = v; }, NEW_GROUP);
    await page.click('.modal-overlay [data-pc="1"]');
    await sleep(1500);
    const d3 = await api();
    console.log('  新增后 groups =', JSON.stringify(groupNames(d3)));
    ok(groupNames(d3).includes(NEW_GROUP), `接口里出现新分组「${NEW_GROUP}」`);
    const domHasNewGroup = await page.evaluate((nm) => document.querySelector('#p-watch .wt-tabs').textContent.includes(nm), NEW_GROUP);
    ok(domHasNewGroup, '标签行上出现新分组标签');
    const penN2 = (await page.$$('#p-watch .wt-tab-edit[data-wt-grename]')).length;
    ok(penN2 === (d3.groups || []).length, '新分组同样带了改名铅笔（可继续改名）');

    await page.screenshot({ path: SHOT, fullPage: false });
    console.log('  截图 =', SHOT);

    console.log('\n=== 5. 控制台 / 网络 ===');
    const realErrs = errs.filter(e => !/favicon/.test(e));
    console.log('  报错 =', realErrs.length ? JSON.stringify(realErrs.slice(0, 8)) : '（无）');
    ok(realErrs.length === 0, '控制台零报错、零 4xx/5xx');

    await browser.close();
  } catch (e) {
    fails.push('异常：' + (e && e.message));
    console.log('  EXCEPTION  ' + (e && e.stack || e));
    console.log('--- server log tail ---\n' + srvLog.slice(-2000));
  } finally {
    try { srv.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(ISO_DIR, { recursive: true, force: true }); console.log('  已清理副本库目录'); } catch (e) { /* ignore */ }
  }
  console.log('\n================ 结论 ================');
  if (fails.length) { console.log(`FAIL（${fails.length} 项）`); fails.forEach(f => console.log('  - ' + f)); process.exit(1); }
  console.log('全部 PASS');
  process.exit(0);
})();
