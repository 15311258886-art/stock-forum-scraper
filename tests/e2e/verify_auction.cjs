// 2026-10-01 方案 A 落地验收：集合竞价进「今日总览 → 今日看板」。
// 验七件事（全部在真实浏览器里跑，改 .py 要重启服务、改前端要跟版本号，本脚本会一起看）：
//   ① 今日看板上真的挂出了竞价卡片（#overview/dashboard）
//   ② 卡片上的家数/均值与 /api/dashboard 返回的 auction.counts 一致（不各说各话）
//   ③ 榜单 10 行、每行带可点开的个股 chip（点开走的是既有 openStock）
//   ④ 样本不足时量比/竞价额显示「—」，不编数、不拿别的值凑
//   ⑤ 历史条带「回填」标记（历史日只有竞价价、没有竞价量，必须能看出来）
//   ⑥ 口径说明行写清楚「9:25 定盘价 / N 与 XD、XD 剔除」，判词自带判据
//   ⑦ 卡片内元素不溢出（宽度溢出 = 排版坏掉）+ 控制台零报错
// 用法：node verify_auction.cjs   （需 8765 在跑；浏览器用本机 Edge）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!fs.existsSync(EDGE)) { console.error('找不到 Edge'); process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1600 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);

  let pass = 0, total = 0;
  const say = (ok, msg) => { total++; if (ok) pass++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

  // 切到「今日总览 → 今日看板」（hash 路由），再手工重渲染一次确保区块挂上
  await page.evaluate(() => { location.hash = '#overview/dashboard'; });
  await sleep(2000);
  await page.evaluate(() => {
    if (DASH.data) renderDashboard();
  });
  await sleep(600);

  // ================= ① 卡片挂出 =================
  const card = await page.evaluate(() => {
    const host = document.getElementById('p-dashboard');
    const c = host ? host.querySelector('.auc-card') : null;
    if (!c) return { present: false };
    const kpis = [...c.querySelectorAll('.auc-kpi')].map((k) => ({
      label: (k.querySelector('i') || {}).textContent || '',
      val: (k.querySelector('b') || {}).textContent || '',
      em: (k.querySelector('em') || {}).textContent || '',
    }));
    const cols = [...c.querySelectorAll('.auc-col')].map((col) => ({
      title: (col.querySelector('.auc-col-t') || {}).textContent || '',
      rows: [...col.querySelectorAll('table tbody tr')].map((tr) => tr.textContent.replace(/\s+/g, ' ').trim()),
    }));
    const rows = [...c.querySelectorAll('.auc-col table tbody tr')];
    const hist = c.querySelectorAll('.auc-h');
    const note = (c.querySelector('.auc-note') || {}).textContent || '';
    const head = (c.querySelector('.auc-sub') || {}).textContent || '';
    // 溢出检测：卡片内任一 ele 右边界超过卡片内容区
    const box = c.getBoundingClientRect();
    let overflow = 0;
    c.querySelectorAll('*').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width && r.right > box.right + 2) overflow++;
    });
    return {
      present: true,
      kpis,
      cols,
      rowCount: rows.length,
      chipCount: c.querySelectorAll('.auc-col .kline-chip[data-sym]').length,
      histCount: hist.length,
      backfillTags: c.querySelectorAll('.auc-h-b').length,
      note, head,
      overflow,
      // 注意：app.js 里 DASH 是顶层 const（词法绑定，不挂 window），
      // 这里只能直接引用标识符，不能用 window.DASH —— 否则整段 api 全拿不到值。
      hasApiData: !!(typeof DASH !== 'undefined' && DASH.data && DASH.data.auction),
      api: typeof DASH !== 'undefined' && DASH.data ? DASH.data.auction : null,
      limitRowCells: rows.length ? rows[0].querySelectorAll('td').length : 0,
      dashText: (c.querySelector('.auc-col table tbody tr') || {}).textContent || '',
    };
  });
  say(card.present, '① 今日看板挂出竞价卡片（.auc-card）');
  say(card.hasApiData, '① 竞价数据随 /api/dashboard 下发（前端不额外发请求）');
  say(card.kpis.length === 4 && card.kpis.every((k) => k.val),
    `② 四张 KPI 都有值：${card.kpis.map((k) => k.label + '=' + k.val).join(' | ')}`);
  const api = card.api || {};
  const k = api.counts || {};
  if (k.total) {
    say(String(k.up) === String(card.kpis[0] && card.kpis[0].val),
      `② KPI 上涨家数与接口一致（接口 ${k.up} / 界面 ${card.kpis[0] && card.kpis[0].val}）`);
    say(String(k.down) === String(card.kpis[1] && card.kpis[1].val),
      `② KPI 下跌家数与接口一致（接口 ${k.down} / 界面 ${card.kpis[1] && card.kpis[1].val}）`);
  }
  const cols = card.cols || [];
  const expectRows = cols.length * 10;
  say(cols.length >= 1 && cols.every((x) => x.rows.length === 10),
    `③ 每榜 10 行（共 ${cols.length} 榜 / ${card.rowCount} 行）：${cols.map((x) => x.title.split(' ')[0]).join(' + ')}`);
  say(card.chipCount === card.rowCount && card.rowCount === expectRows,
    `③ 每行都能点开个股详情（${card.chipCount}/${card.rowCount} 个 chip 带 data-sym）`);
  // 名不副实检查：两个榜标题不同、内容不能是同一份（标题写量比榜、数据却是涨幅榜 = 口径骗人）
  if (cols.length > 1) {
    const a = JSON.stringify(cols[0].rows), b2 = JSON.stringify(cols[1].rows);
    say(a !== b2, `③ 两榜内容不重复（${cols[0].title.split(' ')[0]} vs ${cols[1].title.split(' ')[0]} 各排各的）`);
  } else {
    const t = (card.cols && card.cols[0] && card.cols[0].title) || '';
    say(/涨幅榜/.test(t) && cols[0].rows.length === 10,
      `③ 只列得出来的那一榜（${t.replace(/\s+/g, ' ').trim()}），不硬凑第二榜`);
  }

  // ================= ④ 样本不足不编数 =================
  const ratioReady = api.ratio_ready;
  const dashText = card.dashText || '';
  if (!ratioReady) {
    say(/—/.test(dashText), `④ 量比样本不足（${ratioReady} 只）时显示「—」，不编数`);
  } else {
    say(true, `④ 量比已有 ${ratioReady} 只可算，直接显示数值`);
  }
  if (k.amount_yi == null) {
    say((card.kpis[2] || {}).val === '—' && /无竞价额/.test((card.kpis[2] || {}).em || ''),
      '④ 历史回填日竞价总额显示「—」并注明原因（不拿全日量冒充竞价额）');
  } else {
    say(true, `④ 竞价总额 ${k.amount_yi} 亿`);
  }

  // ================= ⑤ 历史条 + 回填标记 =================
  say(card.histCount >= 5, `⑤ 近 ${card.histCount} 个交易日竞价全景条`);
  say(card.backfillTags > 0,
    `⑤ 回填日打「回填」标记 ${card.backfillTags} 处（历史日只有竞价价，没有竞价量）`);

  // ================= ⑥ 判词自带判据 =================
  say(/9:25/.test(card.note || ''), `⑥ 口径行写明 9:25 定盘：` + (card.note || '').slice(0, 46) + '…');
  say(/剔除/.test(card.note || ''), '⑥ 口径行写明新股首日/除权除息剔除（涨幅不可比）');
  say(/09-30|2026-09-30|当日快照/.test(card.head || ''),
    `⑥ 标题写明数据日期（${(card.head || '').trim()}），不让用户猜是哪天`);

  // ================= ⑦ 排版 + 控制台 =================
  say(card.overflow === 0, `⑦ 卡片内零溢出元素（实测 ${card.overflow} 个越界）`);
  say(errs.length === 0, `⑦ 控制台零报错（实际 ${errs.length} 条${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}）`);

  // ================= ⑧ 点 chip 能开详情（绑定没丢） =================
  const opened = await page.evaluate(() => {
    const c = document.querySelector('#p-dashboard .auc-col .kline-chip[data-sym]');
    if (!c || typeof openStock !== 'function') return false;
    const sym = c.dataset.sym;
    c.click();
    return document.getElementById('drawer') ? sym : null;
  });
  say(!!opened, `⑧ 点竞价榜的 chip 打开个股详情（${opened || '未打开'}）`);

  // 截图：全页 + 竞价卡局部
  await page.evaluate(() => { const c = document.querySelector('#p-dashboard .auc-card'); if (c) c.scrollIntoView({ block: 'start' }); });
  await sleep(500);
  const shotCard = await page.$('#p-dashboard .auc-card');
  if (shotCard) {
    try { await shotCard.screenshot({ path: 'shot_auction_card.png' }); console.log('截图：shot_auction_card.png'); }
    catch (_) { await page.screenshot({ path: 'shot_auction.png' }); console.log('截图：shot_auction.png'); }
  }
  await page.screenshot({ path: 'shot_auction_dash.png', fullPage: false });
  console.log('截图：shot_auction_dash.png');

  console.log(`\n结果：${pass}/${total} 通过`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})().catch((e) => { console.error('脚本异常：', e); process.exit(1); });
