import sys, os, re
sys.stdout.reconfigure(encoding='utf-8')
s = open('viewer/server.py', encoding='utf-8').read()
ls = s.splitlines()

print('=== api_consensus ===')
i = s.find('def api_consensus')
j = s.find('\ndef ', i + 10)
print('span %d-%d' % (i, j))
print(s[i:j][:2500])
print()

print('=== 路由 /api/consensus ===')
for m in re.finditer(r'route == "/api/consensus[^"]*"', s):
    k = m.start()
    print(s[max(0, k-150):k+330])
    print('---')
print()

print('=== 路由 /api/cohold_recent ===')
for m in re.finditer(r'route == "/api/cohold_recent[^"]*"', s):
    k = m.start()
    print(s[max(0, k-150):k+330])
    print('---')
print()

js = open('viewer/static/app.js', encoding='utf-8').read()
print('=== renderConsensus ===')
i = js.find('function renderConsensus')
j = js.find('\nfunction ', i + 10)
print('span %d-%d' % (i, j))
print(js[i:j][:4000])
print()
print('=== loadConsensus / CONS 变量 ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'function loadConsensus|const CONS|CONS\.', l):
        print('%5d: %s' % (n, l.strip()[:170]))
