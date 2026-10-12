# -*- coding: utf-8 -*-
"""临时脚本：压缩导出热点卡片 + 四类事件关键词命中。用后即删。"""
import os, sys, json, sqlite3, re, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
DBP = os.path.join(ROOT, "viewer", "app.db")
con = sqlite3.connect(DBP); con.row_factory = sqlite3.Row
cur = con.cursor()

lines = []
def w(s=""):
    lines.append(str(s))

# ---------- A. 热点卡片 ----------
cols = [d[1] for d in cur.execute("PRAGMA table_info(overseas_hotspots)")]
w("### COLUMNS: " + ", ".join(cols))
rows = [dict(zip(cols, r)) for r in cur.execute("SELECT * FROM overseas_hotspots")]
w("### HOTSPOT COUNT: %d" % len(rows))
for i, r in enumerate(rows, 1):
    w("")
    w("=== [%d] %s ===" % (i, r.get("title")))
    for k in ("topic", "confidence", "heat", "driver_sources", "driver_regions"):
        w("  %s: %s" % (k, r.get(k)))
    for k in ("overview", "variables", "sectors_up", "sectors_down", "sectors_up_detail",
              "sectors_down_detail", "transmission", "amplification", "cross_check"):
        v = r.get(k)
        if v is None:
            continue
        v = str(v).replace("\n", " ")
        if len(v) > 1400:
            v = v[:1400] + "…[截断]"
        w("  %s: %s" % (k, v))
    ev = r.get("evidence")
    try:
        evj = json.loads(ev) if isinstance(ev, str) else ev
        if isinstance(evj, list):
            w("  evidence(%d):" % len(evj))
            for e in evj:
                if isinstance(e, dict):
                    w("    - [%s] %s | %s" % (e.get("source") or e.get("source_name") or "?",
                                              (e.get("title") or "")[:110], e.get("url")))
                else:
                    w("    - %s" % str(e)[:200])
        else:
            w("  evidence: %s" % str(evj)[:600])
    except Exception as ex:
        w("  evidence(raw): %s" % str(ev)[:600])

# ---------- B. 四类事件关键词命中 ----------
CATS = {
 "①地缘冲突与战争": {
   "en": ["war","conflict","strike","sanction","missile","drone","military","troops","ceasefire",
          "invasion","airstrike","blockade","strait","hormuz","nato","geopolit","escalat","shelling",
          "offensive","front line","truce","ultimatum","embargo"],
   "cn": ["战争","冲突","军事","导弹","无人机","制裁","停火","袭击","空袭","封锁","霍尔木兹","海峡",
          "地缘","升级","开战","轰炸","战场","撤军","军演","对峙","边境"],
 },
 "②洪涝等自然灾害": {
   "en": ["flood","flooding","typhoon","hurricane","cyclone","storm","landslide","earthquake","wildfire",
          "drought","tsunami","volcano","eruption","monsoon","evacuat","disaster","rainfall","heatwave",
          "torrential","deluge","storm surge","nor'easter"],
   "cn": ["洪水","洪灾","内涝","台风","飓风","气旋","暴雨","山体滑坡","泥石流","地震","林火","野火",
          "干旱","海啸","火山","喷发","季风","疏散","救灾","灾情","极端天气","暴雨"],
 },
 "③重大科研论文发布": {
   "en": ["study finds","researchers","scientists","paper","journal","nature ","science ",
          "quantum","breakthrough","discovery","peer-reviewed","experiment","trial results",
          "preprint","genome","telescope","superconduct","physicists","astronomers","arxiv"],
   "cn": ["研究","论文","科学家","突破","发现","量子","实验","基因组","望远镜","超导",
          "期刊","团队发现","成果","Nature","Science"],
 },
 "④粮食歉收与减产": {
   "en": ["harvest","crop failure","food security","famine","wheat","grain","cropland","sowing",
          "export curb","crop","agricultur","staple food","food price","rice paddy","soybean",
          "corn price","fertilizer","livestock","food crisis"],
   "cn": ["粮食","歉收","减产","收成","小麦","稻","玉米","大豆","化肥","播种","耕地","饥荒",
          "农产品","食品价格","农业","种植","养殖","粮价"],
 },
}

def q(cond_sql):
    return cur.execute("SELECT url,title,source_name,publish_time,summary FROM overseas_news WHERE " + cond_sql).fetchall()

w("")
w("########## 四类事件命中 ##########")
news_all = cur.execute("SELECT COUNT(*) FROM overseas_news").fetchone()[0]
srcs = sorted({r["source_name"] for r in cur.execute("SELECT DISTINCT source_name FROM overseas_news")})
w("news rows: %d ; sources: %s" % (news_all, "、".join([s or "?" for s in srcs])))

def zao_date(u):
    m = re.search(r"story(\d{8})", u or "")
    return m.group(1) if m else ""

for cat, wl in CATS.items():
    conds = " OR ".join(["title LIKE " + repr("%"+k+"%") for k in wl["en"]] +
                        ["title LIKE " + repr("%"+k+"%") for k in wl["cn"]] +
                        ["summary LIKE " + repr("%"+k+"%") for k in wl["en"]] +
                        ["summary LIKE " + repr("%"+k+"%") for k in wl["cn"]])
    rs = q(conds)
    real = [r for r in rs if "example.local" not in (r["url"] or "")]
    seed = [r for r in rs if "example.local" in (r["url"] or "")]
    w("")
    w("=== %s : 命中 %d / 真实 %d / 示例 %d ===" % (cat, len(rs), len(real), len(seed)))
    seen = set()
    for r in real:
        key = r["title"]
        if key in seen: continue
        seen.add(key)
        d = zao_date(r["url"]) or (r["publish_time"] or "")[:10]
        w("  [%s] (%s) %s" % (d, r["source_name"], (r["title"] or "")[:130]))
        w("      %s" % r["url"])

# ---------- C. 当日（09-27/09-28）全部真实条目抽样 ----------
w("")
w("########## 09-28 当日条目（按 publish_time） ##########")
for r in cur.execute("SELECT url,title,source_name,publish_time FROM overseas_news WHERE publish_time LIKE '2026-09-28%' AND url NOT LIKE '%example.local%' ORDER BY source_name"):
    w("  [%s] (%s) %s" % ((r["publish_time"] or "")[:19], r["source_name"], (r["title"] or "")[:130]))
w("")
w("########## 09-27 当日条目 ##########")
cnt = 0
for r in cur.execute("SELECT url,title,source_name,publish_time FROM overseas_news WHERE publish_time LIKE '2026-09-27%' AND url NOT LIKE '%example.local%' ORDER BY source_name"):
    cnt += 1
    if cnt <= 120:
        w("  [%s] (%s) %s" % ((r["publish_time"] or "")[:19], r["source_name"], (r["title"] or "")[:130]))
w("  ... 09-27 真实条目合计 %d" % cnt)

con.close()
with open(os.path.join(ROOT, "_hot_compact.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("OK lines=%d" % len(lines))
