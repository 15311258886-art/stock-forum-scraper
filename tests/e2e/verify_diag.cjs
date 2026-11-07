/* 个股诊断（合并「估值诊股 + 个股资金流」）无头回归
 * 驱动已运行的 8765 服务，验证 5 段渲染 + 专家卡 + 零控制台报错。
 * 用法：NODE_PATH=<node 工作区>/node_modules node verify_diag.cjs
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer');

const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const BASE = 'http://127.0.0.1:8765';

const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); console.log((cond ? '  PASS ' : '  FAIL ') + msg); };

(async () => {
  // 必须清代理，否则 localhost 会被路由到失效代理而连不上
  ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY'].forEach(k => delete process.env[k]);

  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });
  const page = await browser.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => pageErrors.push(String(e)));

  try {
    console.log('== 打开 ' + BASE + ' ==');
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#sidenav .nav-item', { timeout: 15000 });

    // 1) 左栏点「我的票」
    console.log('== 点左栏 我的票 ==');
    await page.click('#sidenav .nav-item[data-cat="watch"]');
    await page.waitForSelector('#subnav .sub-tab[data-tab="diag"]', { timeout: 8000 });
    const TAB_CLICKED = await page.$eval('#subnav .sub-tab[data-tab="diag"]', (a) => a.textContent.trim());
    console.log('  TAB_CLICKED = ' + TAB_CLICKED);
    ok(TAB_CLICKED.includes('个股诊断'), '子页签出现「个股诊断」');

    // 2) 点「个股诊断」进入面板
    await page.click('#subnav .sub-tab[data-tab="diag"]');
    await page.waitForSelector('#p-diag.active', { timeout: 8000 });
    ok(true, '面板 #p-diag 被激活');
    const hasInput = await page.$('#dgSym');
    ok(!!hasInput, '诊断表单输入框 #dgSym 存在');

    // 3) 输入代码并诊断
    console.log('== 输入 600519 并点开始诊断 ==');
    await page.click('#dgSym', { clickCount: 3 });
    await page.type('#dgSym', '600519');
    await page.click('#dgGo');

    // 4) 等专家卡出现（首次约 2–4s，命中缓存后秒回）
    await page.waitForSelector('#p-diag .dg-expert', { timeout: 30000 });
    console.log('== 诊断结果渲染完成，开始断言 ==');

    const r = await page.evaluate(() => {
      const q = (s) => document.querySelector(s);
      const qa = (s) => document.querySelectorAll(s);
      const h3s = [...qa('#p-diag .dg-sec > h3')].map((h) => h.textContent.trim());
      const scoreTxt = (q('#p-diag .dg-score') || {}).textContent || '';
      return {
        hostLen: (q('#p-diag') || {}).innerHTML ? q('#p-diag').innerHTML.length : 0,
        expertExists: !!q('#p-diag .dg-expert'),
        scoreNum: parseFloat(scoreTxt),
        act: (q('#p-diag .dg-act') || {}).textContent || '',
        horCount: qa('#p-diag .dg-hor > div').length,
        reasons: qa('#p-diag .dg-reasons li').length,
        h3s,
        featScore: !!q('#p-diag .feat-score'),
        featKpis: !!q('#p-diag .feat-kpis'),
        finTb: !!q('#p-diag .dg-fin-tb'),
        org: !!q('#p-diag .dg-org'),
        annItems: qa('#p-diag .dg-ann li').length,
        strip: !!q('#p-diag .dg-strip'),
      };
    });

    ok(r.hostLen > 500, '#p-diag 正文已渲染（长度 ' + r.hostLen + '）');
    ok(r.expertExists, '专家综合分析卡存在');
    ok(!isNaN(r.scoreNum) && r.scoreNum > 0, '专家评分是数字：' + r.scoreNum);
    ok((r.act || '').length > 0, '操作倾向文案非空：' + r.act);
    ok(r.horCount === 3, '短/中/长期三段齐全（' + r.horCount + '）');
    ok(r.reasons >= 1, '专家依据条数 >=1（' + r.reasons + '）');
    ok(r.h3s.includes('估值评分'), '段·估值评分 存在');
    ok(r.h3s.includes('资金流向（主力口径）'), '段·资金流向 存在');
    ok(r.h3s.includes('财务指标'), '段·财务指标 存在');
    ok(r.h3s.includes('公司概况'), '段·公司概况 存在');
    ok(r.h3s.includes('最近公告'), '段·最近公告 存在');
    ok(r.featScore, '估值评分块 .feat-score 渲染');
    ok(r.featKpis, '资金流向 KPI 块 .feat-kpis 渲染');
    ok(r.finTb, '财务指标表 .dg-fin-tb 渲染');
    ok(r.org, '公司概况块 .dg-org 渲染');
    ok(r.annItems >= 1, '最近公告列表 >=1 条（' + r.annItems + '）');
    ok(r.strip, '头部行情条 .dg-strip 渲染');

    ok(pageErrors.length === 0, '页面 JS 异常数 = ' + pageErrors.length);
    ok(consoleErrors.length === 0, '控制台 error 数 = ' + consoleErrors.length);
    if (pageErrors.length) console.log('  PAGEERRORS:', pageErrors.slice(0, 5));
    if (consoleErrors.length) console.log('  CONSOLE:', consoleErrors.slice(0, 5));
  } catch (e) {
    fails.push('EXCEPTION: ' + e.message);
    console.log('  EXCEPTION ' + e.message);
  } finally {
    await browser.close();
  }

  console.log('\n==== 结果：' + (fails.length ? ('FAIL (' + fails.length + ' 项)') : 'ALL PASS') + ' ====');
  process.exit(fails.length ? 1 : 0);
})();
