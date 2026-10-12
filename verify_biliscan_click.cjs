// 回归：一键筛股命中卡的股票名/代码可点 → 开个股详情抽屉（2026-10-12 王总反馈瑰宝科技点不动）
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const fails = [];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails.push(m); };

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new', args: ['--no-sandbox'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1600, height: 1000 });
  const errs = [];
  p.on('pageerror', (e) => errs.push('pageerror:' + e.message));
  p.on('console', (m) => { if (m.type() === 'error') errs.push('console:' + m.text()); });

  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1800));
  await p.evaluate(() => { location.hash = '#learn/bili_techvalue'; });
  // 轮询等命中卡渲染（首次可能触发后台扫描）
  let hits = 0;
  for (let i = 0; i < 30; i++) {
    hits = await p.evaluate(() => document.querySelectorAll('#bili-scan-result .bili-hit').length);
    if (hits > 0) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  ok(hits > 0, `命中卡已渲染（${hits} 张）`);
  if (!hits) { console.log('FAIL no hits, abort'); await b.close(); process.exit(1); }

  const probe = await p.evaluate(() => {
    const first = document.querySelector('#bili-scan-result .bili-hit');
    const nameBtn = first.querySelector('.bili-hit-name[data-code-open]');
    const codeBtn = first.querySelector('.bili-hit-code[data-code-open]');
    return {
      name: first.querySelector('.bili-hit-name') ? first.querySelector('.bili-hit-name').textContent.trim() : null,
      nameIsBtn: !!nameBtn, nameCode: nameBtn ? nameBtn.getAttribute('data-code-open') : null,
      codeIsBtn: !!codeBtn,
      nameVisible: nameBtn ? nameBtn.offsetWidth > 0 : false,
      sixDigit: nameBtn ? /^\d{6}$/.test(nameBtn.getAttribute('data-code-open')) : false,
    };
  });
  ok(probe.nameIsBtn, '股票名是可点入口（data-code-open）');
  ok(probe.codeIsBtn, '代码也是可点入口');
  ok(probe.sixDigit, '入口代码为 6 位 A 股代码（' + probe.nameCode + ' ' + probe.name + '）');
  ok(probe.nameVisible, '股票名可见（offsetWidth>0）');

  // 真坐标点击股票名 → 抽屉应打开且标题含代码
  const nameEl = await p.$('#bili-scan-result .bili-hit .bili-hit-name[data-code-open]');
  await nameEl.click();
  let drawerShown = false, drawerTitle = '', bodyLen = 0;
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 800));
    const st = await p.evaluate(() => ({
      shown: !!(document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show')),
      title: (document.querySelector('#drawerTitle') || {}).textContent || '',
      len: (document.querySelector('#drawerBody') || {}).innerHTML ? document.querySelector('#drawerBody').innerHTML.length : 0,
    }));
    if (st.shown && st.len > 200) { drawerShown = st.shown; drawerTitle = st.title; bodyLen = st.len; break; }
  }
  ok(drawerShown, '点击股票名 → 个股详情抽屉打开');
  ok(drawerTitle.indexOf(probe.nameCode) !== -1, `抽屉标题含代码 ${probe.nameCode}（实际：${drawerTitle.slice(0, 40)}）`);
  ok(bodyLen > 200, `抽屉内容非空（${bodyLen} 字符）`);

  await p.screenshot({ path: '_biliscan_click.png' });
  ok(errs.length === 0, '控制台零报错' + (errs.length ? ' → ' + errs.slice(0, 3).join(' | ') : ''));
  console.log(fails.length ? `RESULT: ${fails.length} FAIL` : 'RESULT: ALL PASS');
  await b.close();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
