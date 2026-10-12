# -*- coding: utf-8 -*-
"""P2 定位：在 词典 / staging / app.db 里找 深华发*、000688、000001、全角名。只读。"""
from __future__ import annotations
import json, sys, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_p2_locate.log", "w", encoding="utf-8")
sys.stdout = log

d = json.loads((ROOT / "scraper" / "stock_names_full.json").read_text(encoding="utf-8"))
print("词典里含 '深华发' 的条目:", {k: d[k] for k in d if "深华发" in k})
print("词典里含全角字母的条目:")
for k in d:
    if any(ord(c) > 0xFF00 and c.isalpha() for c in k):
        print("   ", repr(k), "->", d[k])

print("\n== staging 里含 '深华发' 或全角字母的行 ==")
for l in (BASE / "trades_v2_staging.jsonl").read_text(encoding="utf-8").splitlines():
    if not l.strip():
        continue
    r = json.loads(l)
    nm = r.get("symbol_name") or ""
    if "深华发" in nm or any(ord(c) > 0xFF00 and c.isalpha() for c in nm):
        print("   pid=%s site=%s sym=%s name=%r" % (r.get("post_id"), r.get("site"), r.get("symbol"), nm))

print("\n== app.db trades 里 symbol_name 含 深华发 / 指数 / 科创 / 000688 / 000001 ==")
con = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "viewer" / "app.db").replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
for r in con.execute(
        "SELECT site,author,symbol,symbol_name,action FROM trades WHERE "
        "symbol IN ('000688','000001') OR symbol_name LIKE '%深华发%' OR symbol_name LIKE '%指数%' "
        "OR symbol_name LIKE '%科创%' OR symbol_name LIKE '%成指%'"):
    print("   [%s] %s sym=%s name=%r act=%s" % (r["site"], r["author"], r["symbol"], r["symbol_name"], r["action"]))
con.close()
