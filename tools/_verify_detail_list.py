# -*- coding: utf-8 -*-
"""验证「统计卡片数字 ↔ 明细列表条数」严格一致（/api/detail_list）。

隔离策略：把 VIEWER_DB 指向临时库后再 import viewer.server，
确保整个校验过程**绝不触碰真实的 viewer/app.db**。
"""
import os, sys, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmpdir = tempfile.mkdtemp(prefix="verify-detail-")
os.environ["VIEWER_DB"] = str(Path(tmpdir) / "app.db")

from viewer import db as vdb
assert str(vdb.DB_PATH).startswith(tmpdir), f"DB_PATH 未隔离: {vdb.DB_PATH}"

from viewer.demo_data import load_demo          # noqa: E402
import viewer.server as S                       # noqa: E402

load_demo(S.DB, overseas_live=False, confirm=True)   # 写入临时库（显式确认）
counts = S.DB.counts()

kinds = ["raw_pages", "posts", "trades", "news", "players",
         "positions", "persons", "stock_mentions", "topics"]

print(f"{'kind':16} {'卡片数字':>8} {'明细total':>9} {'返回行数':>8}  一致?")
ok = True
for kind in kinds:
    d = S.api_detail_list(kind, limit=1000)
    if "error" in d:
        print(f"{kind:16} {'-':>8} {'-':>9} {'-':>8}  ✗ {d['error']}")
        ok = False
        continue
    card = counts.get(kind)
    same = (d["total"] == card)
    rows_ok = (d["returned"] == min(card, 1000))
    ok = ok and same and rows_ok
    print(f"{kind:16} {card:>8} {d['total']:>9} {d['returned']:>8}  {'✓' if (same and rows_ok) else '✗'}")

# 分页语义：第 2 页应与第 1 页不同（若总数 > 2）
p1 = S.api_detail_list("trades", limit=5, offset=0)
p2 = S.api_detail_list("trades", limit=5, offset=5)
if p1["total"] > 5:
    diff = (p1["rows"] != p2["rows"])
    ok = ok and diff
    print(f"分页 offset 生效: {'✓' if diff else '✗'}")
    print("  第1页首行:", {k: p1['rows'][0][k] for k in ('id', 'symbol', 'symbol_name') if k in p1['rows'][0]})
    print("  第2页首行:", {k: p2['rows'][0][k] for k in ('id', 'symbol', 'symbol_name') if k in p2['rows'][0]})

# 未知 kind 必须优雅降级（而非 500 / 抛异常）
bad = S.api_detail_list("__nope__")
assert isinstance(bad, dict) and bad.get("error") == "unknown kind", bad
print("未知 kind 优雅降级: ✓", "available=", bad.get("available"))

shutil.rmtree(tmpdir, ignore_errors=True)
print("\n" + ("✅ 校验通过：9 个统计项数字与明细条数严格一致。" if ok else "❌ 校验失败"))
sys.exit(0 if ok else 1)
