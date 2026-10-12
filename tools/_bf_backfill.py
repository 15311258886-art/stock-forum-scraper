# -*- coding: utf-8 -*-
"""一次性回填：按 config/overseas_sources.yaml 的 enabled 值刷新 overseas_sources_status.enabled。
不做任何抓取、不改其他表。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from viewer.db import Database                 # noqa: E402
from overseas.sources import load_overseas_config, sources_of  # noqa: E402

db = Database()
cfg = load_overseas_config()
srcs = sources_of(cfg, enabled_only=False)
n = 0
for s in srcs:
    key = s.get("key") or ""
    if not key:
        continue
    cur = db.conn.execute(
        "UPDATE overseas_sources_status SET enabled=? WHERE source_key=?",
        (int(bool(s.get("enabled"))), key))
    n += cur.rowcount
db.conn.commit()

rows = db.conn.execute(
    "SELECT source_key, name, region, enabled, status FROM overseas_sources_status "
    "ORDER BY enabled DESC, region, name").fetchall()
print(f"config sources={len(srcs)} updated_rows={n} table_rows={len(rows)}")
on = sum(1 for r in rows if r["enabled"])
print(f"enabled=1: {on}  enabled=0: {len(rows) - on}")
for r in rows:
    print(f"  {r['enabled']}  {r['source_key']:<22} {r['region']:<7} {r['status']}")
