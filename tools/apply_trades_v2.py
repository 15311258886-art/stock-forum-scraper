# -*- coding: utf-8 -*-
"""换库工具：把新抽取的交易结果（trades_v2_staging.jsonl）预备成「可一键执行」的迁移。

背景
----
生产 `trades` 表被旧口径污染（348 行，Top4 股票占 63.8%）；新规则重抽得到 staging
956 行（Top1 仅约 3.9%）。王总尚未拍板换库方案（②用新语料重建 / ③并排展示），
故本工具先把「导入 + 对账 + 备份 + 回滚」做成**可重复执行、默认不碰生产库**的一条命令。

绝对红线
--------
* 只读打开生产库时一律使用 `file:...?mode=ro`（外加 `PRAGMA query_only=1` 双保险）。
* `--dry-run` 只写**副本库**；`--apply` / `--rollback` 会写生产库，必须显式 `--yes`。
* 本工具**不重启服务、不强杀进程、不修改 viewer/ 下任何既有文件**。

子命令
------
    --init-copy              用 SQLite backup API 把生产库复制成一份**一致性副本**（含 WAL）
    --dry-run --db <副本>    在副本上重建 trades_v2 并打印对账（默认动作）
    --apply                  备份生产库 → 在生产库写入 trades_v2（需 --yes）
    --rollback --from <备份> 从指定备份恢复生产库（需 --yes）

用法示例
--------
    .venv\\Scripts\\python.exe tools\\apply_trades_v2.py --init-copy
    .venv\\Scripts\\python.exe tools\\apply_trades_v2.py --dry-run --db tools\\_migrate\\app.db
    .venv\\Scripts\\python.exe tools\\apply_trades_v2.py --apply --yes
    .venv\\Scripts\\python.exe tools\\apply_trades_v2.py --rollback --from viewer\\app.db.bak-YYYYmmdd-HHMMSS --yes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
import os

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))
DEFAULT_STAGING = Path(
    r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\trades_v2_staging.jsonl"
)
DEFAULT_COPY_DIR = ROOT / "tools" / "_migrate"
V2_TABLE = "trades_v2"

# trades 表的标准列（作为「实时表缺失」时的兜底定义；实际以 PRAGMA 为准）
FALLBACK_TRADES_COLS: list[tuple[str, str]] = [
    ("id", "INTEGER"),
    ("site", "TEXT"),
    ("author", "TEXT"),
    ("post_id", "TEXT"),
    ("post_url", "TEXT"),
    ("post_time", "TEXT"),
    ("action", "TEXT"),
    ("symbol", "TEXT"),
    ("symbol_name", "TEXT"),
    ("price", "REAL"),
    ("quantity", "INTEGER"),
    ("position_ratio", "REAL"),
    ("buy_time", "TEXT"),
    ("buy_price", "REAL"),
    ("current_price", "REAL"),
    ("sell_time", "TEXT"),
    ("sell_price", "REAL"),
    ("hold_since", "TEXT"),
    ("access_level", "TEXT"),
    ("access_hint", "TEXT"),
    ("source_type", "TEXT"),
    ("raw_text", "TEXT"),
]

# trades_v2 在对齐 trades 之外额外携带的列（保留 staging 溯源信息 + 机器号轴）
EXTRA_COLS: list[tuple[str, str]] = [
    ("nickname", "TEXT"),
    ("corpus_text_kind", "TEXT"),
    ("axis", "TEXT"),
]

# 需抽查名称正确性的样本（代码 → 期望名称）
NAME_SAMPLES = {
    "600519": "贵州茅台",
    "300750": "宁德时代",
    "601318": "中国平安",
    "000001": "平安银行",
}


# --------------------------------------------------------------------------- #
# 连接与副本
# --------------------------------------------------------------------------- #
def open_ro(path: Path) -> sqlite3.Connection:
    """以**只读**方式打开库（mode=ro + query_only 双保险）。"""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _clean_sidecars(target: Path) -> None:
    """删除目标库的 -wal / -shm 残留，避免与新建文件不一致。"""
    for suf in ("-wal", "-shm"):
        p = Path(str(target) + suf)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def make_consistent_copy(src: Path, out: Path) -> Path:
    """用 SQLite backup API 生成**一致性副本**（自动合并 WAL，无需单独拷 -wal/-shm）。

    backup API 读取的是「一个事务一致快照」，是官方推荐的在线备份方式；
    若因环境原因不可用，则回退为「db + wal + shm 三件套整拷」。
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    _clean_sidecars(out)
    try:
        scon = open_ro(src)
        dcon = sqlite3.connect(str(out))
        try:
            scon.backup(dcon)
            dcon.commit()
        finally:
            dcon.close()
            scon.close()
        mode = "backup-api"
    except Exception as exc:  # noqa: BLE001 —— 回退到整拷
        print(f"[warn] backup API 失败（{exc}），回退三件套整拷", file=sys.stderr)
        import shutil

        shutil.copy2(src, out)
        for suf in ("-wal", "-shm"):
            s = Path(str(src) + suf)
            if s.exists():
                shutil.copy2(s, str(out) + suf)
        mode = "file-copy(+wal/shm)"
    # 一致性自检
    con = sqlite3.connect(str(out))
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    print(f"[copy] {src} -> {out}  ({mode}, integrity_check={ok})")
    return out


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #
def table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [(r["name"], (r["type"] or "").upper() or "TEXT") for r in rows]


def ensure_trades_v2(conn: sqlite3.Connection, drop: bool = True) -> list[str]:
    """按 trades 的真实结构 + 附加列，创建 trades_v2（可选先 DROP）。"""
    cols = table_columns(conn, "trades")
    if not cols:
        cols = FALLBACK_TRADES_COLS
    names = {n for n, _ in cols}
    extra = [(n, t) for n, t in EXTRA_COLS if n not in names]

    if drop:
        conn.execute(f"DROP TABLE IF EXISTS {V2_TABLE}")

    defs = []
    for name, ctype in cols:
        if name == "id":
            defs.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            defs.append(f"{name} {ctype}")
    for name, ctype in extra:
        defs.append(f"{name} {ctype}")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {V2_TABLE} (\n  " + ",\n  ".join(defs) + "\n)")
    # 与 trades 同口径的唯一键 + 常用检索索引
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_v2_keys "
        f"ON {V2_TABLE}(site, post_url, author, symbol_name, action)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_trades_v2_symbol ON {V2_TABLE}(symbol)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_trades_v2_author ON {V2_TABLE}(author)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_trades_v2_action ON {V2_TABLE}(action)")
    # 镜像生产库 trades 的第二个唯一键（IFNULL 归一，防 NULL 逃逸去重）
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_v2_keys2 ON {V2_TABLE}"
        f"(site, IFNULL(post_url,''), IFNULL(author,''), IFNULL(symbol,''), IFNULL(action,''))"
    )
    v2cols = [n for n, _ in table_columns(conn, V2_TABLE)]
    if "axis" in v2cols and "stale" in v2cols:
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_trades_v2_axis ON {V2_TABLE}(axis, stale)")
    conn.commit()
    return [n for n, _ in cols] + [n for n, _ in extra]


# --------------------------------------------------------------------------- #
# staging 读取 / 导入
# --------------------------------------------------------------------------- #
def load_staging(path: Path) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def stock_name_map(conn: sqlite3.Connection) -> dict[str, str]:
    """复刻 db._ensure_symbol_name 的名称来源：stock_names(code→name)。"""
    try:
        rows = conn.execute("SELECT code, name FROM stock_names").fetchall()
    except sqlite3.Error:
        return {}
    return {r["code"]: (r["name"] or "") for r in rows}


def _norm_sym(v: Any) -> str:
    s = ("" if v is None else str(v)).strip()
    return s


def import_staging(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
                   fill_times: bool = True) -> dict[str, int]:
    """把 staging 记录写入 trades_v2（INSERT OR IGNORE，按唯一键去重）。

    fill_times=True 时按 action 回填成交时刻列：BUY→buy_time、SELL→sell_time、
    HOLD→buy_time；因为「近 N 日同向操作」面板按 buy_time/sell_time 过滤窗口，
    staging 只有 post_time，不回填则窗口过滤会整体落空（详见回报说明）。
    """
    cols = [n for n, _ in table_columns(conn, V2_TABLE)]
    nmap = stock_name_map(conn)

    stats = Counter()
    sql_cols = ["site", "author", "post_id", "post_url", "post_time", "action",
                "symbol", "symbol_name", "price", "quantity", "raw_text",
                "nickname", "corpus_text_kind"]
    for opt in ("buy_time", "sell_time", "hold_since", "source_type"):
        if opt in cols:
            sql_cols.append(opt)
    placeholders = ",".join(":" + c for c in sql_cols)
    stmt = (f"INSERT OR IGNORE INTO {V2_TABLE} ({','.join(sql_cols)}) "
            f"VALUES ({placeholders})")

    for rec in records:
        symbol = _norm_sym(rec.get("symbol"))
        symbol_name = (rec.get("symbol_name") or "").strip()
        if symbol and not symbol_name:
            symbol_name = nmap.get(symbol, "")
            if symbol_name:
                stats["name_filled"] += 1
        action = (rec.get("action") or "").strip().upper()
        post_time = rec.get("post_time") or ""
        # 关键：INSERT 绑定的是 sql_cols 全集，必须为每个键都提供值（缺省再按 action 覆盖）
        row: dict[str, Any] = {c: None for c in sql_cols}
        row.update({
            "site": rec.get("site"),
            "author": rec.get("author"),
            "post_id": rec.get("post_id"),
            "post_url": rec.get("post_url"),
            "post_time": post_time,
            "action": action,
            "symbol": symbol or None,
            "symbol_name": symbol_name or None,
            "price": rec.get("price"),
            "quantity": rec.get("quantity"),
            "raw_text": rec.get("raw_text", ""),
            "nickname": rec.get("nickname"),
            "corpus_text_kind": rec.get("corpus_text_kind"),
        })
        row["buy_time"] = ""
        row["sell_time"] = ""
        row["hold_since"] = ""
        row["source_type"] = "player_page"
        if fill_times and action in ("BUY", "HOLD"):
            row["buy_time"] = post_time
        if fill_times and action == "SELL":
            row["sell_time"] = post_time
        cur = conn.execute(stmt, row)
        if cur.rowcount:
            stats["inserted"] += 1
        else:
            stats["ignored"] += 1
    conn.commit()
    stats["input"] = len(list(records)) if isinstance(records, list) else stats["inserted"]
    return dict(stats)


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #
def _dist(conn: sqlite3.Connection, table: str, col: str) -> Counter:
    q = (f"SELECT {col} k, COUNT(*) n FROM {table} GROUP BY {col} ORDER BY n DESC")
    return Counter({(r["k"] if r["k"] is not None else "<NULL>"): r["n"]
                    for r in conn.execute(q).fetchall()})


def _stock_concentration(conn: sqlite3.Connection, table: str, topn: int) -> dict:
    rows = conn.execute(
        f"SELECT symbol, symbol_name, COUNT(*) n FROM {table} "
        f"WHERE symbol IS NOT NULL AND symbol<>'' GROUP BY symbol ORDER BY n DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    top = [(r["symbol"], r["symbol_name"] or "", r["n"],
            round(100.0 * r["n"] / total, 1) if total else 0.0)
           for r in rows[:topn]]
    return {"rows_with_symbol": total, "distinct": len(rows), "top": top,
            "top1_pct": top[0][3] if top else 0.0,
            "top5_pct": round(sum(t[3] for t in top[:5]), 1)}


def _join_health(conn: sqlite3.Connection) -> dict:
    players_total = conn.execute("SELECT COUNT(*) n FROM players").fetchone()["n"]
    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
    has_tier = "tier" in pcols
    has_axis = "axis" in pcols

    rows = conn.execute(
        f"""SELECT t.author AS author, COUNT(*) n,
                   MAX(CASE WHEN pl.name IS NOT NULL THEN 1 ELSE 0 END) AS m
              FROM {V2_TABLE} t
              LEFT JOIN players pl ON pl.name = t.author
             WHERE t.author IS NOT NULL AND t.author <> ''
             GROUP BY t.author"""
    ).fetchall()
    matched_rows = unmatched_rows = 0
    matched_names = unmatched_names = 0
    unmatched_list: list[tuple[str, int]] = []
    for r in rows:
        if r["m"]:
            matched_names += 1
            matched_rows += r["n"]
        else:
            unmatched_names += 1
            unmatched_rows += r["n"]
            unmatched_list.append((r["author"], r["n"]))
    unmatched_list.sort(key=lambda x: (-x[1], x[0]))

    # tier 覆盖（D 的「仅看入围选手」开关依赖 players.tier='A'）
    tier_cov = {"available": has_tier}
    if has_tier:
        tier_dist = conn.execute(
            "SELECT COALESCE(tier,'') k, COUNT(*) n FROM players GROUP BY k ORDER BY n DESC"
        ).fetchall()
        tier_cov["players_by_tier"] = {r["k"] or "<NULL>": r["n"] for r in tier_dist}
        a_names = {r["name"] for r in conn.execute(
            "SELECT name FROM players WHERE tier='A'").fetchall()}
        rows_a = conn.execute(
            f"SELECT COUNT(*) n FROM {V2_TABLE} t WHERE t.author IN "
            f"(SELECT name FROM players WHERE tier='A')").fetchone()["n"]
        tier_cov["trades_v2_rows_in_tierA"] = rows_a
        tier_cov["trades_v2_authors_in_tierA"] = len(
            {r["author"] for r in conn.execute(
                f"SELECT DISTINCT author FROM {V2_TABLE} WHERE author IN "
                f"(SELECT name FROM players WHERE tier='A')").fetchall()})
        tier_cov["tierA_players"] = len(a_names)

    axis_cov = {"players_has_axis": has_axis}
    has_axis_trades = conn.execute(
        "SELECT COUNT(*) n FROM pragma_table_info(?) WHERE name='axis'", (V2_TABLE,)
    ).fetchone()["n"]
    axis_cov["trades_v2_has_axis"] = bool(has_axis_trades)
    if has_axis_trades:
        n_axis = conn.execute(
            f"SELECT COUNT(*) n FROM {V2_TABLE} "
            f"WHERE axis IS NOT NULL AND axis<>''").fetchone()["n"]
        axis_cov["trades_v2_rows_with_axis"] = n_axis

    return {
        "players_total": players_total,
        "authors_total": len(rows),
        "matched_rows": matched_rows,
        "matched_names": matched_names,
        "unmatched_rows": unmatched_rows,
        "unmatched_names": unmatched_names,
        "unmatched_top": unmatched_list[:25],
        "tier": tier_cov,
        "axis": axis_cov,
    }


def _completeness(conn: sqlite3.Connection) -> dict:
    total = conn.execute(f"SELECT COUNT(*) n FROM {V2_TABLE}").fetchone()["n"]
    nullsym = conn.execute(
        f"SELECT COUNT(*) n FROM {V2_TABLE} WHERE symbol IS NULL OR symbol=''"
    ).fetchone()["n"]
    good6 = conn.execute(
        f"SELECT COUNT(*) n FROM {V2_TABLE} "
        f"WHERE symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' AND LENGTH(symbol)=6"
    ).fetchone()["n"]
    bad = conn.execute(
        f"SELECT symbol, COUNT(*) n FROM {V2_TABLE} "
        f"WHERE symbol IS NOT NULL AND symbol<>'' "
        f"  AND NOT (symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' AND LENGTH(symbol)=6) "
        f"GROUP BY symbol ORDER BY n DESC LIMIT 20"
    ).fetchall()
    nullname = conn.execute(
        f"SELECT COUNT(*) n FROM {V2_TABLE} "
        f"WHERE symbol IS NOT NULL AND symbol<>'' AND (symbol_name IS NULL OR symbol_name='')"
    ).fetchone()["n"]
    samples = {}
    for code, expect in NAME_SAMPLES.items():
        r = conn.execute(
            f"SELECT symbol_name, COUNT(*) n FROM {V2_TABLE} WHERE symbol=? "
            f"GROUP BY symbol_name ORDER BY n DESC LIMIT 1", (code,)).fetchone()
        got = r["symbol_name"] if r else None
        samples[code] = {"expect": expect, "got": got, "rows": (r["n"] if r else 0),
                         "ok": (got == expect)}
    # 脏名检测：symbol_name 与 stock_names 权威名称不一致的行
    nmap = stock_name_map(conn)
    dirty = conn.execute(
        f"SELECT t.symbol, t.symbol_name, COUNT(*) n FROM {V2_TABLE} t "
        f"WHERE t.symbol_name IS NOT NULL AND t.symbol_name<>'' "
        f"GROUP BY t.symbol, t.symbol_name"
    ).fetchall()
    dirty_list = [(r["symbol"], r["symbol_name"], nmap.get(r["symbol"], ""), r["n"])
                  for r in dirty
                  if nmap.get(r["symbol"]) and r["symbol_name"] != nmap[r["symbol"]]]
    dirty_list.sort(key=lambda x: -x[3])
    # 时间列退化检查
    pt_vals = conn.execute(
        f"SELECT post_time v, COUNT(*) n FROM {V2_TABLE} GROUP BY v ORDER BY n DESC LIMIT 5"
    ).fetchall()
    return {
        "total_rows": total,
        "null_symbol_rows": nullsym,
        "six_digit_rows": good6,
        "non_six_digit": [(r["symbol"], r["n"]) for r in bad],
        "null_name_rows": nullname,
        "name_samples": samples,
        "distinct_post_time": conn.execute(
            f"SELECT COUNT(DISTINCT post_time) n FROM {V2_TABLE}").fetchone()["n"],
        "distinct_post_url": conn.execute(
            f"SELECT COUNT(DISTINCT post_url) n FROM {V2_TABLE}").fetchone()["n"],
        "post_time_top": [(r["v"], r["n"]) for r in pt_vals],
        "dirty_name_rows": sum(x[3] for x in dirty_list),
        "dirty_name_top": dirty_list[:15],
    }


def reconcile(conn: sqlite3.Connection, staging_rows: int, topn: int) -> dict:
    old_rows = conn.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
    rep: dict[str, Any] = {
        "trades_rows": old_rows,
        "trades_v2_rows": conn.execute(f"SELECT COUNT(*) n FROM {V2_TABLE}").fetchone()["n"],
        "staging_rows": staging_rows,
        "trades_distinct_author": conn.execute(
            "SELECT COUNT(DISTINCT author) n FROM trades").fetchone()["n"],
        "trades_v2_distinct_author": conn.execute(
            f"SELECT COUNT(DISTINCT author) n FROM {V2_TABLE}").fetchone()["n"],
        "trades_distinct_symbol": conn.execute(
            "SELECT COUNT(DISTINCT symbol) n FROM trades WHERE symbol IS NOT NULL AND symbol<>''"
        ).fetchone()["n"],
        "trades_v2_distinct_symbol": conn.execute(
            f"SELECT COUNT(DISTINCT symbol) n FROM {V2_TABLE} "
            f"WHERE symbol IS NOT NULL AND symbol<>''").fetchone()["n"],
        "trades_action": dict(_dist(conn, "trades", "action")),
        "trades_v2_action": dict(_dist(conn, V2_TABLE, "action")),
        "trades_site": dict(_dist(conn, "trades", "site")),
        "trades_v2_site": dict(_dist(conn, V2_TABLE, "site")),
        "trades_conc": _stock_concentration(conn, "trades", topn),
        "trades_v2_conc": _stock_concentration(conn, V2_TABLE, topn),
        "post_time_distinct": conn.execute(
            f"SELECT COUNT(DISTINCT post_time) n FROM {V2_TABLE}").fetchone()["n"],
        "join": _join_health(conn),
        "completeness": _completeness(conn),
    }
    return rep


# --------------------------------------------------------------------------- #
# 打印
# --------------------------------------------------------------------------- #
def print_report(rep: dict, fp=None) -> None:
    p = (lambda *a: print(*a, file=fp)) if fp is not None else print
    p("=" * 78)
    p("换库对账（trades  vs  trades_v2）")
    p("=" * 78)
    p(f"staging 行数           : {rep['staging_rows']}")
    p(f"生产 trades 行数       : {rep['trades_rows']}")
    p(f"导入 trades_v2 行数    : {rep['trades_v2_rows']}")
    p(f"distinct 作者  trades={rep['trades_distinct_author']}  "
      f"trades_v2={rep['trades_v2_distinct_author']}")
    p(f"distinct 股票  trades={rep['trades_distinct_symbol']}  "
      f"trades_v2={rep['trades_v2_distinct_symbol']}")
    p("")
    p("[action 分布]")
    p(f"  trades   : {rep['trades_action']}")
    p(f"  trades_v2: {rep['trades_v2_action']}")
    p("[site 分布]")
    p(f"  trades   : {rep['trades_site']}")
    p(f"  trades_v2: {rep['trades_v2_site']}")
    p("")
    p("[Top 股票集中度]")
    for tag, key in (("trades", "trades_conc"), ("trades_v2", "trades_v2_conc")):
        c = rep[key]
        p(f"  {tag:<10} 有码行={c['rows_with_symbol']}  distinct={c['distinct']}  "
          f"Top1={c['top1_pct']}%  Top5={c['top5_pct']}%")
        for s, nm, n, pct in c["top"]:
            p(f"      {s}  {nm or '(无名)':<10} {n:>5} 条  {pct}%")
    p("")
    j = rep["join"]
    p("[join 健康度]   trades_v2.author  ->  players.name")
    p(f"  players 总数              : {j['players_total']}")
    p(f"  trades_v2 作者数          : {j['authors_total']}")
    p(f"  可 join(作者) / 行        : {j['matched_names']} / {j['matched_rows']}")
    p(f"  不可 join(作者) / 行      : {j['unmatched_names']} / {j['unmatched_rows']}")
    if j["unmatched_top"]:
        p("  不可 join 作者样例(昵称 → 行数):")
        for nm, n in j["unmatched_top"]:
            p(f"      {nm!r}  →  {n}")
    t = j["tier"]
    if t.get("available"):
        p(f"  players.tier 分布         : {t.get('players_by_tier')}")
        p(f"  Tier A 选手数             : {t.get('tierA_players')}")
        p(f"  trades_v2 落入 Tier A 的行/作者 : "
          f"{t.get('trades_v2_rows_in_tierA')} / {t.get('trades_v2_authors_in_tierA')}")
    else:
        p("  players 无 tier 列（D 开关无法工作）")
    a = j["axis"]
    p(f"  players.axis 存在         : {a.get('players_has_axis')}  "
      f"trades_v2.axis 存在: {a.get('trades_v2_has_axis')}  "
      f"带 axis 的行: {a.get('trades_v2_rows_with_axis')}")
    p("")
    c = rep["completeness"]
    p("[数据完整性]")
    p(f"  总行 / 空 symbol 行       : {c['total_rows']} / {c['null_symbol_rows']}")
    p(f"  6 位码行                  : {c['six_digit_rows']}")
    if c["non_six_digit"]:
        p(f"  非 6 位码样例             : {c['non_six_digit']}")
    p(f"  有码但无名称的行          : {c['null_name_rows']}")
    p(f"  脏名行(≠权威名)           : {c['dirty_name_rows']}  "
      f"(样例 symbol,实际,权威,行数): {c['dirty_name_top']}")
    p("  名称抽查（代码→期望/实际）:")
    for code, s in c["name_samples"].items():
        flag = "OK " if s["ok"] else "!! "
        p(f"      {flag}{code}  期望={s['expect']}  实际={s['got']}"
          f"{'  ← 该代码在 trades_v2 无行' if s['rows'] == 0 else ''}")
    p("")
    p("[时间列（窗口过滤依赖）]")
    p(f"  distinct post_time = {c['distinct_post_time']}  "
      f"distinct post_url = {c['distinct_post_url']}")
    p(f"  post_time Top 值   : {c['post_time_top']}")
    if c["distinct_post_time"] == 1:
        p("  ⚠ 时间列退化：全部同一时刻 → 「近N日同向操作」窗口/『取最新』排序会失真")
    p("=" * 78)


# --------------------------------------------------------------------------- #
# 各子命令
# --------------------------------------------------------------------------- #
def cmd_init_copy(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else DEFAULT_COPY_DIR / "app.db"
    make_consistent_copy(Path(args.db), out)
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    db = Path(args.db)
    if not db.exists():
        print(f"[error] 副本不存在: {db}（先跑 --init-copy）", file=sys.stderr)
        return 2
    if db.resolve() == DEFAULT_DB.resolve():
        print("[error] --dry-run 指向了生产库，拒绝执行。请传副本路径。", file=sys.stderr)
        return 2
    if not Path(args.staging).exists():
        print(f"[error] staging 不存在: {args.staging}", file=sys.stderr)
        return 2

    records = load_staging(Path(args.staging))
    conn = open_rw(db)
    try:
        ensure_trades_v2(conn, drop=True)
        stats = import_staging(conn, records, fill_times=not args.no_fill_times)
        print(f"[import] staging={len(records)}  {stats}")
        rep = reconcile(conn, len(records), args.topn)
        print_report(rep)
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as fp:
                print_report(rep, fp)
            print(f"[report] 已写出 {args.report_out}")
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[json] 已写出 {args.json_out}")
    finally:
        conn.close()
    return 0


def _backup_prod(tag: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = DEFAULT_DB.with_name(f"{DEFAULT_DB.name}.bak-{ts}")
    make_consistent_copy(DEFAULT_DB, bak)
    if tag:
        print(f"[backup] 生产库已备份 -> {bak}  ({tag})")
    return bak


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        print("[abort] --apply 会写生产库，必须显式加 --yes。", file=sys.stderr)
        return 2
    records = load_staging(Path(args.staging))
    bak = _backup_prod("apply trades_v2")
    conn = open_rw(DEFAULT_DB)
    try:
        ensure_trades_v2(conn, drop=True)
        stats = import_staging(conn, records, fill_times=not args.no_fill_times)
        print(f"[apply] staging={len(records)}  {stats}")
        rep = reconcile(conn, len(records), args.topn)
        print_report(rep)
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as fp:
                print_report(rep, fp)
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        conn.close()
    print(f"[apply] 完成。回滚：--rollback --from {bak} --yes")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        print("[abort] --rollback 会覆盖生产库，必须显式加 --yes。", file=sys.stderr)
        return 2
    src = Path(args.frm)
    if not src.exists():
        print(f"[error] 备份不存在: {src}", file=sys.stderr)
        return 2
    # 覆盖前，先把「当前生产库」也留一份，避免回滚本身不可逆
    safe = DEFAULT_DB.with_name(
        f"{DEFAULT_DB.name}.bak-before-rollback-{time.strftime('%Y%m%d-%H%M%S')}")
    make_consistent_copy(DEFAULT_DB, safe)
    print(f"[rollback] 当前生产库另存 -> {safe}")
    make_consistent_copy(src, DEFAULT_DB)
    _clean_sidecars(DEFAULT_DB)
    print(f"[rollback] 已从 {src} 恢复生产库。请人工重启服务使其生效。")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="换库工具：staging -> trades_v2 + 对账/备份/回滚")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--init-copy", action="store_true", help="生成一致性副本")
    g.add_argument("--dry-run", action="store_true", help="在副本上导入并打印对账（推荐）")
    g.add_argument("--apply", action="store_true", help="备份后写生产库")
    g.add_argument("--rollback", action="store_true", help="从备份恢复生产库")

    ap.add_argument("--db", default=str(DEFAULT_DB), help="目标库（dry-run 应传副本路径）")
    ap.add_argument("--out", default=None, help="--init-copy 的输出副本路径")
    ap.add_argument("--from", dest="frm", default=None, help="--rollback 的备份路径")
    ap.add_argument("--staging", default=str(DEFAULT_STAGING), help="staging jsonl 路径")
    ap.add_argument("--json-out", default=None, help="把对账结果另存为 JSON")
    ap.add_argument("--report-out", default=None, help="把对账文本（UTF-8）另存为文件")
    ap.add_argument("--topn", type=int, default=5, help="集中度 TopN（默认 5）")
    ap.add_argument("--no-fill-times", action="store_true",
                    help="不回填 buy_time/sell_time（默认回填 = post_time）")
    ap.add_argument("--yes", action="store_true", help="确认写入生产库（apply/rollback 必需）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.init_copy:
        return cmd_init_copy(args)
    if args.apply:
        return cmd_apply(args)
    if args.rollback:
        if not args.frm:
            print("[error] --rollback 需要 --from <备份路径>", file=sys.stderr)
            return 2
        return cmd_rollback(args)
    # 默认走 dry-run（显式或隐式）
    return cmd_dry_run(args)


if __name__ == "__main__":
    sys.exit(main())
