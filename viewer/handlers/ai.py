# -*- coding: utf-8 -*-
"""ai 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json, logging, re, threading, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _AI_REVIEW_LOCK,
    _AI_REVIEW_TTL,
    _ai_review_maxid,
    _ai_review_mod,
    _AI_REVIEW_RUNNING,
)
import viewer.server as _srv  # 动态命名空间：DB/_AI_REVIEW_RUNNING 等被测试 monkeypatch S.* 时必须走这里才生效


# ---- WorkBuddy 兜底（P0 修复：WorkBuddy 队列没人接单时，自动改用本地大脑代出）----
# 2026-11-03 现场：WorkBuddy 队列没人取包 → 接口永远 computing → 页面永远转圈十几分钟。
# 兜底阈值：队列躺超过这个秒数仍 pending/taken 就触发；连败达上限则停手避免请求风暴。
_AI_WB_FALLBACK_SEC = 30          # pending 任务躺超过这个秒数没人接单 → 触发兜底
_WB_FALLBACK_MAX_FAIL = 3         # 连败达上限 → 熔断，避免请求风暴
_AI_WB_TAKEN_STALE_SEC = 300      # taken 任务取走超过 5 分钟没回填 → 判死单转兜底（<5分钟不抢跑）
_AI_WB_FALLBACK_ETA_SEC = 30      # 兜底预计耗时（秒），仅给前端展示用


def _pick_fallback_provider(exclude: str = "") -> str:
    """在有 Key 且不是 WorkBuddy 自己的大脑里，按 PROVIDERS 顺序挑第一个。

    workbuddy 恒定返回 "local"（可用），但它是"没人接单的那一方"，绝不能选它自己兜底。
    """
    AR = _ai_review_mod()
    for name in AR.PROVIDERS:
        if name == "workbuddy" or name == exclude:
            continue
        if AR.provider_key(name):
            return name
    return ""


def _active_wb_task(conn, date: str):
    """取该日 workbuddy 队列里仍活跃（pending/taken）的最新任务。

    taken 任务用 taken_at 量"取走多久"，pending 用 created_at 量"排队多久"——两者阈值不同。
    """
    try:
        r = conn.execute(
            "SELECT id, state, created_at, taken_at, brief_chars FROM ai_review_queue "
            "WHERE review_date=? AND provider='workbuddy' AND state IN ('pending','taken') "
            "ORDER BY id DESC LIMIT 1", (date,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "state": r[1], "created_at": r[2],
                "taken_at": r[3], "brief_chars": r[4] or 0}
    except Exception:  # noqa: BLE001
        return None


def _done_wb_task(conn, date: str):
    """取该日 workbuddy 队列里最近一条已完成（done）任务：(qid, err)。"""
    try:
        r = conn.execute(
            "SELECT id, err FROM ai_review_queue "
            "WHERE review_date=? AND provider='workbuddy' AND state='done' "
            "ORDER BY id DESC LIMIT 1", (date,)).fetchone()
        return (int(r[0]), r[1] or "") if r else None
    except Exception:  # noqa: BLE001
        return None


def _wb_fallback_fail_count(conn, date: str) -> int:
    """该日 workbuddy 队列里"兜底已失败"的任务数（连败熔断用）。"""
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ai_review_queue "
            "WHERE review_date=? AND provider='workbuddy' AND err LIKE 'auto-fallback failed%'",
            (date,)).fetchone()[0]
        return int(n or 0)
    except Exception:  # noqa: BLE001
        return 0


def _find_fresh_result(conn, date: str):
    """该日是否已有 TTL 内的非 WorkBuddy 成功复盘（可被兜底直接复用，避免重复烧钱）。

    返回 {"id": rid, "provider": name} 或 None。只查非 workbuddy 的 ok 记录，且按创建时间判 TTL。
    """
    try:
        rows = conn.execute(
            "SELECT id, provider, created_at FROM ai_review "
            "WHERE review_date=? AND provider<>'workbuddy' AND status='ok' "
            "ORDER BY id DESC", (date,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    for r in rows:
        _ca = r[2] or ""
        try:
            age = now - time.mktime(time.strptime(_ca, "%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            age = _AI_REVIEW_TTL + 1
        if age < _AI_REVIEW_TTL:
            return {"id": r[0], "provider": r[1]}
    return None


def _mark_taken_stale(conn, qid: int, taken_sec: int = 0) -> None:
    """taken 任务超时没人回填 → 标 error 留痕（审计要留"取走过但没回填"）。"""
    try:
        err = "stale taken: 取走 %d 秒未回填，已判死单转兜底" % int(taken_sec)
        conn.execute(
            "UPDATE ai_review_queue SET state='error', err=? "
            "WHERE id=? AND state='taken'", (err, int(qid)))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _spawn_wb_fallback(date: str, fb: str, qid: int) -> bool:
    """后台用 fb 大脑代出复盘，并把队列任务标 done（err 带 auto-fallback 前缀 + review_id）。

    防重入：同一 fb 已在跑就返回 False。不翻已作废的任务（force superseded 不许翻回 done）。
    """
    # 同样动态取 server 命名空间，使测试 monkeypatch S.DB / S._AI_REVIEW_RUNNING 生效。
    DB = _srv.DB
    _AI_REVIEW_RUNNING = _srv._AI_REVIEW_RUNNING
    key = "wb-fallback:%s" % fb
    if _AI_REVIEW_RUNNING.get(key):
        return False

    def _run() -> None:
        try:
            AR = _ai_review_mod()
            # 复用优先：该日已有 TTL 内新鲜结果 → 直接结清队列，绝不再花一次 token
            fresh = _find_fresh_result(DB.conn, date)
            if fresh:
                DB.conn.execute(
                    "UPDATE ai_review_queue SET state='done', done_at=?, err=? "
                    "WHERE id=? AND state IN ('pending','taken','error') "
                    "AND (err IS NULL OR err NOT LIKE 'superseded%')",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),
                     "auto-fallback: 复用已有 %s 结果（review_id=%s）"
                     % (fresh["provider"], fresh["id"]), int(qid)))
                DB.conn.commit()
                return
            res = AR.run(DB.conn, date=date, provider=fb)
            rid = None
            if res.get("ok"):
                rev = AR.latest(DB.conn, date=date, provider=fb)
                rid = (rev or {}).get("id")
                DB.conn.execute(
                    "UPDATE ai_review_queue SET state='done', done_at=?, err=? "
                    "WHERE id=? AND (state IN ('pending','taken') "
                    "OR (state='error' AND err LIKE 'stale taken:%'))",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),
                     "auto-fallback: WorkBuddy 未接单，已由 %s 代出（review_id=%s）" % (fb, rid),
                     int(qid)))
            else:
                # 失败必须把任务标 error，绝不能留在 pending 永久卡死
                DB.conn.execute(
                    "UPDATE ai_review_queue SET state='error', err=? "
                    "WHERE id=? AND (state IN ('pending','taken') "
                    "OR (state='error' AND err LIKE 'stale taken:%'))",
                    ("auto-fallback failed: %s" % (res.get("err") or "unknown"), int(qid)))
            DB.conn.commit()
        except Exception:  # noqa: BLE001
            logging.exception("[ai_review] 兜底结算异常 fb=%s date=%s qid=%s", fb, date, qid)
            try:
                DB.conn.execute(
                    "UPDATE ai_review_queue SET state='error', err='auto-fallback failed: exception' "
                    "WHERE id=? AND state IN ('pending','taken')", (int(qid),))
                DB.conn.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            _AI_REVIEW_RUNNING.pop(key, None)

    _AI_REVIEW_RUNNING[key] = time.time()
    threading.Thread(target=_run, name="wb-fallback-%s" % fb, daemon=True).start()
    return True


def _wb_computing(qid, elapsed, fb, note, date, cur_max, provs,
                  queue_state=None, taken_sec=None, fallback_eta=None,
                  include_fallback=True) -> dict:
    d = {"ok": True, "state": "computing", "cached": False,
         "provider": "workbuddy", "review_date": date,
         "queue_id": qid, "elapsed_sec": max(0, int(elapsed)),
         "since_id": cur_max, "providers": provs, "note": note}
    if include_fallback:
        d["fallback"] = fb
    if fallback_eta is not None:
        d["fallback_eta_sec"] = fallback_eta
    if queue_state is not None:
        d["queue_state"] = queue_state
    if taken_sec is not None:
        d["taken_sec"] = taken_sec
    return d

def api_ai_review(date: str = "", provider: str = "", force: int = 0,
                  since_id: int = 0, peek: int = 0) -> dict:
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

    # 动态取 server 当前 DB / _AI_REVIEW_RUNNING：拆分后 handlers 按值导入会绕过测试对
    # S.DB / S._AI_REVIEW_RUNNING 的 monkeypatch，必须改走 viewer.server 命名空间才能生效。
    DB = _srv.DB
    _AI_REVIEW_RUNNING = _srv._AI_REVIEW_RUNNING

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

    # ---- 1.5) WorkBuddy 通道：本进程不调模型，只把分析包放进队列，由 WorkBuddy 取包回填。
    # 但若队列躺太久没人接单（P0 现场：页面永远转圈十几分钟），自动改用本地大脑（DeepSeek 等）代出。
    if provider == "workbuddy":
        fb = _pick_fallback_provider()
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
        try:
            active = _active_wb_task(DB.conn, date)
            done = _done_wb_task(DB.conn, date)
        except Exception as e:  # noqa: BLE001
            log.exception("ai_review 队列查询失败")
            return {"ok": False, "state": "error", "error": "队列查询失败：%s" % str(e)[:150]}
        fresh = _find_fresh_result(DB.conn, date) if not force else None

        # 0) 该日已有 TTL 内新鲜结果 → 直接复用，0 次模型调用、0 秒、结清队列（不重复烧钱）
        if fresh and not force and not done:
            if active:
                try:
                    DB.conn.execute(
                        "UPDATE ai_review_queue SET state='done', done_at=?, err=? "
                        "WHERE id=? AND state IN ('pending','taken')",
                        (time.strftime("%Y-%m-%d %H:%M:%S"),
                         "auto-fallback: 复用已有 %s 结果（review_id=%s）"
                         % (fresh["provider"], fresh["id"]), int(active["id"])))
                    DB.conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            _fr = DB.conn.execute("SELECT * FROM ai_review WHERE id=?", (fresh["id"],)).fetchone()
            _frev = AR.digest_of(_fr) if _fr else {}
            _cj = _fr["context_json"] if (_fr is not None and "context_json" in _fr) else ""
            _cstats = AR.context_brief(_cj or "{}")
            return {"ok": True, "state": "ok", "cached": False,
                    "provider": "workbuddy", "review_date": date,
                    "fallback_from": fresh["provider"], "id": fresh["id"],
                    "fallback_queue_id": (active["id"] if active else 0),
                    "since_id": cur_max, "providers": provs,
                    "data": _frev.get("data"), "context_stats": _cstats,
                    "created_at": _frev.get("created_at"),
                    "note": "该日已有 %s 结果，已直接复用（0 秒出结果，不重复烧钱）" % fresh["provider"]}

        # 1) 有活跃任务（pending/taken）→ 等接单；超时且可兜底则自动代出
        if active and not peek:
            qid = active["id"]
            _is_taken = active["state"] == "taken"
            _ca = (active.get("taken_at") if _is_taken else active.get("created_at")) or ""
            try:
                _elapsed = int(time.time() - time.mktime(time.strptime(_ca, "%Y-%m-%d %H:%M:%S"))) if _ca else 0
            except Exception:  # noqa: BLE001
                _elapsed = 0
            _thr = _AI_WB_TAKEN_STALE_SEC if _is_taken else _AI_WB_FALLBACK_SEC
            if fb and _elapsed >= _thr:
                _fk = "wb-fallback:%s" % fb
                if _is_taken:
                    # taken 死单：先留痕再兜底，绝不抢跑（<阈值时对方可能正算到一半）
                    _mark_taken_stale(DB.conn, qid, _elapsed)
                    _srv._spawn_wb_fallback(date, fb, qid)
                    return _wb_computing(qid, _elapsed, fb,
                                         "WorkBuddy 取走后 %d 秒没回填，已判死单并自动改用 %s 代出，不用你操作" % (_elapsed, fb),
                                         date, cur_max, provs,
                                         queue_state="taken", taken_sec=_elapsed,
                                         fallback_eta=_AI_WB_FALLBACK_ETA_SEC)
                if _AI_REVIEW_RUNNING.get(_fk):
                    return _wb_computing(qid, _elapsed, fb, "兜底计算中（已在后台用 %s 代出）" % fb,
                                         date, cur_max, provs, queue_state="pending",
                                         fallback_eta=_AI_WB_FALLBACK_ETA_SEC)
                if _wb_fallback_fail_count(DB.conn, date) >= _WB_FALLBACK_MAX_FAIL:
                    return _wb_computing(qid, _elapsed, None,
                                         "兜底已连续失败 %d 次，先不再自动重试；请检查本地大脑 Key 或稍后重试" % _WB_FALLBACK_MAX_FAIL,
                                         date, cur_max, provs, queue_state="pending")
                _srv._spawn_wb_fallback(date, fb, qid)
                return _wb_computing(qid, _elapsed, fb,
                                     "WorkBuddy 队列 %d 秒未接单，已自动改用 %s 出结果，不用你操作；约 10~60 秒后刷新即可见" % (_elapsed, fb),
                                     date, cur_max, provs, queue_state="pending",
                                     fallback_eta=_AI_WB_FALLBACK_ETA_SEC)
            # 未到阈值：只告诉前端还在排队，绝不动队列、绝不兜底
            if _is_taken:
                _note = "WorkBuddy 取走后 %d 秒（<5 分钟/300 秒阈值，对方可能仍在算），暂不动队列" % _elapsed
            else:
                _note = "已放入 WorkBuddy 分析队列（任务 #%s，信息包 %s 字）；在 WorkBuddy 里说「分析今天的复盘」即可回填结果。" % (qid, active.get("brief_chars") or 0)
            return _wb_computing(qid, _elapsed, None, _note,
                                 date, cur_max, provs,
                                 queue_state=active["state"],
                                 taken_sec=(_elapsed if _is_taken else None),
                                 include_fallback=(not _is_taken))

        # 2) 无活跃任务但有已完成结果 → 把那份结果交给前端（兜底代出或正常回填都算）
        if done and not force:
            _qid, _err = done
            rev = None
            if _err.startswith("auto-fallback"):
                m = re.search(r"review_id=(\d+)", _err)
                rid = int(m.group(1)) if m else None
                if rid:
                    _row = DB.conn.execute("SELECT * FROM ai_review WHERE id=?", (rid,)).fetchone()
                    rev = AR.digest_of(_row) if _row else None
                if not rev:
                    rev = AR.latest(DB.conn, date=date, provider=fb or "deepseek")
                _fb_from = (rev or {}).get("provider") or "deepseek"
            else:
                rev = AR.latest(DB.conn, date=date, provider="workbuddy")
                _fb_from = "workbuddy"
            if rev:
                # context_stats 用 context_brief 现算（digest_of 不含原始 context_json）
                _cj = ""
                if rev.get("id"):
                    _r = DB.conn.execute("SELECT context_json FROM ai_review WHERE id=?",
                                         (rev.get("id"),)).fetchone()
                    _cj = _r["context_json"] if _r is not None else ""
                _cstats = AR.context_brief(_cj or "{}")
                return {"ok": True, "state": "ok", "cached": False,
                        "provider": "workbuddy", "review_date": date,
                        "fallback_from": _fb_from, "id": rev.get("id"),
                        "fallback_queue_id": _qid,
                        "since_id": cur_max, "providers": provs,
                        "data": rev.get("data"), "context_stats": _cstats,
                        "created_at": rev.get("created_at"),
                        "note": "WorkBuddy 未接单，已由 %s 自动代出" % _fb_from}

        # 3) peek 只查看不落库：没东西可看就返回 computing，绝不入队/兜底
        if peek:
            _pw = _wb_computing(0, 0, None, "（仅查看，未触发兜底）", date, cur_max, provs,
                                include_fallback=False)
            _pw["peek"] = True
            return _pw

        # 4) 兜底线程还在跑（taken 死单已判死 / pending 兜底进行中）→ 别再入队制造新行，
        #    继续告诉前端"在算"，否则每 30 秒一轮会不断新建 pending 行（行数膨胀 + 重复烧钱）。
        if fb and _AI_REVIEW_RUNNING.get("wb-fallback:%s" % fb):
            return _wb_computing(0, 0, fb, "兜底计算中（已在后台用 %s 代出）" % fb,
                                 date, cur_max, provs, fallback_eta=_AI_WB_FALLBACK_ETA_SEC)

        # 5) 既无活跃任务也无结果 → 入队等待
        try:
            q = AR.queue_ensure(DB.conn, date, "workbuddy")
        except Exception as e:  # noqa: BLE001
            log.exception("ai_review 入队失败")
            return {"ok": False, "state": "error", "error": "入队失败：%s" % str(e)[:150]}
        if q.get("state") == "error":
            return {"ok": False, "state": "error", "error": q.get("err") or "入队失败"}
        _running = fb if _AI_REVIEW_RUNNING.get("wb-fallback:%s" % fb) else None
        return _wb_computing(q.get("queue_id") or 0, 0, _running,
                             "已放入 WorkBuddy 分析队列（任务 #%s）；在 WorkBuddy 里说「分析今天的复盘」即可回填结果。" % (q.get("queue_id") or "-"),
                             date, cur_max, provs)

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
