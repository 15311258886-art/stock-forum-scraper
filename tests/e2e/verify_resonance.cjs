/* verify_resonance.cjs —— 「博主共振」板块 UI 回归（2026-10-15 新增，进哥需求）
 *
 * 断言清单：
 *   R1/R2 子导航出现「博主共振」且**紧跟「每日预测」之后**
 *   A1-A5 支柱A 个股共振：卡片数>0 / 每卡有共振分 / 每卡有等级徽章 / 证据链可展开 / 口径≥5 条
 *   B1-B2 支柱B 战法体系：战法表 ≥50 行 / 知识图谱互证条目 >0
 *   C1-C2 支柱C 信源画像：信源行 >0 / 分组 ≥2
 *   E1    控制台零报错
 *
 * 用法：node verify_resonance.cjs   （需后端已就绪；只读）
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
module.paths.push(require('path').join(__dirname, 'node_modules'));
const puppeteer = require('puppeteer-core');
const path = require('path');

const PORT = Number(process.env.VIEWER_PORT || process.env.PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}`;
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const SHOT = path.join(__dirname, '_resonance_panel.png');
const SHOT_EVO = path.join(__dirname, '_evolve_panel.png');

const fails = [];
const ok = (c, m) => { console.log((c ? '  PASS ' : '  FAIL ') + m); if (!c) fails.push(m); };
const step = (n, t) => console.log(`\n[${n}] ${t}`);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--proxy-bypass-list=*', '--disable-features=IsolateOrigins,site-per-process'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1000 });
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  // 2026-10-15 补诊断：ERR_CONNECTION_REFUSED 这类「资源加载失败」在 console 里不带 URL，
  // 只记 message 根本查不出是哪个请求被拒。这里把失败请求的 URL 与原因一并记下。
  // 注意：net::ERR_ABORTED 是**页面切换时浏览器主动取消**在途请求，属正常，不算错（否则每次切 tab 都误报）。
  page.on('requestfailed', (r) => {
    const t = r.failure() ? r.failure().errorText : '?';
    if (t === 'net::ERR_ABORTED') return;
    errs.push('REQFAIL ' + t + ' ' + r.url());
  });
  page.on('response', (r) => { if (r.status() >= 400) errs.push('HTTP ' + r.status() + ' ' + r.url()); });

  step(1, '打开首页并进入「博主共振」');
  // Edge 冷启动/代理抖动会偶发 ERR_CONNECTION_REFUSED → 加载阶段重试 3 次
  let loaded = false;
  for (let i = 0; i < 3 && !loaded; i++) {
    try {
      await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 20000 });
      await page.waitForFunction(() => typeof window.showTab === 'function', { timeout: 25000 });
      loaded = true;
    } catch (e) {
      console.log('  ⚠ 第 ' + (i + 1) + ' 次加载失败（' + (e && e.message || '') + '），重试…');
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  if (!loaded) {
    const diag = await page.evaluate(() => ({
      href: location.href, title: document.title, showTab: typeof window.showTab,
      scripts: Array.from(document.querySelectorAll('script')).map((s) => s.getAttribute('src')),
    }));
    console.log('  ⚠ 诊断: ' + JSON.stringify(diag));
    console.log('  ⚠ 控制台错误: ' + (errs.slice(0, 5).join(' || ') || '(无)'));
    await browser.close();
    process.exit(3);
  }
  await page.evaluate(() => showTab('resonance'));
  await page.waitForSelector('#p-resonance .res-card', { timeout: 45000 });
  ok(true, 'R0 面板已渲染出共振卡');

  step(2, '子导航位置：紧跟「每日预测」之后');
  const nav = await page.evaluate(() =>
    Array.from(document.querySelectorAll('#subnav a')).map((a) => a.textContent.trim()));
  const iP = nav.findIndex((t) => t.includes('每日预测'));
  const iR = nav.findIndex((t) => t.includes('博主共振'));
  ok(iR >= 0, 'R1 子导航出现「博主共振」｜ ' + nav.join(' | '));
  ok(iP >= 0 && iR === iP + 1, `R2 紧跟「每日预测」之后（预测@${iP} 共振@${iR}）`);

  step(3, '支柱A · 个股共振');
  const a = await page.evaluate(() => {
    const box = document.querySelector('#p-resonance');
    return {
      cards: box.querySelectorAll('.res-card').length,
      scores: box.querySelectorAll('.res-score').length,
      badges: box.querySelectorAll('.res-card-h .badge').length,
      evid: box.querySelectorAll('.res-ev summary').length,
      div: box.querySelectorAll('.res-div-item').length,
      caliber: box.querySelectorAll('.res-cal li').length,
      tabs: box.querySelectorAll('.pillar-tabs .sub-tab').length,
    };
  });
  ok(a.cards > 0, 'A1 共振卡 > 0（' + a.cards + ' 张）');
  ok(a.scores === a.cards, 'A2 每张卡都有共振分');
  ok(a.badges === a.cards, 'A3 每张卡都有等级徽章');
  ok(a.evid > 0, 'A4 证据链可展开（' + a.evid + ' 张卡带证据）');
  ok(a.caliber >= 5, 'A5 口径 ≥5 条（' + a.caliber + '）');
  ok(a.tabs === 4, 'A6 面板内四支柱页签（' + a.tabs + '）');
  ok(a.div > 0, 'A7 分歧预警有内容（' + a.div + ' 条）');

  step(4, '支柱B · 战法体系');
  await page.evaluate(() => resTab('strategy'));
  await page.waitForFunction(
    () => document.querySelectorAll('#p-resonance .res-tb tbody tr').length > 0, { timeout: 15000 });
  const b = await page.evaluate(() => {
    const box = document.querySelector('#p-resonance');
    const rows = box.querySelectorAll('.res-tb tbody tr');
    return {
      rows: rows.length,
      kb: box.querySelectorAll('.res-kb-item').length,
      cons: Array.from(box.querySelectorAll('.res-kb-gh')).map((x) => x.textContent.trim()),
      first: rows[0] ? rows[0].innerText.replace(/\s+/g, ' ').slice(0, 80) : '',
    };
  });
  ok(b.rows >= 50, 'B1 战法表 ≥50 行（' + b.rows + '）');
  ok(b.kb > 0, 'B2 知识图谱互证 > 0（' + b.kb + ' 条）');
  ok(b.cons.length >= 2, 'B3 图谱分「一致/存疑」等档展示：' + b.cons.slice(0, 4).join(' '));
  console.log('       首行样本：' + b.first);

  step(5, '支柱C · 信源画像');
  await page.evaluate(() => resTab('source'));
  await page.waitForFunction(
    () => document.querySelectorAll('#p-resonance .res-src-row').length > 0, { timeout: 15000 });
  const c = await page.evaluate(() => {
    const box = document.querySelector('#p-resonance');
    const rows = Array.from(box.querySelectorAll('.res-src-row'));
    const hasScore = rows.filter((r) => {
      const b = r.querySelector('.res-src-m b');
      return b && /\d/.test(b.textContent);
    }).length;
    return { rows: rows.length, groups: box.querySelectorAll('.res-src-g').length, hasScore,
             gh: Array.from(box.querySelectorAll('.res-src-gh')).map((x) => x.textContent.trim().slice(0, 12)) };
  });
  ok(c.rows > 0, 'C1 信源行 > 0（' + c.rows + '）');
  ok(c.groups >= 2, 'C2 分组 ≥2（' + c.groups + '）：' + c.gh.join(' / '));
  ok(c.hasScore > 0, 'C3 有信誉分的源 > 0（' + c.hasScore + '）');

  step(6, '支柱D · 自我进化（2026-10-15 进哥追加）');
  await page.evaluate(() => resTab('evolve'));
  await page.waitForFunction(
    () => document.querySelectorAll('#p-resonance .res-wrow').length > 0, { timeout: 25000 });
  const dv = await page.evaluate(() => {
    const box = document.querySelector('#p-resonance');
    const fw = box.querySelector('.res-wrow');
    return {
      wrows: box.querySelectorAll('.res-wrow').length,
      rules: box.querySelectorAll('.res-tb tbody tr').length,
      kcards: box.querySelectorAll('.res-kcard').length,
      jrn: box.querySelectorAll('.res-jrn').length,
      caliber: box.querySelectorAll('.res-cal li').length,
      firstW: fw ? fw.innerText.replace(/\s+/g, ' ') : '',
    };
  });
  ok(dv.wrows === 4, 'D1 权重对比条 4 个（博主/战法/舆情/主引擎）｜' + dv.firstW);
  ok(dv.rules > 0, 'D2 自生长规则表 > 0 行（' + dv.rules + '）');
  ok(dv.kcards > 0, 'D3 知识内化卡 > 0（' + dv.kcards + '）');
  ok(dv.jrn > 0, 'D4 进化日志条存在');
  ok(dv.caliber >= 5, 'D5 自我进化口径 ≥5 条（' + dv.caliber + '）');
  await page.screenshot({ path: SHOT_EVO });

  step(7, '浏览器内 POST 可用性（curl 走系统代理会被拦，必须在浏览器里验）');
  const postOk = await page.evaluate(async () => {
    try {
      const r2 = await fetch("/api/evolve/run?force=1",
        { method: "POST", headers: { "Content-Type": "application/json" } });
      return { post: r2.status, body: (await r2.text()).slice(0, 90) };
    } catch (e) { return { err: String(e) }; }
  });
  ok(postOk.post === 200, 'D6 浏览器内 POST /api/evolve/run 返回 200｜' + JSON.stringify(postOk));

  step(8, '系统自选 —— 自我进化的成果出口（2026-10-15 进哥要求「学完得有成果」）');
  await page.evaluate(() => { if (typeof resTab === 'function') resTab('evolve'); });
  await new Promise((r) => setTimeout(r, 5000));   // 不用外部 sleep（作用域不同）
  const spv = await page.evaluate(() => {
    const box = document.querySelector('#p-resonance');
    const txt = (box.textContent || '').replace(/\s+/g, ' ');
    // 2026-10-15 返工：自选卡已与共振卡同构（统一 .res-card），不再用 .res-sp-card
    const c0 = box.querySelector('.res-grid .res-card');
    const pk = (sel) => { const el = c0 && c0.querySelector(sel); if (!el) return null;
      const g = getComputedStyle(el); return g.fontSize + '/' + g.fontWeight; };
    return { style: c0 ? { name: pk('.res-name'), code: pk('.res-code'), score: pk('.res-score'),
               verdict: pk('.res-verdict'), bar: !!c0.querySelector('.res-bar'),
               hitUses: c0.querySelectorAll('.res-hit').length } : null,
             cards: box.querySelectorAll('.res-card').length,
             hasPick: /系统自选 —— 学完之后/.test(txt),
             hasStat: /系统自选已结算/.test(txt),
             hasEx: /超额 [\-\+]?\d/.test(txt),
             hasHist: /近 \d+ 期战绩/.test(txt),
             hasOwnTag: /★ 系统自己发现/.test(txt),
             hasCal: /系统自选的口径/.test(txt) };
  });
  ok(spv.hasPick, 'F1 系统自选区已渲染（★ 系统自选）');
  ok(spv.cards >= 20, 'F2 系统自选卡 ≥20 张（实测 ' + spv.cards + '）');
  ok(spv.hasStat, 'F3 战绩条存在（已结算 / 超额 / 胜率）');
  ok(spv.hasEx, 'F4 战绩条含超额口径');
  ok(spv.hasHist, 'F5 历史回算战绩表存在');
  ok(spv.hasOwnTag, 'F6 「★ 系统自己发现」标签出现（不模仿的直接证据）');
  ok(spv.hasCal, 'F7 系统自选口径说明存在');
  // 2026-10-15 进哥指出「自选卡与共振卡样式不一致、字号突兀」→ 固化为断言防回归。
  // 病灶：名字漏 res-name（掉回按钮字号）、正文误用 .res-hit（红色小胶囊）、缺进度条。
  const st = spv.style || {};
  ok(st.name === '15px/600', 'F8 自选卡股票名与共振同款 15px/600（实测 ' + st.name + '）');
  ok(st.score === '17px/700', 'F9 自选卡分数与共振同款 17px/700（实测 ' + st.score + '）');
  ok(st.verdict === '12.5px/400' && st.bar && st.hitUses === 0,
     'F10 自选卡正文 12.5px + 有进度条 + 无红色胶囊（实测 ' + st.verdict
     + ' bar=' + st.bar + ' hit=' + st.hitUses + '）');

  step(9, '切回个股共振 + 截图');
  await page.evaluate(() => resTab('cards'));
  await page.waitForSelector('#p-resonance .res-card', { timeout: 15000 });
  await page.screenshot({ path: SHOT });

  step(9, '控制台零报错');
  ok(errs.length === 0, 'E1 控制台报错 = 0' + (errs.length ? '：' + errs.slice(0, 3).join(' || ') : ''));

  await browser.close();
  console.log('\n截图：' + SHOT);
  console.log('\n=== ' + (fails.length ? ('FAIL ' + fails.length + ' 项') : 'ALL PASS') + ' ===');
  if (fails.length) fails.forEach((f) => console.log('   × ' + f));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('异常:', e && e.message); process.exit(2); });
