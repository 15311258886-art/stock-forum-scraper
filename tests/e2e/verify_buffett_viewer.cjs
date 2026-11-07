/* 研报查看器（右侧停靠侧栏）回归验证 —— 2026-09-30
   背景：旧版是全屏弹窗，点开研报后整个应用被盖住、回不去（用户反馈）。
   本脚本断言「看研报时应用仍可用 + 能一键回到应用 + 不丢阅读位置」。
   断言项：
     ① 侧栏宽度 < 视口（应用露出来），且无遮罩层；
     ② 侧栏覆盖区之外，应用导航仍可点（elementFromPoint 落在应用 DOM 内）；
     ③ iframe 真实加载研报；
     ④ 「← 回到应用」收起为右下角胶囊；
     ⑤ 胶囊点回 → 同一期研报恢复，iframe 未重新加载（src 不变，阅读位置保留）；
     ⑥ 「↗ 新窗口」真的 open 了新标签（URL 指向研报）；
     ⑦ 「⛶ 全屏」占满视口，「⛶ 半屏」回落；
     ⑧ Esc 收起；再按 Esc 关闭胶囊；
     ⑨ 零控制台报错。 */
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const R = [];
const ok = (c, m) => { R.push([!!c, m]); console.log(`${c ? '✓' : '✗'} ${m}`); };

(async () => {
  const b = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--proxy-server=direct://', '--proxy-bypass-list=*', '--window-size=1440,900'],
  });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 900 });
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message.slice(0, 200)));
  p.on('console', (m) => { if (m.type() === 'error') errs.push('console:' + m.text().slice(0, 160)); });

  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1200);
  await p.evaluate(() => { location.hash = '#learn/buffett'; });
  await sleep(3000);

  // 打开第一期研报
  const opened = await p.evaluate(() => {
    const box = document.querySelector('#p-buffett');
    if (!box) return { ok: false };
    const first = box.querySelector('.bf-card .bf-open') || box.querySelector('.bf-card');
    if (!first) return { ok: false };
    first.click();
    return { ok: true, period: (first.dataset && first.dataset.period) || '' };
  });
  await sleep(2500);

  const vp = await p.evaluate(() => {
    const m = document.querySelector('#bfModal');
    if (!m) return { exists: false };
    const r = m.getBoundingClientRect();
    const f = m.querySelector('.bf-modal-iframe');
    // 应用左侧是否仍可命中（不被侧栏/遮罩盖住）
    const el = document.elementFromPoint(60, 300);
    const inModal = !!(el && m.contains(el));
    return {
      exists: true,
      open: m.classList.contains('open'),
      width: Math.round(r.width), vw: window.innerWidth,
      left: Math.round(r.left),
      bg: getComputedStyle(m).backgroundColor,
      src: f ? f.getAttribute('src') : null,
      hitInModal: inModal,
      hitTag: el ? (el.tagName + (el.className ? '.' + String(el.className).split(' ')[0] : '')) : null,
      hasBackdrop: !![...document.querySelectorAll('body > div')].find((d) => {
        const s = getComputedStyle(d);
        return s.position === 'fixed' && parseFloat(s.zIndex || 0) >= 9000 && parseFloat(s.width || 0) >= window.innerWidth && d.id !== 'bfModal';
      }),
    };
  });
  ok(vp.exists && vp.open, `侧栏已打开（period=${opened.period}）`);
  ok(vp.width < vp.vw - 100, `侧栏宽度 ${vp.width} < 视口 ${vp.vw}（应用仍露出 ${vp.vw - vp.width}px）`);
  ok(!vp.hitInModal && !!vp.hitTag, `左侧仍命中应用元素 → ${vp.hitTag}（未被侧栏遮挡）`);
  ok(!vp.hasBackdrop, '无全屏遮罩层');
  ok(!!vp.src && vp.src.indexOf('/static/buffett/') === 0 && vp.src.indexOf('.html') > 0, `iframe 已加载研报 → ${vp.src}`);

  // 「← 回到应用」→ 胶囊
  await p.evaluate(() => document.querySelector('#bfModal [data-bf="back"]').click());
  await sleep(700);
  const dock = await p.evaluate(() => {
    const d = document.querySelector('#bfDock');
    const m = document.querySelector('#bfModal');
    return {
      dockShown: !!(d && d.classList.contains('show') && getComputedStyle(d).display !== 'none'),
      text: d ? d.textContent.trim() : '',
      modalOpen: !!(m && m.classList.contains('open')),
      src: m ? m.querySelector('.bf-modal-iframe').getAttribute('src') : null,
    };
  });
  ok(!dock.modalOpen, '「回到应用」后侧栏收起（应用完全不受遮挡）');
  ok(dock.dockShown, `右下角出现续读胶囊 → 「${dock.text}」`);
  ok(dock.src && dock.src.indexOf('about:blank') < 0, '收起时未销毁 iframe（阅读位置得以保留）');

  // 胶囊点回
  await p.evaluate(() => document.querySelector('#bfDock').click());
  await sleep(800);
  const back = await p.evaluate(() => {
    const m = document.querySelector('#bfModal');
    const f = m.querySelector('.bf-modal-iframe');
    return { open: m.classList.contains('open'), src: f.getAttribute('src'),
             dockShown: document.querySelector('#bfDock').classList.contains('show') };
  });
  ok(back.open && back.src === dock.src, `胶囊恢复同一期研报（src 未变：${String(back.src).split('/').pop()}）`);
  ok(!back.dockShown, '恢复后胶囊自动隐藏');

  // 「↗ 新窗口」
  const popupP = new Promise((res) => { p.browser().once('targetcreated', () => res(true)); setTimeout(() => res(false), 4000); });
  let popupUrl = null;
  p.browser().once('targetcreated', async (t) => { try { const pg = await t.page(); if (pg) popupUrl = pg.url(); } catch (_) {} });
  await p.evaluate(() => document.querySelector('#bfModal [data-bf="newwin"]').click());
  const popped = await popupP;
  await sleep(800);
  ok(popped, `「新窗口」打开了新标签 → ${popupUrl || '(url 读取略)'}`);

  // 全屏 / 半屏
  await p.evaluate(() => document.querySelector('#bfModal [data-bf="fs"]').click());
  await sleep(400);
  const fsv = await p.evaluate(() => {
    const m = document.querySelector('#bfModal');
    return { fs: m.classList.contains('fs'), w: Math.round(m.getBoundingClientRect().width),
             vw: window.innerWidth, label: document.querySelector('#bfModal [data-bf="fs"]').textContent.trim() };
  });
  ok(fsv.fs && fsv.w >= fsv.vw - 2, `全屏生效（宽 ${fsv.w} / 视口 ${fsv.vw}，按钮变「${fsv.label}」）`);
  await p.evaluate(() => document.querySelector('#bfModal [data-bf="fs"]').click());
  await sleep(400);
  const half = await p.evaluate(() => {
    const m = document.querySelector('#bfModal');
    return { fs: m.classList.contains('fs'), w: Math.round(m.getBoundingClientRect().width),
             vw: window.innerWidth, label: document.querySelector('#bfModal [data-bf="fs"]').textContent.trim() };
  });
  ok(!half.fs && half.w < half.vw - 100, `半屏回落（宽 ${half.w}，按钮变「${half.label}」）`);

  // Esc 两段式
  await p.keyboard.press('Escape'); await sleep(600);
  const esc1 = await p.evaluate(() => ({
    open: document.querySelector('#bfModal').classList.contains('open'),
    dock: document.querySelector('#bfDock').classList.contains('show'),
  }));
  ok(!esc1.open && esc1.dock, 'Esc 第一次 → 收起为胶囊（回应用）');
  await p.keyboard.press('Escape'); await sleep(600);
  const esc2 = await p.evaluate(() => ({
    open: document.querySelector('#bfModal').classList.contains('open'),
    dock: document.querySelector('#bfDock').classList.contains('show'),
    src: document.querySelector('#bfModal .bf-modal-iframe').getAttribute('src'),
  }));
  ok(!esc2.open && !esc2.dock && String(esc2.src).indexOf('about:blank') >= 0, 'Esc 第二次 → 关闭并释放 iframe');

  // 点「✕」直接关闭（护栏依赖此按钮）
  await p.evaluate(() => document.querySelector('#p-buffett .bf-card').click());
  await sleep(1800);
  const reOpen = await p.evaluate(() => document.querySelector('#bfModal').classList.contains('open'));
  await p.evaluate(() => document.querySelector('#bfModal .bf-modal-x').click());
  await sleep(500);
  const afterX = await p.evaluate(() => ({ open: document.querySelector('#bfModal').classList.contains('open'), dock: document.querySelector('#bfDock').classList.contains('show') }));
  ok(reOpen, '点期次卡可直接打开研报');
  ok(!afterX.open && !afterX.dock, '「✕」一次性关闭（不残留胶囊）');

  // 拖动调宽：必须先把侧栏打开、再取把手坐标（否则拿的是未渲染时的空框，等于没拖）
  await p.evaluate(() => document.querySelector('#p-buffett .bf-card').click());
  await sleep(2000);
  const before = await p.evaluate(() => Math.round(document.querySelector('#bfModal').getBoundingClientRect().width));
  const grip = await p.evaluate(() => {
    const g = document.querySelector('#bfModal .bf-modal-grip');
    if (!g) return null;
    const r = g.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + 240) };
  });
  if (grip) {
    await p.mouse.move(grip.x, grip.y);
    await p.mouse.down();
    await p.mouse.move(grip.x - 240, grip.y, { steps: 10 });
    await p.mouse.up();
    await sleep(500);
    const w2 = await p.evaluate(() => Math.round(document.querySelector('#bfModal').getBoundingClientRect().width));
    ok(w2 > before + 120, `拖动左边缘可调宽 → ${before}px ⇒ ${w2}px（把手 x=${grip.x}）`);
    await p.evaluate(() => { const x = document.querySelector('#bfModal .bf-modal-x'); if (x) x.click(); });
    await sleep(400);
  } else ok(false, '未找到拖动把手（.bf-modal-grip 不可见/零尺寸）');

  ok(errs.length === 0, `控制台错误 ${errs.length} 条${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}`);

  await p.screenshot({ path: 'verify_buffett_viewer.png', fullPage: false });
  const pass = R.filter((x) => x[0]).length;
  console.log(`\n===== 结果：${pass}/${R.length} 通过 =====`);
  R.filter((x) => !x[0]).forEach((x) => console.log('   ✗ ' + x[1]));
  await b.close();
  process.exit(pass === R.length ? 0 : 1);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
