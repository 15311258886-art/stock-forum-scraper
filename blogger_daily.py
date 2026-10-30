# -*- coding: utf-8 -*-
"""blogger_daily.py —— 「博主荐股 / 参赛股」每日自动刷新的编排 + 服务内调度钩子。

【背景 · 为什么会有这个模块（2026-10-09 重构）】
博主荐股这块数据的**唯一真源是本机 Excel**
`F:\\AI Agent部署知识库\\独股九剑推荐股票追踪.xlsx`。此前查看器是读另一个 App 二次导出的
bloggers.json，链路长、易冻结（原 App 不跑 → 查看器数据不更新）。重构后改由
`tools/import_bloggers_direct.py` 直读 Excel，本模块负责「什么时候去灌」：
  · **源表一变就灌**：每轮调度比对 Excel 的 mtime，用户存一次 Excel → 自动重新导入；
  · **每天兜底一次**：即使 Excel 没动，也在每日定点后重跑一次（顺带刷新 contest.json）。

【设计要点（对齐 lhb_daily.py 的既有工程约定）】
- **幂等**：import_bloggers_direct.py 内部是 UPSERT，可反复运行，不会重复/丢行。
- **不阻塞调度线程**：真正的导入放到 daemon 后台线程；maybe_daily 只在「该跑」时立即返回。
- **mtime 去重 + 当日兜底 + 失败重试**：状态写 meta(key='blogger_daily_state')；
  源表未变且今日已兜底过 → 不跑；失败最多重试 MAX_TRIES 次，次日自动恢复。
- **异常隔离**：任何异常只记日志，绝不外抛拖垮调度线程（与 lhb_daily 一致）。
- **可独立运行**：`python blogger_daily.py --force` 供运维 / 计划任务 / 用户手动一键灌数。

【红线】只调用本工程已验证的真实数据链路（直读源头 Excel / 磁盘上的 contest.json），
不产生任何 mock/示例数据。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
LOG_FILE = os.path.join(BASE, "viewer", "logs", "blogger_daily.log")
IMPORT_SCRIPT = os.path.join("tools", "import_bloggers_direct.py")

# ── 2026-10-30 D33 事故加固：测试永远不许写生产（与 lhb_daily 同一套）─────────
# 起因见 lhb_daily.py 文件头的「事故经过」：孤立的 daemon 线程会活过 pytest 的 monkeypatch
# 撤销，撤销之后它看到的 DB / LOG_FILE 又是生产路径，于是把桩数据写进了生产。
# 本模块已随博主荐股下线（不在 server/_spawn_offproc 的注册表里），但代码还在、函数还能被
# 直接调用，所以闸一样要加。
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


def _xlsx_path() -> str:
    """动态解析源头 Excel —— 统一委托给 tools/import_bloggers_direct.py 的候选解析（单一真源）。

    2026-10-11 事故：源目录整体搬进 `豆包每日抓取\\`，而这里写死旧路径 → 每日任务 rc=2、
    博主荐股+参赛股一起断更。故**不再写死、也不在启动时只算一次**，每次调用都重新解析。
    """
    try:
        tdir = os.path.join(BASE, "tools")
        if tdir not in sys.path:
            sys.path.insert(0, tdir)
        import import_bloggers_direct as _ibd
        return _ibd.pick_newest(_ibd._XLSX_CANDIDATES, "BLOGGER_XLSX")
    except Exception:  # noqa: BLE001
        return os.environ.get(
            "BLOGGER_XLSX", r"F:\AI Agent部署知识库\豆包每日抓取\独股九剑推荐股票追踪.xlsx")


XLSX = _xlsx_path()      # 仅作展示/兜底；判断一律走 _xlsx_path()

# 解释器：优先用工程虚拟环境的 python（与 lhb_daily.py / start_viewer.bat 一致）。
_PREFERRED_PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
PY = _PREFERRED_PY if os.path.exists(_PREFERRED_PY) else sys.executable

STATE_KEY = "blogger_daily_state"
DAILY_HOUR = 8             # 每日兜底刷新时点（08:30），早于开盘，用户开电脑即有最新数据
DAILY_MIN = 30
MAX_TRIES = 3              # 当天连续失败上限（防死循环），次日自动恢复
RUN_TIMEOUT = 120          # 单次导入超时（秒）；纯本地文件读写，正常 <5s

_LOG = logging.getLogger("blogger_daily")
if not _LOG.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _LOG.addHandler(_h)
    _LOG.setLevel(logging.INFO)

_lock = threading.Lock()
_running = {"v": False}     # 是否有后台导入线程在跑（防止重复触发）


# ---------------------------------------------------------------- meta 读写
def ensure_schema(conn) -> None:
    """幂等：确保 meta 键值表存在（其它模块已在用，这里只是兜底）。"""
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    except Exception:  # noqa: BLE001
        _LOG.exception("blogger_daily.ensure_schema 失败（已忽略）")


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
            _LOG.exception("解析 blogger_daily_state 失败，退回空状态")
    return {}


def _state_write(conn, st: dict) -> None:
    _meta_set(conn, STATE_KEY, json.dumps(st, ensure_ascii=False))


# ---------------------------------------------------------------- 源表指纹
def source_mtime() -> str:
    """源头 Excel 的 mtime 指纹（'YYYY-MM-DD HH:MM:SS'）；取不到回 ''。"""
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(_xlsx_path())).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


def _past_daily(now: datetime.datetime) -> bool:
    """是否已过每日兜底时点（默认 08:30）。"""
    return (now.hour, now.minute) >= (DAILY_HOUR, DAILY_MIN)


# ---------------------------------------------------------------- 执行
def run_once(quiet: bool = True, no_contest: bool = False) -> dict:
    """以子进程方式跑一次真实导入（幂等）。返回 {returncode, seconds, tail, mtime}。"""
    cmd = [PY, os.path.join(BASE, IMPORT_SCRIPT)]
    if quiet:
        cmd.append("--quiet")
    if no_contest:
        cmd.append("--no-contest")
    t0 = time.time()
    try:
        r = subprocess.run(
            cmd, cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=RUN_TIMEOUT)
        tail = "\n".join((r.stdout or "").splitlines()[-4:])
        return {"returncode": r.returncode, "seconds": round(time.time() - t0, 1),
                "tail": tail, "mtime": source_mtime()}
    except Exception as e:  # noqa: BLE001
        return {"returncode": None, "seconds": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {e}"[:200], "mtime": source_mtime()}


def _append_result_log(res: dict, trigger: str) -> None:
    """把每一轮结果追加到 viewer/logs/blogger_daily.log（供巡检/证据留存）。"""
    if _prod_write_blocked(LOG_FILE, _PROD_LOG):
        _LOG.warning("写 blogger_daily.log 被拦下：pytest 环境且指向生产日志 %s（不落盘）", LOG_FILE)
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        line = (f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} trigger={trigger} "
                f"rc={res.get('returncode')} {res.get('seconds')}s mtime={res.get('mtime')} "
                f"| {res.get('tail') or res.get('error')} ===\n")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        _LOG.exception("写 blogger_daily.log 失败（已忽略）")


def _record_result(res: dict, today: str, trigger: str) -> None:
    """把一轮结果写回 meta（mtime 去重 / 当日兜底去重 / 失败计数）。独立短事务。"""
    if _prod_write_blocked(DB, _PROD_DB):
        _LOG.warning("_record_result 被拦下：pytest 环境且指向生产库 %s（不落盘）", DB)
        return
    conn = None
    try:
        conn = sqlite3.connect(DB, timeout=30)
        ensure_schema(conn)
        st = _state_read(conn)
        st["last_run_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["last_trigger"] = trigger
        st["last_result"] = {k: res.get(k) for k in ("returncode", "seconds")}
        if res.get("returncode") == 0:
            st["last_ok_at"] = st["last_run_at"]
            st["last_src_mtime"] = res.get("mtime") or ""
            st["last_date"] = today          # 当日兜底已满足
            st["tries"] = 0
            st["last_error"] = None
        else:
            if st.get("tries_date") != today:
                st["tries_date"] = today
                st["tries"] = 1
            else:
                st["tries"] = int(st.get("tries", 0)) + 1
            st["last_error"] = res.get("error") or ("rc=%s" % res.get("returncode"))
        _state_write(conn, st)
    except Exception:  # noqa: BLE001
        _LOG.exception("回写 blogger_daily_state 失败（已忽略）")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    _append_result_log(res, trigger)


def _worker(today: str, trigger: str) -> None:
    """后台线程主体：跑一轮 → 回写状态 → 释放 running 标志。"""
    try:
        res = run_once(quiet=True)
        _record_result(res, today, trigger)
        _LOG.info("blogger_daily 本轮结束 rc=%s trigger=%s", res.get("returncode"), trigger)
    except Exception:  # noqa: BLE001
        _LOG.exception("blogger_daily 后台刷新异常（已忽略）")
    finally:
        with _lock:
            _running["v"] = False


# ---------------------------------------------------------------- 服务内调度钩子
def maybe_daily(conn) -> dict:
    """由 viewer/server.py 的 _scheduler_loop 每轮调用（约每 10s 一次）。

    触发条件（满足其一）：
      · 源表 Excel 的 mtime 变了（用户刚存过表）→ 立即重导（用户改完 Excel 自动灌数）；
      · 已过每日兜底时点（08:30）且当天还没成功跑过 → 兜底重跑一次（顺带刷新 contest.json）。
    任何异常都吞掉，绝不拖垮调度线程。
    """
    try:
        now = datetime.datetime.now()
        today = now.strftime("%Y-%m-%d")
        st = _state_read(conn)

        cur_mtime = source_mtime()
        changed = bool(cur_mtime) and cur_mtime != (st.get("last_src_mtime") or "")
        need_daily = _past_daily(now) and st.get("last_date") != today
        if not (changed or need_daily):
            return {"skip": "up_to_date"}
        trigger = "mtime" if changed else "daily"

        tries = int(st.get("tries", 0)) if st.get("tries_date") == today else 0
        if tries >= MAX_TRIES:
            return {"skip": "failed_today"}

        with _lock:
            if _running["v"]:
                return {"skip": "running"}
            _running["v"] = True
        try:
            st["last_trigger_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            st["last_trigger"] = trigger
            _state_write(conn, st)
            threading.Thread(target=_worker, args=(today, trigger),
                             name="blogger-daily", daemon=True).start()
            _LOG.info("blogger_daily 已触发后台导入（%s / %s）", trigger, today)
            return {"started": today, "trigger": trigger}
        except Exception:  # noqa: BLE001
            with _lock:
                _running["v"] = False
            raise
    except Exception:  # noqa: BLE001 —— 调度钩子绝不能外抛
        _LOG.exception("blogger_daily.maybe_daily 异常（已忽略）")
        return {"error": "exception"}


def status(conn=None) -> dict:
    """返回当前刷新状态（供 API / 巡检读取）。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    try:
        st = _state_read(conn)
        st["running"] = bool(_running["v"])
        st["current_src_mtime"] = source_mtime()
        st["source_exists"] = os.path.exists(XLSX)
        st["daily_after"] = "%02d:%02d" % (DAILY_HOUR, DAILY_MIN)
        st["source"] = XLSX
        return st
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 独立入口
def main() -> int:
    ap = argparse.ArgumentParser(description="博主荐股/参赛股 每日刷新（直读源头 Excel，真实数据）")
    ap.add_argument("--force", action="store_true", help="忽略 mtime/时间窗口，立即跑一轮")
    ap.add_argument("--no-contest", action="store_true", help="只灌博主荐股三表，跳过 contest")
    ap.add_argument("--status", action="store_true", help="只打印当前状态")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2), flush=True)
        return 0

    now = datetime.datetime.now()
    res = run_once(quiet=False, no_contest=args.no_contest)
    _record_result(res, now.strftime("%Y-%m-%d"), "force" if args.force else "manual")
    print("[blogger_daily] " + json.dumps({
        "ok": res.get("returncode") == 0,
        "returncode": res.get("returncode"),
        "seconds": res.get("seconds"),
        "src_mtime": res.get("mtime"),
        "tail": res.get("tail") or res.get("error"),
    }, ensure_ascii=False), flush=True)
    return 0 if res.get("returncode") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
