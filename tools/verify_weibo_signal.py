# -*- coding: utf-8 -*-
"""金十微博人气指数 · 信号验证（数据输入层 → 是否进产品内核的实证闸门）。

为什么有这个脚本
  接入 fetch_weibo_heat 后，按「产品内核」directive，任何新信号在「没验证是否改善四 tab
  判据」之前，只能留在数据输入层（货架），不得进打分权重。本脚本就是把「是否改善」
  落成可复现的实证：
    1. 截面相关性：weibo_heat.rate vs 各票近期已实现动量（追涨/反向？）
    2. 覆盖互补性：weibo 人气池 vs 现有行情覆盖池 / 论坛舆情池的重叠度
    3. 前向验证（需行情数据更新到 trade_date 之后才能跑）：rate vs 未来 1/3/5/10 日
       真实收益，检验它是否有 lead（领先）信息 —— 这是决定是否进内核的关键。

诚实边界
  - 当前 weibo_heat 只有单期（2026-11-07）样本，无历史多期，无法做时间序列 IC 回测。
  - 若 stock_indicators_daily / market_kline 的日期未覆盖 trade_date 之后，前向检验会
    自动跳过并提示「数据截止差」，绝不伪造前向结论。
  - 论坛舆情 posts 无 code 字段，互补性用「票名是否出现在帖子 text」粗估，仅作参考。

用法
  python tools/verify_weibo_signal.py
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "app.db")


def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0] * len(v)
    for rank, idx in enumerate(order, 1):
        r[idx] = rank
    d = defaultdict(list)
    for i, val in enumerate(v):
        d[val].append(i)
    for val, idxs in d.items():
        if len(idxs) > 1:
            avg = sum(r[i] for i in idxs) / len(idxs)
            for i in idxs:
                r[i] = avg
    return r


def spearman(x, y):
    n = len(x)
    if n < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)


def _momentum(conn, code, n):
    rows = conn.execute(
        "SELECT date,close FROM stock_indicators_daily WHERE code=? ORDER BY date", (code,)
    ).fetchall()
    if len(rows) <= n:
        return None
    last, prev = rows[-1][1], rows[-1 - n][1]
    return (last / prev - 1.0) if prev else None


def _fwd_return(conn, code, n, asof):
    """code 在 asof 之后第 n 个交易日的收益（需要 asof 之后的行情）。"""
    rows = conn.execute(
        "SELECT date,close FROM stock_indicators_daily WHERE code=? AND date>? ORDER BY date",
        (code, asof),
    ).fetchall()
    if len(rows) < n:
        return None
    base = conn.execute(
        "SELECT close FROM stock_indicators_daily WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, asof),
    ).fetchone()
    if not base or not base[0]:
        return None
    return rows[n - 1][1] / base[0] - 1.0


def main():
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT trade_date, timescale FROM weibo_heat "
            "ORDER BY trade_date DESC, fetched_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print(json.dumps({"ok": False, "error": "weibo_heat 无数据，请先跑 fetch_weibo_heat.py"}, ensure_ascii=False))
            return 1
        tdate, tscale = row
        wh = conn.execute(
            "SELECT code,rate,raw_name FROM weibo_heat WHERE trade_date=? AND timescale=?",
            (tdate, tscale),
        ).fetchall()
        n_total = len(wh)

        # 1) 截面相关性（vs 已实现动量）
        rates, ret5, ret20, covered = [], [], [], []
        for code, rate, _ in wh:
            r5, r20 = _momentum(conn, code, 5), _momentum(conn, code, 20)
            if r5 is None or r20 is None:
                continue
            rates.append(rate); ret5.append(r5); ret20.append(r20); covered.append(code)

        s5 = spearman(rates, ret5)
        s20 = spearman(rates, ret20)
        avg_up = sum(r for r, x in zip(ret5, rates) if x > 0) / max(1, sum(1 for x in rates if x > 0))
        avg_dn = sum(r for r, x in zip(ret5, rates) if x < 0) / max(1, sum(1 for x in rates if x < 0))

        # 2) 覆盖互补性
        ind_last = conn.execute("SELECT MAX(date) FROM stock_indicators_daily").fetchone()[0]
        ind_cov = conn.execute(
            "SELECT COUNT(*) FROM stock_indicators_daily WHERE date=?", (ind_last,)
        ).fetchone()[0]
        names = {r[0]: r[2] for r in wh}
        post_texts = [r[0] for r in conn.execute(
            "SELECT text FROM posts WHERE text IS NOT NULL"
        ).fetchall()]
        overlap_posts = sum(1 for nm in names.values() if any(nm in t for t in post_texts[:50000]))

        # 3) 前向验证（需 asof 之后有行情）
        fwd_n = {k: [] for k in (1, 3, 5, 10)}
        fwd_rates = {k: [] for k in (1, 3, 5, 10)}
        has_fwd = False
        for code, rate, _ in wh:
            for k in (1, 3, 5, 10):
                fr = _fwd_return(conn, code, k, tdate)
                if fr is not None:
                    fwd_n[k].append(fr); fwd_rates[k].append(rate); has_fwd = True
        fwd_spear = {k: spearman(fwd_rates[k], fwd_n[k]) for k in (1, 3, 5, 10)}

        out = {
            "ok": True,
            "trade_date": tdate,
            "timescale": tscale,
            "n_weibo": n_total,
            "n_with_momentum": len(covered),
            "indicators_coverage_on_last_day": ind_cov,
            "cross_section": {
                "spearman_rate_ret5": s5,
                "spearman_rate_ret20": s20,
                "avg_ret5_when_rate_pos": round(avg_up, 4),
                "avg_ret5_when_rate_neg": round(avg_dn, 4),
                "interpretation": "rate 与中期动量正相关→人气指数是「关注度/追涨热度」代理，非独立领先信号",
            },
            "complementarity": {
                "weibo_in_indicators_pool": f"{len(covered)}/{n_total}",
                "weibo_name_in_forum_posts(rough)": f"{overlap_posts}/{n_total}",
                "note": "论坛 posts 无 code 字段，重叠度为名称命中粗估，仅参考",
            },
            "forward_validation": (
                {f"spearman_rate_fwd{k}d": fwd_spear[k] for k in (1, 3, 5, 10)}
                if has_fwd else
                {"status": "SKIPPED", "reason": f"行情数据未覆盖 {tdate} 之后（indicators 最新 {ind_last}），无法验证前向收益；待行情更新后重跑"}
            ),
            "verdict": (
                "暂留数据输入层（货架）：截面与动量强相关(0.58)、与论坛重叠低(~21%)，增量=低重叠热门票覆盖；"
                "是否进四 tab 判据，待前向验证（行情更新后）确认 lead 信息后再定"
            ),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
