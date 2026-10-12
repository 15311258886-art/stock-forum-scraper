# -*- coding: utf-8 -*-
"""Runtime verification for fix-ui-watchlist (Tasks A/B/C).
Does NOT restart the live service; imports modules fresh and calls API fns directly.
DB is pointed at a temp file so the live viewer/app.db is never touched.
"""
import os
import sys
import time
import json

PROJ = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
sys.path.insert(0, PROJ)

TMP_DB = os.path.join(PROJ, "_verify_tmp_app.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["VIEWER_DB"] = TMP_DB  # isolate from live viewer/app.db

print("=== IMPORT ===")
t0 = time.time()
import screener
import moneyflow
from viewer import server
import watchlib
print("import ok in %.2fs" % (time.time() - t0))

# ---------------- TASK A: 智能选股 default = worth (no 东财 snapshot) ----------------
print("\n--- TASK A: server.api_screener(screen='worth', limit=20) ---")
t0 = time.time()
try:
    res = server.api_screener(screen="worth", limit=20, params={})
    dt = time.time() - t0
    ok = res.get("ok")
    rows = res.get("rows") or []
    print("time=%.2fs ok=%s rows=%d reason=%s desc=%s"
          % (dt, ok, len(rows), res.get("reason"), res.get("desc")))
    if rows:
        print("sample:", json.dumps(rows[0], ensure_ascii=False)[:240])
    print("TASK_A_PASS" if (ok and len(rows) > 0) else "TASK_A_FAIL")
except Exception as e:  # noqa: BLE001
    print("TASK_A_EXCEPTION", repr(e), "after %.2fs" % (time.time() - t0))

# ---------------- TASK B: _watch_prices multi-stock, 东财 down -> 新浪 fallback ----------------
print("\n--- TASK B: server._watch_prices (20 codes, budget=9.0) ---")
codes = ["600519", "000001", "300750", "601318", "000858", "002594", "600036", "600276",
         "000333", "600900", "601899", "000651", "600030", "002415", "600887", "601166",
         "600000", "000002", "601398", "600028"]
t0 = time.time()
try:
    px = server._watch_prices(codes, budget=9.0)
    dt = time.time() - t0
    got = [c for c in codes if (px.get(c) or {}).get("price") is not None]
    print("time=%.2fs budget=9.0 got=%d/%d" % (dt, len(got), len(codes)))
    for c in codes[:6]:
        print("   ", c, px.get(c))
    print("TASK_B_PASS" if (dt <= 11 and len(got) >= len(codes) - 2) else "TASK_B_FAIL")
except Exception as e:  # noqa: BLE001
    print("TASK_B_EXCEPTION", repr(e), "after %.2fs" % (time.time() - t0))

# ---------------- TASK B e2e: api_watch_list with seeded temp DB ----------------
print("\n--- TASK B e2e: server.api_watch_list (seeded temp DB) ---")
try:
    with server._WRITE_LOCK:
        watchlib.ensure_schema(server.DB.conn)
        watchlib.seed_default_groups(server.DB.conn)
        for c in codes[:6]:
            try:
                watchlib.add_item(server.DB.conn, c, name="", kind="stock")
            except Exception:  # noqa: BLE001
                pass
    t0 = time.time()
    wl = server.api_watch_list()
    dt = time.time() - t0
    items = wl.get("items") or []
    priced = [it for it in items if it.get("price") is not None]
    print("time=%.2fs ok=%s items=%d priced=%d"
          % (dt, wl.get("ok"), len(items), len(priced)))
    print("TASK_B_E2E_PASS" if (wl.get("ok") and dt <= 12) else "TASK_B_E2E_FAIL")
except Exception as e:  # noqa: BLE001
    print("TASK_B_E2E_EXCEPTION", repr(e), "after %.2fs" % (time.time() - t0))

# ---------------- TASK C: moneyflow honest message when 东财 unreachable ----------------
print("\n--- TASK C: moneyflow.rank / board_rank (force=True) ---")
try:
    r1 = moneyflow.rank(pz=30, force=True)
    r2 = moneyflow.board_rank(pz=30, force=True)
    print("rank  ok=%s reason=%s" % (r1.get("ok"), str(r1.get("reason"))[:90]))
    print("board ok=%s reason=%s" % (r2.get("ok"), str(r2.get("reason"))[:90]))
    good = (not r1.get("ok") and "数据源暂不可用" in str(r1.get("reason")) and
            not r2.get("ok") and "数据源暂不可用" in str(r2.get("reason")))
    print("TASK_C_PASS" if good else "TASK_C_FAIL")
except Exception as e:  # noqa: BLE001
    print("TASK_C_EXCEPTION", repr(e))

print("\n=== DONE ===")
