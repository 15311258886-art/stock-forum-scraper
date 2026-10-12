const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const URL = 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--proxy-server=direct://','--proxy-bypass-list=*'] });
  const page = await browser.newPage();
  const logs = [];
  page.on('pageerror', (e) => logs.push('[PAGEERROR] ' + e.message.slice(0,300)));
  page.on('console', (m) => { if (m.type()==='error') logs.push('[CONSOLE] ' + m.text().slice(0,300)); });
  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);
  await page.evaluate(() => { location.hash = '#learn/buffett'; });
  await sleep(2500);
  const info = await page.evaluate(() => {
    const box = document.querySelector('#p-buffett');
    if (!box) return { exists:false };
    const cards = box.querySelectorAll('.bf-card');
    const first = box.querySelector('.bf-card .bf-open') || box.querySelector('.bf-card');
    if (first) first.click();
    return { exists:true, len:(box.textContent||'').replace(/\s+/g,' ').trim().length,
      cards: cards.length, hasSearch: !!box.querySelector('#bfQ'),
      head:(box.textContent||'').replace(/\s+/g,' ').trim().slice(0,160) };
  });
  let modal = { opened:false };
  for (let i=0;i<12;i++){ await sleep(500);
    modal = await page.evaluate(() => {
      const m = document.querySelector('#bfModal');
      const f = m ? m.querySelector('.bf-modal-iframe') : null;
      return { opened: !!(m && m.classList.contains('open') && f && f.src && f.src.indexOf('about:blank')<0),
        src: f ? f.src : null, title: m ? (m.querySelector('.bf-modal-title')||{}).textContent : null };
    });
    if (modal.opened) break;
  }
  console.log('【巴菲特选股】面板:', JSON.stringify(info));
  console.log('【点开研报】modal:', JSON.stringify(modal));
  console.log('【控制台】', logs.length? logs.join('\n'):'无');
  await browser.close();
})().catch((e)=>{console.error('FATAL',e.stack||e.message);process.exit(1);});
