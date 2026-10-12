# -*- coding: utf-8 -*-
import sqlite3

conn = sqlite3.connect("viewer/app.db")
conn.row_factory = sqlite3.Row
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


for nm in ("新浪财经客户端", "新浪财经客", "平安银行资讯"):
    out("== %s ==" % nm)
    for t, c in (("trades", "author"), ("positions", "player")):
        r = conn.execute("SELECT COUNT(*) n FROM %s WHERE %s=?" % (t, c), (nm,)).fetchone()
        out("   %-10s %d" % (t, r["n"]))
    r = conn.execute("SELECT COUNT(*) n FROM posts WHERE author=?", (nm,)).fetchone()
    out("   %-10s %d" % ("posts", r["n"]))
    for r in conn.execute(
            "SELECT action, symbol, symbol_name, substr(COALESCE(raw_text,''),1,90) t "
            "FROM trades WHERE author=? LIMIT 4", (nm,)):
        out("   TR", dict(r))
    for r in conn.execute("SELECT substr(text,1,90) t FROM posts WHERE author=? LIMIT 3", (nm,)):
        out("   POST", dict(r))
    out("")

with open("tools/_holdfix/sina_acct.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
print("done")
