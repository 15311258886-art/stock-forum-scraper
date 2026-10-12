# -*- coding: utf-8 -*-
import sys, json, datetime
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
from viewer.db import Database

db = Database()
cur = db.conn.execute("SELECT * FROM overseas_hotspots ORDER BY id")
cols = [d[0] for d in cur.description]
rows = [dict(zip(cols, r)) for r in cur.fetchall()]
db.close()

TODAY = "2026-10-08"
out1 = []
out2 = []

for h in rows:
    try:
        ev = json.loads(h.get("evidence") or "[]")
    except Exception:
        ev = []
    ev_live = [e for e in ev if "example.local" not in str(e.get("url", ""))]
    ev_seed = [e for e in ev if "example.local" in str(e.get("url", ""))]
    out1.append({
        "id": h["id"],
        "topic": h.get("topic"),
        "title": h.get("title"),
        "first_seen": h.get("first_seen"),
        "updated_at": h.get("updated_at"),
        "confidence": h.get("confidence"),
        "heat": h.get("heat"),
        "ev_total": len(ev),
        "ev_live": len(ev_live),
        "ev_seed": len(ev_seed),
        "driver_sources": h.get("driver_sources"),
        "driver_regions": h.get("driver_regions"),
        "is_today": (h.get("first_seen") or "")[:10] == TODAY,
    })
    out2.append({
        "id": h["id"],
        "topic": h.get("topic"),
        "title": h.get("title"),
        "first_seen": h.get("first_seen"),
        "is_today": (h.get("first_seen") or "")[:10] == TODAY,
        "overview": h.get("overview"),
        "variables": h.get("variables"),
        "sectors_up": h.get("sectors_up"),
        "sectors_down": h.get("sectors_down"),
        "transmission": h.get("transmission"),
        "amplification": h.get("amplification"),
        "cross_check": h.get("cross_check"),
        "live_urls": [e.get("url") for e in ev_live][:8],
    })

with open(r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\_q1_1008.json", "w", encoding="utf-8") as f:
    json.dump(out1, f, ensure_ascii=False, indent=1)
with open(r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\_q2_1008.json", "w", encoding="utf-8") as f:
    json.dump(out2, f, ensure_ascii=False, indent=1)

print("rows", len(out1))
print(json.dumps(out1, ensure_ascii=False))
