# -*- coding: utf-8 -*-
"""从 blogger_reco 重算「博主效果对比」表 blogger_stats（幂等，可反复执行）。

逻辑已收敛到顶层模块 ``blogger_rebuild.py``（`rebuild_stats`）——本脚本只是它的 CLI 外壳：
打开库 → 抓重算前快照 → 调 `blogger_rebuild.rebuild_stats(conn)` → 打印前后对比。
（server 读取时自愈、Excel 导入自愈钩子、本 CLI 三处共用同一份实现，避免口径漂移。）

口径见 ``blogger_rebuild`` 模块 docstring。要点：null 绝不当 0；avg_ret_1w 保留原值；
末尾保留一行「全部博主合计」。

用法：
    python tools/rebuild_blogger_stats.py
    python tools/rebuild_blogger_stats.py --db X.db
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


def _fmt(v):
    return "—" if v is None else v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 blogger_reco 重算 blogger_stats（幂等）")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标 SQLite 库（默认 viewer/app.db）")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        before = {r["blogger"]: dict(r) for r in con.execute("SELECT * FROM blogger_stats")}
        res = rb.rebuild_stats(con)
        after = {r["blogger"]: dict(r) for r in con.execute("SELECT * FROM blogger_stats")}
    finally:
        con.close()

    print("重算完成 → %s" % os.path.abspath(args.db))
    if res.get("added_col"):
        print("  · 已新增列 n_pending（ALTER TABLE）")
    print("  博主                     n_stock(前→后)   n_pending   avg_ret(前→后)     up_ratio(前→后)   avg_ret_1w(保留)")
    for name, a in after.items():
        b = before.get(name, {})
        print("  %-22s %5s→%-5s %9s   %5s→%-7s   %5s→%-7s   %s" % (
            name[:22],
            _fmt(b.get("n_stock")), _fmt(a.get("n_stock")),
            _fmt(a.get("n_pending")),
            _fmt(b.get("avg_ret")), _fmt(a.get("avg_ret")),
            _fmt(b.get("up_ratio")), _fmt(a.get("up_ratio")),
            _fmt(a.get("avg_ret_1w"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
