# -*- coding: utf-8 -*-
"""strategy 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_strategy_templates(template=None, limit=200):
    """P5：现成策略模板选股结果。template 可指定单个模板（否则返回全部模板的最新命中），
    按模板分组。落库 stock_strategy_picks(template,code,name,score,reasons,updated)。"""
    import json as _json
    out = {"ok": True, "templates": [], "groups": {}}
    try:
        # 单一事实源：直接读 strategy_templates.TEMPLATES（含 P4 实证 alpha 背书），
        # 不再硬编码 META（避免代码与模板定义两处不一致）。
        from strategy_templates import TEMPLATES
        for tpl in TEMPLATES:
            key = tpl["key"]
            cnt = DB.conn.execute(
                "SELECT COUNT(*) FROM stock_strategy_picks WHERE template=?", (key,)).fetchone()[0]
            out["templates"].append({
                "key": key, "name": tpl["name"], "desc": tpl["desc"],
                "risk": tpl["risk"], "count": cnt, "alpha": tpl.get("alpha")})
        where = ""
        args = ()
        if template:
            where = "WHERE template=?"
            args = (template,)
        rows = DB.conn.execute(
            "SELECT template,code,name,score,reasons,updated FROM stock_strategy_picks %s "
            "ORDER BY template, score DESC LIMIT ?" % where, args + (limit,)).fetchall()
        for tpl, code, name, score, reasons, updated in rows:
            out["groups"].setdefault(tpl, []).append({
                "code": code, "name": name, "score": score,
                "reasons": _json.loads(reasons or "[]"), "updated": updated})
    except Exception as e:  # noqa: BLE001
        out["ok"] = False; out["error"] = str(e)
    return out
