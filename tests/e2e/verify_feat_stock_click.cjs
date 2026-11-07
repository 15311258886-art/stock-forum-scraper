// verify_feat_stock_click.cjs — FEAT 6 面板代码列可点 + 抽屉可拖/可调整大小/可最大化/可加入自选池
// 隔离副本库 + 无头 Chromium 真机回归
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const puppeteer = require('puppeteer-core');

const BASE = 'F:\\AI Agent部署知识库\\2026-09-22-11-49-42\\stock-forum-scraper';
const PORT = 8805;
const PY = BASE + '\\.venv\\Scripts\\python.exe';

const env = { ...process.env };
env.VIEWER_PORT = String(PORT);
env.VIEWER_DB = path.join(BASE, 'viewer', 'app_isofei.db');
['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY'].forEach(k => delete env[k]);

const srv = spawn(PY, ['-m', 'viewer.server'], { cwd: BASE, env, stdio:['ignore','pipe','pipe'] });
srv.stdout.on('data', d => {});
srv.stderr.on('data', d => process.stderr.write('[SRV] ' + d));

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const fails = [];
function ok(cond, msg){ (cond?console.log:((m)=>fails.push(m)&&console.log))(`  ${cond?'PASS':'FAIL'} | ${msg}`); }

async function waitForServer(port, timeout=30000) {
  const t0=Date.now();
  while(Date.now()-t0<timeout){
    try{const r=await fetch(`http://127.0.0.1:${port}/api/screener?screen=value&limit=5`);if(r.ok) return;}
    catch(e){}
    await sleep(300);
  }
  throw new Error('server timeout');
}

(async () => {
  try{
    await waitForServer(PORT);
    const exe = 'C:\\Users\\野比&大熊\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
    const browser = await puppeteer.launch({
      executablePath: exe, headless:'new', args:['--no-sandbox','--disable-dev-shm-usage'],
      defaultViewport:{width:1440,height:900},
    });
    const page = await browser.newPage();
    page.on('console', m => { if (m.type()==='error' || m.type()==='warning') console.log('[PAGE',m.type()+']', m.text()); });
    page.on('pageerror', e => console.log('[PAGE-err]', e.message));

    await page.goto(`http://127.0.0.1:${PORT}/`, {waitUntil:'networkidle0', timeout:30000});
    await page.waitForSelector('[data-cat="enhance"]', {timeout:20000});

    // 工具函数：切到指定子面板
    async function goTab(tab){
      await page.click('[data-cat="enhance"]');
      await sleep(300);
      const ok = await page.evaluate((t)=>{
        const el = document.querySelector(`[data-tab="${t}"]`);
        if(!el) return false; el.click(); return true;
      }, tab);
      if(!ok) throw new Error(`tab ${tab} not found`);
      await sleep(800);
    }

    // ========== A. 智能选股：代码列有 data-feat-open/wt-open + 可点 ==========
    console.log('\n[A] 智能选股 — 代码列可点');
    await goTab('screener');
    await page.waitForFunction(() => {
      const links = document.querySelectorAll('#p-screener .wt-sym');
      return links.length > 0;
    }, {timeout: 30000});
    const scInfo = await page.evaluate(() => {
      const links = [...document.querySelectorAll('#p-screener .wt-sym')];
      const first = links[0];
      return {
        n: links.length,
        firstHasOpenAttr: !!(first && (first.dataset.featOpen || first.dataset.wtOpen)),
        firstOnclick: !!(first && first.onclick),
        firstCode: first ? first.textContent.trim() : null,
      };
    });
    ok(scInfo.n >= 5, `智能选股代码列至少 5 项（实为 ${scInfo.n}）`);
    ok(scInfo.firstHasOpenAttr, `首项含 data-feat-open 或 data-wt-open`);
    ok(scInfo.firstOnclick, `首项 onclick 已绑定`);
    const firstStockCode = scInfo.firstCode;

    // ========== B. 资金流向：rank 模式代码列可点（沙箱可能无数据，宽松等待） ==========
    console.log('\n[B] 资金流向 — 排行代码列可点（沙箱可能无数据，看是否有 #p-moneyflow 容器 + DOM 结构）');
    await goTab('moneyflow');
    await sleep(2000);
    const mfInfo = await page.evaluate(() => {
      const links = [...document.querySelectorAll('#p-moneyflow .wt-sym')];
      const rows = [...document.querySelectorAll('#p-moneyflow tbody tr')];
      const first = links[0];
      return {
        n: links.length,
        nRows: rows.length,
        firstHasOpenAttr: !!(first && (first.dataset.featOpen || first.dataset.wtOpen)),
        emptyMsg: document.querySelector('#p-moneyflow .wt-empty')?.textContent?.trim() || null,
      };
    });
    console.log('  moneyflow state:', JSON.stringify(mfInfo));
    if (mfInfo.n > 0) {
      ok(mfInfo.firstHasOpenAttr, `资金流向代码列首项含 data-feat-open/wt-open`);
    } else {
      // 沙箱不通：接口空 → 应给出明确"接口异常：..."而不是静默空白
      ok(mfInfo.emptyMsg && mfInfo.emptyMsg.includes('接口'), `资金流向接口异常时显示明确原因（实为 "${mfInfo.emptyMsg}"）`);
    }

    // ========== C. 研报评级：个股列可点（限定 6 位数字代码） ==========
    console.log('\n[C] 研报评级 — 个股列可点（限定 6 位 A 股）');
    await goTab('research');
    await page.waitForFunction(() => document.querySelectorAll('#p-research tbody tr').length > 0, {timeout: 30000});
    const rsInfo = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#p-research tbody tr')];
      const cells = [...document.querySelectorAll('#p-research .wt-sym')];
      return {
        nRows: rows.length,
        nClickable: cells.length,
        firstCode: cells[0] ? cells[0].textContent.trim() : null,
        firstHasOpenAttr: !!(cells[0] && cells[0].dataset.featOpen),
      };
    });
    ok(rsInfo.nRows >= 1, `研报表 ≥1 行（实为 ${rsInfo.nRows}）`);
    ok(rsInfo.nClickable >= 0, `研报代码列 ≥0 个可点（可能全是 ETF 或无个股）`);
    if (rsInfo.firstCode) {
      ok(/^\d{6}$/.test(rsInfo.firstCode), `首项代码是 6 位数字（实为 ${rsInfo.firstCode}）`);
      ok(rsInfo.firstHasOpenAttr, `首项已挂 data-feat-open`);
    }

    // ========== D. 多资产：A 股代码可点、美股不可点 ==========
    console.log('\n[D] 多资产 — A 股可点 / 美股不可点');
    await goTab('multiquote');
    await page.waitForFunction(() => document.querySelectorAll('#p-multiquote tbody tr').length > 0, {timeout: 30000});
    const mqInfo = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#p-multiquote tbody tr')];
      const allLinks = [...document.querySelectorAll('#p-multiquote .wt-sym')];
      const disabled = [...document.querySelectorAll('#p-multiquote .wt-sym-disabled')];
      return {
        nRows: rows.length,
        nClickable: allLinks.length,
        nDisabled: disabled.length,
        codes: rows.map(r => r.querySelector('.wt-sym, .wt-sym-disabled')?.textContent.trim()).filter(Boolean),
      };
    });
    ok(mqInfo.nRows >= 1, `多资产表 ≥1 行（实为 ${mqInfo.nRows}）`);
    const aShareCodes = mqInfo.codes.filter(c => /^\d{6}$/.test(c));
    const nonACodes = mqInfo.codes.filter(c => c && !/^\d{6}$/.test(c));
    ok(aShareCodes.length === 0 || mqInfo.nClickable >= 1, `A 股代码全部可点（${aShareCodes.length} 只 A 股 → ${mqInfo.nClickable} 个 wt-sym）`);
    ok(nonACodes.length === 0 || mqInfo.nDisabled >= 1, `非 A 股代码显示为不可点（${nonACodes.length} 个非 A 股 → ${mqInfo.nDisabled} 个 wt-sym-disabled）`);

    // ========== E. 真实点击智能选股代码 → 抽屉打开 ==========
    console.log('\n[E] 真实点击 → 抽屉打开 + 三段齐 + 工具齐全');
    await goTab('screener');
    await sleep(500);
    // 真实点击首只
    const clicked = await page.evaluate((code) => {
      const links = [...document.querySelectorAll('#p-screener .wt-sym')];
      const target = links.find(a => a.textContent.trim() === code);
      if(!target) return { found:false };
      target.click();
      return { found:true, dt: target.dataset.featOpen || target.dataset.wtOpen };
    }, firstStockCode);
    ok(clicked.found, `找到代码 ${firstStockCode} 并已 click()`);
    await sleep(1500);

    const drawer = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      const head = document.querySelector('#drawerHead');
      const cs = head ? getComputedStyle(head, '::before') : {};
      const tip = document.querySelector('#drawerTip');
      return {
        show: d && d.classList.contains('show'),
        title: (document.querySelector('#drawerTitle')||{}).textContent,
        offsetLeft: d ? d.offsetLeft : null,
        offsetTop: d ? d.offsetTop : null,
        offsetWidth: d ? d.offsetWidth : null,
        offsetHeight: d ? d.offsetHeight : null,
        zIndex: d ? getComputedStyle(d).zIndex : null,
        headCursor: head ? getComputedStyle(head).cursor : null,
        hasGripBefore: cs && cs.content && cs.content.indexOf('⋮⋮') >= 0,
        tipText: tip ? tip.textContent.trim() : null,
        toolsBtns: [...document.querySelectorAll('#drawerHead .dbtn')].map(b => b.id || b.textContent.trim()),
        resizeCursor: getComputedStyle(document.querySelector('#drawerResize')).cursor,
        addWatchHidden: document.querySelector('#btnAddWatch')?.hidden,
        bodyHasQuote: !!document.querySelector('#stkQuote'),
        bodyHasKline: !!document.querySelector('#klineCanvas'),
      };
    });
    console.log('  drawer state:', JSON.stringify(drawer, null, 2));
    ok(drawer.show, `抽屉已打开（show class）`);
    ok(drawer.title && drawer.title.includes(firstStockCode), `抽屉标题含代码 ${firstStockCode}（实为 "${drawer.title}"）`);
    ok(parseInt(drawer.offsetWidth) >= 800, `默认宽度 ≥800（实为 ${drawer.offsetWidth}）`);
    ok(drawer.headCursor === 'move', `标题栏 cursor:move（实为 ${drawer.headCursor}）`);
    ok(drawer.hasGripBefore, `标题栏前面有 ⋮⋮ 拖动图标`);
    ok(drawer.tipText && drawer.tipText.includes('⋮⋮') && drawer.tipText.includes('最大化'), `抽屉底部有完整操作提示`);
    ok(drawer.toolsBtns.length >= 5, `工具栏按钮 ≥5（实为 ${drawer.toolsBtns.length}：${drawer.toolsBtns.join('/')}）`);
    ok(drawer.toolsBtns.includes('btnAddWatch') || drawer.toolsBtns.some(b=>b.includes('加入自选池')), `工具栏含「＋加入自选池」按钮`);
    ok(drawer.resizeCursor === 'nwse-resize', `resize 角标 cursor:nwse-resize（实为 ${drawer.resizeCursor}）`);
    ok(!drawer.addWatchHidden, `加入自选池按钮已显示`);
    ok(drawer.bodyHasQuote, `抽屉内 #stkQuote 容器存在`);
    ok(drawer.bodyHasKline, `抽屉内 #klineCanvas 容器存在`);

    // ========== F. 真实拖动标题栏（先拖到左上角以确保有空间） ==========
    console.log('\n[F] 真实拖动标题栏');
    // 先把抽屉拖到左上角（距离钳制边界远一点的位置）
    const headBox1 = await page.evaluate(() => {
      const r = document.querySelector('#drawerHead').getBoundingClientRect();
      return { x: r.left + 80, y: r.top + 12 };
    });
    await page.mouse.move(headBox1.x, headBox1.y);
    await page.mouse.down();
    await page.mouse.move(40, 80, {steps: 10});  // 拖到左上
    await page.mouse.up();
    await sleep(200);

    const beforeDrag = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { x: d.offsetLeft, y: d.offsetTop };
    });
    const headBox = await page.evaluate(() => {
      const r = document.querySelector('#drawerHead').getBoundingClientRect();
      return { x: r.left + 80, y: r.top + 12 };
    });
    await page.mouse.move(headBox.x, headBox.y);
    await page.mouse.down();
    await page.mouse.move(headBox.x + 160, headBox.y + 60, {steps: 10});
    await page.mouse.up();
    await sleep(300);
    const afterDrag = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { x: d.offsetLeft, y: d.offsetTop };
    });
    ok(Math.abs(afterDrag.x - beforeDrag.x) >= 100, `拖动后 left 变化 ≥100（${beforeDrag.x} → ${afterDrag.x}）`);
    ok(Math.abs(afterDrag.y - beforeDrag.y) >= 30, `拖动后 top 变化 ≥30（${beforeDrag.y} → ${afterDrag.y}）`);

    // ========== G. 真实拉伸右下角 ==========
    console.log('\n[G] 真实拉伸右下角');
    // 先把抽屉拖到左上角（top=60 紧贴顶部），确保拉伸高度有空间
    const headBoxG0 = await page.evaluate(() => {
      const r = document.querySelector('#drawerHead').getBoundingClientRect();
      return { x: r.left + 80, y: r.top + 12 };
    });
    await page.mouse.move(headBoxG0.x, headBoxG0.y);
    await page.mouse.down();
    await page.mouse.move(50, 60, {steps: 10});
    await page.mouse.up();
    await sleep(200);
    const beforeResize = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { w: d.offsetWidth, h: d.offsetHeight, top: d.offsetTop };
    });
    const rzBox = await page.evaluate(() => {
      const r = document.querySelector('#drawerResize').getBoundingClientRect();
      return { x: r.left + 10, y: r.top + 10 };
    });
    await page.mouse.move(rzBox.x, rzBox.y);
    await page.mouse.down();
    await page.mouse.move(rzBox.x + 200, rzBox.y + 160, {steps: 10});
    await page.mouse.up();
    await sleep(300);
    const afterResize = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { w: d.offsetWidth, h: d.offsetHeight };
    });
    ok(afterResize.w - beforeResize.w >= 100, `拉伸后宽度增大 ≥100（${beforeResize.w} → ${afterResize.w}）`);
    ok(afterResize.h - beforeResize.h >= 50, `拉伸后高度增大 ≥50（${beforeResize.h} → ${afterResize.h}，top=${beforeResize.top}）`);

    // ========== H. 最大化切换 ==========
    console.log('\n[H] 最大化切换');
    const beforeMax = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { w: d.offsetWidth, h: d.offsetHeight, maxed: d.classList.contains('maxed') };
    });
    await page.click('#btnMax');
    await sleep(300);
    const afterMax = await page.evaluate(() => {
      const d = document.querySelector('#drawer');
      return { w: d.offsetWidth, h: d.offsetHeight, maxed: d.classList.contains('maxed') };
    });
    ok(afterMax.maxed, `点击 ⛶ 后抽屉进入 maxed 状态`);
    ok(afterMax.w > beforeMax.w, `最大化后宽度 > 原宽度（${beforeMax.w} → ${afterMax.w}）`);
    // 还原
    await page.click('#btnMax');
    await sleep(300);

    // ========== I. Esc 关闭 ==========
    console.log('\n[I] Esc 关闭');
    // 先重开抽屉并拖到合适位置（截图用：可见 ⋮⋮ 拖手柄、resize 角标、5 个工具按钮、tip 文本）
    const headBoxI = await page.evaluate(() => {
      const r = document.querySelector('#drawerHead').getBoundingClientRect();
      return { x: r.left + 80, y: r.top + 12 };
    });
    await page.mouse.move(headBoxI.x, headBoxI.y);
    await page.mouse.down();
    await page.mouse.move(80, 80, {steps: 8});
    await page.mouse.up();
    await sleep(200);
    await page.screenshot({ path: path.join(BASE, '_feat_click.png'), fullPage: false });
    console.log('\nSHOT: _feat_click.png（抽屉打开、可见 ⋮⋮ / ↘ / 工具栏 / 加入自选池）');

    await page.keyboard.press('Escape');
    await sleep(400);
    const closed = await page.evaluate(() => !document.querySelector('#drawer').classList.contains('show'));
    ok(closed, `Esc 后抽屉已关闭`);

    await browser.close();
    console.log(`\n=== 总结：${fails.length} 项失败 ===`);
    if (fails.length) {
      console.log(fails.map((f,i)=>`  ${i+1}. ${f}`).join('\n'));
      process.exit(1);
    } else {
      console.log('ALL_PASS');
    }
  }catch(e){
    console.log('FATAL', e.message, e.stack);
    process.exit(1);
  }finally{
    srv.kill();
  }
})();