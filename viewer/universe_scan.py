# -*- coding: utf-8 -*-
"""全宇宙多因子扫描（P2，2026-10-26）。

解决 C1（评估范围仍小）：四支柱只扫流动性前 ~500、Kronos 每次抽 ~60，干净宇宙 5268 没跑满。
本模块对【评估宇宙 = 有因子快照且已剔除垃圾的干净宇宙】全部标的做一遍廉价多因子打分，
直接从 stock_indicators（P1 已算好的 52 因子）读取，一次 DataFrame 全量扫描，秒级覆盖全市场。

三轴因子（权重可调，均在全宇宙内做百分位排名，0~1）：
    动量趋势 0.45  —— 站上 MA20/60/200、MACD 金叉区、站上 Supertrend/BOLL
    资金量能 0.25  —— 量比、ROC、VR
    质量风险 0.30  —— ATR/close 越低越好、RSI 不极端、BIAS 不偏
合成 composite → 百分位排名 → 分档 A(前10%)/B(10~30%)/C(30~70%)/D(后30%)。

落库 stock_universe_scan(code PK)：score/rank_pct/tier/momentum/volume/quality/reasons/updated。
估值轴（PE/PB/市值）暂未接入——依赖 stock_daily_snapshot（须进哥本机跑 fetch_a_shares_snapshot.py 填满，治 C4）。

用法：
    python tools/sched_job.py universe_scan          # 经调度
    .venv/Scripts/python.exe -m viewer.universe_scan  # 直接跑
"""
import os
import sys
import time
import json
import logging
import sqlite3

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 三轴权重（合计 1.0，集中配置见 viewer/config.py）
from viewer.config import W_MOMENTUM, W_VOLUME, W_QUALITY


def ensure_tables(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS stock_universe_scan (
        code TEXT PRIMARY KEY, name TEXT, score REAL, rank_pct REAL, tier TEXT,
        momentum REAL, volume REAL, quality REAL, reasons TEXT, updated TEXT)""")
    # 2026-10-29：全市场打分的**每日历史**。
    # 上面那张表以 code 为主键，每天覆盖写，只保留「当前状态」——于是「评分到底有没有
    # 区分度」这个问题只能拿每天选出的 top20 去算，样本被截断，算出来的 IC 必然被大幅
    # 低估（实测 IC_t1 仅 +0.030，但那是 18 只票上的秩相关，标准误就有 0.24）。
    # 存历史后每天约 5000 只，30 个交易日就有 15 万级样本，才能算真 IC + 分层收益。
    con.execute("""CREATE TABLE IF NOT EXISTS stock_universe_scan_daily (
        trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
        score REAL, rank_pct REAL, tier TEXT, updated TEXT,
        PRIMARY KEY (trade_date, code))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_usd_date ON stock_universe_scan_daily(trade_date)")
    con.commit()


def _pct_rank(s: pd.Series) -> pd.Series:
    """百分位排名（0~1），NaN 填 0.5（中性），避免缺失票被极端化。"""
    r = s.rank(pct=True, method="average")
    return r.fillna(0.5)


def _safe_div(a, b):
    out = a / b.replace(0, np.nan)
    return out


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    """输入 df：每行一只票的全部因子（来自 stock_indicators）。返回带三轴分+composite 的 df。"""
    c = df["close"]

    # —— 动量趋势轴 ——
    mom_parts = []
    for n in (20, 60, 200):
        col = "ma%d" % n
        if col in df and (df[col] > 0).any():
            mom_parts.append(_pct_rank(_safe_div(c, df[col]) - 1.0))   # 股价/均线-1，越大越多头
    # MACD 金叉区：macd>0 且 macdh>0
    if {"macd", "macdh"} <= set(df.columns):
        mom_parts.append(_pct_rank(df["macd"]))          # macd 值越大越多头
        mom_parts.append(_pct_rank(df["macdh"]))         # macdh 越大越金叉
    # 站上 Supertrend / BOLL 中轨（二值 → pct_rank 后仍可区分）
    if "supertrend" in df:
        mom_parts.append((c > df["supertrend"]).astype(float))
    if "boll" in df and (df["boll"] > 0).any():
        mom_parts.append((c > df["boll"]).astype(float))
    momentum = pd.concat(mom_parts, axis=1).mean(axis=1) if mom_parts else pd.Series(0.5, index=df.index)

    # —— 资金量能轴 ——
    vol_parts = []
    if "volume_ratio" in df:
        vol_parts.append(_pct_rank(df["volume_ratio"].clip(upper=5)))   # 量比（封顶 5，避免极端拉爆）
    if "roc" in df:
        vol_parts.append(_pct_rank(df["roc"]))                          # ROC(12) 动量与收益
    if "vr" in df and (df["vr"] > 0).any():
        vol_parts.append(_pct_rank(df["vr"].clip(upper=300)))           # VR(26) 量能
    volume = pd.concat(vol_parts, axis=1).mean(axis=1) if vol_parts else pd.Series(0.5, index=df.index)

    # —— 质量风险轴（越高越好）——
    q_parts = []
    if "atr" in df and (c > 0).any():
        atr_rel = _safe_div(df["atr"], c)
        q_parts.append(1.0 - _pct_rank(atr_rel))          # ATR/close 越低，波动风险越小 → 质量越高
    if "rsi6" in df:
        rsi = df["rsi6"].clip(0, 100)
        rsi_health = (1.0 - (rsi - 50.0).abs() / 50.0).clip(0, 1)   # RSI=50 最佳，越极端越差
        q_parts.append(rsi_health)
    if "bias12" in df:
        q_parts.append(1.0 - _pct_rank(df["bias12"].abs()))          # 偏离 12 日均线越小越稳
    quality = pd.concat(q_parts, axis=1).mean(axis=1) if q_parts else pd.Series(0.5, index=df.index)

    composite = W_MOMENTUM * momentum + W_VOLUME * volume + W_QUALITY * quality
    out = pd.DataFrame(index=df.index)
    out["momentum"] = momentum
    out["volume"] = volume
    out["quality"] = quality
    out["score"] = composite
    out["rank_pct"] = _pct_rank(composite)
    return out


def _tier(rank_pct: float) -> str:
    if rank_pct >= 0.90:
        return "A"
    if rank_pct >= 0.70:
        return "B"
    if rank_pct >= 0.30:
        return "C"
    return "D"


def _reasons(row: pd.Series, df_cols: set) -> list:
    buy = []
    if "ma20" in df_cols and row.get("ma20") and row["close"] > row["ma20"]:
        buy.append("站上MA20")
    if "ma60" in df_cols and row.get("ma60") and row["close"] > row["ma60"]:
        buy.append("站上MA60")
    if "macd" in df_cols and row.get("macd") is not None and row["macd"] > 0 and (row.get("macdh") or 0) > 0:
        buy.append("MACD金叉区")
    if "supertrend" in df_cols and row.get("supertrend") and row["close"] > row["supertrend"]:
        buy.append("站上Supertrend")
    if "volume_ratio" in df_cols and (row.get("volume_ratio") or 0) > 1.5:
        buy.append("量比>1.5")
    if "rsi6" in df_cols and row.get("rsi6") is not None and 30 <= row["rsi6"] <= 70:
        buy.append("RSI健康区")
    return buy


def run(con=None, asof: str = None):
    """全宇宙扫描：读取 stock_indicators → 打分 → 落 stock_universe_scan。"""
    own = con is None
    if con is None:
        from viewer.db import Database
        con = Database().conn
    ensure_tables(con)
    t0 = time.time()
    # 剔除池内的票从扫描结果里清掉（与 indicators 口径一致，避免上一轮陈旧行残留）
    #
    # 2026-10-31 整改（测评 P0-B）：原 `except Exception: excl = set()` 是 fail-open ——
    # 池读不到时不但不剔，还**保留了上一轮的陈旧行**，于是"剔除没生效"会被显示成"名单没变"。
    # 改为 fail-closed：读不到就本轮不产出，返回结构化失败。
    from viewer.universe_filter import get_exclusion_pool_strict, ExclusionPoolUnavailable
    try:
        excl, _, _ = get_exclusion_pool_strict()
    except ExclusionPoolUnavailable as _e:
        logging.getLogger(__name__).error(
            "[universe_scan] 剔除池不可用，按 fail-closed 停止本轮扫描：%s", _e)
        return {"ok": False, "error": "exclusion_pool_unavailable", "detail": str(_e)[:300]}
    if excl:
        ph = ",".join("?" * len(excl))
        con.execute("DELETE FROM stock_universe_scan WHERE code IN (%s)" % ph, list(excl))

    cur = con.execute("SELECT * FROM stock_indicators")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if not rows:
        print("[universe_scan] stock_indicators 为空，跳过", flush=True)
        if own:
            con.close()
        return {"ok": False, "error": "no indicators"}
    df = pd.DataFrame(rows, columns=cols)
    df_cols = set(cols)
    for col in cols:
        if col not in ("code", "name", "date"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

    scored = compute_scores(df)
    # 取 max 而不是 iloc[0]：iloc[0] 是"第一只票的日期"，若个体数据缺失会取到陈旧日期，
    # 而 trade_date 是每日历史表的主键一半，错一天就会把两天的快照混在一行。
    updated = asof or (str(df["date"].max())[:10] if "date" in df.columns else "")
    recs = []
    for i, r in df.iterrows():
        s = scored.loc[i]
        reasons = _reasons(r, df_cols)
        rp = float(s["rank_pct"])
        recs.append((
            r["code"], r.get("name"),
            round(float(s["score"]), 6), round(rp, 6), _tier(rp),
            round(float(s["momentum"]), 6), round(float(s["volume"]), 6), round(float(s["quality"]), 6),
            json.dumps(reasons, ensure_ascii=False), updated,
        ))
    con.executemany(
        "INSERT OR REPLACE INTO stock_universe_scan"
        "(code,name,score,rank_pct,tier,momentum,volume,quality,reasons,updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", recs)
    # 同日历史留档（当日重复跑则覆盖当日，不产生重复行）
    if updated:
        con.executemany(
            "INSERT OR REPLACE INTO stock_universe_scan_daily"
            "(trade_date,code,name,score,rank_pct,tier,updated) VALUES (?,?,?,?,?,?,?)",
            [(updated, r[0], r[1], r[2], r[3], r[4], updated) for r in recs])
    con.commit()
    if own:
        con.close()
    print("[universe_scan] 扫描 %d 只 用时 %.1fs" % (len(recs), time.time() - t0), flush=True)
    return {"ok": True, "scanned": len(recs), "secs": round(time.time() - t0, 1)}


if __name__ == "__main__":
    run()
