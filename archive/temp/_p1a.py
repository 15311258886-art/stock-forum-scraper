import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.getcwd()
js = open('viewer/static/app.js', encoding='utf-8').read()
srv = open('viewer/server.py', encoding='utf-8').read()
h = open('viewer/static/index.html', encoding='utf-8').read()
ls = js.splitlines()

print('app.js lines:', len(ls))
print()

print('=== 1) 学习板块接线点 ===')
i = js.find('function learnSubItems')
if i > 0:
    print(js[i:i+600])
print()
print('  learn tab 定义:')
for n, l in enumerate(ls, 1):
    if re.search(r'tab: "(learn|kb|basic|know)\w*"', l) or 'learnSubItems' in l:
        print('%5d: %s' % (n, l.strip()[:170]))

print()
print('=== 2) 共同持仓/共同卖出接线点 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'function (renderConsensus|loadConsensus)', l):
        print('ANCHOR %5d: %s' % (n, l.strip()[:150]))

print()
print('=== 3) 后端一致 ===')
for k in ['api_consensus', 'api_cohold_recent', 'consensus_stocks', 'route == "/api/consensus']:
    print('  %-28s %d' % (k, srv.count(k)))

print()
print('=== 4) index.html panels ===')
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)])
