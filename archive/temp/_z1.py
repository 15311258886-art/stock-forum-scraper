# -*- coding: utf-8 -*-
"""进程/端口归属审计（纯 Python，不依赖 wmic/ps）"""
import os, subprocess, csv, io, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'

def run(args, enc='utf-8'):
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding=enc, errors='ignore')
        return (r.stdout or '') + (r.stderr or '')
    except Exception as e:
        return 'ERR ' + str(e)

print('=== 监听端口 -> PID ===')
ns = run(['netstat', '-ano'])
watch = {'8765', '8787', '8788', '8899', '8799', '8791'}
for ln in ns.splitlines():
    if 'LISTENING' not in ln:
        continue
    parts = ln.split()
    if len(parts) < 5:
        continue
    local = parts[1]
    port = local.rsplit(':', 1)[-1]
    if port in watch:
        print('  %-22s PID=%s' % (local, parts[-1]))

print()
print('=== python/node 进程 ===')
tl = run(['tasklist', '/FO', 'CSV', '/NH'])
rows = list(csv.reader(io.StringIO(tl)))
for r in rows:
    if len(r) >= 2 and r[0].lower() in ('python.exe', 'pythonw.exe', 'node.exe', 'chrome.exe'):
        print('  %-14s PID=%-8s mem=%s' % (r[0], r[1], r[4] if len(r) > 4 else ''))

print()
print('=== _p3.py 内容 ===')
p = os.path.join(B, '_p3.py')
if os.path.exists(p):
    print(open(p, encoding='utf-8').read())
else:
    print('MISSING')
