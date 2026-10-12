import os, sys, json
BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, os.path.join(BASE, 'viewer'))
os.chdir(os.path.join(BASE, 'viewer'))
import server as S

d = S.api_star_moves(limit=20, periods=4, scope='free')
print('empty:', d.get('empty'))
print('summary:', json.dumps(d.get('summary'), ensure_ascii=False))
print('periods:', d.get('periods'))
print('basis:', d.get('basis'))
print()
for r in d.get('rows', []):
    c = r['counts']
    print('=== %s  新进%s 增持%s 减持%s 退出%s ===' % (r['date'], c['new'], c['add'], c['cut'], c['exit']))
    for tag, key in (('新进', 'new'), ('增持', 'add'), ('减持', 'cut'), ('退出', 'exit')):
        items = r.get(key) or []
        if items:
            s = ' | '.join('%s %s %s(%.2f%%)' % (x['name'], x['symbol_name'] or x['symbol'], x['symbol'], x['ratio'] or 0) for x in items[:4])
            print('   %s: %s' % (tag, s[:190]))
    print()
