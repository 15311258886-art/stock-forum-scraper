"""历史回测板块后端（2026-10-15 新增）。

目的：把「每日预测」的推荐名单与**真实后续走势**对齐，让预测准确性可以直接检验。

口径（100% 复用 predictor 的复盘实现，绝不另写公式 —— 从根上防止口径漂移）：
    收益  ret(p)   = _window_ret(k, idx, p) = k[idx+p].收盘 / k[idx].收盘 - 1
    基准  bench(p) = 候选池（SS.universe(POOL_LIMIT)）全部票同期 ret(p) 的**均值**
    命中  hit(p)   = 1 当且仅当 ret(p) > bench(p)（**跑赢池子**，不是"涨了"）

    开工前已实测对齐（详见 verify_backtest.cjs 的等价性断言，长期守住这条）：
      · 逐股 ret 与 prediction_item.ret_t1/t3/t5 一致率 36/36
      · 基准 bench 与 prediction_run.bench_json 一致率 32/32
      · 命中 hit 一致率 272/277，5 条差异全部是「引擎当时该票没 K 线 → 留空，
        现在数据到了」，非口径差异

数据源：只读 predictor 的本地 K 线缓存 predict_kcache.json —— **不联网、不写业务表、
不做任何 DB 迁移**，因此对 16:00 日更管线零风险。

数据源（2026-10-15 进哥拍板：旧「主引擎预测」是上一版逻辑、问题多、已弃用，
**本板块只留四支柱**）：
    pillars —— 四支柱（pillar_item），2026-10-14 首期
    ⚠️ prediction_item 表本身保留 —— 四支柱 D 柱校准仍在读它，删表会把新引擎搞坏；
       这里撤掉的只是「历史回测面板里的那条旧数据源与切换按钮」。

⚠️ 诚实原则（本模块硬约束）：
    未到期的周期一律 status="pending" 并给出「还差几个交易日 / 预计到期日」，
    **绝不填 0、绝不假装已回测**。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from bisect import bisect_left
from datetime import datetime

import predictor as P

# 回测周期（交易日）。20 日为 2026-10-15 应进哥要求新增（此前引擎只有 1/3/5/10）
PERIODS = (1, 3, 5, 10, 20)
SOURCE_LABEL = {"pillars": "四支柱"}
# 2026-10-16：四支柱键 → 短名（与前端 PILLAR_TABS 同名）。用于「每页回测只算该支柱」的
# 合法性校验与文案；键名以 pillars.py 的产出为准。
PILLAR_LABEL = {"policy_funnel": "政策漏斗", "t1_surf": "竞价冲浪",
                "dip_revert": "超跌修复", "resonance": "共振风控",
                "self_learn": "引擎自加权"}      # 2026-10-16 新增第 5 根支柱

_LOCK = threading.Lock()
_CTX: dict = {"mtime": None, "bars": None, "pool_curves": None, "series": None,
              "asof": None, "cal": None, "elapsed": {}}

# 2026-10-29：回测只需要「预测日之后若干个交易日」的收盘价，不需要全历史。
# 取最近 SERIES_TAIL 个交易日做窗口（四支柱首期 2026-10-14 起，实测远在窗口内）；
# 若某次请求的日期早于窗口起点，_pillar_rows 会自动退回逐只全量读取，绝不静默丢数据。
SERIES_TAIL = 560


class _CloseSeq:
    """把 (dates, closes) 两个数组包成「像 bars 列表」的只读序列。

    为什么要它：下游 `_cells()` / `P._window_ret()` 的入参是 `k[i]["c"]` 形式的 bars 列表。
    为了**一个字都不改口径公式**，这里提供一个只暴露 `d`/`c` 的轻量视图 ——
    `k[idx]["c"]` 依然取到同一个收盘价，但不用为全池 745 只票各建 4400 个 dict
    （那是原实现 73s 与 ~800MB 内存的来源）。
    """

    __slots__ = ("_d", "_c")

    def __init__(self, dates, closes):
        self._d = dates
        self._c = closes

    def __len__(self):
        return len(self._c)

    def __getitem__(self, i):
        if i < 0:
            i += len(self._c)
        if i < 0 or i >= len(self._c):
            raise IndexError(i)
        return {"d": self._d[i], "c": self._c[i]}

    def __iter__(self):
        for i in range(len(self._c)):
            yield {"d": self._d[i], "c": self._c[i]}

    def __bool__(self):
        return bool(self._c)


def _idx_at(dates, rd: str):
    """rd 在升序日期数组里的下标；该日无 K 线（停牌/未上市）返回 None。"""
    if not dates:
        return None
    i = bisect_left(dates, rd)
    if i < len(dates) and dates[i] == rd:
        return i
    return None


def _now() -> str:
    # P2-06: 写入时间戳用 CST 时区感知时间，避免 naive datetime.now() 跨时区错乱
    try:
        from timeutil import now_cst_str
        return now_cst_str()
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _f(v):
    return P._f(v)


# ---------------------------------------------------------------- 上下文（离线+记忆化）
def _context() -> dict:
    """加载 K 线序列与候选池。按 kbase 文件 mtime 记忆化，避免每次请求重读大盘。

    只读本地文件，**不触发任何网络抓取**（这是它敢于直接挂 HTTP 接口的前提）。

    2026-10-29 性能重构（real fix，不是打补丁）：
      旧实现 `pool_curves = {c: _bars_index(bars[c]) for c in codes}` ——
      对候选池 745 只票**逐只** `bars_of()`：745 次开 SQLite 连接 + 360 万条 dict 构造，
      实测单次 /api/backtest **73s**（前端 15s 预算早就放弃 → 面板永远"加载失败"、
      自动重试 3 次越堆越堵）。现改为：
        ① kbase 一次批量取 (date, close)（`close_series_bulk`），只要回测用得到的两个字段；
        ② 只取最近 SERIES_TAIL 个交易日（预测日之后最多 20 根就够）；
        ③ 池索引改为「日期数组 + 二分查找」，不再为每只票建 4400 项 date→idx 字典。
      口径零改动：收益/基准/命中仍走 `P._window_ret` 原函数（见模块头 100% 复用声明）。
    """
    try:
        from viewer import kbase as _kb
        base = _kb.get_base()
        mtime = os.path.getmtime(_kb.DB)
    except Exception:  # noqa: BLE001
        base, mtime = None, None
    with _LOCK:
        if _CTX["bars"] is not None and _CTX["mtime"] == mtime:
            return _CTX
        bars = (P.load_kcache().get("bars") or {})     # KBaseBars 惰性代理，此处零读盘
        try:
            codes, _names, _b = P.SS.universe(P.POOL_LIMIT)
        except Exception:  # noqa: BLE001  候选池拿不到就退化为缓存内前 POOL_LIMIT 只，不阻断页面
            codes = list(bars.keys())[:P.POOL_LIMIT]
        # 交易日历：单条 SQL 取 DISTINCT date（已按 mtime 记忆化），供 _elapsed 与窗口起点用
        try:
            cal = base.distinct_dates() if base is not None else []
        except Exception:  # noqa: BLE001
            cal = []
        min_date = cal[-SERIES_TAIL] if len(cal) > SERIES_TAIL else ""
        series: dict = {}
        if base is not None:
            try:
                skey = (mtime, min_date, len(codes))
                cached = getattr(base, "_series_cache", None) or {}
                if cached.get("key") == skey:
                    series = cached.get("series") or {}
                else:
                    series = base.close_series_bulk(codes, min_date=min_date)
                    base._series_cache = {"key": skey, "series": series}
            except Exception:  # noqa: BLE001  批量失败 → 退化为逐只惰性读（慢但不出错）
                series = {}
        if not series:      # 兜底：kbase 批量不可用时，仍按老路读取（保证功能不挂）
            pool_bars = {c: bars[c] for c in codes if c in bars}
            series = {c: ([str(b.get("d", ""))[:10] for b in v],
                          [b.get("c") for b in v]) for c, v in pool_bars.items()}
        # 口径对齐：基准池严格限定为 SS.universe(POOL_LIMIT) ∩ 本地缓存，且至少 30 根 K 线
        pool_curves = {c: s[0] for c, s in series.items() if len(s[0]) >= 30}
        # asof = 交易日历的最后一天。
        # 2026-10-29：原先取「候选池最后一根 K 线的众数日期」，实测出现自相矛盾——
        # 补齐脚本按代码顺序跑，池内 745 只先拿到 10-28，而全市场日历（覆盖率过滤后）
        # 还是 10-27，于是面板同时显示「K 线截至 2026-10-28」和「已走完 1 个交易日」。
        # 统一取 cal[-1]：与 _elapsed / _bench_at / _window_ret 全部同源，不再打架。
        asof = cal[-1] if cal else None
        _CTX.update({"mtime": mtime, "bars": bars, "pool_curves": pool_curves,
                     "series": series, "asof": asof, "cal": cal, "elapsed": {},
                     "bench_cache": {}})
        return _CTX


def _bench_at(rd: str) -> dict:
    """某预测日的基准：全池同期 ret 均值（与引擎同口径）。未到期该周期为 None。

    2026-10-29：改为在 `_context()` 预取的 (dates, closes) 序列上做二分定位，
    不再逐只 `bars[c]` 惰性读盘（原实现 745×5≈3725 次读盘 ≈14s，叠加冷启动可达 73s）。
    另按预测日记忆化 —— 跨期累计统计要对全部 10 期各算一次基准，不缓存就是 10 倍重复劳动。
    """
    ctx = _context()
    cache = ctx.setdefault("bench_cache", {})
    if rd in cache:
        return cache[rd]
    series, pool = ctx["series"], ctx["pool_curves"]
    out = {}
    for p in PERIODS:
        rets = []
        for c, dates in pool.items():
            idx = _idx_at(dates, rd)
            if idx is None:
                continue
            d_, c_ = series[c]
            x = P._window_ret(_CloseSeq(d_, c_), idx, p)
            if x is not None:
                rets.append(x)
        out[p] = (sum(rets) / len(rets)) if rets else None
    cache[rd] = out
    return out


def _elapsed(rd: str) -> int:
    """相对该预测日已走完的交易日数（= 交易日历中 > rd 的天数）。带记忆化。"""
    ctx = _context()
    cache = ctx["elapsed"]
    if rd in cache:
        return cache[rd]
    cal = ctx["cal"] or []
    n = sum(1 for d in cal if d > rd)   # cal 已排序，量级 660，直接计数足够快
    cache[rd] = n
    return n


def _cells(k: list, idx, rd: str, bench: dict) -> list:
    """逐周期结果。三种状态，必须分清（否则会把"停牌无数据"谎报成"还没到期"）：

        ok      —— 已到期且有值，ret/bench/alpha/hit 齐全
        pending —— 该周期**还没到期**（全局交易日数不够），带 need=还差几个交易日
        nodata  —— 已过了该周期的日期，但**这只票缺 K 线**（停牌/新股/退市），无法回测

    金额一律返回全精度（不在后端取整），取整交给前端显示 ——
    这样 verify_backtest_caliber.py 才能用 1e-9 严格比对引擎已入库值，守住口径不漂移。
    """
    elapsed = _elapsed(rd)
    if idx is None:
        return [{"p": p, "ret": None, "bench": None if bench.get(p) is None else bench[p],
                 "alpha": None, "hit": None, "status": "nodata", "need": 0} for p in PERIODS]
    cells = []
    for p in PERIODS:
        r = P._window_ret(k, idx, p)
        b = bench.get(p)
        if r is None:
            if p > elapsed:
                cells.append({"p": p, "ret": None, "bench": None if b is None else b,
                              "alpha": None, "hit": None,
                              "status": "pending", "need": p - elapsed})
            else:
                cells.append({"p": p, "ret": None, "bench": None if b is None else b,
                              "alpha": None, "hit": None, "status": "nodata", "need": 0})
        else:
            cells.append({"p": p, "ret": r, "bench": b,
                          "alpha": None if b is None else r - b,
                          "hit": (1 if (b is not None and r > b) else 0),
                          "status": "ok", "need": 0})
    return cells


# ---------------------------------------------------------------- 日期清单
def dates(con: sqlite3.Connection) -> list:
    """可回测的日期（**只含四支柱**；2026-10-15 起不再列旧主引擎）。

    n_engine 仍保留在返回体里（恒为 0）—— 只为不给历史版本的其它消费方留下 KeyError。

    2026-10-16（进哥二次修正）：每页底下的回测只算该页那根支柱的票，所以日期清单必须带上
    **每根支柱各自的条数** n_by_pillar —— 否则切到某根支柱时会挑到"它当期 0 只"的那一天，
    打开就是空面板（这类"点了没变化"的坑本项目踩过好几次）。
    """
    m: dict = {}
    try:
        for r in con.execute("SELECT run_date, pillar, COUNT(*) n FROM pillar_item "
                             "GROUP BY run_date, pillar"):
            e = m.setdefault(r["run_date"], {"pillars": 0, "by": {}})
            e["pillars"] += r["n"]
            e["by"][r["pillar"]] = r["n"]
    except sqlite3.Error:
        pass
    out = []
    for d in sorted(m.keys(), reverse=True):
        out.append({"date": d, "sources": ["pillars"],
                    "n_engine": 0, "n_pillars": m[d]["pillars"],
                    "n_by_pillar": m[d]["by"],
                    "elapsed": _elapsed(d)})
    return out


# ---------------------------------------------------------------- 行装配
def _pillar_rows(con, rd: str, bench: dict) -> list:
    ctx = _context()
    bars, series = ctx["bars"], ctx["series"]
    titles = {r["pillar"]: (r["title"] or r["pillar"])
              for r in con.execute("SELECT pillar,title FROM pillar_run WHERE run_date=?", (rd,))}
    rows = []
    for r in con.execute(
            "SELECT symbol,name,pillar,rank,score,reason FROM pillar_item "
            "WHERE run_date=? ORDER BY pillar, rank", (rd,)):
        sym = r["symbol"]
        ser = series.get(sym)
        if ser is not None:
            dates, closes = ser
            k = _CloseSeq(dates, closes)
            idx = _idx_at(dates, rd)
            if idx is None and dates and rd < dates[0]:
                # 该预测日早于预热窗口起点 → 退回逐只全量读取（慢一点，但绝不把
                # 「窗口外」错报成"停牌无数据"，那是口径错误，比慢更难接受）
                k = bars.get(sym) or []
                idx = P._bars_index(k).get(rd)
        else:
            k = bars.get(sym) or []
            idx = P._bars_index(k).get(rd)
        entry = None
        if idx is not None and 0 <= idx < len(k):
            entry = _f(k[idx].get("c"))
        rows.append({
            "symbol": r["symbol"], "name": r["name"], "tier": "",
            "rank": r["rank"], "score": r["score"], "reason": r["reason"] or "",
            "signals": "", "bucket": r["pillar"],
            "bucket_label": titles.get(r["pillar"], r["pillar"]),
            "entry_close": entry, "next_open": None,
            "has_bars": bool(k), "cells": _cells(k, idx, rd, bench),
        })
    return rows


# ---------------------------------------------------------------- 汇总
def _summarize(rows: list, period: int) -> dict:
    """对某周期做汇总：样本/命中率/平均收益/平均超额/最好最差。未到期不参与统计。"""
    rets, alphas, matured, pending, nodata = [], [], [], 0, 0
    hit_n = 0
    best = worst = None
    for r in rows:
        c = next((x for x in r["cells"] if x["p"] == period), None)
        if not c:
            continue
        if c["status"] == "pending":
            pending += 1
            continue
        if c["status"] != "ok":
            nodata += 1
            continue
        matured.append(r)
        rets.append(c["ret"])
        if c["alpha"] is not None:
            alphas.append(c["alpha"])
        if c["hit"]:
            hit_n += 1
        if best is None or c["ret"] > best["ret"]:
            best = {"symbol": r["symbol"], "name": r["name"], "ret": c["ret"]}
        if worst is None or c["ret"] < worst["ret"]:
            worst = {"symbol": r["symbol"], "name": r["name"], "ret": c["ret"]}
    n = len(matured)
    bench = next((x["bench"] for r in matured for x in r["cells"]
                  if x["p"] == period and x["bench"] is not None), None)
    hit_rate = (hit_n / n) if n else None
    avg_ret = (sum(rets) / n) if n else None
    avg_alpha = (sum(alphas) / len(alphas)) if alphas else None
    # 审计#6：明确区分「缺 K 线（nodata，数据缺口）」与「未到期（pending，正常等待）」。
    # 当该周期全部入选标的都缺 K 线、既无到期样本也无未到期样本时 → 显式报警，
    # 避免被误读为"还没到期"（后者只是交易日没走完，未来会有数据）。
    nodata_alert = bool(nodata > 0 and n == 0 and pending == 0)
    if not n:
        if nodata_alert:
            verdict = ("该周期全部入选标的都缺本地 K 线（nodata），无可用收益 —— "
                       "这是**数据缺口**（需刷新 K 线缓存），与「未到期（pending）」不同，"
                       "后者只是交易日还没走完、未来会有数据。")
        else:
            verdict = "该周期尚无已到期样本（数据未走完），不下结论。"
    else:
        tone = "强于" if (hit_rate or 0) > 0.5 else ("弱于" if (hit_rate or 0) < 0.5 else "与")
        verdict = ("该周期已到期 %d 只，跑赢池子 %d 只（%.1f%%），平均收益 %s、平均超额 %s"
                   " → 该期预测%s池子同期平均。" % (
                       n, hit_n, hit_rate * 100,
                       ("%+.2f%%" % (avg_ret * 100)) if avg_ret is not None else "—",
                       ("%+.2f%%" % (avg_alpha * 100)) if avg_alpha is not None else "—", tone))
        if pending:
            verdict += "（另有 %d 只该周期未到期，未计入）" % pending
        if nodata:
            verdict += "（另有 %d 只缺 K 线无法回测，未计入）" % nodata
    return {"period": period, "n": n, "n_pending": pending, "n_nodata": nodata,
            "nodata_alert": nodata_alert, "hit_n": hit_n,
            "hit_rate": None if hit_rate is None else round(hit_rate, 4),
            "avg_ret": None if avg_ret is None else round(avg_ret, 6),
            "avg_bench": None if bench is None else round(bench, 6),
            "avg_alpha": None if avg_alpha is None else round(avg_alpha, 6),
            "best": best, "worst": worst, "verdict": verdict}


# ---------------------------------------------------------------- 跨期累计
def pooled(con: sqlite3.Connection, pillar: str = "") -> dict:
    """跨期累计统计：把所有期次的入选票摊平，按周期汇总成一张总表。

    为什么必须有（2026-10-29 复查新增）：
      单期样本只有 20~30 只票，命中率 33% 还是 60% 完全在噪声区间内，拿它判断
      「四支柱行不行」等于抛硬币。四支柱每天产出一期，把 10 期摊平后样本 200+，
      T+1/T+3/T+5 各都有大样本，这时命中率与平均超额才有统计意义。

    口径：与单期完全一致（同一个 `_cells` / `_window_ret` / `_bench_at`），**不另算公式**，
      只是把「按日期分组」换成「按周期分桶」。同一只票在不同期重复入选会各计一次
      （每期独立开仓，这是策略回测的常规口径，不是重复计数错误）。

    诚实约束不变：未到期计 pending、缺 K 线计 nodata，两者都不进分母。
    """
    ds = dates(con)
    pl = pillar if pillar in PILLAR_LABEL else ""
    acc = {p: {"n": 0, "hit": 0, "rets": [], "alphas": [], "benches": [],
               "pend": 0, "nodata": 0} for p in PERIODS}
    used = []
    for d in ds:
        rd = d["date"]
        if pl and (d.get("n_by_pillar") or {}).get(pl, 0) <= 0:
            continue
        rows = _pillar_rows(con, rd, _bench_at(rd))
        if pl:
            rows = [r for r in rows if (r.get("bucket") or "") == pl]
        if not rows:
            continue
        used.append(rd)
        for r in rows:
            for c in r["cells"]:
                a = acc[c["p"]]
                if c["status"] == "pending":
                    a["pend"] += 1
                elif c["status"] != "ok":
                    a["nodata"] += 1
                else:
                    a["n"] += 1
                    a["hit"] += c["hit"] or 0
                    a["rets"].append(c["ret"])
                    if c["alpha"] is not None:
                        a["alphas"].append(c["alpha"])
                    if c["bench"] is not None:
                        a["benches"].append(c["bench"])
    out = {}
    for p in PERIODS:
        a = acc[p]
        n = a["n"]
        out[str(p)] = {
            "period": p, "n": n, "n_pending": a["pend"], "n_nodata": a["nodata"],
            "hit_n": a["hit"],
            "hit_rate": (round(a["hit"] / n, 4) if n else None),
            "avg_ret": (round(sum(a["rets"]) / n, 6) if n else None),
            "avg_bench": (round(sum(a["benches"]) / len(a["benches"]), 6) if a["benches"] else None),
            "avg_alpha": (round(sum(a["alphas"]) / len(a["alphas"]), 6) if a["alphas"] else None),
        }
    # 一句话结论：只在样本足够时下判断，样本不足就如实说不够
    best_p = max((p for p in PERIODS if (out[str(p)]["n"] or 0) >= 20),
                 key=lambda p: out[str(p)]["avg_alpha"] or -9, default=None)
    if best_p is None:
        verdict = "跨期累计样本还不足（各周期已到期样本均少于 20 个），暂不下结论。"
    else:
        b = out[str(best_p)]
        tone = "正" if (b["avg_alpha"] or 0) > 0 else "负"
        verdict = ("跨期累计 %d 期明细、T+%d 已到期 %d 个样本：跑赢池子 %.1f%%，平均收益 %s、"
                   "平均超额 %s（%s alpha），相对池子整体%s。" % (
                       len(used), best_p, b["n"], (b["hit_rate"] or 0) * 100,
                       "%+.2f%%" % (b["avg_ret"] * 100), "%+.2f%%" % (b["avg_alpha"] * 100),
                       tone, "占优" if tone == "正" else "落后"))
    return {"periods": out, "dates_used": used, "n_dates": len(used),
            "pillar": pl, "pillar_label": PILLAR_LABEL.get(pl, ""), "verdict": verdict}


# ---------------------------------------------------------------- 对外主接口
def payload(con: sqlite3.Connection, source: str = "pillars",
            date: str = "", period: int = 0, pillar: str = "") -> dict:
    """历史回测数据（查看器只读）。

    pillar：2026-10-16 进哥要求 —— 四支柱按支柱分页，**每页底下的回测只算该支柱的票**。
    过滤放在这里做，summary / period_stats 仍由同一份 _summarize 在过滤后的行上算 ——
    口径只有一个真源，前端绝不重算收益/命中。
    """
    src = source if source in SOURCE_LABEL else "pillars"
    pl = pillar if pillar in PILLAR_LABEL else ""
    p_req = int(period or 0)
    ds = dates(con)
    if not ds:
        return {"ok": False, "note": "尚无任何预测记录，无法回测", "dates": [],
                "periods": list(PERIODS), "sources": SOURCE_LABEL}

    # 落在指定 source 上：该日期没有该源的数据就自动换到另一源
    def pick(d: dict) -> str:
        return src if src in d["sources"] else (d["sources"][0] if d["sources"] else "pillars")

    auto_date = not date
    auto_period = p_req not in PERIODS
    if auto_date:
        # 2026-10-29 复查修正：原先默认取 `max(elapsed)`，也就是**最老那一期**——
        # 打开回测看到的是两周前的 10-14，用户第一反应是「回测根本不更新」。
        # 改为「最新一期且已有到期数据」：既跟得上日更，又不会因为最新一期还没走完而整屏 pending。
        # 若连一期到期数据都没有（如刚收盘、K 线未补），退回样本最全的一期，至少不是空面板。
        cand = [d for d in ds if src in d["sources"]
                and (not pl or (d.get("n_by_pillar") or {}).get(pl, 0) > 0)] or ds
        ready = [d for d in cand if d.get("elapsed", 0) >= 1]
        if ready:
            best = max(ready, key=lambda x: x["date"])
        else:
            best = max(cand, key=lambda x: (x["elapsed"], x["date"]))
        rd = best["date"]
    else:
        hit = next((d for d in ds if d["date"] == date), None)
        rd = hit["date"] if hit else ds[0]["date"]
    use_src = pick(next(d for d in ds if d["date"] == rd))
    if auto_period:
        # 默认周期取该期已到期的**最长**周期（样本最全、最有说服力）；一个都没到期则退回 T+1
        el_ = _elapsed(rd)
        cand = [p for p in PERIODS if p <= el_]
        p0 = max(cand) if cand else 1
    else:
        p0 = p_req

    ctx = _context()
    rd_eff = rd
    bench = _bench_at(rd_eff)
    # 2026-10-15：只保留四支柱（旧主引擎数据源与源切换按钮已按进哥要求撤掉）
    rows = _pillar_rows(con, rd_eff, bench)
    if pl:                       # 2026-10-16：只留当前支柱的票（见 payload 文档串）
        rows = [r for r in rows if (r.get("bucket") or "") == pl]

    per_stats = {str(p): _summarize(rows, p) for p in PERIODS}
    summary = per_stats[str(p0)]
    elapsed = _elapsed(rd_eff)
    n_nobars = sum(1 for r in rows if not r["has_bars"])
    # 选中周期零样本 → 如实说明，并指出该期哪些周期已有样本；
    # 否则用户会以为板块坏了（2026-10-15 实测：默认落在最新一期时整屏"—"）
    if summary["n"] == 0 and pl and not rows:
        # "这根支柱这一期压根没有入选标的" ≠ "周期没到期" —— 必须分开说，否则用户以为功能坏了
        summary["verdict"] = ("该期「%s」没有入选标的（同期其它支柱有数据）——"
                              "换个日期，或点上面的其它支柱看。" % PILLAR_LABEL.get(pl, pl))
    elif summary["n"] == 0:
        avail = [(p, per_stats[str(p)]["n"]) for p in PERIODS if per_stats[str(p)]["n"] > 0]
        if avail:
            summary["verdict"] += "该期已有样本的周期：" + "、".join(
                "T+%d（%d 只）" % (p, n) for p, n in avail) + "。"
        else:
            fullest = max(ds, key=lambda x: (x["elapsed"], x["date"]))
            summary["verdict"] += ("该期全部标的都还没走完。当前回测样本最全的一期是 %s"
                                   "（已走完 %d 个交易日），换那一天看即有数据。" % (
                                       fullest["date"], fullest["elapsed"]))
    if n_nobars:
        summary["verdict"] += "（另有 %d 只无本地 K 线，无法回测）" % n_nobars
    # 每个数据源的家底（几期、其中几期已走完至少 1 个交易日）。
    # 用途：前端把「四支柱 · 1 期·未到期」直接写进按钮 —— 否则用户点了发现右栏全是「N日」，
    # 会以为功能没做出来（2026-10-15 进哥原话："我点了半天，感觉也没什么太大变化"）。
    src_stats = {}
    for k in SOURCE_LABEL:
        mine = [x for x in ds if k in x["sources"]]
        src_stats[k] = {"dates": len(mine),
                        "ready": sum(1 for x in mine if x["elapsed"] > 0)}
    return {
        "ok": True, "source": use_src, "source_label": SOURCE_LABEL[use_src],
        "pillar": pl, "pillar_label": PILLAR_LABEL.get(pl, ""), "pillar_n": len(rows),
        "date": rd_eff, "period": p0,
        "periods": list(PERIODS), "sources": SOURCE_LABEL, "source_stats": src_stats,
        "dates": ds, "rows": rows,
        "period_stats": per_stats, "summary": summary,
        "pooled": pooled(con, pl),
        "asof": ctx["asof"], "elapsed": elapsed,
        "n_nobars": n_nobars,
        "auto": {"date": auto_date, "period": auto_period},
        "note": "回测口径：收益=预测日收盘→第 N 个交易日收盘；命中=跑赢候选池同期平均。"
                "未到期周期如实标注，不填 0。数据源为本地日 K 缓存，离线计算。"
                + ("本页只统计「%s」这一根支柱的票。" % PILLAR_LABEL[pl] if pl else ""),
    }


def status(con: sqlite3.Connection) -> dict:
    """健康检查：缓存新鲜度 + 各源日期数。"""
    ctx = _context()
    return {"ok": True, "kcache_updated": ctx.get("asof"), "periods": list(PERIODS),
            "bars_n": len(ctx["bars"] or {}), "pool_n": len(ctx["pool_curves"] or {}),
            "dates": dates(con), "at": _now()}


def warm() -> None:
    """预热：把 K 线缓存与候选池提前加载进内存。

    为什么必须有：`_context()` 首次调用要解析 predict_kcache.json（201 只票、数百 KB～数 MB）
    并调 SS.universe() 取候选池，冷态要数秒。而浏览器对慢响应会**主动掐断连接**
    （此前踩过 `ConnectionAbortedError: WinError 10053`），表现为页面直接显示
    "回测加载失败"——用户会以为功能没做出来（2026-10-15 刚重启后端时实测踩到）。
    由 server.py 在启动时用后台线程调用；预热后首个请求降到 ~0.04s。
    自身不抛异常（预热失败不影响功能，首个请求会自行加载）。
    """
    try:
        ctx = _context()
        print("[backtest] 预热完成：K 线 %d 只 / 候选池 %d 只 / 截至 %s" % (
            len(ctx["bars"] or {}), len(ctx["pool_curves"] or {}), ctx.get("asof")), flush=True)
        # 2026-10-25 补强：仅建上下文不够。全市场底座（5237 只）下，首个 /api/backtest 还要
        # 逐票惰性加载 K 线 + 算 cells（约 264 条），冷态实测 ~14s，浏览器可能掐断（WinError 10053）
        # 表现为「回测加载失败」。这里再真正算一版 payload，把 per-symbol 缓存与计算结果一并预热。
        try:
            con = sqlite3.connect(f"file:{P.DB_PATH}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row   # 2026-10-29：与 server.DB.conn 一致，否则 r["col"] 抛 TypeError 被静默吞掉，预热名存实亡
            try:
                payload(con)  # 默认 source=pillars，触发全部历史票 K 线惰性加载 + cells 计算
            finally:
                con.close()
            print("[backtest] payload 预热完成（首点即秒出）", flush=True)
        except Exception as e:  # noqa: BLE001
            print("[backtest] payload 预热跳过（不影响功能）：%s" % str(e)[:120], flush=True)
    except Exception as e:  # noqa: BLE001
        print("[backtest] 预热失败（不影响功能，首个请求会自行加载）：%s" % str(e)[:140], flush=True)
