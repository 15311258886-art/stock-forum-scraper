// moer_crawler.cjs
// 摩尔金融实盘/组合抓取 - 探查型浏览器抓取段（Node + puppeteer-core）。
//
// 说明：摩尔社区实盘页为 SPA，且 front.moer.cn 经代理访问才稳（本机需开代理 127.0.0.1:53269）。
// 本脚本为「探查型」：给定起始 URL 列表，渲染后抽取帖子文本，正则识别 A 股 6 位代码 +
// 动作词(买入/卖出/持有)，输出 moer_posts.json 供 moer_load.py 入库。
// 若默认 URL 抓不到，请把你浏览器里「摩尔实盘/组合页」的地址填进 moer_targets.json（数组）。
//
// 环境变量：
//   MOER_TARGETS_FILE  起始 URL 数组 JSON（默认 moer_targets.json）
//   MOER_URLS          “,” 分隔 URL（与文件二选一）
//   MOER_OUT           输出路径（默认 moer_posts.json）
//   XQ_CHROME_EXE / XQ_PROXY / XQ_NODE_PATH  同雪球抓取（复用本机 Chromium + 代理）
//
// 输出 moer_posts.json：
//   { "crawled_at":"...", "posts":[ {"author":"","text":"","codes":[{"symbol","action"}],
//       "url":"","time":""} ], "errors":[...] }

const fs = require('fs');
const path = require('path');
const os = require('os');

const HOME = process.env.USERPROFILE || os.homedir();
const CHROME_EXE = process.env.XQ_CHROME_EXE ||
  path.join(HOME, 'AppData', 'Local', 'ms-playwright', 'chromium-1234', 'chrome-win64', 'chrome.exe');
const PROXY = process.env.XQ_PROXY || 'http://127.0.0.1:53269';
const NODE_PATH = process.env.XQ_NODE_PATH ||
  'C:\\Users\\野比&大熊\\.workbuddy\\binaries\\node\\workspace\\node_modules';
const OUT = process.env.MOER_OUT || 'moer_posts.json';

let puppeteer;
try { puppeteer = require(path.join(NODE_PATH, 'puppeteer-core')); }
catch (e) { console.error('LOAD_PUPPETEER_ERR ' + e.message); process.exit(2); }

function loadUrls() {
  let urls = [];
  if (process.env.MOER_URLS) urls = process.env.MOER_URLS.split(',').map(s => s.trim()).filter(Boolean);
  const tf = process.env.MOER_TARGETS_FILE || 'moer_targets.json';
  if (!urls.length && fs.existsSync(tf)) {
    try { urls = JSON.parse(fs.readFileSync(tf, 'utf-8')); } catch (e) { console.error('URLS_PARSE_ERR ' + e.message); }
  }
  if (!Array.isArray(urls) || !urls.length) {
    // 默认尝试摩尔社区热门/实盘页（如抓取不到，请填 moer_targets.json）
    urls = [
      'https://www.moer.cn/',
      'https://m.moer.cn/',
    ];
  }
  return [...new Set(urls.filter(Boolean))];
}

// A 股 6 位代码 + 动作词
const CODE_RE = /(?<!\d)(60\d{4}|688\d{3}|00\d{4}|30\d{4}|8\d{5}|4\d{5})(?!\d)/g;
const ACTION_RE = /(买入|建仓|加仓|全仓买入|买进|低吸|卖出|减仓|清仓|平仓|止盈|止损|割肉|持仓|持有)/;
const ACTION_MAP = { '买入':'BUY','建仓':'BUY','加仓':'BUY','全仓买入':'BUY','买进':'BUY','低吸':'BUY',
  '卖出':'SELL','减仓':'SELL','清仓':'SELL','平仓':'SELL','止盈':'SELL','止损':'SELL','割肉':'SELL',
  '持仓':'HOLD','持有':'HOLD' };

function extractCodes(text) {
  if (!text) return [];
  const out = [];
  const seen = new Set();
  let m;
  while ((m = CODE_RE.exec(text)) !== null) {
    const sym = m[1];
    if (seen.has(sym)) continue;
    seen.add(sym);
    const am = ACTION_RE.exec(text);
    out.push({ symbol: sym, action: am ? ACTION_MAP[am[1]] : 'HOLD' });
  }
  return out;
}

const urls = loadUrls();
console.error('INFO urls=' + urls.length + ' chrome=' + CHROME_EXE);

(async () => {
  const result = { crawled_at: new Date().toISOString(), posts: [], errors: [] };
  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: CHROME_EXE, headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
             '--ignore-certificate-errors', '--proxy-server=' + PROXY],
    });
  } catch (e) { console.error('BROWSER_LAUNCH_ERR ' + e.message); result.errors.push('BROWSER_LAUNCH_ERR: ' + e.message); fs.writeFileSync(OUT, JSON.stringify(result, null, 2)); process.exit(2); }

  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36');

  for (const url of urls) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 25000 });
      // 等帖子节点（宽松：常见容器）
      try { await page.waitForSelector('a, div, li, p, article', { timeout: 8000 }); } catch (e) {}
      await new Promise(r => setTimeout(r, 2000));
      const html = await page.content();
      // 拆分为「块」：按常见帖子容器
      const blocks = await page.evaluate(() => {
        const nodes = Array.from(document.querySelectorAll('article, .post, .feed-item, li, .item, .list-item, div[class*="post"], div[class*="feed"]'));
        return nodes.slice(0, 200).map(n => ({
          text: (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 600),
          author: (n.querySelector('a[class*="user"], a[class*="name"], .author, .nickname') || {}).innerText || '',
          href: location.href,
        })).filter(b => b.text && b.text.length > 4);
      });
      let cnt = 0;
      for (const b of blocks) {
        const codes = extractCodes(b.text);
        if (!codes.length) continue;
        result.posts.push({ author: (b.author || '').trim().slice(0, 40), text: b.text, codes, url: b.href, time: '' });
        cnt++;
      }
      console.error('OK ' + url + ' blocks=' + blocks.length + ' posts=' + cnt);
    } catch (e) {
      result.errors.push('URL_ERR ' + url + ': ' + e.message);
      console.error('URL_ERR ' + url + ' ' + e.message);
    }
  }
  try { await browser.close(); } catch (e) {}
  fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
  console.error('DONE posts=' + result.posts.length + ' -> ' + OUT);
})().catch(e => { console.error('FATAL ' + e.message); process.exit(1); });
