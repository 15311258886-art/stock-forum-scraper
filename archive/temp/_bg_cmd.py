# -*- coding: utf-8 -*-
"""用 wmic 拿进程命令行，精确区分：我的临时服务 / 工程浏览器服务 / 用户自己的浏览器。"""
import subprocess, os

def wmic(cmd):
    r = subprocess.run(['wmic'] + cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='ignore')
    return r.stdout or r.stderr or ''

print("=" * 70)
print("python 进程命令行")
print("=" * 70)
out = wmic(['process', 'where', "name='python.exe'", 'get',
            'ProcessId,ParentProcessId,CommandLine', '/format:csv'])
for ln in out.splitlines():
    ln = ln.strip()
    if not ln or ln.lower().startswith('node,'):
        continue
    if 'python' in ln.lower() or 'server.py' in ln:
        # CSV: Node,CommandLine,ParentProcessId,ProcessId
        parts = ln.split(',')
        pid = parts[-1] if parts else '?'
        ppid = parts[-2] if len(parts) > 1 else '?'
        cmd = ','.join(parts[1:-2]) if len(parts) > 3 else ln
        print("  PID %-8s PPID %-8s %s" % (pid, ppid, cmd.strip()[:150]))

print()
print("=" * 70)
print("node 进程命令行")
print("=" * 70)
out = wmic(['process', 'where', "name='node.exe'", 'get',
            'ProcessId,ParentProcessId,CommandLine', '/format:csv'])
for ln in out.splitlines():
    ln = ln.strip()
    if not ln or ln.lower().startswith('node,'):
        continue
    parts = ln.split(',')
    pid = parts[-1] if parts else '?'
    cmd = ','.join(parts[1:-2]) if len(parts) > 3 else ln
    print("  PID %-8s %s" % (pid, cmd.strip()[:170]))

print()
print("=" * 70)
print("chrome 进程命令行（只看前 12 个，判断哪些是 puppeteer）")
print("=" * 70)
out = wmic(['process', 'where', "name='chrome.exe'", 'get',
            'ProcessId,CommandLine', '/format:csv'])
n = 0
for ln in out.splitlines():
    ln = ln.strip()
    if not ln or ln.lower().startswith('node,'):
        continue
    parts = ln.split(',')
    pid = parts[-1] if parts else '?'
    cmd = ','.join(parts[1:-1]) if len(parts) > 2 else ln
    mark = ''
    low = cmd.lower()
    if 'remote-debugging' in low or 'puppeteer' in low or 'xq_' in low or '--headless' in low:
        mark = '  <<< PUPPETEER/无头'
    elif '--type=' in low:
        mark = '  (子进程)'
    print("  PID %-8s %s%s" % (pid, cmd.strip()[:130], mark))
    n += 1
    if n >= 14:
        break
