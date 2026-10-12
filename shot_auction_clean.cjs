// 单用途：关掉抽屉后，单独截「今日看板 → 集合竞价」卡片，供人工目检排版。
const puppeteer = require('puppeteer-core');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 1800 });
  await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);
  await p.evaluate(() => { location.hash = '#overview/dashboard'; });
  await sleep(2200);
  // 关掉可能已弹出的个股抽屉，避免遮挡
  await p.evaluate(() => {
    const dx = document.getElementById('drawer');
    if (dx) { dx.style.display = 'none'; dx.classList.remove('open'); }
    const host = document.getElementById('p-dashboard');
    if (host) host.scrollIntoView({ block: 'start' });
  });
  await sleep(400);
  const el = await p.$('#p-dashboard .auc-card');
  if (el) { await el.screenshot({ path: 'shot_auction_card_clean.png' }); console.log('ok -> shot_auction_card_clean.png'); }
  else console.log('未找到 .auc-card');
  // 再截一张浅色/深色切换都看一眼（默认主题）
  await p.screenshot({ path: 'shot_auction_dash_clean.png' });
  console.log('ok -> shot_auction_dash_clean.png');
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
