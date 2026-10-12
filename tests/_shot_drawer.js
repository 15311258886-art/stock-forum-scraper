const puppeteer = require('puppeteer-core');
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
(async () => {
  const url = process.argv[2] || 'http://127.0.0.1:8765';
  const sym = process.argv[3] || '600519';
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  await page.goto(url + '/index.html', { waitUntil: 'networkidle0' });
  await page.evaluate(() => {
    const tabs = [...document.querySelectorAll('.tab')];
    const t = tabs.find(x => x.textContent.includes('情绪地图')); if (t) t.click();
  });
  await new Promise(r => setTimeout(r, 500));
  const clicked = await page.evaluate((sym) => {
    const el = document.querySelector(`#p-sentiment .stk-card[data-sym="${sym}"]`);
    if (!el) return false;
    el.click();
    return true;
  }, sym);
  console.log('card clicked:', clicked);
  await new Promise(r => setTimeout(r, 600));
  const info = await page.evaluate(() => {
    const heads = [...document.querySelectorAll('#drawerBody .view-group .vg-head')].map(e => e.textContent.trim());
    const items = [...document.querySelectorAll('#drawerBody .view-item')].map(e => ({
      author: (e.querySelector('.vi-author')||{}).textContent,
      reason: (e.querySelector('.vi-reason')||{}).textContent,
    }));
    const title = (document.querySelector('#drawerHead')||{}).textContent;
    return { title: title && title.trim().slice(0,60), heads, items };
  });
  console.log('DRAWER TITLE:', info.title);
  console.log('VIEW GROUPS:', JSON.stringify(info.heads));
  console.log('VIEW ITEMS:', info.items.length);
  info.items.forEach((v,i)=>console.log(`  [${i+1}] ${v.author} :: ${v.reason.slice(0,60)}`));
  await page.screenshot({ path: 'shot_drawer_sentiment.png', fullPage: true });
  console.log('JS errors:', errs.length, errs.slice(0,3).join(' | '));
  await browser.close();
})();
