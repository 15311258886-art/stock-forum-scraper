// verify_board_flow.cjs — 板块资金流重写回归（2026-09-28）
// puppeteer-core + 已下载 chromium-1234；隔离副本库，不碰王总 live DB。
// 验证项：
//   A. /api/board_flow_status 两源连通 + 曲线来源
//   B. /api/board_flow_snapshot 返回真实排行（新浪主源）
//   C. /api/board_flow_intraday 返回曲线结构（东财/采样/库内三级兜底）
//   D. 前端切到「板块资金流」Tab，快照表有数据、来源标签诚实
//   E. 板块类型切换（行业/概念）可点且重新拉取
//   F. 自动刷新定时器已挂（FEAT.moneyflow.autoTimer）
//   G. 曲线渲染器 bfFlowCurveSvg 生成多折线 + 右侧标签（单元级）
//   H. 旧报错「未取到板块资金流排行（东财接口不可达）」不再出现
//   I. 控制台零报错
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname);
const VIEWER_DIR = path.join(PROJECT_ROOT, 'viewer');
const VENV_PY = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function findChromium() {
  const base = 'C:/Users/野比&大熊/AppData/Local/ms-playwright';
  for (const d of fs.readdirSync(base)) {
    const exe = path.join(base, d, 'chrome-win64', 'chrome.exe');
    if (fs.existsSync(exe)) return exe;
  }
  throw new Error('chromium not found');
}
function freePort() {
  return new Promise((resolve) => {
    const s = require('net').createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
function getJSON(port, ep) {
  return new Promise((resolve) => {
    http.get(`http://127.0.0.1:${port}${ep}`, r => {
      let b = ''; r.on('data', c => b += c);
      r.on('end', () => { try { resolve({ status: r.statusCode, data: JSON.parse(b) }); } catch { resolve({ status: r.statusCode, data: null }); } });
    }).on('error', () => resolve({ status: 0, data: null }));
  });
}

(async () => {
  const PORT = await freePort();
  console.log(`== 隔离端口 ${PORT} ==`);
  const iso = path.join(VIEWER_DIR, 'app_isobf.db');
  // 2026-10-05 修：WAL 库原地 copyFileSync 会拷出「索引指向不存在的表」坏副本
  // （malformed schema / disk image is malformed），首屏接口全 500、前端不渲染，
  // 于是旧版 15s 的 waitForFunction 永远等不到 #mfRefresh。改用 sqlite 在线备份。
  if (fs.existsSync(iso)) fs.unlinkSync(iso);
  require('child_process').execFileSync(VENV_PY, ['-c', `
import sqlite3, os
src = r'${path.join(VIEWER_DIR, 'app.db')}'
dst = r'${iso}'
for s in ['', '-wal', '-shm']:
    if os.path.exists(dst + s):
        try: os.remove(dst + s)
        except OSError: pass
c = sqlite3.connect(src); b = sqlite3.connect(dst)
c.backup(b); b.close(); c.close()
print('db snapshot ok')
`], { stdio: 'pipe' });

  const env = { ...process.env, VIEWER_PORT: String(PORT), VIEWER_DB: iso, PYTHONUNBUFFERED: '1' };
  const server = spawn(VENV_PY, ['-c', `import sys; sys.path.insert(0,r'${VIEWER_DIR}'); import server; srv=server.create_server('127.0.0.1', ${PORT}); srv.serve_forever()`], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', d => process.stderr.write('[server] ' + d));

  let up = false;
  for (let i = 0; i < 60; i++) {
    await sleep(200);
    try { const r = await getJSON(PORT, '/'); if (r.status === 200) { up = true; break; } } catch {}
  }
  if (!up) { console.error('server 未启动'); server.kill(); process.exit(1); }

  let pass = 0, fail = 0;
  const ok = (c, msg) => { console.log((c ? '✓' : '✗') + ' ' + msg); c ? pass++ : fail++; };

  try {
    // A
    const st = await getJSON(PORT, '/api/board_flow_status');
    ok(st.status === 200 && st.data, `A. /api/board_flow_status HTTP ${st.status}`);
    if (st.data) {
      ok(st.data.kinds && st.data.kinds.industry && st.data.kinds.industry.ok, `   行业板块源可达（${st.data.kinds.industry.source}）`);
      ok(st.data.kinds.concept && st.data.kinds.concept.ok, `   概念板块源可达（${st.data.kinds.concept.source}）`);
    }
    // B
    const snap = await getJSON(PORT, '/api/board_flow_snapshot?kind=industry&pz=10');
    ok(snap.status === 200 && snap.data && snap.data.ok === true, `B. /api/board_flow_snapshot ok=${snap.data && snap.data.ok}`);
    ok(snap.data && Array.isArray(snap.data.rows) && snap.data.rows.length >= 1, `   快照有数据（rows=${(snap.data && snap.data.rows || []).length}，来源=${snap.data && snap.data.source_label}）`);
    // C
    const intra = await getJSON(PORT, '/api/board_flow_intraday?kind=industry&top=12');
    ok(intra.status === 200 && intra.data && typeof intra.data.ok === 'boolean', `C. /api/board_flow_intraday 返回结构（ok=${intra.data && intra.data.ok}）`);
    if (intra.data) {
      const hasCurve = intra.data.series && intra.data.series.length >= 1;
      const warming = intra.data.warming_up === true;
      ok(hasCurve || intra.data.ok === false, `   曲线：series=${intra.data.series ? intra.data.series.length : 0} / warming_up=${warming} / source=${intra.data.source || '-'}`);
    }

    // D-I 前端
    const puppeteer = require('C:/Users/野比&大熊/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core');
    const browser = await puppeteer.launch({ executablePath: findChromium(), headless: 'new', args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', e => errs.push('pageerror: ' + e.message));
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(3000);
    await page.evaluate(() => { window.location.hash = '#enhance/moneyflow'; });
    await page.waitForFunction(() => document.querySelector('#p-moneyflow') && document.querySelector('#mfRefresh'), { timeout: 15000 });
    await sleep(1200);

    // D 切到板块资金流
    await page.evaluate(() => document.querySelector('[data-mf-mode="board"]').click());
    await page.waitForFunction(() => document.querySelector('#bfCurve') !== null, { timeout: 15000 }).catch(() => {});
    await sleep(2500);
    const board = await page.evaluate(() => {
      const rows = document.querySelectorAll('#p-moneyflow .wt-tb tbody tr');
      const srcTag = document.querySelector('#p-moneyflow .bf-src-tag');
      // 2026-10-05 修：别用 [data-bf-kind] 数——表格每个板块行也带这个属性（行点击要带 kind），
      // 数出来是 50 不是 2，旧断言就假失败了。pill 有独立的 .bf-kind 类，用它才准。
      const kinds = document.querySelectorAll('#p-moneyflow .bf-kind');
      const curve = document.querySelector('#bfCurve');
      const broken = document.body.innerText.includes('未取到板块资金流排行（东财接口不可达）');
      return {
        rowCount: rows.length,
        firstRowText: rows[0] ? rows[0].innerText.replace(/\s+/g, ' ').slice(0, 60) : '',
        srcTag: srcTag ? srcTag.innerText.trim() : '',
        kindCount: kinds.length,
        curveHTML: curve ? curve.innerHTML.length : 0,
        curveHasSvg: curve ? !!curve.querySelector('svg') : false,
        curveHasNote: curve ? !!curve.querySelector('.bf-note') : false,
        autoTimer: (typeof FEAT !== 'undefined' && FEAT.moneyflow && FEAT.moneyflow.autoTimer) ? 'set' : 'null',
        broken,
      };
    });
    console.log('  [DBG]', JSON.stringify(board));
    ok(board.rowCount >= 1, `D. 板块快照表有数据（${board.rowCount} 行，首行：${board.firstRowText}）`);
    ok(/新浪|东方财富/.test(board.srcTag), `   来源标签诚实展示（${board.srcTag}）`);
    ok(board.kindCount === 2, `   板块类型切换存在（行业/概念，n=${board.kindCount}）`);
    ok(board.curveHTML > 0, `   曲线区已渲染（${board.curveHasSvg ? 'SVG' : board.curveHasNote ? '积累提示' : '空'}）`);
    ok(board.autoTimer === 'set', `F. 自动刷新定时器已挂（autoTimer=${board.autoTimer}）`);
    ok(board.broken === false, `H. 旧报错「东财接口不可达」已消失`);

    // E 切到概念板块
    await page.evaluate(() => { const k = document.querySelector('[data-bf-kind="concept"]'); if (k) k.click(); });
    await sleep(2500);
    const concept = await page.evaluate(() => {
      const active = document.querySelector('#p-moneyflow [data-bf-kind="concept"].active');
      const rows = document.querySelectorAll('#p-moneyflow .wt-tb tbody tr').length;
      return { switched: !!active, rows };
    });
    ok(concept.switched, `E. 切换到「概念板块」成功（rows=${concept.rows}）`);

    // G 曲线渲染器单元级（不依赖实时数据，注入合成曲线）
    const svg = await page.evaluate(() => {
      if (typeof bfFlowCurveSvg !== 'function') return 'NO_FN';
      const axis = []; for (let h = 9; h < 15; h++) for (let m = 0; m < 60; m += 5) axis.push((h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m);
      const series = []; for (let i = 0; i < 12; i++) series.push({ name: '板块' + i, code: 'b' + i, points: axis.map((_, j) => Math.sin(j / 8 + i) * 4 + i * 0.5) });
      return bfFlowCurveSvg({ axis, series });
    });
    ok(typeof svg === 'string' && svg.includes('<svg'), `G. bfFlowCurveSvg 生成 SVG`);
    if (typeof svg === 'string') {
      const nPath = (svg.match(/<path/g) || []).length;
      const nText = (svg.match(/<text/g) || []).length;
      const nCircle = (svg.match(/<circle/g) || []).length;
      ok(nPath >= 12, `   多折线已绘制（path=${nPath}）`);
      ok(nText >= 12, `   右侧板块标签已绘制（text=${nText}）`);
      ok(nCircle >= 12, `   末端色点已绘制（circle=${nCircle}）`);
    }

    // I 控制台
    ok(errs.length === 0, `I. 控制台 0 报错（实际 ${errs.length}）`);
    if (errs.length) errs.slice(0, 5).forEach(e => console.log('  !', e));

    await page.screenshot({ path: path.join(PROJECT_ROOT, '_board_flow.png'), fullPage: false });
    console.log('SHOT: _board_flow.png');
    await browser.close();
  } catch (e) {
    console.error('TEST ERR:', e.message);
    fail++;
  } finally {
    server.kill();
  }
  console.log(`\n========== ${pass} PASS / ${fail} FAIL ==========`);
  try { fs.unlinkSync(iso); } catch {}
  process.exit(fail === 0 ? 0 : 1);
})();
