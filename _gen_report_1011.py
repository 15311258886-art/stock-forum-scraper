# -*- coding: utf-8 -*-
import sys, os, json, html
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
from viewer.db import Database
from overseas.collect import collect

ROOT = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
REP = os.path.join(ROOT, 'reports')
os.makedirs(REP, exist_ok=True)
TODAY = '2026-10-11'
MODE = '联网'

# Step 1: collect (live first, fallback to example)
db = Database()
try:
    r = collect(db=db, include_seed=True, reset=True, live=True)
    MODE = '联网'
except Exception as e:
    try:
        r = collect(db=db, include_seed=True, reset=True, live=False)
        MODE = '示例模式·未联网'
    except Exception as e2:
        r = {'_fatal': str(e2)}
print('COLLECT_MODE', MODE)
print('COLLECT_RAW', json.dumps(r, ensure_ascii=False)[:500])

# Step 2: dump all hotspots
cur = db.conn.execute('SELECT * FROM overseas_hotspots')
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
items = []
for row in rows:
    d = dict(zip(cols, row))
    ev = d.get('evidence')
    ev_list = []
    if ev:
        try:
            ev_list = json.loads(ev) if isinstance(ev, str) else ev
        except Exception:
            ev_list = []
    live_n = sum(1 for x in ev_list if isinstance(x, dict) and 'example.local' not in (x.get('url') or ''))
    seed_n = len(ev_list) - live_n
    items.append({
        'id': d.get('id'), 'title': d.get('title'), 'topic': d.get('topic'),
        'overview': d.get('overview'), 'variables': d.get('variables'),
        'sectors_up': d.get('sectors_up'), 'sectors_down': d.get('sectors_down'),
        'transmission': d.get('transmission'), 'amplification': d.get('amplification'),
        'cross_check': d.get('cross_check'), 'driver_sources': d.get('driver_sources'),
        'driver_regions': d.get('driver_regions'), 'confidence': d.get('confidence'),
        'heat': d.get('heat'), 'first_seen': d.get('first_seen'),
        'updated_at': d.get('updated_at'), 'ev_total': len(ev_list),
        'ev_live': live_n, 'ev_seed': seed_n,
        'live_urls': [x.get('url') for x in ev_list if isinstance(x, dict) and 'example.local' not in (x.get('url') or '')][:8],
        'seed_urls': [x.get('url') for x in ev_list if isinstance(x, dict) and 'example.local' in (x.get('url') or '')][:8],
    })
with open(os.path.join(REP, '_hot_1011.json'), 'w', encoding='utf-8') as f:
    json.dump(items, f, ensure_ascii=False, indent=1)
print('HOTSPOTS_COUNT', len(items))

# Step 3: source status aggregation
try:
    cur = db.conn.execute('SELECT source_key, status, enabled FROM overseas_sources_status')
    srows = cur.fetchall()
    agg = {}
    for sk, st, en in srows:
        agg.setdefault(st, 0)
        agg[st] += 1
    with open(os.path.join(REP, '_src_1011.json'), 'w', encoding='utf-8') as f:
        json.dump({'agg': agg, 'detail': [{'source_key': a, 'status': b, 'enabled': c} for a,b,c in srows]}, f, ensure_ascii=False, indent=1)
    print('SRC_AGG', json.dumps(agg, ensure_ascii=False))
except Exception as e:
    print('SRC_ERR', str(e))

db.close()
print('DONE')
