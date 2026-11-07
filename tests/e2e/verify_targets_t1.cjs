/* 真实点开验证（2026-09-29）：
   ① 选手实盘：标的「名称+代码」是否成对出现、点开是否真有内容（旧版这里显示"第三次/累计/附近"且点不开）；
   ② 每日预测：新增的 T+1 窗口按钮是否可点、点后个股卡是否出 T+1 收益，
      且「自我学习轨迹」出现 T+1 累计命中率。
   用法：node dbg_targets_t1.cjs
*/
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(String(e.message).slice(0, 200)));
  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);

  // ---------- ① 选手实盘 ----------
  await p.evaluate(() => { location.hash = '#players/operating'; });
  await sleep(4000);
  const box = await p.evaluate(() => {
    const el = document.querySelector('#p-operating');
    if (!el) return { exists: false };
    const chips = [...el.querySelectorAll('.kline-chip[data-sym]')];
    const tds = [...el.querySelectorAll('tbody tr td:nth-child(4)')];
    const codes = tds.map((td) => (td.textContent || '').trim()).filter((s) => /\d{6}/.test(s));
    const noCode = tds.map((td) => (td.textContent || '').trim())
      .filter((s) => s && !/\d{6}/.test(s));
    return { exists: true, rows: el.querySelectorAll('tbody tr').length,
             chips: chips.length, withCode: codes.length, noCodeSample: noCode.slice(0, 5),
             first3: tds.slice(0, 3).map((td) => (td.textContent || '').replace(/\s+/g, ' ').trim()) };
  });
  console.log('【选手实盘·标的列】', JSON.stringify(box, null, 0));

  const drill = await p.evaluate(() => {
    const c = document.querySelector('#p-operating .kline-chip[data-sym]');
    if (!c) return { clicked: false };
    c.click();
    return { clicked: true, sym: c.getAttribute('data-sym') };
  });
  let drawer = { open: false, len: 0, head: '' };
  for (let i = 0; i < 16; i++) {
    await sleep(500);
    drawer = await p.evaluate(() => {
      const d = document.querySelector('#drawer');
      if (!d || !d.classList.contains('show')) return { open: false, len: 0, head: '' };
      const txt = (d.textContent || '').replace(/\s+/g, ' ').trim();
      return { open: true, len: txt.length, head: txt.slice(0, 70) };
    });
    if (drawer.open && drawer.len > 80) break;
  }
  console.log('【点开标的小卡】', JSON.stringify({ ...drill, ...drawer }));
  await p.evaluate(() => {
    const d = document.querySelector('#drawer');
    if (d) d.classList.remove('show');
  });
  await sleep(600);

  // ---------- ② 每日预测 T+1 ----------
  await p.evaluate(() => { location.hash = '#overview/prediction'; });
  await sleep(4500);
  const t1 = await p.evaluate(() => {
    const el = document.querySelector('#p-prediction');
    if (!el) return { exists: false };
    const btns = [...el.querySelectorAll('.pred-ctl .pred-pill')].map((b) => b.textContent.trim());
    const dateInput = el.querySelector('#predDate');
    if (dateInput) { dateInput.value = '2026-09-28'; dateInput.dispatchEvent(new Event('change')); }
    return { exists: true, pills: btns, hasT1: btns.includes('T+1') };
  });
  console.log('【预测·窗口按钮】', JSON.stringify(t1));
  await sleep(4000);

  const onT1 = await p.evaluate(() => {
    const el = document.querySelector('#p-prediction');
    const btn = [...el.querySelectorAll('.pred-ctl .pred-pill')].find((b) => b.textContent.trim() === 'T+1');
    if (!btn) return { clicked: false };
    btn.click();
    return { clicked: true };
  });
  await sleep(2500);
  const after = await p.evaluate(() => {
    const el = document.querySelector('#p-prediction');
    const on = el.querySelector('.pred-ctl .pred-pill.on');
    // 取第一张个股卡的 T+1 单元格
    const cells = [...el.querySelectorAll('.pred-card .pred-ret')];
    const t1cells = cells.filter((c) => (c.textContent || '').startsWith('T+1'));
    const track = el.querySelector('.pred-hits');
    return {
      activePill: on ? on.textContent.trim() : null,
      t1Cells: t1cells.length,
      t1Active: t1cells.filter((c) => c.classList.contains('active')).length,
      t1Sample: t1cells.slice(0, 4).map((c) => c.textContent.trim()),
      track: (track ? track.textContent : '').replace(/\s+/g, ' ').trim(),
    };
  });
  console.log('【点 T+1 之后】', JSON.stringify({ ...onT1, ...after }));
  console.log('【控制台错误】', errs.length ? errs.join(' | ') : '无');
  await b.close();
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
