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
- **不阻塞调度线程**：真正的重活（含大量 F10/行情请求，可能数分钟）放到 daemon 后台线程；
  maybe_daily 只在「该跑且当天没跑过」时立即返回。
- **当日去重 + 失败重试**：状态写 meta(key='lhb_daily_state')；成功后当天不再跑，
  失败最多重试 MAX_TRIES 次（防限流打爆数据源），第二天自动恢复。
- **异常隔离**：任何异常只记日志，绝不外抛拖垮调度线程（与 board_flow/predictor 一致）。
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
_running = {"v": False}     # 是否有后台刷新线程在跑（防止重复触发）


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


def _worker(today: str) -> None:
    """后台线程主体：跑一轮 → 回写状态 → 释放 running 标志。"""
    try:
        res = run_once()
        record_result(res, today)
        _LOG.info("lhb_daily 本轮结束 ok=%s", res.get("ok"))
    except Exception:  # noqa: BLE001
        _LOG.exception("lhb_daily 后台刷新异常（已忽略）")
    finally:
        with _lock:
            _running["v"] = False


# ---------------------------------------------------------------- 服务内调度钩子
def maybe_daily(conn) -> dict:
    """由 viewer/server.py 的 _scheduler_loop 每轮调用。

    交易日 18:00 之后、且当天尚未成功刷新时，触发一次后台刷新线程；否则直接返回。
    任何异常都吞掉，绝不拖垮调度线程。
    """
    try:
        now = datetime.now()
        if not _is_weekday(now):
            return {"skip": "weekend"}
        if not _past_publish(now):
            return {"skip": "pre_publish"}
        today = now.strftime("%Y-%m-%d")
        st = _state_read(conn)
        if st.get("last_date") == today:
            return {"skip": "done_today"}
        tries = int(st.get("tries", 0)) if st.get("tries_date") == today else 0
        if tries >= MAX_TRIES:
            return {"skip": "failed_today"}
        with _lock:
            if _running["v"]:
                return {"skip": "running"}
            _running["v"] = True
        try:
            st["last_trigger_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _state_write(conn, st)
            threading.Thread(target=_worker, args=(today,),
                             name="lhb-daily", daemon=True).start()
            _LOG.info("lhb_daily 已触发后台刷新（%s）", today)
            return {"started": today}
        except Exception:  # noqa: BLE001
            with _lock:
                _running["v"] = False
            raise
    except Exception:  # noqa: BLE001 —— 调度钩子绝不能外抛
        _LOG.exception("lhb_daily.maybe_daily 异常（已忽略）")
        return {"error": "exception"}


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
