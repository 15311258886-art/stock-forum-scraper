# -*- coding: utf-8 -*-
import sys, os, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, BASE)
from viewer.db import Database

TODAY = '2026-10-09'
db = Database()

def q(sql, args=()):
    c = db.conn.execute(sql, args)
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in c.fetchall()]

def counts(ev):
    try:
        arr = json.loads(ev) if ev else []
    except Exception:
        arr = []
    live = [e for e in arr if 'example.local' not in str(e.get('url', ''))]
    return len(arr), len(live)

rows = q("SELECT id,title,topic,first_seen,updated_at,confidence,heat,amplification_factor,"
         "driver_sources,driver_regions,direction,related_symbols FROM overseas_hotspots ORDER BY id")
for r in rows:
    n, lv = counts(r.get('evidence') or '')
    # 追加证据计数（单独查，避免大文本进第一段）
    r['_ev_total'] = n
    r['_ev_live'] = lv
    del r['driver_sources']
    r['_ds'] = q("SELECT driver_sources FROM overseas_hotspots WHERE id=?", (r['id'],))[0]['driver_sources']
with open(os.path.join(BASE, '_q2_1009.json'), 'w', encoding='utf-8') as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)

# 第二段：长文本 + 实时证据 URL
FULL = ['id', 'title', 'topic', 'overview', 'variables', 'sectors_up', 'sectors_down',
        'sectors_up_detail', 'sectors_down_detail', 'transmission', 'amplification',
        'cross_check', 'decision', 'related_markets', 'evidence']
cols = ','.join(FULL[1:])
rows2 = q("SELECT %s FROM overseas_hotspots ORDER BY id" % cols)
for r in rows2:
    try:
        arr = json.loads(r.get('evidence') or [])
    except Exception:
        arr = []
    r['_live_urls'] = [{'t': e.get('title', ''), 'u': e.get('url', ''), 's': e.get('source', ''),
                        'p': e.get('publish_time', '')}
                       for e in arr if 'example.local' not in str(e.get('url', ''))]
    r['_seed_urls'] = [e.get('url', '') for e in arr if 'example.local' in str(e.get('url', ''))]
    r.pop('evidence', None)
with open(os.path.join(BASE, '_q3_1009.json'), 'w', encoding='utf-8') as f:
    json.dump(rows2, f, ensure_ascii=False, indent=1)

# 第三段：源状态
src = q("SELECT status, COUNT(*) n FROM overseas_sources_status GROUP BY status")
det = q("SELECT source_key,name,status,count,requires_key,requires_vpn,enabled FROM overseas_sources_status ORDER BY status,source_key")
today_new = q("SELECT id,title,topic,first_seen FROM overseas_hotspots WHERE substr(first_seen,1,10)=?", (TODAY,))
with open(os.path.join(BASE, '_q4_1009.json'), 'w', encoding='utf-8') as f:
    json.dump({'src_status': src, 'src_detail': det, 'today_new': today_new}, f, ensure_ascii=False, indent=1)

db.close()
print('OK')
