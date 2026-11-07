// 2026-10-02 验收：汇总页「最新帖子」→「帖子情绪速览」（结论先行，原帖折叠）。
// 验十件事（真实浏览器里跑；改 .py 要重启服务、改前端要跟版本号，本脚本会一起核）：
//   ① 汇总页挂出 .pd-box，且不再是「作者/时间/情绪/内容」四条并排的原帖表
//   ② 顶部总论行非空，并写明统计窗口
//   ③ 个股结论行数量 == /api/postdigest 的 stocks 数量（前端不自己砍数）
//   ④ 每个徽章的颜色档位（bull/bear/neu）与后端 verdict_key 一致，不各说各话
//   ⑤ 行业/板块区存在
//   ⑥ 原帖进折叠区且默认收起（王总不必再滚一串链接）
//   ⑦ 全区块无 undefined / NaN 脏串
//   ⑧ 口径说明行在，写明判定方式
//   ⑨ 区块内元素不溢出
//   ⑩ 控制台零报错
// 用法：node verify_postdigest.cjs   （需 8765 在跑；浏览器用本机 Edge）
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
  await page.setViewport({ width: 1440, height: 1800 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3500);

  let pass = 0, total = 0;
  const say = (ok, msg) => { total++; if (ok) pass++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

  // 后端真值
  const apiPd = await page.evaluate(async () => {
    const r = await fetch('/api/postdigest?hours=24');
    const j = await r.json();
    return { stocks: j.stocks || [], boards: j.boards || [], verdict: j.verdict,
             verdict_text: j.verdict_text || '', scope: j.scope || {}, basis: j.basis || '' };
  });

  // ================= ① 情绪速览挂出，且不再是原帖四列表 =================
  const box = await page.evaluate(() => {
    const host = document.getElementById('p-summary');
    if (!host) return { present: false };
    const pd = host.querySelector('.pd-box');
    const rows = host.querySelectorAll('.pd-row');
    const firstRow = rows[0] ? rows[0].textContent.replace(/\s+/g, ' ').trim() : '';
    return {
      present: !!pd,
      rowCount: rows.length,
      hasTableBefore: !!host.querySelector('table') && host.querySelector('table').previousElementSibling === null,
      secs: [...host.querySelectorAll('.pd-sec')].map((x) => x.textContent.trim()),
      verdictLine: (host.querySelector('.pd-verdict') || {}).textContent || '',
      mood: (host.querySelector('.pd-mood') || {}).textContent || '',
      basis: (host.querySelector('.pd-basis') || {}).textContent || '',
      detail: (() => { const d = host.querySelector('details.pd-detail'); return d ? { open: d.open, hasTable: !!d.querySelector('table') } : null; })(),
      gen: (host.querySelector('.pd-gen') || {}).textContent || '',
      firstRow,
      overflow: (() => {
        if (!pd) return -1;
        const b = pd.getBoundingClientRect(); let n = 0;
        pd.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.width && r.right > b.right + 2) n++;
        });
        return n;
      })(),
    };
  });

  say(box.present, `① 汇总页挂出「帖子情绪速览」区块 .pd-box`);
  say(box.rowCount > 0 && !box.hasTableBefore,
      `①2 原帖表退到折叠区，首屏不再铺四列原文（首行：${(box.firstRow || '—').slice(0, 42)}…）`);

  // ================= ② 总论行 =================
  say(box.verdictLine.length > 20 && /共 \d+ 帖/.test(box.verdictLine) && /整体情绪/.test(box.verdictLine),
      `② 顶部总论成句（${box.verdictLine.length} 字）：「${box.verdictLine.slice(0, 46)}…」`);
  say(box.mood.length > 0 && box.mood.includes(apiPd.verdict),
      `②2 情绪结论徽章与后端一致：${box.mood.trim()} / 后端 ${apiPd.verdict}`);

  // ================= ③ 行数与接口一致 =================
  say(box.rowCount === apiPd.stocks.length + apiPd.boards.length,
      `③ 结论行数 ${box.rowCount} == 接口个股 ${apiPd.stocks.length} + 板块 ${apiPd.boards.length}，没自己砍数`);

  // ================= ④ 徽章档位与后端一致 =================
  const badgeOk = await page.evaluate(() => {
    const host = document.getElementById('p-summary');
    const rows = [...host.querySelectorAll('.pd-row')];
    return rows.map((r) => {
      const b = r.querySelector('.pd-head .badge');
      const cls = b ? (b.classList.contains('bull') ? 'bull' : b.classList.contains('bear') ? 'bear' : 'neu') : 'none';
      return { cls, txt: (b ? b.textContent : '').trim(), sum: (r.querySelector('.pd-sum') || {}).textContent || '' };
    });
  });
  // 前端只分三档配色（分歧=中性灰），把后端的 diverge 折算成 neu 再比，比的是「同不同意」
  const toCls = (k) => (k === 'bull' ? 'bull' : k === 'bear' ? 'bear' : 'neu');
  const expect = [...apiPd.stocks, ...apiPd.boards].map((x) => toCls(x.verdict_key));
  const got = badgeOk.map((b) => b.cls);
  const diff = got.map((g, i) => (g === expect[i] ? null : `#${i} ${expect[i]}→${g}`)).filter(Boolean);
  say(diff.length === 0,
      `④ ${got.length} 个徽章档位与后端 verdict_key 逐条对齐（${got.slice(0, 5).join('/')}…）${diff.length ? ' 差异:' + diff.join(',') : ''}`);
  say(badgeOk.every((b) => b.sum && b.sum.length > 10),
      `④2 每行都带一句结论总结（最短 ${Math.min(...badgeOk.map((b) => b.sum.length))} 字），不是只丢个方向`);

  // ================= ⑤ 行业/板块区 =================
  say(box.secs.some((s) => s.includes('行业')) && box.secs.some((s) => s.includes('个股')),
      `⑤ 两层都在：${box.secs.join(' ｜ ')}`);

  // ================= ⑥ 原帖折叠默认收起 =================
  say(!!box.detail && box.detail.open === false && box.detail.hasTable,
      '⑥ 原帖进折叠区且默认收起（要看才看，不再占版面）');

  // ================= ⑦ 脏串 =================
  const txt = await page.evaluate(() => (document.getElementById('p-summary') || {}).innerText || '');
  const dirty = ['undefined', 'NaN', '[object'].filter((k) => txt.includes(k));
  say(dirty.length === 0, `⑦ 汇总页无脏串 ${JSON.stringify(dirty)}（正文 ${txt.length} 字）`);

  // ================= ⑧ 口径行 =================
  say(box.basis.includes('口径') && box.basis.length > 30,
      `⑧ 口径说明在（${box.basis.slice(0, 34)}…）`);

  // ================= ⑨ 排版不溢出 =================
  say(box.overflow === 0, `⑨ 情绪速览区块内零溢出元素（越界 ${box.overflow} 个）`);

  // ================= ⑩ 控制台 =================
  say(errs.length === 0, `⑩ 控制台零报错 ${JSON.stringify(errs.slice(0, 2))}`);

  // ================= ⑪ 帖子速览里的股票代码/名称可点（王总：点不动还得自己去找查询） =================
  // 2026-10-12 修正过时断言：codeLink 早已由 <a> 改为 <button type="button" data-code-open>
  // （按钮可正常获得焦点、且不吃父级 onclick），原选择器 `.pd-head a[data-code-open]` 恒为 0。
  // 现改为不看标签名，只认 [data-code-open] 属性 —— 断言的是"可点"，不是"必须用 <a>"。
  const clickable = await page.evaluate(() => {
    const host = document.getElementById('p-summary');
    const rows = [...host.querySelectorAll('.pd-row')];
    const links = [...host.querySelectorAll('.pd-head [data-code-open]')];
    const firstCode = links.length ? links[0].getAttribute('data-code-open') : '';
    return { rowsWithLink: rows.filter((r) => r.querySelector('.pd-head [data-code-open]')).length,
             rowCount: rows.length, linkCount: links.length, firstCode };
  });
  say(clickable.linkCount > 0 && clickable.rowsWithLink >= Math.min(clickable.rowCount, 5),
      `⑪ ${clickable.linkCount} 处代码/名称是可点链接（data-code-open=${clickable.firstCode || '—'}），${clickable.rowsWithLink}/${clickable.rowCount} 行可点`);

  // ================= ⑫ 真点一下：抽屉自己把结果铺好，不要求再点任何按钮 =================
  await page.evaluate(() => {
    const a = document.querySelector('.pd-head [data-code-open]');
    if (a) a.click();
  });
  await sleep(3200);
  const drawer = await page.evaluate(() => {
    const b = document.getElementById('drawerBody');
    return { open: !!(b && b.innerText && b.innerText.length > 100), len: b ? b.innerText.length : 0 };
  });
  say(drawer.open,
      `⑫ 点代码直接出个股结果，没有「还要再点一下」（抽屉正文 ${drawer.len} 字）`);
  await page.evaluate(() => { const b = document.getElementById('drawerClose'); if (b) b.click(); });
  await sleep(600);

  // ================= ⑬ 个股诊断页：进页即自动跑 + 「开始诊断」按钮已撤 =================
  const diag = await page.evaluate(async () => {
    selectNav('watch', 'diag', true);
    await new Promise((r) => setTimeout(r, 4200));
    const host = document.getElementById('p-diag');
    return {
      hasGoBtn: !!(host && host.querySelector('#dgGo')),
      autoRan: (typeof DIAG !== 'undefined') && !!(DIAG.data || DIAG.loading),
      sym: (typeof DIAG !== 'undefined') ? (DIAG.symbol || '') : '',
      hasAutohint: !!(host && host.querySelector('.dg-autohint')),
    };
  });
  say(diag.hasGoBtn === false, '⑬ 诊断页已撤掉「开始诊断」按钮（多此一举的那一下没了）');
  say(diag.autoRan === true && diag.sym === clickable.firstCode,
      `⑬2 进诊断页自动开跑，跑的是刚点过的 ${diag.sym}，没有点任何按钮（提示行 ${diag.hasAutohint}）`);

  say(errs.length === 0, `⑬3 全流程控制台仍零报错 ${JSON.stringify(errs.slice(0, 2))}`);

  // 回到汇总页，再拍图
  await page.evaluate(() => {
    const t = [...document.querySelectorAll('.sub-tab')].find((x) => (x.textContent || '').trim() === '汇总');
    if (t) t.click();
    selectNav('overview', 'summary', true);
  });
  await sleep(1800);

  const SHOT = require('path').join(__dirname, 'artifacts');
  fs.mkdirSync(SHOT, { recursive: true });
  // 只拍情绪速览这一块（整页截图拍到的是今日看板，看不出这区块长什么样）
  // 汇总面板虽然默认 active，但默认 hash 是 #overview/dashboard，路由把它设成
  // display:none（DOM 在、看不见）。要拍/要肉眼确认，得先点「汇总」tab。
  await page.evaluate(() => {
    const t = [...document.querySelectorAll('.sub-tab')].find((x) => (x.textContent || '').trim() === '汇总');
    if (t) t.click();
  });
  await sleep(1600);
  // 用整页截 + 裁到情绪速览这块矩形：elementHandle.screenshot 在这页面会拍歪（拍成别的内容）
  const rect = await page.evaluate(() => {
    const b = document.querySelector('.pd-box');
    if (!b) return null;
    b.scrollIntoView({ block: 'start' });
    const r = b.getBoundingClientRect();
    return { x: Math.max(0, r.x - 10), y: Math.max(0, r.y - 10), width: Math.min(r.width + 20, 1400), height: Math.min(r.height + 20, 1500) };
  });
  await sleep(400);
  const clip = rect || { x: 0, y: 0, width: 1400, height: 1200 };
  await page.screenshot({ path: require('path').join(SHOT, 'shot_postdigest.png'), clip });
  // 折叠区展开后的样子也留一张（证明原文还在，只是默认不铺）
  await page.evaluate(() => { const d = document.querySelector('.pd-detail'); if (d) d.open = true; });
  await sleep(400);
  await page.screenshot({ path: require('path').join(SHOT, 'shot_postdigest_open.png'), fullPage: false });

  console.log(`\n===== ${pass}/${total} PASS =====`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})().catch((e) => { console.error('脚本异常', e); process.exit(2); });
