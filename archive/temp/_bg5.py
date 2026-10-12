# -*- coding: utf-8 -*-
"""后台进程/端口审计 + 清理临时验证服务（不动王总的查看器 8765）。无 wmic 依赖。"""
import subprocess, csv, io, re, os

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='ignore')


out = []

# 1) 端口 -> 进程
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
    m = re.search(r':(\d+)$', p[1])
    if m:
        listen.append((int(m.group(1)), p[-1], p[1]))

WATCH = {8765, 8787, 8788, 8899, 8799, 8791, 8786}
out.append('=== 关注端口监听 ===')
for port, pid, addr in sorted(listen):
    if port in WATCH:
        nm, mem = procs.get(pid, ('?', '?'))
        out.append('  %-22s port=%-6s pid=%-7s %s mem=%s' % (addr, port, pid, nm, mem))

out.append('')
out.append('=== 全部 python / node 进程 ===')
for pid, (nm, mem) in sorted(procs.items(), key=lambda kv: kv[0]):
    if nm.lower() in ('python.exe', 'pythonw.exe', 'node.exe'):
        out.append('  pid=%-7s %-12s mem=%s' % (pid, nm, mem))

# 2) 清理：只杀 8787/8788/8899 的监听进程（我自己起的临时验证服务）
out.append('')
out.append('=== 清理临时验证服务（8787/8788/8899）===')
kill_ports = []
for port, pid, addr in listen:
    if port in (8787, 8788, 8899) and pid.isdigit():
        kill_ports.append((port, pid))
seen = set()
for port, pid in sorted(set(kill_ports)):
    r = sh(['taskkill', '/F', '/PID', pid])
    out.append('  杀 port=%s pid=%s -> %s' % (port, pid, ((r.stdout or r.stderr) or '').strip()[:80]))
    seen.add(pid)
if not kill_ports:
    out.append('  无需清理')

# 3) 复查
after = sh(['netstat', '-ano']).stdout
left = []
for ln in after.splitlines():
    if 'LISTENING' not in ln:
        continue
    m = re.search(r':(\d+)\s', ln)
    if m and int(m.group(1)) in WATCH:
        left.append(ln.strip()[:95])
out.append('')
out.append('=== 清理后仍监听 ===')
out.append('\n'.join('  ' + x for x in left) if left else '  （无）')

# 4) 8799 归属推断（浏览器常驻服务，不杀）
out.append('')
out.append('=== 8799 说明 ===')
for port, pid, addr in listen:
    if port == 8799:
        nm, mem = procs.get(pid, ('?', '?'))
        out.append('  8799 由 %s (pid=%s) 监听 —— 这是 browser_fetch 的常驻浏览器服务，'
                   '抓雪球时会复用，属正常' % (nm, pid))

res = '\n'.join(out)
open(os.path.join(BASE, '_bg5_out.txt'), 'w', encoding='utf-8').write(res)
print(res)
