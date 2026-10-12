"""只读盘点 + 可选修复：清掉「休市日打新日期戳」的脏数据（2026-10-03 王总质疑触发）。

默认 --dry（只打印）；加 --apply 才真写库。改前已备份 app.db.bak-baddate-20261003。
"""
import json
import os
import sqlite3
import sys

DB = os.path.abspath("viewer/app.db")
APPLY = "--apply" in sys.argv

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()


def q(sql, args=()):
    return cur.execute(sql, args).fetchall()


print("=== 1) board_flow_intraday 现状 ===")
for r in q("SELECT trade_date, kind, source, updated_at, length(payload) lp "
           "FROM board_flow_intraday ORDER BY trade_date"):
    print("  ", dict(r))

print("\n=== 2) payload 规模（axis / series 条数）===")
for td in ("2026-09-30", "2026-10-01", "2026-10-03"):
    row = q("SELECT payload FROM board_flow_intraday WHERE trade_date=?", (td,))
    if not row:
        print("  ", td, "none")
        continue
    d = json.loads(row[0][0])
    print("  ", td, "axis=%d series=%d" % (len(d.get("axis") or []), len(d.get("series") or [])))

print("\n=== 3) board_flow_samples 休市日脏行 ===")
for r in q("SELECT trade_date, COUNT(*) n, COUNT(DISTINCT ts) pts, MAX(ts) last_ts "
           "FROM board_flow_samples WHERE trade_date >= '2026-09-29' GROUP BY trade_date"):
    print("  ", dict(r))

if not APPLY:
    print("\n[DRY] 上面就是改动全景；加 --apply 才写库。")
    sys.exit(0)

print("\n=== APPLY ===")
# ① 把 2026-10-03 那条（真实归属 09-30 的完整曲线）补进 09-30 那条（原本只有 10:38 截断版）
row = q("SELECT payload FROM board_flow_intraday WHERE trade_date='2026-10-03'")
row2 = q("SELECT payload FROM board_flow_intraday WHERE trade_date='2026-09-30'")
if row and row2:
    full, trunc = json.loads(row[0][0]), json.loads(row2[0][0])
    merged = {"axis": full.get("axis") or trunc.get("axis") or [],
              "series": full.get("series") or trunc.get("series") or []}
    cur.execute("UPDATE board_flow_intraday SET payload=?, source='eastmoney' "
                "WHERE trade_date='2026-09-30' AND kind='industry'",
                (json.dumps(merged, ensure_ascii=False),))
    print("  09-30 曲线补齐：axis=%d series=%d" % (len(merged["axis"]), len(merged["series"])))

# ② 删掉两条「休市日打今天戳」的假记录
for td in ("2026-10-01", "2026-10-03"):
    n = cur.execute("DELETE FROM board_flow_intraday WHERE trade_date=?", (td,)).rowcount
    print("  删除 intraday %s：%d 行" % (td, n))

# ③ 删掉休市日（10-01）采的样本行——采的那天本来就没开盘，值是上一交易日全日量
n = cur.execute("DELETE FROM board_flow_samples WHERE trade_date='2026-10-01'").rowcount
print("  删除 samples 2026-10-01 休市日采样：%d 行" % n)

con.commit()
print("\n=== 修后复核 ===")
for r in q("SELECT trade_date, kind, source, updated_at FROM board_flow_intraday ORDER BY trade_date"):
    print("  ", dict(r))
print("  samples 最新日期：",
      q("SELECT MAX(trade_date) FROM board_flow_samples")[0][0])
con.close()
