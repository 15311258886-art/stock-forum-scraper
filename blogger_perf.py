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

import datetime
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
  series_json TEXT,
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
        # 幂等迁移：老库的 blogger_perf 没有 series_json 列（2026-10-10 补，荐股后逐日走势）
        cols = [r[1] for r in con.execute("PRAGMA table_info(blogger_perf)").fetchall()]
        if "series_json" not in cols:
            con.execute("ALTER TABLE blogger_perf ADD COLUMN series_json TEXT")
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
    # 2026-10-10：荐股后逐日走势（最多 10 个交易日），chg = 相对荐股日收盘的累计 %，
    # 供前端画「荐股后走势」小曲线；没有后续行情时存空串（不是假数据）。
    series = [{"d": d, "c": round(c, 3), "chg": round((c / base_c - 1.0) * 100.0, 2)}
              for d, c in nxt[:10]]
    return (blogger, rec_date, code, round(base_c, 3),
            out["r1"], out["r3"], out["r5"], out["r10"],
            out["days_avail"], time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(series, ensure_ascii=False) if series else "")


def compute_all(force: bool = False, limit: int = 0) -> dict:
    """全量算一遍并 UPSERT。首次需抓约 200+ 次日 K，8 并发约数秒；当天第二次直接读库秒回。

    2026-10-10 修「8 月荐股的 3/5/10 日永远是 —」：
      旧逻辑只补「blogger_perf 里完全没有的行」——一行算过一次就冻死，哪怕当时
      T+3/T+5/T+10 窗口还没走完、事后也永不重算（与 extref「na 是终态」同类病）。
      现在把「有行但任一窗口为 NULL」的行也排进队列重算；行情取不到时 _rows_for
      返回 None、该行不动，下次再试（失败不会被误标成已处理）。"""
    t0 = time.time()
    ensure_table()
    con = _conn()
    try:
        # 2026-10-15（性能修复 · 「API 随机卡十几秒 / 页面偶发打不开」的真根因）：
        # 旧条件「任一窗口为 NULL 就重算」会让**数据源永远取不到的行情**对应的行**每轮都进队列**
        # —— 实测固定 252 行、每轮 12~38 秒，而 compute_all 要抓 252 只票的 K 线（8 线程网络）
        # 且在 Python 侧解析，**持有 GIL 期间整个服务所有 API 线程都被卡住**
        # （静态资源走 C 层不受影响，所以症状是「接口随机慢十几秒、页面偶发转圈」而非整体崩）。
        # 现改为：只有当「该窗口理论上还没走完」时才重算；已过窗口期限仍未补齐的行
        # 视为数据源不可得，不再每轮空转（仍可用 force=1 全量重算，或调大 _WIN_DAYS 重试）。
        _WIN_DAYS = {"r1": 4, "r3": 8, "r5": 12, "r10": 20}   # 自然日近似（交易日 ×1.5 + 缓冲）
        _today = datetime.date.today()

        def _should_retry(rec_date, p) -> bool:
            for k, need in _WIN_DAYS.items():
                if p[k] is None:
                    try:
                        d = datetime.date.fromisoformat(str(rec_date)[:10])
                    except Exception:  # noqa: BLE001
                        return True                 # 日期异常 → 保守重算
                    if (_today - d).days <= need:
                        return True                 # 窗口还没走完 → 值得再等
            return False

        job = []
        for r in con.execute(
                "SELECT blogger, rec_date, stock_code FROM blogger_reco"
                + (" LIMIT %d" % limit if limit else "")):
            if force:
                job.append((r["blogger"], r["rec_date"], r["stock_code"]))
                continue
            p = con.execute(
                "SELECT r1, r3, r5, r10 FROM blogger_perf WHERE blogger=? AND rec_date=? AND stock_code=?",
                (r["blogger"], r["rec_date"], r["stock_code"])).fetchone()
            # 没有 perf 行 → 补算；有行但「窗口未走完却仍为 NULL」→ 重算（数据源当时取不到）
            if p is None or _should_retry(r["rec_date"], p):
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
                        " r1, r3, r5, r10, days_avail, updated, series_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(blogger, rec_date, stock_code) DO UPDATE SET"
                        " base_close=excluded.base_close, r1=excluded.r1, r3=excluded.r3,"
                        " r5=excluded.r5, r10=excluded.r10, days_avail=excluded.days_avail,"
                        " updated=excluded.updated, series_json=excluded.series_json", res)
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
        sql = ("SELECT blogger, rec_date, stock_code, base_close, r1, r3, r5, r10,"
               " days_avail, updated, series_json FROM blogger_perf")
        where, args = [], []
        if code:
            where.append("stock_code = ?")
            args.append(str(code).strip())
        if blogger:
            where.append("blogger = ?")
            args.append(blogger)
        sql += (" WHERE " + " AND ".join(where)) if where else ""
        sql += " ORDER BY rec_date DESC"
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        # 2026-10-10 王总要求：卡片要能直接看到「现价 vs 荐股日收盘」的至今涨跌。
        # series_json 末点即最新已结算交易日 → 派生 last_date / last_close / last_chg，免得前端各自解析。
        for r in rows:
            r["last_date"] = r["last_close"] = r["last_chg"] = None
            sj = r.get("series_json")
            if sj:
                try:
                    arr = json.loads(sj)
                    if isinstance(arr, list) and arr:
                        last = arr[-1] or {}
                        r["last_date"] = last.get("d")
                        r["last_close"] = last.get("c")
                        r["last_chg"] = last.get("chg")
                except Exception:
                    pass
        return rows
    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(compute_all(force=True), ensure_ascii=False))
    for r in get()[:8]:
        print(r)
