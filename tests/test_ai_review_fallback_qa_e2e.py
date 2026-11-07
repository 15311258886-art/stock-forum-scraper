# -*- coding: utf-8 -*-
"""QA 独立验收（2026-11-03 / 严过关）：WorkBuddy 无限排队的兜底修复，端到端复现原场景。

与 `tests/test_ai_review_fallback.py`（工程师自测）的分工：
  · 工程师那份验的是「三个单点行为对不对」；
  · 这份验的是「**用户真会遇到的那条路**走通没有」，并且专门去证伪两件事：
    ① 兜底会不会**重复烧钱**（该日已有新鲜 deepseek 结果时还去真调一次模型）；
    ② 其它入口（force / peek / taken）有没有被新逻辑改坏。

隔离方式（重要，写清楚便于复核）：
  · 全程用 `tmp_path` 下的**全新 SQLite**，通过 monkeypatch 把 `server.DB` 换成只暴露
    `.conn` 的替身，把 `AR.run` / `AR.call_llm` 打桩 —— **不碰生产库 viewer/app.db，
    不联网，不花 token**。
  · `AR._ensured` / `AR._queue_ensured` 是进程级布尔，必须逐例复位，否则新库会被当成
    "已建表"从而 `no such table`。
  · `S._AI_REVIEW_RUNNING` 是模块级防重入表，用例之间必须清空，否则第 2 例会以为
    "上一轮兜底还在 300 秒窗口内"。

跑法：`.venv/Scripts/python.exe -m pytest tests/test_ai_review_fallback_qa_e2e.py -q`
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
    """摘掉根 logger 的 FileHandler：导入 viewer.server 会挂一个写生产日志的 handler。"""
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import ai_review as AR  # noqa: E402
from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

DATE = "2026-11-03"          # 现场出事那天（生产库 ai_review id=28 就是这天）
QUEUED_SEC = 508             # 现场快照里 #11 的排队秒数


class _FakeDB:
    """`DB` 的最小替身：`api_ai_review()` 与兜底线程都只通过 `DB.conn` 取连接。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn


def _ts(offset_sec: float = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_sec))


def _wait_until(pred, timeout: float = 8.0, interval: float = 0.01):
    """等后台线程（daemon）把活干完。返回最后一次取到的值，超时返回 None。"""
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
    conn = sqlite3.connect(str(tmp_path / "qa_ai_review_e2e.db"),
                           check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row

    monkeypatch.setattr(AR, "_ensured", False, raising=False)
    monkeypatch.setattr(AR, "_queue_ensured", False, raising=False)
    AR.ensure_schema(conn)
    AR.ensure_queue(conn)

    monkeypatch.setattr(S, "DB", _FakeDB(conn))
    # 本机只配了 deepseek 的 Key —— 与线上一致（hunyuan/qwen 未配）
    monkeypatch.setattr(AR, "provider_key",
                        lambda name: "test-key" if name == "deepseek" else "")
    monkeypatch.setattr(S, "_AI_REVIEW_RUNNING", {})

    yield conn
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _insert_queue(conn, state: str, created_at: str, err=None, brief: int = 38403,
                  taken_at=None) -> int:
    cur = conn.execute(
        "INSERT INTO ai_review_queue (review_date, provider, state, brief_chars, created_at, "
        "taken_at, err) VALUES (?,?,?,?,?,?,?)",
        (DATE, "workbuddy", state, brief, created_at, taken_at, err))
    conn.commit()
    return int(cur.lastrowid)


def _insert_review(conn, provider: str, verdict: str, age_sec: float = -120) -> int:
    """插一条成功的复盘记录（模拟生产库 id=28 那份 deepseek 结果）。"""
    cur = conn.execute(
        "INSERT INTO ai_review (review_date, provider, model, status, context_json, context_chars,"
        " output_json, raw_text, prompt_tokens, completion_tokens, elapsed_ms, err, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, provider, "%s-model" % provider, "ok",
         json.dumps({"market": {"state": "缩量"}, "doctrine": {"docs": [{"t": "x"}], "rules": []}},
                    ensure_ascii=False),
         11772,
         json.dumps({"verdict": verdict, "mainlines": [], "picks": []}, ensure_ascii=False),
         None, 120, 240, 7312, None, _ts(age_sec)))
    conn.commit()
    return int(cur.lastrowid)


def _queue_row(conn, qid: int):
    return conn.execute("SELECT state, err, done_at FROM ai_review_queue WHERE id=?",
                        (qid,)).fetchone()


# ============================================================================
# 场景一（最重要）：完整复现「打开就一直转」→ 兜底自动出结果 → 页面看到结论
# ============================================================================


def test_e2e_pending_508s_autofallback_then_result_visible(env, monkeypatch):
    """端到端：pending 508 秒的 WorkBuddy 任务，一次请求内必须收敛到「看得到结果」。

    判据链：
      第 1 次请求 → state=computing + fallback=deepseek + 后台真派活 + 队列结清为 done
      第 2 次请求 → state=ok + provider=workbuddy + fallback_from=deepseek + 正文非空

    2026-11-03 追加：本用例里的那份 deepseek 结果刻意放在 **TTL 之外**（5 小时前）。
    因为新增了「该日已有新鲜结果就直接复用」的 0 秒路径（见
    `test_e2e_pending_508s_with_fresh_result_returns_in_zero_seconds`），
    新鲜结果存在时压根不会再走"真算"这条路。本用例要验的正是**真算**那条，
    所以数据必须构造成"没有可复用的新鲜结果"。
    """
    conn = env
    dispatched: list = []

    def _fake_run(conn_, date: str = "", provider: str = ""):
        dispatched.append((date, provider))
        return {"ok": True, "elapsed_ms": 7312,
                "tokens": {"prompt": 1, "completion": 1}, "err": ""}

    monkeypatch.setattr(AR, "run", _fake_run)

    # 5 小时前 = 超出 _AI_REVIEW_TTL(4h)，`_find_fresh_result` 必须查不到
    review_id = _insert_review(conn, "deepseek", "缩量震荡，等主线明确再动手", age_sec=-5 * 3600)
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    # ---- 第 1 次（用户刚打开面板）----
    r1 = S.api_ai_review(date=DATE, provider="workbuddy")

    assert r1["ok"] is True
    assert r1["state"] == "computing", "首次请求不可能立刻有 workbuddy 结果，应是 computing"
    assert r1["fallback"] == "deepseek", "排队 508s 必须报出已启动兜底"
    assert r1["queue_id"] == qid
    assert r1["elapsed_sec"] >= QUEUED_SEC - 5
    assert r1["fallback_eta_sec"] > 0
    assert "不用你操作" in r1["note"]
    assert "WorkBuddy 里说" not in r1["note"], "旧版要用户手动救活的文案必须消失"

    # ---- 后台兜底必须真派活 ----
    assert _wait_until(lambda: dispatched, timeout=8), "兜底线程没被派发"
    assert dispatched == [(DATE, "deepseek")]

    row = _wait_until(lambda: (_queue_row(conn, qid) if
                               _queue_row(conn, qid)["state"] == "done" else None), timeout=8)
    assert row is not None, "兜底跑完必须把队列任务结清，不能留在 pending"
    assert row["state"] == "done"
    assert row["err"].startswith("auto-fallback"), row["err"]
    assert "deepseek" in row["err"]
    assert row["done_at"], "done_at 必须写，否则无法判断兜底花了多久"

    # ---- 第 2 次（前端 3 秒后的那次轮询）----
    r2 = S.api_ai_review(date=DATE, provider="workbuddy")

    assert r2["state"] == "ok", "兜底已落库还返回 computing，就是用户看到的'永远转圈'"
    assert r2["ok"] is True
    assert r2["provider"] == "workbuddy", "用户选的大脑是谁就还谁，不能突然变成 deepseek"
    assert r2["fallback_from"] == "deepseek", "真实来源必须如实标注"
    assert r2["id"] == review_id
    assert (r2.get("data") or {}).get("verdict") == "缩量震荡，等主线明确再动手"
    assert r2["fallback_queue_id"] == qid
    # 代出也要带读数栏（docs/rules/sections），否则"它读了多少"这块证据就缺了
    stats = r2.get("context_stats") or {}
    assert isinstance(stats, dict) and "docs" in stats and "sections" in stats, stats


def test_e2e_pending_508s_with_fresh_result_returns_in_zero_seconds(env, monkeypatch):
    """现场真实形态（QA 必修 1）：该日已有 TTL 内的 deepseek 结果 → 首次请求就该给出结论。

    生产库原状：ai_review 有 id=27、id=28 两份 deepseek ok（都在 4h TTL 内），
    队列 #11 pending 508 秒。旧实现会**再花一次 token** 算 id=29，用户还得多等 8 秒。
    新口径：直接复用那份已有结果 —— 0 次模型调用、0 秒等待、不多一条记录。
    """
    conn = env
    dispatched: list = []
    monkeypatch.setattr(AR, "run",
                        lambda conn_, date="", provider="": dispatched.append(provider) or
                        {"ok": True, "elapsed_ms": 1})
    monkeypatch.setattr(AR, "call_llm",
                        lambda *a, **k: pytest.fail("已有新鲜结果还去调模型 = 重复烧钱"))

    review_id = _insert_review(conn, "deepseek", "缩量震荡，等主线明确再动手")   # ≈ 生产 id=28
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    # 一次请求内直接收敛，不再有"computing → 等 8 秒 → ok"的中间态
    r = S.api_ai_review(date=DATE, provider="workbuddy")

    assert r["state"] == "ok", "该日已有新鲜结果，必须 0 秒返回，不许再排队"
    assert r["provider"] == "workbuddy"
    assert r["fallback_from"] == "deepseek"
    assert r["id"] == review_id
    assert r["fallback_queue_id"] == qid
    assert (r.get("data") or {}).get("verdict") == "缩量震荡，等主线明确再动手"
    assert dispatched == [], "复用路径绝不许再派一次真算"

    row = _queue_row(conn, qid)
    assert row["state"] == "done", "队列任务必须当场结清，否则下次轮询还在等"
    assert row["err"].startswith("auto-fallback"), row["err"]
    assert "复用" in row["err"] and ("review_id=%s" % review_id) in row["err"], row["err"]


def test_e2e_does_not_mutate_original_review_row(env, monkeypatch):
    """兜底回读只是**改写返回体**，不能把 ai_review 里的真实 provider 改写掉。"""
    conn = env
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="": {"ok": True})
    review_id = _insert_review(conn, "deepseek", "原始结论")
    _insert_queue(conn, "done", _ts(-300),
                  err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=%s）"
                      % review_id)

    res = S.api_ai_review(date=DATE, provider="workbuddy")
    assert res["provider"] == "workbuddy"

    db_row = conn.execute("SELECT provider FROM ai_review WHERE id=?", (review_id,)).fetchone()
    assert db_row["provider"] == "deepseek", "库里必须保留真实来源，只允许在响应里改写"
    # 也不许产生第二条 workbuddy 记录（那会让 history 里凭空多一份"WorkBuddy 算的"）
    n_wb = conn.execute(
        "SELECT COUNT(*) FROM ai_review WHERE review_date=? AND provider='workbuddy'",
        (DATE,)).fetchone()[0]
    assert n_wb == 0


# ============================================================================
# 场景二（重点查）：重复烧钱 —— 该日已有新鲜 deepseek 结果，兜底还会不会真调模型？
# ============================================================================


def test_spawn_fallback_reuses_fresh_result(env, monkeypatch):
    """兜底必须**先查有没有**，有新鲜结果就直接复用 —— 0 次模型调用、0 token。

    2026-11-03 QA 抓到过反例：该日 ai_review 已有 id=27、id=28 两份 deepseek ok
    （TTL 4 小时内），兜底仍真调了一次模型，产出 id=29 第三份。根因是 `AR.run()`
    全程不查缓存（viewer/ai_review.py:1472-1484 只负责"算一份并落库"）。
    现在 `_spawn_wb_fallback` 的线程体开头先走 `_find_fresh_result()`，命中就只结清队列。

    本用例**不打桩 `AR.run`**，只打桩最外面的 `AR.call_llm`（真正的花钱点）和
    `AR.build_context` / `AR._render_user`（避免十几秒的取数），因此它证明的是
    run() 里那条真实执行链：build_context → call_llm → 落库。
    """
    conn = env
    calls: list = []

    def _fake_llm(provider: str, system: str, user: str):
        calls.append(provider)
        return {"ok": True, "text": json.dumps({"verdict": "又算了一遍"}, ensure_ascii=False),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 8000, "err": ""}

    monkeypatch.setattr(AR, "call_llm", _fake_llm)
    monkeypatch.setattr(AR, "build_context", lambda conn_, date, provider=None: {"review_date": date})
    monkeypatch.setattr(AR, "_render_user", lambda ctx: "PROMPT")
    monkeypatch.setattr(AR, "_register_calls", lambda *a, **k: {})   # 计分登记与本缺陷无关

    existing = _insert_review(conn, "deepseek", "已有结论", age_sec=-60)   # 1 分钟前刚算完，TTL 内
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    assert S._spawn_wb_fallback(DATE, "deepseek", qid) is True

    row = _wait_until(lambda: (_queue_row(conn, qid) if
                               _queue_row(conn, qid)["state"] == "done" else None), timeout=8)
    assert row is not None and row["state"] == "done"
    # 判据 1：一次模型调用都不许有（这才是"不重复烧钱"）
    assert calls == [], "已有新鲜结果还去调模型 = 重复花钱"
    # 判据 2：err 里必须写明"复用"，并指向那一份的 review_id（1.3 回读靠它精确定位）
    assert row["err"].startswith("auto-fallback"), row["err"]
    assert "复用" in row["err"], row["err"]
    assert ("review_id=%s" % existing) in row["err"], row["err"]
    # 判据 3：库里仍然只有那一份 ok 记录，没有多出第二份
    n_ok = conn.execute(
        "SELECT COUNT(*) FROM ai_review WHERE review_date=? AND provider='deepseek' "
        "AND status='ok'", (DATE,)).fetchone()[0]
    assert n_ok == 1, "复用不许落新记录（n=%s）" % n_ok


# ============================================================================
# 场景三：防重入 —— 前端每 3 秒轮询一次，绝不能每次都起一路兜底
# ============================================================================


def test_spawn_is_idempotent_while_running(env, monkeypatch):
    """同一 provider 的兜底在跑时，第二次调用必须返回 False 且不再派活（避免双份花钱）。"""
    conn = env
    gate = threading.Event()
    dispatched: list = []

    def _blocking_run(conn_, date: str = "", provider: str = ""):
        dispatched.append(provider)
        gate.wait(5)
        return {"ok": True, "elapsed_ms": 1}

    monkeypatch.setattr(AR, "run", _blocking_run)
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    try:
        assert S._spawn_wb_fallback(DATE, "deepseek", qid) is True
        assert _wait_until(lambda: dispatched, timeout=5)
        assert S._spawn_wb_fallback(DATE, "deepseek", qid) is False, "重入必须被挡住"
    finally:
        gate.set()
    assert _wait_until(lambda: _queue_row(conn, qid)["state"] == "done", timeout=8)
    assert dispatched == ["deepseek"], "只许派一次活"


def test_polling_twice_does_not_double_spend(env, monkeypatch):
    """连点两次/两个标签页同时打开：接口都被调用，但只允许一次真花钱。"""
    conn = env
    gate = threading.Event()
    dispatched: list = []

    def _blocking_run(conn_, date: str = "", provider: str = ""):
        dispatched.append(provider)
        gate.wait(5)
        return {"ok": True, "elapsed_ms": 1}

    monkeypatch.setattr(AR, "run", _blocking_run)
    _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    try:
        r1 = S.api_ai_review(date=DATE, provider="workbuddy")
        r2 = S.api_ai_review(date=DATE, provider="workbuddy")
    finally:
        gate.set()
    assert r1.get("fallback") == "deepseek"
    assert r2.get("fallback") == "deepseek", "第二次也要如实告知兜底生效，否则前端误判成排队异常"
    assert _wait_until(lambda: dispatched, timeout=5)
    time.sleep(0.2)
    assert dispatched == ["deepseek"]


# ============================================================================
# 场景四：兜底不许劫持 force=1（「重新分析」必须真重算）
# ============================================================================


def test_force_rerun_is_not_hijacked_by_fallback_readback(env, monkeypatch):
    """已有一份「代出」结果时点【重新分析】，必须**重新排队/重算**，不能秒回旧结论。"""
    conn = env
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="": {"ok": True})
    old_review = _insert_review(conn, "deepseek", "旧结论")
    old_q = _insert_queue(conn, "done", _ts(-600),
                          err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=%s）"
                              % old_review)

    res = S.api_ai_review(date=DATE, provider="workbuddy", force=1)

    assert res["state"] == "computing", "「重新分析」按下去不能立刻返回旧结论"
    assert not res.get("fallback_from"), "重算不许被兜底回读劫持"
    assert res["queue_id"] != old_q, "必须新建一轮任务"
    assert res["elapsed_sec"] < 5, "新任务的排队计时必须从 0 开始"
    assert not res.get("fallback"), "刚入队不到 30 秒，不该触发兜底"

    new_row = _queue_row(conn, res["queue_id"])
    assert new_row["state"] == "pending"
    # 旧任务不能被 force 顺手改坏：它已经 done，不该被 superseded 掉
    assert _queue_row(conn, old_q)["state"] == "done"


# ============================================================================
# 场景五：peek=1（切大脑 = 只看不算）行为不许被改变
# ============================================================================


def test_peek_has_no_side_effects_on_pending_queue(env, monkeypatch):
    """peek 不许入队、不许起兜底、不许改队列 —— 切大脑不该花钱也不该排队。"""
    conn = env
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawned.append((date, fb, qid)) or True)
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))
    before = conn.execute("SELECT COUNT(*) FROM ai_review_queue").fetchone()[0]

    res = S.api_ai_review(date=DATE, provider="workbuddy", peek=1)

    assert res["peek"] is True
    assert res["state"] == "computing"
    assert not res.get("fallback"), "peek 语义是只看不动手，不许触发兜底"
    assert spawned == []
    after = conn.execute("SELECT COUNT(*) FROM ai_review_queue").fetchone()[0]
    assert after == before, "peek 不许新建队列任务"
    assert _queue_row(conn, qid)["state"] == "pending", "peek 不许改队列状态"


def test_peek_returns_fallback_result_when_already_done(env, monkeypatch):
    """兜底已经算完时，peek（切回 workbuddy）也应当立刻看到结论 —— 这是"打开就给结果"。"""
    conn = env
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="": {"ok": True})
    rid = _insert_review(conn, "deepseek", "已有代出结论")
    _insert_queue(conn, "done", _ts(-300),
                  err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=%s）" % rid)

    res = S.api_ai_review(date=DATE, provider="workbuddy", peek=1)

    assert res["state"] == "ok"
    assert res["provider"] == "workbuddy" and res["fallback_from"] == "deepseek"


# ============================================================================
# 场景六：残留缺陷取证 —— state='taken' 的任务永远不会兜底
# ============================================================================


def test_taken_task_falls_back_after_stale(env, monkeypatch):
    """taken 死单必须被回收：取走 1 小时没回填 → 判死单 → 转兜底。

    这条在 2026-11-03 之前是**已记录缺口**（原名 test_taken_task_never_falls_back）：
    `queue_brief()` 取包即标 taken，全仓没有超时回收，外部代理取包后崩掉就永远 computing。
    现在的口径：taken 未超时（5 分钟内）不介入（对方可能真在算，抢跑 = 双份花钱）；
    超时就判死单并走与 pending 完全相同的「新鲜即复用 / 否则真算」路径。
    """
    conn = env
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawned.append((date, fb, qid)) or True)
    qid = _insert_queue(conn, "taken", _ts(-3600), taken_at=_ts(-3600))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert res["queue_state"] == "taken"
    assert res["fallback"] == "deepseek", "taken 超时必须转兜底，否则就是无限转圈"
    assert spawned == [(DATE, "deepseek", qid)], "必须针对同一具'尸体'起兜底，不新开任务"
    assert res["taken_sec"] >= 3600
    assert "没回填" in res["note"]

    # 死因必须落库可查：看得出"取走过、但没回填"，而不是当成从没被取过
    row = _queue_row(conn, qid)
    assert row["state"] == "error"
    assert row["err"].startswith("stale taken:"), row["err"]


def test_taken_task_not_stale_does_not_fall_back(env, monkeypatch):
    """taken 才 60 秒（< 300）时**不许**兜底、也不许动队列 —— 对方可能正算到一半。"""
    conn = env
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawned.append((date, fb, qid)) or True)
    qid = _insert_queue(conn, "taken", _ts(-60), taken_at=_ts(-60))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert res["queue_state"] == "taken"
    assert "fallback" not in res, "未超时的 taken 抢跑 = 同一份复盘买两遍"
    assert spawned == []
    row = _queue_row(conn, qid)
    assert row["state"] == "taken", "状态不许被改"
    assert not row["err"], row["err"]


def test_fallback_failure_marks_error_and_stops_after_three(env, monkeypatch):
    """兜底失败必须把任务标 error（不能留 pending 永久卡死），且连败三次后停手。"""
    conn = env
    monkeypatch.setattr(AR, "run",
                        lambda conn_, date="", provider="": {"ok": False, "err": "401 未授权"})
    qid = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))

    assert S._spawn_wb_fallback(DATE, "deepseek", qid) is True
    row = _wait_until(lambda: (_queue_row(conn, qid) if
                               _queue_row(conn, qid)["state"] == "error" else None), timeout=8)
    assert row is not None, "失败的兜底绝不能把任务留在 pending"
    assert row["err"].startswith("auto-fallback failed"), row["err"]
    assert not conn.execute(
        "SELECT id FROM ai_review_queue WHERE state IN ('pending','taken')").fetchone()

    # 再补两条失败，凑满三次 → 第 4 次请求不许再派活
    _insert_queue(conn, "error", _ts(-900), err="auto-fallback failed: 401 未授权")
    _insert_queue(conn, "error", _ts(-900), err="auto-fallback failed: 401 未授权")
    q4 = _insert_queue(conn, "pending", _ts(-QUEUED_SEC))
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid_: spawned.append((date, fb, qid_)) or True)

    res = S.api_ai_review(date=DATE, provider="workbuddy")
    assert res["state"] == "computing"
    assert not res.get("fallback")
    assert spawned == []
    assert "连续失败" in res["note"]
    assert _queue_row(conn, q4)["state"] == "pending"   # 不假装在算，也不动它
