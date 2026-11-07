/* 回归验证：关注池「多组并存 / ×N 徽标 / 多选分组 / 组内排序 / 拖拽 / 移出本组」UI 级（2026-10-14）
   独立端到端回归（QA 严过关）：无头 Chromium（puppeteer-core，CDP 协议）+ 临时后端实例。

   隔离纪律：写操作全部落在 VIEWER_DB 隔离副本（项目 sanctioned 机制，db.py 注释明示），
   生产库零写入；用户桌面实例（8765）不受影响。测试结束后副本状态回验为初始态。

   本机环境：shell stdout 捕获故障 → 所有诊断输出写文件，结果落盘 _qa_watch_ui_result.json。
   用法：node verify_watch_multi.cjs   （退出码 0=无 FAIL；1=有 FAIL；2=环境 BLOCKED）
*/
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PY = BASE + '\\.venv\\Scripts\\python.exe';
const PORT = 8795;
const ROOT = `http://127.0.0.1:${PORT}`;
const WORK = BASE + '\\_qa_watch_work';
const RUN_ID = String(Date.now());
const ISO_DIR = WORK + '\\iso_ui-' + RUN_ID;          // 每轮全新目录：零删除，绕过 safe-delete 守卫
const LOG = WORK + '\\ui_run-' + RUN_ID + '.log';
const RESULT = BASE + '\\_qa_watch_ui_result.json';
const SRV_LOG = WORK + '\\ui_server-' + RUN_ID + '.log';
const DEADLINE = Date.now() + 520 * 1000;          // 全局时限保护：留 80s 余量给清理

// puppeteer-core 解析（workspace 优先，项目 node_modules 兜底）
for (const c of ['C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules',
                 BASE + '\\node_modules']) {
  try { require.resolve('puppeteer-core', { paths: [c] }); module.paths.push(c); break; } catch (e) { /* next */ }
}
const puppeteer = require('puppeteer-core');

function log(msg) {
  const s = typeof msg === 'string' ? msg : JSON.stringify(msg);
  try { fs.appendFileSync(LOG, s + '\n'); } catch (e) { /* ignore */ }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const RESULTS = [];
function check(name, cond, detail = '', status) {
  const st = status || (cond ? 'PASS' : 'FAIL');
  RESULTS.push({ name, status: st, detail: String(detail) });
  log(`[${st}] ${name} | ${detail}`);
}

// Chromium 定位（ms-playwright）
function findChrome() {
  const PW = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
  if (fs.existsSync(PW)) {
    for (const d of fs.readdirSync(PW)) {
      if (d.startsWith('chromium-')) {
        const p = path.join(PW, d, 'chrome-win64', 'chrome.exe');
        if (fs.existsSync(p)) return p;
      }
    }
  }
  return null;
}

async function apiPost(ep, body) {
  const r = await fetch(ROOT + ep, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
  return r.json();
}

async function getRows() {
  return page.$$eval('#p-watch tbody tr', (trs) => trs.map((tr) => {
    const sym = tr.querySelector('.wt-sym');
    const multi = tr.querySelector('.wt-multi');
    return {
      symbol: sym ? sym.textContent.trim() : '',
      id: tr.getAttribute('data-wt-drag') || '',
      multiText: multi ? multi.textContent.trim() : null,
      multiTitle: multi ? (multi.getAttribute('title') || '') : null,
      chips: tr.querySelectorAll('.wt-strat .wt-chip').length,
      hasUp: !!tr.querySelector('[data-wt-up]'),
      hasDown: !!tr.querySelector('[data-wt-down]'),
      hasGdel: !!tr.querySelector('[data-wt-gdel-item]'),
    };
  }));
}
const rowSymbols = async () => (await getRows()).map((r) => r.symbol);

async function waitOrder(symbols, timeout = 45000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    const cur = await rowSymbols();
    if (JSON.stringify(cur) === JSON.stringify(symbols)) return true;
    await sleep(900);
  }
  return false;
}
async function waitSymbol(sym, present, timeout = 45000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    const cur = await rowSymbols();
    if (present ? cur.includes(sym) : !cur.includes(sym)) return true;
    await sleep(900);
  }
  return false;
}

let page = null;
let browser = null;
let srv = null;

(async () => {
  const t0 = Date.now();
  const el = (ms) => ((Date.now() - t0) / 1000).toFixed(1) + 's';
  WORK && fs.mkdirSync(WORK, { recursive: true });
  log('=== UI 回归启动 ' + new Date().toISOString() + ' RUN_ID=' + RUN_ID + ' ===');

  // —— 0. 隔离副本 + 临时后端（8795）——
  const mk = spawnSync(PY, [BASE + '\\verify_watch_api.py', 'mkiso', ISO_DIR], { encoding: 'utf8' });
  const isoDb = (mk.stdout || '').trim().split('\n').pop();
  if (mk.status !== 0 || !isoDb || !fs.existsSync(isoDb)) {
    check('ENV.1 隔离副本制作', false, 'mkiso failed: ' + (mk.stderr || mk.stdout), 'BLOCKED');
    return finish(2);
  }
  log('ISO_DB = ' + isoDb);
  const env = Object.assign({}, process.env, { VIEWER_PORT: String(PORT), VIEWER_DB: isoDb });
  for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) delete env[k];
  srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  fs.writeFileSync(WORK + '\\ui_server.pid', String(srv.pid));
  const slf = fs.createWriteStream(SRV_LOG);
  srv.stdout.pipe(slf); srv.stderr.pipe(slf);

  // 等待就绪（探静态页，200 即已可服务）
  let ready = false;
  for (let i = 0; i < 120; i++) {
    try { const r = await fetch(ROOT + '/', { signal: AbortSignal.timeout(5000) }); if (r.status === 200) { ready = true; break; } } catch (e) { /* retry */ }
    await sleep(1000);
  }
  check('ENV.2 临时后端 8795 就绪', ready, ready ? '' : 'server log tail: ' + tail(SRV_LOG), ready ? 'PASS' : 'BLOCKED');
  if (!ready) return finish(2);

  // —— 1. 无头 Chromium ——
  const CHROME = findChrome();
  if (!CHROME) { check('ENV.3 Chromium 可定位', false, 'ms-playwright chromium not found', 'BLOCKED'); return finish(2); }
  try {
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,900',
             '--user-data-dir=' + WORK + '\\chrome-profile-' + Date.now()],
      defaultViewport: { width: 1440, height: 900 },
    });
  } catch (e) {
    check('ENV.3 Chromium 启动', false, e.message, 'BLOCKED'); return finish(2);
  }
  page = await browser.newPage();
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') { const loc = m.location && m.location(); errs.push({ type: 'console', text: m.text(), url: loc ? loc.url : '' }); } });
  page.on('pageerror', (e) => errs.push({ type: 'pageerror', text: e.message, url: '' }));
  page.on('response', (r) => { if (r.status() >= 400) errs.push({ type: 'http', status: r.status(), url: r.url() }); });
  page.on('dialog', (d) => d.accept());
  let lastReorder = null;
  page.on('request', (r) => { if (r.url().includes('/api/watch_item_reorder')) lastReorder = r.postData(); });

  try {
    await page.goto(ROOT + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) { log('initial goto: ' + e.message); }

  // localStorage（按任务书；两个 key 为历史遗留，当前代码未读取，设置无害）
  await page.evaluate(() => { try { localStorage.setItem('wb_dark', '0'); localStorage.setItem('text_select', '1'); } catch (e) { /* ignore */ } });
  try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('reload after localStorage: ' + e.message); }

  async function gotoWatch() {
    await page.waitForSelector('#sidenav .nav-item', { timeout: 20000 });
    await page.click('#sidenav .nav-item[data-cat="watch"]');
    await page.waitForSelector('#subnav .sub-tab[data-tab="watch"]', { timeout: 10000 });
    await page.click('#subnav .sub-tab[data-tab="watch"]');
    await page.waitForSelector('#p-watch .wt-tb tbody tr', { timeout: 60000 });
    await sleep(600);
  }
  async function clickGroup(gid) {
    await page.click(`#p-watch .wt-tab[data-wt-group="${gid}"]`);
    await page.waitForSelector(`#p-watch .wt-tab.active[data-wt-group="${gid}"]`, { timeout: 5000 });
    await sleep(400);
  }
  const shot = (n) => page.screenshot({ path: BASE + '\\' + n });

  await gotoWatch();
  log('导航到关注池完成 ' + el());

  // ================= C1 战法信号 =================
  if (Date.now() < DEADLINE - 30000) {
    try {
      await clickGroup('3');
      const rows = await getRows();
      const jc = rows.find((r) => r.symbol === '603979');
      check('C1.1 长期潜伏视图金诚信行战法信号列有 ≥1 个 .wt-chip', jc && jc.chips >= 1, `chips=${jc && jc.chips}`);
      const th = await page.$eval('#p-watch thead th:nth-child(6)', (x) => x.getAttribute('title') || '');
      check('C1.2 战法列头 title 含 signal_date 2026-10-13', /2026-10-13/.test(th), th);
      await shot('_watch_strat.png');
      log('截图 _watch_strat.png ' + el());
    } catch (e) { check('C1 阶段异常', false, e.message); }
  } else { check('C1', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C2 ×N 徽标 =================
  if (Date.now() < DEADLINE - 30000) {
    try {
      const addR = await apiPost('/api/watch_item_add', { symbol: '603979', groups: [4] });
      check('C2.1 API 追加金诚信到「短期」ok/existed/hint', addR.ok && addR.existed && /已追加到分组/.test(addR.hint || ''), JSON.stringify(addR));
      try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('C2 reload: ' + e.message); }
      await gotoWatch();
      const rowsAll = await getRows();
      const jc2 = rowsAll.find((r) => r.symbol === '603979');
      check('C2.2 全部视图金诚信有 .wt-multi 徽标且文本=×2', jc2 && jc2.multiText === '×2', `multi=${jc2 && jc2.multiText}`);
      check('C2.3 .wt-multi 悬浮 title 含两个组名', jc2 && /长期潜伏/.test(jc2.multiTitle || '') && /短期/.test(jc2.multiTitle || ''), jc2 && jc2.multiTitle);
      const n4 = await page.$eval('#p-watch .wt-tab[data-wt-group="4"] b', (x) => x.textContent.trim());
      check('C2.4 「短期」tab 计数 == 1', n4 === '1', `n=${n4}`);
      await shot('_watch_multi.png');
      log('截图 _watch_multi.png ' + el());
    } catch (e) { check('C2 阶段异常', false, e.message); }
  } else { check('C2', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C3 多选分组（加入弹窗） =================
  if (Date.now() < DEADLINE - 30000) {
    try {
      await page.click('#wtAdd');
      await page.waitForSelector('.modal-overlay .wt-checks', { timeout: 8000 });
      const nBox = (await page.$$('.modal-overlay .wt-fgroup')).length;
      check('C3.1 加入弹窗分组区为 .wt-checks checkbox 列表（5 组）', nBox === 5, `n=${nBox}`);
      const nSel = (await page.$$('.modal-overlay .wt-checks select')).length;
      check('C3.2 分组区不是单选 select', nSel === 0, `select=${nSel}`);
      await page.click('.modal-overlay .wt-fgroup[value="3"]');
      await page.click('.modal-overlay .wt-fgroup[value="5"]');
      const checked = await page.$$eval('.modal-overlay .wt-fgroup', (els) => els.filter((e) => e.checked).map((e) => e.value));
      check('C3.3 可同时勾选多个分组', checked.length === 2 && checked.includes('3') && checked.includes('5'), JSON.stringify(checked));
      await page.click('.modal-overlay .modal-card .btn:not(.primary)');
      await sleep(500);
      const modalGone = (await page.$('.modal-overlay')) === null;
      check('C3.4 取消后弹窗关闭、未提交', modalGone, `modalExists=${!modalGone}`);
    } catch (e) { check('C3 阶段异常', false, e.message); }
  } else { check('C3', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C4 编辑预勾 =================
  if (Date.now() < DEADLINE - 30000) {
    try {
      await page.click('#p-watch [data-wt-edit="26"]');
      await page.waitForSelector('.modal-overlay .wt-checks', { timeout: 8000 });
      const c4 = await page.$$eval('.modal-overlay .wt-fgroup', (els) => els.filter((e) => e.checked).map((e) => e.value));
      check('C4.1 金诚信编辑弹窗预勾 长期潜伏(3)+短期(4)', c4.length === 2 && c4.includes('3') && c4.includes('4'), JSON.stringify(c4));
      await page.click('.modal-overlay .modal-card .btn:not(.primary)');
      await sleep(500);
    } catch (e) { check('C4 阶段异常', false, e.message); }
  } else { check('C4', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C5 排序按钮 =================
  const ORIG = ['603979', '600188', '002756', '603288', '600547', '688117'];   // 长期潜伏初始序（id 26,25,20,18,5,3）
  const MOVED = ['600188', '603979', '002756', '603288', '600547', '688117'];  // 首行下移后
  if (Date.now() < DEADLINE - 60000) {
    try {
      await clickGroup('3');
      const before = await rowSymbols();
      check('C5.1 长期潜伏初始顺序符合预期', JSON.stringify(before) === JSON.stringify(ORIG), JSON.stringify(before));
      const r0 = (await getRows())[0], r1 = (await getRows())[1], r5 = (await getRows())[5];
      check('C5.2 组视图每行有 ↑↓ 控件（首行↓、次行↑↓、末行↑）',
        r0.hasDown && r1.hasUp && r1.hasDown && r5.hasUp, `r0.down=${r0.hasDown} r1.up=${r1.hasUp} r1.down=${r1.hasDown} r5.up=${r5.hasUp}`);
      await page.click('#p-watch tbody tr:first-child [data-wt-down]');
      const moved = await waitOrder(MOVED, 45000);
      check('C5.3 点第一行 ↓ 后该行下移一位', moved,
        moved ? '' : 'actual=' + JSON.stringify(await rowSymbols()) + ' reorderPayload=' + lastReorder);
      try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('C5 reload: ' + e.message); }
      await gotoWatch(); await clickGroup('3');
      const afterReload = await rowSymbols();
      check('C5.4 reload 后顺序保持（持久化）',
        JSON.stringify(afterReload) === JSON.stringify(moved ? MOVED : before), JSON.stringify(afterReload));
      await apiPost('/api/watch_item_reorder', { group_id: 3, ids: [26, 25, 20, 18, 5, 3] });
      try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('C5 restore reload: ' + e.message); }
      await gotoWatch(); await clickGroup('3');
      const restored = await rowSymbols();
      check('C5.5 还原后顺序 == 初始序', JSON.stringify(restored) === JSON.stringify(ORIG), JSON.stringify(restored));
      await shot('_watch_reorder.png');
      log('截图 _watch_reorder.png ' + el());
    } catch (e) { check('C5 阶段异常', false, e.message); }
  } else { check('C5', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C6 HTML5 拖拽 =================
  if (Date.now() < DEADLINE - 60000) {
    try {
      await clickGroup('3');
      lastReorder = null;
      const boxes = await page.$$eval('#p-watch tbody tr', (trs) => trs.slice(0, 3).map((tr) => {
        const b = tr.getBoundingClientRect();
        return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
      }));
      let simMode = 'none';
      let dragClient = null;
      try {
        dragClient = await page.target().createCDPSession();
        await dragClient.send('Input.setInterceptDrags', { enabled: true });
        await dragClient.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: boxes[0].x, y: boxes[0].y, button: 'left', clickCount: 1 });
        await dragClient.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: boxes[0].x + 15, y: boxes[0].y + 15, button: 'left' });
        const intercepted = await Promise.race([
          new Promise((res) => dragClient.once('Input.dragIntercepted', res)),
          sleep(5000).then(() => null),
        ]);
        if (intercepted && intercepted.data) {
          simMode = 'cdp';
          const data = intercepted.data;
          await dragClient.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: boxes[2].x, y: boxes[2].y, button: 'left' });
          await dragClient.send('Input.dispatchDragEvent', { type: 'dragEnter', x: boxes[2].x, y: boxes[2].y, data });
          await dragClient.send('Input.dispatchDragEvent', { type: 'dragOver', x: boxes[2].x, y: boxes[2].y, data });
          await dragClient.send('Input.dispatchDragEvent', { type: 'drop', x: boxes[2].x, y: boxes[2].y, data });
          await dragClient.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: boxes[2].x, y: boxes[2].y, button: 'left' });
        } else {
          simMode = await page.evaluate(() => {
            const trs = Array.from(document.querySelectorAll('#p-watch tbody tr[draggable]'));
            if (trs.length < 3) return 'none';
            const src = trs[0], dst = trs[2];
            const dt = new DataTransfer();
            src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }));
            dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
            dst.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
            src.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: dt }));
            return 'dom';
          });
        }
      } catch (e) { log('drag sim exception: ' + e.message); }
      try { if (dragClient) { await dragClient.send('Input.setInterceptDrags', { enabled: false }); await dragClient.detach(); } } catch (e) { /* ignore */ }
      await sleep(2500);
      const fired = !!lastReorder;
      check('C6.1 拖拽模拟触发 drop 处理器（reorder API 收到提交序）', fired, `simMode=${simMode} payload=${lastReorder}`);
      if (fired) {
        const DRAGGED = ['600188', '002756', '603979', '603288', '600547', '688117'];
        const ok6 = await waitOrder(DRAGGED, 45000);
        check('C6.2 拖拽第一行到第三行后顺序变为 [600188,002756,603979,…]', ok6,
          ok6 ? `simMode=${simMode}` : `simMode=${simMode} actual=${JSON.stringify(await rowSymbols())}`);
      } else {
        check('C6.2 拖拽后顺序变化', false, `PARTIAL：拖拽事件模拟未生效（simMode=${simMode}）——无头环境拖拽依赖真实指针，按钮路径已由 C5 覆盖`, 'PARTIAL');
      }
      await apiPost('/api/watch_item_reorder', { group_id: 3, ids: [26, 25, 20, 18, 5, 3] });
      // CDP 原始输入派发后会污染页面输入状态（无头环境实测：其后 click 失灵）——已 detach 会话，再 reload 复位
      try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('C6 post-drag reload: ' + e.message); }
      await gotoWatch();
    } catch (e) { check('C6 阶段异常', false, e.message); }
  } else { check('C6', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C7 移出本组 =================
  if (Date.now() < DEADLINE - 60000) {
    try {
      await clickGroup('3');
      await page.click('#p-watch [data-wt-gdel-item="26"]');   // confirm 由 dialog 自动接受
      const gone = await waitSymbol('603979', false, 45000);
      check('C7.1 金诚信（×2 态）从长期潜伏视图消失', gone, gone ? '' : 'actual=' + JSON.stringify(await rowSymbols()));
      await clickGroup('4');
      const rowsG4 = await rowSymbols();
      check('C7.2 金诚信仍在「短期」组视图', rowsG4.includes('603979'), JSON.stringify(rowsG4));
      await clickGroup('all');
      const rowsAll2 = await rowSymbols();
      check('C7.3 金诚信仍在全部视图', rowsAll2.includes('603979'), JSON.stringify(rowsAll2));
      await apiPost('/api/watch_item_add', { symbol: '603979', groups: [3] });
      const rem = await apiPost('/api/watch_item_group_remove', { id: 26, group_id: 4 });
      check('C7.4 还原：金诚信仅长期潜伏（left=1）', rem.ok && rem.left === 1, JSON.stringify(rem));
      // add_to_group 把重新加回的票追加到组尾（max+10），需再提交初始序才能精确还原（C9.4 断言初始序）
      await apiPost('/api/watch_item_reorder', { group_id: 3, ids: [26, 25, 20, 18, 5, 3] });
      try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }); } catch (e) { log('C7 reload: ' + e.message); }
      await gotoWatch();
    } catch (e) { check('C7 阶段异常', false, e.message); }
  } else { check('C7', false, 'BLOCKED: 全局时限', 'BLOCKED'); }

  // ================= C8 控制台零报错 =================
  try {
    const ourErrs = errs.filter((e) => (e.url || '').includes(`127.0.0.1:${PORT}`) && !/favicon/.test(e.url || ''));
    const extErrs = errs.filter((e) => !(e.url || '').includes(`127.0.0.1:${PORT}`));
    check('C8.1 自有接口/静态资源零 4xx/5xx、控制台零 pageerror', ourErrs.length === 0, JSON.stringify(ourErrs.slice(0, 6)));
    log('外部请求报错（甄别不计 FAIL，共 ' + extErrs.length + ' 条）: ' + JSON.stringify(extErrs.slice(0, 10)));
  } catch (e) { check('C8 阶段异常', false, e.message); }

  // ================= C9 终态回验（隔离副本 DB 直查） =================
  try {
    const chkPy = WORK + '\\ui_dbcheck.py';
    fs.writeFileSync(chkPy, [
      'import json, sqlite3, sys',
      'db = sys.argv[1]',
      'c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)',
      'c.row_factory = sqlite3.Row',
      'out = {',
      '  "items_n": c.execute("SELECT COUNT(*) FROM watch_items").fetchone()[0],',
      '  "counts": {str(r["group_id"]): r["n"] for r in c.execute("SELECT group_id, COUNT(DISTINCT item_id) n FROM watch_item_groups GROUP BY group_id")},',
      '  "g3_order": [r[0] for r in c.execute("SELECT item_id FROM watch_item_groups WHERE group_id=3 ORDER BY sort_order, item_id")],',
      '  "jc_groups": [r[0] for r in c.execute("SELECT mg.group_id FROM watch_item_groups mg JOIN watch_groups g ON g.id=mg.group_id WHERE mg.item_id=26 ORDER BY g.sort_order, mg.sort_order, g.id")],',
      '  "total_ig_rows": c.execute("SELECT COUNT(*) FROM watch_item_groups").fetchone()[0],',
      '}',
      'c.close()',
      'print(json.dumps(out, ensure_ascii=False))',
    ].join('\n'));
    const chk = spawnSync(PY, [chkPy, isoDb], { encoding: 'utf8' });
    const d = JSON.parse((chk.stdout || '').trim());
    log('C9 DB 直查: ' + JSON.stringify(d));
    check('C9.1 终态 21 只标的', d.items_n === 21, `n=${d.items_n}`);
    check('C9.2 终态分组计数 长期潜伏=6/短期=0/极速下滑=0/正热板块=0/随便看看=15',
      (d.counts['3'] || 0) === 6 && (d.counts['4'] || 0) === 0 && (d.counts['5'] || 0) === 0
      && (d.counts['6'] || 0) === 0 && (d.counts['7'] || 0) === 15,
      JSON.stringify(d.counts));
    check('C9.3 终态金诚信仅长期潜伏（组3）', JSON.stringify(d.jc_groups) === '[3]', JSON.stringify(d.jc_groups));
    check('C9.4 终态长期潜伏组内顺序 == 初始 [26,25,20,18,5,3]',
      JSON.stringify(d.g3_order) === JSON.stringify([26, 25, 20, 18, 5, 3]), JSON.stringify(d.g3_order));
  } catch (e) { check('C9 阶段异常', false, e.message); }

  return finish(0);

  function tail(f, n = 1500) {
    try { return fs.readFileSync(f, 'utf8').slice(-n); } catch (e) { return '(no log)'; }
  }
  function finish(code) {
    return (async () => {
      try { if (browser) await browser.close(); } catch (e) { log('browser close: ' + e.message); }
      try { if (srv) { srv.kill(); } } catch (e) { log('srv kill: ' + e.message); }
      await sleep(500);
      const nP = RESULTS.filter((r) => r.status === 'PASS').length;
      const nF = RESULTS.filter((r) => r.status === 'FAIL').length;
      const nPa = RESULTS.filter((r) => r.status === 'PARTIAL').length;
      const nB = RESULTS.filter((r) => r.status === 'BLOCKED').length;
      const summary = { total: RESULTS.length, passed: nP, failed: nF, partial: nPa, blocked: nB,
                        results: RESULTS, iso_db: isoDb,
                        screenshots: ['_watch_strat.png', '_watch_multi.png', '_watch_reorder.png'],
                        elapsed_s: el() };
      fs.writeFileSync(RESULT, JSON.stringify(summary, null, 1));
      log(`=== 结论 total=${RESULTS.length} PASS=${nP} FAIL=${nF} PARTIAL=${nPa} BLOCKED=${nB} (${el()}) ===`);
      const exitCode = nF > 0 ? 1 : (nPa + nB > 0 ? 2 : 0);
      try { fs.writeFileSync(WORK + '\\ui_exit_code.txt', String(exitCode)); } catch (e) { /* ignore */ }
      process.exit(exitCode);
    })();
  }
})().catch((e) => {
  log('FATAL: ' + (e && e.stack || e));
  try { fs.writeFileSync(RESULT, JSON.stringify({ fatal: String(e && e.message), results: RESULTS }, null, 1)); } catch (e2) { /* ignore */ }
  process.exit(3);
});
