# -*- coding: utf-8 -*-
"""Directly exercise the modified DB layer (no server restart needed).

Run:  .venv\\Scripts\\python.exe tools\\_holdfix\\dbtest.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "viewer"))
import db as dbmod  # noqa: E402

L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


self = dbmod.Database("viewer/app.db")

out("### consensus_stocks(min_holders=2)  [全量]")
for x in self.consensus_stocks(min_holders=2)[:10]:
    out("  %7s %-9s total=%3s  buy=%3s hold=%3s watch=%3s  w=%s bw=%s" % (
        x["symbol"], (x.get("symbol_name") or "")[:8], x["holders"],
        x["buy_holders"], x["hold_holders"], x["watch_holders"],
        x["weighted_holders"], x["buy_weighted"]))

out("")
out("### consensus_stocks(min_holders=2, only_qualified=True)  [仅 Tier A]")
for x in self.consensus_stocks(min_holders=2, only_qualified=True)[:10]:
    out("  %7s %-9s total=%3s  buy=%3s hold=%3s watch=%3s  w=%s bw=%s" % (
        x["symbol"], (x.get("symbol_name") or "")[:8], x["holders"],
        x["buy_holders"], x["hold_holders"], x["watch_holders"],
        x["weighted_holders"], x["buy_weighted"]))

out("")
out("### cohold_recent(days=5)  [全量]")
d = self.cohold_recent(days=5, min_holders=2)
out("  window", d["window_start"], "~", d["window_end"], "| only_qualified", d["only_qualified"])
for g in ("buy", "hold", "sell"):
    out("  [%s] %d 组" % (g, len(d[g])))
    for x in d[g][:8]:
        out("     %7s %-9s pc=%s holds=%s" % (
            x["symbol"], (x.get("symbol_name") or "")[:8], x["player_count"], x["still_holding"]))

out("")
out("### cohold_recent(days=5, only_qualified=True)  [仅 Tier A]")
d2 = self.cohold_recent(days=5, min_holders=2, only_qualified=True)
out("  only_qualified", d2["only_qualified"], "tier_scope", d2["tier_scope"])
for g in ("buy", "hold", "sell"):
    out("  [%s] %d 组" % (g, len(d2[g])))
    for x in d2[g][:8]:
        out("     %7s %-9s pc=%s" % (x["symbol"], (x.get("symbol_name") or "")[:8], x["player_count"]))

with io.open(os.path.join(HERE, "dbtest_summary.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
