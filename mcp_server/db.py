"""mcp_server/db.py — 只读 SQLite 访问层。

设计铁律（A 方案暴露型 MCP 的本质）：
- 只读 URI 连接 + PRAGMA query_only，从协议层杜绝任何写入/修改。
- 不创建、不 alter、不 update app.db，对现有抓取与前端零影响。
"""
import os
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
# 默认指向项目内的主语料库 viewer/app.db
DEFAULT_DB = os.path.normpath(os.path.join(_HERE, "..", "viewer", "app.db"))


def connect(db_path=None):
    """建立只读连接。db_path 缺省时回退到 DEFAULT_DB 或环境变量 SFS_DB。"""
    db_path = db_path or os.environ.get("SFS_DB") or DEFAULT_DB
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        raise FileNotFoundError("SQLite 数据库不存在: " + db_path)
    uri = "file:" + db_path + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        # 双重保险：把连接钉死为查询专用，任何写操作都会被 SQLite 拒绝。
        con.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return con


def resolve_stock(con, query, limit=20):
    """按代码/名称/公司关键字模糊匹配股票，返回 [{code,name,company,market}]。"""
    q = (query or "").strip()
    if not q:
        return []
    cur = con.cursor()
    # 纯 4~6 位数字优先视为代码精确匹配
    if q.isdigit() and len(q) >= 4:
        cur.execute(
            "SELECT code, name, company, market FROM stock_names WHERE code=? LIMIT ?",
            (q, int(limit)),
        )
        rows = cur.fetchall()
        if rows:
            return [dict(r) for r in rows]
    like = "%" + q + "%"
    cur.execute(
        "SELECT code, name, company, market FROM stock_names "
        "WHERE code LIKE ? OR name LIKE ? OR company LIKE ? "
        "ORDER BY CASE WHEN code=? THEN 0 WHEN name=? THEN 1 ELSE 2 END LIMIT ?",
        (q, like, like, q, q, int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def best_code(con, query):
    """把用户输入（代码或名称）解析成 (code, name)；解析不出返回 (None, None)。"""
    rows = resolve_stock(con, query, limit=5)
    if not rows:
        return None, None
    return rows[0]["code"], rows[0]["name"]


def cutoff_date(days):
    """返回 YYYY-MM-DD 字符串，作为 post_time/publish_time 的字符串下界。"""
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d")
