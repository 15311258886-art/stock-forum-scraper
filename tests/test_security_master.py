# -*- coding: utf-8 -*-
"""`security_master` 守卫测试（2026-11-01，P0「全 A 股宇宙不完整」）。

这个文件存在的理由和 `test_universe_kind.py` 一样：这一层出错**不会报错**。
少 328 只北交所，页面照样打开、信号照样生成，只是永远选不出那些票；
一只可转债混进来，超额会悄悄被算错。所以口径必须钉死：

  · 北交所（920 段）必须进 -- 这是 P0 的直接断言；
  · 可转债 / ETF 必须**留在表里但 kind != stock**（不是删掉）；
  · 缺表必须抛异常，绝不返回空列表（空宇宙会伪装成"今天没有信号"）；
  · build 必须幂等（cron 每天跑，行数不能长）；
  · 临时名（XD/XR/DR）要能还原成简称，否则一次除权就会造成跨表"名称错配"。

全部用 `tmp_path` 造库；唯一碰真实库的用例只读打开（`mode=ro`），
**绝不允许测试写生产库**。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import units as U  # noqa: E402
from viewer import security_master as SM  # noqa: E402

PROD_DB = os.path.join(ROOT, "viewer", "app.db")


# ── 样本库 ────────────────────────────────────────────────────────────────────
def _make_db(tmp_path, name="sm.db"):
    """造一个最小但"五脏俱全"的库：各板块 + 北交所 + 转债 + ETF + XD 临时名。

    刻意让 920000 只出现在「名称表 + 指标库 + 快照」里（不在白名单），
    复现生产的真实形态：白名单漏收整块北交所。
    """
    con = sqlite3.connect(str(tmp_path / name))
    con.execute("CREATE TABLE a_shares_universe "
                "(code TEXT PRIMARY KEY, name TEXT, board TEXT, exchange TEXT, "
                " status TEXT, first_seen TEXT, last_seen TEXT)")
    con.execute("CREATE TABLE stock_names "
                "(code TEXT PRIMARY KEY, name TEXT, company TEXT, market TEXT, "
                " source TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE stock_daily_snapshot "
                "(trade_date TEXT, code TEXT, name TEXT, board TEXT, exchange TEXT, "
                " PRIMARY KEY (trade_date, code))")
    con.execute("CREATE TABLE stock_indicators_daily "
                "(date TEXT, code TEXT, name TEXT, close REAL, "
                " PRIMARY KEY (date, code))")
    con.execute("CREATE TABLE stock_org (code TEXT PRIMARY KEY, name TEXT, list_date TEXT)")

    con.executemany("INSERT INTO a_shares_universe "
                    "(code,name,board,exchange,status) VALUES (?,?,?,?,?)", [
                        ("600519", "贵州茅台", "沪市主板", "上交所", "listed"),
                        ("300750", "宁德时代", "创业板", "深交所", "listed"),
                        ("601878", "XD浙商证", "沪市主板", "上交所", "listed"),
                        ("111024", "澳弘转债", "其它", "其它", "listed"),
                        ("513050", "中概互联网ETF易方达", "其它", "其它", "listed"),
                    ])
    con.executemany("INSERT INTO stock_names "
                    "(code,name,market,source) VALUES (?,?,?,?)", [
                        ("600519", "贵州茅台", "沪市主板", "t"),
                        ("300750", "宁德时代", "创业板", "t"),
                        ("601878", "XD浙商证", "沪市主板", "t"),
                        ("920000", "安徽凤凰", "北交所", "t"),
                        ("920012", "XD创达新", "北交所", "t"),
                        ("111024", "澳弘转债", "未知板块", "t"),
                        ("513050", "中概互联网ETF易方达", "未知板块", "t"),
                    ])
    con.executemany("INSERT INTO stock_daily_snapshot "
                    "(trade_date,code,name,board,exchange) VALUES (?,?,?,?,?)", [
                        ("2026-10-29", "600519", "贵州茅台", "沪市主板", "上交所"),
                        ("2026-10-29", "601878", "浙商证券", "沪市主板", "上交所"),
                        ("2026-10-29", "920000", "安徽凤凰", "北交所", "北交所"),
                        ("2026-10-30", "600519", "贵州茅台", "沪市主板", "上交所"),
                        ("2026-10-30", "601878", "XD浙商证", "沪市主板", "上交所"),
                        ("2026-10-30", "920000", "安徽凤凰", "北交所", "北交所"),
                        ("2026-10-30", "111024", "澳弘转债", "其它", "其它"),
                        ("2026-10-30", "513050", "中概互联网ETF易方达", "其它", "其它"),
                    ])
    con.executemany("INSERT INTO stock_indicators_daily "
                    "(date,code,name,close) VALUES (?,?,?,?)", [
                        ("2026-10-29", "600519", "贵州茅台", 1500.0),
                        ("2026-10-30", "600519", "贵州茅台", 1510.0),
                        ("2026-10-30", "300750", "宁德时代", 200.0),
                        ("2026-10-30", "601878", "XD浙商证", 10.0),
                        ("2026-10-30", "920000", "安徽凤凰", 12.0),
                        ("2026-10-30", "920012", "XD创达新", 8.0),
                        ("2014-09-19", "111024", "澳弘转债", 100.0),
                    ])
    con.executemany("INSERT INTO stock_org (code,name,list_date) VALUES (?,?,?)", [
        ("600519", "贵州茅台", "2001-08-27"),
        ("920000", "安徽凤凰", "2020-12-23"),
        ("000004", "国华退", "1990-12-01"),   # 只在工商表里，已退市
    ])
    con.commit()
    SM.reset_cache()
    return con


@pytest.fixture()
def db(tmp_path):
    con = _make_db(tmp_path)
    yield con
    con.close()
    SM.reset_cache()


# ── 1. 北交所必须进宇宙（P0 的直接断言）───────────────────────────────────────
def test_build_backfills_bse(db):
    stats = SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    codes = SM.stock_codes(db)
    assert "920000" in codes, "北交所 920 段没进宇宙 —— 这就是 P0 本身"
    assert "920012" in codes
    assert stats["stock_bse"] == 2
    row = SM.get("920000", db)
    assert row["board"] == SM.BOARD_BSE
    assert row["exchange"] == SM.EXCHANGE_BSE
    assert row["kind"] == SM.KIND_STOCK
    assert "stock_indicators_daily" in row["source"], "补齐来源要可追溯"
    # 名单表里有、白名单没有的票，也要被收进来
    assert "920000" not in {c for (c,) in db.execute("SELECT code FROM a_shares_universe")}


# ── 2. 非股票：保留但标出（不是删除）──────────────────────────────────────────
def test_nonstock_kept_and_marked(db):
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    bond = SM.get("111024", db)
    etf = SM.get("513050", db)
    assert bond is not None and bond["kind"] == SM.KIND_BOND
    assert etf is not None and etf["kind"] == SM.KIND_ETF
    codes = SM.stock_codes(db)
    assert "111024" not in codes and "513050" not in codes
    assert SM.is_stock("111024", db) is False
    assert SM.is_stock("513050", db) is False
    # 表里必须有这些行（"删掉"会让下一个人再踩一次）
    assert db.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] >= 7


# ── 3. kind 判定必须复用 units.stock_kind（不许第二套口径）────────────────────
def test_kind_reuses_units(tmp_path, monkeypatch, db):
    """两条断言：结果与 units 一致 + 确实调用了 units（monkeypatch 计数）。"""
    # (a) 结果一致
    cases = [("600519", "贵州茅台"), ("920000", "安徽凤凰"),
             ("111024", "澳弘转债"), ("513050", "中概互联网ETF易方达"),
             ("999999", "")]
    for code, name in cases:
        kind = SM.classify_kind(code, name)
        unit = U.stock_kind(code, name)
        if unit == U.KIND_STOCK:
            assert kind == SM.KIND_STOCK
        elif unit == U.KIND_BOND:
            assert kind == SM.KIND_BOND
        elif unit == U.KIND_FUND:
            assert kind in (SM.KIND_ETF, SM.KIND_FUND)
        else:
            assert kind == SM.KIND_OTHER

    # (b) build 过程中真的走了 units.stock_kind
    calls = {"n": 0}
    real = U.stock_kind

    def spy(code, name=""):
        calls["n"] += 1
        return real(code, name)

    monkeypatch.setattr(U, "stock_kind", spy)
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    assert calls["n"] >= 7, "build 没有复用 units.stock_kind（可能又写了一套前缀判断）"


def test_kind_vocabulary_is_stable():
    """kind 的取值是全项目契约，改动必须同步改 schema 注释与审计。"""
    for k in (SM.KIND_STOCK, SM.KIND_BOND, SM.KIND_ETF, SM.KIND_FUND,
              SM.KIND_INDEX, SM.KIND_OTHER):
        assert k in SM.KINDS


# ── 4. 缺表必须抛异常，不许返回空列表 ────────────────────────────────────────
def test_missing_table_raises(tmp_path):
    con = sqlite3.connect(str(tmp_path / "empty.db"))
    con.execute("CREATE TABLE a_shares_universe (code TEXT, name TEXT)")
    with pytest.raises(SM.SecurityMasterMissing) as ei:
        SM.load(con)
    assert "build_security_master.py" in str(ei.value), "报错必须给出修复命令"
    with pytest.raises(SM.SecurityMasterMissing):
        SM.stock_codes(con)        # 关键：不是返回 []
    with pytest.raises(SM.SecurityMasterMissing):
        SM.get("600519", con)
    with pytest.raises(SM.SecurityMasterMissing):
        SM.assert_stock("600519", con)
    con.close()


# ── 5. 幂等 ───────────────────────────────────────────────────────────────────
def test_build_is_idempotent(db):
    s1 = SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    n1 = db.execute("SELECT COUNT(*) FROM security_master").fetchone()[0]
    s2 = SM.build(db, kline_db=[], as_of="2026-11-01T00:10:00")
    n2 = db.execute("SELECT COUNT(*) FROM security_master").fetchone()[0]
    assert n1 == n2 == s1["total"] == s2["total"]
    assert s2["inserted"] == 0 and s2["updated"] == n2
    # as_of 必须刷新（消费方靠它判断新鲜度）
    assert {r for (r,) in db.execute("SELECT DISTINCT as_of FROM security_master")} \
        == {"2026-11-01T00:10:00"}
    # 主键唯一，不可能重复行
    assert db.execute("SELECT COUNT(DISTINCT code) FROM security_master").fetchone()[0] == n2


def test_build_converges_when_source_row_disappears(db):
    """来源里没有的代码必须从主表消失 —— 派生表不能攒历史脏代码。"""
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    assert SM.get("920012", db) is not None
    db.execute("DELETE FROM stock_names WHERE code='920012'")
    db.execute("DELETE FROM stock_indicators_daily WHERE code='920012'")
    db.commit()
    stats = SM.build(db, kline_db=[], as_of="2026-11-01T01:00:00")
    assert SM.get("920012", db) is None
    assert stats["orphans_removed"] >= 1


# ── 6. 除权临时名还原 ────────────────────────────────────────────────────────
def test_temp_name_resolved_to_real_name(db):
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    # 名称表里只有 XD 临时名，快照里能找到恢复后的简称
    assert SM.get("601878", db)["name"] == "浙商证券"
    # 全网只有临时名的，如实保留（不编）
    assert SM.get("920012", db)["name"] == "XD创达新"
    assert SM.is_temporary_name("XD浙商证") and SM.is_temporary_name("DR某某")
    assert not SM.is_temporary_name("浙商证券")


def test_name_alias_matches_handles_truncation():
    """交易所把除权名截短（浙商证券 → XD浙商证），比对必须容忍。"""
    assert SM.name_alias_matches("XD浙商证", "浙商证券")
    assert SM.name_alias_matches("浙商证券", "XD浙商证")
    assert SM.name_alias_matches("XD富创精", "富创精密")
    assert not SM.name_alias_matches("XD浙商证", "贵州茅台")
    assert not SM.name_alias_matches("", "贵州茅台")
    assert SM.name_alias_matches("贵州茅台", "贵州茅台")


# ── 7. 板块过滤与 include_bse 语义 ───────────────────────────────────────────
def test_stock_codes_filters(db):
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    all_codes = SM.stock_codes(db)
    no_bse = SM.stock_codes(db, include_bse=False)
    assert len(all_codes) == len(no_bse) + 2
    assert "920000" not in no_bse
    star_only = SM.stock_codes(db, boards=(SM.BOARD_STAR,))
    assert star_only == []
    bse_only = SM.stock_codes(db, boards=SM.BOARD_BSE)     # 传 str 也要能用
    assert bse_only == ["920000", "920012"]


def test_exclude_delist_suspects_is_opt_in(db):
    """决策（2026-11-01）：is_active 保持全 1，退市嫌疑由调用方**显式**过滤。

    买入候选必须传 True；宇宙统计保持默认 False。
    默认值不能替调用方做决定 —— 用默认值把"能不能推荐"盖住，正是这个项目的病根。
    """
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    default_codes = SM.stock_codes(db)
    strict_codes = SM.stock_codes(db, exclude_delist_suspects=True)
    assert "000004" in default_codes, "统计口径下退市嫌疑仍属于宇宙"
    assert "000004" not in strict_codes, "买入候选必须剔除退市嫌疑"
    assert set(strict_codes) < set(default_codes)
    # 只差退市嫌疑，别的一只不许少
    assert set(default_codes) - set(strict_codes) == {r["code"] for r in SM.delist_suspects(db)}
    # 与板块/北交所参数可以叠加
    assert "920000" in SM.stock_codes(db, exclude_delist_suspects=True)


def test_board_and_exchange_derivation():
    assert SM.board_of("600519") == SM.BOARD_SH_MAIN
    assert SM.board_of("688981") == SM.BOARD_STAR
    assert SM.board_of("689009") == SM.BOARD_STAR      # CDR，也属科创板
    assert SM.board_of("300750") == SM.BOARD_CHINEXT
    assert SM.board_of("301029") == SM.BOARD_CHINEXT
    assert SM.board_of("002594") == SM.BOARD_SZ_MAIN
    assert SM.board_of("000004") == SM.BOARD_SZ_MAIN
    assert SM.board_of("920000") == SM.BOARD_BSE
    assert SM.board_of("835670") == SM.BOARD_BSE
    assert SM.board_of("873305") == SM.BOARD_BSE
    assert SM.board_of("513050") == SM.BOARD_OTHER     # ETF 不在任何股票板块
    # 上游给了真正的股票板块就以它为准（例如 689 被白名单错记成沪市主板）
    assert SM.board_of("689009", "沪市主板") == SM.BOARD_SH_MAIN
    assert SM.board_of("600519", "未知板块") == SM.BOARD_SH_MAIN
    assert SM.exchange_of("920000") == SM.EXCHANGE_BSE
    assert SM.exchange_of("600519") == SM.EXCHANGE_SSE
    assert SM.exchange_of("300750") == SM.EXCHANGE_SZSE
    assert SM.exchange_zh(SM.EXCHANGE_SSE) == "上交所"


# ── 8. assert_stock / is_stock 的失败语义 ────────────────────────────────────
def test_assert_stock_semantics(db):
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    assert SM.assert_stock("600519", db) is True
    with pytest.raises(SM.NotAStock):
        SM.assert_stock("111024", db)          # 可转债
    with pytest.raises(SM.NotAStock):
        SM.assert_stock("513050", db)          # ETF
    with pytest.raises(SM.NotAStock):
        SM.assert_stock("999999", db)          # 不认识
    # 不给 conn 时只做代码段判定（文档写明看不到退市状态）
    assert SM.assert_stock("600519") is True
    with pytest.raises(SM.NotAStock):
        SM.assert_stock("111024")


# ── 9. 退市线索只作证据，不擅自改 is_active ─────────────────────────────────
def test_delist_evidence_recorded_but_is_active_unchanged(db):
    SM.build(db, kline_db=[], as_of="2026-11-01T00:00:00")
    row = SM.get("000004", db)      # 只在 stock_org 里，名称带「退」
    assert row["kind"] == SM.KIND_STOCK
    assert row["delist_date"] is None, "没有退市日期数据源时不许编造"
    assert "delist=name_suffix" in row["source"], "证据必须留痕"
    assert row["is_active"] == 1, "按本轮约定：delist_date 为空 ⇒ is_active=1"
    assert [r["code"] for r in SM.delist_suspects(db)] == ["000004"]


# ── 10. dry_run 不写库 ───────────────────────────────────────────────────────
def test_dry_run_does_not_write(tmp_path):
    con = _make_db(tmp_path)
    stats = SM.build(con, kline_db=[], dry_run=True, as_of="2026-11-01T00:00:00")
    assert stats["total"] >= 7
    assert con.execute("SELECT COUNT(*) FROM sqlite_master "
                       "WHERE name='security_master'").fetchone()[0] == 0
    con.close()


# ── 11. 真实生产库：只读断言（库不存在/未建表就 skip）────────────────────────
def test_production_master_is_complete_readonly():
    if not os.path.exists(PROD_DB):
        pytest.skip("无生产库")
    con = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"), uri=True)
    try:
        if not SM.table_exists(con):
            pytest.skip("生产库还没建 security_master（先跑 tools/build_security_master.py）")
        SM.reset_cache()
        rows = SM.load(con, force=True)
        assert len(rows) >= 5500, "主表行数不足，北交所可能又漏了"
        bse = [r for r in rows
               if r["kind"] == SM.KIND_STOCK and r["board"] == SM.BOARD_BSE]
        assert len(bse) >= 300, "北交所股票 < 300，920 段没补齐"
        # 非股票必须表里有、但进不了 stock_codes
        codes = set(SM.stock_codes(con))
        for c in ("111024", "113710", "513050"):
            assert SM.get(c, con) is not None, "非股票被删了 —— 应该标 kind 而不是删"
            assert c not in codes
    finally:
        con.close()
        SM.reset_cache()
