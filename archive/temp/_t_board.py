# -*- coding: utf-8 -*-
"""临时单测：board_flow 数据层（用完删）。"""
import json, sqlite3, sys, time
sys.path.insert(0, ".")
import board_flow as bf

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
bf.ensure_schema(conn)
print("ensure_schema OK")

print("\n=== 1) snapshot 行业（新浪主源）===")
t0 = time.time()
s = bf.snapshot("industry", pz=10, force=True)
print(f"  ok={s.get('ok')} source={s.get('source')} rows={len(s.get('rows') or [])} 耗时={time.time()-t0:.1f}s")
for r in (s.get("rows") or [])[:5]:
    print("   ", json.dumps(r, ensure_ascii=False))
if not s.get("ok"):
    print("   reason:", s.get("reason"))

print("\n=== 2) snapshot 概念 ===")
s2 = bf.snapshot("concept", pz=5, force=True)
print(f"  ok={s2.get('ok')} source={s2.get('source')} rows={len(s2.get('rows') or [])}")
for r in (s2.get("rows") or [])[:3]:
    print("   ", json.dumps(r, ensure_ascii=False))

print("\n=== 3) 缓存是否生效（第二次应命中，耗时≈0）===")
t0 = time.time()
s3 = bf.snapshot("industry", pz=10)
print(f"  命中={s3 is s} 耗时={time.time()-t0:.3f}s")

print("\n=== 4) intraday（先试东财，失败退采样）===")
t0 = time.time()
cur = bf.intraday("industry", top=3, force=True, conn=conn)
print(f"  ok={cur.get('ok')} source={cur.get('source')} series={len(cur.get('series') or [])} "
      f"points={len(cur.get('axis') or [])} 耗时={time.time()-t0:.1f}s")
if cur.get("ok"):
    for se in (cur.get("series") or [])[:3]:
        pts = se.get("points") or []
        print(f"    {se['name']}: {len(pts)} 点  首={pts[0] if pts else None} 末={pts[-1] if pts else None} (亿)")
else:
    print("   reason:", cur.get("reason"))

print("\n=== 5) 采样落库 + 兜底曲线 ===")
n = bf.maybe_sample(conn)
print("  maybe_sample 写入:", n)
bf.reset_sample_clock()
n2 = bf.maybe_sample(conn)
print("  再采一次（reset 后）:", n2)
rows = conn.execute("SELECT trade_date, ts, kind, board_code, board_name, main_net, pct "
                    "FROM board_flow_samples LIMIT 4").fetchall()
for r in rows:
    print("   ", dict(r))
cnt = conn.execute("SELECT COUNT(*) FROM board_flow_samples").fetchone()[0]
print("  采样表总条数:", cnt)
bf.clear_cache()
cur2 = bf.intraday("industry", top=6, force=True, conn=conn)
print(f"  兜底曲线 ok={cur2.get('ok')} source={cur2.get('source')} "
      f"series={len(cur2.get('series') or [])} points={len(cur2.get('axis') or [])}")

print("\n=== 6) status 自检 ===")
print(json.dumps(bf.status(conn), ensure_ascii=False, indent=2)[:900])
