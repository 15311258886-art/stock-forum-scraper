# -*- coding: utf-8 -*-
"""核对导航归拢结果：语法 + 面板一致性 + 函数完整性"""
import sys, re, os, subprocess, socket
sys.stdout.reconfigure(encoding='utf-8')

B = os.getcwd()
js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()
h = open('viewer/static/index.html', encoding='utf-8').read()

print('app.js lines:', len(ls))
print()

# 1) NAV 结构
i = js.find('const NAV')
j = js.find('\n];', i)
print('=== NAV 定义 ===')
print(js[i:j+3])
print()

# 2) tab vs panel
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
tabs = sorted(set(re.findall(r'\{ tab: "([a-z_]+)"', js)))
print('index panels (%d): %s' % (len(ids), ids))
print('NAV tabs (%d): %s' % (len(tabs), tabs))
missing = [t for t in tabs if ('p-' + t) not in ids]
print('缺面板的 tab:', missing or '无')
print()

# 3) 关键函数完整性（归拢后应存在）
print('=== 关键函数/常量（含括号计数） ===')
def fn_ok(name):
    pat = r'(?:async )?function ' + re.escape(name) + r'\b'
    m = re.search(pat, js)
    if not m:
        return None
    start = m.start()
    # 从函数起找到配平的 }
    k = js.find('{', start)
    if k < 0:
        return None
    depth = 0
    for p in range(k, min(len(js), k + 200000)):
        if js[p] == '{':
            depth += 1
        elif js[p] == '}':
            depth -= 1
            if depth == 0:
                return (p - start)
    return -1

for name in ['renderNiusan','renderNsRank','renderNsMoves','loadNiusan','renderDashboard','loadDashboard','selectNav','ensurePanel']:
    n = fn_ok(name)
    print('  %-18s %s' % (name, ('OK len=%d' % n) if isinstance(n, int) and n > 0 else ('MISSING' if n is None else 'UNBALANCED')))

print()
# 4) 残留检查
print('=== 残留检查 ===')
for k in ['renderStarMoves', 'tab: "star_moves"', 'star_moves']:
    print('  %-22s %d' % (k, js.count(k)))
print()
print('=== 关键接线 ===')
for k in ['renderNsMoves', 'ns-tab', 'niusan_view', 'mainlines', 'ml-row-fwd']:
    print('  %-22s %d' % (k, js.count(k)))
print()

# 5) 进程
print('=== 8765 进程 ===')
s = socket.socket(); s.settimeout(0.8)
print('  8765 listening:', s.connect_ex(('127.0.0.1', 8765)) == 0)
s.close()
tl = subprocess.run(['tasklist','/FO','CSV','/NH'], capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
for r in csv.reader(io.StringIO(tl)) if False else []:
    pass
import csv, io
for r in csv.reader(io.StringIO(tl)):
    if len(r) >= 2 and r[0].lower() in ('python.exe','pythonw.exe','node.exe'):
        print('  ', r[0], 'PID', r[1], 'mem', r[4] if len(r) > 4 else '')
