/* 回归验证：方向研判「外溢 / 方向 / 枢纽 chip」点开透视弹窗（2026-09-27）
   需求原话：点开「福建板块 → 数字经济 交集24 市场2 局部共振」这类 chip，
             要能出一份报告说明：交集24 都包括什么、局部共振包括什么、为什么得出这个结论。
   验收点：
   ① 接口 /api/branch_detail 新增字段 reso/wide/state/state_rule/wide_list；阈值与 _sector_outlook 一致；
      降级（无 sector）走全市场概念；未知概念明确 unavailable；
   ② 前端：外溢 chip 可点 → 弹窗出「这个标签怎么读」+「① 交集的是哪些票」+「② 市场共振的是哪几只」+「对你的参考」，
      且市场共振表行数 == 接口 wide_list 长度、状态文案 == 接口 state；方向 chip 降级可用；枢纽个股 chip 可点开详情；
   ③ 控制台零报错。
   用法：node verify_outlook_click.cjs
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8802;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_outlook_click_modal.png');
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
    try { const r = await fetch(`${ROOT}/api/summary`); if (r.ok) return true; } catch (e) { /* not ready */ }
    await sleep(500);
  }
  return false;
}
const bd = (p) => fetch(`${ROOT}/api/branch_detail?` + new URLSearchParams(p)).then((r) => r.json());

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪:\n' + srvLog.slice(-1500));

    /* ---------- ① 接口层 ---------- */
    const A = await bd({ sector: '福建板块', branch: '数字经济' });
    ok('接口 正常路径可用', A.available === true, `overlap=${A.overlap} reso=${A.reso} wide=${A.wide} state=${A.state}`);
    ok('接口 交集名单非空且与 overlap 对齐', Array.isArray(A.stocks) && A.stocks.length > 0 && A.stocks.length <= A.overlap, `stocks=${A.stocks.length} overlap=${A.overlap}`);
    ok('接口 wide_list 长度 == wide', Array.isArray(A.wide_list) && A.wide_list.length === A.wide, `wide_list=${(A.wide_list || []).length} wide=${A.wide}`);
    ok('接口 wide_list 字段齐全', (A.wide_list || []).every((x) => x.code && 'pct' in x && 'net_yi' in x && typeof x.on_mainline === 'boolean'),
      (A.wide_list || []).map((x) => `${x.name}${x.on_mainline ? '主线内' : '概念内'}`).join(' '));
    ok('接口 state 与 wide 阈值一致', A.state === (A.wide >= 5 ? '已扩散' : (A.wide >= 1 ? '局部共振' : '尚未启动')), `${A.wide}→${A.state}`);
    ok('接口 state_rule 随包返回（口径不藏）', typeof A.state_rule === 'string' && A.state_rule.includes('已扩散'), (A.state_rule || '').slice(0, 34));

    const B = await bd({ sector: '福建板块', branch: '人工智能' });
    ok('接口 已扩散样本 wide>=5', B.available === true && B.wide >= 5 && B.state === '已扩散', `overlap=${B.overlap} wide=${B.wide}`);

    const C = await bd({ branch: '数字经济' });
    ok('接口 降级(无 sector)：可用且无主线交集', C.available === true && C.overlap === null && (C.sector_label || '').includes('全市场'), `sector_label=${C.sector_label} stocks=${(C.stocks || []).length}`);
    ok('接口 降级仍给出共振名单', C.wide >= 1 && (C.wide_list || []).length === C.wide, `wide=${C.wide}`);

    const D = await bd({ branch: 'XXYYZZ_NOT_EXIST' });
    ok('接口 未知概念明确不可用', D.available === false && !!D.message, String(D.message).slice(0, 30));

    /* ---------- ② 前端层 ---------- */
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1600,1200'],
      defaultViewport: { width: 1600, height: 1200 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('.nav-item[data-cat="research"]', { timeout: 30000 });
    await page.click('.nav-item[data-cat="research"]');
    await page.waitForSelector('.sub-tab[data-tab="lhb_sectors"]', { timeout: 15000 });
    await page.click('.sub-tab[data-tab="lhb_sectors"]');
    await page.waitForSelector('#p-lhb_sectors .ol-wrap', { timeout: 30000 });

    const spillInfo = await page.evaluate(() => {
      const box = document.getElementById('p-lhb_sectors');
      const chips = [...box.querySelectorAll('.ol-chip[data-ol-sector]')];
      const c = chips[0];
      return { n: chips.length, first: c ? { sec: c.dataset.olSector, br: c.dataset.olBranch, date: c.dataset.olDate, txt: c.textContent.replace(/\s+/g, ' ').trim() } : null };
    });
    ok('外溢 chip 已绑定可点（data-ol-sector）', spillInfo.n >= 1 && !!spillInfo.first, `${spillInfo.n} 个；首=${spillInfo.first && spillInfo.first.txt}`);

    /* 点第一个外溢 chip → 弹窗 */
    await page.evaluate(() => { document.querySelector('#p-lhb_sectors .ol-chip[data-ol-sector]').click(); });
    await page.waitForSelector('#branchModal .bm-read', { timeout: 20000 });
    const modal = await page.evaluate(() => {
      const m = document.getElementById('branchModal');
      const readRows = [...m.querySelectorAll('.bm-read-row')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
      const why = (m.querySelector('.bm-read-why') || {}).textContent || '';
      const state = ((m.querySelector('.bm-state') || {}).textContent || '').trim();
      const secT = [...m.querySelectorAll('.bm-sec')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
      let wideRows = -1;
      const wsec = [...m.querySelectorAll('.bm-sec')].find((x) => x.textContent.includes('市场共振的是哪几只'));
      if (wsec) { const tw = wsec.nextElementSibling; const tb = tw && tw.querySelector('tbody'); wideRows = tb ? tb.querySelectorAll('tr').length : 0; }
      return { readRows, why, state, secT, wideRows, hasRefer: !!m.querySelector('.bm-refer'), hasChips: m.querySelectorAll('.bm-chip[data-sym]').length };
    });
    ok('弹窗打开且含「这个标签怎么读」四行', modal.readRows.length >= 4, modal.readRows.join(' | ').slice(0, 150));
    ok('解读含 交集/主线内上榜/市场/状态 四要素',
      modal.readRows.some((r) => r.includes('交集')) && modal.readRows.some((r) => r.includes('主线内上榜')) && modal.readRows.some((r) => r.includes('市场')) && modal.readRows.some((r) => r.includes('状态')), '');
    ok('弹窗含「① 交集的是哪些票」', modal.secT.some((t) => t.includes('交集的是哪些票')), '');
    ok('弹窗含「② 市场共振的是哪几只」', modal.secT.some((t) => t.includes('市场共振的是哪几只')), '');
    ok('弹窗含「对你的参考（推断）」', modal.hasRefer === true, '');
    ok('弹窗内个股可点（bm-chip）', modal.hasChips >= 1, `${modal.hasChips} 个`);

    const cur = await bd({ sector: spillInfo.first.sec, branch: spillInfo.first.br, date: spillInfo.first.date });
    ok('弹窗状态文案 == 接口 state', modal.state === cur.state, `弹窗=${modal.state} 接口=${cur.state}`);
    ok('市场共振表行数 == 接口 wide_list 长度', modal.wideRows === (cur.wide_list || []).length, `表=${modal.wideRows} 接口=${(cur.wide_list || []).length}`);
    ok('结论文案非空（为什么下此结论）', (modal.why || '').length > 10, modal.why.slice(0, 60));

    await page.screenshot({ path: SHOT, fullPage: false });

    // 关闭外溢弹窗
    await page.evaluate(() => { const x = document.querySelector('#branchModal .bm-x'); if (x) x.click(); });
    await sleep(350);
    ok('弹窗可关闭', await page.evaluate(() => !document.getElementById('branchModal')), '');

    // 方向 chip → 降级弹窗（全市场概念）
    await page.evaluate(() => { const c = document.querySelector('#p-lhb_sectors .ol-chip[data-ol-branch]:not([data-ol-sector])'); if (c) c.click(); });
    await page.waitForSelector('#branchModal .bm-read', { timeout: 20000 });
    const dirWhy = await page.evaluate(() => ((document.querySelector('#branchModal .bm-read-why') || {}).textContent || ''));
    ok('方向 chip 可点（降级：全市场概念）', /全市场概念|无主线/.test(dirWhy), dirWhy.slice(0, 50));
    await page.evaluate(() => { const x = document.querySelector('#branchModal .bm-x'); if (x) x.click(); });
    await sleep(350);

    // 枢纽个股 chip → 个股详情抽屉
    const hubN = await page.evaluate(() => document.querySelectorAll('#p-lhb_sectors .ol-chip[data-ol-sym]').length);
    ok('枢纽个股 chip 已绑定可点（data-ol-sym）', hubN >= 1, `${hubN} 个`);
    if (hubN >= 1) {
      await page.evaluate(() => { document.querySelector('#p-lhb_sectors .ol-chip[data-ol-sym]').click(); });
      await page.waitForSelector('#drawer.show', { timeout: 25000 });
      ok('枢纽个股 chip 点开个股详情抽屉', true, '');
    }

    await page.screenshot({ path: path.join(BASE, '_outlook_click_full.png'), fullPage: true });

    const realErrs = errs.filter((t) => !/favicon|net::ERR_/i.test(t));
    ok('控制台零报错', realErrs.length === 0, realErrs.slice(0, 3).join(' ;; '));
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
