# -*- coding: utf-8 -*-
"""后台任务与端口审计：查清哪些进程/端口还在、那个失败的 8799 是什么。"""
import subprocess, csv, io, socket, os, time

print("=" * 66)
print("① 端口监听状态")
print("=" * 66)
ports = [8765, 8787, 8788, 8799, 8791, 8899]
ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
listen_map = {}
for ln in ns.splitlines():
    if 'LISTENING' not in ln:
        continue
    parts = ln.split()
    if len(parts) < 4:
        continue
    local, pid = parts[1], parts[-1]
    port = local.rsplit(':', 1)[-1]
    if port.isdigit():
        listen_map.setdefault(int(port), set()).add(pid)

for p in ports:
    tag = "LISTENING pid=%s" % ",".join(listen_map[p]) if p in listen_map else "未监听"
    print("  %-6s %s" % (p, tag))

print()
print("=" * 66)
print("② python / node / chrome 进程明细")
print("=" * 66)
tl = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                    text=True, encoding='utf-8', errors='ignore').stdout
rows = list(csv.reader(io.StringIO(tl)))
targets = ('python.exe', 'pythonw.exe', 'node.exe', 'chrome.exe', 'msedgewebview2.exe')
found = []
for r in rows:
    if len(r) >= 2 and r[0].lower() in targets:
        found.append((r[0], r[1], r[4] if len(r) > 4 else ''))
for nm, pid, mem in found:
    print("  %-20s PID %-8s mem %s" % (nm, pid, mem))
print("  合计 %d 个" % len(found))

print()
print("=" * 66)
print("③ 每个监听端口归属的进程名")
print("=" * 66)
pmap = {}
for r in rows:
    if len(r) >= 2 and r[1].isdigit():
        pmap[r[1]] = r[0]
for p in ports:
    if p in listen_map:
        for pid in listen_map[p]:
            print("  port %-6s pid %-8s %s" % (p, pid, pmap.get(pid, '?')))

print()
print("=" * 66)
print("④ 那个失败的 8799 任务 —— 有没有留下日志")
print("=" * 66)
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
cands = ['_isolated_viewer.log', 'viewer_8799.log', '_restart_viewer.log',
         'viewer_restart.log', 'restart_viewer.log', 'server.log']
for f in cands:
    for base in (B, os.path.join(B, 'viewer')):
        p = os.path.join(base, f)
        if os.path.exists(p):
            m = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(p)))
            print("  %-34s %8s B  %s" % (os.path.relpath(p, B), os.path.getsize(p), m))

print()
print("=" * 66)
print("⑤ 最近 2 小时内改动的日志文件")
print("=" * 66)
now = time.time()
for root, dirs, files in os.walk(B):
    dirs[:] = [d for d in dirs if d not in ('.venv', 'node_modules', '__pycache__',
                                            '.git', '.browser-profile', 'artifacts')]
    for f in files:
        if not f.endswith(('.log', '.txt')):
            continue
        p = os.path.join(root, f)
        age = (now - os.path.getmtime(p)) / 60.0
        if age < 150:
            print("  %6.1f 分钟前  %-44s %s B" % (age, os.path.relpath(p, B), os.path.getsize(p)))
