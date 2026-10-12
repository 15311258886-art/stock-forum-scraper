import os, re
B = os.getcwd()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()

print('=== index.html panel ids ===')
ids = [m.group(1) for m in re.finditer(r'id="(p-[A-Za-z_\-]+)"', h)]
for x in ids:
    print('  ', x)
print()
print('=== STARM 定义 ===')
for i, l in enumerate(js.splitlines(), 1):
    if re.search(r'const STARM|STARM\s*=', l):
        print('%5d: %s' % (i, l.strip()[:150]))
print()
print('=== ensurePanel 中 star 相关 ===')
i = js.find('function ensurePanel')
print(js[i:i + 1400] if i > 0 else 'NOT FOUND')
print()
print('=== createPanel / panelMap 相关 ===')
for i, l in enumerate(js.splitlines(), 1):
    if re.search(r'p-star|panelMap|PANEL_|createPanel', l):
        print('%5d: %s' % (i, l.strip()[:150]))
