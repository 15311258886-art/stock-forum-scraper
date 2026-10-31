"""geo_events 表：建表 / 幂等导入 / 查询 + 国际新闻流筛选。

不持有连接：全部函数接收调用方的 sqlite3.Connection（沿用 viewer.db.Database 的连接池），
避免同一 WAL 库出现两套连接管理。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from geo import SEED_PATH
from geo import taxonomy as tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    eid           TEXT UNIQUE,        -- 稳定 id，重跑导入按它 upsert（幂等）
    title         TEXT,               -- 中文标题
    category      TEXT,               -- 七类之一
    panel         TEXT DEFAULT 'general',  -- 归属面板：general(综合)/policy(全球政策雷达)/tech(科技前沿突破)
    event_date    TEXT,               -- 事件日期 YYYY-MM-DD
    intensity     INTEGER DEFAULT 1,  -- 强度 1-5
    direction     TEXT,               -- 方向判断（明确指示，如"利多能源/军工，利空出行"）
    summary       TEXT,               -- 摘要（发生了什么）
    transmission  TEXT,               -- 传导逻辑（一笔带过的因果链）
    sectors_up    TEXT,               -- 受益板块（逗号分隔）
    sectors_down  TEXT,               -- 承压板块（逗号分隔）
    commodities   TEXT,               -- 涉及原材料/大宗（逗号分隔）
    symbols_up    TEXT,               -- 受益代表个股 JSON [{"code","name"}]
    symbols_down  TEXT,               -- 承压代表个股 JSON
    decision      TEXT,               -- 操作取向一句话
    watch         TEXT,               -- 接下来看什么（前瞻验证点）
    sources       TEXT,               -- 来源 JSON [{"name","region","url","lang"}]
    cross_check   TEXT,               -- 国内外信源交叉印证 JSON {"foreign":[...],"domestic":[...],"verdict","note"}
    tags          TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_geo_date ON geo_events(event_date DESC);
CREATE INDEX IF NOT EXISTS ix_geo_cat  ON geo_events(category);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 兼容旧表：补齐后续新增的列（CREATE TABLE IF NOT EXISTS 不会加列）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(geo_events)")}
    if "panel" not in cols:
        conn.execute("ALTER TABLE geo_events ADD COLUMN panel TEXT DEFAULT 'general'")
    if "cross_check" not in cols:
        conn.execute("ALTER TABLE geo_events ADD COLUMN cross_check TEXT")
    # 2026-10-31（D48 收尾）：删除 certainty 列。
    #   该列存的是人工手填常量（0.60~0.90 共 13 档），前端早已改为按信源家数分档展示，
    #   API 也不再需要它。留在库里只会诱导后来者重新拿它当"模型把握度"用。
    #   DROP COLUMN 不可逆：删除前把整列导出到 backups/ 留档（含 eid + title，可回溯）。
    if "certainty" in cols:
        try:
            import os
            from datetime import datetime as _dt
            bak_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
            os.makedirs(bak_dir, exist_ok=True)
            rows = [dict(r) for r in conn.execute(
                "SELECT eid, title, certainty FROM geo_events").fetchall()]
            # sqlite3.Row → dict（普通连接返回 tuple，统一按位置取值）
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({"eid": r[0], "title": r[1], "certainty": r[2]})
            path = os.path.join(bak_dir, "geo_certainty_%s.json" % _dt.now().strftime("%Y%m%d-%H%M%S"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            conn.execute("ALTER TABLE geo_events DROP COLUMN certainty")
            conn.commit()
        except Exception:
            # 迁移失败不影响主流程：_shape() 仍会 pop 掉该字段，API 不会吐出它
            pass
    conn.commit()


def _j(v):
    if v is None or v == "" or v == []:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, dict):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, (list, dict)) else []
    except Exception:
        return []


def import_seed(conn: sqlite3.Connection, path=None) -> dict:
    """从 seed_events.json 幂等导入（按 eid upsert）。返回 {total, inserted, updated}。"""
    src = path or SEED_PATH
    data = json.loads(open(src, encoding="utf-8").read())
    events = data.get("events") if isinstance(data, dict) else data
    events = events or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ins = upd = 0
    for e in events:
        eid = (e.get("eid") or "").strip()
        if not eid:
            continue
        row = {
            "eid": eid,
            "title": e.get("title", ""),
            "category": e.get("category", "politics"),
            "panel": e.get("panel", "general"),
            "event_date": e.get("event_date", ""),
            "intensity": int(e.get("intensity") or 1),
            "direction": e.get("direction", ""),
            "summary": e.get("summary", ""),
            "transmission": e.get("transmission", ""),
            "sectors_up": ",".join(_j(e.get("sectors_up"))),
            "sectors_down": ",".join(_j(e.get("sectors_down"))),
            "commodities": ",".join(_j(e.get("commodities"))),
            "symbols_up": json.dumps(_j(e.get("symbols_up")), ensure_ascii=False),
            "symbols_down": json.dumps(_j(e.get("symbols_down")), ensure_ascii=False),
            "decision": e.get("decision", ""),
            "watch": e.get("watch", ""),
            "sources": json.dumps(_j(e.get("sources")), ensure_ascii=False),
            "cross_check": json.dumps(_j(e.get("cross_check")), ensure_ascii=False),
            "tags": ",".join(_j(e.get("tags"))),
            "updated_at": now,
        }
        exists = conn.execute("SELECT id FROM geo_events WHERE eid=?", (eid,)).fetchone()
        cols = ",".join(row.keys())
        ph = ",".join("?" for _ in row)
        if exists:
            sets = ",".join(f"{k}=?" for k in row)
            conn.execute(f"UPDATE geo_events SET {sets} WHERE eid=?", list(row.values()) + [eid])
            upd += 1
        else:
            conn.execute(f"INSERT INTO geo_events ({cols}) VALUES ({ph})", list(row.values()))
            ins += 1
    conn.commit()
    return {"total": len(events), "inserted": ins, "updated": upd, "file": str(src)}


def _shape(r: sqlite3.Row) -> dict:
    d = dict(r)
    # 2026-10-31：certainty 已废弃（人工手填的常量，不是计算值）。库里已 DROP COLUMN，
    # 这里再 pop 一次是给旧库快照 / 未迁移环境兜底，确保 API 永远不会吐出该字段。
    d.pop("certainty", None)
    d["symbols_up"] = _j(d.get("symbols_up"))
    d["symbols_down"] = _j(d.get("symbols_down"))
    d["sources"] = _j(d.get("sources"))
    d["cross_check"] = _j(d.get("cross_check"))
    d["panel"] = d.get("panel") or "general"
    d["sectors_up"] = [x for x in (d.get("sectors_up") or "").split(",") if x]
    d["sectors_down"] = [x for x in (d.get("sectors_down") or "").split(",") if x]
    d["commodities"] = [x for x in (d.get("commodities") or "").split(",") if x]
    d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
    d["cat_label"] = tx.label_of(d.get("category") or "")
    d["intensity_text"] = tx.intensity_text(d.get("intensity") or 1)
    return d


def query_events(conn: sqlite3.Connection, category: str | None = None,
                 days: int | None = None, panel: str | None = None,
                 limit: int = 100) -> list[dict]:
    """按面板/类别/天数筛选，按「强度降序 → 日期降序」排（重要性优先，同强度看新旧）。"""
    sql = "SELECT * FROM geo_events WHERE 1=1"
    args: list = []
    if panel and panel != "all":
        sql += " AND panel=?"
        args.append(panel)
    if category and category != "all":
        sql += " AND category=?"
        args.append(category)
    if days:
        edge = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        sql += " AND event_date>=?"
        args.append(edge)
    sql += " ORDER BY intensity DESC, event_date DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [_shape(r) for r in conn.execute(sql, args)]


def stats(conn: sqlite3.Connection) -> dict:
    """分类计数 + 时间范围 + 强度分布 + 涉及标的数，供面板顶部与回归断言用。"""
    total = conn.execute("SELECT COUNT(*) FROM geo_events").fetchone()[0]
    by_cat = {r[0]: r[1] for r in conn.execute(
        "SELECT category, COUNT(*) FROM geo_events GROUP BY category")}
    rng = conn.execute("SELECT MIN(event_date), MAX(event_date) FROM geo_events").fetchone()
    uniq = set()
    for (s,) in conn.execute("SELECT symbols_up FROM geo_events UNION ALL "
                             "SELECT symbols_down FROM geo_events"):
        for it in _j(s):
            if isinstance(it, dict) and it.get("code"):
                uniq.add(it["code"])
    up = conn.execute("SELECT COUNT(*) FROM geo_events WHERE intensity>=4").fetchone()[0]
    # 交叉印证口径统计：一致 / 分歧 / 待观察
    verdicts = {"一致": 0, "分歧": 0, "待观察": 0}
    for (raw,) in conn.execute("SELECT cross_check FROM geo_events"):
        cc = _j(raw)
        if isinstance(cc, dict):
            v = cc.get("verdict")
            if v in verdicts:
                verdicts[v] += 1
    # 面板分布（供导航角标）
    by_panel = {r[0]: r[1] for r in conn.execute(
        "SELECT panel, COUNT(*) FROM geo_events GROUP BY panel")}
    return {
        "total": total,
        "by_category": {c["key"]: by_cat.get(c["key"], 0) for c in tx.CATEGORIES},
        "categories": tx.CATEGORIES,
        "date_from": rng[0] if rng else None,
        "date_to": rng[1] if rng else None,
        "major_count": up,
        "symbols_n": len(uniq),
        "verdicts": verdicts,
        "by_panel": by_panel,
        "updated_at": (conn.execute("SELECT MAX(updated_at) FROM geo_events").fetchone() or [None])[0],
    }


# 国际类新闻的筛选口径（用 overseas_news 现成数据，不另起抓取管道）
_INTL_TOPICS = ("国际形势", "地缘冲突与战争", "自然灾害与洪涝", "国际新闻热点",
                "国外疫情舆论", "粮食歉收与减产", "能源与大宗", "金融与汇率", "供应链与制造")
_INTL_TITLE_KW = ("关税", "制裁", "冲突", "袭击", "战争", "峰会", "会晤", "谈判", "出口管制",
                  "稀土", "油价", "原油", "灾害", "台风", "地震", "疫情", "美联储", "利率",
                  "欧元", "美元", "协议", "决议", "加征", "禁运", "封锁", "霍尔木兹")


def recent_intl_news(conn: sqlite3.Connection, days: int = 10, limit: int = 40) -> list[dict]:
    """从海外新闻表里筛出国际类条目（**只分类不给方向**，前端标"未研判"）。

    诚实口径：这些是抓取来的原始新闻，本项目不对其做方向研判；
    方向判断只出现在上方「研判事件」里。
    """
    edge = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    like = " OR ".join(["title LIKE ?"] * len(_INTL_TITLE_KW))
    args = [f"%{k}%" for k in _INTL_TITLE_KW]
    ph = ",".join("?" for _ in _INTL_TOPICS)
    sql = (f"SELECT publish_time, source_name, region, title, url, topic, sentiment "
           f"FROM overseas_news WHERE url NOT LIKE '%example.local%' AND publish_time>=? "
           f"AND (topic IN ({ph}) OR ({like})) "
           f"ORDER BY publish_time DESC LIMIT ?")
    rows = list(conn.execute(sql, [edge] + list(_INTL_TOPICS) + args + [int(limit)]))
    out = []
    for r in rows:
        cat, hits = tx.classify_text(f"{r['title']} {r['topic'] or ''}")
        imp = tx.impact_of(cat)
        out.append({
            "time": (r["publish_time"] or "")[:16],
            "source": r["source_name"], "region": r["region"],
            "title": r["title"], "url": r["url"], "topic": r["topic"],
            "sentiment": r["sentiment"],
            "category": cat, "cat_label": tx.label_of(cat),
            "hint_direction": imp["direction"],      # 机器初判，仅提示
            "hint_up": imp["sectors_up"], "hint_down": imp["sectors_down"],
        })
    return out
