// 单用途（2026-11-05 分类改版）：六个决策台 tab + 工具包分组，逐页截一张 + 抓 JS 报错。
// 只看"页面崩没崩、标注对不对"，不改任何数据。
const puppeteer = require('puppeteer-core');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TABS = [
  ['desk/desk_t1', 'desk_t1'],
  ['desk/desk_hot', 'desk_hot'],
  ['desk/desk_dip', 'desk_dip'],
  ['desk/desk_mid', 'desk_mid'],
  ['desk/desk_base', 'desk_base'],
  ['desk/desk_long', 'desk_long'],
  ['toolbox/toolbox', 'toolbox'],
];

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 1800 });
  const errs = [];
  p.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  p.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
  await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3000);

  for (const [hash, id] of TABS) {
    await p.evaluate((h) => { location.hash = '#' + h; }, hash);
    await sleep(id === 'toolbox' ? 6000 : 3500);
    await p.evaluate(() => {
      const dx = document.getElementById('drawer');
      if (dx) { dx.style.display = 'none'; dx.classList.remove('open'); }
    });
    const info = await p.evaluate((tid) => {
      const host = document.getElementById('p-' + tid);
      if (!host) return { missing: true };
      const hero = host.querySelector('.dk-hero .dk-sub');
      const h1 = host.querySelector('.dk-hero .dk-title');
      const grps = [...host.querySelectorAll('.tbx-grpT')].map((x) => x.textContent.trim());
      const spans = [...host.querySelectorAll('.tbx-grpS')].map((x) => x.textContent.trim());
      const holds = host.querySelectorAll('.tbx-cHold').length;
      const cards = host.querySelectorAll('.tbx-card').length;
      return {
        missing: false,
        title: h1 ? h1.textContent.trim() : '',
        sub: hero ? hero.textContent.trim().slice(0, 90) : '',
        textLen: host.innerText.replace(/\s/g, '').length,
        grps, spans, holds, cards,
      };
    }, id);
    console.log(id, JSON.stringify(info, null, 0));
    await p.screenshot({ path: '_freq6_' + id + '.png' });
  }
  console.log('--- JS errors ---');
  console.log(errs.length ? errs.slice(0, 20).join('\n') : '(none)');
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
