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
    python tools/ensure_indexes.py                 # 补齐（幂等）+ 报告冗余索引
    python tools/ensure_indexes.py --check         # 只检查，缺索引或有冗余索引则 exit 1
    python tools/ensure_indexes.py --drop-redundant # 顺手删掉已知冗余索引（可回收空间）
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
    # 2026-11-01 补登记（P1-4 盘点时发现）：全代码库**没有任何一处**创建它 ——
    # 真库里有这条索引，是当初手工建的，没人负责重建。后果是「重建数据/换机/从备份恢复」后
    # 按日切片（avoid.py、server.py、signal_backtest.py 都这么查）会退化成 1,316 万行全表扫，
    # 而且**不报错**，只是慢 —— 正好是本脚本存在的理由。实测占 270.5 MB，是唯一能服务
    # `WHERE date=?` 的索引（主键前缀 (code,date) 吃不到按 date 的查询）。
    ("viewer/app.db", "idx_sid_date",
     "CREATE INDEX IF NOT EXISTS idx_sid_date ON stock_indicators_daily(date)",
     "按日切片唯一入口（主键 (code,date) 是 code 前缀，服务不了 WHERE date=?）"),
]

# ── 明确删掉、不要再建回来的索引（2026-11-01 P1-4 存储成本）────────────────────
# `idx_sid_code_date ON stock_indicators_daily(code, date)` 与表的主键自动索引
# `sqlite_autoindex_stock_indicators_daily_1`（PRIMARY KEY (code, date)）**逐页完全相同**：
#   两者都是 97,134 页 / 397,860,864 B（379.4 MiB），sqlite_stat1 也都是 "1402740 237 1"。
# 也就是说这份索引把 379 MB 存了两遍，一份还是纯装饰。
#
# 删之前做过的双向验证（别只信"看起来一样"）：
#   ① EXPLAIN QUERY PLAN：这条索引与主键索引被选中的计划形状完全一致
#      （SEARCH ... USING INDEX (code=? AND date>?) / COVERING INDEX (code=?)）；
#   ② 另建一个同构库（同样的 PRIMARY KEY (code, date) + idx_sid_date，但不建 idx_sid_code_date），
#      五条代表性查询全部落到 sqlite_autoindex（含 COVERING），说明主键索引能完全顶替。
#   ③ 真库 DROP 前后各测一遍 query plan 与耗时，确认无退化。
#
# 回滚（万一某天发现某条查询退化成全表扫，把这一行放回去即可）：
#     CREATE INDEX idx_sid_code_date ON stock_indicators_daily(code, date);
REDUNDANT_INDEXES = [
    ("viewer/app.db", "idx_sid_code_date",
     "与主键自动索引 sqlite_autoindex_stock_indicators_daily_1 列序列完全相同（纯重复，379.4 MB）"),
]


def _has_index(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is not None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv
    drop_redundant = "--drop-redundant" in argv
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

    # 冗余索引巡检：本脚本不再创建它们，但别的模块可能偷偷建回来
    # （`viewer/indicators.py::ensure_tables` 里就有一条 CREATE INDEX idx_sid_code_date）。
    # 报出来是为了让"白占 379 MB"这件事没法悄悄复发。
    for db, name, why in REDUNDANT_INDEXES:
        path = os.path.join(ROOT, db)
        if not os.path.exists(path):
            continue
        con = sqlite3.connect(path, timeout=300)
        try:
            if not _has_index(con, name):
                print("[OK]   %s :: %s 已不存在（冗余已清理）" % (db, name))
                continue
            if check_only and not drop_redundant:
                print("[WARN] %s :: 冗余索引 %s 又出现了 —— %s；"
                      "确认后跑 --drop-redundant 清理" % (db, name, why))
                rc = 1
                continue
            if drop_redundant:
                con.execute("DROP INDEX IF EXISTS %s" % name)
                con.commit()
                print("[DROP] %s :: %s （%s）" % (db, name, why))
            else:
                print("[WARN] %s :: 冗余索引 %s 存在 —— %s；--drop-redundant 可清理"
                      % (db, name, why))
        finally:
            con.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
