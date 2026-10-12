# -*- coding: utf-8 -*-
"""修正 positions 的 stale 语义：持仓=当前状态，不因"无日期"被判为滞后噪声。
   - 人轴站（taoguba）的持仓：stale=0（永远显示）
   - 非人轴/历史站（jisilu 2021年帖）：stale=1（隐藏）
"""
import os, sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'viewer', 'app.db')
con = sqlite3.connect(DB, timeout=30)
cur = con.cursor()

print('=== 修正前 ===')
for r in cur.execute("SELECT site, axis, stale, COUNT(*) FROM positions GROUP BY site, axis, stale"):
    print('  ', r)

# 人轴持仓 → 永不判为滞后（持仓代表当前状态，非时间敏感事件）
cur.execute("UPDATE positions SET stale=0 WHERE axis='person'")
print('\n[1] 人轴持仓 stale=0:', cur.rowcount)

# 非人轴（jisilu 2021 历史帖）→ 保持 stale=1
cur.execute("UPDATE positions SET stale=1 WHERE axis='post'")
print('[2] 帖轴持仓 stale=1:', cur.rowcount)

con.commit()
print('\n=== 修正后 ===')
for r in cur.execute("SELECT site, axis, stale, COUNT(*) FROM positions GROUP BY site, axis, stale"):
    print('  ', r)
print('\n[校验] integrity =', cur.execute('PRAGMA integrity_check').fetchone()[0])
con.close()
