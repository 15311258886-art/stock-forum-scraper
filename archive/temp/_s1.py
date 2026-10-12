import os, re
B = os.path.dirname(os.path.abspath(__file__))
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
print('app.js lines:', len(js.splitlines()))
print()
print('=== 今日看板接线状态 ===')
for k in ['dashboard', 'renderDashboard', 'loadDashboard', 'p-dashboard', '看板', 'DASH']:
    print('  app.js %-18s %d' % (k, js.count(k)))
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
print('  index panels:', len(ids), '| 含 p-dashboard:', 'p-dashboard' in ids)
print()
print('=== NAV 一级分组 ===')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  key=%-14s label=%s' % (m.group(1), m.group(2)))
print()
print('=== 二级子标签（tab/label）===')
for m in re.finditer(r'\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  tab=%-14s label=%s' % (m.group(1), m.group(2)))
