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
  // 2026-10-15 新增：博主共振（内置四支柱：个股共振/战法体系/信源画像/自我提升）
  // 2026-10-31：博主共振改为**不钻取**。实测该面板 30,050 字里 `[data-sym]`/`.stk`/`.chip` 全为 0，
  //   即它是一张纯展示页（战法表 + 回测档案 + 图谱条目），本来就没有可点的个股节点；
  //   原配的 `.res-card` 是一张无点击事件的展示卡，点它必然 open:none → 每轮假红。
  ['overview', 'resonance', '博主共振', [], null],
  ['money', 'lhb', '游资龙虎榜', ['.kline-chip[data-sym]'], null],
  ['money', 'lhb_sectors', '板块动向', ['.ol-chip', '.bf-bn[data-bf-open]'], null],
  ['money', 'stocks', '当前讨论股票', ['.stk-card[data-sym]'], null],
  ['money', 'sentiment', '舆论情绪', ['.stk-card[data-sym]'], null],
  ['money', 'moneyflow', '资金流向', ['.bf-bn[data-bf-open]', '.bf-mem[data-wt-open]'], null],
  // 2026-10-15 补：龙头全景（P2-1 从一级分类折叠进「钱往哪走」，此前漏在 TABS 外）
  ['money', 'industry-leaders', '龙头全景', ['.il-row', 'tr[data-sym]'], null],
  ['info', 'topics', '近期热点', ['.stk-mini[data-sym]', '.stk[data-sym]'], null],
  // 2026-10-15 P2-1 已把 全球政策雷达/科技前沿突破 合并进「国际局势」子导航、
  // 国外驱动热点 合并进「海外舆情」子导航 —— TABS 里的旧三条已删（否则跑到空白面板误报）
  ['info', 'geo_events', '国际局势', [], null],
  ['info', 'overseas_sentiment', '海外舆情', ['[data-ov-sym]'], null],
  ['info', 'policy', '政策风向', ['.pol-stk', '.pol-item'], null],
  ['info', 'news', '个股快讯公告', ['.wt-sym[data-wt-open]'], null],
  // 2026-10-15 补：市场情绪（B站视频语「情绪/大势」堆，此前漏在 TABS 外）
  // 2026-10-31 修正：本项原写死 'info'，但 10-17 架构重排后 bili_sentiment 已归「资金动向」(money)，
  //   于是巡检一直在访问 #info/bili_sentiment —— navFromHash 找不到该 tab 就落回首项，
  //   面板从未被渲染，报「len=0」是**巡检自己的映射过期**，不是面板坏了。
  ['money', 'bili_sentiment', '市场情绪', ['.bl-item', '.bp-item', '.stk[data-sym]'], null],
  ['players', 'players', '选手与渠道', ['tr[data-sym]', '.stk[data-sym]'], null],
  ['players', 'operating', '正在操作股票', ['.stk[data-sym]'], null],
  ['players', 'niusan', '牛散追踪', ['.stk[data-sym]'], null],
  ['players', 'consensus', '共同持仓共识', ['tr[data-sym]'], null],
  ['players', 'cohold', '近5日共同买入', ['tr[data-sym]'], null],
  ['players', 'persons', '相关人物', ['.stk[data-sym]'], null],
  // 2026-10-15 P2-1 已把 社区共识/信源胜率榜 合并进「外部荐股验证」面板内子导航
  // 2026-10-31：以下三条**已从巡检移除**——app.js 记录「2026-10-29 进哥拍板：博主荐股 /
  //   外部荐股验证 / 当红参赛股 三页已物理删除」（NAV 里不再有这三个 tab，opinion 只剩研报评级）。
  //   保留检测只会每轮报「面板:缺」，是尺子对不上现实，不是功能坏。
  // ['opinion', 'extref_soft', '外部荐股验证', ['.ex-signal', '.ex-src'], null],   // 已下线
  // ['opinion', 'blogger', '博主荐股', ['.bl-row', 'tr[data-sym]'], null],          // 已下线
  // ['opinion', 'contest', '当红参赛股', ['tr[data-sym]', '.ct-row'], null],       // 已下线
  ['opinion', 'research', '研报评级', ['.wt-sym[data-wt-open]', '.wt-code[data-wt-open]'], null],
  ['watch', 'watch', '关注池', ['tr[data-sym]', '.wt-sym[data-wt-open]'], null],
  ['watch', 'watch_alerts', '到价提醒', [], null],
  ['watch', 'screener', '智能选股', ['.wt-sym[data-wt-open]'], null],
  // 2026-10-12 补：入手概率一直在 TABS 里缺位，改了它的「最值得入手」口径后必须纳入全量审计
  ['watch', 'rate', '入手概率', ['.wt-sym'], null],
  ['watch', 'diag', '个股诊断', [], 'diag'],
  ['watch', 'multiquote', '多资产', [], null],
  // 2026-10-13 补：板块二「长期低位震荡」两页签（lowbox 扫描表 / lowbox_radar 突破雷达）纳入全量审计
  // 2026-10-31 修假红：原选择器写 `.wt-sym[data-wt-open]`，但 lowbox 表里的代码链接是
  //   `<a class="wt-code" data-wt-open=…>`（app.js lowboxTable），从不匹配 → 一路退到
  //   `[data-lb-add]`（「加入自选」按钮，本来就不开弹层）→ 每轮报「钻:BUTTON.btn → 开:none」。
  ['watch', 'lowbox', '长期低位', ['.wt-code[data-wt-open]', '[data-wt-open]', '[data-lb-more]', '[data-lb-add]'], null],
  ['watch', 'lowbox_radar', '突破雷达', [], null],
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
    const tab = window.__curTab || '';
    const host = document.querySelector('#p-' + tab) || document;
    let el = findFirst(host);
    // 2026-10-31：入口常被收在面板内的 <details> 里（数据表折叠），此时 offsetParent 为空，
    //   旧实现会 fallback 到**整个 document**，于是点到导航栏/刷新按钮上，
    //   报出「钻:BUTTON.btn → 开:none」这种既不是功能坏、也定位不到问题的假红。
    //   改为：先在面板内展开所有 details 再找一次；仍找不到就诚实报 no-visible-entry，不跨面板乱点。
    if (!el && host !== document) {
      host.querySelectorAll('details:not([open])').forEach((d) => { d.open = true; });
      el = findFirst(host);
    }
    if (!el) return { clicked: false, reason: host === document ? 'no-entry' : 'no-visible-entry' };
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
      // 2026-10-15：把「加载态占位」文案也识别成 loading（否则占位 len 很短 → 被误判成"面板空白"）。
      // 项目里各面板的占位文案：selectNav 的统一占位「正在加载数据…」+ 策略回测「正在读取策略回测数据…」
      // + 今日决策/消息面等「正在汇总…」+ 博主共振「正在融合多源信号…」。
      loading: /加载中|加载失败|获取失败|出错了|error|正在加载|正在读取|正在汇总|正在融合/i.test(txt.slice(0, 200)),
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

  await page.goto(URL + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
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
    // 防抖动（2026-10-15 增强）：面板已就位但内容为空 → 自适应轮询，最长再等 14s。
    // 背景：博主荐股 / 聪明钱共识 / 相关人物 / 龙虎榜 等大数据面板首屏要 3–8s，
    // 旧的「只多等 5s 且判一次」会把「慢」系统性误报成「空白/打不开」。
    for (let i = 0; i < 7 && info.found && !info.loading && info.len < 40; i++) {
      await sleep(2000);
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
        // 2026-10-31：有些面板点开是**行内展开**（详情区直接铺在面板里），
        //   不是弹层，detectOpen 只认 drawer/branchModal/fundModal 三种，于是判成 none。
        //   这类面板（如博主共振的 .res-card）每轮都假红 → 补一条「面板内容明显变长」的判据。
        if (!open.open) {
          const after = await panelInfo(page, tab);
          const grew = (after.len || 0) - (info.len || 0);
          if (after.found && grew > Math.max(60, (info.len || 0) * 0.05)) {
            open = { open: true, what: 'inline-expand', dLen: grew };
          }
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

  // 基金经理策略（learn 固定子项，自包含 iframe；巴菲特作为「价值」一脉在 iframe 内）
  //   2026-10-31 修假红：原检测 `#p-buffett` 的 `.bf-card` —— 该 tab 早已并入基金经理策略 iframe
  //   （app.js `loadFundMasters()`，NAV fixed 项 key=fundmasters），顶层面板根本不存在，
  //   于是每轮巡检都报「面板:缺 / 期次卡:0」。这不是功能缺失，是尺子过期。
  //   改为：iframe 是否真的加载 → 框内分类 / 标的有多少 → 点一个标的看详情区是否展开。
  //   注意：iframe 内容不计入宿主 innerText，所以面板 len 恒为 0，不能拿 len 判空。
  try {
    await page.evaluate(() => { location.hash = '#learn/fundmasters'; });
    await sleep(4000);
    const fmTop = await page.evaluate(() => {
      const box = document.querySelector('#p-fundmasters');
      const f = box ? box.querySelector('iframe') : null;
      return { panel: !!box, iframe: !!f, src: f ? f.src : '' };
    });
    const fr = page.frames().find((x) => x.url().indexOf('/fund_masters/') >= 0);
    let fmIn = { cats: 0, chips: 0, hasBuffett: false, clicked: false, detailOpen: false, detailLen: 0 };
    if (fr) {
      const pre = await fr.evaluate(() => ({
        cats: document.querySelectorAll('.cat').length,
        chips: document.querySelectorAll('.chip').length,
        hasBuffett: /巴菲特/.test(document.body.innerText || ''),
      }));
      const clk = await fr.evaluate(() => { const c = document.querySelector('.chip'); if (c) c.click(); return !!c; });
      await sleep(1200);
      const post = await fr.evaluate(() => {
        const d = document.querySelector('.detail');
        return { detailOpen: !!(d && !d.classList.contains('hidden')), detailLen: d ? (d.innerText || '').length : 0 };
      });
      fmIn = Object.assign({}, pre, { clicked: clk }, post);
    }
    const fmOpen = { open: fmIn.detailOpen, what: fmIn.detailOpen ? 'fm-iframe-detail' : 'none',
      dLen: fmIn.detailLen, src: fmTop.src };
    const fmInfo = { found: fmTop.iframe, iframe: true, len: fmIn.cats, tables: fmIn.cats,
      cards: fmIn.chips, entries: fmIn.chips, loading: false };
    rows.push({ cat: 'learn', tab: 'fundmasters', label: '基金经理策略', info: fmInfo,
      drill: { clicked: fmIn.clicked, tag: '.chip' }, open: fmOpen, errs: [] });
    console.log(`${fmTop.iframe && fmIn.clicked && fmIn.detailOpen ? '✓' : '✗'} 基金经理策略 | iframe:${fmTop.iframe ? '有' : '无'} 分类:${fmIn.cats} 标的:${fmIn.chips} 含巴菲特:${fmIn.hasBuffett ? '是' : '否'} → 开:${fmOpen.what}(${fmIn.detailLen}字)`);
  } catch (e) { console.log('✗ 基金经理策略 异常:' + e.message.slice(0, 120)); }

  await page.screenshot({ path: 'audit_last.png' });
  const out = { generated: new Date().toISOString(), rows, badResponses: [...new Set(bad)] };
  fs.writeFileSync('audit_clickthrough_result.json', JSON.stringify(out, null, 2), 'utf8');

  // iframe 型面板（基金经理策略）：宿主 innerText 恒为 0，len 是「分类数」不是字数，
  //   故改用「框内标的总数 > 0」判空，否则会一直假红。
  const thin = (r) => (r.info.iframe ? !(r.info.cards > 0) : r.info.len < 40);
  const problem = rows.filter((r) => !r.info.found || r.info.loading || thin(r) || r.errs.length
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
