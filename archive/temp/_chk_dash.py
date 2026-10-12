import os, re
B = os.path.dirname(os.path.abspath(__file__))

p = os.path.join(B, 'app.db')
if os.path.isfile(p) and os.path.getsize(p) == 0:
    os.remove(p)
    print('[已删除] 项目根 0 字节 app.db（空壳，引用全指向 viewer/app.db）')
else:
    print('[跳过] 根 app.db exists=%s size=%s' % (os.path.exists(p), (os.path.getsize(p) if os.path.exists(p) else '')))

js = open(os.path.join(B, 'viewer/static/app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer/static/index.html'), encoding='utf-8').read()

print()
print('=== 今日看板 前端接线核查 ===')
for k in ['dashboard', 'renderDashboard', 'loadDashboard', 'p-dashboard', '看板']:
    print('  app.js %-18s %d' % (k, js.count(k)))
ids = re.findall(r'id="(p-[a-z_]+)"', h)
print('  index panels:', len(ids))
print('  含 p-dashboard:', 'p-dashboard' in ids)

print()
print('=== NAV 一级分组 ===')
i = js.find('const NAV')
print(js[i:i + 220])

print()
print('=== NAV 中一级 key/label 列表 ===')
for m in re.finditer(r'key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  key=%-14s label=%s' % (m.group(1), m.group(2)))

print()
print('=== 现有子标签（含归属） ===')
for m in re.finditer(r'\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  tab=%-16s label=%s' % (m.group(1), m.group(2)))
