# -*- coding: utf-8 -*-
"""tools/backfill_lhb.py —— 龙虎榜个股榜单历史回补（2016 起）。

【为什么需要】
`lhb_stocks` 是全系统实证强度最高的信号来源（近10日上榜且换手≥15% → T+5 超额 -6.12%、
t=-9.05），但库内只有 **25 个交易日**（2026-09-16 起）。t 值再大也只证明「这一个月有效」，
2026-10-31 的长样本复核已经把「用 14 个月窗口给规则判死刑」列为同类错误，这里必须补。

【接口事实（2026-10-31 实测）】
东财 datacenter-web 的 RPT_DAILYBILLBOARD_DETAILSNEW **支持历史等值过滤**：
  2016-01-05 → 108 行；2018-06-15 → 76 行；2020-03-02 → 70 行；2023-05-10 → 73 行
故按交易日枚举即可回补，无需任何新数据源。filter 仍不支持范围写法，只能逐日。

【范围】
只回补 **个股榜单 lhb_stocks**（回测唯一需要的表）。
席位/营业部/机构三张表体量大、逐日 3 次请求，留作最近窗口增量，不回补。

【幂等 / 可中断】
- 交易日清单取自本地 `data/kline_daily.db`（2627 个交易日，2016-01-01 起），
  与库内已有 trade_date 做差集 → 只跑缺的，重复运行安全。
- 每完成一天即 commit，中断后重跑自动续上。
- 单日连续失败 3 次即停（防限流打爆数据源）。

用法：
  .venv\\Scripts\\python.exe tools/backfill_lhb.py                 # 全量回补（缺哪天补哪天）
  .venv\\Scripts\\python.exe tools/backfill_lhb.py --from 2019-01-01
  .venv\\Scripts\\python.exe tools/backfill_lhb.py --limit 200      # 只补最近 200 个缺失日
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import lhb_fetch as L  # noqa: E402  复用已验证的字段映射/建表/写入

DB = os.path.join(BASE, "viewer", "app.db")
KLINE_DB = os.path.join(BASE, "data", "kline_daily.db")
PAGE_SIZE = 500
MAX_FAIL_STREAK = 3


def trading_days(frm: str, to: str) -> list[str]:
    """从本地日线全库取交易日（同一天只要有任一股票有 K 线即为交易日）。"""
    con = sqlite3.connect(f"file:{KLINE_DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute(
            "SELECT DISTINCT d FROM kline WHERE d>=? AND d<=? ORDER BY d",
            (frm.replace("-", ""), to.replace("-", ""))).fetchall()
    finally:
        con.close()
    out = []
    for (d,) in rows:
        d = str(d)
        out.append(f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d)
    return out


def fetch_stocks_paged(day: str) -> list[dict]:
    """个股榜单（带分页）—— 2016 年前后单日可达 300+ 条，pageSize=500 基本够，
    但仍按 total 判断是否需要翻页，避免历史高峰日被截断。"""
    out: list[dict] = []
    page = 1
    while True:
        url = L._api("RPT_DAILYBILLBOARD_DETAILSNEW", day, size=PAGE_SIZE)
        if page > 1:
            url = url.replace("pageNumber=1", f"pageNumber={page}")
        j = L._get_json(url)
        res = j.get("result") or {}
        rows = res.get("data") or []
        if not rows:
            break
        for r in rows:
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            out.append({
                "trade_date": (r.get("TRADE_DATE") or day)[:10],
                "security_code": code,
                "security_name": r.get("SECURITY_NAME_ABBR") or "",
                "market": r.get("MARKET") or "",
                "close_price": r.get("CLOSE_PRICE"),
                "change_rate": r.get("CHANGE_RATE"),
                "turnoverrate": r.get("TURNOVERRATE"),
                "buy_amt": r.get("BILLBOARD_BUY_AMT"),
                "sell_amt": r.get("BILLBOARD_SELL_AMT"),
                "net_amt": r.get("BILLBOARD_NET_AMT"),
                "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
                "deal_net_ratio": r.get("DEAL_NET_RATIO"),
                "free_market_cap": r.get("FREE_MARKET_CAP"),
                "reason": r.get("EXPLANATION") or "",
                "explain": r.get("EXPLAIN") or "",
                "d1_chg": r.get("D1_CLOSE_ADJCHRATE"),
                "d2_chg": r.get("D2_CLOSE_ADJCHRATE"),
                "d5_chg": r.get("D5_CLOSE_ADJCHRATE"),
                "d10_chg": r.get("D10_CLOSE_ADJCHRATE"),
                "d20_chg": r.get("D20_CLOSE_ADJCHRATE"),
                "d30_chg": r.get("D30_CLOSE_ADJCHRATE"),
                "buy_seat": r.get("BUY_SEAT") or "",
                "sell_seat": r.get("SELL_SEAT") or "",
                "sum_buy_amt": r.get("SUM_BUY_AMT"),
                "sum_sell_amt": r.get("SUM_SELL_AMT"),
                "net_bs_amt": r.get("NET_BS_AMT"),
                "deal_amount_ratio": r.get("DEAL_AMOUNT_RATIO"),
            })
        total = res.get("count") or res.get("total") or 0
        if len(out) >= int(total or 0) or len(rows) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.15)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="龙虎榜个股榜单历史回补")
    ap.add_argument("--from", dest="frm", default="2016-01-01")
    ap.add_argument("--to", dest="to", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--limit", type=int, default=0, help="本次最多补多少个缺失日（0=不限）")
    ap.add_argument("--sleep", type=float, default=0.25, help="每日间隔秒（防限流）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=60)
    acted = L.ensure_schema(con)
    if acted:
        print(f"[lhb-backfill] 结构补齐：{', '.join(acted)}", flush=True)

    want = trading_days(args.frm, args.to)
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM lhb_stocks WHERE trade_date>=? AND trade_date<=?",
        (args.frm, args.to))}
    todo = [d for d in want if d not in have]
    if args.limit:
        todo = todo[: args.limit]

    print(f"[lhb-backfill] 交易日 {len(want)} 个｜已有 {len(have)} 个｜待补 {len(todo)} 个"
          f"（{args.frm} ~ {args.to}）", flush=True)
    if not todo:
        print("[lhb-backfill] 无需回补", flush=True)
        return 0

    t0 = time.time()
    done = rows_tot = 0
    fail_streak = 0
    empty_days: list[str] = []
    try:
        for i, d in enumerate(todo, 1):
            try:
                stocks = fetch_stocks_paged(d)
            except Exception as e:  # noqa: BLE001
                fail_streak += 1
                print(f"  {d}: 失败 {type(e).__name__}: {str(e)[:70]}"
                      f"（连续失败 {fail_streak}）", flush=True)
                if fail_streak >= MAX_FAIL_STREAK:
                    print("[lhb-backfill] 连续失败达上限，中止（已补的天数已落库）", flush=True)
                    break
                time.sleep(1.5 * fail_streak)
                continue
            fail_streak = 0
            if not stocks:
                empty_days.append(d)
                continue
            con.execute("DELETE FROM lhb_stocks WHERE trade_date=?", (d,))
            n = L._insert(con, "lhb_stocks", stocks)
            con.commit()
            done += 1
            rows_tot += n
            if i % 25 == 0 or i == len(todo):
                rate = done / max(time.time() - t0, 1e-6)
                left = (len(todo) - i) / max(rate, 1e-6)
                print(f"  [{i}/{len(todo)}] {d} +{n} 行｜累计 {rows_tot} 行｜"
                      f"{rate:.1f} 日/秒｜预计剩 {left/60:.0f} 分钟", flush=True)
            time.sleep(args.sleep)
        mx, mn, cnt, days = con.execute(
            "SELECT MAX(trade_date), MIN(trade_date), COUNT(*), COUNT(DISTINCT trade_date) "
            "FROM lhb_stocks").fetchone()
    finally:
        con.close()

    print(f"[lhb-backfill] 完成：补 {done} 个交易日 / {rows_tot} 行｜耗时 "
          f"{(time.time()-t0)/60:.1f} 分钟｜库内 {cnt} 行 / {days} 个交易日（{mn} ~ {mx}）",
          flush=True)
    if empty_days:
        print(f"[lhb-backfill] 接口返回空（非交易日或数据缺失）{len(empty_days)} 天："
              f"{empty_days[:8]}{' ...' if len(empty_days) > 8 else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
