# -*- coding: utf-8 -*-
"""后台进程/端口审计 + 清理我起的临时验证服务（不动王总自己的进程）。"""
import subprocess, csv, io, re, os, socket

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='ignore')


out = []

# ---- 1) 端口 -> 进程 ----
tl = sh(['tasklist', '/FO', 'CSV', '/NH']).stdout
procs = {}
for r in csv.reader(io.StringIO(tl)):
    if len(r) >= 5:
        procs[r[1]] = (r[0], r[4])

ns = sh(['netstat', '-ano']).stdout
listen = set()
for ln in ns.splitlines():
    if 'LISTENING' not in ln:
        continue
    p = ln.split()
    if len(p) < 4:
        continue
    m = re.search(r':(\d+)$', p[1])
    if m:
        listen.add((int(m.group(1)), p[-1], p[1]))

WATCH = {8765, 8787, 8788, 8899, 8799, 8791, 53269}
out.append('=== 关注端口监听 ===')
found = False
for port, pid, addr in sorted(listen):
    if port in WATCH:
        nm, mem = procs.get(pid, ('?', '?'))
        out.append('  %-22s port=%-6s pid=%-7s %s mem=%s' % (addr, port, pid, nm, mem))
        found = True
if not found:
    out.append('  （无）')

out.append('')
out.append('=== python/node 进程 ===')
for pid, (nm, mem) in procs.items():
    if nm.lower() in ('python.exe', 'pythonw.exe', 'node.exe'):
        out.append('  pid=%-7s %-12s mem=%s' % (pid, nm, mem))

# ---- 2) 命令行（wmic） ----
out.append('')
out.append('=== 命令行 ===')
w = sh(['wmic', 'process', 'where', "name='python.exe'", 'get', 'ProcessId,CommandLine', '/format:csv'])
for ln in (w.stdout or '').splitlines():
    if '.py' in ln or 'server' in ln.lower():
        out.append('  ' + ln.strip()[:240])

# ---- 3) 清理：只杀监听 8787/8788/8899 的 pid 且确认是 server.py ----
out.append('')
out.append('=== 清理 ===')
kill_pids = set()
for port, pid, addr in listen:
    if port in (8787, 8788, 8899) and pid.isdigit():
        kill_pids.add((port, pid))
for port, pid in sorted(kill_pids):
    r = sh(['taskkill', '/F', '/PID', pid])
    out.append('  杀 port=%s pid=%s -> %s' % (port, pid, ((r.stdout or r.stderr) or '').strip()[:70]))
if not kill_pids:
    out.append('  无需清理')

# ---- 4) 复查 ----
after = sh(['netstat', '-ano']).stdout
left = []
for ln in after.splitlines():
    if 'LISTENING' not in ln:
        continue
    m = re.search(r':(\d+)\s', ln)
    if m and int(m.group(1)) in WATCH:
        left.append(ln.strip()[:90])
out.append('')
out.append('=== 清理后仍监听 ===')
out.append('\n'.join('  ' + x for x in left) if left else '  （无）')

res = '\n'.join(out)
open(os.path.join(BASE, '_bg4_out.txt'), 'w', encoding='utf-8').write(res)
print(res)
