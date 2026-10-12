import os, re
B = os.getcwd()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
s = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()

print('=== 后端 ===')
print('  route /api/star_moves:', s.count('"/api/star_moves"'), '| def api_star_moves:', s.count('def api_star_moves'))
print()
print('=== 前端接线 ===')
for k in ['star_moves', 'starMoves', 'renderStarMoves', 'loadStarMoves', 'p-star-moves', 'STAR_MOVES', '牛散动向']:
    print('  app.js %-18s %d' % (k, js.count(k)))
print('  index p-star-moves:', 'p-star-moves' in h)
print()
print('=== NAV 子标签 ===')
for i, l in enumerate(js.splitlines(), 1):
    if re.search(r'tab: "(niusan|star_moves|lhb|lhb_sectors|consensus|operating|players|dashboard)"', l):
        print('%5d: %s' % (i, l.strip()[:130]))
print()
print('=== 相关行 ===')
for i, l in enumerate(js.splitlines(), 1):
    if 'star_moves' in l or 'starMoves' in l or 'StarMoves' in l or '牛散动向' in l:
        print('%5d: %s' % (i, l.strip()[:150]))
