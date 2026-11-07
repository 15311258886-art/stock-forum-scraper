"""抓一份行业分类，供「同池同行业对照」用。

为什么要这个：现在所有反向结论的对照是「同一天、同一个池子里没触发的票」。
如果某类判据（追涨、放量、突破）天然扎堆在少数高波动行业，那么它跑输的可能
是「行业」而不是「判据本身」。换成同日同行业对照，行业这部分就被消掉了。

数据源：东财 clist 的 f100（行业名），一次 100 条，全市场 50 多次请求。
落盘 viewer/logs/industry_map.json，结构 {"_meta": {...}, "map": {code: 行业}}。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import emapi  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "viewer", "logs", "industry_map.json")

# 沪深主板 + 创业板 + 科创板 + 北交所
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def main() -> None:
    got: dict[str, str] = {}
    fail = 0
    for pn in range(1, 90):
        try:
            rows = emapi.clist(FS, "f12,f14,f100", pz=100, pn=pn)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print("第 %d 页失败：%s" % (pn, e))
            if fail >= 3:
                break
            time.sleep(1.0)
            continue
        if not rows:
            break
        for r in rows:
            code = str(r.get("f12") or "").strip()
            ind = str(r.get("f100") or "").strip()
            if len(code) == 6 and ind and ind != "-":
                got[code] = ind
        if pn % 10 == 0:
            print("已取 %d 页，累计 %d 只" % (pn, len(got)))
        time.sleep(0.15)

    if len(got) < 3000:
        raise SystemExit("只拿到 %d 只，太少，不落盘（可能接口变了）" % len(got))

    inds = {}
    for v in got.values():
        inds[v] = inds.get(v, 0) + 1
    big = sorted(inds.items(), key=lambda x: -x[1])
    print("\n行业数 %d；最大的 10 个：" % len(inds))
    for k, v in big[:10]:
        print("  %-12s %d" % (k, v))

    json.dump({"_meta": {"src": "eastmoney clist f100", "n": len(got),
                         "n_industry": len(inds)},
               "map": got},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("\n写出 %s（%d 只 / %d 个行业）" % (OUT, len(got), len(inds)))


if __name__ == "__main__":
    main()
