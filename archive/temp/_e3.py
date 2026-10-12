# -*- coding: utf-8 -*-
"""探查淘股吧/雪球/新浪的人数来源与可扩展候选池"""
import os, json, re, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'

print('=' * 70)
print('【1】data/ 目录文件')
print('=' * 70)
d = os.path.join(B, 'data')
for f in sorted(os.listdir(d)):
    p = os.path.join(d, f)
    if os.path.isfile(p):
        print('  %-46s %s' % (f, os.path.getsize(p)))

print()
print('=' * 70)
print('【2】config/sites.yaml 中的玩家来源配置')
print('=' * 70)
y = os.path.join(B, 'config', 'sites.yaml')
if os.path.exists(y):
    t = open(y, encoding='utf-8').read()
    for ln in t.splitlines():
        if re.search(r'player|blogger|uid|list|taoguba|sina|xueqiu', ln, re.I):
            print('  ' + ln.rstrip()[:150])

print()
print('=' * 70)
print('【3】淘股吧候选池（json）')
print('=' * 70)
for fn in os.listdir(d):
    if 'taoguba' in fn and fn.endswith('.json'):
        p = os.path.join(d, fn)
        try:
            j = json.load(open(p, encoding='utf-8'))
        except Exception as e:
            print('  %-40s parse err %s' % (fn, e)); continue
        if isinstance(j, list):
            print('  %-40s list len=%d' % (fn, len(j)))
            if j and isinstance(j[0], dict):
                print('       keys:', list(j[0].keys())[:14])
        elif isinstance(j, dict):
            print('  %-40s dict keys=%s' % (fn, list(j.keys())[:14]))

print()
print('=' * 70)
print('【4】fetch_taoguba_live.py 如何选人')
print('=' * 70)
p = os.path.join(B, 'tools', 'fetch_taoguba_live.py')
if os.path.exists(p):
    ls = open(p, encoding='utf-8').read().splitlines()
    print('  lines', len(ls))
    for i, l in enumerate(ls, 1):
        if re.search(r'url|http|def |list|uid|blog|limit|range|page|save_player', l, re.I):
            print('  %4d: %s' % (i, l.rstrip()[:155]))
