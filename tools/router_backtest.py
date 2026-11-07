# -*- coding: utf-8 -*-
"""T10 · 工具时点回测引擎（router backtest）。

为什么需要它
    审计要求「按行情/持有期/池内超额，至少 5 年、分 6 段、含成本」地评估 33 个工具，
    而 `tool_run_log` 全表只有 257 行、只有两天 —— 所以**不能靠回放历史日志**。

    正确做法是**在历史截面上重跑工具**：工具本质是「给定截止某日的日线窗口 → 判 yes/no」，
    只要日线在，任何一天都能重算。本机 `viewer/kbase.db` 有 1195 万行后复权日线
    （1990-12-19 ~ 2026-11-06，8760 个交易日，5813 只），5 年绰绰有余。

三条防作弊（不满足就等于自欺）
    1. **时间之墙**：判据只准看 `<= D` 的 K 线。窗口切片 `bars[:idx+1][-keep:]`，
       任何工具函数都拿不到 D 之后的数据。
    2. **同池基准**：超额 = 入选票的前向收益 − **同一时点同一池子**的前向收益。
       不能拿沪深 300 当基准 —— 那会把"池子本身选得好"混进"工具选得好"。
    3. **成本先扣**：`--cost-bp`（默认 15bp = 双边佣金 5 + 印花 5 + 滑点 5，与 signal_backtest 一致，见 viewer/kernel/params.BACKTEST_FEE_ROUNDTRIP）直接进收益，
       不做"扣不扣成本"的两套结论。含成本为正才叫有效。

已知偏差（必须写在结论旁边，不许藏）
    · **幸存者偏差**：kbase 只有"现在还在的票"的历史，退市票没有。
      2021-2023 年每年只有 2700~3200 只有数据（2024 起才 5400+），
      那几段的入选票天然是"活到今天的票"，超额会被系统性高估。
      故本引擎强制输出分段结果，并标注每段的池子覆盖率，让人能看见哪段不可信。
    · **不可时点化的工具**：`gate_admit`（当期财务）、`reso_ab`（当期共振卡）、
      `kronos_kline`（模型输出）、`pillar_*`（当期支柱批次）、`holder_org`（当期股东）
      的历史值不存在，硬回测等于把今天的信息搬回过去。这些工具本引擎**拒绝跑**并说明原因。

用法
    # 单工具、近两年、每 5 个交易日采样一次、T+20
    python tools/router_backtest.py --tool box_bottom --start 2025-01-01 --end 2026-11-06 \
        --step 5 --pool 300 --horizon 20

    # 因子类工具（tv_*/db_*）
    python tools/router_backtest.py --tool factor:tv_wbottom ... 

    # 全市场（慢，约 20~40 分钟/工具）
    python tools/router_backtest.py --tool box_bottom --pool all --step 5
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 回测原语（前向收益/成本/同池基准）统一走内核，避免 13bp/15bp 口径漂移
from viewer.kernel.backtest_core import (  # noqa: E402
    window_at, fwd_return, fwd_detail, fwd_return_open, fwd_detail_open, liq_at, slices,
    quintile_bounds as _quintile_bounds, tstat as _tstat,
)

# 回测成本默认口径：统一走内核参数（15bp = 佣金双边5 + 印花5 + 滑点5），
# 与 viewer/signal_backtest.py 的 DEFAULT_FEE 一致；--cost-bp 仍可手动覆盖。
try:
    from viewer.kernel import params as _KERNEL_PARAMS
    DEFAULT_COST_BP = round(_KERNEL_PARAMS.BACKTEST_FEE_ROUNDTRIP * 10000.0, 4)
except Exception:  # noqa: BLE001
    DEFAULT_COST_BP = 15.0



# ---------------------------------------------------------------- 工具适配层

def tool_spec(name: str) -> dict:
    """把「工具名」翻译成「怎么在任意历史日算它」。

    返回：
        kind     → "box" / "factor" / "refuse"
        keep     → 需要多少根预热 K 线
        fn       → (panel_at_D) -> 入选 code 集合
        reason   → 拒绝回测的原因（kind=refuse 时）
    """
    from viewer import factors as F
    from viewer import universe_box as UB

    if name == "box_bottom":
        from viewer import toolbox as _TB          # BOX_POS_MAX 定义在工具箱里，不在 universe_box
        MIN_BARS = int(UB.MIN_BARS)
        POS_MAX = float(_TB.BOX_POS_MAX)

        def _box(panel):
            # 【时间之墙】panel 已按 D 截断，这里看不到 D 之后的任何一根 K 线
            kept = []
            for code, bars in panel.items():
                if len(bars) < MIN_BARS:
                    continue
                m = UB.scan_one(bars)
                if not m:
                    continue
                if m["pos"] > POS_MAX:
                    continue
                if m["box_years"] < 0.5:
                    continue
                if m["dwell_ratio"] < 0.15:
                    continue
                kept.append(code)
            return kept

        return {"kind": "box", "keep": MIN_BARS + 30, "fn": _box}

    if name.startswith("factor:"):
        key = name.split(":", 1)[1]
        if key not in F.FACTORS:
            return {"kind": "refuse",
                    "reason": "未登记的因子：%s（可用：%s）" % (key, ", ".join(F.FACTORS))}
        keep = int(F.MIN_BARS) + 60

        def _fac(panel):
            # factors.evaluate 吃 {code: {n,o,c,h,l,v}}，不碰数据库 —— 天然可时点化
            kept, _dropped, _score, _marks, _note = F.evaluate(panel, key, list(panel.keys()))
            return kept

        return {"kind": "factor", "keep": keep, "fn": _fac}

    if name.startswith("pick:"):
        # ---- 策略模板（pick_*）的时点化 --------------------------------------
        # 这些工具本身只是「查 stock_strategy_picks 名单」，判据在
        # `viewer/strategy_templates.TEMPLATES` 的一条 SQL 里（纯技术指标阈值）。
        # 所以时点化的关键是**复用同一条 SQL 原文**，而不是把这些阈值手抄一遍 ——
        # 手抄版一旦和线上漂移，回测测的就是另一个工具，而且不会报错。
        #
        # 做法：把「只到 D 日为止」的窗口算出指标 → 灌进内存 SQLite →
        # 直接 `execute(tpl["sql"])`。SQL 一个字都没改，口径必然一致。
        import pandas as pd
        from viewer import indicators as IND
        from viewer import strategy_templates as ST

        _alias = {"pick_ma_bull": "ma_bull_array", "pick_vol_break": "vol_breakout",
                  "pick_oversold": "oversold_rebound", "pick_quality": "quality_leaders"}
        tpl_key = _alias.get(name.split(":", 1)[1], name.split(":", 1)[1])
        tpl = next((t for t in ST.TEMPLATES if t["key"] == tpl_key), None)
        if tpl is None:
            return {"kind": "refuse",
                    "reason": "未登记的模板 %s（可用：%s）"
                              % (tpl_key, ", ".join(t["key"] for t in ST.TEMPLATES))}
        if "stock_universe_scan" in tpl["sql"]:
            return {"kind": "refuse",
                    "reason": "模板 %s 依赖 universe_scan 质量分快照，历史没有版本，"
                              "硬测等于把今天的评分搬回过去" % tpl_key}
        keep = 300                      # ma200 + 缓冲；再多对结论没影响，只是更慢
        # 注意这里**不返回 fn**：pick 的算法是「每票算一次全序列指标，再按采样日取行」，
        # 与「每截面重算一遍窗口」的 box/factor 相反。若按截面重算，同一只票会被算
        # 60 遍（实测 5008 票 × 60 截面 ≈ 50 分钟/工具，慢 60 倍）。
        # 所以走 run() 里的 `kind == "pick"` 快路径，见 precompute_pick()。
        return {"kind": "pick", "keep": keep, "sql": tpl["sql"],
                "cols": ["code", "name"] + list(IND.IND_COLS)}

    # ---- 拒绝回测的工具：历史值不存在，硬测就是把今天的信息搬回过去
    refuse = {
        "gate_admit": "依赖当期财务报表快照（fundamentals 单期），无历史版本",
        "reso_ab": "依赖当期共振卡批次（res_stock_card 单期）",
        "kronos_kline": "依赖模型当期输出，历史不可复现",
        "holder_org": "依赖当期股东名册",
        "pillar_hot": "依赖当期支柱批次（pillar_item 单期）",
        "pillar_dip": "依赖当期支柱批次（pillar_item 单期）",
        "pillar_policy": "依赖当期支柱批次（pillar_item 单期）",
        "pillar_self": "依赖当期支柱批次（pillar_item 单期）",
        "avoid_c": "依赖当期公告标签（stock_ann 有历史但缺当时的标记口径，待单独验证）",
    }
    if name in refuse:
        return {"kind": "refuse", "reason": refuse[name]}
    if name.startswith("oss_"):
        return {"kind": "refuse",
                "reason": "oss 系列经指标快照批次入选，请用 tools/router_backtest_oss.py 走流式快照"}
    return {"kind": "refuse", "reason": "未知工具：%s" % name}


# ---------------------------------------------------------------- 数据面板

# 进程内面板记忆化：同一回测 run 里 7 个工具用的是同一只股票池 + 同一 warm_from，
# 逐个工具重复 load_panel 纯属浪费（每工具 ~9s × 6 次 ≈ 54s）。按 (codes, min_date) 缓存，
# 命中直接返回（工具只读不写共享 panel，无副作用）。
_PANEL_CACHE: dict = {}


def load_panel(codes, min_date: str) -> dict:
    """一次读全窗口日线 → {code: {"dates": [...], "bars": [(d,o,c,h,l,v)...]}}。

    `bars` 的列序与 `universe_box.load_bars` 一致：(日期, 开, 收, 高, 低, 量)。
    整段读进来后只在内存里切片 —— 逐日回查数据库会把一次回测变成几万次 SQL。
    """
    key = (tuple(sorted(str(c) for c in codes)), min_date)
    cached = _PANEL_CACHE.get(key)
    if cached is not None:
        print("[panel] cache hit / %d 只 / 最早 %s" % (
            len(cached),
            min((p["dates"][0] for p in cached.values()), default="-")))
        return cached

    from viewer import kbase as _kb

    t0 = time.time()
    raw = _kb.get_base().ohlcv_bulk([str(c) for c in codes], min_date=min_date)
    panel = {}
    for code, rows in raw.items():
        if not rows:
            continue
        # kbase 返回 (date, o, h, l, c, v) → 转成 (date, o, c, h, l, v)
        bars = [(d, o, c, h, l, v) for (d, o, h, l, c, v) in rows if c]
        if bars:
            panel[str(code)] = {"dates": [b[0] for b in bars], "bars": bars}
    print("[panel] %d 只 / 取数 %.1fs / 最早 %s" % (
        len(panel), time.time() - t0,
        min((p["dates"][0] for p in panel.values()), default="-")))
    _PANEL_CACHE[key] = panel
    return panel


def calendar_of(panel: dict) -> list:
    """交易日历 = 所有票日期的并集（升序）。用它做 D 的取点，不用系统日期。"""
    days = set()
    for p in panel.values():
        days.update(p["dates"])
    return sorted(days)


# ---------------------------------------------------------------- 收益与统计

# ---------------------------------------------------------------- 主循环

def precompute_pick(panel: dict, sample_days: list, cols: list) -> dict:
    """策略模板（pick_*）专用：**每只票只算一次指标**，按采样日分装到内存库。

    为什么必须这样
        模板判据在 `strategy_templates.TEMPLATES` 的一条 SQL 里，SQL 假设表里
        只有「一行 = 一只票的最新状态」。若按截面重算窗口，同一只票会被
        `compute_indicators` 算 60 遍（5008 票 × 60 截面），实测 50 分钟/工具 ——
        慢 60 倍，且结果完全一样。指标序列本来就是一次算完全程、逐日取行的。

    为什么用 SQLite 而不是 Python dict
        30 万个 dict（60 截 × 5008 票 × 62 字段）要 1.5GB+；
        内存 SQLite 存同样的浮点只要约 150MB，而且能直接执行模板原文 SQL。

    时间之墙：panel 的末行 <= end，每个采样日只取「该日那一行」，
    取不到该日（停牌/未上市）就不给这票造行 —— 绝不用前一日的数据顶替。
    """
    import pandas as pd
    from viewer import indicators as IND

    ph = ",".join("?" * len(cols))
    mems = {d: sqlite3.connect(":memory:") for d in sample_days}
    try:
        for m in mems.values():
            m.execute("CREATE TABLE stock_indicators (%s)"
                      % ",".join('"%s"' % c for c in cols))
        want = set(sample_days)
        idx_cols = [c for c in IND.IND_COLS if c in cols]
        for code, entry in panel.items():
            bars = entry["bars"]
            if len(bars) < 260:
                continue
            df = pd.DataFrame({
                "date": [b[0] for b in bars], "code": code,
                "open": [b[1] for b in bars], "high": [b[3] for b in bars],
                "low": [b[4] for b in bars], "close": [b[2] for b in bars],
                "volume": [b[5] for b in bars],
            })
            try:
                ind = IND.compute_indicators(df)
            except Exception:      # noqa: BLE001  单票算失败不该毁掉整个回测
                continue
            if ind.empty:
                continue
            arr = ind[idx_cols].to_numpy(dtype=float, na_value=float("nan"))
            for i, d in enumerate(ind["date"].tolist()):
                if d not in want:
                    continue
                vals = arr[i]
                rec = {"code": code, "name": code}
                for j, c in enumerate(idx_cols):
                    v = vals[j]
                    rec[c] = None if v != v else float(v)      # v != v 即 NaN
                mems[d].execute(
                    "INSERT INTO stock_indicators VALUES (%s)" % ph,
                    [rec.get(c) for c in cols])
        return mems
    except Exception:
        for m in mems.values():
            try:
                m.close()
            except Exception:
                pass
        raise


def pick_select(mem, sql: str) -> list:
    """在一个采样日的指标表上执行模板原文 SQL。"""
    try:
        return [str(x[0]) for x in mem.execute(sql)]
    except Exception:  # noqa: BLE001
        return []


def log_decisions(tool: str, horizon: int, rows: list, cost: float) -> dict:
    """把回测的「历史决策 + 到期收益」写进 T08 账本，让到期打分立刻有样本。

    为什么这一步是 T08 的关键
        `tool_decision_log` / `tool_outcome` 建了表但一直是空的 —— 线上决策要等
        N 个交易日后才有到期收益，一个样本都得等一个月。而回测本来就把
        「哪一天选了谁、之后 N 天赚了多少」全算出来了，直接落库即可。
        这样账本第一时间就有几万条样本，可以按（工具 × 行情 × 持有期）算胜率/IC。

    两条不许混
        ① `status='BT'`：回测样本与线上真实决策必须可区分。混在一起统计，
           等于用「事后算出来的决策」冒充「当时真的做了的决定」。
        ② `realized_date` 记**实际取到收益的那一天**，不是「D + N 天」推算值。
           停牌/退市会让实际日偏移，账本写推算值以后对账会对不上。
    """
    from viewer import tool_router as TR

    con = sqlite3.connect(os.path.join(ROOT, "viewer", "app.db"))
    try:
        TR.ensure_tables(con)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        nd = no = 0
        for r in rows:
            did = "BT:%s:%s:T%d" % (tool, r["date"], horizon)
            con.execute(
                "INSERT OR REPLACE INTO tool_decision_log"
                "(decision_id,as_of,context_hash,market_regime,objective,horizon_days,"
                " stock_code,router_version,contract_version,chain_json,candidate_ids_json,"
                " rejected_json,eligible_json,final_count,status,abstain_reason,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, r["date"], "", "", "backtest", horizon, "",
                 "router-backtest", "tool-contract.v2",
                 json.dumps([tool], ensure_ascii=False),
                 json.dumps([c for c, _g, _d in r["codes"]], ensure_ascii=False),
                 "[]", "[]", len(r["codes"]), "BT", "", now))
            nd += 1
            for code, gross, rdate in r["codes"]:
                bench = r["base"]
                net = gross - bench - cost
                con.execute(
                    "INSERT OR REPLACE INTO tool_outcome"
                    "(decision_id,code,horizon_days,due_date,realized_date,gross_return,"
                    " benchmark_return,net_excess_return,max_drawdown,hit,status,scored_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, code, horizon, rdate, rdate, round(gross, 6),
                     round(bench, 6), round(net, 6), None, 1 if net > 0 else 0, "scored", now))
                no += 1
        con.commit()
        return {"decisions": nd, "outcomes": no}
    finally:
        con.close()

def run(tool: str, start: str, end: str, step: int, pool: int,
        horizon: int, cost_bp: float, warm_days: int, seed: int = 20261106,
        log_decisions_flag: bool = False) -> dict:
    spec = tool_spec(tool)
    if spec["kind"] == "refuse":
        return {"ok": False, "tool": tool, "error": spec["reason"],
                "hint": "该工具无法时点化回测；需要先把判据改造成「只读历史窗口」的纯函数"}

    # ---- 定池
    from viewer import universe_filter as UF
    universe = [str(c) for c in (UF.evaluation_universe() or [])]
    if not universe:
        return {"ok": False, "tool": tool, "error": "干净宇宙为空，无法回测"}
    if pool and pool > 0 and pool < len(universe):
        rnd = random.Random(seed)                 # 固定种子：同一命令跑两次结果必须一样
        codes = sorted(rnd.sample(universe, pool))
    else:
        codes = universe

    # ---- 预热：start 之前还要 warm_days 根 K 线给判据用（750 根 ≈ 3 年）
    #   日历必须用**全市场**交易日历（`kbase.distinct_dates()`），
    #   不能拿单只票的日期序列推 —— 新股上市晚，会把预热起点算到 2024 年去，
    #   于是每只票到 D 日都不足 750 根，判据全部"数据不足"，回测静默输出 0 入选。
    from viewer import kbase as _kb
    cal_all = _kb.get_base().distinct_dates() or []
    if not cal_all:
        return {"ok": False, "tool": tool, "error": "交易日历为空"}
    i_start = bisect.bisect_left(cal_all, start)
    warm_from = cal_all[max(0, i_start - warm_days)] if i_start else cal_all[0]
    panel = load_panel(codes, warm_from)
    if not panel:
        return {"ok": False, "tool": tool, "error": "取不到日线"}

    days = [d for d in calendar_of(panel) if start <= d <= end]
    if not days:
        return {"ok": False, "tool": tool, "error": "区间内无交易日"}
    sample_days = days[::max(1, step)]            # 每 step 个交易日取一个截面

    print("[run] 工具=%s 池=%d 区间=%s~%s 截面=%d（每 %d 日） 持有=%d 成本=%.0fbp"
          % (tool, len(panel), start, end, len(sample_days), step, horizon, cost_bp))
    cost = cost_bp / 10000.0

    # pick 系列走「每票算一次」的快路径（见 precompute_pick 的说明）
    pick_mems = None
    if spec["kind"] == "pick":
        print("[pick] 预计算指标（每票一次，共 %d 只 × %d 个采样日）…"
              % (len(panel), len(sample_days)), flush=True)
        _t = time.time()
        pick_mems = precompute_pick(panel, sample_days, spec["cols"])
        print("[pick] 预计算完成 %.1fs" % (time.time() - _t), flush=True)

    records = []          # 每个截面一条：日期 / 入选数 / 入选均值 / 池均值 / 超额
    log_rows = []         # T08 账本：每个截面的入选明细（仅 --log-decisions 时收集）
    t0 = time.time()
    for di, D in enumerate(sample_days):
        at_D = {}
        idx_of = {}
        for code, entry in panel.items():
            w, i = window_at(entry, D, spec["keep"])          # 【时间之墙】唯一取数口
            if w is None:
                continue
            at_D[code] = w
            idx_of[code] = i
        if len(at_D) < 30:
            continue

        if spec["kind"] == "pick":
            mem = (pick_mems or {}).get(D)
            kept = pick_select(mem, spec["sql"]) if mem is not None else []
        elif spec["kind"] == "factor":
            # factor 要 {n,o,c,h,l,v} 的 numpy 结构，这里就地转换（只转截断后的窗口）
            import numpy as np
            shaped = {}
            for code, bars in at_D.items():
                a = np.asarray([b[1:6] for b in bars], dtype=float)
                shaped[code] = {"n": len(bars), "o": a[:, 0], "c": a[:, 1],
                                "h": a[:, 2], "l": a[:, 3], "v": a[:, 4]}
            kept = spec["fn"](shaped)
        else:
            kept = spec["fn"](at_D)

        # 同池基准：只用「当日有前向收益」的票，保证分子分母同一批人
        keptset = set(kept)
        per = []          # [(code, 前向收益, 规模代理)]
        for code in at_D:
            r = fwd_return_open(panel[code], idx_of[code], horizon)
            if r is None:
                continue
            per.append((code, r, liq_at(at_D[code])))
        if len(per) < 30:
            continue
        ret_all = [x[1] for x in per]
        ret_kept = [x[1] for x in per if x[0] in keptset]
        base = statistics.fmean(ret_all)
        if ret_kept:
            gross = statistics.fmean(ret_kept)
            excess = gross - base - cost          # ★ 成本先扣，不给两套结论
        else:
            gross, excess = None, None

        # ── 规模中性基准（2026-11-06 补）────────────────────────────────
        # 全池等权基准回答「它比池子平均好吗」，但池子平均里混着规模风格：
        # 如果工具专挑小盘票，而这一区间小盘跑赢，超额就是风格给的，不是工具给的。
        # 所以再算一份「与其所属规模档比」的超额，两者对照才知道负超额是真失效还是错配。
        liq_pairs = [(x[2], x[1]) for x in per if x[2]]
        bounds = _quintile_bounds(liq_pairs)
        base_liq = None
        liq_pct_kept = None
        if bounds:
            gret = {}
            for x, r in liq_pairs:
                gret.setdefault(bisect.bisect(bounds, x), []).append(r)
            gbase = {g: statistics.fmean(v) for g, v in gret.items()}
            ks = []
            for code, r, x in per:
                if x is None or code not in keptset:
                    continue
                g = bisect.bisect(bounds, x)
                if g in gbase:
                    ks.append(gbase[g])
            if ks:
                base_liq = statistics.fmean(ks)
            sorted_liq = sorted(x for x, _ in liq_pairs)
            kept_pct = [bisect.bisect_right(sorted_liq, x) / len(sorted_liq)
                        for code, r, x in per if x and code in keptset]
            if kept_pct:
                liq_pct_kept = statistics.median(kept_pct)   # 0=最小盘 1=最大
        excess_liq = (gross - base_liq - cost) if (gross is not None and base_liq is not None) else None

        records.append({"date": D, "n_pool": len(ret_all), "n_kept": len(ret_kept),
                        "base": base, "gross": gross, "excess": excess,
                        "base_liq": base_liq, "excess_liq": excess_liq,
                        "liq_pct_kept": liq_pct_kept})
        if log_decisions_flag and ret_kept:
            det = []
            for code, r, _x in per:
                if code not in keptset:
                    continue
                _g, rdate = fwd_detail_open(panel[code], idx_of[code], horizon)
                det.append((code, r, rdate or ""))
            log_rows.append({"date": D, "codes": det, "base": base})
        if (di + 1) % 20 == 0:
            print("   ... %d/%d 截面  累计入选 %d" % (
                di + 1, len(sample_days), sum(r["n_kept"] for r in records)))

    if pick_mems:
        for m in pick_mems.values():
            try:
                m.close()
            except Exception:  # noqa: BLE001
                pass

    if not records:
        return {"ok": False, "tool": tool, "error": "没有可用截面（数据不足或区间太短）"}

    def agg(rs):
        ex = [r["excess"] for r in rs if r["excess"] is not None]
        exl = [r["excess_liq"] for r in rs if r.get("excess_liq") is not None]
        lp = [r["liq_pct_kept"] for r in rs if r.get("liq_pct_kept") is not None]
        keep_rate = (sum(r["n_kept"] for r in rs) / max(1, sum(r["n_pool"] for r in rs)))
        return {
            "sections": len(rs),
            "avg_kept": round(statistics.fmean([r["n_kept"] for r in rs]), 1),
            "pct_kept": round(keep_rate * 100, 2),          # 入选率（%）
            "excess_pp": round(statistics.fmean(ex) * 100, 3) if ex else None,
            "win_rate": round(100.0 * sum(1 for x in ex if x > 0) / len(ex), 1) if ex else None,
            "t": round(_tstat(ex), 2) if _tstat(ex) is not None else None,
            # 规模中性：与「同规模档」比，而不是与全池比
            "excess_pp_liq": round(statistics.fmean(exl) * 100, 3) if exl else None,
            "t_liq": round(_tstat(exl), 2) if _tstat(exl) is not None else None,
            "liq_pct_kept": round(statistics.median(lp), 3) if lp else None,
        }

    segs = slices(sample_days, 6)
    by_seg = []
    for seg in segs:
        rs = [r for r in records if seg[0] <= r["date"] <= seg[-1]]
        if rs:
            a = agg(rs)
            a["from"], a["to"] = seg[0], seg[-1]
            by_seg.append(a)

    overall = agg(records)

    def _style(ov: dict) -> dict:
        """把「全池超额」与「同规模档超额」的差，说成一句可执行的结论。

        这是中性化唯一有价值的产出：区分「工具本身失效」和「工具只是买错了风格」。
        两者对产品的处置完全不同 —— 前者该降级证据，后者该改基准而不是改工具。
        """
        p, ex, exl = ov.get("liq_pct_kept"), ov.get("excess_pp"), ov.get("excess_pp_liq")
        if p is None:
            tilt = "规模倾斜未知（成交额数据不足）"
        elif p < 0.40:
            tilt = "偏小规模（成交额分位中位数 %.2f，全池应为 0.50）" % p
        elif p > 0.60:
            tilt = "偏大规模（成交额分位中位数 %.2f）" % p
        else:
            tilt = "规模中性（成交额分位中位数 %.2f）" % p
        if ex is None or exl is None:
            concl = "规模中性基准不可用（截面或成交额不足），本条不下结论"
        elif ex < 0 and exl < 0:
            concl = ("剔除规模风格后仍为负（%+.2f → %+.2fpp）：负超额来自工具本身，"
                     "不是风格错配" % (ex, exl))
        elif ex < 0 <= exl:
            concl = ("剔除规模风格后转正（%+.2f → %+.2fpp）：负超额主要是规模错配，"
                     "工具本身不算失效" % (ex, exl))
        elif ex >= 0 > exl:
            concl = ("剔除规模风格后转负（%+.2f → %+.2fpp）：原来的正超额是规模风格给的，"
                     "不能算工具的能力" % (ex, exl))
        else:
            concl = "规模中性前后同向（%+.2f → %+.2fpp）" % (ex, exl)
        return {"liq_pct_kept": p, "tilt": tilt, "conclusion": concl}

    out = {
        "ok": True,
        "tool": tool,
        "kind": spec["kind"],
        "params": {"start": start, "end": end, "step": step, "pool": pool,
                   "horizon": horizon, "cost_bp": cost_bp, "seed": seed},
        "pool_size": len(panel),
        "overall": overall,
        "by_segment": by_seg,
        "style": _style(overall),
        "elapsed_sec": round(time.time() - t0, 1),
        "caveats": [
            "幸存者偏差：kbase 只有现存票的历史，退市票缺失；2021-2023 段覆盖 2700~3200 只，"
            "该段超额系统性偏乐观，请看 by_segment 的 pct_kept 与段区间自行打折",
            "超额基准 = 同一时点、同一批有前向收益的票（不是指数）",
            "excess_pp_liq 是**同成交额五档**内的超额，不是市值中性 —— "
            "kbase 存后复权价且股本只有最新快照，算不出历史市值；成交额只是规模/流动性代理",
            "已扣 %.0f bp 双边成本" % cost_bp,
            "截面每 %d 个交易日取一个，非逐日" % step,
        ],
    }
    if log_decisions_flag and log_rows:
        out["logged"] = log_decisions(tool, horizon, log_rows, cost)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="工具时点回测（T10）")
    ap.add_argument("--tool", required=True, help="box_bottom / factor:tv_wbottom ...")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-11-06")
    ap.add_argument("--step", type=int, default=5, help="每 N 个交易日取一个截面")
    ap.add_argument("--pool", default="300", help="抽样票数，或 all")
    ap.add_argument("--horizon", type=int, default=20, help="持有 N 个交易日")
    ap.add_argument("--cost-bp", type=float, default=DEFAULT_COST_BP)
    ap.add_argument("--warm-days", type=int, default=800, help="预热 K 线根数（>=750）")
    ap.add_argument("--seed", type=int, default=20261106)
    ap.add_argument("--log-decisions", action="store_true",
                    help="把每个截面的历史决策与到期收益写进 T08 账本"
                         "（tool_decision_log / tool_outcome，status='BT'）")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    pool = 0 if str(a.pool).lower() == "all" else int(a.pool)
    r = run(a.tool, a.start, a.end, a.step, pool, a.horizon, a.cost_bp, a.warm_days, a.seed,
            log_decisions_flag=a.log_decisions)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print("[out] ->", a.out)
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
