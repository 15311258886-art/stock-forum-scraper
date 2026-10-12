"""产品全貌盘点：表行数 / 接口数 / 数据新鲜度 / 前端结构。仅供生成对标名录。"""
import os, re, json, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:%s?mode=ro' % DB.replace('\\', '/'), uri=True)
con.row_factory = sqlite3.Row

print('===== 1) 表行数 =====')
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in tabs:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        print('  %-24s %s' % (t, n))
    except Exception as e:
        print('  %-24s ERR %s' % (t, str(e)[:40]))

print()
print('===== 2) 数据新鲜度 =====')
for label, sql in (
    ('龙虎榜个股', "SELECT MAX(trade_date) FROM lhb_stocks"),
    ('龙虎榜席位', "SELECT MAX(trade_date) FROM lhb_seats"),
    ('营业部', "SELECT MAX(trade_date) FROM lhb_depts"),
    ('板块扩散', "SELECT MAX(trade_date) FROM lhb_sector_focus"),
    ('股东(季报)', "SELECT MAX(end_date) FROM stock_holders"),
    ('持仓', "SELECT MAX(post_time) FROM positions WHERE site<>'jisilu'"),
    ('交易', "SELECT MAX(post_time) FROM trades"),
    ('讨论帖', "SELECT MAX(post_time) FROM posts"),
    ('资讯', "SELECT MAX(publish_time) FROM news"),
):
    try:
        print('  %-12s %s' % (label, con.execute(sql).fetchone()[0] or '—'))
    except Exception as e:
        print('  %-12s ERR %s' % (label, str(e)[:40]))
con.close()

print()
print('===== 3) 后端接口数 =====')
srv = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
routes = sorted(set(re.findall(r'route == "(/api/[a-z_/]*)"', srv)))
print('  路由数:', len(routes))
print('  ' + ', '.join(routes))

print()
print('===== 4) 前端 NAV / 面板 =====')
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
print('  app.js 行数:', len(js.splitlines()), '| index.html 行数:', len(html.splitlines()))
print('  面板 id 数:', len(re.findall(r'id="(p-[a-z_]+)"', html)))
print('  p-dashboard 存在:', 'p-dashboard' in html)
print('  renderDashboard 存在:', 'renderDashboard' in js)
print('  前端已接接口:', len(sorted(set(re.findall(r'api\("(/api/[a-z_/]+)', js)))))

print()
print('===== 5) 采集源覆盖 =====')
yaml_p = os.path.join(B, 'config', 'sites.yaml')
if os.path.exists(yaml_p):
    y = open(yaml_p, encoding='utf-8').read()
    for m in re.finditer(r'^  ([a-z_]+):', y, re.M):
        print('  site:', m.group(1))
print()
print('===== 6) 桌面快捷方式 =====')
print('  viewer lnk:', os.path.exists(r'C:\Users\野比&大熊\Desktop\股票论坛可视化查看器.lnk'))
