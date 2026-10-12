import os, re
B=r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js=open(os.path.join(B,'viewer','static','app.js'),encoding='utf-8').read()
ls=js.splitlines()
out=[]
out.append('TOTAL %d' % len(ls))
out.append('')
out.append('=== niusan 相关行 ===')
for i,l in enumerate(ls,1):
    if re.search(r'niusan|NIUSAN|牛散', l, re.I):
        out.append('%5d: %s' % (i, l.rstrip()[:175]))
out.append('')
out.append('=== api_star_moves / star_moves 相关 ===')
for i,l in enumerate(ls,1):
    if 'star_moves' in l or 'STAR_MOVE' in l:
        out.append('%5d: %s' % (i, l.rstrip()[:175]))
out.append('')
out.append('=== renderNiusan 全文 ===')
i=js.find('function renderNiusan')
if i>0:
    j=js.find('\nfunction ', i+10)
    out.append(js[i:j if j>0 else i+3000])
open(os.path.join(B,'_v6_out.txt'),'w',encoding='utf-8').write('\n'.join(out))
print('OK', len(out))
