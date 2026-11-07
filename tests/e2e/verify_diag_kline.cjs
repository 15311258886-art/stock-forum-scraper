/* 个股诊断面板回归：核心指标行（巴菲特口径 8 项）+ 内置 K 线（真实绘制，非空白 canvas）。
   用法：node verify_diag_kline.cjs   （后端需在 8765 跑） */
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const p = await b.newPage();
  await p.setViewport({ width: 1440, height: 950 });
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message.slice(0, 200)));

  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);
  await p.evaluate(() => { location.hash = '#watch/diag'; });
  await sleep(1500);

  // 输入代码并诊断
  const started = await p.evaluate(() => {
    const inp = document.querySelector('#dgSym');
    const go = document.querySelector('#dgGo');
    if (!inp || !go) return false;
    inp.value = '002001';
    go.click();
    return true;
  });
  console.log('点「开始诊断」:', started);
  // 等待指标行与 K 线出现（首诊约 2–6s，K 线单独一路）
  let ready = false;
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    ready = await p.evaluate(() => {
      const bf = document.querySelectorAll('#p-diag .dg-bf-item').length;
      const cv = document.querySelector('#dgKlineCanvas');
      const px = cv ? (() => {
        try {
          const ctx = cv.getContext('2d');
          const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
          let n = 0;
          for (let k = 3; k < d.length; k += 4) if (d[k] > 0) n++;
          return n;
        } catch (e) { return -1; }
      })() : 0;
      return bf >= 8 && px > 1000;
    });
    if (ready) { console.log(`就绪（第 ${i + 1} 秒）`); break; }
  }

  const info = await p.evaluate(() => {
    const host = document.querySelector('#p-diag');
    const items = [...host.querySelectorAll('.dg-bf-item')].map((x) => ({
      k: (x.querySelector('.k') || {}).textContent,
      v: (x.querySelector('.v') || {}).textContent,
      t: (x.querySelector('.t') || {}).textContent,
      pass: x.classList.contains('pass'), fail: x.classList.contains('fail'),
    }));
    const cv = document.querySelector('#dgKlineCanvas');
    let ink = 0;
    if (cv) {
      const ctx = cv.getContext('2d');
      const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
      for (let k = 3; k < d.length; k += 4) if (d[k] > 0) ink++;
    }
    return {
      命中: (host.querySelector('.dg-bf-hd span') || {}).textContent,
      指标: items,
      快照: (host.querySelector('#dgKlineSnap') || {}).textContent?.replace(/\s+/g, ' ').trim().slice(0, 110),
      区间: (document.querySelector('#dgKlineRange') || {}).textContent,
      canvas: cv ? { w: cv.width, h: cv.height, ink } : null,
      周期按钮: [...host.querySelectorAll('.kbtn')].map((x) => x.textContent + (x.classList.contains('active') ? '(选中)' : '')),
    };
  });

  console.log('\n【核心指标行】' + info.命中);
  info.指标.forEach((x) => console.log(`   ${x.k.padEnd(12)} ${String(x.v).padEnd(10)} ${x.t.padEnd(8)} ${x.pass ? '✓达标' : (x.fail ? '—未达' : '?无数据')}`));
  console.log('\n【K 线快照】' + info.快照);
  console.log('【区间】' + info.区间);
  console.log('【canvas】', JSON.stringify(info.canvas));
  console.log('【周期按钮】' + info.周期按钮.join(' / '));

  // 切到周K 验证重绘
  const beforeRange = info.区间;
  await p.evaluate(() => {
    const b = [...document.querySelectorAll('#p-diag .kbtn')].find((x) => x.dataset.p === 'week');
    if (b) b.click();
  });
  await sleep(2500);
  const week = await p.evaluate(() => {
    const cv = document.querySelector('#dgKlineCanvas');
    let ink = 0;
    if (cv) {
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      for (let k = 3; k < d.length; k += 4) if (d[k] > 0) ink++;
    }
    return { 区间: (document.querySelector('#dgKlineRange') || {}).textContent,
             快照: (document.querySelector('#dgKlineSnap') || {}).textContent?.replace(/\s+/g, ' ').trim().slice(0, 80),
             ink };
  });
  console.log('\n【切周K 后】区间=' + week.区间 + ' | ink=' + week.ink);
  console.log('【切周K 后快照】' + week.快照);

  // ---- 悬停：十字光标读数（tip 应出现且有内容）----
  // 注意：诊断面板内容长，canvas 常在视口外；必须先滚进视口，否则 mouse.move 落不到元素上
  await p.evaluate(() => {
    const cv = document.querySelector('#dgKlineCanvas');
    if (cv) cv.scrollIntoView({ block: 'center' });
  });
  await sleep(700);
  const box = await p.evaluate(() => {
    const cv = document.querySelector('#dgKlineCanvas');
    if (!cv) return null;
    const r = cv.getBoundingClientRect();
    return { x: r.left + r.width * 0.6, y: r.top + r.height * 0.4, top: Math.round(r.top), h: Math.round(r.height) };
  });
  if (box) {
    await p.mouse.move(box.x, box.y);
    await sleep(700);
    const tip = await p.evaluate(() => {
      const t = document.querySelector('#dgKlineTip');
      return { hidden: t ? t.hidden : null, txt: t ? t.textContent.replace(/\s+/g, ' ').trim().slice(0, 70) : null };
    });
    console.log('\n【悬停读数】hidden=' + tip.hidden + ' | ' + tip.txt);
  }

  // ---- 换一只票：指标行与 K 线是否随之更新 ----
  await p.evaluate(() => {
    const inp = document.querySelector('#dgSym');
    const go = document.querySelector('#dgGo');
    inp.value = '600938'; go.click();
  });
  await sleep(9000);
  const sw = await p.evaluate(() => {
    const host = document.querySelector('#p-diag');
    const first = host.querySelector('.dg-bf-item');
    const cv = document.querySelector('#dgKlineCanvas');
    let ink = 0;
    if (cv) { const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; for (let k = 3; k < d.length; k += 4) if (d[k] > 0) ink++; }
    return { 命中: (host.querySelector('.dg-bf-hd span') || {}).textContent,
             首项: first ? first.querySelector('.k').textContent + '=' + first.querySelector('.v').textContent : null,
             快照: (host.querySelector('#dgKlineSnap') || {}).textContent?.replace(/\s+/g, ' ').trim().slice(0, 60),
             ink };
  });
  console.log('\n【换 600938】' + sw.命中 + ' | ' + sw.首项 + ' | K线ink=' + sw.ink);
  console.log('【换 600938 快照】' + sw.快照);

  // ---- 关键回归：诊断面板的 K 线组件不得影响个股抽屉那套 K 线 ----
  await p.evaluate(() => { openStock('600938'); });
  await sleep(8000);
  const dr = await p.evaluate(() => {
    const cv = document.querySelector('#klineCanvas');
    let ink = 0;
    if (cv) { const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; for (let k = 3; k < d.length; k += 4) if (d[k] > 0) ink++; }
    return {
      抽屉打开: !!document.querySelector('#drawer.show'),
      抽屉K线ink: ink,
      抽屉K线区间: (document.querySelector('#klineRange') || {}).textContent,
      诊断面板仍在: !!document.querySelector('#p-diag .dg-bf-item'),
      诊断K线仍在: !!document.querySelector('#dgKlineCanvas'),
    };
  });
  console.log('\n【抽屉 K 线回归】', JSON.stringify(dr));

  console.log('\n控制台错误:', errs.length ? errs.join(' | ') : '无');
  await b.close();
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
