# -*- coding: utf-8 -*-
"""FAIL-D self-proof: machine/bot authors must be gone from consensus & cohold.

BEFORE = live :8765 (has C+D but NOT the bot filter — what QA saw).
AFTER  = isolated instance running the CURRENT on-disk code.

Run:  .venv\\Scripts\\python.exe tools\\_holdfix\\botverify.py
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
from db import is_bot_author  # noqa: E402
import server as S  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


def get(base, path):
    with OPENER.open(base + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def consensus_authors(base, oq=False):
    rows = get(base, "/api/consensus?min_holders=2" + ("&only_qualified=1" if oq else ""))
    names = set()
    for x in rows:
        for p in x.get("players", []):
            names.add(p.get("player") or "")
    return rows, names


def cohold_authors(base, oq=False):
    d = get(base, "/api/cohold_recent?days=5&min_holders=2&anchor=auto"
            + ("&only_qualified=1" if oq else ""))
    names = set()
    for g in ("buy", "sell", "hold"):
        for x in d.get(g) or []:
            for p in x.get("players", []):
                names.add(p.get("player") or "")
    return d, names


def bot_hits(names):
    return sorted(n for n in names if n and is_bot_author(n))


def report(tag, base):
    out("=" * 60)
    out("### %s  (base=%s)" % (tag, base))
    c, cn = consensus_authors(base, False)
    cq, cqn = consensus_authors(base, True)
    ch, chn = cohold_authors(base, False)
    chq, chqn = cohold_authors(base, True)
    out("  consensus        rows=%d  authors=%d  BOT=%s" % (len(c), len(cn), bot_hits(cn)))
    out("  consensus TierA  rows=%d  authors=%d  BOT=%s" % (len(cq), len(cqn), bot_hits(cqn)))
    out("  cohold           authors=%d  BOT=%s" % (len(chn), bot_hits(chn)))
    out("  cohold   TierA   authors=%d  BOT=%s" % (len(chqn), bot_hits(chqn)))
    return {
        "consensus_bots": bot_hits(cn), "consensus_tierA_bots": bot_hits(cqn),
        "cohold_bots": bot_hits(chn), "cohold_tierA_bots": bot_hits(chqn),
    }


def main():
    # ---- BEFORE: live production instance (C+D present, bot filter absent) ----
    try:
        before = report("BEFORE  live :8765 (bot filter NOT yet deployed)", "http://127.0.0.1:8765")
    except Exception as exc:  # noqa: BLE001
        out("BEFORE skipped (live 8765 unreachable): %r" % (exc,))
        before = None

    # ---- AFTER: isolated instance running current on-disk code ----
    srv = S.create_server(port=8795)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        after = report("AFTER   isolated :8795 (current code with bot filter)", "http://127.0.0.1:8795")
    finally:
        srv.shutdown()
        srv.server_close()

    out("")
    out("### VERDICT")
    ok = (not after["consensus_bots"] and not after["consensus_tierA_bots"]
          and not after["cohold_bots"] and not after["cohold_tierA_bots"])
    out("  AFTER has zero bot authors in consensus(全量/TierA) & cohold(buy/sell/hold, 全量/TierA): %s" % ok)
    if before:
        out("  BEFORE(A) bot authors in consensus:      %s" % before["consensus_bots"])
        out("  BEFORE(A) bot authors in consensus TierA: %s" % before["consensus_tierA_bots"])
        out("  BEFORE(A) bot authors in cohold:          %s" % before["cohold_bots"])

    with io.open("tools/_holdfix/botverify_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


if __name__ == "__main__":
    main()
