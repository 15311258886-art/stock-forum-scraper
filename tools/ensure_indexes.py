# -*- coding: utf-8 -*-
"""数据层索引补齐（2026-10-31 新增，性能缺陷的持久化修法）。

## 背景

`data/kline_daily.db` 的 `kline` 表原本只有 `idx_kline_code(code)` 单列索引。
回测要按 "**每只票最近 N 根 bar**" 取数，写法是：

    SELECT d, o, h, l FROM kline WHERE code=? ORDER BY d DESC LIMIT 280

单列索引能定位到该 code 的全部行（约 3,000 行），但 `ORDER BY d DESC` 需要
把这些行全读出来再排序 —— 17,085,294 行的库上，等于每只票做几千次随机读。

实测症状（不是估计）：全量回测（5,337 只）CPU 利用率只有 **21%**，
其余时间全在等磁盘；曾经 25 分钟跑完的作业变成 70 分钟以上。

补 `idx_kline_code_d(code, d)` 之后，同一条 SQL 变成索引区间反向扫描：

    000001  1.1 ms   /  600519  1.6 ms  /  000544  9.3 ms

## 为什么要单独一个脚本

因为 `*.db` 在 .gitignore 里 —— 换机、重建数据、从备份恢复，索引都会消失，
而**代码不会因为缺索引而报错**，只会默默地慢几十倍。这种"不报错的退化"
必须有一个可重跑的入口，而不是靠谁记得手敲过一条 CREATE INDEX。

用法：
    python tools/ensure_indexes.py          # 补齐（幂等）
    python tools/ensure_indexes.py --check  # 只检查，缺则 exit 1
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (库文件, 索引名, CREATE 语句, 说明)
INDEXES = [
    ("data/kline_daily.db", "idx_kline_code_d",
     "CREATE INDEX IF NOT EXISTS idx_kline_code_d ON kline(code, d)",
     "回测按「每只票最近 N 根 bar」取数，需 (code, d) 才能走索引反向扫描"),
    ("viewer/app.db", "idx_sid_code_date",
     "CREATE INDEX IF NOT EXISTS idx_sid_code_date ON stock_indicators_daily(code, date)",
     "指标表按 code 取全历史，回测主循环每只票一次"),
]


def _has_index(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is not None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv
    rc = 0
    for db, name, ddl, why in INDEXES:
        path = os.path.join(ROOT, db)
        if not os.path.exists(path):
            print("[SKIP] %s 不存在" % db)
            continue
        con = sqlite3.connect(path, timeout=300)
        try:
            if _has_index(con, name):
                print("[OK]   %s :: %s" % (db, name))
                continue
            if check_only:
                print("[FAIL] %s 缺索引 %s —— %s" % (db, name, why))
                rc = 1
                continue
            print("[BUILD] %s :: %s （%s）" % (db, name, why))
            con.execute(ddl)
            con.commit()
            print("[DONE] %s :: %s" % (db, name))
        finally:
            con.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
