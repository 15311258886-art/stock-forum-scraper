/* 端到端 UI 验证：起临时服务 → 无头 Chromium 打开查看器 → 调 openStock('600519')
   → 抓取「重要信息」区块文本 + K线快照 + 截图。 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8792;
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const SHOT = path.join(BASE, '_ui_shot_600519.png');

const env = { ...process.env, VIEWER_PORT: String(PORT) };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];

const srv = spawn(path.join(BASE, '.venv', 'Scripts', 'python.exe'), ['-m', 'viewer.server'],
  { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', d => srvLog += d.toString());
srv.stderr.on('data', d => srvLog += d.toString());

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // 等服务就绪
  let ready = false;
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/api/stocks`);
      if (r.ok) { ready = true; break; }
    } catch (e) { /* not yet */ }
    await sleep(500);
  }
  console.log('TEMP_SERVER_READY =', ready);
  if (!ready) { console.log(srvLog.slice(0, 900)); srv.kill(); process.exit(1); }

  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--no-proxy-server', '--window-size=1600,1000'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  const badResp = [];
  page.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) badResp.push(`${r.status()} ${r.url()}`); });

  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await sleep(2500);

  // 直接调用全局 openStock，等价于用户点股票
  await page.evaluate(() => { window.openStock('600519'); });
  await sleep(12000);

  const out = await page.evaluate(() => {
    const q = document.querySelector('#stkQuote');
    const snap = document.querySelector('#klineSnap');
    const canvas = document.querySelector('#klineCanvas');
    const drawer = document.querySelector('#drawer') || document.querySelector('.drawer');
    return {
      quoteText: q ? q.innerText.replace(/\n+/g, ' | ').trim() : 'NO #stkQuote',
      snapText: snap ? snap.innerText.replace(/\n+/g, ' | ').trim() : 'NO #klineSnap',
      canvasSize: canvas ? `${canvas.width}x${canvas.height}` : 'NO canvas',
      drawerOpen: !!drawer,
      drawerWidth: drawer ? drawer.getBoundingClientRect().width : 0,
      hasError: !!document.querySelector('.err, .error'),
    };
  });
  console.log('--- 重要信息区块 ---');
  console.log(out.quoteText);
  console.log('--- K 线快照 ---');
  console.log(out.snapText);
  console.log('--- canvas ---', out.canvasSize, '| 抽屉宽', out.drawerWidth);
  console.log('--- 前端报错 ---', errors.length ? errors.slice(0, 6) : '无');
  console.log('--- 非 2xx 请求 ---', badResp.length ? [...new Set(badResp)].slice(0, 10) : '无');

  // 颜色对账：A 股口径「涨=红、跌=绿」（本样本为下跌 -1.14%，价格应为绿）
  const colors = await page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    const price = document.querySelector('#stkQuote .si-price');
    const items = [...document.querySelectorAll('#stkQuote .si-item')].map((el) => ({
      k: el.querySelector('span').textContent,
      v: el.querySelector('b').textContent,
      c: getComputedStyle(el.querySelector('b')).color,
    }));
    return {
      theme: document.documentElement.getAttribute('data-theme') || '(none)',
      bull: cs.getPropertyValue('--bull').trim(),
      bear: cs.getPropertyValue('--bear').trim(),
      priceClass: price ? price.className : 'NONE',
      priceColor: price ? getComputedStyle(price).color : 'NONE',
      items,
    };
  });
  console.log('--- 主题 ---', colors.theme, '| --bull', colors.bull, '(应为红) | --bear', colors.bear, '(应为绿)');
  console.log('--- 价格元素 ---', colors.priceClass, colors.priceColor);
  console.log('--- 带色项 ---', JSON.stringify(colors.items.filter((i) => i.c !== 'rgb(26, 26, 26)' && i.c !== 'rgb(17, 24, 39)')));

  if (out.drawerWidth > 0) {
    const el = await page.$('#drawer') || await page.$('.drawer');
    if (el) await el.screenshot({ path: SHOT });
    else await page.screenshot({ path: SHOT });
    console.log('SCREENSHOT =', SHOT, fs.existsSync(SHOT) ? fs.statSync(SHOT).size + 'B' : 'MISS');
  }

  await browser.close();
  srv.kill();
  console.log('DONE');
})().catch(e => { console.log('FATAL', e.message); try { srv.kill(); } catch (_) {} process.exit(1); });
