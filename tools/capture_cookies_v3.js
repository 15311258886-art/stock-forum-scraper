/**
 * 登录态 Cookie 采集工具 v3（取代 v2，2026-09-22 晚）
 *
 * v2 暴露的两个问题（王总"窗口里没有二维码"）：
 *   1. **误判二维码**：v2 用「页面上有没有大图」判二维码，结果被淘股吧页面
 *      自带的 App 下载二维码（footer_img_qrcode1/2/3.png、wxad.jpg）误触发
 *      → 提前认定"二维码已出现" → 没走兜底。v3 改为只认**点击后新出现的**
 *      canvas / 弹窗容器 / 带登录语义的图。
 *   2. **点击无效**：v2 用 `el.click()`（JS 合成事件），点击前后 DOM 完全无变化
 *      → 登录弹窗没打开。v3 改用 **真实鼠标事件**（hover + mouse.down/up），
 *      并同时检测「是否开了新标签页」「DOM 签名是否变化」两种成功迹象。
 *
 * v3 判定链路（每一步都留截图 + 写 probe，可回溯）：
 *   进首页 → 探测登录入口(含 href/onclick) → 真实鼠标点击
 *     ├─ 开了新标签页        → 切前台 + 截图 → 成功
 *     ├─ DOM 签名变化        → 截图 → 成功（弹窗出现）
 *     └─ 两者都没有          → 按入口 href 跳转；无 href 则跳 SSO 登录页 → 截图
 *
 * 用法：
 *   node tools/capture_cookies_v3.js https://www.tgb.cn/ 30     交互登录
 *   node tools/capture_cookies_v3.js https://www.tgb.cn/ 30 --no-focus   完全不抢前台
 *   node tools/capture_cookies_v3.js --dump                     仅复用 profile 导出
 *
 * 焦点策略（2026-09-22 二次修正，重要）：
 *   等待登录期间**不再周期性抢前台**。原实现在 3 秒轮询里每次都 bringToFront()，
 *   30 分钟约 600 次把窗口顶到最前，用户正在做的其他事情（含本项目看板）被反复盖住，
 *   表现为「这个网页总是自动弹出窗口」。现在只在前 8 秒提示性抢 2 次，之后靠窗口标题
 *   提醒；用户手动关闭窗口即立刻退出，不再空转到超时。
 *
 * 产出：
 *   tools/cookies.json            —— 登录成功后写入
 *   tools/_s1_home.png / _s2_click.png / _s3_login.png           过程截图
 *   tools/_login_probe.json       —— 探测报告
 *   tools/_capture_state.json     —— 轮询状态
 *
 * 说明：不代填账号密码、不破解任何验证；登录动作全部由使用者本人在窗口中完成。
 */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const EXE = process.env.CHROME_EXE ||
  'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const HERE = __dirname;
const PROFILE = path.resolve(HERE, '..', '.browser-profile');

const STATE = path.join(HERE, '_capture_state.json');
const LATEST = path.join(HERE, '_cookies_latest.json');
const FINAL = path.join(HERE, 'cookies.json');
const PROBE = path.join(HERE, '_login_probe.json');
const S1 = path.join(HERE, '_s1_home.png');
const S2 = path.join(HERE, '_s2_click.png');
const S3 = path.join(HERE, '_s3_login.png');

const args = process.argv.slice(2);
const DUMP_ONLY = args.includes('--dump');
const PROBE_ONLY = args.includes('--probe-only');
const NO_FOCUS = args.includes('--no-focus');   // 完全不抢前台（后台/批量采集时用）
const posArgs = args.filter((a) => !a.startsWith('--'));
const START_URL = posArgs[0] || 'https://www.tgb.cn/';
const MAX_MIN = Number(posArgs[1] || 30);
const DOMAIN_HINT = new URL(START_URL).hostname.split('.').slice(-2).join('.');

const SSO_LOGIN = 'https://sso.tgb.cn/web/login/index';
const GATED_URL = 'https://www.tgb.cn/user/getMoreListAction';
const SSO_HINT = 'sso.tgb.cn/web/login';
const OUT_MARKERS = ['hd_unlogin', '登录/注册'];
// 页面自带的营销/App 二维码，一律不算「登录二维码」
const DECOY = /footer|wxad|downapp|\/ad\/|appdown|_ad\./i;
// 带登录语义的图，才算候选
const LOGIN_IMG = /login|qrcode|qr_|erweima|weixin|wx_?login/i;

const writeJson = (p, o) => fs.writeFileSync(p, JSON.stringify(o, null, 2), 'utf8');
const nowIso = () => new Date().toISOString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function httpLoggedIn(cookies, ua) {
  if (!cookies.length) return null;
  try {
    const cookieHdr = cookies.map((c) => `${c.name}=${c.value}`).join('; ');
    const res = await fetch(GATED_URL, {
      headers: { 'User-Agent': ua, Cookie: cookieHdr, 'Accept-Language': 'zh-CN,zh;q=0.9' },
      redirect: 'follow',
    });
    const html = await res.text();
    if (html.length < 3000) return null;
    if ((res.url || '').includes(SSO_HINT)) return false;
    if (OUT_MARKERS.some((m) => html.includes(m))) return false;
    return true;
  } catch {
    return null;
  }
}

// DOM 签名：用于判断"点击后界面是否真的变了"（弹窗出现）
async function domSig(page) {
  return await page.evaluate(() => {
    const txt = (document.body ? document.body.innerText : '').length;
    const nodes = document.querySelectorAll('*').length;
    let overlay = 0;
    for (const d of document.querySelectorAll('div,section,aside')) {
      const s = getComputedStyle(d);
      if (s.position !== 'fixed' && s.position !== 'absolute') continue;
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
      const r = d.getBoundingClientRect();
      if (r.width >= 260 && r.height >= 200) overlay++;
    }
    return txt + '|' + nodes + '|' + overlay;
  }).catch(() => 'err');
}

// 只认「真正的登录二维码」：canvas、够大的 iframe、带登录语义的图（排除营销图）
async function probeQr(page) {
  return await page.evaluate((decoySrc, loginSrc) => {
    const decoy = new RegExp(decoySrc, 'i');
    const login = new RegExp(loginSrc, 'i');
    const out = { canvas: 0, login_imgs: [], iframes: [], login_entries: [] };
    out.canvas = document.querySelectorAll('canvas').length;
    for (const im of document.querySelectorAll('img')) {
      const src = im.src || '';
      if (decoy.test(src)) continue;
      const w = im.width || 0, h = im.height || 0;
      const r = im.getBoundingClientRect();
      const visible = r.width > 40 && r.height > 40;
      if (visible && (w >= 120 && h >= 120) && (login.test(src) || (w >= 150 && h >= 150))) {
        out.login_imgs.push({ w: Math.round(r.width), h: Math.round(r.height), src: src.slice(0, 140) });
      }
    }
    for (const f of document.querySelectorAll('iframe')) {
      const r = f.getBoundingClientRect();
      if (r.width > 120 && r.height > 120) {
        out.iframes.push({ src: (f.src || '').slice(0, 180), w: Math.round(r.width), h: Math.round(r.height) });
      }
    }
    const norm = (s) => (s || '').replace(/\s+/g, '');
    for (const el of document.querySelectorAll('a,button,span,div,li')) {
      const t = norm(el.textContent);
      if (!/^(登录|登录\/注册|登录注册|立即登录|请登录|会员登录)$/.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 6 || r.height < 6 || r.top < 0 || r.top > 320) continue;
      out.login_entries.push({ text: el.textContent.trim(), tag: el.tagName, y: Math.round(r.y) });
    }
    return out;
  }, DECOY.source, LOGIN_IMG.source).catch((e) => ({ error: String(e.message) }));
}

// 找登录入口，带 href / onclick / 中心坐标（坐标供真实鼠标点击用）
async function findLoginEntry(page) {
  return await page.evaluate(() => {
    const norm = (s) => (s || '').replace(/\s+/g, '');
    const out = [];
    for (const el of document.querySelectorAll('a,button,span,div,li')) {
      const t = norm(el.textContent);
      if (!/^(登录|登录\/注册|登录注册|立即登录|请登录|会员登录)$/.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 6 || r.height < 6 || r.top < 0 || r.top > 320) continue;
      const a = el.tagName === 'A' ? el : el.closest('a');
      out.push({
        text: el.textContent.trim(),
        tag: el.tagName,
        is_link: !!a,
        href: a ? (a.getAttribute('href') || '') : '',
        onclick: (el.getAttribute('onclick') || (a ? (a.getAttribute('onclick') || '') : '')).slice(0, 120),
        html: (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 180),
        cx: Math.round(r.x + r.width / 2),
        cy: Math.round(r.y + r.height / 2),
      });
    }
    // 真链接优先，其次靠右上的（页头登录入口）
    out.sort((a, b) => (b.is_link ? 1 : 0) - (a.is_link ? 1 : 0) || b.cy - a.cy);
    return out;
  }).catch(() => []);
}

async function qrCount(page) {
  const q = await probeQr(page);
  if (!q || q.error) return 0;
  return (q.canvas || 0) + (q.login_imgs ? q.login_imgs.length : 0) + (q.iframes ? q.iframes.length : 0);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: false,
    userDataDir: PROFILE,
    defaultViewport: { width: 1400, height: 950 },
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });

  const opened = await browser.pages();
  const page = opened[0] || (await browser.newPage());
  const closedTabs = [];
  for (let i = 1; i < opened.length; i++) {
    try { await opened[i].close(); closedTabs.push(i); } catch { /* ignore */ }
  }

  const newTabs = [];
  browser.on('targetcreated', (t) => {
    if (t.type() === 'page') newTabs.push(t.url());
  });

  const client = await page.target().createCDPSession();
  await client.send('Network.enable');
  const ua = await browser.userAgent();

  const snapshot = async () => {
    const { cookies } = await client.send('Network.getAllCookies');
    const rel = (cookies || []).filter((c) => (c.domain || '').includes(DOMAIN_HINT));
    writeJson(LATEST, { url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: rel });
    return rel;
  };

  if (DUMP_ONLY) {
    await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    const rel = await snapshot();
    writeJson(FINAL, { url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: rel });
    writeJson(STATE, { status: 'dumped', cookie_count: rel.length, cookie_names: rel.map((c) => c.name), updated_at: nowIso() });
    console.log('=== DUMPED === cookies=' + rel.length + ' -> ' + FINAL);
    await browser.close();
    return;
  }

  const probe = { at: nowIso(), closed_extra_tabs: closedTabs, steps: [] };
  const step = (o) => { probe.steps.push({ t: nowIso(), ...o }); writeJson(PROBE, probe); };

  // ---------- 1) 进首页 ----------
  await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);
  await page.bringToFront();
  await page.screenshot({ path: S1 }).catch(() => {});

  let rel = await snapshot();
  const preState = await httpLoggedIn(rel, ua);
  probe.home = {
    url: page.url(),
    title: await page.title().catch(() => ''),
    cookies: rel.length,
    http_logged_in: preState,
    qr: await qrCount(page),
  };
  step({ at: 'home', url: page.url(), cookies: rel.length, logged_in: preState, qr: probe.home.qr });

  if (preState === true) {
    writeJson(FINAL, { url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: rel });
    probe.result = 'already-logged-in';
    writeJson(PROBE, probe);
    writeJson(STATE, { status: 'login_detected', cookie_count: rel.length, http_logged_in: true, page_url: page.url(), updated_at: nowIso() });
    console.log('=== ALREADY LOGGED IN === cookies=' + rel.length);
    await sleep(3000);
    await browser.close();
    return;
  }

  // ---------- 2) 真实鼠标点击登录入口 ----------
  const entries = await findLoginEntry(page);
  probe.login_entries = entries;
  step({ at: 'entries', found: entries.length, first: entries[0] || null });

  const sigBefore = await domSig(page);
  const pagesBefore = (await browser.pages()).length;
  let clickOutcome = 'none';

  if (entries.length) {
    const hit = entries[0];
    try {
      await page.mouse.move(hit.cx, hit.cy);
      await sleep(320);
      await page.mouse.down();
      await sleep(90);
      await page.mouse.up();
      clickOutcome = 'mouse-clicked';
    } catch (e) {
      clickOutcome = 'mouse-error: ' + e.message;
    }
    await sleep(3800);
  }

  const pagesAfter = await browser.pages();
  const sigAfter = await domSig(page);
  const openedNewTab = pagesAfter.length > pagesBefore || newTabs.length > 0;

  // 若开了新标签 → 切到新页面去截图探测
  let target = page;
  if (openedNewTab) {
    try {
      target = pagesAfter[pagesAfter.length - 1];
      await target.bringToFront();
      await sleep(1200);
    } catch { target = page; }
  }
  await target.screenshot({ path: S2 }).catch(() => {});
  const qrAfterClick = await qrCount(target);

  step({
    at: 'after-click',
    click: clickOutcome,
    opened_new_tab: openedNewTab,
    new_tabs: newTabs,
    dom_changed: sigBefore !== sigAfter,
    sig_before: sigBefore,
    sig_after: sigAfter,
    url: target.url(),
    qr: qrAfterClick,
  });

  const clickWorked = openedNewTab || (sigBefore !== sigAfter) || qrAfterClick > 0;

  // ---------- 3) 兜底：直接进登录页 ----------
  if (!clickWorked) {
    const href = (entries[0] && entries[0].href) || '';
    const dest = /^https?:\/\//i.test(href) ? href : SSO_LOGIN;
    step({ at: 'fallback-start', dest });
    try {
      await page.goto(dest, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } catch (e) {
      step({ at: 'fallback-error', error: e.message });
    }
    await sleep(4000);
    await page.bringToFront();
    await page.screenshot({ path: S3 }).catch(() => {});
    const qrFallback = await qrCount(page);
    step({ at: 'fallback-done', url: page.url(), qr: qrFallback });
  }

  probe.result = clickWorked ? 'login-surface-opened' : 'fallback-used';
  probe.final_qr = await qrCount(clickWorked ? (openedNewTab ? (await browser.pages()).slice(-1)[0] : page) : page);
  writeJson(PROBE, probe);

  if (PROBE_ONLY) {
    console.log('=== PROBE ONLY === result=' + probe.result + ' qr=' + probe.final_qr);
    await sleep(2000);
    await browser.close();
    return;
  }

  // ---------- 4) 轮询等待登录 ----------
  const t0 = Date.now();
  const baseline = (await snapshot()).map((c) => c.name);
  let detected = false;
  let raises = 0, titleMarked = false, windowClosed = false;

  while (Date.now() - t0 < MAX_MIN * 60000) {
    // 用户关窗即退出：不再空转到超时（否则进程看似还在动，还会持续写状态文件）
    if (!browser.connected || page.isClosed()) { windowClosed = true; break; }

    await sleep(3000);

    // 抢前台只在前 8 秒做、最多 2 次；之后一律不抢，改窗口标题提示（不打扰用户当前操作）
    const elapsed = Date.now() - t0;
    if (!detected && !NO_FOCUS && raises < 2 && elapsed < 8000) {
      raises++;
      try { await page.bringToFront(); } catch { /* ignore */ }
    } else if (!detected && !NO_FOCUS && !titleMarked && raises >= 2) {
      titleMarked = true;
      try { await page.evaluate(() => { document.title = '【等待扫码登录】' + document.title; }); } catch { /* ignore */ }
    }

    let cur = [];
    try { cur = await snapshot(); } catch { /* 跳转中 */ }
    const names = cur.map((c) => c.name);
    const news = names.filter((n) => !baseline.includes(n));
    const httpState = await httpLoggedIn(cur, ua);
    const guess = httpState === true;

    writeJson(STATE, {
      status: guess ? 'login_detected' : 'waiting',
      cookie_count: cur.length,
      new_cookie_names: news,
      cookie_names: names,
      baseline_names: baseline,
      http_logged_in: httpState,
      page_url: page.url(),
      login_stage: probe.result,
      updated_at: nowIso(),
    });

    if (guess) {
      writeJson(FINAL, { url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: cur });
      console.log('=== LOGIN DETECTED === cookies=' + cur.length + ' -> ' + FINAL);
      detected = true;
      break;
    }
  }

  if (windowClosed) {
    writeJson(STATE, { status: 'window_closed', login_stage: probe.result, updated_at: nowIso() });
    console.log('=== WINDOW CLOSED === 用户已关闭窗口，提前退出（不再空转）。');
  } else if (!detected) {
    const prev = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, 'utf8')) : {};
    writeJson(STATE, { ...prev, status: 'timeout', updated_at: nowIso() });
    console.log('=== TIMEOUT === 未检测到登录。');
  }

  if (windowClosed) {
    try { await browser.close(); } catch { /* 窗口已被用户关闭 */ }
    return;
  }
  await sleep(detected ? 6000 : 60000);
  try { await browser.close(); } catch { /* ignore */ }
})();
