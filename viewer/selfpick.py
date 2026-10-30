# -*- coding: utf-8 -*-
"""selfpick.py —— 「自我提升」的成果出口：**系统自己的每日荐股**。

【为什么会有这个模块（进哥 2026-10-15 原话）】
「你学习完之后你得有有成果呀」——第 4 支柱原先只展示"学过什么"（权重怎么变的、
长出哪些规则、内化了几条知识），却没有一个"用学到的东西主动选股"的出口。本模块就是它：
每天由系统自己选出约 20 只票，给出短期方向预判，并按真实走势回填结算。

【两张榜（这是本模块的核心设计）】
· 主榜「系统自选」：综合分 Top N —— 按学出的权重（当期市场环境取分档权重）＋自生长规则加成排序。
· 附榜「系统自己发现」：**博主声量为 0**、纯靠战法命中与规则选出来的票。
  为什么必须单独给它一张榜（2026-10-15 第一版实测踩到）：博主维度在前者里天然占优，
  「博主 0 声量」的票在综合分上必然吃亏 —— 实测按综合分排序时，20 只里一只都挤不进来
  （候选池里这类票其实有 589 只）。而它们恰恰是"不模仿、长出自己的东西"最直接的证据，
  看不见就等于没做。故独立成榜，各占各的位置。

【与「个股共振」的区别】
· 个股共振（支柱 A）：**融合展示**"多路信号指向同一只票" —— 本质是**别人怎么说**的汇总。
· 系统自选（本模块）：**按学出的权重 + 自生长规则主动排序** —— 是**自己的判断**。

【三条设计红线】
1. **不重复造数据**：候选池读 `res_stock_card`（六路信号聚合结果），零额外抓取。
2. **必须可验证**：当期推荐按 T+1 / T+5 回填；另用历史信号**回算过去若干期**并结算，
   让「近 N 期胜率/超额」立刻是可核对的数字 —— 否则面板上永远是"待结算"，等于无法证明有没有用。
3. **宁缺勿假**：环境权重没学出方向、样本不足、规则没命中，一律如实标注。

数据来源：res_stock_card / sig_unified（历史回算）/ evo_weight / evo_rule /
market_state_log / 本地 K 线。纯离线，零网络。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta

import evolve as E            # 复用桶函数（规则条件比对口径必须与挖掘时一致）
import resonance as R         # 复用 K 线 / 指数 / 基准 / 权重读取

ENGINE_VER = "selfpick-v1.0"

PICK_N = 20                   # 主榜条数（进哥要求「20 只左右」）
OWN_N = 10                    # 「系统自己发现」榜条数
HORIZON = "T+5"               # 预判周期（短期）
CARD_WINDOW_DAYS = 10         # 候选窗口（自然日）
HIST_PERIODS = 12             # 历史回算期数（让战绩立刻可见）
HIST_LOOKBACK = 30            # 历史候选池回看自然日
RULE_BONUS_MAX = 12.0         # 规则加成上限（规则是小样本结论，只该在同分区间里排序）
OWN_MIN_STRAT = 2             # 「自己的东西」判定：博主声量 0 且战法命中 ≥ 此数

DDL = """
CREATE TABLE IF NOT EXISTS self_pick (
    pick_date     TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'main',
    rank          INTEGER,
    code          TEXT NOT NULL,
    name          TEXT,
    score         REAL,
    tier          TEXT,
    direction     TEXT,
    confidence    REAL,
    horizon       TEXT,
    base_score    REAL,
    rule_bonus    REAL,
    rule_hits     TEXT,
    n_blogger     INTEGER,
    blogger_w     REAL,
    bloggers      TEXT,
    n_strat       INTEGER,
    strat_keys    TEXT,
    strat_families TEXT,
    senti_net     REAL,
    senti_n       INTEGER,
    engine_cross  TEXT,
    mkt_state     TEXT,
    w_json        TEXT,
    dims_json     TEXT,
    is_own        INTEGER,
    is_hist       INTEGER DEFAULT 0,
    evidence_json TEXT,
    verdict       TEXT,
    fill_status   TEXT DEFAULT 'pending',
    ret_t1        REAL, ret_t5  REAL,
    ex_t1         REAL, ex_t5   REAL,
    built_at      TEXT, updated_at TEXT,
    PRIMARY KEY (pick_date, kind, code)
);
CREATE INDEX IF NOT EXISTS ix_self_pick_date ON self_pick(pick_date);
CREATE INDEX IF NOT EXISTS ix_self_pick_fill ON self_pick(fill_status);
CREATE INDEX IF NOT EXISTS ix_self_pick_kind ON self_pick(kind);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    cols = [c[1] for c in con.execute("PRAGMA table_info(self_pick)")]
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'main'"), ("is_hist", "INTEGER DEFAULT 0")):
        if col not in cols:
            try:
                con.execute("ALTER TABLE self_pick ADD COLUMN %s %s" % (col, ddl))
            except Exception as e:  # noqa: BLE001 —— P2-02：精确吞「列已存在」，其余异常照常抛出
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass
                else:
                    raise
    con.commit()


# ================================================================ 权重 / 环境 / 规则
def _env_weight(con: sqlite3.Connection) -> tuple:
    """取当期市场环境对应的权重（学出优先），并**如实标注"是否真的学出了方向"**。

    ⚠️ 2026-10-15 实测两件事（都会影响推荐质量，故写在这里）：
      ① "普跌"环境 8629 个样本仍然**学不出方向**（权重与默认完全相同）；
      ② 这种情况**退回"默认权重"是有害的** —— 默认里博主占 34%，而全局学出的结论是
         「博主声量的独立预测力很弱、权重该压到 13.6%」。既然该环境学不出，
         就该用**全局学出的那套**（有信息），而不是拍脑袋的默认值。
    """
    base = R._load_weights(con)
    mkt = ""
    try:
        r = con.execute("SELECT state FROM market_state_log "
                        "ORDER BY trade_date DESC LIMIT 1").fetchone()
        mkt = (r["state"] or "") if r else ""
    except Exception:  # noqa: BLE001
        mkt = ""

    by_state = {}
    try:
        row = con.execute("SELECT by_state_json FROM evo_weight "
                          "WHERE fit_date <= ? ORDER BY fit_date DESC LIMIT 1",
                          (R._latest_trade_date(),)).fetchone()
        if row and row["by_state_json"]:
            by_state = json.loads(row["by_state_json"])
    except Exception:  # noqa: BLE001
        by_state = {}

    w = {"blogger": base["blogger"], "strategy": base["strategy"],
         "senti": base["senti"], "engine": base["engine"]}
    src = "学出（全局）" if base["src"] == "learned" else "默认"
    if mkt and isinstance(by_state.get(mkt), dict):
        bw = by_state[mkt].get("w") or {}
        if all(k in bw for k in ("blogger", "strategy", "senti", "engine")):
            w = {k: float(bw[k]) for k in ("blogger", "strategy", "senti", "engine")}
            same = all(abs(w[k] - E.DEFAULT_W[k]) < 1e-6 for k in E.DIMS)
            if same:
                # 该环境没学出方向 → 用全局学出的（有信息），别退回默认
                if base["src"] == "learned":
                    w = {k: base[k] for k in ("blogger", "strategy", "senti", "engine")}
                    src = "学出（全局；%s 环境样本未能学出方向，未采用默认）" % mkt
                else:
                    src = "默认（%s 环境样本未能学出方向，且全局也无学出权重）" % mkt
            else:
                src = "学出（环境=%s）" % mkt
    # 全局权重与 by_state 权重必须在同一出口收口：历史环境权重也可能带 blogger。
    # _drop_blogger 只在 blogger>0 时重归一化，因此再显式归一化其余三维，兼容
    # blogger 已为 0 但三维合计不为 1 的历史快照。
    w = R._drop_blogger({k: float(w.get(k) or 0.0)
                         for k in ("blogger", "strategy", "senti", "engine")})
    total = sum(max(0.0, float(w.get(k) or 0.0)) for k in ("strategy", "senti", "engine"))
    if total <= 0:
        w = R._drop_blogger(dict(E.DEFAULT_W))
        total = sum(max(0.0, float(w.get(k) or 0.0)) for k in ("strategy", "senti", "engine"))
    for k in ("strategy", "senti", "engine"):
        w[k] = max(0.0, float(w.get(k) or 0.0)) / total
    w["blogger"] = 0.0
    snap = {"w": w, "src": src, "mkt": mkt, "fit_date": base.get("fit_date", "")}
    return w, mkt, src, json.dumps(snap, ensure_ascii=False)


def _adopted_rules(con: sqlite3.Connection) -> list:
    """自生长规则：**只认已采纳的**（候选规则不加分）。"""
    out = []
    try:
        for r in con.execute("SELECT rid,cond_json,cond_text,n,avg_ex,lift,status,oos_avg_ex "
                             "FROM evo_rule WHERE status='已采纳'"):
            try:
                cond = json.loads(r["cond_json"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            out.append({"rid": r["rid"], "cond": cond, "text": r["cond_text"] or "",
                        "n": r["n"], "avg_ex": r["avg_ex"], "lift": r["lift"],
                        "oos": r["oos_avg_ex"]})
    except Exception:  # noqa: BLE001
        out = []
    return out


def _rule_bonus(rec: dict, mkt: str, rules: list) -> tuple:
    """规则命中 → (加成, 命中说明)。加成刻意压到 ≤12 分。

    规则是小样本结论（样本外常翻车），作用是**在同分区间里排序**，不是主导排序。
    这样即便规则失效，推荐质量也不会被带崩 —— 有意的保守设计。
    """
    if not rules:
        return 0.0, []
    bk = {"blogger": E._bucket_blogger(rec.get("n_blogger") or 0),
          "strat": E._bucket_strat(rec.get("n_strat") or 0),
          "senti": E._bucket_senti(rec.get("senti_net") or 0.0),
          "lhb": E._bucket_lhb(rec.get("lhb_bonus") or 0.0), "mkt": mkt}
    hits, bonus = [], 0.0
    for r in rules:
        c = r["cond"]
        ok = True
        for k in ("mkt", "blogger", "strat", "senti", "lhb"):
            cv = c.get(k)
            if cv in (None, "", "任意", "*"):
                continue
            if str(cv) != str(bk.get(k)):
                ok = False
                break
        if ok:
            inc = 4.0 if (r["oos"] is None or (r["oos"] or 0) > 0) else 1.5
            bonus += inc
            hits.append("%s（样本 %s，平均超额 %s%%，样本外 %s%%）"
                        % (r["text"], r["n"], r["avg_ex"],
                           "—" if r["oos"] is None else r["oos"]))
    return min(bonus, RULE_BONUS_MAX), hits


def _pct_norm(arr_sorted, v) -> float:
    """池内百分位（平均排名法）—— 与共振卡口径一致。全池同值 → 50。"""
    n = len(arr_sorted)
    if n == 0 or arr_sorted[0] == arr_sorted[-1]:
        return 50.0
    lo = hi = 0
    for x in arr_sorted:
        if x < v:
            lo += 1
        if x <= v:
            hi += 1
    return round(100.0 * (lo + hi) / (2.0 * n), 1)


# ================================================================ 打分（主榜与历史回算共用）
def _score_pool(cands: list, w: dict, mkt: str, rules: list) -> list:
    """候选池 → 打分排序结果（主榜与历史回算共用同一套口径）。"""
    def _bw(r):
        return r.get("blogger_w") or 0.0

    def _sw(r):
        return float(r.get("n_strat") or 0)

    def _sn(r):
        return r.get("senti_net") or 0.0

    def _eg(r):
        return float(len([x for x in str(r.get("engine_cross") or "").split("、") if x]))

    a_bw = sorted(_bw(r) for r in cands)
    a_sw = sorted(_sw(r) for r in cands)
    # 分位数组只能含**该维可用**的候选。原先把 senti_n=0 的 0.0 也放进分母，
    # 会让少数有舆情的票被“无舆情样本”挤到 5/95 的极端分位；等舆情源恢复，
    # 舆情维（学出权重可达 75.9%）会直接改写排序 —— 这正是要消灭的“数据齐全抬分”。
    a_sn = sorted(_sn(r) for r in cands if (r.get("senti_n") or 0))
    a_eg = sorted(_eg(r) for r in cands)

    rows = []
    for r in cands:
        nb = _pct_norm(a_bw, _bw(r))
        ns = _pct_norm(a_sw, _sw(r))
        # senti_n=0 才表示舆情缺失；全池同值得到的 50 是有效中性分，不能剔除。
        # 缺失维度不占权重，其余可用维度重归一化，避免数据齐全的股票被天然抬分。
        nu = _pct_norm(a_sn, _sn(r)) if (r.get("senti_n") or 0) else None
        ne = _pct_norm(a_eg, _eg(r))
        dims = {"blogger": nb, "strategy": ns, "senti": nu, "engine": ne}
        retired = {"blogger"}
        available = {"blogger": False, "strategy": True,
                     "senti": bool(r.get("senti_n") or 0), "engine": True}
        # coverage 分母只含原始四维中的未退役维；blogger 永久退役，不计入缺失或覆盖。
        original_total = sum(max(0.0, float(w.get(k) or 0.0))
                             for k in dims if k not in retired)
        available_total = sum(max(0.0, float(w.get(k) or 0.0))
                              for k in dims if k not in retired and available[k])
        coverage = available_total / original_total if original_total > 0 else 0.0
        used_w = {k: (max(0.0, float(w.get(k) or 0.0)) / available_total
                      if k not in retired and available[k] and available_total > 0 else 0.0)
                  for k in dims}
        base = sum(used_w[k] * dims[k] for k in dims
                   if k not in retired and available[k] and used_w[k] > 0)
        rb, hits = _rule_bonus(r, mkt, rules)
        score = max(0.0, min(100.0, base + rb))
        own = 1 if ((r.get("n_blogger") or 0) == 0
                    and (r.get("n_strat") or 0) >= OWN_MIN_STRAT) else 0
        rows.append({"r": r, "code": r.get("code"), "name": r.get("name") or "",
                     "base": round(base, 1), "bonus": round(rb, 2), "hits": hits,
                     "score": round(score, 1), "nb": nb, "ns": ns, "nu": nu, "ne": ne,
                     "coverage": round(coverage, 4), "used_w": used_w, "own": own})
    rows.sort(key=lambda x: (-x["score"], -len(x["hits"]), -x["own"],
                             -(x["r"].get("n_strat") or 0), str(x["code"])))
    return rows


def _dir_conf(sc: float) -> tuple:
    """分数 → 短期方向 + 置信度（规则推断，非预测承诺）。"""
    if sc >= 70:
        return "偏多", round(min(95.0, 50 + (sc - 70) * 1.4), 1)
    if sc >= 58:
        return "中性偏多", round(50 + (sc - 58) * 0.9, 1)
    if sc >= 45:
        return "中性", round(45 + (sc - 45) * 0.4, 1)
    return "偏空", round(max(20.0, 45 - (45 - sc)), 1)


def _tier(sc: float) -> str:
    return "A" if sc >= 68 else ("B" if sc >= 58 else ("C" if sc >= 48 else "D"))


def _evidence(x: dict, w: dict, is_hist: bool) -> list:
    r = x["r"]
    ev = []
    if x["hits"]:
        ev.append("【自生长规则】命中 %d 条：%s" % (len(x["hits"]), "；".join(x["hits"])[:300]))
    if x["own"]:
        ev.append("【系统自己发现的】博主声量 0 —— 不是跟着谁买，是「环境 × 战法 × 规则」自己筛出来的")
    if r.get("n_strat"):
        ev.append("【战法】命中 %d 个：%s（%s）" % (r["n_strat"], str(r.get("strat_keys") or "")[:120],
                                                  r.get("strat_families") or "—"))
    if r.get("n_blogger"):
        ev.append("【博主·参考】%s" % (r.get("bloggers") or ""))
    if r.get("engine_cross"):
        ev.append("【主引擎】%s" % (r.get("engine_cross") or ""))
    uw = x.get("used_w") or w
    senti_text = "%.1f" % x["nu"] if x["nu"] is not None else "缺失"
    ev.append("【打分】可用维度加权 %.1f（博主 %.1f×%.0f%% + 战法 %.1f×%.0f%% + 舆情 %s×%.0f%% "
              "+ 主引擎 %.1f×%.0f%%）＋ 规则加成 %.2f = %.1f｜coverage %.1f%%"
              "（可用权重/原始四维中未退役维权重；缺失维度不参与，可用维度已重归一化）"
              % (x["base"], x["nb"], uw["blogger"] * 100, x["ns"], uw["strategy"] * 100,
                 senti_text, uw["senti"] * 100, x["ne"], uw["engine"] * 100,
                 x["bonus"], x["score"], x.get("coverage", 0.0) * 100))
    if is_hist:
        ev.append("【口径】本行为历史回算：用当前学出的权重复盘当日候选池，"
                  "衡量「这套判断放到过去会选出什么」，非当日实时判断")
    return ev


def _write_rows(con, pick_date: str, kind: str, rows: list, w: dict, wsnap: str,
                mkt: str, is_hist: bool) -> int:
    n = 0
    for i, x in enumerate(rows, 1):
        r, sc = x["r"], x["score"]
        if is_hist:
            # 主键是 (pick_date,kind,code)，不含 is_hist；同日实盘与回算撞票时，
            # INSERT OR REPLACE 会把实盘行顶掉。历史回算只允许覆盖历史行。
            old = con.execute("SELECT is_hist FROM self_pick WHERE pick_date=? AND kind=? AND code=?",
                              (pick_date, kind, x["code"])).fetchone()
            if old is not None and not old["is_hist"]:
                continue
        direction, conf = _dir_conf(sc)
        verdict = ("系统自评 %.1f 分（%s 档）｜短期%s（置信 %.0f）｜%s%s"
                   % (sc, _tier(sc), direction, conf,
                      "规则命中 %d 条" % len(x["hits"]) if x["hits"] else "无规则命中",
                      "｜★系统自己发现" if x["own"] else ""))
        con.execute(
            "INSERT OR REPLACE INTO self_pick(pick_date,kind,rank,code,name,score,tier,direction,"
            "confidence,horizon,base_score,rule_bonus,rule_hits,n_blogger,blogger_w,bloggers,"
            "n_strat,strat_keys,strat_families,senti_net,senti_n,engine_cross,mkt_state,w_json,"
            "dims_json,is_own,is_hist,evidence_json,verdict,built_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pick_date, kind, i, x["code"], x["name"], sc, _tier(sc), direction, conf, HORIZON,
             x["base"], x["bonus"], "；".join(x["hits"])[:600], r.get("n_blogger") or 0,
             round(r.get("blogger_w") or 0.0, 1), r.get("bloggers") or "",
             r.get("n_strat") or 0, r.get("strat_keys") or "",
             r.get("strat_families") or "", round(r.get("senti_net") or 0.0, 2),
             r.get("senti_n") or 0, r.get("engine_cross") or "", mkt, wsnap,
             json.dumps({"blogger": x["nb"], "strategy": x["ns"], "senti": x["nu"],
                         "engine": x["ne"], "coverage": x["coverage"]}, ensure_ascii=False),
             x["own"], 1 if is_hist else 0,
             json.dumps(_evidence(x, w, is_hist)[:8], ensure_ascii=False),
             verdict, _now(), _now()))
        n += 1
    return n


# ================================================================ 候选池
def _pool_from_cards(con: sqlite3.Connection, pick_date: str) -> list:
    """当期候选池：读共振卡（六路信号聚合结果，零额外抓取）。"""
    lo = (datetime.strptime(pick_date, "%Y-%m-%d")
          - timedelta(days=CARD_WINDOW_DAYS)).strftime("%Y-%m-%d")
    rows = con.execute("SELECT * FROM res_stock_card WHERE build_date>=? AND build_date<=?",
                       (lo, pick_date)).fetchall()
    if not rows:
        mx = con.execute("SELECT MAX(build_date) m FROM res_stock_card").fetchone()
        if mx and mx["m"]:
            rows = con.execute("SELECT * FROM res_stock_card WHERE build_date=?",
                               (mx["m"],)).fetchall()
    return [{"code": r["code"], "name": r["name"] or "",
             "n_blogger": r["n_blogger"], "blogger_w": r["blogger_w"],
             "bloggers": r["bloggers"], "n_strat": r["n_strat"],
             "strat_keys": r["strat_keys"], "strat_families": r["strat_families"],
             "senti_net": r["senti_net"], "senti_n": r["senti_n"],
             "engine_cross": r["engine_cross"], "lhb_bonus": r["lhb_bonus"] or 0.0}
            for r in rows]


def _pool_from_sig(con: sqlite3.Connection, asof: str) -> list:
    """历史回算候选池：用 sig_unified「截至 asof」的信号重建（口径对齐共振卡）。"""
    lo = (datetime.strptime(asof, "%Y-%m-%d")
          - timedelta(days=HIST_LOOKBACK)).strftime("%Y-%m-%d")
    rows = con.execute("SELECT * FROM sig_unified WHERE ts>=? AND ts<=?", (lo, asof)).fetchall()
    pool: dict = {}
    for r in rows:
        code = r["code"]
        if not code or len(str(code)) != 6:
            continue
        s = pool.setdefault(code, {"code": code, "name": r["name"] or "",
                                   "bw": {}, "strat": [], "senti": 0.0, "senti_n": 0,
                                   "eng": [], "lhb_b": 0.0})
        st = r["src_type"]
        if st == "blogger":
            s["bw"][r["src_key"]] = max(s["bw"].get(r["src_key"], 0), r["strength"] or 0)
        elif st == "strategy":
            for k in str(r["evidence"] or "").split("：")[-1].split("、"):
                k = k.strip()
                if k and k not in s["strat"]:
                    s["strat"].append(k)
        elif st == "lhb":
            s["lhb_b"] += 1.0 if r["direction"] == "多" else (-1.0 if r["direction"] == "空" else 0.0)
        elif st == "sentiment":
            s["senti"] += (r["raw_score"] or 50) - 50
            s["senti_n"] += 1
        elif st == "engine":
            if r["src_key"] not in s["eng"]:
                s["eng"].append(r["src_key"])
    out = []
    for code, s in pool.items():
        if not s["bw"] and not s["strat"] and not s["eng"]:
            continue
        out.append({"code": code, "name": s["name"], "n_blogger": len(s["bw"]),
                    "blogger_w": sum(s["bw"].values()),
                    "bloggers": "、".join(sorted(s["bw"])), "n_strat": len(s["strat"]),
                    "strat_keys": "、".join(s["strat"]), "strat_families": "",
                    "senti_net": round(s["senti"], 2), "senti_n": s["senti_n"],
                    "engine_cross": "、".join(sorted(s["eng"])), "lhb_bonus": s["lhb_b"]})
    return out


# ================================================================ 主流程
def build(con: sqlite3.Connection, bars: dict | None = None,
          n_pick: int = PICK_N, n_own: int = OWN_N) -> dict:
    """生成**当期**两张榜（系统自选 / 系统自己发现），并回填往期。"""
    ensure_tables(con)
    t0 = time.time()
    bars = bars if bars is not None else R.load_bars()
    R._set_bars(bars)
    pick_date = R._latest_trade_date()
    w, mkt, wsrc, wsnap = _env_weight(con)
    rules = _adopted_rules(con)

    cands = _pool_from_cards(con, pick_date)
    if not cands:
        return {"ok": False, "error": "无候选（res_stock_card 为空，请先跑融合引擎）"}

    rows = _score_pool(cands, w, mkt, rules)
    main = rows[:n_pick]
    own_all = [x for x in rows if x["own"]]
    own = own_all[:n_own]

    con.execute("DELETE FROM self_pick WHERE pick_date=? AND is_hist=0", (pick_date,))
    n1 = _write_rows(con, pick_date, "main", main, w, wsnap, mkt, False)
    n2 = _write_rows(con, pick_date, "own", own, w, wsnap, mkt, False)
    con.commit()

    filled = _fill(con, bars)
    return {"ok": True, "engine_ver": ENGINE_VER, "pick_date": pick_date,
            "main": n1, "own": n2, "own_pool_total": len(own_all),
            "mkt_state": mkt, "weight_src": wsrc, "rules_adopted": len(rules),
            "rule_hit_rows": sum(1 for x in main + own if x["hits"]),
            "filled": filled, "elapsed": round(time.time() - t0, 2)}


def build_history(con: sqlite3.Connection, bars: dict | None = None,
                  periods: int = HIST_PERIODS, n_pick: int = PICK_N,
                  n_own: int = OWN_N) -> dict:
    """用历史信号**回算过去 N 期**并结算 —— 让「成果」立刻可验证。

    为什么必须做：当期推荐要等 T+5 才能结算；只看当期，面板上永远是"待结算"，
    等于无法证明这套自学习有没有用。

    ⚠️ 口径（会写进 UI）：历史期次用**当前学出的权重**近似复盘（历史各期权重没留存），
    衡量的是「用今天这套判断去复盘过去」，不等于当日实时判断。
    """
    ensure_tables(con)
    t0 = time.time()
    bars = bars if bars is not None else R.load_bars()
    R._set_bars(bars)
    if not bars:
        return {"ok": False, "error": "本地 K 线缓存为空"}
    ref = max(bars.values(), key=len)
    dates = [r[0] for r in ref]
    if len(dates) < periods + 6:
        return {"ok": False, "error": "交易日不足"}
    usable = dates[-(periods + 5):-5]      # 留出 T+5 结算窗口
    w, mkt, wsrc, wsnap = _env_weight(con)
    rules = _adopted_rules(con)
    mkt_by_day = {}
    try:
        for r in con.execute("SELECT trade_date, state FROM market_state_log"):
            mkt_by_day[r["trade_date"]] = r["state"] or ""
    except Exception:  # noqa: BLE001
        pass

    n_p = n_o = 0
    for d in usable:
        if not d:
            continue
        md = mkt_by_day.get(d, mkt)
        cands = _pool_from_sig(con, d)
        if len(cands) < 20:
            continue
        rows = _score_pool(cands, w, md, rules)
        con.execute("DELETE FROM self_pick WHERE pick_date=? AND is_hist=1", (d,))
        n_p += _write_rows(con, d, "main", rows[:n_pick], w, wsnap, md, True)
        n_o += _write_rows(con, d, "own", [x for x in rows if x["own"]][:n_own], w, wsnap, md, True)
    con.commit()
    filled = _fill(con, bars)
    return {"ok": True, "periods": len(usable), "rows": n_p + n_o,
            "filled": filled, "elapsed": round(time.time() - t0, 2)}


def _fill(con: sqlite3.Connection, bars: dict) -> int:
    """按真实走势回填 T+1 / T+5 涨跌与超额 —— 「成果」必须可核对。"""
    try:
        idx, bench = R.build_indices(bars)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for r in con.execute("SELECT pick_date,kind,code FROM self_pick").fetchall():
        code, d0 = r["code"], r["pick_date"]
        rows = bars.get(code) or []
        m = idx.get(code) or {}
        if d0 not in m:
            continue
        i0 = m[d0]
        base = rows[i0][1]
        if not base:
            continue
        upd = {}
        for w_, kr, ke in ((1, "ret_t1", "ex_t1"), (5, "ret_t5", "ex_t5")):
            j = i0 + w_
            if j < len(rows):
                px, bd = rows[j][1], rows[j][0]
                if px and bd in bench and bench[bd] is not None:
                    ret = round((px / base - 1.0) * 100.0, 2)
                    upd[kr] = ret
                    # 修复 P1-19：超额收益要用「同期累计市场中位涨幅」做基准，而非「第 w 日单日中位」。
                    # bench[d] 是单日中位涨幅（由 c/prev-1 聚合），逐日累乘成累计后再相减。
                    bench_cum = 1.0
                    for di in range(i0 + 1, j + 1):
                        bdi = rows[di][0]
                        if bdi in bench and bench[bdi] is not None:
                            bench_cum *= (1.0 + bench[bdi] / 100.0)
                    bench_cum -= 1.0
                    upd[ke] = round(ret - bench_cum * 100.0, 2)
        if not upd:
            continue
        sets = ", ".join("%s=?" % k for k in upd)
        status = "ok" if "ret_t5" in upd else "pending"
        con.execute("UPDATE self_pick SET %s, fill_status=?, updated_at=? "
                    "WHERE pick_date=? AND kind=? AND code=?" % sets,
                    tuple(upd.values()) + (status, _now(), d0, r["kind"], code))
        if status == "ok":
            n += 1
    con.commit()
    return n


# ================================================================ 输出
_WIN5_SQL = ("AVG(CASE WHEN fill_status='ok' "
             "THEN (CASE WHEN ret_t5>0 THEN 1.0 ELSE 0.0 END) END) win5")
_WINEX5_SQL = ("AVG(CASE WHEN fill_status='ok' "
               "THEN (CASE WHEN ex_t5>0 THEN 1.0 ELSE 0.0 END) END) win_ex5")
# ⚠️ 2026-10-15 修（真 bug）：这两处原来写的是
#   AVG(CASE WHEN fill_status='ok' AND ret_t5>0 THEN 1.0 END)
# —— 不满足条件的行求值为 NULL，被 AVG **直接忽略**，等于"只对赢的那几只求平均"，
# 结果恒为 1.0（显示 100% 胜率），而同一期平均收益明明是负的。
# 必须显式给出 ELSE 0.0，AVG 才有分母。这个坑很隐蔽（SQL 不报错、数还很好看）。


def _tier_stats(con: sqlite3.Connection) -> list:
    """分档实盘成绩：**按交易日聚合**（不是按票），并给出 t 值。

    为什么必须按日聚合：同一天会推 15~20 只票，它们的 T+5 窗口完全重叠，
    按票求均值会把有效样本量放大 20 倍，t 值虚高。（本项目在 hypo 里踩过同一个坑。）

    为什么必须有 t 值：2026-10-30 归因时看到「A 档 T+5 超额 −3.00%」，
    看着像 A 档是反向指标；实际它只分布在 6 个交易日、且这 6 天市场急跌，
    按日 t=−2.05 已是边界。**没有天数和 t 的均值不能拿来改算法方向。**
    """
    import math
    import statistics
    out = []
    # 2026-10-30：新旧算法不能混算。154 条新回算行带 coverage，220 条旧回算行没有；
    # 实盘里 10-30 之前的行同样是旧算法。凡是带 coverage 的才是新口径，其余一律不进统计
    # （旧行保留在表里可追溯，只是不参与“分档实测”的结论）。
    _scope = "dims_json LIKE '%coverage%'"
    for tr in con.execute("SELECT DISTINCT tier FROM self_pick WHERE kind='main' "
                          "AND tier IS NOT NULL AND " + _scope + " ORDER BY tier"):
        tier = tr["tier"]
        rows = con.execute(
            "SELECT pick_date, AVG(ex_t5) e, AVG(ret_t5) r FROM self_pick "
            "WHERE kind='main' AND tier=? AND fill_status='ok' AND ex_t5 IS NOT NULL "
            "AND " + _scope + " "
            "GROUP BY pick_date ORDER BY pick_date", (tier,)).fetchall()
        days = [x["e"] for x in rows if x["e"] is not None]
        n = con.execute("SELECT COUNT(*) c FROM self_pick WHERE kind='main' AND tier=? "
                        "AND fill_status='ok' AND ex_t5 IS NOT NULL AND " + _scope,
                        (tier,)).fetchone()["c"]
        if not days:
            out.append({"tier": tier, "n": n, "days": 0, "avg_ex5": None,
                        "t": None, "win": None})
            continue
        m = statistics.fmean(days)
        sd = statistics.stdev(days) if len(days) > 1 else 0.0
        t = (m / (sd / math.sqrt(len(days)))) if sd else None
        wins = [1.0 if d > 0 else 0.0 for d in days]
        out.append({"tier": tier, "n": n, "days": len(days),
                    "avg_ex5": round(m, 2),
                    "t": None if t is None else round(t, 2),
                    "win": round(100.0 * statistics.fmean(wins), 1)})
    return out


def payload(con: sqlite3.Connection, limit: int = 30) -> dict:
    ensure_tables(con)
    mx = con.execute("SELECT MAX(pick_date) m FROM self_pick WHERE is_hist=0").fetchone()
    cur = (mx["m"] if mx else "") or ""
    if not cur:
        mx = con.execute("SELECT MAX(pick_date) m FROM self_pick").fetchone()
        cur = (mx["m"] if mx else "") or ""

    def _rows(kind, n):
        out = []
        if not cur:
            return out
        for r in con.execute("SELECT * FROM self_pick WHERE pick_date=? AND kind=? "
                             "ORDER BY rank LIMIT ?", (cur, kind, n)):
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            d["dims"] = json.loads(d.pop("dims_json") or "{}")
            d["w"] = json.loads(d.pop("w_json") or "{}")
            out.append(d)
        return out

    picks, own_picks = _rows("main", limit), _rows("own", limit)

    def _stat(where: str) -> dict:
        row = con.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN fill_status='ok' THEN 1 ELSE 0 END) settled, "
            "AVG(CASE WHEN fill_status='ok' THEN ret_t5 END) avg_ret5, "
            "AVG(CASE WHEN fill_status='ok' THEN ex_t5 END) avg_ex5, "
            + _WIN5_SQL + ", " + _WINEX5_SQL + " "
            "FROM self_pick " + where).fetchone()
        return {"total": row["n"] or 0, "settled": row["settled"] or 0,
                "avg_ret5": None if row["avg_ret5"] is None else round(row["avg_ret5"], 2),
                "avg_ex5": None if row["avg_ex5"] is None else round(row["avg_ex5"], 2),
                "win5": None if row["win5"] is None else round(row["win5"], 3),
                "win_ex5": None if row["win_ex5"] is None else round(row["win_ex5"], 3)}

    hist = []
    for r in con.execute(
            "SELECT pick_date, is_hist, COUNT(*) n, "
            "SUM(CASE WHEN fill_status='ok' THEN 1 ELSE 0 END) settled, "
            "AVG(CASE WHEN fill_status='ok' THEN ret_t5 END) avg_ret5, "
            "AVG(CASE WHEN fill_status='ok' THEN ex_t5 END) avg_ex5, "
            + _WIN5_SQL + " "
            "FROM self_pick WHERE kind='main' AND dims_json LIKE '%coverage%' "
            "GROUP BY pick_date, is_hist "
            "ORDER BY pick_date DESC LIMIT 24"):
        hist.append({"date": r["pick_date"], "hist": r["is_hist"], "n": r["n"],
                     "settled": r["settled"],
                     "avg_ret5": None if r["avg_ret5"] is None else round(r["avg_ret5"], 2),
                     "avg_ex5": None if r["avg_ex5"] is None else round(r["avg_ex5"], 2),
                     "win5": None if r["win5"] is None else round(r["win5"], 3)})

    ms = con.execute("SELECT state FROM market_state_log ORDER BY trade_date DESC LIMIT 1").fetchone()
    mkt = (ms["state"] if ms else "") or ""
    wsrc = ""
    if picks:
        try:
            wsrc = (picks[0].get("w") or {}).get("src", "")
        except Exception:  # noqa: BLE001
            wsrc = ""

    return {"ok": True, "engine_ver": ENGINE_VER, "pick_date": cur, "horizon": HORIZON,
            "market_state": mkt, "weight_src": wsrc,
            "picks": picks, "own_picks": own_picks,
            # 统计同分档表：只算新口径（带 coverage）的行，旧行保留可追溯但不进结论。
            "stats": {"main": _stat("WHERE kind='main' AND dims_json LIKE '%coverage%'"),
                      "own": _stat("WHERE kind='own'"),
                      "hist": _stat("WHERE kind='main' AND is_hist=1 "
                                    "AND dims_json LIKE '%coverage%'"),
                      "live": _stat("WHERE kind='main' AND is_hist=0 "
                                    "AND dims_json LIKE '%coverage%'")},
            # 2026-10-30 归因新增：分档实盘成绩（按交易日聚合 + t 值）。
            # 动机是「A 档到底该当买入清单还是回避清单」——见 caliber 第 7 条。
            "tier_stats": _tier_stats(con),
            "history": hist[:14],
            "caliber": [
                "口径1 · 这是**系统自己的推荐**，不是汇总别人的：打分用 evolve 学出的权重"
                "（并按当期市场环境取分档权重），再叠加**自生长规则**加成 —— 权重与规则取值都随学习结果变。",
                "口径2 · 本面板有**两张榜**。主榜「系统自选」按综合分排序；"
                "附榜「系统自己发现」只放**博主声量为 0**、纯靠战法命中与规则选出来的票 —— "
                "后者是「不模仿、长出自己的东西」最直接的证据；其中一部分会凭分数自然进入主榜，单列成榜是为了**保证它们一定被看见**（同一只票出现在两张榜里，是两个不同视角）。",
                "口径3 · 排序权重不写死：由数据学出，每期可能不同。规则加成上限 %d 分，"
                "刻意压小 —— 规则是小样本结论，只该在同分区间里排序，不该主导排序。" % int(RULE_BONUS_MAX),
                "口径4 · 收益为**超额**（对比 %s），T+1 / T+5 口径，以推荐日收盘为基准，"
                "不含费用与滑点；未走完窗口的期次显示「待结算」。" % R.BENCH_LABEL,
                "口径5 · **历史回算期**用当前学出的权重复盘当日候选池，衡量「用今天这套判断"
                "去复盘过去」的效果，**不等于当日实时判断**；实时推荐从今天起逐日累积。",
                "口径6 · 方向（偏多/中性偏多/中性/偏空）与置信度是**规则推断**，不是预测承诺。"
                "系统自选 ≠ 买入建议；它的价值在于「可回溯验证的学习成果」。",
                # 2026-10-30 归因（进哥要求「先做归因再定」）：下面这条是结论，不是免责声明。
                "口径7 · **A 档尚未被证明能当买入清单，也未被证明该回避。**"
                "实盘（2026-09-14 起）到今天，A 档全部集中在 6 个交易日、T+5 按日超额 −3.0%（t=−2.05）；"
                "但同期 C 档是另一段日子（9 月），两者**没有任何一个共同交易日**，所以「A 比 C 差」"
                "这个对比本身不成立。真因在别处：9 月只有博主一维有数据、其余三维全池同值被填成 50，"
                "分数天然压在 C 档；10 月三维齐全后分数整体抬到 90+ 才进 A 档 —— "
                "**A 档目前标记的是「这一天数据齐全」，不是「这一天机会好」。**"
                "评分已改为可用维度重归一化，A 档不再因数据齐全而天然抬分；"
                "在拿到至少 20 个交易日的 A 档样本前，不要把它当买入清单，也不要把它的分数取反当回避清单。",
            ],
            "note": "「自我提升」的成果出口：用学出的权重 + 自生长规则主动选股，并按期回填验证。"}


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    mx = con.execute("SELECT MAX(pick_date) m, COUNT(*) n FROM self_pick").fetchone()
    return {"engine_ver": ENGINE_VER, "latest": (mx["m"] if mx else "") or "",
            "total": (mx["n"] if mx else 0), "today": _today()}


def maybe_build(con: sqlite3.Connection) -> dict:
    """每日一次：当期还没生成就生成（异常隔离，绝不拖垮调度线程）。"""
    try:
        ensure_tables(con)
        d = R._latest_trade_date()
        has = con.execute("SELECT COUNT(*) n FROM self_pick WHERE pick_date=? AND is_hist=0",
                          (d,)).fetchone()["n"]
        if has:
            return {"ok": True, "skipped": "already", "pick_date": d}
        return build(con)
    except Exception as e:  # noqa: BLE001
        logging.exception("selfpick maybe_build 失败（已忽略）")
        return {"ok": False, "error": str(e)[:200]}
