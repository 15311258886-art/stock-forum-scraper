# -*- coding: utf-8 -*-
"""P1/P0/P2 验收探针：对 127.0.0.1:8765 打点 + 计时，结果写 JSON（供 Read 查看）。

用法： python tools/_p1_accept.py <label> <outfile>
直连本机（忽略环境代理），逐接口计时并抓关键字段，输出结构化 JSON。
"""
import json
import os
import statistics
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=30):
    url = BASE + path
    t0 = time.time()
    try:
        with OPENER.open(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            return time.time() - t0, r.status, raw
    except Exception as e:  # noqa: BLE001
        return time.time() - t0, None, f"ERR:{e}"


def timed(path, n, timeout=30):
    ts, last_status, last_body = [], None, None
    for _ in range(n):
        dt, st, body = get(path, timeout=timeout)
        ts.append(round(dt, 3))
        last_status, last_body = st, body
    return {"n": n, "secs": ts, "min": min(ts), "med": round(statistics.median(ts), 3),
            "max": max(ts), "status": last_status}


def body_json(body):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {"_raw_head": (body or "")[:200]}


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    out = sys.argv[2] if len(sys.argv) > 2 else "viewer/logs/_p1_accept.json"
    rep = {"label": label, "at": time.strftime("%Y-%m-%d %H:%M:%S")}

    rep["quote"] = timed("/api/quote?symbol=600519&force=1", 5)
    rep["quote_body"] = body_json(get("/api/quote?symbol=600519&force=1")[2])
    rep["kline"] = timed("/api/kline?symbol=600519&period=day&limit=250", 5)
    kb = body_json(get("/api/kline?symbol=600519&period=day&limit=250")[2])
    rep["kline_keys"] = sorted(kb.keys()) if isinstance(kb, dict) else None
    rep["kline_source"] = kb.get("source") if isinstance(kb, dict) else None

    rep["watch_list"] = timed("/api/watch_list", 3)
    rep["multiquote_preset"] = timed("/api/multiquote_preset", 3)
    mq = body_json(get("/api/multiquote_preset")[2])
    rep["multiquote"] = {"src": mq.get("src"), "n_ok": mq.get("n_ok"),
                         "n_rows": len(mq.get("rows") or [])}

    # P0
    dt, st, b = get("/api/market_freshness")
    rep["market_freshness"] = {"status": st, "secs": round(dt, 3), "body": body_json(b)}

    # P2
    dt, st, b = get("/api/moneyflow_rank?pz=20")
    mfr = body_json(b)
    rep["moneyflow_rank"] = {"status": st, "secs": round(dt, 3), "ok": mfr.get("ok"),
                             "source": mfr.get("source"), "source_label": mfr.get("source_label"),
                             "data_date": mfr.get("data_date"), "data_time": mfr.get("data_time"),
                             "is_intraday": mfr.get("is_intraday"),
                             "trade_date": mfr.get("trade_date"),
                             "n_rows": len(mfr.get("rows") or [])}
    dt, st, b = get("/api/moneyflow_stock?symbol=600519")
    mfs = body_json(b)
    rep["moneyflow_stock"] = {"status": st, "secs": round(dt, 3), "ok": mfs.get("ok"),
                              "source": mfs.get("source"), "data_date": mfs.get("data_date"),
                              "data_time": mfs.get("data_time"),
                              "is_intraday": mfs.get("is_intraday"),
                              "date": mfs.get("date"), "main_net": mfs.get("main_net")}

    # 回归：不相干接口 + 首页
    reg = {}
    for p in ["/", "/api/refresh_status", "/api/stock_names", "/api/detail_list?kind=posts&limit=5",
              "/api/hotspots", "/api/board_flow_snapshot?kind=industry&pz=10",
              "/api/scheduler/status", "/api/kb_list"]:
        dt, st, _ = get(p)
        reg[p] = {"status": st, "secs": round(dt, 3)}
    rep["regression"] = reg

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print("WROTE", os.path.abspath(out))


if __name__ == "__main__":
    main()
