import sqlite3
db = "viewer/app.db"
con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("TABLES(%d):" % len(tabs))
for t in tabs:
    print("  ", t)
print("\n=== SCHEMA of key tables ===")
for t in ["stock_org", "stock_holders", "stock_holder_num", "board_flow_samples",
          "policy_news", "overseas_hotspots", "stock_holder_rank"]:
    try:
        cols = con.execute("PRAGMA table_info(%s)" % t).fetchall()
        print("--- %s (%d cols) ---" % (t, len(cols)))
        print("   ", ", ".join("%s:%s" % (c[1], c[2]) for c in cols))
    except Exception as e:
        print("--- %s ERR %s ---" % (t, e))
con.close()
