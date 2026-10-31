/* verify_lowbox.cjs —— 板块二「长期低位震荡」全链路 UI 实测（2026-10-13）
   模拟真实用户操作：导航 → 填代码 → 扫描 → 表格断言 → 点个股开抽屉 → 入池弹窗 →
   质量档重扫（缓存命中应快）→ 突破雷达 → 雷达点个股。全程限时轮询，不无限等待。
   复用已在跑的 8765 后端（本脚本不自己起服务）。 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const BASE = 'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper';
const CHROMIUM_DIR = 'C:/Users/野比&大熊/AppData/Local/ms-playwright';
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';

function pickChrome() {
  try {
    const dirs = fs.readdirSync(CHROMIUM_DIR).filter(d => d.startsWith('chromium-')).sort().reverse();
    for (const d of dirs) {
      const p = path.join(CHROMIUM_DIR, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(p)) return p;
      const p2 = path.join(CHROMIUM_DIR, d, 'chrome-win', 'chrome.exe');
      if (fs.existsSync(p2)) return p2;
    }
  } catch (e) {}
  return EDGE;
}

const fails = [];
const ok = (cond, msg) => { console.log((cond ? 'PASS' : 'FAIL') + ' ' + msg); if (!cond) fails.push(msg); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const consoleErrors = [];
  const pageErrors = [];
  const browser = await puppeteer.launch({
    executablePath: pickChrome(), headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1680, height: 1050 });
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
  page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 200)));

  try {
    await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForFunction(() => document.querySelector('#sidenav') && document.querySelectorAll('#sidenav *').length > 3, { timeout: 30000 });
    ok(true, '应用加载（sidenav 渲染）');

    // —— 1) 导航到 长期低位 ——
    await page.evaluate(() => { location.hash = '#watch/lowbox'; });
    await sleep(1800);
    const ctl = await page.evaluate(() => ({
      panel: !!document.querySelector('#p-lowbox'),
      codes: !!document.querySelector('#lbCodes'),
      box: !!document.querySelector('#lbBox'),
      low: !!document.querySelector('#lbLow'),
      q: !!document.querySelector('#lbQ'),
      ws: !!document.querySelector('#lbWS'),
      wf: !!document.querySelector('#lbWF'),
      ww: !!document.querySelector('#lbWW'),
      go: !!document.querySelector('#lbGo'),
    }));
    ok(ctl.panel, '#p-lowbox 面板容器存在（动态 ensurePanel）');
    ok(ctl.codes && ctl.box && ctl.low && ctl.q, '控制条：股票池/箱体时长/低位阈值/质量档 控件齐全');
    ok(ctl.ws && ctl.wf && ctl.ww && ctl.go, '权重三输入 + 扫描按钮齐全');

    // —— 2) 填代码 → 扫描（限时 240s 轮询）——
    await page.evaluate(() => { const el = document.querySelector('#lbCodes'); if (el) { el.value = '600519,002423'; } });
    await page.click('#lbGo');
    let scanned = null;
    for (let i = 0; i < 120; i++) {
      await sleep(2000);
      scanned = await page.evaluate(() => {
        const tb = document.querySelectorAll('#p-lowbox .lowbox-tb tbody tr');
        const empty = document.querySelector('#p-lowbox .wt-empty');
        const foot = document.querySelector('#p-lowbox .wt-foot');
        return { rows: tb.length, empty: empty ? empty.textContent.trim().slice(0, 120) : null, foot: foot ? foot.textContent.trim() : null, loading: !!(document.querySelector('#lbGo') && document.querySelector('#lbGo').disabled) };
      });
      if (scanned.rows > 0 || (scanned.empty && !scanned.loading)) break;
    }
    ok(scanned && scanned.rows > 0, `扫描出结果表（rows=${scanned && scanned.rows}，empty=${scanned && scanned.empty}）`);
    if (scanned && scanned.foot) console.log('   INFO 汇总行: ' + scanned.foot.slice(0, 160));

    // 表格断言
    const tbl = await page.evaluate(() => {
      const ths = [...document.querySelectorAll('#p-lowbox .lowbox-tb thead th')].map(t => t.textContent.trim());
      const first = document.querySelector('#p-lowbox .lowbox-tb tbody tr');
      return {
        ths,
        nOpen: document.querySelectorAll('#p-lowbox .lowbox-tb [data-wt-open]').length,
        nAdd: document.querySelectorAll('#p-lowbox .lowbox-tb [data-lb-add]').length,
        firstCode: first ? (first.querySelector('[data-wt-open]') || {}).dataset : null,
        hasNaN: /NaN|undefined|\[object/.test((document.querySelector('#p-lowbox') || {}).textContent || ''),
      };
    });
    ok(tbl.ths.length >= 20, `结果表列数 ${tbl.ths.length}（≥20，含箱体/基本面/国资/突破全维度）`);
    ok(tbl.nOpen > 0, `个股入口 data-wt-open 已绑 ${tbl.nOpen} 处`);
    ok(tbl.nAdd > 0, `入池按钮 ${tbl.nAdd} 处`);
    ok(!tbl.hasNaN, '无 NaN/undefined/[object 脏串');

    // —— 3) 点个股开详情抽屉 ——
    await page.click('#p-lowbox .lowbox-tb [data-wt-open]');
    let drawer = null;
    for (let i = 0; i < 30; i++) {
      await sleep(500);
      drawer = await page.evaluate(() => ({
        show: !!(document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show')),
        title: (document.querySelector('#drawerTitle') || {}).textContent || '',
        len: ((document.querySelector('#drawerBody') || {}).textContent || '').length,
      }));
      if (drawer.show && drawer.len > 50) break;
    }
    ok(drawer && drawer.show && drawer.len > 50, `点个股 → 详情抽屉打开（title=${drawer && drawer.title}，正文${drawer && drawer.len}字）`);
    await page.keyboard.press('Escape');
    await sleep(600);

    // —— 4) 入池弹窗 ——
    await page.click('#p-lowbox .lowbox-tb [data-lb-add]');
    await sleep(800);
    const modal = await page.evaluate(() => {
      const ov = document.querySelector('.modal-overlay .modal-card[aria-label="加入自选"]');
      return { open: !!ov, code: ov ? (ov.textContent.match(/\d{6}/) || [''])[0] : '' };
    });
    ok(modal.open, `点「入池」→ 加入自选弹窗打开（code=${modal.code}）`);
    await page.evaluate(() => { const b = document.querySelector('.modal-overlay .modal-card[aria-label="加入自选"] .btn'); if (b) b.click(); });
    await sleep(500);

    // —— 5) 质量档重扫（缓存命中应快，≤90s）——
    const t0 = Date.now();
    await page.evaluate(() => { const q = document.querySelector('#lbQ'); if (q) q.value = 'loose'; });
    await page.click('#lbGo');
    let rescan = null;
    for (let i = 0; i < 45; i++) {
      await sleep(2000);
      rescan = await page.evaluate(() => {
        const tb = document.querySelectorAll('#p-lowbox .lowbox-tb tbody tr');
        const go = document.querySelector('#lbGo');
        return { rows: tb.length, loading: !!(go && go.disabled) };
      });
      if (rescan.rows > 0 && !rescan.loading) break;
    }
    const dt = Math.round((Date.now() - t0) / 1000);
    ok(rescan && rescan.rows > 0 && !rescan.loading, `质量档 loose 重扫成功（rows=${rescan && rescan.rows}，耗时 ${dt}s）`);

    await page.screenshot({ path: path.join(BASE, 'artifacts', '_lowbox_panel.png') });

    // —— 6) 突破雷达 ——
    await page.evaluate(() => { location.hash = '#watch/lowbox_radar'; });
    await sleep(1500);
    let radar = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      radar = await page.evaluate(() => ({
        rows: document.querySelectorAll('#p-lowbox_radar .lowbox-radar-tb tbody tr').length,
        empty: (document.querySelector('#p-lowbox_radar .wt-empty') || {}).textContent || null,
        loading: !!(document.querySelector('#lbRadarGo') && document.querySelector('#lbRadarGo').disabled),
      }));
      if ((radar.rows > 0 && !radar.loading) || (radar.empty && !radar.loading)) break;
    }
    ok(true, `雷达首载（rows=${radar && radar.rows}，empty=${radar && radar.empty && radar.empty.trim().slice(0, 60)}）`);
    // 点「重新评估」强制复评
    if (radar && !radar.loading) {
      await page.click('#lbRadarGo').catch(() => {});
      for (let i = 0; i < 40; i++) {
        await sleep(1000);
        radar = await page.evaluate(() => ({
          rows: document.querySelectorAll('#p-lowbox_radar .lowbox-radar-tb tbody tr').length,
          loading: !!(document.querySelector('#lbRadarGo') && document.querySelector('#lbRadarGo').disabled),
        }));
        if (radar.rows > 0 && !radar.loading) break;
      }
    }
    ok(radar && radar.rows > 0 && !radar.loading, `重新评估出池表（rows=${radar && radar.rows}）`);
    const radarTbl = await page.evaluate(() => ({
      nOpen: document.querySelectorAll('#p-lowbox_radar .lowbox-radar-tb [data-wt-open]').length,
      hasNaN: /NaN|undefined|\[object/.test((document.querySelector('#p-lowbox_radar') || {}).textContent || ''),
      foot: (document.querySelector('#p-lowbox_radar .wt-foot') || {}).textContent || '',
    }));
    ok(radarTbl.nOpen > 0, `雷达表个股入口 ${radarTbl.nOpen} 处`);
    ok(!radarTbl.hasNaN, '雷达表无脏串');
    if (radarTbl.foot) console.log('   INFO 雷达汇总: ' + radarTbl.foot.trim().slice(0, 160));
    await page.screenshot({ path: path.join(BASE, 'artifacts', '_lowbox_radar.png') });

    // —— 7) 雷达点个股 ——
    if (radarTbl.nOpen > 0) {
      await page.click('#p-lowbox_radar .lowbox-radar-tb [data-wt-open]');
      let d2 = null;
      for (let i = 0; i < 30; i++) {
        await sleep(500);
        d2 = await page.evaluate(() => ({
          show: !!(document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show')),
          len: ((document.querySelector('#drawerBody') || {}).textContent || '').length,
        }));
        if (d2.show && d2.len > 50) break;
      }
      ok(d2 && d2.show && d2.len > 50, '雷达点个股 → 详情抽屉打开');
      await page.keyboard.press('Escape');
    }

    // —— 8) 控制台 ——
    const realErr = consoleErrors.filter(t => !/favicon|net::ERR_FAILED.*favicon/i.test(t));
    ok(realErr.length === 0, `控制台零报错（${realErr.length} 条${realErr.length ? '：' + realErr[0] : ''}）`);
    ok(pageErrors.length === 0, `零页面异常（${pageErrors.length} 条${pageErrors.length ? '：' + pageErrors[0] : ''}）`);
  } catch (e) {
    ok(false, '脚本异常: ' + String(e).slice(0, 300));
  }

  await browser.close();
  console.log('\n==== verify_lowbox 结果: ' + (fails.length ? 'FAIL (' + fails.length + ' 项)' : 'ALL PASS') + ' ====');
  process.exit(fails.length ? 1 : 0);
})();
