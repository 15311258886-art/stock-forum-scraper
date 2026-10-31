# -*- coding: utf-8 -*-
"""出网抓取结果核验器（纯本地，不联网）。

用途：进哥本机跑完 `fetch_a_shares_snapshot.py` 与 `fetch_plate_members.py` 后，
    用本脚本核验「数据是否真的补全」——而不是凭脚本打印的 "OK" 自我安慰。

检查项（对应此前缺口 C3 / C4）：
  1. stock_daily_snapshot 今日快照行数 + 覆盖率（vs a_shares_universe 权威全量）
  2. a_shares_universe 权威全 A 宇宙计数（vs 现有 stock_names / 剔除池口径）
  3. 估值字段 pe_ttm / pb / total_mv 非空比例（治 C4 估值空洞）
  4. plate_members 各板块成员计数 + 总板块数（治 C3 官方池）
  5. stock_names 是否已被快照刷新（source=eastmoney_snapshot）

运行：python tools/verify_fetch.py
退出码 0 = 全部达标；1 = 仍有缺口（给出具体缺失项）。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")


def _q(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001
        return "ERR:" + str(e)[:60]


def main():
    if not os.path.exists(DB_PATH):
        print("[verify] 找不到 %s" % DB_PATH)
        return 2
    con = sqlite3.connect(DB_PATH, timeout=30)
    problems = []
    print("=" * 60)
    print("出网抓取结果核验  @ %s" % DB_PATH)
    print("=" * 60)

    # 1. 今日快照 + 覆盖率
    td = con.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()[0]
    snap_n = con.execute("SELECT COUNT(*) FROM stock_daily_snapshot "
                         "WHERE trade_date=(SELECT MAX(trade_date) FROM stock_daily_snapshot)").fetchone()[0]
    uni_n = con.execute("SELECT COUNT(*) FROM a_shares_universe").fetchone()[0]
    print("\n[1] 日度快照 (stock_daily_snapshot)")
    print("    最新交易日 trade_date = %s" % td)
    print("    最新日快照行数 = %d" % snap_n)
    print("    权威宇宙 a_shares_universe = %d" % uni_n)
    if not td:
        problems.append("快照表为空：fetch_a_shares_snapshot.py 未跑或未落库")
        cov = 0.0
    else:
        cov = round(100.0 * snap_n / uni_n, 2) if uni_n else 0.0
        print("    覆盖率(快照/宇宙) = %.2f%%" % cov)
        if cov < 95.0:
            problems.append("快照覆盖率 %.2f%% < 95%%（可能分页断流或东财限频）" % cov)

    # 2. 估值字段非空比例（治 C4；按"非空"判定，亏损股 PE 为负属正常有效值，不计入空洞）
    print("\n[2] 估值基本面（治 C4 估值空洞）")
    for col in ("pe_ttm", "pb", "total_mv"):
        n = con.execute("SELECT COUNT(*) FROM stock_daily_snapshot WHERE trade_date=(SELECT MAX(trade_date) FROM stock_daily_snapshot) AND %s IS NOT NULL" % col).fetchone()[0]
        ratio = round(100.0 * n / snap_n, 2) if snap_n else 0.0
        print("    %-10s 非空 = %d (%.2f%%)" % (col, n, ratio))
        if snap_n and ratio < 80.0:
            problems.append("估值字段 %s 非空率 %.2f%% < 80%%（C4 仍空洞）" % (col, ratio))

    # 3. 官方板块成分（治 C3）
    print("\n[3] 官方板块成分 (plate_members, 治 C3)")
    pm_rows = _q(con, "SELECT board, COUNT(*) FROM plate_members GROUP BY board ORDER BY board")
    if isinstance(pm_rows, str):
        print("    表缺失或异常:", pm_rows)
        problems.append("plate_members 表缺失（fetch_plate_members.py 未跑）")
        pm_total = 0
    else:
        pm_total = sum(r[1] for r in pm_rows)
        print("    板块数 = %d，成员行数合计 = %d" % (len(pm_rows), pm_total))
        for b, c in pm_rows:
            flag = "" if c >= 10 else "  ⚠偏少"
            print("      %-8s %d%s" % (b, c, flag))
        if pm_total == 0:
            problems.append("plate_members 为 0 行（fetch_plate_members.py 未跑或全未匹配）")

    # 4. stock_names 是否被快照刷新
    print("\n[4] 名称字典刷新 (stock_names)")
    nm_total = con.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    nm_src = con.execute("SELECT COUNT(*) FROM stock_names WHERE source IN ('eastmoney_snapshot','tencent_snapshot')").fetchone()[0]
    print("    总条目 = %d，已由快照刷新(source=eastmoney/tencent_snapshot) = %d" % (nm_total, nm_src))
    if nm_src == 0:
        problems.append("stock_names 未由快照刷新（名称可能陈旧）")

    # 5. 漏斗官方模式可用性推断
    print("\n[5] 漏斗 official 模式可用性")
    if pm_total == 0:
        print("    ⚠ plate_members 为空 → 漏斗 mode=official 仍会回退博主挖名毒池（C3 未治）")
        problems.append("漏斗 official 模式仍回退（plate_members 空）")
    else:
        print("    ✓ plate_members 有数据 → 漏斗 official 模式可用官方池，不再回退毒池")

    con.close()
    print("\n" + "=" * 60)
    if problems:
        print("核验结果：⚠ 仍有 %d 项缺口" % len(problems))
        for p in problems:
            print("  - %s" % p)
        print("=" * 60)
        return 1
    print("核验结果：✓ 全部达标，C3/C4 数据已补全")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
