// 2026-10-01 王总要求：以「筹码集中度」那次（标签说 A、实际按 B 判）为标尺，全站自查口径不一的判词。
// 本脚本验证自查修完后的四项在**真实浏览器**里生效：
//   ① 新闻/公告分类规则从 newsclass.py 单一下发（window.NEWS_RULES），前端不再自带副本
//   ② 前端 newsClassify() 与后端 classify() 对同一批标题给出同一结果（不再两套词表）
//   ③ 选股档位最低档不要再叫「高风险预警」（改叫「技术面弱势」并注明非风险事件）
//   ④ 页面零控制台报错
// 用法：node verify_judge.cjs   （需 8765 在跑；浏览器用本机 Edge）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.VURL || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 与后端 viewer/newsclass.py ANN_TITLE_RULES 同一批标题，期望 (cat, tone) 必须一致
const TITLE_CASES = [
  ['公司拟回购股份', '股东回报', 'good'],
  ['涉嫌违规被立案调查', '风险监管', 'bad'],
  ['*ST公司发布退市风险提示', '风险监管', 'bad'],
  ['净利润同比预增120%', '业绩向好', 'good'],
  ['更换会计师事务所', '人事治理', 'neutral'],
  ['拟减持不超过2%股份', '股东减持', 'bad'],
];

(async () => {
  if (!fs.existsSync(EDGE)) { console.error('找不到 Edge'); process.exit(1); }
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1300 });
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);

  let pass = 0, total = 0;
  const say = (ok, msg) => { total++; if (ok) pass++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

  // ---------- ① 规则表已由后端单下发 ----------
  const rules = await page.evaluate(() => (window.NEWS_RULES || []).length);
  say(rules === 10, `① window.NEWS_RULES 由后端下发，条数=${rules}（期望 10）`);

  // ---------- ② 前端 newsClassify 与后端同源 ----------
  const got = await page.evaluate((cases) => {
    const out = [];
    for (const [t] of cases) {
      try { out.push(newsCatOf(t)); } catch (e) { out.push(['ERR', String(e.message)]); }
    }
    return out;
  }, TITLE_CASES);
  TITLE_CASES.forEach(([title, cat, tone], i) => {
    const g = got[i] || [];
    say(g[0] === cat && g[1] === tone,
      `② "${title}" → ${g[0]}/${g[1]}（期望 ${cat}/${tone}）`);
  });

  // ---------- ③ 选股档位文案（源码级，跑不起面板时也能验） ----------
  const src = fs.readFileSync(require('path').join(__dirname, 'viewer/static/app.js'), 'utf8');
  say(src.includes('&lt;30 技术面弱势') && src.includes('技术面弱势·不参与'), '③ 档位说明已改「<30 技术面弱势」（源码 HTML 转义为 &lt;30，渲染即 <30）');
  say(/不是<\/b>风险事件分级|不是<\/b>风险事件分级/.test(src) || src.includes('不是</b>风险事件分级'),
    '③ 档位说明已注明「不是风险事件分级」');
  say(src.includes('window.NEWS_RULES'), '③ newsClassify 已改为读后端下发规则（无本地副本）');

  // ---------- ④ 控制台 ----------
  say(errs.length === 0, `④ 控制台零报错（实际 ${errs.length} 条${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}）`);

  await page.screenshot({ path: 'shot_judge.png', fullPage: false });
  console.log(`\n截图：shot_judge.png`);
  console.log(`\n结果：${pass}/${total} 通过`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})().catch((e) => { console.error('脚本异常：', e); process.exit(1); });
