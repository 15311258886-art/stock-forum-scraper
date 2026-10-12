/* 外部参考·事后收益（T+1/T+5/T+10）回填验证
 *
 * 背景（王总 2026-09-30 反馈）：信号详情里 T+1/T+5/T+10 全是「—」。
 * 根因 = 旧回测在抓取失败/发布当日无 K 线时直接判 na，而 na 是终态 → 永久冻结。
 * 本轮修：多源兜底（腾讯→新浪）+ 部分回填（T+1 先出）+ na 定期自愈 + 存量 41 条修复。
 *
 * 本脚本做真实点开验证（不是看接口）：
 *  1) 三个外部面板里能不能找到 600825 新华传媒 的信号卡；
 *  2) 点开后抽屉「② 事后收益」是否显示真实百分比（不再是「—」）；
 *  3) 口径文案是否为「部分回填」；
 *  4) 零控制台报错。并截图存证。
 */
const puppeteer = require('puppeteer-core');
const CHROME = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function ok(cond, label) { results.push({ pass: !!cond, label }); console.log((cond ? '  ✓ ' : '  ✗ ') + label); }

(async () => {
  const b = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const p = await b.newPage();
  await p.setViewport({ width: 1600, height: 950 });
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message.slice(0, 200)));
  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);

  // ---- 1) 在三个外部面板里找 600825 信号卡 ----
  let found = null;
  for (const tab of ['extref_soft', 'extref_comm', 'extref_rank']) {
    await p.evaluate((t) => { location.hash = '#opinion/' + t; }, tab);
    await sleep(3200);
    const hit = await p.evaluate(() => {
      const cards = [...document.querySelectorAll('.ex-card[data-ex-sid]')];
      const el = cards.find((c) => (c.dataset.exSid || '').includes('600825'));
      return el ? { sid: el.dataset.exSid, n: cards.length, text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80) } : null;
    });
    if (hit) { found = { tab, ...hit }; break; }
  }
  ok(!!found, found ? `找到 600825 信号卡（面板 ${found.tab}，共 ${found.n} 张）` : '未找到 600825 信号卡');

  if (!found) { await b.close(); process.exit(1); }

  // ---- 2) 点开看抽屉 ----
  await p.evaluate((sid) => {
    const el = [...document.querySelectorAll('.ex-card[data-ex-sid]')].find((c) => c.dataset.exSid === sid);
    if (el) el.click();
  }, found.sid);
  await sleep(3000);

  const drawer = await p.evaluate(() => {
    const d = document.querySelector('#drawer');
    if (!d) return { open: false };
    const txt = (d.textContent || '').replace(/\s+/g, ' ');
    // 取「② 事后收益」这一段的原文
    const i = txt.indexOf('② 事后收益');
    const seg = i >= 0 ? txt.slice(i, i + 200) : '';
    const items = [...d.querySelectorAll('.si-grid .si-item')].map((x) => (x.textContent || '').replace(/\s+/g, ' ').trim());
    return {
      open: d.classList.contains('show') || d.offsetParent !== null,
      items, seg,
      honesty: [...d.querySelectorAll('.ex-honest')].map((x) => (x.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 2),
      len: txt.length,
    };
  });

  console.log('\n  [抽屉] ② 事后收益 →', JSON.stringify(drawer.items));
  console.log('  [抽屉] 段落 →', drawer.seg.slice(0, 150));
  console.log('  [抽屉] 口径 →', JSON.stringify(drawer.honesty));

  const t1 = (drawer.items.find((x) => x.startsWith('T+1'))) || '';
  const t5 = (drawer.items.find((x) => x.startsWith('T+5'))) || '';
  const t10 = (drawer.items.find((x) => x.startsWith('T+10'))) || '';

  ok(drawer.open, `信号详情抽屉已打开（${drawer.len} 字）`);
  ok(/%/.test(t1), `T+1 显示真实收益：${t1}`);
  // T+5/T+10：未到期必须是「—」（不写 0），已到期必须是带 % 的真值 —— 只接受这两种。
  const okVal = (s) => /收益—$/.test(s) || /收益[+-]?\d+(\.\d+)?%$/.test(s);
  ok(okVal(t5), `T+5 取值合法（未到期=「—」/ 已到期=%）：${t5}`);
  ok(okVal(t10), `T+10 取值合法（未到期=「—」/ 已到期=%）：${t10}`);
  ok(!/收益\s*0(\.00)?%/.test(t5 + t10), '未到期的窗口没有被写成 0（0 会被误读为零收益）');
  ok(/部分回填/.test(drawer.seg) || /已回填/.test(drawer.seg), '口径文案标注了回填状态（部分回填 / 已回填）');
  ok(!/取不到日 K/.test(drawer.seg), '不再出现「取不到日 K 行情」的 na 文案');
  ok(errs.length === 0, '零控制台报错' + (errs.length ? '：' + errs.join(' | ') : ''));

  await p.screenshot({ path: 'verify_extref_pnl.png' });
  console.log('\n  截图 → verify_extref_pnl.png');
  const bad = results.filter((r) => !r.pass);
  console.log(`\n结果：${results.length - bad.length}/${results.length} 通过`);
  await b.close();
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
