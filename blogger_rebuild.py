# -*- coding: utf-8 -*-
"""博主荐股「汇总表」重算的**唯一实现**：blogger_stats / blogger_overlap 一律以
blogger_reco 为真源重算。

【为什么单独抽一个模块（2026-10-09）】
汇总表原来要么由源头 Excel 的 sheet 直接灌、要么由一次性脚本重算，导致：
  · 抖音每日自动化（另一工程直接写 blogger_reco）新增记录后，汇总表**不会跟着更新、再次变陈旧**；
  · tools/ 下两个重算脚本 + Excel 导入自愈钩子各写一份逻辑，容易漂移。
本模块把口径收敛成一份，供三处复用：
  1) viewer/server.py 的**读取时自愈**（/api/blogger/stats、/api/blogger/overlap）；
  2) tools/rebuild_blogger_stats.py、tools/rebuild_blogger_overlap.py（CLI 手动重算）；
  3) tools/import_bloggers_direct.py（Excel 导入后的自愈钩子）。

【口径（硬约束，null 绝不当 0）】
blogger_stats：
  · n_stock   = SUM(ret_since IS NOT NULL)   有效样本
  · n_pending = SUM(ret_since IS NULL)       待采集
  · avg_ret   = AVG(ret_since) 仅非空；无有效样本 → NULL
  · up_ratio  = 非空里 ret_since>0 的占比（百分比）；无有效样本 → NULL
  · max_ret/min_ret = 非空最大/最小；无有效样本 → NULL
  · avg_ret_1w = 源表给的「平均一周涨幅」，reco 无对应字段 → **保留原值，绝不覆盖**
  · 末尾保留一行 blogger="全部博主合计"（全库口径）

blogger_overlap：
  · 入选 = 被 ≥2 个不同 blogger 推过的 stock_code
  · n_bloggers / bloggers = 不同博主数与名（「、」连接，按各自最早推荐日期升序）
  · **最新非空口径**：按 rec_date 升序取**最后一条 ret_since IS NOT NULL** 的记录，
    price_at_rec / close_latest / ret_since **三者同源**取自该条（保证三数可互相对上）；
    全表无 ret_since 非空者 → 三者皆 NULL（**不补 0**）。
    （2026-10-09 王总按备份反推源表口径确认为「最新一次有值的推荐」，非「最早」。）
  · note = 保留已有人工填写；新行写「自动汇总：N 位博主同推」
  · 幂等 UPSERT（UNIQUE(stock_code)），只增改不删

所有函数都在**调用方提供的连接**上操作（server 传 DB.conn，CLI 自建），并在函数内 commit。
"""
from __future__ import annotations

import sqlite3

TOTAL_LABEL = "全部博主合计"


# ---------------------------------------------------------------- 小工具
def _r2(v):
    """保留两位小数；None 原样返回（不把 None 变 0）。"""
    if v is None:
        return None
    return round(float(v), 2)


def _max_src(conn: sqlite3.Connection, table: str) -> str | None:
    """表里 MAX(src_updated)；表不存在/无行返回 None。"""
    try:
        row = conn.execute("SELECT MAX(src_updated) AS u FROM %s" % table).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return row["u"]
    except (TypeError, IndexError):
        return row[0]


# ---------------------------------------------------------------- blogger_stats
def ensure_stats_schema(conn: sqlite3.Connection) -> bool:
    """确保 blogger_stats 有 n_pending 列；返回是否为本次新增。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(blogger_stats)").fetchall()}
    if "n_pending" not in cols:
        conn.execute("ALTER TABLE blogger_stats ADD COLUMN n_pending INTEGER DEFAULT 0")
        return True
    return False


def compute_stats_rows(conn: sqlite3.Connection) -> list[dict]:
    """按博主聚合 blogger_reco，返回每位博主一行 + 末尾一行「全部博主合计」。"""
    stats = []
    for r in conn.execute(
            "SELECT blogger,"
            "  SUM(CASE WHEN ret_since IS NOT NULL THEN 1 ELSE 0 END) AS n_valid,"
            "  SUM(CASE WHEN ret_since IS NULL THEN 1 ELSE 0 END) AS n_pending,"
            "  AVG(ret_since) AS avg_ret,"
            "  SUM(CASE WHEN ret_since IS NOT NULL AND ret_since > 0 THEN 1 ELSE 0 END) AS n_up,"
            "  MAX(ret_since) AS max_ret,"
            "  MIN(ret_since) AS min_ret"
            " FROM blogger_reco GROUP BY blogger"):
        n_valid = int(r["n_valid"] or 0)
        stats.append({
            "blogger": r["blogger"],
            "n_stock": n_valid,
            "n_pending": int(r["n_pending"] or 0),
            "avg_ret": _r2(r["avg_ret"]),
            "up_ratio": _r2(100.0 * (r["n_up"] or 0) / n_valid) if n_valid else None,
            "max_ret": _r2(r["max_ret"]),
            "min_ret": _r2(r["min_ret"]),
        })

    agg = conn.execute(
        "SELECT"
        "  SUM(CASE WHEN ret_since IS NOT NULL THEN 1 ELSE 0 END) AS n_valid,"
        "  SUM(CASE WHEN ret_since IS NULL THEN 1 ELSE 0 END) AS n_pending,"
        "  AVG(ret_since) AS avg_ret,"
        "  SUM(CASE WHEN ret_since IS NOT NULL AND ret_since > 0 THEN 1 ELSE 0 END) AS n_up,"
        "  MAX(ret_since) AS max_ret,"
        "  MIN(ret_since) AS min_ret"
        " FROM blogger_reco").fetchone()
    n_valid = int(agg["n_valid"] or 0)
    stats.append({
        "blogger": TOTAL_LABEL,
        "n_stock": n_valid,
        "n_pending": int(agg["n_pending"] or 0),
        "avg_ret": _r2(agg["avg_ret"]),
        "up_ratio": _r2(100.0 * (agg["n_up"] or 0) / n_valid) if n_valid else None,
        "max_ret": _r2(agg["max_ret"]),
        "min_ret": _r2(agg["min_ret"]),
    })
    return stats


def rebuild_stats(conn: sqlite3.Connection) -> dict:
    """重算并 UPSERT blogger_stats；返回 {added_col, rows}。"""
    conn.row_factory = sqlite3.Row        # 幂等：保证本模块内部按列名取值（与 viewer/db.py 一致）
    added_col = ensure_stats_schema(conn)
    before = {r["blogger"]: (r["avg_ret_1w"] if "avg_ret_1w" in r.keys() else None)
              for r in conn.execute("SELECT * FROM blogger_stats")}
    rows = compute_stats_rows(conn)
    src_updated = _max_src(conn, "blogger_reco")
    for row in rows:
        keep_1w = before.get(row["blogger"])       # avg_ret_1w 保留原值（源表语义）
        conn.execute(
            "INSERT INTO blogger_stats"
            " (blogger, n_stock, n_pending, avg_ret, avg_ret_1w, up_ratio, max_ret, min_ret, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blogger) DO UPDATE SET"
            " n_stock=excluded.n_stock, n_pending=excluded.n_pending,"
            " avg_ret=excluded.avg_ret, avg_ret_1w=excluded.avg_ret_1w,"
            " up_ratio=excluded.up_ratio, max_ret=excluded.max_ret,"
            " min_ret=excluded.min_ret, src_updated=excluded.src_updated",
            (row["blogger"], row["n_stock"], row["n_pending"], row["avg_ret"],
             keep_1w, row["up_ratio"], row["max_ret"], row["min_ret"], src_updated))
    conn.commit()
    return {"added_col": added_col, "rows": len(rows)}


# ---------------------------------------------------------------- blogger_overlap
def compute_overlaps(conn: sqlite3.Connection) -> dict[str, dict]:
    """返回 {stock_code: {stock_name, n_bloggers, bloggers, price_at_rec, close_latest, ret_since}}。

    取数规则见模块 docstring「最新非空口径」。
    """
    rows = conn.execute(
        "SELECT stock_code, stock_name, blogger, rec_date, seq, price_at_rec, close_latest, ret_since"
        " FROM blogger_reco"
        " WHERE stock_code IN ("
        "   SELECT stock_code FROM blogger_reco"
        "   GROUP BY stock_code HAVING COUNT(DISTINCT blogger) >= 2)"
        " ORDER BY stock_code, rec_date, seq, blogger").fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["stock_code"], []).append(r)

    out: dict[str, dict] = {}
    for code, rs in groups.items():
        # 每位博主的最早推荐日期（博主排序用）
        first_seen: dict[str, str] = {}
        for r in rs:
            b = r["blogger"]
            d = r["rec_date"] or ""
            if b not in first_seen or d < first_seen[b]:
                first_seen[b] = d
        ordered_bloggers = sorted(first_seen.keys(), key=lambda b: (first_seen[b], b))

        # 最新非空：rs 已按 rec_date,seq 升序 → 迭代到最后的 ret_since 非空那条即「最新一次有值」
        latest = None
        for r in rs:
            if r["ret_since"] is not None:
                latest = r

        out[code] = {
            "stock_code": code,
            "stock_name": next((r["stock_name"] for r in rs if r["stock_name"]), None),
            "n_bloggers": len(ordered_bloggers),
            "bloggers": "、".join(ordered_bloggers),
            # 三数同源：都取自「最新非空 ret_since」那一条
            "price_at_rec": (latest["price_at_rec"] if latest else None),
            "close_latest": (latest["close_latest"] if latest else None),
            "ret_since": (latest["ret_since"] if latest else None),
        }
    return out


def rebuild_overlap(conn: sqlite3.Connection) -> dict:
    """重算并幂等 UPSERT blogger_overlap；返回 {rows, new_codes, updated_codes}。"""
    conn.row_factory = sqlite3.Row        # 幂等：保证本模块内部按列名取值（与 viewer/db.py 一致）
    before = {r["stock_code"]: dict(r) for r in conn.execute("SELECT * FROM blogger_overlap")}
    computed = compute_overlaps(conn)
    src_updated = _max_src(conn, "blogger_reco")

    new_codes, updated_codes = [], []
    for code, v in computed.items():
        old = before.get(code)
        if old is None:
            note = "自动汇总：%d 位博主同推" % v["n_bloggers"]
            new_codes.append(code)
        else:
            note = old.get("note")                 # 保留人工 note
            if (old.get("n_bloggers"), old.get("bloggers")) != (v["n_bloggers"], v["bloggers"]):
                updated_codes.append(code)
        conn.execute(
            "INSERT INTO blogger_overlap"
            " (stock_code, stock_name, n_bloggers, bloggers, price_at_rec, close_latest, ret_since,"
            "  note, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(stock_code) DO UPDATE SET"
            " stock_name=excluded.stock_name, n_bloggers=excluded.n_bloggers,"
            " bloggers=excluded.bloggers, price_at_rec=excluded.price_at_rec,"
            " close_latest=excluded.close_latest, ret_since=excluded.ret_since,"
            " note=excluded.note, src_updated=excluded.src_updated",
            (code, v["stock_name"], v["n_bloggers"], v["bloggers"], v["price_at_rec"],
             v["close_latest"], v["ret_since"], note, src_updated))
    conn.commit()
    return {"rows": len(computed), "new_codes": new_codes, "updated_codes": updated_codes}


# ---------------------------------------------------------------- 陈旧判定（读取时自愈用）
def is_stats_stale(conn: sqlite3.Connection) -> bool:
    """blogger_stats 是否比 blogger_reco 陈旧（reco 的 MAX(src_updated) 更新 或 汇总表为空）。"""
    reco = _max_src(conn, "blogger_reco")
    cur = _max_src(conn, "blogger_stats")
    if reco is None:
        return False
    if cur is None:
        return True
    return str(reco) > str(cur)


def is_overlap_stale(conn: sqlite3.Connection) -> bool:
    """blogger_overlap 是否比 blogger_reco 陈旧。"""
    reco = _max_src(conn, "blogger_reco")
    cur = _max_src(conn, "blogger_overlap")
    if reco is None:
        return False
    if cur is None:
        return True
    return str(reco) > str(cur)
