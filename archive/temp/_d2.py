# -*- coding: utf-8 -*-
"""查淘股吧带日期的帖子内容，能否交叉印证出建仓日"""
import sqlite3, os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== taoguba posts 内容样例（带日期）===')
for r in con.execute("SELECT author, post_time, url, text FROM posts WHERE site='taoguba' ORDER BY post_time DESC LIMIT 12"):
    t = (r['text'] or '').replace('\n', ' ')[:150]
    print('  [%s] %-14s %s' % (r['post_time'], (r['author'] or '')[:14], t))

print()
print('=== taoguba posts 里有股票代码的 ===')
cnt = 0
for r in con.execute("SELECT author, post_time, text FROM posts WHERE site='taoguba'"):
    codes = re.findall(r'\b(?:60|00|30|68|8[3-9]|4[3-9])\d{4}\b', r['text'] or '')
    names = re.findall(r'\$([^$()]{2,8})\(([A-Z]{2}\d{6})\)\$', r['text'] or '')
    if codes or names:
        cnt += 1
        if cnt <= 8:
            print('  [%s] %-12s codes=%s names=%s' % (r['post_time'], (r['author'] or '')[:12], codes[:5], names[:3]))
print('  有代码/标的的帖子数:', cnt, '/78')

print()
print('=== 淘股吧选手主页的帖子列表结构（fetch_taoguba_live 的来源）===')
p = os.path.join(B, 'tools', 'fetch_taoguba_live.py')
if os.path.exists(p):
    t = open(p, encoding='utf-8', errors='ignore').read()
    if '重点关注' in t:
        i = t.find('重点关注')
        print('  [重点关注 上下文]')
        print(t[max(0, i-500):i+700])
con.close()
