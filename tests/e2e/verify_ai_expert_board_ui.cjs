/* 今日荐股 · 合并榜的**真实页面**验收（2026-11-04 进哥需求）。
 *
 * 为什么不能只靠 verify_ai_expert_render.cjs：
 *   那个脚本在 node 里跑纯字符串函数，验证的是"生成的 HTML 对不对"，
 *   看不见 ① 样式有没有把布局撑破 ② 点击是否真的触发（事件绑定在字符串里）
 *   ③ 深色主题下分数块/文字是否还读得清。
 *
 * 数据源是**临时库**（VIEWER_DB 指向 tmp，里面只种了四家的评分样本），
 * 绝不动生产库 viewer/app.db —— 榜单要是拿真报告验，我等于替专家团编了分数。
 *
 * 用法：
 *   VIEWER_PORT=8799 VIEWER_DB=<tmp>/app.db ./.venv/Scripts/python.exe -m viewer.server &
 *   node verify_ai_expert_board_ui.cjs            # 默认 http://127.0.0.1:8799/
 *   BASE=http://127.0.0.1:8799/ OUT=docs/shot.png node verify_ai_expert_board_ui.cjs
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BASE = process.env.BASE || 'http://127.0.0.1:8799/';
const OUT = process.env.OUT || 'docs/shot_board_20261104.png';

let fail = 0;
const ok = (cond, msg) => { console.log((cond ? 'PASS ' : 'FAIL ') + msg); if (!cond) fail++; };

/* INJECT=1 用的样本（只在浏览器内存里，不落库、不进生产数据）。
   分数/理由都是编的样本，唯一目的是把「排序、达标线、共推、点击展开、抽屉带理由」这几条路走通。 */
const SAMPLE = {
  ok: true, date: '2026-11-03', latest_date: '2026-11-03',
  items: [
    { key: 'stock-partner', name: '腾讯自选股股票投研专家团', tags: ['估值定价', '资金风向'], installed: true,
      status: 'ok', stance: '中性', position: '三成以内',
      verdict: '普跌分化别追高：只做新能源链回踩，仓位三成以内',
      picks: [{ code: '600642', name: '申能股份', action: '回踩买入', score: 92 },
              { code: '600900', name: '长江电力', action: '持有', score: 88 },
              { code: '600406', name: '国电南瑞', action: '回踩买入', score: 86 }] },
    { key: 'stock-app-arch', name: '股票行情应用架构专家团', tags: ['股票行情应用'], installed: false,
      status: 'missing', stance: null, position: null, verdict: null, picks: [] },
    { key: 'finance-suite', name: '金融业务全流程专家团', tags: ['金融建模与估值'], installed: true,
      status: 'ok', stance: '偏多', position: '两成仓',
      verdict: '估值端只认现金牛，红利公用是这轮杀跌的避风港',
      picks: [{ code: '600900', name: '长江电力', action: '买入', score: 90 },
              { code: '600011', name: '华能国际', action: '回踩买入', score: 82 }] },
    { key: 'five-wave', name: '股票五浪法专家团', tags: ['波浪数浪'], installed: true,
      status: 'ok', stance: '中性', position: '观察',
      verdict: '上证处于 3 浪 4 调整，窗口未开，个股只作观察',
      picks: [{ code: '601988', name: '中国银行', action: '观察', score: 72 }] }
  ],
  board: {
    min_score: 85, n_strong: 4, n_scored: 6, n_teams: 3,
    items: [
      { key: 'stock-partner', team: '腾讯自选股股票投研专家团', code: '600642', name: '申能股份', score: 92,
        action: '回踩买入', reason: '电力+新能源双属性，今日+2.03%、成交2.82亿，在跌停141只的环境里逆势收红',
        entry: '8.85~9.05', stop: '8.70', horizon: '1-2周', strong: true, co_teams: 1 },
      { key: 'finance-suite', team: '金融业务全流程专家团', code: '600900', name: '长江电力', score: 90,
        action: '买入', reason: '股息率 3.4%，现金流覆盖资本开支，普跌日抗跌性最好',
        entry: '29.5 下方分批', stop: '28.90', horizon: '1月以上', strong: true, co_teams: 2 },
      { key: 'stock-partner', team: '腾讯自选股股票投研专家团', code: '600900', name: '长江电力', score: 88,
        action: '持有', reason: '普跌日仍+1.22%、成交41.20亿，全市场跌3504只而它红盘',
        entry: '29.50~29.90 可加', stop: '28.90', horizon: '1月以上', strong: true, co_teams: 2 },
      { key: 'stock-partner', team: '腾讯自选股股票投研专家团', code: '600406', name: '国电南瑞', score: 86,
        action: '回踩买入', reason: '电网设备龙头 +1.40%，对应十五五电算协同与光伏储能并网需求',
        entry: '23.30~23.90', stop: '22.80', horizon: '1-2周', strong: true, co_teams: 1 },
      { key: 'finance-suite', team: '金融业务全流程专家团', code: '600011', name: '华能国际', score: 82,
        action: '回踩买入', reason: 'PB 0.9 倍、煤价回落修复火电毛利，位置在三年中枢下沿',
        entry: '7.10~7.30', stop: '6.85', horizon: '1-2周', strong: false, co_teams: 1 }
    ]
  }
};

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-gpu'] });
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(String(e).slice(0, 160)));
  await p.setViewport({ width: 1500, height: 1100, deviceScaleFactor: 1 });

  await p.goto(BASE + '#overview/ai_review', { waitUntil: 'domcontentloaded', timeout: 40000 });
  await new Promise((r) => setTimeout(r, 2200));

  // 首次运行的引导弹窗会盖住整块（截图里就是它），必须先关掉，否则截出来的是弹窗不是榜单
  await p.evaluate(() => {
    try {
      localStorage.setItem('viewer-firstrun-done', '1');
      const m = document.getElementById('firstRunModal'), k = document.getElementById('firstRunMask');
      if (m) m.hidden = true;
      if (k) k.hidden = true;
    } catch (e) {}
  });

  // 页面默认可能停在 DeepSeek 分支；专家团只在 WorkBuddy 分支挂载（产品口径），
  // 所以这里显式切过去，再触发一次取数 —— 这正是用户点「WorkBuddy」时会发生的事。
  //
  // INJECT=1 模式：不读库、不写库，把一份**明确标注为测试样本**的 board 直接注入页面状态。
  // 为什么需要它：生产库里 11-03 的报告是评分功能上线前写的，没有分数，榜单自然是空的；
  // 而为了一次截图去起一个带空库的第二个服务实例，那个实例会因缺表在预热后自己退出（实测）。
  // 注入只改浏览器内存里的 AIE.data，服务端一行数据都没动。
  if (process.env.INJECT) {
    await p.evaluate((sample) => {
      try { AIR.provider = 'workbuddy'; } catch (e) {}
      try { AIE.data = sample; AIE.asked = true; paintAiExpert(); } catch (e) {}
    }, SAMPLE);
  } else {
    await p.evaluate(() => {
      try { AIR.provider = 'workbuddy'; } catch (e) {}
      try { AIE.data = null; AIE.asked = false; paintAiExpert(); } catch (e) {}
    });
  }
  await p.waitForSelector('#air-expert .aie-board', { timeout: 20000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 900));

  const info = await p.evaluate(() => {
    const box = document.getElementById('air-expert');
    if (!box) return { missing: true };
    const rows = [...box.querySelectorAll('.aie-brow')];
    const board = box.querySelector('.aie-board');
    const first = rows[0];
    return {
      missing: false,
      rows: rows.length,
      scores: rows.map((r) => parseInt((r.querySelector('.aie-score') || {}).textContent || '0', 10)),
      hot: rows.filter((r) => r.classList.contains('hot')).length,
      hasBoardTitle: !!(board && board.textContent.includes('今日荐股')),
      overflowX: board ? board.scrollWidth - board.clientWidth : -1,
      firstText: first ? first.innerText.slice(0, 120) : '',
      droppedCards: box.querySelectorAll('.aie-card').length,
    };
  });

  ok(!info.missing, '#air-expert 节点存在');
  ok(info.hasBoardTitle, '页面上出现「今日荐股 · 合并榜」');
  ok(info.rows >= 5, `榜单行数 ≥5（实际 ${info.rows}）`);
  ok(info.hot >= 1, `有达标（≥85）的行（实际 ${info.hot}）`);
  const desc = info.scores.every((v, i, a) => i === 0 || a[i - 1] >= v);
  ok(desc, '分数严格降序');
  ok(info.overflowX <= 1, `榜单横向无溢出（scrollWidth-clientWidth=${info.overflowX}）`);
  ok(info.droppedCards === 4, `四家卡仍在（实际 ${info.droppedCards}）`);

  // 点第一行 → 展开买点/止损；只该展开一行
  const opened = await p.evaluate(() => {
    const r = document.querySelector('#air-expert .aie-brow');
    if (!r) return { ok: false };
    r.click();
    const open = document.querySelectorAll('#air-expert .aie-brow.open');
    return { ok: true, open: open.length, x: (open[0] || {}).textContent.includes('买点') };
  });
  ok(opened.open === 1 && opened.x, '点一行只展开这一行的买点/止损');

  // 点代码 → 开个股详情（openStock 会渲染 drawer）
  const drawer = await p.evaluate(async () => {
    const c = document.querySelector('#air-expert .aie-code');
    if (!c) return { ok: false, why: 'no code' };
    c.click();
    await new Promise((r) => setTimeout(r, 1600));
    const dr = document.querySelector('.drawer, #drawer, .stk-drawer');
    return { ok: !!dr, code: c.textContent.trim(),
             shown: dr ? dr.textContent.includes(c.textContent.trim()) : false };
  });
  ok(drawer.ok && drawer.shown, `点代码 ${drawer.code} 打开了个股详情`);

  // 抽屉里必须带出「谁推的/多少分/为什么」——这是进哥的原始诉求（点开个股能看见推荐理由）
  const drBox = await p.evaluate(() => {
    const d = document.querySelector('#drawer .aie-dr');
    return d ? { has: true, txt: d.innerText.replace(/\s+/g, ' ').slice(0, 200) } : { has: false };
  });
  ok(drBox.has, '个股抽屉里出现「专家团理由」块');
  ok(drBox.has && drBox.txt.includes('家提到'), '抽屉理由块写明几家提到这只票');
  if (drBox.has) {
    const dr = await p.$('#drawer');
    if (dr) await dr.screenshot({ path: OUT.replace(/\.png$/, '_drawer.png') });
    console.log('SHOT ' + OUT.replace(/\.png$/, '_drawer.png'));
  }

  // 截图前收掉抽屉和弹窗：截图要证明的是"榜单长什么样"，不是"抽屉能打开"
  await p.evaluate(() => {
    try { closeDrawer(); } catch (e) {}
    const m = document.getElementById('firstRunModal'), k = document.getElementById('firstRunMask');
    if (m) m.hidden = true;
    if (k) k.hidden = true;
    // 展开状再留一行展开态，方便看清「点开才出现的买点/止损」
  });
  await new Promise((r) => setTimeout(r, 400));
  const el = await p.$('#air-expert');
  if (el) await el.screenshot({ path: OUT });
  await p.screenshot({ path: OUT.replace(/\.png$/, '_full.png') });
  console.log('SHOT ' + OUT);

  const bad = errs.filter((s) => !/ResizeObserver|favicon/i.test(s));
  ok(bad.length === 0, `无 JS 报错${bad.length ? '：' + bad.join(' | ') : ''}`);

  await b.close();
  console.log(fail ? ('RESULT FAIL ' + fail) : 'RESULT PASS');
  process.exit(fail ? 1 : 0);
})();
