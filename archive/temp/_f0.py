import os, subprocess, csv, io, socket, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
print('=== python/node 进程 ===')
tl = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
for r in csv.reader(io.StringIO(tl)):
    if len(r) >= 2 and r[0].lower() in ('python.exe', 'pythonw.exe', 'node.exe'):
        print('  ', r[0], 'PID', r[1], 'mem', r[4] if len(r) > 4 else '')

print()
print('=== 监听端口 ===')
ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
for ln in ns.splitlines():
    if 'LISTENING' in ln:
        parts = ln.split()
        if len(parts) >= 2 and parts[1].startswith(('127.0.0.1:', '0.0.0.0:')):
            port = parts[1].rsplit(':', 1)[-1]
            if port in ('8765', '8787', '8788', '8899'):
                print('  ', ln.strip()[:90])

print()
DB = os.path.join(B, 'viewer', 'app.db')
print('=== DB 占用测试（能否独占写）===')
try:
    con = sqlite3.connect(DB, timeout=3)
    con.execute('BEGIN IMMEDIATE')
    con.rollback(); con.close()
    print('  可独占写 OK')
except Exception as e:
    print('  写锁冲突:', type(e).__name__, str(e)[:80])

print()
print('=== 现有 posts/news/trades 列 ===')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
for t in ['posts', 'news', 'trades']:
    print(' ', t, [r[1] for r in con.execute('PRAGMA table_info(%s)' % t)])
con.close()
