import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
p = os.path.join(B, 'viewer', 'static', 'app.js')
js = open(p, encoding='utf-8').read()
ls = js.splitlines()

out = []
out.append('app.js TOTAL %d' % len(ls))
out.append('')
out.append('=== 顶层函数定义（1600-2100 行区间）===')
for i, l in enumerate(ls, 1):
    if 1600 <= i <= 2100 and re.match(r'^(async )?function [a-zA-Z_$]', l):
        out.append('%5d: %s' % (i, l.strip()[:150]))

out.append('')
out.append('=== renderNiusan / renderStarMoves / renderDashboard 关键锚点 ===')
for i, l in enumerate(ls, 1):
    if re.search(r'(renderNiusan|loadNiusan|renderStarMoves|loadStarMoves|renderDashboard|loadDashboard)', l):
        out.append('%5d: %s' % (i, l.strip()[:160]))

out.append('')
out.append('=== 面板 id 引用（p-niusan / p-dashboard / p-star_moves）===')
for i, l in enumerate(ls, 1):
    if re.search(r'#p-(niusan|dashboard|star_moves)', l):
        out.append('%5d: %s' % (i, l.strip()[:160]))

open(os.path.join(B, '_v8_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
