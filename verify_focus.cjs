// 2026-10-01 王总反馈：散户(估算) 86.33% 标「较分散」、散户 46.48% 却标「非常分散」，看不懂。
// 根因：「集中度」用的是东财 HOLD_FOCUS（按股东户数绝对人数分档），与上方机构/自然人/散户色带不是一把尺。
// 本脚本验证改成「按散户占比判集中度」后，两只票的文案自洽 + 户数趋势单独成行。
// 用法：node verify_focus.cjs   （需 8765 在跑；浏览器用本机 Edge）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// [代码, 中文名, 期望的集中度词, 期望的趋势词]
const CASES = [
  ['300505', '川金诺', '非常分散', '收拢'],     // 散户 86.33% → 分散；户数 ▼10.65% → 收拢
  ['600545', '卓郎智能', '基本均衡', '发散'],   // 散户 46.48% → 均衡；户数 ▲40.47% → 发散
];

(async () => {
  if (!fs.existsSync(EDGE)) { console.error('找不到 Edge'); process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1400 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);

  let pass = 0, total = 0;
  for (const [code, name, wantFocus, wantTrend] of CASES) {
    total++;
    await page.evaluate((c) => { try { openStock(c); } catch (e) { console.warn('openStock', e.message); } }, code);
    await sleep(4000);
    const got = await page.evaluate(() => {
      const t = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.replace(/\s+/g, ' ').trim() : null; };
      return {
        concl: t('.sh-concl'),
        hn: t('.sh-hn'),
        legend: t('.sh-legend'),
        // 顺带确认旧的东财透传词已经不出现在「集中度」位
        hasOldFocusRow: Array.from(document.querySelectorAll('.sh-hn')).some((e) => /集中度/.test(e.innerText)),
      };
    });
    const okFocus = got.concl && got.concl.includes(wantFocus);
    const okTrend = got.hn && got.hn.includes(wantTrend);
    const okNoOld = got.hasOldFocusRow === false;
    const ok = okFocus && okTrend && okNoOld;
    if (ok) pass++;
    console.log(`\n===== ${code} ${name} ${ok ? 'PASS' : 'FAIL'} =====`);
    console.log(' 色带:', got.legend);
    console.log(' 结论:', got.concl);
    console.log(' 户数:', got.hn);
    console.log(` 集中度含「${wantFocus}」=${okFocus} · 趋势含「${wantTrend}」=${okTrend} · 旧东财集中度行已移除=${okNoOld}`);
    await page.screenshot({ path: `shot_focus_${code}.png`, fullPage: false });
    // 关抽屉，准备下一只
    await page.evaluate(() => { try { closeStock && closeStock(); } catch (e) { /* 无此函数则按 ESC */ } });
    await page.keyboard.press('Escape');
    await sleep(800);
  }

  console.log('\n---- 控制台异常:', errs.length ? errs.join('\n') : '无');
  console.log(`\n合计 ${pass}/${total} 通过`);
  await browser.close();
  process.exit(pass === total && errs.length === 0 ? 0 : 1);
})();
