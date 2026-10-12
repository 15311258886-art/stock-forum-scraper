/* 回归验证：板块动向 · 方向研判报告（2026-09-26）
   用户原话：「什么原因导致这几个板块上涨？牵扯到哪些板块？预测接下来有哪些？
             你做出一个相对比较专业的分析报告出来。但是要分开啊……分开写。」

   验收点：
   ① 接口 /api/lhb_sectors.outlook：三段数据齐全（成因 / 牵涉方向 / 接力方向），口径 >=4 条；
   ② 事实性护栏：下跌上榜不得把「有价格涨跌幅限制…」这种上涨话术误判成下跌（本日实测修过的 bug）；
      补涨候选必须真的未涨停（<9.8%）；接力方向不得混入风格 / 指数等统计口径；
   ③ 前端：真实点左栏（行情研判 → 板块动向），报告出现且**在表格下方**，三大分节标题齐全，
      chip 可点、口径可展开；控制台零报错。
   用法：node verify_sector_outlook.cjs   （截图 _sector_outlook_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8796;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_sector_outlook_shot.png');
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
    const d = await (await fetch(`${ROOT}/api/lhb_sectors`)).json();
    const ol = d.outlook || {};
    const secs = ol.sectors || [];
    ok('接口 outlook 存在', !!d.outlook && Object.keys(ol).length >= 5, Object.keys(ol).join(','));
    ok('接口 有数据日', !!ol.date, ol.date);
    ok('接口 研判板块数与本表一致', secs.length === (d.rows || []).length, `outlook=${secs.length} rows=${(d.rows || []).length}`);
    ok('接口 每板块都有成因文本', secs.length > 0 && secs.every((s) => (s.why || '').length > 20), secs.map((s) => (s.why || '').length).join('/'));
    ok('接口 每板块有资金形态', secs.length > 0 && secs.every((s) => (s.forms || []).length >= 1),
      secs.map((s) => (s.forms || []).map((f) => `${f.k}${f.n}`).join('+')).join(' | '));
    ok('接口 买方席位结构字段齐全', secs.every((s) => {
      const c = s.capital || {};
      return ['inst_seats', 'connect_seats', 'hot_seats', 'seat_prob_avg'].every((k) => k in c);
    }), secs.map((s) => `${s.sector}:机${s.capital.inst_seats}/通${s.capital.connect_seats}/游${s.capital.hot_seats}`).join(' '));
    ok('接口 每板块给出牵涉方向且带状态', secs.length > 0 && secs.every((s) => (s.spill || []).length >= 1 && s.spill.every((b) => ['已扩散', '局部共振', '尚未启动'].includes(b.state))),
      secs.map((s) => `${s.sector}:${(s.spill || []).length}个`).join(' '));
    ok('接口 接力方向非空', (ol.relay_dirs || []).length >= 3, (ol.relay_dirs || []).map((x) => `${x.name}${x.n}`).join(' '));
    ok('接口 枢纽个股非空（板块之间靠谁串起来）', (ol.hubs || []).length >= 1, (ol.hubs || []).map((h) => `${h.name}(${(h.sectors || []).length})`).join(' '));
    ok('接口 带口径说明（>=4 条，不藏算法）', Array.isArray(ol.basis) && ol.basis.length >= 4, (ol.basis || []).length + ' 条');

    /* 关键回归：本日实测修过的 bug —— 「有价格涨跌幅限制的…」含「跌幅」二字，
       曾把涨停票（福建水泥 +9.97%）误判成「下跌上榜」。下跌项涨幅必须为负。 */
    const fellAll = secs.flatMap((s) => (s.fell || []).map((x) => ({ ...x, sec: s.sector })));
    const fellBad = fellAll.filter((x) => !(x.pct < 0));
    ok('接口 下跌上榜判定未被「涨跌幅」话术误判', fellBad.length === 0,
      fellAll.length ? fellAll.map((x) => `${x.sec}/${x.name}${x.pct}%`).join(' ') : '（本日无下跌上榜项）');
    ok('接口 补涨候选均未涨停（<9.8%）', secs.every((s) => (s.relay || []).every((x) => x.pct == null || x.pct < 9.8)),
      secs.map((s) => `${s.sector}:${(s.relay || []).length}只`).join(' '));
    const STYLE = ['风格', '指数', '成分', '创业板', '主板', '次新', 'ST板块', '昨日', '高送转'];
    const styleBad = (ol.relay_dirs || []).filter((x) => STYLE.some((k) => (x.name || '').includes(k)));
    ok('接口 接力方向已剔除风格 / 指数等统计口径', styleBad.length === 0, styleBad.map((x) => x.name).join(','));

    /* ---------- ② 前端层（走真实导航，不用兜底函数） ---------- */
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
    const clicked = await page.$eval('.sub-tab[data-tab="lhb_sectors"]', (el) => el.textContent.trim());
    ok('真实点到「板块动向」子标签', clicked.includes('板块动向'), `TAB_CLICKED = ${clicked}`);

    await page.waitForSelector('#p-lhb_sectors .ol-wrap', { timeout: 30000 });
    const ui = await page.evaluate(() => {
      const box = document.getElementById('p-lhb_sectors');
      const olw = box.querySelector('.ol-wrap');
      const secT = [...box.querySelectorAll('.ol-sec-t')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
      const cards = box.querySelectorAll('.ol-card').length;
      const spillChips = box.querySelectorAll('.ol-spill .ol-chip').length;
      const relayChips = box.querySelectorAll('.ol-st-relay').length;
      const syms = box.querySelectorAll('.ol-sym[data-ol-sym]').length;
      const srcD = box.querySelectorAll('.ol-src > div').length;
      // 报告必须在原表格之后（不能把表挤掉 / 插错位置）
      const tbl = box.querySelector('table');
      const afterTable = !!(tbl && olw && (tbl.compareDocumentPosition(olw) & Node.DOCUMENT_POSITION_FOLLOWING));
      const hasTable = !!tbl;
      const whys = [...box.querySelectorAll('.ol-why')].map((x) => x.textContent.trim().length);
      return { html: olw ? olw.innerHTML.length : 0, secT, cards, spillChips, relayChips, syms, srcD, afterTable, hasTable, whys };
    });
    ok('板块动向面板出现研判报告区块', ui.html > 1500, `html=${ui.html}`);
    ok('原表格仍在（没被挤掉）', ui.hasTable);
    ok('报告位于表格下方', ui.afterTable);
    ok('三大分节标题齐全（为什么涨/牵涉哪些板块/接下来看什么）',
      ui.secT.length >= 3 && ui.secT.some((t) => t.includes('为什么涨')) && ui.secT.some((t) => t.includes('牵涉哪些板块')) && ui.secT.some((t) => t.includes('接下来看什么')),
      ui.secT.join(' | ').slice(0, 140));
    ok('一、为什么涨：每板块一张成因卡', ui.cards >= 4, `${ui.cards} 张`);
    ok('一、成因卡里有结论句', ui.whys.length >= 4 && ui.whys.every((n) => n > 20), ui.whys.join('/'));
    ok('二、牵涉哪些板块：有外溢方向 chip', ui.spillChips >= 8, `${ui.spillChips} 个`);
    ok('三、接下来看什么：有接力方向 chip', ui.relayChips >= 3, `${ui.relayChips} 个`);
    ok('补涨候选可点（复用个股详情）', ui.syms >= 3, `${ui.syms} 个`);
    ok('口径说明可展开且 >=4 条', ui.srcD >= 4, `${ui.srcD} 条`);

    await page.screenshot({ path: SHOT, fullPage: false });
    // 顺手整页截图，便于人工核对版式
    await page.screenshot({ path: path.join(BASE, '_sector_outlook_full.png'), fullPage: true });

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
