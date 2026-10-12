# -*- coding: utf-8 -*-
import os
lines = []
def L(s=''):
    lines.append(str(s))

PROJ = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
VIEW = os.path.join(PROJ, 'viewer')
WKS  = r'F:\AI Agent部署知识库\2026-09-25-08-34-41'
DESK = r'C:\Users\野比&大熊\Desktop'

for tag, base in [('PROJECT-ROOT', PROJ), ('VIEWER', VIEW)]:
    L('===== %s : %s =====' % (tag, base))
    if not os.path.isdir(base):
        L('MISSING'); L(); continue
    for f in sorted(os.listdir(base)):
        p = os.path.join(base, f)
        if os.path.isdir(p):
            L('  <DIR>  %s' % f)
        else:
            try:
                L('  %9d B  %s' % (os.path.getsize(p), f))
            except Exception as e:
                L('  ERR %s  %s' % (e, f))
    L()

L('===== 0-byte files =====')
SKIP = {'.venv', 'node_modules', '__pycache__', '.git', '.browser-profile', '_edgeprofile'}
n0 = 0
for base in [PROJ, WKS, DESK]:
    if not os.path.isdir(base):
        continue
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            if sz == 0:
                n0 += 1
                L('  0B  %s' % p)
L('  total=%d' % n0)
L()

L('===== basename starts with AI =====')
for base in [PROJ, WKS, DESK, r'F:\\']:
    if not os.path.isdir(base):
        continue
    try:
        entries = os.listdir(base)
    except Exception:
        continue
    for f in entries:
        if f.upper().startswith('AI'):
            p = os.path.join(base, f)
            try:
                s = 'DIR' if os.path.isdir(p) else (str(os.path.getsize(p)) + 'B')
            except Exception:
                s = '?'
            L('  %-8s %s' % (s, p))

open(os.path.join(PROJ, '_scan0.txt'), 'w', encoding='utf-8').write('\n'.join(lines))
print('DONE', len(lines))
