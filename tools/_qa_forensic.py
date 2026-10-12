# -*- coding: utf-8 -*-
"""只读取证：核查 general-purpose-2 报告的"验收期间数据被改写"疑点。

疑点（引其原话）：
  23:45 探针里 600519 选手是「打板小猛 / buy_time 21:53」、overseas HK 有 102 行；
  23:49 变成「打板小郭 / 23:47」、HK 只剩 2 行（app.js 也从 73267B 变成 75658B）。

本脚本只做 SELECT / 文件 stat，不做任何写库写文件（输出文件除外）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "viewer" / "app.db"
OUT = ROOT / "tools" / "_qa_forensic.txt"

L: list[str] = []


def w(s: str = "") -> None:
    L.append(s)


def ts(x) -> str:
    try:
        return datetime.fromtimestamp(float(x)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return repr(x)


def sec(t: str) -> None:
    w("")
    w("=" * 72)
    w(t)
    w("=" * 72)


def q1(con, sql, args=()):
    try:
        cur = con.execute(sql, args)
        return cur.fetchall()
    except Exception as e:
        return [("ERR", f"{type(e).__name__}: {e}")]


# ---------------------------------------------------------------- 1. 文件层
sec("1. 文件层：app.db / -wal / -shm 与关键代码文件的 mtime、size")
stat_rows = []
for name in ("app.db", "app.db-wal", "app.db-shm"):
    p = ROOT / "viewer" / name
    if p.exists():
        st = p.stat()
        stat_rows.append((name, st.st_size, ts(st.st_mtime)))
    else:
        stat_rows.append((name, "-", "不存在"))
code_rows = []
for rel in ("viewer/static/app.js", "viewer/server.py", "viewer/db.py",
            "overseas/sources.py", "overseas/collect.py"):
    p = ROOT / rel
    if p.exists():
        st = p.stat()
        code_rows.append((rel, st.st_size, ts(st.st_mtime)))
    else:
        code_rows.append((rel, "-", "不存在"))

w(f"{'文件':<28}{'size':>10}  mtime")
for n, s, t in stat_rows + code_rows:
    w(f"{n:<28}{str(s):>10}  {t}")

# ---------------------------------------------------------------- 2. 库连接
if not DB.exists():
    w("")
    w(f"[致命] 数据库不存在：{DB}")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print("done")
    raise SystemExit(0)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

sec("2. meta 全表（找 overseas_last_collect / demo 标记 / 任何写入痕迹）")
rows = q1(con, "SELECT * FROM meta")
if rows and rows[0][0] == "ERR":
    w(str(rows))
else:
    for r in rows:
        d = dict(r)
        keys = list(d.keys())
        w(" | ".join(f"{k}={d[k]!r}" for k in keys))

sec("3. 海外新闻 overseas_news：区域分布 + 条数")
rows = q1(con, "SELECT COALESCE(region,'(null)') AS reg, COUNT(*) AS n, "
               "MIN(COALESCE(publish_time,0)) AS mn, MAX(COALESCE(publish_time,0)) AS mx "
               "FROM overseas_news GROUP BY 1 ORDER BY n DESC")
if rows and rows[0][0] == "ERR":
    w(str(rows))
else:
    w(f"{'region':<18}{'条数':>8}   publish_time 最早           最晚")
    for reg, n, mn, mx in rows:
        w(f"{str(reg):<18}{n:>8}   {ts(mn)}  {ts(mx)}")
    tot = q1(con, "SELECT COUNT(*) FROM overseas_news")
    w(f"{'合计':<18}{tot[0][0]:>8}")

sec("4. overseas_sources_status：checked_at 分组计数（是否同一快照）")
rows = q1(con, "SELECT checked_at, COUNT(*) AS n, "
               "GROUP_CONCAT(source_key) AS keys FROM overseas_sources_status "
               "GROUP BY checked_at ORDER BY n DESC")
if rows and rows[0][0] == "ERR":
    w(str(rows))
else:
    for ca, n, keys in rows:
        w(f"checked_at={ts(ca)}  行数={n}")
        w(f"   keys={keys}")
    w("")
    w("-- 每行的 status / enabled 摘要 --")
    rows2 = q1(con, "SELECT source_key, status, enabled, checked_at, item_count "
                    "FROM overseas_sources_status ORDER BY source_key")
    if rows2 and rows2[0][0] == "ERR":
        w(str(rows2))
    else:
        w(f"{'source_key':<26}{'enabled':>8}{'items':>7}  {'checked_at':<21}status")
        for sk, stt, en, ca, ic in rows2:
            w(f"{str(sk):<26}{str(en):>8}{str(ic):>7}  {ts(ca):<21}{stt}")

sec("5. overseas_hotspots：行数与时间戳")
rows = q1(con, "SELECT COUNT(*) FROM overseas_hotspots")
w(f"总行数 = {rows[0][0]}")
cols = q1(con, "PRAGMA table_info(overseas_hotspots)")
colnames = [r[1] for r in cols]
w(f"列 = {colnames}")
tcol = None
for cand in ("created_at", "generated_at", "updated_at", "ts"):
    if cand in colnames:
        tcol = cand
        break
if tcol:
    rows = q1(con, f"SELECT {tcol}, COUNT(*) FROM overseas_hotspots GROUP BY 1 "
                   f"ORDER BY 1 DESC LIMIT 12")
    for v, n in rows:
        w(f"  {tcol}={ts(v)}  n={n}")

sec("6. 国内侧计数：posts / players / positions / trades / stock_mentions")
for t in ("posts", "players", "positions", "trades", "stock_mentions",
          "player_daily", "signals", "hotspots"):
    rows = q1(con, f"SELECT COUNT(*) FROM {t}")
    if rows and rows[0][0] == "ERR":
        w(f"{t:<18} -- {rows[0][1]}")
    else:
        w(f"{t:<18} {rows[0][0]}")

sec("7. players 前 12 位（找「打板小猛」 vs 「打板小郭」）")
cols = q1(con, "PRAGMA table_info(players)")
colnames = [r[1] for r in cols]
w(f"列 = {colnames}")
rows = q1(con, "SELECT * FROM players LIMIT 12")
if rows and rows[0][0] == "ERR":
    w(str(rows))
else:
    for r in rows:
        w("  " + " | ".join(f"{k}={v!r}" for k, v in dict(r).items()))

sec("8. 600519 / 601318 的持仓与成交时间分布")
rows = q1(con, "PRAGMA table_info(positions)")
w(f"positions 列 = {[r[1] for r in rows]}")
for sym in ("600519", "601318"):
    rows = q1(con, "SELECT player, symbol, buy_time, sell_time, channel "
                    "FROM positions WHERE symbol=? ORDER BY buy_time", (sym,))
    if rows and rows[0][0] == "ERR":
        w(f"{sym}: {rows[0][1]}")
        continue
    w(f"-- {sym} 共 {len(rows)} 行 --")
    for r in rows:
        d = dict(r)
        bt = d.get("buy_time")
        stt = d.get("sell_time")
        w(f"   player={d.get('player')!r} buy={ts(bt) if bt else None} "
          f"sell={ts(stt) if stt else None} channel={d.get('channel')!r}")

sec("9. 分钟级写入痕迹：positions.buy_time 按分钟聚簇")
rows = q1(con, "SELECT COUNT(*) FROM positions WHERE buy_time IS NOT NULL")
w(f"buy_time 非空行数 = {rows[0][0]}")
rows = q1(con, "SELECT strftime('%Y-%m-%d %H:%M', buy_time, 'unixepoch') AS m, COUNT(*) AS n "
               "FROM positions WHERE buy_time IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 20")
if rows and rows[0][0] == "ERR":
    w(str(rows))
else:
    for m, n in rows:
        w(f"  {m}  n={n}")

sec("10. SQLite 头/元信息：写入计数器与 page 数")
for pragma in ("page_count", "freelist_count", "schema_version", "user_version",
               "data_version", "journal_mode"):
    rows = q1(con, f"PRAGMA {pragma}")
    w(f"PRAGMA {pragma} = {[tuple(r) for r in rows]}")

con.close()

sec("结论提示（人读）")
w("· 若第 1 节 app.db-wal 的 mtime 明显晚于 app.db 且体积可观 → 确有写入。")
w("· 若第 4 节 41 行 checked_at 全部相同 → 面板读的是同一份旧快照，不是本次重采。")
w("· 第 7 节 players 名单 + 第 8 节 600519 的 buy_time 可直接判定「小猛/小郭」与 21:53/23:47 之争。")
w("· 第 5 节 overseas_hotspots 的时间戳可判断热点是否被重建（rebuild_hotspots.py）。")

OUT.write_text("\n".join(L), encoding="utf-8")
print("done ->", OUT)
