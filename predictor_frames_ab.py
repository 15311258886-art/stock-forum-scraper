"""三层框架离线 A/B（用已复盘真实样本验证是否真提升命中率）。

验证两件事：
  (A) 重排：每只票 NEW分 = DB旧分(含动量冷却+重校准) + 板块加成 + 股东加成，
      按预测日取核心K只重排，看 NEW 是否把更优票顶到前面（门禁）。
  (B) 子集：旧逻辑选出的票里，命中「热板块+股东优」标签的子集是否跑得更好（诚实说明框架有效）。

注意：板块热度/股东结构用的是「当前快照」数据（policy_news/overseas/sentiment 时间戳不可靠，
无法精确回溯到预测日），所以这是 best-effort 代理验证，不是完美样本外测试。
"""
import sys, os, sqlite3, json, statistics
sys.path.insert(0, "viewer")
import predictor
from collections import defaultdict
con = predictor.connect()
con.row_factory = sqlite3.Row

hot_boards, sym_board = predictor._hot_sectors(con)
hot_words = {w for w, _ in hot_boards[:predictor.TOP_BOARDS]}
_codes, _n, _b = predictor.SS.universe(predictor.POOL_LIMIT)
conc_map, holder30_set = predictor._share_conc(con, _codes)
print("热度榜前%d: %s" % (predictor.TOP_BOARDS, "、".join("%s" % w for w, _ in hot_boards[:predictor.TOP_BOARDS])))

rows = list(con.execute("SELECT * FROM prediction_item WHERE ret_t1 IS NOT NULL"))
print("已复盘样本:", len(rows))


def new_score(r):
    s = float(r["score"] or 0.0)
    sb = sym_board.get(r["symbol"])
    if sb and sb[0] in hot_words:
        s += predictor.BOARD_BONUS
    c = conc_map.get(r["symbol"])
    if c:
        s += predictor.CONC_BONUS.get(c[0], 0.0)
    if r["symbol"] in holder30_set:
        s += predictor.HOLDER30_BONUS
    return s


by_date = defaultdict(list)
for r in rows:
    by_date[r["run_date"]].append(r)


def measure(items):
    """返回真实命中数 + 命中率 + 均值收益，避免重复缩放。"""
    n = len(items)
    if n == 0:
        return (0, 0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0)
    h1 = sum(1 for r in items if r["hit_t1"])
    ret1 = [r["ret_t1"] for r in items]
    b1 = [r["bench_t1"] for r in items if r["bench_t1"] is not None]
    s3 = [r for r in items if r["ret_t3"] is not None]
    n3 = len(s3)
    h3 = sum(1 for r in s3 if r["hit_t3"])
    ret3 = [r["ret_t3"] for r in s3]
    b3 = [r["bench_t3"] for r in s3 if r["bench_t3"] is not None]
    return (n, h1, 100 * h1 / n,
            statistics.mean(ret1) if ret1 else 0.0,
            statistics.mean(b1) if b1 else 0.0,
            n3, h3, 100 * h3 / n3 if n3 else 0.0,
            statistics.mean(ret3) if ret3 else 0.0,
            statistics.mean(b3) if b3 else 0.0)


def topset(day_items, key, K):
    return sorted(day_items, key=lambda r: -key(r))[:K]


print("\n=== (A) 重排 A/B：每日取核心K，OLD=DB旧分 / NEW=旧分+三层框架加成 ===")
for K in (8, 10):
    old_set, new_set = [], []
    for rd, its in by_date.items():
        old_set += topset(its, lambda r: float(r["score"] or 0), K)
        new_set += topset(its, new_score, K)
    for label, s in (("OLD", old_set), ("NEW", new_set)):
        n, h1, hp1, r1, b1, n3, h3, hp3, r3, b3 = measure(s)
        print("%s 核心%-2d: T+1 %2d/%2d=%.0f%% 收益%+.2f%%(池%+.2f%%) | T+3 %2d/%2d=%.0f%% 收益%+.2f%%(池%+.2f%%)"
              % (label, K, h1, n, hp1, r1, b1, h3, n3, hp3, r3, b3))

print("\n=== (B) 子集分析：不重排，看框架标签子集是否跑得更好 ===")
def grp(sub, label):
    n, h1, hp1, r1, b1, n3, h3, hp3, r3, b3 = measure(sub)
    print("%-16s n=%-2d T+1 %.0f%%(%d/%d, 收益%+.2f%%/池%+.2f%%) T+3 %.0f%%(%d/%d, 收益%+.2f%%/池%+.2f%%)"
          % (label, n, hp1, h1, n, r1, b1, hp3, h3, n3, r3, b3))

grp(rows, "全样本OLD")
grp([r for r in rows if sym_board.get(r["symbol"]) and sym_board[r["symbol"]][0] in hot_words], "仅热板块")
grp([r for r in rows
      if (sym_board.get(r["symbol"]) and sym_board[r["symbol"]][0] in hot_words)
      and (conc_map.get(r["symbol"], ("分散",))[0] in ("非常集中", "集中", "相对集中")
           or r["symbol"] in holder30_set)], "热板块+股东优")
con.close()
