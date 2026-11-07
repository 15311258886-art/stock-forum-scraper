// 2026-10-01 王总第二轮拍板：「查出来的就改，方向＝通俗易懂、一眼看出判断意向」。
// 上一轮自查列了 5 处「查了但没动」的口径不一，本脚本逐条在**真实浏览器**里验：
//   ① 政策 stance 分层：主档按强度、内有分化作附加面（不再整块吞成「分化」）
//   ② 海外情绪：判词把「净情绪数值 + ±5 持平带」一起写出来
//   ③ 选股筛选项：标签一律写「指标·阈值」（估值低·PE<20 …），判据不藏在代码里
//   ④ newsStanceCls：未收录文案按方向兜底，不再静默落中性色
//   ⑤ 研报「回避」带判据，跟技术面「偏弱回避」不再撞名
// 用法：node verify_koujing.cjs   （需 8765 在跑；浏览器用本机 Edge）
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
  await page.setViewport({ width: 1440, height: 1400 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);

  let pass = 0, total = 0;
  const say = (ok, msg) => { total++; if (ok) pass++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

  // ================= ① 政策 stance 分层 =================
  // hash 路由是 #大类/子类，先切到政策 tab 再渲染，面板才会挂上
  await page.evaluate(() => { location.hash = '#info/policy'; });
  await sleep(1800);
  const pol = await page.evaluate(async () => {
    const d = await (await fetch('/api/policy_desk?num=80')).json();
    const v = d.verdict || {};
    const secs = v.sectors || [];
    // 直接调渲染函数手动铺开，不依赖面板懒加载时机
    FEAT.policy.data = d; renderPolicy();
    const host = document.getElementById('p-policy');
    const html = host ? host.innerHTML : '';
    return {
      n: secs.length,
      mixedSectors: secs.filter((j) => j.mixed).map((j) => j.sector),
      // 徽标函数的行为：主档强利好 + 有分化附加面
      badgeMixed: (typeof polStanceBadge === 'function')
        ? polStanceBadge('强利好', { mixed: true, bear: 3, n: 8 }) : null,
      badgePlain: (typeof polStanceBadge === 'function')
        ? polStanceBadge('强利好', { mixed: false, bear: 0, n: 8 }) : null,
      // 卡片级断言：即使宿主面板没挂上也能验出依据行写了压制条数
      cardHtml: (typeof polJudgeCard === 'function')
        ? polJudgeCard({ stance: '强利好', sector: '医药', n: 8, weight: 24, bear: 3,
                         mixed: true, reasons: ['x'], members: [] }) : '',
      hasBearCount: /压制类\s*\d+\s*条/.test(html),
    };
  });
  say(pol.n > 0, `① 政策面板有 ${pol.n} 个板块（数据可用）`);
  say(/内有分化/.test(pol.badgeMixed || ''),
    `① 徽标「强利好 + 内有分化」→ ${(pol.badgeMixed || '').replace(/<[^>]+>/g, '')}`);
  say(!/内有分化/.test(pol.badgePlain || ''),
    '① 无压制面时不显示「内有分化」（附加面可开可关）');
  say(pol.hasBearCount, '① 政策面板依据行写出「压制类 N 条」');
  say(/压制类\s*3\s*条/.test(pol.cardHtml),
    '① 研判卡依据行露判据（示例卡：依据 8 条 · 加权 24 · 压制类 3 条）');

  // ================= ② 海外情绪口径露出 =================
  await page.evaluate(() => { location.hash = '#info/overseas_sentiment'; });
  await sleep(1800);
  const ov = await page.evaluate(async () => {
    const d = await (await fetch('/api/overseas_sentiment')).json();
    renderOverseasSentiment(d);
    const host = document.getElementById('p-overseas_sentiment');
    const html = host ? host.innerHTML : '';
    return { net: d.net_mood, html };
  });
  say(/净情绪/.test(ov.html),
    `② 海外情绪文案带净情绪数值（net_mood=${ov.net}）`);
  say(/±5\s*内视为持平/.test(ov.html), '② 持平带宽写出「±5 内视为持平」，不再凭感觉判');

  // ================= ③ 选股筛选项「指标·阈值」 =================
  const scr = await page.evaluate(() => {
    const arr = (typeof SCREENER_SCREENS !== 'undefined') ? SCREENER_SCREENS : [];
    const names = arr.map((x) => x[1]);
    return { names, hasThr: names.filter((n) => /[<><]/.test(n)) };
  });
  say(scr.hasThr.length >= 6,
    `③ 筛选项带阈值：${scr.hasThr.join(' / ')}`);
  say(scr.names.includes('估值低·PE<20'), '③「低估值」已改名「估值低·PE<20」（判据直接进标签）');
  const srcJs = fs.readFileSync('viewer/static/app.js', 'utf8');
  say(srcJs.includes('两把不同的尺'), '③ 估值面板已注明五因子综合分 vs PE<20 是两把尺');

  // ================= ④ newsStanceCls 兜底 =================
  const stance = await page.evaluate(() => {
    if (typeof newsStanceCls !== 'function') return null;
    return {
      duo: newsStanceCls('整体偏多'),
      kong: newsStanceCls('整体偏空'),
      known: newsStanceCls('偏正面'),
      mid: newsStanceCls('中性偏正'),
      null_: newsStanceCls('多空均衡'),
    };
  });
  say(stance && stance.duo === 'st-strong',
    `④ 未收录文案「整体偏多」→ ${stance && stance.duo}（旧逻辑会静默落 st-mixed）`);
  say(stance && stance.kong === 'st-bear',
    `④ 未收录文案「整体偏空」→ ${stance && stance.kong}（方向兜底生效）`);
  say(stance && stance.known === 'st-strong' && stance.mid === 'st-good',
    '④ 原三条显式词表长词优先未被破坏（偏正面→强 / 中性偏正→好）');
  say(stance && stance.null_ === 'st-mixed', '④ 真中性（多空均衡）仍落 st-mixed，未过度兜底');

  // ================= ⑤ 研报「回避」带判据 =================
  say(fs.readFileSync('research_insight.py', 'utf8').includes('回避·机构偏空'),
    '⑤ 研报 D 档出口 1：「回避·机构偏空」');
  say(fs.readFileSync('research_insight.py', 'utf8').includes('回避·单点证据弱')
      && fs.readFileSync('research_insight.py', 'utf8').includes('回避·研报评分低'),
    '⑤ 研报 D 档另外两个出口也各带判据（单点证据弱 / 研报评分低）');
  say(srcJs.includes('回避（研报）'), '⑤ 前端分档 chip 标成「回避（研报）」，跟技术面「偏弱回避」区分');

  // ================= ⑥ 控制台 =================
  say(errs.length === 0, `⑥ 控制台零报错（实际 ${errs.length} 条${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}）`);

  await page.screenshot({ path: 'shot_koujing.png', fullPage: false });
  console.log(`\n截图：shot_koujing.png`);
  console.log(`\n结果：${pass}/${total} 通过`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})().catch((e) => { console.error('脚本异常：', e); process.exit(1); });
