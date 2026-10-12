// QA(Edward) 独立验证 · 第4项：C+D 前端（无头 Chromium）
// 校验：共识列 总/买/持/关注 拆分；「仅看入围选手」开关点击后行数变化；控制台零报错；其它主面板无回归。
import puppeteer from 'puppeteer-core';
import fs from 'fs';

const CHROME = 'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8765';
const OUT = 'tools/_qa_abcd/out';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const consoleErrors = [];
const pageErrors = [];

function report(tag, ok, detail) {
  console.log(`${ok ? 'PASS' : 'FAIL'} | ${tag} | ${detail}`);
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--window-size=1600,1200'],
  defaultViewport: { width: 1580, height: 1100 },
});
const page = await browser.newPage();
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', (e) => pageErrors.push(String(e)));

async function gotoHash(hash, waitSel, waitMs = 4500) {
  await page.goto(`${BASE}/${hash}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (waitSel) {
    try { await page.waitForSelector(waitSel, { timeout: 20000 }); } catch (e) { /* 记为空 */ }
  }
  await sleep(waitMs);
}

// ---------- 1) 共识面板 ----------
console.log('== 1) 共同持仓共识 ==');
await gotoHash('#players/consensus', '#p-consensus table');
const consInfo = await page.evaluate(() => {
  const p = document.querySelector('#p-consensus');
  if (!p) return { rows: 0, header: '', sampleCells: [], hasToggle: false, rowTexts: [] };
  const rws = [...p.querySelectorAll('tbody tr')];
  const firstRow = rws[0];
  const header = (p.querySelector('thead') || {}).innerText || '';
  const labels = [...p.querySelectorAll('label.q-toggle')];
  const l = labels.find((x) => x.textContent.includes('仅看入围选手'));
  return {
    rows: rws.length,
    header: header.replace(/\s+/g, ' ').slice(0, 200),
    sampleCells: firstRow ? [...firstRow.querySelectorAll('td')].map((td) => td.innerText.replace(/\s+/g, ' ').slice(0, 60)) : [],
    hasToggle: !!l,
    rowTexts: rws.slice(0, 5).map((tr) => tr.innerText.replace(/\s+/g, ' ').slice(0, 120)),
  };
});
await page.screenshot({ path: `${OUT}/fe_consensus_before.png`, fullPage: true });
report('共识-表格渲染', consInfo.rows > 0, `行数=${consInfo.rows}`);
report('共识-列含总/买/持/关注 拆分', /总|口径拆分/.test(consInfo.header) && /买/.test(consInfo.header) && /持/.test(consInfo.header), `表头="${consInfo.header}"`);
report('共识-数据行出现"买 N / 持 N"', consInfo.sampleCells.some((c) => /买\s*\d|持\s*\d/.test(c)), `首行单元格=${JSON.stringify(consInfo.sampleCells)}`);
report('共识-存在"仅看入围选手"开关', consInfo.hasToggle, `hasToggle=${consInfo.hasToggle}`);
console.log('   前5行:', JSON.stringify(consInfo.rowTexts, null, 0));

// 点击开关
const beforeRows = consInfo.rows;
if (consInfo.hasToggle) {
  const clicked = await page.evaluate(() => {
    const labels = [...document.querySelectorAll('#p-consensus label.q-toggle')];
    const l = labels.find((x) => x.textContent.includes('仅看入围选手'));
    const cb = l && l.querySelector('input[type=checkbox]');
    if (!cb) return false;
    cb.click();
    return true;
  });
  await sleep(6000);
  const after = await page.evaluate(() => {
    const p = document.querySelector('#p-consensus');
    const rws = [...p.querySelectorAll('tbody tr')];
    const cb = [...p.querySelectorAll('label.q-toggle')].find((x) => x.textContent.includes('仅看入围选手')).querySelector('input');
    return { rows: rws.length, checked: cb.checked, rowTexts: rws.slice(0, 6).map((tr) => tr.innerText.replace(/\s+/g, ' ').slice(0, 100)) };
  });
  await page.screenshot({ path: `${OUT}/fe_consensus_after.png`, fullPage: true });
  report('共识-点击开关后 checkbox 变为选中', after.checked === true, `checked=${after.checked}`);
  report('共识-开关真的改变行数(非只回显)', after.rows !== beforeRows, `before=${beforeRows} → after=${after.rows}`);
  console.log('   勾选后行:', JSON.stringify(after.rowTexts, null, 0));
} else {
  report('共识-开关点击', false, '未找到开关，无法点击');
}

// ---------- 2) 共同买入面板 ----------
console.log('\n== 2) 近5日共同买入 ==');
await gotoHash('#players/cohold', '#p-cohold');
const coholdInfo = await page.evaluate(() => {
  const p = document.querySelector('#p-cohold');
  return { textLen: (p ? p.innerText.length : 0), hasToggle: !!([...(p ? p.querySelectorAll('label.q-toggle') : [])].find((x) => x.textContent.includes('仅看入围选手'))), text: p ? p.innerText.replace(/\s+/g, ' ').slice(0, 200) : '' };
});
await page.screenshot({ path: `${OUT}/fe_cohold.png`, fullPage: true });
report('共同买入-面板渲染', coholdInfo.textLen > 20, `文本长度=${coholdInfo.textLen}`);
console.log('   面板文本:', coholdInfo.text);

// ---------- 3) 其它主面板回归 ----------
console.log('\n== 3) 其它主面板回归 ==');
const panels = [
  ['#overview/dashboard', '#p-dashboard', '今日看板/集合竞价'],
  ['#overview/prediction', '#p-dashboard', '每日预测'],
  ['#opinion/blogger', '#p-blogger', '博主荐股'],
  ['#info/news', '#p-news', '个股快讯公告'],
];
for (const [hash, sel, name] of panels) {
  const errBefore = consoleErrors.length + pageErrors.length;
  await gotoHash(hash, sel, 6500);
  const info = await page.evaluate((s) => {
    const el = document.querySelector(s);
    const txt = el ? el.innerText.replace(/\s+/g, ' ') : '';
    return { len: txt.length, head: txt.slice(0, 160) };
  }, sel);
  const shot = `${OUT}/fe_${hash.split('/')[1]}.png`;
  await page.screenshot({ path: shot, fullPage: true });
  const newErr = consoleErrors.length + pageErrors.length - errBefore;
  const hasErrText = /加载失败|Failed to fetch|undefined is not|Cannot read/.test(info.head);
  report(`面板[${name}]`, info.len > 30 && !hasErrText, `文本长度=${info.len} 新增报错=${newErr} 头="${info.head}"`);
}

// ---------- 汇总 ----------
console.log('\n== 控制台/页面错误 ==');
const benign = (t) => /favicon|net::ERR_ABORTED|ERR_INTERNET_DISCONNECTED|Failed to load resource: net::ERR_FAILED/i.test(t);
const realCons = consoleErrors.filter((t) => !benign(t));
console.log('   console.error 总数:', consoleErrors.length, ' 去噪后:', realCons.length);
realCons.slice(0, 20).forEach((t) => console.log('     CERR:', t.slice(0, 200)));
console.log('   pageerror 总数:', pageErrors.length);
pageErrors.slice(0, 20).forEach((t) => console.log('     PERR:', t.slice(0, 200)));
report('控制台无真实报错', realCons.length === 0 && pageErrors.length === 0, `console=${realCons.length} pageerror=${pageErrors.length}`);

await browser.close();
console.log('\nDONE');
