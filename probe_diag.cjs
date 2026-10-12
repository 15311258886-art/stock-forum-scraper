// 个股诊断冷加载时延探针：量化 /api/diag?symbol=600519 首次（缓存未命中）延迟，
// 验证面板最终是否渲染成功（区分"真 bug"与"仅仅是慢"）。
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--ignore-certificate-errors', '--disable-blink-features=AutomationControlled',
      '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const page = await browser.newPage();
  let reqT = null, respT = null, status = null, fail = false;
  page.on('request', (r) => { if (r.url().includes('/api/diag') && r.url().includes('600519')) reqT = Date.now(); });
  page.on('response', (r) => { if (r.url().includes('/api/diag') && r.url().includes('600519')) { respT = Date.now(); status = r.status(); if (r.status() >= 400) fail = true; } });

  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(2000);
  await page.evaluate(() => { location.hash = '#watch/diag'; });
  await sleep(2600);
  await page.evaluate(() => {
    const inp = document.querySelector('#dgSym');
    if (inp) { inp.value = '600519'; inp.dispatchEvent(new Event('input', { bubbles: true })); }
    const btn = document.querySelector('#dgGo'); if (btn) btn.click();
  });
  console.log('已点击 #dgGo（600519），开始轮询…');
  let firstContent = null;
  for (let i = 1; i <= 25; i++) {
    await sleep(1000);
    const s = await page.evaluate(() => {
      const b = document.querySelector('#p-diag');
      const txt = b ? (b.textContent || '').replace(/\s+/g, ' ').trim() : '';
      const loading = /诊断中/.test(txt.slice(0, 40));
      const ink = (() => { try { const cv = document.querySelector('#dgKlineCanvas'); if (!cv) return -1; const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; let n = 0; for (let k = 3; k < d.length; k += 4) if (d[k] > 0) n++; return n; } catch (e) { return -2; } })();
      return { len: txt.length, loading, ink, fail: /诊断失败/.test(txt.slice(0, 60)) };
    });
    if (firstContent === null && s.len > 500 && !s.loading) firstContent = i;
    if (i % 2 === 0 || firstContent !== null) {
      const rel = ((Date.now() - (reqT || Date.now())) / 1000).toFixed(1);
      console.log(`  t+${rel}s panelLen=${s.len} loading=${s.loading} klineInk=${s.ink}${s.fail ? ' FAIL' : ''}` + (firstContent !== null && i === firstContent ? '  <-- 首次完整渲染' : ''));
    }
    if (firstContent !== null && i >= firstContent + 1) break;
  }
  console.log('---- 结果 ----');
  console.log('请求->响应:', reqT && respT ? ((respT - reqT) / 1000).toFixed(1) + 's' : 'n/a', '| HTTP', status, fail ? 'FAIL' : '');
  console.log('面板首次完整渲染:', firstContent ? (firstContent + 's') : '>25s 未渲染');
  console.log('结论:', firstContent !== null ? '冷加载可成功（仅慢，非 bug）' : '冷加载失败（疑似真 bug）');
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.stack || e.message); process.exit(2); });
