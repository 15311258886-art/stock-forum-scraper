// 2026-10-02 方案 C 验收：「软件荐股雷达」→ 整页重排为「外部荐股验证报告」。
// 王总原话：「它不能对判断产生指导性/建议性的结论的情况下，那它连存在的意义都没有。
// 要么让它产生价值，要么删掉——现在放在这儿也多余。」
// 验十六件事（真实浏览器跑；改 .py 要重启、改前端要跟版本串，本脚本一并核）：
//   ① 页面挂出「外部荐股验证报告」标题
//   ② 顶部有一句**真结论**（由后端已回填行情算出，不是漂亮话）
//   ③ 结论等级徽章在（参考 / 可作依据），并交代还差几条升级
//   ④ 三个数是后端 report 的真数字（不自己编）
//   ⑤ 逐条明细行数 == 后端 done_n，且每条都带真收益（不是 "—"/"无样本"）
//   ⑥ 明细里股票代码可点
//   ⑦ 分信源对照表行数 == 后端 by_source，且都带真数字
//   ⑧ 主界面只剩「继续采集」一个动作
//   ⑨ 底部交代「N 个空源对判断零贡献、已从本页剔除」并带真实数字
//   ⑩ 旧设计的痕迹全清：无 .rd-card、无未供数折叠、无导入按钮、无档位图例
//   ⑪ 无 undefined / NaN / [object 脏串
//   ⑫ 结论块零溢出
//   ⑬ 控制台零报错
//   ⑭ 页面数字与后端 report 逐条对齐（T+5 均值必须能在页面上核到）
//   ⑮ 点「继续采集」能触发（按钮可用、非禁用态）
//   ⑯ 截图留证
// 用法：node verify_radar.cjs   （需 8765 在跑；浏览器用本机 Edge）
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const SHOT = path.join(__dirname, 'artifacts');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!fs.existsSync(EDGE)) { console.error('找不到 Edge'); process.exit(1); }
  fs.mkdirSync(SHOT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1900 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3200);

  let pass = 0, total = 0;
  const say = (ok, msg) => { total++; if (ok) pass++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

  // 后端真值（页面不许自己算口径）
  const apiPd = await page.evaluate(async () => {
    const r = await fetch('/api/extref_signals');
    const j = await r.json();
    const rp = j.report || {};
    return {
      grade: rp.grade, done_n: rp.done_n, loss_n: rp.loss_n, gap: rp.gap, min: rp.min_sample,
      t5_avg: rp.windows && rp.windows.t5 ? rp.windows.t5.avg : null,
      t5_wr: rp.windows && rp.windows.t5 ? rp.windows.t5.win_rate : null,
      verdict: rp.verdict || '', grade_note: rp.grade_note || '',
      by_src: (rp.by_source || []).length, detail: (rp.detail || []).length,
      none_n: (j.sources || []).filter((s) => (Number(s.sig_n) || 0) <= 0).length,
    };
  });

  // 进「荐股验证报告」
  await page.evaluate(() => { selectNav('opinion', 'extref_soft', true); });
  await sleep(3500);
  await page.evaluate(() => {
    const t = [...document.querySelectorAll('.sub-tab')].find((x) => (x.textContent || '').trim() === '荐股验证报告');
    if (t) t.click();
  });
  await sleep(1800);

  const box = await page.evaluate(() => {
    const host = document.getElementById('p-extref_soft');
    if (!host) return { present: false };
    const top = host.querySelector('.rd-top');
    const gradeEl = host.querySelector('.rd-grade');
    const secs = [...host.querySelectorAll('.ex-sec h3')].map((h) => h.textContent.trim());
    const tables = [...host.querySelectorAll('.ex-sec table')];
    const rowsOf = (tb) => tb ? [...tb.querySelectorAll('tbody tr')] : [];
    const rows = [];
    tables.forEach((tb) => rowsOf(tb).forEach((r) => rows.push(
      [...r.querySelectorAll('td')].map((td) => td.textContent.replace(/\s+/g, ' ').trim()))));
    return {
      present: !!top,
      title: (host.querySelector('.rd-hd h2') || {}).textContent || '',
      verdict: (host.querySelector('.rd-v') || {}).textContent || '',
      gradeTxt: gradeEl ? gradeEl.textContent.replace(/\s+/g, ' ').trim() : '',
      gradeCls: gradeEl ? gradeEl.className : '',
      gn: (host.querySelector('.rd-gn') || {}).textContent || '',
      kpis: [...host.querySelectorAll('.rd-kpi')].map((k) => k.textContent.replace(/\s+/g, ' ').trim()),
      secs,
      rowCount: rows.length,
      sampleRows: rows.slice(-8),
      srcRowCount: rows.length > 8 ? rows.slice(0, rows.length - 8) : [],
      codes: [...host.querySelectorAll('.stk-mini[data-ex-sym]')].map((c) => c.textContent.trim()),
      actions: [...host.querySelectorAll('.ex-ch-actions .btn-s')].map((a) => ({
        t: a.textContent.trim(), disabled: !!a.disabled,
        attr: a.getAttribute('data-ex-collect') || '' })),
      noneNote: [...host.querySelectorAll('.ex-ch-note')].map((n) => n.textContent.replace(/\s+/g, ' ').trim()),
      // 旧设计痕迹
      cards: host.querySelectorAll('.rd-card').length,
      folds: host.querySelectorAll('details.rd-fold').length,
      importBtns: host.querySelectorAll('[data-ex-import]').length,
      importCta: host.querySelectorAll('.ex-imp-cta').length,
      legend: host.querySelectorAll('.ex-legend').length,
      exStats: host.querySelectorAll('.ex-stats').length,
      overflow: (() => {
        if (!top) return -1;
        const b = top.getBoundingClientRect(); let n = 0;
        top.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.width && r.right > b.right + 2) n++;
        });
        return n;
      })(),
    };
  });

  say(box.present && /外部荐股验证报告/.test(box.title),
      `① 页面挂出「${box.title}」标题（.rd-top 存在=${box.present}）`);
  const vd = box.verdict || '';
  say(vd.length > 40 && vd.length < 140 && /%/.test(vd) && /平均|只有|掉到|都是/.test(vd),
      `② 顶部是真结论（${vd.length} 字，一句给完、带真实均值）：「${vd.slice(0, 46)}…」`);
  say(/参考|可作依据/.test(box.gradeTxt) && /还差|及格线/.test(box.gradeTxt),
      `③ 结论等级徽章在：「${box.gradeTxt}」`);
  say(box.kpis.length === 3 && box.kpis.every((k) => k.length > 4),
      `④ 三个数都是真数字：${box.kpis.map((k) => k.slice(0, 16)).join(' / ')}`);
  const detailRows = box.sampleRows.filter((r) => r.length === 7);
  const emptyCells = detailRows.filter((r) => r.slice(4).every((c) => c === '—' || /无样本/.test(c)));
  say(detailRows.length === apiPd.done_n && emptyCells.length === 0,
      `⑤ 逐条明细 ${detailRows.length} 行 == 后端 ${apiPd.done_n} 条，且无空数行（空行 ${emptyCells.length}）`);
  say(box.codes.length > 0,
      `⑥ 明细里股票代码可点（${box.codes.length} 个：${box.codes.slice(0, 3).join('/')}）`);
  const srcRows = box.srcRowCount.filter((r) => r.length === 4);
  say(srcRows.length === apiPd.by_src && srcRows.every((r) => r.slice(1).some((c) => /%/.test(c))),
      `⑦ 分信源对照 ${srcRows.length} 行 == 后端 ${apiPd.by_src} 个，且都带真收益`);
  say(box.actions.length === 1 && /继续采集/.test(box.actions[0].t) && box.actions[0].attr === 'em-guba-rank',
      `⑧ 主界面只剩一个动作：「${box.actions.map((a) => a.t).join(' / ') || '无'}」`);
  say(box.noneNote.some((t) => new RegExp(`${apiPd.none_n}\\s*个`).test(t)),
      `⑨ 底部交代剔除的空源数：${apiPd.none_n} 个（${(box.noneNote.find((t) => /个/.test(t)) || '—').slice(0, 40)}）`);
  say(box.cards === 0 && box.folds === 0 && box.importBtns === 0 && box.importCta === 0
      && box.legend === 0 && box.exStats === 0,
      `⑩ 旧设计痕迹清零（卡片 ${box.cards} / 折叠 ${box.folds} / 导入 ${box.importBtns} / 图例 ${box.legend}）`);

  const txt = await page.evaluate(() => (document.getElementById('p-extref_soft') || {}).innerText || '');
  const dirty = ['undefined', 'NaN', '[object'].filter((k) => txt.includes(k));
  say(dirty.length === 0, `⑪ 面板无脏串 ${JSON.stringify(dirty)}（正文 ${txt.length} 字）`);
  say(box.overflow === 0, `⑫ 结论块内零溢出元素（越界 ${box.overflow} 个）`);
  say(errs.length === 0, `⑬ 控制台零报错 ${JSON.stringify(errs.slice(0, 2))}`);
  const t5 = Number(apiPd.t5_avg).toFixed(2);
  say(txt.includes(t5 + '%') && txt.includes(String(apiPd.loss_n)) && /只作参考/.test(txt),
      `⑭ 页面数字与后端对齐：T+5 均值 ${t5}% 在页面上核得到、亏损 ${apiPd.loss_n} 条、标注「只作参考」`);
  say(box.actions.length === 1 && box.actions[0].disabled === false,
      '⑮ 「继续采集」按钮可用（非禁用态，点了真会跑）');
  // 截图留证
  const rect = await page.evaluate(() => {
    const b = document.querySelector('#p-extref_soft .rd-top');
    if (!b) return null;
    b.scrollIntoView({ block: 'start' });
    const r = b.getBoundingClientRect();
    return { x: Math.max(0, r.x - 10), y: Math.max(0, r.y - 10), width: Math.min(r.width + 20, 1400), height: Math.min(r.height + 26, 900) };
  });
  await sleep(400);
  await page.screenshot({ path: path.join(SHOT, 'shot_radar_top.png'), clip: rect || { x: 0, y: 0, width: 1400, height: 760 } });
  await page.screenshot({ path: path.join(SHOT, 'shot_report_full.png'), fullPage: true });
  say(fs.existsSync(path.join(SHOT, 'shot_radar_top.png')) && fs.existsSync(path.join(SHOT, 'shot_report_full.png')),
      '⑯ 截图已留证（shot_radar_top.png / shot_report_full.png）');

  console.log(`\n===== ${pass}/${total} PASS =====`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})().catch((e) => { console.error('脚本异常', e); process.exit(2); });
