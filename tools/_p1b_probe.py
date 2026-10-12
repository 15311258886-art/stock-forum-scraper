# -*- coding: utf-8 -*-
"""P1 收尾验收：race_priority 语义 + 强制东财不可用的回退实测 + /api/quote bps 实测；P0 层级核对。"""
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


def httpx_json(path):
    with OP.open(BASE + path, timeout=40) as r:
        return r.status, json.loads(r.read().decode("utf-8", "ignore")), None


try:
    import emapi

    # ---- 1) race_priority 语义（确定性，无网络）----
    def sleep_ret(v, delay):
        def _f():
            time.sleep(delay)
            return v
        return _f

    def boom():
        raise RuntimeError("em down")

    t0 = time.time()
    r = emapi.race_priority(sleep_ret("EM", 5.0), [sleep_ret("TX", 0.2)], grace=1.0, timeout=6.0)
    out["case_primary_hang"] = {"ret": r, "secs": round(time.time() - t0, 3),
                                "expect": "TX ~1.0s"}
    t0 = time.time()
    r = emapi.race_priority(boom, [sleep_ret("TX", 0.2)], grace=1.0, timeout=6.0)
    out["case_primary_fail"] = {"ret": r, "secs": round(time.time() - t0, 3),
                                "expect": "TX ~0.2s"}
    t0 = time.time()
    r = emapi.race_priority(sleep_ret("EM", 0.3), [sleep_ret("TX", 0.5)], grace=1.0, timeout=6.0)
    out["case_primary_fast"] = {"ret": r, "secs": round(time.time() - t0, 3),
                                "expect": "EM ~0.3s"}

    # ---- 2) 强制东财不可用（emapi 层注入）+ 回退实测 ----
    _orig = emapi._get_text

    def _patched(url, referer, **kw):
        if "push2.eastmoney.com" in url:
            raise RuntimeError("forced EM down")
        return _orig(url, referer, **kw)

    emapi._get_text = _patched
    try:
        def em_try():
            q = emapi.quote("1.600519", timeout=3, retries=0)
            return ("EM", q) if q else None

        def tx_try():
            tx = emapi.tencent_quote("600519", timeout=3)
            return ("TX", tx) if tx else None

        t0 = time.time()
        r = emapi.race_priority(em_try, [tx_try], grace=1.4, timeout=3.0)
        out["forced_em_down"] = {"src": (r[0] if r else None),
                                 "secs": round(time.time() - t0, 3),
                                 "has_price": bool(r and (r[1] or {}).get("price"))}
    finally:
        emapi._get_text = _orig

    # ---- 3) 真实 /api/quote 连打 5 次（东财健康时应走 EM 且 bps 非 null）----
    qs = []
    for _ in range(5):
        t0 = time.time()
        st, j, _ = httpx_json("/api/quote?symbol=600519&force=1")
        qs.append({"status": st, "secs": round(time.time() - t0, 3),
                   "source": (j.get("source") or "")[:14],
                   "bps": (j.get("valuation") or {}).get("bps"),
                   "pe_ttm": (j.get("valuation") or {}).get("pe_ttm"),
                   "price": (j.get("quote") or {}).get("price")})
    out["quote_live"] = qs

    # ---- 4) P0 层级核对 ----
    st, j, _ = httpx_json("/api/market_freshness")
    out["market_freshness"] = {"status": st, "top_keys": sorted(j.keys()), "body": j}
    st, j, _ = httpx_json("/api/refresh_status")
    out["refresh_status_market"] = {"status": st, "has_market_key": "market" in j,
                                    "market": j.get("market")}
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_p1b_probe.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
