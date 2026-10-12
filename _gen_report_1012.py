import sys, json, html
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
from viewer.db import Database

db = Database()
cur = db.conn.cursor()
TODAY = '2026-10-12'

# introspect columns
col_rows = cur.execute("PRAGMA table_info(overseas_hotspots)").fetchall()
cols = [r[1] for r in col_rows]
print("COLS:", cols, file=sys.stderr)

sel = ", ".join(cols)
rows = cur.execute(f"SELECT {sel} FROM overseas_hotspots").fetchall()
recs = []
for row in rows:
    d = dict(zip(cols, row))
    # parse evidence JSON
    ev_raw = d.get('evidence')
    ev = []
    if ev_raw:
        try:
            ev = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
        except Exception:
            ev = []
    if not isinstance(ev, list):
        ev = []
    live = [e for e in ev if isinstance(e, dict) and 'example.local' not in (e.get('url') or '')]
    seed = [e for e in ev if isinstance(e, dict) and 'example.local' in (e.get('url') or '')]
    d['_evidence_live'] = len(live)
    d['_evidence_seed'] = len(seed)
    d['_evidence_live_list'] = live
    recs.append(d)

# source status
scol = cur.execute("PRAGMA table_info(overseas_sources_status)").fetchall()
scols = [r[1] for r in scol]
print("SCOLS:", scols, file=sys.stderr)
ssel = ", ".join(scols)
srows = cur.execute(f"SELECT {ssel} FROM overseas_sources_status").fetchall()
srcs = [dict(zip(scols, r)) for r in srows]

with open(r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\_hot_1012.json', 'w', encoding='utf-8') as f:
    json.dump(recs, f, ensure_ascii=False, indent=2)
with open(r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\_src_1012.json', 'w', encoding='utf-8') as f:
    json.dump(srcs, f, ensure_ascii=False, indent=2)

# quick summary to stderr
print(f"HOTSPOTS={len(recs)} TODAY_NEW={sum(1 for r in recs if (r.get('first_seen') or '')[:10]==TODAY)}", file=sys.stderr)
db.close()
