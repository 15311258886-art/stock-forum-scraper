import re, os
p = 'viewer/static/app.js'
src = open(p, encoding='utf-8').read()
ls = src.splitlines()
print('TOTAL', len(ls))

print('=== NAV 一级分组定义 ===')
for i, l in enumerate(ls, 1):
    if re.search(r'\{\s*key:\s*"', l) or re.search(r'items:\s*\[', l) or 'const NAV' in l:
        print('%5d: %s' % (i, l.strip()[:170]))

print()
print('=== 首个 nav 之后的 40 行 ===')
i = src.find('const NAV')
print(src[i:i+1500])

print()
print('=== 按需渲染分支 ===')
for i, l in enumerate(ls, 1):
    if 'if (tab ===' in l:
        print('%5d: %s' % (i, l.strip()[:150]))

print()
print('=== 状态变量与加载器定义 ===')
for i, l in enumerate(ls, 1):
    if re.search(r'^const (LHB|LBHS|NS|SUMMARY|DASH)', l) or re.search(r'^function (loadLhb|loadNiusan|renderNiusan|renderSummary)', l):
        print('%5d: %s' % (i, l.strip()[:150]))

print()
print('=== index.html panels ===')
h = open('viewer/static/index.html', encoding='utf-8').read()
for i, l in enumerate(h.splitlines(), 1):
    if 'class="panel"' in l:
        print('%5d: %s' % (i, l.strip()[:140]))
