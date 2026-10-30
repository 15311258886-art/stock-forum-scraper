# -*- coding: utf-8 -*-
"""公告历史月窗回补 CLI（2026-10-31，T03）。

用途：
  从 2016-01 起按自然月串行调用 viewer.ann_feed.refresh_range，写入 stock_ann。
  每月状态记录在 ann_backfill_state，只有 API 实际读取行数 seen>=total_hits 才标 done；
  失败/截断/进程中断的月份保留 error/running，下次运行自动重试。stock_ann 的
  (code,url) 幂等由 ann_feed 负责，因此同月重跑 added 应为 0。

示例：
  # 先抽两个月
  python tools/backfill_ann.py --from 2016-01 --to 2016-01
  python tools/backfill_ann.py --from 2026-09 --to 2026-09

  # 全量断点续跑（适合放后台；日志同时落盘）
  python tools/backfill_ann.py --resume --log-file viewer/data/backfill_ann.log

  # 每次只跑 3 个未完成月份
  python tools/backfill_ann.py --resume --months 3

纪律：
  * 不在 Web 请求线程运行，也不并发启动多个回补进程；
  * 每页由 refresh_range 提交，进程被杀后重跑当月即可；
  * 历史行 is_new=0，不污染「未读公告」；
  * 只写 stock_ann 与 ann_backfill_state，不改分类口径和 stock_ann schema。
"""
from __future__ import annotations

import argparse
import calendar
import os
import sqlite3
import sys
import time
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import ann_feed as af  # noqa: E402

DEFAULT_BEGIN = "2016-01-01"
STATE_DDL = """
CREATE TABLE IF NOT EXISTS ann_backfill_state (
    window_start TEXT PRIMARY KEY,
    window_end   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    pages        INTEGER DEFAULT 0,
    seen         INTEGER DEFAULT 0,
    kept         INTEGER DEFAULT 0,
    added        INTEGER DEFAULT 0,
    total_hits   INTEGER DEFAULT 0,
    attempts     INTEGER DEFAULT 0,
    error        TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ann_backfill_status ON ann_backfill_state(status);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _month_start(value: str) -> date:
    """支持 YYYY-MM 或 YYYY-MM-DD，统一到当月 1 日。"""
    s = str(value or "").strip()
    if len(s) == 7:
        s += "-01"
    try:
        d = date.fromisoformat(s[:10])
    except ValueError as e:
        raise ValueError("日期格式应为 YYYY-MM 或 YYYY-MM-DD：%s" % value) from e
    return date(d.year, d.month, 1)


def ensure_state(con: sqlite3.Connection) -> None:
    con.executescript(STATE_DDL)
    con.commit()


def month_windows(begin: str, end: str, months: int = 0) -> list[tuple[str, str]]:
    """生成闭区间的自然月窗口；末月按 end 截断，不抓未来日期。"""
    start = _month_start(begin)
    limit = _month_start(end)
    if limit < start:
        return []
    out = []
    cur = start
    while cur <= limit:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = date(cur.year, cur.month, last_day)
        if (cur.year, cur.month) == (limit.year, limit.month):
            d = date.fromisoformat(str(end)[:10]) if len(str(end)) > 7 else month_end
            month_end = min(month_end, d)
        out.append((cur.isoformat(), month_end.isoformat()))
        if months and len(out) >= months:
            break
        cur = date(cur.year + (1 if cur.month == 12 else 0),
                   1 if cur.month == 12 else cur.month + 1, 1)
    return out


def month_cat_distribution(con: sqlite3.Connection, begin: str, end: str) -> dict:
    rows = con.execute(
        "SELECT cat, COUNT(*) FROM stock_ann WHERE notice_date>=? AND notice_date<=? "
        "GROUP BY cat ORDER BY COUNT(*) DESC", (begin, end)).fetchall()
    return {str(r[0] or "未分类"): int(r[1]) for r in rows}


def _state_row(con: sqlite3.Connection, begin: str):
    return con.execute("SELECT * FROM ann_backfill_state WHERE window_start=?",
                       (begin,)).fetchone()


def _mark(con: sqlite3.Connection, begin: str, end: str, status: str,
          result: dict | None, err: str | None, bump_attempt: bool = True) -> None:
    old = _state_row(con, begin)
    attempts = old["attempts"] if old and old["attempts"] is not None else 0
    if bump_attempt:
        attempts += 1
    r = result or {}
    con.execute(
        "INSERT INTO ann_backfill_state(window_start,window_end,status,pages,seen,kept,"
        "added,total_hits,attempts,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(window_start) DO UPDATE SET window_end=excluded.window_end,"
        "status=excluded.status,pages=excluded.pages,seen=excluded.seen,"
        "kept=excluded.kept,added=excluded.added,total_hits=excluded.total_hits,"
        "attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
        (begin, end, status, int(r.get("pages") or 0), int(r.get("seen") or 0),
         int(r.get("kept") or 0), int(r.get("added") or 0),
         int(r.get("total_hits") or 0), attempts, err, _now()))
    con.commit()


def run_month(con: sqlite3.Connection, begin: str, end: str, max_pages: int,
              log) -> dict:
    """执行一个月窗；异常转为 error，不把失败月份标 done。"""
    _mark(con, begin, end, "running", None, None, bump_attempt=False)
    try:
        r = af.refresh_range(con, begin, end, max_pages=max_pages,
                             historical=True, log=log)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "complete": False, "begin": begin, "end": end,
             "total_hits": 0, "pages": 0, "seen": 0, "kept": 0, "added": 0,
             "duplicates": 0, "err": "%s: %s" % (type(e).__name__, e), "secs": 0.0,
             "cat_added": {}}
        log("  ERROR %s" % r["err"])
    complete = bool(r.get("complete") and r.get("seen", 0) >= r.get("total_hits", 0))
    status = "done" if complete else "error"
    err = None if complete else (r.get("err") or "分页未完整")
    _mark(con, begin, end, status, r, err)
    return r


def _make_logger(log_file: str):
    fh = open(log_file, "a", encoding="utf-8", buffering=1) if log_file else None

    def log(msg: str) -> None:
        line = "[%s] %s" % (_now(), msg)
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")

    return log, fh


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="东财全市场公告月度历史回补")
    p.add_argument("--from", dest="begin", default=DEFAULT_BEGIN,
                   help="起始月/日，默认 2016-01-01")
    p.add_argument("--begin", dest="begin", help="--from 的别名")
    p.add_argument("--to", dest="end", default=date.today().isoformat(),
                   help="结束月/日，默认今天")
    p.add_argument("--end", dest="end", help="--to 的别名")
    p.add_argument("--months", dest="months", type=int, default=0,
                   help="本次最多处理 N 个自然月；0=全部")
    p.add_argument("--months-per-run", dest="months", type=int,
                   help="--months 的别名")
    p.add_argument("--resume", action="store_true",
                   help="跳过 status=done 的月份（默认同样跳过）")
    p.add_argument("--force", action="store_true", help="连 done 月份也重跑")
    p.add_argument("--max-pages", type=int, default=1000,
                   help="单月分页上限，默认 1000（10万条）")
    p.add_argument("--max-consecutive-failures", type=int, default=3,
                   help="连续失败 N 月后停止，默认 3")
    p.add_argument("--dry-run", action="store_true", help="只列月份，不抓取")
    p.add_argument("--log-file", default="", help="同时追加进度日志到文件")
    p.add_argument("--db", default="", help="数据库路径，默认 viewer/app.db")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        windows = month_windows(args.begin, args.end, max(0, args.months or 0))
    except ValueError as e:
        print("参数错误：%s" % e, file=sys.stderr)
        return 2
    if not windows:
        print("没有可处理月份：%s ~ %s" % (args.begin, args.end), file=sys.stderr)
        return 2

    db_path = args.db or af.DB_PATH
    log, fh = _make_logger(args.log_file)
    con = sqlite3.connect(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    try:
        ensure_state(con)
        done = skipped = failed = attempted = 0
        consecutive_failures = 0
        t0 = time.time()
        log("公告回补开始：%s ~ %s，共 %d 个自然月，db=%s"
            % (windows[0][0], windows[-1][1], len(windows), db_path))
        for begin, end in windows:
            old = _state_row(con, begin)
            if old and old["status"] == "done" and not args.force:
                skipped += 1
                log("SKIP %s~%s（已完成：seen=%s/total=%s added=%s）"
                    % (begin, end, old["seen"], old["total_hits"], old["added"]))
                continue
            if args.dry_run:
                status = old["status"] if old else "pending"
                log("DRY  %s~%s（%s）" % (begin, end, status))
                continue
            attempted += 1
            log("RUN  %s~%s" % (begin, end))
            r = run_month(con, begin, end, max(1, args.max_pages), log)
            cats = month_cat_distribution(con, begin, end)
            cat_added = r.get("cat_added") or {}
            if r.get("complete") and r.get("seen", 0) >= r.get("total_hits", 0):
                done += 1
                consecutive_failures = 0
                log("DONE %s~%s  pages=%d seen=%d total_hits=%d kept=%d added=%d "
                    "duplicates=%d %.1fs"
                    % (begin, end, r.get("pages", 0), r.get("seen", 0),
                       r.get("total_hits", 0), r.get("kept", 0), r.get("added", 0),
                       r.get("duplicates", 0), r.get("secs", 0.0)))
                log("     added_cats=%s" % (cat_added or {}))
                log("     month_cats=%s" % cats)
            else:
                failed += 1
                consecutive_failures += 1
                log("FAIL %s~%s  seen=%s total_hits=%s err=%s"
                    % (begin, end, r.get("seen", 0), r.get("total_hits", 0),
                       r.get("err") or "分页未完整"))
                if consecutive_failures >= max(1, args.max_consecutive_failures):
                    log("连续失败 %d 个月，停止本次运行；未完成月份保留 checkpoint"
                        % consecutive_failures)
                    break
        total_secs = time.time() - t0
        log("公告回补结束：done=%d failed=%d skipped=%d attempted=%d %.1fs"
            % (done, failed, skipped, attempted, total_secs))
        if args.dry_run:
            return 0
        return 0 if failed == 0 else 1
    finally:
        con.close()
        if fh:
            fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
