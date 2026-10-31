# -*- coding: utf-8 -*-
"""盘中快照采集（2026-10-21 dy9）——为「14:30 重建 vs 盘后重建」攒回测样本。

背景与取舍（进哥 2026-10-21 拍板「按你的建议处理」）：
  · dy8 的硬结论：信号在**当天**有效，改到**次日**买入后超额转负。
  · 理论上「14:30 盘中重建 → 当日尾盘买入」能吃到那段 alpha。
  · 但**历史没有 14:30 快照**（本地 bars 只有日线收盘），所以这件事**无法立刻回测证明**。
  · 故本模块不改调度，只做「向前攒数据」：每交易日 14:30 抓一次快照入库，
    攒够 5~10 个交易日后即可回测「盘中价买入 vs 收盘价买入 vs 次日买入」，
    用真实数据决定是否把 resonance_build 挪到盘中。

数据源：腾讯 qt.gtimg.cn（与 fetch_float 同源，已验证可用）
  [3]现价 [32]涨幅% [38]换手率% [44]流通市值(亿) [49]量比 [30]行情时间
入库：intraday_snap(asof, code, price, pct, turnover_pct, vol_ratio, mktcap_yi, quote_time)
     主键 (asof_date, code)，同一天重复跑覆盖（保留最后一次）
"""
from __future__ import annotations
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")


def _qq(code: str) -> str:
    c = str(code).zfill(6)
    if c[0] in ("6", "9"):
        return "sh" + c
    if c[:2] in ("43", "83", "87", "88"):
        return "bj" + c
    return "sz" + c


def _fetch(codes, batch=60):
    import emapi
    out = {}
    for i in range(0, len(codes), batch):
        part = codes[i:i + batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(_qq(c) for c in part)
        try:
            raw = emapi._get_text(url, "https://gu.qq.com/")
        except Exception as e:  # noqa: BLE001
            print("  批次 %d ERR %s %s" % (i + 1, type(e).__name__, str(e)[:80]))
            continue
        for seg in raw.split(";"):
            seg = seg.strip()
            if "=" not in seg:
                continue
            parts = seg.split('="', 1)
            if len(parts) < 2:
                continue
            f = parts[1].rstrip('"').split("~")
            if len(f) < 50:
                continue
            def num(idx):
                try:
                    return float(f[idx])
                except Exception:  # noqa: BLE001
                    return None
            out[f[2].strip()] = {
                "price": num(3), "pct": num(32), "turnover_pct": num(38),
                "mktcap_yi": num(44), "vol_ratio": num(49), "quote_time": f[30],
            }
        time.sleep(0.3)
    return out


def main() -> dict:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS intraday_snap(
        snap_date TEXT NOT NULL,
        snap_time TEXT NOT NULL,
        code TEXT NOT NULL,
        price REAL,
        pct REAL,
        turnover_pct REAL,
        vol_ratio REAL,
        mktcap_yi REAL,
        quote_time TEXT,
        PRIMARY KEY (snap_date, code)
    )""")
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM res_stock_card ORDER BY code")]
    if not codes:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM sig_unified ORDER BY code")]
    if not codes:
        con.close()
        return {"ok": False, "error": "无候选标的"}

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    today = time.strftime("%Y-%m-%d")
    data = _fetch(codes)
    if not data:
        con.close()
        return {"ok": False, "error": "行情全部未取到（网络或接口异常）", "at": now}

    # 校验行情新鲜度：取到的最新 quote_time 若不是今天，说明是休市日/数据源未更新
    qt = max((v.get("quote_time") or "" for v in data.values()), default="")
    qt_date = qt[:8]
    stale = (qt_date != today.replace("-", ""))
    rows = [(today, now[11:16], code, v["price"], v["pct"], v["turnover_pct"],
             v["vol_ratio"], v["mktcap_yi"], v["quote_time"]) for code, v in data.items()]
    con.executemany("""INSERT INTO intraday_snap
        (snap_date,snap_time,code,price,pct,turnover_pct,vol_ratio,mktcap_yi,quote_time)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(snap_date,code) DO UPDATE SET
          snap_time=excluded.snap_time, price=excluded.price, pct=excluded.pct,
          turnover_pct=excluded.turnover_pct, vol_ratio=excluded.vol_ratio,
          mktcap_yi=excluded.mktcap_yi, quote_time=excluded.quote_time""", rows)
    con.commit()
    n_days = con.execute("SELECT COUNT(DISTINCT snap_date) d FROM intraday_snap").fetchone()["d"]
    n_today = con.execute("SELECT COUNT(*) c FROM intraday_snap WHERE snap_date=?", (today,)).fetchone()["c"]
    con.close()
    # 攒够 5 天前，明确提示"还不够回测"——不让空数据冒充结论
    ready = n_days >= 5
    return {"ok": True, "snap_date": today, "at": now, "rows": len(rows),
            "snap_days": n_days, "today_rows": n_today,
            "quote_time": qt, "stale": stale,
            "backtest_ready": ready,
            "note": ("已攒 %d 天，可回测「盘中/收盘/次日」三种买入时点" % n_days) if ready
                    else ("已攒 %d/5 天，暂不足以回测" % n_days)}


if __name__ == "__main__":
    r = main()
    print(r)
    sys.exit(0 if r.get("ok") else 1)
