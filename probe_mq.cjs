const puppeteer = require('puppeteer-core');
const fs = require('fs');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function pick() { try { fs.accessSync(CHROME); return CHROME; } catch (e) {} try { fs.accessSync(EDGE); return EDGE; } catch (e) {} return CHROME; }

(async () => {
  const browser = await puppeteer.launch({
    executablePath: pick(), headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const page = await browser.newPage();
  const mqReqs = [];
  page.on('response', (r) => { if (r.url().includes('multiquote')) mqReqs.push(r.status() + ' ' + r.url()); });
  await page.goto(URL + '/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(2500);

  // 1) 仿审计：先走 diag（填代码+诊断，开抽屉）
  await page.evaluate(() => { location.hash = '#watch/diag'; });
  await sleep(2600);
  await page.evaluate(() => {
    const inp = document.querySelector('#dgSym');
    if (inp) { inp.value = '600519'; inp.dispatchEvent(new Event('input', { bubbles: true })); }
    const btn = document.querySelector('#dgGo'); if (btn) btn.click();
  });
  await sleep(9000);
  const diag = await page.evaluate(() => {
    const b = document.querySelector('#p-diag'); const d = document.querySelector('#drawer');
    return { diagLen: b ? b.textContent.length : -1, drawerOpen: d ? d.classList.contains('show') : false };
  });
  console.log('after diag:', JSON.stringify(diag));

  // 2) 不关闭抽屉，直接切到 multiquote（仿审计的相邻切换）
  await page.evaluate(() => { location.hash = '#watch/multiquote'; });
  await sleep(6000);
  const mq = await page.evaluate(() => {
    const b = document.querySelector('#p-multiquote');
    return { innerLen: b ? b.innerHTML.length : -1, rows: b ? b.querySelectorAll('tbody tr').length : -1,
             active: b ? b.classList.contains('active') : false };
  });
  console.log('after diag->multiquote:', JSON.stringify(mq));
  console.log('mq responses:', JSON.stringify(mqReqs));
  await browser.close();
})();
