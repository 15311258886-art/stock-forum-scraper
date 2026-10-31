/* 回归：① 分组管理弹窗 ↑/↓ 调序真点真存真还原；② 加入自选池弹窗分组复选框横向排列。
   跑法：node verify_wgorder.cjs（临时端口 8795，不碰 8765 用户实例）。 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const PORT = 8795;
const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const env = { ...process.env, VIEWER_PORT: String(PORT) };
['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY'].forEach(k => delete env[k]);
const srv = spawn(PY, ['-m','viewer.server'], { cwd: BASE, env, stdio:['ignore','pipe','pipe'] });
const fails = [];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails.push(m); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  // 等服务起来
  let up = false;
  for (let i = 0; i < 30; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/`); if (r.ok) { up = true; break; } } catch (e) {}
    await sleep(500);
  }
  ok(up, '临时服务已起 (PORT ' + PORT + ')');
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: 'new', args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.evaluateOnNewDocument(() => {
    try { localStorage.setItem('extref.autoBackfillAt', String(Date.now())); } catch (e) {}
  });
  await page.goto(`http://127.0.0.1:${PORT}/#watch/watch`, { waitUntil: 'networkidle2', timeout: 60000 });
  // 等 WATCH.data 就位（WATCH 是顶层 const，页面作用域裸名可读；轮询最多 20s，未到就主动 loadWatch）
  let dataOk = false;
  for (let i = 0; i < 40; i++) {
    dataOk = await page.evaluate(() => !!(typeof WATCH !== 'undefined' && WATCH.data && (WATCH.data.groups || []).length));
    if (dataOk) break;
    if (i === 10) await page.evaluate(() => { try { loadWatch(); } catch (e) {} });
    await sleep(500);
  }
  ok(dataOk, 'WATCH.data 已加载（含分组）');
  await sleep(300);

  // ===== ② 加入自选池弹窗：复选框与组名横向并列 =====
  await page.evaluate(() => openWatchAddModal(null));
  await sleep(400);
  const addChk = await page.evaluate(() => {
    const el = document.querySelector('.wt-checks .wt-check');
    if (!el) return null;
    const cs = getComputedStyle(el);
    const input = el.querySelector('input[type="checkbox"]');
    const nm = el.querySelector('.wt-check-nm');
    const ri = input.getBoundingClientRect(), rn = nm.getBoundingClientRect();
    return { dir: cs.flexDirection, sameRow: Math.abs(ri.top - rn.top) < 4 && ri.left < rn.left };
  });
  ok(!!addChk, '弹窗分组复选框列表已渲染');
  ok(addChk && addChk.dir === 'row', 'flex-direction=row（实测 ' + (addChk && addChk.dir) + '）');
  ok(addChk && addChk.sameRow, '勾选框与组名在同一行、勾选框在左');
  await page.evaluate(() => { const o = document.querySelector('.modal-overlay'); if (o) o.remove(); });
  await sleep(200);

  // ===== ① 分组管理 ↑/↓：真点、真存、真还原 =====
  const before = await page.evaluate(() => (WATCH.data.groups || []).map(g => g.id));
  ok(before.length >= 2, '分组 ≥2 个可调序（' + before.length + ' 组）');
  await page.evaluate(() => openWatchGroupMgrModal());
  await sleep(400);
  let mv = await page.evaluate(() => document.querySelectorAll('.wt-gmv').length);
  ok(mv === before.length * 2, '↑/↓ 按钮数 = 组数×2（实测 ' + mv + '）');
  const waitOrder = async (expected) => {
    const want = expected.join(',');
    for (let i = 0; i < 50; i++) {   // 20s：容忍后台写入竞争（extref 回填等）
      const cur = await page.evaluate(() => (WATCH.data.groups || []).map(g => g.id).join(','));
      if (cur === want) return true;
      await sleep(400);
    }
    return false;
  };
  // 点第一组的 ↓ → 新顺序应为 [1,0,...]
  await page.evaluate(() => { document.querySelector('.wt-glist [data-gdn]').click(); });
  const downOk = await waitOrder([before[1], before[0], ...before.slice(2)]);
  const afterDown = await page.evaluate(() => (WATCH.data.groups || []).map(g => g.id));
  const diag = await page.evaluate(() => ({
    modalRows: [...document.querySelectorAll('.wt-glist .wt-gname')].map(i => i.dataset.gid),
    hint: (document.querySelector('#statusHint') || {}).textContent || '',
  }));
  console.log('  DIAG modalRows=' + diag.modalRows.join(',') + ' hint=' + diag.hint);
  ok(downOk, '点第一组 ↓ 后顺序交换（' + before.join(',') + ' → ' + afterDown.join(',') + '）');
  // 服务端已落库
  const srv1 = await (await fetch(`http://127.0.0.1:${PORT}/api/watch_list`)).json();
  ok(JSON.stringify((srv1.groups || []).map(g => g.id)) === JSON.stringify(afterDown), '服务端 watch_list 顺序与前端一致（已落库）');
  // 点（现在在第二位的原第一组）↑ 还原 —— 第一行的 ↑ 是禁用的，必须点第二行
  await page.evaluate((gid) => {
    const el = document.querySelector(`.wt-glist [data-gup="${gid}"]`);
    el.click();
  }, String(before[0]));
  const restoreOk = await waitOrder(before);
  const restored = await page.evaluate(() => (WATCH.data.groups || []).map(g => g.id));
  ok(restoreOk, '再点 ↑ 已还原原顺序（' + restored.join(',') + '）');
  // 边界：第一组的 ↑ / 最后一组的 ↓ 是禁用态
  const edge = await page.evaluate(() => ({
    firstUpDis: !!document.querySelector('.wt-glist [data-gup].wt-op-dis'),
    dnAll: [...document.querySelectorAll('.wt-glist [data-gdn]')].map(el => el.classList.contains('wt-op-dis')),
  }));
  ok(edge.firstUpDis, '第一组 ↑ 为禁用态');
  ok(edge.dnAll.length > 0 && edge.dnAll[edge.dnAll.length - 1], '最后一组 ↓ 为禁用态');
  await page.evaluate(() => { const o = document.querySelector('.modal-overlay'); if (o) o.remove(); });

  ok(errors.length === 0, '控制台零报错' + (errors.length ? '（' + errors[0] + '…）' : ''));
  await browser.close();
  console.log(fails.length ? 'WGORDER_FAIL (' + fails.length + ')' : 'WGORDER_PASS');
  srv.kill();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e); srv.kill(); process.exit(1); });
