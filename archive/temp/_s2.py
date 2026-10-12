import os, re
B = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(B, 'viewer', 'static', 'app.js')
ls = open(p, encoding='utf-8').read().splitlines()
print('TOTAL', len(ls))
print()
print('=== NAV 定义全文 ===')
i = None
for n, l in enumerate(ls, 1):
    if 'key: "overview"' in l:
        i = n - 1
        break
if i is not None:
    for j in range(max(0, i - 12), min(len(ls), i + 70)):
        print('%5d: %s' % (j + 1, ls[j][:175]))
print()
print('=== 按需渲染分支 ===')
for n, l in enumerate(ls, 1):
    if 'if (tab ===' in l or re.search(r'else\s+load\w+\(\)', l):
        print('%5d: %s' % (n, l.strip()[:170]))
