# -*- coding: utf-8 -*-
"""把 stock_indicators_daily 的因子历史从「最近 260 日」回补到 2016-01-01（2026-10-31）。

为什么必须做：hypo.evaluate() / evidence.py / oss_mine 的长样本复核，样本深度完全取决于
这张表的日期跨度。回补前表内只有 288 个交易日（2025-08-18 起），意味着「所有 claim 的
t 值」其实都建立在 14 个月、单一牛市的样本上 —— 结论对别的市场环境未经验证。

口径：
  - 数据源 data/kline_daily.db.kline（全市场 5573 只，与新浪 hfq 逐字段一致），不出网。
  - 因子由**全长序列**计算（warm-up 从 2015-01-01 起），只落库 >= --from（默认 2016-01-01）
    的行，保证 MA200 / ADX / TRIX 等长周期因子在落库首日就已收敛，不会出现"0 值冒充有效值"。
  - 股票池沿用 indicators.run() 的口径：剔除 exclusion_pool + 名称含 ST/退 的票。
    因此长样本 = 「当前干净宇宙」，仍存在"当前 ST 被整体剔除"的轻微幸存偏差，已知并披露。
  - 与 indicators.run() 共用 compute_indicators，避免两套公式算出的因子对不上。

用法：
    python tools/backfill_factors.py --from 2016-01-01 --warm 2015-01-01
    python tools/backfill_factors.py --limit 50        # 小批量自测
"""
import os
import sys
import time
import sqlite3
import argparse
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from viewer.indicators import compute_indicators, ensure_tables, IND_COLS  # noqa: E402

APP_DB = os.path.join(ROOT, "viewer", "app.db")
KLINE_DB = os.path.join(ROOT, "data", "kline_daily.db")


def _ymd(s: str) -> int:
    return int(str(s).replace("-", "")[:8])


def _iso(d: int) -> str:
    s = str(d).zfill(8)
    return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])


def load_universe(con):
    """返回 {code: name}，沿用 indicators.run() 的剔除口径。"""
    names = {c: (n or c) for c, n in
             con.execute("SELECT code,name FROM stock_names WHERE code IS NOT NULL")}
    try:
        from viewer.universe_filter import get_exclusion_pool
        excl, _, _ = get_exclusion_pool()
    except Exception as e:  # noqa: BLE001
        print("[backfill] 剔除池不可用(%s)，仅按名称过滤 ST/退" % e, flush=True)
        excl = set()
    out = {}
    n_st = n_ex = 0
    for code, nm in names.items():
        if "ST" in nm or "退" in nm:
            n_st += 1
            continue
        if code in excl:
            n_ex += 1
            continue
        out[code] = nm
    print("[backfill] 股票池 %d 只（剔 ST/退 %d、剔噪声 %d）" % (len(out), n_st, n_ex), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default="2016-01-01", help="落库起始日")
    ap.add_argument("--warm", dest="d_warm", default="2015-01-01", help="因子预热起始日")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=40, help="每 N 只提交一次")
    ap.add_argument("--codes", default="", help="逗号分隔，仅回补指定代码（自测用）")
    args = ap.parse_args()

    frm, warm = _ymd(args.d_from), _ymd(args.d_warm)
    t0 = time.time()
    con = sqlite3.connect(APP_DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    ensure_tables(con)
    uni = load_universe(con)
    codes = sorted(uni)
    if args.codes:
        codes = [c for c in args.codes.split(",") if c]
    if args.limit:
        codes = codes[:args.limit]
    print("[backfill] 目标 %d 只｜落库 %s 起｜预热 %s 起" % (len(codes), _iso(frm), _iso(warm)), flush=True)

    kcon = sqlite3.connect(KLINE_DB, timeout=60)
    insert_sql = ("INSERT OR REPLACE INTO stock_indicators_daily(date,code,name,%s) VALUES (%s)"
                  % (",".join(IND_COLS), ",".join(["?"] * (len(IND_COLS) + 3))))

    done = skip = written = 0
    buf = []
    for code in codes:
        rows = kcon.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND c>0 ORDER BY d",
            (code, warm)).fetchall()
        done += 1
        if len(rows) < 260:      # 上市不足一年，因子不可信
            skip += 1
        else:
            df = pd.DataFrame(rows, columns=["d", "open", "close", "high", "low", "volume"])
            df["code"] = code
            df["date"] = df["d"].map(_iso)
            res = compute_indicators(df)
            res = res[res["date"] >= _iso(frm)]
            nm = uni.get(code, code)
            recs = res.to_dict("records")
            for r in recs:
                vals = []
                for c in IND_COLS:
                    x = r.get(c)
                    vals.append(None if (x is None or x != x) else float(x))
                buf.append((r["date"], code, nm, *vals))
            written += len(recs)
        if done % args.chunk == 0:
            if buf:
                con.executemany(insert_sql, buf)
                con.commit()
                buf = []
            el = time.time() - t0
            eta = el / done * (len(codes) - done)
            print("  ..%d/%d  行 %d  跳过 %d  %.0fs  ETA %.0fmin"
                  % (done, len(codes), written, skip, el, eta / 60), flush=True)
    if buf:
        con.executemany(insert_sql, buf)
        con.commit()

    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) "
                    "FROM stock_indicators_daily").fetchone()
    print("[backfill] 完成：代码 %d（跳过 %d）｜写入行 %d｜表内 %d 行 / %d 个交易日 / %s ~ %s｜%.1fs"
          % (done, skip, written, n[0], n[1], n[2], n[3], time.time() - t0), flush=True)
    kcon.close()
    con.close()


if __name__ == "__main__":
    main()
