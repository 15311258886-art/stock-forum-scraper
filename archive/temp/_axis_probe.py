# -*- coding: utf-8 -*-
"""验证「人主页」里是否有实盘持仓/操作 —— 人轴可行性的决定性证据
逐站取一个真实用户的个人主页，用浏览器（过WAF）抓回，检查：
  a) 页面是否含持仓/操作/仓位 这类结构化信息
  b) 是否含发帖时间（帖子可作印证）
  c) 页面大小与标题
"""
import os, re, sys, sqlite3, json, time

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, BASE)
os.chdir(BASE)

from scraper import browser_fetch as bf

DB = os.path.join(BASE, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

OUT = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    OUT.append(s)
    print(s, flush=True)

# --- 取样本：每站一个真实用户 ---
samples = []
for site in ('taoguba', 'xueqiu', 'sina'):
    r = con.execute("""SELECT name, source_url FROM players WHERE site=?
        AND source_url IS NOT NULL AND TRIM(source_url)<>'' LIMIT 1""", (site,)).fetchone()
    if r:
        samples.append((site, r['name'], r['source_url']))
# 东财：从原帖里拿 uid 构造主页
em = con.execute("""SELECT author, post_url FROM trades WHERE site='eastmoney'
    AND post_url IS NOT NULL AND TRIM(post_url)<>'' LIMIT 1""").fetchone()
if em:
    samples.append(('eastmoney', em['author'], em['post_url']))
con.close()

P('=' * 76)
P('人主页实盘数据可行性验证')
P('=' * 76)

for site, name, url in samples:
    P()
    P('─' * 76)
    P('[%s] %s' % (site, name))
    P('  入口: %s' % url)

    # 东财：先回抓原帖拿 uid，再访问人主页
    if site == 'eastmoney':
        try:
            r = bf.browser_get(url, timeout=45)
            html = getattr(r, 'text', '') or ''
            P('  原帖: %s bytes, status=%s' % (len(html), getattr(r, 'status_code', '?')))
            # 从 guba 帖子里抠 uid
            ids = re.findall(r'i\.eastmoney\.com/(\d{10,})', html)
            ids = [x for x in dict.fromkeys(ids)]
            P('  帖子内提取到的作者uid: %s' % (ids[:5] or '(无)'))
            if not ids:
                continue
            url = 'https://i.eastmoney.com/%s' % ids[0]
            P('  → 人主页: %s' % url)
        except Exception as e:
            P('  原帖抓取异常: %s %s' % (type(e).__name__, str(e)[:100]))
            continue

    # 访问人主页
    try:
        t0 = time.time()
        r = bf.browser_get(url, timeout=45)
        dt = time.time() - t0
        html = getattr(r, 'text', '') or ''
        P('  人主页: %s bytes, status=%s, %.1fs' % (len(html), getattr(r, 'status_code', '?'), dt))

        # 标题
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
        P('  标题: %s' % (re.sub(r'\s+', ' ', m.group(1)).strip()[:70] if m else '(无)'))

        # 是否 WAF 拦截
        if len(html) < 4000 and ('访问提示' in html or 'aliyun' in html.lower() or 'waf' in html.lower()):
            P('  ⚠ 疑似 WAF 拦截页')
        if 'PROXY_UNREACHABLE' in html:
            P('  ⚠ 代理不可达')

        # a) 持仓/操作类关键词
        kws = ['持仓', '仓位', '重仓', '买入', '卖出', '建仓', '操作', '实盘', '成本', '盈亏', '调仓', '自选', '组合']
        hits = {k: html.count(k) for k in kws if html.count(k) > 0}
        P('  a) 持仓/操作词命中: %s' % (json.dumps(hits, ensure_ascii=False) if hits else '(无)'))

        # b) 时间格式（帖子可作印证）
        tpat = [r'20\d{2}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}', r'\d{1,2}-\d{1,2}\s+\d{2}:\d{2}',
                r'\d{1,2}-[01]\d-[0-3]\d', r'今天\s*\d{1,2}:\d{2}', r'\d+分钟前', r'\d+小时前']
        th = {p: len(re.findall(p, html)) for p in tpat}
        th = {k: v for k, v in th.items() if v}
        P('  b) 时间文本命中: %s' % (json.dumps(th, ensure_ascii=False) if th else '(无)'))

        # c) 股票代码数
        codes = set(re.findall(r'\b(?:60|00|30|68|83|87|43|92)\d{4}\b', html))
        P('  c) 页面内 A 股代码数: %d  样例 %s' % (len(codes), sorted(codes)[:8]))

        # 存原始页供细看
        fp = os.path.join(BASE, '_ax_%s.html' % site)
        open(fp, 'w', encoding='utf-8').write(html)
        P('  已存原始页: %s' % os.path.basename(fp))
    except Exception as e:
        P('  人主页抓取异常: %s %s' % (type(e).__name__, str(e)[:120]))

P()
P('=' * 76)
open(os.path.join(BASE, '_axis_probe_out.txt'), 'w', encoding='utf-8').write('\n'.join(OUT))
