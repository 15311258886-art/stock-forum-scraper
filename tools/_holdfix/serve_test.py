# -*- coding: utf-8 -*-
"""Spin up a throwaway instance of the (modified) viewer server on port 8766,
request the affected endpoints over real HTTP, dump the JSON, then shut down.

The live production instance on 8765 is left untouched (this bypasses
main()/_acquire_server_instance by calling create_server() directly).

Run:  .venv\\Scripts\\python.exe tools\\_holdfix\\serve_test.py
"""
import io
import json
import os
import sys
import threading
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

import server as S  # noqa: E402

PORT = int(os.environ.get("HOLDFIX_PORT", "8791"))
OUT = os.path.join("tools", "_holdfix")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


def get(path):
    with OPENER.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    srv = S.create_server(port=PORT)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        out("# AFTER /api/consensus?min_holders=2")
        cons = get("/api/consensus?min_holders=2")
        _dump_cons(cons)
        with io.open(os.path.join(OUT, "after_consensus.json"), "w", encoding="utf-8") as fh:
            json.dump(cons, fh, ensure_ascii=False)

        out("")
        out("# AFTER /api/consensus?min_holders=2&only_qualified=1  (仅 Tier A)")
        consq = get("/api/consensus?min_holders=2&only_qualified=1")
        _dump_cons(consq)
        with io.open(os.path.join(OUT, "after_consensus_tierA.json"), "w", encoding="utf-8") as fh:
            json.dump(consq, fh, ensure_ascii=False)

        out("")
        out("# AFTER /api/cohold_recent?days=5&min_holders=2&anchor=auto")
        ch = get("/api/cohold_recent?days=5&min_holders=2&anchor=auto")
        _dump_cohold(ch)
        with io.open(os.path.join(OUT, "after_cohold.json"), "w", encoding="utf-8") as fh:
            json.dump(ch, fh, ensure_ascii=False)

        out("")
        out("# AFTER /api/cohold_recent?...&only_qualified=1")
        chq = get("/api/cohold_recent?days=5&min_holders=2&anchor=auto&only_qualified=1")
        out("  only_qualified=%s tier_scope=%r" % (chq.get("only_qualified"), chq.get("tier_scope")))
        for g in ("buy", "hold", "sell"):
            out("  [%s] %d 组" % (g, len(chq.get(g) or [])))
        with io.open(os.path.join(OUT, "after_cohold_tierA.json"), "w", encoding="utf-8") as fh:
            json.dump(chq, fh, ensure_ascii=False)

        out("")
        out("# AFTER /api/cohold_recent?days=30&min_holders=2&only_qualified=1  (放宽窗口看 Tier A)")
        chq30 = get("/api/cohold_recent?days=30&min_holders=2&anchor=auto&only_qualified=1")
        out("  window %s ~ %s" % (chq30.get("window_start"), chq30.get("window_end")))
        for g in ("buy", "hold", "sell"):
            rows = chq30.get(g) or []
            out("  [%s] %d 组" % (g, len(rows)))
            for x in rows[:8]:
                out("     %7s %-9s pc=%s" % (x["symbol"], (x.get("symbol_name") or "")[:8], x["player_count"]))
    finally:
        srv.shutdown()
        srv.server_close()

    with io.open(os.path.join(OUT, "after_summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


def _dump_cons(cons):
    out("  rows:", len(cons))
    for x in cons[:10]:
        out("  %7s %-9s total=%3s buy=%3s hold=%3s watch=%3s" % (
            x["symbol"], (x.get("symbol_name") or "")[:8], x.get("holders"),
            x.get("buy_holders"), x.get("hold_holders"), x.get("watch_holders")))


def _dump_cohold(ch):
    out("  window %s ~ %s" % (ch.get("window_start"), ch.get("window_end")))
    for g in ("buy", "hold", "sell"):
        rows = ch.get(g) or []
        out("  [%s] %d 组" % (g, len(rows)))
        for x in rows[:8]:
            out("     %7s %-9s pc=%s holds=%s" % (
                x["symbol"], (x.get("symbol_name") or "")[:8], x["player_count"], x.get("still_holding")))


if __name__ == "__main__":
    main()
