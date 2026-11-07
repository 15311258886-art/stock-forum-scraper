"""行业中性对照：把「同池同日」的对照组换成「同池·同日·同行业」，看反向结论会不会翻。

起因：现有所有反向结论用的是「同一天池子里没触发的票」做对照。如果追涨类判据
（放量突破 / 量比 / 连板 / 金叉）天然扎堆在少数高波动行业，那它跑输的可能是**行业**，
不是判据本身。换成同行业对照，行业这一层就被消掉了。

做法（两趟，因为行业均值必须先算完才能减）：
  第 1 趟：对每个 (进场, 持有)，累加 每个(行业, 日期) 的前向收益和与样本数 → 行业基准
  第 2 趟：每笔触发减去当日同行业均值，再按日等权累计 → 行业中性超额

输出：每个工具在「池中性」和「行业中性」两个口径下的同一格对比。
池中性的数字直接从 cond_matrix_<stamp>.json 读，保证两套口径是同一批数据。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
import zlib

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from viewer.indicators import compute_indicators  # noqa: E402
from tools.cond_engine import TOOLS, limit_thr  # noqa: E402

try:                       # 判据库（pb_*）需要的字段，缺了对应判据会返回全 False
    from viewer.playbook import enrich_X as _pb_enrich
    _PB = True
except Exception:          # noqa: BLE001
    _PB = False

ROOT = BASE
QFQ = os.path.join(ROOT, "data", "kline_daily.db")
OUT = os.path.join(ROOT, "viewer", "logs")
IND_MAP = os.path.join(OUT, "industry_map.json")

DATA_START = 20100101
COST = 0.0025
MIN_AMOUNT = 5.0e6
MIN_BARS = 260
ENTRIES = (0, 1, 3)
HOLDS = (1, 3, 5, 20, 60, 120)
END = 20261104


def block_t(x: np.ndarray, stride: int) -> tuple[float, int, float]:
    s = x[::max(1, stride)]
    s = s[np.isfinite(s)]
    n = len(s)
    if n < 4:
        return float("nan"), n, float("nan")
    m = float(s.mean())
    sd = float(s.std(ddof=1))
    if sd <= 0:
        return m, n, float("nan")
    return m, n, m / (sd / math.sqrt(n))


def load_stock(con, code):
    q = con.execute(
        "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND d<=? ORDER BY d",
        (code, DATA_START, END)).fetchall()
    if len(q) < MIN_BARS + 2:
        return None
    df = pd.DataFrame(q, columns=["d", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
    df["code"] = code
    try:
        ind = compute_indicators(df)
    except Exception:  # noqa: BLE001
        return None
    return df, ind


def build_X(df, ind):
    c = df["close"].to_numpy(np.float64)
    v = df["volume"].to_numpy(np.float64)
    n = len(c)
    prev = np.concatenate(([np.nan], c[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        ret1 = (c / prev - 1.0) * 100.0
        mom20 = (c / np.concatenate((np.full(20, np.nan), c[:-20])) - 1.0) * 100.0
        high250 = pd.Series(c).rolling(251).max().to_numpy(np.float64)
        dist_high250 = (c / high250 - 1.0) * 100.0
        ma20 = ind["ma20"].to_numpy(np.float64)
        ma20_dev = (c / ma20 - 1.0) * 100.0
        tr = np.maximum(df["high"].to_numpy(np.float64) - df["low"].to_numpy(np.float64),
                        np.maximum(np.abs(df["high"].to_numpy(np.float64) - prev),
                                   np.abs(df["low"].to_numpy(np.float64) - prev)))
        atr14 = pd.Series(tr).rolling(14).mean().to_numpy(np.float64)
        atr_pct = atr14 / c * 100.0
    X = dict(close=c, volume=v, ret1=ret1, mom20=mom20,
             dist_high250=dist_high250, ma20_dev=ma20_dev, atr_pct=atr_pct,
             ma5=ind["ma5"].to_numpy(np.float64), ma20=ma20,
             ma60=ind["ma60"].to_numpy(np.float64),
             ma200=ind["ma200"].to_numpy(np.float64),
             macd=ind["macd"].to_numpy(np.float64),
             rsi6=ind["rsi6"].to_numpy(np.float64),
             wr6=ind["wr6"].to_numpy(np.float64),
             cci=ind["cci"].to_numpy(np.float64),
             volume_ratio=ind["volume_ratio"].to_numpy(np.float64))
    return X, c, v, n, ret1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    imap = json.load(open(IND_MAP, encoding="utf-8"))["map"]
    con = sqlite3.connect(QFQ)
    dates_all = np.array([int(r[0]) for r in con.execute(
        "SELECT DISTINCT d FROM kline WHERE d>=? AND d<=? ORDER BY d",
        (DATA_START, END))], dtype=np.int64)
    n_dates = len(dates_all)
    dpos = {int(d): i for i, d in enumerate(dates_all)}

    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d>=? AND d<=?", (DATA_START, END))]
    if args.limit:
        codes = codes[:args.limit]

    inds = sorted({v for v in imap.values()})
    gidx = {v: i for i, v in enumerate(inds)}
    NG = len(inds) + 1          # 最后一格给「行业未知」
    gmap = {c: gidx.get(imap.get(c, ""), len(inds)) for c in codes}

    print("交易日 %d；票 %d；行业 %d（含未知格）" % (n_dates, len(codes), NG))

    combos = [(e, H) for e in ENTRIES for H in HOLDS]
    gsum = {k: np.zeros((NG, n_dates), dtype=np.float64) for k in combos}
    gcnt = {k: np.zeros((NG, n_dates), dtype=np.float64) for k in combos}

    t0 = time.time()

    # ---------- 第 1 趟：行业基准 ----------
    for i, code in enumerate(codes):
        got = load_stock(con, code)
        if not got:
            continue
        df, ind = got
        X, c, v, n, ret1 = build_X(df, ind)
        amt = c * v
        thr = limit_thr(code)
        lim = ret1 >= thr - 0.2
        base = (np.isfinite(c) & (c > 0) & np.isfinite(v) & (v > 0)
                & (amt >= MIN_AMOUNT) & np.isfinite(X["ma200"]) & np.isfinite(ret1))
        if not base.any():
            continue
        gp = np.array([dpos.get(int(d), -1) for d in df["d"].to_numpy()], dtype=np.int64)
        keep = gp >= 0
        base &= keep
        gp = np.where(keep, gp, 0)
        g = gmap.get(code, NG - 1)
        for e in ENTRIES:
            ok_e = base.copy()
            if e > 0:
                fl = np.ones(n, dtype=bool)
                fl[:n - e] = ~lim[e:]
                fl[n - e:] = False
                ok_e &= fl
            else:
                ok_e &= ~lim
            for H in HOLDS:
                f = np.full(n, np.nan)
                if n > e + H:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        f[:n - e - H] = c[e + H:] / c[e:n - H] - 1.0 - COST
                ok = ok_e & np.isfinite(f)
                if not ok.any():
                    continue
                k = (e, H)
                gsum[k][g] += np.bincount(gp[ok], weights=f[ok], minlength=n_dates)
                gcnt[k][g] += np.bincount(gp[ok], minlength=n_dates)
        if (i + 1) % 1000 == 0:
            print("  第1趟 %d/%d  %.0fs" % (i + 1, len(codes), time.time() - t0))

    gmean = {}
    for k in combos:
        with np.errstate(divide="ignore", invalid="ignore"):
            m = gsum[k] / gcnt[k]
        gmean[k] = m          # (NG, n_dates)，NaN 表示当日该行业无样本
    print("第 1 趟完成 %.0fs" % (time.time() - t0))

    # ---------- 第 2 趟：逐工具减行业均值 ----------
    acc = {(kk, e, H): [np.zeros(n_dates), np.zeros(n_dates)]
           for kk in TOOLS for e in ENTRIES for H in HOLDS}
    for i, code in enumerate(codes):
        got = load_stock(con, code)
        if not got:
            continue
        df, ind = got
        X, c, v, n, ret1 = build_X(df, ind)
        X["rand5"] = np.random.default_rng(
            zlib.crc32(code.encode("utf-8")) % (2 ** 32)).random(n)
        amt = c * v
        thr = limit_thr(code)
        if _PB:
            _pb_enrich(X, df, ind)
            X["_limit_thr"] = thr
        lim = ret1 >= thr - 0.2
        base = (np.isfinite(c) & (c > 0) & np.isfinite(v) & (v > 0)
                & (amt >= MIN_AMOUNT) & np.isfinite(X["ma200"]) & np.isfinite(ret1))
        if not base.any():
            continue
        gp = np.array([dpos.get(int(d), -1) for d in df["d"].to_numpy()], dtype=np.int64)
        keep = gp >= 0
        base &= keep
        gp = np.where(keep, gp, 0)
        g = gmap.get(code, NG - 1)
        masks = {}
        for kk, spec in TOOLS.items():
            m = spec["fn"](X)
            if m.any():
                masks[kk] = m
        for e in ENTRIES:
            ok_e = base.copy()
            if e > 0:
                fl = np.ones(n, dtype=bool)
                fl[:n - e] = ~lim[e:]
                fl[n - e:] = False
                ok_e &= fl
            else:
                ok_e &= ~lim
            for H in HOLDS:
                f = np.full(n, np.nan)
                if n > e + H:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        f[:n - e - H] = c[e + H:] / c[e:n - H] - 1.0 - COST
                if not np.isfinite(f).any():
                    continue
                # 留一法：行业均值里必须先扣掉这只票自己，否则「触发组自己污染基准」。
                # 行业规模中位数只有 26 只（70 个行业不足 30 只），一条判据常能触发掉一个行业里的
                # 三四成，若把自身算进去，超额会被系统性压向 0 —— 那会把「判据反向」误读成
                # 「行业暴露造成的」。全市场池子有 4000 只，这个问题在池中性口径下可忽略，
                # 在行业中性口径下不可忽略。
                gs = gsum[(e, H)][g][gp].copy()
                gc = gcnt[(e, H)][g][gp].copy()
                fin = np.isfinite(f) & ok_e
                gs = gs - np.where(fin, np.nan_to_num(f), 0.0)
                gc = gc - fin.astype(np.float64)
                with np.errstate(divide="ignore", invalid="ignore"):
                    bm = np.where(gc > 0, gs / gc, np.nan)
                ex = f - bm
                for kk, m in masks.items():
                    ok = m & ok_e & np.isfinite(f) & np.isfinite(ex)
                    if not ok.any():
                        continue
                    a = acc[(kk, e, H)]
                    a[0][:] += np.bincount(gp[ok], weights=ex[ok], minlength=n_dates)
                    a[1][:] += np.bincount(gp[ok], minlength=n_dates)
        if (i + 1) % 1000 == 0:
            print("  第2趟 %d/%d  %.0fs" % (i + 1, len(codes), time.time() - t0))

    con.close()
    print("第 2 趟完成 %.0fs" % (time.time() - t0))

    # ---------- 输出对比 ----------
    try:
        old = json.load(open(os.path.join(OUT, "cond_matrix_%s.json" % args.stamp),
                            encoding="utf-8"))["cells"]
    except FileNotFoundError:
        old = []
    oldm = {(c["tool"], c["entry"], c["hold"], c["regime"]): c for c in old}

    cells = []
    for (kk, e, H), (sm, ct) in acc.items():
        with np.errstate(divide="ignore", invalid="ignore"):
            daily = np.where(ct > 0, sm / np.maximum(ct, 1), np.nan) * 100.0
        m, nn, t = block_t(daily, H)
        cells.append(dict(tool=kk, entry=e, hold=H, n_days=nn,
                          diff_pp=None if not np.isfinite(m) else round(m, 3),
                          block_t=None if not np.isfinite(t) else round(t, 2),
                          daily=daily))
    out = os.path.join(OUT, "ind_neutral_%s.json" % args.stamp)
    json.dump(dict(meta=dict(n_stocks=len(codes), n_industry=NG, cost=COST,
                             note="diff_pp = 触发收益 − 当日同行业等权收益，日度等权后再平均"),
                   cells=[{k2: v for k2, v in c.items() if k2 != "daily"} for c in cells]),
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n写出 %s（%d 格）" % (out, len(cells)))

    if not old:
        print("（没有 cond_matrix_%s.json，跳过两口径对比）" % args.stamp)
        return

    print("\n" + "=" * 108)
    print("池中性 vs 行业中性：同一格的两个口径（这是本次要回答的唯一问题）")
    print("=" * 108)
    print("%-24s %-6s %-4s %10s %10s %8s %s"
          % ("工具", "行情", "格", "池中性pp", "行业中性pp", "变化", "结论"))
    by = {(c["tool"], c["entry"], c["hold"]): c for c in cells}
    for tool in sorted({c["tool"] for c in old}):
        cand = [c for c in old if c["tool"] == tool and c.get("consistent")
                and abs(c.get("block_t") or 0) >= 2
                and abs(c.get("diff_pp") or 0) >= 0.25]
        if not cand:
            continue
        best = max(cand, key=lambda c: abs(c["diff_pp"]))
        n = by.get((tool, best["entry"], best["hold"]))
        if not n or n["diff_pp"] is None:
            continue
        o, nn2 = best["diff_pp"], n["diff_pp"]
        same = (o > 0) == (nn2 > 0)
        keep = "不变" if same else "**翻号**"
        print("%-24s %-6s e%d/T+%-3d %+10.3f %+11.3f %+8.3f %s"
              % (tool, best["regime"], best["entry"], best["hold"], o, nn2,
                 nn2 - o, keep))
    print("\n注：这里只对比「池中性口径下它最强的那一格」，不重新挑行业中性的最强者——")
    print("    重新挑等于再挑一次最大值，必然好看，那就失去对照的意义了。")


if __name__ == "__main__":
    main()
