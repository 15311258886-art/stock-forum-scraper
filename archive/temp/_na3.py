# -*- coding: utf-8 -*-
"""核对 NAV 结构、默认落点、看板渲染触发链"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

print('app.js TOTAL', len(ls))
print()
print('=== NAV 定义 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
print(js[i:j+3] if i >= 0 else 'NAV NOT FOUND')
print()
print('=== 默认 tab / 初始落点 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'DEFAULT_TAB|currentTab|activeTab|defaultTab|location\.hash|selectNav\(|showTab\(|switchTab\(|curTab', l):
        print('%5d: %s' % (n, l.strip()[:170]))
print()
print('=== 看板相关 ===')
for k in ['renderDashboard', 'loadDashboard', 'DASH', 'p-dashboard', 'api/dashboard']:
    hits = [(n, l.strip()[:150]) for n, l in enumerate(ls, 1) if k in l]
    print('  %-18s %d 处' % (k, len(hits)))
    for n, l in hits[:6]:
        print('      %5d: %s' % (n, l))
