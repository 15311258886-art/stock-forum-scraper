"""验证：近期热点（国内）重构——结构 + 与 topics 表解耦（隔离库，不碰真库）。

断言：
1) build_hotspots 返回三层（main/rhythm/risk），层内按证据强度降序；
2) 命中传导表的词条带 up/down（含核心作用/价值/代表标的），judgment=true；
3) 每个 item.evidence.symbols 用 stock_names 补出了名称（不再全空）；
4) 解耦：清空 topics 表后重算，分层结构与命中词条数完全一致（证明热点视图
   不再依赖 n-gram 抽出的 topics 碎片）。
"""
import os, shutil, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import viewer.db as dbmod
from viewer import hotspots as HS

SRC = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
DST_DIR = os.path.join(ROOT, ".iso_hot")
DST = os.path.join(DST_DIR, "app.db")
os.makedirs(DST_DIR, exist_ok=True)
shutil.copyfile(SRC, DST)

db = dbmod.Database(DST)


def snapshot():
    res = HS.build_hotspots(db)
    layers = res.get("layers", [])
    keys = [L["key"] for L in layers]
    judged = [(L["key"], it["topic"]) for L in layers for it in L["items"] if it["judgment"]]
    return keys, judged, layers


keys, judged, layers = snapshot()
print("layers:", keys)
assert any(k == "main" for k in keys), "缺少 层一 政策与行业主线"
assert any(k == "rhythm" for k in keys), "缺少 层二 交易节奏"
assert any(k == "risk" for k in keys), "缺少 层三 风险预警"

# 层内降序检查
for L in layers:
    s = [it["evidence"]["strength"] for it in L["items"]]
    assert s == sorted(s, reverse=True), f"{L['key']} 层内未按证据强度降序: {s}"

# 命中传导表的词条
print(f"judged(命中传导表) 词条数: {len(judged)}")
assert len(judged) >= 10, f"命中词条过少（{len(judged)}），疑似驱动源异常"
for L in layers:
    for it in L["items"]:
        if not it["judgment"]:
            continue
        assert it["up"] or it["down"], f"{it['topic']} 命中但 up/down 皆空"
        for grp in (it["up"] + it["down"]):
            assert grp.get("sector") and grp.get("role") and grp.get("value"), \
                f"{it['topic']} 领域缺 核心作用/价值: {grp}"

# 涉及标的已补名称（不再全空）
syms = [(it["topic"], s) for L in layers for it in L["items"] for s in it["evidence"]["symbols"]]
named = [s for _, s in syms if s.get("name")]
print(f"涉及标的条数: {len(syms)}，其中有名称: {len(named)}")
assert syms, "没有任何词条带涉及标的"
assert len(named) == len(syms), "涉及标的仍存在空名称（stock_names 未生效）"

# 解耦测试：清空 topics 表后重算，结果应完全一致
before = set(judged)
db.clear_topics()
keys2, judged2, _ = snapshot()
assert set(judged2) == before, f"清空 topics 后结果变化，热点视图仍依赖 topics 表: {set(judged2) ^ before}"
print("解耦 OK：清空 topics 表后热点视图结果不变（不再依赖 n-gram 碎片）")

db.conn.close()
shutil.rmtree(DST_DIR)
print("\n=== 全部通过 ===")
