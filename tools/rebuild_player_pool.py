# -*- coding: utf-8 -*-
"""一次性重建选手池（行为证据版），并打印前后对比。

背景：示例数据清理后 players 只剩淘股吧 68 人（东财/同花顺无公开选手榜、雪球未配选手页）。
本脚本跨站从真实发帖/交易/持仓聚合作者，按行为证据分补足入选名单至 ~200，并保持来源多样。
幂等：重复执行结果一致（既有行不覆盖，新增行按证据分填充）。

用法：
    .venv/Scripts/python.exe tools/rebuild_player_pool.py [--target 200]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from viewer.db import Database  # noqa: E402
from viewer.ingest import Ingestor  # noqa: E402


def _stats(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        total = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        by_site = dict(con.execute("SELECT site, COUNT(*) FROM players GROUP BY site").fetchall())
        top = con.execute("SELECT COUNT(*) FROM players WHERE is_top_player=1").fetchone()[0]
        return {"total": total, "by_site": by_site, "top": top}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=None, help="入选总规模（默认取配置 selection.top_n=200）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（默认备份）")
    args = ap.parse_args()

    db_path = str(Path(ROOT) / "viewer" / "app.db")
    if not os.path.exists(db_path):
        print(f"[ERR] 生产库不存在: {db_path}")
        return 1

    if not args.no_backup:
        bak = f"{db_path}.bak.before-playerpool-{time.strftime('%Y%m%d-%H%M%S')}"
        # WAL 模式：先 checkpoint 再复制，保证备份文件完整
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        shutil.copy2(db_path, bak)
        print(f"[1/3] 已备份生产库 -> {bak}")

    before = _stats(db_path)
    print(f"[2/3] 重建前: total={before['total']} top={before['top']} by_site={before['by_site']}")

    db = Database(db_path)
    ing = Ingestor(db=db)
    res = ing.rebuild_player_pool(target_total=args.target)
    print(f"      重建结果: {res}")

    after = _stats(db_path)
    print(f"[3/3] 重建后: total={after['total']} top={after['top']} by_site={after['by_site']}")
    print("      渠道分布:", dict(sqlite3.connect(db_path)
                                .execute("SELECT tier, COUNT(*) FROM players GROUP BY tier ORDER BY tier")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
