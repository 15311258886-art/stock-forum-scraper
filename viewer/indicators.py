# -*- coding: utf-8 -*-
"""技术指标 / 因子层（P1，2026-10-26）。

来源：直接吸收 myhhub/stock(InStock) 的 calculate_indicator.py 算法 + pythonstock/stock 的
`guess_indicators_daily` 建表范式，适配成本地 SQLite + 纯 pandas/numpy（本环境无 talib）。
原版用 talib.STOCH/BBANDS/RSI... 这里全部用等价 pandas 公式重写，参数与 InStock 保持一致
（KDJ 用 EMA5 平滑、BOLL 用总体标准差 ddof=0 等），结果对齐同花顺/通信达口径。

数据来源：本地 data/kline_daily.db.kline（code,d(YYYYMMDD),o,c,h,l,v），全市场 5550 只，沙箱可直读，不需出网。
落库：
    stock_indicators_daily (code,date 联合主键) —— 每只票最近 WINDOW(260) 交易日因子历史（查看器用；K线拉取回看 KLINE_LOOKBACK_DAYS=420 日历日以确保 MA200 等长因子有效）
    stock_indicators       (code 主键)          —— 每只票最新一日因子快照
    stock_signals          (code 主键)          —— 由因子派生的买卖信号（buy/sell reasons）

用法：
    python tools/sched_job.py indicators_daily            # 日更（仅 upsert 最新一日）
    .venv/Scripts/python.exe -m viewer.indicators --mode backfill   # 一次性回填 window 历史
    .venv/Scripts/python.exe -m viewer.indicators --mode daily --limit 50  # 小批量自测
"""
import os
import sys
import time
import json
import logging
import math
import sqlite3

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer.config import WINDOW, KLINE_LOOKBACK_DAYS  # 集中配置（见 viewer/config.py）

# ---- 因子列（与 pythonstock guess_indicators_daily 对齐并扩展）----
IND_COLS = [
    "close", "ma5", "ma10", "ma20", "ma60", "ma200",
    "macd", "macds", "macdh",
    "kdjk", "kdjd", "kdjj",
    "boll", "boll_ub", "boll_lb",
    "rsi6", "rsi12", "rsi24",
    "wr6", "wr10", "wr14",
    "cci", "atr",
    "pdi", "mdi", "adx", "adxr",
    "trix", "trix_20_sma",
    "vr", "vr_6_sma",
    "roc", "rocma",
    "obv",
    "bias6", "bias12", "bias24",
    "dma", "dma_10_sma",
    "tema",
    "ppo", "ppos", "ppoh",
    "stochrsi_k", "stochrsi_d",
    "psy", "psyma",
    "brar_ar", "brar_br",
    "sar", "supertrend", "ene",
    "vol_ma5", "vol_ma10", "volume_ratio",
]

ALL_COLS = ["date", "code", "name"] + IND_COLS


def ensure_tables(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS stock_indicators_daily (
        date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
        %s,
        PRIMARY KEY (code, date))""" % ",\n        ".join("%s REAL" % c for c in IND_COLS))
    con.execute("""CREATE TABLE IF NOT EXISTS stock_indicators (
        code TEXT PRIMARY KEY, name TEXT, date TEXT,
        %s)""" % ",\n        ".join("%s REAL" % c for c in IND_COLS))
    con.execute("""CREATE TABLE IF NOT EXISTS stock_signals (
        code TEXT PRIMARY KEY, name TEXT, date TEXT,
        buy_score REAL, sell_score REAL,
        buy_reasons TEXT, sell_reasons TEXT)""")
    # 2026-11-01（P1-4 存储成本）**移除** 原第 77 行：
    #     CREATE INDEX IF NOT EXISTS idx_sid_code_date ON stock_indicators_daily(code, date)
    # 原因：它与上表的主键自动索引 sqlite_autoindex_stock_indicators_daily_1
    # （PRIMARY KEY (code, date)）逐页完全相同 —— 同为 97,134 页 / 397,860,864 B（379.4 MB），
    # sqlite_stat1 也同为 "1402740 237 1"。等于同一份索引存了两遍，白占 379 MB。
    # 已 DROP，并验证过主键索引能完全顶替它（五条代表性查询的计划形状与耗时不变）。
    # 注意：`CREATE INDEX IF NOT EXISTS` 不报错也不提示，所以这行留着就会在每次因子重算时
    # 把 379 MB 悄悄建回来 —— 这是它必须被删掉、而不只是"知道就行"的原因。
    # 若确需重建（万一某条查询退化成全表扫）：见 tools/ensure_indexes.py 顶部的回滚 DDL，
    # 并先把 tools/ensure_indexes.py 的 REDUNDANT_INDEXES 里对应条目去掉，否则巡检会一直报警。
    con.commit()


# ---------------- 因子计算（纯 pandas，输入 df 按日期升序，含 open/high/low/close/volume）----------------
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs))


def _sar(high, low, accel=0.02, max_ac=0.20):
    n = len(high)
    sar = np.full(n, np.nan)
    if n < 2:
        return sar
    uptrend = high[1] >= high[0]
    ep = high[0] if uptrend else low[0]
    sar[0] = low[0] if uptrend else high[0]
    af = accel
    for i in range(1, n):
        prev = sar[i - 1]
        if uptrend:
            cur = prev + af * (ep - prev)
            cur = min(cur, low[i - 1], low[i])
            if high[i] > ep:
                ep = high[i]; af = min(af + accel, max_ac)
            if low[i] < cur:
                uptrend = False; cur = ep; ep = low[i]; af = accel
        else:
            cur = prev + af * (ep - prev)
            cur = max(cur, high[i - 1], high[i])
            if low[i] < ep:
                ep = low[i]; af = min(af + accel, max_ac)
            if high[i] > cur:
                uptrend = True; cur = ep; ep = high[i]; af = accel
        sar[i] = cur
    return sar


def _supertrend(high, low, close, atr, mult=3):
    n = len(close)
    ub = np.empty(n); lb = np.empty(n); st = np.empty(n)
    hl_avg = (high + low) / 2.0
    b_ub = hl_avg + mult * atr
    b_lb = hl_avg - mult * atr
    for i in range(n):
        if i == 0:
            ub[i] = b_ub[i]; lb[i] = b_lb[i]
            st[i] = b_ub[i] if close[i] <= b_ub[i] else b_lb[i]
            continue
        lc = close[i - 1]; cc = close[i]
        lub = ub[i - 1]; llb = lb[i - 1]; lst = st[i - 1]
        cu = b_ub[i]; cl = b_lb[i]
        ub[i] = cu if (cu < lub or lc > lub) else lub
        lb[i] = cl if (cl > llb or lc < llb) else llb
        if lst == lub:
            st[i] = ub[i] if cc <= ub[i] else lb[i]
        else:
            st[i] = lb[i] if cc > lb[i] else ub[i]
    return st


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df 列：date,code,open,high,low,close,volume（按 date 升序）。返回带全部因子的同长度 df。"""
    c = df["close"]; h = df["high"]; l = df["low"]; o = df["open"]; v = df["volume"]
    out = pd.DataFrame(index=df.index)
    out["date"] = df["date"]; out["code"] = df["code"]

    # 均线
    for n in (5, 10, 20, 60, 200):
        out["ma%d" % n] = c.rolling(n).mean()
    # MACD(12,26,9)：macd=DIF, macds=DEA, macdh=DIF-DEA
    e12 = _ema(c, 12); e26 = _ema(c, 26)
    dif = e12 - e26; dea = _ema(dif, 9)
    out["macd"] = dif; out["macds"] = dea; out["macdh"] = dif - dea
    # KDJ(9,EMA5)：K=EMA(RSV,5), D=EMA(K,5), J=3K-2D
    llv = l.rolling(9).min(); hhv = h.rolling(9).max()
    rsv = (c - llv) / (hhv - llv).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)
    K = _ema(rsv, 5); D = _ema(K, 5)
    out["kdjk"] = K; out["kdjd"] = D; out["kdjj"] = 3 * K - 2 * D
    # BOLL(20,2) 总体标准差
    mid = c.rolling(20).mean(); sd = c.rolling(20).std(ddof=0)
    out["boll"] = mid; out["boll_ub"] = mid + 2 * sd; out["boll_lb"] = mid - 2 * sd
    # RSI
    out["rsi6"] = _rsi(c, 6); out["rsi12"] = _rsi(c, 12); out["rsi24"] = _rsi(c, 24)
    # WR
    for n, key in ((6, "wr6"), (10, "wr10"), (14, "wr14")):
        hh = h.rolling(n).max(); ll = l.rolling(n).min()
        out[key] = -100 * (hh - c) / (hh - ll).replace(0, np.nan)
    # CCI(14)
    tp = (h + l + c) / 3.0; mt = tp.rolling(14).mean()
    out["cci"] = (tp - mt) / (0.015 * (tp - mt).abs().rolling(14).mean()).replace(0, np.nan)
    # ATR(14) Wilder
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (pc - l).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    out["atr"] = atr
    # DMI/ADX（stockstats 风格，对齐 InStock）
    hd = h.diff(); ld = -l.diff()
    hm = (hd + hd.abs()) / 2.0; lm = (ld + ld.abs()) / 2.0
    pdm = pd.Series(np.where(hm.values > lm.values, hm.values, 0.0), index=df.index)
    mdm = pd.Series(np.where(lm.values > hm.values, lm.values, 0.0), index=df.index)
    pdm_e = pdm.ewm(span=14, adjust=False).mean()
    mdm_e = mdm.ewm(span=14, adjust=False).mean()
    atr_safe = atr.replace(0, np.nan)
    pdi = (pdm_e / atr_safe * 100); mdi = (mdm_e / atr_safe * 100)
    out["pdi"] = pdi; out["mdi"] = mdi
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    out["adx"] = dx.ewm(span=6, adjust=False).mean()
    out["adxr"] = out["adx"].ewm(span=6, adjust=False).mean()
    # TRIX(12) + 20SMA
    e1 = _ema(c, 12); e2 = _ema(e1, 12); e3 = _ema(e2, 12)
    trix = (e3 - e3.shift(1)) / e3.shift(1).replace(0, np.nan) * 100
    out["trix"] = trix; out["trix_20_sma"] = trix.rolling(20).mean()
    # VR(26)
    pct = c.pct_change() * 100
    av = np.where(pct.values > 0, v.values, 0.0)
    bv = np.where(pct.values < 0, v.values, 0.0)
    cv = np.where(pct.values == 0, v.values, 0.0)
    avs = pd.Series(av, index=df.index).rolling(26).sum()
    bvs = pd.Series(bv, index=df.index).rolling(26).sum()
    cvs = pd.Series(cv, index=df.index).rolling(26).sum()
    vr = (avs + cvs / 2.0) / (bvs + cvs / 2.0).replace(0, np.nan) * 100
    out["vr"] = vr; out["vr_6_sma"] = vr.rolling(6).mean()
    # ROC(12)
    out["roc"] = (c / c.shift(12) - 1) * 100; out["rocma"] = out["roc"].rolling(6).mean()
    # OBV
    sign = np.sign(c.diff().fillna(0).values)
    out["obv"] = pd.Series(sign, index=df.index) * v; out["obv"] = out["obv"].cumsum()
    # BIAS
    for n, key in ((6, "bias6"), (12, "bias12"), (24, "bias24")):
        ma = c.rolling(n).mean()
        out[key] = (c - ma) / ma.replace(0, np.nan) * 100
    # DMA(10,50)
    ma10 = c.rolling(10).mean(); ma50 = c.rolling(50).mean()
    out["dma"] = ma10 - ma50; out["dma_10_sma"] = out["dma"].rolling(10).mean()
    # TEMA(14)
    t1 = _ema(c, 14); t2 = _ema(t1, 14); t3 = _ema(t2, 14)
    out["tema"] = 3 * t1 - 3 * t2 + t3
    # PPO(12,26)
    ppo = (e12 - e26) / e26.replace(0, np.nan) * 100
    out["ppo"] = ppo; out["ppos"] = _ema(ppo, 9); out["ppoh"] = ppo - out["ppos"]
    # STOCHRSI(14)
    rsi14 = _rsi(c, 14)
    rmin = rsi14.rolling(14).min(); rmax = rsi14.rolling(14).max()
    k = (rsi14 - rmin) / (rmax - rmin).replace(0, np.nan) * 100
    out["stochrsi_k"] = k; out["stochrsi_d"] = k.rolling(3).mean()
    # PSY(12)
    up = (c > pc).rolling(12).sum() / 12.0 * 100
    out["psy"] = up; out["psyma"] = up.rolling(6).mean()
    # BRAR(26)
    out["brar_ar"] = (h - o).rolling(26).sum() / (o - l).rolling(26).sum().replace(0, np.nan) * 100
    out["brar_br"] = (h - pc).rolling(26).sum() / (pc - l).rolling(26).sum().replace(0, np.nan) * 100
    # SAR
    out["sar"] = _sar(h.values, l.values)
    # Supertrend(3*ATR)
    out["supertrend"] = _supertrend(h.values, l.values, c.values, atr.values, mult=3)
    # ENE(基于 ma10)
    out["ene"] = ((1.11 + 0.91) / 2.0) * c.rolling(10).mean()  # 中线
    # 量能
    out["vol_ma5"] = v.rolling(5).mean(); out["vol_ma10"] = v.rolling(10).mean()
    out["volume_ratio"] = v / out["vol_ma5"].replace(0, np.nan)

    # 收尾：inf/nan → 0（对齐 InStock 习惯，避免污染下游）
    for col in IND_COLS:
        if col in ("date", "code", "name", "close"):
            continue
        s = out[col]
        s = s.where(np.isfinite(s), 0.0)
        out[col] = s
    out["close"] = c
    return out


# ---------------- 信号派生（由最新因子 → 买卖原因）----------------
def _derive_signals(row: dict):
    buy, sell = [], []
    r = row
    if r.get("rsi6") is not None:
        if r["rsi6"] < 20: buy.append("RSI6超卖<%0.0f" % r["rsi6"])
        if r["rsi6"] > 80: sell.append("RSI6超买>%0.0f" % r["rsi6"])
        # Williams %R 取值 [-100,0]：-80 以下超卖(买)，-20 以上超买(卖)
        if r["wr6"] < -80: buy.append("WR6超卖<%0.0f" % r["wr6"])
        if r["wr6"] > -20: sell.append("WR6超买>%0.0f" % r["wr6"])
        if r["close"] < (r["boll_lb"] or 0) and r["boll_lb"]: buy.append("跌破BOLL下轨")
        if r["close"] > (r["boll_ub"] or 0) and r["boll_ub"]: sell.append("突破BOLL上轨")
        if r["macd"] > 0 and r["macdh"] > 0: buy.append("MACD零上金叉区")
        if r["macd"] < 0 and r["macdh"] < 0: sell.append("MACD零下死叉区")
        if r["kdjk"] < 20 and r["kdjk"] >= r["kdjd"]: buy.append("KDJ超卖金叉")
        if r["kdjk"] > 80 and r["kdjk"] <= r["kdjd"]: sell.append("KDJ超买死叉")
        if r["pdi"] > r["mdi"]: buy.append("PDI>MDI多头")
        if r["pdi"] < r["mdi"]: sell.append("PDI<MDI空头")
        if (r["volume_ratio"] or 0) > 2: buy.append("放量>%0.1f" % r["volume_ratio"])
        if r["supertrend"] and r["close"] > r["supertrend"]: buy.append("股价在Supertrend上")
        if r["supertrend"] and r["close"] < r["supertrend"]: sell.append("股价在Supertrend下")
        if r["cci"] is not None and r["cci"] < -100: buy.append("CCI超卖<%0.0f" % r["cci"])
        if r["cci"] is not None and r["cci"] > 100: sell.append("CCI超买>%0.0f" % r["cci"])
    return buy, sell


def run(con=None, mode: str = "daily", limit: int = None, asof: str = None, window: int = WINDOW):
    """mode: daily=仅 upsert 最新一日（日更用）；backfill=upsert 整个 window（回填/历史用）。"""
    own = con is None
    if con is None:
        from viewer.db import Database
        con = Database().conn
    ensure_tables(con)
    t0 = time.time()
    # K 线在独立库 data/kline_daily.db，读连接单独开；落库走 con(app.db)
    kline_path = os.path.join(ROOT, "data", "kline_daily.db")
    kcon = sqlite3.connect(kline_path)
    # 名称字典
    names = {}
    for code, name in con.execute("SELECT code,name FROM stock_names WHERE name IS NOT NULL"):
        names[code] = name
    # 干净宇宙（排除 ST/无K线/极弱流动性），与选股口径一致
    #
    # 2026-10-31 整改（测评 P0-B，放行表第 4 行）：
    # 原写法是 `try: … get_exclusion_pool() … except Exception: excl = set()`。
    # **一次读失败 = 空池 = 一只都不剔 = ST 可进指标表并一路进候选名单**，
    # 而且不写日志、不中断、不告警 —— 界面上看起来一切正常。
    # 现在改为 fail-closed：池不可用就本轮失败退出（ok=False），由调度记 FAILED 并退避重试。
    # 停一轮是**可见**的（日志有 FAILED、顶栏有红），静默放行是**不可见**的。
    # 判据：宁可让数据晚一天，也不让 ST 提前进名单。
    from viewer.universe_filter import get_exclusion_pool_strict, ExclusionPoolUnavailable
    try:
        excl, _, _ = get_exclusion_pool_strict()
    except ExclusionPoolUnavailable as _e:
        _msg = "剔除池不可用，按 fail-closed 停止本轮指标计算：%s" % _e
        print("[indicators][FATAL] " + _msg, flush=True)
        logging.getLogger(__name__).error("[indicators] " + _msg)
        try:
            kcon.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "exclusion_pool_unavailable", "detail": str(_e)[:300]}
    # 剔除池内的票：不仅跳过计算，还要清掉它们在三张表里的陈旧行
    # （否则 backfill 改写后，上一轮写进的 ST/博主噪声旧行会残留，造成"剔除没生效"的假象）
    if excl:
        ph = ",".join("?" * len(excl))
        excl_list = list(excl)
        for t in ("stock_indicators", "stock_signals", "stock_indicators_daily"):
            con.execute("DELETE FROM %s WHERE code IN (%s)" % (t, ph), excl_list)
    # 读 K 线（最近 window+slack 天，覆盖每只票最近 window 日）
    maxd_row = kcon.execute("SELECT MAX(d) FROM kline").fetchone()
    if not maxd_row or not maxd_row[0]:
        print("[indicators] kline 为空，跳过", flush=True)
        kcon.close()
        return {"ok": False, "error": "kline empty"}
    maxd = maxd_row[0]
    # 用真实日期偏移算 min_d（YYYYMMDD 整数直接减会算出非法日期）
    from datetime import datetime, timedelta
    _ms = str(maxd)
    _dt = datetime(int(_ms[:4]), int(_ms[4:6]), int(_ms[6:8]))
    min_d = int((_dt - timedelta(days=KLINE_LOOKBACK_DAYS + 10)).strftime("%Y%m%d"))
    rows = kcon.execute(
        "SELECT code,d,o,c,h,l,v FROM kline WHERE d>=? ORDER BY code, d", (min_d,)).fetchall()
    if not rows:
        return {"ok": False, "error": "no kline rows"}
    df = pd.DataFrame(rows, columns=["code", "d", "open", "close", "high", "low", "volume"])
    df["date"] = df["d"].astype(str).str.zfill(8).map(
        lambda s: "%s-%s-%s" % (s[0:4], s[4:6], s[6:8]))

    codes = sorted(df["code"].unique())
    if limit:
        codes = codes[:limit]
    daily_rows, snap_rows, sig_rows = [], [], []
    n_done = 0
    for code in codes:
        if code in excl:
            continue
        nm = names.get(code, code)
        # 实时兜底（2026-10-30）：剔除池 exclusion_pool 是 20:00 重建的，而因子 15:30 就跑，
        # 所以因子用的永远是**昨天的**剔除池 —— 当天新戴帽的 ST 会漏进来，于是
        # stock_indicators 长期残留 ST 行（smoke 的「因子表无 ST/退 残留」一直 FAIL）。
        # 名称就在手上，直接判，不依赖任何表的时序。
        if "ST" in nm or "退" in nm:
            continue
        g = df[df["code"] == code].sort_values("date")
        if len(g) < 60:       # 少于 60 交易日，因子不可信，跳过
            continue
        res = compute_indicators(g)
        last = res.iloc[-1]
        # 历史 upsert（backfill 全量；daily 仅最新一行）
        if mode == "backfill":
            subset = res.iloc[-window:]   # 仅落库最近 window 日；因子由全长序列计算，快照仍用最新一行
        else:
            subset = res.iloc[[-1]]
        for _, r in subset.iterrows():
            rec = [r["date"], code, nm] + [None if (isinstance(r[c], float) and (math.isnan(r[c]) or math.isinf(r[c]))) else r[c] for c in IND_COLS]
            daily_rows.append(tuple(rec))
        # 快照（最新一日）
        snap = [code, nm, last["date"]] + [None if (isinstance(last[c], float) and (math.isnan(last[c]) or math.isinf(last[c]))) else last[c] for c in IND_COLS]
        snap_rows.append(tuple(snap))
        # 信号
        buy, sell = _derive_signals({c: (None if (isinstance(last[c], float) and (math.isnan(last[c]) or math.isinf(last[c]))) else last[c]) for c in IND_COLS})
        sig_rows.append((code, nm, last["date"], len(buy), len(sell),
                         json.dumps(buy, ensure_ascii=False), json.dumps(sell, ensure_ascii=False)))
        n_done += 1

    if daily_rows:
        con.executemany(
            "INSERT OR REPLACE INTO stock_indicators_daily(date,code,name,%s) VALUES (%s)"
            % (",".join(IND_COLS), ",".join(["?"] * (len(IND_COLS) + 3))), daily_rows)
    if snap_rows:
        con.executemany(
            "INSERT OR REPLACE INTO stock_indicators(code,name,date,%s) VALUES (%s)"
            % (",".join(IND_COLS), ",".join(["?"] * (len(IND_COLS) + 3))), snap_rows)
    if sig_rows:
        con.executemany(
            "INSERT OR REPLACE INTO stock_signals(code,name,date,buy_score,sell_score,buy_reasons,sell_reasons) "
            "VALUES (?,?,?,?,?,?,?)", sig_rows)
    con.commit()
    kcon.close()
    if own:
        con.close()
    print("[indicators] mode=%s codes=%d daily=%d snap=%d sig=%d %.1fs"
          % (mode, n_done, len(daily_rows), len(snap_rows), len(sig_rows), time.time() - t0), flush=True)
    return {"ok": True, "mode": mode, "codes": n_done,
            "daily_rows": len(daily_rows), "snap": len(snap_rows), "signals": len(sig_rows),
            "secs": round(time.time() - t0, 1)}


def main():
    args = sys.argv[1:]
    mode = "daily"
    limit = None
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    run(mode=mode, limit=limit)


if __name__ == "__main__":
    main()
