/* verify_backtest.cjs —— 「历史回测」板块的 UI 回归（2026-10-15 新增功能）
 *
 * 对应进哥 6 条要求逐条断言：
 *   ① 板块在四支柱下方  ② 不再限高内滚（2026-10-16 进哥反转要求：四段往下铺、整页流动）
 *   ③ 日期按钮可切换    ④ T+1/3/5/10/20 周期可切换（右栏高亮列 + 下方总结同步）
 *   ⑤ 左右分栏且逐行对齐  ⑥ 下方总结区（命中率/平均收益/平均超额/判读）
 * 外加诚实性红线：未到期显示「还差 N 日」、缺 K 线显示「—」，**绝不出现填 0 假冒有值**。
 *
 * 用法：VIEWER_PORT=8797 node verify_backtest.cjs   （需后端已就绪；只读）
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
module.paths.push(require('path').join(__dirname, 'node_modules'));
const puppeteer = require('puppeteer-core');

const PORT = Number(process.env.VIEWER_PORT || process.env.PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}`;
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const SHOT = require('path').join(__dirname, '_backtest_panel.png');

const fails = [];
const ok = (c, m) => { console.log((c ? '  PASS ' : '  FAIL ') + m); if (!c) fails.push(m); };

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 950 });
  const errs = [];
  const reqs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('request', (r) => reqs.push(r.url()));

  console.log(`\n[1] 打开 ${BASE}/ 进入「每日预测」页`);
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.showTab === 'function', { timeout: 30000 });
  await page.evaluate(() => showTab('prediction'));
  await page.waitForSelector('#p-prediction .bt-sec', { timeout: 30000 });
  await page.waitForFunction(() => document.querySelectorAll('#p-prediction .bt-row').length > 0,
    { timeout: 30000 });

  console.log('\n[2] ①板块位置：必须落在四支柱列表之后');
  const pos = await page.evaluate(() => {
    const sec = document.querySelector('#p-prediction .bt-sec');
    const stack = document.querySelector('#p-prediction .pil-stack');
    if (!sec || !stack) return null;
    const a = stack.getBoundingClientRect(), b = sec.getBoundingClientRect();
    return { stackBottom: Math.round(a.bottom), secTop: Math.round(b.top),
             inPanel: !!document.querySelector('#p-prediction #bt-sec') };
  });
  ok(!!pos, 'B0 板块存在且位于 #p-prediction 内');
  ok(pos && pos.secTop >= pos.stackBottom - 2,
    `B1 历史回测板块在四支柱列表**下方**（四支柱底=${pos && pos.stackBottom}，回测顶=${pos && pos.secTop}）`);

  console.log('\n[3] ②不再限高内滚 + 子页签只渲染当前支柱（2026-10-16 进哥两次修正）');
  const h = await page.evaluate(() => {
    const el = document.querySelector('#p-prediction .pil-stack .pil-list');
    const stack = document.querySelector('#p-prediction .pil-stack');
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { maxH: cs.maxHeight, ovf: cs.overflowY, scrollH: el.scrollHeight, clientH: el.clientHeight,
             cols: cs.gridTemplateColumns, secs: stack.querySelectorAll(':scope > .pil-col').length };
  });
  ok(h && (h.maxH === 'none'),
    `H1 四支柱列表**不再限高内滚**（max-height=${h && h.maxH}, overflow-y=${h && h.ovf}）`);
  ok(h && Math.abs(h.scrollH - h.clientH) <= 2,
    `H2 没有框内滚动（内容 ${h && h.scrollH}px ≈ 可视 ${h && h.clientH}px，整页往下流）`);
  ok(h && h.secs === 1,
    `H2b 只渲染当前支柱 1 段（实测 ${h && h.secs} 段 —— 子页签就地换页，不滚动跳转）`);
  ok(h && (h.cols || '').split(' ').length === 2,
    `H2c 宽屏两列网格（grid-template-columns=${h && h.cols}）`);

  console.log('\n[3b] 日期按钮行与结果必须能同屏（进哥原话：想看前天的要来回上下滚）');
  const dv = await page.evaluate(() => {
    const sc = document.scrollingElement;
    sc.scrollTop = sc.scrollHeight;
    const d = document.querySelector('#p-prediction .bt-dates');
    const r = d.getBoundingClientRect();
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), vh: window.innerHeight,
             n: document.querySelectorAll('#p-prediction .bt-date').length };
  });
  ok(dv.top >= 8 && dv.bottom <= dv.vh,
    `H3 滚到底时日期按钮行完整在视口内（top=${dv.top} bottom=${dv.bottom} 视口=${dv.vh}，共 ${dv.n} 个日期）`);
  // 2026-10-16：整页往下流之后，靠 sticky 保证日期行不被滚走 —— 这条断言守住它
  const h3s = await page.evaluate(() => {
    const b = document.querySelector('#p-prediction .bt-stickybar');
    if (!b) return null;
    const cs = getComputedStyle(b);
    const d = document.querySelector('#p-prediction .bt-date');
    const tb = document.querySelector('.topbar');
    return { pos: cs.position, top: cs.top, zi: cs.zIndex, inBar: !!(d && b.contains(d)),
             barTop: Math.round(b.getBoundingClientRect().top),
             tbBottom: tb ? Math.round(tb.getBoundingClientRect().bottom) : 0,
             tbH: tb ? Math.round(tb.getBoundingClientRect().height) : 0 };
  });
  ok(h3s && h3s.pos === 'sticky' && h3s.inBar,
    `H3b 日期行/周期行已钉住（position=${h3s && h3s.pos} top=${h3s && h3s.top} z-index=${h3s && h3s.zi}）`);
  // 2026-10-16 新增红线：钉住条**不能被顶栏压住**（顶栏实测 84px 且 82% 半透明，
  // 按旧的 58px 摆会让日期行跟顶栏那行叠着透出来 —— 这是本轮真踩到的坑）
  ok(h3s && h3s.barTop >= h3s.tbBottom - 1,
    `H3c 钉住条完整落在顶栏下方、不被压住（条顶=${h3s && h3s.barTop} / 顶栏底=${h3s && h3s.tbBottom}，顶栏高 ${h3s && h3s.tbH}px）`);

  console.log('\n[3c] 子页签就地换页 + 每页回测只算该支柱（2026-10-16 进哥二次修正）');
  const before = await page.evaluate(() => {
    document.scrollingElement.scrollTop = 0;
    return { y: document.scrollingElement.scrollTop,
             title: (document.querySelector('#p-prediction .pil-col .pil-title') || {}).textContent };
  });
  const clicked = await page.evaluate(() => {
    const a = [...document.querySelectorAll('#p-prediction .pillar-tabs .sub-tab')]
      .find((x) => /竞价冲浪/.test(x.textContent));
    if (!a) return false;
    a.click();
    return true;
  });
  ok(clicked, `W0 存在「竞价冲浪」子页签（换页前停在：${before.title}）`);
  await new Promise((r) => setTimeout(r, 2500));
  const after = await page.evaluate(() => {
    const secs = [...document.querySelectorAll('#p-prediction .pil-stack > .pil-col')];
    return { y: document.scrollingElement.scrollTop,
             secs: secs.length,
             title: secs[0] ? (secs[0].querySelector('.pil-title') || {}).textContent : null,
             active: (document.querySelector('#p-prediction .pillar-tabs .sub-tab.active') || {}).textContent,
             rows: document.querySelectorAll('#p-prediction .bt-row').length,
             tags: [...new Set([...document.querySelectorAll('#p-prediction .bt-tag')].map((x) => x.textContent.trim()))],
             sub: (document.querySelector('#p-prediction .bt-sub') || {}).textContent || '' };
  });
  ok(after.secs === 1 && /竞价冲浪/.test(after.title || ''),
    `W1 换页后只显示该支柱（1 段：${after.title}）`);
  ok(/竞价冲浪/.test(after.active || ''), `W2 子页签高亮跟随（${after.active}）`);
  ok(/竞价冲浪/.test(after.sub), `W3 回测标题写明这是哪根支柱的回测（${after.sub.slice(0, 26)}…）`);
  ok(after.rows > 0 && after.tags.length > 0 && after.tags.every((t) => /竞价冲浪/.test(t)),
    `W4 每页底下的回测**只含该支柱的票**（${after.rows} 行；分组标签 ${after.tags.join(" / ")}）`);
  ok(Math.abs(after.y - before.y) <= 40,
    `W5 换页是"就地换内容"、不滚动跳转（换页前 ${before.y} → 换页后 ${after.y}）`);

  console.log('\n[3d] 第 5 根支柱「自我提升」（2026-10-16 进哥新需求）');
  const hasSL = await page.evaluate(() => {
    const a = [...document.querySelectorAll('#p-prediction .pillar-tabs .sub-tab')]
      .find((x) => /引擎自加权/.test(x.textContent));
    if (!a) return false;
    a.click();
    return true;
  });
  ok(hasSL, 'X0 页签里有「引擎自加权」（第 5 根）');
  await new Promise((r) => setTimeout(r, 2800));
  const sl = await page.evaluate(() => {
    const secs = [...document.querySelectorAll('#p-prediction .pil-stack > .pil-col')];
    return { secs: secs.length,
             title: secs[0] ? (secs[0].querySelector('.pil-title') || {}).textContent : null,
             cards: document.querySelectorAll('#p-prediction .pil-stack .pil-item').length,
             weightLine: (document.querySelector('#p-prediction .pil-boards') || {}).textContent || '',
             sum: (document.querySelector('#p-prediction .pil-sum') || {}).textContent || '',
             rows: document.querySelectorAll('#p-prediction .bt-row').length,
             tags: [...new Set([...document.querySelectorAll('#p-prediction .bt-tag')].map((x) => x.textContent.trim()))],
             sub: (document.querySelector('#p-prediction .bt-sub') || {}).textContent || '' };
  });
  ok(sl.secs === 1 && /引擎自加权/.test(sl.title || ''), `X1 一页只显示这一根（${sl.title}）`);
  ok(sl.cards >= 1, `X2 有产出的名单（${sl.cards} 只）`);
  ok(/学到的支柱权重/.test(sl.weightLine) && /未达门槛|已生效/.test(sl.weightLine),
    `X3 如实展示「学到的权重 + 样本数 + 是否生效」：${sl.weightLine.slice(0, 56)}…`);
  ok(/口径同「历史回测」/.test(sl.sum), 'X4 摘要写明学习口径（与历史回测同一套口径）');
  ok(sl.rows > 0 && sl.tags.length > 0 && sl.tags.every((t) => /引擎自加权/.test(t)),
    `X5 它自己的回测只含它自己的票（${sl.rows} 行；分组标签 ${sl.tags.join(" / ")}）`);
  ok(/引擎自加权/.test(sl.sub), `X6 回测标题写明是「引擎自加权」的回测（${sl.sub.slice(0, 22)}…）`);
  const slApi = await page.evaluate(() => fetch('/api/backtest?pillar=self_learn').then((r) => r.json()));
  ok(slApi.ok && slApi.pillar === 'self_learn' && slApi.pillar_label === '引擎自加权',
    `X7 后端支持按 self_learn 过滤（${slApi.pillar_label}，${(slApi.rows || []).length} 行）`);
  const slRaw = await page.evaluate(() => fetch('/api/pillars').then((r) => r.json()));
  const st = (slRaw.pillars && slRaw.pillars.self_learn && slRaw.pillars.self_learn.state) || {};
  const ws = Object.values(st.weights || {});
  ok(ws.length === 4 && Math.abs(ws.reduce((a, b) => a + b, 0) - 1) < 1e-6,
    `X8 权重归一（4 根、合计 ${ws.reduce((a, b) => a + b, 0).toFixed(3)}）`);
  const minS = Math.min(...Object.values(st.per_pillar || {}).map((x) => x.samples));
  ok(!st.weights_effective || minS >= st.gate,
    `X9 诚实性红线：权重生效必须每根支柱样本都达门槛（生效=${st.weights_effective}，最小样本=${minS}，门槛=${st.gate}）`);

  console.log('\n[4] ③④ 日期按钮 + 周期按钮');
  const btns = await page.evaluate(() => ({
    dates: [...document.querySelectorAll('#p-prediction .bt-date')].map((x) => x.textContent.trim()),
    periods: [...document.querySelectorAll('#p-prediction .bt-period')].map((x) => x.textContent.trim()),
    activeDate: (document.querySelector('#p-prediction .bt-date.active') || {}).textContent,
    activePeriod: (document.querySelector('#p-prediction .bt-period.active') || {}).textContent,
  }));
  ok(btns.dates.length >= 1, `D1 日期按钮渲染（${btns.dates.length} 个：${btns.dates.slice(0, 9).join(" ")}）`);
  ok(btns.periods.join("|") === "T+1|T+3|T+5|T+10|T+20",
    `P1 周期按钮 = T+1/3/5/10/20（实测 ${btns.periods.join(" ")}）`);
  ok(btns.activeDate && btns.activePeriod, `D2 默认有选中日期与周期（${btns.activeDate} / ${btns.activePeriod}）`);

  console.log('\n[4b] 默认必须落在「有已到期样本」的一期（否则打开就是空面板）');
  const def = await page.evaluate(() => fetch('/api/backtest').then((r) => r.json()));
  const fullest = def.dates.reduce((a, b) => (b.elapsed > a.elapsed ? b : a));
  ok(def.auto && (def.auto.date || def.auto.period), `A1 后端带 auto 标记（${JSON.stringify(def.auto)}）`);
  ok(def.summary.n > 0,
    `A2 默认就有已到期样本：${def.date} · T+${def.period} · 样本 ${def.summary.n} 只 · 命中率 ` +
    `${def.summary.hit_rate === null ? "—" : (def.summary.hit_rate * 100).toFixed(1) + "%"}`);
  // 2026-10-15 改：默认周期 = 该期**已到期的最长**周期。只走完 1 个交易日时 T+1 就是它，
  // 原断言写死 >1，会在数据回填进度变化后变成假失败（不是功能坏）。
  const _matured = def.periods.filter((p) => p <= fullest.elapsed);
  const _want = _matured.length ? Math.max(..._matured) : 1;
  ok(def.period === _want,
    `A3 默认周期 = 该期已到期的最长周期（实测 T+${def.period}，已走完 ${fullest.elapsed} 日 → 应为 T+${_want}）`);
  ok(def.date === fullest.date,
    `A4 默认日期 = 走完交易日最多的那一期（${def.date}，走完 ${fullest.elapsed} 日）`);
  const uiDef = await page.evaluate(() => ({
    kpi: [...document.querySelectorAll('#p-prediction .bt-kpi-v')].map((x) => x.textContent.trim()),
    tip: (document.querySelector('#p-prediction .bt-sec .bt-hint') || {}).textContent || '',
  }));
  ok(uiDef.kpi[0] !== '—' && uiDef.kpi[0] !== '', `A5 界面默认就有命中率（KPI：${uiDef.kpi.join(" | ")}）`);

  console.log('\n[4c] 旧「主引擎预测」数据源必须已撤（进哥：旧逻辑不对，历史回测只留四支柱）');
  const chips = await page.evaluate(() => ({
    n: document.querySelectorAll('#p-prediction .bt-chip').length,
    srcHint: document.querySelectorAll('#p-prediction .bt-srchint').length,
    title: (document.querySelector('#p-prediction .bt-title') || {}).textContent || '',
    dateTitle: (document.querySelector('#p-prediction .bt-date') || {}).getAttribute('title') || '',
  }));
  ok(chips.n === 0, `V0 数据源切换按钮已移除（实测 ${chips.n} 个）`);
  ok(chips.srcHint === 0, 'V0b 「两套算法」说明条也已撤掉');
  // 2026-10-16（进哥二次修正）：回测按支柱分页，标题要写明**当前这一页是哪根支柱**的回测
  ok(new RegExp('政策漏斗|竞价冲浪|超跌修复|共振风控|引擎自加权').test(chips.title), `V0c 板块标题写明当前支柱：${chips.title.slice(0, 44)}`);
  ok(new RegExp('政策漏斗|竞价冲浪|超跌修复|共振风控|引擎自加权').test(chips.dateTitle) && !/主引擎/.test(chips.dateTitle),
    `V0d 日期按钮 title 报的是当前支柱的只数、且不再提旧引擎（${chips.dateTitle.slice(0, 60)}）`);

  console.log('\n[5] ⑤左右分栏逐行对齐');
  const geo = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('#p-prediction .bt-row')].slice(0, 6).map((r) => {
      const cl = r.querySelector('.bt-cl').getBoundingClientRect();
      const cr = r.querySelector('.bt-cr').getBoundingClientRect();
      const rb = r.getBoundingClientRect();
      const cy = (x) => Math.round((x.top + x.bottom) / 2);
      return { clTop: Math.round(cl.top), crTop: Math.round(cr.top),
               dTop: Math.abs(cl.top - cr.top),
               dCenter: Math.abs(cy(cl) - cy(cr)),
               clIn: cl.top >= rb.top - 1 && cl.bottom <= rb.bottom + 1,
               crIn: cr.top >= rb.top - 1 && cr.bottom <= rb.bottom + 1,
               clRight: Math.round(cl.right), crLeft: Math.round(cr.left),
               cells: r.querySelectorAll('.bt-c').length };
    });
    const body = document.querySelector('#p-prediction .bt-grid-body');
    return { rows, bodyMaxH: body ? getComputedStyle(body).maxHeight : null,
             bodyOvf: body ? getComputedStyle(body).overflowY : null };
  });
  // 两栏是 align-items:center 垂直居中的网格项，高度不等 → 应比**中心线**，不是 top
  ok(geo.rows.length > 0 && geo.rows.every((r) => r.dCenter <= 2),
    'S1 每行左右两栏**同一行内居中对齐**（中心线差 ≤2px：' +
    geo.rows.map((r) => r.dCenter).join('/') + '）');
  ok(geo.rows.length > 0 && geo.rows.every((r) => r.clIn && r.crIn),
    'S1b 左右两栏都完整落在同一行盒子内（无错位/溢出）');
  ok(geo.rows.every((r) => r.clRight <= r.crLeft + 1),
    'S2 左栏（预测）与右栏（回测）横向不重叠，确实是左右分栏');
  ok(geo.rows.every((r) => r.cells === 5),
    `S3 右栏每行 5 个周期单元格（实测 ${geo.rows.map((r) => r.cells).join("/")}）`);
  ok(geo.bodyMaxH === 'none' && geo.bodyOvf === 'visible',
    `S4 回测列表也不限高内滚，跟着整页往下流（max-height=${geo.bodyMaxH} / overflow-y=${geo.bodyOvf}）`);

  console.log('\n[6] ⑥下方总结区');
  const sum = await page.evaluate(() => {
    const k = [...document.querySelectorAll('#p-prediction .bt-kpi')].map((x) => ({
      v: x.querySelector('.bt-kpi-v').textContent.trim(), k: x.querySelector('.bt-kpi-k').textContent.trim(),
    }));
    return { kpis: k, verdict: (document.querySelector('#p-prediction .bt-verdict') || {}).textContent,
             strip: document.querySelectorAll('#p-prediction .bt-psum').length,
             title: (document.querySelector('#p-prediction .bt-sum-h') || {}).textContent };
  });
  ok(sum.kpis.length === 4, `U1 总结区 4 个 KPI（${sum.kpis.map((x) => x.k + "=" + x.v).join(", ")}）`);
  ok(/命中率/.test(sum.kpis.map((x) => x.k).join("")) && /平均超额/.test(sum.kpis.map((x) => x.k).join("")),
    'U2 KPI 含「命中率」「平均超额」');
  ok(!!sum.verdict && sum.verdict.length > 6, `U3 有当日判读文字：${(sum.verdict || "").slice(0, 60)}…`);
  ok(sum.strip === 5, `U4 各周期一览 5 格（实测 ${sum.strip}）`);

  console.log('\n[7] ④切周期：右栏高亮列 + 总结同步');
  await page.evaluate(() => btSet('period', 3));
  await new Promise((r) => setTimeout(r, 250));
  const p3 = await page.evaluate(() => ({
    active: (document.querySelector('#p-prediction .bt-period.active') || {}).textContent,
    onCount: document.querySelectorAll('#p-prediction .bt-row .bt-c.on').length,
    title: (document.querySelector('#p-prediction .bt-sum-h') || {}).textContent,
  }));
  ok(p3.active === 'T+3', `P2 点 T+3 后按钮高亮转移（active=${p3.active}）`);
  ok(p3.onCount > 0, `P3 右栏每行高亮列跟随切换（${p3.onCount} 个单元格高亮）`);
  ok(/T\+3/.test(p3.title || ''), `P4 总结区标题跟随周期（${(p3.title || "").trim()}）`);

  console.log('\n[8] 诚实性红线：未到期必须标「还差 N 日」，绝不填 0');
  await page.evaluate(() => btSet('period', 20));
  await new Promise((r) => setTimeout(r, 250));
  const api20 = await page.evaluate(() => fetch('/api/backtest?period=20').then((r) => r.json()));
  const c20 = api20.rows.flatMap((r) => r.cells.filter((c) => c.p === 20));
  ok(c20.length > 0 && c20.every((c) => c.status !== 'ok'),
    `T1 当前 T+20 无一到期（${c20.length} 个单元格，全部非 ok）—— 与"本地最长只走完 ${api20.elapsed} 个交易日"一致`);
  ok(c20.filter((c) => c.status === 'pending').every((c) => c.need > 0 && c.ret === null),
    'T2 未到期单元格带 need>0 且 ret=null（不假造数值）');
  ok(api20.summary.n === 0 && /尚无已到期样本/.test(api20.summary.verdict || ''),
    `T3 无样本时判读如实说"尚无已到期样本"，而非给 0（verdict=${(api20.summary.verdict || '').slice(0, 40)}…）`);
  const ui20 = await page.evaluate(() => {
    const cells = [...document.querySelectorAll('#p-prediction .bt-row .bt-c')]
      .filter((x) => x.querySelector('em').textContent.trim() === 'T+20');
    return { n: cells.length, pend: cells.filter((x) => x.classList.contains('bt-pend')).length,
             txt: cells.slice(0, 3).map((x) => x.textContent.trim()) };
  });
  ok(ui20.n > 0 && ui20.pend === ui20.n,
    `T4 界面 T+20 列全部显示「N日」待期样式（${ui20.txt.join(" | ")}）`);

  console.log('\n[9] ③切日期：左栏名单随之变化');
  const d2 = await page.evaluate(() => {
    const list = [...document.querySelectorAll('#p-prediction .bt-date')];
    const before = [...document.querySelectorAll('#p-prediction .bt-nm')].slice(0, 3).map((x) => x.textContent.trim());
    // 2026-10-15 改：必须点**非当前高亮**的那个日期（原先点 list[1]，而四支柱只有 2 期时
    // 自动选中的恰好就是 list[1] → 点了等于没点，形成假失败）
    const other = list.find((x) => !x.classList.contains('active'));
    if (!other) return null;
    other.click();
    return { before, clicked: other.textContent.trim() };
  });
  if (d2) {
    await new Promise((r) => setTimeout(r, 1200));
    const after = await page.evaluate(() => ({
      active: (document.querySelector('#p-prediction .bt-date.active') || {}).textContent,
      names: [...document.querySelectorAll('#p-prediction .bt-nm')].slice(0, 3).map((x) => x.textContent.trim()),
      date: (document.querySelector('#p-prediction .bt-sum-h') || {}).textContent,
    }));
    ok(after.names.length > 0 && after.names.join() !== d2.before.join(),
      `D3 切到下一天后名单确实变了（${d2.before[0]} → ${after.names[0]}）`);
    ok(/2026-/.test(after.date || ''), `D4 总结区日期同步（${(after.date || "").trim()}）`);
  } else {
    ok(false, 'D3 只有一个日期，无法验证切换');
  }

  console.log('\n[10] 四支柱数据（已无源切换，进来就是它）');
  const pv = await page.evaluate(() => ({
    tags: [...new Set([...document.querySelectorAll('#p-prediction .bt-tag')].map((x) => x.textContent.trim()))],
    rows: document.querySelectorAll('#p-prediction .bt-row').length,
  }));
  ok(pv.rows > 0, `V2 四支柱源有可回测标的（${pv.rows} 行）`);
  ok(pv.tags.some((t) => /政策漏斗|竞价冲浪|超跌修复|共振风控|引擎自加权/.test(t)),
    `V3 左栏带四支柱分组标签（${pv.tags.slice(0, 5).join("、")}）`);

  console.log('\n[11] 截图（复位到打开时的默认视图，定格在有数据的画面）');
  await page.evaluate(() => {
    // 复位到"打开页面时的默认视图" = 第一个支柱（政策漏斗）+ 默认日期/周期
    if (typeof PRED !== 'undefined') PRED.ptab = 'policy_funnel';
    BT.source = 'pillars'; BT.date = null; BT.period = 0;
    BT.periodLocked = false; BT.data = null; BT.err = null;
    if (typeof renderPrediction === 'function') renderPrediction(); else loadBacktest();
  });
  await new Promise((r) => setTimeout(r, 2000));
  // 取景＝"滚到底"，与进哥实际看结果的位置一致（避免截图取景与实际使用脱节）
  const shot = await page.evaluate(() => {
    const sc = document.scrollingElement;
    sc.scrollTop = sc.scrollHeight;
    const g = (sel) => {
      const e = document.querySelector('#p-prediction ' + sel);
      if (!e) return null;
      const r = e.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
    };
    return { bar: g('.bt-stickybar'), dates: g('.bt-dates'), vh: window.innerHeight,
             kpi: [...document.querySelectorAll('#p-prediction .bt-kpi-v')].map((x) => x.textContent.trim()) };
  });
  await new Promise((r) => setTimeout(r, 500));
  ok(shot.kpi.length === 4 && shot.kpi[0] !== '—' && shot.kpi[0] !== '',
    `A6 复位后默认视图仍有数据（KPI：${shot.kpi.join(" | ")}）`);
  ok(shot.dates && shot.dates.top >= 8 && shot.dates.bottom <= shot.vh,
    `H4 截图状态（滚到底）下日期行同时可见（top=${shot.dates && shot.dates.top} / 视口=${shot.vh}）`);
  // 2026-10-16：整页流动后板块标题行本来就该被滚走（页面变长是进哥要的），
  // 该守的是「钉住条（日期/周期行）在滚到底时仍可见」——它接管了原标题行的"我在这"作用。
  ok(shot.bar && shot.bar.top >= 0 && shot.bar.bottom <= shot.vh,
    `H5 滚到底时钉住条（日期/周期行）可见（top=${shot.bar && shot.bar.top} bottom=${shot.bar && shot.bar.bottom}）`);
  try { await page.screenshot({ path: SHOT, fullPage: false }); console.log(`    截图 → ${SHOT}`); }
  catch (e) { console.log('    截图失败：' + e.message); }

  console.log('\n[12] 界面已彻底移除旧引擎（进哥：不要让旧的再出现在界面上）');
  const purge = await page.evaluate(() => {
    const navText = [...document.querySelectorAll(".sidenav a, .sidenav button, .sidenav li, .sidenav div")]
      .map((x) => x.textContent || "").join(" ");
    return { hasGrowthNav: /成长总览/.test(navText),
             hasGrowthPanel: !!document.querySelector("#p-growth") };
  });
  ok(!purge.hasGrowthNav, "U1 左栏导航里已无「成长总览」入口");
  ok(!purge.hasGrowthPanel, "U2 DOM 里已无 #p-growth 面板（旧引擎专属面板）");
  const legacy = reqs.filter((u) => /\/api\/(prediction|growth)\b/.test(u));
  ok(legacy.length === 0,
    `U3 页面不再请求任何旧引擎接口（实测 ${legacy.length} 次）${legacy.slice(0, 3).join(" , ")}`);

  ok(errs.length === 0, `F 控制台零报错${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}`);

  await browser.close();
  console.log('\n' + (fails.length
    ? `BACKTEST FAIL (${fails.length})\n` + fails.map((f) => ' - ' + f).join('\n')
    : 'BACKTEST_PASS'));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('脚本异常：', e); process.exit(2); });
