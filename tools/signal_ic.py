# -*- coding: utf-8 -*-
"""全市场打分 IC / 分层收益（2026-10-29）。

解决什么问题
------------
此前"评分到底有没有区分度"这个判断，只能拿每天**选出来的 top20** 去算秩相关。
那是被截断的样本：实测每日 IC 在 -0.47 ~ +0.40 之间乱跳，均值 +0.030，
而 20 个样本的 IC 标准误本身就有 ~0.22 —— 结论等于"看不出来"。
`stock_universe_scan_daily` 每天存全市场约 5000 只打分后，同一问题才能有答案：
样本量放大 250 倍，标准误降到 ~0.01 量级。

输出什么
--------
1) **日度 IC**：当日全市场 score 与未来 N 日收益的 Spearman 秩相关。
   评分有效 → 稳定为正；无信息 → 在 0 附近随机正负；方向反了 → 稳定为负。
2) **分层收益**：按 score 分 5 层，各层的平均前向收益与胜率。
   单调递增才叫"能排序"；只有顶层好、其余杂乱 = 只是在赌极端值。
3) **IC 均值与 t 值**：判断"这个均值是不是噪音"。

用法：
    .venv/Scripts/python.exe tools/signal_ic.py                # 前向 1/3/5 日
    .venv/Scripts/python.exe tools/signal_ic.py --fwd 5 --layers 10
样本不足（少于 --min-days 个交易日）时如实报"积累中"，不硬给结论。
"""
import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "app.db")


def _rank(v):
    """平均秩（处理并列），返回与输入等长的秩列表。"""
    n = len(v)
    order = sorted(range(n), key=lambda i: v[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def _load_closes(con):
    """{code: ([dates], [closes])}，一次扫描，避免逐支查询。"""
    out = {}
    cur = con.execute("SELECT code, date, close FROM stock_indicators_daily ORDER BY code, date")
    last = None
    for code, d, c in cur:
        if code != last:
            out[code] = ([], [])
            last = code
        if c is None:
            continue
        out[code][0].append(str(d))
        out[code][1].append(float(c))
    return out


def _fwd_ret(series, date, fwd):
    """按日期定位，取 date 收盘 → date+fwd 交易日收盘的收益。"""
    dates, closes = series
    try:
        i = dates.index(date)
    except ValueError:
        return None
    j = i + fwd
    if j >= len(dates) or closes[i] == 0:
        return None
    return closes[j] / closes[i] - 1.0


def run(fwds=(1, 3, 5), layers=5, min_days=5):
    con = sqlite3.connect(DB, timeout=30)
    try:
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM stock_universe_scan_daily ORDER BY trade_date")]
    except sqlite3.Error:
        print("[signal_ic] 还没有 stock_universe_scan_daily 表，先跑一次 universe_scan")
        return {"ok": False}
    if not days:
        print("[signal_ic] 留档为空，先跑一次 universe_scan")
        return {"ok": False}

    scores = defaultdict(dict)
    for d, code, sc in con.execute(
            "SELECT trade_date, code, score FROM stock_universe_scan_daily"):
        if sc is not None:
            scores[str(d)][code] = float(sc)
    closes = _load_closes(con)
    con.close()

    print("[signal_ic] 留档 %d 个交易日（%s ~ %s），每日约 %d 只" %
          (len(days), days[0], days[-1], len(scores[days[-1]])))
    result = {"ok": True, "days": len(days), "detail": {}}

    for fwd in fwds:
        ics, per_day = [], []
        layer_acc = defaultdict(list)          # 层号 -> 收益列表（跨日累积）
        for d in days:
            sc = scores.get(d) or {}
            xs, ys, pairs = [], [], []
            for code, s in sc.items():
                ser = closes.get(code)
                if not ser:
                    continue
                r = _fwd_ret(ser, d, fwd)
                if r is None:
                    continue
                xs.append(s)
                ys.append(r)
                pairs.append((s, r))
            if len(xs) < 30:
                continue                          # 当日可用样本太少，跳过
            ic = spearman(xs, ys)
            if ic is None:
                continue
            ics.append(ic)
            # 分层：按 score 升序切 layers 段
            pairs.sort(key=lambda t: t[0])
            step = max(1, len(pairs) // layers)
            for k in range(layers):
                seg = pairs[k * step:(k + 1) * step] if k < layers - 1 else pairs[k * step:]
                if seg:
                    layer_acc[k].append(sum(r for _, r in seg) / len(seg))
            per_day.append({"date": d, "n": len(xs), "ic": round(ic, 4)})

        if not ics:
            print("\n前向%d日：可用交易日 0（尚无已走完 %d 个交易日的打分）" % (fwd, fwd))
            result["detail"][fwd] = {"ok": False, "days_usable": 0}
            continue

        mean = sum(ics) / len(ics)
        sd = (sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5 if len(ics) > 1 else 0.0
        tval = (mean / (sd / math.sqrt(len(ics)))) if sd else 0.0
        result["detail"][fwd] = {"ok": True, "days_usable": len(ics),
                                 "ic_mean": round(mean, 4), "ic_sd": round(sd, 4),
                                 "t": round(tval, 2), "layers": {}}

        print("\n=== 前向 %d 日 | 可用 %d 天 ===" % (fwd, len(ics)))
        if len(ics) < min_days:
            print("  样本积累中（%d/%d 天），暂不下结论" % (len(ics), min_days))
        else:
            verdict = ("评分有区分度" if mean > 0.02 and tval > 2 else
                       "评分方向相反" if mean < -0.02 and tval < -2 else
                       "与噪音无法区分")
            print("  IC 均值 %+.4f ｜ 标准差 %.4f ｜ t = %+.2f ｜ 裁决：%s"
                  % (mean, sd, tval, verdict))
        for k in sorted(layer_acc):
            arr = layer_acc[k]
            avg = sum(arr) / len(arr)
            win = sum(1 for x in arr if x > 0) / len(arr)
            tag = "←评分最低" if k == 0 else ("←评分最高" if k == layers - 1 else "")
            print("   第%d层 平均 %+.3f%%  胜率 %.1f%%  %s" % (k + 1, avg * 100, win * 100, tag))
            result["detail"][fwd]["layers"][k + 1] = {"avg_ret": round(avg, 5),
                                                      "winrate": round(win, 4),
                                                      "n_days": len(arr)}
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fwd", type=int, nargs="+", default=[1, 3, 5])
    ap.add_argument("--layers", type=int, default=5)
    ap.add_argument("--min-days", type=int, default=5)
    a = ap.parse_args()
    run(fwds=tuple(a.fwd), layers=a.layers, min_days=a.min_days)
