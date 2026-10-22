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
_CTX: dict = {"mtime": None, "bars": None, "pool_curves": None, "asof": None,
              "cal": None, "elapsed": {}}


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
    """加载 K 线缓存与候选池。按缓存文件 mtime 记忆化，避免每次请求重解析大 JSON。

    只读本地文件，**不触发任何网络抓取**（这是它敢于直接挂 HTTP 接口的前提）。
    """
    try:
        mtime = os.path.getmtime(P.KCACHE)
    except OSError:
        mtime = None
    with _LOCK:
        if _CTX["bars"] is not None and _CTX["mtime"] == mtime:
            return _CTX
        bars = (P.load_kcache().get("bars") or {})
        try:
            codes, _names, _b = P.SS.universe(P.POOL_LIMIT)
        except Exception:  # noqa: BLE001  候选池拿不到就退化为缓存内全部票，不阻断页面
            codes = list(bars.keys())
        # 口径对齐：基准池严格限定为 SS.universe(POOL_LIMIT) ∩ 本地缓存，
        # 与 predictor.review() 的 pool_curves 完全同源（实测 bench 32/32 一致）
        pool_bars = {c: bars[c] for c in codes if c in bars}
        pool_curves = {c: P._bars_index(v) for c, v in pool_bars.items() if len(v) >= 30}
        asof = P._trade_date_of(pool_bars) or P._trade_date_of(bars)
        # 交易日历只建一次（全票 bar 日期并集），供 _elapsed 二分；否则每行都要重扫 13 万个 bar
        calset = set()
        for v in bars.values():
            for b in v:
                d = str(b.get("d", ""))[:10]
                if d:
                    calset.add(d)
        _CTX.update({"mtime": mtime, "bars": bars, "pool_curves": pool_curves, "asof": asof,
                     "cal": sorted(calset), "elapsed": {}})
        return _CTX


def _bench_at(rd: str) -> dict:
    """某预测日的基准：全池同期 ret 均值（与引擎同口径）。未到期该周期为 None。"""
    ctx = _context()
    bars, pool = ctx["bars"], ctx["pool_curves"]
    out = {}
    for p in PERIODS:
        rets = [x for c, idx in pool.items()
                if (x := P._window_ret(bars[c], idx.get(rd), p)) is not None]
        out[p] = (sum(rets) / len(rets)) if rets else None
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
    bars = ctx["bars"]
    titles = {r["pillar"]: (r["title"] or r["pillar"])
              for r in con.execute("SELECT pillar,title FROM pillar_run WHERE run_date=?", (rd,))}
    rows = []
    for r in con.execute(
            "SELECT symbol,name,pillar,rank,score,reason FROM pillar_item "
            "WHERE run_date=? ORDER BY pillar, rank", (rd,)):
        k = bars.get(r["symbol"]) or []
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
        # 默认落在「回测数据最全」的一期（走完交易日最多，并列取较新）。
        # 不能默认取最新一期 —— 最新一期必然全部未到期，打开就是空面板，
        # 那就违背了"让我直观看到回测数据"的目的（2026-10-15 实测踩到）。
        # ⚠️ 且必须**限定在该数据源有数据的日期里**选，否则切到"四支柱"时会挑到
        #    只有主引擎数据的那天，随后 pick() 又回落成主引擎，表现为"点了没换过去"
        #    （2026-10-15 verify_backtest 实测踩到）。
        # 选定支柱时还必须限定在「该支柱有票」的日期里挑，否则打开就是空面板
        cand = [d for d in ds if src in d["sources"]
                and (not pl or (d.get("n_by_pillar") or {}).get(pl, 0) > 0)] or ds
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
    except Exception as e:  # noqa: BLE001
        print("[backtest] 预热失败（不影响功能，首个请求会自行加载）：%s" % str(e)[:140], flush=True)
