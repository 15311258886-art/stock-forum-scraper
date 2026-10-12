/* 回归验证：导航合并（2026-09-26）
   需求：①「知识获取」+「跟我学习」两个一级分类合并为一个（新名「学习中心」）；
        ② 横条里撤下 数据源 / 原始存档 / 抓取与登录记录 三项（后端照旧存储）；
        ③ 横条第一项是「基础常识」。

   接口层：/api/kb/list 必须透出 order（value_basics=1 排前、zdx_stock=2），group 全为 learn。
   前端层：左栏 4 个大类；进入「学习中心」后横条 = 基础常识 / 中短线炒股 / 策略回测 / 学习轨道管理；
          三项管道信息不再出现；点「基础常识」正文能出内容；控制台零报错。

   用法：node verify_navmerge2.cjs   （截图 _navmerge2_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8794;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_navmerge2_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;

// 2026-09-30 修正：对齐 2026-09-29 信息架构重构后的 7 大类；learn 横条 = 动态轨道（首项基础常识）
//                     + 固定三项（策略回测/巴菲特选股/学习轨道管理）置尾。
const EXPECT_CATS = ['今日总览', '钱往哪走', '消息与外围', '别人怎么做', '别人怎么看', '我的票', '行业龙头', '复盘成长'];
const EXPECT_LEARN_TAIL = ['策略回测', '巴菲特选股', '学习轨道管理'];
const HIDDEN_TABS = ['数据源', '原始存档', '抓取与登录记录'];

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
console.log('CHROME =', CHROME);

const env = { ...process.env, VIEWER_PORT: String(PORT) };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];

const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', d => srvLog += d.toString());
srv.stderr.on('data', d => srvLog += d.toString());
const sleep = ms => new Promise(r => setTimeout(r, ms));

const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) fails.push(msg); };

(async () => {
  try {
    // 等服务起来
    for (let i = 0; i < 60; i++) {
      try { const r = await fetch(ROOT + '/api/summary'); if (r.status === 200) break; } catch (e) { /* retry */ }
      await sleep(500);
    }
    console.log('\n=== 1. 接口层 /api/kb/list ===');
    const kl = await (await fetch(ROOT + '/api/kb/list')).json();
    const docs = kl.docs || [];
    console.log('  docs =', docs.map(d => `${d.doc_key}(group=${d.group},order=${d.order})`).join(' '));
    ok(docs.every(d => typeof d.order === 'number'), '每份文档都带数值型 order 字段');
    ok(docs.every(d => d.group === 'learn'), '所有轨道 group 统一为 learn（无 acquire 残留）');
    const vb = docs.find(d => d.doc_key === 'value_basics');
    const zx = docs.find(d => d.doc_key === 'zdx_stock');
    ok(!!vb && vb.order === 1, '基础常识 order=1');
    ok(!!zx && zx.order === 2, '中短线炒股 order=2');
    ok(docs.length >= 2 && [...docs].sort((a, b) => a.order - b.order)[0].doc_key === 'value_basics',
      '按 order 升序排序后基础常识排第一');

    console.log('\n=== 2. 前端层（无头 Chromium） ===');
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

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#sidenav .nav-item', { timeout: 20000 });
    await sleep(1500);   // 等 kb/list 回来并重绘横条

    const cats = await page.$$eval('#sidenav .nav-item .ni-txt', els => els.map(e => e.textContent.trim()));
    console.log('  左栏大类 =', JSON.stringify(cats));
    ok(JSON.stringify(cats) === JSON.stringify(EXPECT_CATS), '左栏 = ' + EXPECT_CATS.join(' / ') + '（含行业龙头的当前 8 大类）');
    ok(!cats.includes('知识获取') && !cats.includes('跟我学习'), '旧的两个分类名已消失');

    await page.click('#sidenav .nav-item[data-cat="learn"]');
    await sleep(2500);
    const tabs = await page.$$eval('#subnav .sub-tab', els => els.map(e => e.textContent.trim()));
    console.log('  横条子板块 =', JSON.stringify(tabs));
    ok(EXPECT_LEARN_TAIL.every(t => tabs.includes(t)), '横条包含固定三项（策略回测/巴菲特选股/学习轨道管理）均存在（实际=' + JSON.stringify(tabs) + '）');
    ok(tabs[0] === '基础常识', '第一项是「基础常识」');
    ok(!tabs.some(t => HIDDEN_TABS.includes(t)), '数据源/原始存档/抓取与登录记录 已从横条撤下');
    ok(await page.$('#kbStateChip') !== null, '横条右端仍保留「知识库状态 chip」上下文');

    // 点第一项，确认正文能出内容（不是空壳）
    const first = await page.$('#subnav .sub-tab');
    await first.click();
    await sleep(2500);
    const active = await page.$eval('.panel.active', el => el.id);
    const inner = await page.$eval('.panel.active', el => el.innerText.slice(0, 80).replace(/\s+/g, ' '));
    console.log('  当前面板 =', active, '| 正文首段 =', inner);
    ok(active === 'p-learn_value_basics', '「基础常识」面板被激活（p-learn_value_basics）');
    ok(inner.length > 20, '「基础常识」正文有实际内容（非空白/加载中）');

    // 隐藏的三项面板容器应仍在 DOM（保留复原能力）
    const stillThere = await page.evaluate(() => ['p-sources', 'p-archive', 'p-fetchlog'].every(id => !!document.getElementById(id)));
    ok(stillThere, '三个管道面板容器仍保留在 DOM（可随时复原，未删代码）');

    await page.screenshot({ path: SHOT, fullPage: false });
    console.log('  截图 =', SHOT);

    console.log('\n=== 3. 控制台 / 网络 ===');
    const realErrs = errs.filter(e => !/favicon/.test(e));
    console.log('  报错 =', realErrs.length ? JSON.stringify(realErrs.slice(0, 8)) : '（无）');
    ok(realErrs.length === 0, '控制台零报错、零 4xx/5xx');

    await browser.close();
  } catch (e) {
    fails.push('异常：' + (e && e.message));
    console.log('  EXCEPTION  ' + (e && e.stack || e));
    console.log('--- server log tail ---\n' + srvLog.slice(-2000));
  } finally {
    try { srv.kill(); } catch (e) { /* ignore */ }
  }
  console.log('\n================ 结论 ================');
  if (fails.length) { console.log(`FAIL（${fails.length} 项）`); fails.forEach(f => console.log('  - ' + f)); process.exit(1); }
  console.log('全部 PASS');
  process.exit(0);
})();
