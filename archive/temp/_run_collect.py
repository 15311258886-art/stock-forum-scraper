# -*- coding: utf-8 -*-
"""临时脚本：执行境外热点采集 + 落盘快照，供日报撰写使用。用后即删。"""
import sys, os, json, sqlite3, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

out = {}

# ---------- 1. 采集 ----------
try:
    from viewer.db import Database
    from overseas.collect import collect
    db = Database()
    r = collect(db=db, include_seed=True, reset=True, live=True)
    out["collect_result"] = repr(r)
    out["mode"] = "live"
except Exception as e:
    out["collect_error"] = "%s: %s" % (type(e).__name__, e)
    out["mode"] = "live_failed"

# ---------- 2. 直连 sqlite 取快照 ----------
DBP = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(DBP)
con.row_factory = sqlite3.Row
cur = con.cursor()

def scalar(sql):
    try:
        return cur.execute(sql).fetchone()[0]
    except Exception as e:
        return "ERR:%s" % e

stats = {}
stats["news_total"] = scalar("SELECT COUNT(*) FROM overseas_news")
stats["news_seed"] = scalar("SELECT COUNT(*) FROM overseas_news WHERE url LIKE '%example.local%'")
stats["news_real"] = scalar("SELECT COUNT(*) FROM overseas_news WHERE url NOT LIKE '%example.local%'")
stats["news_distinct_url"] = scalar("SELECT COUNT(DISTINCT url) FROM overseas_news")
stats["hotspots_total"] = scalar("SELECT COUNT(*) FROM overseas_hotspots")
stats["sources_total"] = scalar("SELECT COUNT(*) FROM overseas_sources_status")
for st in ("已接入", "待激活", "未启用"):
    stats["src_" + st] = scalar(
        "SELECT COUNT(*) FROM overseas_sources_status WHERE status LIKE '%%%s%%'" % st)
out["stats"] = stats

# 热点全字段
try:
    cols = [d[1] for d in cur.execute("PRAGMA table_info(overseas_hotspots)")]
    out["hotspot_columns"] = cols
    rows = [dict(zip(cols, rw)) for rw in cur.execute("SELECT * FROM overseas_hotspots")]
    out["hotspots"] = rows
except Exception as e:
    out["hotspots_error"] = str(e)

# 源状态
try:
    scols = [d[1] for d in cur.execute("PRAGMA table_info(overseas_sources_status)")]
    out["source_columns"] = scols
    out["sources"] = [dict(zip(scols, rw)) for rw in cur.execute("SELECT * FROM overseas_sources_status")]
except Exception as e:
    out["sources_error"] = str(e)

con.close()

with open(os.path.join(ROOT, "_hot_snapshot.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)

print("OK")
print(json.dumps({"mode": out.get("mode"), "collect": out.get("collect_result") or out.get("collect_error"), "stats": stats}, ensure_ascii=False, indent=1))
