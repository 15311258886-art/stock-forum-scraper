# -*- coding: utf-8 -*-
"""`viewer/fastfetch.py` + `tools/fast_grab.py` 的回归测试（2026-11-04 新增）。

## 这份文件为什么存在

2026-11-03 交付的 1287 行新代码在 `tests/` 下**零覆盖**。结果 2026-11-04 QA 一跑就
抓到 P1：`write_snapshot` 那句「源行情日 ≠ 目标日 → 拒绝落库」是**死代码** ——
`fastfetch` 解析出来写的是 `session`，生产代码 `_session_mismatch()` 读的是 `_session`，
于是守卫恒返回 `{}`、被当成「全部一致」，把 11-03 的价格写成了 40 行 11-04 的快照
（而且 `INSERT OR REPLACE` 会覆盖目标日原本正确的行）。

这个 bug 只要有**一条**「让 fastfetch 的输出喂给真守卫」的用例就能当场抓到。
所以下面每条用例都对着一个真实发生过（或差一点发生）的事故，不是为了凑覆盖率。

## 为什么全部离线

盘后包与腾讯报价都是外部依赖。这里统一用**构造的字节**：`.cod`/`.md1` 的偏移量
（150 / 512 字节，offset 4/12/20/28/36 双精度、56 int32 股、72 双精度元）是从
2026-11-02 的真包反推出来的，构造出来的字节与真包同构，所以解析路径被测到的是
**同一段代码**，只是数据换成了已知值（比拿真包做 golden 更好定位：错了能指出第几个字段）。

跑法：

    .venv\\Scripts\\python.exe -m pytest tests/test_fast_grab.py -q
"""
from __future__ import annotations

import io
import os
import sqlite3
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools"), str(ROOT / "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_kline_db as BKD          # noqa: E402
import fetch_a_shares_tencent as FT   # noqa: E402
import fast_grab as FG                # noqa: E402
from viewer import fastfetch as FF    # noqa: E402


# ---------------------------------------------------------------- 构造盘后包字节

_COD_REC = 150
_MD1_REC = 512


def _cod_entry(code: str, name: str = "") -> bytes:
    """.cod 一条 = 150 字节，前 6 字节代码 ASCII，offset 40 起 GBK 名称。"""
    b = bytearray(b"\x00" * _COD_REC)
    b[0:6] = code.encode("ascii")
    nm = name.encode("gbk")[:60]
    b[40:40 + len(nm)] = nm
    return bytes(b)


def _md1_entry(rec: dict) -> bytes:
    """.md1 一条 = 512 字节（盘口快照）。字段偏移见 viewer/fastfetch.py 顶部说明。"""
    b = bytearray(b"\x00" * _MD1_REC)
    struct.pack_into("<5d", b, 4,
                     float(rec["prev"]), float(rec["open"]), float(rec["high"]),
                     float(rec["low"]), float(rec["close"]))
    struct.pack_into("<i", b, 56, int(rec["volume"]))
    struct.pack_into("<d", b, 72, float(rec["amount"]))
    return bytes(b)


def _market_blob(recs: list):
    """一个市场 = .cod（代码表）+ .md1（行情表），同序等长。"""
    cod = b"".join(_cod_entry(r["code"], r.get("name", "")) for r in recs)
    md1 = b"".join(_md1_entry(r) for r in recs)
    return cod, md1


def _zip_bytes(date8: str, markets: dict) -> bytes:
    """markets: {"sh": [rec, ...], "sz": [...], "bj": [...]} → 与真包同构的 zip 字节。"""
    ymd = date8[2:]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for mkt, recs in markets.items():
            cod, md1 = _market_blob(recs)
            zf.writestr("%s%s.cod" % (mkt, ymd), cod)
            zf.writestr("%s%s.md1" % (mkt, ymd), md1)
    return buf.getvalue()


def _rec(code, close, prev=None, open_=None, high=None, low=None, volume=1000, amount=1.0e7):
    return {"code": code, "close": close, "prev": close if prev is None else prev,
            "open": close if open_ is None else open_,
            "high": close if high is None else high,
            "low": close if low is None else low,
            "volume": volume, "amount": amount}


@pytest.fixture()
def small_thresholds(monkeypatch):
    """把「全市场规模下限」调到 1 只。

    真阈值是 sh 10000 / sz 3000 / bj 50 条 `.cod`（有价记录 3000/3000/50），
    造一份 13550 条的市场切片要 7MB，纯属浪费 —— 阈值这条防线本身用单独用例测
    （`test_package_rejects_thin_market`），这里只测**解析值对不对**。
    """
    one = {"sh": 1, "sz": 1, "bj": 1}
    monkeypatch.setattr(FF, "TDX_MARKET_MIN_ROWS", dict(one))
    monkeypatch.setattr(FF, "TDX_MARKET_MIN_PRICED", dict(one))


# ---------------------------------------------------------------- 1. 包解析

def test_package_parses_exact_fields(small_thresholds):
    """`.cod`/`.md1` 解析出的每个字段必须与构造值逐位相同。

    这条守的是「偏移量写错一位也不报错」：价格少读 8 字节会读到下一个 double，
    成交量按 int32 读成 double 会解出天文数字，两者都不会抛异常，只会静默错。
    """
    markets = {
        "sh": [_rec("600519", 1410.0, prev=1400.0, open_=1405.0, high=1420.0, low=1395.0,
                    volume=7412056, amount=1.045e10),
               _rec("688981", 100.5, prev=99.0, open_=99.5, high=101.0, low=99.0,
                    volume=34214339, amount=3.4e9)],
        "sz": [_rec("000001", 11.4, prev=11.0, open_=11.1, high=11.5, low=10.9,
                    volume=213967422, amount=2.4e9)],
        "bj": [_rec("920001", 5.1, prev=5.0, open_=5.05, high=5.2, low=4.9,
                    volume=1258857, amount=6.4e6)],
    }
    blob = _zip_bytes("20261102", markets)
    rows = FF.tdx_daily_package("20261102", blob=blob)
    got = {r["code"]: r for r in rows}

    assert set(got) == {"600519", "688981", "000001", "920001"}
    r = got["600519"]
    assert r["close"] == pytest.approx(1410.0)
    assert r["prev_close"] == pytest.approx(1400.0)
    assert r["open"] == pytest.approx(1405.0)
    assert r["high"] == pytest.approx(1420.0)
    assert r["low"] == pytest.approx(1395.0)
    # volume 单位是「股」、amount 是「元」—— 这两个单位是本模块最贵的一条结论，
    # 一旦有人「顺手 ×100」，600519 的成交量会凭空多两个数量级。
    assert r["volume"] == pytest.approx(7412056.0)
    assert r["amount"] == pytest.approx(1.045e10)

    # trade_date 由调用日注入（包里没有「今天是哪天」），必须整批一致
    assert {x["trade_date"] for x in rows} == {"20261102"}
    # 科创板 688981 同样按「股」解（不是手），这条是 2026-11-03 实测过的口径
    assert got["688981"]["volume"] == pytest.approx(34214339.0)


def test_package_rejects_thin_market(monkeypatch):
    """某市场有价记录低于下限 → RuntimeError，**绝不返回部分市场当全市场**。

    2026-11-03 踩过的坑：把「.cod 条目下限 10000」拿去卡「有价记录数」，
    结果把完全正常的 20261102（沪 5095 条有价）判成故障。所以这两个阈值必须分开，
    这里也分开测：条目够、但有价记录不够（全 0 成交）时同样要拒。
    """
    monkeypatch.setattr(FF, "TDX_MARKET_MIN_ROWS", {"sh": 1, "sz": 1, "bj": 1})
    monkeypatch.setattr(FF, "TDX_MARKET_MIN_PRICED", {"sh": 1, "sz": 1, "bj": 2})
    markets = {
        "sh": [_rec("600519", 1410.0)],
        "sz": [_rec("000001", 11.4)],
        # 北交所 2 条里只有 1 条有价（另一条 volume=0 → 停牌/退市空记录，被解析器丢掉）
        "bj": [_rec("920001", 5.1), _rec("920002", 0.0, volume=0, amount=0.0)],
    }
    with pytest.raises(RuntimeError) as ei:
        FF.tdx_daily_package("20261102", blob=_zip_bytes("20261102", markets))
    assert "有价记录过少" in str(ei.value)


def test_package_rejects_cod_md1_count_mismatch():
    """代码表与行情表条数不一致 → RuntimeError。

    不硬失败就会**按位错位**：把 A 的收盘价写成 B 的，且全程不报错。
    这种数据比缺数据危险得多（缺数据有人会问，错数据没人会问）。
    """
    cod, md1 = _market_blob([_rec("600519", 1410.0), _rec("600000", 10.0)])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sh261102.cod", cod)
        zf.writestr("sh261102.md1", md1[:512])       # 故意只留一条行情
        zf.writestr("sz261102.cod", _market_blob([_rec("000001", 11.4)])[0])
        zf.writestr("sz261102.md1", _market_blob([_rec("000001", 11.4)])[1])
        zf.writestr("bj261102.cod", _market_blob([_rec("920001", 5.1)])[0])
        zf.writestr("bj261102.md1", _market_blob([_rec("920001", 5.1)])[1])
    with pytest.raises(RuntimeError) as ei:
        FF.tdx_daily_package("20261102", blob=buf.getvalue())
    assert "条数不一致" in str(ei.value)


def test_package_dedupes_cross_market_same_code(small_thresholds):
    """沪深都有 000001：只能留「交易所归属与市场一致」的那条。

    实测 20261102 三个市场合计 10,473 条有价记录，不去重时上证指数（3638.03）
    会顶掉平安银行（11.54），对账结果是「93 只价格差 N 倍」+「多出 1242 只库里没有的票」，
    看起来像大面积单位错，实际只是撞号。
    """
    markets = {
        "sh": [_rec("000001", 3638.03), _rec("600519", 1410.0)],   # 沪市 000001 = 上证指数
        "sz": [_rec("000001", 11.4)],                              # 深市 000001 = 平安银行
        "bj": [_rec("920001", 5.1)],
    }
    rows = FF.tdx_daily_package("20261102", blob=_zip_bytes("20261102", markets))
    got = {r["code"]: r for r in rows}
    assert got["000001"]["close"] == pytest.approx(11.4)
    assert got["000001"]["market"] == "sz"
    assert len([r for r in rows if r["code"] == "000001"]) == 1


# ---------------------------------------------------------------- 2. 前复权库

def _init_kline_db(path: str) -> None:
    """建一个与真库同构的空 kline_daily.db（列序 o,c,h,l 是历史口径，别「纠正」）。"""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE kline (code TEXT, d INTEGER, o REAL, c REAL, "
                    "h REAL, l REAL, v REAL, PRIMARY KEY(code, d))")
        con.execute("CREATE INDEX idx_kline_code ON kline(code)")
        con.commit()
    finally:
        con.close()


def test_write_kline_ex_div_refetches_instead_of_appending(tmp_path, monkeypatch):
    """今日除权 → 必须走 `build_kline_db._fetch_one()` 全量重抓，不许直接追加原始价。

    为什么这条最要紧：前复权以最新价为锚，除权日当天**整段历史**都要乘因子重排。
    直接追加原始价，这只票的历史价位会永久错位，而且不报错 —— 一次静默的全历史污染。
    """
    db = str(tmp_path / "kline.db")
    _init_kline_db(db)
    monkeypatch.setattr(FG, "KLINE_DB", db)

    calls = []

    def fake_fetch_one(code):
        calls.append(code)
        # 重抓回来的整段历史：11-02 的收盘价是 99.0（重排后的值），
        # 而包里给的是原始价 10.0 —— 断言最终落库的是 99.0，才能证明走的是重抓。
        return code, [["2026-11-01", 1.0, 1.0, 1.0, 1.0, 5.0],
                      ["2026-11-02", 1.0, 99.0, 1.0, 1.0, 5.0]], [], "ok"

    monkeypatch.setattr(BKD, "_fetch_one", fake_fetch_one)

    pkg = [_rec("300735", 10.0, volume=500)]
    stat = FG.write_kline(pkg, "2026-11-02", {"300735"}, dry_run=False)

    assert calls == ["300735"], "除权票必须整只重抓，而不是只拿包里那一根"
    assert stat["refetch_full"] == 1
    assert stat["refetch_full_done"] == 1
    assert stat["insert"] == 0, "除权票不该走「直接追加」那条路"

    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT o,c,h,l,v FROM kline WHERE code=? AND d=?", ("300735", 20261102)).fetchone()
    finally:
        con.close()
    assert row == (1.0, 99.0, 1.0, 1.0, 5.0), "落库的必须是重抓重排后的价，不是包里的原始价"


def test_write_kline_skips_existing_rows(tmp_path, monkeypatch):
    """幂等：同一天重复跑不许多出行（否则每跑一次库就胖一圈，且无法分辨谁写的）。"""
    db = str(tmp_path / "kline.db")
    _init_kline_db(db)
    monkeypatch.setattr(FG, "KLINE_DB", db)

    pkg = [_rec("600519", 1410.0, volume=7412056)]
    first = FG.write_kline(pkg, "2026-11-02", set(), dry_run=False)
    second = FG.write_kline(pkg, "2026-11-02", set(), dry_run=False)

    assert first["insert"] == 1
    assert second["insert"] == 0
    assert second["skip"].get("already_exists") == 1

    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM kline WHERE code=?", ("600519",)).fetchone()[0]
    finally:
        con.close()
    assert n == 1


# ---------------------------------------------------------------- 3. 后复权库

def _init_kbase_db(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE kline (code TEXT, date TEXT, o REAL, h REAL, l REAL, "
                    "c REAL, v REAL, adj TEXT, PRIMARY KEY(code, date))")
        con.commit()
    finally:
        con.close()


def test_write_kbase_skips_code_without_anchor(tmp_path, monkeypatch):
    """取不到比值（kbase 里没这只票的锚点）→ 跳过并计 `no_anchor`，库里不许出现当日行。

    纪律②：不许拿 1.0 顶上。1.0 等于宣布「这只票今天没除权」，
    而真正的原因是我们**不知道**它有没有 —— 给除权票凭空造一个平台，比缺一根更坏。
    """
    kdb = str(tmp_path / "kline.db")
    bdb = str(tmp_path / "kbase.db")
    _init_kline_db(kdb)
    _init_kbase_db(bdb)

    con = sqlite3.connect(kdb)
    try:
        con.execute("INSERT INTO kline VALUES(?,?,?,?,?,?,?)",
                    ("600519", 20261102, 1400.0, 1405.0, 1410.0, 1395.0, 7412056.0))
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(bdb)
    try:
        # 600519 有锚点：kbase 末日 11-02，收盘 1405.0；kline 同日也是 1405.0 → 比值 1.0
        con.execute("INSERT INTO kline VALUES(?,?,?,?,?,?,?,?)",
                    ("600519", "2026-11-02", 1400.0, 1410.0, 1395.0, 1405.0, 7412056.0, "hfq"))
        con.commit()
    finally:
        con.close()

    monkeypatch.setattr(FG, "KLINE_DB", kdb)
    monkeypatch.setattr(FG, "KBASE_DB", bdb)

    pkg = [_rec("600519", 1410.0, volume=7412056), _rec("000002", 20.0, volume=100)]
    stat = FG.write_kbase(pkg, "2026-11-03", dry_run=False)

    assert stat["insert"] == 1
    assert stat["skip"].get("no_anchor") == 1, "000002 没有锚点，必须计入 no_anchor 而不是硬写"

    con = sqlite3.connect(bdb)
    try:
        assert con.execute("SELECT COUNT(*) FROM kline WHERE code=? AND date=?",
                           ("000002", "2026-11-03")).fetchone()[0] == 0
        c = con.execute("SELECT c FROM kline WHERE code=? AND date=?",
                        ("600519", "2026-11-03")).fetchone()[0]
    finally:
        con.close()
    assert c == pytest.approx(1410.0), "比值 1.0 时 hfq 应等于原始价"


def test_write_kbase_applies_ratio(tmp_path, monkeypatch):
    """有比值时必须乘上去：hfq_today = 原始价 × (hfq_anchor / qfq_anchor)。

    这条守的是「除权换算法」本身。2026-11-03 实测 300735 比值 1.005884，
    不乘就会把 14.51 写成 14.51 而不是 14.5954 —— 少了一个台阶，回测里就是假的跳空。
    """
    kdb = str(tmp_path / "kline.db")
    bdb = str(tmp_path / "kbase.db")
    _init_kline_db(kdb)
    _init_kbase_db(bdb)

    con = sqlite3.connect(kdb)
    try:
        con.execute("INSERT INTO kline VALUES(?,?,?,?,?,?,?)",
                    ("300735", 20261102, 14.40, 14.445, 14.53, 14.30, 1000.0))
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(bdb)
    try:
        con.execute("INSERT INTO kline VALUES(?,?,?,?,?,?,?,?)",
                    ("300735", "2026-11-02", 14.40, 14.53, 14.30, 14.53, 1000.0, "hfq"))
        con.commit()
    finally:
        con.close()

    monkeypatch.setattr(FG, "KLINE_DB", kdb)
    monkeypatch.setattr(FG, "KBASE_DB", bdb)

    stat = FG.write_kbase([_rec("300735", 14.51, volume=1000)], "2026-11-03", dry_run=False)
    assert stat["insert"] == 1
    assert stat["ratio_ne_one"] == 1
    assert stat["ratio_samples"][0][1] == pytest.approx(1.005884, abs=1e-6)

    con = sqlite3.connect(bdb)
    try:
        c = con.execute("SELECT c FROM kline WHERE code=? AND date=?",
                        ("300735", "2026-11-03")).fetchone()[0]
    finally:
        con.close()
    assert c == pytest.approx(14.5954, abs=1e-4)


# ---------------------------------------------------------------- 4. 快照守卫（P1）

def _quote(code: str, *, session=None, name="测试股", price=10.0,
           volume_raw=1000.0, amount=1.0e6) -> dict:
    """模拟 `fastfetch` 解析出来的一行。`session=None` = 上游没给会话日。"""
    r = {
        "code": code, "name": name, "price": price, "prev_close": price,
        "open": price, "high": price, "low": price, "pct": 0.0, "change": 0.0,
        "volume": volume_raw * 100.0, "volume_raw": volume_raw, "volume_unit": "hand",
        "amount": amount, "turnover": None, "pe_ttm": None, "pb": None,
        "mcap_yi": 1.0, "total_mv": 1.0e8, "float_mv": 1.0e8,
        "session": session or "", "trade_date": (session or "")[:8],
    }
    if session:
        # 与 viewer/fastfetch.py 现在的行为一致：`session` 与 `_session` 同值双写
        r["_session"] = session
    return r


def _snapshot_rows(path: str, trade_date: str) -> int:
    """数一数临时 app.db 里目标日写了多少行（表还没建 = 0 行）。"""
    if not os.path.exists(path):
        return 0
    con = sqlite3.connect(path)
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_daily_snapshot'"
        ).fetchone()
        if not has:
            return 0
        return con.execute(
            "SELECT COUNT(*) FROM stock_daily_snapshot WHERE trade_date=?",
            (trade_date,)).fetchone()[0]
    finally:
        con.close()


def test_snapshot_refuses_when_source_date_differs(tmp_path, monkeypatch):
    """**P1 回归用例**：源会话日 11-03、目标日 11-04 → 写 0 行 + 明确报错。

    2026-11-04 QA 实测的事故：这条守卫曾经恒通过，结果把 11-03 的价格写成了
    40 行 `trade_date=2026-11-04` 的快照，而且 `INSERT OR REPLACE` 会覆盖目标日
    原本正确的行 —— 既伪造了历史，又毁掉了真数据。
    """
    appdb = str(tmp_path / "app.db")
    monkeypatch.setattr(FG, "APP_DB", appdb)

    quotes = {"600519": _quote("600519", session="20261103161453"),
              "000001": _quote("000001", session="20261103161453")}
    stat = FG.write_snapshot(quotes, "2026-11-04", dry_run=False)

    assert stat["refused"] == "source_session_mismatch"
    assert stat["insert"] == 0
    assert stat["session_check"]["want"] == "20261104"
    assert stat["session_check"]["got"] == {"20261103": 2}
    assert _snapshot_rows(appdb, "2026-11-04") == 0, "一行都不许落 —— 这是伪造历史"


def test_snapshot_refuses_when_source_has_no_session(tmp_path, monkeypatch):
    """**P1 回归用例（真正的根因）**：源里没有任何会话日 → 也必须拒写。

    `_session_mismatch()` 返回 `{}` 有**两种**含义：「全部一致」和「一条可校验的都没有」。
    原代码把两者当同一件事 → 守卫变成死代码。**无法校验 ≠ 校验通过**，必须 fail-closed。
    这里刻意只给 `session` 不给 `_session`，复现当时键名不匹配的现场。
    """
    appdb = str(tmp_path / "app.db")
    monkeypatch.setattr(FG, "APP_DB", appdb)

    quotes = {"600519": _quote("600519"), "000001": _quote("000001")}
    assert all("_session" not in r for r in quotes.values())

    stat = FG.write_snapshot(quotes, "2026-11-04", dry_run=False)

    assert stat["refused"] == "source_session_unknown"
    assert stat["insert"] == 0
    assert stat["session_check"]["checkable"] == 0
    assert _snapshot_rows(appdb, "2026-11-04") == 0


def test_snapshot_writes_when_session_matches(tmp_path, monkeypatch):
    """会话日与目标日一致时**必须照常写** —— 防止上面两条修过头把正常路径堵死。

    这是「fail-closed 不等于 fail-always」的那一半：正常当日抓取时 `got` 本来就是空的
    （一致所以为空），绝不能因为「got 为空」就拒写。
    """
    appdb = str(tmp_path / "app.db")
    monkeypatch.setattr(FG, "APP_DB", appdb)

    quotes = {"600519": _quote("600519", session="20261104150500"),
              "000001": _quote("000001", session="20261104150500")}
    stat = FG.write_snapshot(quotes, "2026-11-04", dry_run=False)

    assert "refused" not in stat
    assert stat["insert"] == 2
    assert stat["session_check"]["got"] == {}
    assert _snapshot_rows(appdb, "2026-11-04") == 2


def test_snapshot_drops_row_without_session_but_keeps_others(tmp_path, monkeypatch):
    """混合情形：整批有会话日、个别行没有 → 只丢那些行，其余照写，且丢弃数可见。

    这里不能「整批拒」（太脆，一只异常票会让整轮降级失效），
    也不能「照写」（就写进了一行日期不明的价）。丢弃计数进 skip 直方图，不是静默吞。
    """
    appdb = str(tmp_path / "app.db")
    monkeypatch.setattr(FG, "APP_DB", appdb)

    quotes = {"600519": _quote("600519", session="20261104150500"),
              "000001": _quote("000001")}          # 这一行没有会话日
    stat = FG.write_snapshot(quotes, "2026-11-04", dry_run=False)

    assert stat["insert"] == 1
    assert stat["skip"].get("session_unverified") == 1
    assert _snapshot_rows(appdb, "2026-11-04") == 1


# ---------------------------------------------------------------- 5. 键名契约

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tencent_body(code: str, *, session: str) -> bytes:
    """造一行腾讯 ~ 分隔报价（88 段，序号与 fetch_a_shares_tencent 一致）。"""
    f = ["0"] * 88
    f[0] = "1"
    f[1] = "贵州茅台"
    f[2] = code
    f[3] = "1410.00"        # close
    f[4] = "1400.00"        # prev_close
    f[5] = "1405.00"        # open
    f[30] = session         # 源自己声明的「这是哪一场」
    f[31] = "10.00"
    f[32] = "0.71"
    f[33] = "1420.00"
    f[34] = "1395.00"
    f[36] = "74121"         # 成交量（手）
    f[37] = "1045106.00"    # 成交额（万元）→ amount/close/vol ≈ 100 = 手
    f[38] = "0.50"
    f[39] = "22.10"
    f[44] = "177000000.00"
    f[45] = "177000000.00"
    f[46] = "8.50"
    return ('v_sh%s="%s";' % (code, "~".join(f))).encode("gbk")


def test_fastfetch_output_is_readable_by_production_guard(monkeypatch):
    """**P1 根因用例**：`fastfetch` 的输出必须能被既有 `_session_mismatch()` 读懂。

    这是把两边接在一起测 —— 各测各的都绿，拼起来才是坏的那种 bug。
    只要这条存在，「解析端写 session、守卫端读 _session」就不可能再悄悄回归。
    """
    body = _tencent_body("600519", session="20261103161453")

    class _Req:
        Request = urllib.request.Request

        @staticmethod
        def urlopen(req, timeout=None):
            return _FakeResp(body)

    class _UR:
        parse = urllib.parse
        error = urllib.error
        request = _Req

    monkeypatch.setattr(FF, "urllib", _UR)

    out = FF._fetch_tencent_batch(["600519"])
    r = out["600519"]

    assert r["session"] == "20261103161453"
    assert r["_session"] == "20261103161453", "守卫读的是 _session，源头必须写这个键"
    assert r["trade_date"] == "20261103"

    # 拿**生产代码里那个真守卫**来判：11-03 的价去填 11-04 必须被抓出来
    assert FT._session_mismatch({"600519": r}, "2026-11-04") == {"20261103": 1}
    # 目标日一致时必须为空（否则正常路径会被自己拦死）
    assert FT._session_mismatch({"600519": r}, "2026-11-03") == {}


def test_fastfetch_keeps_amount_in_yuan_and_volume_unit(monkeypatch):
    """腾讯给的成交额是「万元」，必须 ×10000 成「元」；成交量单位必须自证出来。

    单位错不会报错，只会让筛选器把一只票的换手率算成 100 倍或 1%——
    2026-10-31 那次 P0（K 线显示 599.27 万手 vs 真实 5.99 万手）就是这么来的。
    """
    body = _tencent_body("600519", session="20261103161453")

    class _Req:
        Request = urllib.request.Request

        @staticmethod
        def urlopen(req, timeout=None):
            return _FakeResp(body)

    class _UR:
        parse = urllib.parse
        error = urllib.error
        request = _Req

    monkeypatch.setattr(FF, "urllib", _UR)
    r = FF._fetch_tencent_batch(["600519"])["600519"]

    assert r["amount"] == pytest.approx(1045106.00 * 10000.0)
    assert r["volume_unit"] == "hand"
    assert r["volume"] == pytest.approx(74121 * 100.0), "手 → 股 必须 ×100"
    assert r["volume_raw"] == pytest.approx(74121.0), "源原值要留着，快照表存的就是它"
