/* 回归：B站博主分组「股票卡片式大卡」（2026-10-12 王总：字太小看不清） */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const fails = [];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails.push(m); };

function findChrome() {
  const cands = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    ...fs.readdirSync('C:/Users/野比&大熊/AppData/Local/ms-playwright')
      .filter(d => d.startsWith('chromium-'))
      .map(d => `C:/Users/野比&大熊/AppData/Local/ms-playwright/${d}/chrome-win64/chrome.exe`),
  ];
  for (const p of cands) { try { if (fs.existsSync(p)) return p; } catch (e) {} }
  return null;
}

(async () => {
  const exe = findChrome();
  ok(!!exe, '找到浏览器可执行文件: ' + exe);
  const browser = await puppeteer.launch({ executablePath: exe, headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  // 用哈希深链接切到 复盘成长 → B站博主
  await page.evaluate(() => { location.hash = '#learn/bili_authors'; });
  // 轮询等卡片渲染（pile API 5000 条较大，首拉要几秒）
  let n = 0;
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 700));
    n = await page.evaluate(() => document.querySelectorAll('#p-bili_authors .ba-card').length);
    if (n > 0) break;
  }
  ok(n >= 20, `B站博主卡片数 ${n} ≥ 20`);

  const secs = await page.evaluate(() => document.querySelectorAll('#p-bili_authors .ba-sec').length);
  ok(secs >= 2, `分组区块数（价值/技术/情绪）${secs} ≥ 2`);

  // 字号核验：名字 ≥14px、条数大数字 ≥15px、分组标题 ≥15px
  const fonts = await page.evaluate(() => {
    const g = s => { const el = document.querySelector('#p-bili_authors ' + s); if (!el) return null; return parseFloat(getComputedStyle(el).fontSize); };
    return { name: g('.ba-name'), stat: g('.ba-stat b'), hd: g('.ba-sec-hd'), sub: g('.ba-sub') };
  });
  ok(fonts.name >= 14, `博主名字字号 ${fonts.name}px ≥ 14`);
  ok(fonts.stat >= 15, `条数大数字字号 ${fonts.stat}px ≥ 15`);
  ok(fonts.hd >= 15, `分组标题字号 ${fonts.hd}px ≥ 15`);
  ok(fonts.sub >= 12, `副行字号 ${fonts.sub}px ≥ 12`);

  // 旧的小药丸不该再出现在该面板
  const oldPills = await page.evaluate(() => document.querySelectorAll('#p-bili_authors .btn[data-blogger]').length);
  ok(oldPills === 0, `旧小药丸残留 ${oldPills} == 0`);

  // 有知识库徽标存在（金色描边卡）
  const kb = await page.evaluate(() => document.querySelectorAll('#p-bili_authors .ba-card.has-kb').length);
  ok(kb >= 1, `「有知识库」卡片数 ${kb} ≥ 1`);

  // 点击行为回归：点无知识库的卡 → 打开视频列表抽屉；点有知识库的卡 → 跳知识沉淀
  const nav1 = await page.evaluate(() => new Promise(res => {
    const c = [...document.querySelectorAll('#p-bili_authors .ba-card')].find(x => !x.classList.contains('has-kb'));
    if (!c) return res('no-card');
    c.click();
    setTimeout(() => res(document.querySelector('#drawer') && document.querySelector('#drawer').classList.contains('show') ? 'drawer' : 'none'), 1500);
  }));
  ok(nav1 === 'drawer', `点无知识库卡 → 抽屉打开（结果=${nav1}）`);
  await page.evaluate(() => { const d = document.querySelector('#drawer'); if (d) d.classList.remove('show'); });

  const nav2 = await page.evaluate(() => new Promise(res => {
    const c = document.querySelector('#p-bili_authors .ba-card.has-kb');
    if (!c) return res('no-kb-card');
    c.click();
    setTimeout(() => res(location.hash.indexOf('learn_') >= 0 ? 'nav' : location.hash), 1500);
  }));
  ok(nav2 === 'nav', `点有知识库卡 → 跳知识沉淀轨道（hash=${nav2}）`);

  // 截图
  await page.evaluate(() => { location.hash = '#learn/bili_authors'; });
  await new Promise(r => setTimeout(r, 2200));
  await page.screenshot({ path: '_biliauthors_cards.png', fullPage: false });
  const el = await page.$('#p-bili_authors');
  if (el) await el.screenshot({ path: '_biliauthors_cards_panel.png' }).catch(() => {});

  ok(errors.length === 0, `控制台零报错（${errors.length}）${errors.slice(0, 3).join(' | ')}`);
  await browser.close();
  console.log(fails.length ? `\nRESULT: FAIL (${fails.length})` : '\nRESULT: ALL PASS');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
