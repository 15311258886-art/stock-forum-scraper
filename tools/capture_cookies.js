/**
 * 登录态 Cookie 采集工具（给"扫码/验证码登录"的站点用）
 *
 * 用法：
 *   1) 交互登录（会打开一个可见的浏览器窗口，请在其中完成登录）
 *      node tools/capture_cookies.js https://www.tgb.cn/ 30
 *   2) 只导出（复用上次的浏览器配置，不再打开登录流程）
 *      node tools/capture_cookies.js --dump
 *
 * 产出：
 *   tools/cookies.json          —— 可直接喂给 `python main.py credentials add-cookie taoguba --file tools/cookies.json`
 *   tools/_cookies_latest.json  —— 轮询期间的最新快照（诊断用）
 *   tools/_capture_state.json   —— 状态（waiting / login_detected / timeout）
 *
 * 说明：
 * - 使用持久化配置目录（.browser-profile），登录态会保留；下次可用 --dump 直接导出。
 * - 只导出与目标域相关的 Cookie；同时记录 userAgent，服务端请求需带上同一个 UA。
 * - 本工具不代填账号密码、不破解任何验证；登录动作全部由使用者本人在窗口中完成。
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

const args = process.argv.slice(2);
const DUMP_ONLY = args.includes('--dump');
const posArgs = args.filter((a) => !a.startsWith('--'));
const START_URL = posArgs[0] || 'https://www.tgb.cn/';
const MAX_MIN = Number(posArgs[1] || 30);
const DOMAIN_HINT = new URL(START_URL).hostname.split('.').slice(-2).join('.');

const writeJson = (p, o) => fs.writeFileSync(p, JSON.stringify(o, null, 2), 'utf8');
const nowIso = () => new Date().toISOString();

async function snapshot(page, client, ua, extra) {
  const { cookies } = await client.send('Network.getAllCookies');
  const rel = (cookies || []).filter((c) => (c.domain || '').includes(DOMAIN_HINT));
  const pack = {
    url: page.url(),
    user_agent: ua,
    captured_at: nowIso(),
    cookies: rel,
  };
  writeJson(LATEST, pack);
  return { rel, pack, ...extra };
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: false,
    userDataDir: PROFILE,
    defaultViewport: { width: 1280, height: 880 },
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });
  const page = (await browser.pages())[0] || (await browser.newPage());
  const client = await page.target().createCDPSession();
  await client.send('Network.enable');

  const ua = await browser.userAgent();

  if (DUMP_ONLY) {
    await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    const { rel, pack } = await snapshot(page, client, ua);
    writeJson(FINAL, pack);
    writeJson(STATE, {
      status: 'dumped', cookie_count: rel.length,
      cookie_names: rel.map((c) => c.name), updated_at: nowIso(),
    });
    console.log('=== DUMPED === cookies=' + rel.length + ' -> ' + FINAL);
    await browser.close();
    return;
  }

  await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // 判定一：页头是否仍显示「登录/注册」入口（正向包含匹配，不是精确相等）
  const headerHasLogin = () => page.evaluate(() => {
    const els = [...document.querySelectorAll('a,button,span,div')];
    return els.some((el) => {
      const t = (el.textContent || '').trim();
      if (!t.includes('登录') && !t.includes('注册')) return false;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return false;
      return r.top < 240 && r.width < 200;
    });
  }).catch(() => null);

  // 判定二（决定性）：带着当前 Cookie 发一次真实 HTTP 请求，检查服务端是否仍把请求踢去登录。
  // —— 2026-09-22 修正：判据由「首页」升级为「受限页」。
  //    原因：淘股吧登录态由独立 SSO 域 sso.tgb.cn 承载，未登录访问「我的关注」会被
  //    302 到 https://sso.tgb.cn/web/login/index?url=...；首页判据偏软，受限页判据正负证据都硬。
  const GATED_URL = 'https://www.tgb.cn/user/getMoreListAction';
  const SSO_HINT = 'sso.tgb.cn/web/login';
  const OUT_MARKERS = ['hd_unlogin', '登录/注册'];
  async function httpLoggedIn(cookies, ua) {
    if (!cookies.length) return null;
    try {
      const cookieHdr = cookies.map((c) => `${c.name}=${c.value}`).join('; ');
      const res = await fetch(GATED_URL, {
        headers: { 'User-Agent': ua, Cookie: cookieHdr, 'Accept-Language': 'zh-CN,zh;q=0.9' },
        redirect: 'follow',
      });
      const html = await res.text();
      if (html.length < 3000) return null;              // 拿不到完整页面时不判定，避免误判
      if ((res.url || '').includes(SSO_HINT)) return false;   // 被踢到 SSO 登录页 = 未登录
      if (OUT_MARKERS.some((m) => html.includes(m))) return false;
      return true;
    } catch {
      return null;
    }
  }

  const t0 = Date.now();
  let baseline = null;
  let detected = false;

  while (Date.now() - t0 < MAX_MIN * 60000) {
    await new Promise((r) => setTimeout(r, 3000));
    let rel = [];
    try {
      const snap = await snapshot(page, client, ua);
      rel = snap.rel;
    } catch { /* 页面跳转中，下一轮再取 */ }

    const names = rel.map((c) => c.name);
    if (!baseline) baseline = names.slice();
    const news = names.filter((n) => !baseline.includes(n));

    const loginEntry = await headerHasLogin();
    const httpState = await httpLoggedIn(rel, ua);   // true=已登录 false=未登录 null=无法判定
    const guess = httpState === true;

    writeJson(STATE, {
      status: guess ? 'login_detected' : 'waiting',
      cookie_count: rel.length,
      new_cookie_names: news,
      cookie_names: names,
      baseline_names: baseline,
      header_has_login_entry: loginEntry,
      http_logged_in: httpState,
      updated_at: nowIso(),
    });

    if (guess) {
      writeJson(FINAL, {
        url: page.url(), user_agent: ua, captured_at: nowIso(), cookies: rel,
      });
      console.log('=== LOGIN DETECTED === cookies=' + rel.length +
        ' new=' + news.join(',') + ' -> ' + FINAL);
      detected = true;
      break;
    }
  }

  if (!detected) {
    const prev = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, 'utf8')) : {};
    writeJson(STATE, { ...prev, status: 'timeout', updated_at: nowIso() });
    console.log('=== TIMEOUT / 未检测到登录 === 窗口将保持 60 秒；如你已登录，请告知，我用 --dump 导出。');
  }

  await new Promise((r) => setTimeout(r, detected ? 6000 : 60000));
  await browser.close();
})();
