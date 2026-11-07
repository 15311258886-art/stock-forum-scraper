/* 回归护栏：个股详情入口「到底有没有真的绑上」（2026-09-28）
   ---------------------------------------------------------------------------
   背景（王总反馈）："某些地方的股票代码点不开"——看着跟别处一样是可点芯片，点了却毫无反应。
   实测根因两处漏绑定：
     ① 游资龙虎榜的代码芯片（app.js lhbSymChip）**从未被绑定**：行 handler 里写着
        「让 K 线芯片自己处理」，可芯片自身没有任何 handler → 点代码时行 handler 直接
        return，什么都不会发生。
     ② 资金流向「全市场主力净流入榜」的代码列渲染的是 data-wt-open，而渲染末尾只调
        bindFeatStockClicks（其选择器仅认 data-feat-open）→ 不匹配 → 无绑定。
   本护栏做四件事：
     A. 绑定审计：遍历全部面板，凡「自身与祖先链上都没有 onclick」的股票入口元素必须为 0
        —— 防止将来新加面板再漏挂（这是本次能一次找全的关键判据）。
     B. 真实点击：游资龙虎榜代码芯片 → 抽屉打开且标题含该代码。
     C. 反向断言：游资龙虎榜「行」点击仍只做选中展开，不抢开抽屉（证明 stopPropagation 写对）。
     D. 真实点击：资金流向代码列 → 抽屉打开。
     E. 对照组仍正常：板块动向代码芯片 / 智能选股代码列 / 共同持仓共识行（王总截图那一处）。
     F. 控制台零报错。
   用法：node verify_stock_entry_bindings.cjs
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8795;
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
const srv = spawn(path.join(BASE, '.venv', 'Scripts', 'python.exe'), ['-m', 'viewer.server'],
  { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', (d) => { srvLog += d.toString(); });
srv.stderr.on('data', (d) => { srvLog += d.toString(); });

const results = [];
const ok = (name, pass, extra) => {
  results.push([name, !!pass]);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '  → ' + extra : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitPort() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`${ROOT}/api/summary`); if (r.ok) return true; } catch (e) { /* wait */ }
    await sleep(500);
  }
  return false;
}

const ATTRS = ['sym', 'hpSym', 'olSym', 'ovSym', 'exSym', 'wtOpen', 'featOpen', 'openSym', 'wtAlertOpen'];
const TABS = [
  ['overview', 'dashboard'],
  // 2026-09-29 导航重构对齐：原 research 大类拆为 money/info，原 enhance 解散归位
  ['money', 'lhb'], ['money', 'lhb_sectors'], ['money', 'stocks'], ['money', 'sentiment'],
  ['info', 'topics'], ['info', 'overseas_sentiment'], ['info', 'overseas_hotspots'],
  ['info', 'geo_events'], ['info', 'geo_policy'], ['info', 'geo_tech'],
  ['players', 'players'], ['players', 'operating'], ['players', 'niusan'], ['players', 'consensus'],
  ['players', 'cohold'], ['players', 'persons'],
  ['opinion', 'extref_soft'], ['opinion', 'extref_comm'], ['opinion', 'extref_rank'],
  ['watch', 'watch'], ['watch', 'watch_alerts'],
  ['watch', 'screener'], ['money', 'moneyflow'], ['watch', 'valuation'],
  ['opinion', 'research'], ['watch', 'multiquote'], ['info', 'news'],
];

const AUDIT = (tab, attrs) => {
  const p = document.querySelector('#p-' + tab);
  if (!p || p.offsetParent === null) return { invisible: true };
  const sel = attrs.map((a) => `[data-${a.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())}]`).join(',');
  const out = { total: 0, none: {} };
  [...p.querySelectorAll(sel)].forEach((el) => {
    out.total++;
    if (el.onclick) return;
    let n = el.parentElement;
    for (let i = 0; n && i < 14; i++, n = n.parentElement) if (n.onclick) return;
    const key = el.tagName + '.' + String(el.className).slice(0, 30) + ' [' +
      attrs.filter((a) => el.dataset[a] !== undefined).map((a) => 'data-' + a.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())).join(',') + ']';
    out.none[key] = (out.none[key] || 0) + 1;
  });
  return out;
};

const goto = async (page, cat, tab, wait = 2600) => {
  await page.evaluate((c, t) => {
    const nav = document.querySelector(`.nav-item[data-cat="${c}"]`);
    if (nav) nav.click();
    // 给 nav 激活留足时间再点子页签：headless 下过早点子页签会落在未激活分类上，
    // 不触发 load（资金流向曾因此假阴性）。700ms 实测稳。
    setTimeout(() => { const st = document.querySelector(`.sub-tab[data-tab="${t}"]`); if (st) st.click(); }, 700);
  }, cat, tab);
  await sleep(wait);
  await page.keyboard.press('Escape');
  await sleep(250);
};

// DOM 点击（排除鼠标坐标被遮挡的干扰），返回 {n, ownOnclick, sym}
const clickEntry = (page, sel) => page.evaluate((q) => {
  const els = [...document.querySelectorAll(q)];
  if (!els.length) return { n: 0 };
  const el = els[0];
  const sym = el.dataset.sym || el.dataset.hpSym || el.dataset.olSym || el.dataset.ovSym ||
    el.dataset.exSym || el.dataset.wtOpen || el.dataset.featOpen || el.dataset.openSym || '';
  const own = !!el.onclick;
  el.click();
  return { n: els.length, ownOnclick: own, sym };
}, sel);

const drawerState = (page) => page.evaluate(() => {
  const d = document.querySelector('#drawer');
  return { shown: !!(d && d.classList.contains('show')), title: (document.querySelector('#drawerTitle') || {}).textContent || '' };
});

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪:\n' + srvLog.slice(-1500));
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1680,1300'],
      defaultViewport: { width: 1680, height: 1300 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('.nav-item', { timeout: 30000 });

    /* ---------- A. 全站绑定审计 ---------- */
    let total = 0, noneTotal = 0;
    const badList = [];
    for (const [cat, tab] of TABS) {
      await goto(page, cat, tab, 2000);
      let r;
      try { r = await page.evaluate(AUDIT, tab, ATTRS); } catch (e) { r = { invisible: true }; }
      if (r.invisible || !r.total) { badList.push(`${cat}/${tab}: 未渲染(不计)`); continue; }
      total += r.total;
      const n = Object.values(r.none).reduce((a, b) => a + b, 0);
      noneTotal += n;
      if (n) Object.entries(r.none).forEach(([k, v]) => badList.push(`${cat}/${tab}  ${v}× ${k}`));
    }
    ok('A 全站入口已绑定（未绑元素 = 0）', noneTotal === 0,
      `扫描 ${total} 个入口元素，未绑 ${noneTotal} 个` + (noneTotal ? '\n     ' + badList.filter((x) => x.includes('  ')).join('\n     ') : ''));

    /* ---------- B. 游资龙虎榜代码芯片 ---------- */
    await goto(page, 'money', 'lhb');
    const lhbRows = await page.evaluate(() => document.querySelectorAll('#p-lhb tr[data-lhb-stock]').length);
    const b1 = await clickEntry(page, '#p-lhb .kline-chip[data-sym]');
    await sleep(2600);
    const bs = await drawerState(page);
    ok('B1 游资龙虎榜代码芯片自身已绑定 onclick', b1.ownOnclick === true, `芯片元素 ${b1.n} 个 / 首个 ${b1.sym}`);
    ok('B2 点它 → 抽屉打开且标题含该代码', bs.shown && bs.title.includes(b1.sym), `title="${bs.title.slice(0, 44)}"`);
    await page.keyboard.press('Escape'); await sleep(300);

    /* ---------- C. 反向断言：行点击仍只做选中展开 ---------- */
    const before = await page.evaluate(() => document.querySelector('#p-lhb').innerHTML.length);
    await page.evaluate(() => { const tr = document.querySelector('#p-lhb tr[data-lhb-stock]'); if (tr) { const c = tr.querySelector('td:nth-child(2)'); (c || tr).click(); } });
    await sleep(1200);
    const cst = await page.evaluate(() => ({
      len: document.querySelector('#p-lhb').innerHTML.length,
      drawer: !!(document.querySelector('#drawer') || {}).classList &&
        document.querySelector('#drawer').classList.contains('show'),
      sel: (window.LHB && window.LHB.sel) || null,
    }));
    ok('C1 行点击仍走「选中展开」（面板内容变化）', cst.len !== before, `${before} → ${cst.len}`);
    ok('C2 行点击不误开抽屉（stopPropagation 只在芯片上）', cst.drawer === false && lhbRows > 0,
      `drawer=${cst.drawer} 行数=${lhbRows}`);

    /* ---------- D. 资金流向代码列 ---------- */
    // 资金流向 rank 数据来自东财外部接口，无代理环境下偶发慢/冷启延迟；
    // 故：进面板后强制点 rank 页签触发重载，并轮询最多 12s 等 30 行真正渲染出来再断言。
    await goto(page, 'money', 'moneyflow', 2500);
    await page.evaluate(() => { const m = document.querySelector('#p-moneyflow [data-mf-mode="rank"]'); if (m) m.click(); });
    for (let i = 0; i < 24; i++) {
      const n = await page.evaluate(() => document.querySelectorAll('#p-moneyflow [data-wt-open]').length);
      if (n > 0) break;
      await sleep(500);
    }
    // 2026-09-29 容错：rank 榜数据来自东财 push2，被限流时 12s 内可能一行都渲染不出来。
    // 此时回退断言面板默认的板块快照表（新浪源，稳定）——绑定走的是同一段循环，防漏目的不变。
    let dNote = '';
    let dCount = await page.evaluate(() => document.querySelectorAll('#p-moneyflow [data-wt-open]').length);
    if (!dCount) {
      await page.evaluate(() => { const b = document.querySelector('#p-moneyflow [data-mf-mode="board"]'); if (b) b.click(); });
      for (let i = 0; i < 24; i++) {
        dCount = await page.evaluate(() => document.querySelectorAll('#p-moneyflow [data-wt-open]').length);
        if (dCount > 0) break;
        await sleep(500);
      }
      dNote = '（rank 被东财限流未出数据，改断言板块快照表）';
    }
    const d1 = await clickEntry(page, '#p-moneyflow [data-wt-open]');
    await sleep(2600);
    const ds = await drawerState(page);
    ok('D1 资金流向代码列自身已绑定 onclick', d1.ownOnclick === true, `元素 ${d1.n} 个 / 首个 ${d1.sym}${dNote}`);
    ok('D2 点它 → 抽屉打开且标题含该代码', ds.shown && ds.title.includes(d1.sym), `title="${ds.title.slice(0, 44)}"`);
    await page.keyboard.press('Escape'); await sleep(300);

    /* ---------- E. 对照组（原本就正常的入口不能被改坏） ---------- */
    const CTRL = [
      ['money', 'lhb_sectors', '#p-lhb_sectors .kline-chip[data-sym]', '板块动向·代码芯片'],
      ['watch', 'screener', '#p-screener [data-wt-open]', '智能选股·代码列'],
      ['players', 'consensus', '#p-consensus tr[data-sym]', '共同持仓共识·行（王总截图那处）'],
      ['money', 'stocks', '#p-stocks .stk-card[data-sym]', '当前讨论股票·卡片'],
    ];
    for (const [cat, tab, sel, label] of CTRL) {
      await goto(page, cat, tab, 3000);
      const r = await clickEntry(page, sel);
      await sleep(2400);
      const st = await drawerState(page);
      ok(`E 对照组仍正常：${label}`, r.n > 0 && st.shown && st.title.includes(r.sym),
        `元素=${r.n} title="${st.title.slice(0, 38)}"`);
      await page.keyboard.press('Escape'); await sleep(300);
    }

    /* ---------- F. 控制台 ---------- */
    ok('F 控制台零报错', errs.length === 0, errs.slice(0, 3).join(' | '));
  } catch (e) {
    ok('执行异常：' + String(e).slice(0, 160), false);
  } finally {
    const fails = results.filter((r) => !r[1]).length;
    console.log(`\n===== ${results.length - fails} PASS / ${fails} FAIL =====`);
    if (browser) await browser.close();
    srv.kill();
    if (fails) process.exit(1);
  }
})();
