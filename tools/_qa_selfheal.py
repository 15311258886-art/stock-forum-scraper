# -*- coding: utf-8 -*-
"""E15 自愈机制独立复现（读接口读时自愈）。
只写 blogger_stats 表，测完全量还原并逐行校验无残留。"""
from __future__ import annotations
import json, sqlite3, urllib.request

DB = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db"
BASE = "http://127.0.0.1:8765"
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_stats():
    with _OP.open(BASE + "/api/blogger/stats", timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def rows(c):
    return {r["blogger"]: dict(r) for r in c.execute("SELECT * FROM blogger_stats")}


def main():
    c = conn()
    orig = rows(c)
    print("原始 blogger_stats:")
    for k, v in orig.items():
        print("   ", k, "| n_stock=", v["n_stock"], "| n_pending=", v["n_pending"], "| src=", v["src_updated"])

    # ---------- E15-literal：只改一行（任务清单描述的写法） ----------
    print("\n== E15-literal：仅把「独股九剑」src_updated→2000-01-01 且 n_stock→999 ==")
    c.execute("UPDATE blogger_stats SET src_updated='2000-01-01', n_stock=999 WHERE blogger='独股九剑'")
    c.commit()
    after_corrupt = rows(c)["独股九剑"]
    print("  改后 DB:", after_corrupt["n_stock"], after_corrupt["src_updated"])
    d = get_stats()
    api_djj = next((x for x in d["rows"] if x["blogger"] == "独股九剑"), None)
    db_djj = rows(c)["独股九剑"]
    print("  接口返回 独股九剑:", api_djj["n_stock"], "/", api_djj["n_pending"])
    print("  读后 DB 独股九剑:", db_djj["n_stock"], "/", db_djj["src_updated"])
    literal_healed = (db_djj["n_stock"] == 53 and db_djj["src_updated"] == "2026-10-09 12:16")
    print("  => 单行改坏是否被自愈纠正:", literal_healed)
    print("  （注：表级 MAX(src_updated) 仍取自其它 5 行未变的 '2026-10-09 12:16'，"
          "不构成「比 reco 陈旧」，故自愈不触发——这是设计使然）")

    # 还原
    o = orig["独股九剑"]
    c.execute("UPDATE blogger_stats SET n_stock=?, n_pending=?, avg_ret=?, avg_ret_1w=?, up_ratio=?, "
              "max_ret=?, min_ret=?, src_updated=? WHERE blogger='独股九剑'",
              (o["n_stock"], o["n_pending"], o["avg_ret"], o["avg_ret_1w"], o["up_ratio"],
               o["max_ret"], o["min_ret"], o["src_updated"]))
    c.commit()

    # ---------- E15-real：模拟「reco 比 stats 新」的真实场景 ----------
    print("\n== E15-real：把所有行 src_updated→2000-01-01（表级陈旧）+ 独股九剑 n_stock→999 ==")
    c.execute("UPDATE blogger_stats SET src_updated='2000-01-01'")
    c.execute("UPDATE blogger_stats SET n_stock=999 WHERE blogger='独股九剑'")
    c.commit()
    print("  改后 DB 独股九剑:", rows(c)["独股九剑"]["n_stock"], "| 表 MAX src:", 
          c.execute("SELECT MAX(src_updated) FROM blogger_stats").fetchone()[0],
          "| reco MAX src:", c.execute("SELECT MAX(src_updated) FROM blogger_reco").fetchone()[0])
    d2 = get_stats()
    api2 = next((x for x in d2["rows"] if x["blogger"] == "独股九剑"), None)
    db2 = rows(c)
    healed = (db2["独股九剑"]["n_stock"] == 53 and db2["独股九剑"]["src_updated"] == "2026-10-09 12:16"
              and api2["n_stock"] == 53 and api2["n_pending"] == 73)
    print("  接口返回 独股九剑: n_stock=", api2["n_stock"], "n_pending=", api2["n_pending"])
    print("  读后 DB 独股九剑: n_stock=", db2["独股九剑"]["n_stock"], "src=", db2["独股九剑"]["src_updated"])
    print("  读后 DB 全部行 src:", sorted({v["src_updated"] for v in db2.values()}))
    print("  => 表级陈旧是否触发自愈并纠正:", healed)

    # ---------- 残留校验 ----------
    final = rows(c)
    residual = []
    def same(a, b):
        if a is None or b is None:
            return a is None and b is None
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-9
        return a == b
    for k, v in orig.items():
        f = final.get(k)
        if f is None:
            residual.append((k, "行丢失"))
            continue
        for col in ("n_stock", "n_pending", "avg_ret", "avg_ret_1w", "up_ratio", "max_ret", "min_ret", "src_updated"):
            if not same(v[col], f[col]):
                residual.append((k, col, v[col], f[col]))
    print("\n== 残留校验 ==")
    print("  残留差异:", residual if residual else "无（已完全还原）")
    print("  行数:", len(final), "（原", len(orig), "）")
    c.close()
    return 0 if (healed and not residual) else 1


if __name__ == "__main__":
    raise SystemExit(main())
