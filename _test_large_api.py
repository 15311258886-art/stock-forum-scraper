"""建议8 / P3 自测：大接口分页 / 降采样 / 缓存。

验证四件事：
   1) K 线默认只回最近 250 根，earliest_date 仍保留「全历史首日=上市日」，
      first_date 才是本次切片首日；limit=0 才拉全历史
   2) persons 由「每人一条 DISTINCT」改成一次 GROUP BY，并支持 limit/offset 分页
   3) screener_decline 按参数键内存缓存 1 小时，命中标 cached=True，force=1 可绕过
   4) 路由参数：kline 默认 limit=250、persons 默认全量、decline 支持 force

跑法：.venv\\Scripts\\python.exe _test_large_api.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ROOT / "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from viewer import server as SVR  # noqa: E402

ok = True
FAILS: list[str] = []


def chk(cond: bool, msg: str) -> None:
    global ok
    if cond:
        print(f"  PASS  {msg}")
    else:
        ok = False
        FAILS.append(msg)
        print(f"  FAIL  {msg}")


def mkpayload(n: int = 800, start: int = 20050104) -> dict:
    rows = [{"date": f"{start + i:08d}", "open": 10.0 + i * 0.01, "close": 10.1 + i * 0.01,
             "high": 10.2, "low": 9.9, "volume": 1000.0 + i, "amount": 1e6, "pct": 0.5}
            for i in range(n)]
    return {"symbol": "sh600519", "code": "600519", "name": "TEST", "period": "day",
            "market": "1", "count": n, "klines": rows, "adjust": "qfq",
            "source": "TEST", "current": {"price": 10.0, "pct": 0.5}}


class CountingConn:
    """转发 sqlite 连接并统计 execute 次数（用来证明 N+1 → 1）。"""

    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self.calls: list[str] = []

    def execute(self, sql, *a):
        self.calls.append(re.sub(r"\s+", " ", str(sql)).strip())
        return self._real.execute(sql, *a)

    def executescript(self, sql):
        return self._real.executescript(sql)

    def commit(self):
        return self._real.commit()


class FakeDB:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = CountingConn(conn)


# ---------------------------------------------------------------- 1) _slice_kline
print("\n[1] _slice_kline 降采样边界")
P = mkpayload(800)
N = len(P["klines"])
FULL_FIRST = P["klines"][0]["date"]

s1 = SVR._slice_kline(mkpayload(800), 250, 0)
chk(s1["count"] == 250, "limit=250 → count=250")
chk(s1["total"] == 800, "limit=250 → total 仍是全量 800")
chk(s1["has_more"] is True, "limit=250 → has_more=True（前端所以给「拉全部历史」入口）")
chk(s1["earliest_date"] == FULL_FIRST, "earliest_date = 全历史首日（=上市日，不能被切片吃掉）")
chk(s1["first_date"] == P["klines"][N - 250]["date"],
    f"offset=0 → 取的是最近 250 根（首日 = 第 {N-250} 根，不是最早那 250 根）")
chk(s1["last_date"] == P["klines"][N - 1]["date"], "limit=250 → 末根 = 全历史最后一根（最近一根）")
chk(s1["klines"][-1]["date"] == P["klines"][-1]["date"], "limit=250 → 保留最近一根")

s2 = SVR._slice_kline(mkpayload(800), 0, 0)
chk(s2["count"] == 800 and s2["total"] == 800, "limit=0 → count=total=全量")
chk(s2["has_more"] is False, "limit=0 → has_more=False")
chk(len(s2["klines"]) == 800, "limit=0 → klines 是完整 800 根")

s3 = SVR._slice_kline(mkpayload(800), 250, 250)
chk(s3["offset"] == 250 and s3["count"] == 250, "offset=250 → offset/count 正确")
chk(s3["klines"][0]["date"] == P["klines"][300]["date"],
    "offset=250 → 往上翻一页拿到「最近 250 根之前的 250 根」")
chk(s3["klines"][-1]["date"] == P["klines"][549]["date"],
    "offset=250 → 末根紧接最近 250 根的首根（两段不重叠不留缝）")
chk(s3["earliest_date"] == FULL_FIRST, "翻页后 earliest_date 依旧是全历史首日")
chk(s3["first_date"] == P["klines"][300]["date"], "翻页后 first_date 才是本次切片首日（翻页段首根）")
chk(s3["earliest_date"] != s3["first_date"], "关键：earliest_date 与 first_date 必须能区分（上市日≠切片首日）")

s4 = SVR._slice_kline(mkpayload(800), 250, 99999)
chk(s4["count"] >= 0 and s4["klines"] is not None, "offset 越界（99999）→ clamp，不崩不抛")
chk(s4["offset"] <= 799, "offset 越界 → 被 clamp 在合法范围内")

s5 = SVR._slice_kline(mkpayload(0), 250, 0)
chk(s5["klines"] == [], "空 klines → klines=[]（不抛异常）")
chk(s5["count"] == 0 and s5["total"] == 0, "空 klines → count/total 都是 0")
chk(s5["earliest_date"] == "" and s5["first_date"] == "" and s5["last_date"] == "",
    "空 klines → 日期键补齐为空串（前端读 undefined 会渲染成 NaN）")

s6 = SVR._slice_kline(mkpayload(100), 250, 0)
chk(s6["count"] == 100 and len(s6["klines"]) == 100, "全历史不足 250 根 → 原样返回（不截断）")
chk(s6["has_more"] is False, "全历史不足 250 根 → has_more=False")


# ---------------------------------------------------------------- 2) api_kline
print("\n[2] api_kline 默认 250 + 缓存复用")
_em_calls = {"n": 0}
_real_em = SVR._eastmoney_kline


def _fake_em(symbol, period):
    _em_calls["n"] += 1
    return mkpayload(800)


SVR._eastmoney_kline = _fake_em
SVR._sina_kline_result = lambda *a, **k: None
SVR._tencent_kline_result = lambda *a, **k: None
SVR._KLINE_CACHE.clear()

try:
    r1 = SVR.api_kline("sh600519", "day")
    chk(r1["count"] == 250 and r1["total"] == 800, "api_kline 默认 → 250 根 / total 800")
    chk(r1["has_more"] is True, "api_kline 默认 → has_more=True")
    chk(r1["earliest_date"] == FULL_FIRST, "api_kline 默认 → earliest_date 保留上市日")
    chk(_em_calls["n"] == 1, "首次调用抓了 1 次行情源")

    r2 = SVR.api_kline("sh600519", "day")
    chk(_em_calls["n"] == 1, "缓存命中（90s 内）→ 不再打行情源")
    chk(r2["count"] == 250, "缓存命中后切片结果一致（仍 250 根）")

    r3 = SVR.api_kline("sh600519", "day", limit=0)
    chk(r3["count"] == 800, "api_kline(limit=0) → 拉全历史 800 根")
    chk(r3["has_more"] is False, "api_kline(limit=0) → has_more=False")
    chk(_em_calls["n"] == 1, "limit=0 也是复用同一份缓存（切片现算，不再抓源）")

    r4 = SVR.api_kline("sh600519", "day", offset=250)
    chk(r4["offset"] == 250 and r4["klines"][0]["date"] == P["klines"][300]["date"],
        "api_kline(offset=250) → 向上翻页生效")
    chk(r4["klines"][-1]["date"] == P["klines"][549]["date"],
        "api_kline(offset=250) → 翻页段紧接最近 250 根（不重叠、不留缝）")
    chk(r4["earliest_date"] == FULL_FIRST, "api_kline 翻页后 earliest_date 仍是全历史首日")

    r5 = SVR.api_kline("", "day")
    chk("error" in r5, "api_kline('') → 返回 error（空 symbol 不静默）")
finally:
    SVR._eastmoney_kline = _real_em
    SVR._KLINE_CACHE.clear()


# ---------------------------------------------------------------- 3) persons
print("\n[3] persons：GROUP BY 聚合 + 分页")
PERSON_COLS = list(getattr(SVR, "_PERSON_SCORE_COLUMNS", []))
fake = sqlite3.connect(":memory:")
fake.row_factory = sqlite3.Row   # 与真实 DB.conn 一致（db.py:51）
fake.execute("CREATE TABLE persons (name TEXT, mention_count INTEGER, role TEXT)")
fake.executemany("INSERT INTO persons VALUES (?,?,?)", [
    ("张三", 99, "游资"), ("李四", 80, "游资"), ("王五", 70, "机构"),
    ("赵六", 60, "游资"), ("孙七", 50, "机构"),
])
fake.execute("CREATE TABLE players (name TEXT, site TEXT, platform TEXT, tier TEXT, channel TEXT)")
fake.execute("INSERT INTO players VALUES ('张三','taoguba','taoguba','A','chat')")
fake.execute("CREATE TABLE posts (author TEXT, site TEXT)")
fake.executemany("INSERT INTO posts VALUES (?,?)", [
    ("张三", "taoguba"), ("张三", "eastmoney"), ("张三", "xueqiu"),
    ("李四", "taoguba"), ("王五", "eastmoney"),
])
if PERSON_COLS:
    def _coltype(c):
        return "TEXT" if c in ("score_detail_json", "public_record_json") else "REAL"
    cols = ", ".join(f"{c} {_coltype(c)}" for c in PERSON_COLS)
    fake.execute(f"CREATE TABLE person_scores ({cols})")
    fake.execute("INSERT INTO person_scores (%s) VALUES (%s)" % (
        ", ".join(PERSON_COLS), ", ".join(["0"] * len(PERSON_COLS))))
REAL_DB = SVR.DB
try:
    SVR.DB = FakeDB(fake)
    smap = SVR._person_site_map()
    chk(sorted(smap.get("张三") or []) == ["eastmoney", "taoguba", "xueqiu"],
        "_person_site_map 一次聚合出多站点作者（三站同名只进一次 map）")
    chk(smap.get("李四") == ["taoguba"] and smap.get("王五") == ["eastmoney"],
        "_person_site_map 单站点作者映射正确")
    chk("赵六" not in smap, "_person_site_map 只收有效 author/site（未发帖的人不进 map）")
    chk(fake and isinstance(SVR.DB.conn, CountingConn), "_person_site_map 走的是统一 conn 出口")

    ncalls = len(SVR.DB.conn.calls)
    chk(ncalls == 1, f"_person_site_map 只发 1 条 SQL（N+1 → 1），实际 {ncalls} 条")

    allp = SVR.api_persons()
    chk(len(allp) == 5, "api_persons(limit=0) → 全量 5 人（不传 limit 与从前一致）")
    chk(allp[0]["name"] == "张三"
        and sorted(allp[0]["sites"] or []) == ["eastmoney", "taoguba", "xueqiu"],
        "api_persons items.sites 由聚合结果带出（前端不用再查一次）")

    page = SVR.api_persons(limit=2, offset=2)
    chk(len(page) == 2, "api_persons(limit=2) → 一页 2 条")
    chk([p["name"] for p in page] == ["王五", "赵六"], "api_persons(offset=2) → 从第 3 人开始（分页方向正确）")

    rest = SVR.api_persons(limit=2, offset=4)
    chk([p["name"] for p in rest] == ["孙七"], "api_persons 末页不补空、不多给")
finally:
    SVR.DB = REAL_DB
    fake.close()


# ---------------------------------------------------------------- 4) decline 缓存
print("\n[4] screener_decline 内存缓存 1 小时")
_real_pool = SVR._decline_pool
_scan_calls = {"n": 0}


def _fake_pool():
    return {"600519": "贵州茅台", "000001": "平安银行"}


def _fake_scan(pool, days=30, down=25, limit=80, sort="down"):
    _scan_calls["n"] += 1
    return {"items": [{"code": "600519", "name": "贵州茅台"}], "days": days, "down": down}


SVR._decline_pool = _fake_pool
_real_scan = SVR.screener.decline_scan
SVR.screener.decline_scan = _fake_scan
SVR._DECLINE_CACHE.clear()
try:
    d1 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="down")
    chk(_scan_calls["n"] == 1, "首次扫描执行 1 次")
    chk(d1.get("cached") is not True, "首次结果不标 cached=True")

    d2 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="down")
    chk(_scan_calls["n"] == 1, "同参数二次调用 → 命中缓存，不再重扫")
    chk(d2.get("cached") is True, "命中缓存的结果标 cached=True")

    d3 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="score")
    chk(_scan_calls["n"] == 2, "换 sort → 缓存键不同，重新扫描")
    chk(d3.get("cached") is not True, "换参数不误命中旧缓存")

    d4 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="down", force=1)
    chk(_scan_calls["n"] == 3, "force=1 → 强制重算（绕过缓存）")
    chk(d4.get("cached") is not True, "force 这次是真的新算的，不谎报 cached=True")

    d5 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="down")
    chk(_scan_calls["n"] == 3, "force 的结果写回了缓存 → 紧接一次普通调用直接命中")
    chk(d5.get("cached") is True, "force 之后普通调用命中缓存并返回 cached=True")

    # TTL 边界：把缓存时间戳往回拨到刚好过期
    k = (30, 25, 80, "down")
    if k in SVR._DECLINE_CACHE:
        at, payload = SVR._DECLINE_CACHE[k]
        SVR._DECLINE_CACHE[k] = (at - SVR._DECLINE_TTL - 1, payload)
    d6 = SVR.api_screener_decline(days=30, down=25, limit=80, sort="down")
    chk(_scan_calls["n"] == 4, "超过 TTL（1 小时）→ 自动过期重算")
finally:
    SVR._decline_pool = _real_pool
    SVR.screener.decline_scan = _real_scan
    SVR._DECLINE_CACHE.clear()


# ---------------------------------------------------------------- 5) 路由 / 前端静态
print("\n[5] 路由与前端口径")
src = Path(SVR.__file__).read_text(encoding="utf-8", errors="replace")
m_kline = re.search(r'if route == "/api/kline":(.*?)(?=\n        if route ==)', src, re.S)
m_persons = re.search(r'if route == "/api/persons":(.*?)(?=\n        if route ==)', src, re.S)
m_decline = re.search(r'if route == "/api/screener_decline":(.*?)(?=\n        if route ==)', src, re.S)
chk(bool(m_kline), "定位到 /api/kline 路由")
chk(bool(m_kline) and 'self._int_arg(q, "limit", 250' in m_kline.group(1),
    "/api/kline 默认 limit=250")
chk(bool(m_kline) and 'self._int_arg(q, "offset"' in m_kline.group(1),
    "/api/kline 支持 offset 翻页")
chk(bool(m_persons) and 'self._int_arg(q, "limit", 0' in m_persons.group(1),
    "/api/persons 默认 limit=0（全量，行为与从前一致）")
chk(bool(m_persons) and 'self._int_arg(q, "offset"' in m_persons.group(1),
    "/api/persons 支持 offset")
chk(bool(m_decline) and "force=" in m_decline.group(1), "/api/screener_decline 支持 force 绕过缓存")

js = (ROOT / "viewer" / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
chk("KLINE_DEFAULT_LIMIT = 250" in js or "KLINE_DEFAULT_LIMIT=250" in js,
    "前端 KLINE_DEFAULT_LIMIT=250（默认值与后端对齐）")
chk("klineLoadAll" in js or "klineLoadAll" in js, "前端有「拉全部历史」入口")
chk("limit: 0" in js, "前端「拉全部历史」传 limit=0")
chk("hasMore" in js and "earliest" in js, "前端消费 has_more / earliest_date")

print("=" * 60)
print("ALL_PASS" if ok else "HAS_FAILURE:" + ("; ".join(FAILS)))
sys.exit(0 if ok else 1)
