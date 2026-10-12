# -*- coding: utf-8 -*-
"""核查 stock_holders 结构与家族污染面（只读）"""
import sqlite3, os, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')
DB = r'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/viewer/app.db'
con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True)
con.row_factory = sqlite3.Row

print('=== stock_holders 列 ===')
for r in con.execute('PRAGMA table_info(stock_holders)'):
    print('  %-20s %s' % (r['name'], r['type']))

print()
print('=== scope 分布 ===')
for r in con.execute('SELECT scope, COUNT(*) n, COUNT(DISTINCT symbol) s FROM stock_holders GROUP BY scope'):
    print('  %-12s n=%-7s 股=%s' % (r['scope'], r['n'], r['s']))

print()
print('=== holder_type 分布 ===')
for r in con.execute('SELECT holder_type, COUNT(*) n FROM stock_holders GROUP BY holder_type ORDER BY n DESC'):
    print('  %-14s %s' % (r['holder_type'], r['n']))

print()
print('=== 占比字段类型（自然人）===')
for r in con.execute("SELECT typeof(hold_ratio) t, COUNT(*) n FROM stock_holders WHERE holder_type='自然人' GROUP BY typeof(hold_ratio)"):
    print('  %-10s %s' % (r['t'], r['n']))

print()
print('=== 自然人 高占比 Top15（疑似实控人/家族）===')
for r in con.execute("""SELECT symbol, symbol_name, holder_name, end_date, scope, holder_rank, hold_ratio
  FROM stock_holders WHERE holder_type='自然人'
  ORDER BY CAST(hold_ratio AS REAL) DESC LIMIT 15"""):
    print('  %s %-8s %-10s %s r%-3s %s%%' % (
        r['symbol'], (r['symbol_name'] or '')[:8], (r['holder_name'] or '')[:10],
        r['end_date'], r['holder_rank'], r['hold_ratio']))

print()
print('=== 同一股票自然人数量 Top12（家族抱团）===')
for r in con.execute("""SELECT symbol, symbol_name, COUNT(DISTINCT holder_name) n
  FROM stock_holders WHERE holder_type='自然人'
  GROUP BY symbol ORDER BY n DESC LIMIT 12"""):
    print('  %s %-8s %s 人' % (r['symbol'], (r['symbol_name'] or '')[:8], r['n']))

print()
print('=== 跨公司数分布（自然人）===')
for r in con.execute("""SELECT n, COUNT(*) people FROM (
  SELECT holder_name, COUNT(DISTINCT symbol) n FROM stock_holders
  WHERE holder_type='自然人' AND LENGTH(holder_name) BETWEEN 2 AND 4
  GROUP BY holder_name) GROUP BY n ORDER BY n DESC LIMIT 12"""):
    print('  跨%s家: %s 人' % (r['n'], r['people']))

con.close()
