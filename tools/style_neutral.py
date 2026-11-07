#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风格中性化复核：今晚那些「负超额」的结论，有多少是市值/成交额风格造成的？

要回答的问题
------------
今晚所有超额都长这样：`个股收益 − 同日全市场等权 − 0.25%`。这个基准里**混着风格**。
如果 2023-11~2026-11 小盘整体跑输，那任何偏向小盘的选股方法都会显示「负超额」——
**那不是方法错，是风格不在这边。** 十档法里 236/240 格全负、中位 −0.83pp，
这个整齐程度反而像风格，不像六条独立方法同时失败。

三步（全部用现有 npz，不重建任何东西）
--------------------------------------
  1. `style`  按「近 20 日平均成交额」把全市场分 5 档，算每档 T+5/T+20/T+40 的日等权超额，
              报 Q5−Q1（最大 − 最小）与 6 个半年段。**先量风格有多大。**
  2. `decile` `exc_neu = exc − 当日同档均值`，重跑 6 条因子的十档表：
              `db_wave` 的反向排序消失 → 风格假象；还在 → 真缺陷。
  3. `oss`    用 `exc_neu` 重跑 5 条「稳定反向」规则的 m_S / Δ_rule：
              显著的负超额还在不在。

判定（**跑之前写死，跑完不改**）
--------------------------------
  |Q5−Q1|（T+20）< 0.3pp → 风格不成立，今晚结论全部照旧
  |Q5−Q1|（T+20）≥ 0.3pp → 含「负超额」的判定都要标注「未做风格中性」，报告首页加一句

口径说明
--------
  · 成交额 = v × c（复权价 × 复权量）。若库里的量已按价格的反比调整过，乘积就等于原始成交额；
    用 `000001` 实测：2024-02 起 v×c 在 3.1e8 ~ 6.7e9 元，与日均成交额量级一致。
  · 分档用**秩**（等数量分 5 组），不用金额阈值 —— 成交额是重尾分布，阈值法会让 Q5 塞爆。
  · 「近 20 日」要求至少 15 个有效值，否则该票当日不参与分档。
  · 两个库各自配套用：第 3 步的 exc 来自 kbase 矩阵，成交额也从 kbase 取；
    第 2 步的 exc 来自 `cascade_topn.build_returns`（前复权库），成交额也从 `data/kline_daily.db` 取。
    **不跨库混用**，否则分档和收益不是同一批票。

用法：
    .venv/Scripts/python.exe tools/style_neutral.py --stage style
    .venv/Scripts/python.exe tools/style_neutral.py --stage decile
    .venv/Scripts/python.exe tools/style_neutral.py --stage oss
    .venv/Scripts/python.exe tools/style_neutral.py --stage all

★ 只读 kbase.db / kline_daily.db / viewer/logs/*.npz，只写 viewer/logs/*.json，不碰 viewer/*.py。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                            # noqa: E402

import backtest_oss as B                                      # noqa: E402
import cascade_delta as CD                                    # noqa: E402

STAMP = "20261105"
LOG = ROOT / "viewer" / "logs"
KBASE = ROOT / "viewer" / "kbase.db"
QFQDB = ROOT / "data" / "kline_daily.db"
MATRIX = LOG / ("cascade_matrix_%s.npz" % STAMP)
FSCORE = LOG / ("factor_scores_%s.npz" % STAMP)
OUT = LOG / ("style_neutral_%s.json" % STAMP)

NQ = 5                 # 5 档
WIN = 20               # 近 20 日平均成交额
MIN_OBS = 15           # 20 日里至少 15 个有效值
MIN_COUNT = 40         # 当日可分组票数少于这个数就不分（每档至少 8 只）
GATE = 0.3             # 预注册门槛：|Q5−Q1|(T+20) ≥ 0.3pp → 风格成立

OSS5 = ("oss_burst_vol", "oss_burst", "oss_shrink_vol",
        "oss_rsi_overbought", "oss_pullback_ma20")
OSS_H = (5, 10, 20)

DEC = {
    "tv_shrink_pull": {"cn": "缩量回踩 MA20", "H": (5, 20)},
    "db_chan": {"cn": "缠论中枢下沿", "H": (5, 20)},
    "tv_macd_div": {"cn": "MACD 底背离", "H": (5, 20)},
    "tv_wbottom": {"cn": "双底 W", "H": (5, 20)},
    "db_wave": {"cn": "波浪三浪启动", "H": (5, 20)},
    "tv_platform": {"cn": "平台突破", "H": (5, 20)},
}


# ================================================================
# 成交额矩阵
# ================================================================

def _fmt_date(d, is_int_date: bool) -> str:
    """qfq 库的日期是 YYYYMMDD 整数，kbase 是 'YYYY-MM-DD' 字符串 —— 统一成后者。"""
    if not is_int_date:
        return str(d)
    s = str(int(d))
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 else s


def _amt_matrix(db: Path, date_col: str, is_int_date: bool,
                codes: np.ndarray, dates: np.ndarray, lo: str, hi: str) -> np.ndarray:
    """(code × date) 成交额，单位元。两个库的日期类型不同，用 is_int_date 区分。"""
    t0 = time.time()
    code_pos = {str(c): i for i, c in enumerate(codes)}
    dpos = {str(d): j for j, d in enumerate(dates)}
    out = np.full((codes.size, dates.size), np.nan, dtype=np.float32)
    con = sqlite3.connect(str(db))
    q = ("SELECT code,%s,c,v FROM kline WHERE %s>=? AND %s<=? AND c>0 AND v>0"
         % (date_col, date_col, date_col))
    a = int(lo.replace("-", "")) if is_int_date else lo
    b = int(hi.replace("-", "")) if is_int_date else hi
    cur = con.execute(q, (a, b))
    n = 0
    for code, d, c, v in cur:
        i = code_pos.get(str(code))
        if i is None:
            continue
        j = dpos.get(_fmt_date(d, is_int_date))
        if j is None:
            continue
        out[i, j] = float(c) * float(v)
        n += 1
    con.close()
    cov = float(np.isfinite(out).mean())
    print("[amt] %s：%d 格 / 覆盖 %.1f%%（%.1fs）"
          % (db.name, n, cov * 100, time.time() - t0), flush=True)
    return out


def _roll20(a: np.ndarray) -> np.ndarray:
    """近 20 个交易日的均值（要求 ≥15 个有效值）。纯 numpy，20 次平移。"""
    s = np.zeros_like(a, dtype=np.float64)
    c = np.zeros(a.shape, dtype=np.int16)
    m = np.isfinite(a)
    s += np.where(m, a, 0.0)
    c += m
    for k in range(1, WIN):
        sh = np.full_like(a, np.nan)
        sh[:, k:] = a[:, :-k]
        mk = np.isfinite(sh)
        s += np.where(mk, sh, 0.0)
        c += mk
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c >= MIN_OBS, s / np.maximum(c, 1), np.nan).astype(np.float32)


def _quintiles(amt20: np.ndarray, elig: np.ndarray) -> np.ndarray:
    """逐日按成交额秩等分 5 组（0 = 最小，4 = 最大）。不够票数的日子记 −1。"""
    q = np.full(amt20.shape, -1, dtype=np.int8)
    for j in range(amt20.shape[1]):
        ok = elig[:, j] & np.isfinite(amt20[:, j])
        idx = np.where(ok)[0]
        n = idx.size
        if n < MIN_COUNT:
            continue
        order = idx[np.argsort(amt20[idx, j], kind="stable")]
        b = np.minimum((np.arange(n) * NQ // n).astype(np.int8), NQ - 1)
        q[order, j] = b
    return q


def _bucket_mean(exc: np.ndarray, q: np.ndarray) -> np.ndarray:
    """每只票当日所属档的档内等权均值（档外为 NaN）。"""
    out = np.full(exc.shape, np.nan, dtype=np.float32)
    for k in range(NQ):
        m = (q == k) & np.isfinite(exc)
        n = m.sum(axis=0)
        s = np.where(m, exc, 0.0).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            mk = np.where(n > 0, s / np.maximum(n, 1), np.nan).astype(np.float32)
        out = np.where(m, mk[None, :], out)
    return out


def _daily_mean(exc: np.ndarray, mask: np.ndarray) -> np.ndarray:
    n = mask.sum(axis=0)
    s = np.where(mask, exc, 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, s / np.maximum(n, 1), np.nan)


def _stats(x: np.ndarray, H: int) -> dict:
    x = np.asarray(x, dtype=np.float64)
    ok = np.isfinite(x)
    if ok.sum() < 10:
        return {"n": int(ok.sum()), "mean": None, "t": None}
    return {"n": int(ok.sum()), "mean": round(float(np.mean(x[ok])), 3),
            "t": round(float(B.nw_tstat(x, H)), 2)}


def _segs(dates: np.ndarray, x: np.ndarray) -> list[dict]:
    out = []
    for s0, s1, label in B.SEGMENTS:
        sel = (dates >= s0) & (dates < s1) & np.isfinite(x)
        if sel.sum() == 0:
            out.append({"label": label, "n": 0, "mean": None})
            continue
        out.append({"label": label, "n": int(sel.sum()),
                    "mean": round(float(np.mean(x[sel])), 3)})
    return out


# ================================================================
# ① 风格有多大
# ================================================================

def stage_style() -> dict:
    z = np.load(MATRIX, allow_pickle=False)
    codes, dates = z["codes"], z["dates"]
    uni = z["uni"]
    amt = _amt_matrix(KBASE, "date", False, codes, dates,
                      str(dates[0]), str(dates[-1]))
    amt20 = _roll20(amt)
    q = _quintiles(amt20, uni)
    have_q = float((q >= 0).sum() / max(uni.sum(), 1))
    print("[style] 分档覆盖：%d 个股票日有档位（占 U 的 %.1f%%）"
          % (int((q >= 0).sum()), have_q * 100), flush=True)

    out = {"stage": "style", "stamp": STAMP,
           "window": [str(dates[0]), str(dates[-1])],
           "q_coverage_pct": round(have_q * 100, 2),
           "nq": NQ, "win": WIN, "gate": GATE, "by_H": {}}
    for H in (5, 20, 40):
        exc = z["exc_%d" % H]
        base = z["base_%d" % H]
        qm = []
        for k in range(NQ):
            qm.append(_daily_mean(exc, (q == k) & base))
        d51 = qm[NQ - 1] - qm[0]
        rec = {"quintile_mean": [round(float(np.nanmean(v)), 3) for v in qm],
               "q5_minus_q1": _stats(d51, H),
               "segments": _segs(dates, d51)}
        out["by_H"]["H%d" % H] = rec
        print("[style] T+%-2d Q1 %+.3f Q2 %+.3f Q3 %+.3f Q4 %+.3f Q5 %+.3f | "
              "Q5−Q1 %+.3fpp (t=%.2f, n=%d)  %s"
              % (H, rec["quintile_mean"][0], rec["quintile_mean"][1],
                 rec["quintile_mean"][2], rec["quintile_mean"][3],
                 rec["quintile_mean"][4],
                 rec["q5_minus_q1"]["mean"] or 0.0, rec["q5_minus_q1"]["t"] or 0.0,
                 rec["q5_minus_q1"]["n"],
                 " ".join("%s:%s" % (s["label"][-5:], "—" if s["mean"] is None
                                     else "%+.2f" % s["mean"]) for s in rec["segments"])),
              flush=True)

    # 自检：中性化之后，每个「当日 × 档位」组的均值必须 ≈ 0（否则减错了对象）
    exc20, base20 = z["exc_20"], z["base_20"]
    bm20 = _bucket_mean(exc20, q)
    neu20 = np.where(np.isfinite(bm20), exc20 - bm20, np.nan)
    worst = 0.0
    for k in range(NQ):
        m = (q == k) & np.isfinite(neu20)
        for j in range(neu20.shape[1]):
            v = neu20[m[:, j], j]
            if v.size:
                worst = max(worst, abs(float(v.mean())))
    can = int((base20 & np.isfinite(bm20)).sum())
    out["selfcheck"] = {
        "neutralizable_pct": round(100.0 * can / max(int(base20.sum()), 1), 2),
        "max_abs_bucket_mean_after": round(worst, 8),
        "pass": bool(worst < 1e-3),
    }
    print("[check] 可中性化 %.1f%% 的 base 单元；中性化后各「当日×档位」组均值"
          "绝对值最大 %.2e（应≈0）→ %s"
          % (out["selfcheck"]["neutralizable_pct"], worst,
             "通过" if out["selfcheck"]["pass"] else "**失败**"), flush=True)

    gate_val = out["by_H"]["H20"]["q5_minus_q1"]["mean"]
    out["style_matters"] = bool(gate_val is not None and abs(gate_val) >= GATE)
    print("\n[style] 预注册闸门：|Q5−Q1|(T+20) = %.3fpp vs 门槛 %.1fpp → **风格%s**"
          % (abs(gate_val or 0.0), GATE, "成立" if out["style_matters"] else "不成立"),
          flush=True)
    return out


# ================================================================
# ② 十档表重跑（exc_neu）
# ================================================================

def stage_decile() -> dict:
    from cascade_topn import build_returns
    from factor_score_decile import decile_curve, _verdict, MIN_HITS_DAY

    s = np.load(FSCORE, allow_pickle=False)
    codes, dates = s["codes"], s["dates"]
    R = build_returns(codes, dates, h_list=(5, 20))
    amt = _amt_matrix(QFQDB, "d", True, codes, dates, str(dates[0]), str(dates[-1]))
    amt20 = _roll20(amt)
    print("[decile] 成交额覆盖 %.1f%%" % (100.0 * float(np.isfinite(amt20).mean())), flush=True)

    out = {"stage": "decile", "stamp": STAMP, "factors": {}}
    for f, spec in DEC.items():
        hit, score = s["hit_" + f], s["score_" + f]
        rec = {"cn": spec["cn"], "by_H": {}}
        print("\n[decile] %s %s" % (f, spec["cn"]), flush=True)
        for H in spec["H"]:
            exc = R["exc_%d" % H]
            elig = np.isfinite(exc)
            q = _quintiles(amt20, elig)
            bm = _bucket_mean(exc, q)
            neu = np.where(np.isfinite(bm), exc - bm, np.nan).astype(np.float32)
            raw = decile_curve(hit, score, exc, H)
            cur = decile_curve(hit, score, neu, H)
            rec["by_H"]["H%d" % H] = {
                "raw": {"d10_minus_d1": raw.get("d10_minus_d1"),
                        "t": raw.get("d10_minus_d1_t"),
                        "rho": raw.get("rho_deciles"),
                        "rho_daily_t": raw.get("rho_daily_t"),
                        "n_days": raw.get("n_days"),
                        "deciles": raw.get("deciles"),
                        "verdict": _verdict(raw)},
                "neu": {"d10_minus_d1": cur.get("d10_minus_d1"),
                        "t": cur.get("d10_minus_d1_t"),
                        "rho": cur.get("rho_deciles"),
                        "rho_daily_t": cur.get("rho_daily_t"),
                        "n_days": cur.get("n_days"),
                        "deciles": cur.get("deciles"),
                        "verdict": _verdict(cur)},
            }
            print("  T+%-2d 原 D10−D1 %+.3f (t=%+.2f, ρ=%+.2f, 日内t=%+.2f) → "
                  "中性后 %+.3f (t=%+.2f, ρ=%+.2f, 日内t=%+.2f)  %s → %s"
                  % (H, raw.get("d10_minus_d1") or 0, raw.get("d10_minus_d1_t") or 0,
                     raw.get("rho_deciles") or 0, raw.get("rho_daily_t") or 0,
                     cur.get("d10_minus_d1") or 0, cur.get("d10_minus_d1_t") or 0,
                     cur.get("rho_deciles") or 0, cur.get("rho_daily_t") or 0,
                     _verdict(raw), _verdict(cur)), flush=True)
        out["factors"][f] = rec
    return out


# ================================================================
# ③ 5 条 oss 规则重跑
# ================================================================

def stage_oss() -> dict:
    z = np.load(MATRIX, allow_pickle=False)
    codes, dates = z["codes"], z["dates"]
    amt = _amt_matrix(KBASE, "date", False, codes, dates,
                      str(dates[0]), str(dates[-1]))
    amt20 = _roll20(amt)
    out = {"stage": "oss", "stamp": STAMP, "rules": {}}
    for rule in OSS5:
        hit = z["hit_" + rule]
        rec = {"rows": []}
        print("\n[oss] %s" % rule, flush=True)
        for H in OSS_H:
            exc = z["exc_%d" % H]
            base = z["base_%d" % H]
            q = _quintiles(amt20, base)
            bm = _bucket_mean(exc, q)
            neu = np.where(np.isfinite(bm), exc - bm, np.nan).astype(np.float32)
            a = CD._row(exc, base, hit, H, dates)
            b = CD._row(neu, base, hit, H, dates)
            rec["rows"].append({
                "H": H,
                "raw": {"m_U": a["m_U"], "m_S": a["m_S"], "m_R": a["m_R"],
                        "d_rule": a["d_rule"], "nw": a["nw_rule"],
                        "share_pct": a["share_pct"], "verdict": a["verdict"]},
                "neu": {"m_U": b["m_U"], "m_S": b["m_S"], "m_R": b["m_R"],
                        "d_rule": b["d_rule"], "nw": b["nw_rule"],
                        "share_pct": b["share_pct"], "verdict": b["verdict"]},
            })
            print("  T+%-2d m_S %+.3f → %+.3f | Δ_rule %+.4f → %+.4f | "
                  "剔后池与全市场差 %+.3f → %+.3f"
                  % (H, a["m_S"], b["m_S"], a["d_rule"], b["d_rule"],
                     a["m_R"] - a["m_U"], b["m_R"] - b["m_U"]), flush=True)
        out["rules"][rule] = rec
    return out


# ================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="风格中性化复核")
    ap.add_argument("--stage", default="all",
                    choices=("style", "decile", "oss", "all"))
    a = ap.parse_args(argv)
    res = {}
    if OUT.exists():
        try:
            res = json.loads(OUT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            res = {}
    if a.stage in ("style", "all"):
        res["style"] = stage_style()
    if a.stage in ("decile", "all"):
        res["decile"] = stage_decile()
    if a.stage in ("oss", "all"):
        res["oss"] = stage_oss()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写入 %s" % OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
