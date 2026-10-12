/* verify_extref_pnl_ui.cjs —— 外部信号「T+1 / T+5 / T+10 收益」接口 + UI 回归护栏

背景（2026-09-28 王总反馈：「这里边显示的 G+1、G+5、G+10 的这个情况是没有」）：
  ① 当日可见的信号多数是 2026-09-27（周日）采集入库的，发布日非交易日 → 基准日顺延到
     其后第一个交易日（09-28），T+1 至少要等 09-29 收盘，T+5/T+10 更晚 —— 所以显示「—」
     本身是**诚实口径**（未走完窗口一律 null，不写 0），不是坏数据；
  ② 但实质缺陷是：**回填只能手动点按钮**。采集（拿数据）与回测（算收益）原本是两个独立
     动作，只点前者就永远看不到收益 —— 这才让王总觉得「这些收益根本没有」。

本护栏锁住修复后的行为：
  A 接口：详情必须带 pnl_expect（讲清为什么是「—」/何时会有）；已回填信号必须给出真实数字；
         采集接口必须顺带自动回填（返回 backtest 字段）。
  B 前端：进入「社区共识」点开信号卡 → 抽屉里三段收益标签齐全 + 说明文案在；
         已到期信号能看到带 % 的收益；进面板会挂上 6h 节流的惰性回填。
  C 控制台零报错。

跑法：node verify_extref_pnl_ui.cjs   （自带临时后端，端口 8811）
*/
const fs = require('fs');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8811;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';

function findChrome() {
  const base = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
  if (!fs.existsSync(base)) return null;
  for (const d of fs.readdirSync(base)) {
    if (!d.startsWith('chromium-')) continue;
    const p = path.join(base, d, 'chrome-win64', 'chrome.exe');
    if (fs.existsSync(p)) return p;
  }
  return null;
}

let ok = 0;
const fails = [];
function chk(cond, msg) {
  if (cond) { ok++; console.log('  PASS ' + msg); }
  else { fails.push(msg); console.log('  FAIL ' + msg); }
}

function waitReady(timeoutMs) {
  const t0 = Date.now();
  return new Promise((resolve) => {
    const tick = () => {
      const req = http.get({ host: '127.0.0.1', port: PORT, path: '/api/summary', timeout: 2000 }, (r) => {
        r.resume();
        if (r.statusCode === 200) return resolve(true);
        retry();
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); retry(); });
      function retry() {
        if (Date.now() - t0 > timeoutMs) return resolve(false);
        setTimeout(tick, 400);
      }
    };
    tick();
  });
}

async function waitFn(page, fn, timeout, label) {
  try {
    await page.waitForFunction(fn, { timeout, polling: 250 });
    return true;
  } catch (e) {
    console.log('  (等待超时：' + label + ')');
    return false;
  }
}

(async () => {
  const env = { ...process.env, VIEWER_PORT: String(PORT) };
  ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'].forEach((k) => delete env[k]);
  const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser = null;
  try {
    const ready = await waitReady(90000);
    chk(ready, '临时后端就绪（127.0.0.1:' + PORT + '）');
    if (!ready) throw new Error('backend not ready');

    const chrome = findChrome();
    chk(!!chrome, '找到本机 Chromium（' + (chrome || '未找到') + '）');
    if (!chrome) throw new Error('no chrome');

    browser = await puppeteer.launch({
      executablePath: chrome, headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });

    const api = (p) => page.evaluate((u) => fetch(u).then((r) => r.json()), p);

    console.log('== A) 接口：说明字段 + 自动回填 ==');
    const sigs = await api('/api/extref_signals');
    const stats = sigs.stats || {};
    chk(typeof stats.pnl_pending === 'number' && typeof stats.pnl_done === 'number',
      'stats 带回填计数（done=' + stats.pnl_done + ' / pending=' + stats.pnl_pending + '）');

    const pend = (sigs.signals || []).find((s) => s.pnl_status === 'pending');
    const done = (sigs.signals || []).find((s) => s.pnl_status === 'done');
    chk(!!pend, '存在待回填信号样本');
    chk(!!done, '存在已回填信号样本（自动回测生效）');

    if (pend) {
      const pd = await api('/api/extref_signal_detail?sid=' + encodeURIComponent(pend.sid));
      const ex = String(pd.pnl_expect || '');
      chk(ex.length > 10, '待回填信号详情带 pnl_expect 说明');
      chk(ex.indexOf('个交易日') >= 0, '说明含「还要走几个交易日」（实得：' + ex.slice(0, 46) + '…）');
      chk(/（周[一二三四五六日]/.test(ex), '说明点出发布日是星期几（非交易日顺延口径）');
      chk(String(pd.signal && pd.signal.pnl_t1) === 'undefined' || pd.signal.pnl_t1 === null,
        '待回填信号 pnl_t1 仍为 null（不写 0）');
    }
    if (done) {
      const dd = await api('/api/extref_signal_detail?sid=' + encodeURIComponent(done.sid));
      const ex = String(dd.pnl_expect || '');
      chk(ex.indexOf('T+1') >= 0 && ex.indexOf('%') >= 0,
        '已回填信号详情给出三段真实收益（' + ex.slice(0, 60) + '）');
      chk(dd.signal && dd.signal.pnl_t1 !== null, '已回填信号 pnl_t1 非空');
    }

    const cl = await api('/api/extref_collect?src_key=em-guba-rank');
    chk(cl.ok === true, '采集接口 ok=True');
    chk(cl.backtest && typeof cl.backtest === 'object' && 'skipped' in cl.backtest,
      '采集接口顺带自动回填（backtest=' +
      JSON.stringify({ done: (cl.backtest || {}).done, skipped: (cl.backtest || {}).skipped }) + '）');
    chk((cl.backtest || {}).na === 0 || (cl.backtest || {}).na === undefined,
      '本轮回填没有误标 na（窗口未走完的不会被踢出队列）');

    console.log('== B) 前端：进面板 → 点信号卡 → 抽屉 ===');
    await page.waitForSelector('.nav-item[data-cat="extref"]', { timeout: 30000 });
    await page.click('.nav-item[data-cat="extref"]');
    await new Promise((r) => setTimeout(r, 800));
    await page.waitForSelector('.sub-tab[data-tab="extref_comm"]', { timeout: 20000 });
    await page.click('.sub-tab[data-tab="extref_comm"]');
    const gotCards = await waitFn(page, () => document.querySelectorAll('.ex-card.ex-click[data-ex-sid]').length > 0,
      25000, '社区共识信号卡');
    const nCards = await page.evaluate(() => document.querySelectorAll('.ex-card.ex-click[data-ex-sid]').length);
    chk(gotCards && nCards > 0, '社区共识面板渲染出信号卡（' + nCards + ' 张）');

    await page.click('.ex-card.ex-click[data-ex-sid]');
    const drawerOpen = await waitFn(page, () => {
      const d = document.querySelector('#drawer');
      return d && d.classList.contains('show') && (document.querySelector('#drawerBody') || {}).textContent;
    }, 20000, '详情抽屉');
    chk(drawerOpen, '点信号卡打开详情抽屉');

    const body = await page.evaluate(() => (document.querySelector('#drawerBody') || {}).textContent || '');
    chk(body.indexOf('事后收益') >= 0, '抽屉含「② 事后收益」段');
    chk(body.indexOf('T+1 收益') >= 0 && body.indexOf('T+5 收益') >= 0 && body.indexOf('T+10 收益') >= 0,
      'T+1 / T+5 / T+10 三行标签齐全');
    chk(body.indexOf('个交易日') >= 0, '抽屉里显示「什么时候会有」的说明（pnl_expect 落地）');

    // 该卡若是待回填信号 → 数值应是「—」（诚实口径）；若是已到期 → 应带 %
    const firstIsPend = await page.evaluate(() => {
      const it = [...document.querySelectorAll('#drawerBody .si-item')]
        .find((x) => (x.querySelector('span') || {}).textContent === 'T+1 收益');
      return it ? (it.querySelector('b') || {}).textContent : null;
    });
    chk(firstIsPend === '—' || /%$/.test(String(firstIsPend)),
      '第一张卡 T+1 显示为「—」（未到期）或带 %（已回填），实得：' + firstIsPend);

    if (done) {
      await page.evaluate((sid) => openExtrefSignal(sid), done.sid);
      const doneShown = await waitFn(page, () => {
        const b = (document.querySelector('#drawerBody') || {}).textContent || '';
        return b.indexOf('%') >= 0 && b.indexOf('T+10 收益') >= 0;
      }, 20000, '已到期信号收益');
      const db = await page.evaluate(() => (document.querySelector('#drawerBody') || {}).textContent || '');
      chk(doneShown, '已到期信号能看到真实收益（含 % · ' + (db.match(/[+-]?\d+\.\d+%/g) || []).slice(0, 3).join(' / ') + '）');
    }

    const lsOk = await page.evaluate(() => !!localStorage.getItem('extref.autoBackfillAt'));
    chk(lsOk, '进面板已挂上 6h 节流的惰性回填（localStorage 时间戳已写）');

    console.log('== C) 控制台 ==');
    chk(errs.length === 0, '控制台零报错' + (errs.length ? '（' + errs.slice(0, 2).join(' | ') + '）' : ''));
  } catch (e) {
    chk(false, '运行异常：' + (e && e.message));
  } finally {
    if (browser) { try { await browser.close(); } catch (e) { /* ignore */ } }
    try { srv.kill(); } catch (e) { /* ignore */ }
  }
  console.log('\nPASS=' + ok + '  FAIL=' + fails.length);
  fails.forEach((f) => console.log('  !! ' + f));
  process.exit(fails.length ? 1 : 0);
})();
