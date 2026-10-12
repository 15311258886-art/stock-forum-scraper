# -*- coding: utf-8 -*-
import sqlite3, sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scraper.parsers.taoguba import TaogubaParser

db = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT raw_text FROM trades WHERE raw_text LIKE '%沈鼓%' LIMIT 1").fetchall()
con.close()
text = rows[0][0]

orig = TaogubaParser._extract_pq
def spy(self, t, anchor):
    r = orig(self, t, anchor)
    ctx = repr(t[max(0, anchor - 35):anchor + 35]) if anchor is not None else None
    print(f"  anchor={anchor} -> {r}\n     ctx={ctx}")
    return r
TaogubaParser._extract_pq = spy

print("=== 沈鼓 process ===")
for tr in TaogubaParser(cfg={})._extract_trades(text):
    print("RESULT:", tr)
