# -*- coding: utf-8 -*-
"""从 blogger_reco 重算「重复推荐」表 blogger_overlap（幂等，可反复执行）。

逻辑已收敛到顶层模块 ``blogger_rebuild.py``（`rebuild_overlap`）——本脚本只是它的 CLI 外壳。
口径见 ``blogger_rebuild`` 模块 docstring。要点（2026-10-09 王总按备份反推源表口径确认）：
  · 入选 = 被 ≥2 个不同 blogger 推过的 stock_code；
  · **最新非空口径**：按 rec_date 升序取最后一条 ret_since 非空的记录，
    price_at_rec / close_latest / ret_since 三数同源取自该条；全空则三者皆 NULL（不补 0）；
  · note 保留人工原值，新行写「自动汇总：N 位博主同推」；幂等 UPSERT，只增改不删。

用法：
    python tools/rebuild_blogger_overlap.py
    python tools/rebuild_blogger_overlap.py --db X.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import blogger_rebuild as rb  # noqa: E402

DEFAULT_DB = os.environ.get("VIEWER_DB") or os.path.join(PROJ, "viewer", "app.db")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 blogger_reco 重算 blogger_overlap（幂等）")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标 SQLite 库（默认 viewer/app.db）")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        before = {r["stock_code"]: dict(r) for r in con.execute("SELECT * FROM blogger_overlap")}
        res = rb.rebuild_overlap(con)
        after = {r["stock_code"]: dict(r) for r in con.execute("SELECT * FROM blogger_overlap")}
    finally:
        con.close()

    print("重算完成 → %s" % os.path.abspath(args.db))
    print("  行数 %d → %d" % (len(before), len(after)))
    print("  新增：%s" % ("、".join(res["new_codes"]) or "无"))
    print("  变更博主名单的：%s" % ("、".join(res["updated_codes"]) or "无"))
    for code in sorted(after):
        a = after[code]
        mark = " NEW" if code in res["new_codes"] else ""
        print("  %s %-8s n=%d %s ret_since=%s note=%s%s" % (
            code, a.get("stock_name") or "", a.get("n_bloggers") or 0,
            a.get("bloggers") or "", a.get("ret_since"), (a.get("note") or "")[:28], mark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
