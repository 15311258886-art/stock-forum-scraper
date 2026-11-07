# -*- coding: utf-8 -*-
"""kb 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    KB_DEFAULT_KEY,
    KB_INTERVAL_SECONDS,
    KB_STALE_SECONDS,
    _kb_sources,
    kb,
)

def api_kb_list() -> dict:
    """知识库索引：已吸取的文档 + 配置里声明但尚未吸取的来源，一并列出。

    `group` 与 `order`（左侧一级分类归属 + 横条内的排序）**只从 yaml 读、不落库**——
    它们属于展示配置而非内容，从配置读的好处是改一行 yaml 刷新页面即生效，
    不必为了挪个位置去重建整份知识库。

    2026-09-26：原 `learn`（跟我学习）与 `acquire`（知识获取）两个一级分类已合并为一个
    （前端 NAV 的 key 仍沿用 learn），故 group 现缺省一律为 learn；`order` 数值小的排前面
    （缺省 0，同值时保持库内既有顺序），用来固定「基础常识」等轨道的横条位置。
    """
    rows = DB.kb_list()
    cfgs = {c.get("doc_key") or "kb": c for c in _kb_sources()}

    def group_of(cfg: dict) -> str:
        return (str(cfg.get("group") or "").strip() or "learn")

    def order_of(cfg: dict) -> int:
        """轨道在横向子板块里的排序权重。写非数字/不写一律按 0 处理（而非报错整表挂掉）。"""
        try:
            return int(cfg.get("order") or 0)
        except (TypeError, ValueError):
            return 0

    for r in rows:
        cfg = cfgs.get(r["doc_key"]) or {}
        r["track"] = r.get("track") or cfg.get("track") or ""
        r["group"] = group_of(cfg)
        r["order"] = order_of(cfg)
        r["configured"] = bool(cfg)
        r["source_exists"] = bool(kb.resolve_source(cfg)) if cfg else False
        # 内容是否相对上次发生变化（kb_save 写入）：前端「有更新」角标用
        r["changed"] = bool(r.get("changed"))
    have = {r["doc_key"] for r in rows}
    for key, cfg in cfgs.items():
        if key not in have:
            rows.append({
                "doc_key": key, "track": cfg.get("track") or "",
                "group": group_of(cfg), "order": order_of(cfg),
                "title": cfg.get("title") or key, "version": "", "status": "never",
                "node_count": 0, "ingested_at": "", "source_path": kb.resolve_source(cfg) or "",
                "configured": True,
                "source_exists": bool(kb.resolve_source(cfg)), "changed": False,
            })
    return {"docs": rows, "default_key": KB_DEFAULT_KEY,
            "interval_seconds": KB_INTERVAL_SECONDS,
            "stale_seconds": KB_STALE_SECONDS}
def api_kb_doc(doc_key: str | None = None, light: bool = False) -> dict:
    """取一份知识库文档（**扁平结构**，前端不必在嵌套 JSON 里找键）。

    light=True 只回 meta + outline（几 KB），供汇总卡片/导航预检；
    默认回完整正文（约 250 KB），含 8 流派 / 50 博主 / 33 条回测明细。
    """
    d = DB.kb_get(doc_key or None)
    if not d:
        cfg = next((c for c in _kb_sources()
                    if (c.get("doc_key") or "kb") == (doc_key or KB_DEFAULT_KEY)), None)
        return {
            "doc_key": doc_key or KB_DEFAULT_KEY, "status": "empty",
            "track": (cfg or {}).get("track") or "", "title": (cfg or {}).get("title") or "",
            "version": "", "node_count": 0, "char_count": 0, "ingested_at": "",
            "source_path": kb.resolve_source(cfg) if cfg else "",
            "parser_version": 0, "parser_current": kb.PARSER_VERSION, "parser_stale": False,
            "meta": {}, "outline": [],
            "message": "尚未吸取知识库。点「立即吸取」从来源文件重建，"
                       "或到「学习轨道管理」检查来源路径。",
        }
    env = d.get("envelope") or {}
    body = d.get("doc") or {}
    out = {
        "doc_key": d.get("doc_key"),
        "track": d.get("track") or env.get("track") or "",
        "title": env.get("title") or d.get("title") or "",
        "version": d.get("version") or env.get("version") or "",
        "source_path": d.get("source_path") or env.get("source_path") or "",
        "source_mtime": d.get("source_mtime") or env.get("source_mtime") or "",
        "content_hash": d.get("content_hash") or "",
        "char_count": d.get("char_count") or 0,
        "node_count": d.get("node_count") or 0,
        "status": d.get("status") or env.get("status") or "ok",
        "error": d.get("error") or env.get("error") or "",
        "ingested_at": d.get("ingested_at") or "",
        "prev_hash": d.get("prev_hash") or "",
        "prev_ingested_at": d.get("prev_ingested_at") or "",
        "changed": bool(d.get("changed")),
        "reload_count": d.get("reload_count") or 0,
        # 解析器版本：库中存值与当前代码值不等 → 这份正文是旧解析逻辑的产物，
        # 点一次「立即吸取」即可按新逻辑重建（上游文件没变也照样重建）。
        "parser_version": int(d.get("parser_version") or env.get("parser_version") or 0),
        "parser_current": kb.PARSER_VERSION,
        "parser_stale": int(d.get("parser_version") or env.get("parser_version") or 0)
                        != kb.PARSER_VERSION,
        "meta": body.get("meta") or {},
        "outline": body.get("outline") or [],
    }
    if d.get("payload_error"):
        out["payload_error"] = d["payload_error"]
    if not light:
        # 正文各区块**按 payload 原样透传**。
        # 2026-09-26 修：原先是白名单式逐个列举（schools/conclusions/compare/pitfalls/
        # backtests/risks/sources），新增文档类型时会静默丢字段——例如词条型知识库
        # （kind="glossary"）的 groups / kind 都不在白名单里，结果是"数据已吸进库、
        # 接口也回 200、但页面一片空白"，排查时极难定位。改为除 meta/outline 外全透传。
        for _k, _v in body.items():
            if _k in ("meta", "outline"):
                continue
            out.setdefault(_k, _v)
    return out
