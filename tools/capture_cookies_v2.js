/**
 * 登录态 Cookie 采集工具 v2 —— 自动把登录入口送到使用者面前
 *
 * 相对 v1 的三处修正（2026-09-22 晚，王总反馈"窗口里没有二维码"）：
 *   1. 启动后**关掉多余标签页**（持久 profile 会恢复上次的 about:blank，
 *      且浏览器把焦点落在最后一个空白页 → 使用者看到一片黑，以为程序坏了）。
 *   2. 自动 `bringToFront()` + **自动点击「登录/注册」入口**触发登录弹窗，
 *      不再要求使用者自己在页面里找入口。
 *   3. 若弹窗未出现则兜底跳转 SSO 登录页，并**自动截图 + 写探测报告**，
 *      让"到底显示了什么"有图像证据，不靠口头描述。
 *
 * 用法：
 *   node tools/capture_cookies_v2.js https://www.tgb.cn/ 30     交互登录
 *   node tools/capture_cookies_v2.js --dump                     仅复用 profile 导出
 *
 * 产出：
 *   tools/cookies.json          —— 登录成功后写入，喂给 credentials add-cookie
 *   tools/_login_screen.png     —— 登录页截图（自证二维码是否出现）
 *   tools/_login_probe.json     —— 探测报告（页面 URL / 点击结果 / 二维码候选）
 *   tools/_capture_state.json   —— 轮询状态
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
const SHOT = path.join(HERE, '_login_screen.png');
const PROBE = path.join(HERE, '_login_probe.json');

const args = process.argv.slice(2);
const DUMP_ONLY = args.includes('--dump');
const posArgs = args.filter((a) => !a.startsWith('--'));
const START_URL = posArgs[0] || 'https://www.tgb.cn/';
const MAX_MIN = Number(posArgs[1] || 30);
const DOMAIN_HINT = new URL(START_URL).hostname.split('.').slice(-2).join('.');

const SSO_LOGIN = 'https://sso.tgb.cn/web/login/index';
const GATED_URL = 'https://www.tgb.cn/user/getMoreListAction';
const SSO_HINT = 'sso.tgb.cn/web/login';
const OUT_MARKERS = ['hd_unlogin', '登录/注册'];

const writeJson = (p, o) => fs.writeFileSync(p, JSON.stringify(o, null, 2), 'utf8');
const nowIso = () => new Date().toISOString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 受限页判定：带 Cookie 发真实请求，被踢到 SSO 登录页即未登录
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

// 探测页面上的"二维码候选"（canvas / 大图 / 大 iframe）
async function probeQr(page) {
  return await page.evaluate(() => {
    const out = { canvas: 0, imgs: [], iframes: [], login_entries: [] };
    out.canvas = document.querySelectorAll('canvas').length;
    for (const im of document.querySelectorAll('img')) {
      const w = im.width || 0, h = im.height || 0, src = im.src || '';
      if ((w >= 100 && h >= 100) || /qrcode|qr_|erweima|weixin|login/i.test(src)) {
        out.imgs.push({ w, h, src: src.slice(0, 140) });
      }
    }
    for (const f of document.querySelectorAll('iframe')) {
      const r = f.getBoundingClientRect();
      if (r.width > 80 && r.height > 80) {
        out.iframes.push({ src: (f.src || '').slice(0, 180), w: Math.round(r.width), h: Math.round(r.height) });
      }
    }
    const norm = (s) => (s || '').replace(/\s+/g, '');
    for (const el of document.querySelectorAll('a,button,span,div,li')) {
      const t = norm(el.textContent);
      if (!/^(登录|登录\/注册|登录注册|立即登录|请登录|会员登录)$/.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 6 || r.height < 6 || r.top < 0 || r.top > 300) continue;
      out.login_entries.push({ text: el.textContent.trim(), x: Math.round(r.x), y: Math.round(r.y), tag: el.tagName });
    }
    return out;
  }).catch((e) => ({ error: String(e.message) }));
}

// 自动点击「登录/注册」触发登录弹窗
async function clickLoginEntry(page) {
  return await page.evaluate(() => {
    const norm = (s) => (s || '').replace(/\s+/g, '');
    const cands = [];
    for (const el of document.querySelectorAll('a,button,span,div,li')) {
      const t = norm(el.textContent);
      if (!/^(登录|登录\/注册|登录注册|立即登录|请登录|会员登录)$/.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 6 || r.height < 6 || r.top < 0 || r.top > 300) continue;
      cands.push({ el, len: t.length, text: el.textContent.trim(), x: Math.round(r.x), y: Math.round(r.y) });
    }
    if (!cands.length) return { ok: false, reason: 'no-entry-found' };
    cands.sort((a, b) => a.len - b.len);
    const hit = cands[0];
    const target = hit.el.closest('a') || hit.el;
    target.click();
    return { ok: true, clicked_text: hit.text, at: { x: hit.x, y: hit.y }, candidates: cands.length };
  }).catch((e) => ({ ok: false, reason: 'eval-error: ' + e.message }));
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: false,
    userDataDir: PROFILE,
    defaultViewport: { width: 1400, height: 950 },
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });

  // —— 修正 1：只保留一个标签页，并把它置于前台 ——
  const opened = await browser.pages();
  const page = opened[0] || (await browser.newPage());
  const closed = [];
  for (let i = 1; i < opened.length; i++) {
    try { await opened[i].close(); closed.push(i); } catch { /* ignore */ }
  }

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
    writeJson(STATE, {
      status: 'dumped', cookie_count: rel.length,
      cookie_names: rel.map((c) => c.name), updated_at: nowIso(),
    });
    console.log('=== DUMPED === cookies=' + rel.length + ' -> ' + FINAL);
    await browser.close();
    return;
  }

  await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(2500);
  await page.bringToFront();

  // 先看持久 profile 里是否已带登录态（有的话就不用麻烦使用者了）
  let rel0 = await snapshot();
  const preState = await httpLoggedIn(rel0, ua);

  const probe = {
    at: nowIso(),
    closed_extra_tabs: closed,
    page_url: page.url(),
    title: await page.title().catch(() => ''),
    cookies_before_login: rel0.length,
    http_logged_in_before: preState,
    step: 'initial',
    initial_qr: await probeQr(page),
  };

  let clickRes = null;
  if (preState !== true) {
    // —— 修正 2：自动触发登录入口，把二维码送到面前 ——
    clickRes = await clickLoginEntry(page);
    await sleep(3500);
    await page.bringToFront();

    // 点击可能开了新标签页
    const after = await browser.pages();
    if (after.length > 1) {
      try { await after[after.length - 1].bringToFront(); } catch { /* ignore */ }
    }

    await page.screenshot({ path: SHOT }).catch(() => {});
    probe.step = 'after-click';
    probe.click_result = clickRes;
    probe.page_url_after_click = page.url();
    probe.qr_after_click = await probeQr(page);

    const q = probe.qr_after_click || {};
    const hasQr = (q.canvas > 0) || (q.imgs && q.imgs.length) || (q.iframes && q.iframes.length);

    // —— 修正 3：兜底走 SSO 独立登录页 ——
    if (!hasQr) {
      probe.step = 'fallback-sso';
      await page.goto(SSO_LOGIN, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
      await sleep(3500);
      await page.bringToFront();
      await page.screenshot({ path: SHOT.replace('.png', '_sso.png') }).catch(() => {});
      probe.sso_url = page.url();
      probe.qr_on_sso = await probeQr(page);
      const q2 = probe.qr_on_sso || {};
      probe.final_has_qr = (q2.canvas > 0) || (q2.imgs && q2.imgs.length) || (q2.iframes && q2.iframes.length);
    } else {
      probe.final_has_qr = true;
    }
  } else {
    probe.step = 'already-logged-in';
    probe.final_has_qr = false;
  }

  writeJson(PROBE, probe);

  // 回到首页等待登录（首页更贴近真实会话，且登录后会自动刷新出登录态）
  if (preState !== true && probe.step === 'fallback-sso') {
    await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await sleep(2000);
    await page.bringToFront();
    try { await clickLoginEntry(page); } catch { /* ignore */ }
  }

  const t0 = Date.now();
  let baseline = (await snapshot()).map((c) => c.name);
  let detected = false;

  while (Date.now() - t0 < MAX_MIN * 60000) {
    await sleep(3000);
    let rel = [];
    try { rel = await snapshot(); } catch { /* 跳转中，下一轮再取 */ }

    const names = rel.map((c) => c.name);
    const news = names.filter((n) => !baseline.includes(n));
    const httpState = await httpLoggedIn(rel, ua);
    const guess = httpState === true;

    // 每轮都确保窗口在前台，避免使用者又看到空白页
    if (!detected) { try { await page.bringToFront(); } catch { /* ignore */ } }

    writeJson(STATE, {
      status: guess ? 'login_detected' : 'waiting',
      cookie_count: rel.length,
      new_cookie_names: news,
      cookie_names: names,
      baseline_names: baseline,
      http_logged_in: httpState,
      page_url: page.url(),
      login_screen_shot: path.basename(SHOT),
      updated_at: nowIso(),
    });

    if (guess) {
      writeJson(FINAL, { url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: rel });
      console.log('=== LOGIN DETECTED === cookies=' + rel.length + ' -> ' + FINAL);
      detected = true;
      break;
    }
  }

  if (!detected) {
    const prev = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, 'utf8')) : {};
    writeJson(STATE, { ...prev, status: 'timeout', updated_at: nowIso() });
    console.log('=== TIMEOUT === 未检测到登录；请告知，我用 --dump 导出。');
  }

  await sleep(detected ? 6000 : 60000);
  await browser.close();
})();
