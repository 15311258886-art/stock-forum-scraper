import sys, os, re, json, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

js = open('viewer/static/app.js', encoding='utf-8').read()
srv = open('viewer/server.py', encoding='utf-8').read()
html = open('viewer/static/index.html', encoding='utf-8').read()

print('=== 1) NAV 结构 ===')
i = js.find('const NAV'); j = js.find('\n];', i)
print(js[i:j+3] if i >= 0 else 'NAV NOT FOUND')

print()
print('=== 2) learn 相关（子项/按需渲染/面板） ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'learn|学习|zdx', l, re.I):
        print('%5d: %s' % (n, l.strip()[:170]))

print()
print('=== 3) index.html 面板 ===')
print(' panels:', [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)])

print()
print('=== 4) 已有 strategy 痕迹 ===')
for k in ['strategy', '策略', 'simulate', 'backtest', '模拟']:
    print('  app.js %-12s %d | server.py %d' % (k, js.count(k), srv.count(k)))

print()
print('=== 5) 数据可用性 ===')
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
for t in ['positions', 'lhb_stocks', 'lhb_sector_focus', 'trades', 'stock_holders', 'kline_cache', 'strategy_sim']:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        print('  %-20s %s' % (t, n))
    except Exception as e:
        print('  %-20s MISSING' % t)
print('  持仓代码数:', con.execute('SELECT COUNT(DISTINCT symbol) FROM positions').fetchone()[0])
print('  龙虎榜代码数:', con.execute('SELECT COUNT(DISTINCT security_code) FROM lhb_stocks').fetchone()[0])
con.close()

print()
print('=== 6) 本地 K 线缓存文件 ===')
for f in os.listdir(B):
    if 'kline' in f.lower() or 'cache' in f.lower():
        print('  %-40s %s' % (f, os.path.getsize(os.path.join(B, f))))

print()
print('=== 7) lhb_sector_link 可复用函数 ===')
src = open('lhb_sector_link.py', encoding='utf-8').read()
for i2, l in enumerate(src.splitlines(), 1):
    if re.match(r'^def ', l) or re.match(r'^[A-Z_]+ =', l):
        print('  %4d: %s' % (i2, l.strip()[:110]))

print()
print('=== 8) 8765 是否在跑 ===')
import socket
s = socket.socket(); s.settimeout(0.8)
print('  8765 listening:', s.connect_ex(('127.0.0.1', 8765)) == 0)
s.close()
