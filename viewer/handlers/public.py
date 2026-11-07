# -*- coding: utf-8 -*-
"""public 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_public_records() -> list[dict]:
    """公开年赛战绩榜（外部参照）。

    背景：用户要看的核心是「谁用多少钱做到了多少钱」。这是本库里**唯一**带真实收益的
    公开战绩（淘股吧官方年赛 / 淘实盘榜单），但它与 persons 表**零交集**（竞赛账号 vs 本库
    抓取的新浪/东财用户），所以单独成榜、不参与本库人物评分排名。

    口径：
    - 每个昵称取**代表条**，规则（2026-09-26 QA 复核后修正）：
      ① **优先取「可核验」的那一期**（source_kind='article' 且有 source_url）；
      ② 在同类里取 as_of_date 最新的一期；同日期并列取收益更高者（保证幂等）。
      修正原因：原来纯按日期取最新，导致榜首 ra1en 展示 09-24 的 1177.26（搜索快照、无链接、
      不可核验），却把他有原文可核验的 09-21 = 941.47 藏起来——方向反了。用户明确要求
      「能查到的标注来源」，故宁可展示可核验的较早值。该人**完全没有任何带链接的记录**时，
      才退回到快照里的最新一期。
    - 「截至日期」列显示该代表条真实的 as_of_date，让口径透明（不让人误以为是今天的数据）。
    - 按 total_return 降序（无收益的排最后）。
    - has_link：只有「有稳定原文链接且来源不是搜索引擎快照」时才为 True，前端据此决定是否渲染外链。
      绝不造一个假链接——用户明确要求「能查到的标注来源，查不到就留空」。

    健壮性：public_records 表不存在时返回空数组（优雅降级，不 500）。
    """
    try:
        rows = DB.conn.execute(
            "SELECT nickname, platform, group_name, total_return, as_of_date, source_url, source_kind "
            "FROM public_records"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 表不存在时降级为空榜
        return []
    best: dict[str, tuple] = {}
    for r in rows:
        d = dict(r)
        nick = d.get("nickname")
        if not nick:
            continue
        # 可核验 = 有原文链接且非搜索快照
        verifiable = 1 if (d.get("source_url") and d.get("source_kind") == "article") else 0
        # 排序键：(可核验优先, as_of 越新越好, 收益越高越好)；三者全是可比较的确定值 → 幂等
        ret = d.get("total_return") if d.get("total_return") is not None else -1e18
        key = (verifiable, d.get("as_of_date") or "", ret)
        prev = best.get(nick)
        if prev is None or key > prev[0]:
            best[nick] = (key, d)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda x: (x.get("total_return") is None, -(x.get("total_return") or 0)))
    for d in out:
        d["has_link"] = bool(d.get("source_url")) and (d.get("source_kind") != "search_snapshot")
    return out
