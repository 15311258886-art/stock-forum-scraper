# -*- coding: utf-8 -*-
import sqlite3, sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scraper.parsers.taoguba import TaogubaParser

db = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT id, author, action, symbol_name, raw_text FROM trades "
                   "WHERE raw_text LIKE '%沈鼓%' ORDER BY id").fetchall()
con.close()
print("含沈鼓的行数:", len(rows))
p = TaogubaParser(cfg={})
for rid, author, action, name, text in rows:
    print("=" * 78)
    print(f"id={rid} author={author} action={action} name={name} len={len(text or '')}")
    print("TEXT:", repr(text))
    for t in p._extract_trades(text or ""):
        print("   ->", t)
