// 单用途（2026-10-09 集合竞价修复验收）：无头截图「今日看板 → 集合竞价」卡片 + 整页，
// 并回读卡片的标题日期 / 副标题 / 徽标 / KPI 文案，作为「日期前进到今日 + 非今日徽标消失」的机器可读证据。
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const EDGE = process.env.EDGE_BIN || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const OUT = path.resolve(__dirname, '_probe_out');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  if (!fs.existsSync(EDGE)) { console.log('EDGE_NOT_FOUND=' + EDGE); }
  const b = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 1800 });
  await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3000);
  await p.evaluate(() => { location.hash = '#overview/dashboard'; });
  await sleep(2500);
  await p.evaluate(() => {
    const dx = document.getElementById('drawer');
    if (dx) { dx.style.display = 'none'; dx.classList.remove('open'); }
    const host = document.getElementById('p-dashboard');
    if (host) host.scrollIntoView({ block: 'start' });
  });
  await sleep(500);

  const info = await p.evaluate(() => {
    const c = document.querySelector('#p-dashboard .auc-card');
    if (!c) return { found: false };
    const txt = (sel) => { const e = c.querySelector(sel); return e ? e.textContent.trim() : null; };
    return {
      found: true,
      title: txt('.auc-t'),
      sub: txt('.auc-sub'),
      staleBadge: txt('.auc-stale'),
      kpis: [...c.querySelectorAll('.auc-kpi')].map((x) => x.textContent.replace(/\s+/g, ' ').trim()),
    };
  });
  console.log('AUCTION_CARD_JSON=' + JSON.stringify(info));

  const el = await p.$('#p-dashboard .auc-card');
  if (el) {
    const cardPath = path.join(OUT, 'auction_card_after.png');
    await el.screenshot({ path: cardPath });
    console.log('ok -> ' + cardPath);
  } else {
    console.log('auc-card not found');
  }
  const dashPath = path.join(OUT, 'dashboard_after.png');
  await p.screenshot({ path: dashPath });
  console.log('ok -> ' + dashPath);
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
