# -*- coding: utf-8 -*-
"""只读：在 viewer/app.db 里查 "新浪财经客户端" 是否作为 author/nickname 出现。
mode=ro，绝不写。输出 tools/_probe_xlc_db.log。
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "viewer" / "app.db"
import sqlite3
log = open(ROOT / "tools" / "_probe_xlc_db.log", "w", encoding="utf-8")
sys.stdout = log
con = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
NEEDLE = "新浪财经客户端"
for t in ("posts", "trades", "players"):
    cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % t)]
    authcol = "author" if "author" in cols else ("name" if "name" in cols else None)
    txtcol = "text" if "text" in cols else ("raw_text" if "raw_text" in cols else None)
    print("== %s == 列: %s" % (t, cols))
    if authcol:
        n = cur.execute("SELECT COUNT(*) c FROM %s WHERE %s=?" % (t, authcol), (NEEDLE,)).fetchone()["c"]
        print("   author=… 精确等于 %r 的行 = %d" % (NEEDLE, n))
        like = cur.execute("SELECT COUNT(*) c FROM %s WHERE %s LIKE ?" % (t, authcol), ("%客户端%",)).fetchone()["c"]
        print("   author LIKE '%%客户端%%' 的行 = %d" % like)
        for r in cur.execute("SELECT %s a, COUNT(*) c FROM %s GROUP BY 1 HAVING %s LIKE ? ORDER BY c DESC LIMIT 10"
                             % (authcol, t, authcol), ("%客户端%",)):
            print("      - author=%r  行=%d" % (r["a"], r["c"]))
    if txtcol:
        nt = cur.execute("SELECT COUNT(*) c FROM %s WHERE %s LIKE ?" % (t, txtcol), ("%新浪财经客户端%",)).fetchone()["c"]
        print("   %s LIKE '%%新浪财经客户端%%' 的行 = %d" % (txtcol, nt))
    print()
con.close()
