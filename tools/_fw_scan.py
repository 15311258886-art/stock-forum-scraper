# -*- coding: utf-8 -*-
"""广搜：staging + app.db 里所有 symbol_name 含全角字母 / 深华发的行。只读。"""
from __future__ import annotations
import json, sys, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_fw_scan.log", "w", encoding="utf-8")
sys.stdout = log

def fw(s):
    return any(0xFF00 <= ord(c) <= 0xFFEF and c.isalpha() for c in (s or ""))

print("== staging 全角名 ==")
n = 0
for l in (BASE / "trades_v2_staging.jsonl").read_text(encoding="utf-8").splitlines():
    if not l.strip():
        continue
    r = json.loads(l)
    if fw(r.get("symbol_name")):
        n += 1
        print("   pid=%s sym=%s name=%r" % (r.get("post_id"), r.get("symbol"), r.get("symbol_name")))
print("   共", n)

print("\n== app.db trades 全角名 / 深华发 ==")
con = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "viewer" / "app.db").replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
n = 0
for r in con.execute("SELECT id,site,author,symbol,symbol_name FROM trades"):
    if fw(r["symbol_name"]) or "深华发" in (r["symbol_name"] or ""):
        n += 1
        print("   id=%s [%s] %s sym=%s name=%r" % (r["id"], r["site"], r["author"], r["symbol"], r["symbol_name"]))
print("   共", n)

# app.db 里 000688 / 指数 / 科创
print("\n== app.db trades 000688 / 指数 / 科创 ==")
for r in con.execute("SELECT site,author,symbol,symbol_name,action FROM trades WHERE symbol='000688' OR symbol_name LIKE '%指数%' OR symbol_name LIKE '%科创%'"):
    print("   [%s] %s sym=%s name=%r act=%s" % (r["site"], r["author"], r["symbol"], r["symbol_name"], r["action"]))
con.close()
