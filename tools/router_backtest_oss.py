# -*- coding: utf-8 -*-
"""T10 · oss / 形态规则系列（11 个工具）的历史回测。

为什么单独一个引擎
    这 11 个工具的判据不是「看 K 线序列」，而是「看**某个交易日的指标快照**」
    （`ma5/ma20/ma60`、`volume_ratio`、`atr`、`rsi6`）。快照来自 `stock_indicators_daily`，
    而这张表只回溯到 2025-09-30（264 个交易日）。

    和「自己按 kbase 日线重算 58 个指标」相比，**直接复用现成快照**的好处是口径零风险：
    `_build_oss_snapshot()` 已经被工具箱用了几个月，卡片上的数字就是它算的。
    自己重算最多做到"看起来差不多"，而回测结论最怕的就是"差一点"。

    所以本引擎的区间上限由数据决定：约 **2025-11 ~ 2026-11**（前 22 个交易日要留给
    `ret1/ret2/ret5/mom20` 的滚动窗口）。这是硬边界，写在 caveats 里，不假装能跑 5 年。

口径逐字照抄 `toolbox._build_oss_snapshot`（连"差一根 K 线"的 ret5/mom20 都照抄，
因为那两条规则的回测数字就是按这个口径跑的，改成"正确"的会与卡片对不上）：
    ret1 = close / back[-2] - 1      ret2 = close / back[-3] - 1
    ret5 = close / back[-7] - 1      mom20 = close / back[-22] - 1
    dist_high250 = close / high - 1  ma20_dev = close / ma20 - 1
    atr_pct = atr / close * 100

前向收益用 **kbase 后复权收盘**（不复权价在除权日会凭空跳空，把分红当成亏损）。

用法
    python tools/router_backtest_oss.py --tool oss_high_vol --horizon 20 --step 5
"""
from __future__ import annotations

import argparse

try:    # 单一成本事实源（P4 统一 15bp）；导入失败则退回旧默认，不阻断回测
    from viewer.kernel import params as _P
except Exception:  # noqa: BLE001
    _P = None
import bisect
import json
import math
import os
import statistics
import sys
import time
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_CACHE = {"panel": None, "kline": None}


# ---------------------------------------------------------------- 数据

def _indicator_dates(con) -> list:
    return [str(r[0]) for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]


def _kline_panel(codes, min_date: str) -> dict:
    """{code: (dates, closes)}，后复权。用于算前向收益与对齐日期。"""
    key = (len(codes), min_date)
    if _CACHE["kline"] and _CACHE["kline"][0] == key:
        return _CACHE["kline"][1]
    from viewer import kbase as _kb
    raw = _kb.get_base().ohlcv_bulk([str(c) for c in codes], min_date=min_date)
    out = {}
    for code, rows in raw.items():
        ds = [r[0] for r in rows]
        cs = [r[4] for r in rows]              # kbase 列序 (date,o,h,l,c,v) → 第 5 位是 close
        if cs:
            out[str(code)] = (ds, cs)
    _CACHE["kline"] = (key, out)
    return out


def _pct(a, b):
    if a is None or b is None:
        return None
    try:
        b = float(b)
        if b == 0:
            return None
        return (float(a) / b - 1.0) * 100.0
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        v = float(x)
        return v if v == v else None          # 排除 NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 单工具回测

def run_oss(tool: str, start: str, end: str, step: int = 5, horizon: int = 20,
            cost_bp: float = (round(_P.BACKTEST_FEE_ROUNDTRIP * 10000.0, 2) if _P else 13.0),
            pool: int = 0) -> dict:
    import sqlite3

    from viewer import toolbox as TB

    pred = TB._OSS_PRED.get(tool)             # noqa: SLF001 项目内同一套判据，不另抄
    if pred is None:
        return {"ok": False, "tool": tool, "error": "不是 oss 系列工具"}

    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "viewer", "app.db"), uri=True)
    try:
        # ★ 必须用**工具登记的默认参数**，不能传 {}：
        #   `oss_near_high` 与 `oss_near_high_tight` 共用同一个判据 `_p_near_high`，
        #   区别全在默认参数（max_gap 25% vs 5%）。传空参数会让 tight 版多选 677 只
        #   （实测 125 → 802），而且这种错不会报任何异常 —— 回测结论直接失真。
        from viewer import tool_contract as TC
        try:
            reg = dict(TB.get_tool(con, tool) or {})
            prm = TC.effective_defaults(reg, TC.get(con, tool))
        except Exception:                      # noqa: BLE001
            prm = {}
        print("[oss] %s 参数：%s" % (tool, json.dumps(prm, ensure_ascii=False)))
        dates = _indicator_dates(con)
        if not dates:
            return {"ok": False, "tool": tool, "error": "指标表为空"}
        # 前 22 个交易日留给滚动窗口；再往前不可能有快照
        usable_from = dates[22] if len(dates) > 22 else dates[0]
        s = max(start, usable_from)
        e = min(end, dates[-1])
        sample = [d for d in dates if s <= d <= e][::max(1, step)]
        if len(sample) < 5:
            return {"ok": False, "tool": tool,
                    "error": "区间内截面不足（指标表只回溯到 %s，可用起点 %s）"
                             % (dates[0], usable_from)}

        # ---- 定池：干净宇宙 ∩ 指标表
        from viewer import universe_filter as UF
        uni = {str(c) for c in (UF.evaluation_universe() or [])}
        pool_codes = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT code FROM stock_indicators_daily")} & uni
        if not pool_codes:
            pool_codes = {str(r[0]) for r in con.execute(
                "SELECT DISTINCT code FROM stock_indicators_daily")}
        pool_codes = sorted(pool_codes)
        if pool and 0 < pool < len(pool_codes):
            import random
            pool_codes = sorted(random.Random(20261106).sample(pool_codes, pool))

        kl = _kline_panel(pool_codes, dates[0])
        target = set(sample)

        # ---- 一次流式扫描：按日推进历史窗口，到目标日就判一次
        hist: dict = {}
        high: dict = {}
        kept_by_day: dict = {}
        t0 = time.time()
        cur = con.execute(
            "SELECT date,code,close,ma5,ma20,ma60,rsi6,atr,volume_ratio"
            " FROM stock_indicators_daily WHERE date>=? AND date<=? AND close>0"
            " ORDER BY date", (dates[0], e))
        for dt, code, close, ma5, ma20, ma60, rsi6, atr, vr in cur:
            dt = str(dt)
            code = str(code)
            c = _f(close)
            if c is None:
                continue
            dq = hist.get(code)
            if dq is None:
                dq = hist[code] = deque(maxlen=22)   # 22 = mom20 要 21 个历史 + 当日
            dq.append(c)
            h = high.get(code)
            if h is None or c > h:
                high[code] = c
            if dt not in target:
                continue                              # 【时间之墙】非目标日只推进窗口，不判定
            n = len(dq)
            snap = TB._Snap(                          # noqa: SLF001 与工具箱同一结构
                code=code, name="", close=c,
                ma5=_f(ma5), ma20=_f(ma20), ma60=_f(ma60), rsi6=_f(rsi6),
                atr_pct=(float(atr) / c * 100.0) if _f(atr) else None,
                volume_ratio=_f(vr),
                ret1=_pct(c, dq[-2] if n >= 2 else None),
                ret2=_pct(c, dq[-3] if n >= 3 else None),
                ret5=_pct(c, dq[-7] if n >= 7 else None),
                mom20=_pct(c, dq[-22] if n >= 22 else None),
                dist_high250=_pct(c, high.get(code)),
                ma20_dev=_pct(c, ma20),
            )
            try:
                ok, _why, _sc = pred(snap, prm)
            except Exception:                          # noqa: BLE001
                ok = False
            if ok:
                kept_by_day.setdefault(dt, set()).add(code)

        print("[oss] %s 扫描完 %.1fs，%d 个截面有入选" % (
            tool, time.time() - t0, len(kept_by_day)))

        # ---- 逐截面算超额（同池基准）
        cost = cost_bp / 10000.0
        records = []
        for D in sample:
            kept = kept_by_day.get(D) or set()
            ret_all, ret_kept = [], []
            for code in pool_codes:
                p = kl.get(code)
                if not p:
                    continue
                ds, cs = p
                i = bisect.bisect_right(ds, D) - 1
                if i < 0 or i + horizon >= len(cs):
                    continue
                c0 = cs[i]
                c1 = cs[i + horizon]
                if not c0 or c0 <= 0 or not c1:
                    continue
                r = c1 / c0 - 1.0
                ret_all.append(r)
                if code in kept:
                    ret_kept.append(r)
            if len(ret_all) < 30:
                continue
            base = statistics.fmean(ret_all)
            if ret_kept:
                gross = statistics.fmean(ret_kept)
                excess = gross - base - cost
            else:
                gross = excess = None
            records.append({"date": D, "n_pool": len(ret_all), "n_kept": len(ret_kept),
                            "base": base, "gross": gross, "excess": excess})
    finally:
        con.close()

    if not records:
        return {"ok": False, "tool": tool, "error": "没有可用截面"}

    def agg(rs):
        ex = [r["excess"] for r in rs if r["excess"] is not None]
        m = statistics.fmean(ex) if ex else None
        t = None
        if len(ex) >= 3:
            sd = statistics.pstdev(ex)
            if sd > 0:
                t = m / (sd / math.sqrt(len(ex)))
        return {
            "sections": len(rs),
            "avg_kept": round(statistics.fmean([r["n_kept"] for r in rs]), 1),
            "pct_kept": round(100.0 * sum(r["n_kept"] for r in rs)
                              / max(1, sum(r["n_pool"] for r in rs)), 2),
            "excess_pp": round(m * 100, 3) if m is not None else None,
            "win_rate": round(100.0 * sum(1 for x in ex if x > 0) / len(ex), 1) if ex else None,
            "t": round(t, 2) if t is not None else None,
        }

    segs = []
    size = max(1, len(sample) // 6)
    for i in range(0, len(sample), size):
        seg = sample[i:i + size]
        rs = [r for r in records if seg[0] <= r["date"] <= seg[-1]]
        if rs:
            a = agg(rs)
            a["from"], a["to"] = seg[0], seg[-1]
            segs.append(a)

    return {
        "ok": True, "tool": tool, "kind": "oss",
        "params": {"start": s, "end": e, "step": step, "horizon": horizon,
                   "cost_bp": cost_bp, "pool": pool},
        "pool_size": len(pool_codes),
        "overall": agg(records),
        "by_segment": segs[:6],
        "elapsed_sec": round(time.time() - t0, 1),
        "caveats": [
            "区间受 stock_indicators_daily 限制（只回溯到 %s），**不是 5 年**" % dates[0],
            "判据口径与工具箱完全一致（复用 _Snap / _OSS_PRED，不重算指标）",
            "名称用 stock_names 的当前名（skip_st 默认关闭，影响可忽略）",
            "超额基准 = 同时点同池；已扣 %.0f bp 双边成本" % cost_bp,
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", required=True)
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="2026-11-06")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--cost-bp", type=float,
                     default=(round(_P.BACKTEST_FEE_ROUNDTRIP * 10000.0, 2) if _P else 13.0))
    ap.add_argument("--pool", type=int, default=0)
    a = ap.parse_args(argv)
    r = run_oss(a.tool, a.start, a.end, a.step, a.horizon, a.cost_bp, a.pool)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
