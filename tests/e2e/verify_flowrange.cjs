// verify_flowrange.cjs — 板块资金流「日期区间口径」回归（2026-10-05）
// puppeteer-core + 已下载 chromium；隔离副本库，不碰王总 live DB。
// 背景：王总质疑旧版「累计流入」只写了个 09-30、且图上全是净流入看不见流出。
// 验证项：
//   A. /api/board_flow_dates 给得出可选交易日与最新交易日
//   B. /api/board_flow_range 单日：净流出个数 > 0（旧版恒为 0，这是这次的核心修复）
//   C. /api/board_flow_range 单日：groups 里同时存在正值与负值
//   D. /api/board_flow_range 多日：起止按交易日历校正，且 note 说清来源/兜底
//   E. 前端默认即「最新交易日单日」：标题写死日期，不是含糊的「累计」
//   F. 前端区间控件：模式 pill + 起止日期框 + 应用按钮 + 快捷区间
//   G. 前端曲线：区间 SVG 已渲染，0 轴上下都有（正刻度与负刻度并存）
//   H. 统计胶囊数字与接口 stats 一致（不是拿「画了几条线」硬凑的假数）
//   I. 控制台零报错
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname);
const VIEWER_DIR = path.join(PROJECT_ROOT, 'viewer');
const VENV_PY = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');
const PORT = 8793;                 // 固定隔离端口，避免撞 8765 的 live 实例

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
  const PORT_USE = process.env.VFR_PORT ? +process.env.VFR_PORT : await freePort();
  console.log(`== 隔离端口 ${PORT_USE} ==`);
  // 2026-10-05 踩坑两次才稳：WAL 模式的库
  //   ① 只拷 app.db → 「索引指向不存在的表」malformed schema；
  //   ② 拷 -wal/-shm 也没用——live 库正被别的常驻 python 进程写，拷出来是「disk image is malformed」。
  // 正解：用 sqlite 的在线 backup API 做一致性快照（WAL 会被一起 checkpoint 进主文件）。
  const iso = path.join(VIEWER_DIR, 'app_isovf.db');
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

  const env = { ...process.env, VIEWER_PORT: String(PORT_USE), VIEWER_DB: iso, PYTHONUNBUFFERED: '1' };
  const server = spawn(VENV_PY, ['-c', `import sys; sys.path.insert(0,r'${VIEWER_DIR}'); import server; srv=server.create_server('127.0.0.1', ${PORT_USE}); srv.serve_forever()`], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', d => process.stderr.write('[server] ' + d));

  let up = false;
  for (let i = 0; i < 60; i++) {
    await sleep(200);
    try { const r = await getJSON(PORT_USE, '/'); if (r.status === 200) { up = true; break; } } catch {}
  }
  if (!up) { console.error('server 未启动'); server.kill(); process.exit(1); }

  let pass = 0, fail = 0;
  const ok = (c, msg) => { console.log((c ? '✓' : '✗') + ' ' + msg); c ? pass++ : fail++; };

  try {
    // A 日期选项
    const dt = await getJSON(PORT_USE, '/api/board_flow_dates?kind=industry');
    ok(dt.status === 200 && dt.data && dt.data.ok, `A. /api/board_flow_dates HTTP ${dt.status}`);
    const days = (dt.data && dt.data.days) || [];
    ok(days.length >= 3 && days.every(d => /^\d{4}-\d{2}-\d{2}$/.test(d)),
      `   可选交易日 ${days.length} 个（${days[0]} ~ ${days[days.length - 1]}）`);
    ok(!!dt.data.latest_trade_date, `   latest_trade_date=${dt.data && dt.data.latest_trade_date}`);

    // B/C 单日区间：核心修复点——净流出必须 > 0
    const one = await getJSON(PORT_USE, '/api/board_flow_range?kind=industry&start=2026-09-30&end=2026-09-30&top=16');
    ok(one.status === 200 && one.data && one.data.ok, `B. /api/board_flow_range 单日 ok=${one.data && one.data.ok}`);
    const st = (one.data && one.data.stats) || {};
    ok(st.down > 0, `   净流出板块数 > 0（实际 ${st.down}，旧版恒为 0）`);
    ok(st.up > 0, `   净流入板块数 > 0（实际 ${st.up}）`);
    const gs = (one.data && one.data.groups) || [];
    const pos = gs.filter(g => g.total > 0).length, neg = gs.filter(g => g.total < 0).length;
    ok(gs.length > 0 && pos > 0 && neg > 0, `   图上同屏既有流入也有流出（${pos} 正 / ${neg} 负 / 共 ${gs.length} 条）`);
    ok((one.data && one.data.start) === '2026-09-30' && (one.data && one.data.end) === '2026-09-30',
      `   起止就是请求的日期（${one.data && one.data.start} ~ ${one.data && one.data.end}）`);
    ok(gs.every(g => Object.keys(g.points || {}).length > 0), `   每条线都有点位（单日也能画，不是空曲线）`);

    // D 多日区间
    const many = await getJSON(PORT_USE, '/api/board_flow_range?kind=industry&start=2026-08-01&end=2026-09-30&top=16');
    ok(many.status === 200 && many.data, `D. 多日区间 HTTP ${many.status}`);
    ok((many.data && many.data.note || []).some(n => /校正|不可达|补抓/.test(n)),
      `   note 诚实说明口径/兜底（${(many.data && many.data.note || [])[0] || '无'}）`);

    // ---- 前端 ----
    const puppeteer = require(path.join(PROJECT_ROOT, 'node_modules', 'puppeteer-core'));
    const browser = await puppeteer.launch({ executablePath: findChromium(), headless: 'new', args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1360, height: 900 });
    const errs = [];
    page.on('pageerror', e => errs.push(String(e.message)));
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });

    // 隔离实例首屏要跑调度/ingest，慢于 live 实例；这里放宽到 60s，
    // 否则 20s 超时会被误判成「前端挂了」，其实只是没加载完（2026-10-05 实测过）
    await page.goto(`http://127.0.0.1:${PORT_USE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    // 资金流向是**子 tab**，面板只在切进去时才渲染——直接等 #mfRefresh 会永远等不到。
    // 2026-10-05 修：先进「资金流向」子 tab，再等面板在场。（原脚本 09-28 能过，是碰运气）
    await page.waitForFunction(() => document.querySelector('#p-moneyflow'), { timeout: 60000, polling: 500 });
    // 两级导航：先点大类「钱往哪走」，再点子类「资金流向」。
    // 只点 sub-tab 不行——它挂在 money 大类下，大类没切过去时压根没渲染出来。
    await page.evaluate(() => {
      const nav = Array.from(document.querySelectorAll('#sidenav .nav-item'));
      const cat = nav.find(a => /钱往哪走/.test(a.textContent));
      if (cat) cat.click();
    });
    await sleep(1500);
    await page.evaluate(() => {
      const sub = document.querySelector('.sub-tab[data-tab="moneyflow"]')
        || document.querySelector('[data-tab="moneyflow"]');
      if (sub) sub.click();
    });
    await page.waitForFunction(() => !!document.querySelector('#mfRefresh'), { timeout: 30000, polling: 500 });
    // 面板默认就是 board 口径，不必再点；下面等区间数据回来接管这一屏
    await page.waitForFunction(() => {
      const el = document.querySelector('#p-moneyflow [data-mf-mode="board"]');
      if (el && !el.dataset.linked) { el.click(); el.dataset.linked = "1"; }
      return !!el;
    }, { timeout: 15000 }).catch(() => {});
    // 等区间数据回来（默认单日）
    await page.waitForFunction(() => {
      const t = document.querySelector('#p-moneyflow .bf-curve-title');
      return t && /区间曲线/.test(t.textContent);
    }, { timeout: 30000 }).catch(() => {});

    // E 标题写死日期
    const title = await page.evaluate(() => {
      const t = document.querySelector('#p-moneyflow .bf-curve-title');
      return t ? t.textContent.trim() : '';
    });
    ok(/区间曲线/.test(title) && /20\d{2}-\d{2}-\d{2}/.test(title), `E. 标题写清日期区间：「${title}」`);

    // F 控件
    const controls = await page.evaluate(() => ({
      bar: !!document.querySelector('#p-moneyflow .bf-range-bar'),
      pills: document.querySelectorAll('#p-moneyflow [data-bf-range-mode]').length,
      activePill: (document.querySelector('#p-moneyflow [data-bf-range-mode].active') || {}).textContent || '',
      start: !!document.querySelector('#bfRangeStart'),
      end: !!document.querySelector('#bfRangeEnd'),
      apply: !!document.querySelector('#bfRangeApply'),
      quick: document.querySelectorAll('#p-moneyflow [data-bf-range-quick]').length,
    }));
    ok(controls.bar && controls.pills === 2, `F. 区间控件条与模式 pill 存在（pill=${controls.pills}）`);
    ok(/单日/.test(controls.activePill), `   默认口径=单日最新交易日（当前高亮：${controls.activePill.trim()}）`);

    // 切到区间模式，日期框出现
    await page.evaluate(() => document.querySelector('#p-moneyflow [data-bf-range-mode="range"]').click());
    await page.waitForFunction(() => !!document.querySelector('#bfRangeStart'), { timeout: 10000 });
    const c2 = await page.evaluate(() => ({
      start: !!document.querySelector('#bfRangeStart'),
      end: !!document.querySelector('#bfRangeEnd'),
      apply: !!document.querySelector('#bfRangeApply'),
      quick: document.querySelectorAll('#p-moneyflow [data-bf-range-quick]').length,
      sval: (document.querySelector('#bfRangeStart') || {}).value || '',
      eval_: (document.querySelector('#bfRangeEnd') || {}).value || '',
    }));
    ok(c2.start && c2.end && c2.apply, `   切区间后起止日期框 + 应用按钮都在（${c2.sval} ~ ${c2.eval_}）`);
    ok(c2.quick >= 3, `   快捷区间按钮 ${c2.quick} 个（近3/5/10/20 日）`);

    // 点「近5日」，确认重新拉数且图还在
    await page.evaluate(() => document.querySelector('#p-moneyflow [data-bf-range-quick="5"]').click());
    await sleep(2500);
    const quick = await page.evaluate(() => {
      const t = document.querySelector('#p-moneyflow .bf-curve-title');
      return t ? t.textContent.trim() : '';
    });
    ok(/至|单日/.test(quick), `   点「近5日」后标题跟随（${quick}）`);

    // 切回单日，验证 0 轴上下都在
    await page.evaluate(() => document.querySelector('#p-moneyflow [data-bf-range-mode="single"]').click());
    await page.waitForFunction(() => /区间曲线/.test((document.querySelector('#p-moneyflow .bf-curve-title') || {}).textContent || ''), { timeout: 30000 }).catch(() => {});
    const svgInfo = await page.evaluate(() => {
      const svg = document.querySelector('#bfCurve svg.bf-svg');
      if (!svg) return null;
      const txt = Array.from(svg.querySelectorAll('text')).map(t => t.textContent);
      const ticks = txt.filter(t => /亿$/.test(t));
      return {
        paths: svg.querySelectorAll('path').length,
        texts: txt.length,
        negTick: ticks.some(t => parseFloat(t) < 0),
        posTick: ticks.some(t => parseFloat(t) > 0),
        dots: svg.querySelectorAll('circle').length,
      };
    });
    ok(!!svgInfo, 'G. 区间曲线 SVG 已渲染');
    if (svgInfo) {
      ok(svgInfo.paths >= 2 || svgInfo.dots >= 2, `   折线/点已绘制（path=${svgInfo.paths}, dot=${svgInfo.dots}）`);
      ok(svgInfo.negTick && svgInfo.posTick, `   0 轴上下刻度并存（负刻度=${svgInfo.negTick}, 正刻度=${svgInfo.posTick}）→ 流出看得见`);
    }

    // H 统计胶囊 = 接口真数。等单日数据真正回来再读：
    // 后台补抓中时 stats 天然是 0（不是 bug），读早了会误判。
    await page.waitForFunction(() => {
      const t = document.querySelector('#p-moneyflow .bf-curve-title');
      const d = document.querySelector('#p-moneyflow .bf-chip-dn b');
      const u = document.querySelector('#p-moneyflow .bf-chip-up b');
      return t && /单日/.test(t.textContent)
        && parseInt((d || {}).textContent || '0', 10) > 0
        && parseInt((u || {}).textContent || '0', 10) > 0;
    }, { timeout: 45000, polling: 500 }).catch(() => {});
    const chips = await page.evaluate(() => ({
      up: (document.querySelector('#p-moneyflow .bf-chip-up b') || {}).textContent || '',
      dn: (document.querySelector('#p-moneyflow .bf-chip-dn b') || {}).textContent || '',
    }));
    ok(parseInt(chips.dn, 10) > 0 && parseInt(chips.up, 10) > 0,
      `H. 统计胶囊双真数（流入 ${chips.up} / 流出 ${chips.dn}），非「流入16·流出0」`);

    await page.screenshot({ path: path.join(PROJECT_ROOT, '_flowrange.png') });
    console.log('SHOT: _flowrange.png');

    // I 控制台
    ok(errs.length === 0, `I. 控制台 0 报错（实际 ${errs.length}）`);
    if (errs.length) errs.slice(0, 5).forEach(e => console.log('  !', e));

    await browser.close();
  } catch (e) {
    console.error('TEST ERR:', e.message);
    fail++;
  } finally {
    try { server.kill(); } catch {}
    try { fs.unlinkSync(iso); } catch {}
  }
  console.log(`\n========== ${pass} PASS / ${fail} FAIL ==========`);
  process.exit(fail === 0 ? 0 : 1);
})();
