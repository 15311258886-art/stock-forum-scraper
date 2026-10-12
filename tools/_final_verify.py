# -*- coding: utf-8 -*-
"""最终验收：P0 时间戳对齐直接源 + watch_list 取值 + 结构回归。"""
import json
import os
import sys
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8765"

out = {}


def get(path):
    with OP.open(BASE + path, timeout=40) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def getj(path):
    _, b = get(path)
    try:
        return json.loads(b)
    except Exception:  # noqa: BLE001
        return {"_raw": b[:200]}


try:
    # P0：服务时间戳 vs 直接源时间戳（同一秒内对比）
    mf = getj("/api/market_freshness").get("market") or {}
    txt = OP.open(urllib.request.Request(
        "https://hq.sinajs.cn/list=sh000001",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}),
        timeout=4).read().decode("gbk", "ignore")
    f = txt.split("=", 1)[1].strip().strip(";").strip('"').split(",")
    tx = OP.open(urllib.request.Request(
        "https://qt.gtimg.cn/q=sh000001",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}),
        timeout=4).read().decode("gbk", "ignore")
    tp = tx.split("=", 1)[1].strip().strip(";").strip('"').split("~")
    out["P0"] = {
        "service_market": mf,
        "direct_sina": {"time": f"{f[30]} {f[31]}", "price": f[3]},
        "direct_tencent": {"time": f"{tp[30][:4]}-{tp[30][4:6]}-{tp[30][6:8]} "
                                   f"{tp[30][8:10]}:{tp[30][10:12]}:{tp[30][12:14]}", "price": tp[3]},
    }

    # watch_list 取值
    wl = getj("/api/watch_list")
    items = wl.get("items") or wl.get("symbols") or []
    priced = [it for it in items if isinstance(it, dict) and it.get("price")]
    out["watch_list"] = {"top_keys": sorted(wl.keys())[:12], "n_items": len(items),
                         "n_priced": len(priced),
                         "sample": [{k: it.get(k) for k in ("symbol", "name", "price", "pct")}
                                    for it in items[:5]]}

    # 结构核对
    out["quote_keys"] = sorted(getj("/api/quote?symbol=600519").keys())
    kb = getj("/api/kline?symbol=600519&period=day&limit=50")
    out["kline_keys"] = sorted(kb.keys())
    out["kline_source"] = kb.get("source")
    mq = getj("/api/multiquote_preset")
    out["multiquote"] = {"src": mq.get("src"), "n_ok": mq.get("n_ok"),
                         "keys": sorted(mq.keys())}
    out["multiquote_row_keys"] = sorted((mq.get("rows") or [{}])[0].keys())

    # 回归
    reg = {}
    for p in ["/", "/api/refresh_status", "/api/stock_names", "/api/detail_list?kind=posts&limit=5",
              "/api/board_flow_snapshot?kind=industry&pz=10", "/api/scheduler/status",
              "/api/watch_list", "/api/moneyflow_board?pz=10", "/api/kline?symbol=000001",
              "/api/quote?symbol=000001"]:
        st, _ = get(p)
        reg[p] = st
    out["regression"] = reg
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_final_verify.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
