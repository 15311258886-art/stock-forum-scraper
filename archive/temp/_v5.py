# -*- coding: utf-8 -*-
"""核查：①牛散接口(隔离后) ②变动流数据基础 ③板块扩散缺日根因"""
import os, sys, json, sqlite3

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')

print('=' * 60)
print('[1] 牛散接口（风险隔离后）')
sys.path.insert(0, os.path.join(BASE, 'viewer'))
os.chdir(os.path.join(BASE, 'viewer'))
try:
    import server as S
    d = S.api_niusan(min_hold=2, scope='free')
    s = d.get('summary') or {}
    print('  summary:', json.dumps(s, ensure_ascii=False))
    print('  牛散 Top6:')
    for p in (d.get('investors') or [])[:6]:
        print('    %-12s 持%2s只 合计%.2f%%' % (p['name'][:12], p['stock_count'], p.get('total_ratio') or 0))
    print('  协同股:', len(d.get('cohold') or []))
    for c in (d.get('cohold') or [])[:5]:
        print('    %s %-8s %s位同持' % (c['symbol'], c['symbol_name'][:8], c['people_count']))
    # 校验：家族持股是否已被挡住
    names = [p['name'] for p in (d.get('investors') or [])]
    for bad in ['张国芳', '张春芳', '朱大庆', '陈慧']:
        print('  家族名(%s)出现在榜中: %s' % (bad, bad in names))
except Exception as e:
    import traceback; traceback.print_exc()

print()
print('=' * 60)
print('[2] 变动流数据基础')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
print('  star 级 hold_change 取值分布:')
for r in con.execute("""SELECT hold_change, COUNT(*) n FROM stock_holders
  WHERE holder_kind='star' GROUP BY hold_change ORDER BY n DESC LIMIT 12"""):
    print('    %-16r %s' % (r['hold_change'], r['n']))
print()
print('  star 级持有的报告期分布:')
for r in con.execute("""SELECT end_date, COUNT(*) n, COUNT(DISTINCT holder_name) people
  FROM stock_holders WHERE holder_kind='star' GROUP BY end_date ORDER BY end_date DESC LIMIT 8"""):
    print('    %s  n=%-5s 人=%s' % (r['end_date'], r['n'], r['people']))
print()
print('  跨期可对比的牛散（同股出现≥2期）:')
n = con.execute("""SELECT COUNT(*) FROM (
  SELECT holder_name, symbol, COUNT(DISTINCT end_date) k FROM stock_holders
  WHERE holder_kind='star' GROUP BY holder_name, symbol HAVING k>=2)""").fetchone()[0]
print('   ', n)

print()
print('=' * 60)
print('[3] 板块扩散缺日根因')
print('  lhb_stocks 各日:')
for r in con.execute("SELECT trade_date, COUNT(DISTINCT security_code) c FROM lhb_stocks GROUP BY trade_date ORDER BY trade_date DESC LIMIT 8"):
    print('    %s  上榜股=%s' % (r['trade_date'], r['c']))
print('  lhb_sector_focus 各日:')
for r in con.execute("SELECT trade_date, COUNT(*) n FROM lhb_sector_focus GROUP BY trade_date ORDER BY trade_date DESC"):
    print('    %s  %s 个板块' % (r['trade_date'], r['n']))
con.close()
