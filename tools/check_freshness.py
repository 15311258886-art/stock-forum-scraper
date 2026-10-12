# -*- coding: utf-8 -*-
"""排查「最后更新」列两类行（未更新 vs 未纳入刷新）的成因。

要回答的问题：截图里 A 层级选手显示「⚠ 49 分钟前 未更新」，B/C/D 层级显示「未纳入刷新」。
是"数据源查找失败"，还是"数据源缺失 / 刷新条件不满足 / 配置限制"？
逐项取事实：跟踪来源清单、refresh_status 逐来源记录、players 的来源字段分布。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from viewer.db import Database          # noqa: E402
from viewer import kb, server           # noqa: E402

db = Database()
print("DB =", db.path, "size =", os.path.getsize(db.path))
print()

# ---------- 1. 被跟踪（参与时效判定）的来源 ----------
print("=" * 70)
print("[1] /api/refresh_status 的 tracked_sources（= 配置里声明的抓取来源）")
rs = server.api_refresh_status()
print("  tracked_sources :", rs["tracked_sources"])
print("  interval_seconds:", rs["interval_seconds"], " stale_seconds:", rs["stale_seconds"])
print("  整轮 __all__    :", rs.get("last_ok_at"), "status =", rs.get("status"))
print("  逐来源记录：")
for s in rs["sources"]:
    tracked = "✓跟踪" if s["source_key"] in rs["tracked_sources"] else "✗未跟踪"
    print(f"    {s['source_key']:<14} {tracked}  last_ok_at={s['last_ok_at']} "
          f"status={s['status']} name={s['display_name']}")

# ---------- 2. 配置里到底声明了哪些站点 ----------
print()
print("=" * 70)
print("[2] 抓取配置（决定 tracked_sources 的范围）")
try:
    cfg = server.load_config()
    print("  config sites:", list((cfg.get("sites") or {}).keys()))
except Exception as e:
    print("  load_config 失败:", e)
print("  kb_sources  :", [(c.get("doc_key"), c.get("track")) for c in kb.load_config()])

# ---------- 3. players 表的来源字段分布 ----------
print()
print("=" * 70)
print("[3] players 表：来源字段分布（时效列按哪个字段取值）")
cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(players)")]
print("  players 列:", cols)
for field in ("platform", "channel", "tier"):
    if field in cols:
        rows = db.conn.execute(
            f"SELECT COALESCE(NULLIF({field},''),'<空>') AS v, COUNT(*) n "
            f"FROM players GROUP BY v ORDER BY n DESC").fetchall()
        print(f"  {field:<9}:", {r["v"]: r["n"] for r in rows})

print()
print("  截图里出现的选手逐行实况：")
names = ["打板客小李", "尾盘狙击", "短线作手老王", "盘中快讯",
         "量化小张", "复盘笔记", "价值投资人", "老股民陈叔", "趋势猎人"]
q = ("SELECT name, tier, channel, platform, is_visible, is_certified, "
     "COALESCE(source_url,'') AS src FROM players WHERE name=?")
for n in names:
    r = db.conn.execute(q, (n,)).fetchone()
    if not r:
        print(f"    {n:<12} <players 表中不存在>")
        continue
    print(f"    {n:<12} tier={r['tier']:<3} channel={r['channel'] or '<空>':<10} "
          f"platform={r['platform'] or '<空>':<10} visible={r['is_visible']} src={r['src'][:38]}")

# ---------- 4. 各业务表里的 site 分布 ----------
print()
print("=" * 70)
print("[4] 业务表的 site / 来源分布（时效列的数据来源）")
for tbl in ("positions", "trades", "posts", "raw_pages"):
    if tbl not in {r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        continue
    tcols = [r["name"] for r in db.conn.execute(f"PRAGMA table_info({tbl})")]
    if "site" not in tcols:
        print(f"  {tbl:<10} <无 site 列> 列={tcols[:8]}")
        continue
    rows = db.conn.execute(
        "SELECT COALESCE(NULLIF(site,''),'<空>') AS v, COUNT(*) n "
        f"FROM {tbl} GROUP BY v ORDER BY n DESC").fetchall()
    print(f"  {tbl:<10}", {r["v"]: r["n"] for r in rows})

print()
print("  raw_pages 逐来源最近抓取时刻（时效列回填/判定的依据）：")
for r in db.conn.execute(
        "SELECT site, COUNT(*) n, MAX(fetched_at) mx FROM raw_pages "
        "GROUP BY site ORDER BY n DESC").fetchall():
    print(f"    {r['site'] or '<空>':<14} 页数={r['n']:<5} 最近抓取={r['mx']}")

# ---------- 5. meta 里的抓取时间 ----------
print()
print("=" * 70)
print("[5] meta 关键键（时间基准）")
for k in ("last_ingest", "last_fetch_at"):
    print(f"  {k:<14} = {db.get_meta(k)}")

# ---------- 6. 现在的真实时间与相对差 ----------
print()
print("=" * 70)
print("[6] 当前时间与判定基准")
from datetime import datetime           # noqa: E402
now = datetime.now()
print("  now =", now.strftime("%Y-%m-%d %H:%M:%S"))
for s in rs["sources"]:
    if s["source_key"] in rs["tracked_sources"] and s["last_ok_at"]:
        try:
            t = datetime.strptime(s["last_ok_at"][:19], "%Y-%m-%d %H:%M:%S")
            mins = (now - t).total_seconds() / 60
            verdict = "超时(>%.0f分)" % (rs["stale_seconds"] / 60) if mins * 60 > rs["stale_seconds"] else "未超时"
            print(f"    {s['source_key']:<14} 距今 {mins:.1f} 分钟 → {verdict}")
        except ValueError as e:
            print(f"    {s['source_key']:<14} 时间解析失败 {s['last_ok_at']} ({e})")
