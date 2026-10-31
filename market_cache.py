# -*- coding: utf-8 -*-
"""行情预存层（P2，2026-10-22）。

在 app.db 中新增两张表：
  · market_quotes(symbol PK, name, price, pct, open, high, low, prev_close, pe, pb, updated_at, src)
  · market_kline(symbol, date, open, high, low, close, volume)  —— PK(symbol,date)
由 market_prefetch 定时批量落库；网页首屏 / 个股详情改为读这两张表（本地、毫秒级），
仅在缺失 / 过期时回退现抓，彻底消除「每次打开网页都现抓外部行情」导致的卡顿。

零第三方依赖：复用项目既有的 emapi（竞速取数基座）与 urllib。
"""
from __future__ import annotations
import json
import logging
import sqlite3
import urllib.request
from datetime import datetime

import emapi

logger = logging.getLogger("market_cache")

CACHE_TTL_SECONDS = 300  # 预存行情「新鲜」时长；过期则回退现抓（仍走缓存优先）

# 直连优先（忽略环境代理；避免失效 http(s)_proxy 触发 ProxyError）
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "—", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_quotes (
            symbol      TEXT PRIMARY KEY,
            name        TEXT,
            price       REAL, pct REAL, open REAL, high REAL, low REAL, prev_close REAL,
            pe          REAL, pb   REAL,
            updated_at  TEXT,
            src         TEXT
        )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_kline (
            symbol TEXT,
            date   TEXT,
            open   REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, date)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mk_symbol ON market_kline(symbol)")
    conn.commit()


def get_universe(conn: sqlite3.Connection, cap: int = 250) -> list:
    """取「值得预存」的热池：自选 + 近期龙虎榜 + 论坛高提及 Top。"""
    cur = conn.cursor()
    score: dict = {}
    try:
        for (s,) in cur.execute("SELECT DISTINCT code FROM self_pick").fetchall():
            s = (s or "").strip()
            if s:
                score[s] = score.get(s, 0) + 5
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = cur.execute(
            "SELECT DISTINCT security_code FROM lhb_stocks "
            "WHERE trade_date >= (SELECT MAX(trade_date) FROM lhb_stocks)").fetchall()
        for (s,) in rows:
            s = (s or "").strip()
            if s:
                score[s] = score.get(s, 0) + 3
    except Exception:  # noqa: BLE001
        pass
    try:
        for s, c in cur.execute(
                "SELECT symbol, COUNT(*) c FROM stock_mentions "
                "GROUP BY symbol ORDER BY c DESC LIMIT 200").fetchall():
            s = (s or "").strip()
            if s:
                score[s] = score.get(s, 0) + min(c, 50)
    except Exception:  # noqa: BLE001
        pass
    ranked = sorted(score.items(), key=lambda x: -x[1])
    return [s for s, _ in ranked[:cap]]


def _fetch_quote(symbol: str):
    def tx():
        return emapi.tencent_quote(symbol)

    def em():
        return emapi.quote(symbol)

    atom = emapi.race_first([tx, em], timeout=3.0)
    if not atom:
        return None
    return dict(name=atom.get("name"), price=atom.get("price"), pct=atom.get("pct"),
                open=atom.get("open"), high=atom.get("high"), low=atom.get("low"),
                prev_close=atom.get("prev_close"), pe=atom.get("pe"), pb=atom.get("pb"))


def _tencent_kline_rows(code, market, period="day", days=120, timeout=12):
    prefix = {"1": "sh", "0": "sz", "105": "us", "116": "hk"}.get(market, "")
    if not prefix:
        return []
    sym = prefix + code
    key = {"day": "qfqday", "week": "qfqweek", "month": "qfqmonth"}.get(period, "qfqday")
    cnt = max(20, min(days, 120)) if period == "day" else 160
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},{period},,,{cnt},qfq")
    headers = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}
    raw = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with _DIRECT.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            break
        except Exception:  # noqa: BLE001
            raw = None
    if not raw:
        return []
    try:
        j = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    d = ((j or {}).get("data") or {}).get(sym) or {}
    rows = list(d.get(key) or d.get(period) or [])
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        close = _num(r[2])
        out.append({"date": str(r[0])[:10], "open": _num(r[1]), "close": close,
                    "high": _num(r[3]), "low": _num(r[4]),
                    "volume": _num(r[5]) if len(r) > 5 else None})
    out.sort(key=lambda x: x["date"])
    for i, r in enumerate(out):
        base = out[i - 1]["close"] if i > 0 else r["open"]
        r["pct"] = round((r["close"] - base) / base * 100, 2) if base else 0.0
    return out


def _fetch_kline(symbol, days=120):
    try:
        m, code, _ = emapi.secid_parts(symbol)
    except Exception:  # noqa: BLE001
        return []
    return _tencent_kline_rows(code, m, "day", days)


def prefetch_quotes(conn, symbols=None, cap=250):
    if symbols is None:
        symbols = get_universe(conn, cap)
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok = fail = 0
    for s in symbols:
        try:
            q = _fetch_quote(s)
        except Exception:  # noqa: BLE001
            q = None
        if not q:
            fail += 1
            continue
        cur.execute(
            """INSERT INTO market_quotes
               (symbol,name,price,pct,open,high,low,prev_close,pe,pb,updated_at,src)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name=excluded.name, price=excluded.price, pct=excluded.pct,
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 prev_close=excluded.prev_close, pe=excluded.pe, pb=excluded.pb,
                 updated_at=excluded.updated_at, src=excluded.src""",
            (s, q["name"], q["price"], q["pct"], q["open"], q["high"], q["low"],
             q["prev_close"], q["pe"], q["pb"], now, "tencent"))
        ok += 1
    conn.commit()
    return dict(total=len(symbols), ok=ok, fail=fail, updated_at=now)


def prefetch_klines(conn, symbols=None, days=120, cap=250):
    if symbols is None:
        symbols = get_universe(conn, cap)
    cur = conn.cursor()
    ok = fail = 0
    for s in symbols:
        try:
            rows = _fetch_kline(s, days)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            fail += 1
            continue
        cur.execute("DELETE FROM market_kline WHERE symbol=?", (s,))
        for r in rows:
            cur.execute(
                "INSERT OR REPLACE INTO market_kline"
                "(symbol,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                (s, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]))
        ok += 1
    conn.commit()
    return dict(total=len(symbols), ok=ok, fail=fail, days=days)


def prefetch_all(conn, cap=250, kline_days=120):
    q = prefetch_quotes(conn, cap=cap)
    k = prefetch_klines(conn, cap=cap, days=kline_days)
    return dict(quotes=q, klines=k, at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def read_quote(conn, symbol, ttl=CACHE_TTL_SECONDS, force=False):
    cur = conn.cursor()
    row = cur.execute(
        "SELECT symbol,name,price,pct,open,high,low,prev_close,pe,pb,updated_at,src "
        "FROM market_quotes WHERE symbol=?", (symbol,)).fetchone()
    fresh = False
    if row:
        ua = row[10]
        try:
            dt = datetime.strptime(ua, "%Y-%m-%d %H:%M:%S")
            fresh = (datetime.now() - dt).total_seconds() < ttl
        except Exception:  # noqa: BLE001
            fresh = False
    if row and fresh and not force:
        return dict(cached=True, fresh=True, symbol=row[0], name=row[1], price=row[2],
                    pct=row[3], open=row[4], high=row[5], low=row[6], prev_close=row[7],
                    pe=row[8], pb=row[9], updated_at=row[10], src=row[11])
    q = _fetch_quote(symbol)
    if q:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            """INSERT INTO market_quotes
               (symbol,name,price,pct,open,high,low,prev_close,pe,pb,updated_at,src)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name=excluded.name, price=excluded.price, pct=excluded.pct,
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 prev_close=excluded.prev_close, pe=excluded.pe, pb=excluded.pb,
                 updated_at=excluded.updated_at, src=excluded.src""",
            (symbol, q["name"], q["price"], q["pct"], q["open"], q["high"], q["low"],
             q["prev_close"], q["pe"], q["pb"], now, "live"))
        conn.commit()
        q.update(cached=True, fresh=True, symbol=symbol, updated_at=now, src="live")
        return q
    if row:
        return dict(cached=True, fresh=False, stale=True, symbol=row[0], name=row[1],
                    price=row[2], pct=row[3], open=row[4], high=row[5], low=row[6],
                    prev_close=row[7], pe=row[8], pb=row[9], updated_at=row[10], src=row[11])
    return dict(cached=False, ok=False, symbol=symbol, reason="无缓存且现抓失败")


def read_kline(conn, symbol, limit=120):
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT date,open,high,low,close,volume FROM market_kline "
        "WHERE symbol=? ORDER BY date DESC LIMIT ?", (symbol, limit)).fetchall()
    if rows and len(rows) >= 2:
        kl = [dict(date=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
              for r in rows][::-1]
        return dict(cached=True, symbol=symbol, count=len(kl), klines=kl)
    k = _fetch_kline(symbol, limit)
    if k:
        cur.execute("DELETE FROM market_kline WHERE symbol=?", (symbol,))
        for r in k:
            cur.execute(
                "INSERT OR REPLACE INTO market_kline"
                "(symbol,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                (symbol, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]))
        conn.commit()
        return dict(cached=True, symbol=symbol, count=len(k), klines=k, from_live=True)
    return dict(cached=False, ok=False, symbol=symbol, reason="无缓存且现抓失败")


def cache_status(conn):
    cur = conn.cursor()
    qn = cur.execute("SELECT COUNT(*) FROM market_quotes").fetchone()[0]
    kn = cur.execute("SELECT COUNT(*) FROM market_kline").fetchone()[0]
    latest = cur.execute("SELECT MAX(updated_at) FROM market_quotes").fetchone()[0]
    return dict(ok=True, quotes_rows=qn, kline_symbols=kn, last_update=latest or None,
                ttl_seconds=CACHE_TTL_SECONDS)

def list_symbols(conn):
    """缓存热池符号列表（供前端下拉；零外部依赖）。"""
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT symbol,name,price,pct,updated_at FROM market_quotes "
        "ORDER BY symbol").fetchall()
    return [dict(symbol=r[0], name=r[1], price=r[2], pct=r[3], updated_at=r[4])
            for r in rows]


__all__ = ["ensure_schema", "get_universe", "prefetch_quotes", "prefetch_klines",
           "prefetch_all", "read_quote", "read_kline", "cache_status", "list_symbols", "CACHE_TTL_SECONDS"]
