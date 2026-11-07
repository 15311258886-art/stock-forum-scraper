# -*- coding: utf-8 -*-
"""金十数据中心 · 微博个股人气指数 日更采集（接产品只读 app.db）。

来源真相（诚实，勿夸大）：
  akshare 的 `stock_js_weibo_report` 并非爬微博，而是打金十数据中心的
  `https://datacenter-api.jin10.com/weibo/list` 接口，把微博讨论热度打包成
  「个股人气排行指数」—— 按股票**名称**返回，每只票一个 `rate` 热度值（区间约 -1~1，
  负值代表该票讨论偏向下跌方向、正值偏向上行方向；绝对值越大讨论越热）。
  本模块直接调用该端点（已实测 HTTP 200、返回约 50 只票、免费、无需登录）。

落库：
  写入 `weibo_heat(code, trade_date, timescale, rate, raw_name)`，名称经 `stock_names`
  反查为 code；解析不到的名称（指数 / ETF / 错别字 / 未覆盖票）丢弃并计数，绝不编造 code。

调度：
  注册为 `tools/sched_job.JOBS["weibo_heat"]`，由 `viewer/server.py::_sched_tasks`
  的 `_spawn_offproc("weibo_heat")` 触发；自带「当日 done」闸门（meta.weibo_heat_done），
  每天最多落一次，避免每 tick 重跑。

⚠️ 这是**注意力 / 热度信号**（不是全文情绪），属数据输入层，不是四 tab 判据本身。
  它是否真改善舆情 / 共振面板，由接入后的信号验证决定（见 DECISIONS），不在此处假定。
"""
from __future__ import annotations

import os
import sys
import time
import json
import sqlite3
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB_PATH = os.path.join(ROOT, "viewer", "app.db")

JIN10_URL = "https://datacenter-api.jin10.com/weibo/list"
JIN10_HEADERS = {
    "authority": "datacenter-api.jin10.com",
    "x-app-id": "rU6QIu7JHe2gOUeR",
    "x-version": "1.0.0",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "origin": "https://datacenter.jin10.com",
    "referer": "https://datacenter.jin10.com/market",
    "accept-language": "zh-CN,zh;q=0.9",
}
DEFAULT_TIMESCALE = "CNHOUR12"
META_DONE_KEY = "weibo_heat_done"


def fetch_heat(timescale: str = DEFAULT_TIMESCALE) -> list[dict]:
    """打金十端点，返回 [{"name": str, "rate": float|None}, ...]。失败抛异常由调用方处理。"""
    import requests

    r = requests.get(
        JIN10_URL,
        params={"timescale": timescale, "_": int(time.time() * 1000)},
        headers=JIN10_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json() or {}
    data = payload.get("data") or []
    out: list[dict] = []
    for row in data:
        nm = (row.get("name") or "").strip()
        try:
            rate = float(row.get("rate"))
        except (TypeError, ValueError):
            rate = None
        if nm:
            out.append({"name": nm, "rate": rate})
    return out


def _norm(nm: str) -> str:
    # 去全/半角空格，避免「比亚迪 」之类尾随空白匹配失败
    return nm.replace(" ", "").replace(" ", "").strip()


def resolve_codes(rows: list[dict], conn) -> tuple[list[dict], int]:
    """名称 → code（查 stock_names）。解析不到的丢弃并计数。"""
    cur = conn.cursor()
    cache: dict[str, str | None] = {}
    resolved: list[dict] = []
    skipped = 0
    for row in rows:
        nm = _norm(row["name"])
        if nm in cache:
            code = cache[nm]
        else:
            try:
                hit = cur.execute(
                    "SELECT code FROM stock_names WHERE name=?", (nm,)
                ).fetchone()
            except Exception:  # noqa: BLE001
                hit = None
            code = hit[0] if hit else None
            cache[nm] = code
        if code:
            resolved.append({"code": code, "name": row["name"], "rate": row["rate"]})
        else:
            skipped += 1
    return resolved, skipped


def run_daily_sync(conn=None, timescale: str = DEFAULT_TIMESCALE) -> dict:
    """同步跑完一轮：fetch → resolve → upsert → 置 done 闸门。返回 {ok, ...}。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
    try:
        # 防御性建表（产品库引导在 db.py；独立子进程首次跑时兜底）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta "
            "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT "
            "DEFAULT (datetime('now','localtime')))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS weibo_heat ("
            "  code TEXT NOT NULL,"
            "  trade_date TEXT NOT NULL,"
            "  timescale TEXT NOT NULL,"
            "  rate REAL,"
            "  raw_name TEXT,"
            "  fetched_at TEXT DEFAULT (datetime('now','localtime')),"
            "  PRIMARY KEY (code, trade_date, timescale)"
            ")"
        )
        today = datetime.date.today().strftime("%Y-%m-%d")
        done = (conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_DONE_KEY,)
        ).fetchone() or (None,))[0]
        if done == today:
            return {"ok": True, "skipped": "done", "date": today}

        t0 = time.time()
        rows = fetch_heat(timescale)
        resolved, skipped = resolve_codes(rows, conn)
        cur = conn.cursor()
        n = 0
        for rec in resolved:
            cur.execute(
                "INSERT OR REPLACE INTO weibo_heat "
                "(code, trade_date, timescale, rate, raw_name, fetched_at) "
                "VALUES (?,?,?,?,?, datetime('now','localtime'))",
                (rec["code"], today, timescale, rec["rate"], rec["name"]),
            )
            n += 1
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (META_DONE_KEY, today),
        )
        conn.commit()
        return {
            "ok": True,
            "date": today,
            "timescale": timescale,
            "fetched": len(rows),
            "mapped": n,
            "skipped_unmapped": skipped,
            "elapsed": round(time.time() - t0, 2),
        }
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    r = run_daily_sync(timescale=DEFAULT_TIMESCALE)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
