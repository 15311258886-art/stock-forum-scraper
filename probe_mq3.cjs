// 冷加载时延探针：量化 /api/multiquote_preset 首次（缓存未命中）延迟，
// 并验证面板最终是否渲染成功 —— 以区分「真 bug（永不返回/报错）」与「仅仅是慢（审计等待不足）」。
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
  const t0 = {};      // 请求发起时间
  let firstByte = null, done = null, status = null, rows = null;
  page.on('request', (r) => { if (r.url().includes('/api/multiquote_preset')) t0.url = Date.now(); });
  page.on('response', (r) => {
    if (r.url().includes('/api/multiquote_preset')) { firstByte = Date.now(); status = r.status(); }
  });

  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(2000);
  // 清掉可能的服务端缓存，确保冷加载
  await page.evaluate(() => { try { if (window.FEAT && FEAT.multiquote) FEAT.multiquote.data = null; } catch (e) {} });
  console.log('冷加载开始 -> 导航到 #watch/multiquote');
  const navStart = Date.now();
  await page.evaluate(() => { location.hash = '#watch/multiquote'; });

  // 每 1s 轮询面板长度，记录首次 >40 的时刻
  let firstRender = null;
  for (let i = 1; i <= 20; i++) {
    await sleep(1000);
    const len = await page.evaluate(() => {
      const b = document.querySelector('#p-multiquote');
      return b ? (b.textContent || '').replace(/\s+/g, ' ').trim().length : -1;
    });
    if (firstRender === null && len > 40) firstRender = i;
    if (i % 2 === 0 || firstRender !== null) {
      const rel = ((Date.now() - navStart) / 1000).toFixed(1);
      console.log(`  t+${rel}s  panelLen=${len}` + (firstRender !== null && i === firstRender ? '  <-- 首次渲染' : ''));
    }
    if (firstRender !== null && i >= firstRender + 1) break;
  }
  // 读取最终行数
  rows = await page.evaluate(() => {
    const tb = document.querySelector('#p-multiquote tbody');
    return tb ? tb.querySelectorAll('tr').length : -1;
  });
  const navEnd = Date.now();
  console.log('---- 结果 ----');
  console.log('请求发起 -> 首字节:', firstByte && t0.url ? ((firstByte - t0.url) / 1000).toFixed(1) + 's' : 'n/a', '| HTTP', status);
  console.log('导航 -> 面板首次渲染:', firstRender ? (firstRender + 's') : '>20s 未渲染');
  console.log('最终表格行数:', rows);
  console.log('结论:', rows > 0 ? '冷加载可成功（仅慢，非 bug）' : '冷加载失败（疑似真 bug）');
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.stack || e.message); process.exit(2); });
