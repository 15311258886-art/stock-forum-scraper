# -*- coding: utf-8 -*-
"""宇宙一致性审计（只读，2026-10-31 决策④；2026-11-01 扩 P0「全 A 股宇宙」）。

## 为什么需要它

「可转债混进 K 线库」这件事被报告提过两次，但真正查下来，**K 线库是干净的**：
111024 在 kline 里只有 2005-2014 的 528 行（那是老代码体系里的另一只债券，
不是 2021 年才上市的澳弘转债），113710 根本不在 kline 里。

脏的是另外几张表，而它们都没人查：

  1. `stock_indicators_daily` —— 111024 有 273 行（2010-01 ~ 2014-09），
     名称却写着「澳弘转债」。同一个代码，一只 2014 年就退市的债券，
     被贴上了一只 2021 年才上市的可转债的名字。**名称与代码错配**。
  2. `a_shares_universe`（号称权威全量宇宙）——
     · 混进 2 只可转债（111024、113710）+ 1 只 ETF（513050），board 记为「其它」；
     · **漏收全部 328 只北交所 920 段**（它是 2023 年起启用的新代码段）。

第 2 条的后果比可转债严重得多：任何信了「这张表是全量」的取码逻辑，
都会静默丢掉整个北交所。

## 2026-11-01 扩展：以 security_master 为口径基准

本轮建了 `security_master`（`tools/build_security_master.py`）作为单一口径源，
审计随之增加 ⑤⑥⑦ 三节，并要求 ①-④ 按新口径分级：

  WARN  —— 本应修好、且本轮有能力修好的缺陷（→ `--strict` 退出 1）
  INFO  —— **数据层面不允许本轮消除**的既存差异（遗留脏行不可删、白名单冻结）。
           每条 INFO 都必须在报告里写清三件事：为什么允许存在、消费方必须怎么过滤、
           以及修复命令。INFO 不计入退出码，但 `--fail-on-info` 可以把它变成硬失败。

把 WARN 降级成 INFO 只有一个正当理由：**这条差异在本轮约束下改不了**。
凡是"改得了却没改"的（比如主表漏收北交所），一律保持 WARN，绝不下调。

## 用法

    python tools/audit_universe.py                # 人读
    python tools/audit_universe.py --json         # 机读（供 CI 引用）
    python tools/audit_universe.py --strict       # 有 WARN 即 exit 1
    python tools/audit_universe.py --fail-on-info # 连 INFO 也算失败（更严）

退出码：0 = PASS 或仅 INFO；1 = --strict 且有 WARN（或 --fail-on-info 且有 INFO）；
        2 = 读库失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import units as U  # noqa: E402
from viewer import security_master as SM  # noqa: E402

DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")

# 「近期活跃」的判定窗口：非股票残留在最近 30 天内还有行，就是**在产线上**的污染
# （会让今天的统计直接算错），必须 WARN；只有旧行才算历史残留、可降级 INFO。
LIVE_WINDOW_DAYS = 30

# 主表最少应有的行数。北交所补齐后全市场约 5,580+；5,500 是"明显漏了整块"的红线。
MIN_MASTER_ROWS = 5500
# 北交所股票下限。真实约 350 只；300 是"整块没丢"的判据。
MIN_MASTER_BSE = 300

KLINE_CANDIDATES = (os.path.join(ROOT, "data", "kline_daily.db"),
                    os.path.join(ROOT, "viewer", "data", "kline_daily.db"))


def _seg(code: str) -> str:
    """代码段前三/前二，用于把漏收清单压成可读的几行。"""
    c = str(code or "")
    for p in ("920", "688", "689", "600", "601", "603", "605",
              "000", "001", "002", "003", "300", "301", "302", "43", "83", "87", "88"):
        if c.startswith(p):
            return p
    return c[:2]


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def _open_kline(path):
    """只读打开 K 线库；不存在/打不开返回 None（审计不因缺 K 线库而失败）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                               uri=True, timeout=30)
    except sqlite3.Error:
        return None


def _days_between(a: str, b: str):
    """两个 ISO 日期相差多少天（b - a）。解析不了返回 None（不猜）。"""
    try:
        da = datetime.strptime(str(a)[:10], "%Y-%m-%d").date()
        db = datetime.strptime(str(b)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (db - da).days


def collect(con: sqlite3.Connection, kline_db=None) -> dict:
    """只读采集，返回一个纯数据 dict（不含副作用）。

    kline_db: 传了路径才做「K 线库 vs 主表」比对（1.6GB 库，CI 里未必存在）。
    """
    findings = []

    def add(level, key, msg, detail=None):
        findings.append({"level": level, "key": key, "msg": msg,
                         "detail": detail or {}})

    # ── 指标库：一次扫描拿齐（代码数 5,337 / 行数 1,300 万，所以只 GROUP BY 一趟）──
    ind = {}
    if _has_table(con, "stock_indicators_daily"):
        sql = ("SELECT code, MAX(date), MIN(date), COUNT(*), "
               "MAX(name), "
               "MAX(date || '|' || COALESCE(name,'')) "
               "FROM stock_indicators_daily GROUP BY code")
        for code, d_max, d_min, n, name_max, latest_key in con.execute(sql):
            latest_name = latest_key.split("|", 1)[1] if latest_key else ""
            ind[code] = {"code": code, "name": name_max or "",
                         "latest_name": latest_name or (name_max or ""),
                         "d_max": d_max, "d_min": d_min, "n": n or 0}
    ind_last_day = max((v["d_max"] for v in ind.values() if v["d_max"]), default=None)

    def is_live(d_max: str) -> bool:
        if not ind_last_day or not d_max:
            return False
        gap = _days_between(d_max, ind_last_day)
        return gap is not None and gap <= LIVE_WINDOW_DAYS

    # ── 白名单 ─────────────────────────────────────────────────────────────────
    uni = {}
    if _has_table(con, "a_shares_universe"):
        for code, name, board, exchange in con.execute(
                "SELECT code, name, board, exchange FROM a_shares_universe"):
            uni[code] = {"code": code, "name": name or "", "board": board or "",
                         "exchange": exchange or ""}

    # ── 快照最新一日 ───────────────────────────────────────────────────────────
    snap = {}
    snap_day = None
    if _has_table(con, "stock_daily_snapshot"):
        row = con.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()
        snap_day = row[0] if row else None
        if snap_day:
            for code, name, board in con.execute(
                    "SELECT code, name, board FROM stock_daily_snapshot WHERE trade_date=?",
                    (snap_day,)):
                snap[code] = {"code": code, "name": name or "", "board": board or ""}

    # ── 名称表 ─────────────────────────────────────────────────────────────────
    name_map = {}
    if _has_table(con, "stock_names"):
        name_map = {c: (n or "") for c, n in
                    con.execute("SELECT code, name FROM stock_names")}

    # ── security_master（本轮新增的口径基准）──────────────────────────────────
    sm = {}
    if _has_table(con, SM.TABLE):
        for code, name, board, kind, is_active in con.execute(
                "SELECT code, name, board, kind, is_active FROM %s" % SM.TABLE):
            sm[code] = {"code": code, "name": name or "", "board": board or "",
                        "kind": kind or "", "is_active": int(is_active or 0)}
    sm_nonstock = {c: r for c, r in sm.items() if r["kind"] != SM.KIND_STOCK}
    sm_stock = {c: r for c, r in sm.items()
                if r["kind"] == SM.KIND_STOCK and r["is_active"]}

    # ── ① 指标库里的非股票标的 ────────────────────────────────────────────────
    ind_nonstock = []
    for c, r in sorted(ind.items()):
        kind = sm[c]["kind"] if c in sm else U.stock_kind(c, r["name"])
        if kind != SM.KIND_STOCK:
            ind_nonstock.append({
                "code": c, "name": r["name"], "kind": kind,
                "d_min": r["d_min"], "d_max": r["d_max"], "rows": r["n"],
                "live": is_live(r["d_max"]),
                "in_master": c in sm,
            })
    live_ind_nonstock = [x for x in ind_nonstock if x["live"]]
    if live_ind_nonstock:
        add("WARN", "ind_live_nonstock",
            "指标库混入非股票 %d 只且**近期仍活跃**（当日统计/长窗口回测会被静默计入）"
            % len(live_ind_nonstock),
            {"codes": [x["code"] for x in live_ind_nonstock]})
    elif ind_nonstock:
        add("INFO", "ind_hist_nonstock",
            "指标库有 %d 只非股票的历史残留行（2010-2014 的债券，本轮不许删行）"
            % len(ind_nonstock),
            {"codes": [x["code"] for x in ind_nonstock],
             "why": "这些行是 2010-2014 年的真实历史（111024 是当年另一只债券），"
                    "删除属于数据清洗决策，不在本轮「只读/只新增」范围内。",
             "filter": "消费方必须用 security_master（kind='stock'）或 "
                       "units.is_tradable_stock() 过滤后再统计，而不是直接 "
                       "GROUP BY stock_indicators_daily。",
             "fix": "待数据清洗工单：DELETE FROM stock_indicators_daily "
                    "WHERE code NOT IN (SELECT code FROM security_master "
                    "WHERE kind='stock')"})

    # ── ② 白名单里的非股票标的 ────────────────────────────────────────────────
    uni_nonstock = []
    for c, r in sorted(uni.items()):
        kind = sm[c]["kind"] if c in sm else U.stock_kind(c, r["name"])
        if kind != SM.KIND_STOCK:
            uni_nonstock.append({"code": c, "name": r["name"], "board": r["board"],
                                 "exchange": r["exchange"], "kind": kind})
    if uni_nonstock:
        add("INFO", "uni_nonstock",
            "白名单混入非股票 %d 只（遗留表，本轮禁止删行）" % len(uni_nonstock),
            {"codes": [x["code"] for x in uni_nonstock],
             "why": "a_shares_universe 是遗留表，本轮约束是「只读/只新增」——"
                    "删行是另一个决策；security_master 已把它们标成 kind != 'stock'。",
             "filter": "消费方一律改读 security_master.stock_codes(conn)；"
                       "仍读白名单的代码必须自己按 units 过滤。",
             "fix": "白名单下线/重建工单（本轮不做）"})

    # ── ③ 白名单漏收（指标库有数据、白名单没有）────────────────────────────────
    missing = {}
    for c, r in ind.items():
        if c in uni:
            continue
        kind = sm[c]["kind"] if c in sm else U.stock_kind(c, r["name"])
        if kind != SM.KIND_STOCK:
            continue  # 非股票不进来，①已单独列
        missing.setdefault(_seg(c), []).append(c)
    missing_summary = [
        {"seg": s, "n": len(v), "sample": sorted(v)[:3]}
        for s, v in sorted(missing.items(), key=lambda kv: -len(kv[1]))
    ]
    missing_total = sum(x["n"] for x in missing_summary)
    missing_codes = [c for v in missing.values() for c in v]
    missing_uncovered = [c for c in missing_codes if c not in sm_stock]
    if missing_total:
        if missing_uncovered:
            # 白名单漏了、主表也没收 —— 这才是真缺陷：整个板块会从宇宙里消失
            add("WARN", "uni_missing_uncovered",
                "白名单漏收 %d 只股票，且 security_master 也没收录（宇宙真缺口）"
                % len(missing_uncovered),
                {"codes": missing_uncovered[:200]})
        else:
            add("INFO", "uni_missing_covered",
                "白名单漏收 %d 只股票（北交所为主），但 security_master 已全部收录"
                % missing_total,
                {"codes": missing_codes[:20],
                 "why": "a_shares_universe 本轮冻结（禁写）；缺口已由 security_master 补齐，"
                        "凡改读主表的模块不再受影响。",
                 "filter": "读 a_shares_universe 的旧代码仍会漏掉这些票 —— 必须改读 "
                           "security_master.stock_codes(conn)。",
                 "fix": "python tools/build_security_master.py --sync-universe"
                        "（只 INSERT 补进白名单，不删不改）"})

    # ── ④ 名称错配（指标库名 vs 名称表名），区分「临时名」与「真错配」──────────
    mismatch = []
    alias = []
    for c, r in ind.items():
        other = name_map.get(c)
        if other is None:
            continue
        a, b = (r["name"] or "").strip(), other.strip()
        if not a or not b:
            continue
        if a == b:
            continue
        if SM.name_alias_matches(a, b) or SM.name_alias_matches(b, a):
            alias.append({"code": c, "indicators_name": a, "names_table": b})
        else:
            mismatch.append({"code": c, "indicators_name": a, "names_table": b})
    mismatch.sort(key=lambda x: x["code"])
    alias.sort(key=lambda x: x["code"])
    if mismatch:
        add("WARN", "name_mismatch",
            "名称错配 %d 处（同一代码两个名字，且不是除权临时名）" % len(mismatch),
            {"rows": mismatch[:50]})
    if alias:
        add("INFO", "name_alias",
            "除权临时名 %d 处（XD/XR/DR 前缀，去掉前缀可匹配到主名，可接受）"
            % len(alias),
            {"rows": alias,
             "why": "交易所除权除息日把简称改成 XD+截短名（「浙商证券」→「XD浙商证」），"
                    "次日恢复；这是显示口径差异，不是数据错配。",
             "filter": "按名称做 join / 去重的代码必须用 "
                       "security_master.name_alias_matches() 判等，不能用 ==。",
             "fix": "无需修复；security_master 已优先采用非临时名。"})

    # ── ⑤ security_master 完整性（P0 主检查）─────────────────────────────────
    lost_ind = []
    lost_snap = []
    lost_kl = []
    if not sm:
        add("WARN", "sm_missing",
            "security_master 不存在或为空（P0 未修复；上层会取不到北交所）",
            {"fix": "python tools/build_security_master.py"})
    else:
        if len(sm) < MIN_MASTER_ROWS:
            add("WARN", "sm_rows",
                "security_master 只有 %d 行（< %d，明显漏了整块市场）"
                % (len(sm), MIN_MASTER_ROWS))
        bse = [c for c, r in sm_stock.items() if r["board"] == SM.BOARD_BSE]
        if len(bse) < MIN_MASTER_BSE:
            add("WARN", "sm_bse",
                "security_master 北交所股票仅 %d 只（< %d，920 段没补齐）"
                % (len(bse), MIN_MASTER_BSE))

        # 主表漏收：指标库 / 快照 / K 线里明明有行、主表却没有 → 宇宙真缺口
        lost_ind = [c for c, r in ind.items()
                    if c not in sm and (U.stock_kind(c, r["name"]) == U.KIND_STOCK)]
        lost_snap = [c for c, r in snap.items()
                     if c not in sm and U.stock_kind(c, r["name"]) == U.KIND_STOCK]
        lost_all = sorted(set(lost_ind) | set(lost_snap))
        if lost_all:
            add("WARN", "sm_lost",
                "security_master 漏收指标库/快照中已有的股票 %d 只" % len(lost_all),
                {"codes": lost_all[:200]})
        kl = _open_kline(kline_db) if kline_db else None
        if kl is not None:
            try:
                kl_codes = {c for (c,) in kl.execute("SELECT DISTINCT code FROM kline")}
            except sqlite3.Error:
                kl_codes = set()
            finally:
                kl.close()
            lost_kl = [c for c in kl_codes if c not in sm
                       and U.stock_kind(c) == U.KIND_STOCK]
            if lost_kl:
                add("WARN", "sm_lost_kline",
                    "security_master 漏收 K 线库里已有的股票 %d 只" % len(lost_kl),
                    {"codes": sorted(lost_kl)[:200]})
        else:
            lost_kl = []

    # ── ⑥ 主表标为非股票、却仍出现在指标库的（要求项 3 的正面检查）─────────────
    sm_nonstock_in_ind = [
        {"code": c, "name": r["name"], "kind": sm[c]["kind"],
         "d_min": ind[c]["d_min"], "d_max": ind[c]["d_max"], "rows": ind[c]["n"],
         "live": is_live(ind[c]["d_max"])}
        for c, r in sorted(sm_nonstock.items()) if c in ind
    ]
    # 与 ① 同源，不重复计入 WARN；这里只做"口径确认"并提供消费方过滤指引
    if sm_nonstock_in_ind:
        add("INFO", "sm_nonstock_in_ind",
            "security_master 的 %d 只非股票仍出现在指标库（口径已确认，见 ①）"
            % len(sm_nonstock_in_ind),
            {"rows": sm_nonstock_in_ind,
             "filter": "指标库消费方必须 INNER JOIN security_master ON kind='stock'。"})

    # ── ⑦ security_master ⟷ a_shares_universe 差集 ───────────────────────────
    uni_not_in_sm = sorted(c for c in uni if c not in sm) if sm else []
    sm_stock_not_in_uni = sorted(c for c in sm_stock if c not in uni) if sm else []
    if sm and uni_not_in_sm:
        # 白名单有、主表没有 = 主表口径漏了 → 真缺陷
        add("WARN", "sm_gap_from_uni",
            "白名单里有 %d 只代码不在 security_master（主表口径漏收）"
            % len(uni_not_in_sm), {"codes": uni_not_in_sm[:200]})
    if sm_stock_not_in_uni:
        by_seg = {}
        for c in sm_stock_not_in_uni:
            by_seg.setdefault(_seg(c), []).append(c)
        add("INFO", "uni_gap_from_sm",
            "白名单缺 %d 只主表已收录的股票（口径不一致：旧表冻结，主表为准）"
            % len(sm_stock_not_in_uni),
            {"by_seg": {k: len(v) for k, v in sorted(by_seg.items(),
                                                     key=lambda kv: -len(kv[1]))},
             "sample": sm_stock_not_in_uni[:10],
             "why": "a_shares_universe 本轮按约束冻结（禁删禁改）；缺的正是整块北交所。"
                    "security_master 已把它补齐，两张表口径不一致是**已知且被记录**的，"
                    "不是静默容忍：本节每次都单列，并在 --fail-on-info 下算失败。",
             "filter": "任何仍读 a_shares_universe 的取码路径都会继续漏掉这些票。",
             "fix": "python tools/build_security_master.py --sync-universe"
                    "（只 INSERT 补进白名单，不删不改；补完本节归零）"})

    # ── 快照污染（非股票混进行情表）───────────────────────────────────────────
    snap_nonstock = []
    for c, r in sorted(snap.items()):
        kind = sm[c]["kind"] if c in sm else U.stock_kind(c, r["name"])
        if kind != SM.KIND_STOCK:
            snap_nonstock.append({"code": c, "name": r["name"], "kind": kind,
                                  "board": r["board"]})
    if snap_nonstock:
        add("INFO", "snap_nonstock",
            "行情快照（%s）混入非股票 %d 只（遗留污染，本轮禁止删行）"
            % (snap_day or "?", len(snap_nonstock)),
            {"rows": snap_nonstock,
             "why": "快照表由抓取端按白名单落库，白名单本身混了 2 只转债 + 1 只 ETF；"
                    "删行会让当日统计断档，属于数据清洗决策。",
             "filter": "统计涨跌家数/成交额前必须 INNER JOIN security_master "
                       "ON kind='stock'；security_master 已把 3 只标成非股票。",
             "fix": "抓取端（fetch_a_shares_snapshot.py）改用 "
                    "security_master.stock_codes() 取码"})

    # ── 主表 name 与指标库最新名的一致性（要求项 5）───────────────────────────
    sm_name_mismatch = []
    sm_name_alias = []
    if sm:
        for c, r in ind.items():
            m = sm.get(c)
            if not m or not m["name"]:
                continue
            a, b = (r["latest_name"] or "").strip(), m["name"].strip()
            if not a or not b or a == b:
                continue
            if SM.name_alias_matches(a, b):
                sm_name_alias.append({"code": c, "indicators_name": a, "master": b})
            else:
                sm_name_mismatch.append({"code": c, "indicators_name": a, "master": b})
        sm_name_mismatch.sort(key=lambda x: x["code"])
        sm_name_alias.sort(key=lambda x: x["code"])
        if sm_name_mismatch:
            add("WARN", "sm_name_mismatch",
                "security_master 与指标库最新名不一致 %d 处（非临时名）"
                % len(sm_name_mismatch), {"rows": sm_name_mismatch[:50]})
        if sm_name_alias:
            add("INFO", "sm_name_alias",
                "security_master 与指标库最新名不一致 %d 处，全部是除权临时名"
                % len(sm_name_alias),
                {"rows": sm_name_alias,
                 "why": "指标库最新一行用的是除权日临时名（XD…），主表保留恢复后的简称。",
                 "filter": "跨表按名称对齐时用 security_master.name_alias_matches()。"})

    # ── board 分布（给报告用，不参与判定）─────────────────────────────────────
    by_board, by_kind = {}, {}
    for r in sm.values():
        by_board[r["board"]] = by_board.get(r["board"], 0) + 1
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    return {
        # 兼容旧字段（老测试/老 CI 直接读这些键）
        "n_indicators": len(ind),
        "n_universe": len(uni),
        "indicators_nonstock": ind_nonstock,
        "universe_nonstock": uni_nonstock,
        "universe_missing": missing_summary,
        "universe_missing_total": missing_total,
        "name_mismatch": mismatch[:50],
        "name_mismatch_total": len(mismatch),
        # 新增
        "snap_day": snap_day,
        "ind_last_day": ind_last_day,
        "name_alias": alias,
        "name_alias_total": len(alias),
        "snapshot_nonstock": snap_nonstock,
        "master_exists": bool(sm),
        "master_rows": len(sm),
        "master_by_board": by_board,
        "master_by_kind": by_kind,
        "master_stock": len(sm_stock),
        "master_stock_bse": len([c for c, r in sm_stock.items()
                                 if r["board"] == SM.BOARD_BSE]),
        "master_nonstock": [{"code": c, "name": r["name"], "kind": r["kind"]}
                            for c, r in sorted(sm_nonstock.items())],
        "master_nonstock_in_indicators": sm_nonstock_in_ind,
        "master_lost_from_indicators": sorted(lost_ind) if sm else [],
        "master_lost_from_snapshot": sorted(lost_snap) if sm else [],
        "master_lost_from_kline": sorted(lost_kl) if sm else [],
        "universe_not_in_master": uni_not_in_sm,
        "master_stock_not_in_universe": sm_stock_not_in_uni,
        "master_name_mismatch": sm_name_mismatch[:50],
        "master_name_mismatch_total": len(sm_name_mismatch),
        "master_name_alias_total": len(sm_name_alias),
        "findings": findings,
    }


def verdict_of(r: dict) -> tuple:
    """返回 (level, warns)。level ∈ PASS / INFO / WARN；warns 只含 WARN 级别的条目。

    只有「数据层面能在本轮修好」的问题才进 warns；不能修的既存差异进 INFO
    （见模块 docstring 的降级规则），因此 `--strict` 只对前者失败。
    """
    warns = [f["msg"] for f in r.get("findings", []) if f["level"] == "WARN"]
    infos = [f["msg"] for f in r.get("findings", []) if f["level"] == "INFO"]
    if warns:
        return "WARN", warns
    if infos:
        return "INFO", warns
    return "PASS", warns


def _print_detail(f: dict, limit: int = 8) -> None:
    d = f.get("detail") or {}
    rows = d.get("rows") or []
    if rows:
        for x in rows[:limit]:
            if "indicators_name" in x:
                print("       %-8s 指标库=%-14s 对照=%s"
                      % (x["code"], x["indicators_name"],
                         x.get("names_table") or x.get("master") or ""))
            else:
                print("       %-8s %-20s %s  %s"
                      % (x["code"], x.get("name", ""), x.get("kind", ""),
                         ("近%d行 %s~%s" % (x.get("rows", 0), x.get("d_min"), x.get("d_max")))
                         if x.get("d_max") else ""))
    for k in ("why", "filter", "fix"):
        if d.get(k):
            label = {"why": "为什么允许存在", "filter": "消费方必须", "fix": "修复命令"}[k]
            print("       %s：%s" % (label, d[k]))
    if d.get("by_seg"):
        print("       按代码段：%s"
              % "、".join("%s×%d" % (k, v) for k, v in d["by_seg"].items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="宇宙一致性审计（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="有 WARN 即非零退出")
    ap.add_argument("--fail-on-info", action="store_true",
                    help="INFO 也算失败（把既存差异当硬门禁）")
    ap.add_argument("--kline-db", default=None,
                    help="K 线库路径（默认探测 data/kline_daily.db）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("[audit_universe] 找不到 %s" % DB, file=sys.stderr)
        return 2

    kline_path = args.kline_db
    if kline_path is None:
        kline_path = next((p for p in KLINE_CANDIDATES if os.path.exists(p)), None)

    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"),
                             uri=True, timeout=30)
        r = collect(con, kline_db=kline_path)
        con.close()
    except sqlite3.Error as e:
        print("[audit_universe] 读库失败：%s" % e, file=sys.stderr)
        return 2

    level, warns = verdict_of(r)
    r["level"] = level
    r["warnings"] = warns
    r["infos"] = [f["msg"] for f in r["findings"] if f["level"] == "INFO"]

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        if level == "WARN" and args.strict:
            return 1
        if level == "INFO" and args.fail_on_info:
            return 1
        return 0

    print("=" * 72)
    print("宇宙一致性审计（只读，不改任何数据）")
    print("  指标库 %d 只 / 白名单 %d 只 / 主表 %d 行（股票 %d，北交所 %d）"
          % (r["n_indicators"], r["n_universe"], r["master_rows"],
             r["master_stock"], r["master_stock_bse"]))
    print("  指标库最新日 %s ／ 快照最新日 %s" % (r["ind_last_day"], r["snap_day"]))
    print("=" * 72)

    print("\n① 指标库里的非股票标的：%d 只" % len(r["indicators_nonstock"]))
    for x in r["indicators_nonstock"][:20]:
        print("   %-8s %-14s %-18s %s 行 %s~%s%s"
              % (x["code"], x["name"], x["kind"], x["rows"], x["d_min"], x["d_max"],
                 "  ← 近期活跃" if x["live"] else ""))
    if not r["indicators_nonstock"]:
        print("   （无）")

    print("\n② 白名单里的非股票标的：%d 只" % len(r["universe_nonstock"]))
    for x in r["universe_nonstock"][:20]:
        print("   %-8s %-20s board=%-8s %s"
              % (x["code"], x["name"], x["board"], x["kind"]))
    if not r["universe_nonstock"]:
        print("   （无）")

    print("\n③ 白名单漏收（指标库有数据、白名单没有）：%d 只"
          % r["universe_missing_total"])
    for x in r["universe_missing"]:
        print("   段 %-4s %4d 只  例: %s" % (x["seg"], x["n"], ", ".join(x["sample"])))
    if not r["universe_missing"]:
        print("   （无）")

    print("\n④ 名称错配（指标库名 ≠ 名称表名）：真错配 %d 处 / 除权临时名 %d 处"
          % (r["name_mismatch_total"], r["name_alias_total"]))
    for x in r["name_mismatch"][:15]:
        print("   %-8s 指标库=%-14s 名称表=%s"
              % (x["code"], x["indicators_name"], x["names_table"]))
    for x in r["name_alias"][:10]:
        print("   %-8s 指标库=%-14s 名称表=%-14s （XD 临时名，可接受）"
              % (x["code"], x["indicators_name"], x["names_table"]))
    if not r["name_mismatch"] and not r["name_alias"]:
        print("   （无）")

    print("\n⑤ security_master（本轮 P0 口径基准）：%d 行，股票 %d 只，北交所 %d 只"
          % (r["master_rows"], r["master_stock"], r["master_stock_bse"]))
    if r["master_by_board"]:
        print("   board：" + "  ".join("%s %d" % (k, v) for k, v in
                                       sorted(r["master_by_board"].items(),
                                              key=lambda kv: -kv[1])))
    if r["master_by_kind"]:
        print("   kind ：" + "  ".join("%s %d" % (k, v) for k, v in
                                       sorted(r["master_by_kind"].items(),
                                              key=lambda kv: -kv[1])))
    print("   主表漏收：指标库 %d / 快照 %d / K线 %d"
          % (len(r["master_lost_from_indicators"]),
             len(r["master_lost_from_snapshot"]),
             len(r["master_lost_from_kline"])))
    print("   主表非股票（保留并标注）：%d 只" % len(r["master_nonstock"]))
    for x in r["master_nonstock"][:10]:
        print("       %-8s %-22s %s" % (x["code"], x["name"], x["kind"]))

    print("\n⑥ 主表标为非股票、却仍在指标库：%d 只"
          % len(r["master_nonstock_in_indicators"]))
    for x in r["master_nonstock_in_indicators"][:10]:
        print("   %-8s %-14s %-18s %s 行 %s~%s%s"
              % (x["code"], x["name"], x["kind"], x["rows"], x["d_min"], x["d_max"],
                 "  ← 近期活跃" if x["live"] else ""))
    if not r["master_nonstock_in_indicators"]:
        print("   （无）")

    print("\n⑦ 主表 ⟷ 白名单差集：白名单有主表无 %d 只 / 主表有白名单无 %d 只"
          % (len(r["universe_not_in_master"]),
             len(r["master_stock_not_in_universe"])))
    if r["master_stock_not_in_universe"]:
        seg = {}
        for c in r["master_stock_not_in_universe"]:
            seg[_seg(c)] = seg.get(_seg(c), 0) + 1
        print("   主表有、白名单无，按代码段：%s"
              % "、".join("%s×%d" % (k, v) for k, v in
                          sorted(seg.items(), key=lambda kv: -kv[1])))
        print("   例：%s" % ", ".join(r["master_stock_not_in_universe"][:8]))

    print("\n⑧ 主表名 vs 指标库最新名：真错配 %d 处 / 除权临时名 %d 处"
          % (r["master_name_mismatch_total"], r["master_name_alias_total"]))
    for x in r["master_name_mismatch"][:15]:
        print("   %-8s 指标库=%-14s 主表=%s"
              % (x["code"], x["indicators_name"], x["master"]))
    if not r["master_name_mismatch_total"] and not r["master_name_alias_total"]:
        print("   （无）")

    print("\n" + "-" * 72)
    print("结论：%s（WARN %d 条 / INFO %d 条）"
          % (level, len(warns), len(r["infos"])))
    for f in r["findings"]:
        print("  · [%s] %s" % (f["level"], f["msg"]))
        if f["level"] == "INFO":
            _print_detail(f)
    if level == "INFO":
        print("  注：INFO 是「本轮约束下改不了的既存差异」，每条都写了为什么允许、")
        print("      消费方必须怎么过滤、以及修复命令。要让它们也变成硬失败，")
        print("      加 --fail-on-info。")
    return 1 if ((level == "WARN" and args.strict) or
                 (level == "INFO" and args.fail_on_info)) else 0


if __name__ == "__main__":
    sys.exit(main())
