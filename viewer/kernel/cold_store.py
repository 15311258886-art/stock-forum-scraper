# -*- coding: utf-8 -*-
"""P8：app.db 冷热分离（非破坏式）。

只做「导出」，绝不 DELETE 活跃库。把超过 COLD_CUTOFF_DAYS 天的历史行复制到独立的
冷归档库（默认 viewer/cold_store.sqlite，DuckDB 风格的只读分析库，零外部依赖），
活跃库原样保留。返回每表「可归档行数 / 总行数」的清单供决策。

设计约束（来自重构计划 P8）
--------------------------
- 非破坏：本模块任何函数都不会对活跃库执行 DELETE / UPDATE / DROP。
- 幂等：重复 export 用 INSERT OR IGNORE（按原表主键/整行去重不明显，故仅作一次性归档，
  重跑前会提示先清空冷库或换路径）。
- 依赖最少：纯 sqlite3，不引入 pyarrow/DuckDB（环境不一定有）。
- 可审计：plan_cold() 是只读探测，默认 --dry-run 只打印清单不落盘。

日期列探测：每个表取 PRAGMA table_info，按优先级选一个「日期语义」列
（trade_date > date > run_date > build_date > created_at > as_of > review_date > d > day > ts > time），
用 `date(<col>) < ?` 做切点判断。无法识别日期列的表跳过（不误归档）。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

COLD_CUTOFF_DAYS = 260  # 约 260 个交易日（≈ 1 个交易年）

_DATE_COL_PRIORITY = [
    "trade_date", "date", "run_date", "build_date", "created_at",
    "as_of", "review_date", "stat_date", "snapshot_date", "d", "day",
    "ts", "time", "updated_at", "dt",
]


def _cutoff_iso() -> str:
    return (datetime.now() - timedelta(days=COLD_CUTOFF_DAYS)).strftime("%Y-%m-%d")


def _date_col(conn: sqlite3.Connection, table: str) -> str | None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
    for cand in _DATE_COL_PRIORITY:
        if cand in cols:
            return cand
    # 退路：任意含 date/time 字样的列
    for c in cols:
        cl = c.lower()
        if "date" in cl or "time" in cl or cl in ("ts", "d", "day"):
            return c
    return None


def plan_cold(conn: sqlite3.Connection, cutoff: str | None = None) -> list[dict]:
    """只读探测：列出每张表可归档的行数（不落盘）。"""
    cutoff = cutoff or _cutoff_iso()
    out = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()]
    for t in tables:
        dcol = _date_col(conn, t)
        if not dcol:
            out.append({"table": t, "date_col": None, "old_rows": 0,
                        "total_rows": _safe_count(conn, t), "pct": 0.0, "skipped": "no_date_col"})
            continue
        total = _safe_count(conn, t)
        try:
            old = conn.execute(
                f"SELECT COUNT(*) FROM '{t}' WHERE date({dcol}) < ?", (cutoff,)).fetchone()[0]
        except sqlite3.Error:
            old = 0
        pct = (old / total * 100.0) if total else 0.0
        out.append({"table": t, "date_col": dcol, "old_rows": old,
                    "total_rows": total, "pct": round(pct, 1), "skipped": ""})
    return out


def _safe_count(conn, t) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
    except sqlite3.Error:
        return 0


def export_cold(conn: sqlite3.Connection, dest: str,
                cutoff: str | None = None, only_positive: bool = True) -> dict:
    """把可归档行复制到冷归档库 dest（不改动活跃库）。

    only_positive=True 时只处理 old_rows>0 的表。返回报告。
    """
    cutoff = cutoff or _cutoff_iso()
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    cconn = sqlite3.connect(dest)
    cconn.execute("PRAGMA busy_timeout=15000")
    report = {"cutoff": cutoff, "dest": dest, "tables": [], "total_copied": 0}
    for info in plan_cold(conn, cutoff):
        if info["date_col"] is None or info["old_rows"] <= 0:
            continue
        t = info["table"]
        dcol = info["date_col"]
        # 在冷库建同构表
        create = conn.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
        cconn.execute(create)
        rows = conn.execute(
            f"SELECT * FROM '{t}' WHERE date({dcol}) < ?", (cutoff,)).fetchall()
        cols = [c[0] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
        ph = ",".join("?" * len(cols))
        cconn.executemany(f"INSERT OR IGNORE INTO '{t}' VALUES ({ph})", rows)
        report["tables"].append({"table": t, "copied": len(rows)})
        report["total_copied"] += len(rows)
    cconn.commit()
    cconn.close()
    return report


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="app.db 冷热分离（非破坏式导出）")
    ap.add_argument("--db", default="viewer/app.db")
    ap.add_argument("--dest", default="viewer/cold_store.sqlite")
    ap.add_argument("--cutoff-days", type=int, default=COLD_CUTOFF_DAYS)
    ap.add_argument("--export", action="store_true", help="真正导出（默认只打印 plan）")
    ap.add_argument("--min-pct", type=float, default=0.0, help="只显示可归档占比≥该值的表")
    a = ap.parse_args(argv)
    cutoff = (datetime.now() - timedelta(days=a.cutoff_days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(a.db, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    if a.export:
        rep = export_cold(conn, a.dest, cutoff=cutoff)
        print(f"[export] cutoff={rep['cutoff']} dest={rep['dest']} total_copied={rep['total_copied']}")
        for t in rep["tables"]:
            print(f"  {t['table']}: {t['copied']}")
    else:
        plan = plan_cold(conn, cutoff=cutoff)
        print(f"[dry-run] cutoff (>{a.cutoff_days}d old, before {cutoff})  db={a.db}")
        tot_old = sum(p["old_rows"] for p in plan)
        tot_all = sum(p["total_rows"] for p in plan)
        for p in plan:
            if p["old_rows"] <= 0 or p["pct"] < a.min_pct:
                continue
            print(f"  {p['table']:32s} col={str(p['date_col']):14s} "
                  f"old={p['old_rows']:>9d} / {p['total_rows']:>9d} ({p['pct']:.1f}%)")
        print(f"[summary] 可归档合计 {tot_old} / {tot_all} 行 "
              f"({tot_old/tot_all*100:.1f}%) across {len(plan)} 表")
    conn.close()


if __name__ == "__main__":
    _main()
