# -*- coding: utf-8 -*-
"""逐项对比：为何 _intraday_rank 拿不到 push2 clist 数据。"""
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
    import emapi
    import moneyflow

    def once(tag, **kw):
        t0 = time.time()
        try:
            rows = emapi.clist(kw.pop("fs"), moneyflow._RANK_FIELDS, **kw)
            return {"tag": tag, "secs": round(time.time() - t0, 3), "n": len(rows)}
        except Exception as e:  # noqa: BLE001
            return {"tag": tag, "secs": round(time.time() - t0, 3), "err": repr(e)}

    out["A_a_share_fs_pz5_t3"] = once("A", fs=moneyflow._A_SHARE_FS, pz=5, fid="f62", po=1, timeout=3, retries=0)
    out["B_a_share_fs_pz20_t2"] = once("B", fs=moneyflow._A_SHARE_FS, pz=20, fid="f62", po=1, timeout=2, retries=0)
    out["C_fs_fund_rank_pz5_t3"] = once("C", fs=emapi.FS_FUND_RANK, pz=5, fid="f62", po=1, timeout=3, retries=0)
    out["D_intraday_rank"] = None
    t0 = time.time()
    intra = moneyflow._intraday_rank(20)
    out["D_intraday_rank"] = {"secs": round(time.time() - t0, 3),
                              "ok": bool(intra and intra.get("rows")),
                              "n": len((intra or {}).get("rows") or [])}
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_clist_cmp.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
