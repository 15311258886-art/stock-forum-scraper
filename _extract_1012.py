import sys, json, html
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
from viewer.db import Database

db = Database()
cur = db.conn.cursor()
TODAY = '2026-10-12'
rows = cur.execute("SELECT * FROM overseas_hotspots").fetchall()
cols = [d[0] for d in cur.description]

# four-category keyword map
def classify(rec):
    blob = " ".join(str(rec.get(k,'') or '') for k in ['key','title','topic','overview','variables','transmission','cross_check','driver_sources']).lower()
    cats = []
    geo_kw = ['地缘','战争','冲突','制裁','伊朗','俄罗斯','乌克兰','以','加沙','红海','船队','军事','导弹','核']
    nat_kw = ['洪涝','洪水','灾害','地震','飓风','台风','干旱','自然','海啸','野火','暴雨','泥石流']
    sci_kw = ['论文','科研','nature','science','研究','实验','突破','phys.org','arxiv','期刊','发现']
    food_kw = ['粮食','歉收','减产','小麦','稻','玉米','大豆','粮价','作物','丰收','饥荒','农产品']
    if any(k in blob for k in geo_kw): cats.append('geo')
    if any(k in blob for k in nat_kw): cats.append('nat')
    if any(k in blob for k in sci_kw): cats.append('sci')
    if any(k in blob for k in food_kw): cats.append('food')
    return cats

out = []
for row in rows:
    rec = dict(zip(cols, row))
    ev_raw = rec.get('evidence')
    ev = []
    try:
        ev = json.loads(ev_raw) if isinstance(ev_raw, str) else (ev_raw or [])
    except Exception:
        ev = []
    if not isinstance(ev, list): ev = []
    live = [e for e in ev if isinstance(e, dict) and 'example.local' not in (e.get('url') or '')]
    seed = [e for e in ev if isinstance(e, dict) and 'example.local' in (e.get('url') or '')]
    clean = lambda s: html.unescape(str(s or '')) if s else ''
    item = {
        'id': rec.get('id'),
        'key': clean(rec.get('key')),
        'title': clean(rec.get('title')),
        'topic': clean(rec.get('topic')),
        'overview': clean(rec.get('overview')),
        'variables': clean(rec.get('variables')),
        'transmission': clean(rec.get('transmission')),
        'amplification': clean(rec.get('amplification')),
        'cross_check': clean(rec.get('cross_check')),
        'sectors_up': clean(rec.get('sectors_up')),
        'sectors_down': clean(rec.get('sectors_down')),
        'decision': clean(rec.get('decision')),
        'confidence': rec.get('confidence'),
        'heat': rec.get('heat'),
        'first_seen': clean(rec.get('first_seen')),
        'updated_at': clean(rec.get('updated_at')),
        'driver_sources': clean(rec.get('driver_sources')),
        'driver_regions': clean(rec.get('driver_regions')),
        'cats': classify(rec),
        'live_n': len(live),
        'seed_n': len(seed),
        'live_urls': [{'title': clean(e.get('title')), 'url': e.get('url'), 'source': clean(e.get('source')), 'date': clean(e.get('date'))} for e in live[:10]],
    }
    out.append(item)

with open(r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\_ex_1012.json','w',encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"WROTE {len(out)} hotspots", file=sys.stderr)
db.close()
