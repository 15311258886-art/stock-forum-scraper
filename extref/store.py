"""extref_* 表：建表 / 幂等导入 / 查询 / 胜率聚合。

不持有连接：全部函数接收调用方的 sqlite3.Connection（沿用 viewer.db.Database 的连接池），
与 geo/store.py 同一做法——避免同一 WAL 库出现两套连接管理。

⚠️ 隔离纪律（本模块唯一必须遵守的硬约束）：
  本文件**只允许**出现 extref_sources / extref_signals 两张表；
  不得出现任何 JOIN、外键、或对 posts/trades/players/lhb_stocks/geo_events 的读写。
  与既有数据的一切关联都靠"约定的字段值"（如 symbol 是 6 位 A 股代码），不靠外键。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from extref import SEED_SIGNALS, SEED_SOURCES
from extref import taxonomy as tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS extref_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_key     TEXT UNIQUE,          -- 稳定 id，重跑导入按它 upsert
    name        TEXT,                 -- 显示名
    kind        TEXT,                 -- software/mcp/rank/community/research/seat
    tier        TEXT,                 -- A/B/C/D
    vendor      TEXT,                 -- 运营主体（用于核执照）
    licensed    INTEGER DEFAULT 0,    -- 是否持牌（1/0/-1 未知）
    access      TEXT,                 -- mcp/public_page/manual/login/blocked
    has_export  INTEGER DEFAULT 0,    -- 是否支持导出
    has_history INTEGER DEFAULT 0,    -- 是否有历史可追溯（回测前提）
    status      TEXT DEFAULT 'pending', -- live/pending/blocked
    url         TEXT,
    note        TEXT,
    role        TEXT,                 -- 作用：这个信源是干什么的（详情抽屉用）
    value       TEXT,                 -- 价值：能贡献什么信号 / 可信度与局限
    howto       TEXT,                 -- 接入路径：怎么才能真的「接住」它
    order_n     INTEGER DEFAULT 0,    -- 展示排序（数值小的靠前）
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_extref_src_tier ON extref_sources(tier, order_n);

CREATE TABLE IF NOT EXISTS extref_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT UNIQUE,          -- 稳定 id：<src_key>-<pub_date>-<symbol>-<n>
    src_key     TEXT,                 -- 信源主键（约定关联，非外键）
    src_name    TEXT,                 -- 冗余显示名（刻意冗余，避免 JOIN）
    src_kind    TEXT,
    src_tier    TEXT,
    pub_date    TEXT,                 -- 信号发布日 YYYY-MM-DD
    symbol      TEXT,                 -- 6 位 A 股代码（非 A 股留空）
    symbol_name TEXT,
    sector      TEXT,
    direction   TEXT,                 -- 看多/看空/中性
    strength    INTEGER DEFAULT 1,    -- 1-5
    reason      TEXT,                 -- 推荐理由 / 逻辑
    claim       TEXT,                 -- 原始口径摘要（尽量贴原文）
    url         TEXT,                 -- 可点外链（证据）
    hardness    TEXT,                 -- hard/soft
    pnl_t1      REAL,                 -- 信号后 1 日收益%   —— 回测灵魂字段
    pnl_t5      REAL,                 -- 5 日
    pnl_t10     REAL,                 -- 10 日
    pnl_status  TEXT DEFAULT 'pending', -- pending(未到期)/done(已回填)/na(不可回测)
    tags        TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_extref_sig_date ON extref_signals(pub_date DESC);
CREATE INDEX IF NOT EXISTS ix_extref_sig_src  ON extref_signals(src_key);
CREATE INDEX IF NOT EXISTS ix_extref_sig_sym  ON extref_signals(symbol);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 兼容旧表：2026-09-27 给 extref_sources 补 role/value/howto 三列（详情抽屉用）。
    # CREATE TABLE IF NOT EXISTS 不会改已存在的表，故用 PRAGMA 探测后 ALTER。
    have = {r[1] for r in conn.execute("PRAGMA table_info(extref_sources)")}
    for col in ("role", "value", "howto"):
        if col not in have:
            conn.execute(f"ALTER TABLE extref_sources ADD COLUMN {col} TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _csv(v):
    if v is None or v == "" or v == []:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [s for s in str(v).split(",") if s]


def _load(path) -> list:
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError:
        return []
    items = data.get("sources") if isinstance(data, dict) else data
    if items is None and isinstance(data, dict):
        items = data.get("signals")
    return items or []


# ---------------- 信源档案 ----------------

def import_sources(conn: sqlite3.Connection, path=None) -> dict:
    """幂等导入信源档案（按 src_key upsert）。返回 {total, inserted, updated}。"""
    src = path or SEED_SOURCES
    items = _load(src)
    now = _now()
    ins = upd = 0
    for s in items:
        key = (s.get("src_key") or "").strip()
        if not key:
            continue
        row = {
            "src_key": key,
            "name": s.get("name", ""),
            "kind": s.get("kind", "software"),
            "tier": s.get("tier", "C"),
            "vendor": s.get("vendor", ""),
            "licensed": int(s.get("licensed", 0) or 0),
            "access": s.get("access", "blocked"),
            "has_export": int(1 if s.get("has_export") else 0),
            "has_history": int(1 if s.get("has_history") else 0),
            "status": s.get("status", "pending"),
            "url": s.get("url", ""),
            "note": s.get("note", ""),
            "role": s.get("role", ""),
            "value": s.get("value", ""),
            "howto": s.get("howto", ""),
            "order_n": int(s.get("order_n", 0) or 0),
            "updated_at": now,
        }
        exists = conn.execute("SELECT id FROM extref_sources WHERE src_key=?", (key,)).fetchone()
        if exists:
            sets = ",".join(f"{k}=?" for k in row)
            conn.execute(f"UPDATE extref_sources SET {sets} WHERE src_key=?",
                         list(row.values()) + [key])
            upd += 1
        else:
            cols = ",".join(row.keys())
            ph = ",".join("?" for _ in row)
            conn.execute(f"INSERT INTO extref_sources ({cols}) VALUES ({ph})", list(row.values()))
            ins += 1
    conn.commit()
    return {"total": len(items), "inserted": ins, "updated": upd, "file": str(src)}


# ---------------- 信号 ----------------

def import_signals(conn: sqlite3.Connection, path=None) -> dict:
    """幂等导入信号（按 sid upsert）。返回 {total, inserted, updated}。"""
    src = path or SEED_SIGNALS
    items = _load(src)
    now = _now()
    ins = upd = 0
    for s in items:
        sid = (s.get("sid") or "").strip()
        if not sid:
            continue
        row = {
            "sid": sid,
            "src_key": s.get("src_key", ""),
            "src_name": s.get("src_name", ""),
            "src_kind": s.get("src_kind", ""),
            "src_tier": s.get("src_tier", ""),
            "pub_date": s.get("pub_date", ""),
            "symbol": (s.get("symbol") or "").strip(),
            "symbol_name": s.get("symbol_name", ""),
            "sector": s.get("sector", ""),
            "direction": s.get("direction", "看多"),
            "strength": int(s.get("strength") or 1),
            "reason": s.get("reason", ""),
            "claim": s.get("claim", ""),
            "url": s.get("url", ""),
            "hardness": s.get("hardness", "soft"),
            "pnl_t1": s.get("pnl_t1"),
            "pnl_t5": s.get("pnl_t5"),
            "pnl_t10": s.get("pnl_t10"),
            "pnl_status": s.get("pnl_status", "pending"),
            "tags": ",".join(_csv(s.get("tags"))),
            "updated_at": now,
        }
        exists = conn.execute("SELECT id FROM extref_signals WHERE sid=?", (sid,)).fetchone()
        if exists:
            # ⚠️ 保护回测结果：seed 里 pnl 恒为 pending/空，若直接整行覆盖，
            # 会把回测器（backtest.py）已回填的 done/na 又冲掉。故当 incoming 未带
            # 任何收益数据时，从 UPDATE 集合里剔除 pnl_* 四列，只更新其余字段。
            inc_pending = (s.get("pnl_status", "pending") or "pending") == "pending"
            inc_no_pnl = all(s.get(k) is None for k in ("pnl_t1", "pnl_t5", "pnl_t10"))
            upd_row = dict(row)
            if inc_pending and inc_no_pnl:
                for k in ("pnl_t1", "pnl_t5", "pnl_t10", "pnl_status"):
                    upd_row.pop(k, None)
            sets = ",".join(f"{k}=?" for k in upd_row)
            conn.execute(f"UPDATE extref_signals SET {sets} WHERE sid=?",
                         list(upd_row.values()) + [sid])
            upd += 1
        else:
            cols = ",".join(row.keys())
            ph = ",".join("?" for _ in row)
            conn.execute(f"INSERT INTO extref_signals ({cols}) VALUES ({ph})", list(row.values()))
            ins += 1
    conn.commit()
    return {"total": len(items), "inserted": ins, "updated": upd, "file": str(src)}


def _shape_src(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["kind_label"] = tx.kind_label(d.get("kind"))
    d["tier_label"] = tx.tier_label(d.get("tier"))
    d["tier_color"] = tx.tier_color(d.get("tier"))
    d["tier_desc"] = tx.tier_desc(d.get("tier"))
    d["access_label"] = tx.access_label(d.get("access"))
    d["status_label"] = tx.status_label(d.get("status"))
    d["licensed"] = int(d.get("licensed") or 0)
    return d


def _shape_sig(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["hardness_label"] = tx.hardness_label(d.get("hardness"))
    d["tier_label"] = tx.tier_label(d.get("src_tier"))
    d["tier_color"] = tx.tier_color(d.get("src_tier"))
    d["kind_label"] = tx.kind_label(d.get("src_kind"))
    d["strength_text"] = tx.strength_text(d.get("strength"))
    d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
    return d


def query_sources(conn: sqlite3.Connection, tier: str | None = None,
                  kind: str | None = None, status: str | None = None,
                  limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM extref_sources WHERE 1=1"
    args: list = []
    if tier and tier != "all":
        sql += " AND tier=?"
        args.append(tier)
    if kind and kind != "all":
        sql += " AND kind=?"
        args.append(kind)
    if status and status != "all":
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY order_n ASC, tier ASC, name ASC LIMIT ?"
    args.append(int(limit))
    return [_shape_src(r) for r in conn.execute(sql, args)]


def query_signals(conn: sqlite3.Connection, src_kind: str | None = None,
                  tier: str | None = None, direction: str | None = None,
                  hardness: str | None = None, days: int | None = None,
                  limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM extref_signals WHERE 1=1"
    args: list = []
    if src_kind and src_kind != "all":
        sql += " AND src_kind=?"
        args.append(src_kind)
    if tier and tier != "all":
        sql += " AND src_tier=?"
        args.append(tier)
    if direction and direction != "all":
        sql += " AND direction=?"
        args.append(direction)
    if hardness and hardness != "all":
        sql += " AND hardness=?"
        args.append(hardness)
    if days:
        from datetime import timedelta
        edge = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        sql += " AND pub_date>=?"
        args.append(edge)
    sql += " ORDER BY pub_date DESC, strength DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [_shape_sig(r) for r in conn.execute(sql, args)]


def get_source(conn: sqlite3.Connection, src_key: str) -> dict | None:
    """取单个信源档案（详情抽屉用）。仍只查 extref_sources 单表。"""
    row = conn.execute("SELECT * FROM extref_sources WHERE src_key=?", (src_key,)).fetchone()
    return _shape_src(row) if row else None


def get_signal(conn: sqlite3.Connection, sid: str) -> dict | None:
    """取单条信号（详情抽屉用）。仍只查 extref_signals 单表。"""
    row = conn.execute("SELECT * FROM extref_signals WHERE sid=?", (sid,)).fetchone()
    return _shape_sig(row) if row else None


def signals_of_source(conn: sqlite3.Connection, src_key: str, limit: int = 200) -> list[dict]:
    """该信源已入库的信号（详情抽屉用）。"""
    rows = conn.execute(
        "SELECT * FROM extref_signals WHERE src_key=? ORDER BY pub_date DESC, id DESC LIMIT ?",
        (src_key, int(limit))).fetchall()
    return [_shape_sig(r) for r in rows]


def upsert_signal(conn: sqlite3.Connection, row: dict) -> str:
    """单条信号 upsert（采集器 / 回测器回填用）。返回 'inserted' 或 'updated'。

    只写 extref_signals 单表；按 sid 幂等。缺失字段留空、收益字段**不写 0**。
    """
    sid = (row.get("sid") or "").strip()
    if not sid:
        raise ValueError("sid required")
    full = {
        "sid": sid, "src_key": row.get("src_key", ""), "src_name": row.get("src_name", ""),
        "src_kind": row.get("src_kind", ""), "src_tier": row.get("src_tier", ""),
        "pub_date": row.get("pub_date", ""), "symbol": (row.get("symbol") or "").strip(),
        "symbol_name": row.get("symbol_name", ""), "sector": row.get("sector", ""),
        "direction": row.get("direction", "中性"), "strength": int(row.get("strength") or 1),
        "reason": row.get("reason", ""), "claim": row.get("claim", ""), "url": row.get("url", ""),
        "hardness": row.get("hardness", "soft"), "pnl_t1": row.get("pnl_t1"),
        "pnl_t5": row.get("pnl_t5"), "pnl_t10": row.get("pnl_t10"),
        "pnl_status": row.get("pnl_status", "pending"),
        "tags": ",".join(_csv(row.get("tags"))), "updated_at": _now(),
    }
    exists = conn.execute("SELECT id FROM extref_signals WHERE sid=?", (sid,)).fetchone()
    if exists:
        # ⚠️ 保护回测结果（与 import_signals 同口径）：采集器 upsert 时**不携带真实收益**
        # （rec.pnl_status 恒为 "pending"、pnl_* 为 None），若整行覆盖会把 backtest.py 已回填的
        # done/na 又冲回 pending。故 UPDATE 分支从集合里剔除 pnl 四列，只更新其它字段。
        upd_full = dict(full)
        for k in ("pnl_t1", "pnl_t5", "pnl_t10", "pnl_status"):
            upd_full.pop(k, None)
        sets = ",".join(f"{k}=?" for k in upd_full)
        conn.execute(f"UPDATE extref_signals SET {sets} WHERE sid=?", list(upd_full.values()) + [sid])
        conn.commit()
        return "updated"
    cols = ",".join(full.keys())
    ph = ",".join("?" for _ in full)
    conn.execute(f"INSERT INTO extref_signals ({cols}) VALUES ({ph})", list(full.values()))
    conn.commit()
    return "inserted"


def pending_signals(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """待回填信号（回测器输入）。只读 extref_signals 单表。"""
    rows = conn.execute(
        "SELECT * FROM extref_signals WHERE pnl_status='pending' AND symbol<>'' "
        "ORDER BY pub_date ASC, id ASC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def backfill_pnl(conn: sqlite3.Connection, sid: str, t1, t5, t10, status: str = "done") -> None:
    """回填某条信号的收益字段（回测器用）。收益为 None 表示不可测，status 记 na。"""
    conn.execute(
        "UPDATE extref_signals SET pnl_t1=?, pnl_t5=?, pnl_t10=?, pnl_status=?, updated_at=? "
        "WHERE sid=?", (t1, t5, t10, status, _now(), sid))
    conn.commit()


def signal_rank(conn: sqlite3.Connection) -> list[dict]:
    """信源胜率榜：按 src_key 聚合**已回填**信号（pnl_status='done'）。

    只用 extref_signals 单表，Python 内聚合，不做 JOIN。
    诚实口径：样本 <5 条不给胜率（标"样本不足"），未回填的不计入。
    """
    buckets: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT src_key, src_name, src_tier, src_kind, direction, symbol, pub_date,"
        "       pnl_t1, pnl_t5, pnl_t10, pnl_status FROM extref_signals"
    ):
        b = buckets.setdefault(r["src_key"], {
            "src_key": r["src_key"], "src_name": r["src_name"], "src_tier": r["src_tier"],
            "src_kind": r["src_kind"], "signals_n": 0, "done_n": 0, "pending_n": 0,
            "win_t1": 0, "win_t5": 0, "win_t10": 0, "sum_t5": 0.0, "n_t5": 0,
            "last_date": "",
        })
        b["signals_n"] += 1
        if (r["pub_date"] or "") > b["last_date"]:
            b["last_date"] = r["pub_date"] or ""
        if (r["pnl_status"] or "") == "done":
            b["done_n"] += 1
            if r["pnl_t1"] is not None and r["pnl_t1"] > 0:
                b["win_t1"] += 1
            if r["pnl_t5"] is not None:
                b["n_t5"] += 1
                b["sum_t5"] += float(r["pnl_t5"])
                if r["pnl_t5"] > 0:
                    b["win_t5"] += 1
            if r["pnl_t10"] is not None and r["pnl_t10"] > 0:
                b["win_t10"] += 1
        else:
            b["pending_n"] += 1
    out = []
    for b in buckets.values():
        ok = b["done_n"] >= 5 and b["n_t5"] > 0
        b["rankable"] = bool(ok)
        b["win_rate_5"] = round(b["win_t5"] / b["n_t5"] * 100, 1) if b["n_t5"] else None
        b["avg_ret_5"] = round(b["sum_t5"] / b["n_t5"], 2) if b["n_t5"] else None
        # 诚实口径（2026-09-27 调整）：未达 5 条门槛时**仍显示已算出的数字**，
        # 但明确标注「未达门槛 · 仅供参考」——既不给假数，也不让用户只看到"样本不足"而无从判断。
        if ok:
            b["sample_note"] = "已达门槛（已回填 %d 条）" % b["done_n"]
        elif b["n_t5"] == 0:
            b["sample_note"] = "样本不足（尚无已回填，需 ≥5 条）"
        else:
            b["sample_note"] = "样本不足（已回填 %d 条 · 未达 5 条门槛 · 数字仅供参考）" % b["done_n"]
        b["tier_label"] = tx.tier_label(b.get("src_tier"))
        b["tier_color"] = tx.tier_color(b.get("src_tier"))
        b["kind_label"] = tx.kind_label(b.get("src_kind"))
        out.append(b)
    out.sort(key=lambda x: (-int(x["rankable"]), -(x["win_rate_5"] or -1), -x["done_n"]))
    return out


# 「外部荐股验证报告」的样本及格线（2026-10-02 王总方案 C 定的硬规则）。
# 低于这条线，页面上的结论一律只能标「参考」，不许降级成「可作依据」。
VERDICT_MIN_SAMPLE = 20
_WINDOW_LABELS = (("t1", "T+1"), ("t5", "T+5"), ("t10", "T+10"))


def signal_verdict(conn: sqlite3.Connection) -> dict:
    """外部荐股验证报告：**只拿已回填真实行情**的信号，算出一个能指导判断的结论。

    硬性口径（2026-10-02）：
      · 只用 pnl_status='done' 且 pnl_tN 非空的样本；未走完窗口的一律不进统计、不写 0；
      · 逐窗口（T+1/5/10）算平均收益与胜率，再按这些真数字拼结论，**不写死一句漂亮话**；
      · 样本 <20 条 → grade='reference'（只能参考）；≥20 条 → grade='usable'（可作依据）。
    返回：{done_n, loss_n, windows, by_source, best, worst, grade, grade_note, verdict, detail}
    """
    _cur = conn.execute(
        "SELECT sid, src_key, src_name, src_tier, src_kind, pub_date, symbol, symbol_name,"
        "       direction, pnl_t1, pnl_t5, pnl_t10, pnl_status"
        " FROM extref_signals ORDER BY pub_date DESC, id DESC")
    # 不依赖 conn.row_factory（server 侧是 sqlite3.Row、命令行直连是 tuple），
    # 统一按列名取，两种连接都能跑通。
    _cols = [d[0] for d in _cur.description]
    _rows = []
    for _r in _cur.fetchall():
        _d = dict(zip(_cols, _r))
        _d["t1"] = _d.get("pnl_t1")
        _d["t5"] = _d.get("pnl_t5")
        _d["t10"] = _d.get("pnl_t10")
        _rows.append(_d)
    rows = _rows

    detail: list[dict] = []
    by_src: dict = {}
    acc = {k: {"n": 0, "sum": 0.0, "win": 0} for k, _ in _WINDOW_LABELS}
    loss_n = 0

    for r in rows:
        if (r["pnl_status"] or "") != "done":
            continue                      # 未回填 / na 一律不进统计
        b = by_src.setdefault((r["src_key"] or "") or (r["src_name"] or ""), {
            "src_key": r["src_key"] or "", "src_name": r["src_name"] or "",
            "src_tier": r["src_tier"] or "", "done_n": 0,
            "windows": {k: {"n": 0, "sum": 0.0, "win": 0} for k, _ in _WINDOW_LABELS},
        })
        item = {
            "sid": r["sid"], "src_name": r["src_name"] or "", "pub_date": r["pub_date"] or "",
            "symbol": r["symbol"] or "", "symbol_name": r["symbol_name"] or "",
            "direction": r["direction"] or "",
        }
        seen_neg = False
        for k, _ in _WINDOW_LABELS:
            v = r[k]
            if v is None:
                item[k] = None
                continue
            fv = float(v)
            item[k] = fv
            if fv > 0:
                acc[k]["win"] += 1
            else:
                seen_neg = True
            acc[k]["n"] += 1
            acc[k]["sum"] += fv
            b["windows"][k]["n"] += 1
            b["windows"][k]["sum"] += fv
            if fv > 0:
                b["windows"][k]["win"] += 1
        b["done_n"] += 1
        if seen_neg:
            loss_n += 1
        detail.append(item)

    windows: dict = {}
    for k, lbl in _WINDOW_LABELS:
        a = acc[k]
        windows[k] = {
            "label": lbl, "n": a["n"],
            "avg": round(a["sum"] / a["n"], 2) if a["n"] else None,
            "win_rate": round(a["win"] / a["n"] * 100, 1) if a["n"] else None,
        }
    by_source = []
    for b in by_src.values():
        w = {}
        for k, lbl in _WINDOW_LABELS:
            a = b["windows"][k]
            w[k] = {"n": a["n"],
                    "avg": round(a["sum"] / a["n"], 2) if a["n"] else None,
                    "win_rate": round(a["win"] / a["n"] * 100, 1) if a["n"] else None}
        by_source.append({"src_key": b["src_key"], "src_name": b["src_name"],
                          "src_tier": b["src_tier"], "done_n": b["done_n"], "windows": w})

    done_n = len(detail)
    # 有数的窗口里比最好/最差（没有样本的不参与）
    usable = [(k, windows[k]) for k, _ in _WINDOW_LABELS if windows[k]["n"] > 0]
    best = max(usable, key=lambda x: (x[1]["avg"] if x[1]["avg"] is not None else -1e9)) if usable else None
    worst = min(usable, key=lambda x: (x[1]["avg"] if x[1]["avg"] is not None else 1e9)) if usable else None

    grade = "usable" if done_n >= VERDICT_MIN_SAMPLE else "reference"
    if done_n == 0:
        verdict = ("还没有任何一条外部榜单选股的真实收益被回填——这时候给结论就是编。"
                   "跑一次「继续采集」把样本拉出来，本页会自动出判断；在此之前不显示任何收益数字。")
    else:
        # 只把均值摆进句子（胜率数字由 KPI / 对照表呈现），避免一句话啰嗦到三行以上，
        # 也避免和前端小数取整口径打架（前端统一按 1 位小数显示）。
        parts = [f"{windows[k]['label']} {windows[k]['avg']:+.2f}%"
                 for k, _ in _WINDOW_LABELS if windows[k]["n"]]
        head = f"拿这 {done_n} 条已回填的真实行情核对外部榜单推的票：{' / '.join(parts)}。"
        pos = [x for x in usable if (x[1]["avg"] or 0) > 0]
        neg = [x for x in usable if (x[1]["avg"] or 0) < 0]
        if pos and neg:
            tail = (f"{pos[0][1]['label']} 还是赚的（{pos[0][1]['avg']:+.2f}%），"
                    f"但 {neg[0][1]['label']} 已经转亏（{neg[0][1]['avg']:+.2f}%）——"
                    f"短窗口吃的是情绪，拉长才是真金。")
        elif neg and not pos:
            tail = (f"三个窗口一段都赚不出来——最好的 {best[1]['label']} 也只有 "
                    f"{best[1]['avg']:+.2f}%，最差的 {worst[1]['label']} 已经掉到 "
                    f"{worst[1]['avg']:+.2f}%。跟榜买的票，在这批样本里没有一段拿得住。")
        elif pos and not neg:
            tail = (f"三个窗口都是赚的，最好的是 {best[1]['label']} {best[1]['avg']:+.2f}%；"
                    f"但样本过少时「都赚」往往只是运气，别急着当依据。")
        else:
            tail = "收益分布不足以下结论。"
        verdict = head + tail

    if done_n >= VERDICT_MIN_SAMPLE:
        grade_note = f"样本 {done_n} 条已达 {VERDICT_MIN_SAMPLE} 条及格线，以上可作依据。"
    elif done_n == 0:
        grade_note = f"样本 0 条，本页不构成任何判断。"
    else:
        grade_note = (f"样本只有 {done_n} 条、未达 {VERDICT_MIN_SAMPLE} 条及格线，"
                      f"以上只作参考，不作决策依据；再采 {VERDICT_MIN_SAMPLE - done_n} 条才会升级为「可作依据」。")

    return {
        "done_n": done_n,
        "loss_n": loss_n,
        "min_sample": VERDICT_MIN_SAMPLE,
        "gap": max(0, VERDICT_MIN_SAMPLE - done_n),
        "grade": grade,
        "grade_note": grade_note,
        "windows": windows,
        "by_source": by_source,
        "best": {"key": best[0], "label": best[1]["label"], "avg": best[1]["avg"]} if best else None,
        "worst": {"key": worst[0], "label": worst[1]["label"], "avg": worst[1]["avg"]} if worst else None,
        "verdict": verdict,
        "detail": detail,
    }


def stats(conn: sqlite3.Connection) -> dict:
    """信源/信号计数 + 分档分布 + 回测完成度，供面板顶部与回归断言用。"""
    src_total = conn.execute("SELECT COUNT(*) FROM extref_sources").fetchone()[0]
    sig_total = conn.execute("SELECT COUNT(*) FROM extref_signals").fetchone()[0]
    by_tier = {r[0]: r[1] for r in conn.execute(
        "SELECT tier, COUNT(*) FROM extref_sources GROUP BY tier")}
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM extref_sources GROUP BY kind")}
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM extref_sources GROUP BY status")}
    sig_by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT src_kind, COUNT(*) FROM extref_signals GROUP BY src_kind")}
    sig_by_dir = {r[0]: r[1] for r in conn.execute(
        "SELECT direction, COUNT(*) FROM extref_signals GROUP BY direction")}
    done = conn.execute("SELECT COUNT(*) FROM extref_signals WHERE pnl_status='done'").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM extref_signals WHERE pnl_status='pending'").fetchone()[0]
    lic = conn.execute("SELECT COUNT(*) FROM extref_sources WHERE licensed=1").fetchone()[0]
    rng = conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM extref_signals").fetchone()
    return {
        "sources_total": src_total,
        "signals_total": sig_total,
        "licensed_n": lic,
        "by_tier": {t["key"]: by_tier.get(t["key"], 0) for t in tx.TIERS},
        "by_kind": {k["key"]: by_kind.get(k["key"], 0) for k in tx.KINDS},
        "by_status": {s["key"]: by_status.get(s["key"], 0) for s in tx.STATUS},
        "signals_by_kind": sig_by_kind,
        "signals_by_direction": sig_by_dir,
        "pnl_done": done,
        "pnl_pending": pend,
        "date_from": rng[0] if rng else None,
        "date_to": rng[1] if rng else None,
        "tiers": tx.TIERS,
        "kinds": tx.KINDS,
        "access_options": tx.ACCESS,
        "status_options": tx.STATUS,
        "hardness_options": tx.HARDNESS,
        "red_line": tx.RED_LINE,
        "updated_at": (conn.execute("SELECT MAX(updated_at) FROM extref_signals").fetchone() or [None])[0],
    }
