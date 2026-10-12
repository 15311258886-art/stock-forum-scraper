/* 回归验证：分支透视弹窗「可拖动 / 可最大化 / 与个股详情共存」（2026-09-27）
   需求原话（王总）：在弹窗里点开个股，右侧确实显示出来了我却看不见（被挡）；而且那个新弹出的框也没法拖。
                    同时这个弹窗自己也没法拖，信息看不全。
   根因：.modal-overlay 是 z-index:9999 的全屏遮罩，个股面板 #drawer 只有 70 →
        详情被压在遮罩下：看不见 / 点不到 / 拖不动；弹窗自身也没有拖拽实现。
   验收点：
   ① 弹窗标题栏可拖动：位置真实变化，且进入 bm-floating 自由摆放态；
   ② 「⛶」最大化铺满视口（宽≈vw-24），长名单表格 maxHeight 提到 58vh；再点还原回居中模态；
   ③ 点弹窗内个股 chip → #drawer.show，且：
      · overlay 加 bm-behind（遮罩透明、pointer-events:none）
      · 层级倒挂修正：branchModal z-index(60) < drawer(70)
      · 抽屉标题栏中心点 elementFromPoint 命中 #drawer 内元素（= 详情不再被遮罩挡住）← 核心证据
      · #drawerBody 有真实内容
   ④ 详情面板自身可拖（标题栏拖动后位置变化）—— 层级修好后鼠标事件才传得下去；
   ⑤ 点弹窗内容不误关详情；关掉详情后弹窗自动恢复模态（bm-behind 移除、z-index 回 9999）；
   ⑥ 控制台零报错。
   用法：node verify_branch_modal_drag.cjs
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8803;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT_LET = path.join(BASE, '_bm_drag_modal.png');   // 弹窗让位 + 详情浮起
const SHOT_MAX = path.join(BASE, '_bm_maxed.png');        // 弹窗最大化
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

/* 取某个元素中心点坐标（供真实鼠标操作） */
const center = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, l: r.left, t: r.top };
}, sel);

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪:\n' + srvLog.slice(-1500));

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

    /* 打开分支透视弹窗 */
    await page.evaluate(() => { document.querySelector('#p-lhb_sectors .ol-chip[data-ol-sector]').click(); });
    await page.waitForSelector('#branchModal .bm-read', { timeout: 20000 });
    ok('弹窗打开（并含解读块）', true, '');

    /* ---------- ① 弹窗拖动 ---------- */
    const r0 = await center(page, '#branchModal .branch-modal');
    const h0 = await center(page, '#branchModal .bm-head');
    await page.mouse.move(h0.x, h0.y);
    await page.mouse.down();
    await page.mouse.move(h0.x + 120, h0.y + 90, { steps: 15 });
    await page.mouse.up();
    await sleep(200);
    const r1 = await page.evaluate(() => {
      const el = document.querySelector('#branchModal .branch-modal');
      const r = el.getBoundingClientRect();
      return { l: r.left, t: r.top, floating: document.getElementById('branchModal').classList.contains('bm-floating') };
    });
    ok('弹窗标题栏可拖动（位置真实变化）', Math.abs(r1.l - r0.l) > 40 && Math.abs(r1.t - r0.t) > 20,
      `left ${Math.round(r0.l)}→${Math.round(r1.l)} / top ${Math.round(r0.t)}→${Math.round(r1.t)}`);
    ok('拖动后进入自由摆放态(bm-floating)', r1.floating === true, '');

    /* ---------- ② 最大化 / 还原 ---------- */
    await page.evaluate(() => { document.querySelector('#branchModal .bm-max').click(); });
    await sleep(250);
    const mx = await page.evaluate(() => {
      const card = document.querySelector('#branchModal .branch-modal');
      const r = card.getBoundingClientRect();
      const tw = document.querySelector('#branchModal .bm-tw');
      const btn = document.querySelector('#branchModal .bm-max');
      return {
        w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight,
        maxed: document.getElementById('branchModal').classList.contains('bm-maxed'),
        twMax: tw ? getComputedStyle(tw).maxHeight : null,
        btnOn: btn.classList.contains('on'),
      };
    });
    ok('最大化：铺满视口宽度', mx.maxed === true && Math.abs(mx.w - (mx.vw - 24)) <= 6,
      `w=${Math.round(mx.w)} 期望=${mx.vw - 24}`);
    ok('最大化：长名单表格放宽到 58vh',
      mx.twMax === null || Math.abs(parseFloat(mx.twMax) - mx.vh * 0.58) <= 3,
      `maxHeight=${mx.twMax} 期望≈${Math.round(mx.vh * 0.58)}px`);
    ok('最大化按钮进入高亮态', mx.btnOn === true, '');
    await page.screenshot({ path: SHOT_MAX, fullPage: false });

    await page.evaluate(() => { document.querySelector('#branchModal .bm-max').click(); });
    await sleep(250);
    const bk = await page.evaluate(() => {
      const b = document.getElementById('branchModal');
      const r = document.querySelector('#branchModal .branch-modal').getBoundingClientRect();
      return { maxed: b.classList.contains('bm-maxed'), floating: b.classList.contains('bm-floating'), l: r.left, w: r.width, vw: window.innerWidth };
    });
    ok('再点还原：回到居中模态', bk.maxed === false && bk.floating === false && bk.w < bk.vw - 60 && bk.l > 100,
      `w=${Math.round(bk.w)} left=${Math.round(bk.l)}`);

    /* ---------- ③ 点弹窗内个股 chip → 详情可见（核心） ---------- */
    const chip = await page.evaluate(() => {
      const c = document.querySelector('#branchModal .bm-chip[data-sym]');
      const r = c.getBoundingClientRect();
      return { sym: c.dataset.sym, name: c.textContent.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    await page.mouse.click(chip.x, chip.y);
    await page.waitForSelector('#drawer.show', { timeout: 25000 });
    await sleep(700);
    ok('点弹窗内个股 → 详情面板打开', true, `${chip.name}(${chip.sym})`);

    const vis = await page.evaluate(() => {
      const bm = document.getElementById('branchModal');
      const card = document.querySelector('#branchModal .branch-modal');
      const d = document.getElementById('drawer');
      const dr = d.getBoundingClientRect();
      // 取详情面板标题栏上的一个点，看它到底命中的是谁（被遮罩挡住的话命中的就不是抽屉）
      const px = dr.left + dr.width / 2, py = dr.top + 18;
      const hit = document.elementFromPoint(px, py);
      return {
        behind: bm.classList.contains('bm-behind'),
        zBm: getComputedStyle(bm).zIndex,
        zDrawer: getComputedStyle(d).zIndex,
        peBm: getComputedStyle(bm).pointerEvents,
        peCard: getComputedStyle(card).pointerEvents,
        bgBm: getComputedStyle(bm).backgroundColor,
        hitInDrawer: !!(hit && hit.closest('#drawer')),
        hitTag: hit ? hit.tagName + (hit.className ? '.' + String(hit.className).split(' ')[0] : '') : null,
        bodyLen: document.getElementById('drawerBody').textContent.replace(/\s+/g, '').length,
        drawerLeft: Math.round(dr.left), drawerTop: Math.round(dr.top),
      };
    });
    ok('弹窗进入让位态(bm-behind)', vis.behind === true, '');
    ok('遮罩不再拦截点击(pointer-events:none / 卡片仍可交互)',
      vis.peBm === 'none' && vis.peCard === 'auto', `overlay=${vis.peBm} card=${vis.peCard}`);
    ok('层级倒挂修正：弹窗(60) < 详情(70)', Number(vis.zBm) === 60 && Number(vis.zDrawer) === 70,
      `bm=${vis.zBm} drawer=${vis.zDrawer}`);
    ok('遮罩已转透明（不再压暗/遮挡）', /rgba\(0, 0, 0, 0\)|transparent/.test(vis.bgBm), vis.bgBm);
    ok('★ 详情面板不再被挡：标题栏命中 #drawer 内元素', vis.hitInDrawer === true,
      `命中=${vis.hitTag} 面板@(${vis.drawerLeft},${vis.drawerTop})`);
    ok('详情面板有真实内容', vis.bodyLen > 20, `正文 ${vis.bodyLen} 字`);

    /* ---------- ④ 详情面板可拖（层级修好后事件才传得下去） ---------- */
    const d0 = await center(page, '#drawer');
    const dhead = await center(page, '#drawerHead');
    await page.mouse.move(dhead.x, dhead.y);
    await page.mouse.down();
    await page.mouse.move(dhead.x - 110, dhead.y + 70, { steps: 15 });
    await page.mouse.up();
    await sleep(200);
    const d1 = await page.evaluate(() => {
      const r = document.getElementById('drawer').getBoundingClientRect();
      return { l: r.left, t: r.top };
    });
    ok('详情面板可拖动', Math.abs(d1.l - d0.l) > 40, `left ${Math.round(d0.l)}→${Math.round(d1.l)}`);

    await page.screenshot({ path: SHOT_LET, fullPage: false });

    /* ---------- ⑤ 交互不打架 + 关闭后恢复 ---------- */
    await page.evaluate(() => {
      const el = document.querySelector('#branchModal .bm-read') || document.querySelector('#branchModal .bm-body');
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    });
    await sleep(220);
    ok('点弹窗内容不误关详情面板', await page.evaluate(() => document.getElementById('drawer').classList.contains('show')), '');

    // 在弹窗里换另一只票（挑一个没被详情面板盖住的位置真实点击）
    const chip2 = await page.evaluate(() => {
      const dr = document.getElementById('drawer').getBoundingClientRect();
      for (const c of document.querySelectorAll('#branchModal .bm-chip[data-sym]')) {
        const r = c.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        if (x < dr.left - 8 && y > 4 && y < innerHeight - 4) return { sym: c.dataset.sym, x, y };
      }
      return null;
    });
    if (chip2) {
      await page.mouse.click(chip2.x, chip2.y);
      await sleep(500);
      const sw = await page.evaluate(() => ({
        show: document.getElementById('drawer').classList.contains('show'),
        title: document.getElementById('drawerTitle').textContent.trim(),
      }));
      ok('换另一只票：详情原地刷新且仍可见', sw.show === true && sw.title.length > 0, `标题=${sw.title}`);
    } else {
      ok('换另一只票：详情原地刷新且仍可见', true, 'SKIP（弹窗内无未被遮挡的 chip 位置）');
    }

    await page.evaluate(() => { document.getElementById('btnClose').click(); });
    await sleep(350);
    const after = await page.evaluate(() => {
      const bm = document.getElementById('branchModal');
      return {
        show: document.getElementById('drawer').classList.contains('show'),
        behind: bm ? bm.classList.contains('bm-behind') : null,
        z: bm ? getComputedStyle(bm).zIndex : null,
        bg: bm ? getComputedStyle(bm).backgroundColor : null,
      };
    });
    ok('关掉详情后弹窗自动恢复模态', after.show === false && after.behind === false && Number(after.z) === 9999,
      `z=${after.z} behind=${after.behind}`);
    ok('恢复后遮罩重新压暗', after.bg !== null && !/rgba\(0, 0, 0, 0\)/.test(after.bg), after.bg);

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
