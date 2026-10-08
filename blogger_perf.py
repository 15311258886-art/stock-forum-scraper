# -*- coding: utf-8 -*-
"""博主荐股「荐股后固定周期收益」（T+1 隔日 / T+3 / T+5 / T+10）。

口径（2026-10-01 与王总确认）：
  · 基准价 = **荐股日收盘价**（即 blogge_reco.rec_date 当日收盘）
  · r1 = 基准日之后第 1 个交易日收盘 / 基准日收盘 - 1（「隔日」）
  · r3 / r5 / r10 同理，分别表示之后第 3 / 5 / 10 个交易日收盘的持有收益
  · 不足 N 个交易日（比如今天刚荐、尚未走完）→ **一律 null，绝不写 0**
    写 0 会被读成「没赚没亏」，与「还没走完」是两回事（沿用 contest_track 的硬口径）

数据源：新浪日 K（money.finance.sina.com.cn）。东财 push2 本机不可达（2026-10-01 实测
全部 RemoteDisconnected），新浪日 K 实测 136ms 可用，故只走新浪、不引入双源。

落库：新表 `blogger_perf`，UNIQUE(blogger, rec_date, stock_code) 幂等 UPSERT。
幂等建表（CREATE TABLE IF NOT EXISTS），**不依赖 tools/import_blogger_contest.py 的 DDL**，
因为这个模块的数据是算出来的、不是导入的；这样即使 import 脚本没重跑，本表也照样可用。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")

# 出网口径与项目其它模块（emapi / board_flow）一致：**直连优先**（显式忽略环境代理，
# 免得本机失效的 http(s)_proxy 把请求带歪或返回 502）→ 失败再回退系统代理。
# 2026-10-09：原先 _fetch_kline 用裸 urllib.request.urlopen（会吃 env 代理），改为下述 opener。
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OPENER_PROXY = urllib.request.build_opener()

DDL = """
CREATE TABLE IF NOT EXISTS blogger_perf (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  blogger TEXT NOT NULL, rec_date TEXT, stock_code TEXT,
  base_close REAL, r1 REAL, r3 REAL, r5 REAL, r10 REAL,
  days_avail INTEGER, updated TEXT,
  UNIQUE(blogger, rec_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_bp_code ON blogger_perf(stock_code);
"""

WINDOWS = (1, 3, 5, 10)          # 需要的「之后第 N 个交易日」
DAYS_BACK = 60                    # 往前多取些，保证 rec_date 落在交易日上
_WORKERS = 8
_KCACHE: dict[str, list] = {}     # 进程内按 code 缓存日 K（一只票可能被多位博主/多日推荐）
_KCACHE_TS: dict[str, float] = {}
_KCACHE_TTL = 900                 # 15 分钟内复用同一份日 K


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    con = _conn()
    try:
        con.executescript(DDL)
        con.commit()
    finally:
        con.close()


def sina_symbol(code: str) -> str:
    """6 位代码 → 新浪日 K 需要的 sh/sz/bj 前缀。"""
    c = str(code or "").strip()
    if not c or not c.isdigit():
        return ""
    if c.startswith("92"):          # 北交所新代码段（920xxx），必须先判，否则会被 sh 前缀吃掉
        return "bj" + c
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("0", "2", "3")):
        return "sz" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sh" + c


def _fetch_kline(sym: str) -> list:
    """取日 K（升序 [{day, close}]），失败返回 []。"""
    now = time.time()
    hit = _KCACHE.get(sym)
    if hit is not None and now - _KCACHE_TS.get(sym, 0) < _KCACHE_TTL:
        return hit
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=5&datalen=%d"
           % (sym, DAYS_BACK + 40))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.sina.com.cn",
    })
    try:
        raw = None
        for _op in (_OPENER_DIRECT, _OPENER_PROXY):     # 直连优先，忽略 env 代理；失败再回退系统代理
            try:
                with _op.open(req, timeout=10) as _resp:
                    raw = _resp.read().decode("utf-8", "ignore").strip()
                break
            except Exception:  # noqa: BLE001
                raw = None
        if not raw or raw in ("null", "[]"):
            raise ValueError("empty kline")
        arr = json.loads(raw)
        if not isinstance(arr, list):
            raise ValueError("bad payload")
        out = []
        for it in arr:
            d = it.get("day")
            c = it.get("close")
            if not d or c in (None, ""):
                continue
            try:
                out.append((str(d)[:10], float(c)))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
    except Exception:
        out = []
    _KCACHE[sym] = out
    _KCACHE_TS[sym] = now
    return out


def _rows_for(arg: tuple) -> tuple | None:
    blogger, rec_date, code = arg
    k = _fetch_kline(sina_symbol(code))
    if len(k) < 2:
        return None
    base_d, base_c = None, None
    for d, c in k:
        if d <= str(rec_date):
            base_d, base_c = d, c
        else:
            break
    if base_c is None:           # rec_date 早于日K起点（极少），取第一段
        base_d, base_c = k[0]
    nxt = [x for x in k if x[0] > base_d]
    out = {"r1": None, "r3": None, "r5": None, "r10": None, "days_avail": len(nxt)}
    for w in WINDOWS:
        if len(nxt) >= w:
            out["r%d" % w] = round((nxt[w - 1][1] / base_c - 1.0) * 100.0, 2)
    return (blogger, rec_date, code, round(base_c, 3),
            out["r1"], out["r3"], out["r5"], out["r10"],
            out["days_avail"], time.strftime("%Y-%m-%d %H:%M:%S"))


def compute_all(force: bool = False, limit: int = 0) -> dict:
    """全量算一遍并 UPSERT。首次需抓约 200+ 次日 K，8 并发约数秒；当天第二次直接读库秒回。"""
    t0 = time.time()
    ensure_table()
    con = _conn()
    try:
        job = []
        for r in con.execute(
                "SELECT blogger, rec_date, stock_code FROM blogger_reco"
                + (" LIMIT %d" % limit if limit else "")):
            if force:
                job.append((r["blogger"], r["rec_date"], r["stock_code"]))
            elif not con.execute(
                    "SELECT 1 FROM blogger_perf WHERE blogger=? AND rec_date=? AND stock_code=?",
                    (r["blogger"], r["rec_date"], r["stock_code"])).fetchone():
                job.append((r["blogger"], r["rec_date"], r["stock_code"]))
    finally:
        con.close()

    done = fail = 0
    if job:
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            for res in ex.map(_rows_for, job):
                if not res:
                    fail += 1
                    continue
                con = _conn()
                try:
                    con.execute(
                        "INSERT INTO blogger_perf (blogger, rec_date, stock_code, base_close,"
                        " r1, r3, r5, r10, days_avail, updated)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(blogger, rec_date, stock_code) DO UPDATE SET"
                        " base_close=excluded.base_close, r1=excluded.r1, r3=excluded.r3,"
                        " r5=excluded.r5, r10=excluded.r10, days_avail=excluded.days_avail,"
                        " updated=excluded.updated", res)
                    con.commit()
                finally:
                    con.close()
                done += 1

    con = _conn()
    try:
        n = con.execute("SELECT COUNT(*) AS n FROM blogger_perf").fetchone()["n"]
    finally:
        con.close()
    return {"queued": len(job), "inserted": done, "failed": fail,
            "total": n, "ms": int((time.time() - t0) * 1000)}


def get(code: str | None = None, blogger: str | None = None) -> list:
    """按股票代码（或按博主）取周期收益行；没算过的返回空列表，不抛错。"""
    ensure_table()
    con = _conn()
    try:
        sql = "SELECT blogger, rec_date, stock_code, base_close, r1, r3, r5, r10, days_avail, updated"
        sql += " FROM blogger_perf"
        where, args = [], []
        if code:
            where.append("stock_code = ?")
            args.append(str(code).strip())
        if blogger:
            where.append("blogger = ?")
            args.append(blogger)
        sql += (" WHERE " + " AND ".join(where)) if where else ""
        sql += " ORDER BY rec_date DESC"
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(compute_all(force=True), ensure_ascii=False))
    for r in get()[:8]:
        print(r)
