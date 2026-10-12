# -*- coding: utf-8 -*-
"""P2/P0 事实核对：直接打 push2 clist(f62) 与 新浪/腾讯/东财 基准时间戳（绕过服务）。"""
import json
import os
import sys
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

out = {}
try:
    import emapi

    # 1) push2 clist 盘中榜是否可达
    t0 = time.time()
    rows = emapi.clist(emapi.FS_FUND_RANK, "f2,f3,f12,f14,f62,f184,f66,f72", pz=5, fid="f62", po=1,
                       timeout=3, retries=0)
    out["clist_secs"] = round(time.time() - t0, 3)
    out["clist_n"] = len(rows)
    out["clist_sample"] = [{k: r.get(k) for k in ("f12", "f14", "f2", "f62")} for r in rows[:3]]

    # 2) 三源基准时间戳（上证指数 sh000001）
    try:
        out["tencent_000001"] = emapi.tencent_quote_multi(["1.000001"], timeout=3).get("1.000001")
    except Exception as e:  # noqa: BLE001
        out["tencent_err"] = repr(e)

    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        txt = op.open(urllib.request.Request(
            "https://hq.sinajs.cn/list=sh000001",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}),
            timeout=3).read().decode("gbk", "ignore")
        f = txt.split("=", 1)[1].strip().strip(";").strip('"').split(",")
        out["sina_000001"] = {"name": f[0], "price": f[3], "date": f[30], "time": f[31]}
    except Exception as e:  # noqa: BLE001
        out["sina_err"] = repr(e)

    try:
        out["em_quote_000001"] = emapi.quote("1.000001", timeout=3, retries=0)
    except Exception as e:  # noqa: BLE001
        out["em_err"] = repr(e)

    out["server_now"] = time.strftime("%Y-%m-%d %H:%M:%S")
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_p2_facts.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
