# -*- coding: utf-8 -*-
"""stocks 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    _named,
    postdigest,
)

def api_stocks() -> list[dict]:
    # sources：该股讨论帖来自哪些来源站点（Request G：「最后更新」列/角标据此判定时效）
    rows = DB.conn.execute(
        """SELECT symbol, symbol_name, COUNT(*) AS mentions,
                  MAX(p.post_time) AS last_time,
                  GROUP_CONCAT(DISTINCT p.site) AS sources
           FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref
           GROUP BY symbol ORDER BY mentions DESC LIMIT 50"""
    ).fetchall()
    if not rows:
        # 回退：从 trades 取操作过的股票
        rows = DB.conn.execute(
            "SELECT symbol, symbol_name, COUNT(*) AS mentions, MAX(post_time) AS last_time, "
            "GROUP_CONCAT(DISTINCT site) AS sources "
            "FROM trades WHERE symbol IS NOT NULL GROUP BY symbol ORDER BY mentions DESC LIMIT 50"
        ).fetchall()
    out = []
    for r in rows:
        n = _named(r["symbol"], r["symbol_name"] or "")
        name = n["name"]
        out.append({
            "symbol": r["symbol"],
            "symbol_name": name,
            "name": name,               # 与 /api/sentiment 字段对齐，前端可统一渲染
            "company": n["company"],
            "market": n["market"],
            "name_missing": n["name_missing"],
            "sources": [s for s in (r["sources"] or "").split(",") if s],
            # label 供旧版前端/其他调用方兼容；新版界面按 name/code 分栏展示
            "label": f"{name}（{r['symbol']}）" if name else f"{r['symbol']}（名称待补全）",
            "mentions": r["mentions"],
            "last_time": r["last_time"] or "",
        })
    # 2026-10-12 体验审计 P0-C：「人气高」≠「被看好」—— 给每只票补「人群态度」三态。
    # 数据来自帖子情绪速览（postdigest，与「汇总」面板同一把尺子、近 72 小时窗口）。
    # 没被任何帖子明确表态的票 posture 为 null，前端只显示「人气」不硬贴标签（不编方向）。
    try:
        pd = postdigest.build(hours=72)
        vmap = {s.get("key"): s for s in (pd.get("stocks") or []) if s.get("key")}
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("api_stocks 情绪三态附加失败: %s", e)
        vmap = {}
    for it in out:
        s = vmap.get(it["symbol"])
        if not s:
            continue
        it["posture"] = s.get("verdict_key")
        it["posture_text"] = s.get("verdict")
        it["posture_pos"] = s.get("pos") or 0
        it["posture_neg"] = s.get("neg") or 0
        it["posture_posts"] = s.get("posts") or 0
    return out
