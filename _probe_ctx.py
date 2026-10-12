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
print("FULL TEXT LEN:", len(text))
print(text)
print("=" * 78)
p = TaogubaParser(cfg={})
for t in p._extract_trades(text):
    print(t)
