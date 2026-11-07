/* 回归验证：B站博主分组框与后台数据同步（2026-10-11 晚，王总指出前后不一致）
   断言：① 深链 #learn/bili_authors 正常渲染；
        ② 面板按 价值/技术/情绪 三堆分组、总人数=后台 bili_pile 实际博主数（17）；
        ③ 「赚1亿」零残留（数据已删，展示必须同步消失）；
        ④ 有知识库的博主（6 位）带「有知识库」标；无轨道博主可点（视频抽屉）；
        ⑤ 控制台零报错。截图 _authsync_shot.png。
*/
const fs = require('fs');
const path = require('path');
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const ROOT = 'http://127.0.0.1:8765';
const SHOT = path.join(__dirname, '_authsync_shot.png');
const PW = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright';
let CHROME = null;
if (fs.existsSync(PW)) {
  for (const d of fs.readdirSync(PW)) {
    if (d.startsWith('chromium-')) {
      const p = path.join(PW, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(p)) { CHROME = p; break; }
    }
  }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) fails.push(msg); };

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,900'],
    defaultViewport: { width: 1440, height: 900 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
  page.on('response', r => { if (r.status() >= 400) errs.push(`HTTP ${r.status()} ${r.url()}`); });

  await page.goto(ROOT + '/#learn/bili_authors', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#p-bili_authors button[data-blogger]', { timeout: 30000 });
  await sleep(1200);

  const txt = await page.$eval('#p-bili_authors', e => e.innerText);
  const btns = await page.$$eval('#p-bili_authors button[data-blogger]', els => els.map(e => e.dataset.blogger));
  const uniq = [...new Set(btns)];

  ok(uniq.length === 17, `面板博主数 = ${uniq.length}（应为 17，与 bili_pile 一致）`);
  ok(!txt.includes('赚1亿'), '「赚1亿」零残留（已删数据的博主不再显示）');
  ok(txt.includes('价值主播') && txt.includes('技术主播') && txt.includes('情绪主播'), '三堆分组框齐全（价值/技术/情绪）');
  for (const nm of ['李大霄', '土斯土耶夫斯基', '史诗级韭菜', '小师妹财经', '价值兔_财报投资避雷']) {
    ok(uniq.includes(nm), `真实博主在列：${nm}`);
  }
  const dkBloggers = await page.$$eval('#p-bili_authors button[data-blogger]', els =>
    [...new Set(els.filter(e => e.dataset.dk).map(e => e.dataset.blogger))]);
  ok(dkBloggers.length === 6, `有知识库博主数（去重）= ${dkBloggers.length}（应为 6：土斯/牛筋丸/鼠柚子/土豆/李大霄/知行）`);
  const noDk = uniq.filter(b => !dkBloggers.includes(b));
  ok(noDk.length === 11, `无轨道博主数 = ${noDk.length}（应为 11，点名走视频列表）`);

  // 点一个无轨道博主 → 抽屉视频列表（正文容器是 #drawerBody）
  await page.evaluate(() => {
    const b = [...document.querySelectorAll('#p-bili_authors button[data-blogger]')]
      .find(x => !x.dataset.dk);
    if (b) b.click();
  });
  await sleep(1500);
  const drawerTxt = await page.evaluate(() => {
    const d = document.querySelector('#drawer');
    const t = document.querySelector('#drawerTitle');
    const body = document.querySelector('#drawerBody');
    return (d && d.classList.contains('show')) ? (t.textContent + '||' + (body ? body.innerText.length : 0)) : '';
  });
  ok(drawerTxt.includes('视频列表'), '无轨道博主点名 → 打开视频列表抽屉（#drawer.show，标题含「视频列表」，正文 ' + drawerTxt.split('||')[1] + ' 字符）');

  await page.screenshot({ path: SHOT, fullPage: false });
  console.log('  截图 ->', SHOT);
  ok(errs.length === 0, '控制台零报错' + (errs.length ? '；实际: ' + errs.slice(0, 3).join(' | ') : ''));

  await browser.close();
  console.log(fails.length ? `\nEXIT=1（${fails.length} 项 FAIL）` : '\nALL PASS, EXIT=0');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(2); });
