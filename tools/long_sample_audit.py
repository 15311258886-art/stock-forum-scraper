# -*- coding: utf-8 -*-
"""长样本复核：把证据账本（viewer/evidence.py 的 CLAIMS）用 2016 年至今的全样本重跑一遍。

要解决的问题
------------
账本里 13 条 claim 的 n / days / excess / t，全部建立在这张因子表当时能覆盖的
**288 个交易日（2025-08-18 起）** 上 —— 也就是单一牛市段落。一条规则在 14 个月里
成立，不等于在 2018 熊市、2020 疫情、2021-2022 阴跌里也成立。样本深度不够时，
t 值再大也只是"这一年有效"，不是"这个规则有效"。

本工具做的事情：对每条 claim 的同一谓词、同一 horizon、同一严格口径（exec_off=1），
把事件日区间换成 --start（默认 2016-01-01）重算一遍，与账本存的数字并列对照，
按下列四档判定：

    consistent   同向且 |t|>=2、幅度保留 >=50%   → 原结论可继续使用
    weakened     同向但幅度打折过半，或 |t|<2      → 降级为 weak
    reversed     符号相反                          → 必须推翻，禁止进结论
    no_sample    样本不足无法判定

用法：
    .venv/Scripts/python.exe tools/long_sample_audit.py --start 2016-01-01
    .venv/Scripts/python.exe tools/long_sample_audit.py --keys lhb_turnover,vol_extreme
输出：docs/长样本复核_<日期>.md + data/long_sample_audit.json
"""
import os
import re
import sys
import json
import time
import sqlite3
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

import hypo  # noqa: E402
from viewer.evidence import CLAIMS  # noqa: E402


def parse_predicate(claim: dict) -> str:
    """从 probe 字段里抠出 predicate。probe 形如：viewer/hypo.py（predicate: volume_ratio >= 3）"""
    m = re.search(r"predicate:\s*([^）)]+)", claim.get("probe") or "")
    return m.group(1).strip() if m else ""


def judge(old: float, new: float, told: float, tnew: float) -> str:
    """old = 账本里的逐样本均值（短样本）；new = 长样本**按日均值**（抗聚集，与 t 同源）。

    old 的绝对值小于 deadband 时（短样本本来就判"无方向"），不能拿它的符号去判
    "翻转"——例如 vol_down 旧值 +0.01%（t=−1.73，实际是"看不出来"），长样本 −0.56%
    不是反转而是**补上了方向**。这类统一按长样本自身的显著性判定。
    """
    if new is None or tnew is None:
        return "no_sample"
    if old is None or abs(old) < 0.2:
        return "consistent" if abs(new) >= 0.3 and abs(tnew) >= 2 else "weakened"
    if new * old < 0:
        return "reversed"
    keep = abs(new) / abs(old)
    if abs(tnew) >= 2 and keep >= 0.5:
        return "consistent"
    return "weakened"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--keys", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    want = [k.strip() for k in args.keys.split(",") if k.strip()]
    conn = sqlite3.connect(os.path.join(ROOT, "viewer", "app.db"), timeout=60)
    t0 = time.time()
    rows, errs = [], []
    for c in CLAIMS:
        if want and c["key"] not in want:
            continue
        pred = parse_predicate(c)
        if not pred:
            errs.append((c["key"], "probe 里没有 predicate，跳过"))
            continue
        try:
            r = hypo.evaluate(conn, pred, horizon=int(c.get("horizon") or 5),
                              start=args.start, exec_off=1)
        except Exception as e:  # noqa: BLE001
            errs.append((c["key"], "%s: %s" % (type(e).__name__, e)))
            continue
        old_ex, old_t = c.get("excess"), c.get("t")
        new_ex, new_t = r.get("excess"), r.get("t")
        new_day = r.get("excess_day")
        v = judge(old_ex, new_day, old_t, new_t)
        rows.append({
            "key": c["key"], "name": c.get("name"), "group": c.get("group"),
            "predicate": pred, "horizon": c.get("horizon"),
            "old": {"n": c.get("n"), "days": c.get("days"), "excess": old_ex,
                    "t": old_t, "status": c.get("status")},
            "long": {"ok": r.get("ok"), "n": r.get("n"), "days": r.get("days"),
                     "excess": new_ex, "excess_day": new_day, "t": new_t,
                     "win": r.get("win"), "conflict": r.get("sign_conflict"),
                     "note": r.get("note")},
            "verdict": v,
        })
        print("[audit] %-14s %-9s 旧 %+7.2f%% t=%6.2f (%s日) → 长样本 逐样本%+7.2f%% 按日%+7.2f%% t=%6.2f (%s日)%s  %s"
              % (c["key"], v, old_ex or 0, old_t or 0, c.get("days"),
                 new_ex or 0, new_day or 0, new_t or 0, r.get("days"),
                 " [口径冲突]" if r.get("sign_conflict") else "",
                 "%.0fs" % (time.time() - t0)), flush=True)

    out_json = os.path.join(ROOT, "data", "long_sample_audit.json")
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"asof": time.strftime("%Y-%m-%d %H:%M"), "start": args.start,
                   "rows": rows, "errors": errs}, f, ensure_ascii=False, indent=2)

    cnt = {}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    print("[audit] 完成 %d 条 %.0fs｜%s｜错误 %d" % (len(rows), time.time() - t0, cnt, len(errs)), flush=True)
    for k, m in errs:
        print("  ERR %s %s" % (k, m), flush=True)

    if args.out:
        lines = ["# 长样本复核（%s 起）" % args.start, "",
                 "口径：严格口径 exec_off=1；长样本超额取**按日均值**（与 t 同源，抗单日聚集）。",
                 "", "| claim | 旧超额(逐样本) | 旧t | 旧天数 | 长样本按日 | 长样本逐样本 | 长样本t | 长样本天数 | 判定 |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append("| %s %s | %+.2f%% | %.2f | %s | %s | %s | %s | %s | **%s** |" % (
                r["key"], r["name"], r["old"]["excess"] or 0, r["old"]["t"] or 0, r["old"]["days"],
                ("%+.2f%%" % r["long"]["excess_day"]) if r["long"]["excess_day"] is not None else "—",
                ("%+.2f%%" % r["long"]["excess"]) if r["long"]["excess"] is not None else "—",
                ("%.2f" % r["long"]["t"]) if r["long"]["t"] is not None else "—",
                r["long"]["days"], r["verdict"]))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("[audit] 写出 %s" % args.out, flush=True)
    conn.close()


if __name__ == "__main__":
    main()
