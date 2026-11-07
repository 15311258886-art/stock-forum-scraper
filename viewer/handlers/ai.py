# -*- coding: utf-8 -*-
"""ai 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json, logging, threading, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _AI_REVIEW_LOCK,
    _AI_REVIEW_TTL,
    _ai_review_maxid,
    _ai_review_mod,
)

def api_ai_review(date: str = "", provider: str = "", force: int = 0,
                  since_id: int = 0) -> dict:
    """读最新一份 AI 复盘。没有 / 已过期 / 比 since_id 旧就起后台线程去算，本请求立刻返回 computing。

    绝不在这里同步调模型：一次完整复盘要 10~60s，放在请求线程里，
    用户点了之后页面会一直转圈，且后端所有请求都排在后面 —— 正是「点开一个功能就卡死」的成因。

    since_id 是**结果水位线**，专门解决一个真实踩到的坑：
    点「重新分析」后后台要 10s 才算完，这段时间里轮询如果只看「有没有缓存」，
    会立刻拿到**上一份旧结果**并误判成"算完了"，页面显示的还是旧判断（库里也确实还是旧 id）。
    所以 force 重算时把当时的 MAX(id) 回给前端，前端轮询带着它；
    只有出现 id > since_id 的新记录，才算这一轮真的出结果了。
    """
    log = logging.getLogger(__name__)
    try:
        AR = _ai_review_mod()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "state": "error", "error": "ai_review 模块加载失败：%s" % str(e)[:150]}

    try:
        AR.ensure_schema(DB.conn)
    except Exception:  # noqa: BLE001
        log.exception("ai_review 建表失败")

    provs = AR.available_providers()
    ready = [p["key"] for p in provs if p.get("ready")]
    provider = (provider or "").strip() or (ready[0] if ready else AR.DEFAULT_PROVIDER)
    if provider not in AR.PROVIDERS:
        provider = AR.DEFAULT_PROVIDER

    date = (date or "").strip() or AR.latest_trade_date(DB.conn)
    cur_max = _ai_review_maxid(DB.conn)

    # ---- 1) 命中缓存（同日同模型、未过 TTL、且没要求强制重算、且比水位线新）
    if not force:
        cached = AR.latest(DB.conn, date=date, provider=provider)
        if cached and cached.get("created_at"):
            fresh_enough = int(cached.get("id") or 0) > int(since_id or 0)
            try:
                age = time.time() - time.mktime(
                    time.strptime(cached["created_at"], "%Y-%m-%d %H:%M:%S"))
            except Exception:  # noqa: BLE001
                age = _AI_REVIEW_TTL + 1
            if fresh_enough and age < _AI_REVIEW_TTL:
                return {"ok": True, "state": "ok", "cached": True, "age_sec": int(age),
                        "since_id": cur_max, "providers": provs, **cached}

    # ---- 1.5) WorkBuddy 通道（2026-10-29）：本进程**不调模型**，只把分析包放进队列。
    # 与 deepseek/hunyuan/qwen 的本质区别：那三家由本进程同步调 API；WorkBuddy 由主代理
    # 通过 MCP 取包、分析、回填。因此这里既不起后台线程，也不受本机 API Key 限制。
    # 前端完全无感：它读的仍是 /api/ai_review，拿到 state=computing 就继续轮询，
    # 回填后 ai_review 主表出现新记录（id > since_id），轮询自然接上。
    if provider == "workbuddy":
        try:
            if force:
                # 「重新分析」语义：把该日尚未被取走的旧任务作废，避免拿到上一次的旧包
                try:
                    DB.conn.execute(
                        "UPDATE ai_review_queue SET state='error', err='superseded by force rerun', done_at=? "
                        "WHERE review_date=? AND provider='workbuddy' AND state IN ('pending','taken')",
                        (time.strftime("%Y-%m-%d %H:%M:%S"), date))
                    DB.conn.commit()
                except Exception:  # noqa: BLE001 —— 队列可能尚未建表，交给 queue_ensure 兜住
                    pass
            q = AR.queue_ensure(DB.conn, date, provider)
        except Exception as e:  # noqa: BLE001
            log.exception("ai_review 入队失败")
            return {"ok": False, "state": "error", "error": "入队失败：%s" % str(e)[:150]}
        if q.get("state") == "error":
            return {"ok": False, "state": "error", "error": q.get("err") or "入队失败"}
        item = (AR.queue_status(DB.conn, q.get("queue_id") or 0) or {}).get("item") or {}
        # 等待时长：队列里躺了多久（前端据此显示"已入队 N 秒"，而不是永远 0）
        try:
            _ca = q.get("created_at") or item.get("created_at") or ""
            _elapsed = int(time.time() - time.mktime(time.strptime(_ca, "%Y-%m-%d %H:%M:%S"))) if _ca else 0
        except Exception:  # noqa: BLE001
            _elapsed = 0
        return {
            "ok": True, "state": "computing", "cached": False,
            "provider": provider, "review_date": date,
            "since_id": cur_max, "providers": provs,
            "queue_id": q.get("queue_id"),
            "queue_state": item.get("state") or q.get("state"),
            "brief_chars": q.get("brief_chars") or 0,
            "elapsed_sec": max(0, _elapsed),
            "note": ("已放入 WorkBuddy 分析队列（任务 #%s，信息包 %s 字）。"
                     "在 WorkBuddy 里说「分析今天的复盘」即可回填结果。"
                     % (q.get("queue_id") or "-", q.get("brief_chars") or "—")),
        }

    # ---- 2) 已在后台算着 → 告诉前端继续轮询，别重复起线程
    with _AI_REVIEW_LOCK:
        started = _AI_REVIEW_RUNNING.get(provider)
        running = bool(started and (time.time() - started) < 300)
        if not running:
            _AI_REVIEW_RUNNING[provider] = time.time()
            started = _AI_REVIEW_RUNNING[provider]

    if running:
        return {"ok": True, "state": "computing", "cached": False,
                "elapsed_sec": int(time.time() - (started or time.time())),
                "since_id": int(since_id or 0), "providers": provs, "review_date": date}

    # ---- 3) 起后台线程真算
    def _run() -> None:
        try:
            res = AR.run(DB.conn, date=date, provider=provider)
            logging.info("[ai_review] 完成 date=%s prov=%s ok=%s %.1fs tokens=%s%s",
                         date, provider, res.get("ok"), (res.get("elapsed_ms") or 0) / 1000.0,
                         res.get("tokens"), (" err=" + res["err"]) if res.get("err") else "")
        except Exception:  # noqa: BLE001 —— 后台失败只记日志，接口早已返回
            logging.exception("[ai_review] 后台计算失败")
        finally:
            with _AI_REVIEW_LOCK:
                _AI_REVIEW_RUNNING.pop(provider, None)

    try:
        threading.Thread(target=_run, name="ai-review-%s" % provider, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        with _AI_REVIEW_LOCK:
            _AI_REVIEW_RUNNING.pop(provider, None)
        return {"ok": False, "state": "error", "error": "后台启动失败：%s" % str(e)[:150]}

    return {"ok": True, "state": "computing", "cached": False, "elapsed_sec": 0,
            "since_id": cur_max, "providers": provs, "review_date": date,
            "note": "已在后台用 %s 分析，约 10~60 秒后自动出结果" % provider}
def api_ai_review_run(provider: str = "", date: str = "", since_id: int = 0) -> dict:
    """强制重算（POST）。与 GET 的区别仅在于跳过 TTL 缓存。"""
    return api_ai_review(date=date, provider=provider, force=1, since_id=since_id)
def api_ai_review_history(limit: int = 30) -> dict:
    """历史复盘列表 —— 让「每天一份、可以回看、能对比兑现没有」成立。"""
    try:
        AR = _ai_review_mod()
        AR.ensure_schema(DB.conn)
        rows = AR.history(DB.conn, limit=limit)
        return {"ok": True, "items": rows, "providers": AR.available_providers()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:150]), "items": []}
def api_ai_review_detail(review_id: int = 0) -> dict:
    """单份复盘详情 + 它当时喂进去的信息包（审计：用户有权知道 AI 读了什么）。"""
    try:
        AR = _ai_review_mod()
        AR.ensure_schema(DB.conn)
        rows = DB.conn.execute("SELECT * FROM ai_review WHERE id=?", (int(review_id),)).fetchall()
        if not rows:
            return {"ok": False, "error": "没有这条复盘记录"}
        d = AR.digest_of(rows[0])
        try:
            d["context"] = json.loads(rows[0]["context_json"] or "{}")
        except Exception:  # noqa: BLE001
            d["context"] = {}
        return {"ok": True, **d}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
def api_ai_provider_key(provider: str, key: str) -> dict:
    """给 AI 复盘面板的任一大脑填 Key（2026-11-02）。

    此前只有 `/api/settings/deepseek_key` 一个入口，硬编码 deepseek ——
    于是混元、通义千问虽然 provider 早就写好了，界面上却**根本没有地方填 Key**，
    按钮永远是灰的「（未配 Key）」。那不是"没配"，是"配不了"。
    """
    try:
        import ai_review as AIR
        return AIR.set_provider_key(provider, key)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "保存失败：%s: %s" % (type(e).__name__, str(e)[:160])}
def api_ai_provider_test(provider: str) -> dict:
    """只测连通性，不写任何东西 —— 排查"Key 明明填了为什么还失败"时用。"""
    try:
        import ai_review as AIR
        return AIR.test_provider(provider)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])}
def api_ai_picks(date: str = "", provider: str = "") -> dict:
    """AI 荐股板（2026-11-02）：某一份复盘的票单 + 它现在的实际表现 + 累计成绩。

    三个部分一次给全，前端不用拼三个接口：
      · board   —— 这一次推了哪几只，各自的分数/价位/逻辑，以及次日与 T+N 的真实涨跌
      · score   —— 累计成绩单（命中率/上涨率/平均收益/超额，分档）
      · history —— 有复盘记录的日期列表（给日期选择器用）
    """
    out: dict = {"ok": True}
    try:
        import review_score as RS
        RS.score_pending(DB.conn)          # 先回填，保证页面看到的是最新事实
        out["board"] = RS.picks_board(DB.conn, date, provider)
        out["score"] = RS.report(DB.conn)
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["reason"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    try:
        rows = DB.conn.execute(
            "SELECT review_date, provider, COUNT(*) FROM ai_review "
            "WHERE status='ok' GROUP BY review_date, provider "
            "ORDER BY review_date DESC, provider LIMIT 60").fetchall()
        out["history"] = [{"date": r[0], "provider": r[1], "n": r[2]} for r in rows]
    except Exception:  # noqa: BLE001
        out["history"] = []
    return out
