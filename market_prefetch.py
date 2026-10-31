# -*- coding: utf-8 -*-
"""行情预存调度器（P2 守护进程 / 单次跑）。

用法：
  # 单次跑一轮（预存热池行情+K线后退出，便于验证 / 手动触发）
  python market_prefetch.py --once

  # 常驻调度：交易时段每 INTERVAL 秒一轮（默认 300s；非交易时段自动放宽到 900s）
  python market_prefetch.py

  # 只刷行情（不拉 K 线，更快）
  python market_prefetch.py --once --quotes-only

  # 自定义库路径 / 热池上限 / K线天数
  python market_prefetch.py --db viewer/app.db --cap 300 --kline-days 120

设计：复用 market_cache（零第三方依赖、复用 emapi 竞速基底）。表建在 app.db 内，
与查看器共用同一库（WAL 模式，写入不阻塞网页读）。
"""
from __future__ import annotations
import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market_cache as mc

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("market_prefetch")

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "viewer", "app.db")
INTERVAL_TRADING = 300     # 交易时段：5 分钟一轮
INTERVAL_OFFHOURS = 900    # 非交易时段：15 分钟一轮


def _is_trading_now() -> bool:
    """粗判当前是否盘中（交易日 + 9:30–11:30 / 13:00–15:00）。

    P3-24 修复：不再只靠 now.weekday() 判断「是不是工作日」——A 股还有法定节假日调休，
    单纯看星期会把调休补班的交易日误判为休市、把假日误判为开市，导致预存节奏错乱。
    改走本项目唯一权威交易日历（auction.is_trading_day / 2026 确定性日历兜底），
    **仅用于决定刷新节奏，不用于强约束**。日历取不到（None=未知）时回退到星期判断，
    绝不因网络抖动把调度卡死。
    P2-06：时间用 CST 时区感知（now_cst），避免跨时区错乱。
    """
    # P2-06: CST 时区感知当前时间
    try:
        from timeutil import now_cst
        now = now_cst()
    except Exception:  # noqa: BLE001
        now = datetime.now()
    # 交易日历（唯一权威入口）；None=未知 → 回退星期判断，False=确定休市 → 直接 False
    try:
        from auction import is_trading_day
        is_td = is_trading_day(now.strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001 —— 日历取不到不阻断调度
        is_td = None
    if is_td is False:
        return False
    if is_td is None and now.weekday() >= 5:   # 未知时回退：周末当休市
        return False
    t = now.hour * 60 + now.minute
    if (9 * 60 + 30) <= t <= (11 * 60 + 30):
        return True
    if (13 * 60) <= t <= (15 * 60):
        return True
    return False


def _run_once(db: str, cap: int, kline_days: int, quotes_only: bool) -> dict:
    conn = sqlite3.connect(db, timeout=30)
    try:
        mc.ensure_schema(conn)
        q = mc.prefetch_quotes(conn, cap=cap)
        k = mc.prefetch_klines(conn, cap=cap, days=kline_days) if not quotes_only else None
        logger.info("预存完成 quotes=%s klines=%s", q, k)
        return {"quotes": q, "klines": k}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="行情预存调度器（P2）")
    ap.add_argument("--db", default=DEFAULT_DB, help="app.db 路径")
    ap.add_argument("--once", action="store_true", help="跑一轮后退出")
    ap.add_argument("--quotes-only", action="store_true", help="只刷行情，不拉 K 线")
    ap.add_argument("--cap", type=int, default=250, help="热池上限（个股数）")
    ap.add_argument("--kline-days", type=int, default=120, help="K 线回溯天数")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        logger.error("库不存在: %s", args.db)
        sys.exit(2)

    if args.once:
        res = _run_once(args.db, args.cap, args.kline_days, args.quotes_only)
        print("PREFETCH_RESULT:", res)
        return

    logger.info("进入常驻调度（库=%s 热池=%d）Ctrl+C 退出", args.db, args.cap)
    try:
        while True:
            try:
                _run_once(args.db, args.cap, args.kline_days, args.quotes_only)
            except Exception as e:  # noqa: BLE001
                logger.exception("本轮预存异常（跳过继续）: %s", e)
            interval = INTERVAL_TRADING if _is_trading_now() else INTERVAL_OFFHOURS
            logger.info("下次预存 %.0f 秒后", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("收到中断，退出")


if __name__ == "__main__":
    main()
