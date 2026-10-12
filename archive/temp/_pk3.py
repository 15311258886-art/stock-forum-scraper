import os, re, sys
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
js = open(os.path.join(B,'viewer','static','app.js'), encoding='utf-8').read()
srv = open(os.path.join(B,'viewer','server.py'), encoding='utf-8').read()
html = open(os.path.join(B,'viewer','static','index.html'), encoding='utf-8').read()

out = []
out.append('===== renderConsensus 全文 =====')
i = js.find('function renderConsensus')
if i < 0:
    out.append('NOT FOUND')
else:
    m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
    j = i + 10 + m.start() + 1 if m else i + 3000
    out.append('span %d-%d' % (i, j))
    out.append(js[i:j])

out.append('')
out.append('===== api_consensus 全文 =====')
i = srv.find('def api_consensus')
if i < 0:
    out.append('NOT FOUND')
else:
    j = srv.find('\ndef ', i+10)
    out.append(srv[i:j])

out.append('')
out.append('===== DB.cohold_recent 实现 =====')
i = srv.find('def cohold_recent')
if i < 0:
    i = srv.find('cohold_recent')
out.append(srv[max(0,i-200):i+1800] if i>0 else 'NOT FOUND')

out.append('')
out.append('===== index.html panel ids =====')
out.append(', '.join([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)]))

out.append('')
out.append('===== coholdTable 定义 =====')
i = js.find('function coholdTable')
if i >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
    j = i + 10 + m.start() + 1 if m else i + 2600
    out.append(js[i:j])

out.append('')
out.append('===== 学习组 NAV 定义 =====')
i = js.find('const NAV')
j = js.find('\n];', i)
out.append(js[i:j+3])

out.append('')
out.append('===== 学习相关函数 =====')
for n, l in enumerate(js.splitlines(), 1):
    if re.match(r'^(async )?function (loadKbTracks|ensureKbDoc|renderKbTracks|renderKbDoc)\b', l) or re.match(r'^const KB\b', l):
        out.append('%5d: %s' % (n, l.strip()[:140]))

open(os.path.join(B,'_pk3_out.txt'),'w',encoding='utf-8').write('\n'.join(out))
print('written', len('\n'.join(out)))
