# -*- coding: utf-8 -*-
"""回测 第1步：时效性 + 拉通一致性 体检（只读）"""
import sqlite3, os, sys, datetime, json

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

now = datetime.datetime.now()
print('=' * 72)
print(f'[体检时刻] {now:%Y-%m-%d %H:%M:%S}  {["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]}')
print('=' * 72)

tabs = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(f'\n[表清单] 共 {len(tabs)} 张')

# ---------- 1) 各表行数 + 时间列最新值 ----------
TIME_COLS = ['post_time', 'publish_time', 'trade_date', 'fetched_at', 'updated_at',
             'created_at', 'date', 'day', 'collected_at', 'published_at']
print('\n' + '=' * 72)
print('【1】各表规模与最新时间')
print('=' * 72)
rows_report = []
for t in tabs:
    if t.startswith('sqlite_'):
        continue
    try:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception:
        continue
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
    latest = []
    for c in cols:
        if c.lower() in TIME_COLS:
            try:
                mx = con.execute(f'SELECT MAX("{c}") FROM "{t}"').fetchone()[0]
                if mx:
                    latest.append((c, str(mx)[:19]))
            except Exception:
                pass
    rows_report.append((t, n, latest))

for t, n, latest in sorted(rows_report, key=lambda x: -x[1]):
    if n == 0:
        print(f'  {t:<26} {n:>7}   (空表)')
        continue
    ls = '  '.join(f'{c}={v}' for c, v in latest) if latest else '(无时间列)'
    print(f'  {t:<26} {n:>7}   {ls}')

# ---------- 2) 各站点最后成功时间（工程自带状态表） ----------
print('\n' + '=' * 72)
print('【2】各站点抓取状态（refresh_status）')
print('=' * 72)
try:
    for r in con.execute('SELECT * FROM refresh_status'):
        d = dict(r)
        print('  ' + json.dumps({k: (str(v)[:60] if v is not None else None)
                                 for k, v in d.items()}, ensure_ascii=False))
except Exception as e:
    print('  读取失败', e)

# ---------- 3) 业务表按日期分布：是否存在"今日缺口" ----------
print('\n' + '=' * 72)
print('【3】关键业务数据的日期分布（看是否落后于今日）')
print('=' * 72)
for tbl, col in [('lhb_stocks', 'trade_date'), ('lhb_seats', 'trade_date'),
                 ('lhb_depts', 'trade_date'), ('lhb_inst', 'trade_date'),
                 ('lhb_sector_focus', 'trade_date')]:
    try:
        rs = list(con.execute(
            f'SELECT "{col}" d, COUNT(*) n FROM "{tbl}" GROUP BY "{col}" ORDER BY "{col}" DESC LIMIT 8'))
        print(f'  {tbl:<20} ' + ' | '.join(f'{r["d"]}:{r["n"]}' for r in rs))
    except Exception as e:
        print(f'  {tbl:<20} ERR {str(e)[:60]}')

# ---------- 4) 跨表引用完整性 ----------
print('\n' + '=' * 72)
print('【4】拉通一致性（孤儿记录 / 可解析率）')
print('=' * 72)
checks = [
    ('positions 的 player 是否都在 players 表',
     "SELECT COUNT(*) FROM positions p WHERE NOT EXISTS(SELECT 1 FROM players x WHERE x.name=p.player AND x.site=p.site)"),
    ('positions 的 symbol 是否都能解析成真实A股',
     "SELECT COUNT(*) FROM positions p WHERE p.symbol IS NOT NULL AND TRIM(p.symbol)<>'' AND NOT EXISTS(SELECT 1 FROM stock_names s WHERE s.code=p.symbol)"),
    ('positions 的 site 是否都在 players 里出现过',
     "SELECT COUNT(*) FROM positions p WHERE NOT EXISTS(SELECT 1 FROM players x WHERE x.site=p.site)"),
    ('trades 的 symbol 是否都能解析',
     "SELECT COUNT(*) FROM trades t WHERE t.symbol IS NOT NULL AND TRIM(t.symbol)<>'' AND NOT EXISTS(SELECT 1 FROM stock_names s WHERE s.code=t.symbol)"),
    ('trades 的 author 是否都在 players 表',
     "SELECT COUNT(*) FROM trades t WHERE NOT EXISTS(SELECT 1 FROM players x WHERE x.name=t.author AND x.site=t.site)"),
    ('lhb_seats 的股票是否都在 lhb_stocks 同日出现过',
     "SELECT COUNT(*) FROM lhb_seats s WHERE NOT EXISTS(SELECT 1 FROM lhb_stocks k WHERE k.trade_date=s.trade_date AND k.security_code=s.security_code)"),
    ('lhb_sector_focus 上榜股是否可解析',
     "SELECT COUNT(*) FROM lhb_sector_focus"),
    ('news 的 url 是否重复',
     "SELECT COUNT(*) FROM (SELECT url FROM news GROUP BY site,url HAVING COUNT(*)>1)"),
    ('trades 是否有重复行',
     "SELECT COUNT(*) FROM (SELECT site,IFNULL(post_url,''),IFNULL(author,''),IFNULL(symbol,''),IFNULL(action,'') k FROM trades GROUP BY k HAVING COUNT(*)>1)"),
]
for label, sql in checks:
    try:
        v = con.execute(sql).fetchone()[0]
        flag = 'OK ' if v == 0 else '!! '
        print(f'  {flag}{label}: {v}')
    except Exception as e:
        print(f'  ?? {label}: ERR {str(e)[:70]}')

# ---------- 5) 字段填充率（关键字段） ----------
print('\n' + '=' * 72)
print('【5】关键字段填充率')
print('=' * 72)
for tbl, cols in [('positions', ['symbol', 'symbol_name', 'ratio', 'action', 'post_time',
                                 'hold_since', 'buy_price', 'post_url']),
                  ('trades', ['symbol', 'symbol_name', 'action', 'post_time', 'post_url']),
                  ('players', ['source_url', 'channel', 'real_holding'])]:
    try:
        n = con.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
        if n == 0:
            continue
        print(f'  --- {tbl} (n={n}) ---')
        for c in cols:
            empty = con.execute(
                f'SELECT SUM(CASE WHEN "{c}" IS NULL OR TRIM(CAST("{c}" AS TEXT))=\'\' THEN 1 ELSE 0 END) FROM "{tbl}"'
            ).fetchone()[0] or 0
            pct = (n - empty) * 100.0 / n
            print(f'      {c:<14} 有值 {n-empty:>5}  ({pct:5.1f}%)  空 {empty:>5}')
    except Exception as e:
        print(f'  {tbl} ERR {str(e)[:70]}')

# ---------- 6) 调度器状态 ----------
print('\n' + '=' * 72)
print('【6】内置调度器状态（meta.scheduler_state）')
print('=' * 72)
try:
    for r in con.execute("SELECT key, substr(value,1,400) v FROM meta"):
        if 'sched' in r['key'].lower() or 'refresh' in r['key'].lower() or 'update' in r['key'].lower():
            print(f'  {r["key"]} = {r["v"]}')
except Exception as e:
    print('  ERR', e)

con.close()
print('\n[体检完成]')
