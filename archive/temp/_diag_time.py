# -*- coding: utf-8 -*-
"""诊断：识别 post_time 中"实为抓取时刻"的伪时间（只读，不改数据）。"""
import sqlite3
from datetime import datetime, timedelta

DB = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db'
con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row


def p(s=''):
    print(s)


p('=== 各站点 raw_pages 抓取时刻（全部） ===')
fetch = {}
for r in con.execute("SELECT site, fetched_at FROM raw_pages ORDER BY fetched_at"):
    fetch.setdefault(r['site'], []).append(r['fetched_at'])
for s, arr in sorted(fetch.items()):
    p(f'  {s:<10} n={len(arr):<4} {arr[0]} ~ {arr[-1]}')
    # 打印按小时聚合
    hrs = {}
    for t in arr:
        hrs[t[:13]] = hrs.get(t[:13], 0) + 1
    p(f'             按小时: {dict(sorted(hrs.items()))}')


def parse(t):
    try:
        return datetime.strptime(str(t)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        try:
            return datetime.strptime(str(t)[:10], '%Y-%m-%d')
        except Exception:
            return None


def is_fetch_time(site, t, tol_sec=180):
    """post_time 是否落在该站点任一抓取时刻 ±tol_sec 内"""
    dt = parse(t)
    if dt is None:
        return None
    for f in fetch.get(site, []):
        fd = parse(f)
        if fd and abs((dt - fd).total_seconds()) <= tol_sec:
            return f
    return False


p()
p('=== trades: 伪时间识别 ===')
for r in con.execute("SELECT site, COUNT(*) n FROM trades GROUP BY site"):
    pass
tot_fake = {}
for r in con.execute("SELECT id, site, author, post_time, symbol, symbol_name FROM trades ORDER BY id"):
    res = is_fetch_time(r['site'], r['post_time'])
    if res and res is not False:
        tot_fake.setdefault(r['site'], []).append((r['id'], r['post_time'], r['author'], res))
p('  [伪] 命中抓取时刻的记录：')
for s, arr in tot_fake.items():
    for i, t, a, f in arr:
        p(f'    id={i} {s} {t}  (抓取 {f})  {a}')
p(f'  合计伪时间 {sum(len(v) for v in tot_fake.values())} 条 / trades 总 {con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]} 条')

p()
p('=== positions: 时间形态 ===')
for r in con.execute("""SELECT site,
      SUM(CASE WHEN post_time IS NULL OR TRIM(post_time)='' THEN 1 ELSE 0 END) empty,
      SUM(CASE WHEN post_time NOT LIKE '%:%' AND TRIM(post_time)<>'' THEN 1 ELSE 0 END) date_only,
      COUNT(*) n, MIN(post_time) mn, MAX(post_time) mx
      FROM positions GROUP BY site"""):
    p(f"  {r['site']:<10} n={r['n']:<5} 空={r['empty']:<4} 仅日期={r['date_only']:<5} {r['mn']} ~ {r['mx']}")

p()
p('=== news: 伪时间识别（同口径） ===')
nf = 0
nall = 0
samples = []
cols = [x[1] for x in con.execute('PRAGMA table_info(news)')]
tcol = 'publish_time' if 'publish_time' in cols else None
p(f'  news 时间列 = {tcol}')
if tcol:
    for r in con.execute(f"SELECT site, {tcol} t FROM news"):
        nall += 1
        res = is_fetch_time(r['site'], r['t'])
        if res and res is not False:
            nf += 1
            if len(samples) < 8:
                samples.append((r['site'], r['t'], res))
    p(f'  疑似伪时间 {nf} / {nall}')
    for s, t, f in samples:
        p(f'    {s} {t} (抓取 {f})')

con.close()
