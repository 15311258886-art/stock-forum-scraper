# -*- coding: utf-8 -*-
"""stock_ann「同标题多文档」审计（2026-10-31 新建，只读）。

## 起因

`tests/test_ann_backfill.py` 有一条生产库存基线（2026-09 = 11,061）。2026-10-31 跑全量测试
时变成 11,065，红了。第一反应是"同一份公告存了两行"，于是写了脚本准备去重 ——
**但查完发现不能删，这个脚本就是那次查证的记录。**

## 为什么不能删

按 (code, notice_date, title) 分组，全表 629 组同名、678 条"多余"行（0.0585%）。
但把 url 一起看：**629 组里，每一组的 url 都不相同**（`distinct url == 组内行数`）。

    ('600276', '2025-12-20', '恒瑞医药:恒瑞医药关于获得药物临床试验批准通知书的公告', 4 行，4 个 url)
    ('300606', '2026-03-20', '深交所:金太阳_监管函', 4 行，4 个 url)

这更像"**同一天发了 4 份标题相同的独立文件**"（不同药品的临床批件、不同事由的监管函），
而不是"同一份文件被存了 4 次"。按标题去重会把真公告删掉，而且删完没有任何报错 ——
**静默丢数据比留重复严重得多。**

真正被证明有问题的是 `viewer/ann_feed.py::_ann_key` 的粒度：
它优先用 `(code, url)`，无 url 才退到 `(code, date, title)`。这个选择在这批数据上是对的
（否则上面那 4 份会被互相判重）。所以**代码不该改，数据不该删**。

## 那库存基线为什么会漂

因为它是"当月公告条数"，而公告是持续增补的：同一天可能晚些时候再发一份，历史月也可能被
补抓进来。基线是**某一次抽测的库存**，不是不变量 —— 把它写成断言，测试就会随数据自然增长而红。
所以测试里已改成"记录基线 + 允许增长，但增长必须能解释"的口径。

## 用法

    python tools/audit_ann_dupes.py            # 只报告
    python tools/audit_ann_dupes.py --sample 20

**没有 --apply。** 这个脚本的结论就是"不要删"，它不提供删的入口。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "app.db")


def scan(con: sqlite3.Connection) -> dict:
    total = con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0]
    groups, extra = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(c - 1), 0) FROM ("
        "  SELECT code, notice_date, title, COUNT(*) c FROM stock_ann"
        "  GROUP BY code, notice_date, title HAVING c > 1)").fetchone()
    # 关键判别：组内 url 是否全不同。全不同 ⇒ 大概率是不同文档，不是重复行。
    all_diff = con.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT code, notice_date, title, COUNT(*) c, COUNT(DISTINCT url) du"
        "  FROM stock_ann GROUP BY code, notice_date, title"
        "  HAVING c > 1 AND du = c)").fetchone()[0]
    # 真正的重复只可能是：同名且 url 相同
    exact = con.execute(
        "SELECT COALESCE(SUM(c - 1), 0) FROM ("
        "  SELECT code, notice_date, title, url, COUNT(*) c FROM stock_ann"
        "  GROUP BY code, notice_date, title, url HAVING c > 1)").fetchone()[0]
    return {"total": total, "groups": groups, "extra": extra,
            "groups_all_url_distinct": all_diff, "exact_dup_rows": exact}


def samples(con: sqlite3.Connection, n: int) -> list:
    return con.execute(
        "SELECT code, notice_date, title, COUNT(*) c, COUNT(DISTINCT url) du,"
        "       GROUP_CONCAT(id) ids"
        "  FROM stock_ann GROUP BY code, notice_date, title"
        " HAVING c > 1 ORDER BY c DESC, notice_date DESC LIMIT ?", (n,)).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="stock_ann 同标题多文档审计（只读）")
    ap.add_argument("--sample", type=int, default=8, help="打印多少条样本")
    a = ap.parse_args([] if argv is None else list(argv))

    if not os.path.exists(DB):
        print("[FAIL] 找不到库：%s" % DB)
        return 2
    con = sqlite3.connect(DB, timeout=30)
    try:
        st = scan(con)
        print("=" * 72)
        print("stock_ann · 同 (code, notice_date, title) 多行审计")
        print("=" * 72)
        print("  总行数              : %d" % st["total"])
        print("  同名组              : %d" % st["groups"])
        print("  同名多余行          : %d  (%.4f%%)"
              % (st["extra"], 100.0 * st["extra"] / max(1, st["total"])))
        print("  其中 url 全不相同的 : %d 组" % st["groups_all_url_distinct"])
        print("  真正的重复行(同 url): %d" % st["exact_dup_rows"])
        print()
        if st["exact_dup_rows"] == 0:
            print("  结论：同名组全部是「同标题的不同文件」（url 互不相同），**不是重复行**。")
            print("        按标题去重会删掉真公告，且删完不报错。→ 数据不动，代码不动。")
        else:
            print("  结论：存在 %d 行同 url 重复，这才是可安全删除的部分"
                  "（本脚本仍不删，交人工确认）。" % st["exact_dup_rows"])
        if st["groups"]:
            print("\n样本（c=行数 / du=不同 url 数）:")
            for code, nd, title, c, du, ids in samples(con, a.sample):
                flag = "  ← 全不同 url，判为不同文档" if du == c else "  ← 含同 url，可能有真重复"
                print("  %s %s x%d (url %d)%s\n     %s\n     ids=%s"
                      % (code, nd, c, du, flag, (title or "")[:70], ids))
        print("\n[只读] 未做任何修改；本工具不提供删除入口（结论就是不要删）。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
