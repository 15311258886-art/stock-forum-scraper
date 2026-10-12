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

对外函数：
  ensure_schema(conn)                     建表（幂等）
  seed_default_groups(conn)               无分组时补默认分组
  list_groups / add_group / rename_group / remove_group
  list_items / add_item / get_item / update_item / remove_item
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
    group_id        INTEGER,                   -- 归属分组（弱关联，不做外键）
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
    conn.executescript(SCHEMA)
    conn.commit()


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
    rows = conn.execute(
        "SELECT g.*, "
        " (SELECT COUNT(*) FROM watch_items w WHERE w.group_id = g.id) AS n_items"
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


def remove_group(conn: sqlite3.Connection, gid: int, reassign_to: int | None = None) -> dict:
    """删分组。组内标的**不删**，改挂到 reassign_to（缺省挂到第一个其它分组；无其它组则为未分组）。"""
    row = conn.execute("SELECT id, name FROM watch_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        return {"ok": False, "error": "分组不存在"}
    other = reassign_to
    if other is None:
        r = conn.execute("SELECT id FROM watch_groups WHERE id<>? ORDER BY sort_order, id LIMIT 1", (gid,)).fetchone()
        other = r[0] if r else None
    moved = conn.execute("SELECT COUNT(*) FROM watch_items WHERE group_id=?", (gid,)).fetchone()[0]
    conn.execute("UPDATE watch_items SET group_id=? WHERE group_id=?", (other, gid))
    conn.execute("DELETE FROM watch_groups WHERE id=?", (gid,))
    conn.commit()
    return {"ok": True, "moved": moved, "reassigned_to": other}


# ---------------------------------------------------------------- 标的 CRUD
def list_items(conn: sqlite3.Connection, group_id: int | None = None) -> list[dict]:
    sql = ("SELECT i.*, g.name AS group_name, g.color AS group_color "
           "FROM watch_items i LEFT JOIN watch_groups g ON g.id = i.group_id ")
    params: tuple = ()
    if group_id is not None:
        sql += "WHERE i.group_id=? "
        params = (group_id,)
    sql += "ORDER BY i.attention DESC, i.sort_order, i.id DESC"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["days_watched"] = _days_between(d.get("added_at"))
        out.append(d)
    return out


def get_item(conn: sqlite3.Connection, iid: int) -> dict | None:
    r = conn.execute("SELECT * FROM watch_items WHERE id=?", (iid,)).fetchone()
    return dict(r) if r else None


_ITEM_FIELDS = ("symbol", "name", "kind", "group_id", "sector", "note", "attention",
                "target_price", "alert_dir", "alert_on", "sort_order")


def add_item(conn: sqlite3.Connection, symbol: str, name: str = "", kind: str = "stock",
             group_id: int | None = None, sector: str = "", note: str = "",
             attention: int = 3, target_price=None, alert_dir: str = "",
             alert_on: int = 1, price_at_add=None) -> dict:
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok": False, "error": "缺少股票 / 基金代码"}
    # 同池同标的去重（同代码只留一条；已存在则提示改用编辑）
    dup = conn.execute("SELECT id FROM watch_items WHERE symbol=?", (sym,)).fetchone()
    if dup:
        return {"ok": False, "error": "该标的已在池中（%s），请直接编辑" % sym, "id": dup[0]}
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
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "symbol": sym}


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

    # 重复加入去重
    print("dup:", add_item(c, "sh600519"))

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
    print("SELFTEST_OK")
