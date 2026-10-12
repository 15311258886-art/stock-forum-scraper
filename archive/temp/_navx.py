import os, re, json
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B,'viewer','static','app.js'), encoding='utf-8').read()
ls = js.splitlines()
out = []

out.append('TOTAL %d' % len(ls))
out.append('')

# 1) NAV 定义
i = js.find('const NAV')
if i >= 0:
    j = js.find('\n];', i)
    out.append('===== NAV 定义 (%d~%d) =====' % (ls.index(js[i:i+30].split('\n')[0])+1 if js[i:i+30].split('\n')[0] in ls else 0, 0))
    out.append(js[i:j+2])
out.append('')

# 2) 子导航渲染函数
for name in ['renderSubNav','subNavHtml','buildSub','selectNav','ensurePanel']:
    k = js.find('function ' + name)
    if k >= 0:
        out.append('===== %s =====' % name)
        out.append(js[k:k+1200])
        out.append('')

# 3) 面板渲染函数（待整合的）
for name in ['renderNiusan','renderStarMoves','renderLhb','renderLhbSectors','renderConsensus','renderOperating','renderPlayers']:
    k = js.find('function ' + name)
    if k >= 0:
        out.append('===== %s @line %d =====' % (name, js[:k].count('\n')+1))
        out.append(js[k:k+300])
        out.append('...')
        out.append('')

# 4) loadAll / 初始加载
k = js.find('function loadAll')
if k >= 0:
    out.append('===== loadAll =====')
    out.append(js[k:k+1600])
    out.append('')

# 5) 面板 id 清单
h = open(os.path.join(B,'viewer','static','index.html'), encoding='utf-8').read()
out.append('===== index.html panel ids =====')
out.append(', '.join([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]))

open(os.path.join(B,'_navx_out.txt'),'w',encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:9500])
