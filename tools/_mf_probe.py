# -*- coding: utf-8 -*-
"""核对 moneyflow.rank(force=True) 的盘中/日终选择。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

out = {}
try:
    import moneyflow
    out["is_trading_time"] = moneyflow._is_trading_time()
    t0 = time.time()
    r = moneyflow.rank(pz=20, force=True)
    out["rank_secs"] = round(time.time() - t0, 3)
    out["rank"] = {k: r.get(k) for k in ("ok", "source", "source_label", "data_date",
                                         "data_time", "is_intraday", "trade_date")}
    out["rank_n"] = len(r.get("rows") or [])
    out["rank_sample"] = [{k: x.get(k) for k in ("code", "name", "main_net")}
                          for x in (r.get("rows") or [])[:3]]
    # 直接看 _intraday_rank 单独结果
    t1 = time.time()
    intra = moneyflow._intraday_rank(20)
    out["intra_secs"] = round(time.time() - t1, 3)
    out["intra_ok"] = bool(intra and intra.get("rows"))
    out["intra_n"] = len((intra or {}).get("rows") or [])
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_mf_probe.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
