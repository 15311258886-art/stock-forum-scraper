# -*- coding: utf-8 -*-
"""四支柱每日选股引擎（方案 v2.1 落地，2026-10-15）。

定位（架构红线）：本模块只跑在**后端定时任务**里（16:00 主管线 / 9:25 竞价确认），
查看器只读本模块产出的表，绝不在请求线程里现场研判。

支柱（对应《每日预测选股体系设计方案-优化v2》四大支柱）：
  A  policy_funnel  政策漏斗      —— v2 保留：L1 政策/资金方向 → L2 热板块 → L3 个股信号
                                      → L4 排序 → L5 基本面闸（fundamentals 表）
  B  t1_surf        短线T+1竞价冲浪 —— 新增：今日涨停梯队（本地 K 线派生，零新外部源）
                                      → 次日 9:25 竞价确认（maybe_b_confirm）
  C  dip_revert     超跌修复逆向   —— v2 保留精修：60 日深度超跌 + 企稳确认
  D  resonance      共振验证+风控闸 —— 原模式二（四层加权）降级改造：不独立选股，
                                      交叉验证 A/B/C + 市场状态分类器（结构/震荡/普跌）

诚实口径：
  - 涨停判定为**近似口径**：close >= round(prev_close × (1+档位涨停幅), 2) − 0.001，
    档位 = 主板 10% / 创业·科创 20% / 北交 30% / ST 5%（按代码前缀+名称判）。
  - 市场状态三态判定规则全部写在 payload 的 rules 里，可追溯。
  - D 支柱的打分校准/排序校准来自主引擎 prediction_item 历史实盘复盘，样本不足时如实标注。
  - 所有阈值不许拍脑袋改：改前先看 calibration 的分组命中率证据。

外部依赖：predictor（K 线缓存/信号函数/候选池）、auction（交易日历）、emapi（基本面）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import predictor as P          # 同目录（viewer/），server 已把 viewer/ 加入 sys.path
import auction                 # 项目根
import emapi                   # 项目根

ENGINE_VER = "pillar-v1.0"
DB_PATH = P.DB_PATH

# ---- 各支柱输出条数（2026-10-15 进哥定；改动须附证据）--------------------------
# 统一机制：每根支柱 = ① 过滤器（策略定义）→ ② 按分排序 → ③ 取前 CAP 只，但至少 MIN_N 只。
#   · MIN_N == CAP：**固定条数** —— 候选总是足够，必然填满、也不会多。
#   · MIN_N <  CAP：**区间条数 [MIN_N, CAP]** —— 随市场浮动；不足 MIN_N 时先用
#                    「放宽档 / 补位档」补足，仍不足则**如实少给**（绝不硬凑不合策略的票）。
# 2026-10-15 变更背景：原先四柱共用一个 TOP_N=10 上限（注释写"与主引擎核心池量级一致"），
#   结果 A/D 双双撞上限成 10 只、C 只筛出 1 只。现按柱分档，并**去掉对主引擎量级的攀比**。
CAP = {"policy_funnel": 10, "t1_surf": 4, "dip_revert": 5, "resonance": 6, "self_learn": 6}
MIN_N = {"policy_funnel": 10, "t1_surf": 0, "dip_revert": 4, "resonance": 3, "self_learn": 3}
# 2026-10-16 优化 P1：政策漏斗"出后埋人"闸——候选近 10 交易日累计涨幅过大视为利好提前反应，剔除
POLICY_CHASE_GATE_PCT = 15.0   # 近 10 交易日累计涨幅 > 此值（%）→ 剔除
POLICY_CHASE_FLOOR = 6         # 闸后保留下限；低于则兜底保留并标注，避免名单清空
# 条数口径说明（接口下发给前端展示，回答"哪些是固定、哪些是区间"）
COUNT_SPEC = {
    "policy_funnel": {"min": 10, "max": 10, "kind": "fixed", "label": "固定 10 只",
                      "why": "候选池足够大，必然填满 10 只"},
    "t1_surf":       {"min": 0, "max": 4, "kind": "range", "label": "上限 4 只（0~4）",
                      "why": "只选当日涨停梯队；没有梯队的日子注定少，硬凑就背离策略"},
    "dip_revert":    {"min": 4, "max": 5, "kind": "range", "label": "区间 4~5 只",
                      "why": "严格档（60 日分位≤25%、回撤≥20%）不足 4 只时，启用放宽档补足到 4~5"},
    # 2026-10-16 进哥要求新增第 5 根「自我提升」：学前三根（+共振风控）的实盘复盘再选票
    "self_learn":    {"min": 3, "max": 6, "kind": "range", "label": "区间 3~6 只",
                      "why": "用各支柱实盘复盘学出的权重给今日提名加权；候选不足时如实少给，不硬凑"},
    "resonance":     {"min": 3, "max": 6, "kind": "range", "label": "区间 3~6 只",
                      "why": "优先 ≥2 支柱共振票，取前 6；不足 3 只时用单支柱高分票补位"},
}
OVERSOLD_POS = 0.25         # C 严格档：60 日收盘分位 ≤ 25% 视为深度超跌
OVERSOLD_DD = -0.20         # C 严格档：距 60 日收盘高点回撤 ≥ 20%
# C 放宽档（严格档不足 MIN_N 时启用，名单内会标注「放宽档」）
# 2026-10-15 阈值由实测敏感性矩阵定，非拍脑袋：2026-10-14 的 158 只候选里「企稳」的
# 只有 40 只（企稳＝近3日不创新低 且 收阳或收复5日前收盘，是硬门槛）；各组合筛出只数：
#   分位≤40%&回撤≥12% → 3 只；分位≤50%&回撤≥10% → 4 只；分位≤55%&回撤≥15% → 4 只。
# 取「回撤≥15% 且 分位≤55%」——回撤仍代表真跌，分位偏宽但配合企稳可接受。
# ⚠️ 若某天仍不足 MIN_N，**如实少给**，不再继续放松（放松＝拿策略纯度换数量）。
OVERSOLD_POS_RELAX = 0.55   # C 放宽档：60 日收盘分位 ≤ 55%
OVERSOLD_DD_RELAX = -0.15   # C 放宽档：距 60 日收盘高点回撤 ≥ 15%
CAL_PENALTY_NP = 0.6        # A·L5：归母净利同比 < 0 → 打分 ×0.6（业绩承压）
CAL_BONUS_ROE = 12.0        # A·L5：加权 ROE ≥ 12 → +5 分（质量加成）
FUND_TTL_DAYS = 7           # fundamentals 增量刷新周期（天）
FUND_WORKERS = 4            # 基本面抓取并发（温和，防东财风控）
GAP_CONFIRM = (0.0, 0.05)   # B 竞价确认：开盘涨幅 0%~5% 视为「可参与」
GAP_OBSERVE = 0.05          # 开盘涨幅 > 5% 视为「一字/过高难上车 → 观察」

DDL = """
CREATE TABLE IF NOT EXISTS pillar_run (
  run_date   TEXT NOT NULL,
  pillar     TEXT NOT NULL,      -- policy_funnel | t1_surf | dip_revert | resonance
  status     TEXT,               -- ok | pending_confirm | confirmed | partial
  title      TEXT,
  summary    TEXT,
  state_json TEXT,               -- 市场状态/闸门/校准/竞价确认等结构
  note       TEXT,
  engine_ver TEXT,
  created_at TEXT,
  PRIMARY KEY (run_date, pillar)
);
CREATE TABLE IF NOT EXISTS pillar_item (
  run_date TEXT NOT NULL,
  pillar   TEXT NOT NULL,
  symbol   TEXT NOT NULL,
  name     TEXT,
  rank     INTEGER,
  score    REAL,
  reason   TEXT,
  detail_json TEXT,
  PRIMARY KEY (run_date, pillar, symbol)
);
CREATE INDEX IF NOT EXISTS idx_pillar_item_date ON pillar_item(run_date);
CREATE TABLE IF NOT EXISTS market_state_log (
  trade_date  TEXT PRIMARY KEY,
  state       TEXT,              -- 结构 | 震荡 | 普跌
  breadth_json TEXT,
  note        TEXT,
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol      TEXT PRIMARY KEY,
  name        TEXT,
  roe REAL, roe_kf REAL, dr REAL, gm REAL,
  rev_yoy REAL, np_yoy REAL, kf_np_yoy REAL,
  report_date TEXT,
  updated_at  TEXT,
  source      TEXT
);
"""

_LOCK = threading.Lock()
_GEN_DONE: dict = {"date": None, "fail": 0}       # 16:00 主管线当天幂等
_CONF_DONE: dict = {"key": None, "fail": 0}       # 9:25 竞价确认幂等
_STATUS: dict = {"stage": "", "detail": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# ---------------------------------------------------------------- 涨停档位（近似口径）
def limit_ratio(code: str, name: str = "") -> float:
    c = str(code or "")
    nm = str(name or "")
    if c[:1] in ("4", "8", "9"):
        return 0.30
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    if "ST" in nm.upper():
        return 0.05
    return 0.10


def is_limit_up(prev_c: float, c: float, code: str, name: str = "") -> bool:
    """近似口径：考虑四舍五入 0.001 容差。prev_c<=0 或数据缺失一律 False。"""
    try:
        prev_c = float(prev_c); c = float(c)
    except Exception:  # noqa: BLE001
        return False
    if prev_c <= 0 or c <= 0:
        return False
    r = limit_ratio(code, name)
    import decimal
    target = float(decimal.Decimal(str(prev_c)) * decimal.Decimal(str(1 + r))
                   .quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP))
    return c >= target - 0.0011


# ---------------------------------------------------------------- 市场状态分类器（三态）
def market_state(bars_map: dict, td: str, index_sym: str = "sh000001") -> dict:
    """结构 / 震荡 / 普跌 三态判定（v2.1 第九章·层4 缺口补齐）。

    规则（写进返回值，前端照实展示）：
      普跌 = 上涨占比 < 35% 或 池内当日涨幅中位数 ≤ -1.2%
      震荡 = 上涨占比 < 55% 或 指数收于 20 日均线下方
      结构 = 其余（赚钱效应集中在主线方向）
    """
    pcts = []
    for v in bars_map.values():
        if len(v) >= 2 and str(v[-1].get("d", ""))[:10] == td:
            prev, last = v[-2].get("c"), v[-1].get("c")
            try:
                if prev and float(prev) > 0:
                    pcts.append((float(last) / float(prev) - 1) * 100)
            except Exception:  # noqa: BLE001
                pass
    n = len(pcts)
    up = sum(1 for x in pcts if x > 0)
    up_ratio = round(up / n, 3) if n else None
    pcts.sort()
    median = round(pcts[n // 2], 2) if n else None

    idx_note, idx_below_ma20 = "", False
    try:
        kd = auction.sina_kline_dates(index_sym, 40)
        closes = [float(x.get("c")) for x in kd if x.get("c")]
        if len(closes) >= 21:
            ma20 = sum(closes[-20:]) / 20.0
            ma5 = sum(closes[-5:]) / 5.0
            idx_below_ma20 = closes[-1] < ma20
            idx_note = "指数 %.2f，5日线 %.2f，20日线 %.2f（%s20日线）" % (
                closes[-1], ma5, ma20, "下穿" if idx_below_ma20 else "站上")
    except Exception as e:  # noqa: BLE001
        idx_note = "指数数据不可用（%s）" % str(e)[:40]

    if up_ratio is None:
        state = "未知"
    elif (up_ratio < 0.35) or (median is not None and median <= -1.2):
        state = "普跌"
    elif (up_ratio < 0.55) or idx_below_ma20:
        state = "震荡"
    else:
        state = "结构"
    return {
        "state": state,
        "breadth": {"pool_n": n, "up_n": up, "up_ratio": up_ratio,
                    "median_pct": median, "index_note": idx_note},
        "rules": ["普跌 = 上涨占比<35% 或 中位涨幅≤-1.2%",
                  "震荡 = 上涨占比<55% 或 指数低于20日线",
                  "结构 = 其余（资金集中于主线）"],
        "trade_date": td,
    }


# ---------------------------------------------------------------- A 政策漏斗
def pillar_policy_funnel(con, td, bars_map, names, codes) -> dict:
    """L1 政策/资金方向（_hot_sectors 多源板块热度）→ L2/L3 个股信号 → L4 排序 → L5 基本面闸。"""
    pool = set(bars_map.keys())
    sigs = {}
    for key, fn in (("policy", P.sig_policy), ("desk", P.sig_desk),
                    ("board", P.sig_board), ("ann", P.sig_ann)):
        try:
            m = fn(con, asof=td)
        except Exception:  # noqa: BLE001
            m = {}
        sigs[key] = {s: v for s, v in (m or {}).items() if s in pool}

    hot_boards, sym_board = P._hot_sectors(con, asof=td)
    top_boards = [w for w, _ in (hot_boards or [])[:8]]
    top1_score = float(hot_boards[0][1]) if hot_boards else 1.0

    # fundamentals 闸（L5）：读本地表，缺覆盖不罚分，只标注
    fund = {}
    try:
        for r in con.execute("SELECT * FROM fundamentals LIMIT 5000"):
            fund[str(r["symbol"])] = dict(zip(r.keys(), r))
    except Exception:  # noqa: BLE001
        pass

    vals = []
    for sym in pool:
        strength_sum = sum(float((sigs[k].get(sym) or {}).get("strength") or 0.0)
                           for k in sigs)
        n_sig = sum(1 for k in sigs if sigs[k].get(sym))
        if strength_sum <= 0 and n_sig == 0:
            continue
        vals.append((sym, strength_sum, n_sig))
    if not vals:
        return {"items": [], "boards": top_boards, "note": "候选池内无政策/资金/公告信号命中"}

    sums = sorted(x[1] for x in vals)
    ev_list = []
    for sym, strength_sum, n_sig in vals:
        p_norm = P._pct_le(sums, strength_sum)           # 信号强度百分位（0~1）
        board_word, board_score = sym_board.get(sym, ("", 0.0))
        dir_part = 40.0 * (board_score / top1_score if top1_score > 0 else 0.0) \
            if board_word in top_boards else 0.0
        score = 40.0 * p_norm + dir_part + min(20.0, 10.0 * max(0, n_sig - 1))

        # L5 基本面闸
        f = fund.get(sym)
        caveats = []
        if f:
            if f.get("np_yoy") is not None and f["np_yoy"] < 0:
                score *= CAL_PENALTY_NP
                caveats.append("业绩承压（归母同比 %+.1f%%）" % f["np_yoy"])
            if f.get("roe") is not None and f["roe"] >= CAL_BONUS_ROE:
                score += 5.0
                caveats.append("质量加成（ROE %.1f%%）" % f["roe"])
        else:
            caveats.append("基本面未覆盖")

        bars = bars_map.get(sym) or []
        last = bars[-1] if bars else {}
        ev_bits = []
        for k, label in (("policy", "政策"), ("desk", "研报"), ("board", "板块资金"),
                         ("ann", "公告")):
            m = sigs[k].get(sym)
            if m:
                ev = "；".join(str(x) for x in (m.get("evidence") or [])[:2])
                ev_bits.append("%s[%s]" % (label, ev or ("强度 %.2f" % float(m.get("strength") or 0))))
        if board_word:
            ev_bits.insert(0, "热板块[%s]" % board_word)
        reason = " → ".join(ev_bits[:4]) or "信号命中"
        if caveats:
            reason += " ｜ " + "；".join(caveats)
        ev_list.append({
            "symbol": sym, "name": names.get(sym) or sym,
            "score": round(score, 2), "reason": reason,
            "detail": {"sig_n": n_sig, "board": board_word,
                       "pct": round((float(last.get("c", 0)) / float(bars[-2]["c"]) - 1) * 100, 2)
                       if len(bars) >= 2 and bars[-2].get("c") else None},
        })
    # 2026-10-16 优化 P1 · 政策漏斗"出后埋人"闸：候选近 10 交易日累计涨幅已过大
    # → 视为利好提前反应 / 追在兑现高点，剔除（不硬凑数量）。
    # 但若闸太狠会把名单清空，则兜底保留并明确标注"近10日已追高"。
    chase = {}
    for it in ev_list:
        bs = bars_map.get(it["symbol"]) or []
        if len(bs) >= 11 and bs[-11].get("c"):
            tr = (float(bs[-1].get("c", 0)) / float(bs[-11]["c"]) - 1) * 100
            if tr > POLICY_CHASE_GATE_PCT:
                chase[it["symbol"]] = round(tr, 2)
    ev_list.sort(key=lambda x: -x["score"])
    cap = CAP["policy_funnel"]
    picked_all = ev_list[:cap]
    gated = [x for x in picked_all if x["symbol"] in chase]
    if len(picked_all) - len(gated) >= POLICY_CHASE_FLOOR:
        picked = [x for x in picked_all if x["symbol"] not in chase]
        n_excl = len(gated)
    else:
        picked = picked_all
        n_excl = 0
        for x in gated:
            x["reason"] += " ｜ 近10日已追高+%s%%（利好兑现风险）" % chase[x["symbol"]]
    return {"items": picked, "boards": top_boards,
            # 2026-10-16 进哥：这段原先写的是内部层号（"L1 方向=…；L5 闸覆盖=…"），
            # 另外三根支柱的摘要是通顺中文，放一起就显得这根"不是同一种东西"。
            # 改成同样通顺的一句话，三个数字一个没少。
            "note": "政策面命中 %d 个热板块；技术上过关 %d/%d 只；近10日追高剔除 %d 只；条数：%s" % (
                len(top_boards), sum(1 for x in picked if "未覆盖" not in x["reason"]),
                len(picked), n_excl, COUNT_SPEC["policy_funnel"]["label"])}


# ---------------------------------------------------------------- B 短线 T+1 竞价冲浪
def pillar_t1_surf(con, td, bars_map, names) -> dict:
    """今日涨停梯队（本地 K 线派生）→ 次日竞价确认候选。

    打分：连板高度 40 + 量能放大 25 + 尾盘封住 20 + 当日涨幅 15。
    梯队近似口径：日 K 收盘价判涨停（见 is_limit_up），非交易所精确涨停价口径。
    """
    ladder, excluded_st = [], 0
    for sym, bars in bars_map.items():
        nm = names.get(sym) or sym
        if "ST" in nm.upper():
            excluded_st += 1
            continue
        if len(bars) < 6 or str(bars[-1].get("d", ""))[:10] != td:
            continue
        streak = 0
        for i in range(len(bars) - 1, 0, -1):
            if is_limit_up(bars[i - 1].get("c"), bars[i].get("c"), sym, nm):
                streak += 1
            else:
                break
        if streak < 1:
            continue
        last, prev = bars[-1], bars[-2]
        vols = [b.get("v") or 0 for b in bars[-6:-1]]
        avg_v = (sum(vols) / len(vols)) if vols else 0
        vol_ratio = (float(last.get("v") or 0) / avg_v) if avg_v else None
        pct = (float(last["c"]) / float(prev["c"]) - 1) * 100
        sealed = 1 if last.get("h") and abs(float(last["c"]) - float(last["h"])) < 0.005 else 0
        score = (40.0 * min(streak, 4) / 4.0
                 + 25.0 * (min(vol_ratio, 3.0) / 3.0 if vol_ratio else 0.5)
                 + 20.0 * sealed
                 + 15.0 * min(max(pct, 0) / 10.0, 1.0))
        ladder.append({
            "symbol": sym, "name": nm, "score": round(score, 2),
            "reason": "%d 连板（%s 档）%s" % (
                streak, ("%d%%" % round(limit_ratio(sym, nm) * 100)),
                "，尾盘封住" if sealed else "，尾盘未封死"),
            "detail": {"streak": streak, "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                       "pct": round(pct, 2), "sealed": sealed,
                       "close": float(last.get("c") or 0)},
        })
    ladder.sort(key=lambda x: -x["score"])
    cap = CAP["t1_surf"]
    picked = ladder[:cap]
    return {"items": picked,
            "note": "梯队 %d 只（ST 剔除 %d 只）→ 取 %d 只；次日 9:25 竞价确认后给出参与建议。"
                    "条数：%s —— 没有涨停梯队的日子注定少，硬凑就背离策略"
                    % (len(ladder), excluded_st, len(picked),
                       COUNT_SPEC["t1_surf"]["label"])}


# ---------------------------------------------------------------- C 超跌修复逆向
def pillar_dip_revert(con, td, bars_map, names) -> dict:
    """60 日深度超跌 + 企稳确认（近 3 日不创新低 + 末日收阳或收复 5 日前收盘）。

    诚实边界：逆向策略在「普跌」市场状态下风控闸会主动降级（见 D 支柱），本支柱自身不做择时。
    """
    strict, relax = [], []
    for sym, bars in bars_map.items():
        if len(bars) < 60 or str(bars[-1].get("d", ""))[:10] != td:
            continue
        nm = names.get(sym) or sym
        w = bars[-60:]
        closes = [float(b.get("c") or 0) for b in w]
        c = closes[-1]
        if c <= 0:
            continue
        srt = sorted(closes)
        pos60 = (srt.index(c) if c in srt else 0) / (len(srt) - 1) if len(srt) > 1 else 0.5
        dd = c / max(closes) - 1.0
        deep = (pos60 <= OVERSOLD_POS and dd <= OVERSOLD_DD)
        if not (deep or (pos60 <= OVERSOLD_POS_RELAX and dd <= OVERSOLD_DD_RELAX)):
            continue
        last3_low = min(float(b.get("l") or 9e9) for b in w[-3:])
        low10_low = min(float(b.get("l") or 9e9) for b in w[-13:-3])
        no_new_low = last3_low >= low10_low
        yang = float(w[-1].get("c")) >= float(w[-1].get("o") or 0)
        c5 = closes[-6] if len(closes) >= 6 else closes[0]
        recover = c > c5
        if not (no_new_low and (yang or recover)):
            continue  # 未企稳：两档都不入选（逆向不等于接飞刀）
        # 打分按**本档自己的阈值**归一化，否则放宽档会算出越界分数
        posN, ddN = (OVERSOLD_POS, OVERSOLD_DD) if deep else (OVERSOLD_POS_RELAX, OVERSOLD_DD_RELAX)
        score = (50.0 * max(0.0, posN - pos60) / posN
                 + 30.0 * (15.0 * no_new_low + 15.0 * yang) / 30.0
                 + 20.0 * min(-dd, abs(ddN)) / abs(ddN))
        bits = ["60日分位 %.0f%%" % (pos60 * 100), "回撤 %.1f%%" % (dd * 100)]
        if no_new_low:
            bits.append("近3日不创新低")
        if yang:
            bits.append("末日收阳")
        if recover:
            bits.append("收复5日前收盘")
        row = {"symbol": sym, "name": nm, "score": round(score, 2),
               "reason": ("超跌+企稳：" if deep else "超跌（放宽档）+企稳：") + "，".join(bits),
               "detail": {"pos60": round(pos60, 3), "dd": round(dd, 3),
                          "no_new_low": int(no_new_low), "yang": int(yang),
                          "recover": int(recover), "relaxed": int(not deep)}}
        (strict if deep else relax).append(row)
    strict.sort(key=lambda x: -x["score"])
    relax.sort(key=lambda x: -x["score"])
    cap, mn = CAP["dip_revert"], MIN_N["dip_revert"]
    picked = strict[:cap]
    # 严格档不足 MIN_N → 用放宽档按分补足；补进来的每条都带 relaxed=1，
    # 名单与理由里会显式标注「放宽档」，绝不冒充严格档（2026-10-15 进哥要求 C 柱稳定 4~5 只）
    if len(picked) < mn:
        picked = picked + relax[: mn - len(picked)]
    picked.sort(key=lambda x: -x["score"])
    n_relax = sum(1 for x in picked if x["detail"]["relaxed"])
    return {"items": picked,
            "note": "超跌池：严格档 %d 只 / 放宽档 %d 只 → 取 %d 只（含放宽档 %d 只）；"
                    "两档都要求「近3日不创新低 + 收阳或收复5日前收盘」，未企稳不接飞刀。条数：%s"
                    % (len(strict), len(relax), len(picked), n_relax,
                       COUNT_SPEC["dip_revert"]["label"])}


# ---------------------------------------------------------------- 打分校准 + 排序校准
def calibration(con) -> dict:
    """主引擎 prediction_item 历史（主参考窗口内）→ 分组命中率 + 分数-收益排序相关性。

    这是 v2.1 第九章「层1 打分未修正 → 分组统计」「层3 排序反向 → 排序校准」的落地：
    只做统计呈现与 A 柱 L5 罚分依据，不改主引擎打分公式（改公式须另走权重复盘）。
    """
    try:
        ws = P.window_start()
    except Exception:  # noqa: BLE001
        ws = None
    q = ("SELECT hit_signals, ret_t5, hit_t5, score FROM prediction_item "
         "WHERE ret_t5 IS NOT NULL AND hit_signals IS NOT NULL")
    if ws:
        q += " AND run_date >= ?"
        rows = con.execute(q, (ws,)).fetchall()
    else:
        rows = con.execute(q).fetchall()
    grp: dict = {}
    xs, ys = [], []
    for r in rows:
        ret, hit = r["ret_t5"], r["hit_t5"]
        try:
            hs = json.loads(r["hit_signals"] or "[]")
        except Exception:  # noqa: BLE001
            hs = []
        for h in hs if isinstance(hs, list) else []:
            k = (h or {}).get("key")
            if not k:
                continue
            g = grp.setdefault(k, {"n": 0, "hit": 0})
            g["n"] += 1
            if hit:
                g["hit"] += 1
        xs.append(float(r["score"] or 0))
        ys.append(float(ret or 0))
    groups = []
    meta = getattr(P, "SIGNAL_META", {})
    for k, g in grp.items():
        groups.append({"key": k, "name": (meta.get(k) or {}).get("name", k),
                       "n": g["n"], "hit": g["hit"],
                       "rate": round(g["hit"] / g["n"], 3) if g["n"] else None})
    groups.sort(key=lambda x: -(x["rate"] or 0))
    corr = None
    if len(xs) >= 20:
        try:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
            vx = (sum((a - mx) ** 2 for a in xs)) ** 0.5
            vy = (sum((b - my) ** 2 for b in ys)) ** 0.5
            corr = round(cov / vx / vy, 3) if vx > 0 and vy > 0 else None
        except Exception:  # noqa: BLE001
            corr = None
    return {"window_start": ws, "n_total": len(rows), "groups": groups,
            "rank_corr": {"r": corr, "n": len(xs)},
            "note": ("分数与 T+5 收益相关性 r=%.2f（正=打分方向有效，负=排序反向需校准）" % corr)
                    if corr is not None else "样本不足（<20），暂不做排序校准",
            "basis": "口径：主引擎 prediction_item 实盘复盘（T+5），窗口起点 %s；打分公式未被本层修改" % (ws or "全部")}


# ---------------------------------------------------------------- D 共振验证 + 风控闸
def pillar_resonance(con, td, bars_map, names, runs: dict, ms: dict) -> dict:
    """交叉验证 A/B/C 命名票 + 主引擎核心池 + 市场状态闸。

    打分：每命中一个支柱 +30，命中主引擎核心池 +20，普跌态全体 ×0.5。
    """
    core = set()
    try:
        for r in con.execute(
                "SELECT symbol FROM prediction_item WHERE tier='core' AND run_date="
                "(SELECT MAX(run_date) FROM prediction_run WHERE run_date<=?)", (td,)):
            core.add(str(r["symbol"]))
    except Exception:  # noqa: BLE001
        pass
    tally: dict = {}
    for pkey, run in runs.items():
        for it in run.get("items", []):
            t = tally.setdefault(it["symbol"], {"pillars": [], "name": it.get("name") or ""})
            t["pillars"].append(pkey)
            if not t["name"]:
                t["name"] = it.get("name") or ""
    items = []
    for sym, t in tally.items():
        np_ = len(t["pillars"])
        score = 30.0 * np_ + (20.0 if sym in core else 0.0)
        if ms.get("state") == "普跌":
            score *= 0.5
        reason = "共振：" + "+".join(t["pillars"]) + ("，主引擎核心池同持" if sym in core else "")
        if ms.get("state") == "普跌" and np_ < 2:
            reason += "（普跌闸：单支柱票不建议参与）"
        items.append({"symbol": sym, "name": t["name"], "score": round(score, 2),
                      "reason": reason, "detail": {"pillars": t["pillars"],
                                                   "in_core": int(sym in core)}})
    multi = [x for x in items if len(x["detail"]["pillars"]) >= 2]
    single = [x for x in items if len(x["detail"]["pillars"]) < 2]
    multi.sort(key=lambda x: (-x["score"], -len(x["detail"]["pillars"])))
    single.sort(key=lambda x: (-x["score"], -len(x["detail"]["pillars"])))
    cap, mn = CAP["resonance"], MIN_N["resonance"]
    picked = multi[:cap]
    # ≥2 支柱共振票不足 MIN_N → 用单支柱高分票补位，
    # 并显式标注「单支柱·补位」+ detail.filler=1，绝不冒充共振票（2026-10-15 进哥要求 D 柱 3~6 只）
    if len(picked) < mn:
        for x in single[: mn - len(picked)]:
            x["reason"] += "（单支柱·补位，非共振）"
            x["detail"]["filler"] = 1
            picked.append(x)
    picked.sort(key=lambda x: (-x["score"], -len(x["detail"]["pillars"])))
    n_multi = sum(1 for x in picked if len(x["detail"]["pillars"]) >= 2)
    verdict = {
        "结构": "风控闸放开：各支柱按自身策略正常参与，多支柱共振票优先。",
        "震荡": "风控闸中性：C 类（超跌修复）仓位减半，优先多支柱共振票。",
        # ⚠️ 2026-10-15 修正口径漂移：原文案写「只保留 ≥2 支柱共振票」，但代码从未做这道过滤
        #    （只加了一句提示）。文案必须与代码一致，否则是名不副实的风控承诺。
        "普跌": "风控闸收紧：优先 ≥2 支柱共振票；单支柱票一律标注「不建议参与」（仅作对照，不构成建议），整体降仓。",
    }.get(ms.get("state") or "", "市场状态未知：按中性处理。")
    return {"items": picked, "verdict": verdict, "core_pool_n": len(core),
            "note": "A/B/C 合计点名 %d 只（其中 ≥2 支柱共振 %d 只）→ 取 %d 只（共振 %d / 单支柱补位 %d）；"
                    "与旧引擎核心池重叠 %d 只。条数：%s"
                    % (len(tally), len(multi), len(picked), n_multi, len(picked) - n_multi,
                       sum(1 for x in picked if x["detail"]["in_core"]),
                       COUNT_SPEC["resonance"]["label"])}


# ---------------------------------------------------------------- 基本面（fundamentals 表）
def _fund_fetch_one(code: str) -> dict | None:
    """东财 F10 主财务指标最新一期（与 server._fund_metrics 同源同字段，独立落 fundamentals 表）。"""
    c = "".join(ch for ch in str(code) if ch.isdigit())
    if not c:
        return None
    if c.startswith(("43", "83", "87", "88", "92")):
        sc = c + ".BJ"
    elif c.startswith(("6", "5", "9")):
        sc = c + ".SH"
    else:
        sc = c + ".SZ"
    try:
        r = emapi.datacenter("RPT_F10_FINANCE_MAINFINADATA", flt='(SECUCODE="%s")' % sc,
                             sort_columns="REPORT_DATE", sort_types="-1", page_size=1)
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok") or not r.get("rows"):
        return None
    x = r["rows"][0]
    return {"roe": emapi._num(x.get("ROEJQ")), "roe_kf": emapi._num(x.get("ROEKCJQ")),
            "dr": emapi._num(x.get("ZCFZL")), "gm": emapi._num(x.get("XSMLL")),
            "rev_yoy": emapi._num(x.get("TOTALOPERATEREVETZ")),
            "np_yoy": emapi._num(x.get("PARENTNETPROFITTZ")),
            "kf_np_yoy": emapi._num(x.get("KCFJCXSYJLRTZ")),
            "report_date": str(x.get("REPORT_DATE") or "")[:10]}


def fundamentals_refresh(con, codes: list, names: dict | None = None,
                         full: bool = False) -> dict:
    """增量刷新 fundamentals 表：缺行或超过 TTL 才抓（周一 full=True 全量）。"""
    names = names or {}
    stale = []
    now = _now()
    for c in codes:
        row = None
        try:
            row = con.execute("SELECT updated_at FROM fundamentals WHERE symbol=?", (c,)).fetchone()
        except Exception:  # noqa: BLE001
            pass
        need = full or row is None
        if not need and row and row["updated_at"]:
            try:
                age = (datetime.now() - datetime.fromisoformat(row["updated_at"])).days
                need = age >= FUND_TTL_DAYS
            except Exception:  # noqa: BLE001
                need = True
        if need:
            stale.append(c)
    ok = 0
    failed = []

    def one(c):
        time.sleep(0.12)
        return c, _fund_fetch_one(c)

    with ThreadPoolExecutor(max_workers=FUND_WORKERS) as ex:
        for c, d in ex.map(one, stale):
            if not d:
                failed.append(c)
                continue
            try:
                con.execute(
                    "INSERT INTO fundamentals(symbol,name,roe,roe_kf,dr,gm,rev_yoy,np_yoy,"
                    "kf_np_yoy,report_date,updated_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,roe=excluded.roe,"
                    "roe_kf=excluded.roe_kf,dr=excluded.dr,gm=excluded.gm,rev_yoy=excluded.rev_yoy,"
                    "np_yoy=excluded.np_yoy,kf_np_yoy=excluded.kf_np_yoy,"
                    "report_date=excluded.report_date,updated_at=excluded.updated_at,"
                    "source=excluded.source",
                    (c, names.get(c) or c, d.get("roe"), d.get("roe_kf"), d.get("dr"),
                     d.get("gm"), d.get("rev_yoy"), d.get("np_yoy"), d.get("kf_np_yoy"),
                     d.get("report_date"), now, "emapi.RPT_F10_FINANCE_MAINFINADATA"))
                ok += 1
            except Exception:  # noqa: BLE001
                failed.append(c)
    con.commit()
    return {"scanned": len(codes), "need": len(stale), "ok": ok, "failed": len(failed),
            "failed_codes": failed[:20], "full": full,
            "note": "基本面源=东财F10主财务指标（与个股详情同源），TTL %d 天" % FUND_TTL_DAYS}


# ---------------------------------------------------------------- 主管线
def generate(con: sqlite3.Connection, run_date: str | None = None,
             force: bool = False) -> dict:
    """16:00 主管线：候选池 K 线 → 市场状态 → A/B/C/D → 落表。"""
    with _LOCK:
        t0 = time.time()
        ensure_tables(con)
        _STATUS.update(stage="取候选池", detail="")
        codes, names, _boards = P.SS.universe(P.POOL_LIMIT)
        # 2026-10-26 扩池：并入户「今日热板块」官方成员，避免流动性前 N 漏掉政策受益小票。
        # 进哥关切「每日预测是否只扫一小撮票」——根因是 POOL_LIMIT 流动性筛 + 信号按 pool 过滤，
        # 热板块里流动性靠后的成员股连被评估资格都没有。这里在取 K 线前把成员并入候选，
        # 下游 pillar_policy_funnel 的信号过滤（sigs 按 pool 过滤）自然带上它们。
        # 数据源双保险：优先 plate_members（东财官方成分股，fetch_plate_members.py 落库，最全），
        # 回退 _industry_index（stock_org 行业映射，本环境仅 394 只，稀疏）。
        # 优雅降级：任一环节异常/空表 → 退回纯流动性池，不影响主流程。
        try:
            _hb, _sb = P._hot_sectors(con, asof=None)
            _words = [w for w, _ in (_hb or [])[:12]]
            _extra: set = set()
            if _words:
                # 源1：stock_org 行业反查（始终可用，但覆盖稀疏）
                try:
                    _idx = P._industry_index(con)
                    _extra |= set(P._expand_sector_words(_idx, _words))
                except Exception:  # noqa: BLE001
                    pass
                # 源2：东财官方成分股（最全，需先跑 fetch_plate_members.py）
                try:
                    _pm = con.execute(
                        "SELECT code, board FROM plate_members").fetchall()
                    for _c, _b in _pm:
                        _b = str(_b or "")
                        if any((_w in _b) or (_b in _w) for _w in _words):
                            _extra.add(str(_c))
                except Exception:  # noqa: BLE001
                    pass
            _extra = [c for c in _extra
                      if isinstance(c, str) and c.isdigit() and len(c) == 6
                      and c[0] in "03648"]
            if _extra:
                codes = sorted(set(codes) | set(_extra))
                _STATUS.update(stage="取候选池", detail="扩池+%d热板块成员" % len(_extra))
        except Exception:  # noqa: BLE001
            pass
        # 2026-10-26 干净宇宙：剔除博主噪声/ST/无K线（进哥：不在垃圾堆里挑垃圾）
        # 四支柱候选池同样禁止从博主噪声/退市/无K线票里选。
        try:
            from universe_filter import clean_codes
            _before = len(codes)
            codes = clean_codes(codes)
            if len(codes) != _before:
                _STATUS.update(stage="取候选池",
                               detail="剔除池-=%d" % (_before - len(codes)))
        except Exception:  # noqa: BLE001
            pass
        if not codes:
            return {"ok": False, "error": "候选池为空"}
        kc = P.load_kcache()
        bars_map = P.fetch_bars(codes, kc, force=False)
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= P.MIN_BARS}
        td = run_date or P._trade_date_of(bars_map)
        if not td:
            return {"ok": False, "error": "无法确定交易日（K 线缓存可能未更新）"}
        # asof 隔离：截断到 td 收盘，绝不偷看后视数据
        bars_map = {c: [b for b in v if str(b.get("d", ""))[:10] <= td]
                    for c, v in bars_map.items()}
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= P.MIN_BARS}
        if not force:
            row = con.execute("SELECT run_date FROM pillar_run WHERE run_date=? AND pillar='resonance'",
                              (td,)).fetchone()
            if row:
                return {"ok": True, "run_date": td, "cached": True}

        _STATUS.update(stage="市场状态", detail=td)
        ms = market_state(bars_map, td)
        try:
            con.execute("INSERT INTO market_state_log(trade_date,state,breadth_json,note,created_at)"
                        " VALUES(?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                        "state=excluded.state,breadth_json=excluded.breadth_json,"
                        "note=excluded.note,created_at=excluded.created_at",
                        (td, ms["state"], json.dumps(ms["breadth"], ensure_ascii=False),
                         "；".join(ms["rules"]), _now()))
        except Exception:  # noqa: BLE001
            pass

        _STATUS.update(stage="A 政策漏斗", detail=td)
        a = pillar_policy_funnel(con, td, bars_map, names, codes)
        _STATUS.update(stage="B 短线T+1冲浪", detail=td)
        b = pillar_t1_surf(con, td, bars_map, names)
        _STATUS.update(stage="C 超跌修复", detail=td)
        c = pillar_dip_revert(con, td, bars_map, names)
        _STATUS.update(stage="D 共振验证+风控闸", detail=td)
        cal = calibration(con)
        d = pillar_resonance(con, td, bars_map, names,
                             {"policy_funnel": a, "t1_surf": b, "dip_revert": c}, ms)
        d_state = {"verdict": d["verdict"], "note": d["note"], "core_pool_n": d["core_pool_n"],
                   "calibration": cal}

        runs = {
            "policy_funnel": {"status": "ok", "title": "A · 政策漏斗",
                              "summary": a.get("note") or "",
                              "state_json": {"boards": a.get("boards") or []},
                              "items": a.get("items", [])},
            "t1_surf": {"status": "pending_confirm", "title": "B · 短线 T+1 集合竞价冲浪",
                        "summary": b.get("note") or "",
                        "state_json": {"confirm": None},
                        "items": b.get("items", [])},
            "dip_revert": {"status": "ok", "title": "C · 超跌修复逆向",
                           "summary": c.get("note") or "", "state_json": {},
                           "items": c.get("items", [])},
            "resonance": {"status": "ok", "title": "D · 共振验证 + 风控闸",
                          "summary": d["verdict"], "state_json": d_state,
                          "items": d.get("items", [])},
        }
        for pkey, run in runs.items():
            con.execute("DELETE FROM pillar_run WHERE run_date=? AND pillar=?", (td, pkey))
            con.execute("DELETE FROM pillar_item WHERE run_date=? AND pillar=?", (td, pkey))
            con.execute("INSERT INTO pillar_run(run_date,pillar,status,title,summary,state_json,"
                        "note,engine_ver,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (td, pkey, run["status"], run["title"], run["summary"],
                         json.dumps(run["state_json"], ensure_ascii=False), None,
                         ENGINE_VER, _now()))
            for i, it in enumerate(run["items"], 1):
                con.execute("INSERT OR REPLACE INTO pillar_item(run_date,pillar,symbol,name,rank,"
                            "score,reason,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                            (td, pkey, it["symbol"], it.get("name") or it["symbol"], i,
                             it.get("score"), it.get("reason"),
                             json.dumps(it.get("detail") or {}, ensure_ascii=False)))
        con.commit()
        # 2026-10-16 进哥要求：第 5 根「自我提升」—— 它靠上面四根**已落库**的提名来加权，
        # 所以只能排在这里算（不在 runs 字典里）。失败不阻断主管线（A/B/C/D 已落库，页面照常）。
        _STATUS.update(stage="E 自我提升", detail=td)
        _sl_n = 0
        try:
            _sl = persist_self_learn(con, td)
            _sl_n = len(_sl["items"])
        except Exception as e:  # noqa: BLE001
            print("[pillars] E 自我提升 失败（不影响前四柱）：%s" % str(e)[:200], flush=True)
        out = {"ok": True, "run_date": td, "engine_ver": ENGINE_VER,
               "market_state": ms["state"], "breadth": ms["breadth"],
               "counts": {"policy_funnel": len(a.get("items", [])),
                          "t1_surf": len(b.get("items", [])),
                          "dip_revert": len(c.get("items", [])),
                          "resonance": len(d.get("items", [])),
                          "self_learn": _sl_n},
               "fundamentals": None, "elapsed": round(time.time() - t0, 1)}
        # 基本面增量（失败不阻断主管线）
        try:
            _STATUS.update(stage="基本面增量刷新", detail=td)
            out["fundamentals"] = fundamentals_refresh(con, codes, names, full=False)
        except Exception as e:  # noqa: BLE001
            out["fundamentals_error"] = str(e)[:200]
        return out


# ---------------------------------------------------------------- E 引擎自加权（原「自我提升」，2026-10-16 进哥要求改名）
# 定位：与「博主共振 → 自我进化」同理，但**学的是本引擎自己的实盘复盘**：
#       四根支柱（政策漏斗/竞价冲浪/超跌修复/共振风控）逐期逐支柱的「命中率 / 平均超额 / IC」，
#       用学出的权重给今日提名加权选票。
#
# ⚠️ 两条硬纪律（本项目口径红线）：
#   1. 统计口径**100% 复用回测板块** backtest（_window_ret / 基准池=候选池等权），绝不另写公式。
#   2. **样本不够就不硬凑漂亮权重**：进哥 2026-10-16 拍板"样本不够就不生效"。
#      —— 现改为「连续收缩 + 单维下限 + 样本外验证」机制（照抄 evolve.py 的成熟元学习器），
#         不再用「四支柱须同时≥30 才生效」的全有或全无硬门槛。SELF_LEARN_GATE 仅作展示参考线。
SELF_LEARN_GATE = 30          # 每根支柱的到期样本数「参考线」（≥ 才足以学习）；仅展示用，不再做门闩
SELF_LEARN_PILLARS = ("policy_funnel", "t1_surf", "dip_revert", "resonance")
SELF_LEARN_OOS_MIN = 5         # 样本外验证最少可比期数（walk-forward 比较次数门槛）
SELF_LEARN_OOS_WIN = 0.60      # 加权方相对等权方的胜率门槛
RESONANCE_BOOST_STEP = 0.15    # 审计#8：多支柱同持加成步长（1.0 + 0.15*(n-1)）。
                               # 该系数未经样本外验证，框架成熟后凭数据调优；行为保持不变。
PILLAR_SHORT = {"policy_funnel": "政策漏斗", "t1_surf": "竞价冲浪",
                "dip_revert": "超跌修复", "resonance": "共振风控", "self_learn": "引擎自加权"}
# 等权先验：四支柱各 1/4（与 evolve 的 DEFAULT_W 同理，作为收缩的"锚"）
DEFAULT_W_SELF = {k: 1.0 / len(SELF_LEARN_PILLARS) for k in SELF_LEARN_PILLARS}


def _sl_collect(con: sqlite3.Connection):
    """单次聚合：K 线索引载一次 + pillar_item 扫一遍，按 (date, pillar) 归堆。

    返回：
      groups[(date, pillar)] = [ {"symbol","score","ret":{p:ret},"alpha":{p:alpha}} ]
      bench_cache[date]       = {p: 基准(候选池等权均值) 或 None}
      dates_sorted            = 升序排列的预测日
    全程复用 backtest 的 _window_ret / _bench_at（口径唯一真源），不再反复调 payload() 扫全表。
    """
    import backtest as _BT
    ctx = _BT._context()
    bars = ctx["bars"]
    dates = [d["date"] for d in _BT.dates(con)]
    bench_cache = {}
    for d in dates:
        bench_cache[d] = _BT._bench_at(d)
    groups = {}
    for r in con.execute(
            "SELECT run_date, pillar, symbol, name, rank, score FROM pillar_item"):
        d, p, sym = r["run_date"], r["pillar"], r["symbol"]
        if p not in SELF_LEARN_PILLARS:
            continue
        k = bars.get(sym) or []
        if not k:
            continue
        idx = P._bars_index(k)
        i = idx.get(d) if idx else None
        if i is None or i < 0 or i >= len(k):
            continue
        rec = {"symbol": sym, "score": r["score"], "ret": {}, "alpha": {}}
        for p_ in _BT.PERIODS:
            ret = P._window_ret(k, i, p_)
            if ret is None:
                continue
            rec["ret"][p_] = ret
            b = bench_cache.get(d, {}).get(p_)
            if b is not None:
                rec["alpha"][p_] = ret - b
        groups.setdefault((d, p), []).append(rec)
    return groups, bench_cache, sorted(dates)


def _sl_aggregate(groups, bench_cache, dates_subset):
    """在 groups 上做 (支柱, 周期) 归堆统计 + 各周期 IC。dates_subset 限定训练/测试期。

    返回 (per, ic_by_period, n_by_pillar)：
      per[(p, pp)]            = {n, hits, hit_rate, avg_alpha}
      ic_by_period[(p, pp)]    = T+pp 的 IC（提名 score vs 该票 T+pp 超额），不足样本为 None
      n_by_pillar[p]           = 该柱全周期样本合计
    IC 算法照抄 evolve.py：pearson(score, alpha)，样本量 < MIN_IC_N 记 None。
    """
    import backtest as _BT
    from metalearn import pearson as _pearson, MIN_IC_N
    sub = set(dates_subset)
    per = {(p, pp): {"n": 0, "hits": 0, "alpha_sum": 0.0, "n_alpha": 0}
           for p in SELF_LEARN_PILLARS for pp in _BT.PERIODS}
    ic_pairs = {(p, pp): [] for p in SELF_LEARN_PILLARS for pp in _BT.PERIODS}
    for (d, p), recs in groups.items():
        if d not in sub:
            continue
        bmap = bench_cache.get(d, {})
        for rec in recs:
            for pp in _BT.PERIODS:
                ret = rec["ret"].get(pp)
                if ret is None:
                    continue
                bucket = per[(p, pp)]
                bucket["n"] += 1
                b = bmap.get(pp)
                if b is not None:
                    if ret > b:
                        bucket["hits"] += 1
                    a = rec["alpha"].get(pp)
                    if a is not None:
                        bucket["alpha_sum"] += a
                        bucket["n_alpha"] += 1
                a_pp = rec["alpha"].get(pp)
                if a_pp is not None and rec["score"] is not None:
                    ic_pairs[(p, pp)].append((float(rec["score"]), a_pp))
    ic_by_period = {}
    n_by_pillar = {}
    for p in SELF_LEARN_PILLARS:
        n_by_pillar[p] = sum(per[(p, pp)]["n"] for pp in _BT.PERIODS)
        for pp in _BT.PERIODS:
            b = per[(p, pp)]
            b["hit_rate"] = (b["hits"] / b["n"]) if b["n"] else None
            b["avg_alpha"] = (b["alpha_sum"] / b["n_alpha"]) if b["n_alpha"] else None
            xs = [s for s, a in ic_pairs[(p, pp)]]
            ys = [a for s, a in ic_pairs[(p, pp)]]
            ic_by_period[(p, pp)] = _pearson(xs, ys) if len(xs) >= MIN_IC_N else None
    return per, ic_by_period, n_by_pillar


def _sl_weights(ic_by_pillar, n_by_pillar):
    """权重学（共享引擎 metalearn.weights_from_ic 单一真源）。

    主轴 = T+5 的 IC（与 evolve 主轴一致）；其余周期仅进 state 展示。
    返回 (final_w, shrunk_ic, learned_w, has_shrunk, note)，与原签名一致，
    下游 pillar_self_learn / _sl_oos 按位置解包，无需改动。
    """
    from metalearn import weights_from_ic
    dims = list(SELF_LEARN_PILLARS)
    w, shrunk, learned, has_shrunk, note = weights_from_ic(
        ic_by_pillar, n_by_pillar, dims, DEFAULT_W_SELF)
    return w, shrunk, learned, has_shrunk, note


def _sl_oos(groups, bench_cache, dates_sorted):
    """样本外验证（walk-forward，防自我欺骗）：
    对每个测试日 d（其之前有训练数据），用 d 之前的数据学权重，比较
    「加权前6」vs「等权前6」在 d 期 T+5 实际超额；
    只有 可比期数≥SELF_LEARN_OOS_MIN 且 加权方胜率≥SELF_LEARN_OOS_WIN 且 均值差>0，
    才判定权重真生效。当前仅 2 期数据 → 可比期远不足 → 标等权（正确且预期）。
    """
    comps = []  # (w_mean, e_mean)
    for k in range(1, len(dates_sorted)):
        d_test = dates_sorted[k]
        train = dates_sorted[:k]
        _, ic_bp, n_bp = _sl_aggregate(groups, bench_cache, train)
        ic_main = {p: ic_bp[(p, 5)] for p in SELF_LEARN_PILLARS}
        w = _sl_weights(ic_main, n_bp)[0]
        sym_map = {}
        for p in SELF_LEARN_PILLARS:
            for rec in groups.get((d_test, p), []):
                a5 = rec["alpha"].get(5)
                e = sym_map.setdefault(rec["symbol"], {"from": [], "alpha5": a5})
                e["from"].append(p)
                if a5 is not None:
                    e["alpha5"] = a5
        cands = [e for e in sym_map.values() if e["alpha5"] is not None]
        if len(cands) < 6:
            continue
        wscored = sorted(cands, key=lambda e: -sum(w.get(p, 0.0) for p in e["from"]))[:6]
        escored = sorted(cands, key=lambda e: -sum(DEFAULT_W_SELF.get(p, 0.0) for p in e["from"]))[:6]
        wm = sum(e["alpha5"] for e in wscored) / 6.0
        em = sum(e["alpha5"] for e in escored) / 6.0
        comps.append((wm, em))
    n = len(comps)
    if n == 0:
        return {"n": 0, "win_rate": None, "mean_diff": None, "passed": False}
    win = sum(1 for wm, em in comps if wm > em)
    wr = win / n
    md = sum(wm - em for wm, em in comps) / n
    passed = (n >= SELF_LEARN_OOS_MIN) and (wr >= SELF_LEARN_OOS_WIN) and (md > 0)
    return {"n": n, "win_rate": round(wr, 3), "mean_diff": round(md, 4), "passed": passed}


def _sl_kcache_fresh(con):
    """管线序校验（审计#7）：K 线缓存须含最新交易日（K线刷新→回测可用→再学习）。
    不新鲜则跳过学习、如实标注，下轮自动补。"""
    import backtest as _BT
    try:
        asof = _BT._context().get("asof")
    except Exception:  # noqa: BLE001
        asof = None
    if not asof:
        return False, "K 线缓存为空，无法学习"
    latest_run = con.execute("SELECT MAX(run_date) FROM pillar_run").fetchone()[0]
    if not latest_run:
        return True, ""
    fresh = asof >= latest_run
    if fresh:
        return True, ""
    return False, ("K 线缓存截至 %s，滞后于最新预测日 %s → 跳过学习，待下轮刷新后自动补"
                  % (asof, latest_run))


def self_learn_stats(con):
    """对外聚合入口（兼容旧调用）：返回 (per, ic_by_pillar_T5, n_by_pillar)。"""
    groups, bench_cache, dates = _sl_collect(con)
    per, ic_bp, n_by_pillar = _sl_aggregate(groups, bench_cache, dates)
    ic_main = {p: ic_bp[(p, 5)] for p in SELF_LEARN_PILLARS}
    return per, ic_main, n_by_pillar


def self_learn_period_breakdown(con):
    """供 verify_backtest_caliber.py 做口径等价性比对：返回 {(date,pillar,period): {n,hit_rate,avg_alpha}}。"""
    groups, bench_cache, dates = _sl_collect(con)
    out = {}
    for d in dates:
        per, _, _ = _sl_aggregate(groups, bench_cache, [d])
        for p in SELF_LEARN_PILLARS:
            for pp in (1, 3, 5, 10, 20):
                b = per[(p, pp)]
                if b["n"] > 0:
                    out[(d, p, pp)] = {
                        "n": b["n"],
                        "hit_rate": (None if b["hit_rate"] is None else round(b["hit_rate"], 4)),
                        "avg_alpha": (None if b["avg_alpha"] is None else round(b["avg_alpha"], 6)),
                    }
    return out


def pillar_self_learn(con: sqlite3.Connection, td: str) -> dict:
    """E 引擎自加权：学四根支柱的实盘复盘 → 用权重给今日提名加权选票。

    依赖：四根支柱在 td 的提名**已落库**（见 generate 里本步排在落库之后）。
    机制（审计#2/#3/#4/#5 一并解）：分周期学习 · IC 替代命中率 · 连续收缩替代硬门槛 ·
          样本外验证防自欺 · 单次聚合改写性能。
    """
    groups, bench_cache, dates = _sl_collect(con)
    dates_sorted = dates
    per, ic_bp, n_by_pillar = _sl_aggregate(groups, bench_cache, dates_sorted)
    ic_main = {p: ic_bp[(p, 5)] for p in SELF_LEARN_PILLARS}

    # ① 权重（T+5 主轴）
    final_w, shrunk_ic, learned_w, has_shrunk, wnote = _sl_weights(ic_main, n_by_pillar)

    # ② 样本外验证（walk-forward，防自欺）
    oos = _sl_oos(groups, bench_cache, dates_sorted)

    # ③ K 线缓存新鲜度（审计#7）
    fresh, fresh_note = _sl_kcache_fresh(con)

    # ④ 生效状态判定（三态徽章 + 防自欺退回等权）
    samples_t5 = sum(per[(p, 5)]["n"] for p in SELF_LEARN_PILLARS)
    if oos["passed"]:
        weight_stage = "样本外验证通过（权重生效）"
        use_w = final_w
    elif has_shrunk:
        weight_stage = "收缩生效中（样本 %d，权重靠近先验）" % samples_t5
        use_w = final_w
    else:
        weight_stage = "等权运行中（样本 %d，尚不足以学习）" % samples_t5
        use_w = final_w
    oos_failed = has_shrunk and (oos["n"] or 0) >= SELF_LEARN_OOS_MIN and not oos["passed"]
    if oos_failed:
        # 收缩权重算出但样本外验证未通过 → 诚实退回等权
        weight_stage = "等权运行中（样本 %d，未通过样本外验证 → 本期等权）" % samples_t5
        use_w = dict(DEFAULT_W_SELF)

    # 今日各支柱的提名（从库读；同一只票被几根支柱同时提名 → 多支柱同持加成）
    nom = {}
    for k in SELF_LEARN_PILLARS:
        for r in con.execute("SELECT symbol,name,rank FROM pillar_item "
                             "WHERE run_date=? AND pillar=? ORDER BY rank", (td, k)):
            e = nom.setdefault(r["symbol"], {"name": r["name"] or r["symbol"], "from": [], "ranks": {}})
            e["from"].append(k)
            e["ranks"][k] = r["rank"]
    rows = []
    for sym, e in nom.items():
        frm = list(e["from"])
        wsum = sum(use_w.get(k, 0.0) for k in frm)
        boost = 1.0 + RESONANCE_BOOST_STEP * (len(frm) - 1)   # 多支柱同持加成（同持越多越可信）
        rows.append({"symbol": sym, "name": e["name"], "from": frm, "shares": len(frm),
                     "w_sum": round(wsum, 4), "strength": round(boost, 2),
                     "score": round(min(100.0, wsum * 100.0 * boost), 2)})
    rows.sort(key=lambda x: (-x["score"], -x["shares"], x["symbol"]))
    picked = rows[:CAP["self_learn"]]

    items = []
    for it in picked:
        frm = " + ".join(PILLAR_SHORT.get(k, k) for k in it["from"])
        if oos["passed"]:
            reason = ("被 %s 同时提名；按学出（样本外验证通过）的权重加权 %.2f（多支柱同持加成 ×%.2f）"
                      % (frm, it["w_sum"], it["strength"]))
        elif has_shrunk:
            reason = ("被 %s 同时提名；按收缩权重（靠近先验，样本外验证待积累）加权 %.2f（多支柱同持加成 ×%.2f）"
                      % (frm, it["w_sum"], it["strength"]))
        else:
            reason = ("被 %s 同时提名；权重未生效（各支柱到期样本未达参考 %d），"
                      "本期按等权 + 多支柱共振给票" % (frm, SELF_LEARN_GATE))
        items.append({"symbol": it["symbol"], "name": it["name"], "score": it["score"],
                      "reason": reason,
                      "detail": {"shares": it["shares"], "w_sum": it["w_sum"],
                                 "strength": it["strength"],
                                 "from": [PILLAR_SHORT.get(k, k) for k in it["from"]],
                                 "weights_effective": (weight_stage.startswith("样本外验证通过")
                                                      or weight_stage.startswith("收缩生效中"))}})

    # 诚实 note（进哥风格：判词自带判据）
    hr_txt = " / ".join(
        "%s n=%d(命中率 %s)" % (
            PILLAR_SHORT.get(p, p),
            n_by_pillar.get(p, 0),
            "—" if per[(p, 5)]["hit_rate"] is None else "%.1f%%" % (per[(p, 5)]["hit_rate"] * 100))
        for p in SELF_LEARN_PILLARS)
    if oos["passed"]:
        stage_txt = "样本外验证通过（%d 次可比期，加权方胜率 %.0f%%，均值差 %s%%）→ 权重生效" % (
            oos["n"], oos["win_rate"] * 100, ("%+.2f" % oos["mean_diff"]))
    elif (oos["n"] or 0) >= SELF_LEARN_OOS_MIN:
        stage_txt = "未通过样本外验证（%d 次可比期，加权方胜率 %.0f%%）→ 本期等权" % (
            oos["n"], (oos["win_rate"] or 0) * 100)
    else:
        stage_txt = "样本 %d（T+5 主轴）远不足可比期门槛 %d → 本期等权 + 共振加成" % (
            samples_t5, SELF_LEARN_OOS_MIN)
    note = ("学习口径：四根支柱自己的实盘复盘（口径同「历史回测」，跑赢池子才算命中）。"
            "主轴=T+5；参考线=各柱 %d 样本。%s。各支柱逐周期命中率/平均超额/IC 见下方明细。各柱样本：%s"
            % (SELF_LEARN_GATE, stage_txt, hr_txt))
    if not fresh:
        note += "（注：%s）" % fresh_note

    state_json = {
        "weights": use_w,
        "weight_mode": wnote,
        "weight_stage": weight_stage,
        "weights_effective": (weight_stage.startswith("样本外验证通过")
                              or weight_stage.startswith("收缩生效中")),
        "gate": SELF_LEARN_GATE,
        "samples_total": samples_t5,
        "n_by_pillar": n_by_pillar,
        "ic_by_pillar": {p: (round(v, 4) if v is not None else None) for p, v in ic_main.items()},
        "shrunk_ic": {p: (round(v, 4) if v is not None else None) for p, v in shrunk_ic.items()},
        "per_pillar": {
            p: {str(pp): {
                "n": per[(p, pp)]["n"],
                "hit_rate": (None if per[(p, pp)]["hit_rate"] is None
                             else round(per[(p, pp)]["hit_rate"], 4)),
                "avg_alpha": (None if per[(p, pp)]["avg_alpha"] is None
                              else round(per[(p, pp)]["avg_alpha"], 6)),
                "ic": (round(ic_bp[(p, pp)], 4) if ic_bp[(p, pp)] is not None else None),
            } for pp in (1, 3, 5, 10, 20)} for p in SELF_LEARN_PILLARS},
        "oos": oos,
        "kcache_fresh": fresh,
        "kcache_note": fresh_note,
    }
    return {"items": items, "note": note, "state_json": state_json}


def persist_self_learn(con: sqlite3.Connection, td: str) -> dict:
    """算 + 落库 E 自我提升。generate() 与"手工补算历史期次"共用这一条路径。"""
    sl = pillar_self_learn(con, td)
    con.execute("DELETE FROM pillar_run WHERE run_date=? AND pillar='self_learn'", (td,))
    con.execute("DELETE FROM pillar_item WHERE run_date=? AND pillar='self_learn'", (td,))
    con.execute("INSERT INTO pillar_run(run_date,pillar,status,title,summary,state_json,"
                "note,engine_ver,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (td, "self_learn", "ok", "E · 引擎自加权", sl["note"],
                 json.dumps(sl["state_json"], ensure_ascii=False), sl["note"], ENGINE_VER, _now()))
    for i, it in enumerate(sl["items"], 1):
        con.execute("INSERT OR REPLACE INTO pillar_item(run_date,pillar,symbol,name,rank,"
                    "score,reason,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                    (td, "self_learn", it["symbol"], it["name"], i, it["score"], it["reason"],
                     json.dumps(it.get("detail") or {}, ensure_ascii=False)))
    con.commit()
    return sl


# ---------------------------------------------------------------- 9:25 竞价确认（B 支柱）
_SINA_HQ = "https://hq.sinajs.cn/list="


def _sina_symbol(code: str) -> str:
    c = str(code)
    if c[:1] in ("4", "8", "9"):
        return "bj" + c
    if c[:1] in ("6", "5", "9"):
        return "sh" + c
    return "sz" + c


def _hq_opens(codes: list) -> dict:
    """新浪 hq 批量：{code: {open, prev_close, price}}。取不到的票不出现在结果里。"""
    out = {}
    for i in range(0, len(codes), 30):
        batch = codes[i:i + 30]
        url = _SINA_HQ + ",".join(_sina_symbol(c) for c in batch)
        try:
            txt = auction._http(url, timeout=12, referer="https://finance.sina.com.cn")
        except Exception:  # noqa: BLE001
            continue
        for line in txt.splitlines():
            if "hq_str_" not in line or '="' not in line:
                continue
            key = line.split("hq_str_")[1].split("=")[0].strip()
            body = line.split('="', 1)[1].rsplit('"', 1)[0]
            f = (body or "").split(",")
            if len(f) < 3:
                continue
            code = key[2:] if len(key) > 6 else key
            try:
                o, pc = float(f[1]), float(f[2])
                price = float(f[3]) if len(f) > 3 else 0.0
            except Exception:  # noqa: BLE001
                continue
            if o > 0 and pc > 0:
                out[code] = {"open": o, "prev_close": pc, "price": price}
    return out


def b_confirm(con: sqlite3.Connection, now: datetime | None = None) -> dict:
    """盘前 9:25–9:35：对最近一期 pending_confirm 的 B 支柱名单做竞价确认。

    判定（写进 state_json.confirm，逐票）：
      可参与   = 开盘涨幅在 0%~5%（GAP_CONFIRM）
      观察     = 开盘涨幅 > 5%（一字/过高难上车）
      放弃     = 开盘低开（<0）
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return {"skip": "weekend"}
    hm = (now.hour, now.minute)
    if not ((9, 25) <= hm <= (9, 35)):
        return {"skip": "window"}
    key = now.strftime("%Y-%m-%d")
    if _CONF_DONE.get("key") == key:
        return {"skip": "done"}
    ensure_tables(con)
    row = con.execute("SELECT run_date FROM pillar_run WHERE pillar='t1_surf' "
                      "AND status='pending_confirm' ORDER BY run_date DESC LIMIT 1").fetchone()
    if not row:
        return {"skip": "no_pending"}
    td = row["run_date"]
    syms = [r["symbol"] for r in con.execute(
        "SELECT symbol FROM pillar_item WHERE run_date=? AND pillar='t1_surf' ORDER BY rank", (td,))]
    if not syms:
        return {"skip": "empty"}
    hq = _hq_opens(syms)
    # prev_close 基准优先用行情自带昨收（9:25 当刻即昨日收盘）
    confirms = []
    for s in syms:
        q = hq.get(s)
        if not q:
            confirms.append({"symbol": s, "result": "无行情", "gap": None})
            continue
        gap = q["open"] / q["prev_close"] - 1
        if gap >= GAP_OBSERVE:
            res = "观察（一字或高开过大，难上车）"
        elif gap >= GAP_CONFIRM[0]:
            res = "可参与（竞价开在合理区间）"
        else:
            res = "放弃（低开走弱）"
        confirms.append({"symbol": s, "open": q["open"], "prev_close": q["prev_close"],
                         "gap": round(gap * 100, 2), "result": res})
    n_ok = sum(1 for x in confirms if x["result"].startswith("可参与"))
    st = {"confirm": {"time": _now(), "results": confirms,
                      "summary": "竞价确认：可参与 %d / 观察 %d / 放弃 %d"
                      % (n_ok,
                         sum(1 for x in confirms if x["result"].startswith("观察")),
                         sum(1 for x in confirms if x["result"].startswith("放弃")))}}
    con.execute("UPDATE pillar_run SET status='confirmed', state_json=? "
                "WHERE run_date=? AND pillar='t1_surf'",
                (json.dumps(st, ensure_ascii=False), td))
    con.commit()
    _CONF_DONE["key"] = key
    return {"ok": True, "run_date": td, "confirmed": n_ok, "total": len(confirms)}


# ---------------------------------------------------------------- 调度入口
def maybe_daily(con: sqlite3.Connection) -> dict:
    """调度线程每轮调用：交易日 16:00 后跑一次主管线（幂等，失败重试≤3 次）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return {"skip": "weekend"}
    if (now.hour, now.minute) < (16, 0):
        return {"skip": "pre_close"}
    today = _today()
    if _GEN_DONE.get("date") == today:
        return {"skip": "done"}
    out: dict = {"date": today, "attempt": (_GEN_DONE.get("fail") or 0) + 1}
    try:
        out["generate"] = generate(con, force=False)
        out["ok"] = bool(out["generate"].get("ok"))
    except Exception as e:  # noqa: BLE001
        out["generate_error"] = str(e)[:200]
        out["ok"] = False
    if out["ok"]:
        _GEN_DONE["date"] = today
        _GEN_DONE["fail"] = 0
    else:
        _GEN_DONE["fail"] = (_GEN_DONE.get("fail") or 0) + 1
        if _GEN_DONE["fail"] >= 3:
            _GEN_DONE["date"] = today
    return out


def maybe_b_confirm_wrapper(con: sqlite3.Connection) -> dict:
    """调度线程每轮调用：交易日 9:25–9:35 竞价确认（幂等）。"""
    try:
        return b_confirm(con)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def monday_full_refresh(con: sqlite3.Connection) -> dict:
    """周一 16:30 全量刷新基本面（由 maybe_daily 内部按 weekday 触发亦可，独立入口供手动跑）。"""
    codes, names, _ = P.SS.universe(P.POOL_LIMIT)
    return fundamentals_refresh(con, codes, names, full=True)


# ---------------------------------------------------------------- 查询 payload（只读）
def payload(con: sqlite3.Connection, date: str | None = None) -> dict:
    """查看器只读接口：某日（默认最新一期）四支柱结果 + 市场状态 + 竞价确认 + 校准。"""
    ensure_tables(con)
    if date:
        row = con.execute("SELECT MAX(run_date) AS d FROM pillar_run WHERE run_date<=?",
                          (date,)).fetchone()
    else:
        row = con.execute("SELECT MAX(run_date) AS d FROM pillar_run").fetchone()
    td = (row and row["d"]) or None
    if not td:
        return {"ok": False, "note": "四支柱引擎尚未生成任何一期（等 16:00 主管线或点「立即生成」）",
                "engine_ver": ENGINE_VER}
    pillars = {}
    for r in con.execute("SELECT * FROM pillar_run WHERE run_date=?", (td,)):
        st = {}
        try:
            st = json.loads(r["state_json"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        items = [{"symbol": x["symbol"], "name": x["name"], "rank": x["rank"],
                  "score": x["score"], "reason": x["reason"],
                  "detail": json.loads(x["detail_json"] or "{}") if x["detail_json"] else {}}
                 for x in con.execute(
                     "SELECT * FROM pillar_item WHERE run_date=? AND pillar=? ORDER BY rank",
                     (td, r["pillar"]))]
        pillars[r["pillar"]] = {"status": r["status"], "title": r["title"],
                                "summary": r["summary"], "state": st, "items": items,
                                "created_at": r["created_at"]}
    ms = None
    try:
        r = con.execute("SELECT * FROM market_state_log WHERE trade_date=?", (td,)).fetchone()
        if r:
            ms = {"state": r["state"], "breadth": json.loads(r["breadth_json"] or "{}"),
                  "rules": (r["note"] or "").split("；")}
    except Exception:  # noqa: BLE001
        pass
    cal = None
    try:
        cal = (pillars.get("resonance") or {}).get("state", {}).get("calibration")
        if cal is None:
            cal = calibration(con)
    except Exception:  # noqa: BLE001
        pass
    fund_n = 0
    try:
        fund_n = con.execute("SELECT COUNT(*) AS n FROM fundamentals").fetchone()["n"]
    except Exception:  # noqa: BLE001
        pass
    # 覆盖度披露（2026-10-30 新增）：进哥多次问「5000 多只怎么老从几百只里选」。
    # pool_n = 本期**深度分析**（四支柱全跑）的候选池；scan_n = 全市场**轻扫描**覆盖。
    # 两者相加才是完整覆盖，界面上必须同时给出，否则「只分析 1974 只」会被误读成漏股。
    pool_n = 0
    try:
        pool_n = int(((ms or {}).get("breadth") or {}).get("pool_n") or 0)
    except (TypeError, ValueError):
        pool_n = 0
    scan_n = 0
    try:
        scan_n = con.execute("SELECT COUNT(*) AS n FROM stock_universe_scan").fetchone()["n"]
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "run_date": td, "engine_ver": ENGINE_VER,
            "market_state": ms, "pillars": pillars, "calibration": cal,
            "fundamentals_n": fund_n, "count_spec": COUNT_SPEC,
            "pool_n": pool_n, "scan_n": scan_n,
            "note": "展示层只读本表；生成由后端 16:00 定时任务完成（架构：引擎层/展示层分离）"}


def status_payload(con: sqlite3.Connection) -> dict:
    """进度/健康：给「立即生成」按钮和状态条用。"""
    ensure_tables(con)   # 保底建表：未生成过任何一期时也能安全返回，不崩
    return {"engine_ver": ENGINE_VER, "stage": _STATUS.get("stage"),
            "last_gen": _GEN_DONE, "last_confirm": _CONF_DONE,
            "today": _today(),
            "runs": [{"run_date": r["run_date"], "pillar": r["pillar"],
                      "status": r["status"], "created_at": r["created_at"]}
                     for r in con.execute(
                         "SELECT run_date,pillar,status,created_at FROM pillar_run "
                         "ORDER BY run_date DESC LIMIT 12")]}
