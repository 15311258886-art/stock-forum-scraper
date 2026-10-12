# -*- coding: utf-8 -*-
"""诊断：卖出数据时间分布 + cohold_recent/consensus_stocks 实现"""
import sys, re, os, sqlite3, json
sys.stdout.reconfigure(encoding='utf-8')
B = os.path.dirname(os.path.abspath(__file__))
out = []

db = open(os.path.join(B, 'viewer', 'db.py'), encoding='utf-8').read()
for fn in ['consensus_stocks', 'cohold_recent']:
    i = db.find('def ' + fn)
    j = db.find('\n    def ', i + 10)
    out.append('=========== DB.%s ===========' % fn)
    out.append(db[i:j if j > 0 else i + 3500])
    out.append('')

con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
out.append('=========== 全部 SELL 类 trades ===========')
for r in con.execute("SELECT id,site,author,symbol,symbol_name,action,post_time,post_url FROM trades WHERE UPPER(IFNULL(action,'')) LIKE '%SELL%' OR action LIKE '%卖%' ORDER BY post_time"):
    out.append('  %s | %s | %s | %s | %s | %s | %s' % (r['id'], r['site'], r['author'], r['symbol'], r['symbol_name'], r['action'], r['post_time']))

out.append('')
out.append('=========== trades.action 取值分布 ===========')
for r in con.execute("SELECT action, COUNT(*) n FROM trades GROUP BY action ORDER BY n DESC"):
    out.append('  %r  %s' % (r['action'], r['n']))

out.append('')
out.append('=========== trades 有时间的 (buy/sell) ===========')
for r in con.execute("SELECT site,author,symbol,symbol_name,action,post_time FROM trades WHERE post_time IS NOT NULL AND TRIM(post_time)<>'' ORDER BY post_time DESC LIMIT 40"):
    out.append('  %-10s %-12s %-8s %-8s %-6s %s' % (r['site'], (r['author'] or '')[:12], r['symbol'], (r['symbol_name'] or '')[:8], r['action'], r['post_time']))
con.close()

open(os.path.join(B, '_z_sell_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('WROTE', os.path.getsize(os.path.join(B, '_z_sell_out.txt')))
