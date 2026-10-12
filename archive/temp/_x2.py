import sys, os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
ls = open('viewer/static/app.js', encoding='utf-8').read().splitlines()

def dump(a, b, tag):
    print('========== %s (行 %d-%d) ==========' % (tag, a, b))
    for i in range(a-1, min(b, len(ls))):
        print('%5d: %s' % (i+1, ls[i]))
    print()

dump(1636, 1712, 'loadNiusan + renderNiusan')
dump(1836, 1935, 'STARM + loadStarMoves + renderStarMoves')
