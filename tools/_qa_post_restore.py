# -*- coding: utf-8 -*-
"""回滚后只读验收：库内容 + 面板接口 + 新代码（enabled 列 / 双实例保护）是否加载。

严格只发 GET，不触碰任何写接口。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "_qa_post_restore.txt"
DB = ROOT / "viewer" / "app.db"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = f"http://127.0.0.1:{PORT}"

L: list[str] = []
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def w(s: str = "") -> None:
    L.append(s)


def getjson(path: str, timeout: float = 60.0):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


w(f"回滚后只读验收 @ {BASE}")
w("=" * 74)

# ---- A. 库内容 ----
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
w("[A] 库内容")
for t in ("posts", "players", "positions", "trades", "overseas_news",
          "overseas_hotspots", "overseas_sentiment_daily", "overseas_sources_status"):
    try:
        w(f"    {t:<28} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    except Exception as e:
        w(f"    {t:<28} ERR {e}")
w(f"    meta.overseas_last_collect = {con.execute(chr(83)+'ELECT value FROM meta WHERE key=?', ('overseas_last_collect',)).fetchone()}")
cols = [r[1] for r in con.execute("PRAGMA table_info(overseas_sources_status)")]
w(f"    overseas_sources_status 列 = {cols}")
w(f"    含 enabled 列 = {'enabled' in cols}")
regs = con.execute("SELECT COALESCE(region,'(null)'), COUNT(*) FROM overseas_news "
                   "GROUP BY 1 ORDER BY 2 DESC").fetchall()
w(f"    区域分布 = {dict(regs)}")
con.close()

# ---- B. 面板接口 ----
w("")
w("[B] 面板接口（只发 GET）")
st, ov = getjson("/api/overseas_sentiment")
if st is None:
    w(f"    /api/overseas_sentiment  失败：{ov}")
else:
    news = ov.get("news") or []
    w(f"    /api/overseas_sentiment  HTTP {st}  total={ov.get('total')}  "
      f"region_dist={ov.get('region_dist')}")
    w(f"        news 返回 {len(news)} 条，区域分布 = "
      f"{ {r: sum(1 for n in news if (n.get('region') or '') == r) for r in {n.get('region') for n in news}} }")

st2, hs = getjson("/api/overseas_hotspots")
if st2 is None:
    w(f"    /api/overseas_hotspots  失败：{hs}")
else:
    hs_l = hs if isinstance(hs, list) else (hs.get("items") or [])
    w(f"    /api/overseas_hotspots  HTTP {st2}  n={len(hs_l)}  "
      f"decision 非空={sum(1 for x in hs_l if x.get('decision'))}  "
      f"sectors_up_detail 类型={sorted({type(x.get('sectors_up_detail')).__name__ for x in hs_l})}")

st3, src = getjson("/api/overseas_sources")
if st3 is None:
    w(f"    /api/overseas_sources  失败：{src}")
else:
    rows = src.get("sources") or []
    dist: dict[str, int] = {}
    for r in rows:
        dist[r.get("status")] = dist.get(r.get("status"), 0) + 1
    w(f"    /api/overseas_sources  HTTP {st3}  n={len(rows)}")
    w(f"        status 分布 = {dist}")
    w(f"        带 enabled 字段 = {sum(1 for r in rows if 'enabled' in r)}/{len(rows)}，"
      f"enabled=1 共 {sum(1 for r in rows if r.get('enabled'))}")

for p in ("/api/summary", "/api/players", "/api/consensus"):
    stx, v = getjson(p)
    n = len(v) if isinstance(v, list) else (len(v) if isinstance(v, dict) else "?")
    w(f"    {p:<26} {'HTTP ' + str(stx) if stx else '失败'}  顶层条目={n}")

# ---- C. 故意不做 ----
# 2026-09-22 起 C 段永久移除：本段原本要 POST /api/demo、/api/overseas 来验证 confirm 守卫，
# 但**在守卫实现之前**这个请求本身就是破坏性的（load_demo 会 DELETE 14 张表）——
# 一旦脚本被误跑，707 条真实海外新闻立刻归零，正是本轮事故的翻版。
# 写接口守卫的验证必须在 **VIEWER_DB 指向副本** 的隔离实例上做，不得对着生产库。
w("")
w("[C] 已跳过：写接口守卫验证必须在隔离副本库（VIEWER_DB/VIEWER_PORT）上做，")
w("    对生产库发 POST /api/demo 本身即破坏性操作。见 tools/_qa_fix_backend2.txt。")

OUT.write_text("\n".join(L), encoding="utf-8")
print("done")
