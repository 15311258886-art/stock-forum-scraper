// verify_signal_weights.cjs —— 「每日预测」十二路信号条 + 权重表布局护栏
// 依赖：puppeteer-core（受管 node workspace）+ 项目 .venv 起后端
// 跑法：node verify_signal_weights.cjs
// 2026-10-01 新增：六路 → 十二路后，信号条从「命中数」单列改「命中 · 覆盖% · 权%」三列，
//   值列 .pred-sb-v 由 64px 撑到 148px；本脚本守住「12 行 + 零溢出 + 无旧 fund 残留」。
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8814;
const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PY   = BASE + '\\.venv\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_sig_weights_shot.png');

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
    try { const r = await getJSON(PORT, '/api/prediction/today'); if (r.status) { up = true; break; } } catch (e) {}
    await new Promise(r => setTimeout(r, 500));
  }
  if (!up) { console.log('后端起不来，退出'); try { srv.kill(); } catch (e) {} process.exit(1); }

  console.log('A. 接口口径');
  const scores = await getJSON(PORT, '/api/prediction/scores');
  ok(scores.status === 200, 'scores 200');
  const keys = (scores.body.signals || []).map(s => s.key);
  ok(!keys.includes('fund'), '旧「龙虎榜资金」fund 已移除（残留会污染权重读回）');
  const want = ['tech', 'holders', 'ann', 'desk', 'inst', 'policy',
                'board', 'sentiment', 'overseas', 'follow', 'extref', 'lhb'];
  want.forEach(k => ok(keys.includes(k), 'scores 含 ' + k));
  ok(keys.length === 12, '信号共 12 路（实=' + keys.length + '）');

  const wmap = {};
  (scores.body.signals || []).forEach(s => { if (typeof s.weight === 'number') wmap[s.key] = s.weight; });
  const rank = Object.keys(wmap).sort((a, b) => wmap[b] - wmap[a]);
  ok(rank[0] === 'tech', '权重最高为技术面（实=' + rank[0] + '）');
  // 覆盖度校正是否真的在起作用：覆盖 2 只（1.3%）的外部参考必须低于覆盖 52 只（33%）的游资上榜。
  // 若两条权重倒挂，说明覆盖度乘数形同虚设（2026-10-01 前的老口径就是这样：两者同拿 W_MIN 底权）。
  ok(wmap.extref < wmap.lhb,
     '覆盖度校正生效：外部参考(2只) ' + (wmap.extref * 100).toFixed(2) + '% < 游资上榜(52只) ' +
     (wmap.lhb * 100).toFixed(2) + '%');
  // 龙虎榜两路（机构席位 / 游资上榜）整体必须处于噪声级，不得挤进前列
  ok((wmap.inst + wmap.lhb) < wmap.tech,
     '龙虎榜两路合计 ' + ((wmap.inst + wmap.lhb) * 100).toFixed(1) + '% < 技术面单路 ' +
     (wmap.tech * 100).toFixed(1) + '%');
  ok(wmap.lhb <= 0.05, '游资上榜权重 ≤5%（噪声级，实=' + (wmap.lhb * 100).toFixed(2) + '%）');
  const sum = Object.values(wmap).reduce((a, b) => a + b, 0);
  ok(Math.abs(sum - 1) < 0.02, '权重合计≈100%（实=' + (sum * 100).toFixed(1) + '%）');

  const CHROME = findChrome();
  if (!CHROME) {
    console.log('\n[跳过] 未装 chromium，浏览器段不跑（接口段已验）。');
    try { srv.kill(); } catch (e) {}
    process.exit(fails.length ? 1 : 0);
  }
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--window-size=1600,1000'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));

  await page.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'networkidle2' });
  await page.evaluate(() => { const el = [...document.querySelectorAll('[data-tab="prediction"]')][0]; if (el) el.click(); });
  await new Promise(r => setTimeout(r, 1200));

  console.log('B. 浏览器布局');
  const panel = await page.evaluate(() => {
    const p = document.querySelector('#p-prediction');
    if (!p) return null;
    const rows = [...p.querySelectorAll('.pred-sb-row')];
    const overflow = [];
    rows.forEach((r) => {
      const v = r.querySelector('.pred-sb-v');
      if (v && (v.scrollWidth > v.clientWidth + 1 || v.getBoundingClientRect().right >
                 r.getBoundingClientRect().right + 1)) {
        overflow.push((r.querySelector('.pred-sb-nm') || {}).textContent + ':' + v.textContent.trim());
      }
    });
    const head = [...p.querySelectorAll('.pred-sec-t')].map(e => e.textContent.trim());
    // 「信号贡献」「信号权重」两个区块共用 .pred-sb-row 类名，行数要按区块分别数，
    // 直接全表 count 会把两段相加。
    const sectionOf = (kw) => [...p.querySelectorAll('.pred-sec-t')]
      .find((e) => e.textContent.includes(kw))
      ?.closest('div,section')?.parentElement;
    const tallySec = sectionOf('信号贡献');
    const tallyRows = tallySec ? tallySec.querySelectorAll('.pred-sb-row').length : 0;
    // 旧字样检查只针对「信号名」列：历史 run 的 prediction_item.hit_signals 里
    // 存着 v1.0 的六路旧名，那是历史记录、不应被判定为缺陷。
    const nmTexts = [...p.querySelectorAll('.pred-sb-nm')].map((e) => e.textContent.trim());
    return {
      visible: p.offsetParent !== null,
      sbRows: tallyRows,
      overflow,
      heads: head,
      hasOldName: nmTexts.includes('龙虎榜资金'),
      nmTexts,
    };
  });
  ok(panel && panel.visible, '面板已渲染可见');
  ok(panel && panel.sbRows === 12, '信号贡献条 12 行（实=' + (panel && panel.sbRows) + '）');
  ok(panel && panel.overflow.length === 0, '信号条值列零溢出（溢出=' + (panel && panel.overflow.join(' | ')) + '）');
  ok(panel && panel.heads.some(h => h.includes('12路')), '区块标题显示「12路」（实=' + (panel && panel.heads.join(' / ')) + '）');
  ok(panel && !panel.hasOldName, '信号名列无旧「龙虎榜资金」（实=' + (panel && panel.nmTexts.join('/')) + '）');

  // 卡片 closest 溢出（12 路进入 TOP10 reason 后，卡片文字 shouldn't clip）
  const cardClip = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('#p-prediction .pred-card')];
    const bad = [];
    cards.forEach((c) => {
      const r = c.querySelector('.pred-card-reason') || c;
      if (r.scrollHeight > r.clientHeight + 2 && getComputedStyle(r).overflow !== 'visible') {
        bad.push((c.textContent || '').slice(0, 12));
      }
    });
    return bad;
  });
  ok(cardClip.length === 0, '预测卡片文字未被裁切（裁切=' + cardClip.join(',') + '）');

  ok(consoleErrors.length === 0, '控制台零报错（实=' + consoleErrors.length + '）');
  if (consoleErrors.length) consoleErrors.slice(0, 5).forEach(e => console.log('   ERR: ' + e));

  const el = await page.$('#p-prediction');
  if (el) await el.screenshot({ path: SHOT });
  console.log('  截图：' + SHOT);

  await browser.close();
  try { srv.kill(); } catch (e) {}
  console.log('\n==== 结果：' + (fails.length ? ('FAIL ' + fails.length + ' 项') : 'ALL PASS') + ' ====');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
