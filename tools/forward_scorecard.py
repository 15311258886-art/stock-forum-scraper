# -*- coding: utf-8 -*-
"""前向记账 · 第三棒：计分卡（按来源 × 持有期）—— 2026-11-05

## 它回答的问题

「决策台 / 三条判据说的话，到点准不准？」

按 `(src, hold)` 分组，给：样本数、到期数、胜率、平均超额、超额中位数、t 值。

## 「准」的定义

前向超额 = 个股收益 − 同期全市场等权收益 − 0.25% 双边成本，**> 0 记胜**。
（收益与成本口径统一由 `tools/forward_lib.py` 提供，本文件不另算一套。）

## t 值为什么必须用非重叠抽样

同一天出的票、以及相邻几天出的票，持有窗口高度重叠 —— 直接拿所有行算 t 会把
样本量虚报好几倍（本项目在 `tools/cond_engine.py` 里已经吃过这个亏）。
所以这里先把「同一天的多只票」压成当日一个观测（等权平均），再按持有期 H
把交易日历切成长度 H 的桶，**每桶只取一条**，最后对这条非重叠序列算 t。

## 样本不足怎么办

**到期样本 < 20 的格子一律打「样本不足，不作结论」，并且把胜率/超额/t 全部留空。**
理由很实在：10 个样本里对 7 个就是「胜率 70%」，读的人会当成结论，
而它跟抛硬币没区别。宁可少说，不能说错。

## 用法

  python tools/forward_scorecard.py
  python tools/forward_scorecard.py --min-n 30
  python tools/forward_scorecard.py --no-write     # 只看屏，不覆盖 docs
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:                                                          # 直接跑：tools/ 在 sys.path[0]
    import forward_lib as FL
except ImportError:                                           # `python -m tools.forward_scorecard`
    from tools import forward_lib as FL                       # noqa: E402

MIN_N = 20              # 到期样本下限：低于它不给任何结论
OUT_MD = FL.DOCS / "前向记账计分卡.md"


def _label(src: str) -> str:
    """来源的人读名字。决策台记 `pillar:<主线>`，判据直接用键。"""
    if src.startswith(FL.PILLAR_PREFIX):
        return "决策台·" + src[len(FL.PILLAR_PREFIX):]
    return {
        "oss_near_high": "距250日高点≥-25%",
        "oss_reversal20": "20日跌幅≥20%",
        "pb_vol_price_diverge": "创20日新高·量比<0.8",
    }.get(src, src)


def _role(src: str) -> str:
    """排除型判据的超额方向要反过来读（负超额=避开是对的）。"""
    return "排除" if src == "pb_vol_price_diverge" else "选股"


def _bucket_t(days: list[int], vals: list[float], hold: int,
              cal_pos: dict[int, int]) -> tuple[float | None, int]:
    """非重叠抽样的 t 值。返回 (t, 用到的观测数)。

    做法：把每个信号日映射到全局交易日序号，按 `pos // hold` 分桶，每桶只留
    最早的一天（同桶内窗口必然重叠，留多了等于重复计数）。
    桶内多条已在上游压成当日一条，所以这里一天就是一个观测。
    """
    by_bucket: dict[int, tuple[int, float]] = {}
    for d, v in zip(days, vals):
        pos = cal_pos.get(int(d))
        if pos is None or not np.isfinite(v):
            continue
        b = pos // max(1, int(hold))
        if b not in by_bucket or pos < by_bucket[b][0]:
            by_bucket[b] = (pos, float(v))
    s = [v for _p, v in by_bucket.values()]
    n = len(s)
    if n < 3:
        return None, n
    arr = np.array(s, dtype=np.float64)
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return None, n
    return float(arr.mean() / (sd / math.sqrt(n))), n


def build_rows(con_app: sqlite3.Connection, cal_pos: dict[int, int],
               min_n: int) -> tuple[list[dict], dict]:
    """从 forward_result × forward_book 汇总出每个 (src, hold) 一行。"""
    q = con_app.execute(
        "SELECT b.src, r.hold, b.snap_date, r.matured, r.excess_pct "
        "FROM forward_result r JOIN forward_book b ON b.id = r.book_id "
        "ORDER BY b.src, r.hold, b.snap_date")
    groups: dict[tuple[str, int], dict] = defaultdict(
        lambda: {"n_total": 0, "n_matured": 0, "ex": [], "by_day": defaultdict(list)})
    for r in q:
        key = (str(r["src"]), int(r["hold"]))
        g = groups[key]
        g["n_total"] += 1
        if int(r["matured"]) != 1:
            continue
        g["n_matured"] += 1
        ex = r["excess_pct"]
        if ex is None:
            continue
        g["ex"].append(float(ex))
        g["by_day"][int(r["snap_date"])].append(float(ex))

    total_books = con_app.execute("SELECT COUNT(*) FROM forward_book").fetchone()[0]
    pending = con_app.execute(
        "SELECT COUNT(*) FROM forward_book WHERE entry_px IS NULL AND skip_reason IS NULL"
    ).fetchone()[0]
    skipped = con_app.execute(
        "SELECT skip_reason, COUNT(*) n FROM forward_book "
        "WHERE skip_reason IS NOT NULL GROUP BY skip_reason").fetchall()

    rows: list[dict] = []
    for (src, hold), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        ex = np.array(g["ex"], dtype=np.float64) if g["ex"] else np.array([])
        usable = len(ex) >= min_n
        if usable:
            days = sorted(g["by_day"])
            day_means = [float(np.mean(g["by_day"][d])) for d in days]
            t, n_eff = _bucket_t(days, day_means, hold, cal_pos)
            rows.append({
                "src": src, "label": _label(src), "role": _role(src), "hold": hold,
                "n_total": g["n_total"], "n_matured": g["n_matured"],
                "n_used": len(ex), "n_days": len(days), "n_eff": n_eff,
                "win": float((ex > 0).mean()) * 100.0,
                "mean": float(ex.mean()), "median": float(np.median(ex)),
                "t": t, "ok": True,
            })
        else:
            rows.append({
                "src": src, "label": _label(src), "role": _role(src), "hold": hold,
                "n_total": g["n_total"], "n_matured": g["n_matured"],
                "n_used": len(ex), "n_days": len(g["by_day"]), "n_eff": 0,
                "win": None, "mean": None, "median": None, "t": None, "ok": False,
            })
    meta = {"total_books": int(total_books), "pending": int(pending),
            "skipped": {str(r["skip_reason"]): int(r["n"]) for r in skipped},
            "by_src": _src_progress(con_app)}
    return rows, meta


def _src_progress(con_app: sqlite3.Connection) -> list[dict]:
    """各来源的进度：账本条数 / 已进场 / 待回填 / 跳过。

    计分卡里只有「已进场且已到期」的格子会出现，所以刚建账的头几天
    卡片是空的。这张进度表是给人看「它在动」的，不参与任何结论。
    """
    q = con_app.execute(
        "SELECT src, COUNT(*) n, "
        "       SUM(CASE WHEN entry_px IS NOT NULL THEN 1 ELSE 0 END) filled, "
        "       SUM(CASE WHEN entry_px IS NULL AND skip_reason IS NULL THEN 1 ELSE 0 END) pending, "
        "       SUM(CASE WHEN skip_reason IS NOT NULL THEN 1 ELSE 0 END) skipped "
        "FROM forward_book GROUP BY src ORDER BY n DESC")
    return [{"src": str(r["src"]), "label": _label(str(r["src"])),
             "n": int(r["n"] or 0), "filled": int(r["filled"] or 0),
             "pending": int(r["pending"] or 0), "skipped": int(r["skipped"] or 0)}
            for r in q]


def _w(s: str) -> int:
    """字符串显示宽度：CJK 算 2 格（`%-20s` 对中文会错位，表格就歪了）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    """按显示宽度补空格（右对齐用 right=True）。"""
    gap = max(0, width - _w(s))
    return (" " * gap + s) if right else (s + " " * gap)


def print_table(rows: list[dict], meta: dict, min_n: int) -> None:
    print("\n== 前向记账计分卡（超额 = 个股收益 − 同期全市场等权 − 0.25% 成本）==")
    print("   账本 %d 条；待回填 %d 条；跳过 %s"
          % (meta["total_books"], meta["pending"],
             meta["skipped"] or "无"))
    print("   到期样本 < %d 的格子不给结论（打印「样本不足，不作结论」）" % min_n)
    print()
    hdr = ("%s %s %s %s %s %s %s %s %s %s"
           % (_pad("来源", 22), _pad("名称", 24), _pad("持有", 5), _pad("角色", 4),
              _pad("样本", 6, True), _pad("到期", 6, True), _pad("胜率%", 7, True),
              _pad("平均超额", 10, True), _pad("中位超额", 10, True), _pad("t", 6, True)))
    print(hdr)
    print("-" * 118)
    insufficient = 0
    for r in rows:
        head = "%s %s %s %s %s %s" % (
            _pad(r["src"], 22), _pad(r["label"], 24), _pad("T+%d" % r["hold"], 5),
            _pad(r["role"], 4), _pad(str(r["n_total"]), 6, True),
            _pad(str(r["n_matured"]), 6, True))
        if r["ok"]:
            print("%s %s %s %s %s"
                  % (head, _pad("%.1f" % r["win"], 7, True),
                     _pad("%+.3f" % r["mean"], 10, True),
                     _pad("%+.3f" % r["median"], 10, True),
                     _pad("n/a" if r["t"] is None else "%.2f" % r["t"], 6, True)))
        else:
            insufficient += 1
            print("%s  %s" % (head, "样本不足，不作结论"))
    print("-" * 118)
    if insufficient:
        print("★ 其中 %d 个格子到期样本不足，已按「样本不足，不作结论」处理 ——"
              " 不要引用这些格子的胜率。" % insufficient)
    print("   角色=排除 的判据：超额为**负**才说明「避开它」是对的，方向别读反。")


def write_md(rows: list[dict], meta: dict, min_n: int) -> Path:
    FL.DOCS.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# 前向记账计分卡")
    lines.append("")
    lines.append("由 `tools/forward_scorecard.py` 生成于 %s（**不要手改**，重跑会覆盖）。" % FL.now_str())
    lines.append("")
    lines.append("## 这是在验证什么")
    lines.append("")
    lines.append("决策台（`pillar_item`）与三条已采纳判据都是**快照型判断**：依赖当日舆情 /"
                 " 资金流 / 当日形态，无法用历史数据重建。所以它们只能靠**前向记账**验证 ——"
                 " 每天把「它今天说了什么」写进 `forward_book`，到期用真实收盘价回看。")
    lines.append("")
    lines.append("## 口径（固定）")
    lines.append("")
    lines.append("| 项 | 口径 |")
    lines.append("|---|---|")
    lines.append("| 持有期 | 1 / 3 / 5 / 20 / 60 / 120 个交易日 |")
    lines.append("| 进场 | 信号日**次日收盘**（次日收盘涨停记 `limit_up`，不参与打分）|")
    lines.append("| 收益 | 进场日收盘 → 到期日收盘（`data/kline_daily.db`，已后复权）|")
    lines.append("| 成本 | 双边合计 0.25%，**从收益里扣** |")
    lines.append("| 基准 | 同区间全市场等权（`viewer/logs/ew_index.json`）|")
    lines.append("| 准 | 前向超额 = 个股收益 − 等权收益 − 成本 > 0 记胜 |")
    lines.append("")
    lines.append("## 账本存量")
    lines.append("")
    lines.append("- 账本记录：%d 条" % meta["total_books"])
    lines.append("- 待回填（次日还没到）：%d 条" % meta["pending"])
    if meta["skipped"]:
        lines.append("- 已跳过：%s"
                     % "、".join("%s %d 条" % (k, v) for k, v in meta["skipped"].items()))
    else:
        lines.append("- 已跳过：无")
    lines.append("")
    if meta.get("by_src"):
        lines.append("### 各来源进度（看进度用，不是结论）")
        lines.append("")
        lines.append("| 来源 | 名称 | 账本条数 | 已进场 | 待回填 | 跳过 |")
        lines.append("|---|---|---|---|---|---|")
        for g in meta["by_src"]:
            lines.append("| `%s` | %s | %d | %d | %d | %d |"
                         % (g["src"], g["label"], g["n"], g["filled"],
                            g["pending"], g["skipped"]))
        lines.append("")
    lines.append("## 计分卡")
    lines.append("")
    if not rows:
        lines.append("**还没有任何已进场的记录** —— 先跑 `tools/forward_snapshot.py` 记账，"
                     "次日收盘后跑 `tools/forward_score.py` 回填。")
    else:
        lines.append("到期样本 < %d 的格子一律标「样本不足，不作结论」，胜率/超额/t 留空 ——"
                     " 10 个样本里对 7 个也会显示「70%%」，那是噪声不是结论。" % min_n)
        lines.append("")
        lines.append("| 来源 | 名称 | 持有期 | 角色 | 样本数 | 到期数 | 胜率 | 平均超额(pp) |"
                     " 超额中位数(pp) | t（非重叠） |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            if r["ok"]:
                lines.append("| `%s` | %s | T+%d | %s | %d | %d | %.1f%% | %+.3f | %+.3f | %s |"
                             % (r["src"], r["label"], r["hold"], r["role"], r["n_total"],
                                r["n_matured"], r["win"], r["mean"], r["median"],
                                "n/a" if r["t"] is None else "%.2f" % r["t"]))
            else:
                lines.append("| `%s` | %s | T+%d | %s | %d | %d | 样本不足，不作结论 | | | |"
                             % (r["src"], r["label"], r["hold"], r["role"], r["n_total"],
                                r["n_matured"]))
        lines.append("")
    lines.append("## 怎么读这张表")
    lines.append("")
    lines.append("1. **只看「到期数 ≥ %d」的行。** 其余行留着是为了看进度，不是看结论。" % min_n)
    lines.append("2. `t` 用**非重叠抽样**算的（同一天的票先压成一条，再按持有期切桶）。"
                 "重叠窗口直接算 t 会把样本量虚报好几倍，这是本项目在 `cond_engine` 里"
                 "已经踩过的坑。")
    lines.append("3. `角色=排除` 的判据（创 20 日新高、量比 < 0.8）**超额为负才对** ——"
                 "它是「别买」的判据，负超额说明避开它是对的。")
    lines.append("4. 分来源看，别把六个持有期加起来看：同一批票在 1/3/5/20/60/120 上是"
                 " 六条互相重叠的序列，加起来会把同一个事件数六遍。")
    lines.append("5. 前向记账的价值随时间线性增长：T+1 次日就有分，T+120 要半年。"
                 "**头一个月不要下任何结论。**")
    lines.append("")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return OUT_MD


def main() -> int:
    FL.setup_stdout()
    ap = argparse.ArgumentParser(description="前向记账计分卡")
    ap.add_argument("--min-n", type=int, default=MIN_N, help="到期样本下限，默认 %d" % MIN_N)
    ap.add_argument("--no-write", action="store_true", help="只打印，不写 docs")
    args = ap.parse_args()
    print("[forward_scorecard] 开始 %s" % FL.now_str())

    con_app = FL.connect_app()
    try:
        FL.ensure_schema(con_app)
        # 交易日序号：非重叠抽样要用全局日轴，不能用「有信号的日子的序号」
        try:
            con_kline = FL.connect_kline()
            try:
                cal = FL.trading_dates(con_kline)
            finally:
                con_kline.close()
        except sqlite3.Error as e:
            print("!! 读不到 K 线库（%s），改用等权基准里的日期轴算 t 值。" % e)
            cal = FL.EwIndex().load().dates
        cal_pos = {int(d): i for i, d in enumerate(cal)}

        rows, meta = build_rows(con_app, cal_pos, args.min_n)
        print_table(rows, meta, args.min_n)
        if args.no_write:
            print("\n（--no-write：未写 docs）")
            return 0
        path = write_md(rows, meta, args.min_n)
        print("\n写盘：%s" % path)
    except sqlite3.OperationalError as e:
        print("!! 数据库暂时不可用：%s" % e)
        print("   多半是日更正在写库。本次跳过，稍后重跑。")
        return 0
    finally:
        con_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
