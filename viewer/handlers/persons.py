# -*- coding: utf-8 -*-
"""persons 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _PERSON_SCORE_COLUMNS,
    _load_json_or_none,
    _person_site_map,
    _player_index,
)

def api_persons(limit: int = 0, offset: int = 0) -> list[dict]:
    """选手榜：返回 list（不传 limit 时与从前完全一致，前端零改）。

    limit>0 = 分页（默认 0 = 全量，界面文案承诺「不遗漏任何一个名称」）；
    offset 配合 limit 使用。附带把「每人查一次站点」改成一次 GROUP BY 聚合（N+1 → 1）。
    """
    # 2026-09-22 修复 P2-5：原 LIMIT 100 会把 170 人截断成 100 人，
    # 而界面文案承诺「不遗漏任何一个名称」；上限放到 1000（仅兜底防爆表）。
    scored = True
    try:
        cols = ", ".join(f"s.{c} AS {c}" for c in _PERSON_SCORE_COLUMNS)
        rows = DB.conn.execute(
            "SELECT p.name AS name, p.mention_count AS mention_count, p.role AS role, "
            f"{cols} FROM persons p LEFT JOIN person_scores s ON s.name = p.name "
            "ORDER BY p.mention_count DESC LIMIT 1000"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— person_scores 不存在（或列不全）时降级
        scored = False
        rows = DB.conn.execute(
            "SELECT name, mention_count, role FROM persons ORDER BY mention_count DESC LIMIT 1000"
        ).fetchall()
    idx = _player_index()
    site_map = _person_site_map()
    start = max(0, int(offset or 0))
    page = max(0, int(limit or 0))
    rows = rows[start: start + page] if page else rows
    out = []
    for r in rows:
        name = r["name"]
        sites = site_map.get(name) or []
        meta = idx.get(name or "", {})
        item = {
            "name": name,
            "mention_count": r["mention_count"],
            "role": r["role"],
            "sites": sites,
            "tier": meta.get("tier"),
            "platform": meta.get("platform"),
        }
        if scored:
            item.update({c: r[c] for c in _PERSON_SCORE_COLUMNS})
            # 明细/公开战绩解析成对象，前端直接展开；无表时这些键缺失，前端按空处理
            item["score_detail"] = _load_json_or_none(r["score_detail_json"])
            item["public_record"] = _load_json_or_none(r["public_record_json"])
        out.append(item)
    return out
