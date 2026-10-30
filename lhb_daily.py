# -*- coding: utf-8 -*-
"""lhb_daily.py —— 龙虎榜 / 板块扩散 / 主线研判 的「每日自动刷新」编排 + 服务内调度钩子。

【背景 · 为什么会有这个模块（2026-10-09 修复）】
龙虎榜四表（lhb_stocks/lhb_seats/lhb_depts/lhb_inst）、板块扩散（lhb_sector_focus）、
主线研判（sector_chain）这三块数据，**此前从未接入服务内调度**：
  · viewer/server.py 的 _scheduler_loop 只管 ingest / kline 预热 / board_flow / predictor /
    policydesk / watchdesk / auction——没有 lhb，也没有 sector_chain；
  · 它们只由外挂脚本 auto_update.py 事后补，而 auto_update.py 自称「本机定时任务的执行体」，
    可系统里唯一的项目计划任务 `\\股票论坛查看器兜底` 实际是 **Disabled**，且动作只是
    `tools\\viewer_service_guard.py --once`（服务保活），根本不跑数据更新。
结果：国庆长假（2026-10-01~10-07 休市，10-08 重开）后，这三块数据一直冻结在节前
最后交易日 **2026-09-30**。

【做法】
把这条链路接进服务内调度：交易日 18:00 之后（龙虎榜晚间才发布）跑一次，依次执行
三个已验证的真实数据脚本（全部幂等，可反复运行）：
  1) lhb_fetch.py        —— 个股/席位/营业部/机构 四张龙虎榜表
  2) lhb_sector_link.py  —— 龙虎榜 → 题材板块 → T+1/T+3 扩散验证（落 lhb_sector_focus）
  3) sector_chain.py     —— 方向→龙头→梯队→分支 主线研判链（落 sector_chain）

【设计要点】
- **幂等**：三个脚本内部都是「按 trade_date 先删后插 / INSERT OR REPLACE」，可反复运行。
- **同步执行，跑完才返回**（2026-10-30 D33 修正，见 `run_daily_sync` 注释）：
  原设计把重活丢进 daemon 后台线程、`maybe_daily` 立即 return，前提是"调用方是个常驻进程"。
  但它真正的调用方是 `tools/sched_job.py` —— 一个短命子进程，print 完就退出，
  CPython 关解释器时把 daemon 线程直接杀掉 → 一轮都没跑、`record_result` 从未执行。
  实测 2026-10-19 之后 191 次 `{'started': today}`、每次 0.1~0.2s，数据冻结 11 天。
  现在 `run_daily_sync` 会一直等到三个子脚本跑完（历史约 484s）；`_JOB_TIMEOUT` 默认 3600s 兜底。
- **当日去重 + 失败重试**：状态写 meta(key='lhb_daily_state')；成功后当天不再跑，
  失败最多重试 MAX_TRIES 次（防限流打爆数据源），第二天自动恢复。
  闸门是 `last_date`（**只在成功时写**），`last_run_at` 无条件写但不参与判定。
- **异常隔离**：`maybe_daily` 仍吞异常（不拖垮调用方），但返回值带 `ok`，
  让 `sched_job` 按「ok is False → rc=1」判失败并退避重试，不再把失败记成完成。
- **可独立运行**：`python lhb_daily.py --force` 供运维 / 计划任务手动驱动。

【红线】只调用本工程已验证的真实数据链路，不产生任何 mock/示例数据。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
LOG_FILE = os.path.join(BASE, "viewer", "logs", "lhb_daily.log")

# ── 2026-10-30 D33 事故加固：测试永远不许写生产 ─────────────────────────────
# 事故经过（真实发生，不是我猜的）：验证 `lhb_daily`「daemon 线程被短命子进程杀掉」时，
# 在**同一个 pytest 进程内**把模块文件换回旧版 → 旧版 `maybe_daily()` 起了一个 daemon 线程
# → 跑完才 return。测试结束、pytest 撤销 monkeypatch 之后，那个**孤儿线程**才把活干完，
# 此时它看到的 `DB` / `LOG_FILE` 已经恢复成**生产路径** → 它把桩数据写进了生产库：
#   · `viewer/logs/lhb_daily.log` 多出 4 条 `started=2026-10-30 18:00:00 … 484.0s`（一眼假）
#   · `meta.lhb_daily_state.last_date` 被推成 2026-10-30
# 后果极重：`_gate` 的闸门是 `last_date` → 当晚 18:00 判 `skip: done_today`，
# **真正该跑的那一轮被自己的测试挡掉了** —— 把上一轮刚修好的 11 天停更 bug 原样复刻一遍。
# 所以下面两个写盘点加一道闸：在 pytest 里、且指向生产路径 → 只 warning，不落盘。
# ⚠️ 但这道闸**只覆盖 pytest 内**（判据是 `PYTEST_CURRENT_TEST`）。事故当场的做法是
#    「非 pytest 的进程内红态验证」（`python _xxx.py` 里把模块换回旧版），那个环境变量
#    根本不存在 —— 所以真正的防线在测试侧，两层：
#      ① tests/test_lhb_daily_offproc.py 的红态证明全部改到**子进程**里跑（路径一开始就指 tmp）；
#      ② 该文件的 `ld` fixture 在 `yield` 之后 join 掉 "lhb-daily" 线程并断言其已退出，
#         保证任何"起线程就返回"的回归都活不过测试收尾（此时它看到的仍是 tmp 路径）。
# 换句话说：环境变量闸防"测试代码误写"，fixture join 防"孤儿线程活过 monkeypatch 撤销"。
_PROD_DB = os.path.abspath(DB)
_PROD_LOG = os.path.abspath(LOG_FILE)


def _prod_write_blocked(path: str, prod: str) -> bool:
    """pytest 环境下、且目标仍是生产路径 → 拦下这次写。返回 True 表示已拦截。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return os.path.abspath(path) == prod
    except Exception:  # noqa: BLE001
        return False

# 解释器：优先用工程虚拟环境的 python（与 auto_update.py / start_viewer.bat 一致）。
_PREFERRED_PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
PY = _PREFERRED_PY if os.path.exists(_PREFERRED_PY) else sys.executable

STATE_KEY = "lhb_daily_state"
PUBLISH_HOUR = 18          # 龙虎榜晚间发布：18:00 之后才保证当日数据就绪
PUBLISH_MIN = 0
MAX_TRIES = 3              # 当天连续失败上限（防限流死循环），次日自动恢复
FETCH_DAYS = 8             # lhb_fetch 向前枚举的自然日数（覆盖周末/假期断档）
LINK_DAYS = 7              # 板块扩散 / 主线研判 回溯的交易日数

_LOG = logging.getLogger("lhb_daily")
if not _LOG.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _LOG.addHandler(_h)
    _LOG.setLevel(logging.INFO)

_lock = threading.Lock()
_running = {"v": False}     # 是否有一轮正在跑（防同进程内重复触发；status() 也读它）
                            # 2026-10-30 D33：以前是"有没有后台线程在跑"，现在一轮是同步跑的，
                            # 语义只剩"本轮是否在执行中"。跨进程去重靠 _OFFPROC_GAP + 父进程那道闸。


# ---------------------------------------------------------------- meta 读写
def ensure_schema(conn) -> None:
    """幂等：确保 meta 键值表存在（其它模块已在用，这里只是兜底）。"""
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    except Exception:  # noqa: BLE001
        _LOG.exception("lhb_daily.ensure_schema 失败（已忽略）")


def _meta_get(conn, key, default=None):
    """读 meta[key]，兼容 sqlite3.Row 与普通 tuple 的 row_factory。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return row["value"]
        except (TypeError, IndexError):
            return row[0]
    except Exception:  # noqa: BLE001
        _LOG.exception("读 meta[%s] 失败", key)
        return default


def _meta_set(conn, key, value) -> bool:
    """写 meta[key]（UPSERT）；独立短事务。"""
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        _LOG.exception("写 meta[%s] 失败", key)
        return False


def _state_read(conn) -> dict:
    raw = _meta_get(conn, STATE_KEY)
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            _LOG.exception("解析 lhb_daily_state 失败，退回空状态")
    return {}


def _state_write(conn, st: dict) -> None:
    _meta_set(conn, STATE_KEY, json.dumps(st, ensure_ascii=False))


# ---------------------------------------------------------------- 时间窗口
def _is_weekday(now: datetime) -> bool:
    """周一~周五（节假日暂不外接日历；跑空只是幂等空转，无害）。"""
    return now.weekday() < 5


def _past_publish(now: datetime) -> bool:
    """是否已过当日龙虎榜发布时点（默认 18:00）。"""
    return (now.hour, now.minute) >= (PUBLISH_HOUR, PUBLISH_MIN)


# ---------------------------------------------------------------- 执行
def _run_script(script: str, args: list[str], timeout: int) -> dict:
    """以子进程方式跑某个真实数据脚本（同名约定：cwd=BASE，用工程 .venv python）。"""
    cmd = [PY, os.path.join(BASE, script)] + list(args)
    t0 = time.time()
    try:
        r = subprocess.run(
            cmd, cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=timeout)
        tail = "\n".join((r.stdout or "").splitlines()[-6:])
        return {"script": script, "returncode": r.returncode,
                "seconds": round(time.time() - t0, 1), "tail": tail}
    except Exception as e:  # noqa: BLE001
        return {"script": script, "returncode": None,
                "seconds": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {e}"[:200]}


def run_once(days_fetch: int = FETCH_DAYS, days_link: int = LINK_DAYS) -> dict:
    """立即跑一轮完整刷新（真实数据、幂等）。返回汇总；是否落 meta 由调用方决定。"""
    out: dict = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "steps": []}
    steps = (
        ("lhb_fetch.py",       ["--days", str(days_fetch)], 900),
        ("lhb_sector_link.py", ["--days", str(days_link)], 1800),
        ("sector_chain.py",    ["--days", str(days_link)], 1800),
    )
    for name, args, to in steps:
        res = _run_script(name, args, to)
        out["steps"].append(res)
        _LOG.info("lhb_daily 步骤 %s rc=%s %ss", name, res.get("returncode"),
                  res.get("seconds"))
    out["ok"] = all(s.get("returncode") == 0 for s in out["steps"])
    out["ended"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def _append_result_log(res: dict) -> None:
    """把每一轮结果追加到 viewer/logs/lhb_daily.log（供巡检/证据留存）。"""
    if _prod_write_blocked(LOG_FILE, _PROD_LOG):
        # 孤儿 daemon 线程会活过 monkeypatch 撤销，撤销后它看到的又是生产路径（见文件头事故说明）
        _LOG.warning("写 lhb_daily.log 被拦下：pytest 环境且指向生产日志 %s（不落盘）", LOG_FILE)
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        line = (f"=== {res.get('started')}~{res.get('ended')} ok={res.get('ok')} "
                f"steps=" + "; ".join(
                    f"{s['script']}(rc={s.get('returncode')},{s.get('seconds')}s)"
                    for s in res.get("steps", [])) + " ===\n")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        _LOG.exception("写 lhb_daily.log 失败（已忽略）")


def record_result(res: dict, today: str) -> None:
    """把一轮结果写回 meta（当日去重/失败计数）。独立短事务。"""
    if _prod_write_blocked(DB, _PROD_DB):
        # 见文件头事故说明：`last_date` 一旦被测试推成今天，当晚 18:00 就会被判 done_today。
        _LOG.warning("record_result 被拦下：pytest 环境且指向生产库 %s（不落盘）", DB)
        return
    conn = None
    try:
        conn = sqlite3.connect(DB, timeout=30)
        ensure_schema(conn)
        st = _state_read(conn)
        st["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["last_result"] = {k: res.get(k) for k in ("ok", "started", "ended")}
        if res.get("ok"):
            st["last_date"] = today
            st["last_ok_at"] = st["last_run_at"]
            st["tries"] = 0
            st["last_error"] = None
        else:
            if st.get("tries_date") != today:
                st["tries_date"] = today
                st["tries"] = 1
            else:
                st["tries"] = int(st.get("tries", 0)) + 1
            st["last_error"] = "; ".join(
                f"{s['script']}:rc={s.get('returncode')}" for s in res.get("steps", [])
                if s.get("returncode") != 0) or "unknown"
        _state_write(conn, st)
    except Exception:  # noqa: BLE001
        _LOG.exception("回写 lhb_daily_state 失败（已忽略）")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    _append_result_log(res)


def _gate(conn, now: datetime | None = None):
    """判断「这一轮该不该跑」。返回 `(skip, today)`：`skip` 非 None 表示本轮跳过。

    2026-10-30 D33：抽出来给 `run_daily_sync` 与 `maybe_daily` **共用同一份判断**。
    两处若各写一套，「手工跑得成、调度跑不成」这种鬼故事迟早再来一次。

    判断顺序（与历史实现逐条一致，只搬不改）：
      周末 → 未到 18:00 发布时点 → 今天已成功过 → 今天失败已达 MAX_TRIES → 有实例在跑。
    ⚠️ 闸门是 `last_date`（只在成功时写），**不是** `last_run_at`（无条件写，只给巡检看）。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not _is_weekday(now):
        return {"skip": "weekend"}, today
    if not _past_publish(now):
        return {"skip": "pre_publish"}, today
    st = _state_read(conn)
    if st.get("last_date") == today:
        return {"skip": "done_today"}, today
    tries = int(st.get("tries", 0)) if st.get("tries_date") == today else 0
    if tries >= MAX_TRIES:
        return {"skip": "failed_today"}, today
    with _lock:
        if _running["v"]:
            return {"skip": "running"}, today
    return None, today


def run_daily_sync(conn) -> dict:
    """**同步**跑完一轮日更，跑完才返回（供 `tools/sched_job.py` 的子进程调用）。

    2026-10-30 D33 —— 这是一个「返回码 0 骗了 11 天」的 bug，病因是**谁活到最后**：
      · 旧 `maybe_daily()` 起一个 `daemon=True` 线程后**立刻 return**（设计前提是"服务里
        有个常驻调度线程，不阻塞它"）；
      · 但它**唯一的调用方**是 `tools/sched_job.py`：父进程只为「起进程 + 看退出码」，
        子进程 print 完 `{'started': today}` 就 `return 0` 退出；
      · CPython 关闭解释器时**直接杀掉 daemon 线程** → `_worker()` 一行没跑 →
        `record_result()` 永不执行 → `last_date` 停在上一次成功那天 → 第二天重演。
    实测后果：`meta.lhb_daily_state.last_run_at` 冻结在 2026-10-19 22:24:56，
    `viewer/logs/lhb_daily.log` 最后一条也是 10-19；此后 191 次 `{'started': today}`，
    每次 **0.1~0.2s**（真跑一轮约 484s）—— 10-29 的明细空洞就是这么攒出来的。

    改成同步后，子进程必须等这一轮真跑完才退出。返回值带 `ok`，让 `sched_job.main()`
    的「`r.get("ok") is False` → 非 0 退出」生效（那条规则是为 a_shares_snapshot 加的，
    这里正好用上：失败要能被父进程看见并退避重试，而不是记成"今天跑过了"）。
    """
    skip, today = _gate(conn)
    if skip is not None:
        _LOG.info("lhb_daily 本轮跳过：%s", skip.get("skip"))
        return skip

    with _lock:
        if _running["v"]:                      # double-check，防止与同进程别的调用叠跑
            return {"skip": "running"}
        _running["v"] = True
    try:
        # 先记 last_trigger_at（供巡检；**不是闸门**，见 _gate）。写失败不影响主流程。
        try:
            st = _state_read(conn)
            st["last_trigger_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _state_write(conn, st)
        except Exception:  # noqa: BLE001
            _LOG.exception("写 last_trigger_at 失败（已忽略）")

        _LOG.info("lhb_daily 开始同步执行（%s）", today)
        res = run_once()                       # ← 三个子脚本，历史约 484s，这里会一直等
        record_result(res, today)              # ← 只有它写 last_date / tries / lhb_daily.log
        _LOG.info("lhb_daily 本轮结束 ok=%s", res.get("ok"))
        return {
            "started": today,
            "ok": bool(res.get("ok")),
            "steps": [{"script": s.get("script"), "returncode": s.get("returncode"),
                       "seconds": s.get("seconds")} for s in res.get("steps", [])],
        }
    finally:
        with _lock:
            _running["v"] = False


# ---------------------------------------------------------------- 调度钩子
def maybe_daily(conn) -> dict:
    """历史名，现为 `run_daily_sync` 的别名（**同步**，跑完才返回）。

    为什么改：原实现是「起 daemon 线程后立即返回」，而它的唯一调用方是短命子进程
    `tools/sched_job.py` —— 线程必被进程退出腰斩，详见 `run_daily_sync` 的注释。
    保留这个名字是因为 `tools/sched_job.py:52` 与可能的巡检脚本按名调用；行为已统一。
    异常在这里仍然吞掉（沿用历史契约：调度钩子不拖垮调用方），但**返回值会带 `ok`**，
    让 `sched_job` 能按「ok is False → rc=1」判失败。
    """
    try:
        return run_daily_sync(conn)
    except Exception:  # noqa: BLE001 —— 调度钩子绝不外抛
        _LOG.exception("lhb_daily.maybe_daily 异常（已忽略）")
        return {"error": "exception", "ok": False}


def status(conn=None) -> dict:
    """返回当前刷新状态（供 API/巡检读取）。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    try:
        st = _state_read(conn)
        st["running"] = bool(_running["v"])
        st["publish_after"] = f"{PUBLISH_HOUR:02d}:{PUBLISH_MIN:02d}"
        st["fetch_days"] = FETCH_DAYS
        st["link_days"] = LINK_DAYS
        return st
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 独立入口
def main() -> int:
    ap = argparse.ArgumentParser(description="龙虎榜/板块扩散/主线研判 每日刷新（真实数据）")
    ap.add_argument("--force", action="store_true", help="忽略时间窗口与当日去重，立即跑一轮")
    ap.add_argument("--days-fetch", type=int, default=FETCH_DAYS, help="龙虎榜回溯自然日数")
    ap.add_argument("--days-link", type=int, default=LINK_DAYS, help="扩散/研判回溯交易日数")
    ap.add_argument("--status", action="store_true", help="只打印当前状态")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2), flush=True)
        return 0

    now = datetime.now()
    if not args.force:
        if not _is_weekday(now) or not _past_publish(now):
            print(f"[lhb_daily] 非刷新窗口（{now:%Y-%m-%d %H:%M}）；如需强跑请加 --force",
                  flush=True)
            return 0

    res = run_once(args.days_fetch, args.days_link)
    record_result(res, now.strftime("%Y-%m-%d"))
    print("[lhb_daily] " + json.dumps({
        "ok": res["ok"], "started": res["started"], "ended": res["ended"],
        "steps": [{"script": s["script"], "rc": s.get("returncode"),
                   "seconds": s.get("seconds")} for s in res["steps"]],
    }, ensure_ascii=False), flush=True)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
