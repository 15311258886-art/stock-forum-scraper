// 单用途（2026-11-05 分类改版）：在**不改服务、不重启**的前提下验证工具墙的六分类分组。
// 做法：拦截 /api/toolbox，用 _tbx_payload.json（当前磁盘代码 api_toolbox 的真实返回）应答。
// 线上服务还是旧进程，它的 /api/toolbox 里没有 pillar_meta，直接用会测到兜底分支。
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PAYLOAD = fs.readFileSync('_tbx_payload.json', 'utf8');

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 1600 });
  const errs = [];
  p.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  await p.setRequestInterception(true);
  p.on('request', (req) => {
    const u = req.url();
    if (/\/api\/toolbox(\?|$)/.test(u)) {
      return req.respond({ status: 200, contentType: 'application/json; charset=utf-8',
        body: PAYLOAD });
    }
    req.continue();
  });
  await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3000);
  await p.evaluate(() => { location.hash = '#toolbox/toolbox'; });
  await sleep(4000);
  await p.evaluate(() => {
    const dx = document.getElementById('drawer');
    if (dx) { dx.style.display = 'none'; dx.classList.remove('open'); }
  });
  const info = await p.evaluate(() => {
    const host = document.getElementById('p-toolbox');
    const grps = [...host.querySelectorAll('.tbx-grp')].map((g) => ({
      t: (g.querySelector('.tbx-grpT') || {}).textContent,
      s: (g.querySelector('.tbx-grpS') || {}).textContent,
      n: (g.querySelector('.tbx-grpN') || {}).textContent,
      cards: g.querySelectorAll('.tbx-card').length,
      holds: [...g.querySelectorAll('.tbx-cHold')].slice(0, 1).map((x) => x.textContent),
    }));
    return { grps, wallText: host.querySelector('.tbx-wall').innerText.slice(0, 400) };
  });
  console.log(JSON.stringify(info.grps, null, 1));
  console.log('--- 首屏文字 ---');
  console.log(info.wallText);
  console.log('--- JS errors ---');
  console.log(errs.length ? errs.join('\n') : '(none)');
  await p.screenshot({ path: '_freq6_toolbox_grouped.png', fullPage: false });
  // 再截一张「已淘汰」组
  await p.evaluate(() => {
    const gs = [...document.querySelectorAll('#p-toolbox .tbx-grp')];
    if (gs.length) gs[gs.length - 1].scrollIntoView({ block: 'start' });
  });
  await sleep(500);
  await p.screenshot({ path: '_freq6_toolbox_dead.png' });
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
