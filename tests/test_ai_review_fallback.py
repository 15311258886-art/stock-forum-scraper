# -*- coding: utf-8 -*-
"""P0 回归：WorkBuddy 队列没人接单时，必须**自动**出结果，不能永远转圈。

## 被测的 bug（2026-11-03 现场，进哥原话）

> 「我打开都得快十多分钟了，还在那转圈。」

现场快照：
```
AI 复盘面板 · 大脑=WorkBuddy
分析包已备好，但还没被取走（任务 #11，38403 字）· 已在队列排队 508 秒
<CSS 转圈动画一直在转>
```

三个病灶叠在一起：

1. `api_ai_review()` 的 `provider == "workbuddy"` 分支只做 `AR.queue_ensure()` 入队，
   **本进程不调任何模型**，靠外部 WorkBuddy 代理通过 MCP 取包回填。没人取 →
   队列永远 `pending` → 接口永远 `state=computing` → 页面永远转圈。
2. 同一日期其实**已经有** DeepSeek 的结果（`ai_review` id=28，2026-11-03，status=ok，
   耗时 7.3 秒）。但请求的 provider 是 workbuddy，缓存判断只查 `provider='workbuddy'`，
   所以看不见那一份 —— 明明算好了却用不上。
3. 前端 `airStartPoll()` 轮询上限 30 次（≈190 秒）就停。停了之后画面冻结在
   「排队 N 秒」+ 转圈动画上，看起来像转了十几分钟。

## 本文件的三条判据（对应修复的三块）

1. **排队超时自动兜底**：队列 `pending` 且已躺 40 秒（> `_AI_WB_FALLBACK_SEC`=30）→
   调 `api_ai_review(provider="workbuddy")` 必须返回 `fallback="deepseek"`，
   并且后台线程真的把 `AR.run(..., provider="deepseek")` 派出去。
   `AR.run` 全程 monkeypatch 打桩，**绝不真调模型**。
2. **兜底结果能被 workbuddy 请求读到**：队列 `done` + `err` 以 `auto-fallback` 开头，
   库里有一条该日 deepseek 的 ok 记录 → 调 `api_ai_review(provider="workbuddy")`
   必须返回 `state="ok"`、`provider="workbuddy"`、`fallback_from="deepseek"`，
   且正文就是那份 deepseek 的结论。
3. **没有可兜底的大脑就别假装在算**：`_pick_fallback_provider()` 在所有 provider 都没
   Key 时必须返回空串（调用方据此不启动兜底），有 Key 时按 `PROVIDERS` 顺序优先
   deepseek。

跑法：`.venv/Scripts/python.exe -m pytest tests/test_ai_review_fallback.py -q`

全部在 tmp 目录的独立 SQLite 上跑，不碰生产库、不联网、不花 token。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 上的 FileHandler。

    教训见 `tests/test_offproc_backoff_retry.py`：导入 `viewer.server` 会在根 logger 上
    挂一个指向 `viewer/logs/server-<date>.log` 的 RotatingFileHandler，于是本文件里的
    每一行日志都会写进**生产日志**，污染事后排查。
    """
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
    """`DB` 的最小替身。

    `api_ai_review()` 只通过 `DB.conn` 取连接，所以替身只需要这一个属性。
    连接必须 `check_same_thread=False`：兜底线程（`_spawn_wb_fallback` 里起的 daemon）
    要用同一条连接落库，真实现里 `viewer/db.py:Database` 用的也是这个参数。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn


def _ts(offset_sec: float = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_sec))


def _wait_until(pred, timeout: float = 8.0, interval: float = 0.01):
    """等后台 daemon 线程干完活。返回最后一次取到的值（超时返回 None）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


def _queue_row(conn, qid: int):
    return conn.execute("SELECT state, err, done_at FROM ai_review_queue WHERE id=?",
                        (qid,)).fetchone()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """独立的临时库 + 独立的 provider/DB 打桩。"""
    conn = sqlite3.connect(str(tmp_path / "ai_review_fallback.db"),
                           check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row

    # `ensure_schema` / `ensure_queue` 各自只有一个进程级布尔（`_ensured`），
    # 第一次建过就再也不建 —— 而每个用例都是**新库**，不复位就会拿着空库跑，
    # 表现为 "no such table"，看着像代码 bug 其实是夹具的锅。
    monkeypatch.setattr(AR, "_ensured", False, raising=False)
    monkeypatch.setattr(AR, "_queue_ensured", False, raising=False)
    AR.ensure_schema(conn)
    AR.ensure_queue(conn)

    monkeypatch.setattr(S, "DB", _FakeDB(conn))
    # 兜底候选只看 `provider_key()` 非空。默认造一个"只有 deepseek 有 Key"的本机环境，
    # 与线上一致；任何用例都不该真的读凭据库或发网络请求。
    monkeypatch.setattr(AR, "provider_key",
                        lambda name: "test-key" if name == "deepseek" else "")
    # 防重入表是模块级全局，用例之间必须清空，否则第 2 个用例会因为
    # "上一轮兜底还在 300 秒窗口内"而拿不到 fallback 字段。
    monkeypatch.setattr(S, "_AI_REVIEW_RUNNING", {})

    yield conn
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _insert_queue(conn, state: str, created_at: str, err=None, brief: int = 38403) -> int:
    cur = conn.execute(
        "INSERT INTO ai_review_queue (review_date, provider, state, brief_chars, created_at, err) "
        "VALUES (?,?,?,?,?,?)", (DATE, "workbuddy", state, brief, created_at, err))
    conn.commit()
    return int(cur.lastrowid)


def _insert_review(conn, provider: str, verdict: str) -> int:
    cur = conn.execute(
        "INSERT INTO ai_review (review_date, provider, model, status, context_json, context_chars,"
        " output_json, raw_text, prompt_tokens, completion_tokens, elapsed_ms, err, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, provider, "%s-model" % provider, "ok",
         json.dumps({"market": {"state": "缩量"}, "doctrine": {"docs": [], "rules": []}},
                    ensure_ascii=False),
         12000,
         json.dumps({"verdict": verdict, "mainlines": [], "picks": []}, ensure_ascii=False),
         None, 120, 240, 7300, None, _ts(-120)))
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 判据 1


def test_workbuddy_pending_over_timeout_triggers_fallback(env, monkeypatch):
    """排队超 30 秒没人接单 → 接口必须告诉前端"已兜底"，且后台真派了活。"""
    conn = env
    called: list = []

    def _fake_run(conn_, date: str = "", provider: str = ""):
        """替身：不联网、不花 token，但把"谁被叫去干活"记下来。"""
        called.append((date, provider))
        return {"ok": True, "elapsed_ms": 7300, "tokens": {"prompt": 1, "completion": 1},
                "err": ""}

    monkeypatch.setattr(AR, "run", _fake_run)

    qid = _insert_queue(conn, "pending", _ts(-40))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["ok"] is True
    assert res["state"] == "computing"
    assert res["fallback"] == "deepseek", "超过兜底线还没接单，必须报出兜底大脑"
    assert res["queue_id"] == qid
    assert res["elapsed_sec"] >= S._AI_WB_FALLBACK_SEC
    assert "不用你操作" in res["note"], "文案不能再要求用户去别处手动救活"
    assert "WorkBuddy 里说" not in res["note"]

    # 兜底是后台线程，给它一点时间跑（替身是毫秒级返回，这里只是防调度抖动）
    for _ in range(200):
        if called:
            break
        time.sleep(0.01)
    assert called == [(DATE, "deepseek")], "必须真用 deepseek 派活，而不是只改个字段"

    # 队列任务必须被结清成 done，且 err 带上 auto-fallback 前缀 ——
    # 判据 2 就是靠这个前缀认出"这份是代出的"。
    row = None
    for _ in range(200):
        row = conn.execute("SELECT state, err, done_at FROM ai_review_queue WHERE id=?",
                           (qid,)).fetchone()
        if row and row["state"] == "done":
            break
        time.sleep(0.01)
    assert row is not None and row["state"] == "done", "兜底跑完必须把队列任务标 done"
    assert row["err"].startswith("auto-fallback"), row["err"]
    assert "deepseek" in row["err"]
    assert row["done_at"], "done_at 必须写，否则无法判断兜底花了多久"


# ---------------------------------------------------------------- 判据 2


def test_workbuddy_reads_auto_fallback_result(env):
    """兜底算完之后，选着 WorkBuddy 的场景必须能**看到**那份结果（不再重复转圈）。"""
    conn = env
    # 先插结果拿到真实 id，再写进队列 err —— 1.3 回读是按 err 里的 review_id **精确**取行的，
    # err 里写一个不存在的 id 就只能走退化路径，验不到本来要验的那条。
    review_id = _insert_review(conn, "deepseek", "缩量震荡，等主线明确再动手")
    _insert_queue(conn, "done", _ts(-300),
                  err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=%s）"
                      % review_id)

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["ok"] is True
    assert res["state"] == "ok", "兜底结果已落库，就不能再返回 computing"
    assert res["provider"] == "workbuddy", "用户选的大脑是谁，返回就还是谁"
    assert res["fallback_from"] == "deepseek", "真实来源必须如实标注，不能伪装成 WorkBuddy 算的"
    assert res["id"] == review_id
    assert (res.get("data") or {}).get("verdict") == "缩量震荡，等主线明确再动手"

    # 「这次 AI 实际读了什么」那条读数栏也要在，否则代出的结论会少一块证据
    assert isinstance(res.get("context_stats"), dict)


def test_readback_uses_review_id_from_queue_err_not_newest(env):
    """必修 4：回读必须按队列 err 里记的 review_id 取那一行，不能取"该日最新非 workbuddy"。

    旧实现取最新：只要该日后来又算过一份（比如用户切到 deepseek 点过一次重新分析），
    队列里这条代出记录就会把**别人**的新结论当成自己的交上去 —— 用户看到的结论
    与 err 里记的 review_id 对不上，审计链断掉。
    """
    conn = env
    mine = _insert_review(conn, "deepseek", "这份才是当时代出的（id 记在队列 err 里）")
    newer = _insert_review(conn, "deepseek", "后来才出的更新的一份")     # id 更大
    assert newer > mine
    _insert_queue(conn, "done", _ts(-300),
                  err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=%s）" % mine)

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "ok"
    assert res["id"] == mine, "必须精确命中 err 里那个 review_id，不能取最新"
    assert (res.get("data") or {}).get("verdict", "").startswith("这份才是当时代出的")


def test_readback_falls_back_to_fresh_result_when_review_id_gone(env, monkeypatch):
    """必修 4 的退化路径：err 里的 review_id 已不存在 → 退化为 TTL 内的同 provider 结果。

    关键是**不能**再退化成"该日最新非 workbuddy ok"：没有 TTL 校验时，
    三个月前的旧结论也会被当成"刚算好的"顶上来。
    """
    conn = env
    rid = _insert_review(conn, "deepseek", "TTL 内的那一份")
    _insert_queue(conn, "done", _ts(-300),
                  err="auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=999999）")

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "ok"
    assert res["id"] == rid, "退化路径也要落在 TTL 内那份上"
    assert res["fallback_from"] == "deepseek"


def test_workbuddy_keeps_computing_when_newer_task_is_still_pending(env, monkeypatch):
    """用户点了「重新分析」→ 有新任务在排队时，不许拿上一轮的代出结果顶上去。

    否则「重新分析」按下去跟没按一样：页面立刻显示旧结论，等于骗人。
    """
    conn = env
    monkeypatch.setattr(AR, "run",
                        lambda conn_, date="", provider="": {"ok": True, "elapsed_ms": 1})
    old = _insert_queue(conn, "done", _ts(-300))
    conn.execute("UPDATE ai_review_queue SET err=? WHERE id=?",
                 ("auto-fallback: WorkBuddy 未接单，已由 deepseek 代出（review_id=28）", old))
    conn.commit()
    _insert_review(conn, "deepseek", "旧结论")
    _insert_queue(conn, "pending", _ts(-5))   # 新一轮刚入队，还没到兜底线

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert not res.get("fallback_from")
    assert res["queue_id"] != old, "必须指向新的那一轮任务"


# ---------------------------------------------------------------- 判据 3


def test_taken_stale_keeps_reporting_fallback_on_next_poll(env, monkeypatch):
    """taken 死单转兜底后，下一轮轮询**不许**把进度说退回去。

    真实时序：taken 超时 → 那行被标 error（审计要留"取走过但没回填"）→ 下一轮
    `queue_ensure()` 看不到 pending/taken，新开一条 0 秒的 pending。
    若这时响应里没有 fallback，前端就会从「已自动改用 DeepSeek 计算中」倒退回
    「等 WorkBuddy 接单（0 秒）」—— 用户会以为进度倒流，比转圈更让人以为坏了。
    """
    conn = env
    spawned: list = []

    def _fake_spawn(date: str, fb: str, qid_) -> bool:
        spawned.append((date, fb, qid_))
        S._AI_REVIEW_RUNNING["wb-fallback:%s" % fb] = time.time()   # 模拟"已在跑"
        return True

    # 实现注记：这里**不起真线程**，只复刻真实 spawn 的副作用（往防重入表登记 key）。
    # 本文件把 `DB.conn` 换成了一个共享连接，若留一个阻塞的 daemon 线程跨用例边界，
    # 夹具关连接时它会撞上 use-after-close —— Windows 上是直接 access violation 崩进程。
    monkeypatch.setattr(S, "_spawn_wb_fallback", _fake_spawn)
    qid = _insert_queue(conn, "taken", _ts(-3600), err=None)
    conn.execute("UPDATE ai_review_queue SET taken_at=? WHERE id=?", (_ts(-3600), qid))
    conn.commit()

    r1 = S.api_ai_review(date=DATE, provider="workbuddy")
    assert r1["fallback"] == "deepseek"
    assert spawned == [(DATE, "deepseek", qid)]

    # 第二轮：死单已被标 error，于是会新开一条 0 秒 pending
    r2 = S.api_ai_review(date=DATE, provider="workbuddy")
    assert r2["state"] == "computing"
    assert r2["fallback"] == "deepseek", "兜底在跑时不许把进度说退回去"
    assert r2["queue_id"] != qid, "前提：死单被标死后新开了一轮任务"
    assert len(spawned) == 1, "第二轮不许再派一次活（已经在跑）"


def test_settle_does_not_revive_force_superseded_task(env, monkeypatch):
    """必修 3：兜底收尾绝不能把 force 已作废的任务翻回 done。

    时序：用户点【重新分析】→ force 把旧任务标成 error + 'superseded by force rerun'；
    若上一轮兜底线程这时才收尾，一条无脑的 `UPDATE ... SET state='done' WHERE id=?`
    会把它翻回 done —— 页面就会显示"重新分析成功"，实际用的是旧结果。
    """
    conn = env
    monkeypatch.setattr(AR, "run", lambda conn_, date="", provider="": {"ok": True})
    qid = _insert_queue(conn, "error", _ts(-60), err="superseded by force rerun")

    assert S._spawn_wb_fallback(DATE, "deepseek", qid) is True
    # 等线程真的收尾（防重入表被 pop）再断言，避免"它还没跑就说它没写"的假绿
    _wait_until(lambda: "wb-fallback:deepseek" not in S._AI_REVIEW_RUNNING, timeout=8)
    row = _queue_row(conn, qid)
    assert row["state"] == "error", "被 force 作废的任务不许被翻回 done"
    assert row["err"].startswith("superseded"), row["err"]


def test_fallback_retry_stops_after_three_failures(env, monkeypatch):
    """兜底连败三次后必须停手 —— 否则每 30 秒真发一次 API 调用，变成请求风暴 + 反复花钱。"""
    conn = env
    spawned: list = []
    monkeypatch.setattr(S, "_spawn_wb_fallback",
                        lambda date, fb, qid: spawned.append((date, fb, qid)) or True)
    for _ in range(3):
        _insert_queue(conn, "error", _ts(-600), err="auto-fallback failed: 401 未授权")
    _insert_queue(conn, "pending", _ts(-40))

    res = S.api_ai_review(date=DATE, provider="workbuddy")

    assert res["state"] == "computing"
    assert not res.get("fallback"), "连败三次后不该再自动兜底"
    assert spawned == [], "更不该真起兜底线程"
    assert "连续失败" in res["note"]


def test_pick_fallback_provider_returns_empty_without_keys(env, monkeypatch):
    """一个能用的 Key 都没有 → 返回空串，调用方据此**不启动兜底**。"""
    monkeypatch.setattr(AR, "provider_key", lambda name: "")
    assert S._pick_fallback_provider() == ""


def test_pick_fallback_provider_prefers_deepseek(env, monkeypatch):
    """都可用时按 PROVIDERS 顺序优先 deepseek；exclude 的 provider 绝不返回。"""
    monkeypatch.setattr(AR, "provider_key", lambda name: "k" if name != "workbuddy" else "local")
    assert S._pick_fallback_provider() == "deepseek"
    assert S._pick_fallback_provider(exclude="deepseek") != "deepseek"
    # workbuddy 恒定返回 "local"，绝不能被选成兜底大脑（它就是没人接单的那一方）
    assert S._pick_fallback_provider(exclude="") != "workbuddy"
