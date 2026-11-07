# -*- coding: utf-8 -*-
"""P1 抽取 + git checkout 事故后的「审计期 HTTP 桥接端点」重建层。

背景
----
审计期在 server.py 工作区新增了一批把引擎模块接出来的 HTTP 桥接函数
（api/router/*、api/toolbox*、api/ai_expert、api/ai_debate*、api/universe_box、
api/factors/*、api/fast_grab*）。它们**未提交**，在一次恢复操作中被 `git checkout`
冲掉，且 git 无悬空 blob 可恢复（最大悬空 blob 仅 260KB，远小于丢失的 ~870KB）。

底层引擎模块（viewer/tool_router、toolbox_stack、toolbox、ai_expert、universe_box、
debate、factor_import、tools/fast_grab）仍在磁盘、业务逻辑完好。本模块按
「引擎公开签名 + 前端调用点（app.js / debate.html / toolbox.js）+ tests/routes_snapshot.json
的 golden 路由清单」重建这些桥接函数，与既有 157 个 api_* 处理器保持同一风格：
模块级函数、返回 dict、惰性 import 引擎、异常兜底返回 {ok:False, error}。

事实与判断分离（透明标注）
--------------------------
- **事实**：每个端点的业务语义来自对应引擎模块的公开函数签名（逐条对得上）。
- **判断（本次重建的工程决定，前端联调若发现偏差以实际为准）**：
  - HTTP 参数名 / 默认值（limit、horizon_days 等）按引擎签名合理取名；
  - GET/POST 划分：状态变更类（toolbox/status、factors/admit、factors/source_status、
    ai_debate/run、ai_debate/single）判为 POST，其余 GET；
  - /api/router/plan 的 GET 设为 persist=False（只读、不落决策日志，HTTP GET 不应有副作用）；
  - /api/toolbox/stack 的筛道 spec 由前端经 `?tools=<JSON>` 传入（toolbox.js 实测如此）；
  - /api/fast_grab* 原实现已不可考，按 tools/fast_grab.run(write=False) dry-run 重建，
    离线/无网络时返回 {ok:False, error} 而非崩溃。
"""
from __future__ import annotations

import json
import logging
import threading

log = logging.getLogger("viewer.recovery")


def _db():
    """惰性取 server 共享库连接（调用时 server 已完全加载，无循环导入）。"""
    from viewer.server import DB
    return DB


# ───────────────────────── router（决策台内核 tool_router） ─────────────────────────
def api_router_context(as_of: str = "", horizon_days: int = 20,
                       objective: str = "research", stock_code: str = "") -> dict:
    try:
        from viewer import tool_router as _tr
        ctx = _tr.build_context(_db().conn, str(as_of or ""), int(horizon_days or 20),
                                str(objective or "research"), str(stock_code or ""))
        return {"ok": True, "context": ctx}
    except Exception as e:  # noqa: BLE001
        log.exception("router/context 失败")
        return {"ok": False, "error": str(e)}


def api_router_plan(as_of: str = "", horizon_days: int = 20,
                    objective: str = "research", stock_code: str = "",
                    max_candidates: int = 30, risk_level: str = "normal") -> dict:
    try:
        from viewer import tool_router as _tr
        # 只读、不落决策日志（HTTP GET 不应产生副作用）
        return _tr.plan(_db().conn, str(as_of or ""), int(horizon_days or 20),
                        str(objective or "research"), str(stock_code or ""),
                        int(max_candidates or 30), str(risk_level or "normal"),
                        persist=False)
    except Exception as e:  # noqa: BLE001
        log.exception("router/plan 失败")
        return {"ok": False, "error": str(e)}


def api_router_explain(decision_id: str = "") -> dict:
    try:
        from viewer import tool_router as _tr
        return _tr.explain(_db().conn, str(decision_id or ""))
    except Exception as e:  # noqa: BLE001
        log.exception("router/explain 失败")
        return {"ok": False, "error": str(e)}


def api_router_decisions(limit: int = 20) -> dict:
    try:
        from viewer import tool_router as _tr
        return {"ok": True, "decisions": _tr.recent_decisions(_db().conn, int(limit or 20))}
    except Exception as e:  # noqa: BLE001
        log.exception("router/decisions 失败")
        return {"ok": False, "error": str(e)}


def api_router_policy_status() -> dict:
    try:
        from viewer import tool_router as _tr
        return _tr.policy_status()
    except Exception as e:  # noqa: BLE001
        log.exception("router/policy/status 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── toolbox（货架 toolbox / toolbox_stack） ─────────────────────────
def api_toolbox() -> dict:
    try:
        from viewer import toolbox as _tb
        return {"ok": True, "pillar_meta": _tb.pillar_meta(),
                "tools": _tb.list_tools(_db().conn)}
    except Exception as e:  # noqa: BLE001
        log.exception("toolbox 失败")
        return {"ok": False, "error": str(e)}


def api_toolbox_funnel(steps_json: str = "", limit: int = 200) -> dict:
    try:
        from viewer import toolbox as _tb
        steps = []
        if steps_json:
            try:
                raw = json.loads(steps_json)
                if isinstance(raw, list):
                    steps = [s for s in raw if isinstance(s, dict)]
            except Exception:
                steps = []
        return _tb.run_funnel(_db().conn, steps, int(limit or 200))
    except Exception as e:  # noqa: BLE001
        log.exception("toolbox/funnel 失败")
        return {"ok": False, "error": str(e)}


def api_toolbox_stack(limit: int = 200, tools_json: str = "") -> dict:
    try:
        from viewer import toolbox_stack as _ts
        spec = None
        if tools_json:
            try:
                raw = json.loads(tools_json)
                if isinstance(raw, list):
                    spec = []
                    for t in raw:
                        if isinstance(t, dict) and t.get("tool"):
                            spec.append({"tool": t["tool"], "params": t.get("params") or {}})
                        elif isinstance(t, str):
                            spec.append({"tool": t, "params": {}})
            except Exception:
                spec = None
        return _ts.run_stack(_db().conn, spec, int(limit or 200))
    except Exception as e:  # noqa: BLE001
        log.exception("toolbox/stack 失败")
        return {"ok": False, "error": str(e)}


def api_toolbox_status(payload: dict | None = None) -> dict:
    try:
        from viewer import toolbox as _tb
        p = payload or {}
        _tb.set_status(_db().conn, str(p.get("tool_key") or ""),
                       str(p.get("status") or ""), str(p.get("reason") or ""))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        log.exception("toolbox/status 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── ai_expert（四家合并榜，恒 4 条） ─────────────────────────
def api_ai_expert(date: str = "", key: str = "") -> dict:
    """四家专家团当日复盘面板。

    契约（tests/test_ai_expert_desk.py 钉死，重建时严格对齐）：
    - 永远回 4 条（四家恒在），缺报告的那家 status="missing"；
    - 不传 date 时取「已有专家复盘的最大 review_date」（前端日期下拉用），
      空库退回 latest_review_date（今天）；
    - 列表接口不带 report_md 全文；只有 `key` 被显式指定时才给那一条补全文；
    - board 合并打分榜恒在，min_score=ai_expert.MIN_SCORE；
    - 空库首次 GET 必须 ensure_schema 自愈，否则页面永远 4 条 missing。
    """
    try:
        from viewer import ai_expert as _ae
        conn = _db().conn
        # 空库自愈：首次 GET 也要能把表建出来
        try:
            _ae.ensure_schema(conn)
        except Exception:  # noqa: BLE001
            pass
        # 不传 date → 取已有专家复盘的最大日期；空库退回 latest_review_date（今天）
        latest = None
        try:
            row = conn.execute(
                "SELECT MAX(review_date) FROM ai_expert_review").fetchone()
            latest = row[0] if row else None
        except Exception:  # noqa: BLE001
            latest = None
        if not latest:
            latest = _ae.latest_review_date(conn)
        latest = str(latest)
        d = str(date or latest)
        items = _ae.build_items(conn, d, with_report_key=str(key or ""))
        board = _ae.build_board(items, min_score=_ae.MIN_SCORE)
        return {"ok": True, "date": d, "latest_date": latest,
                "items": items, "board": board}
    except Exception as e:  # noqa: BLE001
        log.exception("ai_expert 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── universe_box（全市场箱体波谷） ─────────────────────────
def api_universe_box(limit: int = 40, min_dwell: float = 0.15, box_mode: str = "") -> dict:
    try:
        from viewer import universe_box as _ub
        md = 0.15 if str(min_dwell) in ("", "None") else float(min_dwell)
        return _ub.run(limit=int(limit or 40), min_dwell=md,
                       codes=None, box_mode=(box_mode or None))
    except Exception as e:  # noqa: BLE001
        log.exception("universe_box 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── ai_debate（多空辩论） ─────────────────────────
def api_ai_debate(mode: str = "batch", code: str = "") -> dict:
    try:
        from viewer import debate as _db_
        return _db_.latest(_db().conn, str(mode or "batch"), str(code or ""))
    except Exception as e:  # noqa: BLE001
        log.exception("ai_debate 失败")
        return {"ok": False, "error": str(e)}


def api_ai_debate_run(pool: int = 0, final_min: int = 0, final_max: int = 0,
                      provider: str = "") -> dict:
    """POST 触发批量辩论：同步阻塞风险高，放到后台线程，前端轮询 /api/ai_debate?mode=batch。"""
    def _run():
        con = None
        try:
            import sqlite3
            from viewer import debate as _db_
            con = sqlite3.connect(_db().path, check_same_thread=False, timeout=15)
            con.row_factory = sqlite3.Row
            _db_.debate_research(con, "", str(provider or ""),
                                 int(pool) if pool else None,
                                 final_min=int(final_min or 0) or None,
                                 final_max=int(final_max or 0) or None)
        except Exception:
            log.exception("ai_debate/run 后台失败")
        finally:
            try:
                con.close()
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True,
            "note": "辩论在后台跑，轮询 /api/ai_debate?mode=batch 取结果"}


def api_ai_debate_single(code: str = "", provider: str = "") -> dict:
    try:
        from viewer import debate as _db_
        return _db_.single_stock_research(_db().conn, str(code or ""), str(provider or ""))
    except Exception as e:  # noqa: BLE001
        log.exception("ai_debate/single 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── factors（因子准入 factor_import） ─────────────────────────
def api_factors_candidates(include_rejected: bool = True) -> dict:
    try:
        from viewer import factor_import as _fi
        return {"ok": True, "candidates": _fi.list_candidates(_db().conn, bool(include_rejected))}
    except Exception as e:  # noqa: BLE001
        log.exception("factors/candidates 失败")
        return {"ok": False, "error": str(e)}


def api_factors_sources(limit: int = 40) -> dict:
    try:
        from viewer import factor_import as _fi
        return {"ok": True, "sources": _fi.list_sources(_db().conn, int(limit or 40))}
    except Exception as e:  # noqa: BLE001
        log.exception("factors/sources 失败")
        return {"ok": False, "error": str(e)}


def api_factors_admit(payload: dict | None = None) -> dict:
    try:
        from viewer import factor_import as _fi
        p = payload or {}
        return _fi.admit(_db().conn, str(p.get("key") or ""), str(p.get("name") or ""),
                         str(p.get("expr") or ""), p.get("codes") or [],
                         origin=str(p.get("origin") or ""), why=str(p.get("why") or ""),
                         warmup=int(p.get("warmup") or 0) or None)
    except Exception as e:  # noqa: BLE001
        log.exception("factors/admit 失败")
        return {"ok": False, "error": str(e)}


def api_factors_source_status(payload: dict | None = None) -> dict:
    try:
        from viewer import factor_import as _fi
        p = payload or {}
        return _fi.set_source_status(_db().conn, str(p.get("full_name") or ""),
                                     str(p.get("status") or ""), str(p.get("note") or ""))
    except Exception as e:  # noqa: BLE001
        log.exception("factors/source_status 失败")
        return {"ok": False, "error": str(e)}


# ───────────────────────── fast_grab（快照抓取 tools/fast_grab） ─────────────────────────
def api_fast_grab(date: str = "") -> dict:
    try:
        import tools.fast_grab as _fg
        return _fg.run(date=str(date or None), write=False)
    except Exception as e:  # noqa: BLE001
        log.exception("fast_grab 失败")
        return {"ok": False, "error": str(e)}


def api_fast_grab_status(date: str = "") -> dict:
    try:
        import tools.fast_grab as _fg
        # 轻量：复用 run(write=False) 的摘要（不落盘、不取数）
        r = _fg.run(date=str(date or None), write=False)
        if isinstance(r, dict):
            r = {k: r.get(k) for k in ("ok", "date", "new_codes", "total", "note") if k in r}
        return {"ok": True, "status": r}
    except Exception as e:  # noqa: BLE001
        log.exception("fast_grab/status 失败")
        return {"ok": False, "error": str(e)}
