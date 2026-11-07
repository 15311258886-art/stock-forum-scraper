/* verify_addwatch_groups.cjs — 「加入自选池」分组勾选的排版回归
 *
 * 背景（2026-10-15 进哥反馈）：分组勾选框、组名、只数竖着摞三行，多选时
 * 分不清勾的是哪一个。根因＝.wt-checks/.wt-check 这套类零 CSS 定义，
 * 被祖先 `.wt-form label{display:flex;flex-direction:column}` 接管。
 *
 * 断言（几何级，不看类名是否存在）：
 *   A 容器 .wt-checks 是横向弹性盒
 *   B 分组项 ≥2，且「不同 top 数 < 项数」= 确实有两个以上并排（左右排列）
 *   C 每一项内：勾选框与组名在同一行（垂直中心差 ≤6px）且勾选框在组名左侧
 *   D 勾选框没被 .wt-form input 的 padding 撑成大块（高 ≤20px）
 *   E 点胶囊任意位置（非 checkbox 本体）能勾上，且整颗胶囊高亮变化
 *   F 控制台零报错
 *
 * 用法：node verify_addwatch_groups.cjs   （需 8765 已就绪；只读，不写库）
 */
module.paths.push('C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules');
module.paths.push(require('path').join(__dirname, 'node_modules'));
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const PORT = Number(process.env.PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}`;
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const SHOT = require('path').join(__dirname, '_addwatch_groups.png');

const fails = [];
const ok = (c, m) => { console.log((c ? '  PASS ' : '  FAIL ') + m); if (!c) fails.push(m); };

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

  console.log(`\n[1] 打开 ${BASE}/ 并等 app.js 就绪`);
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.showTab === 'function', { timeout: 30000 });

  console.log('[2] 载入自选池数据（取分组）');
  await page.evaluate(() => { showTab('watch'); });
  await page.evaluate(() => loadWatch());
  await page.waitForFunction(() => {
    try { return (WATCH.data && WATCH.data.groups && WATCH.data.groups.length) > 0; }
    catch (e) { return false; }
  }, { timeout: 30000 });
  const grpN = await page.evaluate(() => WATCH.data.groups.length);
  console.log(`    库内分组数 = ${grpN}`);

  console.log('[3] 打开「＋ 加入自选池」弹窗');
  await page.evaluate(() => openWatchAddModal(null));
  await page.waitForSelector('.wt-checks .wt-check', { timeout: 15000 });

  console.log('\n[4] 几何测量');
  const d = await page.evaluate(() => {
    const cont = document.querySelector('.wt-checks');
    const items = [...cont.querySelectorAll('.wt-check')];
    const rows = items.map((el) => {
      const r = el.getBoundingClientRect();
      const cb = el.querySelector('input[type=checkbox]').getBoundingClientRect();
      const nm = el.querySelector('.wt-check-nm').getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        name: el.querySelector('.wt-check-nm').textContent.trim(),
        n: el.querySelector('.wt-check-n').textContent.trim(),
        top: Math.round(r.top), left: Math.round(r.left),
        h: Math.round(r.height), w: Math.round(r.width),
        cbTop: Math.round(cb.top), cbLeft: Math.round(cb.left),
        cbH: Number(cb.height.toFixed(1)), cbW: Number(cb.width.toFixed(1)),
        nmTop: Math.round(nm.top), nmLeft: Math.round(nm.left),
        nmH: Number(nm.height.toFixed(1)), nmCenterY: Number((nm.top + nm.height / 2).toFixed(1)),
        cbCenterY: Number((cb.top + cb.height / 2).toFixed(1)),
        display: cs.display, dir: cs.flexDirection,
        radius: cs.borderRadius,
      };
    });
    return {
      contDisplay: getComputedStyle(cont).display,
      contWrap: getComputedStyle(cont).flexWrap,
      rows,
      tops: [...new Set(rows.map((r) => r.top))],
    };
  });

  console.log(`    容器 display=${d.contDisplay} flex-wrap=${d.contWrap}`);
  d.rows.forEach((r) => {
    console.log(`    · ${r.name.padEnd(6)} ${r.n.padEnd(5)} top=${r.top} left=${r.left} ` +
      `| cb(left=${r.cbLeft},${r.cbW}x${r.cbH}) nm(left=${r.nmLeft}) | ${r.display}/${r.dir} r=${r.radius}`);
  });

  ok(d.contDisplay === 'flex', 'A 容器 .wt-checks 为 flex（横向排列的载体）');
  ok(d.contWrap === 'wrap', 'A2 容器允许 flex-wrap（多组自动换行，不会溢出弹窗）');
  ok(d.rows.length >= 2, `B 分组项 ≥2（实测 ${d.rows.length}）`);
  ok(d.tops.length < d.rows.length,
    `B2 有 ≥2 项并排同一行（distinct top=${d.tops.length} < 项数 ${d.rows.length}）＝左右排列`);

  const sameRow = d.rows.every((r) => Math.abs(r.cbCenterY - r.nmCenterY) <= 6);
  ok(sameRow, 'C 每项内勾选框与组名垂直中心对齐（同一行，不再上下分离）：' +
    d.rows.map((r) => `Δ${Math.abs(r.cbCenterY - r.nmCenterY).toFixed(1)}`).join('/'));
  const cbLeftOfName = d.rows.every((r) => r.cbLeft < r.nmLeft);
  ok(cbLeftOfName, 'C2 每项内勾选框在组名左侧（勾哪个一眼可辨）');
  const cbSize = d.rows.every((r) => r.cbH > 0 && r.cbH <= 20 && r.cbW <= 20);
  ok(cbSize, `D 勾选框保持 15px 原生尺寸（未被 padding 撑成大块）：${d.rows.map((r) => r.cbH).join('/')}`);
  const pill = d.rows.every((r) => parseFloat(r.radius) >= 12 && r.h <= 40);
  ok(pill, 'D2 每个分组是圆角胶囊且高度紧凑（≤40px）');

  console.log('\n[5] 交互：点胶囊非 checkbox 区域 → 勾上 + 整颗高亮');
  const before = await page.evaluate(() => {
    const el = document.querySelector('.wt-checks .wt-check');
    const cs = getComputedStyle(el);
    return { checked: el.querySelector('input').checked, bg: cs.backgroundColor, bc: cs.borderTopColor };
  });
  const pt = await page.evaluate(() => {
    const el = document.querySelector('.wt-checks .wt-check');
    const nm = el.querySelector('.wt-check-nm').getBoundingClientRect();
    return { x: nm.left + nm.width / 2, y: nm.top + nm.height / 2 };  // 点组名文字上，别点 checkbox 本体
  });
  await page.mouse.click(pt.x, pt.y);   // 真实坐标点击，走 hit test
  // ⚠️ 胶囊有 .15s 颜色过渡：立刻读 getComputedStyle 拿到的是插值中的瞬时色（曾误判成"高亮没生效"）。
  // 必须等过渡结束后再测终态。
  await new Promise((r) => setTimeout(r, 400));
  const after = await page.evaluate(() => {
    const el = document.querySelector('.wt-checks .wt-check');
    const cs = getComputedStyle(el);
    return {
      checked: el.querySelector('input').checked,
      bg: cs.backgroundColor, bc: cs.borderTopColor, fg: cs.color, fw: cs.fontWeight,
    };
  });
  console.log(`    checked: bg=${after.bg} bd=${after.bc} fg=${after.fg} fw=${after.fw}`);
  ok(after.checked !== before.checked, 'E 点组名即可切换勾选（label 包裹生效，不必精准点小方块）');
  const ACC = 'rgb(14, 116, 144)';                       // --accent #0E7490
  ok(after.bc === ACC && after.fg === ACC && after.fw === '600',
    `E2 勾选后整颗胶囊高亮：描边与文字=深青 accent、字重 600（实测 bd=${after.bc} fg=${after.fg} fw=${after.fw}）`);
  const alpha = /\/\s*([\d.]+)\s*\)/.exec(after.bg);
  ok(after.bg !== before.bg && (!alpha || Number(alpha[1]) > 0.05),
    `E3 勾选后有强调底色（非纯白）：${before.bg} → ${after.bg}`);

  console.log('\n[6] 多选：再勾一组，确认互不串号');
  const multi = await page.evaluate(() => {
    const els = [...document.querySelectorAll('.wt-checks .wt-check')];
    els[1].querySelector('input').click();
    return els.map((el) => ({ name: el.querySelector('.wt-check-nm').textContent.trim(), on: el.querySelector('input').checked }));
  });
  console.log('    ' + multi.map((m) => `${m.name}:${m.on ? '✓' : '·'}`).join('  '));
  const onCount = multi.filter((m) => m.on).length;
  const onNames = multi.filter((m) => m.on).map((m) => m.name);
  ok(onCount === 2, `E3 多选生效且各勾各的（勾中 ${onCount} 组：${onNames.join('、')}）`);

  try { await page.screenshot({ path: SHOT }); console.log(`\n    截图 → ${SHOT}`); } catch (e) { console.log('    截图失败：' + e.message); }

  ok(errs.length === 0, `F 控制台零报错${errs.length ? '：' + errs.slice(0, 3).join(' | ') : ''}`);

  await browser.close();
  console.log('\n' + (fails.length ? `ADDWATCH_GROUPS FAIL (${fails.length})\n` + fails.map((f) => ' - ' + f).join('\n') : 'ADDWATCH_GROUPS_PASS'));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('脚本异常：', e); process.exit(2); });
