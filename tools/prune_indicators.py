# -*- coding: utf-8 -*-
"""stock_indicators_daily 历史瘦身（对应 docs/storage_plan.md 的 M4）。

做法（不用 DELETE，改用「建新表 + 搬近端 + 换名」）：
  DELETE 1179 万行在 SQLite 上要写满 8GB 级 WAL、耗时以小时计；
  而近端只有 137 万行，直接建新表搬过去再 VACUUM，量级差一个数量级。

默认 dry-run（只统计、不写）；加 --apply 才真正执行。

⚠️ 已知影响（务必知情）：
  · signal_backtest.py / security_master.py 会读 260 日以前的行 —— 回测与「全表聚合」
    的口径会变（退市票可能不再出现）。回测结论正驱动 SIGNAL_WEIGHTS，删前请确认。
  · 本脚本只动 stock_indicators_daily 一张表，不碰其他表。

用法：
  python tools/prune_indicators.py                # 预演
  python tools/prune_indicators.py --apply        # 执行
  python tools/prune_indicators.py --days 500 --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "app.db")
TABLE = "stock_indicators_daily"
NEW = TABLE + "__prune_new"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.2f %s" % (n, unit)
        n /= 1024
    return "%.2f GB" % n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=260, help="保留最近 N 个交易日（默认 260）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预演）")
    a = ap.parse_args()

    if not os.path.exists(DB):
        print("找不到数据库: %s" % DB)
        return 1

    conn = sqlite3.connect(DB, timeout=60, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    print("数据库: %s (%s)" % (DB, human(os.path.getsize(DB))))

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM %s ORDER BY date DESC LIMIT ?" % TABLE, (a.days + 2,))]
    if len(dates) < a.days:
        print("交易日不足 %d 天（实际 %d），无需瘦身。" % (a.days, len(dates)))
        return 0
    boundary = dates[a.days - 1]
    print("最新交易日: %s" % dates[0])
    print("保留边界  : %s（最近 %d 个交易日）" % (boundary, a.days))

    total = conn.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    keep = conn.execute("SELECT COUNT(*) FROM %s WHERE date >= ?" % TABLE, (boundary,)).fetchone()[0]
    drop = total - keep
    print("总行数 %s / 保留 %s / 删除 %s（%.1f%%）"
          % (f"{total:,}", f"{keep:,}", f"{drop:,}", drop * 100.0 / max(total, 1)))
    print("保留日期范围: %s ~ %s" % (
        conn.execute("SELECT MIN(date) FROM %s WHERE date>=?" % TABLE, (boundary,)).fetchone()[0],
        conn.execute("SELECT MAX(date) FROM %s" % TABLE).fetchone()[0]))

    # 近端行数占比 → 预估瘦身后体积
    est = os.path.getsize(DB) * keep / max(total, 1)
    print("预估瘦身后库体积: 约 %s（当前 %s）" % (human(est), human(os.path.getsize(DB))))

    if not a.apply:
        print("\n[预演] 未做任何修改。确认无误后加 --apply 执行。")
        conn.close()
        return 0

    # ---------------- 真正执行 ----------------
    src_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if not src_sql or not src_sql[0]:
        print("拿不到建表语句，中止。")
        return 1
    new_sql = re.sub(r"(?i)CREATE\s+TABLE\s+[\"']?%s[\"']?" % TABLE,
                     "CREATE TABLE %s" % NEW, src_sql[0], count=1)

    t0 = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS %s" % NEW)
        conn.execute(new_sql)
        n = conn.execute("INSERT INTO %s SELECT * FROM %s WHERE date >= ?" % (NEW, TABLE),
                         (boundary,)).rowcount
        print("已搬移近端行数: %s" % f"{n:,}")
        conn.execute("DROP TABLE %s" % TABLE)
        conn.execute("ALTER TABLE %s RENAME TO %s" % (NEW, TABLE))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sid_date ON %s(date)" % TABLE)
        conn.execute("COMMIT")
        print("表重建完成，耗时 %.1fs" % (time.time() - t0))
    except Exception as e:  # noqa: BLE001
        conn.execute("ROLLBACK")
        print("失败已回滚: %s %s" % (type(e).__name__, e))
        conn.close()
        return 1

    print("开始 VACUUM（重写全库，可能需要几分钟）…")
    t1 = time.time()
    conn.execute("VACUUM")
    print("VACUUM 完成，耗时 %.1fs" % (time.time() - t1))

    # 验证
    after = conn.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    rng = conn.execute("SELECT MIN(date), MAX(date) FROM %s" % TABLE).fetchone()
    print("\n=== 验证 ===")
    print("行数 %s（预期 %s）%s" % (f"{after:,}", f"{keep:,}", "✅" if after == keep else "❌ 不一致"))
    print("日期范围 %s ~ %s" % rng)
    print("库体积 %s" % human(os.path.getsize(DB)))
    # 抽一只票看数据是否连续可用
    row = conn.execute("SELECT date,code,name,close FROM %s ORDER BY date DESC LIMIT 1" % TABLE).fetchone()
    print("最新一行: %s" % (row,))
    conn.close()
    print("\n完成。回滚方式：用执行前的备份文件覆盖 app.db。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
