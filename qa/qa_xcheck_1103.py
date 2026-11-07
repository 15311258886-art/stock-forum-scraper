# -*- coding: utf-8 -*-
"""QA 独立交叉验证：kline_daily.db 11-03 OHLC 对比腾讯实时接口。
不复用 fast_grab.reconcile()。
"""
import random
import sqlite3
import sys
import urllib.request

D = 20261103
DB = "file:data/kline_daily.db?mode=ro"


def prefix(code):
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8", "92")):
        return "bj" + code
    return "sh" + code


def fetch(codes, size=60):
    out = {}
    for i in range(0, len(codes), size):
        chunk = codes[i:i + size]
        q = ",".join(prefix(c) for c in chunk)
        url = "https://qt.gtimg.cn/q=" + q
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
        for line in raw.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            _, _, body = line.partition("=")
            body = body.strip().strip('"')
            f = body.split("~")
            if len(f) < 38:
                continue
            key = line[2:line.index("=")]
            out[key] = f
    return out


def main():
    con = sqlite3.connect(DB, uri=True)
    rows = list(con.execute(
        "select code,o,c,h,l,v from kline where d=?", (D,)))
    print("DB 11-03 rows:", len(rows))
    by_code = {r[0]: r for r in rows}

    # 分层抽样：主板6/0、创业板3、科创板688/689、北交所8/920
    buckets = {"sh6": [], "sz0": [], "sz3": [], "star68": [], "bj8": [], "bj920": []}
    for c in by_code:
        if c.startswith("6"):
            buckets["sh6"].append(c)
        elif c.startswith("0"):
            buckets["sz0"].append(c)
        elif c.startswith("3"):
            buckets["sz3"].append(c)
        elif c.startswith("8"):
            buckets["bj8"].append(c)
        elif c.startswith("92"):
            buckets["bj920"].append(c)
        else:
            buckets.setdefault("other", []).append(c)
    print("分桶:", {k: len(v) for k, v in buckets.items()})

    random.seed(20261103)
    quota = {"sh6": 90, "sz0": 60, "sz3": 50, "star68": 40, "bj8": 15, "bj920": 15,
             "other": 50}
    sample = []
    # 科创板必须单列 688/689
    star = [c for c in buckets["sh6"] if c.startswith("688") or c.startswith("689")]
    main_board = [c for c in buckets["sh6"] if not (c.startswith("688") or c.startswith("689"))]
    sample += random.sample(main_board, min(quota["sh6"], len(main_board)))
    sample += random.sample(star, min(quota["star68"], len(star)))
    for k in ("sz0", "sz3", "bj8", "bj920"):
        if buckets.get(k):
            sample += random.sample(buckets[k], min(quota[k], len(buckets[k])))
    sample += random.sample(buckets.get("other", []) or ["600000"], 0)
    # 定向加入 4 只除权票 + 11 只 2 根票
    must = ["000788", "300735", "605179", "605300",
            "920025", "920157", "920162", "920196", "920201", "920202",
            "920229", "920285", "920296", "920298", "920306"]
    sample = list(dict.fromkeys(sample + [c for c in must if c in by_code]))
    print("样本数:", len(sample))

    quotes = fetch(sample)
    print("腾讯返回:", len(quotes))

    bad = []
    missing = []
    checked = 0
    for c in sample:
        k = prefix(c)
        f = quotes.get(k)
        if not f:
            missing.append(c)
            continue
        db = by_code[c]
        do, dc, dh, dl = db[1], db[2], db[3], db[4]
        # f[3]现价 f[5]今开 f[33]最高 f[34]最低
        try:
            t_c = float(f[3]); t_o = float(f[5]); t_h = float(f[33]); t_l = float(f[34])
            t_prev = float(f[4])
        except Exception:
            missing.append(c)
            continue
        # 停牌：腾讯现价=0 或 成交量=0
        if t_c == 0:
            missing.append(c + "(停牌/无价)")
            continue
        checked += 1
        diffs = {}
        for name, a, b in (("o", do, t_o), ("c", dc, t_c), ("h", dh, t_h), ("l", dl, t_l)):
            if abs(a - b) > 1e-6:
                diffs[name] = (a, b)
        if diffs:
            bad.append((c, f[1], diffs, t_prev))

    print("\n=== 结果 ===")
    print("实际比对:", checked, " 缺失:", len(missing), " 不一致:", len(bad))
    if missing:
        print("缺失清单(前20):", missing[:20])
    if bad:
        print("\n不一致明细:")
        for c, nm, diffs, tp in bad:
            print("  %s %s 昨收=%s diffs=%s" % (c, nm, tp, diffs))
    else:
        print("OHLC 全部零误差")


if __name__ == "__main__":
    sys.exit(main())
