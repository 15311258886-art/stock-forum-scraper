# -*- coding: utf-8 -*-
"""只读比对：app.db 与两份 .bak 快照的业务数据规模，判断是否可回滚。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "_qa_backup_check.txt"
L: list[str] = []

V = ROOT / "viewer"
targets = [("CURRENT", V / "app.db")]
for p in sorted(V.glob("app.db.bak-*")):
    targets.append((p.name, p))

TABLES = ["posts", "players", "positions", "trades", "stock_mentions", "news",
          "overseas_news", "overseas_mentions", "overseas_sentiment_daily",
          "overseas_hotspots", "overseas_sources_status"]


def ts(x):
    try:
        return datetime.fromtimestamp(float(x)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return repr(x)


L.append("备份/现状比对（只读）")
L.append("=" * 78)
for label, p in targets:
    if not p.exists():
        L.append(f"{label:<28} 不存在")
        continue
    st = p.stat()
    L.append(f"{label:<28} size={st.st_size:>10}  mtime={ts(st.st_mtime)}")
L.append("")
L.append("=" * 78)
L.append("各表行数 / 关键 meta")
L.append("=" * 78)

header = f"{'表':<26}" + "".join(f"{lab[:22]:>24}" for lab, _ in targets)
L.append(header)
data = {}
for label, p in targets:
    if not p.exists():
        continue
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rowscount = {}
    for t in TABLES:
        try:
            rowscount[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:
            rowscount[t] = f"ERR({type(e).__name__})"
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except Exception:
        meta = {}
    try:
        regs = dict(con.execute(
            "SELECT COALESCE(region,'(null)'), COUNT(*) FROM overseas_news GROUP BY 1").fetchall())
    except Exception:
        regs = {}
    try:
        stsrc = con.execute(
            "SELECT checked_at, COUNT(*) FROM overseas_sources_status GROUP BY 1").fetchall()
    except Exception:
        stsrc = []
    try:
        dsrc = con.execute(
            "SELECT COUNT(DISTINCT url) FROM overseas_news").fetchone()[0]
    except Exception:
        dsrc = "-"
    try:
        demourl = con.execute(
            "SELECT COUNT(*) FROM players WHERE source_url LIKE '%example.local%'").fetchone()[0]
    except Exception:
        demourl = "-"
    data[label] = dict(counts=rowscount, meta=meta, regs=regs, stsrc=stsrc,
                       distinct_url=dsrc, demo_players=demourl)
    con.close()

for t in TABLES:
    line = f"{t:<26}"
    for label, _ in targets:
        v = data.get(label, {}).get("counts", {}).get(t, "-")
        line += f"{str(v):>24}"
    L.append(line)

L.append("")
for label, _ in targets:
    d = data.get(label)
    if not d:
        continue
    L.append("-" * 78)
    L.append(f"[{label}]")
    L.append(f"  meta = {d['meta']}")
    L.append(f"  overseas_news 区域分布 = {d['regs']}   去重 url 数 = {d['distinct_url']}")
    L.append(f"  overseas_sources_status checked_at 分组 = {d['stsrc']}")
    L.append(f"  players 中 source_url 含 example.local 的行数 = {d['demo_players']}")

OUT.write_text("\n".join(L), encoding="utf-8")
print("done")
