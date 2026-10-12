# -*- coding: utf-8 -*-
"""Ad-hoc probe: capture /api/consensus and /api/cohold_recent from the live viewer.

Run:  .venv\\Scripts\\python.exe tools\\_holdfix\\probe.py [prefix]
Writes <prefix>_consensus.json / <prefix>_cohold.json / <prefix>_summary.txt.
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LINES = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    _LINES.append(s)


def get(path):
    with OPENER.open(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else "before"
    try:
        cons = get("/api/consensus?min_holders=2")
    except Exception as exc:  # noqa: BLE001
        out("consensus FAILED:", exc)
        _flush(prefix)
        return
    with open("tools/_holdfix/%s_consensus.json" % prefix, "w", encoding="utf-8") as fh:
        json.dump(cons, fh, ensure_ascii=False)
    out("consensus rows:", len(cons))
    for x in cons[:12]:
        acts = {}
        for p in x.get("players", []):
            acts[p.get("action")] = acts.get(p.get("action"), 0) + 1
        out("  %7s %-9s holders=%3s buy=%s hold=%s w=%s acts=%s" % (
            x["symbol"], (x.get("symbol_name") or "")[:8], x.get("holders"),
            x.get("buy_holders"), x.get("hold_holders"), x.get("weighted_holders"), acts))

    cohold = get("/api/cohold_recent?days=5&min_holders=2&anchor=auto")
    with open("tools/_holdfix/%s_cohold.json" % prefix, "w", encoding="utf-8") as fh:
        json.dump(cohold, fh, ensure_ascii=False)
    out("--- cohold", cohold.get("window_start"), cohold.get("window_end"),
        "anchor", cohold.get("anchor"))
    for g in ("buy", "sell"):
        out(" ", g, "groups:", len(cohold.get(g) or []))
        for x in (cohold.get(g) or [])[:8]:
            acts = sorted({q.get("action") for q in x.get("players", [])})
            out("     ", x["symbol"], (x.get("symbol_name") or "")[:8],
                "pc=", x.get("player_count"), "acts=", acts)
    _flush(prefix)


def _flush(prefix):
    with open("tools/_holdfix/%s_summary.txt" % prefix, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LINES))


if __name__ == "__main__":
    main()
