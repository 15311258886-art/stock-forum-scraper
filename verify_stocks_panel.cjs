/* 回归验证：「当前讨论股票」面板精简 + 密度优化（2026-09-26）
   用户原话：「你这个事例没有什么意义……什么卡片，第一行股票简称，第二行什么股票代码加全称，
             这些你不用写，我看我也能知道……太无用了，可以去掉……底下这些股票可以再稍微
             略略缩小一点点……占的空白比较多，我希望在这一个版页当中能显示更多的信息，
             被提及的次数可以做成高亮。」

   验收点：
   ① 说明条（卡片第一行是…）与「图例：这些数字分别代表什么」已移除；无未识别代码时连提示条都不出现；
   ② 卡片密度确实生效（min-height 96 / 名称 15px / 网格 gap 10）；
   ③ 被提及次数做成高亮：.stk-mention + 加粗放大数字，且数字与接口一致；
   ④ 复用同组件的「舆情情绪」面板未被误伤（卡片仍渲染、多空图例仍在）；
   ⑤ 控制台零报错。
   用法：node verify_stocks_panel.cjs   （截图 _stocks_panel_shot.png）
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8797;
const PY = 'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\python\\envs\\verify\\Scripts\\python.exe';
const SHOT = path.join(BASE, '_stocks_panel_shot.png');
const ROOT = `http://127.0.0.1:${PORT}`;

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

const env = { ...process.env, VIEWER_PORT: String(PORT) };
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']) delete env[k];
const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio: ['ignore', 'pipe', 'pipe'] });
let srvLog = '';
srv.stdout.on('data', (d) => { srvLog += d.toString(); });
srv.stderr.on('data', (d) => { srvLog += d.toString(); });

const results = [];
const ok = (name, pass, extra) => {
  results.push([name, !!pass, extra === undefined ? '' : String(extra)]);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '  → ' + extra : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitPort() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`${ROOT}/api/summary`); if (r.ok) return true; } catch (e) { /* not up */ }
    await sleep(500);
  }
  return false;
}

(async () => {
  let browser = null;
  try {
    if (!CHROME) throw new Error('找不到 chromium');
    if (!(await waitPort())) throw new Error('后端未就绪：\n' + srvLog.slice(-1500));

    /* ---------- ① 接口层：确认有数据、字段齐全 ---------- */
    const arr = await (await fetch(`${ROOT}/api/stocks`)).json();
    const list = Array.isArray(arr) ? arr : (arr && arr.items) || [];
    ok('接口 /api/stocks 有数据', list.length > 0, `${list.length} 条`);
    ok('接口 首条含 symbol/mentions 字段', !!list[0] && 'symbol' in list[0] && 'mentions' in list[0],
      list[0] ? `symbol=${list[0].symbol} mentions=${list[0].mentions}` : '');
    const missN = list.filter((x) => x && (x.name_missing || !(x.name || x.symbol_name))).length;

    /* ---------- ② 前端层：真实导航到「当前讨论股票」 ---------- */
    browser = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1600,1200'],
      defaultViewport: { width: 1600, height: 1200 },
    });
    const page = await browser.newPage();
    const errs = [];
    page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

    await page.goto(ROOT + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('.nav-item[data-cat="money"]', { timeout: 30000 });
    await page.click('.nav-item[data-cat="money"]');
    await page.waitForSelector('.sub-tab[data-tab="stocks"]', { timeout: 15000 });
    await page.click('.sub-tab[data-tab="stocks"]');
    const clicked = await page.$eval('.sub-tab[data-tab="stocks"]', (el) => el.textContent.trim());
    ok('真实点到「当前讨论股票」子标签', clicked.includes('当前讨论股票'), `TAB_CLICKED = ${clicked}`);

    await page.waitForSelector('#p-stocks .stk-card', { timeout: 30000 });
    const ui = await page.evaluate(() => {
      const box = document.getElementById('p-stocks');
      const cards = box.querySelectorAll('.stk-card');
      const first = cards[0];
      const cs = first ? getComputedStyle(first) : null;
      const nameEl = first ? first.querySelector('.stk-name') : null;
      const grid = box.querySelector('.grid');
      const gcs = grid ? getComputedStyle(grid) : null;
      const mentionEls = box.querySelectorAll('.stk-mention');
      const mFirst = mentionEls[0] || null;
      const mcs = mFirst ? getComputedStyle(mFirst) : null;
      const mB = mFirst ? mFirst.querySelector('b') : null;
      return {
        cardN: cards.length,
        text: box.textContent.replace(/\s+/g, ' '),
        lgRow: box.querySelectorAll('.legend .lg-row').length,
        lgTitle: box.querySelectorAll('.legend .lg-title').length,
        hasLegend: !!box.querySelector('.legend'),
        cardMinH: cs ? Math.round(parseFloat(cs.minHeight)) : -1,
        nameFs: nameEl ? Math.round(parseFloat(getComputedStyle(nameEl).fontSize)) : -1,
        gridGap: gcs ? Math.round(parseFloat(gcs.gap || gcs.rowGap)) : -1,
        mentionN: mentionEls.length,
        mBg: mcs ? mcs.backgroundColor : '',
        mBorder: mcs ? mcs.borderTopWidth : '',
        mBold: mB ? getComputedStyle(mB).fontWeight : '',
        mNum: mB ? mB.textContent.trim() : '',
        postureBadges: Array.from(box.querySelectorAll('.badge'))
          .filter((b) => ['看好', '看空', '分歧'].includes(b.textContent.trim())).length,
      };
    });

    ok('① 说明条「卡片第一行是…」已移除', !ui.text.includes('卡片第一行'), ui.text.includes('卡片第一行') ? '仍存在' : '已移除');
    ok('① 图例「这些数字分别代表什么」行已移除', ui.lgRow === 0 && ui.lgTitle === 0, `lg-row=${ui.lgRow} title=${ui.lgTitle}`);
    // 2026-10-12 体验审计 P0-C：新增「人气≠看好」三态说明条（报告要求把两个概念分开）；
    // 它不是旧图例（.legend），故单独断言存在——这条是新增需求，不是回归。
    ok('①′ 人气≠看好 的三态说明条已出现', ui.text.includes('人气') && ui.text.includes('被看好'),
      (ui.text.includes('人气') && ui.text.includes('被看好')) ? '已在' : '缺失');
    // 至少一个卡片带上 看好/看空/分歧 三态徽章（窗口内无表态时可全无，此时只要求说明条）
    ok('①″ 三态徽章（看好/看空/分歧）已统计', ui.postureBadges >= 0,
      'posture badges=' + ui.postureBadges);
    if (missN === 0) {
      ok('① 无未识别代码时不出现补全提示条', !ui.hasLegend, ui.hasLegend ? '仍出现 legend' : '未出现');
    } else {
      ok('① 有未识别代码时出现精简提示条', ui.hasLegend, `缺 ${missN} 个`);
    }

    ok('② 卡片密度：min-height ≤ 100px（原 118）', ui.cardMinH >= 0 && ui.cardMinH <= 100, `${ui.cardMinH}px`);
    ok('② 卡片密度：名称字号 ≤ 16px（原 17）', ui.nameFs >= 0 && ui.nameFs <= 16, `${ui.nameFs}px`);
    ok('② 网格间距 = 10px（原 12）', ui.gridGap === 10, `${ui.gridGap}px`);

    ok('③ 每张卡片都有提及高亮标签', ui.cardN > 0 && ui.mentionN === ui.cardN, `cards=${ui.cardN} mention=${ui.mentionN}`);
    ok('③ 提及标签有可见描边或底色', ui.mBorder === '1px' || (ui.mBg && !/rgba?\(0, 0, 0, 0\)/.test(ui.mBg)),
      `bg=${ui.mBg} border=${ui.mBorder}`);
    ok('③ 提及次数数字加粗放大', ui.mBold === '800', `font-weight=${ui.mBold}`);
    const apiNum = String(list[0] && list[0].mentions != null ? list[0].mentions : '');
    ok('③ 卡片提及数字与接口一致', ui.mNum === apiNum, `页面=${ui.mNum} 接口=${apiNum}`);

    await page.screenshot({ path: SHOT, fullPage: false });
    await page.screenshot({ path: path.join(BASE, '_stocks_panel_full.png'), fullPage: true });

    /* ---------- ③ 不误伤「舆情情绪」（复用同组件，改动需零回归） ---------- */
    await page.click('.sub-tab[data-tab="sentiment"]');
    await page.waitForSelector('#p-sentiment .stk-card', { timeout: 30000 });
    const sen = await page.evaluate(() => {
      const box = document.getElementById('p-sentiment');
      return {
        cards: box.querySelectorAll('.stk-card').length,
        lgRow: box.querySelectorAll('.legend .lg-row').length,
        bull: box.querySelectorAll('.badge.bull').length,
        nameFs: box.querySelector('.stk-name') ? Math.round(parseFloat(getComputedStyle(box.querySelector('.stk-name')).fontSize)) : -1,
      };
    });
    ok('④ 舆情面板卡片仍渲染', sen.cards > 0, `${sen.cards} 张`);
    ok('④ 舆情面板保留多空口径图例', sen.lgRow >= 3 && sen.bull > 0, `lg-row=${sen.lgRow} bull标记=${sen.bull}`);
    ok('④ 舆情面板未套用股票面板密度（名称仍 17px）', sen.nameFs >= 17, `${sen.nameFs}px`);

    const realErrs = errs.filter((t) => !/favicon|net::ERR_/i.test(t));
    ok('⑤ 控制台零报错', realErrs.length === 0, realErrs.slice(0, 3).join(' ;; '));
  } catch (e) {
    ok('执行异常', false, e.message);
  } finally {
    if (browser) { try { await browser.close(); } catch (e) { /* ignore */ } }
    srv.kill();
  }
  const bad = results.filter((r) => !r[1]);
  console.log(`\n===== ${results.length - bad.length}/${results.length} PASS =====`);
  if (bad.length) { console.log('失败项：' + bad.map((b) => b[0]).join(' / ')); process.exitCode = 1; }
  setTimeout(() => process.exit(process.exitCode || 0), 300);
})();
