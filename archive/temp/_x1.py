import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()

print('app.js TOTAL', len(ls))
print()

# 1) 导航结构
i = js.find('const NAV')
j = js.find('\n];', i)
print('=== NAV 定义 (span %d-%d) ===' % (i, j))
print(js[i:j+3])
print()

# 2) 关键函数定位
print('=== 关键函数/常量 ===')
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (renderNiusan|loadNiusan|renderStarMoves|loadStarMoves|selectNav|renderSubNav|ensurePanel|renderDashboard|loadDashboard)\b', l) or re.match(r'^const (NIU|STARM|LHB|LBHS|DASH)\b', l):
        print('%5d: %s' % (n, l.strip()[:150]))
print()

# 3) 面板 id
h = open('viewer/static/index.html', encoding='utf-8').read()
print('=== index.html 面板 id ===')
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)])
print()

# 4) 持仓 chip 现状
print('=== 持仓 chip 现状 ===')
for n, l in enumerate(ls, 1):
    if 'chip-time' in l or 'hls.slice' in l or 'hlShown' in l:
        print('%5d: %s' % (n, l.strip()[:180]))
