#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""质量漏斗 ①②③ 层：全市场 → 行业内质量前 30% → 出名单（2026-11-05）。

## 口径（进哥 2026-11-05 当场定的，不要擅自改）

1. **前 30% = 行业内部前 30%**，不是全市场前 30%。
   理由：PE/PB 天生带行业属性（银行 5 倍、芯片 80 倍都是"正常"），
   全市场统一排序等于把银行券商整个刷掉、把赛道股整个留下 —— 那是用一把尺子量。
2. **四个板全留**（主板 / 创业板 / 科创板 / 北交所），这一层**不砍板块**。
   理由：砍掉创业板+科创板+北交所等于放弃 44% 的池子，而其中也有真龙头。
   不同板的差异应该体现在**后面的判据**里，不是在这一层把票扔掉。

## 这一层只回答一个问题

「**这家公司是不是一门好生意**」—— 只跟同行比，不跟全市场比，也不看股价。
「**现在贵不贵**」是第 ⑤ 层的事，本层不算、不排、不筛。

## 为什么 PE 不进质量分

因为"便宜"不等于"优质"。一只 ROE 25%、营收连年 30% 增长的公司
PE 60 倍，它仍然是优质公司，只是当下贵 —— 这种票必须留在池子里，
否则池子会退化成"低估值陷阱收集器"。
PE 作为**价位标签**挂在结果里，供第 ⑤ 层匹配工具时用。

## 为什么用「行业内分位」而不是加权平均原始值

量纲不同：ROE 是百分比、负债率是百分比、营收增速可以 -1000%。
直接加权平均会被极端值绑架（样例里就有 np_yoy = -1036%）。
分位法只看排名，天然免疫极端值，也自动完成了"跟同行比"这件事 —— 一举两得。

## 已知局限（必须写在结论里，不能藏）

- `fundamentals` 只有**当期一期**（2026-09-30），没有历史序列。
  后果：**判不出周期性**。一家周期股在景气底部 ROE 为负、在顶部 ROE 30%，
  用单期数据会把底部的优质周期股判成垃圾、把顶部的判成优质 —— 方向恰好反了。
  这是本层最大的偏差，补 12 期历史财报后才能修（见 DECISIONS D-20261105-funnel）。
- 覆盖 85.3%（5058/5927）。缺财务的票不踢出，也不给分，单列 `no_fund` 标记 ——
  宁可让它留在池里被人工看到，也不要静默扔掉。

## 用法

    python tools/quality_funnel.py                     # 默认行业内前 30%
    python tools/quality_funnel.py --pct 0.20          # 收紧到 20%
    python tools/quality_funnel.py --min-keep 3        # 小行业至少留 3 只
    python tools/quality_funnel.py --out data/f.csv    # 指定输出
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DB = os.path.join(ROOT, "viewer", "app.db")
IND_MAP = os.path.join(ROOT, "viewer", "logs", "industry_map.json")

# 质量分权重（只跟"生意好不好"有关，不含估值）
#   盈利 45%：ROE 30 + 毛利率 15 —— ROE 是股东回报的根，毛利率是护城河的影子
#   成长 40%：营收 20 + 净利 20 —— 营收看需求真假，净利看赚钱效率，两者背离要警惕
#   健康 15%：负债率 15 —— 不占高权重，因为金融地产天生高负债，行业内分位已吸收大半
W = {"roe": 0.30, "gm": 0.15, "rev_yoy": 0.20, "np_yoy": 0.20, "dr": 0.15}
# 方向：+1 = 越大越好，-1 = 越小越好
DIR = {"roe": 1, "gm": 1, "rev_yoy": 1, "np_yoy": 1, "dr": -1}


def _load_industry() -> dict:
    with io.open(IND_MAP, encoding="utf-8") as fh:
        d = json.load(fh)
    return {str(k): str(v) for k, v in (d.get("map") or {}).items() if v}


def _pct_rank(vals: list) -> dict:
    """把一组数值换成 0~100 的百分位（`None` 不参与，返回里也不出现）。

    用「平均秩」处理并列：三只票 ROE 都是 10% 时，它们拿同一个分位，
    而不是按字典序分三六九等 —— 否则排序结果会依赖 code 大小，是假信号。
    """
    idx = [(i, v) for i, v in enumerate(vals) if v is not None and v == v]
    if len(idx) < 2:
        return {}
    idx.sort(key=lambda x: x[1])
    out, n = {}, len(idx)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and idx[j + 1][1] == idx[i][1]:
            j += 1
        # 并列组共享平均秩
        avg = (i + j) / 2.0
        pct = 100.0 * avg / (n - 1)
        for k in range(i, j + 1):
            out[idx[k][0]] = pct
        i = j + 1
    return out


def build(pct: float, min_keep: int, db: str) -> dict:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row

    # ── 1. 宇宙：全 A 清单 − 剔除池（真剔除，不含幽灵）───────────────────
    universe = {}
    for r in con.execute("SELECT code, name, board FROM a_shares_universe"):
        universe[str(r["code"])] = (r["name"] or "", r["board"] or "")
    n_all = len(universe)
    ex = set()
    for r in con.execute("SELECT code FROM exclusion_pool"):
        ex.add(str(r["code"]))
    # 只剔除真实存在于宇宙里的剔除记录（幽灵不影响结果，这里天然对齐）
    alive = {c: v for c, v in universe.items() if c not in ex}
    n_alive = len(alive)

    # ── 2. 最新快照（价位标签 + 市值，用于并列时的次序）─────────────────
    last_day = con.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()[0]
    snap = {}
    for r in con.execute("SELECT code, close, pe_ttm, pb, total_mv, pct FROM stock_daily_snapshot"
                         " WHERE trade_date=?", (last_day,)):
        snap[str(r["code"])] = (r["close"], r["pe_ttm"], r["pb"], r["total_mv"], r["pct"])

    # ── 3. 基本面 ─────────────────────────────────────────────────────
    fund = {}
    for r in con.execute("SELECT symbol, roe, roe_kf, dr, gm, rev_yoy, np_yoy, report_date"
                         " FROM fundamentals"):
        fund[str(r["symbol"])] = r
    con.close()

    ind = _load_industry()

    # ── 4. 分组 + 行业内分位 ──────────────────────────────────────────
    groups = defaultdict(list)
    for c, (name, board) in alive.items():
        groups[ind.get(c, "未分类")].append(c)

    rows_out = []
    for iname, codes in groups.items():
        # 本行业各指标的原始序列（顺序与 codes 对齐）
        series = {}
        for k in DIR:
            series[k] = []
            for c in codes:
                f = fund.get(c)
                v = None
                if f is not None:
                    try:
                        v = f[k]
                        v = float(v) if v is not None else None
                    except (TypeError, ValueError):
                        v = None
                series[k].append(v)
        pr = {k: _pct_rank(series[k]) for k in DIR}

        scored = []
        for i, c in enumerate(codes):
            f = fund.get(c)
            if f is None:
                scored.append((None, c, {}, True))     # 无财务数据
                continue
            parts = {}
            for k in DIR:
                p = pr[k].get(i)
                # 缺某个指标：该项按 50 分（行业中位）处理，不奖不罚；
                # 若整个票五项全缺，等同于没有财务数据。
                parts[k] = 50.0 if p is None else (p if DIR[k] > 0 else 100.0 - p)
            score = sum(W[k] * parts[k] for k in DIR)
            scored.append((score, c, parts, False))

        # ── 5. 组内取前 pct ───────────────────────────────────────────
        ranked = [x for x in scored if x[0] is not None]
        ranked.sort(key=lambda x: (-x[0], -(snap.get(x[1], (0,)*5)[3] or 0)))
        k = max(int(math.ceil(len(codes) * pct)), min_keep)
        k = min(k, len(ranked))
        picked = ranked[:k]

        for rank_i, (score, c, parts, _) in enumerate(picked, 1):
            name, board = alive[c]
            s = snap.get(c, (None,) * 5)
            f = fund.get(c)
            rows_out.append({
                "code": c, "name": name, "industry": iname, "board": board,
                "quality": round(score, 1),
                "roe": f["roe"] if f else None,
                "gm": f["gm"] if f else None,
                "rev_yoy": f["rev_yoy"] if f else None,
                "np_yoy": f["np_yoy"] if f else None,
                "dr": f["dr"] if f else None,
                "loss": 1 if (f and f["roe"] is not None and f["roe"] < 0) else 0,
                "pe_ttm": s[1], "pb": s[2], "total_mv": s[3], "close": s[0],
                "ind_n": len(codes), "ind_rank": rank_i,
                "report_date": f["report_date"] if f else "",
            })

    rows_out.sort(key=lambda r: (r["industry"], r["ind_rank"]))
    return {
        "n_all": n_all, "n_alive": n_alive, "n_excluded": n_all - n_alive,
        "n_fund": sum(1 for c in alive if c in fund),
        "n_ind": len(groups), "last_day": last_day,
        "picked": rows_out,
        "no_fund": [c for c in alive if c not in fund],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="质量漏斗 ①②③ 层")
    ap.add_argument("--pct", type=float, default=0.30)
    ap.add_argument("--min-keep", type=int, default=2)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    t0 = time.time()
    r = build(a.pct, a.min_keep, a.db)
    p = r["picked"]

    print("=" * 62)
    print("质量漏斗（行业内前 %.0f%%，四板全留）" % (a.pct * 100))
    print("=" * 62)
    print("① 全 A 清单        %6d 只" % r["n_all"])
    print("   剔出（剔除池）    %6d 只" % r["n_excluded"])
    print("② 可评估宇宙       %6d 只   有财务 %d（%.1f%%）"
          % (r["n_alive"], r["n_fund"], 100.0 * r["n_fund"] / max(1, r["n_alive"])))
    _sz = sorted(_ind_sizes(p).values())
    _med = _sz[len(_sz) // 2] if _sz else 0
    print("③ 行业内前 %.0f%%    %6d 只   （%d 个行业，入选行业中位每行业 %d 只）"
          % (a.pct * 100, len(p), r["n_ind"], _med))
    print("   快照日           %s   耗时 %.1fs" % (r["last_day"], time.time() - t0))

    from collections import Counter
    bc = Counter(x["board"] for x in p)
    print("\n板块分布：" + "  ".join("%s %d" % (k, v) for k, v in bc.most_common()))
    loss = sum(x["loss"] for x in p)
    print("亏损股（ROE<0）：%d 只（%.1f%%）—— 单期口径下分不清「真垃圾」与「周期底部」，"
          "见 DECISIONS D-20261105-funnel"
          % (loss, 100.0 * loss / max(1, len(p))))

    top_ind = Counter(x["industry"] for x in p).most_common(6)
    print("入选最多的行业：" + "  ".join("%s %d" % (k, v) for k, v in top_ind))
    print("质量分 Top 15：")
    for x in sorted(p, key=lambda z: -z["quality"])[:15]:
        print("   %-6s %-8s %-10s Q=%.0f  ROE=%s  营收=%s%%  净利=%s%%"
              % (x["code"], (x["name"] or "")[:8], x["industry"][:10], x["quality"],
                 x["roe"], _f1(x["rev_yoy"]), _f1(x["np_yoy"])))

    out = a.out or os.path.join(ROOT, "data", "quality_funnel_%s.csv"
                                % time.strftime("%Y%m%d"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cols = ["code", "name", "industry", "board", "quality", "ind_rank", "ind_n",
            "roe", "gm", "rev_yoy", "np_yoy", "dr", "loss",
            "pe_ttm", "pb", "total_mv", "close", "report_date"]
    with io.open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(p)
    print("\n名单 → %s（%d 行）" % (out, len(p)))
    if r["no_fund"]:
        print("无财务数据（未参与打分、未入名单）：%d 只，样例 %s"
              % (len(r["no_fund"]), r["no_fund"][:8]))
    return 0


def _f1(v):
    try:
        return "%.1f" % float(v)
    except (TypeError, ValueError):
        return "-"


def _ind_sizes(rows):
    d = defaultdict(int)
    for x in rows:
        d[x["industry"]] += 1
    return d


if __name__ == "__main__":
    sys.exit(main())
