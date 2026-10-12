# -*- coding: utf-8 -*-
"""综合状态核查：看板接线 / 数据量 / 股东污染面 / 时间口径 / 最新交易日覆盖"""
import os, re, sqlite3, json, datetime

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)
out = []

def P(*a):
    s = ' '.join(str(x) for x in a)
    out.append(s)

# ---------- 1) 前端/后端接线状态 ----------
js = open('viewer/static/app.js', encoding='utf-8').read()
h  = open('viewer/static/index.html', encoding='utf-8').read()
sv = open('viewer/server.py', encoding='utf-8').read()

P('===== 1) 接线状态 =====')
P('app.js lines:', len(js.splitlines()))
for k in ['renderDashboard', 'loadDashboard', 'const DASH', 'renderNiusan', 'loadNiusan',
          'const NS', 'renderLhbSectors', 'loadLhbSectors', 'const LBHS']:
    P('  app.js %-22s %d' % (k, js.count(k)))
ids = re.findall(r'id="(p-[a-z_]+)"', h)
P('  index panels(%d):' % len(ids), ', '.join(ids))
P()
P('  后端接口:')
for k in ['def api_dashboard', 'def api_niusan', 'def api_lhb_sectors', 'def api_holders_panel',
          'def api_holders', 'def api_stockholder_flow', 'route == "/api/dashboard"',
          'route == "/api/niusan"', 'route == "/api/lhb_sectors"']:
    P('    %-34s %d' % (k, sv.count(k)))

# ---------- 2) 数据量 ----------
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
P()
P('===== 2) 数据量 =====')
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in ['players', 'positions', 'trades', 'posts', 'news', 'lhb_stocks', 'lhb_seats',
          'lhb_depts', 'lhb_inst', 'lhb_sector_focus', 'stock_holders', 'stock_holder_num']:
    if t in tabs:
        P('  %-20s %s' % (t, con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]))
P('  全部表:', ', '.join(tabs))

# ---------- 3) 股东数据污染面（核心） ----------
P()
P('===== 3) 股东数据污染面（家族持股检测） =====')
P('  holder_type 取值:')
for r in con.execute("SELECT holder_type, COUNT(*) n FROM stock_holders GROUP BY holder_type ORDER BY n DESC"):
    P('    %r %s' % (r['holder_type'], r['n']))

# 自然人 & 跨公司数
P()
P('  自然人跨公司分布:')
for r in con.execute("""SELECT n, COUNT(*) people FROM (
    SELECT holder_name, COUNT(DISTINCT symbol) n FROM stock_holders
    WHERE holder_type='自然人' GROUP BY holder_name) GROUP BY n ORDER BY n DESC LIMIT 12"""):
    P('    持%s只: %s人' % (r['n'], r['people']))

# 家族疑似：同股票同姓氏 >=3 人 且 合计持股高
P()
P('  疑似家族持股（同一只股同姓氏≥2人 且 合计占比>30%）Top12:')
q = """
SELECT symbol, symbol_name,
       COUNT(*) fam_n, SUM(CAST(hold_ratio AS REAL)) fam_ratio,
       GROUP_CONCAT(holder_name, '/') names
FROM stock_holders
WHERE holder_type='自然人' AND hold_ratio IS NOT NULL
GROUP BY symbol
HAVING fam_n >= 2 AND fam_ratio > 30
ORDER BY fam_ratio DESC LIMIT 12
"""
try:
    for r in con.execute(q):
        P('    %s %-8s %s人 合计%.2f%%  %s' % (
            r['symbol'], (r['symbol_name'] or '')[:8], r['fam_n'], r['fam_ratio'], (r['names'] or '')[:52]))
except Exception as e:
    P('    ERR', e)

# 真正的牛散：跨>=2 公司
n2 = con.execute("""SELECT COUNT(*) FROM (SELECT holder_name FROM stock_holders
  WHERE holder_type='自然人' GROUP BY holder_name HAVING COUNT(DISTINCT symbol)>=2)""").fetchone()[0]
n3 = con.execute("""SELECT COUNT(*) FROM (SELECT holder_name FROM stock_holders
  WHERE holder_type='自然人' GROUP BY holder_name HAVING COUNT(DISTINCT symbol)>=3)""").fetchone()[0]
P()
P('  跨>=2公司（牛散候选）: %s 人' % n2)
P('  跨>=3公司（核心牛散）: %s 人' % n3)

# ---------- 4) 时间口径 ----------
P()
P('===== 4) 时间口径 =====')
for t, c in [('trades', 'post_time'), ('news', 'publish_time'), ('positions', 'post_time'), ('posts', 'post_time')]:
    try:
        tot = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        blank = con.execute('SELECT COUNT(*) FROM "%s" WHERE %s IS NULL OR TRIM(%s)=\'\'' % (t, c, c)).fetchone()[0]
        zero = con.execute("SELECT COUNT(*) FROM \"%s\" WHERE %s LIKE '%%00:00:00%%'" % (t, c)).fetchone()[0]
        mx = con.execute('SELECT MAX(%s) FROM "%s"' % (c, t)).fetchone()[0]
        P('  %-10s total=%-5s blank=%-5s 00:00:00=%-4s max=%s' % (t, tot, blank, zero, mx))
    except Exception as e:
        P('  %-10s ERR %s' % (t, str(e)[:60]))

# ---------- 5) 最新交易日覆盖 ----------
P()
P('===== 5) 最新交易日覆盖 =====')
for r in con.execute('SELECT trade_date, COUNT(DISTINCT security_code) c FROM lhb_stocks GROUP BY trade_date ORDER BY trade_date DESC LIMIT 8'):
    P('  lhb_stocks %s: %s 只' % (r['trade_date'], r['c']))
P('  ---')
for r in con.execute('SELECT trade_date, COUNT(*) n FROM lhb_sector_focus GROUP BY trade_date ORDER BY trade_date DESC LIMIT 8'):
    P('  sector_focus %s: %s 板块' % (r['trade_date'], r['n']))
P('  ---')
for r in con.execute('SELECT end_date, COUNT(*) n FROM stock_holders GROUP BY end_date ORDER BY end_date DESC LIMIT 8'):
    P('  stock_holders 报告期 %s: %s 行' % (r['end_date'], r['n']))

con.close()

open('_v2_state_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
