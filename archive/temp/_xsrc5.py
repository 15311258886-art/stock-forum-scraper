# -*- coding: utf-8 -*-
"""探查雪球搜索结果页：帖子结构 + 时间格式 + 作者/动作线索。只读。"""
import sqlite3, re, os, json, html as H

B = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(B, 'viewer', 'app.db')

con = sqlite3.connect('file:%s?mode=ro' % DB.replace('\\', '/'), uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('A) 雪球搜索页内容体检')
print('=' * 70)
for r in con.execute("SELECT id, url, length(html) l FROM raw_pages WHERE site='xueqiu' AND length(html)>10000 ORDER BY id"):
    h = r['html'] or ''
    print('  id=%-4s len=%-7s %s' % (r['id'], r['l'], r['url']))
    for kw in ['timeline__item', 'status__time', 'user-name', 'datetime', 'time', '股票', '讨论']:
        print('      %-16s %d' % (kw, h.count(kw)))
print()

print('=' * 70)
print('B) 找一个搜索页，抠出帖子块与时间串')
print('=' * 70)
row = con.execute("SELECT id, url, html FROM raw_pages WHERE site='xueqiu' AND url LIKE '%q=600519%' ORDER BY id DESC LIMIT 1").fetchone()
if row:
    h = row['html'] or ''
    print('  页面:', row['url'], 'len', len(h))
    # 1) 所有疑似时间串
    pats = {
        'YYYY-MM-DD HH:MM': r'20\d{2}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}',
        'YYYY-MM-DD': r'20\d{2}-\d{1,2}-\d{1,2}',
        'MM-DD HH:MM': r'\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}',
        'HH:MM': r'(?<![\d:])\d{1,2}:\d{2}(?![\d:])',
        'x分钟前': r'\d+\s*分钟前',
        'x小时前': r'\d+\s*小时前',
        '今天': r'今天',
        '昨天': r'昨天',
        'data-time': r'data-time="[^"]*"',
        'datetime=': r'datetime="[^"]*"',
        'created_at': r'created_at[^,}]{0,40}',
        'time=': r'[?&]time=\d+',
    }
    for k, p in pats.items():
        hits = re.findall(p, h)
        print('  %-20s %3d  %s' % (k, len(hits), hits[:6]))
    print()
    # 2) 帖子块
    m = re.search(r'<div[^>]*class="[^"]*timeline__item[^"]*"', h)
    print('  timeline__item 首次出现位置:', m.start() if m else None)
    if m:
        seg = h[m.start(): m.start() + 3000]
        seg = re.sub(r'\s+', ' ', seg)
        print('  --- 帖子块前 2200 字 ---')
        print(seg[:2200])
    # 3) 找 statuses/search JSON 是否内嵌
    for kw in ['__INITIAL_STATE__', 'statuses', 'searchResult', 'window.__']:
        idx = h.find(kw)
        print('  内嵌 %-18s at %s' % (kw, idx))
con.close()
