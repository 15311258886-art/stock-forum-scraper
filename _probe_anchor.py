# -*- coding: utf-8 -*-
import sqlite3, sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scraper import name_match as nm
from scraper.parsers.taoguba import TaogubaParser

db = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT raw_text FROM trades WHERE raw_text LIKE '%沈鼓%' LIMIT 1").fetchall()
con.close()
text = rows[0][0]

for m in __import__("re").finditer(r"([\u4e00-\u9fa5A-Za-z]{0,8})?[（(](?:sh|sz|SH|SZ)?(\d{6})[)）]", text):
    print("CODE MATCH", m.group(0), "at", m.start(), m.end())
for m in __import__("re").finditer(r"[买入|同日]", text):
    print("ACTION KW", m.group(0), "at", m.start(), m.end())
    fwd = nm.match_after(text, m.end(), max_len=6)
    print("   match_after ->", fwd)
    if fwd:
        an = fwd[3]
        print("   seg(anchor=%s) => %r" % (an, text[max(0, an - 35):min(len(text), an + 35)]))
        print("   >>> extract =>", TaogubaParser(cfg={})._extract_pq(text, an))
