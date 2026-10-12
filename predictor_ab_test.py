"""一键筛股/每日预测 —— 新旧规则离线 A/B 回放（2026-10-11）。

用已复盘的真实样本（含 T+1/T+3 实现收益）做新旧规则对比：
  OLD = 实际生成时用的旧权重（库里存的）+ 无动量冷却
  NEW_W = 新权重（T+1/T+3 纳入主轴 + 先验重校准，_compute_weights 现算未落库）
  NEW_WC = 新权重 + 动量冷却（生产实际生效版）

口径：在每个预测日内部，按各规则打分取 top-N（N=该日实际选出的只数）重新排序，
再统计这批重新选出的票的实现命中率/平均收益。命中 = 跑赢候选池同期等权平均。

局限（如实标注）：DB 只存了「被选中」的票，未存同日落选的候选，故 A/B 只能在同一日
已选中集合内重排，无法验证「冷却把热票整个踢出候选池」那部分增益——这部分增益由诊断阶段
的分组实证（前5日未涨 T+3 33%/-0.36% vs 前5日已涨 16%/-4.35%）单独证明。
"""
import sys, os, json, sqlite3
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))
import predictor
from predictor import _f, COOL_THR, COOL_SLOPE, COOL_MAX

DB = os.path.join(ROOT, "viewer", "app.db")


def mom5_of(k, date):
    idx = None
    for i, b in enumerate(k):
        if str(b.get("d", ""))[:10] == date:
            idx = i
            break
    if idx is None or idx < 5:
        return None
    c0 = _f(k[idx - 5].get("c"))
    c1 = _f(k[idx].get("c"))
    return (c1 / c0 - 1.0) if c0 else None


def cool_of(m):
    if m is None or m <= COOL_THR:
        return 1.0
    return max(1.0 - COOL_MAX, 1.0 - (m - COOL_THR) / COOL_SLOPE)


def main():
    con = sqlite3.connect("file:%s?mode=rw" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    # 刷新聚合（把 71 条实现收益回填到 predictor_score 的 n_t1/n_t3，供 _compute_weights 吃）
    predictor._aggregate(con)

    old_w = predictor.load_weights(con)
    new_w, detail = predictor._compute_weights(con)

    print("=== 权重对比（OLD 库存 vs NEW 现算，单位 %）===")
    print("%-10s %8s %8s %8s" % ("信号", "OLD", "NEW", "Δ"))
    for k in predictor.SIGNAL_KEYS:
        o = old_w.get(k, 0) * 100
        n_ = new_w.get(k, 0) * 100
        print("%-10s %7.1f%% %7.1f%% %+6.1f%%" % (predictor.SIGNAL_META[k]["name"], o, n_, n_ - o))

    rows = list(con.execute(
        "SELECT * FROM prediction_item WHERE ret_t1 IS NOT NULL"))
    print("\n已复盘样本（有 ret_t1）: %d 条，覆盖 %d 个预测日" % (
        len(rows), len({r["run_date"] for r in rows})))

    syms = sorted({r["symbol"] for r in rows})
    kc = predictor.load_kcache()
    bars = predictor.fetch_bars(syms, kc, force=False)
    predictor.save_kcache(kc)

    # 每个 pick 重算三种分
    by_date = defaultdict(list)
    for r in rows:
        try:
            hs = json.loads(r["hit_signals"] or "[]")
        except Exception:
            hs = []
        nsig = len(hs)
        conv = 0.10 * (nsig - 1) if nsig >= 2 else 0
        old_s = float(r["score"]) if r["score"] is not None else 0.0
        # NEW_W：用库里存的 OLD 权重结构但换成 NEW 权重重算 base（保留收敛加成口径一致）
        new_base = conv + sum(new_w.get(h.get("key"), 0) * _f(h.get("pct"), 0.0) for h in hs)
        m = mom5_of(bars.get(r["symbol"]) or [], r["run_date"])
        cf = cool_of(m)
        new_wc = new_base * cf
        by_date[r["run_date"]].append({
            "sym": r["symbol"], "old": old_s, "new": new_base, "new_wc": new_wc,
            "hit1": r["hit_t1"], "hit3": r["hit_t3"], "ret1": r["ret_t1"], "ret3": r["ret_t3"],
            "mom5": m, "cool": cf,
        })

    def measure(items):
        n = len(items)
        if not n:
            return (0, 0.0, 0.0, 0.0, 0.0)
        h1 = sum(1 for it in items if it["hit1"])
        h3 = sum(1 for it in items if it["hit3"])
        r1 = [it["ret1"] for it in items if it["ret1"] is not None]
        r3 = [it["ret3"] for it in items if it["ret3"] is not None]
        h3n = len(r3)
        return (n, 100.0 * h1 / n, 100.0 * (h3 / h3n if h3n else 0),
                100.0 * (sum(r1) / len(r1) if r1 else 0.0),
                100.0 * (sum(r3) / len(r3) if r3 else 0.0))

    def topset(day_items, key, K):
        srt = sorted(day_items, key=lambda it: -it[key])
        return srt[:K]  # 只取前 K 名（核心池），才能看出重排是否把赢家顶到前面

    print("\n=== A/B 结果（每个预测日内部重排，取前 K 名汇总；K 越小越能体现重排增益）===")
    for K in (8, 10, 12, 20):
        print("\n--- K = %d（每日前 K 名）---" % K)
        print("%-24s %5s %9s %9s %9s %9s" % ("规则", "样本", "T+1命中", "T+3命中", "T+1收益", "T+3收益"))
        for label, key in (("OLD（实际生成）", "old"),
                           ("NEW_W（仅换权重）", "new"),
                           ("NEW_WC（权重+冷却）", "new_wc")):
            agg = []
            for rd, its in by_date.items():
                agg += topset(its, key, K)
            n, h1, h3, r1, r3 = measure(agg)
            print("%-24s %5d %8.1f%% %8.1f%% %8.2f%% %8.2f%%" % (label, n, h1, h3, r1, r3))

    # 冷却分组贡献
    cool_on = [it for it in base_newc if it["cool"] < 1.0]
    print("\n被动量冷却降权的票: %d/%d（前5日平均涨幅 %.1f%%）" % (
        len(cool_on), len(base_newc),
        100.0 * sum((it["mom5"] or 0) for it in cool_on) / len(cool_on) if cool_on else 0))
    con.close()


if __name__ == "__main__":
    main()
