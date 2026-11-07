"""把回测裁决写进 tool_registry，让结论变成产品行为，而不是只躺在报告里。

在此之前的状态：33 个工具在货架上，`evidence` 是人写的「实测表现」，
和 2952 格矩阵跑出来的东西没有任何连接。这个脚本就是那条连接。

写三样东西：
  bt_verdict / bt_regime / bt_tier / bt_edge / bt_at —— 回测结论（可审计、可追溯）
  status      —— 全行情反向的直接转 shadow（还在货架、可手动用，但不再参与进出决策）
  advisory    —— 同上，1 = 说明性标注

规则（写死）：
  D 采纳 / C 有条件           → status=active，advisory=0（让它真的参与决策）
  B 否决·反向 且 全行情反向    → status=shadow，advisory=1
  B 否决·反向 但只局部反向     → advisory=1，status 不动（它可能在别的行情里还有用，不擅自下架）
  A 无证据                    → advisory=1

默认 dry-run，加 --apply 才写库。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE, "viewer", "logs")
APP_DB = os.path.join(BASE, "viewer", "app.db")

COLS = [
    ("bt_verdict", "TEXT NOT NULL DEFAULT ''"),
    ("bt_regime", "TEXT NOT NULL DEFAULT ''"),
    ("bt_tier", "TEXT NOT NULL DEFAULT ''"),
    ("bt_edge", "REAL"),
    ("bt_hold", "INTEGER"),
    ("bt_at", "TEXT NOT NULL DEFAULT ''"),
]


def ensure_cols(con: sqlite3.Connection) -> list[str]:
    have = {r[1] for r in con.execute("PRAGMA table_info(tool_registry)")}
    added = []
    for name, decl in COLS:
        if name not in have:
            con.execute("ALTER TABLE tool_registry ADD COLUMN %s %s" % (name, decl))
            added.append(name)
    con.commit()
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--apply", action="store_true", help="真的写库（否则只打印）")
    ap.add_argument("--db", default=APP_DB)
    args = ap.parse_args()

    adj = json.load(open(os.path.join(LOGS, "adjudicate_%s.json" % args.stamp),
                        encoding="utf-8"))["rows"]
    tier = {r["tool"]: r for r in json.load(
        open(os.path.join(LOGS, "tier_assign_%s.json" % args.stamp),
             encoding="utf-8"))["tools"]}

    con = sqlite3.connect(args.db)
    added = ensure_cols(con) if args.apply else []
    have = {r[0] for r in con.execute("SELECT tool_key FROM tool_registry")}

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    plan = []
    for r in adj:
        k = r["tool"]
        if k not in have or k == "random5":
            continue
        t = tier.get(k, {})
        v = r["verdict"]
        allrev = bool(t.get("all_regime_rev"))
        if v.startswith(("D", "C")):
            new_status, new_adv = "active", 0
        elif v.startswith("B"):
            new_status = "shadow" if allrev else None
            new_adv = 1
        else:
            new_status, new_adv = None, 1
        plan.append(dict(tool=k, verdict=v, status=new_status, adv=new_adv,
                         regime=t.get("regime", ""), tier=t.get("tier", ""),
                         edge=(r["best"] or {}).get("diff_pp"),
                         hold=(r["best"] or {}).get("hold"),
                         allrev=allrev))

    if args.apply:
        for p in plan:
            sets = ["bt_verdict=?", "bt_regime=?", "bt_tier=?", "bt_edge=?",
                    "bt_hold=?", "bt_at=?", "advisory=?"]
            vals = [p["verdict"], p["regime"], p["tier"], p["edge"], p["hold"],
                    now, p["adv"]]
            if p["status"]:
                sets.append("status=?")
                vals.append(p["status"])
            vals.append(p["tool"])
            con.execute("UPDATE tool_registry SET %s WHERE tool_key=?"
                        % ",".join(sets), vals)
        con.commit()

    print("=" * 104)
    print("登记表同步 ｜ %s ｜ %s"
          % (os.path.basename(args.db), "已写库" if args.apply else "dry-run（加 --apply 才写）"))
    print("=" * 104)
    if added:
        print("新增列：%s" % "、".join(added))
    print("%-24s %-14s %-8s %-6s %-4s %8s %6s" %
          ("工具", "裁决", "状态变更", "行情", "档位", "超额pp", "全行情反"))
    n = {"shadow": 0, "active": 0}
    for p in sorted(plan, key=lambda x: x["verdict"]):
        chg = p["status"] or "—"
        if p["status"]:
            n[p["status"]] = n.get(p["status"], 0) + 1
        print("%-24s %-14s %-8s %-6s %-4s %8s %6s"
              % (p["tool"], p["verdict"], chg, p["regime"] or "—",
                 p["tier"] or "—",
                 "—" if p["edge"] is None else "%+.3f" % p["edge"],
                 "是" if p["allrev"] else "否"))
    print("\n汇总：转 shadow %d 个，保持/恢复 active %d 个，共写入 %d 行"
          % (n.get("shadow", 0), n.get("active", 0), len(plan)))
    if not args.apply:
        print("（以上仅为预览，未改动数据库）")
    con.close()


if __name__ == "__main__":
    main()
