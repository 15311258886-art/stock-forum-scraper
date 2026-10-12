import os, re
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B,'viewer','static','app.js'), encoding='utf-8').read()
out = []

# selectNav full
i = js.find('function selectNav')
j = js.find('\n}\n', i)
out.append('===== selectNav FULL =====')
out.append(js[i:j+2])
out.append('')

for name in ['loadStarMoves','loadNiusan','loadLhb','loadLhbSectors','renderStocks','renderSentiment']:
    k = js.find('function ' + name)
    if k < 0:
        out.append('### %s NOT FOUND' % name); continue
    e = js.find('\n}\n', k)
    out.append('===== %s =====' % name)
    out.append(js[k:e+2])
    out.append('')

# subnav 相关 CSS
css = open(os.path.join(B,'viewer','static','app.css'), encoding='utf-8').read()
out.append('===== subnav CSS =====')
for m in re.finditer(r'[^\n{}]*\.(subnav|sub-tab|subnav-spacer|subnav-group)[^{]*\{[^}]*\}', css):
    out.append(m.group(0))
out.append('')
out.append('subnav-group exists: %s' % ('subnav-group' in css))

# loadAll 中 star_moves / niusan 相关
out.append('')
out.append('===== loadAll 中 niusan/star_moves/lhb 行 =====')
i = js.find('function loadAll')
j = js.find('\n}\n', i)
seg = js[i:j]
for n,l in enumerate(seg.splitlines(),1):
    if 'niusan' in l.lower() or 'star' in l.lower() or 'lhb' in l.lower() or 'NIU' in l or 'STAR' in l or 'LHB' in l:
        out.append('  %d: %s' % (n, l.strip()))

open(os.path.join(B,'_paint2_out.txt'),'w',encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:11000])
