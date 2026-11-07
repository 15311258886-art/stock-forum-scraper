/* 回归验证：「策略回测」板块点击策略 → 弹出「关联基金」卡片的端到端检查。

   做法：起临时服务(8793，不动用户正在用的 8765) → 无头 Chromium 打开查看器
        → 切「策略回测」→ 点策略 → 抓弹窗文本/表格行数 → 截图。
   断言：弹窗存在、表格有行、9 列齐全（含半年/一年/策略数）、
        场内/场外标签渲染、行高一致且无重叠、半年/一年/策略数 单元格合法、
        共识度排序切换生效、无前端报错、无非 2xx 请求。

   覆盖两个关键策略：
     - obv_ma_cross  ：用户投诉过的「OBV 上穿均线」——依赖成交量，
                       必须靠场内 ETF/LOF 池才能判定；本次修复的核心验证点。
     - cci_zero_cross：场外净值即可判定的基线策略，覆盖场外池。

   用法：node verify_fund_modal.cjs   （截图输出 _fund_modal_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

// 本项目未在本地装 node_modules，puppeteer-core 由受管 node 工作区提供
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8793;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const KEYS = ['obv_ma_cross', 'cci_zero_cross'];   // [0] 即用户投诉的 OBV 策略
const SHOT = path.join(BASE, '_fund_modal_shot.png');

// 弹窗表格列定义（顺序即断言顺序）：新增「策略数」为第 3 列
const HEADERS = ['基金', '类型', '策略数', '信号日', '距今', '信号日至今', '20日', '半年', '一年'];
const PCT_RE = /^([+-]\d+\.\d{2}%|—)$/;
const NS_RE = /^(\d+|—)$/;   // 策略数：正整数或「—」

// 自动定位 playwright chromium
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

const env = { ...process.env, VIEWER_PORT: String(PORT) };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];

const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', d => srvLog += d.toString());
srv.stderr.on('data', d => srvLog += d.toString());

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/api/fund_strategies?key=${KEYS[0]}`);
      if (r.ok) { ready = true; break; }
    } catch (e) { /* not yet */ }
    await sleep(500);
  }
  console.log('TEMP_SERVER_READY =', ready);
  if (!ready) { console.log(srvLog.slice(0, 1600)); srv.kill(); process.exit(1); }
  if (!CHROME) { console.log('找不到 chromium'); srv.kill(); process.exit(1); }

  // 接口层断言：n_strategies 字段已透出
  const api = await fetch(`http://127.0.0.1:${PORT}/api/fund_strategies?key=${KEYS[0]}`).then(r => r.json());
  const f0 = api.funds && api.funds[0];
  console.log('API n_strategies_total =', api.n_strategies_total, '| first n_strategies =', f0 && f0.n_strategies);

  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--no-proxy-server', '--window-size=1600,1000'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  const badResp = [];
  page.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) badResp.push(`${r.status()} ${r.url()}`); });

  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await sleep(2500);

  // 切「策略回测」（文本匹配；匹配不到就靠 loadStrategyLab 兜底）
  const tab = await page.evaluate(() => {
    const cands = [...document.querySelectorAll('button,a,div,span,li,[role=tab]')];
    const hit = cands.find(x => (x.textContent || '').trim() === '策略回测');
    if (hit) { hit.click(); return hit.tagName + '.' + (hit.className || ''); }
    return null;
  });
  console.log('TAB_CLICKED =', tab);
  await sleep(3000);

  let hasList = await page.evaluate(() => !!document.querySelector('#labLeftBody .lab-item[data-key]'));
  if (!hasList) {
    await page.evaluate(() => { try { window.loadStrategyLab && window.loadStrategyLab(); } catch (e) {} });
    await sleep(3500);
    hasList = await page.evaluate(() => !!document.querySelector('#labLeftBody .lab-item[data-key]'));
  }
  console.log('STRATEGY_LIST =', hasList);

  const results = {};
  for (const KEY of KEYS) {
    await page.evaluate((k) => { try { document.getElementById('fundModal')?.remove(); } catch (e) {}; try { window.onStrategyClick(k); } catch (e) {} }, KEY);
    await sleep(5000);

    const out = await page.evaluate(() => {
      const m = document.getElementById('fundModal');
      if (!m) return { modal: false };
      const rows = [...m.querySelectorAll('.fm-table tbody tr')];
      const g = (sel) => { const e = m.querySelector(sel); return e ? e.innerText.replace(/\s+/g, ' ').trim() : ''; };
      const mkCount = m.querySelectorAll('.fm-mk').length;           // 场内/场外标签
      const inCount = m.querySelectorAll('.fm-mk-in').length;        // 场内标签（OBV 应为 >0）
      const judge = g('.fm-judge-ok, .fm-judge-no');

      // 表头 / 列数
      const heads = [...m.querySelectorAll('.fm-table thead th')].map(th => th.innerText.trim());
      const cellCounts = rows.map(tr => tr.querySelectorAll('td').length);

      // 行高与重叠（防「文字溢出 / 挤字 / 压行」）
      const rects = rows.map(tr => { const r = tr.getBoundingClientRect(); return { t: r.top, b: r.bottom }; });
      let overlap = 0;
      for (let i = 1; i < rects.length; i++) if (rects[i].t < rects[i - 1].b - 1.5) overlap++;
      const hs = rects.map(r => Math.round(r.b - r.t));
      const hMin = hs.length ? Math.min(...hs) : 0;
      const hMax = hs.length ? Math.max(...hs) : 0;

      // 横向是否溢出（9 列应能塞进弹窗，不出现横向滚动条）
      const wrap = m.querySelector('.fm-table-wrap');
      const hScroll = wrap ? Math.round(wrap.scrollWidth - wrap.clientWidth) : null;

      const cellsOf = (i) => rows[i] ? [...rows[i].querySelectorAll('td')].map(td => td.innerText.replace(/\s+/g, ' ').trim()) : [];
      return {
        modal: true,
        title: g('.fm-head h3'),
        judgeOk: !!m.querySelector('.fm-judge-ok'),
        judgeText: judge,
        heads: heads,
        headCount: heads.length,
        cellCountsUnique: [...new Set(cellCounts)],
        rowCount: rows.length,
        rowHeights: { min: hMin, max: hMax },
        overlap: overlap,
        hScroll: hScroll,
        marketTagCount: mkCount,
        innerTagCount: inCount,
        nsClassCount: m.querySelectorAll('.fm-ns').length,   // 策略数单元格
        row0: cellsOf(0),
        row1: cellsOf(1),
        foot: g('.fm-foot'),
        srcCollapsed: !!m.querySelector('.fm-src'),
      };
    });
    results[KEY] = out;
    console.log('--- 弹窗内容 [' + KEY + '] ---');
    console.log(JSON.stringify(out, null, 2));

    // 给第一个（OBV）策略：验证共识度排序切换
    let cons = null;
    if (KEY === KEYS[0]) {
      await page.evaluate(() => {
        const els = [...document.querySelectorAll('#fundModal .fm-sort')];
        const c = els.find(e => e.innerText.trim() === '共识度');
        if (c) c.click();
      });
      await sleep(800);
      cons = await page.evaluate(() => {
        const m = document.getElementById('fundModal');
        const rows = [...m.querySelectorAll('.fm-table tbody tr')];
        const nums = rows.map(tr => {
          const td = tr.querySelectorAll('td')[2];
          const t = td ? td.innerText.trim() : '';
          return t === '—' ? null : parseInt(t, 10);
        });
        const valid = nums.filter(v => v != null);
        let sortedDesc = valid.length > 1;
        for (let i = 1; i < valid.length; i++) if (valid[i - 1] < valid[i]) sortedDesc = false;
        const onConsensus = !!document.querySelector('#fundModal .fm-sort.on') &&
          document.querySelector('#fundModal .fm-sort.on').innerText.trim() === '共识度';
        return { first: valid[0], last: valid[valid.length - 1], sortedDesc, onConsensus, sample: valid.slice(0, 6) };
      });
      console.log('CONSENSUS_SORT =', JSON.stringify(cons));
      // 切回新鲜度，再截图（保持默认视图）
      await page.evaluate(() => {
        const els = [...document.querySelectorAll('#fundModal .fm-sort')];
        const c = els.find(e => e.innerText.trim() === '新鲜度');
        if (c) c.click();
      });
      await sleep(600);
      try {
        const el = await page.$('#fundModal .modal-card');
        if (el) await el.screenshot({ path: SHOT });
        else await page.screenshot({ path: SHOT });
        console.log('SCREENSHOT =', SHOT, fs.existsSync(SHOT) ? fs.statSync(SHOT).size + 'B' : 'MISS');
      } catch (e) { console.log('截图失败', e.message); }
    }
    results[KEY]._cons = cons;
  }

  console.log('--- 前端报错 ---', errors.length ? errors.slice(0, 6) : '无');
  console.log('--- 非2xx请求 ---', badResp.length ? [...new Set(badResp)].slice(0, 10) : '无');

  await browser.close();
  srv.kill();

  // ---- 断言 ----
  const fails = [];
  // 接口层
  if (!(api.n_strategies_total > 0)) fails.push('接口未返回 n_strategies_total');
  if (!(f0 && f0.n_strategies > 0)) fails.push('接口首只基金未带 n_strategies');

  for (const KEY of KEYS) {
    const r = results[KEY] || {};
    const tag = '[' + KEY + ']';
    if (!r.modal) { fails.push(tag + ' 弹窗未出现'); continue; }
    if (!r.rowCount) { fails.push(tag + ' 表格无数据行'); continue; }
    if (r.headCount !== HEADERS.length) fails.push(tag + ` 表头 ${r.headCount} 列，应为 ${HEADERS.length}`);
    const hmis = HEADERS.filter((h, i) => r.heads[i] !== h);
    if (hmis.length) fails.push(tag + ' 表头不符：' + JSON.stringify(r.heads));
    if (r.cellCountsUnique.length !== 1 || r.cellCountsUnique[0] !== HEADERS.length) {
      fails.push(tag + ' 数据行列数不一致：' + JSON.stringify(r.cellCountsUnique));
    }
    if (r.overlap > 0) fails.push(tag + ` 行重叠 ${r.overlap} 处`);
    if (!(r.rowHeights.min > 0) || (r.rowHeights.max - r.rowHeights.min) > 2) {
      fails.push(tag + ' 行高不一致/异常：' + JSON.stringify(r.rowHeights));
    }
    if (r.hScroll != null && r.hScroll > 2) fails.push(tag + ` 表格横向溢出 ${r.hScroll}px`);
    if (r.nsClassCount !== r.rowCount) fails.push(tag + ' 策略数列渲染数 != 行数');
    const r0 = r.row0 || [];
    if (!NS_RE.test(String(r0[2] || ''))) fails.push(tag + ' 策略数列非法：' + JSON.stringify(r0[2]));
    for (const idx of [7, 8]) {           // 半年 / 一年
      if (!PCT_RE.test(String(r0[idx] || ''))) fails.push(tag + ` 第 ${idx + 1} 列（${HEADERS[idx]}）非数值：` + JSON.stringify(r0[idx]));
    }
    if (!r.judgeOk) fails.push(tag + ' 结论未标绿（判定异常）');
    // 共识度排序（仅 OBV 测）
    if (KEY === KEYS[0] && r._cons) {
      if (!r._cons.onConsensus) fails.push(tag + ' 点「共识度」后未高亮');
      if (!r._cons.sortedDesc) fails.push(tag + ' 共识度排序非降序：' + JSON.stringify(r._cons.sample));
      if (!(r._cons.first > r._cons.last)) fails.push(tag + ' 共识度排序首行未大于末行');
    }
  }
  const obv = results['obv_ma_cross'] || {};
  if (!(obv.innerTagCount > 0)) fails.push('[obv_ma_cross] 无场内标签，成交量池未生效');
  if (errors.length) fails.push('前端报错 ' + errors.length + ' 条');

  console.log('OBV_innerTagCount =', obv.innerTagCount, '(应 > 0，证明场内ETF/LOF成交量池生效)');
  console.log('OBV 首行 =', JSON.stringify(obv.row0));
  if (fails.length) { console.log('FAIL 明细：'); fails.forEach(f => console.log('  - ' + f)); }
  const ok = fails.length === 0;
  console.log(ok ? 'RESULT = PASS' : 'RESULT = FAIL');
  process.exit(ok ? 0 : 1);
})().catch(e => { console.log('FATAL', e.message); try { srv.kill(); } catch (_) {} process.exit(1); });
