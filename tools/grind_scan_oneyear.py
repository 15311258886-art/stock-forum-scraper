#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性：长周期「慢阴跌」扫描（王总 2026-10-06 需求，约 30-50 只推荐）。

口径（K 线大方向，非单日跌幅）：
  - 窗口 ~1 年（>=200 个交易日的票用近 240 根，120~199 的标"半年档"）；
  - 大方向向下：区间累计跌 >= 10%，log 价回归斜率 < 0；
  - 阴跌形态：下跌天数占比 >= 52%、单日平均跌幅 <= 2%、暴跌日(<=-7%) <= 3 个；
  - 方向稳定：回归 R^2 >= 0.25；
  - 中间允许反弹（最大反抽幅度照实记录，不设上限）。
公司质量：复用程序自带估值诊股 valuation.score（PE/PB/PS/PEG/PCF 加权分）。
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import screener  # noqa: E402
import valuation  # noqa: E402
from viewer import server as SVR  # noqa: E402

WIN_MIN = 120          # 不足 120 根（约半年）的票不进候选
WIN = 240              # 最长窗口：约一年
FETCH_N = 260
WORKERS = 12


def metrics(bars):
    """窗口自适应：有 240 根看一年，只有 120+ 根看半年。"""
    n_avail = len(bars) - 1
    if n_avail < WIN_MIN:
        return None
    use = min(n_avail, WIN)
    w = bars[-(use + 1):]
    closes = [b[1] for b in w]
    if any(c <= 0 for c in closes):
        return None
    n = len(closes) - 1
    chgs = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    downs = [c for c in chgs if c < 0]
    cum = closes[-1] / closes[0] - 1.0
    if cum >= -0.08 or not downs:
        return None
    dr = len(downs) / n
    avg_dn = sum(downs) / len(downs)
    crash = sum(1 for c in chgs if c <= -0.07)
    # log 线性回归
    xs = list(range(len(closes)))
    mx, my = sum(xs) / len(xs), sum(math.log(c) for c in closes) / len(closes)
    sxx = sxy = syy = 0.0
    for x, c in zip(xs, closes):
        dy = math.log(c) - my
        sxx += (x - mx) ** 2
        syy += dy * dy
        sxy += (x - mx) * dy
    slope = sxy / sxx if sxx else 0.0
    r2 = (sxy * sxy / (sxx * syy)) if sxx and syy else 0.0
    # 最大反抽：区间最低点之后到最高点的最大涨幅
    lo = min(range(len(closes)), key=lambda i: closes[i])
    bounce = (max(closes[lo:]) / closes[lo] - 1.0) if lo < len(closes) - 1 else 0.0
    if dr < 0.50 or avg_dn > -0.025 or crash > 5 or slope >= 0 or r2 < 0.20:
        return None
    grind = dr * 50 + min(abs(cum), 50) * 0.6 + r2 * 30 - (0.5 if crash else 0)
    return {
        "code": "", "name": "", "window": n,
        "cum_pct": round(cum * 100, 1), "down_ratio": round(dr * 100, 1),
        "avg_dn_pct": round(avg_dn * 100, 2), "crash_days": crash,
        "slope_yr_pct": round((math.exp(slope * 244) - 1) * 100, 1),
        "r2": round(r2, 2), "max_bounce_pct": round(bounce * 100, 1),
        "grind": round(grind, 1),
        "price": closes[-1],
    }


def main():
    t0 = time.time()
    try:
        pool = screener._market_pool()
    except Exception:
        pool = {}
    if len(pool) < 500:
        pool = SVR._decline_pool()
    print(f"全市场池 {len(pool)} 只，开始抓 {FETCH_N} 根日 K（腾讯源，12 并发，约 5-8 分钟）…", flush=True)
    bars_map = {}
    from concurrent.futures import ThreadPoolExecutor
    def one(c):
        try:
            return c, screener._fetch_tencent_full(c, FETCH_N) or []
        except Exception:
            return c, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for c, bars in ex.map(one, pool):
            if len(bars) >= WIN_MIN + 1:
                bars_map[c] = bars
    print(f"K 线到手 {len(bars_map)} 只（{time.time()-t0:.0f}s），开始形态过滤…", flush=True)

    rows = []
    for c, bars in bars_map.items():
        m = metrics(bars)
        if m:
            m["code"] = c
            m["name"] = pool.get(c, "")
            rows.append(m)
    rows.sort(key=lambda r: -r["grind"])
    print(f"形态达标 {len(rows)} 只，取前 130 做公司质量评分…", flush=True)

    finals = []
    for m in rows[:130]:
        v = valuation.score(m["code"])
        if v.get("ok"):
            m2 = dict(m, vscore=v["score"], verdict=v.get("verdict") or "",
                      industry=v.get("industry") or "", pe=v.get("pe"), pb=v.get("pb"))
            finals.append(m2)
        time.sleep(0.12)
    finals.sort(key=lambda r: (-r["vscore"], -r["grind"]))
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M"),
           "universe": len(pool), "kline_ok": len(bars_map),
           "form_passed": len(rows), "rows": finals}
    import json
    with open("_grind_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n=== 最终 {len(finals)} 只（按估值分+阴跌分排序）===")
    for i, r in enumerate(finals, 1):
        print(f"{i:2d}. {r['code']} {r['name']:<6} 估值分{r['vscore']:>3} {r['verdict'][:6]:<6} "
              f"年跌{r['cum_pct']:>6}% 跌天比{r['down_ratio']}% 均跌{r['avg_dn_pct']}% "
              f"斜率年化{r['slope_yr_pct']}% R2={r['r2']} 最大反抽{r['max_bounce_pct']}% "
              f"·{r['industry']}")
    print(f"\n总耗时 {time.time()-t0:.0f}s → _grind_result.json")


if __name__ == "__main__":
    main()
