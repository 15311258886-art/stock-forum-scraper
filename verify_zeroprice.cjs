/* verify_zeroprice.cjs —— 关注池「停牌/无成交标的不得显示 0.00 或 -100%」回归护栏
 *
 * 背景（2026-10-09 王总带截图纠）：关注池里 *ST康佳A(000016，当日停牌) 与 佰仁医疗(688198，盘前瞬时)
 * 被显示成「当前价 0.00 / 涨跌 -100.00%」——根因是取价链把「停牌回 0 价」当成有效行情，
 * 再用 0 算 (0-昨收)/昨收*100 = -100%。属凭空捏造、且与事实相反的结论。
 *
 * 断言（对真实运行的 8765 服务）：
 *   A. 接口层：停牌票 price/pct 必须为 null（绝不 0 / -100）；正常票必须有正价。
 *   B. 界面层：#p-watch 全表文本不得出现 -100.00%；停牌票行显示 «—»；出现「无有效行情」提示条。
 *   C. 控制台零报错。
 * 用法：node verify_zeroprice.cjs        （需 8765 已在跑）
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');
const http = require('http');

const BASE = 'http://127.0.0.1:8765';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) fails.push(msg); };

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get(BASE + path, (res) => {
      let s = '';
      res.on('data', (c) => (s += c));
      res.on('end', () => { try { resolve(JSON.parse(s)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

(async () => {
  // ---------- A. 接口层 ----------
  console.log('\n[A] 接口层：停牌票不得吐 0 / -100');
  let d;
  try {
    d = await getJson('/api/watch_list');
  } catch (e) {
    console.log('  FAIL  接口不可达：' + e.message + '（先确认 8765 在跑）');
    process.exit(1);
  }
  const items = d.items || [];
  ok(items.length > 0, `关注池有标的（${items.length} 只）`);
  const zeroBad = items.filter((it) => it.price === 0 || it.pct === -100);
  ok(zeroBad.length === 0, `无「price=0 / pct=-100」条目（命中 ${zeroBad.length} 只：${zeroBad.map((x) => x.symbol).join(',') || '—'}）`);
  const suspended = items.filter((it) => it.price === null || it.price === undefined);
  console.log(`  INFO  无有效行情的标的：${suspended.map((x) => x.symbol + ' ' + (x.name || '')).join(' / ') || '（本时段无）'}`);
  ok(suspended.every((it) => it.pct === null && it.since_add_pct === null),
     '无价标的的 pct / since_add_pct 一并为空（不残留派生假值）');

  // ---------- B/C. 浏览器 ----------
  console.log('\n[B] 界面层：全表无 -100%、停牌行显 «—»、有说明提示');
  const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof loadWatch === 'function', { timeout: 30000 });
  const info = await page.evaluate(async () => {
    await loadWatch();
    const host = document.querySelector('#p-watch');
    const txt = host ? host.textContent : '';
    const rows = host ? [...host.querySelectorAll('tbody tr')].map((tr) => tr.textContent.replace(/\s+/g, ' ').trim()) : [];
    const hintEl = host ? host.querySelector('.wt-noquote') : null;
    return { txt, rows, rowsN: rows.length, hint: hintEl ? hintEl.textContent.replace(/\s+/g, ' ').trim() : '' };
  });
  ok(!info.txt.includes('-100.00'), '关注池全表不含「-100.00%」');
  ok(info.rowsN === items.length, `表体行数(${info.rowsN}) == 接口标的数(${items.length})`);
  if (suspended.length) {
    for (const s of suspended) {
      const row = info.rows.find((t) => t.includes(s.symbol)) || '';
      ok(row.includes('—') || row.includes('«'), `${s.symbol} 行以 «—» 表示取不到价`);
      ok(!/\-?0\.00%/.test(row), `${s.symbol} 行不出涨跌幅数字`);
    }
    ok(!!info.hint && info.hint.includes('无有效行情'), '出现「无有效行情」说明条');
    ok(info.hint.includes(String(suspended.length)), `说明条数量与接口一致（${suspended.length}）`);
  } else {
    console.log('  INFO  当前无停牌标的，跳过针对性断言（接口层断言已覆盖 0 价规则）');
  }

  console.log('\n[C] 控制台');
  ok(errs.length === 0, `零控制台报错（${errs.length}）` + (errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''));

  await browser.close();
  console.log('\n==== ' + (fails.length ? 'FAIL ' + fails.length + ' 项' : 'ALL PASS') + ' ====');
  if (fails.length) { fails.forEach((f) => console.log('  - ' + f)); process.exit(1); }
})();
