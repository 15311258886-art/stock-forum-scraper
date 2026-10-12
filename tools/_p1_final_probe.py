# -*- coding: utf-8 -*-
"""P1 收尾最终验收（2026-10-09）。

覆盖：
  1) /api/quote?symbol=600519&force=1 ×5 —— valuation.bps / finance.bps 是否非 null + 耗时 + 采纳源；
  2) race_priority 语义（确定性）：主源挂住→grace 后用次源；主源快速失败→立刻用次源；主源快→用主源；
     并做一次「emapi 层注入东财失败」的真实网络回退实测（≤3s）；
  3) market 层级核对：/api/market_freshness 与 /api/refresh_status；
  4) 回归：首页 + 若干端点状态码。
结果写 viewer/logs/_p1_final.json。
"""
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


def get(path, timeout=40):
    t0 = time.time()
    try:
        with OP.open(BASE + path, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            return r.status, raw, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, f"ERR:{e}", time.time() - t0


def jload(body):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


try:
    # ---- 1) /api/quote ×5：bps 实测 ----
    qs = []
    for i in range(5):
        st, body, el = get("/api/quote?symbol=600519&force=1")
        d = jload(body) or {}
        val = d.get("valuation") or {}
        fin = d.get("finance") or {}
        q = d.get("quote") or {}
        qs.append({
            "i": i + 1, "status": st, "secs": round(el, 3),
            "source": d.get("source"),
            "valuation_bps": val.get("bps"),
            "finance_bps": fin.get("bps"),
            "pe_ttm": val.get("pe_ttm"),
            "price": q.get("price"),
            "name": d.get("name"),
            "err": d.get("error"),
        })
        time.sleep(0.3)
    out["quote_x5"] = qs
    out["quote_valuation_bps_all_nonnull"] = all(r["valuation_bps"] is not None for r in qs)
    out["quote_sources_prefix"] = [(r["source"] or "")[:6] for r in qs]

    # ---- 2) race_priority 语义 + 真实网络回退 ----
    import emapi

    def boom():
        raise RuntimeError("forced EM down")

    def sleep_ret(v, d):
        def _f():
            time.sleep(d)
            return v
        return _f

    t0 = time.time()
    r1 = emapi.race_priority(sleep_ret("EM", 5.0), [sleep_ret("TX", 0.2)], grace=1.0, timeout=6.0)
    out["race_primary_hang"] = {"ret": r1, "secs": round(time.time() - t0, 3), "expect": "TX ~1.0s"}
    t0 = time.time()
    r2 = emapi.race_priority(boom, [sleep_ret("TX", 0.2)], grace=1.8, timeout=3.0)
    out["race_primary_fail"] = {"ret": r2, "secs": round(time.time() - t0, 3), "expect": "TX ~0.2s (<=3s)"}
    t0 = time.time()
    r3 = emapi.race_priority(sleep_ret("EM", 0.3), [sleep_ret("TX", 0.5)], grace=1.8, timeout=3.0)
    out["race_primary_fast"] = {"ret": r3, "secs": round(time.time() - t0, 3), "expect": "EM ~0.3s"}

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
        rr = emapi.race_priority(em_try, [tx_try], grace=1.8, timeout=3.0)
        out["forced_em_down_realnet"] = {
            "src": (rr[0] if rr else None),
            "secs": round(time.time() - t0, 3),
            "has_price": bool(rr and (rr[1] or {}).get("price")),
        }
    finally:
        emapi._get_text = _orig

    # ---- 3) market 层级 ----
    st, body, el = get("/api/market_freshness")
    mf = jload(body)
    out["market_freshness"] = {
        "status": st, "secs": round(el, 3),
        "top_keys": sorted(mf.keys()) if isinstance(mf, dict) else None,
        "market_type": (type(mf.get("market")).__name__ if isinstance(mf, dict) else None),
        "market_keys": (sorted((mf.get("market") or {}).keys()) if isinstance(mf, dict) else None),
        "body": mf,
    }
    st, body, el = get("/api/refresh_status")
    rs = jload(body)
    mkt = rs.get("market") if isinstance(rs, dict) else None
    out["refresh_status"] = {
        "status": st,
        "has_market_key": isinstance(rs, dict) and ("market" in rs),
        "market_type": type(mkt).__name__,
        "market_ok": ((mkt or {}).get("ok") if isinstance(mkt, dict) else None),
        "market_quote_time": ((mkt or {}).get("quote_time") if isinstance(mkt, dict) else None),
        "market_src": ((mkt or {}).get("src") if isinstance(mkt, dict) else None),
    }

    # ---- 4) 回归 ----
    regs = ["/", "/api/refresh_status", "/api/stock_names",
            "/api/detail_list?kind=posts&limit=5", "/api/hotspots",
            "/api/scheduler/status", "/api/market_freshness",
            "/api/kline?symbol=600519&period=day&limit=250"]
    reg = {}
    for p in regs:
        st, body, el = get(p)
        reg[p] = {"status": st, "secs": round(el, 3)}
    out["regression"] = reg

    st, body, el = get("/api/kline?symbol=600519&period=day&limit=250")
    kb = jload(body) or {}
    out["kline"] = {"status": st, "secs": round(el, 3), "source": kb.get("source"),
                    "keys": sorted(kb.keys()) if isinstance(kb, dict) else None}
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

outpath = os.path.join(ROOT, "viewer", "logs", "_p1_final.json")
with open(outpath, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
print("WROTE", outpath)
