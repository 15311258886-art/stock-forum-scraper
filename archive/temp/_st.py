import re, os, sqlite3
js = open('viewer/static/app.js', encoding='utf-8').read()
h  = open('viewer/static/index.html', encoding='utf-8').read()
srv= open('viewer/server.py', encoding='utf-8').read()

print('=== 前端接线状态 ===')
for k in ['api_dashboard', 'renderDashboard', 'tab: "dashboard"', 'p-dashboard']:
    print('  app.js  %-22s %d' % (k, js.count(k)))
for k in ['api_niusan', 'renderNiusan', 'tab: "niusan"', 'p-niusan']:
    print('  app.js  %-22s %d' % (k, js.count(k)))
ids = re.findall(r'id="(p-[a-z_]+)"', h)
print('  index panels:', len(ids))
print('  p-dashboard:', 'p-dashboard' in ids, '| p-niusan:', 'p-niusan' in ids)
print()
print('=== 后端接口/路由 ===')
for k in ['def api_dashboard', 'route == "/api/dashboard"', 'def api_niusan', 'route == "/api/niusan"', 'def api_lhb_sectors', 'route == "/api/lhb_sectors"']:
    print('  %-30s %d' % (k, srv.count(k)))
print()
print('=== 数据现状 ===')
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
for t in ['lhb_stocks', 'lhb_sector_focus', 'stock_holders', 'players', 'positions', 'trades']:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        print('  %-20s %s' % (t, n))
    except Exception as e:
        print('  %-20s ERR' % t)
print('  龙虎榜日期:', [r[0] for r in con.execute('SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC LIMIT 8')])
print('  板块扩散日期:', [r[0] for r in con.execute('SELECT DISTINCT trade_date FROM lhb_sector_focus ORDER BY trade_date DESC')])
print('  股东新进条数:', con.execute("SELECT COUNT(*) FROM stock_holders WHERE hold_change='新进'").fetchone()[0])
con.close()
