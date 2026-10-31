# -*- coding: utf-8 -*-
"""存储体检报告（P1-4）。一份人能读、也能机读（--json）的「钱花在哪」账单。

## 为什么需要它

`viewer/app.db` 从 1 GB 涨到 8.9 GB 的过程中，没有人能回答三个基本问题：
  ① 这 8.9 GB 里，哪张表、哪个索引占的？
  ② 哪些索引是白占的（和主键/别的索引前缀重复）？
  ③ 删掉东西之后，为什么 `du` 一点没变？

SQLite 自己不给 per-object 体积统计，所以过去只能靠 `ls -l` 猜。
本工具用 `dbstat` 虚拟表把每一页归到具体对象上（SQLite 官方提供的、唯一可靠的口径），
用 `sqlite_stat1` 取行数（零成本，不用 COUNT(*) 扫表），并实现一条**通用**的冗余判定规则。

## 冗余判定规则（通用，不是特例清单）

对同一张表上的两个索引 A、B：若 A 的列序列（含 ASC/DESC 与排序规则）是 B 的**前缀**，
则 A 能被 B 完全顶替 —— A 是冗余候选。

  · `idx(x, y)` 与 `idx(x, y, z)`：前者冗余（后者能服务 x 单点、x+y 组合）。
  · `idx(x, y)` 与 `idx(x, y)`：完全重复 —— 此时不可删的一方胜出：
    PK/UNIQUE 约束的自动索引（`sqlite_autoindex_*`）删不掉，所以把**命名索引**标为候选。
  · 若 A 是 partial index（带 WHERE）、或列名相同但 DESC/COLLATE 不同，**不判**冗余 ——
    这两种情况下 B 并不能顶替 A，误判会掉进"以为省了空间，结果查询变全表扫"的坑。
  · 只判定、不自动删：删除是有副作用的破坏性操作，必须人看过再动。

## 退出码

  0  正常
  3  发现冗余索引（可回收空间）
  4  发现超期缓存（`viewer/data/kline_*.json` 超过 30 天未动）
  两者同时存在时返回 3（先处理结构性问题，再处理冷数据）。

用法：
    .venv\\Scripts\\python.exe tools/storage_report.py            # 人读报告
    .venv\\Scripts\\python.exe tools/storage_report.py --json      # 机读（喂给看板/告警）
    .venv\\Scripts\\python.exe tools/storage_report.py --no-dbstat # 跳过全库扫描（秒回，只有行数）
    .venv\\Scripts\\python.exe tools/storage_report.py --top 25

注意：默认会做一次 `dbstat` **全库扫描**（8.9 GB 库实测 8.3 s，只读、不影响服务）。
急的时候用 --no-dbstat。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
# 备份目录：viewer/backups 是本工具族的产出，backups/ 是历史手工快照 —— 两边都算，都是钱
BACKUP_DIRS = [os.path.join(ROOT, "viewer", "backups"), os.path.join(ROOT, "backups")]
CACHE_DIR = os.path.join(ROOT, "viewer", "data")
CACHE_PATTERN = "kline_*.json"
STALE_DAYS = 30

EXIT_OK = 0
EXIT_REDUNDANT_INDEX = 3
EXIT_STALE_CACHE = 4


# ──────────────────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────────────────
def _human(n) -> str:
    """>=1024 用 KB/MB/GB 显示（1 KB = 1024 B，与 Windows 资源管理器一致）。"""
    if n is None:
        return "n/a"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return ("%.0f %s" % (f, unit)) if unit == "B" else ("%.1f %s" % (f, unit))
        f /= 1024.0
    return "%.1f TB" % f


def _dw(s) -> int:
    """字符串的显示宽度：中日韩全角字符算 2 列。

    为什么需要：`%-20s` 按**字符数**补空格，而表格里有中文表头/中文判定，
    终端里是双宽 —— 不按显示宽度算，表头和数据列会错开一大截，报告就没法扫读了。
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def _lj(s, width: int) -> str:
    """按显示宽度左对齐。"""
    s = str(s)
    return s + " " * max(0, width - _dw(s))


def _rj(s, width: int) -> str:
    """按显示宽度右对齐。"""
    s = str(s)
    return " " * max(0, width - _dw(s)) + s


def _num(n) -> str:
    """行数带千分位；None → n/a。"""
    return "n/a" if n is None else "{:,}".format(int(n))


def _connect_ro(path: str) -> sqlite3.Connection:
    """只读打开：体检工具绝不允许意外写库（连 `PRAGMA optimize` 都不做）。"""
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=30)


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _dbstat_available(con: sqlite3.Connection) -> bool:
    """探测 dbstat 虚拟表是否编译进来（部分发行版裁掉了 SQLITE_ENABLE_DBSTAT_VTAB）。"""
    try:
        con.execute("SELECT SUM(pgsize) FROM dbstat WHERE name='__nonexistent__'").fetchone()
        return True
    except sqlite3.Error:
        return False


# ──────────────────────────────────────────────────────────────────────────
# 体积：dbstat 逐对象归账
# ──────────────────────────────────────────────────────────────────────────
def _objects_by_dbstat(con: sqlite3.Connection) -> dict:
    """一次全库扫描，返回 {对象名: {"bytes":..., "pages":...}}。

    为什么只扫一遍：dbstat 每扫一次都要走完全部 btree，8.9 GB 库实测单遍 8.3 s，
    扫两遍就是白等十几秒。
    归不到 sqlite_master 的名字（如 sqlite_schema 自身）保留在结果里，不丢账。
    """
    out = {}
    cur = con.execute(
        "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages FROM dbstat GROUP BY name")
    for name, size, pages in cur:
        out[name] = {"bytes": size or 0, "pages": pages or 0}
    return out


def _objects_by_pragma(con: sqlite3.Connection) -> dict:
    """dbstat 不可用时的退化路径：用 page_count/page_size 只能给全库总量，逐对象为 n/a。

    故意**不**用「行数 × 平均行长」估算 —— 那种数字看起来像测量结果，实际误差能到几倍，
    比老实承认"测不出来"更有害。
    """
    return {}


# ──────────────────────────────────────────────────────────────────────────
# 行数：sqlite_stat1（零成本）；没有统计时如实标 n/a
# ──────────────────────────────────────────────────────────────────────────
def _row_counts(con: sqlite3.Connection) -> dict:
    """{(表名): stat1 里记的行数} —— **只用来判断统计信息是否过期，不当真。**

    坑（2026-11-01 实测踩到）：`sqlite_stat1` 里 `stock_indicators_daily` 记的是
    1,402,740 行，而表里实际有 13,167,610 行 —— ANALYZE 是在 10-31 那次「回补 2016→今」
    之前跑的，之后涨了近 10 倍。把过期统计当行数报出来，等于给读者一个错一个数量级的数字。
    """
    rows = {}
    try:
        for tbl, idx, stat in con.execute("SELECT tbl, idx, stat FROM sqlite_stat1"):
            try:
                n = int(str(stat).split()[0])
            except (ValueError, IndexError):
                continue
            # 索引行的第一个整数也是「表行数」，同一张表多行取最大（最接近真实规模的那条）
            if n > rows.get(tbl, -1):
                rows[tbl] = n
    except sqlite3.Error:
        pass
    return rows


def _count_rows(con: sqlite3.Connection, table: str, exact: bool = True) -> tuple:
    """返回 (行数, 是否精确)。

    实测（2026-11-01）：`SELECT COUNT(*) FROM stock_indicators_daily`（1,316 万行）只花 **0.21 s** ——
    SQLite 会挑最小的那个索引整段扫，而不是回表。所以默认就给精确值，别拿 MAX(rowid) 糊弄：
    该表 `INSERT OR REPLACE` 反复改写过，MAX(rowid) 是 3,003 万，真实行数 1,316 万，
    错 2.3 倍的行数会让人对"这张表到底有多大"产生完全错误的印象。

    只有 `--no-count` 时才退化成 MAX(rowid) 上界（快，但要在输出里标 *）。
    """
    if exact:
        try:
            return con.execute("SELECT COUNT(*) FROM %s" % _quote(table)).fetchone()[0], True
        except sqlite3.Error:
            return None, False
    try:
        n = con.execute("SELECT MAX(rowid) FROM %s" % _quote(table)).fetchone()[0]
    except sqlite3.Error:
        return None, False
    if n is None:
        return 0, True              # MAX(rowid) 为空 = 空表，行数就是 0
    return n, False                 # 上界：REPLACE/删行会让它偏离真值


def _quote(ident: str) -> str:
    """安全地把标识符塞进 SQL（表名来自 sqlite_master，仍然按规矩转义）。"""
    return '"' + str(ident).replace('"', '""') + '"'


# ──────────────────────────────────────────────────────────────────────────
# 索引列与冗余判定
# ──────────────────────────────────────────────────────────────────────────
def _index_columns(con: sqlite3.Connection, index: str) -> tuple[list, bool]:
    """返回 (列序列, 是否表达式/部分索引无法比较)。

    用 `PRAGMA index_xinfo` 而不是 `index_info`：前者带 DESC 与 COLLATE，
    `idx(a ASC)` 与 `idx(a DESC)` 是两个不同的东西，漏掉这两列会误判成"重复"。
    只收 key=1 的列（key=0 是自动附带的 rowid 定位列）。
    """
    cols = []
    try:
        for seqno, cid, name, desc, coll, key in con.execute(
                "PRAGMA index_xinfo(%s)" % _quote(index)):
            if not key:
                continue
            if cid < 0 or name is None:
                # 表达式索引（cid=-2）或纯 rowid 索引：列语义不可比，放弃判定
                return [], True
            cols.append({"col": name, "desc": bool(desc), "coll": (coll or "BINARY").upper()})
    except sqlite3.Error:
        return [], True
    return cols, False


def _is_prefix(short: list, long: list) -> bool:
    """short 是否为 long 的前缀（逐列比较 DESC 与 COLLATE，必须完全一致）。"""
    if not short or len(short) > len(long):
        return False
    for a, b in zip(short, long):
        if a["col"] != b["col"] or a["desc"] != b["desc"] or a["coll"] != b["coll"]:
            return False
    return True


def _partial_flags(con: sqlite3.Connection) -> dict:
    """{索引名: 是否部分索引（带 WHERE）}。

    `PRAGMA index_xinfo` 看不出 partial，必须走 `index_list` 的第 5 列。
    这条信息决定判定方向：部分索引顶替不了全量索引（反过来可以）。
    """
    out = {}
    for (tbl,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        try:
            for row in con.execute("PRAGMA index_list(%s)" % _quote(tbl)):
                out[row[1]] = bool(row[4])
        except sqlite3.Error:
            continue
    return out


def _same_cols_loosely(a: list, b: list) -> bool:
    """列名序列相同但 DESC/COLLATE 不同 —— 用来给出"看着像重复，其实不是"的说明。"""
    return [x["col"] for x in a] == [x["col"] for x in b]


def _mark_redundant(indexes: list) -> None:
    """就地给每个索引加上 `redundant` / `redundancy_reason`（只判定，不删）。

    规则见模块 docstring：A 的列序列是 B 的前缀 → A 冗余候选；
    A 是不可删的约束自动索引且与 B 完全同列 → 反过来把 B 标为候选。
    """
    for idx in indexes:
        idx["redundant"] = False
        idx["redundancy_reason"] = None

    # 按表分组比较（跨表谈前缀没有意义）
    by_table: dict = {}
    for idx in indexes:
        by_table.setdefault(idx["table"], []).append(idx)

    for _table, group in by_table.items():
        for a in group:
            if a["auto"]:
                # PK/UNIQUE 约束的自动索引不可 DROP —— 标成"冗余候选"只会误导人去删它。
                # 它与命名索引重复时，由下面 b 的那一轮把**命名索引**标为候选。
                continue
            if a["skip_reason"]:
                continue                      # 表达式/不可读索引：不参与判定
            for b in group:
                if a is b or b["skip_reason"]:
                    continue
                reason = _judge(a, b)
                if reason:
                    a["redundant"] = True
                    a["redundancy_reason"] = reason
                    break


def _judge(a: dict, b: dict) -> str | None:
    """判断 a 能否被 b 顶替；能则返回人类可读的理由，否则 None。"""
    a_cols, b_cols = a["columns"], b["columns"]
    if not a_cols or not b_cols:
        return None
    if b["partial"] and not a["partial"]:
        # 部分索引（带 WHERE）只覆盖子集，顶替不了全量索引 —— 反方向才成立
        return None
    if not _is_prefix(a_cols, b_cols):
        if _same_cols_loosely(a_cols, b_cols):
            # 列名相同但 DESC/COLLATE 不同：B 顶替不了 A（排序方向/比较规则不同）
            return None
        return None
    if len(a_cols) == len(b_cols):
        # 完全重复：自动索引（PK/UNIQUE 约束）删不掉，因此它胜出，把命名索引判为冗余
        if a["auto"] and not b["auto"]:
            return None                       # a 不可删，让 b 的循环去标 b
        if b["auto"] and not a["auto"]:
            return ("与主键/唯一约束的自动索引 %s 列序列完全相同"
                    "（该自动索引不可删，故本索引可删）" % b["name"])
        return "与索引 %s 列序列完全相同" % b["name"]
    return ("列序列 %s 是索引 %s 的前缀，组合查询可由后者覆盖"
            % (_col_txt(a_cols), b["name"]))


def _col_txt(cols: list) -> str:
    return "(" + ", ".join(c["col"] + (" DESC" if c["desc"] else "") for c in cols) + ")"


def _load_indexes(con: sqlite3.Connection, sizes: dict) -> list:
    """从 sqlite_master 读出所有索引（含自动索引），补上列序列与体积。"""
    partials = _partial_flags(con)
    out = []
    for name, tbl, sql in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'").fetchall():
        cols, bad = _index_columns(con, name)
        size = sizes.get(name, {})
        out.append({
            "name": name,
            "table": tbl,
            "auto": sql is None,             # sql IS NULL = PK/UNIQUE 自动索引，不可 DROP
            "unique": bool(sql and "UNIQUE" in sql.upper()),
            "partial": partials.get(name, False),
            "columns": cols,
            "columns_text": _col_txt(cols) if cols else "(不可读)",
            "bytes": size.get("bytes"),
            "pages": size.get("pages"),
            "skip_reason": "表达式索引/无法读取列定义，跳过冗余判定" if bad else None,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# 备份 / 缓存目录
# ──────────────────────────────────────────────────────────────────────────
def _dir_report(path: str) -> dict:
    """目录总占用 + 文件数 + 最老/最新 mtime。目录不存在时全部为 0/None（幂等）。"""
    info = {"path": path, "exists": os.path.isdir(path), "bytes": 0, "files": 0,
            "oldest": None, "newest": None}
    if not info["exists"]:
        return info
    for entry in os.scandir(path):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat()
        except OSError:
            continue
        info["bytes"] += st.st_size
        info["files"] += 1
        m = st.st_mtime
        if info["oldest"] is None or m < info["oldest"]:
            info["oldest"] = m
        if info["newest"] is None or m > info["newest"]:
            info["newest"] = m
    return info


def _cache_report() -> dict:
    """K 线缓存（viewer/data/kline_*.json）：数量、总占用、最老/最新，以及超期个数。

    为什么单独盯它：这是项目里唯一"每天新增、从不清理"的目录（实测 5,661 个 / 2.2 GB）。
    """
    files = glob.glob(os.path.join(CACHE_DIR, CACHE_PATTERN))
    now = time.time()
    stale_cut = now - STALE_DAYS * 86400
    total, oldest, newest, stale = 0, None, None, 0
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        total += st.st_size
        if oldest is None or st.st_mtime < oldest:
            oldest = st.st_mtime
        if newest is None or st.st_mtime > newest:
            newest = st.st_mtime
        if st.st_mtime < stale_cut:
            stale += 1
    return {"dir": CACHE_DIR, "pattern": CACHE_PATTERN, "files": len(files),
            "bytes": total, "oldest": oldest, "newest": newest,
            "stale_files": stale, "stale_after_days": STALE_DAYS}


def _ts(v) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(v)) if v else "-"


# ──────────────────────────────────────────────────────────────────────────
# 汇总
# ──────────────────────────────────────────────────────────────────────────
def build_report(db_path: str = DB_PATH, use_dbstat: bool = True, top: int = 15,
                 exact_counts: bool = True) -> dict:
    """采集全部指标，返回结构化报告（--json 直接输出它）。"""
    t0 = time.time()
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": {"path": db_path, "exists": os.path.exists(db_path)},
        "dbstat_used": False,
        "exact_counts": exact_counts,
        "sizes": {},          # 库/WAL/freelist
        "tables": [],         # Top N，按占用降序
        "indexes": [],        # 全部索引（含冗余判定）
        "redundant_indexes": [],
        "backups": [],
        "cache": {},
        "took_sec": 0.0,
    }
    if not report["db"]["exists"]:
        report["error"] = "库文件不存在：%s" % db_path
        return report

    con = _connect_ro(db_path)
    try:
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        report["dbstat_used"] = bool(use_dbstat and _dbstat_available(con))
        sizes = _objects_by_dbstat(con) if report["dbstat_used"] else _objects_by_pragma(con)

        db_bytes = _file_size(db_path)
        wal_bytes = _file_size(db_path + "-wal") or 0
        freelist_bytes = freelist * page_size
        report["sizes"] = {
            "page_size": page_size,
            "page_count": page_count,
            "page_count_bytes": page_count * page_size,
            "freelist_count": freelist,
            "freelist_bytes": freelist_bytes,
            "db_file_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            # 库文件 + WAL 才是"这堆东西实际占的盘"
            "disk_total_bytes": (db_bytes or 0) + wal_bytes,
            "live_bytes": page_count * page_size - freelist_bytes,
        }

        # ── 表：行数 + 占用 ──
        stat1_rows = _row_counts(con)
        tables = []
        stale = []
        for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall():
            size = sizes.get(name, {})
            n, exact = _count_rows(con, name, exact=exact_counts)
            tables.append({"name": name, "rows": n, "rows_exact": exact,
                           "bytes": size.get("bytes"), "pages": size.get("pages")})
            # 统计信息过期会直接害到查询计划（规划器按 stat1 估成本），顺手报出来。
            # 只在行数**精确**时比较：--no-count 下 n 是 MAX(rowid) 上界，拿它跟 stat1 比
            # 会把所有表都误报成"过期"（实测把 37 张表全标红，纯噪音）。
            s1 = stat1_rows.get(name)
            if exact_counts and exact and s1 is not None and n and (s1 > n * 2 or s1 * 2 < n):
                stale.append({"table": name, "stat1_rows": s1, "real_rows": n})
        tables.sort(key=lambda r: ((r["bytes"] if r["bytes"] is not None else -1),
                                   (r["rows"] or 0)), reverse=True)
        report["tables"] = tables[:top]
        report["stale_stats"] = stale

        # ── 索引：体积 + 冗余判定 ──
        indexes = _load_indexes(con, sizes)
        _mark_redundant(indexes)
        indexes.sort(key=lambda r: (r["bytes"] if r["bytes"] is not None else -1), reverse=True)
        report["indexes"] = indexes
        report["redundant_indexes"] = [i for i in indexes if i["redundant"]]
    finally:
        con.close()

    report["backups"] = [_dir_report(d) for d in BACKUP_DIRS]
    report["cache"] = _cache_report()
    report["took_sec"] = round(time.time() - t0, 2)
    return report


def render_text(report: dict, top: int = 15) -> str:
    """人读版。列宽固定、单位统一，扫一眼就能看出"钱花在哪"。"""
    lines = []

    def add(text: str = "") -> None:
        lines.append(text)
    add("=" * 78)
    add("存储体检报告  %s  （耗时 %.1fs%s）" % (
        report["generated_at"], report["took_sec"],
        "" if report["dbstat_used"] else "，未用 dbstat：逐对象体积为 n/a"))
    add("=" * 78)
    if report.get("error"):
        add("错误：%s" % report["error"])
        return "\n".join(lines)

    s = report["sizes"]
    add()
    add("【总账】")
    add("  库文件        %12s   %s" % (_human(s["db_file_bytes"]), report["db"]["path"]))
    add("  WAL           %12s   （未落盘的增量，重启/checkpoint 后归零）" % _human(s["wal_bytes"]))
    add("  合计占盘      %12s   ← 这是 `du -h` 看到的数" % _human(s["disk_total_bytes"]))
    add("  页大小×页数   %s × %d = %s" % (s["page_size"], s["page_count"],
                                         _human(s["page_count_bytes"])))
    add("  freelist      %d 页 = %s（已回收但未归还 OS）"
        % (s["freelist_count"], _human(s["freelist_bytes"])))
    add("  有效数据      %s" % _human(s["live_bytes"]))
    if s["freelist_bytes"]:
        add()
        add("  为什么删了东西 `du` 不变：SQLite 删表/删索引只是把页挂到 freelist 上复用，")
        add("  并不把字节还给文件系统。要让磁盘真的变小必须 VACUUM —— 它会重写整个库、")
        add("  需要约 1 倍库大小的额外空间并长时间锁库，所以本工具**不**自动做。")

    add()
    add("【表占用 Top %d】%s" % (top, "" if report["dbstat_used"] else "（无 dbstat，体积不可测）"))
    add("  %s %s %s  %s" % (_lj("表", 34), _rj("占用", 10), _rj("页数", 9), _rj("行数", 16)))
    add("  " + "-" * 72)
    for t in report["tables"]:
        rows_txt = _num(t["rows"]) + ("" if t["rows_exact"] else "*")
        add("  %s %s %s  %s" % (
            _lj(t["name"][:34], 34), _rj(_human(t["bytes"]), 10),
            _rj("-" if t["pages"] is None else "{:,}".format(t["pages"]), 9),
            _rj(rows_txt, 16)))
    if report.get("exact_counts"):
        add("  行数为 SELECT COUNT(*)，精确值（SQLite 扫最小索引，1,316 万行约 0.2s）")
    else:
        add("  *. --no-count 模式：行数取 MAX(rowid) 上界（REPLACE/删行会让它偏大）")

    add()
    add("【索引占用 + 冗余判定】")
    add("  %s %s %s  %s" % (_lj("索引", 44), _lj("所属表", 26), _rj("占用", 10), "判定"))
    add("  " + "-" * 100)
    small_cnt = 0
    for i in report["indexes"]:
        big = (i["bytes"] or 0) >= 1048576
        if not i["redundant"] and not big:
            small_cnt += 1
            continue
        tag = "冗余候选" if i["redundant"] else ("自动" if i["auto"] else "在用")
        add("  %s %s %s  %s" % (
            _lj(i["name"][:44], 44), _lj(i["table"][:26], 26), _rj(_human(i["bytes"]), 10), tag))
        if i["redundant"]:
            add("      └ %s；列 %s" % (i["redundancy_reason"], i["columns_text"]))
    if small_cnt:
        add("  （另有 %d 个 < 1MB 的普通索引未列出，--json 可看全量）" % small_cnt)
    if report["redundant_indexes"]:
        add()
        add("  ▶ 冗余索引 %d 个，可回收 %s（只判定，不自动删）" % (
            len(report["redundant_indexes"]),
            _human(sum(i["bytes"] or 0 for i in report["redundant_indexes"]))))
    else:
        add()
        add("  ▶ 未发现冗余索引")

    stale = report.get("stale_stats") or []
    if stale:
        add()
        add("【统计信息过期】sqlite_stat1 与真实行数差 2 倍以上（会让规划器估错成本）")
        for s in stale[:10]:
            add("  %s stat1 记 %s，实际 %s —— 建议跑一次 ANALYZE"
                % (_lj(s["table"][:32], 32), _num(s["stat1_rows"]), _num(s["real_rows"])))

    add()
    add("【备份目录】")
    for b in report["backups"]:
        if not b["exists"]:
            add("  %-40s 不存在" % b["path"])
            continue
        add("  %-40s %10s  %4d 个文件  最老 %s / 最新 %s" % (
            b["path"], _human(b["bytes"]), b["files"], _ts(b["oldest"]), _ts(b["newest"])))

    add()
    c = report["cache"]
    add("【冷数据：K 线缓存】%s" % os.path.join("viewer", "data", c["pattern"]))
    add("  %d 个文件 / %s  最老 %s / 最新 %s" % (
        c["files"], _human(c["bytes"]), _ts(c["oldest"]), _ts(c["newest"])))
    if c["stale_files"]:
        add("  ▶ 其中 %d 个超过 %d 天未变动（退出码 %d）"
            % (c["stale_files"], c["stale_after_days"], EXIT_STALE_CACHE))
    else:
        add("  ▶ 无超期缓存（阈值 %d 天）" % c["stale_after_days"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="存储体检报告（表/索引占用 + 冗余索引 + 冷数据）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机读）")
    ap.add_argument("--no-dbstat", action="store_true",
                    help="跳过全库 dbstat 扫描（秒回；逐对象体积显示 n/a）")
    ap.add_argument("--no-count", action="store_true",
                    help="行数改用 MAX(rowid) 上界（更快但不精确，输出会标 *）")
    ap.add_argument("--top", type=int, default=15, help="表占用 Top N（默认 15）")
    ap.add_argument("--db", default=DB_PATH, help="库文件路径（默认 viewer/app.db）")
    args = ap.parse_args(argv)

    report = build_report(db_path=args.db, use_dbstat=not args.no_dbstat, top=args.top,
                          exact_counts=not args.no_count)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, top=args.top))

    if report.get("error"):
        return 1
    if report["redundant_indexes"]:
        return EXIT_REDUNDANT_INDEX
    if report["cache"].get("stale_files"):
        return EXIT_STALE_CACHE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
