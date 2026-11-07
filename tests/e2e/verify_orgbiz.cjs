/* 回归验证：个股详情「公司主营」条（2026-09-26）
   王总原话：「所有类似于这种就是点开之后，股票信息的里边儿有这个股票代码、简称、重要信息的这种的，
             包括这个 K 线图，在这个重要信息的这个板块，这个后面或者下面加一点点。什么东西呢？
             就是这个公司主营业务是干什么的？简对公司的主营业务和包括干什么做一个简单介绍。
             这个东西要好吧，所有的这些都需要，所以你现在把这个所有的这个对于单股信息介绍的，把这一条都加上。」

   验收点：
   ① 接口 /api/quote 的 org：抽样 8 只不同行业标的，main_business / profile / industry 均非空；
   ② 文本归一（无换行/NBSP/双空格）+ 行业双口径 + source 标注；
   ③ 主营两种形态都工作：列表型（项数 3~14 且单项 ≤14 字）铺标签、句子型回落原文；
   ④ 覆盖率：库内 A 股标的已落库公司概况比例 ≥ 95%（stock_org 表）；
   ⑤ 前端（真实点击）：抽屉「重要信息」板块末尾出现「公司主营」——主营 / 所属行业 / 公司简介齐全；
      长简介（招行 964 字）默认折叠且可展开可收拢；短简介（宁德时代 83 字）不出按钮、全文直接可见
      〔护栏：不足两三行还挂「展开全文」，点了没反应＝像坏了〕；
   ⑥ 连点 3 只不同标的都能看到该块 —— 兑现「所有的都需要」；
   ⑦ 控制台零报错。
   用法：node verify_orgbiz.cjs   （截图 _orgbiz_shot.png / _orgbiz_expand.png / _orgbiz_full.png）
*/
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8799;
const SHOT = path.join(BASE, '_orgbiz_shot.png');
const SHOT2 = path.join(BASE, '_orgbiz_expand.png');
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
  results.push([name, !!pass, extra === undefined ? '' : String(extra)]);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '  → ' + extra : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitPort() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`${ROOT}/api/summary`); if (r.ok) return true; } catch (e) { /* 未就绪 */ }
    await sleep(500);
  }
  return false;
}

// 与 app.js siBizHtml 完全同规则（node 侧复算，用于交叉核对前端渲染结果）
const splitMain = (t) => String(t || '').split(/[,，、;；]/)
  .map((s) => s.trim().replace(/[。.；;,，]+$/, '').trim()).filter(Boolean);
const isListLike = (t) => {
  const p = splitMain(t);
  return p.length >= 3 && p.length <= 14 && p.every((x) => x.length <= 14);
};

const SAMPLES = [
  ['600519', '贵州茅台'], ['300750', '宁德时代'], ['600036', '招商银行'], ['601688', '华泰证券'],
  ['300122', '智飞生物'], ['002594', '比亚迪'], ['601318', '中国平安'], ['300059', '东方财富'],
];

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪：\n' + srvLog.slice(-1500));

    /* ---------- ① ② ③ 接口层 ---------- */
    const q = {};
    let miss = [];
    for (const [code, nm] of SAMPLES) {
      const d = await (await fetch(`${ROOT}/api/quote?symbol=${code}`)).json();
      q[code] = d;
      const o = d.org;
      if (!(o && o.main_business && o.profile && o.industry)) miss.push(`${code}(${nm})`);
      ok(`① ${code} ${nm} 公司概况齐全`, !!(o && o.main_business && o.profile && o.industry),
        o ? `主营「${o.main_business.slice(0, 22)}…」${o.main_business.length}字 / 简介${o.profile.length}字 / 行业=${o.industry}` : 'org=null');
    }
    ok('① 抽样 8 只全部命中（跨行业）', miss.length === 0, miss.join(' '));

    const o1 = (q['600519'] || {}).org || {};
    ok('② 主营/简介已归一（无换行 / NBSP / 双空格）',
      !/[\n\r\xa0\u3000]|\s{2,}/.test(o1.main_business || '') && !/[\n\r\xa0\u3000]|\s{2,}/.test(o1.profile || ''),
      JSON.stringify((o1.main_business || '').slice(0, 26)));
    ok('② 含行业双口径（东财 + 证监会）', !!o1.industry && !!o1.industry_csrc,
      `${o1.industry} | ${o1.industry_csrc}`);
    ok('② 接口 source 标注含「F10 公司概况」', /F10 公司概况/.test(q['600519'].source || ''),
      q['600519'].source);

    const chipsList = SAMPLES.filter(([c]) => isListLike((q[c].org || {}).main_business));
    const plainList = SAMPLES.filter(([c]) => !isListLike((q[c].org || {}).main_business));
    ok('③ 两种主营形态均被覆盖（列表型 / 句子型）',
      chipsList.length >= 1 && plainList.length >= 1,
      `列表型=${chipsList.map((x) => x[1]).join(',') || '无'} | 句子型=${plainList.map((x) => x[1]).join(',') || '无'}`);
    ok('③ 句子型标的不会被硬拆（2 项也算句子）',
      !isListLike('计算产业、系统装备及其他业务') && !isListLike('各类乳品及健康饮品的生产与销售'),
      '中国长城 / 伊利股份');
    // 前端样本要用到的另两只，补抓接口值用于交叉核对
    for (const c of ['000858', '000001']) q[c] = await (await fetch(`${ROOT}/api/quote?symbol=${c}`)).json();

    /* ---------- ④ 覆盖率 ---------- */
    const pySnip = [
      'import sqlite3',
      "c=sqlite3.connect('viewer/app.db')",
      "rows=c.execute(\"SELECT DISTINCT symbol FROM (SELECT symbol FROM stock_mentions UNION SELECT symbol FROM trades UNION SELECT symbol FROM positions)\").fetchall()",
      "codes=[r[0] for r in rows if r[0] and len(r[0])==6 and r[0].isdigit()]",
      "have={r[0] for r in c.execute('SELECT code FROM stock_org')}",
      "print(len([x for x in codes if x in have]), len(codes))",
    ].join('\n');
    let covHit = 0, covAll = 0;
    try {
      const out = execFileSync(path.join(BASE, '.venv', 'Scripts', 'python.exe'), ['-c', pySnip],
        { cwd: BASE, timeout: 30000 }).toString().trim().split(/\s+/);
      covHit = parseInt(out[0], 10); covAll = parseInt(out[1], 10);
    } catch (e) { /* 取不到不阻塞其余断言 */ }
    const covPct = covAll ? Math.round((covHit / covAll) * 1000) / 10 : 0;
    ok('④ 公司概况覆盖率 ≥ 95%（库内 A 股标的）', covAll > 0 && covPct >= 95, `${covHit}/${covAll} = ${covPct}%`);

    /* ---------- ⑤ ⑥ 前端：真实点击 ---------- */
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
    await page.waitForSelector('.nav-item[data-cat="research"]', { timeout: 30000 });
    await page.click('.nav-item[data-cat="research"]');
    await page.waitForSelector('.sub-tab[data-tab="stocks"]', { timeout: 15000 });
    await page.click('.sub-tab[data-tab="stocks"]');
    await page.waitForSelector('#p-stocks .stk-card', { timeout: 30000 });

    const hows = [];
    const openSym = async (sym) => {
      await page.keyboard.press('Escape');
      await sleep(250);
      const how = await page.evaluate((s) => {
        const el = document.querySelector(`#p-stocks .stk-card[data-sym="${s}"]`);
        if (el) { el.click(); return 'card'; }                    // 优先真实点击卡片
        const a = document.querySelector('#p-stocks .stk-card') || document.body;
        if (typeof window.openStock === 'function') { window.openStock(s, a); return 'api'; }
        return 'none';
      }, sym);
      hows.push([sym, how]);
      await page.waitForSelector('#drawer.show', { timeout: 20000 });
      await page.waitForSelector('#drawer #stkQuote .si-biz', { timeout: 45000 });
      return how;
    };

    const readBiz = () => page.evaluate(() => {
      const box = document.querySelector('#drawer #stkQuote');
      if (!box) return null;
      const secTitles = [...box.querySelectorAll('.si-sec')].map((e) => e.textContent.trim());
      const biz = box.querySelector('.si-biz');
      if (!biz) return { hasBiz: false, secTitles };
      const rows = [...biz.querySelectorAll('.si-biz-row')];
      const rowOf = (k) => {
        const r = rows.find((x) => (x.querySelector('.si-biz-k') || {}).textContent === k);
        return r ? r.querySelector('.si-biz-v') : null;
      };
      const cell = (k) => { const c = rowOf(k); return c ? c.textContent.replace(/\s+/g, ' ').trim() : ''; };
      const prof = biz.querySelector('#siBizProf');
      const mainT = biz.querySelector('#siBizMain');
      const btns = [...biz.querySelectorAll('.si-biz-toggle')].map((b) => ({
        id: b.dataset.bizToggle, text: b.textContent.trim() }));
      const boxOf = (el) => el ? { clamp: el.classList.contains('clamp'), ch: el.clientHeight, sh: el.scrollHeight } : null;
      return {
        hasBiz: true, secTitles,
        title: secTitles.find((s) => s.includes('公司主营')) || '',
        rowN: rows.length,
        main: cell('主营业务'),
        chips: [...biz.querySelectorAll('.si-biz-chip')].map((c) => c.textContent.trim()),
        hasMainClamp: !!mainT,
        ind: cell('所属行业'),
        profText: prof ? prof.textContent.trim() : '',
        prof: boxOf(prof), mainEl: boxOf(mainT), btns,
        rawNewline: /[\n\r]/.test((prof ? prof.textContent : '') + (mainT ? mainT.textContent : '')),
      };
    });

    /* 第一只：列表型标的（华泰证券，主营 4 项短语） */
    const howList = await openSym('601688');
    const bl = await readBiz();
    ok('⑤ 抽屉「重要信息」末尾出现「公司主营」区块',
      !!(bl && bl.hasBiz) && /公司主营/.test(bl.title || ''),
      `开抽屉方式=${howList} 区块标题区=${(bl && bl.secTitles || []).join(' / ').slice(0, 96)}`);
    ok('⑤ 三行齐全（主营 / 所属行业 / 公司简介）', !!bl && bl.rowN === 3, `rows=${bl && bl.rowN}`);
    const expChips = splitMain((q['601688'].org || {}).main_business);
    ok('⑤ 列表型主营渲染为标签，且与接口拆分一致',
      !!bl && bl.chips.length === expChips.length && bl.chips.every((c, i) => c === expChips[i]),
      `页面 ${bl && bl.chips.length} 个 = 接口 ${expChips.length} 个 → ${(bl && bl.chips || []).slice(0, 5).join('|')}`);

    /* 第二只：句子型标的（五粮液「白酒生产和销售」） */
    await openSym('000858');
    const bp = await readBiz();
    const expPlain = ((q['000858'] || {}).org || {}).main_business || '';
    ok('⑤ 句子型主营回落原文（不硬拆成标签）',
      !!bp && bp.chips.length === 0 && expPlain.length > 3 && expPlain.startsWith(bp.main.slice(0, 8)),
      `chips=${bp && bp.chips.length} 原文=「${bp && bp.main}」 接口=「${expPlain}」`);

    /* 第三只：长简介（招商银行 964 字） */
    await openSym('600036');
    const blong = await readBiz();
    const profBtn0 = (blong.btns || []).find((b) => b.id === 'siBizProf');
    ok('⑤ 长简介默认折叠（clamp 生效 + 出现展开按钮）',
      !!blong && blong.prof && blong.prof.clamp === true && blong.prof.ch < blong.prof.sh && !!profBtn0,
      `client=${blong.prof && blong.prof.ch} scroll=${blong.prof && blong.prof.sh} 按钮=${profBtn0 && profBtn0.text}`);
    await page.screenshot({ path: SHOT, fullPage: false });

    await page.click('#drawer [data-biz-toggle="siBizProf"]');
    await sleep(350);
    const blong2 = await readBiz();
    const profBtn1 = (blong2.btns || []).find((b) => b.id === 'siBizProf');
    ok('⑤ 点「展开全文」真的展开（可视高度变大 + 文案变收起）',
      !!blong2 && blong2.prof.clamp === false && blong2.prof.ch > blong.prof.ch && profBtn1.text === '收起',
      `client ${blong.prof.ch} → ${blong2.prof.ch}，按钮=${profBtn1 && profBtn1.text}`);
    await page.screenshot({ path: SHOT2, fullPage: false });
    await page.click('#drawer [data-biz-toggle="siBizProf"]');
    await sleep(300);
    const blong3 = await readBiz();
    ok('⑤ 再次点击可收拢', !!blong3 && blong3.prof.clamp === true
      && (blong3.btns.find((b) => b.id === 'siBizProf') || {}).text === '展开全文',
      `按钮=${(blong3.btns.find((b) => b.id === 'siBizProf') || {}).text}`);

    /* 第四只：短简介（宁德时代 83 字）—— 不该出现点了没反应的「展开全文」 */
    await openSym('300750');
    const bshort = await readBiz();
    const profBtnS = (bshort.btns || []).find((b) => b.id === 'siBizProf');
    ok('⑤ 短简介不出「展开全文」按钮（避免点了没反应的假按钮）',
      !!bshort && !profBtnS && bshort.prof && bshort.prof.clamp === false,
      `简介${bshort.profText.length}字 client=${bshort.prof && bshort.prof.ch} scroll=${bshort.prof && bshort.prof.sh} 按钮=${profBtnS ? profBtnS.text : '无'}`);
    ok('⑤ 简介与主营文本无硬换行', !!bshort && !bshort.rawNewline, `简介「${(bshort.profText || '').slice(0, 24)}…」`);

    /* ⑥ 连点 3 只不同标的 */
    const seen = [];
    for (const sym of ['600519', '601318', '000001']) {
      await openSym(sym);
      const b = await readBiz();
      const good = !!(b && b.hasBiz && b.main.length >= 4 && b.profText.length > 20 && b.ind.length >= 4);
      seen.push(`${sym}:${good ? 'OK' : 'MISS'}`);
      ok(`⑥ ${sym} 也带公司主营`, good,
        b ? `主营${b.main.length}字/标签${b.chips.length}个 行业=${b.ind} 简介${b.profText.length}字` : 'no .si-biz');
    }
    ok('⑥ 连点多只全部命中（兑现「所有的都需要」）', seen.every((x) => x.endsWith('OK')), seen.join(' '));
    const cardN = hows.filter(([, h]) => h === 'card').length;
    ok('⑥ 至少 2 次经真实点击卡片打开（非脚本直调）', cardN >= 2,
      hows.map(([s, h]) => `${s}:${h}`).join(' '));

    await page.screenshot({ path: path.join(BASE, '_orgbiz_full.png'), fullPage: true });

    const realErrs = errs.filter((t) => !/favicon|net::ERR_/i.test(t));
    ok('⑦ 控制台零报错', realErrs.length === 0, realErrs.slice(0, 3).join(' ;; '));
  } catch (e) {
    ok('执行异常', false, e.message);
  } finally {
    if (browser) { try { await browser.close(); } catch (e) { /* ignore */ } }
    srv.kill();
  }
  const bad = results.filter((r) => !r[1]);
  console.log(`\n===== ${results.length - bad.length}/${results.length} PASS =====`);
  if (bad.length) { console.log('失败项：' + bad.map((b) => b[0]).join(' / ')); process.exitCode = 1; }
  setTimeout(() => process.exit(process.exitCode || 0), 300);
})();
