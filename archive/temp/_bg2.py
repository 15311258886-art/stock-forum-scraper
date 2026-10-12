# -*- coding: utf-8 -*-
"""后台进程/端口审计：谁在占端口、是不是我起的、能不能安全清掉。"""
import os, re, subprocess, csv, io, socket, datetime

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_bg2_out.txt')
L = []
def p(*a):
    L.append(' '.join(str(x) for x in a))

p('='*66)
p('后台进程 / 端口审计')
p('='*66)

# 1) 全部 python / node 进程（含 PID、内存、启动时间）
tl = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
pmap = {}
for row in csv.reader(io.StringIO(tl)):
    if len(row) >= 5 and row[0].lower() in ('python.exe', 'pythonw.exe', 'node.exe', 'chrome.exe'):
        pmap[row[1]] = (row[0], row[4])

p()
p('--- 相关进程 ---')
for pid, (name, mem) in sorted(pmap.items(), key=lambda kv: kv[1][0]):
    p('  %-12s PID %-8s mem %s' % (name, pid, mem))

# 2) 监听端口 -> PID
ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
p()
p('--- 监听端口（本机） ---')
for ln in ns.splitlines():
    if 'LISTENING' not in ln:
        continue
    parts = ln.split()
    if len(parts) < 4:
        continue
    local, pid = parts[1], parts[-1]
    m = re.search(r':(\d+)$', local)
    if not m:
        continue
    port = int(m.group(1))
    if pid in pmap or port in (8765, 8787, 8788, 8899, 8799, 8791):
        nm = pmap.get(pid, ('?', ''))[0]
        p('  %-26s pid=%-8s %s' % (local, pid, nm))

# 3) 关键端口探活
p()
p('--- 关键端口探活 ---')
for port in (8765, 8787, 8788, 8899, 8799):
    s = socket.socket(); s.settimeout(0.6)
    ok = s.connect_ex(('127.0.0.1', port)) == 0
    s.close()
    p('  %-6s %s' % (port, 'LISTENING' if ok else '空闲'))

# 4) 用 wmic 取命令行（判断是不是我起的临时验证服务）
p()
p('--- 进程命令行（wmic） ---')
r = subprocess.run(['wmic', 'process', 'where',
                    "name='python.exe' or name='node.exe'",
                    'get', 'ProcessId,CommandLine', '/format:csv'],
                   capture_output=True, text=True, encoding='utf-8', errors='ignore')
lines = [x for x in (r.stdout or '').splitlines() if x.strip()]
for ln in lines[:40]:
    p('  ' + ln[:220])

open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
print('\n'.join(L))
print('\nWROTE', OUT)
