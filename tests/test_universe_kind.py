# -*- coding: utf-8 -*-
"""标的类型判定与宇宙审计的守卫测试（2026-10-31 决策④）。

这个文件存在的理由：`stock_kind()` 判错的后果是**静默**的 ——
一只债券被当成股票算进超额，或者整个北交所被当成"不存在"，
两种情况都不会报错，只会让数字悄悄变错。所以判据必须钉死。
"""
from __future__ import annotations

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import units as U  # noqa: E402


# ── 判定分层 ──────────────────────────────────────────────────────────────────
def test_convertible_bonds_are_not_stocks():
    """可转债必须判成 bond —— 111024 是真实混入过的那只。"""
    for code, name in (("111024", "澳弘转债"), ("113710", "四方转债"),
                       ("110043", ""), ("123456", ""), ("127789", ""), ("128001", "")):
        assert U.stock_kind(code, name) == U.KIND_BOND, code
        assert not U.is_tradable_stock(code, name), code


def test_name_overrides_lookalike_code():
    """代码像股票、名字是转债时，按转债处理 —— 宁可漏算，不可错算。"""
    assert U.stock_kind("600001", "某某转债") == U.KIND_BOND
    assert not U.is_tradable_stock("600001", "某某转债")


def test_bse_920_segment_is_stock():
    """920 段是北交所 2023 起的新代码段，最常被漏掉的一段。
    漏掉它的后果是整个板块从选股宇宙里消失。"""
    assert U.stock_kind("920000", "安徽凤凰") == U.KIND_STOCK
    assert U.is_tradable_stock("920000", "安徽凤凰")
    # 也必须在涨跌停表里有定义（30%），否则涨跌停判定会当成"不设限"
    assert U.price_limit_pct("920000", "安徽凤凰") == 0.30


def test_all_a_share_segments_are_stocks():
    """各交易所板块的真实代码都要认成股票，一个都不能漏。"""
    cases = [("600519", "贵州茅台"), ("601318", "中国平安"), ("603288", "海天味业"),
             ("605588", "冠石科技"), ("688981", "中芯国际"), ("689009", "九号公司-WD"),
             ("000001", "平安银行"), ("001979", "招商蛇口"), ("002594", "比亚迪"),
             ("003816", "中国广核"), ("300750", "宁德时代"), ("301029", "怡合达"),
             ("302132", "中航成飞"), ("835670", "数字人"), ("873305", "九菱科技")]
    for code, name in cases:
        assert U.stock_kind(code, name) == U.KIND_STOCK, (code, name)


def test_funds_are_not_stocks():
    """ETF / LOF 不是股票。513050 是真实出现在白名单里的那只。"""
    for code, name in (("513050", "中概互联网ETF易方达"), ("510300", "沪深300ETF"),
                       ("159915", "创业板ETF"), ("161725", "招商中证白酒")):
        assert U.stock_kind(code, name) == U.KIND_FUND, code
        assert not U.is_tradable_stock(code, name), code


def test_unknown_is_not_defaulted_to_stock():
    """判不出时必须是 unknown 且不可交易 —— 不许默认当股票。

    这是本模块最核心的一条：默认当股票，正是当初让债券混进统计的方式。
    """
    assert U.stock_kind("999999", "") == U.KIND_UNKNOWN
    assert U.stock_kind("", "") == U.KIND_UNKNOWN
    assert not U.is_tradable_stock("999999", "")
    assert not U.is_tradable_stock("", "")


def test_old_is_convertible_bond_signature_still_works():
    """旧调用方只用 code，加 name 参数后不能破坏它们。"""
    assert U.is_convertible_bond("111024")
    assert not U.is_convertible_bond("600519")


# ── 审计工具 ──────────────────────────────────────────────────────────────────
def _mini_db(tmp_path):
    """造一个最小库：1 只股票 + 1 只债券 + 白名单漏了股票。"""
    import sqlite3
    p = tmp_path / "mini.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE stock_indicators_daily "
                "(date TEXT, code TEXT, name TEXT, close REAL)")
    con.executemany("INSERT INTO stock_indicators_daily VALUES (?,?,?,?)", [
        ("2026-01-05", "600519", "贵州茅台", 1500.0),
        ("2026-01-05", "920000", "安徽凤凰", 10.0),
        ("2026-01-05", "111024", "澳弘转债", 100.0),
    ])
    con.execute("CREATE TABLE a_shares_universe "
                "(code TEXT, name TEXT, board TEXT, exchange TEXT, status TEXT)")
    con.executemany("INSERT INTO a_shares_universe VALUES (?,?,?,?,?)", [
        ("600519", "贵州茅台", "沪市主板", "上交所", "listed"),
        ("111024", "澳弘转债", "其它", "其它", "listed"),
    ])
    con.execute("CREATE TABLE stock_names (code TEXT, name TEXT, market TEXT)")
    con.executemany("INSERT INTO stock_names VALUES (?,?,?)", [
        ("600519", "贵州茅台", "沪市主板"),
        ("920000", "安徽凤凰", "北交所"),
        ("111024", "澳弘转债", "未知板块"),
    ])
    con.commit()
    return con


def test_audit_collect_finds_the_three_problems(tmp_path):
    """审计必须同时抓出：指标库混债券、白名单混债券、白名单漏北交所。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import audit_universe as AU

    con = _mini_db(tmp_path)
    r = AU.collect(con)
    con.close()

    assert [x["code"] for x in r["indicators_nonstock"]] == ["111024"]
    assert [x["code"] for x in r["universe_nonstock"]] == ["111024"]
    assert r["universe_missing_total"] == 1
    assert any(x["seg"] == "920" for x in r["universe_missing"])
    level, warns = AU.verdict_of(r)
    assert level == "WARN"
    assert len(warns) == 3, warns


def test_audit_does_not_modify_data(tmp_path):
    """审计只读 —— 调前后行数、内容必须逐位相同。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import audit_universe as AU

    con = _mini_db(tmp_path)
    before = {
        t: con.execute("SELECT * FROM %s ORDER BY 1,2" % t).fetchall()
        for t in ("stock_indicators_daily", "a_shares_universe", "stock_names")
    }
    AU.collect(con)
    after = {
        t: con.execute("SELECT * FROM %s ORDER BY 1,2" % t).fetchall()
        for t in ("stock_indicators_daily", "a_shares_universe", "stock_names")
    }
    con.close()
    assert before == after, "审计工具改动了数据 —— 它必须只读"
