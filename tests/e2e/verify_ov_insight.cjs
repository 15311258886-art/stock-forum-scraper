/* 回归验证：海外舆情（美股港股舆情）· 决策报告（2026-09-26）
   用户原话：「你给我的这些信息，我不知道有什么意义……他们应该关注他们都说了些什么，
             他们做了哪些动作？然后这个用能量视图、饼图、柱状图这种的，你给我表示一下，
             就是说他们这边现在比如说在 AI 方面放了多少钱，在金融上面放了多少钱……
             对于现在国内或者是其他的方向投资有什么建议，产生什么样的影响。」

   验收点：
   ① 接口 /api/overseas_sentiment.insight：四段数据齐全（话题榜 / 板块投向 / 事件动向 / 影响与建议）+ 口径；
   ② 诚实口径护栏：所有「权重」必须来自 heat（关注度），不得出现金额字样；代表标题优先真实源（非 example.local）；
      影响项不得混入「无显著…」占位；板块条形按热度单调递减且榜首=100；
   ③ 前端：真实点左栏（行情研判 → 美股港股舆情），四段标题齐全、环形图/条形图真渲染、
      代表个股可点且已绑定、原始情绪区折叠、口径可展开；控制台零报错。
   用法：node verify_ov_insight.cjs   （截图 _ov_insight_shot.png / _ov_insight_full.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8798;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_ov_insight_shot.png');
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
// 用项目自带运行时（含 pyyaml），临时端口起后端
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

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪：\n' + srvLog.slice(-1500));

    /* ---------- ① 接口层 ---------- */
    const d = await (await fetch(`${ROOT}/api/overseas_sentiment`)).json();
    const ins = d.insight || {};
    const topics = ins.topics || [], sectors = ins.sectors || [];
    ok('接口 insight 存在且四段键齐全', !!d.insight &&
      ['topics', 'sectors', 'moves', 'impacts', 'bull', 'bear', 'basis', 'verdict'].every((k) => k in ins),
      Object.keys(ins).join(','));
    ok('接口 话题榜非空且字段齐全', topics.length >= 5 && topics.every((t) => t.topic && t.n >= 1 && t.heat > 0 && t.share >= 0),
      topics.slice(0, 4).map((t) => `${t.topic}${t.share}%`).join(' / '));
    const shareSum = topics.reduce((s, t) => s + Number(t.share || 0), 0);
    ok('接口 话题占比合计≈100%', Math.abs(shareSum - 100) <= 1.5, shareSum.toFixed(1) + '%');
    ok('接口 前 3 主题都带真实标题', topics.slice(0, 3).every((t) => (t.headlines || []).length >= 1),
      topics.slice(0, 3).map((t) => `${t.topic}:${(t.headlines || []).length}`).join(' '));

    /* 诚实/质量护栏：代表标题优先真实源——热度第一的那条常常是种子数据（example.local），
       展示出来对用户没有参考价值，故取数时把示例源排到最后。 */
    const h0 = (topics[0] && topics[0].headlines && topics[0].headlines[0]) || {};
    ok('接口 代表标题优先真实源（非 example.local）',
      !!h0.url && !/example\.local/i.test(h0.url), `${h0.source || '-'} | ${(h0.url || '').slice(0, 52)}`);

    ok('接口 板块投向非空且按热度递减', sectors.length >= 8 &&
      sectors.every((s, i) => i === 0 || Number(sectors[i - 1].heat) >= Number(s.heat)),
      sectors.slice(0, 4).map((s) => `${s.sector}${s.heat}`).join(' '));
    ok('接口 板块条形榜首=100%（相对宽度）', sectors.length > 0 && Number(sectors[0].w) === 100, 'w=' + (sectors[0] || {}).w);

    const moves = ins.moves || [], impacts = ins.impacts || [];
    ok('接口 事件动向非空且字段齐全', moves.length >= 5 && moves.every((m) => m.title && m.direction && m.heat != null),
      `${moves.length} 条 | 首条方向：${(moves[0] || {}).direction}`);
    ok('接口 事件动向带关联海外标的', moves.some((m) => (m.symbols || []).length >= 1),
      (moves.find((m) => (m.symbols || []).length) || {}).symbols.join(',') || '无');

    /* 关键护栏：影响项不得把「无显著…」这类占位当成板块带进来（会污染主攻/回避结论） */
    const IMP_PLACEHOLDER = '无显著';
    const impBad = impacts.filter((h) => [...(h.sectors || []), ...(h.up || []).map((x) => x.sector), ...(h.down || []).map((x) => x.sector)]
      .some((s) => String(s || '').includes(IMP_PLACEHOLDER)))
      .concat([{ t: (ins.bull || []).join(','), d: (ins.bear || []).join(',') }].filter((x) => x.t.includes(IMP_PLACEHOLDER) || x.d.includes(IMP_PLACEHOLDER)));
    ok('接口 影响项已剔除「无显著…」占位', impBad.length === 0, impBad.slice(0, 2).map((x) => x.sector || x.t).join(';') || '干净');
    ok('接口 影响与建议非空且带结论或传导', impacts.length >= 5 && impacts.every((h) => (h.decision || '').length > 8 || (h.transmission || '').length > 8),
      `${impacts.length} 条 | 首条：${((impacts[0] || {}).decision || '').slice(0, 40)}`);
    ok('接口 主攻 / 回避方向非空', (ins.bull || []).length >= 4 && (ins.bear || []).length >= 2,
      `主攻 ${ins.bull.length}：${ins.bull.slice(0, 3).join('/')} ｜ 回避 ${ins.bear.length}：${ins.bear.slice(0, 2).join('/')}`);
    ok('接口 带口径说明（>=4 条，不藏算法）', Array.isArray(ins.basis) && ins.basis.length >= 4, (ins.basis || []).length + ' 条');

    /* 诚实口径护栏：全文不得承诺金额（库中无海外持仓 / 成交数据，只能用关注度加权） */
    const basisText = (ins.basis || []).join(' ');
    ok('口径明确声明「不是成交金额」', /不是成交金额|无海外真实持仓/.test(basisText), basisText.slice(0, 46));

    /* ---------- 中文化护栏（2026-09-26 用户反馈"卡片全是英文，看不懂"） ----------
       根因：lexicon.TRANSMISSION_TABLE 早期未配中文事件名，hotspots 生成时按
       `title = tr.get("title") or lead["title"]` 回落到**英文新闻原标题**当卡片标题。
       修复：lexicon.EVENT_TITLE_ZH 中文事件名 + 展示层兜底；英文原文落 title_en 供核对。 */
    const HAN = /[\u4e00-\u9fff]/;
    const hot = await (await fetch(`${ROOT}/api/overseas_hotspots`)).json();
    const enTitles = (hot || []).filter((h) => h.title && !HAN.test(h.title));
    ok('接口 热点卡标题已全部中文化', (hot || []).length >= 10 && enTitles.length === 0,
      `${(hot || []).length} 条 | 非中文 ${enTitles.length}：${enTitles.slice(0, 2).map((h) => (h.title || '').slice(0, 26)).join(' / ') || '无'}`);
    ok('接口 中文化后英文原文可追溯（title_en）',
      (hot || []).filter((h) => h.title_en).length >= 8,
      (hot || []).filter((h) => h.title_en).length + ' 条带 title_en');
    const seedLeft = (hot || []).filter((h) => (h.driver_sources || []).some((s) => String(s).includes('示例源')));
    ok('接口 驱动源已剔除「示例源·xx」种子', seedLeft.length === 0, seedLeft.length + ' 条残留');
    const enMove = moves.filter((m) => m.title && !HAN.test(m.title));
    const enImp = impacts.filter((x) => x.title && !HAN.test(x.title));
    ok('接口 事件动向 / 影响卡标题中文化', enMove.length === 0 && enImp.length === 0,
      `moves 非中文 ${enMove.length} · impacts 非中文 ${enImp.length}`);
    ok('接口 动态新闻标题带中文多空标注',
      ((topics[0] || {}).headlines || []).every((h) => ['看多', '看空', '中性'].includes(h.sentiment)),
      (((topics[0] || {}).headlines || []).map((h) => h.sentiment).join('/') || '无'));
    ok('口径含「中文事件名 / 原文可核对」说明',
      /中文事件名/.test(basisText) && /原文/.test(basisText), basisText.slice(0, 40));

    /* ---------- ② 前端层（走真实导航，不用兜底函数） ---------- */
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1600,1400'],
      defaultViewport: { width: 1600, height: 1400 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('.nav-item[data-cat="research"]', { timeout: 30000 });
    await page.click('.nav-item[data-cat="research"]');
    await page.waitForSelector('.sub-tab[data-tab="overseas_sentiment"]', { timeout: 15000 });
    await page.click('.sub-tab[data-tab="overseas_sentiment"]');
    const clicked = await page.$eval('.sub-tab[data-tab="overseas_sentiment"]', (el) => el.textContent.trim());
    ok('真实点到「美股港股舆情」子标签', clicked.includes('美股港股舆情'), `TAB_CLICKED = ${clicked}`);

    await page.waitForSelector('#p-overseas_sentiment .ov2-pie', { timeout: 30000 });
    const ui = await page.evaluate(() => {
      const box = document.getElementById('p-overseas_sentiment');
      const hs = [...box.querySelectorAll('.ov2-h')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
      const arcs = box.querySelectorAll('.ov2-pie svg path').length;
      const lg = box.querySelectorAll('.ov2-pie .ov2-lg').length;
      const tblocks = box.querySelectorAll('.ov2-topic').length;
      const extLinks = [...box.querySelectorAll('.ov2-head a[href]')].filter((a) => /^https?:/i.test(a.getAttribute('href'))).length;
      const bars = box.querySelectorAll('.ov2-bar').length;
      const moves = box.querySelectorAll('.ov2-move').length;
      const bullC = box.querySelectorAll('.ov2-bb-col.up .badge').length;
      const bearC = box.querySelectorAll('.ov2-bb-col.down .badge').length;
      const imps = box.querySelectorAll('.ov2-imp').length;
      const impUp = box.querySelectorAll('.ov2-imp-grp.up').length;
      const impDn = box.querySelectorAll('.ov2-imp-grp.down').length;
      const syms = [...box.querySelectorAll('[data-ov-sym]')];
      const symsBound = syms.filter((el) => typeof el.onclick === 'function').length;
      const foldSrc = box.querySelector('details.ov2-src');
      const foldBasis = box.querySelector('details.ov2-basis');
      const basisN = box.querySelectorAll('.ov2-basis li').length;
      const srcKids = box.querySelectorAll('#ov-sources .ov-src-status').length;
      const hasMood = !!box.querySelector('.ov2-src-in .ov-mood');
      const barWidths = [...box.querySelectorAll('.ov2-bar-fill')].map((x) => parseFloat(x.style.width) || 0);
      return { hs, arcs, lg, tblocks, extLinks, bars, moves, bullC, bearC, imps, impUp, impDn,
        syms: syms.length, symsBound, hasFoldSrc: !!foldSrc, hasFoldBasis: !!foldBasis, basisN, srcKids, hasMood, barWidths };
    });
    ok('四段标题齐全（说什么 / 投向哪 / 做了什么 / 对内陆建议）',
      ui.hs.length >= 4 && ['他们在说什么', '注意力投向', '他们做了什么', '对内陆的影响与建议'].every((k) => ui.hs.some((t) => t.includes(k))),
      ui.hs.join(' | ').slice(0, 150));
    ok('一、环形图真渲染（path 数=取色块数）', ui.arcs >= 5 && ui.arcs === ui.lg, `arcs=${ui.arcs} legend=${ui.lg}`);
    ok('一、主题块 + 真实外链可点', ui.tblocks >= 3 && ui.extLinks >= 3, `主题块 ${ui.tblocks} · 外链 ${ui.extLinks}`);
    ok('二、横向条形图渲染且宽度递减', ui.bars >= 8 && ui.barWidths.every((w, i) => i === 0 || ui.barWidths[i - 1] >= w),
      `${ui.bars} 条 | 首=${ui.barWidths[0]}% 末=${ui.barWidths[ui.barWidths.length - 1]}%`);
    ok('三、事件动向卡 >=5', ui.moves >= 5, `${ui.moves} 张`);
    ok('四、主攻 / 回避 chips 齐全', ui.bullC >= 4 && ui.bearC >= 2, `主攻 ${ui.bullC} · 回避 ${ui.bearC}`);
    ok('四、影响卡 >=5 且含受益 / 承压分组', ui.imps >= 5 && ui.impUp >= 3 && ui.impDn >= 2,
      `卡 ${ui.imps} · 受益组 ${ui.impUp} · 承压组 ${ui.impDn}`);
    ok('代表个股可点（已绑定详情）', ui.syms >= 5 && ui.symsBound === ui.syms, `${ui.symsBound}/${ui.syms} 已绑定`);
    ok('原始情绪与数据源收进折叠区（不占主位）', ui.hasFoldSrc && ui.hasMood, `折叠=${ui.hasFoldSrc} 含情绪块=${ui.hasMood}`);
    ok('口径说明可展开且 >=4 条', ui.hasFoldBasis && ui.basisN >= 4, `${ui.basisN} 条`);

    await page.screenshot({ path: SHOT, fullPage: false });
    await page.screenshot({ path: path.join(BASE, '_ov_insight_full.png'), fullPage: true });

    /* ---------- ③ 模块二（国外驱动热点）：中文标题 + 原文核对入口 + 驱动源截断 ---------- */
    await page.click('.sub-tab[data-tab="overseas_hotspots"]');
    await page.waitForSelector('#p-overseas_hotspots .ov-hs-card', { timeout: 30000 });
    const hsUi = await page.evaluate(() => {
      const box = document.getElementById('p-overseas_hotspots');
      const cards = [...box.querySelectorAll('.ov-hs-card')];
      const titles = [...box.querySelectorAll('.ov-hs-title')]
        .map((x) => ((x.firstChild ? x.firstChild.textContent : x.textContent) || '').trim());
      const marks = [...box.querySelectorAll('.ov-src-en')];
      const drvBoxes = [...box.querySelectorAll('.ov-hs-drivers')];
      const maxDrv = Math.max(0, ...drvBoxes.map((d) => d.querySelectorAll('.ov-driver').length));
      return {
        n: cards.length, titles,
        enMarks: marks.length,
        titleAttrs: marks.filter((x) => x.getAttribute('title')).length,
        maxDrv,
        moreTags: box.querySelectorAll('.ov-driver-more').length,
        seedTxt: /示例源/.test(box.textContent),
        varChips: box.querySelectorAll('.ov-hs-vars .badge').length,
        fcGroups: box.querySelectorAll('.ov-hs-fc-grp').length,
      };
    });
    const enLeftUi = hsUi.titles.filter((t) => t && !HAN.test(t));
    ok('模块二 热点卡全部渲染', hsUi.n >= 10, hsUi.n + ' 张');
    ok('模块二 卡片标题全部中文（无纯英文标题）', enLeftUi.length === 0,
      `非中文 ${enLeftUi.length}：${enLeftUi.slice(0, 2).join(' / ') || '无'}`);
    ok('模块二 标题带「原文」核对入口 + 悬停原题', hsUi.enMarks >= 8 && hsUi.titleAttrs >= 8,
      `原文标 ${hsUi.enMarks} · 带 title 属性 ${hsUi.titleAttrs}`);
    ok('模块二 驱动源已截断（每家 ≤6 个 + 等 N 家）', hsUi.maxDrv <= 6 && hsUi.moreTags >= 1,
      `最多 ${hsUi.maxDrv} 家 · 等N家标 ${hsUi.moreTags}`);
    ok('模块二 无「示例源」种子残留', !hsUi.seedTxt, hsUi.seedTxt ? '仍含示例源' : '干净');
    ok('模块二 传导变量 / 涨跌预判仍在', hsUi.varChips >= 8 && hsUi.fcGroups >= 4,
      `变量 chip ${hsUi.varChips} · 预判组 ${hsUi.fcGroups}`);
    await page.screenshot({ path: path.join(BASE, '_ov_zh_hotspots.png'), fullPage: false });

    /* ---------- ④ 代表个股 → 个股详情（K 线 / 估值 / 盈利） ----------
       2026-09-26 王总要求：热点卡「③ 涨跌预判」里的代表个股（如中信证券 600030）要能点开，
       看到 K 线图 + 基础信息 + PE / ROE。本段验证入口真的接上、且点开后抽屉内容齐备。 */
    const hpUi = await page.evaluate(() => {
      const box = document.getElementById('p-overseas_hotspots');
      const chips = [...box.querySelectorAll('.ov-hs-dom-stks .stk')];
      const ck = chips.filter((c) => c.hasAttribute('data-hp-sym'));
      return {
        total: chips.length,
        clickable: ck.length,
        codes: ck.map((c) => c.getAttribute('data-hp-sym')),
        titled: ck.filter((c) => /K 线/.test(c.getAttribute('title') || '')).length,
        legend: /点公司名可查/.test(box.textContent),
      };
    });
    ok('模块二 代表个股已挂详情入口', hpUi.clickable >= 8, `${hpUi.clickable} 可点 / 共 ${hpUi.total} 个`);
    ok('模块二 可点代码均为 6 位 A 股（不给港美股挂必然失败的入口）',
      hpUi.codes.length > 0 && hpUi.codes.every((c) => /^\d{6}$/.test(c)),
      hpUi.codes.slice(0, 5).join(',') || '无');
    ok('模块二 可点个股带操作提示（悬停文案 + 说明行）',
      hpUi.titled === hpUi.clickable && hpUi.legend,
      `带提示 ${hpUi.titled}/${hpUi.clickable} · 说明行 ${hpUi.legend}`);

    const firstCode = hpUi.codes[0];
    await page.click('#p-overseas_hotspots .stk[data-hp-sym]');
    await page.waitForSelector('#drawer.show', { timeout: 20000 });
    const dHead = await page.evaluate(() => ({
      title: (document.getElementById('drawerTitle') || {}).textContent || '',
      body: (document.getElementById('drawerBody') || {}).textContent || '',
    }));
    ok('点代表个股 → 抽屉打开且标题含该代码', dHead.title.includes(firstCode), dHead.title.slice(0, 70));
    ok('抽屉含「重要信息 / K 线走势 / 持仓选手」三段',
      /重要信息/.test(dHead.body) && /K 线走势/.test(dHead.body) && /持仓选手/.test(dHead.body));

    // 等「重要信息」联网回来（东财 push2 / 腾讯行情兜底）
    await page.waitForFunction(() => {
      const b = document.getElementById('stkQuote');
      return b && /市盈率/.test(b.textContent);
    }, { timeout: 90000 });
    const qUi = await page.evaluate(() => {
      const b = document.getElementById('stkQuote');
      return {
        txt: (b.textContent || '').replace(/\s+/g, ' '),
        lamps: document.querySelectorAll('#stkQuote .vl-item').length,
        items: document.querySelectorAll('#stkQuote .si-item').length,
      };
    });
    ok('弹窗披露估值（市盈率 TTM / 市净率 PB）',
      /市盈率 TTM/.test(qUi.txt) && /市净率 PB/.test(qUi.txt), qUi.txt.slice(0, 78));
    ok('弹窗披露盈利（ROE 加权 / 毛利率 / 营收）',
      /ROE\(加权\)/.test(qUi.txt) && /毛利率/.test(qUi.txt) && /营业总收入/.test(qUi.txt), qUi.items + ' 项指标');
    ok('弹窗披露基础信息（上市日期 / 总市值 / 每股净资产）',
      /上市日期/.test(qUi.txt) && /总市值/.test(qUi.txt) && /每股净资产/.test(qUi.txt));
    ok('弹窗含价值标准对照三盏灯（PE / ROE / 股息率）', qUi.lamps === 3, qUi.lamps + ' 盏');

    // 等 K 线出图（全历史，新浪/东财/腾讯三源兜底）
    await page.waitForFunction(() => {
      const el = document.getElementById('klineRange');
      return el && /\d{4}-\d{2}-\d{2}/.test(el.textContent || '');
    }, { timeout: 90000 });
    const kUi = await page.evaluate(() => {
      const c = document.getElementById('klineCanvas');
      const r = document.getElementById('klineRange');
      return { w: c ? c.width : 0, h: c ? c.height : 0, range: (r ? r.textContent : '') || '' };
    });
    ok('K 线画布已出图（尺寸 + 日期区间 + 根数）',
      kUi.w > 200 && kUi.h > 100 && /\d{4}-\d{2}-\d{2}/.test(kUi.range),
      `${kUi.w}×${kUi.h} · ${kUi.range.slice(0, 46)}`);
    await page.screenshot({ path: path.join(BASE, '_hp_stock_detail.png'), fullPage: false });
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.getElementById('drawer').classList.contains('show'), { timeout: 8000 });
    ok('Esc 能关掉个股抽屉（回到面板）', true);

    /* ---------- ⑤ 模块一（近期热点）板块标的同样可点 ---------- */
    await page.click('.sub-tab[data-tab="topics"]');
    await page.waitForSelector('#p-topics .stk-mini', { timeout: 25000 });
    const miniUi = await page.evaluate(() => {
      const box = document.getElementById('p-topics');
      const all = [...box.querySelectorAll('.stk-mini')];
      const ck = all.filter((x) => x.hasAttribute('data-sym'));
      return { total: all.length, ck: ck.length, codes: ck.map((x) => x.getAttribute('data-sym')) };
    });
    ok('模块一 板块标的已挂详情入口', miniUi.ck >= 5, `${miniUi.ck} 可点 / 共 ${miniUi.total} 个`);
    ok('模块一 可点标的均为 6 位 A 股代码',
      miniUi.codes.length > 0 && miniUi.codes.every((c) => /^\d{6}$/.test(c)), miniUi.codes.slice(0, 5).join(','));

    const realErrs = errs.filter((t) => !/favicon|net::ERR_/i.test(t));
    ok('控制台零报错', realErrs.length === 0, realErrs.slice(0, 3).join(' ;; '));
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
