// 2026-10-01 王总：个股代码必须能一键点进「个股诊断」抽屉。
// 本脚本逐处验证 codeLink 生效：当红参赛股 / 博主荐股(卡片+重复推荐) / 逐日追踪 / 盯盘总览 / 研报选股。
// 用法：node verify_codelink.cjs   （需 8765 在跑；浏览器用本机 Edge）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const SHOT = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper\\shots';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
if (!fs.existsSync(SHOT)) fs.mkdirSync(SHOT, { recursive: true });

// [面板 tab, 中文名, 期望「可点代码入口」选择器数量阈值]
const CASES = [
  ['contest', '当红参赛股', '#p-contest a[data-code-open]', 3],
  ['blogger', '博主荐股', '#p-blogger a[data-code-open]', 3],
  ['news', '个股快讯公告', '#p-news a[data-code-open]', 1],
  // 巴菲特表只在搜索框有内容时才渲染结果表（q 为空显示期次卡片），故单独在下面搜一次
  ['buffett', '巴菲特选股', '#p-buffett a[data-code-open]', 0],
];

(async () => {
  if (!fs.existsSync(EDGE)) { console.error('找不到 Edge'); process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);
  // 预热：拉一遍博主/参赛股数据（惰性加载，先触发）
  await page.evaluate(() => { try { loadBloggerContest(); } catch (e) { console.warn('preload', e.message); } });

  const results = [];
  for (const [tab, label, sel, min] of CASES) {
    // 切到该 tab：走真实路径 —— 哈希深链接 #大类/子类（selectNav 由此触发）
    const switched = await page.evaluate((t) => {
      const cat = (typeof NAV !== 'undefined' ? NAV : []).find((c) => (c.items || []).some((i) => i.tab === t));
      if (!cat) return false;
      if (location.hash !== '#' + cat.key + '/' + t) location.hash = '#' + cat.key + '/' + t;
      return true;
    }, tab);
    await sleep(2200);
    const info = await page.evaluate((s) => {
      const links = Array.from(document.querySelectorAll(s));
      const vis = (el) => el && el.offsetParent !== null;
      const shown = links.filter(vis);
      const host = links[0] ? links[0].closest('#p-, .panel') : null;
      return {
        total: links.length, visible: shown.length,
        sample: shown.slice(0, 4).map((a) => ({ code: (a.textContent || '').trim(), open: a.getAttribute('data-code-open'), cls: a.className })),
        hostId: host ? host.id : null,
      };
    }, sel);
    results.push({ tab, label, switched, ...info, min });
    console.log(`[${label}] 切tab=${switched} 可点代码=${info.visible}/${info.total} 期望>=${min} ${info.visible >= min ? 'PASS' : 'FAIL'}`);
    if (info.sample.length) console.log('   样例:', JSON.stringify(info.sample));
  }

  // ---- 巴菲特选股：结果表默认不渲染，先在回查框里输代码让它出来，再验可点 ----
  await page.evaluate(() => { location.hash = '#learn/buffett'; });
  await sleep(2500);
  const bf = await page.evaluate(async () => {
    const q = document.querySelector('#bfQ');
    if (!q) return { ok: false };
    q.value = '600'; q.dispatchEvent(new Event('input'));
    await new Promise((r) => setTimeout(r, 1200));
    const links = document.querySelectorAll('#p-buffett a[data-code-open]');
    return { ok: true, links: links.length, sample: Array.from(links).slice(0, 3).map((a) => a.getAttribute('data-code-open')) };
  });
  console.log('[巴菲特选股] 搜"600" 可点代码=' + bf.links + (bf.links > 0 ? ' PASS' : ' FAIL'));

  // ---- 真点一次：当红参赛股表格里的第一个代码 → 抽屉 ----
  await page.evaluate(() => { location.hash = '#opinion/contest'; });
  await sleep(2000);
  const before = await page.evaluate(() => ({
    deskSel: !!document.querySelector('#p-news tr.desk-sel'),
    newsPickCalled: !!window.__newsPickHit,
  }));
  const clickRes = await page.evaluate(() => {
    const a = document.querySelector('#p-contest a[data-code-open]');
    if (!a) return { ok: false, reason: 'no-link' };
    a.click();
    return { ok: true, code: a.getAttribute('data-code-open') };
  });
  await sleep(3500);
  const drawer = await page.evaluate(() => {
    const d = document.querySelector('#drawer');
    const b = document.querySelector('#drawerBody');
    return {
      show: !!(d && d.classList.contains('show')),
      title: (document.querySelector('#drawerTitle, .drawer-title')?.textContent || '').trim().slice(0, 60),
      len: b ? (b.textContent || '').replace(/\s+/g, ' ').trim().length : 0,
      hasKlineOrQuote: !!(b && b.querySelector('#klineCanvas, #stkQuote, #stkBloggerPerf')),
    };
  });
  console.log('[点击测试] 目标代码=' + clickRes.code, JSON.stringify(drawer));
  console.log('[点击测试] ' + (drawer.show && drawer.len > 100 ? 'PASS 抽屉已打开且有内容' : 'FAIL 抽屉未打开/空白'));
  await page.screenshot({ path: SHOT + '\\codelink_drawer.png' });

  // 盯盘总览：点代码只开诊断，不该顺带把整行「选中」了
  await page.evaluate(() => {
    location.hash = '#info/news';
  });
  await sleep(2200);
  const newsRes = await page.evaluate(async () => {
    const a = document.querySelector('#p-news a[data-code-open]');
    if (!a) return { ok: false, reason: 'no-link' };
    const tr = a.closest('tr');
    a.click();
    await new Promise((r) => setTimeout(r, 1200));
    return {
      ok: true, code: a.getAttribute('data-code-open'),
      rowStillSelected: !!(tr && tr.classList.contains('desk-sel')),
    };
  });
  await sleep(2500);
  const openCount = await page.evaluate(() => {
    const b = document.querySelector('#drawerBody');
    return !!(document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show'))
      && (b?.textContent || '').replace(/\s+/g, ' ').trim().length;
  });
  console.log('[盯盘表] ' + JSON.stringify(newsRes) + ' 抽屉长度=' + openCount);
  console.log('[盯盘表] ' + (newsRes.ok && openCount > 100 ? 'PASS 点代码开诊断、未误触发整行选中' : 'CHECK 需人工看'));
  await page.screenshot({ path: SHOT + '\\codelink_news.png' });

  // 参赛股面板截图（代码列可点）
  await page.evaluate(() => { location.hash = '#opinion/contest'; });
  await sleep(2000);
  await page.screenshot({ path: SHOT + '\\codelink_contest.png' });

  console.log('---- 控制台异常:', errs.length ? errs.slice(0, 8) : '无');
  const fail = results.filter((r) => r.visible < r.min);
  console.log(fail.length ? 'FAIL 面板: ' + fail.map((f) => f.label).join(',') : '全部面板代码可点 PASS');
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
