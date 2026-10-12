"""结果存储：JSON / CSV / SQLite。输入可为 dataclass 或 dict。"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime
from typing import Iterable


def _normalize(records: Iterable) -> list[dict]:
    rows = []
    for r in records:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        rows.append(d)
    return rows


def save_json(records: Iterable, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_normalize(records), f, ensure_ascii=False, indent=2)


def save_csv(records: Iterable, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = _normalize(records)
    if not rows:
        open(path, "w", encoding="utf-8-sig").close()
        return
    keys = list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def save_sqlite(records: Iterable, path: str, table: str = "records") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = _normalize(records)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    if rows:
        cols = list(rows[0].keys())
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            + ", ".join(f'"{c}" TEXT' for c in cols)
            + ")"
        )
        ph = ", ".join("?" * len(cols))
        for row in rows:
            cur.execute(
                f"INSERT INTO {table} VALUES ({ph})",
                [row.get(c) for c in cols],
            )
    conn.commit()
    conn.close()
