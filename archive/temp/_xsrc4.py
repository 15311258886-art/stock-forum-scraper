# -*- coding: utf-8 -*-
"""探源3：雪球有内容的原始页 + 东财原帖结构"""
import os, re, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('A) 雪球 raw_pages 全清单（url / 长度 / 是否含 timeline__item）')
print('=' * 70)
for r in con.execute("SELECT rowid, url, length(html) l, fetched_at FROM raw_pages WHERE site='xueqiu' ORDER BY rowid"):
    html = con.execute("SELECT html FROM raw_pages WHERE rowid=?", (r['rowid'],)).fetchone()['html'] or ''
    n_item = html.count('timeline__item')
    n_created = len(re.findall(r'"created_at"\s*:\s*\d+', html))
    print('  [%s] len=%-7s item=%-4s created_at=%-4s %s' % (r['rowid'], r['l'], n_item, n_created, r['url'][:70]))

print()
print('=' * 70)
print('B) 东财 trades 数据结构（post_id / post_url）')
print('=' * 70)
for r in con.execute("SELECT id, author, post_id, post_url, post_time, action, symbol, symbol_name, raw_text FROM trades WHERE site='eastmoney' ORDER BY id LIMIT 8"):
    d = dict(r)
    print('  id=%s author=%s' % (d['id'], d['author']))
    print('     pid=%s time=%r action=%s sym=%s %s' % (d['post_id'], d['post_time'], d['action'], d['symbol'], d['symbol_name']))
    print('     url=%s' % (d['post_url'] or '')[:120])
    print('     raw=%s' % (d['raw_text'] or '')[:100])

print()
print('=' * 70)
print('C) 雪球 trades 全字段（哪两条有时间、时间从哪来）')
print('=' * 70)
for r in con.execute("SELECT * FROM trades WHERE site='xueqiu' ORDER BY id"):
    d = dict(r)
    ts = d.get('post_time') or '(空)'
    print('  id=%s time=%r author=%s sym=%s %s' % (d['id'], ts, d['author'], d['symbol'], d['symbol_name']))
    print('     url=%s' % (d.get('post_url') or '')[:120])
    print('     raw=%s' % (d.get('raw_text') or '')[:130])

con.close()
