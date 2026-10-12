// 全量点开审计：遍历当前 NAV 全部 7 大类 25 子项，每个面板检查真实渲染内容，
// 并钻入代表性节点（个股 chip / 分支弹窗 / 板块详情 / 信号抽屉 / 基金窗 / 巴菲特研报 iframe）
// 验证「点开后有真实生成物」。收集控制台报错与非 2xx。
// 用法：node audit_clickthrough.cjs   （复用已在 8765 运行的后端）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 每个子项：[cat, tab, 中文名, 优先入口选择器(数组), 特殊]
const TABS = [
  // 2026-10-12 体验审计 P0-A：新增「今日决策」默认落地页（四卡 + 一句话结论）
  ['overview', 'today', '今日决策', ['.td-sym'], null],
  ['overview', 'dashboard', '今日看板', ['.ml-branch', '.stk-card[data-sym]'], null],
  // 2026-10-12 体验审计 P0-D/E/F：三个大类各自新增的置顶「结论页」
  ['info', 'info_brief', '消息面汇总', ['.td-sym'], null],
  ['players', 'smart_money', '聪明钱共识', ['.td-sym'], null],
  ['watch', 'watch_brief', '今日提示', ['.td-sym'], null],
  ['overview', 'summary', '汇总', ['.kline-chip[data-sym]', '.wt-sym[data-wt-open]'], null],
  ['overview', 'prediction', '每日预测', ['.kline-chip[data-sym]'], null],
  ['money', 'lhb', '游资龙虎榜', ['.kline-chip[data-sym]'], null],
  ['money', 'lhb_sectors', '板块动向', ['.ol-chip', '.bf-bn[data-bf-open]'], null],
  ['money', 'stocks', '当前讨论股票', ['.stk-card[data-sym]'], null],
  ['money', 'sentiment', '舆论情绪', ['.stk-card[data-sym]'], null],
  ['money', 'moneyflow', '资金流向', ['.bf-bn[data-bf-open]', '.bf-mem[data-wt-open]'], null],
  ['info', 'topics', '近期热点', ['.stk-mini[data-sym]', '.stk[data-sym]'], null],
  ['info', 'geo_events', '国际局势', [], null],
  ['info', 'geo_policy', '全球政策雷达', [], null],
  ['info', 'geo_tech', '科技前沿突破', [], null],
  ['info', 'overseas_sentiment', '美股港股舆情', ['[data-ov-sym]'], null],
  ['info', 'overseas_hotspots', '国外驱动热点', ['.stk[data-hp-sym]'], null],
  ['info', 'policy', '政策风向', ['.pol-stk', '.pol-item'], null],
  ['info', 'news', '个股快讯公告', ['.wt-sym[data-wt-open]'], null],
  ['players', 'players', '选手与渠道', ['tr[data-sym]', '.stk[data-sym]'], null],
  ['players', 'operating', '正在操作股票', ['.stk[data-sym]'], null],
  ['players', 'niusan', '牛散追踪', ['.stk[data-sym]'], null],
  ['players', 'consensus', '共同持仓共识', ['tr[data-sym]'], null],
  ['players', 'cohold', '近5日共同买入', ['tr[data-sym]'], null],
  ['players', 'persons', '相关人物', ['.stk[data-sym]'], null],
  ['opinion', 'extref_soft', '软件荐股雷达', ['.ex-signal', '.ex-src'], null],
  ['opinion', 'extref_comm', '社区共识', ['.ex-signal'], null],
  ['opinion', 'extref_rank', '信源胜率榜', ['.ex-rank-row', '.ex-src'], null],
  ['opinion', 'research', '研报评级', ['.wt-sym[data-wt-open]'], null],
  ['watch', 'watch', '关注池', ['tr[data-sym]', '.wt-sym[data-wt-open]'], null],
  ['watch', 'watch_alerts', '到价提醒', [], null],
  ['watch', 'screener', '智能选股', ['.wt-sym[data-wt-open]'], null],
  // 2026-10-12 补：入手概率一直在 TABS 里缺位，改了它的「最值得入手」口径后必须纳入全量审计
  ['watch', 'rate', '入手概率', ['.wt-sym'], null],
  ['watch', 'diag', '个股诊断', [], 'diag'],
  ['watch', 'multiquote', '多资产', [], null],
  ['learn', 'strategy_lab', '策略回测', ['.sl-item'], null],
  ['learn', 'learn_tracks', '学习轨道管理', ['.kb-track-item', '.kb-src'], null],
];

function pickChrome() {
  try { fs.accessSync(CHROME); return CHROME; } catch (e) {}
  try { fs.accessSync(EDGE); return EDGE; } catch (e) {}
  return CHROME;
}

// 通用：钻入第一个入口元素，检测打开了什么、是否有内容
async function drill(page, entrySels) {
  return await page.evaluate((sels) => {
    const findFirst = (root) => {
      for (const s of sels) {
        const el = root.querySelector(s);
        if (el && el.offsetParent !== null) return el;
      }
      return null;
    };
    const host = document.querySelector('#p-' + (window.__curTab || '')) || document;
    const el = findFirst(host) || findFirst(document);
    if (!el) return { clicked: false, reason: 'no-entry' };
    const tag = el.tagName + (el.dataset.sym ? '[sym=' + el.dataset.sym + ']' : '') + (el.className ? '.' + String(el.className).split(' ')[0] : '');
    try { el.click(); } catch (e) { return { clicked: false, reason: 'click-throw:' + e.message, tag }; }
    return { clicked: true, tag };
  }, entrySels);
}

async function detectOpen(page) {
  return await page.evaluate(() => {
    const vis = (el) => { if (!el) return false; const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.01; };
    const drawer = document.querySelector('#drawer');
    const dShow = drawer && drawer.classList.contains('show');
    const dBody = document.querySelector('#drawerBody');
    const dLen = dBody ? (dBody.textContent || '').replace(/\s+/g, ' ').trim().length : 0;
    const dIframe = dBody ? dBody.querySelector('iframe') : null;
    const branch = document.querySelector('#branchModal');
    const branchVis = branch && vis(branch) && !branch.classList.contains('bm-hidden');
    const fund = document.querySelector('#fundModal');
    const fundVis = fund && vis(fund);
    const open = dShow || branchVis || fundVis;
    let what = 'none';
    if (dShow && dIframe) what = 'drawer-iframe';
    else if (dShow) what = 'drawer';
    else if (branchVis) what = 'branchModal';
    else if (fundVis) what = 'fundModal';
    return { open, what, dLen, dShow: !!dShow, branchVis: !!branchVis, fundVis: !!fundVis };
  });
}

async function closeAll(page) {
  await page.evaluate(() => {
    const vis = (el) => el && getComputedStyle(el).display !== 'none';
    const drawer = document.querySelector('#drawer');
    const branch = document.querySelector('#branchModal');
    const fund = document.querySelector('#fundModal');
    if (drawer && drawer.classList.contains('show')) {
      const c = drawer.querySelector('.drawer-close, #drawerClose, [class*="close"]');
      if (c) c.click(); else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    }
    if (branch && vis(branch)) { const c = branch.querySelector('[class*="close"], .bm-x'); if (c) c.click(); }
    if (fund && vis(fund)) { const c = fund.querySelector('[class*="close"]'); if (c) c.click(); }
    // 兜底 Esc
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  });
  await sleep(400);
}

async function panelInfo(page, tab) {
  return await page.evaluate((t) => {
    const box = document.querySelector('#p-' + t);
    if (!box) return { found: false };
    const txt = (box.textContent || '').replace(/\s+/g, ' ').trim();
    const err = (box.querySelector('.err, .error, .panel-error, .load-error, [class*="err"]')?.textContent || '').trim();
    return {
      found: true,
      active: box.classList.contains('active'),
      len: txt.length,
      head: txt.slice(0, 120),
      tables: box.querySelectorAll('table').length,
      cards: box.querySelectorAll('.card, .ml-card, .stk-card, .ex-signal, .kb-term, .kb-src, .sl-item, .bf-report').length,
      empty: !!box.querySelector('.empty'),
      loading: /加载中|加载失败|获取失败|出错了|error/i.test(txt.slice(0, 200)),
      errText: err.slice(0, 120),
      // 该面板内可点击个股/节点入口数量
      entries: box.querySelectorAll('[data-sym], [data-wt-open], [data-feat-open], [data-ol-sym], [data-ov-sym], [data-hp-sym], [data-bf-open], .ml-branch, .ol-chip, .ex-signal').length,
    };
  }, tab);
}

(async () => {
  const exe = pickChrome();
  console.log('CHROME =', exe);
  const browser = await puppeteer.launch({
    executablePath: exe, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--ignore-certificate-errors', '--disable-blink-features=AutomationControlled',
      '--proxy-server=direct://', '--proxy-bypass-list=*', '--window-size=1600,1000'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });

  const errs = [];
  const bad = [];
  page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + e.message.slice(0, 200)));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('[CONSOLE] ' + m.text().slice(0, 200)); });
  page.on('response', (r) => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url().replace(URL, '')); });

  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(2500);

  const rows = [];
  let diagKlineInk = 0;      // diag 专项：K 线 canvas 的非透明像素数（>1000 视为真的画出来了）
  for (const [cat, tab, label, entrySels, special] of TABS) {
    errs.length = 0;
    const before = errs.length;
    // 导航
    if (special === 'diag') {
      await page.evaluate((c, t) => { location.hash = '#' + c + '/' + t; }, cat, tab);
      await sleep(2600);
      // 填代码并诊断（注意：必须用精确 id —— 面板里第一个 button 是「🔄 重新诊断」，
      // 旧写法 `#p-diag button` 会点到它，DIAG.symbol 为空 → 面板停在初始提示，护栏形同虚设）
      await page.evaluate(() => {
        const inp = document.querySelector('#dgSym');
        if (inp) { inp.value = '600519'; inp.dispatchEvent(new Event('input', { bubbles: true })); }
        const btn = document.querySelector('#dgGo');
        if (btn) btn.click();
      });
      // 个股诊断首诊并行拉行情/财务/估值/资金/公告 + K 线 + F10；东财限流时可达 10s+。
      // 自适应轮询：直到面板有内容且 K 线 canvas 画出像素（或命中"诊断失败"），最长 22s。
      for (let i = 0; i < 22; i++) {
        await sleep(1000);
        const s = await page.evaluate(() => {
          const b = document.querySelector('#p-diag');
          const txt = b ? (b.textContent || '').replace(/\s+/g, ' ').trim() : '';
          let ink = 0;
          try { const cv = document.querySelector('#dgKlineCanvas'); if (cv) { const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; for (let k = 3; k < d.length; k += 4) if (d[k] > 0) ink++; } } catch (e) {}
          return { len: txt.length, ink, fail: /诊断失败/.test(txt.slice(0, 60)) };
        });
        if (s.len > 500 && s.ink > 1000) break;
        if (s.fail) break;
      }
      // K 线是否真的画出来了（非空白 canvas）
      try {
        const ink = await page.evaluate(() => {
          const cv = document.querySelector('#dgKlineCanvas');
          if (!cv) return 0;
          const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
          let n = 0;
          for (let k = 3; k < d.length; k += 4) if (d[k] > 0) n++;
          return n;
        });
        diagKlineInk = ink;
      } catch (e) { diagKlineInk = -1; }
    } else {
      await page.evaluate((c, t) => { location.hash = '#' + c + '/' + t; }, cat, tab);
      // 「多资产」冷加载走东财 push2 多标的抓取，首字节约 5.6s、整表渲染约 6s，
      // 偶发被限流时更慢；给足等待，避免把"慢"误判为"打不开"。
      await sleep(tab === 'multiquote' ? 7000 : 3000);
    }
    let info = await panelInfo(page, tab);
    // 防抖动：面板已就位但内容是空的（且不在 loading）→ 稍等再测。
    if (info.found && !info.loading && info.len < 40) {
      await sleep(tab === 'multiquote' ? 12000 : 5000);
      info = await panelInfo(page, tab);
    }
    // 钻节点
    let drillRes = { clicked: false, reason: 'skip' }, open = { open: false, what: 'none' };
    if (entrySels && entrySels.length && info.found && !info.empty) {
      await page.evaluate((t) => { window.__curTab = t; }, tab);
      drillRes = await drill(page, entrySels);
      if (drillRes.clicked) {
        // 等待打开（最多 9s）
        for (let i = 0; i < 18; i++) {
          await sleep(500);
          open = await detectOpen(page);
          if (open.open) break;
        }
      }
      await closeAll(page);
      await sleep(500);
    }
    const entryCount = info.entries || 0;
    rows.push({ cat, tab, label, info, drill: drillRes, open, errs: errs.slice(before),
                ...(special === 'diag' ? { klineInk: diagKlineInk } : {}) });
    const flag = (!info.found || info.loading || info.len < 40 || errs.length || (drillRes.clicked && !open.open)
      || (special === 'diag' && !(diagKlineInk > 1000)))
      ? '✗' : '✓';
    console.log(`${flag} ${label.padEnd(8)} | 面板:${info.found ? '有' : '缺'} len:${info.len} 表:${info.tables} 卡:${info.cards} 入口:${entryCount}`
      + `${special === 'diag' ? ` K线ink:${diagKlineInk}` : ''}`
      + `${info.empty ? ' [空态]' : ''}${info.loading ? ' [⚠加载/错]' : ''}`
      + ` | 钻:${drillRes.clicked ? drillRes.tag : drillRes.reason} → 开:${open.what}(${open.dLen}字)`
      + (errs.length ? ' ⚠err:' + JSON.stringify(errs.slice(-2)) : ''));
  }

  // 巴菲特研报专项（learn 固定子项，渲染进 #p-buffett；研报以 bfModal 弹层打开）
  try {
    await page.evaluate(() => { location.hash = '#learn/buffett'; });
    await sleep(3000);
    const bf = await page.evaluate(() => {
      const box = document.querySelector('#p-buffett');
      if (!box) return { rows: 0, clicked: false, exists: false };
      const cards = box.querySelectorAll('.bf-card');
      const first = box.querySelector('.bf-card .bf-open') || box.querySelector('.bf-card');
      if (first) first.click();
      return { rows: cards.length, clicked: !!first, exists: true };
    });
    let bfOpen = { open: false, what: 'none', dLen: 0 };
    for (let i = 0; i < 14; i++) {
      await sleep(500);
      bfOpen = await page.evaluate(() => {
        const m = document.querySelector('#bfModal');
        const f = m ? m.querySelector('.bf-modal-iframe') : null;
        const open = !!(m && m.classList.contains('open') && f && f.src && f.src.indexOf('about:blank') < 0);
        if (!open) return { open: false, what: 'none', dLen: 0 };
        const fr = f.contentDocument || f.contentWindow ? null : null;
        return { open: true, what: 'bfModal', dLen: 0, src: f.src };
      });
      if (bfOpen.open) break;
    }
    // 关闭弹层
    await page.evaluate(() => { const x = document.querySelector('#bfModal .bf-modal-x'); if (x) x.click(); });
    const bfInfo = await panelInfo(page, 'buffett');
    rows.push({ cat: 'learn', tab: 'buffett', label: '巴菲特选股', info: { ...bfInfo, entries: bf.rows }, drill: { clicked: bf.clicked, tag: 'bf-card' }, open: bfOpen, errs: [] });
    console.log(`${bf.exists && bf.clicked && bfOpen.open ? '✓' : '✗'} 巴菲特选股 | 期次卡:${bf.rows} 钻:${bf.clicked ? 'bf-card' : '无'} → 开:${bfOpen.what}(${bfOpen.src ? bfOpen.src.split('/').pop() : '-'})`);
  } catch (e) { console.log('✗ 巴菲特选股 异常:' + e.message.slice(0, 120)); }

  await page.screenshot({ path: 'audit_last.png' });
  const out = { generated: new Date().toISOString(), rows, badResponses: [...new Set(bad)] };
  fs.writeFileSync('audit_clickthrough_result.json', JSON.stringify(out, null, 2), 'utf8');

  const problem = rows.filter((r) => !r.info.found || r.info.loading || r.info.len < 40 || r.errs.length
    || (r.drill && r.drill.clicked && r.open && !r.open.open)
    || (r.klineInk !== undefined && !(r.klineInk > 1000)));
  console.log('\n================ 汇总 ================');
  console.log('访问子板块:', rows.length, '| 有问题:', problem.length);
  problem.forEach((r) => console.log('  ✗', r.label, '| found=' + r.info.found, 'len=' + r.info.len,
    'loading=' + r.info.loading, 'drill=' + (r.drill && r.drill.clicked) + ' open=' + (r.open && r.open.what),
    'err=' + JSON.stringify(r.errs.slice(-2))));
  console.log('非 2xx 响应:', out.badResponses.length ? out.badResponses.slice(0, 12) : '无');
  await browser.close();
  process.exit(problem.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e.stack || e.message); process.exit(2); });
