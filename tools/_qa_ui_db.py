# -*- coding: utf-8 -*-
"""只读体检 viewer/app.db：数据来源混杂度、时间填充、缺失字段。绝不写库。
用法：python tools/_qa_ui_db.py
输出：tools/_qa_ui_db.txt
"""
import sqlite3, os, sys, json
from datetime import datetime, timedelta
from collections import Counter, defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer", "app.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_qa_ui_db.txt")
L = []
def p(*a):
    L.append(" ".join(str(x) for x in a))

p("DB =", DB, " size=", os.path.getsize(DB), "bytes  mtime=", datetime.fromtimestamp(os.path.getmtime(DB)).strftime("%Y-%m-%d %H:%M:%S"))

con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

def one(sql, args=()):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None

p("\n=========== 1. 各表行数 ===========")
tables = ["raw_pages","posts","trades","players","positions","news","stock_mentions","persons",
          "topics","login_status","meta","stock_names","overseas_news","overseas_mentions",
          "overseas_sentiment_daily","overseas_hotspots","overseas_sources_status"]
for t in tables:
    try:
        p("  %-26s %s" % (t, one("SELECT COUNT(*) FROM %s" % t)))
    except Exception as e:
        p("  %-26s ERR %s" % (t, e))

p("\n=========== 2. meta 表（判定示例/真实的关键）===========")
for r in c.execute("SELECT key,value FROM meta ORDER BY key"):
    p("  %-24s = %s" % (r["key"], r["value"]))

p("\n=========== 3. 示例数据 vs 真实数据：逐表按来源 URL 判别 ===========")
def split_url(tbl, col="post_url"):
    try:
        demo = one("SELECT COUNT(*) FROM %s WHERE %s LIKE '%%example.local%%' OR %s LIKE '%%example.%%'" % (tbl, col, col))
        real = one("SELECT COUNT(*) FROM %s WHERE %s IS NULL OR (%s NOT LIKE '%%example.local%%' AND %s NOT LIKE '%%example.%%')" % (tbl, col, col, col))
        tot = one("SELECT COUNT(*) FROM %s" % tbl)
        blank = one("SELECT COUNT(*) FROM %s WHERE %s IS NULL OR %s=''" % (tbl, col, col))
        p("  %-14s 总=%-5s 示例URL=%-5s 非示例URL=%-5s (其中空URL=%-5s)" % (tbl, tot, demo, real, blank))
    except Exception as e:
        p("  %-14s ERR %s" % (tbl, e))
split_url("posts")
split_url("trades")
split_url("positions")
split_url("players", "source_url")
split_url("news", "url")
split_url("overseas_news", "url")
split_url("overseas_hotspots", "key")

p("\n=========== 4. posts 时间字段 ===========")
p("  posts 总数:", one("SELECT COUNT(*) FROM posts"))
p("  post_time 为空/NULL:", one("SELECT COUNT(*) FROM posts WHERE post_time IS NULL OR post_time=''"))
p("  post_time 最早/最晚:", c.execute("SELECT MIN(post_time), MAX(post_time) FROM posts").fetchone()[:])
today = datetime.now().strftime("%Y-%m-%d")
p("  今日(%s)的 post_time 条数:" % today, one("SELECT COUNT(*) FROM posts WHERE post_time LIKE ?", (today+"%",)))
# 同一秒多条 => now 兜底特征
p("  post_time 精确到同一秒的重复组数(>1):")
dup = c.execute("SELECT post_time, COUNT(*) n FROM posts GROUP BY post_time HAVING n>1 ORDER BY n DESC LIMIT 8").fetchall()
for r in dup:
    p("     %s  x%s" % (r["post_time"], r["n"]))
p("  示例 posts(example.local):", one("SELECT COUNT(*) FROM posts WHERE post_url LIKE '%%example.local%%'"))
p("  真实 posts(非 example):", one("SELECT COUNT(*) FROM posts WHERE post_url IS NULL OR post_url NOT LIKE '%%example.local%%'"))

p("\n=========== 5. trades / positions 时间字段 ===========")
for tbl in ("trades","positions"):
    p("  %s 总数=%s  post_time空=%s  buy_time空=%s  hold_since空=%s" % (
        tbl, one("SELECT COUNT(*) FROM %s"%tbl),
        one("SELECT COUNT(*) FROM %s WHERE post_time IS NULL OR post_time=''"%tbl),
        one("SELECT COUNT(*) FROM %s WHERE buy_time IS NULL OR buy_time=''"%tbl),
        one("SELECT COUNT(*) FROM %s WHERE hold_since IS NULL OR hold_since=''"%tbl)))
    p("     示例=%s 真实=%s" % (
        one("SELECT COUNT(*) FROM %s WHERE post_url LIKE '%%example.local%%'"%tbl),
        one("SELECT COUNT(*) FROM %s WHERE post_url IS NULL OR post_url NOT LIKE '%%example.local%%'"%tbl)))

p("\n=========== 6. overseas_news：kind / 示例源 / 时间 ===========")
p("  kind 分布:", dict(c.execute("SELECT COALESCE(kind,'<null>') k, COUNT(*) n FROM overseas_news GROUP BY k ORDER BY n DESC").fetchall()))
p("  source_name 以「示例源·」开头:", one("SELECT COUNT(*) FROM overseas_news WHERE source_name LIKE '示例源·%%'"))
p("  url 为 example.local:", one("SELECT COUNT(*) FROM overseas_news WHERE url LIKE '%%example.local%%'"))
p("  publish_time 为空/NULL:", one("SELECT COUNT(*) FROM overseas_news WHERE publish_time IS NULL OR publish_time=''"))
p("  publish_time 今日条数:", one("SELECT COUNT(*) FROM overseas_news WHERE publish_time LIKE ?", (today+"%",)))
p("  publish_time 最早/最晚:", c.execute("SELECT MIN(publish_time), MAX(publish_time) FROM overseas_news").fetchone()[:])
last_collect = one("SELECT value FROM meta WHERE key='overseas_last_collect'")
p("  meta.overseas_last_collect =", last_collect)
# 同一秒被填成 now 的特征：统计 publish_time 的秒级唯一度
p("  publish_time 精确同一秒重复组(>1，前 10):")
for r in c.execute("SELECT publish_time, COUNT(*) n FROM overseas_news GROUP BY publish_time HAVING n>1 ORDER BY n DESC LIMIT 10").fetchall():
    p("     %s  x%s" % (r["publish_time"], r["n"]))
tot_ov = one("SELECT COUNT(*) FROM overseas_news")
dup_rows = one("SELECT COALESCE(SUM(n),0) FROM (SELECT COUNT(*) n FROM overseas_news GROUP BY publish_time HAVING n>1)")
p("  落在「重复时间戳」里的条目数:", dup_rows, "/", tot_ov)

p("\n=========== 7. overseas_sentiment_daily 序列（趋势图数据源）===========")
p("  行数:", one("SELECT COUNT(*) FROM overseas_sentiment_daily"))
rows = c.execute("SELECT day, SUM(total) t FROM overseas_sentiment_daily GROUP BY day ORDER BY day DESC LIMIT 12").fetchall()
for r in rows:
    p("     %s  total=%s" % (r["day"], r["t"]))
p("  今日行的 total:", one("SELECT SUM(total) FROM overseas_sentiment_daily WHERE day=?", (today,)))

p("\n=========== 8. 缺失字段体检（前端会渲染成 undefined 的位置）===========")
def nulls(tbl, cols):
    p("  [%s]" % tbl)
    tot = one("SELECT COUNT(*) FROM %s" % tbl)
    for col in cols:
        n = one("SELECT COUNT(*) FROM %s WHERE %s IS NULL OR %s=''" % (tbl, col, col))
        if n:
            p("     %-18s 空/NULL = %s / %s" % (col, n, tot))
nulls("stocks" if False else "stock_mentions", ["symbol","symbol_name","context"])
nulls("posts", ["site","author","text","sentiment","post_time"])
nulls("trades", ["symbol","symbol_name","action","price","current_price","access_level"])
nulls("positions", ["symbol","symbol_name","player","channel","ratio"])
nulls("players", ["name","channel","tier","rank_position","ranking_score","influence","platform"])
nulls("overseas_hotspots", ["title","direction","amplification_factor","confidence","heat","decision","overview","transmission"])
nulls("overseas_news", ["source_name","region","title","topic","sentiment","heat"])

p("\n=========== 9. overseas_hotspots 关键字段例样 ===========")
for r in c.execute("SELECT key,title,direction,amplification_factor,confidence,heat,first_seen,updated_at FROM overseas_hotspots ORDER BY heat DESC LIMIT 5"):
    p("  ", dict(r))

p("\n=========== 10. persons / players 计数对账 ===========")
p("  persons 表总数:", one("SELECT COUNT(*) FROM persons"), " /api/persons LIMIT 100")
p("  players 表总数:", one("SELECT COUNT(*) FROM players"))
p("  players tier 分布:", dict(c.execute("SELECT COALESCE(tier,channel) t, COUNT(*) n FROM players GROUP BY t ORDER BY t").fetchall()))
p("  players is_top_player=1:", one("SELECT COUNT(*) FROM players WHERE is_top_player=1"))
p("  players is_visible=1:", one("SELECT COUNT(*) FROM players WHERE is_visible=1"))
p("  positions 覆盖选手数:", one("SELECT COUNT(DISTINCT player) FROM positions"))
p("  positions 覆盖标的数:", one("SELECT COUNT(DISTINCT symbol) FROM positions"))
p("  stock_mentions distinct symbol:", one("SELECT COUNT(DISTINCT symbol) FROM stock_mentions"))

con.close()
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("\n".join(L))
