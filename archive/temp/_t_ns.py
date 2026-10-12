# -*- coding: utf-8 -*-
"""验证风险隔离后的牛散接口"""
import os, sys, json
BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, os.path.join(BASE, 'viewer'))
os.chdir(os.path.join(BASE, 'viewer'))
import server as S

d = S.api_niusan(min_hold=2, scope='free')
print('=== summary ===')
print(json.dumps(d.get('summary'), ensure_ascii=False))
print('basis:', json.dumps(d.get('basis'), ensure_ascii=False))
print()

inv = d.get('investors') or []
print('牛散数:', len(inv))
for p in inv[:12]:
    print('  %-10s 持%2s只 合计%.2f%% 新进%s 减持%s | %s' % (
        p['name'][:10], p['stock_count'], p.get('total_ratio') or 0,
        p.get('new_cnt'), p.get('cut_cnt'),
        '/'.join([x.get('symbol_name', '')[:6] for x in (p.get('holdings') or [])[:4]])))
print()

ch = d.get('cohold') or []
print('协同股:', len(ch))
for c in ch[:10]:
    print('  %s %-8s %s位牛散同持 合计%.2f%% | %s' % (
        c['symbol'], c['symbol_name'][:8], c['people_count'],
        c.get('star_ratio_sum') or 0,
        '/'.join([x['name'] for x in (c.get('people') or [])[:5]])))
