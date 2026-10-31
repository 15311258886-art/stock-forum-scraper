# -*- coding: utf-8 -*-
"""龙虎榜 / 减持公告 —— 细分实证（2026-10-30 夜，进哥全权委托）

背景：
  · 上一轮 sig_unified 显示「龙虎榜·日换手 T+5 超额 -8.34% t=-6.50」，但只有 57 条样本。
    现 lhb_stocks 已有 1899 条 / 25 个交易日，值得用更大样本重测，并且拆细：
    上榜理由（涨/跌/换手/振幅/累计涨幅）、净买卖额方向、换手率档位。
  · stock_ann 把减持公告全打了 cat='股东减持'，但里面混着三个完全不同的阶段：
      预披露/减持计划（最早，股东"打算卖"）→ 进展/触及1%（"正在卖"）→ 结果/届满（"已经卖完"）
    这三者对未来的预测力不可能一样，必须拆开。结果公告是滞后的，把它当风险信号是错的。

口径（重要，与之前所有探针一致）：
  · 事件日 d = 上榜日 / 公告日。d 当天收盘后才知情，**d 日收盘价不可用于决策**。
    故一律用「次日收盘 → 其后第 5 个交易日收盘」= close[i+1] → close[i+6] 作为可执行收益。
  · 超额 = 个股收益 − 同日全市场等权均值（demean）。
  · t 值按「日聚合」后再检验（同日几千样本不能当独立样本）。

用法：python tools/probe_lhb_ann.py
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
import os

DB = os.environ.get("VIEWER_DB") or "viewer/app.db"
H = 5          # 前向 5 个交易日
MIN_DAY_N = 100  # 单日全市场样本少于该数则该日不做基准


def tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    return m / (sd / n ** 0.5) if sd else None


def ft(t):
    return "%.2f" % t if t is not None else "-"


def norm_reason(r):
    """把中英文两套写法归一成 6 类。"""
    r = r or ""
    if "退市整理" in r:
        return "退市整理"
    if "跌幅偏离" in r:
        return "跌幅偏离(大跌上榜)"
    if "振幅" in r:
        return "振幅15%(剧烈波动)"
    if "涨幅" in r and ("30%" in r):
        return "累计涨幅30%(连续爆炒)"
    if "涨幅" in r and "累计" in r:
        return "累计涨幅20%(连续上涨)"
    if "涨幅" in r:
        return "单日涨幅7-15%(暴涨上榜)"
    if "换手率" in r and "30%" in r:
        return "换手率>30%(极度换手)"
    if "换手率" in r:
        return "换手率>20%(高换手)"
    return "其他"


def build_fwd(con):
    """返回 (cal, fwd 可执行 5 日收益 dict[(code,date)], day_mean dict[date])"""
    cal = [r[0] for r in con.execute("SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
    idx_of = {d: i for i, d in enumerate(cal)}
    px = defaultdict(dict)
    for code, d, c in con.execute(
            "SELECT code, date, close FROM stock_indicators_daily WHERE close>0"):
        px[code][d] = c

    fwd = {}
    day_acc = defaultdict(list)
    n = len(cal)
    for code, dm in px.items():
        for i in range(n - H - 1):
            d0, d1, d2 = cal[i], cal[i + 1], cal[i + 1 + H]
            c1 = dm.get(d1)
            c2 = dm.get(d2)
            if not c1 or not c2:
                continue
            v = (c2 / c1 - 1.0) * 100.0
            fwd[(code, d0)] = v
            day_acc[d0].append(v)
    day_mean = {d: statistics.fmean(v) for d, v in day_acc.items() if len(v) >= MIN_DAY_N}
    return cal, idx_of, fwd, day_mean


def report(title, groups, day_mean, fwd, cal, idx_of, note=""):
    """groups: list of (label, [(code, date), ...])"""
    print("\n" + "=" * 100)
    print(title)
    if note:
        print("  " + note)
    print("=" * 100)
    print("%-30s %5s %5s %9s %8s %8s  %s" % ("分组", "n", "日数", "超额%", "t", "胜率", "判定"))
    print("-" * 100)
    for label, evs in groups:
        per_day = defaultdict(list)
        allv = []
        for code, d in evs:
            if d not in day_mean:
                continue
            f = fwd.get((code, d))
            if f is None:
                continue
            ex = f - day_mean[d]
            per_day[d].append(ex)
            allv.append(ex)
        if len(allv) < 12:
            print("%-30s %5d  样本不足" % (label, len(allv)))
            continue
        dv = [statistics.fmean(v) for v in per_day.values() if v]
        t = tstat(dv)
        mean = statistics.fmean(allv)
        win = 100.0 * sum(1 for v in allv if v > 0) / len(allv)
        verdict = "显著负" if (t is not None and t <= -2.0) else ("显著正" if (t is not None and t >= 2.0) else "不显著")
        print("%-30s %5d %5d %+8.2f%% %8s %7.1f%%  %s"
              % (label, len(allv), len(dv), mean, ft(t), win, verdict))
    return


def main():
    con = sqlite3.connect(DB)
    print("[载入] 构建可执行前向收益（次日→T+5）…", flush=True)
    cal, idx_of, fwd, day_mean = build_fwd(con)
    print("       交易日 %s .. %s（%d 日），可用基准日 %d 个" % (cal[0], cal[-1], len(cal), len(day_mean)))

    # ================= ① 龙虎榜 =================
    rows = list(con.execute(
        "SELECT trade_date, security_code, reason, turnoverrate, net_amt, change_rate, deal_amt "
        "FROM lhb_stocks WHERE trade_date>='2026-09-16'"))
    print("[龙虎榜] 原始 %d 条" % len(rows))
    # 同一票同日可能因多个理由重复上榜 → 去重，理由取"最严重"的一个
    by_key = {}
    for d, code, reason, tr, net, chg, deal in rows:
        k = (code, d)
        r = norm_reason(reason)
        if k not in by_key:
            by_key[k] = {"reasons": set(), "tr": tr, "net": net, "chg": chg}
        by_key[k]["reasons"].add(r)
    print("       去重后 %d 个 (股票,日期) 事件" % len(by_key))

    PRIO = ["退市整理", "换手率>30%(极度换手)", "换手率>20%(高换手)", "累计涨幅30%(连续爆炒)",
            "累计涨幅20%(连续上涨)", "单日涨幅7-15%(暴涨上榜)", "振幅15%(剧烈波动)",
            "跌幅偏离(大跌上榜)", "其他"]
    def primary(rs):
        for p in PRIO:
            if p in rs:
                return p
        return "其他"

    g_reason = defaultdict(list)
    g_net = defaultdict(list)
    g_tr = defaultdict(list)
    g_chg = defaultdict(list)
    all_ev = []
    for (code, d), v in by_key.items():
        all_ev.append((code, d))
        g_reason[primary(v["reasons"])].append((code, d))
        # 净买卖：正=上榜资金净买，负=净卖
        try:
            net = float(v["net"] or 0)
        except Exception:
            net = 0.0
        g_net["净买入>0" if net > 0 else "净卖出≤0"].append((code, d))
        try:
            tr = float(v["tr"] or 0)
        except Exception:
            tr = 0.0
        if tr >= 30:
            g_tr["换手≥30%"].append((code, d))
        elif tr >= 20:
            g_tr["换手20-30%"].append((code, d))
        elif tr >= 10:
            g_tr["换手10-20%"].append((code, d))
        else:
            g_tr["换手<10%"].append((code, d))
        try:
            chg = float(v["chg"] or 0)
        except Exception:
            chg = 0.0
        if chg >= 9.8:
            g_chg["当日涨停/近涨停"].append((code, d))
        elif chg > 0:
            g_chg["当日小涨"].append((code, d))
        else:
            g_chg["当日下跌"].append((code, d))

    report("① 龙虎榜：上榜当日 → 次日可执行 T+5 超额（按上榜理由）",
           sorted(g_reason.items(), key=lambda x: -len(x[1])), day_mean, fwd, cal, idx_of,
           "理由已归一（中英文双写法合并）；同票同日多理由时取优先级最高的一个")
    report("② 龙虎榜：按上榜资金净买卖方向",
           sorted(g_net.items()), day_mean, fwd, cal, idx_of,
           "净买入 = 上榜席位合计买入 > 卖出")
    report("③ 龙虎榜：按当日换手率分档",
           [("换手≥30%", g_tr["换手≥30%"]), ("换手20-30%", g_tr["换手20-30%"]),
            ("换手10-20%", g_tr["换手10-20%"]), ("换手<10%", g_tr["换手<10%"])],
           day_mean, fwd, cal, idx_of)
    report("④ 龙虎榜：按当日涨跌幅",
           [("当日涨停/近涨停", g_chg["当日涨停/近涨停"]), ("当日小涨", g_chg["当日小涨"]),
            ("当日下跌", g_chg["当日下跌"])], day_mean, fwd, cal, idx_of)
    report("⑤ 龙虎榜：全样本基线", [("全部上榜", all_ev)], day_mean, fwd, cal, idx_of,
           "这一行是对照组，各分组的强弱都以它为参照")

    # ================= ⑥ 减持公告分阶段 =================
    ann = list(con.execute(
        "SELECT notice_date, code, title FROM stock_ann "
        "WHERE title LIKE '%减持%' AND notice_date>='2025-08-01'"))
    print("\n[公告] 含「减持」字样 %d 条" % len(ann))

    def stage(title):
        t = title or ""
        # 结果/完成 → 滞后信息
        if re.search(r"结果|届满|实施完成|完成", t) and not re.search(r"预披露|计划", t):
            return "结果/届满（已卖完·滞后）"
        if re.search(r"预披露|减持计划|拟减持", t):
            return "预披露/减持计划（打算卖·最早）"
        if re.search(r"进展|触及|累计|超过|过半", t):
            return "进展/触及1%（正在卖）"
        return "其他"

    g_stage = defaultdict(list)
    for d, code, title in ann:
        if not code or not d:
            continue
        g_stage[stage(title)].append((code, d))
    report("⑥ 减持公告：按阶段拆分（次日可执行 T+5 超额）",
           sorted(g_stage.items(), key=lambda x: -len(x[1])), day_mean, fwd, cal, idx_of,
           "预披露=股东刚说「打算卖」；结果/届满=已经卖完（滞后，不该当风险信号）")

    # 预披露再按「是否控股股东/实控人」拆
    g_own = defaultdict(list)
    for d, code, title in ann:
        if stage(title) != "预披露/减持计划（打算卖·最早）":
            continue
        if re.search(r"控股股东|实际控制人|实控人", title):
            g_own["控股股东/实控人"].append((code, d))
        elif re.search(r"5%以上|持股5%|大股东", title):
            g_own["5%以上股东"].append((code, d))
        elif re.search(r"董事|监事|高级管理|高管", title):
            g_own["董监高"].append((code, d))
        else:
            g_own["未标明身份"].append((code, d))
    report("⑦ 减持预披露：按减持主体身份",
           sorted(g_own.items(), key=lambda x: -len(x[1])), day_mean, fwd, cal, idx_of,
           "控股股东减持的预测力通常最强（体量最大、最了解公司）")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
