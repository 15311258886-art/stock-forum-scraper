"""十档法：这几个形态因子的分数，到底能不能当排序依据（2026-11-05）。

为什么要有这一步
----------------
第 12 节用「重建桶 + 取前 5 只」的方式发现「分数在头部反预测」。团队不认这个结论，
理由站得住：超跌桶只有 3 个可比日、池子本身在亏钱、而且「随机扰动后重取前 5 能白拿
+1.2pp」这个数没法用常理解释（随机扰动应该收敛到 0）。**一个解释不了的数不能当证据。**

更重要的是：第 12 节测的是 `factors.py` 的分数，而**决策台排序用的是 `pillars.py` 的分数**
（`pillars.py` 全是 `sort(key=lambda x: -x["score"])` 降序）。两套分、两套消费者。
所以那个发现**最多只能落在「筛台里这几条形态因子工具的名单排序」上**，不能推及决策台。

十档法为什么更干净
------------------
不取前 N、不求交集、不重建任何桶，只在**每条因子自己当日的全部命中票**里按分数分十档：

  · 没有池子规模问题（每个因子一天几十到几千只，十档每档都有样本）
  · 没有「槽位特殊性质」问题（不做任何截断）
  · 没有重建路径（用的就是生产函数 `factors.py`，只是换个日期重放）
  · 用满 3 年 726 个信号日（不是 261 天）

判读规则（**跑之前写死，不许事后改**）
--------------------------------------
  · 第 10 档 − 第 1 档 > 0 且十档单调（档位 Spearman ≥ 0.5）→ 分数方向对，
    第 12 节那个发现作废（是重建路径的产物）
  · 第 10 档 − 第 1 档 < 0 且单调（Spearman ≤ −0.5）→ 分数方向反了，产品缺陷
  · 其余 → 分数没有排序能力（比「反了」更常见，也更麻烦：它意味着这列分数不该当排序键）

用法
----
    .venv/Scripts/python.exe tools/factor_score_decile.py --stage scores   # 重放分数（约 8min）
    .venv/Scripts/python.exe tools/factor_score_decile.py --stage decile   # 十档表 + Spearman
    .venv/Scripts/python.exe tools/factor_score_decile.py --stage check    # 自检（合成分数）
    .venv/Scripts/python.exe tools/factor_score_decile.py --stage all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import numpy as np                                            # noqa: E402

from cascade_delta import CATEGORY_CN                          # noqa: E402
from cascade_topn import NPZ_QFQ, build_returns, nw_t          # noqa: E402

LOG_DIR = ROOT / "viewer" / "logs"
STAMP = "20261105"
SCORE_NPZ = LOG_DIR / ("factor_scores_%s.npz" % STAMP)
OUT_JSON = LOG_DIR / ("factor_score_decile_%s.json" % STAMP)

NDEC = 10                    # 十档
FACTOR_KEEP = 320            # 与 viewer/factors.LOAD_KEEP 一致（生产就喂 320 根）
MIN_BARS_PROD = 250          # 与 viewer/factors.MIN_BARS 一致：不足 250 根生产直接丢掉
WARMUP_EXTRA = 340           # 726 天 + 320 根预热，多留一点
MIN_HITS_DAY = 10            # 当日命中不足 10 只，十档无意义 → 不计入
# 各分类持有期并集（`cascade_topn.H_LIST` 只到 T+20，中线突破要 T+40/T+60，得显式传）
H_ALL = (5, 10, 20, 30, 40, 60)

# 因子 → (中文名, 分类, 该分类的持有期组 + T+5 对照)
FACTORS: dict[str, dict] = {
    "tv_shrink_pull": {"cn": "缩量回踩 MA20", "cat": "short_dip", "H": (5, 10, 20, 30)},
    "db_chan": {"cn": "缠论中枢下沿", "cat": "short_dip", "H": (5, 10, 20, 30)},
    "tv_macd_div": {"cn": "MACD 底背离", "cat": "short_dip", "H": (5, 10, 20, 30)},
    "tv_wbottom": {"cn": "双底 W", "cat": "mid_break", "H": (5, 20, 40, 60)},
    "db_wave": {"cn": "波浪三浪启动", "cat": "mid_break", "H": (5, 20, 40, 60)},
    "tv_platform": {"cn": "平台突破", "cat": "mid_break", "H": (5, 20, 40, 60)},
}


# ================================================================
# ① 把分数在 726 个信号日上重放一遍
# ================================================================

def load_factor_bars(keep: int) -> dict:
    """取日线 → {code: {arr..., d=[日期], pos={日期:下标}}}。

    列序转换仍走 `universe_box.load_bars`（已把 (d,o,h,l,c,v) 转成 (d,o,c,h,l,v)），
    不自己转 —— 2026-11-02 曾因下标错位把日期当收盘价，排查很久。
    """
    from viewer import universe_box as ub

    t0 = time.time()
    raw = ub.load_bars(None, keep=keep)
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        a = np.asarray([r[1:6] for r in rows], dtype=np.float64)
        out[str(code)] = {
            "n": len(rows), "o": a[:, 0], "c": a[:, 1],
            "h": a[:, 2], "l": a[:, 3], "v": a[:, 4],
            "d": [r[0] for r in rows],
            "pos": {d: i for i, d in enumerate(r[0] for r in rows)},
        }
    print("[scores] 取数 %d 只 / 每只约 %d 根（%.1fs）"
          % (len(out), len(next(iter(out.values()))["d"]) if out else 0, time.time() - t0),
          flush=True)
    return out


def stage_scores(limit_days: int = 0, max_codes: int = 0) -> dict:
    from viewer import factors as fa

    z = np.load(NPZ_QFQ, allow_pickle=False)
    codes, dates = z["codes"], z["dates"]
    if max_codes:
        codes = codes[:max_codes]
    if limit_days:
        dates = dates[:limit_days]
    n_stock, n_day = codes.size, dates.size
    keep = n_day + WARMUP_EXTRA
    bars = load_factor_bars(keep)

    hit = {f: np.zeros((n_stock, n_day), dtype=bool) for f in FACTORS}
    score = {f: np.zeros((n_stock, n_day), dtype=np.float32) for f in FACTORS}
    # 当日"生产能算"的票数（有几根 >=250），用来交代窗口为什么比 726 天短
    coverage = np.zeros(n_day, dtype=np.int32)

    t0 = time.time()
    for i, code in enumerate(codes):
        b = bars.get(str(code))
        if b is None:
            continue
        if i % 500 == 0:
            print("      票 %d/%d（%.0fs）" % (i, n_stock, time.time() - t0), flush=True)
        o, c, h, l, v, pos = b["o"], b["c"], b["h"], b["l"], b["v"], b["pos"]
        for j, d in enumerate(dates):
            idx = pos.get(str(d))
            if idx is None:
                continue
            # 生产口径：喂最近 320 根；不足 250 根的直接判"画不出形态"丢掉
            nb = min(idx + 1, FACTOR_KEEP)
            if nb < MIN_BARS_PROD:
                continue
            coverage[j] += 1
            sl = slice(idx + 1 - nb, idx + 1)
            sub = {"n": nb, "o": o[sl], "c": c[sl],
                   "h": h[sl], "l": l[sl], "v": v[sl]}
            for f in FACTORS:
                ok, sc, _ = fa.FACTORS[f]["fn"](sub, {})
                if ok:
                    hit[f][i, j] = True
                    score[f][i, j] = float(sc or 0.0)

    store = {"codes": codes, "dates": dates, "coverage": coverage}
    for f in FACTORS:
        store["hit_" + f] = hit[f]
        store["score_" + f] = score[f]
    np.savez(SCORE_NPZ, **store)
    meta = {
        "stage": "scores", "stamp": STAMP,
        "n_stock": int(n_stock), "n_day": int(n_day),
        "window": [str(dates[0]), str(dates[-1])],
        "elapsed_s": round(time.time() - t0, 1),
        "npz": str(SCORE_NPZ), "npz_mb": round(SCORE_NPZ.stat().st_size / 1048576.0, 1),
        "hits": {f: int(hit[f].sum()) for f in FACTORS},
        "hit_per_day_p50": {f: float(np.median(hit[f].sum(axis=0))) for f in FACTORS},
        "coverage": {"min": int(coverage.min()), "p10": int(np.percentile(coverage, 10)),
                     "p50": int(np.median(coverage)), "max": int(coverage.max()),
                     "days_ge_3000": int((coverage >= 3000).sum())},
        "usable_window": [str(dates[int(np.argmax(coverage >= 3000))]) if (coverage >= 3000).any() else "",
                          str(dates[-1])],
    }
    print("[scores] 落盘 %s（%.1f MB，%.1fs）"
          % (SCORE_NPZ.name, meta["npz_mb"], meta["elapsed_s"]), flush=True)
    return meta


# ================================================================
# ② 十档 + Spearman
# ================================================================

def _avg_rank(x: np.ndarray) -> np.ndarray:
    """平均秩（并列取平均），numpy 自带没有，自己写 —— 分数是 1 位小数，并列很多。"""
    n = x.size
    order = np.argsort(x, kind="stable")
    r = np.empty(n, dtype=np.float64)
    r[order] = np.arange(n, dtype=np.float64)
    xs = x[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[j + 1] == xs[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman = 秩的 Pearson。并列用平均秩，不丢样本。"""
    if x.size < 3:
        return float("nan")
    rx, ry = _avg_rank(x), _avg_rank(y)
    rx -= rx.mean()
    ry -= ry.mean()
    d = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / d) if d > 0 else float("nan")


def decile_curve(hit: np.ndarray, score: np.ndarray, exc: np.ndarray,
                 H: int, rng: np.random.Generator | None = None) -> dict:
    """逐日：命中集内按分数分 10 档 → 每档当日等权超额 → 再对日期取均值。

    为什么要「逐日再平均」而不是把所有股票日堆在一起：堆一起会被命中数多的日子主导，
    而那几天恰好是市场状态最极端的几天（命中多通常意味着单边行情），等于让少数几天决定结论。
    """
    n_day = hit.shape[1]
    rows = [[] for _ in range(NDEC)]
    rhos, spreads = [], []
    n_used = 0
    for j in range(n_day):
        m = hit[:, j] & np.isfinite(exc[:, j])
        n = int(m.sum())
        if n < MIN_HITS_DAY:
            continue
        idx = np.where(m)[0]
        s = score[idx, j]
        e = exc[idx, j]
        rk = _avg_rank(s)
        # 秩 → 档位（0..9）。用秩而不是分位数阈值：并列多的时候阈值法会把某一档塞爆
        dec = np.minimum((rk / n * NDEC).astype(np.int64), NDEC - 1)
        for k in range(NDEC):
            sel = dec == k
            if sel.any():
                rows[k].append(float(e[sel].mean()))
        n_used += 1
        rhos.append(spearman(s, e))
        d = [e[dec == k].mean() for k in range(NDEC) if (dec == k).any()]
        if len(d) == NDEC:
            spreads.append(d[-1] - d[0])
    if not n_used:
        return {"n_days": 0}
    means = [float(np.mean(r)) if r else float("nan") for r in rows]
    d10_minus_d1 = means[NDEC - 1] - means[0]
    rho_dec = spearman(np.arange(NDEC, dtype=np.float64), np.asarray(means))
    rho_day = np.asarray(rhos, dtype=np.float64)
    return {
        "n_days": n_used,
        "deciles": [round(x, 3) for x in means],
        "d10_minus_d1": round(d10_minus_d1, 3),
        "d10_minus_d1_t": round(nw_t(np.asarray(spreads, dtype=np.float64), H), 2),
        "rho_deciles": round(rho_dec, 3),
        "rho_daily_mean": round(float(np.nanmean(rho_day)), 3),
        "rho_daily_t": round(nw_t(rho_day, H), 2),
        "H": H,
    }


def _verdict(r: dict) -> str:
    """预注册判读：先看方向 + 单调，再看是否杂乱。"""
    if not r or not r.get("n_days"):
        return "无样本"
    d, rho = r["d10_minus_d1"], r["rho_deciles"]
    if d > 0 and rho >= 0.5:
        return "方向对"
    if d < 0 and rho <= -0.5:
        return "方向反了（产品缺陷）"
    return "无排序能力"


def stage_decile() -> dict:
    s = np.load(SCORE_NPZ, allow_pickle=False)
    dates = s["dates"]
    codes = s["codes"]
    R = build_returns(codes, dates, h_list=H_ALL)
    cov = s["coverage"] if "coverage" in s.files else np.zeros(dates.size, dtype=np.int32)
    ge = cov >= 3000
    out = {"stage": "decile", "stamp": STAMP,
           "window": [str(dates[0]), str(dates[-1])], "n_day": int(dates.size),
           "n_stock": int(codes.size), "ndec": NDEC, "min_hits_day": MIN_HITS_DAY,
           # 历史深度交代：库里每只票最多约 660 根，可算票数不是 726 天满覆盖
           "coverage": {"min": int(cov.min()),
                        "p10": int(np.percentile(cov, 10)),
                        "p50": int(np.median(cov)),
                        "max": int(cov.max()),
                        "days_ge_3000": int(ge.sum())},
           "usable_window": [str(dates[int(np.argmax(ge))]) if ge.any() else "",
                             str(dates[-1])],
           "factors": {}}
    for f, spec in FACTORS.items():
        hit = s["hit_" + f]
        score = s["score_" + f]
        rec = {"cn": spec["cn"], "cat": spec["cat"], "cat_cn": CATEGORY_CN.get(spec["cat"], ""),
               "hit_total": int(hit.sum()),
               "hit_p50": float(np.median(hit.sum(axis=0))),
               "hit_max": int(hit.sum(axis=0).max()),
               "by_H": {}}
        for H in spec["H"]:
            r = decile_curve(hit, score, R["exc_%d" % H], H)
            r["verdict"] = _verdict(r)
            rec["by_H"]["H%d" % H] = r
        out["factors"][f] = rec
        print("[decile] %-16s %s 命中/日 p50=%.0f  %s"
              % (f, spec["cn"], rec["hit_p50"],
                 " | ".join("T+%d %s" % (H, rec["by_H"]["H%d" % H].get("verdict", "—"))
                            for H in spec["H"])), flush=True)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[decile] 写入 %s" % OUT_JSON, flush=True)
    return out


# ================================================================
# ③ 自检：用「合成分数」验证整条链路的方向
# ================================================================

def stage_check() -> int:
    """三件事，任何一条不过，十档表就不能读：

      [1] Spearman 实现在单调递增/递减上必须给出 +1 / −1（含大量并列的场合）
      [2] 合成分数 = 未来超额本身（上帝分数）→ 十档必须单调递增、ρ=+1
      [3] 合成分数 = 未来超额的相反数（反向上帝分数）→ 十档必须单调递减、ρ=−1
      [4] 档位必须完整分割当日命中集（不重不漏）
    """
    ok = True
    rng = np.random.default_rng(20261105)

    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    r1 = spearman(x, x)
    r2 = spearman(x, -x)
    ok &= abs(r1 - 1) < 1e-9 and abs(r2 + 1) < 1e-9
    print("[1] Spearman 单调性：+%.3f / %.3f  %s"
          % (r1, r2, "通过" if abs(r1 - 1) < 1e-9 and abs(r2 + 1) < 1e-9 else "**失败**"))

    t = np.array([1.0, 1.0, 1.0, 2.0, 3.0, 3.0])
    rr = _avg_rank(t)
    ok_rank = np.allclose(rr, [1.0, 1.0, 1.0, 3.0, 4.5, 4.5])
    ok &= ok_rank
    print("[1b] 并列平均秩 [1,1,1,2,3,3] → %s  %s"
          % (rr.tolist(), "通过" if ok_rank else "**失败**"))

    s = np.load(SCORE_NPZ, allow_pickle=False)
    f = "tv_wbottom"          # 命中量最大的因子，十档最稳
    hit, score = s["hit_" + f], s["score_" + f]
    dates = s["dates"]
    R = build_returns(s["codes"], dates)
    H = 20
    exc = R["exc_%d" % H]

    for name, synthetic, want in (
            ("上帝分数（=未来超额）", None, +1),
            ("反向上帝分数（=−未来超额）", None, -1)):
        syn = np.zeros_like(score, dtype=np.float32)
        for j in range(hit.shape[1]):
            m = hit[:, j] & np.isfinite(exc[:, j])
            if m.sum() < MIN_HITS_DAY:
                continue
            syn[m, j] = exc[m, j] if want > 0 else -exc[m, j]
        r = decile_curve(hit, syn, exc, H)
        good = (r["rho_deciles"] * want) >= 0.9 and (r["d10_minus_d1"] * want) > 1.0
        ok &= good
        print("[%s] %s → 十档 %s | ρ=%.2f, D10−D1=%+.2fpp  %s"
              % ("2" if want > 0 else "3", name, r["deciles"], r["rho_deciles"],
                 r["d10_minus_d1"], "通过" if good else "**失败**"))

    # [4] 档位完整分割
    part_ok = True
    for j in range(min(50, hit.shape[1])):
        m = hit[:, j] & np.isfinite(exc[:, j])
        n = int(m.sum())
        if n < MIN_HITS_DAY:
            continue
        idx = np.where(m)[0]
        rk = _avg_rank(score[idx, j])
        dec = np.minimum((rk / n * NDEC).astype(np.int64), NDEC - 1)
        cnt = np.bincount(dec, minlength=NDEC)
        if cnt.sum() != n or (cnt == 0).any():
            # 并列会让某一档为空，但总数必须守恒
            if cnt.sum() != n:
                part_ok = False
    ok &= part_ok
    print("[4] 档位分割：各档计数之和 == 当日命中数  %s"
          % ("通过" if part_ok else "**失败**"))

    print("\n结论：%s" % ("全部通过" if ok else "**有检查未通过，十档表不要读**"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="十档法：形态因子分数的排序能力")
    ap.add_argument("--stage", default="all", choices=("scores", "decile", "check", "all"))
    ap.add_argument("--limit-days", type=int, default=0, help="冒烟测试：只跑前 N 个信号日")
    ap.add_argument("--max-codes", type=int, default=0, help="冒烟测试：只跑前 N 只票")
    a = ap.parse_args(argv)
    if a.stage in ("scores", "all"):
        stage_scores(a.limit_days, a.max_codes)
    if a.stage in ("decile", "all"):
        stage_decile()
    if a.stage in ("check", "all"):
        return stage_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
