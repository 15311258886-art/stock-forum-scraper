# -*- coding: utf-8 -*-
"""P0-2 验证：用库里真实 raw_text 跑量价抽取，打印作者+量价+原文，便于人工核。

只做本地纯文本解析，不发请求、不写库。
"""
import sqlite3, sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scraper.parsers.taoguba import TaogubaParser

db = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT id, author, action, symbol_name, raw_text FROM trades "
                   "WHERE raw_text IS NOT NULL AND raw_text<>'' ORDER BY id").fetchall()
con.close()

p = TaogubaParser(cfg={})
hit_price = hit_qty = 0
out = []
for rid, author, action, name, text in rows:
    for t in p._extract_trades(text or ""):
        pr, q = t["price"], t["quantity"]
        if pr is not None:
            hit_price += 1
        if q is not None:
            hit_qty += 1
        out.append((rid, author, t["action"], t["symbol_name"], pr, q, (text or "")[:90]))

print(f"总条目 {len(out)} | 抽到价格 {hit_price} | 抽到数量 {hit_qty}")
print("=" * 76)
print("【抽到数量或价格的条目（逐条可核）】")
for rid, author, act, nm, pr, q, head in out:
    if pr is not None or q is not None:
        unit = "股" if (q or 0) >= 10000 else "股"
        print(f"row {rid} | {author} | {act} | {nm} | price={pr} | qty={q}{unit}")
        print(f"    原文: {head}...")
print("=" * 76)
print("【抽查：沈鼓那条三兄弟】")
for r in out:
    if r[0] in (7948, 7949, 7950):
        print(f"row {r[0]} {r[2]} {r[3]} price={r[4]} qty={r[5]}")
