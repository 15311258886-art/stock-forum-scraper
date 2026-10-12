import os, sys, json, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
BASE = os.getcwd()
sys.path.insert(0, os.path.join(BASE, 'viewer'))
os.chdir(os.path.join(BASE, 'viewer'))
import server as S

print('=== server 里的相关函数 ===')
for n in dir(S):
    if 'consensus' in n.lower() or 'cohold' in n.lower():
        print('  ', n)

print()
print('=== /api/consensus 返回结构 ===')
try:
    d = S.api_consensus(min_holders=2)
    print('  type', type(d).__name__, 'len', len(d) if hasattr(d,'__len__') else '')
    if isinstance(d, list) and d:
        print('  item keys:', list(d[0].keys()))
        for x in d[:4]:
            print('   %s %s holders=%s weighted=%s' % (x.get('symbol'), x.get('symbol_name'), x.get('holders'), x.get('weighted_holders')))
except Exception as e:
    print('  ERR', type(e).__name__, str(e)[:200])

print()
print('=== /api/cohold_recent 返回结构 ===')
try:
    c = S.api_cohold_recent(days=5, min_holders=2, anchor='auto')
    print('  keys:', list(c.keys()))
    print('  days=%s window=%s~%s anchor=%s' % (c.get('days'), c.get('window_start'), c.get('window_end'), c.get('anchor')))
    print('  buy n=%s  sell n=%s' % (len(c.get('buy') or []), len(c.get('sell') or [])))
    for g in ('buy', 'sell'):
        for x in (c.get(g) or [])[:4]:
            print('   [%s] %s %s 人数=%s' % (g, x.get('symbol'), x.get('symbol_name'), x.get('player_count')))
except Exception as e:
    print('  ERR', type(e).__name__, str(e)[:200])

print()
print('=== trades 里卖出记录分布 ===')
con = sqlite3.connect('file:app.db?mode=ro', uri=True); con.row_factory = sqlite3.Row
for r in con.execute("SELECT site, action, COUNT(*) n FROM trades GROUP BY site, action ORDER BY n DESC"):
    print('  %-11s %-6s %s' % (r['site'], r['action'], r['n']))
print()
print('=== 卖出记录明细 ===')
for r in con.execute("SELECT site,author,symbol,symbol_name,post_time FROM trades WHERE UPPER(IFNULL(action,'')) LIKE '%SELL%' OR action LIKE '%卖%' ORDER BY post_time DESC"):
    print('  %-10s %-14s %s %s %s' % (r['site'], (r['author'] or '')[:14], r['symbol'], r['symbol_name'], r['post_time']))
print()
print('=== positions 里 action 取值 ===')
for r in con.execute("SELECT action, COUNT(*) n FROM positions GROUP BY action ORDER BY n DESC"):
    print('  %r %s' % (r['action'], r['n']))
con.close()
