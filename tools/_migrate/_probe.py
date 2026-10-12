# -*- coding: utf-8 -*-
"""只读探针：确认 trades/positions 的真实列与索引，及 trades_v2 的若干明细。"""
from __future__ import annotations
import sqlite3
import json
from pathlib import Path

COPY = Path(__file__).resolve().parent / "app.db"
OUT = Path(__file__).resolve().parent / "probe.txt"


def cols(conn, t):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]


def main():
    c = sqlite3.connect(str(COPY))
    c.row_factory = sqlite3.Row
    lines = []
    for t in ("trades", "positions", "players", "posts"):
        cc = cols(c, t)
        lines.append(f"[{t}] cols({len(cc)}) = {cc}")
        lines.append(f"    has axis = {'axis' in cc}")
    lines.append("")
    for t in ("trades", "trades_v2"):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (t,)).fetchone()
        lines.append(f"[{t}] DDL:")
        lines.append(sql[0] if sql else "(none)")
        idx = c.execute(f"PRAGMA index_list({t})").fetchall()
        lines.append(f"[{t}] indexes = " +
                     json.dumps([{k: r[k] for k in r.keys()} for r in idx], ensure_ascii=False))
        lines.append("")
    lines.append("[trades_v2] 有码但无名称的 symbol:")
    for r in c.execute("SELECT symbol, COUNT(*) n FROM trades_v2 "
                       "WHERE symbol IS NOT NULL AND symbol<>'' AND "
                       "(symbol_name IS NULL OR symbol_name='') GROUP BY symbol"):
        lines.append(f"    {r['symbol']}  x{r['n']}")
    lines.append("")
    lines.append("[trades_v2] 作者按 tier 分布 (players.left-join):")
    for r in c.execute(
        "SELECT COALESCE(pl.tier,'<no-player>') tier, COUNT(DISTINCT t.author) authors, "
        "COUNT(*) rows FROM trades_v2 t LEFT JOIN players pl ON pl.name=t.author "
        "GROUP BY tier ORDER BY rows DESC"
    ):
        lines.append(f"    {r['tier']:<12} authors={r['authors']:<4} rows={r['rows']}")
    lines.append("")
    lines.append("[trades_v2] Tier A 作者行数:")
    for r in c.execute(
        "SELECT t.author, COUNT(*) n FROM trades_v2 t JOIN players pl ON pl.name=t.author "
        "WHERE pl.tier='A' GROUP BY t.author ORDER BY n DESC"
    ):
        lines.append(f"    {r['author']}  {r['n']}")
    lines.append("")
    lines.append("[trades_v2] players.axis 非空的作者数:")
    lines.append("    " + str(c.execute(
        "SELECT COUNT(DISTINCT name) n FROM players WHERE axis IS NOT NULL AND axis<>''"
    ).fetchone()["n"]))
    c.close()
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
