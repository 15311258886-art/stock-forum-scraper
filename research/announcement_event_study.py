# -*- coding: utf-8 -*-
"""公告事件研究：stock_ann.cat × tone → T+1 / T+5 / T+20 相对全市场等权均值的超额。

为什么做这个
------------
系统里所有指标都是"价格派生"的（K线、量、龙虎榜、四支柱），属同一类信息源。
`stock_ann` 是库里唯一一份**与价格无关、且长达 11 年**的个股事件数据
（1,158,448 行 / 2016-01-01 ~ 2026-10-31 / 3,106 个披露日 / 15 个类别 / good-bad-neutral 语气），
但至今没有任何一处代码消费它做预测。本脚本先回答一个问题：
**这些事件里，哪几类在事件后确实有可测的超额，方向和量级是多少。**

方法（每条都是为了堵一个具体的坑，改动前请先读理由）
----------------------------------------------------
1. **事件生效日 = 严格大于 notice_date 的第一个交易日。**
   实测 `stock_ann` 存在 notice_date 为周六（如 2026-10-31）、
   disclose_at 为该校 15:00 的记录；用 notice_date 当日收盘价买入会引入前视。
2. **同一 (code, cat) 在 COOLDOWN_DAYS 内只取第一次。**
   单票单日最多有 67 条公告（002608 于 2016-04-29），不去重会让同一件事
   在事件表里出现几十次，等价于给个别样本加权。
3. **基准 = 全市场等权均值**（与 viewer/evidence.py、viewer/avoid.py、
   viewer/selection.py 同源）。**不用中位数**：resonance 的 BENCH_MODE 用中位数，
   中位数按定义低于均值，两把尺子混用会让"超额"不可比（DECISIONS D36）。
4. **统计量按"交易日"聚合后再算 t，不按事件算。**
   同一交易日的事件高度截面相关，同一只票的持仓期又互相重叠；
   按事件算 t 会严重高估显著性。这里先对每天取均值，再对日序列做检验。
5. **预先登记判据 |t| >= 3.0。**
   15 个类别 × 3 个持有期 = 45 次检验，Bonferroni α=0.05/45≈0.0011，
   双尾临界 z≈3.26；取 3.0 已是宽松下限。**不按 2.0 判显著**——
   系统里已经有过一次教训：把 t=2.05 当有效，同时做了几十次比较。
6. **block bootstrap（块长 = 持有期）给均值置信区间**，处理日间自相关。

已知局限（必须和结论一起引用）
------------------------------
- **幸存者偏差**：`kline_daily.db` 只含当前在册的 5,575 只，2016 年退市的票不在库里，
  因此早年事件样本偏向"活下来的公司"。绝对收益会被高估，但"同一天相对全市场"
  的部分互相抵消，超额的方向性结论受影响较小。
- 只做单变量统计，**不控制市值/行业/前期涨幅**。若某类别高度集中于小市值，
  超额里可能混有市值因子。这一步留给后续分层。

用法
----
    .venv/Scripts/python.exe research/announcement_event_study.py
    .venv/Scripts/python.exe research/announcement_event_study.py --min-events 200

输出：控制台表格 + research/out/announcement_event_study.md
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DB = os.path.join(ROOT, "viewer", "app.db")
KLINE_DB = os.path.join(ROOT, "data", "kline_daily.db")
OUT_DIR = os.path.join(ROOT, "research", "out")

HORIZONS = (1, 5, 20)
COOLDOWN_DAYS = 60          # 同一 (code, cat) 的冷却自然日
T_CRIT = 3.0                # 预先登记判据
BOOT_ITERS = 400
SEED = 20261030


def _ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def to_int_date(s: str) -> int | None:
    """'2026-10-31' -> 20261031；无法解析返回 None。"""
    s = (s or "").strip()[:10]
    if len(s) < 10:
        return None
    try:
        return int(s[:4]) * 10000 + int(s[5:7]) * 100 + int(s[8:10])
    except Exception:  # noqa: BLE001
        return None


def load_bars():
    """返回 {code: (dates int32 升序, closes float64)}。"""
    con = _ro(KLINE_DB)
    bars: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    cur = con.execute("SELECT code, d, c FROM kline ORDER BY code, d")
    cur_code = None
    buf_d: list[int] = []
    buf_c: list[float] = []
    for code, d, c in cur:
        if code != cur_code:
            if cur_code is not None and buf_d:
                bars[cur_code] = (np.asarray(buf_d, dtype=np.int64),
                                  np.asarray(buf_c, dtype=np.float64))
            cur_code = code
            buf_d, buf_c = [], []
        if d is None or c in (None, 0):
            continue
        buf_d.append(int(d))
        buf_c.append(float(c))
    if cur_code is not None and buf_d:
        bars[cur_code] = (np.asarray(buf_d, dtype=np.int64),
                          np.asarray(buf_c, dtype=np.float64))
    con.close()
    return bars


def build_market_benchmark(bars, all_days: np.ndarray):
    """全市场等权均值：每个交易日、每个持有期，全体当日在册个股的平均远期收益。

    返回 {h: {day_int: mean_return}}。
    """
    day_pos = {int(d): i for i, d in enumerate(all_days)}
    n_days = len(all_days)
    bench: dict[int, dict[int, float]] = {}
    for h in HORIZONS:
        sums = np.zeros(n_days, dtype=np.float64)
        cnts = np.zeros(n_days, dtype=np.int64)
        for dates, closes in bars.values():
            n = len(dates)
            if n <= h:
                continue
            fwd = closes[h:] / closes[:-h] - 1.0
            fwd = np.where(np.isfinite(fwd), fwd, np.nan)
            idx = np.fromiter((day_pos.get(int(x), -1) for x in dates[:-h]),
                              dtype=np.int64, count=n - h)
            ok = (idx >= 0) & np.isfinite(fwd)
            if not ok.any():
                continue
            np.add.at(sums, idx[ok], fwd[ok])
            np.add.at(cnts, idx[ok], 1)
        good = cnts > 0
        bench[h] = {int(all_days[i]): float(sums[i] / cnts[i]) for i in np.flatnonzero(good)}
        print("  基准 T+%d：%d 个交易日，日均样本 %.0f 只"
              % (h, int(good.sum()), float(cnts[good].mean()) if good.any() else 0), flush=True)
    return bench


def load_events(cooldown_days: int):
    """读公告，按 (code, cat) 冷却去重。返回 [(code, cat, tone, notice_int)]。"""
    con = _ro(APP_DB)
    rows = []
    cur = con.execute(
        "SELECT code, notice_date, cat, tone FROM stock_ann "
        "WHERE notice_date IS NOT NULL AND notice_date <> '' "
        "AND cat IS NOT NULL AND cat <> '' ORDER BY code, cat, notice_date"
    )
    last = {}
    kept = 0
    total = 0
    for code, nd, cat, tone in cur:
        total += 1
        di = to_int_date(nd)
        if di is None:
            continue
        key = (code, cat)
        prev = last.get(key)
        # 粗略把自然日差换算成日历天（YYYYMMDD 差值不是连续计数，用日期序数更准）
        if prev is not None:
            pv = (prev // 10000) * 372 + ((prev // 100) % 100) * 31 + (prev % 100)
            dv = (di // 10000) * 372 + ((di // 100) % 100) * 31 + (di % 100)
            if dv - pv < cooldown_days:
                continue
        last[key] = di
        rows.append((code, cat, (tone or "").strip() or "unknown", di))
        kept += 1
    con.close()
    print("  公告原始 %d 条 → 冷却 %d 天后保留 %d 条（%.1f%%）"
          % (total, cooldown_days, kept, 100.0 * kept / total), flush=True)
    return rows


def block_bootstrap_ci(x: np.ndarray, block: int, iters: int = BOOT_ITERS, seed: int = SEED):
    """日序列的均值置信区间，块长=持有期，处理跨日自相关。"""
    n = len(x)
    if n < block * 3:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    means = np.empty(iters, dtype=np.float64)
    max_start = n - block
    for i in range(iters):
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])[:n]
        means[i] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def study(events, bars, bench, group_by_tone: bool):
    """按 (cat[, tone]) × 持有期 收集"每日平均超额"，再对日序列做检验。"""
    all_days = np.array(sorted(bench[1].keys()), dtype=np.int64)

    # 每类每天的超额累加
    acc: dict[tuple, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    skipped = 0
    used = 0

    for code, cat, tone, di in events:
        b = bars.get(code)
        if b is None:
            skipped += 1
            continue
        dates, closes = b
        # 生效日 = 严格大于 notice_date 的第一个交易日
        pos = int(np.searchsorted(dates, di, side="right"))
        if pos >= len(dates):
            skipped += 1
            continue
        if pos < 1:
            skipped += 1
            continue
        base = closes[pos - 1]
        if not np.isfinite(base) or base <= 0:
            skipped += 1
            continue
        key0 = (cat, tone) if group_by_tone else (cat,)
        day = int(dates[pos])
        for h in HORIZONS:
            j = pos - 1 + h
            if j >= len(closes):
                continue
            mk = bench[h].get(day)
            if mk is None:
                continue
            ex = (closes[j] / base - 1.0) - mk
            if np.isfinite(ex):
                acc[key0 + (h,)][day].append(float(ex))
        used += 1

    print("  事件可用 %d 条（跳过 %d 条：无K线/超出数据末端）" % (used, skipped), flush=True)

    out = []
    for key, by_day in acc.items():
        h = key[-1]
        cat = key[0]
        tone = key[1] if len(key) == 3 else ""
        daily = np.array([float(np.mean(v)) for v in by_day.values()], dtype=np.float64)
        n_days = len(daily)
        n_events = sum(len(v) for v in by_day.values())
        if n_days < 3:
            continue
        mean = float(daily.mean())
        sd = float(daily.std(ddof=1)) if n_days > 1 else float("nan")
        t = mean / (sd / np.sqrt(n_days)) if sd and sd > 0 else float("nan")
        lo, hi = block_bootstrap_ci(daily, block=max(1, h))
        win = float((daily > 0).mean())
        out.append({
            "category": cat, "tone": tone, "horizon": h,
            "n_events": n_events, "n_days": n_days,
            "mean_excess_pct": mean * 100.0, "t": t,
            "ci_lo_pct": lo * 100.0, "ci_hi_pct": hi * 100.0,
            "day_winrate_pct": win * 100.0,
            "verdict": ("显著为正" if t >= T_CRIT else
                        "显著为负" if t <= -T_CRIT else "不显著"),
        })
    out.sort(key=lambda r: (r["category"], r["tone"], r["horizon"]))
    return out


def render_md(rows, min_events, group_by_tone, meta):
    lines = ["# 公告事件研究（stock_ann × K线远期超额）", ""]
    lines.append("基准：全市场等权均值 · 统计量按交易日聚合 · 判据 |t| ≥ %.1f · block bootstrap 400 次" % T_CRIT)
    lines.append("")
    for k, v in meta.items():
        lines.append("- %s：%s" % (k, v))
    lines.append("")
    groups = defaultdict(list)
    for r in rows:
        groups[(r["category"], r["tone"])].append(r)
    lines.append("| 类别 | 语气 | 事件数 | 交易日 | 持有期 | 平均超额 | t | 95%CI | 日胜率 | 判定 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---:|---|")
    for (cat, tone), rs in sorted(groups.items(), key=lambda kv: -max(x["abs_t"] if "abs_t" in x else abs(x["t"]) for x in kv[1])):
        rs.sort(key=lambda x: x["horizon"])
        for r in rs:
            if r["n_events"] < min_events:
                continue
            ci = "[%+.2f%%, %+.2f%%]" % (r["ci_lo_pct"], r["ci_hi_pct"])
            lines.append("| %s | %s | %d | %d | T+%d | %+.3f%% | %+.2f | %s | %.1f%% | %s |"
                         % (cat, tone or "-", r["n_events"], r["n_days"], r["horizon"],
                            r["mean_excess_pct"], r["t"], ci, r["day_winrate_pct"], r["verdict"]))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="公告事件研究（只读，不改任何库）")
    ap.add_argument("--min-events", type=int, default=200, help="低于此样本量不出结论")
    ap.add_argument("--split-tone", action="store_true", help="再按 good/bad/neutral 拆一层")
    args = ap.parse_args()

    if not os.path.exists(KLINE_DB):
        print("找不到 %s" % KLINE_DB, file=sys.stderr)
        return 2

    print("[1/4] 载入日线…", flush=True)
    bars = load_bars()
    all_days = np.array(sorted({int(d) for dates, _ in bars.values() for d in dates}), dtype=np.int64)
    print("  %d 只股票，%d 个交易日（%d ~ %d）"
          % (len(bars), len(all_days), all_days[0], all_days[-1]), flush=True)

    print("[2/4] 计算全市场等权基准…", flush=True)
    bench = build_market_benchmark(bars, all_days)

    print("[3/4] 读取公告事件…", flush=True)
    events = load_events(COOLDOWN_DAYS)

    print("[4/4] 统计…", flush=True)
    rows = study(events, bars, bench, args.split_tone)

    meta = {
        "股票池": "%d 只（kline_daily.db，仅当前在册，存在幸存者偏差）" % len(bars),
        "交易日区间": "%d ~ %d（共 %d 个交易日）" % (all_days[0], all_days[-1], len(all_days)),
        "事件去重": "同一 (代码, 类别) %d 自然日内只取第一次" % COOLDOWN_DAYS,
        "判据": "|t| ≥ %.1f（15 类 × 3 持有期 = 45 次检验的 Bonferroni 宽松下限）" % T_CRIT,
        "未控制": "市值 / 行业 / 前期涨幅（单变量统计，分层留给后续）",
    }
    md = render_md(rows, args.min_events, args.split_tone, meta)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "announcement_event_study.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print("\n写入 %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
