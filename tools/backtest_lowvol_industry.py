# -*- coding: utf-8 -*-
"""低位池 / 低波池 行业上限回测（2026-10-30）

背景与口径
==========
项目里实际被称为"低波池"的，是 viewer/server.py 的 api_screener_lowbox（长期低位震荡池，
简称 lowbox）。其候选集 = 关注池 ∪ 板块一Top30（最多 80 只），按综合分排序后取前 limit（默认 30），
产出落到 /api/screener_lowbox + data/lowbox_scan_cache.json。该池是"按需计算"、依赖用户关注池，
无法在历史维度上逐日重放。因此本回测**隔离"行业上限"这一规则本身**在"低波动率选股"上的效果：

  · 用项目既有的 stock_indicators_daily（code,date,close + 52 因子，2010→2026 全市场约 5338 只）
    构造"按月再平衡的最低年化波动率前 N 只"池子（N=30，与 lowbox 默认池大小一致）；
  · 分别在【不加行业上限】与【加行业上限】下比较同一批再平衡日的：
        ① T+5 / T+20 前向超额收益（池子等权 − 同日全市场等权）
        ② 前向 20 日最大回撤（等权组合 NAV 路径）
        ③ 行业集中度（池子第一大一级行业占比、前三大累计占比）
  · 样本按"再平衡日"聚合（每个再平衡日 = 1 个观测），**不**按个股求均值，避免 t 值虚高。
  · 行业一级 = stock_org.industry_csrc 的"-"前段；缺失归"未知"组（同样受上限）。
  · 选股/截断逻辑直接复用生产代码 viewer/pool_cap.apply_industry_cap，保证回测结论与上线阈值口径一致。

数据库并发
==========
此时有公告全量回补进程在后台写 announcement 相关表（不碰 stock_indicators_daily / stock_org）。
本脚本以读为主；连接加 timeout=30 并带短重试，遇 database is locked 自动退避。

用法
====
  .venv/Scripts/python.exe tools/backtest_lowvol_industry.py              # 全量（按月再平衡，2011-2026）
  .venv/Scripts/python.exe tools/backtest_lowvol_industry.py --limit 60  # 池子 60 只
  .venv/Scripts/python.exe tools/backtest_lowvol_industry.py --fast      # 仅最近 5 年，快速验证
  .venv/Scripts/python.exe tools/backtest_lowvol_industry.py --start 2018
"""
import os
import sys
import time
import math
import argparse
import sqlite3

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import pool_cap as _pc  # 生产同款行业上限逻辑

DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
FWD = (5, 20)          # 前向窗口
VOL_WIN = 60           # 年化波动率回看交易日
ANN = 244.0


def _connect():
    """带超时 + 短重试的连接（应对后台公告回补的偶发 database is locked）。"""
    last = None
    for attempt in range(6):
        try:
            con = sqlite3.connect(DB_PATH, timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
            return con
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise last


def load_data(start_year: int):
    """流式读取 stock_indicators_daily → 价格矩阵，并加载行业一级映射。"""
    con = _connect()
    try:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM stock_indicators_daily ORDER BY code")]
        n = len(dates)
        m = len(codes)
        print("[load] 交易日 %d 个 | 股票 %d 只" % (n, m), flush=True)
        date_pos = {d: i for i, d in enumerate(dates)}
        code_pos = {c: i for i, c in enumerate(codes)}

        mat = np.full((n, m), np.nan, dtype=np.float64)
        t0 = time.time()
        cur = con.execute(
            "SELECT code, date, close FROM stock_indicators_daily WHERE close>0 ORDER BY code, date")
        done = 0
        for code, date, close in cur:
            mat[date_pos[date], code_pos[code]] = close
            done += 1
            if done % 2000000 == 0:
                print("[load] 已读 %d 行 (%.1fs)" % (done, time.time() - t0), flush=True)
        print("[load] 价格矩阵 %d x %d 装载完成 (%.1fs)" % (n, m, time.time() - t0), flush=True)

        # 行业一级映射
        indmap = {}
        try:
            for code, cs in con.execute("SELECT code, industry_csrc FROM stock_org"):
                indmap[code] = _pc.industry_level1(cs)
        except Exception as e:  # noqa: BLE001
            print("[load] 行业映射加载失败（全部归未知）: %s" % e, flush=True)
            indmap = {}
    finally:
        con.close()

    close_df = pd.DataFrame(mat, index=dates, columns=codes)
    # 日收益 + 滚动 60 日年化波动率
    mret = close_df.pct_change().fillna(np.nan).values  # (n, m)，首行 nan
    vol = pd.DataFrame(mret).rolling(VOL_WIN).std() * math.sqrt(ANN)
    vol_mat = vol.values
    mret = np.asarray(mret)
    return dates, codes, close_df, mret, vol_mat, indmap


def rebalance_dates(dates, start_year: int):
    """月末交易日（每月最后一个有数据的交易日）作为再平衡日。"""
    rb = []
    prev_ym = None
    for i, d in enumerate(dates):
        if d < "%04d-01-01" % start_year:
            continue
        ym = d[:7]
        if ym != prev_ym:
            if prev_ym is not None and rb:
                rb[-1] = i - 1  # 上一个月的月末 = 本月第一条前一条
            rb.append(i)
            prev_ym = ym
    # 修正：rb 里存的是每月"第一条"，月末应是它减一；首条特殊处理
    out = []
    for k in range(1, len(rb)):
        out.append(rb[k] - 1)
    if rb:
        out.append(len(dates) - 1)  # 最后一个月取最后一条
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="池子大小 N（默认 30，对齐 lowbox）")
    ap.add_argument("--start", type=int, default=2011, help="回测起始年（需 ≥2011 才有足够波动率样本）")
    ap.add_argument("--fast", action="store_true", help="仅最近 5 年，快速验证")
    ap.add_argument("--vol-win", type=int, default=VOL_WIN)
    args = ap.parse_args()
    start_year = max(2011, args.start)
    if args.fast:
        start_year = max(start_year, 2021)

    N = args.limit
    dates, codes, close_df, mret, vol_mat, indmap = load_data(start_year)
    n = len(dates)
    rb = rebalance_dates(dates, start_year)
    rb = [p for p in rb if p >= args.vol_win and p + max(FWD) < n]
    print("[bt] 再平衡日 %d 个（起始 %s，池子 N=%d）" % (len(rb), dates[rb[0]] if rb else "-", N), flush=True)

    # 候选阈值方案
    schemes = [
        ("不加上限", None, None),
        ("行业占比≤15%", None, 0.15),
        ("行业占比≤20%", None, 0.20),
        ("行业占比≤25%", None, 0.25),
        ("单行业≤5只", 5, None),
        ("单行业≤8只", 8, None),
        ("单行业≤10只", 10, None),
    ]

    # 累积容器
    agg = {name: {"ex5": [], "ex20": [], "dd": [], "t1": [], "t3": [], "size": []}
           for name, _, _ in schemes}
    nh = {h: "h%d" % h for h in FWD}

    t0 = time.time()
    for ti, p in enumerate(rb):
        cp = close_df.iloc[p].values.astype(float)
        vp = vol_mat[p]
        cph5 = close_df.iloc[p + 5].values.astype(float)
        cph20 = close_df.iloc[p + 20].values.astype(float)
        # eligible：p 有价、波动率可算、p+5/p+20 有价
        valid = np.isfinite(vp) & np.isfinite(cp) & (cp > 0) \
            & np.isfinite(cph5) & (cph5 > 0) & np.isfinite(cph20) & (cph20 > 0)
        if valid.sum() < N:
            continue
        idx = np.nonzero(valid)[0]
        order = idx[np.argsort(vp[idx])]          # 按年化波动率升序（越低越"低波"）
        # 市场等权基准（同日全市场等权）
        mkt5 = float(np.nanmean(cph5[valid] / cp[valid] - 1))
        mkt20 = float(np.nanmean(cph20[valid] / cp[valid] - 1))
        # 组合 20 日前向 NAV 路径所需日收益切片
        slice_ret = mret[p + 1:p + 1 + 20, :]      # (20, m)

        for name, per, pct in schemes:
            if per is None and pct is None:
                sel = order[:N]
            else:
                rows = [{"industry1": indmap.get(codes[j], "未知"), "_idx": j} for j in order]
                capped, _ = _pc.apply_industry_cap(
                    rows, N, max_per_industry=per, max_industry_pct=pct, industry_key="industry1")
                sel = np.array([r["_idx"] for r in capped], dtype=int) if capped else np.array([], dtype=int)
                if sel.size == 0:
                    sel = order[:N]
            scp = cp[sel]
            port5 = float(np.nanmean(cph5[sel] / scp - 1))
            port20 = float(np.nanmean(cph20[sel] / scp - 1))
            # 集中度
            from collections import Counter
            cnt = Counter(indmap.get(codes[j], "未知") for j in sel)
            t1, t3, _, sz = _pc.concentration(dict(cnt))
            # 最大回撤
            if sel.size > 0:
                sret = slice_ret[:, sel]                       # (20, k)
                pdaily = np.nanmean(sret, axis=1)             # (20,)
                nav = np.cumprod(1.0 + pdaily)
                peak = np.maximum.accumulate(nav)
                dd = float((nav / peak - 1).min())
            else:
                dd = 0.0
            a = agg[name]
            a["ex5"].append(port5 - mkt5)
            a["ex20"].append(port20 - mkt20)
            a["dd"].append(dd)
            a["t1"].append(t1)
            a["t3"].append(t3)
            a["size"].append(sz)

        if (ti + 1) % 25 == 0:
            print("[bt] %d/%d 再平衡日完成 (%.1fs)" % (ti + 1, len(rb), time.time() - t0), flush=True)

    # 汇总输出
    print("\n==================== 低位池/低波池 行业上限回测 ====================")
    print("样本区间 %s → %s | 再平衡日 %d 个 | 池子 N=%d | 波动率回看 %d 日"
          % (dates[rb[0]], dates[rb[-1]], len(rb), N, args.vol_win))
    print("（超额收益 = 池子等权 − 同日全市场等权；按再平衡日聚合）\n")
    hdr = "%-14s | %8s %8s | %10s | %8s %8s | %6s" % (
        "方案", "T+5超额", "T+20超额", "最大回撤", "Top1行业", "Top3行业", "池均长")
    print(hdr)
    print("-" * len(hdr))
    summary = {}
    for name, per, pct in schemes:
        a = agg[name]
        def mean(x):
            return float(np.mean(x)) if x else float("nan")
        ex5 = mean(a["ex5"]) * 100
        ex20 = mean(a["ex20"]) * 100
        dd = mean(a["dd"]) * 100
        t1 = mean(a["t1"]) * 100
        t3 = mean(a["t3"]) * 100
        sz = mean(a["size"])
        summary[name] = (ex5, ex20, dd, t1, t3, sz)
        print("%-14s | %7.2f%% %7.2f%% | %9.2f%% | %7.2f%% %7.2f%% | %5.1f" % (
            name, ex5, ex20, dd, t1, t3, sz))
    print("\n单位：收益/回撤为百分比(%)；Top1/Top3 为占池子比例(%)；池均长=平均入选只数。")

    # 把汇总写成 json 便于下游/报告引用
    out_json = os.path.join(ROOT, "tools", "backtest_lowvol_industry_result.json")
    try:
        import json
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump({
                "params": {"start": start_year, "N": N, "vol_win": args.vol_win,
                           "n_rebalance": len(rb),
                           "range": [dates[rb[0]], dates[rb[-1]]]},
                "schemes": {k: {"ex5_pct": v[0], "ex20_pct": v[1], "maxdd_pct": v[2],
                               "top1_pct": v[3], "top3_pct": v[4], "pool_size": v[5]}
                            for k, v in summary.items()},
            }, fh, ensure_ascii=False, indent=2)
        print("\n结果已写 %s" % out_json)
    except Exception as e:  # noqa: BLE001
        print("写 json 失败：%s" % e)

    print("\n解读提示：对比'不加上限'与'加上限'——若加上限后 T+5/T+20 超额不显著恶化、"
          "最大回撤不放大，而 Top1 行业占比明显下降，则值得加；反之若收益明显受损且无回撤改善，则不建议加。")


if __name__ == "__main__":
    main()
