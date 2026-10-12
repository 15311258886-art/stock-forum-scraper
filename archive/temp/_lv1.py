import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
s = open('viewer/server.py', encoding='utf-8').read()
ls = s.splitlines()

print('=== server.py: learn / kb / consensus 相关 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'def api_(kb|learn|consensus|cohold|strategy)', l) or re.search(r'route == "/api/(kb|learn|consensus|cohold|strategy)', l):
        print('%5d: %s' % (n, l.strip()[:150]))

print()
print('=== 全部路由（含 learn/kb/consensus/strategy） ===')
routes = sorted(set(re.findall(r'route == "(/api/[a-z_/]*)"', s)))
print(' 共', len(routes), '个')
for r in routes:
    if any(k in r for k in ('kb', 'learn', 'consensus', 'cohold', 'strategy', 'basics', 'sim')):
        print('  ', r)

print()
print('=== NAV 中 learn 组 ===')
js = open('viewer/static/app.js', encoding='utf-8').read()
i = js.find('const NAV')
j = js.find('\n];', i)
navseg = js[i:j+3]
print(navseg)
