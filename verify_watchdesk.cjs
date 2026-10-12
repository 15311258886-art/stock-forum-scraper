/* 回归验证：主动盯盘 / 个股预判面板（2026-10-01）
   需求（王总原话）：「个股快讯不应该是你自动查的吗…… 然后预判风险和增长机会，这个应该是你来
   预判的呀…… 总结完之后，应该给出相对说比较清晰明确的这种市场预判。」

   已确认口径：博主池 199 + 自选 5 全扫 · 交易时段每 15 分钟一轮 · 结构化结论 + 触发条件。

   隔离：复制生产库（app.db 三件套）到临时目录，VIEWER_DB 指向副本、VIEWER_PORT=8806；
   本脚本只读，不写生产数据。

   用法：node verify_watchdesk.cjs   （截图 _watchdesk_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8806;
const PY = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\.venv\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_watchdesk_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;

// —— 隔离副本库 ——
const ISO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'vw_desk_iso_'));
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

(async () => {
  try {
    for (let i = 0; i < 80; i++) {
      try { const r = await fetch(ROOT + '/api/summary'); if (r.status === 200) break; } catch (e) { /* retry */ }
      await sleep(500);
    }

    console.log('\n=== 1. 接口层（隔离副本） ===');
    const dw = await (await fetch(ROOT + '/api/watchdesk')).json();
    console.log('  keys =', Object.keys(dw), ' stats =', JSON.stringify(dw.stats));
    ok(dw.ok === true, '/api/watchdesk 返回正常');
    ok(Array.isArray(dw.rows) && dw.rows.length >= 100, `盯盘池返回 ${dw.rows ? dw.rows.length : 0} 只`);
    const badName = (dw.rows || []).filter(r => !r.name || String(r.name).length < 2);
    ok(badName.length === 0, `全部有中文名称（缺失 ${badName.length} 只）`);
    const st = dw.stats || {};
    ok(!!st.last_scan_at, `有「上次扫描」时间（${st.last_scan_at}）`);
    ok(typeof st.running === 'boolean', `running 字段可读（${st.running}）`);
    ok(!!st.next_scan_at, `有「下次扫描」时间（${st.next_scan_at}）`);
    const verdicts = (dw.rows || []).filter(r => r.verdict && String(r.verdict).length > 20);
    ok(verdicts.length === (dw.rows || []).length, `每只都有一句明确结论（${verdicts.length}/${(dw.rows || []).length}）`);

    const first = (dw.rows || [])[0] || {};
    const det = await (await fetch(ROOT + '/api/watchdesk/detail?code=' + encodeURIComponent(first.code))).json();
    console.log(`  detail ${det.code} ${det.name} | ${det.stance}`);
    ok(det.ok === true, '/api/watchdesk/detail 路由不报错');
    ok(Array.isArray(det.risks) && Array.isArray(det.opps) && Array.isArray(det.triggers),
       '风险 / 机会 / 触发点三类字段齐全');
    ok(Array.isArray(det.anns), `公告流水 ${(det.anns || []).length} 条`);
    ok(!!det.sentence, `财务佐证句：${(det.sentence || '').slice(0, 40)}`);

    console.log('\n=== 2. 前端层（无头 Chromium） ===');
    const browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'],
      defaultViewport: { width: 1440, height: 1000 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
    page.on('response', r => { if (r.status() >= 400) errs.push(`HTTP ${r.status()} ${r.url()}`); });

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#sidenav .nav-item', { timeout: 20000 });
    await sleep(800);
    await page.click('#sidenav .nav-item[data-cat="info"]');
    await sleep(1000);
    const subSel = '#subnav .sub-tab[data-tab="news"]';
    await page.waitForSelector(subSel, { timeout: 8000 });
    await page.click(subSel);
    await page.waitForSelector('#p-news .desk-tb tbody tr', { timeout: 15000 });
    await sleep(1200);

    const panel = await page.$eval('#p-news', el => ({
      rows: el.querySelectorAll('.desk-tb tbody tr').length,
      hasDetail: !!el.querySelector('.pol-judge'),
      hasVerdict: !!el.querySelector('.desk-verdict'),
      hasRiskList: !!el.querySelector('.desk-list.desk-r'),
      hasOppList: !!el.querySelector('.desk-list.desk-o'),
      hasTrig: !!el.querySelector('.desk-trig'),
      judgeText: (el.querySelector('.pol-jh') || {}).innerText || '',
      header: (el.querySelector('.wt-head .hint') || {}).innerText || '',
    }));
    console.log('  panel =', JSON.stringify(panel, null, 2));
    ok(panel.rows >= 50, `盯盘表渲染 ${panel.rows} 行`);
    ok(panel.hasDetail && panel.hasVerdict, '结论卡渲染（立场 + 结论段）');
    ok(panel.hasRiskList && panel.hasOppList, '风险清单 / 机会清单双列渲染');
    ok(panel.hasTrig, '改判触发点渲染');
    ok(/自动盯盘 \d+ 只/.test(panel.header), `面板头标注自动盯盘：${panel.header.replace(/\s+/g, ' ').slice(0, 70)}`);

    // —— 排版溢出自查（王总用办公软件逐页看，这里按 DOM 量化）——
    const overflow = await page.evaluate(() => {
      const host = document.querySelector('#p-news');
      if (!host) return { err: 'no panel' };
      const hostRect = host.getBoundingClientRect();
      // 祖先（或自身）可横滚 → 超出属于正常滚动，不算排版缺陷
      const scrollable = (el) => {
        let p = el;
        while (p && p !== document.documentElement) {
          const cs = getComputedStyle(p);
          if (/auto|scroll/.test(cs.overflowX) || /auto|scroll/.test(cs.overflow)) return true;
          p = p.parentElement;
        }
        return false;
      };
      const bad = [];
      host.querySelectorAll('*').forEach(el => {
        if (scrollable(el)) return;
        const cs = getComputedStyle(el);
        if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
          bad.push({ cls: (el.className || '').toString().slice(0, 40), tag: el.tagName, sw: el.scrollWidth, cw: el.clientWidth });
        }
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > hostRect.right + 2) {
          bad.push({ cls: (el.className || '').toString().slice(0, 40), tag: el.tagName, over: Math.round(r.right - hostRect.right) });
        }
      });
      // 表格仍需横滚多少（列宽优化的效果度量）
      const tb = host.querySelector('.desk-tb');
      const sc = tb && tb.closest('.wt-tw');
      const scrollNeed = (tb && sc) ? Math.max(0, tb.scrollWidth - sc.clientWidth) : 0;
      return { bad: bad.slice(0, 12), total: bad.length, scrollNeed };
    });
    console.log('  overflow =', JSON.stringify(overflow));
    ok(overflow.total === 0, `面板内无真溢出 / 越界元素（${overflow.total} 处；表格横滚余量 ${overflow.scrollNeed}px）`);
    ok(overflow.scrollNeed <= 120, `盯盘表列宽已收敛（横滚余量 ${overflow.scrollNeed}px ≤ 120px）`);

    // 0 分清单要给空态文案，不能整块空白
    const emptyN = await page.$$eval('#p-news .desk-empty', els => els.length);
    ok(emptyN >= 1, `空态提示渲染（${emptyN} 处：0 分清单不再留白）`);

    // —— 点一行切换个股 ——
    const before = panel.judgeText;
    const rows = await page.$$('#p-news .desk-tb tbody tr');
    if (rows.length > 3) {
      await rows[3].click();
      await sleep(1200);
      const after = await page.$eval('#p-news .pol-jh', el => el.innerText);
      ok(after !== before, `点列表第 4 行可切换个股（切换后：${after.replace(/\s+/g, ' ').slice(0, 50)}）`);
    }

    // 结论卡区域单独截一张（列表较长，首屏截不到双列研判区）
    await page.$eval('#p-news .pol-judge', el => el.scrollIntoView({ block: 'start' }));
    await sleep(900);
    const SHOT2 = path.join(BASE, '_watchdesk_detail.png');
    await page.screenshot({ path: SHOT2, fullPage: false });
    console.log('  SHOT2 =', SHOT2);

    await page.$eval('#p-news .wt-tw', el => el.scrollIntoView({ block: 'start' }));
    await sleep(600);
    await page.screenshot({ path: SHOT, fullPage: false });
    console.log('  SHOT =', SHOT);
    ok(errs.length === 0, `前端无控制台错误 / 无 4xx（${errs.length} 条）`);
    if (errs.length) console.log('  errs =', JSON.stringify(errs.slice(0, 6), null, 2));

    await browser.close();
  } catch (e) {
    console.log('EXCEPTION', e && e.stack || e);
    fails.push('脚本异常：' + (e && e.message));
  } finally {
    try { srv.kill(); } catch (e) { /* ignore */ }
  }
  console.log('\n=== 结论 ===');
  console.log(fails.length ? 'FAIL(' + fails.length + '):\n  - ' + fails.join('\n  - ') : 'ALL PASS');
  if (srvLog.trim()) console.log('\n--- server log ---\n' + srvLog.trim().split('\n').slice(-12).join('\n'));
  process.exit(fails.length ? 1 : 0);
})();
