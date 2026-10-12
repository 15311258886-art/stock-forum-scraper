/* 补充验收（2026-10-09）：个股诊断页「资金流向」区块的数据日徽标（需触发一次诊断） */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');
const EDGE = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const OUT = path.resolve(__dirname, '_probe_out');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'] });
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(String(e && e.message || e)));
  await p.setViewport({ width: 1366, height: 1100 });
  await p.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3000);
  await p.evaluate(() => { location.hash = '#watch/diag'; });
  await sleep(1500);
  // 触发一次诊断（输入即跑）
  await p.evaluate(() => {
    const el = document.getElementById('dgSym');
    if (el) { el.value = '600519'; el.dispatchEvent(new Event('input', { bubbles: true })); }
  });
  await sleep(1200);
  await p.evaluate(() => { try { loadDiag('600519'); } catch (e) {} });
  await sleep(9000);
  const info = await p.evaluate(() => {
    const host = document.getElementById('p-diag');
    if (!host) return { found: false };
    const badges = [...host.querySelectorAll('.dd-badge')].map((x) => x.textContent.replace(/\s+/g, ' ').trim());
    const sec = [...host.querySelectorAll('.dg-sec')].find((s) => /资金流向/.test(s.querySelector('h3') ? s.querySelector('h3').textContent : ''));
    return { found: true, badges, mfSection: sec ? sec.textContent.replace(/\s+/g, ' ').slice(0, 220) : null };
  });
  console.log('DIAG=' + JSON.stringify(info));
  console.log('UNCAUGHT=' + errs.length + (errs.length ? ' ' + JSON.stringify(errs) : ''));
  await p.screenshot({ path: path.join(OUT, 'fresh_diag_moneyflow.png') });
  await b.close();
  const ok = info.found && (info.badges || []).some((x) => /数据日|盘中快照/.test(x));
  console.log(ok ? 'DIAG_BADGE=PASS' : 'DIAG_BADGE=FAIL');
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(1); });
