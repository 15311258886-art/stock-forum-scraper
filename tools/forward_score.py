# -*- coding: utf-8 -*-
"""前向记账 · 第二棒：回填进场价 + 按 6 个持有期打分 —— 2026-11-05

## 两件事，顺序不能换

### ① 回填（`entry_px IS NULL AND skip_reason IS NULL`）

信号日是 `snap_date`，**进场时点是次日收盘**（决策台收盘后才出票，当天买不到）。
所以每到期一个新交易日，就把账本里「还没进场」的记录补上 `entry_date / entry_px`。

真实约束有两条，都如实记账、不美化：

  · **次日收盘涨停 → 买不进**：记 `skip_reason='limit_up'`，不参与打分。
    涨停门槛按代码段（见 `forward_lib.limit_thr`），判定式 `ret1 >= 阈值 - 0.2`。
  · **次日没有 bar（停牌/退市）→ 没有可成交价格**：记 `skip_reason='no_bar'`。
    这一条不在原设计里，是实测补的：不记的话这些行会永远挂在「待回填」，
    每次统计都混一个假「待定」，看不出到底是没到时还是真买不到。

### ② 打分（`entry_px` 已就绪的记录 × 6 个持有期）

  · 到期价 = 该票**自己**第 H 根 bar 的收盘（停牌不该被算成「到期」）
  · 收益 `ret_pct` = (exit/entry - 1) × 100 − 0.25（双边成本，从收益里扣）
  · 基准 `bench_pct` = 同区间的全市场等权收益（`forward_lib.EwIndex`）
  · 超额 `excess_pct` = `ret_pct − bench_pct`
  · 还没走完 H 根 → `matured=0`，`exit_*` 留 NULL（**不拿最后一天的价格冒充到期**）

## 幂等

`forward_result` 主键 `(book_id, hold)`，反复跑是 `INSERT OR REPLACE`，
行数只随「新进场 / 新到期」变化，重复跑不产生重复行。

## 用法

  python tools/forward_score.py
  python tools/forward_score.py --holds 1,3,5     # 只算这几档（调试用）
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:                                                          # 直接跑：tools/ 在 sys.path[0]
    import forward_lib as FL
except ImportError:                                           # `python -m tools.forward_score`
    from tools import forward_lib as FL                       # noqa: E402


def backfill(con_app: sqlite3.Connection, con_kline: sqlite3.Connection) -> dict:
    """把「信号日次日」的进场价补进账本。返回统计字典。

    「次日」取全局交易日轴的下一根（`bisect` 在一次性读出的日历上做，不用逐条发 SQL ——
    待回填上千条时逐条查会把 30 秒花在同一个答案上）。
    """
    rows = con_app.execute(
        "SELECT id, snap_date, src, code FROM forward_book "
        "WHERE entry_px IS NULL AND skip_reason IS NULL ORDER BY snap_date, id").fetchall()
    stat = {"pending": 0, "filled": 0, "limit_up": 0, "no_bar": 0, "wait_next": 0,
            "wait_data": 0}
    if not rows:
        return stat
    stat["pending"] = len(rows)

    cal = FL.trading_dates(con_kline)
    for r in rows:
        book_id = int(r["id"])
        snap = int(r["snap_date"])
        code = str(r["code"])
        i = bisect.bisect_right(cal, snap)
        if i >= len(cal):
            stat["wait_next"] += 1          # 次日还没开盘（当天的记录）
            continue
        nd = int(cal[i])
        bars = FL.last_two_bars(con_kline, code, nd)
        if not bars or int(bars[-1][0]) != int(nd):
            # 次日在全市场是交易日，但这只票没有 bar。
            # 若次日就是**最后一根已知交易日**，更可能是当日 K 线还没补完（而不是停牌），
            # 这时不写 no_bar（写下去就永远改不回来了），留到下一轮再判。
            if nd >= int(cal[-1]):
                stat["wait_data"] += 1
                continue
            con_app.execute(
                "UPDATE forward_book SET skip_reason=? WHERE id=?", ("no_bar", book_id))
            stat["no_bar"] += 1
            continue
        entry_px = float(bars[-1][1] or 0.0)
        if entry_px <= 0:
            con_app.execute(
                "UPDATE forward_book SET skip_reason=? WHERE id=?", ("no_bar", book_id))
            stat["no_bar"] += 1
            continue
        prev_px = float(bars[-2][1]) if len(bars) >= 2 else 0.0
        if FL.is_limit_up(code, prev_px, entry_px):
            # 涨停收盘买不进：记原因，entry_px 保持 NULL（不进打分）
            con_app.execute(
                "UPDATE forward_book SET entry_date=?, skip_reason=? WHERE id=?",
                (int(nd), "limit_up", book_id))
            stat["limit_up"] += 1
            continue
        con_app.execute(
            "UPDATE forward_book SET entry_date=?, entry_px=? WHERE id=?",
            (int(nd), entry_px, book_id))
        stat["filled"] += 1
    con_app.commit()
    return stat


def _code_bars_from_entry(con_kline: sqlite3.Connection, code: str,
                          entry_date: int, max_hold: int) -> list[tuple[int, float]]:
    """取该票从进场日起的 bar（升序，最多 max_hold+1 根）。"""
    rows = con_kline.execute(
        "SELECT d,c FROM kline WHERE code=? AND d>=? ORDER BY d LIMIT ?",
        (str(code), int(entry_date), int(max_hold) + 1)).fetchall()
    return [(int(r[0]), float(r[1] or 0.0)) for r in rows]


def score(con_app: sqlite3.Connection, con_kline: sqlite3.Connection,
          ew: FL.EwIndex, holds: tuple[int, ...], full: bool = False) -> dict:
    """给每条已回填记录写 N 行（每档一行，未到期也写 matured=0）。

    **只重算还能变的记录**：6 档全部到期之后，结果不会再变（数据已定、成本固定、
    基准固定），没必要每天重算。账本按现在的记账速度一年能到几十万条，
    全量重算会从「几秒」变成「几十分钟」。要修数据用 `--full` 强制全量。
    """
    n_holds = len(holds)
    if full:
        books = con_app.execute(
            "SELECT id, snap_date, code, entry_date, entry_px FROM forward_book "
            "WHERE entry_px IS NOT NULL AND skip_reason IS NULL ORDER BY id").fetchall()
    else:
        books = con_app.execute(
            "SELECT b.id, b.snap_date, b.code, b.entry_date, b.entry_px "
            "FROM forward_book b "
            "WHERE b.entry_px IS NOT NULL AND b.skip_reason IS NULL "
            "  AND (SELECT COUNT(*) FROM forward_result r "
            "        WHERE r.book_id=b.id AND r.matured=1) < ? "
            "ORDER BY b.id", (int(n_holds),)).fetchall()
    frozen = con_app.execute(
        "SELECT COUNT(*) FROM forward_book WHERE entry_px IS NOT NULL AND skip_reason IS NULL"
    ).fetchone()[0] - len(books)
    stat = {"books": len(books), "frozen": int(max(0, frozen)), "rows": 0,
            "matured": 0, "immature": 0, "no_bench": 0}
    if not books:
        return stat

    max_hold = max(holds)
    for b in books:
        entry_date = int(b["entry_date"])
        entry_px = float(b["entry_px"])
        bars = _code_bars_from_entry(con_kline, str(b["code"]), entry_date, max_hold)
        if not bars or bars[0][0] != entry_date:
            # 进场日那根不见了（数据被回填改写）→ 该记录无法打分，留给下一轮
            continue
        for H in holds:
            exit_date = None
            exit_px = None
            ret_pct = None
            bench_pct = None
            excess_pct = None
            matured = 0
            if len(bars) > H:
                exit_date, exit_px = bars[H]
                if exit_px > 0 and entry_px > 0:
                    matured = 1
                    ret_pct = (exit_px / entry_px - 1.0) * 100.0 - FL.COST_PCT
                    bench_pct = ew.ret_pct(entry_date, exit_date)
                    if bench_pct is not None:
                        excess_pct = ret_pct - bench_pct
                    else:
                        stat["no_bench"] += 1
                else:
                    exit_date, exit_px = None, None
            con_app.execute(
                "INSERT OR REPLACE INTO forward_result "
                "(book_id, hold, exit_date, exit_px, ret_pct, bench_pct, excess_pct, matured) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(b["id"]), int(H), exit_date, exit_px,
                 None if ret_pct is None else round(ret_pct, 6),
                 None if bench_pct is None else round(bench_pct, 6),
                 None if excess_pct is None else round(excess_pct, 6),
                 int(matured)))
            stat["rows"] += 1
            if matured:
                stat["matured"] += 1
            else:
                stat["immature"] += 1
    con_app.commit()
    return stat


def main() -> int:
    FL.setup_stdout()
    ap = argparse.ArgumentParser(description="前向记账 · 回填与打分")
    ap.add_argument("--holds", default=",".join(str(h) for h in FL.HOLDS),
                    help="持有期（交易日，逗号分隔），默认 %s"
                         % ",".join(str(h) for h in FL.HOLDS))
    ap.add_argument("--full", action="store_true",
                    help="全量重算（默认跳过「六档都已到期、结果不会再变」的记录）")
    args = ap.parse_args()
    holds = tuple(sorted({int(x) for x in args.holds.split(",") if x.strip()}))
    print("[forward_score] 开始 %s ｜ 持有期 %s" % (FL.now_str(), list(holds)))

    con_app = FL.connect_app()
    try:
        FL.ensure_schema(con_app)

        try:
            con_kline = FL.connect_kline()
        except sqlite3.Error as e:
            print("!! 打不开 K 线库 data/kline_daily.db：%s" % e)
            print("   多半是日更正在写（WAL 独占）。本次跳过，稍后重跑。")
            return 0

        try:
            total = con_app.execute("SELECT COUNT(*) FROM forward_book").fetchone()[0]
            if not total:
                print("账本还是空的 —— 先跑 tools/forward_snapshot.py 记账。本次无事可做。")
                return 0
            print("账本共 %d 条" % int(total))

            stat = backfill(con_app, con_kline)
            print("\n== 回填进场价 ==")
            print("  待回填 %d 条 → 本次回填 %d；涨停买不进(skip=limit_up) %d；"
                  "次日无 bar(skip=no_bar) %d；次日还没到 %d；次日数据未补齐待定 %d"
                  % (stat["pending"], stat["filled"], stat["limit_up"], stat["no_bar"],
                     stat["wait_next"], stat.get("wait_data", 0)))

            # 基准补齐到 K 线最新一天（缺的日期按同一公式补算并落盘）
            last_d = con_kline.execute("SELECT MAX(d) FROM kline").fetchone()[0]
            ew = FL.EwIndex().load()
            added = ew.ensure(int(last_d or 0), con_kline)
            if added:
                ew.save()
                print("  等权基准补算 %d 天（%s → %s），已写回 %s"
                      % (added, ew.extended[0], ew.extended[-1], FL.EW_JSON.name))
            print("  等权基准覆盖 %d ~ %d（%d 天）"
                  % (ew.dates[0], ew.dates[-1], len(ew.dates)))

            st = score(con_app, con_kline, ew, holds, args.full)
            print("\n== 打分 ==")
            print("  本次打分 %d 条（已定型跳过 %d 条）→ 写 %d 行 forward_result"
                  "（到期 %d / 未到期 %d）"
                  % (st["books"], st["frozen"], st["rows"], st["matured"], st["immature"]))
            if st["no_bench"]:
                print("  ⚠ 有 %d 行拿不到同期基准（等权指数缺日期），这些行 excess_pct 为 NULL"
                      % st["no_bench"])

            # 分持有期存量
            print("  分持有期（到期/总计）：")
            for H in holds:
                row = con_app.execute(
                    "SELECT SUM(matured) m, COUNT(*) n FROM forward_result WHERE hold=?",
                    (int(H),)).fetchone()
                print("    T+%-3d %4d / %4d" % (H, int(row["m"] or 0), int(row["n"] or 0)))

            # 幂等自检：主键约束下总行数必须等于「已进场条数 × 持有期档数」
            dup = con_app.execute(
                "SELECT COUNT(*) FROM (SELECT book_id, hold FROM forward_result "
                "GROUP BY book_id, hold HAVING COUNT(*) > 1)").fetchone()[0]
            print("  重复行检查：%d（必须为 0）" % int(dup))
        finally:
            con_kline.close()
    except sqlite3.OperationalError as e:
        print("!! 数据库暂时不可用：%s" % e)
        print("   多半是日更正在写库。本次跳过，稍后重跑。")
        return 0
    finally:
        con_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
