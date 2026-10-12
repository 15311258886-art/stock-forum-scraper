import json, ssl, urllib.request, urllib.parse, re, sqlite3, os, time

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))


def get(url, ref='', timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': ref or url})
    with op.open(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')


B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('A) 东财：用 post_id 抓股吧帖子页，看能否取到发布时间')
print('=' * 70)
em = con.execute("SELECT author, post_id, post_url, raw_text FROM trades WHERE site='eastmoney' AND post_id<>'' LIMIT 3").fetchall()
for r in em:
    pid, url = r['post_id'], r['post_url']
    print('\n--- post_id=%s %s' % (pid, r['author']))
    print('    url:', url)
    # A1: 帖子详情页
    try:
        t = get(url, 'https://guba.eastmoney.com/')
        print('    [页面] bytes=%d  waf=%s' % (len(t), ('访问提示' in t or 'aliyun' in t.lower())))
        for pat in [r'20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', r'20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}',
                    r'\d{2}-\d{2}\s+\d{2}:\d{2}', r'"post_time"\s*:\s*"([^"]+)"', r'"publish_time"\s*:\s*"([^"]+)"']:
            h = re.findall(pat, t)
            if h:
                print('     命中 %-42s %s' % (pat[:40], h[:4]))
    except Exception as e:
        print('    [页面] FAIL', type(e).__name__, str(e)[:90])
    # A2: 股吧 API
    for api in ['https://gbapi.eastmoney.com/api/Post/GetPostInfo?postid=%s&client=web&plat=web' % pid,
                'https://guba.eastmoney.com/interface/GetData.aspx?path=newlist&postid=%s' % pid]:
        try:
            t = get(api, 'https://guba.eastmoney.com/')
            print('    [API] %s -> %s' % (api.split('/')[2], t[:200].replace('\n', ' ')))
        except Exception as e:
            print('    [API] %s FAIL %s' % (api.split('/')[2], type(e).__name__))
    time.sleep(0.4)

print()
print('=' * 70)
print('B) 东财「实盘/模拟大赛」是否有公开持仓页')
print('=' * 70)
for name, u in [('模拟炒股', 'https://mncg.eastmoney.com/'),
                ('实盘大赛', 'https://ds.eastmoney.com/'),
                ('财富号', 'https://caifuhao.eastmoney.com/'),
                ('股吧用户主页', 'https://i.eastmoney.com/')]:
    try:
        t = get(u, 'https://www.eastmoney.com/')
        print('  %-12s %-40s bytes=%-8d waf=%s' % (name, u, len(t), ('访问提示' in t or 'aliyun' in t.lower())))
    except Exception as e:
        print('  %-12s %-40s FAIL %s' % (name, u, type(e).__name__))
    time.sleep(0.3)

print()
print('=' * 70)
print('C) 雪球：帖子时间能否取到')
print('=' * 70)
xq = con.execute("SELECT author, post_id, post_url, raw_text FROM trades WHERE site='xueqiu' LIMIT 4").fetchall()
print('  雪球 trades 样本:')
for r in xq:
    print('   ', r['author'], '| pid=', r['post_id'], '| url=', (r['post_url'] or '')[:70])
print()
xq2 = con.execute("SELECT author, url, html FROM raw_pages WHERE site='xueqiu' AND length(html)>5000 LIMIT 1").fetchone()
if xq2:
    t = xq2['html']
    print('  raw_pages 雪球页:', xq2['url'][:60], len(t))
    for pat in [r'"created_at"\s*:\s*(\d{10,13})', r'"timeBefore"\s*:\s*"([^"]+)"', r'20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}']:
        h = re.findall(pat, t)
        print('   命中 %-38s %s' % (pat[:36], h[:4]))
con.close()
