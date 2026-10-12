import os
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B,'viewer','static','app.js'), encoding='utf-8').read()
out = []

k = js.find('function paintSubnav')
out.append('===== paintSubnav @%d =====' % (js[:k].count('\n')+1))
out.append(js[k:k+1500])
out.append('')

k = js.find('function renderNav')
out.append('===== renderNav @%d =====' % (js[:k].count('\n')+1))
out.append(js[k:k+1200])
out.append('')

# renderNiusan full
i = js.find('function renderNiusan')
j = js.find('\nfunction ', i+10)
out.append('===== renderNiusan FULL (len %d) =====' % (j-i))
out.append(js[i:j])
out.append('')

# renderStarMoves full
i = js.find('function renderStarMoves')
j = js.find('\nfunction ', i+10)
out.append('===== renderStarMoves FULL (len %d) =====' % (j-i))
out.append(js[i:j])
out.append('')

# STARM / NIUSAN state defs
for name in ['const STARM','const NIUSAN','let STARM','let NIUSAN']:
    i = js.find(name)
    if i>=0:
        j = js.find(';', i)
        out.append('%s => %s' % (name, js[i:j+1]))

open(os.path.join(B,'_paint_out.txt'),'w',encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:12000])
