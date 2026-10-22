# -*- coding: utf-8 -*-
"""watchlib —— 自选 / 关注池 + 到价提醒（隔离模块）

设计纪律（与 extref/ 同一套）：
  * 独立三张表 watch_groups / watch_items / watch_alerts，**零 JOIN、零外键**，
    不与任何既有业务表（posts / players / positions / extref_* …）发生关联；
  * 本模块**只做存储 + 规则判定，不做任何网络请求**——最新价由调用方（server）批量取好后，
    通过 prices={code: price} 注入 check_alerts()，因此模块可在无网络环境下完整单测；
  * 诚实口径：取不到价一律 None（前端显示 «—»），**绝不写 0**；
    目标价 / 提醒方向 / 开关任一缺失即不参与触发，不臆造。

三张表：
  watch_groups  分组（**名字可自定义**；默认给几组，用户可改名 / 增删）
  watch_items   池中标的（股票或基金；目标价、关注度、板块、备注、加入时价格…）
  watch_alerts  触发记录（一条 = 一次「到价」事件；支持已读 / 未读）

多对多分组（2026-10-14 改造）：
  一只标的可同时在多个分组。主存储 = watch_item_groups(item_id, group_id, sort_order)；
  watch_items.group_id **保留为主组**（兼容旧读法），新逻辑一律以关联表为准：
    · list_items 附加 groups（数组）/ n_groups；
    · list_groups 的 n_items 按 DISTINCT item_id 去重计数；
    · 增 / 改 / 移出分组走 set_groups / add_to_group / remove_from_group；
    · 组内排序走 reorder_in_group（sort_order = 10,20,30…）。
  ensure_schema 内一次性幂等迁移：历史单 group_id → 关联表。

对外函数：
  ensure_schema(conn)                     建表 + 多对多迁移（幂等）
  seed_default_groups(conn)               无分组时补默认分组
  list_groups / add_group / rename_group / remove_group
  list_items / add_item / get_item / update_item / remove_item
  item_group_ids(conn, iid)               标的的全部关联组 id（按组 sort_order）
  set_groups / add_to_group / remove_from_group / reorder_in_group
  check_alerts(conn, prices, now)         到价判定 → 生成 watch_alerts，返回触发清单
  list_alerts / count_unread / mark_alert / mark_all_read / clear_alerts
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


# ---------------------------------------------------------------- 基础工具
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _to_float(v):
    """空 / 占位符 → None，其余转 float（脏值不写入）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "—", "None", "null", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_symbol(sym) -> str:
    """代码规范化：去 sh/sz/bj 前缀与空格，保留 6 位纯数字（兼容用户黏贴 sh600519）。"""
    s = str(sym or "").strip().lower()
    if s.startswith(("sh", "sz", "bj")):
        s = s[2:]
    return s.strip()


def _days_between(added_at: str) -> int | None:
    """加入至今的自然天数（含当日=1）。解析失败返回 None。"""
    try:
        d = datetime.strptime(str(added_at)[:10], "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None
    return (datetime.now().date() - d.date()).days + 1


# ---------------------------------------------------------------- 建表
SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,          -- 可自定义（用户在应用内直接改名）
    kind        TEXT DEFAULT 'mix',            -- mix | stock | fund  （仅作展示分组语义）
    color       TEXT DEFAULT '',               -- 可选色标
    sort_order  INTEGER DEFAULT 0,
    is_default  INTEGER DEFAULT 0,             -- 系统预置组（可改名，不建议删）
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS watch_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,             -- 6 位代码（不含前缀）
    name            TEXT DEFAULT '',
    kind            TEXT DEFAULT 'stock',      -- stock | fund
    group_id        INTEGER,                   -- 主组（弱关联，不做外键；新逻辑以 watch_item_groups 为准）
    sector          TEXT DEFAULT '',           -- 板块 / 行业
    note            TEXT DEFAULT '',           -- 备注
    attention       INTEGER DEFAULT 3,         -- 关注度 1..5
    target_price    REAL,                      -- 目标价（可空）
    alert_dir       TEXT DEFAULT '',           -- '' | above | below
    alert_on        INTEGER DEFAULT 1,         -- 是否开启到价提醒
    price_at_add    REAL,                      -- 加入时价格（算关注期间涨幅）
    added_at        TEXT,
    last_price      REAL,
    last_pct        REAL,
    last_checked_at TEXT,
    sort_order      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_watch_items_symbol ON watch_items(symbol);
CREATE INDEX IF NOT EXISTS ix_watch_items_group  ON watch_items(group_id);

-- 多对多分组（2026-10-14）：一只标的可同时在多个分组，组内排序存 sort_order。
CREATE TABLE IF NOT EXISTS watch_item_groups (
    item_id     INTEGER NOT NULL,
    group_id    INTEGER NOT NULL,
    sort_order  INTEGER DEFAULT 0,
    added_at    TEXT,
    PRIMARY KEY (item_id, group_id)
);

CREATE INDEX IF NOT EXISTS ix_wig_group ON watch_item_groups(group_id);

CREATE TABLE IF NOT EXISTS watch_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER,
    symbol          TEXT,
    name            TEXT,
    rule            TEXT,                      -- above | below
    target_price    REAL,
    triggered_price REAL,
    triggered_at    TEXT,
    read            INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_watch_alerts_read ON watch_alerts(read);
"""

DEFAULT_GROUPS = [
    # (name, kind, color, sort_order)  —— 用户可在应用内改名 / 增删
    ("重点关注", "stock", "#dc2626", 10),
    ("观察中",   "stock", "#0e7490", 20),
    ("基金池",   "fund",  "#7c3aed", 30),
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）+ 多对多迁移（幂等，只跑 INSERT OR IGNORE，不覆盖用户后续排序）。"""
    conn.executescript(SCHEMA)
    # 历史数据迁移：单 group_id → 关联表。已存在 (item_id, group_id) 则忽略，
    # 因此重复调用 / 用户改过组内排序后都不会被冲掉。
    try:
        conn.execute(
            "INSERT OR IGNORE INTO watch_item_groups(item_id, group_id, sort_order, added_at)"
            " SELECT id, group_id, sort_order, added_at FROM watch_items WHERE group_id IS NOT NULL")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()


def seed_default_groups(conn: sqlite3.Connection) -> None:
    """分组表为空时补 3 个默认分组；已有分组则不动（避免覆盖用户改名）。"""
    n = conn.execute("SELECT COUNT(*) FROM watch_groups").fetchone()[0]
    if n:
        return
    ts = _now()
    for name, kind, color, order in DEFAULT_GROUPS:
        conn.execute(
            "INSERT OR IGNORE INTO watch_groups(name, kind, color, sort_order, is_default, created_at)"
            " VALUES(?,?,?,?,1,?)",
            (name, kind, color, order, ts),
        )
    conn.commit()


# ---------------------------------------------------------------- 分组 CRUD
def list_groups(conn: sqlite3.Connection) -> list[dict]:
    """分组列表。n_items = 关联表 DISTINCT item_id（一只票在多组不重复计数）。"""
    rows = conn.execute(
        "SELECT g.*, "
        " (SELECT COUNT(DISTINCT item_id) FROM watch_item_groups w WHERE w.group_id = g.id) AS n_items"
        " FROM watch_groups g ORDER BY g.sort_order, g.id"
    ).fetchall()
    return [dict(r) for r in rows]


def add_group(conn: sqlite3.Connection, name: str, kind: str = "mix", color: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "分组名不能为空"}
    kind = kind if kind in ("mix", "stock", "fund") else "mix"
    order = (conn.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM watch_groups").fetchone()[0]) or 10
    try:
        cur = conn.execute(
            "INSERT INTO watch_groups(name, kind, color, sort_order, is_default, created_at)"
            " VALUES(?,?,?,?,0,?)",
            (name, kind, color, order, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "已存在同名分组：%s" % name}
    return {"ok": True, "id": cur.lastrowid, "name": name}


def rename_group(conn: sqlite3.Connection, gid: int, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "新分组名不能为空"}
    row = conn.execute("SELECT id FROM watch_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        return {"ok": False, "error": "分组不存在"}
    try:
        conn.execute("UPDATE watch_groups SET name=? WHERE id=?", (name, gid))
        conn.commit()
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "已存在同名分组：%s" % name}
    return {"ok": True, "id": gid, "name": name}


def reorder_groups(conn: sqlite3.Connection, ordered_ids) -> dict:
    """按给定顺序重写 watch_groups.sort_order = 10,20,30…（2026-10-14 进哥：分组前后顺序可调）。
    ordered_ids 应为**完整**分组 id 列表（前端从 list_groups 全量传入）；
    万一漏了某个 id，漏掉的排到末尾（按 id 序），不会被丢掉。"""
    ids = [int(x) for x in (ordered_ids or []) if str(x).lstrip("-").isdigit()]
    if not ids:
        return {"ok": False, "error": "缺少 ids"}
    known = {r["id"] for r in conn.execute("SELECT id FROM watch_groups").fetchall()}
    ids = [g for g in ids if g in known]
    leftover = sorted(known - set(ids))
    try:
        with conn:
            for i, gid in enumerate(ids):
                conn.execute("UPDATE watch_groups SET sort_order=? WHERE id=?", ((i + 1) * 10, gid))
            base = len(ids) * 10
            for j, gid in enumerate(leftover):
                conn.execute("UPDATE watch_groups SET sort_order=? WHERE id=?", (base + (j + 1) * 10, gid))
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "n": len(ids) + len(leftover)}


def remove_group(conn: sqlite3.Connection, gid: int, reassign_to: int | None = None) -> dict:
    """删分组。组内标的**不删**：关联表记录删除；主组被删的 item 改挂剩余第一组，
    无剩余组回退 reassign_to / 默认组（保持「标的不删」老行为）。"""
    row = conn.execute("SELECT id, name FROM watch_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        return {"ok": False, "error": "分组不存在"}
    other = reassign_to
    if other is None:
        r = conn.execute("SELECT id FROM watch_groups WHERE id<>? ORDER BY sort_order, id LIMIT 1", (gid,)).fetchone()
        other = r[0] if r else None
    moved = conn.execute(
        "SELECT COUNT(DISTINCT item_id) FROM watch_item_groups WHERE group_id=?", (gid,)).fetchone()[0]
    # 主组指向被删组的 item：优先改挂它自己的剩余关联组，否则 reassign_to / 默认组
    victims = [r[0] for r in conn.execute(
        "SELECT id FROM watch_items WHERE group_id=?", (gid,)).fetchall()]
    conn.execute("DELETE FROM watch_item_groups WHERE group_id=?", (gid,))
    for iid in victims:
        r = conn.execute(
            "SELECT mg.group_id FROM watch_item_groups mg "
            "JOIN watch_groups g ON g.id = mg.group_id "
            "WHERE mg.item_id=? ORDER BY g.sort_order, mg.sort_order, g.id LIMIT 1", (iid,)).fetchone()
        if r:
            conn.execute("UPDATE watch_items SET group_id=? WHERE id=?", (r[0], iid))
        else:
            fallback = other if other is not None else _default_group_id(conn)
            conn.execute("UPDATE watch_items SET group_id=? WHERE id=?", (fallback, iid))
    conn.execute("DELETE FROM watch_groups WHERE id=?", (gid,))
    conn.commit()
    return {"ok": True, "moved": moved, "reassigned_to": other}


# ---------------------------------------------------------------- 标的 CRUD
def list_items(conn: sqlite3.Connection, group_id: int | None = None) -> list[dict]:
    """池内标的。每项附加 groups（数组，按组 sort_order 排）/ n_groups；保留 group_id/group_name（主组）。

    排序：指定 group_id 时按该组内 mg.sort_order, attention DESC, id DESC；
    不指定时按主组 sort_order，再 attention DESC, id DESC。
    """
    if group_id is not None:
        sql = ("SELECT i.*, g.name AS group_name, g.color AS group_color "
               "FROM watch_items i "
               "JOIN watch_item_groups mg ON mg.item_id = i.id AND mg.group_id = ? "
               "LEFT JOIN watch_groups g ON g.id = i.group_id "
               "ORDER BY mg.sort_order, i.attention DESC, i.id DESC")
        params: tuple = (group_id,)
    else:
        sql = ("SELECT i.*, g.name AS group_name, g.color AS group_color "
               "FROM watch_items i LEFT JOIN watch_groups g ON g.id = i.group_id "
               "ORDER BY COALESCE(g.sort_order, 9999), i.attention DESC, i.id DESC")
        params = ()
    rows = conn.execute(sql, params).fetchall()
    # 一次性取全部关联分组（避免 N+1），按组 sort_order 排序
    rel: dict = {}
    for r in conn.execute(
            "SELECT mg.item_id AS item_id, g.id AS gid, g.name AS name, g.color AS color,"
            " mg.sort_order AS isort "
            "FROM watch_item_groups mg JOIN watch_groups g ON g.id = mg.group_id "
            "ORDER BY g.sort_order, mg.sort_order, g.id").fetchall():
        rel.setdefault(r["item_id"], []).append(
            {"id": r["gid"], "name": r["name"], "color": r["color"], "sort_order": r["isort"]})
    out = []
    for r in rows:
        d = dict(r)
        gl = rel.get(d["id"]) or []
        d["groups"] = gl
        d["n_groups"] = len(gl)
        d["days_watched"] = _days_between(d.get("added_at"))
        out.append(d)
    return out


def get_item(conn: sqlite3.Connection, iid: int) -> dict | None:
    r = conn.execute("SELECT * FROM watch_items WHERE id=?", (iid,)).fetchone()
    return dict(r) if r else None


_ITEM_FIELDS = ("symbol", "name", "kind", "group_id", "sector", "note", "attention",
                "target_price", "alert_dir", "alert_on", "sort_order")


def _group_name(conn: sqlite3.Connection, gid) -> str:
    r = conn.execute("SELECT name FROM watch_groups WHERE id=?", (gid,)).fetchone()
    return r[0] if r else str(gid)


def _default_group_id(conn: sqlite3.Connection):
    """兜底组：优先系统预置组（is_default=1），否则第一个组；无分组返回 None。"""
    r = conn.execute("SELECT id FROM watch_groups WHERE is_default=1 ORDER BY sort_order, id LIMIT 1").fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT id FROM watch_groups ORDER BY sort_order, id LIMIT 1").fetchone()
    return r[0] if r else None


def _sync_primary_group(conn: sqlite3.Connection, iid: int):
    """主组 = 剩余关联组中按组 sort_order 的第一个；无剩余则回退默认组。"""
    r = conn.execute(
        "SELECT mg.group_id FROM watch_item_groups mg "
        "JOIN watch_groups g ON g.id = mg.group_id "
        "WHERE mg.item_id=? ORDER BY g.sort_order, mg.sort_order, g.id LIMIT 1", (iid,)).fetchone()
    gid = r[0] if r else _default_group_id(conn)
    conn.execute("UPDATE watch_items SET group_id=? WHERE id=?", (gid, iid))
    return gid


def item_group_ids(conn: sqlite3.Connection, iid: int) -> list:
    """标的的全部关联组 id（按组 sort_order 排）。"""
    return [r[0] for r in conn.execute(
        "SELECT mg.group_id FROM watch_item_groups mg "
        "JOIN watch_groups g ON g.id = mg.group_id "
        "WHERE mg.item_id=? ORDER BY g.sort_order, mg.sort_order, g.id", (iid,)).fetchall()]


def add_to_group(conn: sqlite3.Connection, iid: int, gid: int) -> dict:
    """单组追加（幂等）：已在组内不重复插；仅当该标原本无任何组时同步主组。"""
    row = conn.execute("SELECT id FROM watch_items WHERE id=?", (iid,)).fetchone()
    if not row:
        return {"ok": False, "error": "标的不存在"}
    g = conn.execute("SELECT id FROM watch_groups WHERE id=?", (gid,)).fetchone()
    if not g:
        return {"ok": False, "error": "分组不存在"}
    ex = conn.execute("SELECT 1 FROM watch_item_groups WHERE item_id=? AND group_id=?", (iid, gid)).fetchone()
    if ex:
        return {"ok": True, "id": iid, "group_id": gid, "existed": True}
    had_groups = bool(conn.execute(
        "SELECT 1 FROM watch_item_groups WHERE item_id=? LIMIT 1", (iid,)).fetchone())
    order = (conn.execute(
        "SELECT COALESCE(MAX(sort_order),0)+10 FROM watch_item_groups WHERE group_id=?", (gid,)).fetchone()[0]) or 10
    conn.execute(
        "INSERT INTO watch_item_groups(item_id, group_id, sort_order, added_at) VALUES(?,?,?,?)",
        (iid, gid, order, _now()))
    if not had_groups:
        conn.execute("UPDATE watch_items SET group_id=? WHERE id=?", (gid, iid))
    conn.commit()
    return {"ok": True, "id": iid, "group_id": gid, "existed": False}


def set_groups(conn: sqlite3.Connection, iid: int, gids) -> dict:
    """全量设置关联组（编辑弹窗用）。已有组保留原 sort_order；新组 sort_order=该组 max+10。
    gids 为空 → 回退默认组（不允许变无组）。同步主组 = 剩余第一组（按组 sort_order）。"""
    row = conn.execute("SELECT id FROM watch_items WHERE id=?", (iid,)).fetchone()
    if not row:
        return {"ok": False, "error": "标的不存在"}
    want: list = []
    for g in (gids or []):
        try:
            g = int(g)
        except Exception:  # noqa: BLE001
            continue
        if g not in want:
            want.append(g)
    if not want:
        d = _default_group_id(conn)
        want = [d] if d else []
    ok_ids = {r[0] for r in conn.execute("SELECT id FROM watch_groups").fetchall()}
    want = [g for g in want if g in ok_ids]
    if not want:
        return {"ok": False, "error": "目标分组不存在"}
    have = {r[0]: r[1] for r in conn.execute(
        "SELECT group_id, sort_order FROM watch_item_groups WHERE item_id=?", (iid,)).fetchall()}
    for g in want:
        if g in have:
            continue
        order = (conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+10 FROM watch_item_groups WHERE group_id=?", (g,)).fetchone()[0]) or 10
        conn.execute(
            "INSERT INTO watch_item_groups(item_id, group_id, sort_order, added_at) VALUES(?,?,?,?)",
            (iid, g, order, _now()))
    for g in list(have):
        if g not in want:
            conn.execute("DELETE FROM watch_item_groups WHERE item_id=? AND group_id=?", (iid, g))
    _sync_primary_group(conn, iid)
    conn.commit()
    return {"ok": True, "id": iid, "groups": item_group_ids(conn, iid)}


def remove_from_group(conn: sqlite3.Connection, iid: int, gid: int) -> dict:
    """移出单个组。剩其他组 → 主组若被移则改挂剩余第一组；无剩余组 → 回退默认组。返回剩余组数。"""
    row = conn.execute("SELECT id FROM watch_items WHERE id=?", (iid,)).fetchone()
    if not row:
        return {"ok": False, "error": "标的不存在"}
    conn.execute("DELETE FROM watch_item_groups WHERE item_id=? AND group_id=?", (iid, gid))
    if item_group_ids(conn, iid):
        _sync_primary_group(conn, iid)
    else:
        # 不允许变无组：回退默认组（保持「标的不删、永在池中」的老行为）
        d = _default_group_id(conn)
        if d:
            order = (conn.execute(
                "SELECT COALESCE(MAX(sort_order),0)+10 FROM watch_item_groups WHERE group_id=?", (d,)).fetchone()[0]) or 10
            conn.execute(
                "INSERT OR IGNORE INTO watch_item_groups(item_id, group_id, sort_order, added_at)"
                " VALUES(?,?,?,?)", (iid, d, order, _now()))
        conn.execute("UPDATE watch_items SET group_id=? WHERE id=?", (d, iid))
    conn.commit()
    return {"ok": True, "id": iid, "left": len(item_group_ids(conn, iid))}


def reorder_in_group(conn: sqlite3.Connection, gid: int, ordered_ids) -> dict:
    """按给定顺序把该组 sort_order 重写为 10,20,30…；不在表里的 id 忽略。"""
    n = 0
    for i, iid in enumerate(ordered_ids or []):
        try:
            iid = int(iid)
        except Exception:  # noqa: BLE001
            continue
        cur = conn.execute(
            "UPDATE watch_item_groups SET sort_order=? WHERE group_id=? AND item_id=?",
            ((i + 1) * 10, gid, iid))
        if cur.rowcount and cur.rowcount > 0:
            n += 1
    conn.commit()
    return {"ok": True, "group_id": gid, "moved": n}


def add_item(conn: sqlite3.Connection, symbol: str, name: str = "", kind: str = "stock",
             group_id: int | None = None, sector: str = "", note: str = "",
             attention: int = 3, target_price=None, alert_dir: str = "",
             alert_on: int = 1, price_at_add=None, groups=None) -> dict:
    """加入自选池。

    多对多（2026-10-14）：同代码已在池**不再报错**——若 payload 带 group_id/groups
    且该票不在目标组，则追加到目标组并返回 existed + hint；已在目标组同样返回
    existed + 提示。全新插入逻辑不变（同时写关联表）。
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok": False, "error": "缺少股票 / 基金代码"}
    dup = conn.execute("SELECT id FROM watch_items WHERE symbol=?", (sym,)).fetchone()
    if dup:
        iid = dup[0]
        target: list = []
        if isinstance(groups, (list, tuple)):
            target = [g for g in groups if str(g).lstrip("-").isdigit()]
        elif group_id is not None and str(group_id).lstrip("-").isdigit():
            target = [int(group_id)]
        have = item_group_ids(conn, iid)
        to_add = [int(g) for g in target if int(g) not in have]
        if to_add:
            for g in to_add:
                add_to_group(conn, iid, g)
            names = "、".join(_group_name(conn, g) for g in to_add)
            return {"ok": True, "id": iid, "existed": True, "symbol": sym,
                    "hint": "该标的已在池中，已追加到分组「%s」" % names}
        if target:
            names = "、".join(_group_name(conn, g) for g in target)
            return {"ok": True, "id": iid, "existed": True, "symbol": sym,
                    "hint": "该标的已在池中，且已在分组「%s」" % names}
        return {"ok": True, "id": iid, "existed": True, "symbol": sym,
                "hint": "该标的已在池中"}
    if group_id is None:
        r = conn.execute("SELECT id FROM watch_groups ORDER BY sort_order, id LIMIT 1").fetchone()
        group_id = r[0] if r else None
    kind = kind if kind in ("stock", "fund") else "stock"
    att = int(attention) if str(attention).isdigit() else 3
    att = max(1, min(5, att))
    ad = alert_dir if alert_dir in ("above", "below") else ""
    cur = conn.execute(
        "INSERT INTO watch_items(symbol, name, kind, group_id, sector, note, attention,"
        " target_price, alert_dir, alert_on, price_at_add, added_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sym, (name or "").strip(), kind, group_id, (sector or "").strip(), (note or "").strip(),
         att, _to_float(target_price), ad, 1 if alert_on else 0, _to_float(price_at_add), _now()),
    )
    iid = cur.lastrowid
    # 关联表：groups 优先，其次 group_id；sort_order 追加到组尾（max+10）
    tg: list = []
    if isinstance(groups, (list, tuple)):
        tg = [int(g) for g in groups if str(g).lstrip("-").isdigit()]
    elif group_id is not None:
        tg = [int(group_id)]
    for g in tg:
        order = (conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+10 FROM watch_item_groups WHERE group_id=?", (g,)).fetchone()[0]) or 10
        conn.execute(
            "INSERT OR IGNORE INTO watch_item_groups(item_id, group_id, sort_order, added_at)"
            " VALUES(?,?,?,?)", (iid, g, order, _now()))
    conn.commit()
    return {"ok": True, "id": iid, "symbol": sym}


def update_item(conn: sqlite3.Connection, iid: int, **fields) -> dict:
    row = conn.execute("SELECT * FROM watch_items WHERE id=?", (iid,)).fetchone()
    if not row:
        return {"ok": False, "error": "标的不存在"}
    sets, params = [], []
    for k in _ITEM_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if k == "attention":
            v = max(1, min(5, int(v) if str(v).isdigit() else 3))
        elif k == "target_price":
            v = _to_float(v)
        elif k == "alert_dir":
            v = v if v in ("above", "below") else ""
        elif k == "alert_on":
            v = 1 if v in (1, "1", True, "true", "on") else 0
        elif k == "group_id":
            v = int(v) if str(v).lstrip("-").isdigit() else None
        elif k == "symbol":
            v = _norm_symbol(v)
        sets.append("%s=?" % k)
        params.append(v)
    if not sets:
        return {"ok": False, "error": "无可更新字段"}
    params.append(iid)
    conn.execute("UPDATE watch_items SET %s WHERE id=?" % ",".join(sets), params)
    conn.commit()
    return {"ok": True, "id": iid}


def remove_item(conn: sqlite3.Connection, iid: int) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM watch_items WHERE id=?", (iid,)).fetchone()[0]
    if not n:
        return {"ok": False, "error": "标的不存在"}
    conn.execute("DELETE FROM watch_items WHERE id=?", (iid,))
    conn.execute("DELETE FROM watch_item_groups WHERE item_id=?", (iid,))
    conn.execute("DELETE FROM watch_alerts WHERE item_id=?", (iid,))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 到价判定
def check_alerts(conn: sqlite3.Connection, prices: dict, now: str | None = None) -> dict:
    """按注入的 prices={code: price} 判定到价。

    规则：alert_on=1 且 target_price 非空 且 alert_dir∈{above,below} 且 有价 →
        above: price >= target  触发
        below: price <= target  触发
    去重：同一标的 + 同一方向 + 同一目标价，若已有**未读**告警则不重复生成
        （用户标记已读、或改了目标价后可再次触发）。
    会顺带刷新每个标的的 last_price / last_pct（若有）/ last_checked_at。
    """
    now = now or _now()
    items = conn.execute(
        "SELECT * FROM watch_items WHERE alert_on=1 AND target_price IS NOT NULL"
    ).fetchall()
    checked = 0
    triggered = []
    for r in items:
        d = dict(r)
        code = d.get("symbol")
        p = prices.get(code)
        price = p if isinstance(p, (int, float)) else (p or {}).get("price") if isinstance(p, dict) else None
        pct = (p or {}).get("pct") if isinstance(p, dict) else None
        if price is not None:
            conn.execute("UPDATE watch_items SET last_price=?, last_pct=?, last_checked_at=? WHERE id=?",
                         (_to_float(price), _to_float(pct), now, d["id"]))
        tgt = _to_float(d.get("target_price"))
        ad = d.get("alert_dir")
        if price is None or tgt is None or ad not in ("above", "below"):
            continue
        checked += 1
        hit = (ad == "above" and price >= tgt) or (ad == "below" and price <= tgt)
        if not hit:
            continue
        dup = conn.execute(
            "SELECT id FROM watch_alerts WHERE item_id=? AND rule=? AND IFNULL(target_price,-1)=?"
            " AND read=0 LIMIT 1",
            (d["id"], ad, tgt),
        ).fetchone()
        if dup:
            continue
        cur = conn.execute(
            "INSERT INTO watch_alerts(item_id, symbol, name, rule, target_price, triggered_price,"
            " triggered_at, read) VALUES(?,?,?,?,?,?,?,0)",
            (d["id"], code, d.get("name") or "", ad, tgt, _to_float(price), now),
        )
        triggered.append({
            "id": cur.lastrowid, "item_id": d["id"], "symbol": code, "name": d.get("name") or "",
            "rule": ad, "target_price": tgt, "triggered_price": _to_float(price), "triggered_at": now,
        })
    conn.commit()
    return {"checked": checked, "triggered": triggered, "triggered_n": len(triggered),
            "items_total": len(items), "priced": len([1 for c in prices if prices.get(c) is not None])}


def list_alerts(conn: sqlite3.Connection, limit: int = 200, only_unread: bool = False) -> list[dict]:
    sql = "SELECT * FROM watch_alerts "
    if only_unread:
        sql += "WHERE read=0 "
    sql += "ORDER BY read ASC, triggered_at DESC, id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]


def count_unread(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM watch_alerts WHERE read=0").fetchone()[0]


def mark_alert(conn: sqlite3.Connection, aid: int, read: bool = True) -> dict:
    conn.execute("UPDATE watch_alerts SET read=? WHERE id=?", (1 if read else 0, aid))
    conn.commit()
    return {"ok": True}


def mark_all_read(conn: sqlite3.Connection) -> dict:
    n = conn.execute("UPDATE watch_alerts SET read=1 WHERE read=0").rowcount
    conn.commit()
    return {"ok": True, "marked": n}


def clear_alerts(conn: sqlite3.Connection) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM watch_alerts").fetchone()[0]
    conn.execute("DELETE FROM watch_alerts")
    conn.commit()
    return {"ok": True, "cleared": n}


# ---------------------------------------------------------------- 自检（离线）
if __name__ == "__main__":
    import json
    import os
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    # 用内存库自检：建表 → 默认分组 → 增改删 → 到价触发（注入价格，零网络）
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    seed_default_groups(c)
    gs = list_groups(c)
    print("groups:", [(g["name"], g["kind"], g["n_items"]) for g in gs])

    gid = gs[0]["id"]
    r1 = add_item(c, "600519", "贵州茅台", group_id=gid, sector="白酒",
                  attention=5, target_price=1500, alert_dir="above", note="回到1500提醒")
    r2 = add_item(c, "000001", "平安银行", group_id=gid, kind="stock",
                  attention=4, target_price=12.5, alert_dir="below")
    print("add:", r1, r2)
    assert r1["ok"] and r2["ok"]

    # 重复加入：不再报错——同票已在池，返回 existed + 提示
    dup = add_item(c, "sh600519")
    print("dup:", dup)
    assert dup["ok"] and dup.get("existed")

    # 到价：茅台 1495（未破1500）、平安 12.2（破12.5 下穿）
    res = check_alerts(c, {"600519": 1495.0, "000001": 12.2})
    print("check1 triggered:", [t["symbol"] for t in res["triggered"]], "checked=", res["checked"])
    assert [t["symbol"] for t in res["triggered"]] == ["000001"], "应仅平安触发下穿"

    # 再查一次：未读去重 → 不再重复
    res2 = check_alerts(c, {"600519": 1495.0, "000001": 12.2})
    print("check2 triggered(去重后应为空):", res2["triggered"])
    assert res2["triggered_n"] == 0

    # 茅台涨过 1500 → 触发
    res3 = check_alerts(c, {"600519": 1508.0, "000001": 12.6})
    print("check3 triggered:", [(t["symbol"], t["rule"], t["triggered_price"]) for t in res3["triggered"]])
    assert [t["symbol"] for t in res3["triggered"]] == ["600519"]

    # 已读后可再次触发
    mark_all_read(c)
    res4 = check_alerts(c, {"600519": 1510.0, "000001": 12.2})
    print("check4 after read triggered:", [t["symbol"] for t in res4["triggered"]])
    assert set(t["symbol"] for t in res4["triggered"]) == {"600519", "000001"}

    # 改名 / 删组（组内标的改挂）
    print("rename:", rename_group(c, gs[1]["id"], "观察(改名)"))
    print("remove group:", remove_group(c, gid))
    print("items after group remove:", [(i["symbol"], i["group_name"]) for i in list_items(c)])

    print("unread:", count_unread(c), "| alerts:", len(list_alerts(c)))

    # —— 多对多分组自检（2026-10-14）——
    c2 = sqlite3.connect(":memory:")
    c2.row_factory = sqlite3.Row
    ensure_schema(c2)
    seed_default_groups(c2)
    gs2 = list_groups(c2)
    gA, gB, gC = gs2[0]["id"], gs2[1]["id"], gs2[2]["id"]

    # 1) 同票加两个组：第二次带 groups=[gB] → 不再报错，追加到 B
    ra = add_item(c2, "600519", "贵州茅台", group_id=gA)
    rb = add_item(c2, "600519", "贵州茅台", groups=[gB])
    print("multi add:", ra, "|", rb)
    assert rb["ok"] and rb.get("existed") and "已追加" in (rb.get("hint") or "")
    assert sorted(item_group_ids(c2, ra["id"])) == sorted([gA, gB])

    # 2) n_items 去重计数：A=1 / B=1 / C=0（不是 2）
    cnt = {g["id"]: g["n_items"] for g in list_groups(c2)}
    print("n_items dedup:", cnt)
    assert cnt[gA] == 1 and cnt[gB] == 1 and cnt[gC] == 0

    # 3) list_items 带 groups / n_groups；按组视图能查到
    byB = list_items(c2, group_id=gB)
    print("list by gB:", [(i["symbol"], i["n_groups"], [g["name"] for g in i["groups"]]) for i in byB])
    assert len(byB) == 1 and byB[0]["n_groups"] == 2 and len(byB[0]["groups"]) == 2

    # 4) set_groups 全量改 → 仅剩 [gC, gA]；主组按组 sort_order = gA
    sg = set_groups(c2, ra["id"], [gC, gA])
    print("set_groups:", sg)
    assert sg["ok"] and sorted(item_group_ids(c2, ra["id"])) == sorted([gC, gA])
    assert get_item(c2, ra["id"])["group_id"] == gA

    # 5) reorder_in_group：组内顺序保持
    r2b = add_item(c2, "000001", "平安银行", group_id=gB)
    r3b = add_item(c2, "300750", "宁德时代", group_id=gB)
    reorder_in_group(c2, gB, [r3b["id"], r2b["id"]])
    order = [i["id"] for i in list_items(c2, group_id=gB)]
    print("reorder order:", order, "expect:", [r3b["id"], r2b["id"]])
    assert order == [r3b["id"], r2b["id"]]

    # 6) remove_from_group 移出单组：其他组仍在；主组被移则改挂剩余第一组
    left = remove_from_group(c2, ra["id"], gA)
    print("remove_from_group left:", left, "groups:", item_group_ids(c2, ra["id"]))
    assert left["ok"] and left["left"] == 1 and item_group_ids(c2, ra["id"]) == [gC]
    assert get_item(c2, ra["id"])["group_id"] == gC

    # 7) 移到只剩兜底：回退默认组（不允许变无组）
    left2 = remove_from_group(c2, ra["id"], gC)
    print("fallback left:", left2, "groups:", item_group_ids(c2, ra["id"]))
    assert item_group_ids(c2, ra["id"]) == [gA]

    print("SELFTEST_OK")
