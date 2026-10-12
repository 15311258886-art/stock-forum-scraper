# -*- coding: utf-8 -*-
"""核查：共同持仓共识的实现 + 卖出数据可用性"""
import sys, os, re, sqlite3, json
sys.stdout.reconfigure(encoding='utf-8')

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

print('=' * 60)
print('1) 后端：共同持仓共识相关实现')
print('=' * 60)
s = open('viewer/server.py', encoding='utf-8').read()
for k in ['cohold', 'consensus', 'api_consensus', 'sell', 'SELL', '净卖出', '共同卖出']:
    print('  %-16s %d' % (k, s.count(k)))
print()
for n, l in enumerate(s.splitlines(), 1):
    if re.match(r'^def api_', l):
        print('%5d: %s' % (n, l.strip()[:110]))

print()
print('=' * 60)
print('2) positions 表：卖出记录的可用性')
print('=' * 60)
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
print('  positions.action 取值分布:')
for r in con.execute("SELECT action, COUNT(*) n FROM positions GROUP BY action ORDER BY n DESC"):
    print('    %r  %s' % (r['action'], r['n']))
print()
print('  positions 列:', [r[1] for r in con.execute('PRAGMA table_info(positions)')])
print()
print('  positions 按站点:')
for r in con.execute("SELECT site, COUNT(*) n FROM positions GROUP BY site"):
    print('    %-11s %s' % (r['site'], r['n']))

print()
print('=' * 60)
print('3) trades 表：卖出记录')
print('=' * 60)
print('  trades.action 取值分布:')
for r in con.execute("SELECT action, COUNT(*) n FROM trades GROUP BY action ORDER BY n DESC"):
    print('    %r  %s' % (r['action'], r['n']))
print()
print('  trades 按站点+动作:')
for r in con.execute("SELECT site, action, COUNT(*) n FROM trades GROUP BY site, action ORDER BY site, n DESC"):
    print('    %-11s %-8s %s' % (r['site'], r['action'] or '', r['n']))

print()
print('=' * 60)
print('4) 卖出语义统计')
print('=' * 60)
for tbl in ('positions', 'trades'):
    try:
        n = con.execute("SELECT COUNT(*) FROM %s WHERE action LIKE '%%卖%%' OR action LIKE '%%清%%' OR action LIKE '%%SELL%%' OR action LIKE '%%sell%%' OR action LIKE '%%减%%'" % tbl).fetchone()[0]
        print('  %s 含卖出语义: %s' % (tbl, n))
    except Exception as e:
        print('  %s ERR %s' % (tbl, str(e)[:60]))
print()
print('  positions 中 action 非"持仓/关注操作"的样例:')
for r in con.execute("SELECT site, player, symbol, symbol_name, action, post_time FROM positions WHERE (action IS NULL OR action NOT IN ('持仓','关注/操作')) LIMIT 12"):
    print('   ', dict(r))
con.close()
