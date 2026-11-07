# -*- coding: utf-8 -*-
"""完整条件化矩阵：全历史 × 全工具 × 全持有期 × 进场时点 × 行情状态 —— 2026-11-05

## 为什么要有这一支（上一版错在哪）

`cond_engine.py` 测的是「工具 × 我拍脑袋定的 2~4 个持有期 × 触发当天收盘进场」。
两个致命缺口：

1. **持有期谱太窄**。只测了 1/3/5/10/20/60，没有 40/90/180/250。
   而「这个策略适合短/中/长」这件事，恰恰要求把整条谱铺开才看得出来。
2. **把「触发」当成了「买点」**。触发当天收盘买，是所有进场时点里最差的一个 ——
   A 股 1~3 日有明确的反转效应，追着信号进场必然吃这一段。
   「什么时候开始关注」（触发）和「什么时候进」（买点）是两件事，之前只测了前者。

## 这一版测什么

  工具（15）× 进场时点（0/1/3 日）× 持有期（9 档：1~120）× 行情状态（上涨/震荡/下跌）
  ≈ 405 个单元，每个单元再切 3 种行情。

持有期谱：1, 2, 3, 5, 10, 20, 40, 60, 120 个交易日。

进场时点（关键新维度）：
  e0 = 触发当天收盘买（旧口径，最差）
  e1 = 次一交易日收盘买
  e3 = 触发后第 3 个交易日收盘买
  若 e1/e3 把负值抹平或翻正 → 说明问题在**进场时机**，不在工具方向。
  这直接决定产品该做「信号提醒」还是「买点提醒」。

行情状态：用全市场等权指数的**滚动 120 日年化收益**给每一天贴标签
  > +15%/年 → 上涨 ｜ −15%~+15% → 震荡 ｜ < −15%/年 → 下跌

## 算得动吗

贵的部分是每只票算一次指标（约 200 秒），所有工具、所有单元共用。
单元数增加几乎不增加时间——这就是为什么"全谱矩阵"和"只测 5 天"的成本几乎一样。
之前只测 5 天，不是算不动，是我没想到要铺开。

## 用法
  python tools/cond_matrix.py --limit 300          # 计时
  python tools/cond_matrix.py                      # 全量
  python tools/cond_matrix.py --min-edge 0.3       # 只列 |边缘|≥0.3pp 的单元
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sqlite3
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 注意：不能用 io.TextIOWrapper 包一层再被覆盖 —— 被替换掉的旧 wrapper 释放时会关掉
# 底层 buffer，后面所有 print 都报 "I/O operation on closed file"。
# 导入 cond_engine 时它也会做同样的包装，所以这里用 reconfigure 就地改编码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # noqa: BLE001
    pass

from viewer.indicators import compute_indicators  # noqa: E402
from tools.cond_engine import TOOLS, limit_thr  # noqa: E402
try:
    from viewer.playbook import enrich_X as _pb_enrich
    _PB = True
except Exception:      # noqa: BLE001
    _PB = False

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
OUT = ROOT / "viewer" / "logs"
EW = OUT / "ew_index.json"

DATA_START = 20100101
COST = 0.0025
MIN_AMOUNT = 5.0e6
MIN_BARS = 260

HOLDS = (1, 2, 3, 5, 10, 20, 40, 60, 120)
ENTRIES = (0, 1, 3)


def load_regime(n_dates: int, dates_all: np.ndarray) -> np.ndarray:
    """给每个交易日贴行情标签：上涨 / 震荡 / 下跌（滚动 120 日年化）。"""
    d = json.loads(EW.read_text(encoding="utf-8"))
    ew_d = [int(x) for x in d["dates"]]
    ew_i = np.array(d["index"], dtype=np.float64)
    p = {x: i for i, x in enumerate(ew_d)}
    out = np.array(["未知"] * n_dates, dtype=object)
    for k, dd in enumerate(dates_all):
        i = p.get(int(dd))
        if i is None or i < 120 or ew_i[i - 120] <= 0:
            continue
        gross = ew_i[i] / ew_i[i - 120] - 1.0
        ann = ((1.0 + gross) ** (252.0 / 120.0) - 1.0) * 100.0
        out[k] = "上涨" if ann > 15.0 else ("下跌" if ann < -15.0 else "震荡")
    return out


def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan"), float("nan")
    m = x.mean()
    d = x - m
    var = float((d ** 2).sum()) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float((d[k:] * d[:-k]).sum()) / n
    if var <= 0:
        return m, float("nan")
    return m, m / math.sqrt(var / n)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-edge", type=float, default=0.25)
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--by-stock", action="store_true",
                    help="额外按每只票累计（A4 单票适配度用）")
    args = ap.parse_args()

    con = sqlite3.connect(str(QFQ))
    dates_all = np.array([int(r[0]) for r in con.execute(
        "SELECT DISTINCT d FROM kline WHERE d>=? ORDER BY d", (DATA_START,))], dtype=np.int64)
    n_dates = len(dates_all)
    dpos = {int(d): i for i, d in enumerate(dates_all)}
    regime = load_regime(n_dates, dates_all)
    print("交易日 %d 个（%d ~ %d）；行情标签：上涨 %d / 震荡 %d / 下跌 %d / 未知 %d"
          % (n_dates, dates_all[0], dates_all[-1],
             int((regime == "上涨").sum()), int((regime == "震荡").sum()),
             int((regime == "下跌").sum()), int((regime == "未知").sum())))

    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d>=? AND d<=20261104", (DATA_START,))]
    if args.limit:
        codes = codes[:args.limit]
    print("候选票 %d 只；单元数 = %d 工具 × %d 进场时点 × %d 持有期 = %d"
          % (len(codes), len(TOOLS), len(ENTRIES), len(HOLDS),
             len(TOOLS) * len(ENTRIES) * len(HOLDS)))

    combos = [(e, H) for e in ENTRIES for H in HOLDS]
    pool = {c: (np.zeros(n_dates), np.zeros(n_dates)) for c in combos}
    acc = {(k, e, H): (np.zeros(n_dates), np.zeros(n_dates))
           for k in TOOLS for e in ENTRIES for H in HOLDS}

    # A4 单票适配度：每只票在「它自己历史上触发 vs 不触发」的差。
    # 每只工具只取一个（进场, 持有），默认用上一轮定档的峰值格，取不到就退到 (0,20)。
    bst: dict[str, tuple[int, int]] = {}
    sacc: dict[str, list[np.ndarray]] = {}
    if args.by_stock:
        prior = OUT / ("tier_assign_%s.json" % args.stamp)
        pm = {}
        if prior.exists():
            for r in json.loads(prior.read_text(encoding="utf-8"))["tools"]:
                if r.get("peak_hold"):
                    pm[r["tool"]] = (int(r["entry"]), int(r["peak_hold"]))
        nc = len(codes)
        sidx = {c: i for i, c in enumerate(codes)}
        for k in TOOLS:
            bst[k] = pm.get(k, (0, 20))
            # 8 段：前半（触发 sum/cnt、非触发 sum/cnt）+ 后半同上。
            # 主要是为了做「前半段的单票历史能不能预测后半段」这个检验——
            # 只存总数看不出这一点，而这一点恰恰决定单票适配度能不能做产品。
            sacc[k] = [np.zeros(nc) for _ in range(8)]
        mid = n_dates // 2
        print("单票累计已开启：%d 个工具" % len(bst))

    t0 = time.time()
    done = 0
    for code in codes:
        si = sidx[code] if args.by_stock else -1
        q = con.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND d<=20261104 ORDER BY d",
            (code, DATA_START)).fetchall()
        if len(q) < MIN_BARS + 2:
            continue
        df = pd.DataFrame(q, columns=["d", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
        df["code"] = code
        try:
            ind = compute_indicators(df)
        except Exception:      # noqa: BLE001
            continue

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
            atr_pct = ind["atr"].to_numpy(np.float64) / c * 100.0
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
        X["rand5"] = np.random.default_rng(
            zlib.crc32(code.encode("utf-8")) % (2 ** 32)).random(n)
        if _PB:
            _pb_enrich(X, df, ind)
            X["_limit_thr"] = limit_thr(code)

        amt = c * v
        thr = limit_thr(code)
        lim = ret1 >= thr - 0.2          # 当日涨停收盘 → 收盘价买不到
        # 触发日的可交易性：不再把「涨停」写死在这里。
        # 原来的写法把 lim 并进 base，而 base 是**触发日**的过滤，结果「首板涨停 / 二连板」
        # 这两条判据的触发日必然涨停 → mask & base 恒为空 → 整条工具静默消失（0 格，不报错）。
        # 涨停该管的是**进场那天**能不能成交，不是触发那天。所以拆成两段。
        base = (np.isfinite(c) & (c > 0) & np.isfinite(v) & (v > 0)
                & (amt >= MIN_AMOUNT) & np.isfinite(X["ma200"]) & np.isfinite(ret1))
        if not base.any():
            continue
        gpos = np.array([dpos.get(int(d), -1) for d in df["d"].to_numpy()], dtype=np.int64)
        keep = gpos >= 0
        base = base & keep
        gpos = np.where(keep, gpos, 0)

        # enter_ok[e][i]：在 i 日触发、第 i+e 日进场，那一天的收盘价能不能真的买到
        enter_ok: dict[int, np.ndarray] = {}
        for e in ENTRIES:
            ok_e = base.copy()
            if e > 0:
                flim = np.ones(n, dtype=bool)     # 默认「可买」
                flim[:n - e] = ~lim[e:]
                flim[n - e:] = False              # 进场日越界
                ok_e &= flim
            else:
                ok_e &= ~lim                      # e0：当天涨停收盘买不到
            enter_ok[e] = ok_e

        # 各（进场时点, 持有期）的前向收益
        R: dict[tuple[int, int], np.ndarray] = {}
        for e in ENTRIES:
            for H in HOLDS:
                f = np.full(n, np.nan)
                if n > e + H:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        f[:n - e - H] = c[e + H:] / c[e:n - H] - 1.0 - COST
                R[(e, H)] = f
                ok = enter_ok[e] & np.isfinite(f)
                if ok.any():
                    pb = pool[(e, H)]
                    pb[0][:] += np.bincount(gpos[ok], weights=f[ok], minlength=n_dates)
                    pb[1][:] += np.bincount(gpos[ok], minlength=n_dates)

        for k, spec in TOOLS.items():
            mask = spec["fn"](X)
            if not mask.any():
                continue
            for e in ENTRIES:
                for H in HOLDS:
                    f = R[(e, H)]
                    ok = mask & enter_ok[e] & np.isfinite(f)
                    if not ok.any():
                        continue
                    if args.by_stock and bst[k] == (e, H):
                        nz = enter_ok[e] & np.isfinite(f)
                        tr = mask & nz
                        no = nz & ~mask
                        p1 = gpos < mid
                        ar = sacc[k]
                        ar[0][si] += float(f[tr & p1].sum())
                        ar[1][si] += float((tr & p1).sum())
                        ar[2][si] += float(f[no & p1].sum())
                        ar[3][si] += float((no & p1).sum())
                        ar[4][si] += float(f[tr & ~p1].sum())
                        ar[5][si] += float((tr & ~p1).sum())
                        ar[6][si] += float(f[no & ~p1].sum())
                        ar[7][si] += float((no & ~p1).sum())
                    a = acc[(k, e, H)]
                    a[0][:] += np.bincount(gpos[ok], weights=f[ok], minlength=n_dates)
                    a[1][:] += np.bincount(gpos[ok], minlength=n_dates)

        done += 1
        if done % 400 == 0:
            print("  已扫 %d 只（%.0fs）" % (done, time.time() - t0))
    con.close()
    print("扫完 %d 只，用时 %.0fs" % (done, time.time() - t0))

    # ---------------- 汇总 ----------------
    cells: list[dict] = []
    for k in TOOLS:
        for e in ENTRIES:
            for H in HOLDS:
                st, ct = acc[(k, e, H)]
                sp, cp = pool[(e, H)]
                with np.errstate(divide="ignore", invalid="ignore"):
                    m_t = np.where(ct > 0, st / np.maximum(ct, 1), np.nan)
                    m_u = np.where(cp - ct > 0, (sp - st) / np.maximum(cp - ct, 1), np.nan)
                diff = m_t - m_u
                base_ok = np.isfinite(diff) & (ct >= 5) & (cp - ct >= 5)
                for rg in ("全部", "上涨", "震荡", "下跌"):
                    sel = base_ok & ((regime == rg) if rg != "全部" else True)
                    if sel.sum() < 40:
                        continue
                    dd = diff[sel]
                    m1, t1 = nw_t(dd, max(1, H // 5))
                    bm, bn, bt = block_t(dd, H)
                    # 多重比较防线：3240 个格子里挑最强者必然挑出大数。
                    # 时间对半分，前后半段各自算，同号才算这个格子"不是拟合出来的"。
                    h = len(dd) // 2
                    h1 = float(np.nanmean(dd[:h])) * 100 if h >= 20 else float("nan")
                    h2 = float(np.nanmean(dd[h:])) * 100 if len(dd) - h >= 20 else float("nan")
                    consistent = (np.isfinite(h1) and np.isfinite(h2)
                                  and np.sign(h1) == np.sign(h2) and np.sign(m1) == np.sign(h1))
                    cells.append({
                        "tool": k, "entry": e, "hold": H, "regime": rg,
                        "grp": TOOLS[k]["grp"], "n_days": int(sel.sum()),
                        "diff_pp": round(m1 * 100, 3),
                        "nw_t": round(t1, 2),
                        "block_n": int(bn),
                        "block_t": None if not np.isfinite(bt) else round(bt, 2),
                        "hit_pct": round(float((dd > 0).mean()) * 100, 1),
                        "avg_trig": round(float(ct[sel].mean()), 1),
                        "half1_pp": None if not np.isfinite(h1) else round(h1, 3),
                        "half2_pp": None if not np.isfinite(h2) else round(h2, 3),
                        "consistent": bool(consistent),
                    })

    path = OUT / ("cond_matrix_%s.json" % args.stamp)
    path.write_text(json.dumps({"meta": {"holds": list(HOLDS), "entries": list(ENTRIES),
                                         "n_stocks": done, "cost": COST,
                                         "min_amount": MIN_AMOUNT,
                                         "n_cells": len(cells)},
                                "cells": cells}, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    if args.by_stock:
        sp = OUT / ("by_stock_%s.npz" % args.stamp)
        np.savez_compressed(
            sp, codes=np.array(codes),
            **{"%s|%d|%d" % (k, bst[k][0], bst[k][1]): np.vstack(sacc[k])
               for k in TOOLS})
        print("单票累计写盘：%s" % sp)

    # 每个工具取「|边缘| 最大且同号稳定」的一格，作为它的适用场景
    print("\n== 每个工具的最强单元格（|边缘| 最大，非重叠 t 有值）==")
    print("%-22s %-4s %-5s %-5s %7s %9s %8s %8s %8s"
          % ("工具", "进场", "持有", "行情", "有效日", "边缘pp", "前半段", "后半段", "同号"))
    for k in TOOLS:
        cand = [c for c in cells if c["tool"] == k and c["block_t"] is not None]
        if not cand:
            continue
        best = max(cand, key=lambda c: abs(c["diff_pp"]))
        print("%-22s %-4s %-5s %-5s %7d %+9.3f %+8s %+8s %8s"
              % (best["tool"], "e%d" % best["entry"], "T+%d" % best["hold"],
                 best["regime"], best["n_days"], best["diff_pp"],
                 "n/a" if best["half1_pp"] is None else "%.3f" % best["half1_pp"],
                 "n/a" if best["half2_pp"] is None else "%.3f" % best["half2_pp"],
                 "是" if best["consistent"] else "否"))

    print("\n== 稳定正向单元格（边缘 ≥%.2fpp、非重叠 t ≥ 2、前后半段同号）==" % args.min_edge)
    good = [c for c in cells if c["diff_pp"] >= args.min_edge
            and c["block_t"] is not None and c["block_t"] >= 2.0
            and c["consistent"]]
    if not good:
        print("  无")
    for c in sorted(good, key=lambda x: -x["diff_pp"])[:40]:
        print("  %-22s e%d T+%-3d %-4s 边缘 %+6.3fpp t %+5.2f 前半 %+6.3f 后半 %+6.3f 有效日 %d 胜率 %.1f%%"
              % (c["tool"], c["entry"], c["hold"], c["regime"], c["diff_pp"],
                 c["block_t"], c["half1_pp"] or 0, c["half2_pp"] or 0,
                 c["n_days"], c["hit_pct"]))

    print("\n== 进场时点的作用（同一工具同一持有期，e0 vs e1 vs e3）==")
    print("%-22s %-5s %10s %10s %10s" % ("工具", "持有", "e0 触发日", "e1 次日", "e3 第三天"))
    for k in TOOLS:
        for H in (3, 5, 10, 20):
            v = {}
            for e in ENTRIES:
                c = [x for x in cells if x["tool"] == k and x["entry"] == e
                     and x["hold"] == H and x["regime"] == "全部"]
                if c:
                    v[e] = c[0]["diff_pp"]
            if len(v) >= 2:
                print("%-22s T+%-3d %+10.3f %+10.3f %+10.3f"
                      % (k, H, v.get(0, float("nan")), v.get(1, float("nan")),
                         v.get(3, float("nan"))))

    print("\n写盘：%s（%d 个单元格）" % (path, len(cells)))


if __name__ == "__main__":
    main()
