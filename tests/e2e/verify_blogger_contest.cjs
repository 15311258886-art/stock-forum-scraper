// verify_blogger_contest.cjs —— 「博主荐股」+「当红参赛股」两板块布局与交互护栏
// 依赖：puppeteer-core（受管 node workspace）+ 项目 .venv 起后端
// 跑法：node verify_blogger_contest.cjs
// 覆盖：筛选 chip 数 / 搜索 / 排序 / 卡片渲染 / 逐日色格红涨绿跌 / 期数切换 / 零溢出 / 零控制台报错
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8816;
const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PY = BASE + '\\.venv\\Scripts\\python.exe';
const SHOT_BLOG = path.join(BASE, '_blogger_shot.png');
const SHOT_CONTEST = path.join(BASE, '_contest_shot.png');

const fails = [];
function ok(cond, msg) { if (!cond) { fails.push(msg); console.log('  x ' + msg); } else { console.log('  . ' + msg); } }

function getJSON(port, pathname) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: pathname }, (res) => {
      let buf = ''; res.on('data', (c) => buf += c);
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
  const env = { ...process.env, VIEWER_PORT: String(PORT) };
  ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'].forEach(k => delete env[k]);
  const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  srv.stderr.on('data', (d) => { const s = d.toString(); if (/ERROR|Traceback/.test(s)) process.stderr.write('[srv] ' + s); });

  let up = false;
  for (let i = 0; i < 40; i++) {
    try { const r = await getJSON(PORT, '/api/blogger/facets'); if (r.status) { up = true; break; } } catch (e) {}
    await new Promise(r => setTimeout(r, 500));
  }
  if (!up) { console.log('后端起不来，退出'); try { srv.kill(); } catch (e) {} process.exit(1); }
  console.log('后端已起 :%d\n', PORT);

  const chrome = findChrome();
  if (!chrome) { console.log('找不到 chromium，退出'); try { srv.kill(); } catch (e) {} process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: chrome, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000 });
  const errs = [];
  page.on('pageerror', e => errs.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

  console.log('A. 博主荐股面板');
  await page.goto('http://127.0.0.1:' + PORT + '/#opinion/blogger', { waitUntil: 'networkidle2' });
  await page.waitForSelector('#p-blogger .bt-card', { timeout: 30000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 1200));

  const blog = await page.evaluate(() => {
    const p = document.querySelector('#p-blogger');
    if (!p) return null;
    const over = [...p.querySelectorAll('.bt-card')].filter(c => {
      const r = c.getBoundingClientRect(); return r.right > window.innerWidth + 1;
    }).length;
    return {
      visible: p.offsetParent !== null,
      chips: [...p.querySelectorAll('.bt-chip')].map(e => e.textContent.trim()),
      cards: p.querySelectorAll('.bt-card').length,
      statsRows: p.querySelectorAll('.bt-detail > summary')[0] ? p.querySelectorAll('.bt-tb tbody tr').length : 0,
      hasUp: p.querySelectorAll('.bt-num.up').length,
      hasDn: p.querySelectorAll('.bt-num.dn').length,
      overflow: over,
      firstCard: (p.querySelector('.bt-card') || {}).textContent || '',
    };
  });
  ok(blog && blog.visible, '博主荐股面板已渲染可见');
  // 2026-10-12：博主数会随源头 Excel 增长（曾有 5 位，现 9 位 + 待采集占位）→ 改 ≥6
  ok(blog && blog.chips.length >= 6, '筛选 chip ≥6 个（全部 + 各博主），实=' + (blog && blog.chips.length) + ' → ' + (blog ? blog.chips.slice(0, 12).join(' / ') : ''));
  ok(blog && blog.cards > 100, '荐股卡片已渲染，条数=' + (blog && blog.cards));
  ok(blog && blog.hasUp > 0 && blog.hasDn > 0, '红涨绿跌着色生效（红' + (blog && blog.hasUp) + ' 绿' + (blog && blog.hasDn) + '）');
  ok(blog && blog.overflow === 0, '卡片零溢出（溢出=' + (blog && blog.overflow) + '）');

  // 筛选：点「独股九剑」
  const picked = await page.evaluate(() => {
    const all = [...document.querySelectorAll('#p-blogger .bt-chip')].find(x => x.textContent.trim().startsWith('全部'));
    const m = all ? (all.textContent.match(/(\d+)/) || [])[1] : null;
    const a = [...document.querySelectorAll('#p-blogger .bt-chip')].find(x => x.textContent.includes('独股九剑'));
    if (a) a.click();
    return { ok: !!a, allN: m ? Number(m) : null };
  });
  await new Promise(r => setTimeout(r, 500));
  const afterChip = await page.evaluate(() => ({
    cards: document.querySelectorAll('#p-blogger .bt-card').length,
    allBloggers: [...document.querySelectorAll('#p-blogger .bt-blog')].every(e => e.textContent.includes('独股九剑')),
  }));
  // 2026-10-12 改为**数据自适应**：博主数与推荐条数会随源头 Excel 增长（53 是旧快照），
  // 写死必然过期。断言"筛得动且真筛了"：结果 >0，且严格小于「全部」条数。
  ok(picked.ok && afterChip.cards > 0 && (picked.allN == null || afterChip.cards < picked.allN),
     `点「独股九剑」筛出 ${afterChip.cards} 条（全部 ${picked.allN == null ? "?" : picked.allN}）`);
  ok(afterChip.allBloggers, '筛选后卡片博主列全为「独股九剑」');

  // 搜索
  await page.evaluate(() => {
    const a = [...document.querySelectorAll('#p-blogger .bt-chip')].find(x => x.textContent.trim().startsWith('全部'));
    if (a) a.click();
  });
  await new Promise(r => setTimeout(r, 400));
  await page.click('#btSearch');
  await page.type('#btSearch', '万科');
  await new Promise(r => setTimeout(r, 900));
  const afterSearch = await page.evaluate(() => ({
    cards: document.querySelectorAll('#p-blogger .bt-card').length,
    names: [...document.querySelectorAll('#p-blogger .bt-card .bt-name')].map(e => e.textContent),
    focusKept: document.activeElement && document.activeElement.id === 'btSearch',
  }));
  ok(afterSearch.cards > 0, '搜索「万科」命中 ' + afterSearch.cards + ' 条（' + afterSearch.names.join('、') + '）');
  ok(afterSearch.focusKept, '搜索后输入框保持焦点（重绘不抢焦点）');

  await page.evaluate(() => { const s = document.querySelector('#btSearch'); if (s) { s.value = ''; } });
  await new Promise(r => setTimeout(r, 600));

  // 排序切换
  const beforeSort = await page.evaluate(() => (document.querySelector('#p-blogger .bt-card .bt-num') || {}).textContent || '');
  await page.evaluate(() => { const a = [...document.querySelectorAll('#p-blogger .bt-sort')].find(x => x.dataset.btSort === 'ret'); if (a) a.click(); });
  await new Promise(r => setTimeout(r, 500));
  const afterSort = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('#p-blogger .bt-card')];
    return { txt: (cards[0] || {}).textContent || '', n: cards.length };
  });
  ok(beforeSort !== afterSort.txt, '切「涨幅高→低」后首行变化（' + beforeSort.trim() + ' → ' + afterSort.txt.trim().slice(0, 26) + '）');

  await page.screenshot({ path: SHOT_BLOG, fullPage: false });
  console.log('  截图 → %s', path.basename(SHOT_BLOG));

  console.log('\nB. 当红参赛股面板');
  await page.evaluate(() => { try { location.hash = '#opinion/contest'; } catch (e) {} });
  await page.goto('http://127.0.0.1:' + PORT + '/#opinion/contest', { waitUntil: 'networkidle2' });
  await page.waitForSelector('#p-contest .ct-tb tbody tr', { timeout: 30000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 1200));

  const ct = await page.evaluate(() => {
    const p = document.querySelector('#p-contest');
    if (!p) return null;
    const over = [...p.querySelectorAll('.ct-cell, .ct-tb tbody td, .ct-tb thead th')].filter(c => {
      const r = c.getBoundingClientRect(); return r.right > window.innerWidth + 1;
    }).length;
    return {
      visible: p.offsetParent !== null,
      roundChips: [...p.querySelectorAll('.ct-chip')].map(e => e.textContent.trim()),
      rankRows: p.querySelectorAll('.ct-tb tbody tr').length,
      upCells: p.querySelectorAll('.ct-cell.ct-up').length,
      dnCells: p.querySelectorAll('.ct-cell.ct-dn').length,
      naCells: p.querySelectorAll('.ct-cell.ct-na, .ct-cell.ct-fut').length,
      hasTrackTable: !!p.querySelector('.ct-tb2'),
      overflow: over,
    };
  });
  const chipTxt = ct ? ct.roundChips.join(' / ') : '';
  ok(ct && ct.visible, '当红参赛股面板已渲染可见');
  // 2026-10-12 断言改为**数据自适应**（背后是 BRTV 每周一期，期数与每期人数都会变）：
  //   期数 ≥2；排名行数 ≥1（新一期可能只有 9 人）；色格渲染出来即可 —— 红涨绿跌的正确性
  //   由下面「切到 09-19 期（已有收盘数据）」那条断言验证（默认期若刚开赛，收盘全空是正常的）。
  ok(ct && ct.roundChips.length >= 2, '期数 ≥2，实=' + chipTxt);
  ok(ct && ct.rankRows >= 1, '排名表已渲染，实=' + (ct && ct.rankRows) + ' 行');
  ok(ct && (ct.upCells + ct.dnCells + ct.naCells) > 0,
     '逐日色格已渲染（涨' + (ct && ct.upCells) + '/跌' + (ct && ct.dnCells) + '/无数据' + (ct && ct.naCells) + '）');
  ok(ct && ct.hasTrackTable, '逐日追踪矩阵已渲染');
  ok(ct && ct.overflow === 0, '色格/表头零溢出（溢出=' + (ct && ct.overflow) + '）');

  // 切到 09-19 期（该期已结算，应有真实收益与积分）
  const switched = await page.evaluate(() => {
    const a = [...document.querySelectorAll('#p-contest .ct-chip')].find(x => x.textContent.includes('2026-09-19'));
    if (a) { a.click(); return true; }
    return false;
  });
  await new Promise(r => setTimeout(r, 1200));
  const ct19 = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('#p-contest .ct-tb tbody tr')].slice(0, 3);
    return {
      rows: rows.map(r => [...r.querySelectorAll('td')].map(t => t.textContent.trim())),
      up: document.querySelectorAll('#p-contest .ct-cell.ct-up').length,
      na: document.querySelectorAll('#p-contest .ct-cell.ct-na, #p-contest .ct-cell.ct-fut').length,
    };
  });
  ok(switched, '期数切换到 2026-09-19');
  ok(ct19.rows.length >= 3 && ct19.rows[0][4].indexOf('+') >= 0,
     '09-19 期第一行「本周最高收益」为正（' + (ct19.rows[0] ? ct19.rows[0].slice(1, 5).join(' / ') : '') + '）');
  ok(ct19.up > 0, '09-19 期逐日色格有上涨（红=' + ct19.up + '）');

  await page.screenshot({ path: SHOT_CONTEST, fullPage: false });
  console.log('  截图 → %s', path.basename(SHOT_CONTEST));

  console.log('\nC. 控制台');
  const realErrs = errs.filter(e => !/favicon|404 \(Not Found\)/i.test(e));
  ok(realErrs.length === 0, '控制台零报错（' + realErrs.length + ' 条' + (realErrs.length ? '：' + realErrs.slice(0, 3).join(' | ') : '') + '）');

  await browser.close();
  try { srv.kill(); } catch (e) {}

  console.log('\n=== ' + (fails.length ? 'FAIL ' + fails.length + ' 项' : 'ALL PASS') + ' ===');
  if (fails.length) { fails.forEach(f => console.log('  - ' + f)); process.exit(1); }
  process.exit(0);
})();
