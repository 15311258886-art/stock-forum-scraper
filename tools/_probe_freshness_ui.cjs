/* ============================================================================
   验收脚本（2026-10-09，feat-freshness-ui）：顶栏「帖抓取 / 行情」拆栏 + 隔日面板「数据日」标注
   ----------------------------------------------------------------------------
   两遍：
     Pass A：打**真实**后端（/api/refresh_status 目前无 market 字段）→ 验证**降级路径**
             （行情栏显示「时效未知」，绝不用帖抓取时间冒充）；顺带验证主力净流入榜
             用真实 trade_date 渲染出「数据日」徽标；切 4 个其它面板确认未破坏。
     Pass B：请求拦截，伪造 /api/refresh_status.market 与 /api/moneyflow_rank(is_intraday)
             → 验证**正常路径**（盘中滞后警示色 / 休市 / 盘中快照徽标）。
   输出：JSON 断言 + 截图路径（tools/_probe_out/）。
   用法：node tools/_probe_freshness_ui.cjs
   ========================================================================== */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const EDGE_CANDIDATES = [
  process.env.EDGE_BIN,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean);
const EDGE = EDGE_CANDIDATES.find((p) => fs.existsSync(p));
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const OUT = path.resolve(__dirname, '_probe_out');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const errors = [];
const assert = (name, cond, detail) => results.push({ name, pass: !!cond, detail: String(detail == null ? '' : detail) });

function attachConsole(p) {
  p.on('pageerror', (e) => errors.push({ kind: 'pageerror', text: String(e && e.message || e) }));
  p.on('console', (m) => { if (m.type() === 'error') errors.push({ kind: 'console.error', text: m.text() }); });
}

async function topbar(p) {
  return p.evaluate(() => {
    const t = (id) => { const e = document.getElementById(id); return e ? e.textContent.trim() : null; };
    const c = (id) => { const e = document.getElementById(id); return e ? e.className : null; };
    const tb = document.querySelector('.topbar');
    return {
      rsText: t('rsText'), rsDot: c('rsDot'),
      marketWrap: !!document.getElementById('rsMarketWrap'),
      marketText: t('rsMarket'), marketDot: c('rsMktDot'),
      topbarH: tb ? tb.offsetHeight : null,
      scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
    };
  });
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  if (!EDGE) { console.log('EDGE_NOT_FOUND=' + JSON.stringify(EDGE_CANDIDATES)); process.exit(2); }
  console.log('EDGE=' + EDGE);
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'],
  });

  /* ===================== Pass A：真实后端（降级路径） ===================== */
  {
    const p = await browser.newPage();
    attachConsole(p);
    await p.setViewport({ width: 1366, height: 900 });
    await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3500);

    const tb = await topbar(p);
    console.log('A_TOPBAR=' + JSON.stringify(tb));
    assert('A1 顶栏「帖抓取」栏存在且文案含「帖抓取」', tb.rsText && tb.rsText.includes('帖抓取'), tb.rsText);
    assert('A2 顶栏新增「行情」栏存在', tb.marketWrap === true, tb.marketWrap);
    assert('A3 后端无 market 字段 → 行情栏降级为「时效未知」',
      /时效未知/.test(tb.marketText || ''), tb.marketText);
    assert('A4 降级时不得用帖抓取时间冒充行情（行情栏无 HH:MM:SS）',
      !/\d{2}:\d{2}:\d{2}/.test(tb.marketText || ''), tb.marketText);
    assert('A5 1366 宽无横向溢出', tb.scrollW <= tb.innerW + 2, `scrollW=${tb.scrollW} innerW=${tb.innerW}`);

    await p.screenshot({ path: path.join(OUT, 'fresh_topbar_real.png'), clip: { x: 0, y: 0, width: 1366, height: 120 } });

    // 主力净流入榜：切到 rank 子页签，验证真实 trade_date 渲染出「数据日」徽标
    await p.evaluate(() => { location.hash = '#money/moneyflow'; });
    await sleep(2500);
    const rankTab = await p.$('#p-moneyflow [data-mf-mode="rank"]');
    assert('A6 资金流向面板出现「全市场主力净流入榜」子页签', !!rankTab, !!rankTab);
    if (rankTab) { await rankTab.click(); await sleep(3000); }
    const mf = await p.evaluate(() => {
      const host = document.getElementById('p-moneyflow');
      if (!host) return { found: false };
      const badge = host.querySelector('.dd-bar .dd-badge');
      const note = host.querySelector('.dd-bar .dd-note');
      const firstCode = host.querySelector('.wt-tb tbody tr td:nth-child(2)');
      return {
        found: true,
        badge: badge ? badge.textContent.replace(/\s+/g, ' ').trim() : null,
        badgeCls: badge ? badge.className : null,
        note: note ? note.textContent.replace(/\s+/g, ' ').trim() : null,
        hasRows: !!firstCode,
      };
    });
    console.log('A_MONEYFLOW=' + JSON.stringify(mf));
    assert('A7 主力净流入榜出现「数据日」徽标', mf.found && /数据日\s*2026-10-08/.test(mf.badge || ''), mf.badge);
    assert('A8 主力净流入榜徽标为「日终」口径（dd-past 琥珀警示）', /dd-past/.test(mf.badgeCls || ''), mf.badgeCls);
    assert('A9 榜单表格有数据行', mf.hasRows === true, mf.hasRows);
    await p.screenshot({ path: path.join(OUT, 'fresh_moneyflow_rank_real.png') });

    // 切 4 个其它面板，确认未破坏
    const panels = [
      ['#overview/dashboard', 'p-dashboard', '今日看板'],
      ['#money/lhb', 'p-lhb', '游资龙虎榜'],
      ['#money/lhb_sectors', 'p-lhb_sectors', '板块动向'],
      ['#watch/diag', 'p-diag', '个股诊断'],
    ];
    for (const [hash, id, label] of panels) {
      await p.evaluate((h) => { location.hash = h; }, hash);
      await sleep(2200);
      const info = await p.evaluate((pid) => {
        const el = document.getElementById(pid);
        if (!el) return { ok: false };
        const db = el.querySelector('.dd-badge');
        return { ok: true, active: el.classList.contains('active'),
          len: (el.innerText || '').length, badge: db ? db.textContent.replace(/\s+/g, ' ').trim() : null };
      }, id);
      console.log(`A_PANEL ${label}=` + JSON.stringify(info));
      assert(`A10 面板「${label}」已激活且有内容`, info.ok && info.active && info.len > 40, JSON.stringify(info));
    }
    await p.screenshot({ path: path.join(OUT, 'fresh_diag_panel.png') });
    await p.close();
  }

  /* ===================== Pass B：拦截伪造（正常路径） ===================== */
  {
    const p = await browser.newPage();
    attachConsole(p);
    await p.setViewport({ width: 1366, height: 900 });
    // 可变的伪造行情，供切换测试
    const FAKE = { market: { src: 'tencent', quote_time: '2026-10-09 13:19:29', server_now: '2026-10-09 13:21:00', lag_seconds: 30, is_trading: true } };
    const FAKE_RANK = { intraday: true };
    await p.setRequestInterception(true);
    p.on('request', (req) => {
      const u = req.url();
      try {
        if (u.includes('/api/market_freshness')) return req.respond({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' });
        if (u.includes('/api/refresh_status')) {
          const body = JSON.stringify({
            last_ok_at: '2026-10-09 13:35:55', last_attempt_at: '2026-10-09 13:35:55', status: 'ok', error: '',
            sources: [], tracked_sources: [], interval_seconds: 300, stale_seconds: 600,
            scheduler_last_run_at: '2026-10-09 13:35:55', scheduler_enabled: true,
            market: FAKE.market,
          });
          return req.respond({ status: 200, contentType: 'application/json', body });
        }
        if (u.includes('/api/moneyflow_rank')) {
          const rows = FAKE_RANK.rows || [
            { code: '601208', name: '东材科技', price: 50.92, pct: 5.5993, main_net: 720587120.0, main_pct: null, huge_net: 894013264.0, big_net: -173426144.0 },
            { code: '603002', name: '宏昌电子', price: 18.69, pct: 7.7855, main_net: 590085088.0, main_pct: null, huge_net: 643387600.0, big_net: -53302512.0 },
          ];
          const payload = { ok: true, rows, source: 'eastmoney_datacenter' };
          if (FAKE_RANK.intraday) { payload.data_date = '2026-10-09'; payload.data_time = '13:19'; payload.is_intraday = true; }
          else { payload.data_date = '2026-10-08'; payload.data_time = ''; payload.is_intraday = false; }
          return req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
        }
      } catch (e) { /* 落到 continue */ }
      return req.continue();
    });
    await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3500);

    // B1 盘中新鲜（lag=30）
    let tb = await topbar(p);
    console.log('B_FRESH=' + JSON.stringify(tb));
    assert('B1 行情栏显示真实报价时间', /13:19:29/.test(tb.marketText || ''), tb.marketText);
    assert('B2 行情栏显示滞后秒数', /滞后\s*30\s*秒/.test(tb.marketText || ''), tb.marketText);
    assert('B3 盘中新鲜 → 绿点(rs-ok)', /rs-ok/.test(tb.marketDot || ''), tb.marketDot);
    await p.screenshot({ path: path.join(OUT, 'fresh_topbar_inject_fresh.png'), clip: { x: 0, y: 0, width: 1366, height: 120 } });

    // B4 盘中滞后（lag=161 > 120）→ 警示
    FAKE.market = { src: 'tencent', quote_time: '2026-10-09 13:19:29', server_now: '2026-10-09 13:22:10', lag_seconds: 161, is_trading: true };
    await p.evaluate(() => loadRefreshStatus());
    await sleep(800);
    tb = await topbar(p);
    console.log('B_STALE=' + JSON.stringify(tb));
    assert('B4 盘中滞后 161s → 文案含「滞后 161 秒」与警示符号', /滞后\s*161\s*秒/.test(tb.marketText || '') && /⚠/.test(tb.marketText || ''), tb.marketText);
    assert('B5 盘中滞后 → 警示色 rs-stale', /rs-stale/.test(tb.marketDot || ''), tb.marketDot);
    const wrapCls = await p.evaluate(() => { const e = document.getElementById('rsMarketWrap'); return e ? e.className : null; });
    assert('B6 行情栏 class 含 rb-market-stale', /rb-market-stale/.test(wrapCls || ''), wrapCls);
    await p.screenshot({ path: path.join(OUT, 'fresh_topbar_inject_stale.png'), clip: { x: 0, y: 0, width: 1366, height: 120 } });

    // B7 休市
    FAKE.market = { src: 'tencent', quote_time: '2026-10-08 15:00:00', server_now: '2026-10-09 13:22:10', lag_seconds: 80650, is_trading: false };
    await p.evaluate(() => loadRefreshStatus());
    await sleep(800);
    tb = await topbar(p);
    console.log('B_CLOSED=' + JSON.stringify(tb));
    assert('B7 休市 → 文案含「休市」', /休市/.test(tb.marketText || ''), tb.marketText);
    assert('B8 休市 → 中性点(rs-never)', /rs-never/.test(tb.marketDot || ''), tb.marketDot);

    // B9 盘中快照口径的榜单徽标
    await p.evaluate(() => { location.hash = '#money/moneyflow'; });
    await sleep(2000);
    const rt = await p.$('#p-moneyflow [data-mf-mode="rank"]');
    if (rt) { await rt.click(); await sleep(2500); }
    const mf = await p.evaluate(() => {
      const b = document.querySelector('#p-moneyflow .dd-bar .dd-badge');
      return b ? { text: b.textContent.replace(/\s+/g, ' ').trim(), cls: b.className } : null;
    });
    console.log('B_RANK_INTRADAY=' + JSON.stringify(mf));
    assert('B9 is_intraday=true → 榜单标「盘中快照 · 更新于 13:19」', /盘中快照/.test(mf && mf.text || '') && /13:19/.test(mf && mf.text || ''), mf && mf.text);
    assert('B10 盘中快照用 accent 色（非 dd-past）', /dd-intraday/.test(mf && mf.cls || ''), mf && mf.cls);
    await p.screenshot({ path: path.join(OUT, 'fresh_moneyflow_rank_intraday.png') });
    await p.close();
  }

  await browser.close();

  console.log('\n===== ASSERTIONS =====');
  let fail = 0;
  for (const r of results) { if (!r.pass) fail++; console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  <= ' + r.detail : ''}`); }
  const uncaught = errors.filter((e) => e.kind === 'pageerror');
  console.log(`\nTOTAL=${results.length} FAIL=${fail}`);
  console.log('UNCAUGHT_EXCEPTIONS=' + uncaught.length);
  if (uncaught.length) console.log(JSON.stringify(uncaught, null, 2));
  if (errors.length) console.log('CONSOLE_ERRORS=' + JSON.stringify(errors.slice(0, 20)));
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
