/**
 * 实测淘股吧登录流程：点击「登录」前后 cookie 的变化
 * 目的：确认 loginStatus=qrcode 究竟是「已登录」还是「扫码流程进行中」的状态标记
 * 产出：tools/_probe_login_flow.json + tools/_probe_login_after_click.png
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const puppeteer = require('puppeteer-core');

const EXE = process.env.CHROME_EXE ||
  'C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const HERE = __dirname;
const PROFILE = path.join(os.tmpdir(), 'tgb-probe-profile-' + Date.now());
const OUT_MARKERS = ['hd_unlogin', '登录/注册'];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EXE,
    headless: true,
    userDataDir: PROFILE,
    defaultViewport: { width: 1440, height: 900 },
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });
  const page = (await browser.pages())[0] || (await browser.newPage());
  const client = await page.target().createCDPSession();
  await client.send('Network.enable');
  const ua = await browser.userAgent();
  const rep = { profile: PROFILE, user_agent: ua };

  const snap = async () => {
    const { cookies } = await client.send('Network.getAllCookies');
    return (cookies || []).filter((c) => (c.domain || '').includes('tgb'));
  };

  await page.goto('https://www.tgb.cn/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await new Promise((r) => setTimeout(r, 2500));

  const c1 = await snap();
  rep.step1_initial_cookie_names = c1.map((c) => c.name);
  rep.step1_has_loginStatus = c1.some((c) => c.name === 'loginStatus');
  rep.step1_has_tgbuser = c1.some((c) => c.name === 'tgbuser');
  const html1 = await page.content();
  rep.step1_page_unlogin_markers = OUT_MARKERS.filter((m) => html1.includes(m));

  rep.step2_login_entry_candidates = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('a,button,span,div,li').forEach((el) => {
      const t = (el.textContent || '').trim();
      if (!t.includes('登录')) return;
      const r = el.getBoundingClientRect();
      if (r.width > 8 && r.height > 8 && r.top < 320) out.push({ tag: el.tagName, text: t.slice(0, 16), top: Math.round(r.top) });
    });
    return out.slice(0, 12);
  }).catch(() => []);

  rep.step3_clicked = await page.evaluate(() => {
    const els = [...document.querySelectorAll('a,button,span,div,li')];
    const cand = els.filter((el) => {
      const t = (el.textContent || '').trim();
      const r = el.getBoundingClientRect();
      return t.includes('登录') && r.width > 8 && r.height > 8 && r.top < 320 && t.length <= 10;
    });
    if (!cand.length) return false;
    cand[0].click();
    return true;
  }).catch(() => false);

  await new Promise((r) => setTimeout(r, 4500));

  const c2 = await snap();
  const names1 = c1.map((c) => c.name);
  rep.step4_after_click_cookie_names = c2.map((c) => c.name);
  rep.step5_new_cookie_names = c2.filter((c) => !names1.includes(c.name)).map((c) => c.name);
  rep.step6_new_cookies_detail = c2.filter((c) => !names1.includes(c.name))
    .map((c) => ({ name: c.name, value: (c.value || '').slice(0, 100), domain: c.domain }));
  rep.step7_loginStatus_value = (c2.find((c) => c.name === 'loginStatus') || {}).value || null;
  rep.step8_tgbuser_value = (c2.find((c) => c.name === 'tgbuser') || {}).value || null;
  rep.step9_final_url = page.url();

  const bodyText = await page.evaluate(() => (document.body ? document.body.innerText : '').slice(0, 6000)).catch(() => '');
  rep.step10_scan_words_on_page = ['扫码', '二维码', '微信', '手机号', '验证码', '登录'].filter((w) => bodyText.includes(w));

  await page.screenshot({ path: path.join(HERE, '_probe_login_after_click.png'), fullPage: false }).catch(() => {});
  fs.writeFileSync(path.join(HERE, '_probe_login_flow.json'), JSON.stringify(rep, null, 2), 'utf8');
  await browser.close();
})().catch((e) => {
  fs.writeFileSync(path.join(HERE, '_probe_login_flow.json'),
    JSON.stringify({ error: String(e) }, null, 2), 'utf8');
});
