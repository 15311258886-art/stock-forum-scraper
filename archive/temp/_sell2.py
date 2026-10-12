# -*- coding: utf-8 -*-
import sys, os, re, sqlite3, json
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

s = open('viewer/server.py', encoding='utf-8').read()
ls = s.splitlines()

print('=' * 60)
print('api_consensus (1369-1384)')
print('=' * 60)
for i in range(1368, 1384):
    print('%5d: %s' % (i+1, ls[i]))

print()
print('=' * 60)
print('api_cohold_recent (1384-1408)')
print('=' * 60)
for i in range(1383, 1408):
    print('%5d: %s' % (i+1, ls[i]))

print()
print('=' * 60)
print('trades SELL 全部明细')
print('=' * 60)
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,site,author,symbol,symbol_name,action,post_time,post_url,raw_text FROM trades WHERE action='SELL' ORDER BY post_time DESC"):
    d = dict(r)
    print('  id=%s %-10s %-10s %-7s %-8s %s' % (d['id'], d['site'], (d['author'] or '')[:10], d['symbol'], (d['symbol_name'] or '')[:8], d['post_time']))
    print('     url=%s' % (d['post_url'] or '')[:90])
    print('     raw=%s' % (d['raw_text'] or '')[:110])
print()
print('=== trades 全部（含 BUY）按 symbol 聚合 ===')
for r in con.execute("""SELECT symbol, symbol_name, action, COUNT(DISTINCT author) people, COUNT(*) n, GROUP_CONCAT(author) who
  FROM trades WHERE symbol IS NOT NULL AND symbol<>'' GROUP BY symbol, action ORDER BY people DESC, n DESC LIMIT 25"""):
    print('  %-7s %-10s %-5s %s人 %s次 | %s' % (r['symbol'], (r['symbol_name'] or '')[:10], r['action'], r['people'], r['n'], (r['who'] or '')[:60]))
con.close()
