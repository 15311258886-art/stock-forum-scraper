# -*- coding: utf-8 -*-
"""一次性导出：进程归属 + NAV 结构 + dashboard 渲染 + sector_chain 产出"""
import os, re, json, sqlite3, subprocess, csv, io

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)
out = []

# ---------- 1) 进程归属 ----------
out.append('===== 1) 残留进程归属 =====')
tl = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                    encoding='utf-8', errors='ignore').stdout
# 用 powershell 拿命令行（wmic 已移除）
ps = ("Get-CimInstance Win32_Process -Filter \"name='node.exe' or name='python.exe'\" | "
      "ForEach-Object { \"{0}|{1}\" -f $_.ProcessId, ($_.CommandLine -replace \"[`r`n]\",\" \") }")
try:
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                       text=True, encoding='utf-8', errors='ignore', timeout=60)
    for ln in (r.stdout or '').splitlines():
        if '|' in ln:
            out.append('  ' + ln.strip()[:190])
except Exception as e:
    out.append('  PS ERR ' + str(e)[:80])
out.append('')

# ---------- 2) NAV 结构 ----------
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
out.append('===== 2) NAV 完整定义 =====')
i = js.find('const NAV')
if i >= 0:
    j = js.find('\n];', i)
    out.append(js[i:j+3])
out.append('')

# ---------- 3) dashboard 渲染 ----------
ls = js.splitlines()
out.append('===== 3) 关键函数行号 =====')
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (renderDashboard|loadDashboard|renderChain|renderMainline|renderLhb|renderLhbSectors|renderNiusan|renderStarMoves)\b', l):
        out.append('%5d: %s' % (n, l.strip()[:130]))
out.append('')

# ---------- 4) sector_chain 产出 ----------
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%chain%' OR name LIKE '%mainline%' OR name LIKE '%leader%')")]
out.append('===== 4) 产业链相关表 ===== ' + str(tabs))
for t in tabs:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
        out.append('  %-24s n=%-5s %s' % (t, n, cols))
        for r in con.execute('SELECT * FROM "%s" LIMIT 3' % t):
            d = dict(r)
            out.append('     ' + json.dumps({k: (str(v)[:60] if v is not None else None) for k, v in d.items()}, ensure_ascii=False)[:400])
    except Exception as e:
        out.append('  %s ERR %s' % (t, str(e)[:60]))
con.close()

open(os.path.join(B, '_s9_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:9000])
