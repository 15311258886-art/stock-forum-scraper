// verify_policybear.cjs — 政策风向「承压/利空面」回归（2026-10-05）
// puppeteer-core + 已下载 chromium；隔离副本库，不碰王总 live DB。
// 背景：王总指出政策风向只列利好（银行/券商/传媒/地产一水儿的利好卡），
// 但政策也会打压行业（如集采压价、清朗整治），利空面必须单独列出来。
// 验证项：
//   A. /api/policy_desk 返回 verdict.bear_sectors，且数量 > 0（旧版这个字段根本不存在）
//   B. 每个承压板块有 level / impacts / evidence，impact 分型可读
//   C. evidence 带标题（可跳原文）+ signal（命中的压制措辞）+ age_days（时效）
//   D. 与利好面分屏：利好卡与承压卡是两个区块，不混在一起
//   E. 前端渲染出 .pol-bears 区块，卡片数与接口一致
//   F. 无压制时是「未命中」空态，不是留白也不许造数据
//   G. 脚注写清扫描口径（政策条数 + 时间跨度）
//   H. 控制台零报错
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname);
const VIEWER_DIR = path.join(PROJECT_ROOT, 'viewer');
const VENV_PY = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');
const PORT = 8794;                 // 固定隔离端口，避免撞 8765 的 live 实例

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
  const PORT_USE = process.env.VPB_PORT ? +process.env.VPB_PORT : await freePort();
  console.log(`== 隔离端口 ${PORT_USE} ==`);
  // 一致快照：WAL 库只拷 app.db 会拷出坏副本，用 sqlite 在线 backup（verify_flowrange.cjs 同款写法）
  const iso = path.join(VIEWER_DIR, 'app_isovp.db');
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
    // A 接口层
    const pd = await getJSON(PORT_USE, '/api/policy_desk?num=80');
    ok(pd.status === 200 && pd.data && pd.data.ok, `A. /api/policy_desk HTTP ${pd.status}`);
    const v = (pd.data && pd.data.verdict) || {};
    const bears = v.bear_sectors || [];
    ok(bears.length > 0, `   verdict.bear_sectors 存在且 > 0（实际 ${bears.length} 个：${bears.map(b => b.sector).join('、')}）`);

    // B 结构完整：level / impacts / evidence 都在
    ok(bears.every(b => b.level === '承压' || b.level === '关注'),
      `   每个板块有 level 档位（${bears.map(b => b.sector + ':' + b.level).join('、')}）`);
    const imps = [...new Set(bears.flatMap(b => b.impacts || []))];
    ok(imps.length > 0, `   压制类型 impact 分型可读（${imps.join('、')}）`);

    // C 依据可核：标题 + 命中措辞 + 时效
    const evs = bears.flatMap(b => b.evidence || []).filter((e, i, a) => a.findIndex(x => x.title === e.title) === i);
    ok(evs.length > 0 && evs.every(e => e.title && e.title.length > 4),
      `   evidence 都有政策标题可核（${evs.length} 条，例：${(evs[0] || {}).title || '-'}）`);
    ok(evs.some(e => e.signal), `   至少一条标出命中的压制措辞（${evs.filter(e => e.signal).map(e => e.signal).join('、')}）`);
    ok(evs.every(e => typeof e.age_days === 'number' && e.age_days >= 0),
      `   每条依据带时效天数（${evs.slice(0, 4).map(e => e.date + '/' + e.age_days + '天').join('，')}）`);

    // 口径：扫描范围要写清楚（不能拿半年跨度说成「当前风向」）
    const sc = v.scan || {};
    ok(Number(sc.total) > 0 && !!sc.span, `   scan 口径写明条数与跨度（${sc.total} 条 / ${sc.span}）`);

    // D 利好面不受影响
    ok((v.sectors || []).length > 0 && (v.sectors || []).every(j => j.stance),
      `   利好面研判仍完整（${(v.sectors || []).map(j => j.sector + ':' + j.stance).join('、')}）`);

    // ---- 前端 ----
    const puppeteer = require(path.join(PROJECT_ROOT, 'node_modules', 'puppeteer-core'));
    const browser = await puppeteer.launch({ executablePath: findChromium(), headless: 'new', args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1000 });
    const errs = [];
    page.on('pageerror', e => errs.push(String(e.message)));
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });

    await page.goto(`http://127.0.0.1:${PORT_USE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    // 政策风向在「消息与外围」大类下，是**两级导航**：首屏 #p-policy 压根不存在，
    // 必须先点大类再点子 tab（2026-10-05 实测：先等 #p-policy 会干等到 60s 超时）。
    await page.evaluate(() => {
      const nav = Array.from(document.querySelectorAll('#sidenav .nav-item'));
      const cat = nav.find(a => /消息与外围/.test(a.textContent));
      if (cat) cat.click();
    });
    await sleep(1500);
    await page.evaluate(() => {
      const sub = document.querySelector('.sub-tab[data-tab="policy"]')
        || document.querySelector('[data-tab="policy"]');
      if (sub) sub.click();
    });
    await page.waitForFunction(
      () => document.querySelector('#p-policy') && document.querySelector('#p-policy .pol-verdict'),
      { timeout: 60000, polling: 500 });

    const fe = await page.evaluate(() => {
      const hosts = document.querySelectorAll('#p-policy .pol-bears');
      const cards = document.querySelectorAll('#p-policy .pol-bear');
      const gatext = (document.querySelector('#p-policy .pol-judges .pol-gt') || {}).textContent || '';
      const bttext = (document.querySelector('#p-policy .pol-bears .pol-gt') || {}).textContent || '';
      const foot = (document.querySelector('#p-policy .pol-bfoot') || {}).textContent || '';
      const first = cards[0] ? cards[0].textContent.replace(/\s+/g, ' ').trim().slice(0, 90) : '';
      return {
        hasBlock: hosts.length > 0,
        cards: cards.length,
        hasFoot: /扫描口径/.test(foot),
        foot: foot.slice(0, 70),
        judgeTitle: gatext.slice(0, 20),
        bearTitle: bttext.slice(0, 40),
        first,
        empty: !!document.querySelector('#p-policy .pol-bears .empty'),
        hasImpact: !!document.querySelector('#p-policy .pol-bears .pol-bear-imp'),
      };
    });

    ok(fe.hasBlock, `E. 前端渲染出「政策承压研判」区块（标题：${fe.bearTitle.trim()}）`);
    ok(fe.cards === bears.length, `   承压卡片数与接口一致（前端 ${fe.cards} / 接口 ${bears.length}）`);
    ok(/利好面/.test(fe.judgeTitle), `D. 上方利好面区块独立标注（${fe.judgeTitle.trim()}）`);
    ok(fe.hasImpact, `   承压卡带 impact 压制类型徽标`);
    ok(fe.hasFoot, `G. 脚注写明扫描口径（${fe.foot.trim()}）`);
    ok(!fe.empty || bears.length === 0,
      `F. 无压制时是「未命中」空态而非造数据（当前 ${bears.length} 个承压板块，空态=${fe.empty}）`);
    console.log(`   首张卡：${fe.first}`);

    // 截图留证
    const el = await page.$('#p-policy');
    if (el) { try { await el.screenshot({ path: path.join(PROJECT_ROOT, '_policybear.png') }); } catch {} }

    ok(errs.length === 0, `H. 控制台零报错${errs.length ? '（' + errs.slice(0, 2).join(' | ') + '）' : ''}`);

    await browser.close();
  } catch (e) {
    console.error('✗ 异常：' + (e && e.message));
    fail++;
  } finally {
    server.kill();
    for (const s of ['', '-wal', '-shm']) {
      try { if (fs.existsSync(iso + s)) fs.unlinkSync(iso + s); } catch {}
    }
  }

  console.log(`\n== ${pass} pass / ${fail} fail ==`);
  process.exit(fail ? 1 : 0);
})();
