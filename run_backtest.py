# -*- coding: utf-8 -*-
"""全市场底座上的历史回测（独立运行版，双口径）。

背景：进哥要求"K 线底座必须是全市场级（5000+ 只），且做出来之后自己搞一个回测"。
本脚本在已夯实的 viewer/kbase.db（5237 只全 A 股日 K 沉淀）上，对「四支柱/六支柱」
预测引擎（pillar_item 表）做到期窗口复盘，并按两种入场口径并列输出：

    口径 A（乐观）：_window_ret  —— T 日收盘买入 → T+w 收盘卖出（原复盘口径）
    口径 B（真实）：_window_ret2 —— T+1 开盘买入 + 双边滑点 0.1% → T+w 收盘卖出
                     （与 screener_extra.SLIPPAGE_RATE=0.001 对齐）

口径 100% 复用 predictor 的复盘实现（_window_ret / _window_ret2 / _bars_index），
绝不另写公式 —— 从根上防止口径漂移。基准同步切口径：
    候选池基准 bench_pool(mode) = SS.universe(POOL_LIMIT) 全部票同 mode 收益均值
    全市场基准 bench_mkt(mode)  = 全市场等权；口径 B 用次日开盘价入场（同扣滑点）

内存纪律：K 线按只惰性读取（kbase.bars_of），绝不全量载入；候选池仅 ~160 只、
全市场基准走单条 SQL（命中 idx_kline_date 索引），符合"内存最小"要求。

诚实原则：未到期周期一律记为 pending（不计入样本、不填 0、不假装已回测）；
预测日无 K 线记为 nodata（数据缺口，单独计数）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

from viewer import kbase
import predictor as P

PERIODS = (1, 3, 5, 10, 20)
DB = os.path.join(ROOT, "viewer", "app.db")
_SLIP = 0.001  # 与 predictor._window_ret2 / screener_extra.SLIPPAGE_RATE 一致


def _cell():
    return {"rets": [], "alphas": [], "hits": 0, "beats": 0,
            "best": None, "worst": None, "n_ok": 0, "n_pending": 0, "n_nodata": 0}


def main() -> int:
    base = kbase.get_base()
    cal = base.distinct_dates()
    date_pos = {d: i for i, d in enumerate(cal)}
    asof = base.latest_date()
    print(f"[run_backtest] kbase 截至 {asof}，交易日历 {len(cal)} 天，全市场 {base.count()} 只")

    # ---- 候选池基准（与引擎同口径，离线从 app.db 取池） ----
    codes, _names, _boards = P.SS.universe(P.POOL_LIMIT)
    pool_bars = {c: base.bars_of(c) for c in codes}
    pool_curves = {c: P._bars_index(v) for c, v in pool_bars.items() if len(v) >= 30}
    print(f"[run_backtest] 候选池 {len(pool_curves)} 只（用于 bench_pool）")

    # ---- 读取预测记录 ----
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    items = con.execute(
        "SELECT symbol, name, pillar, rank, score, run_date, reason "
        "FROM pillar_item ORDER BY run_date, pillar, rank"
    ).fetchall()
    con.close()
    if not items:
        print("[run_backtest] 无 pillar_item 记录，无法回测")
        return 1
    print(f"[run_backtest] 载入预测记录 {len(items)} 条，"
          f"覆盖日期 {items[0]['run_date']}→{items[-1]['run_date']}")

    # 预取每个预测日的双口径基准（pool 用复盘函数；mkt 用全市场 SQL）
    def bench_pool_at(rd: str) -> dict:
        out = {}
        for w in PERIODS:
            r1 = [P._window_ret(pool_bars[c], pool_curves[c].get(rd), w)
                  for c in pool_curves]
            r2 = [P._window_ret2(pool_bars[c], pool_curves[c].get(rd), w)
                  for c in pool_curves]
            r1 = [x for x in r1 if x is not None]
            r2 = [x for x in r2 if x is not None]
            out[w] = {"A": (sum(r1) / len(r1)) if r1 else None,
                      "B": (sum(r2) / len(r2)) if r2 else None}
        return out

    def bench_mkt_at(rd: str) -> dict:
        out = {}
        pos = date_pos.get(rd)
        if pos is None:
            return {w: {"A": None, "B": None} for w in PERIODS}
        for w in PERIODS:
            tgt = cal[pos + w] if pos + w < len(cal) else None
            if not tgt:
                out[w] = {"A": None, "B": None}
                continue
            # 口径 A：rd 收盘 → tgt 收盘
            c = sqlite3.connect(f"file:{kbase.DB}?mode=ro", uri=True)
            m = {}
            for code, date, close in c.execute(
                    "SELECT code, date, c FROM kline WHERE date IN (?,?)", (rd, tgt)):
                m.setdefault(code, {})[date] = close
            rets_a = [dd[tgt] / dd[rd] - 1.0
                      for dd in m.values() if rd in dd and tgt in dd and dd[rd]]
            # 口径 B：次日开盘(扣滑点买) → tgt 收盘(扣滑点卖)
            entry = cal[pos + 1] if pos + 1 < len(cal) else None
            rets_b = []
            if entry and entry <= tgt:
                m2 = {}
                for code, date, cc, oo in c.execute(
                        "SELECT code, date, c, o FROM kline WHERE date IN (?,?)",
                        (entry, tgt)):
                    m2.setdefault(code, {})[date] = (cc, oo)
                for dd in m2.values():
                    if entry in dd and tgt in dd:
                        o1, c1 = dd[entry][1], dd[tgt][0]
                        if o1 and c1 is not None:
                            rets_b.append((c1 * (1 - _SLIP)) / (o1 * (1 + _SLIP)) - 1.0)
            c.close()
            out[w] = {"A": (sum(rets_a) / len(rets_a)) if rets_a else None,
                      "B": (sum(rets_b) / len(rets_b)) if rets_b else None}
        return out

    # ---- 逐条复盘（双口径独立计数） ----
    agg = defaultdict(lambda: defaultdict(_cell))    # 口径 A
    agg2 = defaultdict(lambda: defaultdict(_cell))   # 口径 B
    all_pillars = set()

    # 同一预测日被大量记录共享，按 rd 缓存基准，避免重复计算
    _bp_cache, _bm_cache = {}, {}
    for it in items:
        sym = it["symbol"]
        rd = it["run_date"]
        pl = it["pillar"]
        all_pillars.add(pl)
        k = base.bars_of(sym)
        idx = P._bars_index(k).get(rd)
        bp = _bp_cache.get(rd) or _bp_cache.setdefault(rd, bench_pool_at(rd))
        bm = _bm_cache.get(rd) or _bm_cache.setdefault(rd, bench_mkt_at(rd))
        for w in PERIODS:
            # 口径 A
            cell = agg[pl][w]
            if idx is None:
                cell["n_nodata"] += 1
                agg2[pl][w]["n_nodata"] += 1
                continue
            r1 = P._window_ret(k, idx, w)
            r2 = P._window_ret2(k, idx, w)
            for cell2, r, bp2, bm2 in (
                    (cell, r1, bp[w]["A"], bm[w]["A"]),
                    (agg2[pl][w], r2, bp[w]["B"], bm[w]["B"])):
                if r is None:
                    cell2["n_pending"] += 1
                    continue
                cell2["n_ok"] += 1
                cell2["rets"].append(r)
                if bp2 is not None:
                    cell2["alphas"].append(r - bp2)
                    if r > bp2:
                        cell2["hits"] += 1
                if bm2 is not None and r > bm2:
                    cell2["beats"] += 1
                if cell2["best"] is None or r > cell2["best"]["ret"]:
                    cell2["best"] = {"symbol": sym, "name": it["name"], "ret": r}
                if cell2["worst"] is None or r < cell2["worst"]["ret"]:
                    cell2["worst"] = {"symbol": sym, "name": it["name"], "ret": r}

    # ---- 汇总输出 ----
    def pct(x):
        return "%.1f%%" % (x * 100)

    def _b(v):
        return (f"{v['symbol']}({('+' if v['ret'] >= 0 else '') + pct(v['ret'])})"
                if v else "—")

    lines = []
    lines.append("# 全市场底座历史回测报告（双口径）")
    lines.append("")
    lines.append(f"- 数据截至：{asof}（kbase 全市场 {base.count()} 只日 K）")
    lines.append(f"- 预测来源：pillar_item（四支柱/六支柱引擎），共 {len(items)} 条，"
                 f"{items[0]['run_date']}→{items[-1]['run_date']}")
    lines.append(f"- 候选池基准：SS.universe(POOL_LIMIT={P.POOL_LIMIT})，{len(pool_curves)} 只")
    lines.append(f"- 全市场基准：kbase 全部票等权（日历口径近似）")
    lines.append(f"- 回测周期：T+{ ' / T+'.join(map(str,PERIODS)) }")
    lines.append("")
    lines.append("**双口径定义**（收益函数 100% 复用 predictor，未到期记 pending 不填 0）：")
    lines.append("")
    lines.append("- **口径 A（乐观）**：`_window_ret` —— T 日收盘买入 → T+w 收盘卖出。"
                 "相当于\"看到信号当天收盘就能买到\"，偏乐观。")
    lines.append(f"- **口径 B（真实）**：`_window_ret2` —— T+1 开盘买入 → T+w 收盘卖出，"
                 f"双边滑点各 {_SLIP:.1%}（与 screener_extra.SLIPPAGE_RATE 对齐）。"
                 "T+1 无开盘价（停牌等）退化为口径 A。")
    lines.append("")

    for mode, aggp, mlabel in (("A", agg, "口径 A（T 收盘入场，乐观）"),
                               ("B", agg2, "口径 B（T+1 开盘 + 双边滑点，真实）")):
        lines.append(f"## {mlabel}")
        lines.append("")
        for pl in sorted(all_pillars):
            lines.append(f"### 支柱：{pl}")
            lines.append("")
            lines.append("| 周期 | 样本 | 待到期 | 无K线 | 命中率(对池) | 平均收益 | 平均超额 | 跑赢大盘 | 最佳 | 最差 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for w in PERIODS:
                c = aggp[pl][w]
                n = c["n_ok"]
                hr = (c["hits"] / n) if n else None
                ar = (sum(c["rets"]) / n) if n else None
                aa = (sum(c["alphas"]) / len(c["alphas"])) if c["alphas"] else None
                bm_rate = (c["beats"] / n) if n else None
                lines.append(
                    f"| T+{w} | {n} | {c['n_pending']} | {c['n_nodata']} | "
                    f"{pct(hr) if hr is not None else '—'} | "
                    f"{('+' if ar is not None and ar >= 0 else '') + pct(ar) if ar is not None else '—'} | "
                    f"{('+' if aa is not None and aa >= 0 else '') + pct(aa) if aa is not None else '—'} | "
                    f"{pct(bm_rate) if bm_rate is not None else '—'} | "
                    f"{_b(c['best'])} | {_b(c['worst'])} |"
                )
            lines.append("")

        # 全量汇总
        lines.append(f"### {mlabel} —— 全量汇总（所有支柱合并）")
        lines.append("")
        lines.append("| 周期 | 样本 | 待到期 | 无K线 | 命中率(对池) | 平均收益 | 平均超额 | 跑赢大盘 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for w in PERIODS:
            rets, alphas, hits, beats, n_ok, n_pend, n_nod = [], [], 0, 0, 0, 0, 0
            for pl in all_pillars:
                c = aggp[pl][w]
                n_ok += c["n_ok"]; n_pend += c["n_pending"]; n_nod += c["n_nodata"]
                hits += c["hits"]; beats += c["beats"]
                rets += c["rets"]; alphas += c["alphas"]
            n = n_ok
            hr = (hits / n) if n else None
            ar = (sum(rets) / n) if n else None
            aa = (sum(alphas) / len(alphas)) if alphas else None
            bm_rate = (beats / n) if n else None
            lines.append(
                f"| T+{w} | {n} | {n_pend} | {n_nod} | "
                f"{pct(hr) if hr is not None else '—'} | "
                f"{('+' if ar is not None and ar >= 0 else '') + pct(ar) if ar is not None else '—'} | "
                f"{('+' if aa is not None and aa >= 0 else '') + pct(aa) if aa is not None else '—'} | "
                f"{pct(bm_rate) if bm_rate is not None else '—'} |"
            )
        lines.append("")

    # ---- 双口径对比（仅到期的 T+1/T+3/T+5 有意义） ----
    lines.append("## 双口径对比（同一批样本，乐观 vs 真实）")
    lines.append("")
    lines.append("| 支柱 | 周期 | A 命中率 | B 命中率 | A 均收 | B 均收 | B−A 差值 |")
    lines.append("|---|---|---|---|---|---|---|")
    for pl in sorted(all_pillars):
        for w in (1, 3, 5):
            ca, cb = agg[pl][w], agg2[pl][w]
            if not ca["n_ok"] and not cb["n_ok"]:
                continue
            ha = (ca["hits"] / ca["n_ok"]) if ca["n_ok"] else None
            hb = (cb["hits"] / cb["n_ok"]) if cb["n_ok"] else None
            aa_ = (sum(ca["rets"]) / ca["n_ok"]) if ca["n_ok"] else None
            ab = (sum(cb["rets"]) / cb["n_ok"]) if cb["n_ok"] else None
            drag = (ab - aa_) if (aa_ is not None and ab is not None) else None
            lines.append(
                f"| {pl} | T+{w} | "
                f"{pct(ha) if ha is not None else '—'} | "
                f"{pct(hb) if hb is not None else '—'} | "
                f"{('+' if aa_ is not None and aa_ >= 0 else '') + pct(aa_) if aa_ is not None else '—'} | "
                f"{('+' if ab is not None and ab >= 0 else '') + pct(ab) if ab is not None else '—'} | "
                f"{('+' if drag is not None and drag >= 0 else '') + pct(drag) if drag is not None else '—'} |"
            )
    lines.append("")
    lines.append("> 差值 = 口径 B 均收 − 口径 A 均收（同批样本）。B 比 A 多付双边滑点"
                 f"（约 −{2*_SLIP:.1%}），但 T+1 开盘价可能低于或高于 T 收盘，故差值可正可负：")
    lines.append(">")
    lines.append("> - 差值为正：T+1 开盘普遍比 T 收盘低（跳空低开入场更便宜），利好 B；")
    lines.append("> - 差值为负：T+1 高开或一字板追高，B 吃亏。"
                 "若某支柱 A 下为正、B 下翻负，说明其优势吃的是入场时点的乐观假设。")
    lines.append("")

    report = "\n".join(lines)
    out_path = os.path.join(ROOT, "backtest_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + report)
    print(f"\n[run_backtest] 报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
