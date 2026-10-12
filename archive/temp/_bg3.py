# -*- coding: utf-8 -*-
"""后台进程/端口审计：区分「我起的验证服务」与「王总自己的进程」，只清我起的。"""
import subprocess, csv, io, re, os, socket, json

def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

out = []
tl = sh(['tasklist', '/FO', 'CSV', '/NH']).stdout
procs = {}
for r in csv.reader(io.StringIO(tl)):
    if len(r) >= 5:
        procs[r[1]] = (r[0], r[4])

ns = sh(['netstat', '-ano']).stdout
listen = []
for ln in ns.splitlines():
    if 'LISTENING' not in ln:
        continue
    p = ln.split()
    if len(p) < 4:
        continue
    addr, pid = p[1], p[-1]
    m = re.search(r':(\d+)$', addr)
    if not m:
        continue
    listen.append((int(m.group(1)), pid, addr))

WATCH = {8765, 8787, 8788, 8899, 8799, 8791, 53269}
out.append('=== 监听端口（关注集）===')
for port, pid, addr in sorted(set(listen)):
    if port in WATCH:
        nm, mem = procs.get(pid, ('?', '?'))
        out.append('  %-24s port=%-6s pid=%-7s %s mem=%s' % (addr, port, pid, nm, mem))
out.append('')
out.append('=== 所有 python/node 进程 ===')
for pid, (nm, mem) in procs.items():
    if nm.lower() in ('python.exe', 'pythonw.exe', 'node.exe'):
        out.append('  pid=%-7s %-12s mem=%s' % (pid, nm, mem))

# 命令行（用 wmic，失败则跳过）
out.append('')
out.append('=== 进程命令行 ===')
w = sh(['wmic', 'process', 'where', "name='python.exe' or name='pythonw.exe'", 'get', 'ProcessId,CommandLine', '/format:csv'])
txt = (w.stdout or '') + (w.stderr or '')
for ln in txt.splitlines():
    if 'python' in ln.lower() or '.py' in ln:
        out.append('  ' + ln.strip()[:230])

res = '\n'.join(out)
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_bg3_out.txt'), 'w', encoding='utf-8').write(res)
print(res)
