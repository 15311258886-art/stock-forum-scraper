# -*- coding: utf-8 -*-
"""QA 第二轮回归验收（2026-11-04 / 严过关）—— 只验工程师自述的 5 处改动。

对应上轮我提的 P1-1（兜底重复烧钱）与 P1-2（taken 死等），加上被顺手改的 P2-1/P2-2。

隔离方式与上轮一致：`tmp_path` 全新 SQLite + monkeypatch `S.DB` / `AR.run` / `AR.call_llm`，
**不碰生产库 viewer/app.db，不联网，不花 token**。

跑法：`.venv/Scripts/python.exe -m pytest tests/test_qa_round2_fallback.py -q`
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import ai_review as AR  # noqa: E402
from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

DATE = "2026-11-03"


class _FakeDB:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn


def _ts(offset_sec: float = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_sec))


def _wait_until(pred, timeout: float = 8.0, interval: float = 0.01):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.fixture()
def env(monkeypatch, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "qa_round2.db"), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(AR, "_ensured", False, raising=False)
    monkeypatch.setattr(AR, "_queue_ensured", False, raising=False)
    AR.ensure_schema(conn)
    AR.ensure_queue(conn)
    monkeypatch.setattr(S, "DB", _FakeDB(conn))
    monkeypatch.setattr(AR, "provider_key", lambda name: "k" if name == "deepseek" else "")
    monkeypatch.setattr(S, "_AI_REVIEW_RUNNING", {})
    yield conn
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _q(conn, state: str, created_at: str, taken_at=None, err=None, brief: int = 38403) -> int:
    cur = conn.execute(
        "INSERT INTO ai_review_queue (review_date, provider, state, brief_chars, created_at, taken_at, err) "
        "VALUES (?,?,?,?,?,?,?)", (DATE, "workbuddy", state, brief, created_at, taken_at, err))
    conn.commit()
    return int(cur.lastrowid)


def _r(conn, provider: str, verdict: str, age_sec: float = -120) -> int:
    cur = conn.execute(
        "INSERT INTO ai_review (review_date, provider, model, status, context_json, context_chars,"
        " output_json, raw_text, prompt_tokens, completion_tokens, elapsed_ms, err, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, provider, "%s-model" % provider, "ok",
         json.dumps({"market": {}, "doctrine": {"docs": [], "rules": []}}, ensure_ascii=False), 11772,
         json.dumps({"verdict": verdict}, ensure_ascii=False), None, 1, 1, 7312, None,
         _ts(age_sec)))
    conn.commit()
    return int(cur.lastrowid)


def _row(conn, qid: int):
    return conn.execute("SELECT state, err, done_at, taken_at FROM ai_review_queue WHERE id=?",
                        (qid,)).fetchone()


def _drain(timeout: float = 8.0) -> None:
    """等后台兜底线程彻底退出。

    必须在用例末尾调用：daemon 线程还在写库时 fixture 关连接，会让 sqlite 关连接阻塞在
    别线程的未完成语句上，表现为整条 pytest 挂死（踩过一次，别删）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with S._AI_REVIEW_LOCK:
            busy = any(k.startswith("wb-fallback:") for k in S._AI_REVIEW_RUNNING)
        if not busy:
            break
        time.sleep(0.02)
    time.sleep(0.05)


def _boom(*a, **k):
    raise AssertionError("零调用用例里 AR.run 竟然被调用了 —— 说明又在重复花钱")


# ============================================================================
# 1) 两个 P1 是否真消失
# ============================================================================


def test_p1_1_real_scene_zero_model_call(env, monkeypatch):
    """现场真实形态：该日 TTL 内有 deepseek ok + #11 pending 508 秒。

    必须**一次请求直接 ok、零模型调用**，队列当场结清且 err 指向正确 review_id。
    """
    conn = env
    monkeypatch.setattr(AR, "run", _boom)              # 一旦被调用立刻炸，证明"零调用"
    monkeypatch.setattr(AR, "call_llm", _boom)

    rid = _r(conn, "deepseek", "缩量震荡，等主线明确再动手", age_sec=-660)   # ≈ 生产 id=28
    qid = _q(conn, "pending", _ts(-508))

    t0 = time.time()
    res = S.api_ai_review(date=DATE, provider="workbuddy")
    cost = time.time() - t0

    assert res["state"] == "ok", "有新鲜结果就必须 0 秒给结论，不许再排队"
    assert res["provider"] == "workbuddy"
    assert res["fallback_from"] == "deepseek"
    assert res["id"] == rid
    assert (res.get("data") or {}).get("verdict") == "缩量震荡，等主线明确再动手"
    assert res["fallback_queue_id"] == qid
    assert isinstance(res.get("context_stats"), dict) and res["context_stats"], "读数栏不能丢"
    assert cost < 1.0, "这条路径不该有任何等待，实测 %.2fs" % cost

    row = _row(conn, qid)
    assert row["state"] == "done", "队列必须当场结清，否则下次又冒出来"
    assert row["err"].startswith("auto-fallback: 复用已有")
    assert "review_id=%d" % rid in row["err"], row["err"]
    n_rows = conn.execute("SELECT COUNT(*) FROM ai_review WHERE review_date=?", (DATE,)).fetchone()[0]
    assert n_rows == 1, "不许再插一份新结果（重复烧钱的直接证据）"


def test_p1_1_second_call_still_ok_and_cheap(env, monkeypatch):
    """连打第三次也不能因为队列已 done 就退回排队；仍然零调用。"""
    conn = env
    monkeypatch.setattr(AR, "run", _boom)
    rid = _r(conn, "deepseek", "结论A")
    _q(conn, "pending", _ts(-508))

    S.api_ai_review(date=DATE, provider="workbuddy")
    r2 = S.api_ai_review(date=DATE, provider="workbuddy")
    r3 = S.api_ai_review(date=DATE, provider="workbuddy")
    for r in (r2, r3):
        assert r["state"] == "ok" and r["id"] == rid and r["fallback_from"] == "deepseek"


def test_p1_2_taken_1h_falls_back(env, monkeypatch):
    """taken 死单（取走 1 小时没回填）→ 必须判死单并转兜底，队列不能永远 taken。

    用闸门把兜底线程卡住，先验「判死的审计痕迹」，再放行验「最终收敛到 done」。
    """
    conn = env
    gate = threading.Event()
    called: list = []

    def _fake_run(conn_, date="", provider=""):
        called.append(provider)
        gate.wait(6)
        return {"ok": True, "elapsed_ms": 7312, "tokens": {}, "err": ""}

    monkeypatch.setattr(AR, "run", _fake_run)
    qid = _q(conn, "taken", _ts(-3700), taken_at=_ts(-3600))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert res["queue_state"] == "taken"
    assert res["fallback"] == "deepseek", "taken 死单必须转兜底"
    assert res["taken_sec"] >= 3500, res["taken_sec"]
    assert "取走后" in res["note"]

    assert _wait_until(lambda: called, timeout=8), "兜底线程没被派发"
    row = _wait_until(lambda: (_row(conn, qid) if _row(conn, qid)["state"] == "error" else None),
                      timeout=5)
    assert row is not None, "死单必须留下审计痕迹（error/stale taken），实际 %s" % (
        _row(conn, qid)["state"])
    assert row["err"].startswith("stale taken"), row["err"]
    assert "3600" in row["err"] or "36" in row["err"]

    gate.set()
    done = _wait_until(lambda: (_row(conn, qid) if _row(conn, qid)["state"] == "done" else None),
                       timeout=8)
    assert done is not None, "兜底算完必须写回 done（否则下一轮又新建任务，永远收敛不了）"
    assert done["err"].startswith("auto-fallback"), done["err"]
    _drain()


def test_p1_2_taken_60s_does_not_touch_queue(env, monkeypatch):
    """taken 仅 60 秒 → 对方可能正在算，绝不许抢跑（不兜底、不动队列）。"""
    conn = env
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawned.append((date, fb, qid)) or True)
    qid = _q(conn, "taken", _ts(-3700), taken_at=_ts(-60))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert not res.get("fallback"), "未到 5 分钟不许抢跑"
    assert spawned == []
    row = _row(conn, qid)
    assert row["state"] == "taken", "不许动对方的任务"
    assert row["err"] in (None, "")
    assert 55 <= res["taken_sec"] <= 70, res["taken_sec"]
    # 后端 note 用的是 300 秒（前端自己算成"5 分钟"，且 computing 分支不渲染 note）
    assert "300 秒" in res["note"] or "5 分钟" in res["note"]


def test_p1_2_taken_1h_with_fresh_result_reuses(env, monkeypatch):
    """taken 死单 + 该日有新鲜结果 → 直接复用，连兜底线程都不该起。"""
    conn = env
    monkeypatch.setattr(AR, "run", _boom)
    rid = _r(conn, "deepseek", "已有结论", age_sec=-300)
    qid = _q(conn, "taken", _ts(-3700), taken_at=_ts(-3600))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "ok" and res["id"] == rid and res["fallback_from"] == "deepseek"
    assert _row(conn, qid)["state"] == "done"


# ============================================================================
# 2) force 语义没被复用逻辑吃掉
# ============================================================================


def test_force_not_hijacked_by_reuse(env, monkeypatch):
    """【最狠的一种】force=1 时同时存在：新鲜结果 + pending 超期任务。

    必须真排新一轮，不能把「重新分析」按成"拿旧结论"。
    """
    conn = env
    called: list = []
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="": called.append(1) or
                        {"ok": True})
    rid = _r(conn, "deepseek", "旧结论", age_sec=-120)
    old_q = _q(conn, "pending", _ts(-508))

    res = S.api_ai_review(date=DATE, provider="workbuddy", force=1)

    assert res["state"] == "computing", "重新分析必须真重算"
    assert not res.get("fallback_from"), "不许被复用逻辑劫持"
    assert res.get("id") != rid
    assert res["queue_id"] != old_q, "必须新建一轮"
    assert res["elapsed_sec"] < 5, "新任务计时从 0 开始，进不了兜底/复用门槛"
    assert not res.get("fallback")
    assert _row(conn, old_q)["state"] == "error", "旧任务被 force 作废"
    assert "superseded" in (_row(conn, old_q)["err"] or "")
    assert _row(conn, res["queue_id"])["state"] == "pending"
    assert called == [], "请求线程内不该同步算"


# ============================================================================
# 3) _settle 的 stale-taken 例外不许顺带复活 force 的 superseded
# ============================================================================


def test_settle_does_not_revive_superseded(env, monkeypatch):
    conn = env
    monkeypatch.setattr(AR, "run",
                        lambda conn_, date="", provider="": {"ok": True, "elapsed_ms": 7312})
    dead = _q(conn, "error", _ts(-900), err="superseded by force rerun")

    assert S._spawn_wb_fallback(DATE, "deepseek", dead) is True
    _drain()   # 结清是同步的，等线程退出即可（不 sleep 死等）
    row = _row(conn, dead)
    assert row["state"] == "error", "_settle 把 force 作废的任务翻回 done 了（重算会显示成'成功'）"
    assert row["err"] == "superseded by force rerun", row["err"]


def test_settle_does_write_back_stale_taken(env, monkeypatch):
    """对照：自己刚标的 stale taken 死单，兜底算完**必须**能写回 done，否则永远收敛不了。"""
    conn = env
    monkeypatch.setattr(AR, "run",
                        lambda conn_, date="", provider="": {"ok": True, "elapsed_ms": 7312})
    dead = _q(conn, "error", _ts(-900), err="stale taken: 取走后 3600 秒未回填，转兜底")

    assert S._spawn_wb_fallback(DATE, "deepseek", dead) is True
    row = _wait_until(lambda: (_row(conn, dead) if _row(conn, dead)["state"] == "done" else None),
                      timeout=5)
    assert row is not None and row["state"] == "done"
    _drain()


# ============================================================================
# 4) 「①.5」恒返回 fallback 会不会造成新状态机问题
# ============================================================================


def _fake_run_inserting(conn, delay: float = 0.05, verdict: str = "兜底算出来的"):
    """模拟真实 AR.run：睡一会儿、插一条 deepseek ok 行、返回 ok。"""
    def _run(conn_, date="", provider=""):
        time.sleep(delay)
        new_id = _r(conn, provider or "deepseek", verdict, age_sec=0)
        return {"ok": True, "elapsed_ms": int(delay * 1000), "tokens": {}, "err": "",
                "review_id": new_id}
    return _run


def test_section_1_5_no_row_explosion(env, monkeypatch):
    """taken 死单被判死后连打 5 次：队列行数稳定、只 spawn 一次、fallback 恒在。"""
    conn = env
    spawned: list = []
    gate = threading.Event()

    def _slow_run(conn_, date="", provider=""):
        spawned.append(provider)
        gate.wait(6)
        return {"ok": True, "elapsed_ms": 1}

    monkeypatch.setattr(AR, "run", _slow_run)
    qid = _q(conn, "taken", _ts(-3700), taken_at=_ts(-3600))

    try:
        responses = [S.api_ai_review(date=DATE, provider="workbuddy") for _ in range(5)]
        n_rows = conn.execute("SELECT COUNT(*) FROM ai_review_queue").fetchone()[0]
    finally:
        gate.set()
        _drain()

    assert all(r.get("fallback") == "deepseek" for r in responses), \
        [r.get("fallback") for r in responses]
    assert len(spawned) == 1, "5 次请求只许 spawn 一次，实际 %d" % len(spawned)
    assert n_rows <= 2, "队列行数不该膨胀（死单 + 最多一条新 pending），实际 %d" % n_rows


def test_section_1_5_result_visible_immediately_after_fallback(env, monkeypatch):
    """【重点】taken 死单兜底算完之后，结果必须马上可见 —— 不许再干等 30 秒。

    这条是「死单被判死 → queue_ensure 新建 0 秒 pending」带来的副作用：
    兜底结清的是**旧**任务行，而新 pending 行 id 更大，会卡住 1.3 回读的 still_busy 检查。
    """
    conn = env
    monkeypatch.setattr(AR, "run", _fake_run_inserting(conn, delay=0.05))
    qid = _q(conn, "taken", _ts(-3700), taken_at=_ts(-3600))

    r1 = S.api_ai_review(date=DATE, provider="workbuddy")
    assert r1.get("fallback") == "deepseek"

    # 等兜底把结果算完并落库
    new_id = _wait_until(
        lambda: conn.execute("SELECT MAX(id) FROM ai_review WHERE review_date=?", (DATE,)).fetchone()[0],
        timeout=5)
    assert new_id
    _wait_until(lambda: _row(conn, qid)["state"] == "done", timeout=5)
    assert conn.execute("SELECT COUNT(*) FROM ai_review WHERE review_date=?", (DATE,)).fetchone()[0] == 1

    t0 = time.time()
    r2 = S.api_ai_review(date=DATE, provider="workbuddy")
    waited = time.time() - t0

    assert r2["state"] == "ok", (
        "兜底结果已经落库（ai_review id=%s），页面却还是 %s：note=%r，"
        "等于让用户对着已算好的结论再等一轮（最多 30 秒）"
        % (new_id, r2.get("state"), (r2 or {}).get("note")))
    assert r2.get("fallback_from") == "deepseek"
    assert waited < 1.0
    _drain()


# ============================================================================
# 5) 失败路径收敛
# ============================================================================


def test_fb_fails_counts_only_failures(env, monkeypatch):
    """成功的「auto-fallback: …」绝不能被算成失败（连败计数口径）。"""
    conn = env
    spawn = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawn.append((date, fb, qid)) or True)
    _q(conn, "done", _ts(-900), err="auto-fallback: 复用已有 deepseek 结果（review_id=28）")
    _q(conn, "done", _ts(-900), err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=9）")
    _q(conn, "error", _ts(-900), err="auto-fallback failed: 401 未授权")
    _q(conn, "error", _ts(-900), err="auto-fallback failed: 401 未授权")
    _q(conn, "pending", _ts(-40))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res.get("fallback") == "deepseek", "才失败 2 次，必须继续兜底"
    assert len(spawn) == 1


def test_key_invalid_converges_to_stop(env, monkeypatch):
    """Key 无效：连败 3 次后必须停手，给出明确终态文案，而不是继续 30 秒一轮地转。"""
    conn = env
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="":
                        {"ok": False, "err": "401 Unauthorized"})
    spawn = []

    def _fake_spawn(date, fb, qid):
        spawn.append((date, fb, qid))
        threading.Thread(target=lambda: (
            time.sleep(0.02),
            AR.run(conn, date=date, provider=fb),
            conn.execute("UPDATE ai_review_queue SET state='error', done_at=?, err=? WHERE id=?",
                         (_ts(0), "auto-fallback failed: 401 Unauthorized", qid)),
            conn.commit()), daemon=True).start()
        return True

    monkeypatch.setattr(S, "_spawn_wb_fallback", _fake_spawn)
    for _ in range(3):
        _q(conn, "error", _ts(-900), err="auto-fallback failed: 401 Unauthorized")
    _q(conn, "pending", _ts(-40))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert not res.get("fallback")
    assert spawn == [], "连败 3 次后不许再自动重试（防止每 30 秒真发一次请求）"
    assert "连续失败" in res["note"]
    assert "先不再自动重试" in res["note"]
