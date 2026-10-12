import os, re
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
p = os.path.join(B, 'viewer', 'server.py')
ls = open(p, encoding='utf-8').read().splitlines()
print('TOTAL', len(ls))
print()
for i, l in enumerate(ls, 1):
    if 'route == ' in l and ('dashboard' in l or 'niusan' in l or 'lhb_sectors' in l or 'niusan_changes' in l):
        print('---- around line', i)
        for j in range(max(0, i - 2), min(i + 8, len(ls))):
            print('%5d: %s' % (j + 1, ls[j][:160]))
        print()
print('=== api_niusan_changes def? ===')
s = '\n'.join(ls)
print('def api_niusan_changes:', s.count('def api_niusan_changes'))
print('api_niusan_changes( use:', s.count('api_niusan_changes('))
print()
print('=== api_ 函数清单 ===')
for i, l in enumerate(ls, 1):
    if l.startswith('def api_'):
        print('  %5d: %s' % (i, l.strip()[:110]))
