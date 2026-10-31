# -*- coding: utf-8 -*-
"""消费方迁移守卫测试（2026-11-01，P0「全 A 股宇宙不完整」的下半场）。

## 为什么单独一个文件

上一轮把「有哪些股票」收口成 `security_master` 一张表；但**只要还有代码直接读
`a_shares_universe`，那 350 只北交所就仍然会静默消失**。收口是否真的生效，
唯一的证据是"取码路径实际返回了什么"，所以这里对每个迁移过的入口各钉一条断言：

    viewer/db.py           all_symbols()          —— 名称回填/兜底取码
    viewer/selection.py    _allowed_codes()       —— 选股池（有意排除北交所）
    tools/fetch_*          _only_stocks / _market_prefix —— 抓取端只落股票
    tools/verify_fetch.py  覆盖率分母 + 退出码      —— 用漏数据当分母＝自证清白
    tools/intraday_snap.py _qq()                   —— 盘中快照的 920→sh 同型 bug
    strategy_sim.py        _product_universe()     —— 剔北交所＝产品选择，不写段表
    holders_fetch.py       collect_codes/_secucode  —— 同上，且 .BJ 后缀靠主表

## 判定口径（重要）

「迁移后集合 ⊇ 迁移前」的**股票部分**成立；但迁移后**有意剔除**了 3 个非股票代码
（111024/113710 可转债、513050 ETF）—— 那是修复，不是回归，所以断言写成
"旧口径里的股票一个不少 + 北交所 350 全在 + 非股票被剔除"，而不是整体超集。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from viewer import security_master as SM  # noqa: E402
from viewer import units as U  # noqa: E402


# ── 样本库：各板块 + 北交所 + 转债 + ETF + 退市名 + XD 临时名 ───────────────────
def _mini(tmp_path, name="app.db"):
    from viewer.db import Database      # 用真实类，确保测的是线上那条路径
    db = Database(path=tmp_path / name)
    con = db.conn
    con.execute("CREATE TABLE IF NOT EXISTS a_shares_universe ("
                "code TEXT PRIMARY KEY, name TEXT, board TEXT, exchange TEXT, "
                "status TEXT, first_seen TEXT, last_seen TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS stock_daily_snapshot ("
                "trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, board TEXT, "
                "exchange TEXT, pe_ttm REAL, pb REAL, total_mv REAL, "
                "PRIMARY KEY (trade_date, code))")
    con.execute("CREATE TABLE IF NOT EXISTS stock_indicators_daily ("
                "date TEXT, code TEXT, name TEXT, PRIMARY KEY (date, code))")
    con.execute("CREATE TABLE IF NOT EXISTS stock_org ("
                "code TEXT PRIMARY KEY, name TEXT, list_date TEXT)")

    con.executemany("INSERT INTO a_shares_universe "
                    "(code,name,board,exchange,status) VALUES (?,?,?,?,?)", [
                        ("600519", "贵州茅台", "沪市主板", "上交所", "listed"),
                        ("300750", "宁德时代", "创业板", "深交所", "listed"),
                        ("835670", "数字人", "北交所", "北交所", "listed"),
                        ("111024", "澳弘转债", "其它", "其它", "listed"),
                        ("513050", "中概互联网ETF易方达", "其它", "其它", "listed"),
                    ])
    con.executemany("INSERT INTO stock_names (code,name,market,source,updated_at) "
                    "VALUES (?,?,?,?,?)", [
                        ("600519", "贵州茅台", "沪市主板", "t", "2026-10-30"),
                        ("300750", "宁德时代", "创业板", "t", "2026-10-30"),
                        ("835670", "数字人", "北交所", "t", "2026-10-30"),
                        ("920000", "安徽凤凰", "北交所", "t", "2026-10-30"),
                        ("920012", "XD创达新", "北交所", "t", "2026-10-30"),
                        ("111024", "澳弘转债", "未知板块", "t", "2026-10-30"),
                        ("513050", "中概互联网ETF易方达", "未知板块", "t", "2026-10-30"),
                    ])
    con.executemany("INSERT INTO stock_indicators_daily (date,code,name) VALUES (?,?,?)", [
        ("2026-10-30", "600519", "贵州茅台"),
        ("2026-10-30", "920000", "安徽凤凰"),
        ("2014-09-19", "111024", "澳弘转债"),
    ])
    con.executemany("INSERT INTO stock_org (code,name,list_date) VALUES (?,?,?)", [
        ("000004", "国华退", "1990-12-01"),
    ])
    con.execute("INSERT INTO stock_daily_snapshot (trade_date,code,name,board,exchange) "
                "VALUES ('2026-10-30','600519','贵州茅台','沪市主板','上交所'),"
                "       ('2026-10-30','300750','宁德时代','创业板','深交所'),"
                "       ('2026-10-30','835670','数字人','北交所','北交所')")
    con.commit()
    SM.build(con, kline_db=[], as_of="2026-11-01T00:00:00")
    return db


@pytest.fixture()
def db(tmp_path):
    d = _mini(tmp_path)
    yield d
    try:
        d.conn.close()
    except Exception:  # noqa: BLE001
        pass
    SM.reset_cache()


# ── 1. viewer/db.py: all_symbols 不再漏北交所 ────────────────────────────────
def test_db_all_symbols_covers_bse_and_drops_nonstock(db):
    con = db.conn
    # 旧口径（迁移前）：白名单 ∪ 名称表
    legacy = {r[0] for r in con.execute(
        "SELECT code FROM a_shares_universe UNION SELECT code FROM stock_names")}
    legacy_stocks = {c for c in legacy if U.stock_kind(c) == U.KIND_STOCK}

    codes = set(db.all_symbols())
    assert legacy_stocks <= codes, "迁移后丢了旧口径里的股票：%s" % (legacy_stocks - codes)
    assert {"920000", "920012", "835670"} <= codes, "北交所没进 all_symbols"
    assert "000004" in codes, "只在工商表里的代码也要能取到（名称回填需要）"
    # 非股票被剔除 —— 这是修复
    assert "111024" not in codes and "513050" not in codes
    assert legacy - legacy_stocks == {"111024", "513050"}


def test_db_all_symbols_never_returns_empty_when_master_missing(tmp_path):
    """主表缺失时必须回落到旧源，而不是返回空列表（空列表＝静默停摆）。"""
    from viewer.db import Database
    db = Database(path=tmp_path / "bare.db")
    con = db.conn
    con.execute("CREATE TABLE IF NOT EXISTS a_shares_universe (code TEXT, name TEXT)")
    con.execute("INSERT INTO a_shares_universe (code,name) VALUES ('600519','贵州茅台')")
    con.commit()
    assert db.all_symbols() == ["600519"]
    con.close()
    SM.reset_cache()


# ── 2. viewer/selection.py: 显式排除北交所 + 排除退市标的 ────────────────────
def test_selection_allowed_codes_explicit_policy(db):
    import viewer.selection as SEL
    con = db.conn
    allowed = SEL._allowed_codes(con)

    assert "920000" not in allowed, "北交所必须被显式排除（产品选择）"
    assert "600519" in allowed and "300750" in allowed
    assert "111024" not in allowed and "513050" not in allowed
    assert "000004" not in allowed, "退市整理期标的不能进买入候选"

    # 与"迁移前"的股票口径比：旧前缀规则放行的股票，新口径一只不少
    old_ok = {c for c in SM.stock_codes(con)
              if not (c[:2] == "92" or c[:1] in ("4", "8"))}
    assert old_ok >= (allowed | {"000004"}) - {"000004"}
    assert old_ok - allowed == {"000004"}   # 唯一差集是退市嫌疑，且是故意的


def test_selection_names_and_limit_threshold_from_master(db):
    import viewer.selection as SEL
    names = SEL._names(db.conn)
    assert names["600519"][0] == "贵州茅台"
    assert names["920000"][1] == SM.BOARD_BSE
    # 涨跌停阈值按板块判（以前是 selection 自己一份前缀表，和 units 不一致）
    assert SEL._zt_thr("600519") == 0.098
    assert SEL._zt_thr("300750") == 0.198
    assert SEL._zt_thr("688981") == 0.198
    assert SEL._zt_thr("920000") == 0.295      # 北交所 30%
    assert SEL._zt_thr("302132") == 0.198      # 创业板新段 302（units 尚未覆盖）
    assert SEL._zt_thr("835670") == 0.295


def test_selection_refuses_to_build_empty_pool(tmp_path):
    """主表缺失时必须 ok=False（不是产出空池）。"""
    import viewer.selection as SEL
    con = sqlite3.connect(str(tmp_path / "empty.db"))
    r = SEL.refresh_pool(con)
    con.close()
    SM.reset_cache()
    assert r["ok"] is False
    assert "security_master" in r["error"]


# ── 3. 抓取端：只落股票 + 修 920 的市场前缀 ──────────────────────────────────
def test_snapshot_fetcher_only_stocks():
    import fetch_a_shares_snapshot as F
    rows = [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "920000", "name": "安徽凤凰"},
        {"code": "111024", "name": "澳弘转债"},
        {"code": "513050", "name": "中概互联网ETF易方达"},
        {"code": "123456", "name": "某某转债"},
    ]
    kept = F._only_stocks(rows)
    assert [r["code"] for r in kept] == ["600519", "920000"]


def test_snapshot_fetcher_board_of_covers_920():
    import fetch_a_shares_snapshot as F
    assert F._board_of("920000") == ("北交所", "北交所")
    assert F._board_of("835670") == ("北交所", "北交所")
    assert F._board_of("600519") == ("沪市主板", "上交所")
    assert F._board_of("688981") == ("科创板", "上交所")
    assert F._board_of("689009") == ("科创板", "上交所")   # CDR
    assert F._board_of("300750") == ("创业板", "深交所")
    assert F._board_of("302132") == ("创业板", "深交所")
    assert F._board_of("000004") == ("深市主板", "深交所")


def test_tencent_market_prefix_fixes_920():
    """920 段以前被判成 sh（腾讯返回空，静默拿不到行情）。"""
    import fetch_a_shares_tencent as T
    assert T._market_prefix("920000") == "bj"
    assert T._market_prefix("835670") == "bj"
    assert T._market_prefix("430047") == "bj"
    assert T._market_prefix("600519") == "sh"
    assert T._market_prefix("688981") == "sh"
    assert T._market_prefix("300750") == "sz"
    assert T._market_prefix("000004") == "sz"


def test_tencent_candidates_from_master(db):
    import fetch_a_shares_tencent as T
    codes = T._candidate_codes(db.conn)
    assert "920000" in codes, "北交所必须在抓取候选里（否则快照永远缺这一块）"
    assert "111024" not in codes and "513050" not in codes


# ── 4. tools/verify_fetch.py: 分母换成权威主表 ───────────────────────────────
def test_verify_fetch_uses_master_as_denominator(db, monkeypatch, capsys):
    import verify_fetch as V
    monkeypatch.setattr(V, "DB_PATH", db.path)
    rc = V.main([])
    out = capsys.readouterr().out
    assert "权威股票数 security_master" in out
    assert "覆盖率(权威股票 ∩ 快照 / 权威股票)" in out
    # 快照只有 3 只、主表 6 只股票 → 50%，必须报缺口；且北交所缺口被点名，
    # 证明分母真的换了（旧分母用 a_shares_universe 只有 5 行，会算出 60%）
    assert "北交所" in out
    assert rc == V.EXIT_GAP == 1
    assert "退出码 1（真实缺口）" in out


def test_verify_fetch_lists_every_missing_code_with_reason(db, monkeypatch, capsys, tmp_path):
    """缺口必须逐个列 code + 归因，不许只给百分比（只给百分比等于没给信息）。"""
    import verify_fetch as V
    monkeypatch.setattr(V, "DB_PATH", db.path)
    # 取证目录指向空目录：真实仓库里那份是生产数据，不能拿来解释测试库的缺口
    monkeypatch.setattr(V, "SNAP_DIR", str(tmp_path / "no_snap"))
    rc = V.main([])
    out = capsys.readouterr().out
    assert rc == V.EXIT_GAP
    for code in ("920000", "920012", "000004"):
        assert code in out, "缺失代码 %s 没被逐个列出" % code
    assert "no_evidence" in out, "无取证文件时必须归因到 no_evidence，而不是猜"


def test_verify_fetch_exit2_when_db_missing(monkeypatch, capsys):
    import verify_fetch as V
    monkeypatch.setattr(V, "DB_PATH", os.path.join(ROOT, "_no_such_db_.db"))
    assert V.main([]) == V.EXIT_ERR == 2


def test_verify_fetch_exit2_when_master_missing(tmp_path, monkeypatch, capsys):
    """主表缺失 = 没有分母 → 读码失败（2），不能报成"有缺口"（1），更不能当达标（0）。"""
    import verify_fetch as V
    p = tmp_path / "nostock.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE stock_daily_snapshot (trade_date TEXT, code TEXT, "
                "pe_ttm REAL, pb REAL, total_mv REAL)")
    con.execute("INSERT INTO stock_daily_snapshot (trade_date,code) VALUES ('2026-10-30','600519')")
    con.commit()
    con.close()
    monkeypatch.setattr(V, "DB_PATH", str(p))
    SM.reset_cache()
    assert V.main([]) == V.EXIT_ERR == 2
    SM.reset_cache()


def test_verify_fetch_exit0_when_coverage_passes(tmp_path, monkeypatch, capsys):
    """覆盖率 ≥ 阈值就必须 0：缺口长红等于没人看退出码，红绿灯会失效。"""
    import verify_fetch as V
    from viewer.db import Database
    db = Database(path=tmp_path / "full.db")
    con = db.conn
    con.execute("CREATE TABLE IF NOT EXISTS a_shares_universe ("
                "code TEXT PRIMARY KEY, name TEXT, board TEXT, exchange TEXT, "
                "status TEXT, first_seen TEXT, last_seen TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS stock_daily_snapshot ("
                "trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, board TEXT, "
                "exchange TEXT, pe_ttm REAL, pb REAL, total_mv REAL, "
                "PRIMARY KEY (trade_date, code))")
    con.execute("CREATE TABLE IF NOT EXISTS stock_indicators_daily ("
                "date TEXT, code TEXT, name TEXT, PRIMARY KEY (date, code))")
    con.execute("CREATE TABLE IF NOT EXISTS stock_org (code TEXT PRIMARY KEY, name TEXT, list_date TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS plate_members (board TEXT, code TEXT)")
    stocks = [("600519", "贵州茅台"), ("300750", "宁德时代"), ("835670", "数字人"),
              ("920000", "安徽凤凰"), ("000004", "国华退")]
    con.executemany("INSERT INTO a_shares_universe (code,name,board,exchange,status) "
                    "VALUES (?,?,?,?,?)", [(c, n, "x", "y", "listed") for c, n in stocks])
    con.executemany("INSERT INTO stock_names (code,name,market,source,updated_at) "
                    "VALUES (?,?,?,?,?)", [(c, n, "x", "tencent_snapshot", "2026-10-30")
                                           for c, n in stocks])
    con.executemany("INSERT INTO stock_daily_snapshot "
                    "(trade_date,code,name,board,exchange,pe_ttm,pb,total_mv) "
                    "VALUES ('2026-10-30',?,?,?,?,10.0,1.0,1e10)",
                    [(c, n, "x", "y") for c, n in stocks])
    con.executemany("INSERT INTO plate_members (board,code) VALUES ('白酒',?)",
                    [("600519",), ("300750",), ("835670",)])
    con.commit()
    SM.build(con, kline_db=[], as_of="2026-11-01T00:00:00")
    monkeypatch.setattr(V, "DB_PATH", db.path)
    rc = V.main([])
    out = capsys.readouterr().out
    assert "100.00%" in out
    assert rc == V.EXIT_OK == 0, "全量覆盖却没给 0：\n" + out
    assert "退出码 0（达标）" in out
    con.close()
    SM.reset_cache()


# ── 5. 剔北交所 = 产品选择，口径不许再写代码段白名单 ────────────────────────
def test_no_hardcoded_board_prefix_rule_left_in_consumers():
    """`c[0] in "036"` 这种白名单式判断必须在**代码**里绝迹（注释/文档字符串可提）。

    2026-11-01：holders_fetch / strategy_sim 两处各有一份，语义是"剔北交所"——
    那是产品选择，不是数据缺失；写成代码段白名单后，920 段一启用就静默偏离口径。

    用 AST 而不是 grep：注释和 docstring 里提到这条规则恰恰是我们想要的
    （后人要知道为什么删），用 grep 会把解释性文字一起判死。
    """
    import ast
    for rel in ("holders_fetch.py", "strategy_sim.py",
                os.path.join("tools", "fetch_a_shares_tencent.py"),
                os.path.join("tools", "intraday_snap.py"),
                os.path.join("viewer", "selection.py")):
        path = os.path.join(ROOT, rel)
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comp in node.comparators:
                # 形如 x in "036" / x[0] not in "03648"：长度为 2~8 的纯数字字符串常量
                if (isinstance(comp, ast.Constant) and isinstance(comp.value, str)
                        and comp.value.isdigit() and 2 <= len(comp.value) <= 8):
                    bad.append(node.lineno)
        assert not bad, "%s: 第 %s 行仍有代码段白名单，请改用 " \
                        "security_master.stock_codes(include_bse=False)" % (rel, sorted(set(bad)))


def test_strategy_sim_product_universe_from_master(db, monkeypatch):
    """strategy_sim 的"可选池"必须等于 stock_codes(include_bse=False)，一只不差。"""
    import strategy_sim as SS
    monkeypatch.setattr(SS, "DB", db.path)
    SS._PRODUCT_UNIVERSE_CACHE.clear()
    got = SS._product_universe()
    ref = set(SM.stock_codes(db.conn, include_bse=False))
    assert got == ref
    assert "600519" in got and "300750" in got
    assert "920000" not in got and "835670" not in got, "北交所是产品选择排除，不是漏"
    assert "111024" not in got and "513050" not in got, "非股票不该进标的池"
    SS._PRODUCT_UNIVERSE_CACHE.clear()


def test_strategy_sim_fails_closed_without_master(tmp_path, monkeypatch):
    """主表缺失时宁可 FATAL，也不产出"含北交所"的回测（那种污染在结果里看不出来）。"""
    import strategy_sim as SS
    p = tmp_path / "bare2.db"
    sqlite3.connect(str(p)).close()
    monkeypatch.setattr(SS, "DB", str(p))
    SS._PRODUCT_UNIVERSE_CACHE.clear()
    SM.reset_cache()
    with pytest.raises(SystemExit) as ei:
        SS._product_universe()
    assert "security_master" in str(ei.value)
    SS._PRODUCT_UNIVERSE_CACHE.clear()
    SM.reset_cache()


def test_strategy_sim_market_prefix_fixes_920():
    """新浪前缀表里 920 段会落到 "sz"（返回空数组、不报错）—— 与腾讯那个 bug 同型。"""
    import strategy_sim as SS
    assert SS._mkt_prefix("920000") == "bj"
    assert SS._mkt_prefix("835670") == "bj"
    assert SS._mkt_prefix("430047") == "bj"
    assert SS._mkt_prefix("600519") == "sh"
    assert SS._mkt_prefix("688981") == "sh"
    assert SS._mkt_prefix("300750") == "sz"
    assert SS._mkt_prefix("000004") == "sz"


def test_intraday_snap_prefix_fixes_920():
    """盘中快照的 `_qq`：920 段旧代码命中 "sh" 分支，腾讯返回空（静默无数据）。"""
    import intraday_snap as IS
    assert IS._qq("920000") == "bj920000"
    assert IS._qq("835670") == "bj835670"
    assert IS._qq("600519") == "sh600519"
    assert IS._qq("300750") == "sz300750"
    assert IS._qq("513050") == "sh513050"


def test_tencent_refuses_to_backfill_history():
    """`--date` 不是时光机：源声明属于 10-30，就别写进 10-29 的行。

    2026-11-01 实测：`--date 2026-10-29 --boards 北交所 --merge` 请求 348 只，
    348 只的 f[30] 全部是 20261030 —— 写进去就是"看着完整、实际全错"的历史快照。
    """
    import fetch_a_shares_tencent as T
    parsed = {c: {"_session": "20261030153209"} for c in ("920000", "920819", "920001")}
    assert T._session_mismatch(parsed, "2026-10-30") == {}, "同一场不该被拦"
    assert T._session_mismatch(parsed, "2026-10-29") == {"20261030": 3}
    assert T._session_mismatch(parsed, "2026-10-27") == {"20261030": 3}
    # 源没给 f[30] 时无法判定 -> 不算不匹配（不因为缺字段就把整批判死）
    assert T._session_mismatch({"920000": {"_session": ""}}, "2026-10-29") == {}


def test_verify_fetch_history_date_marks_no_source(db, monkeypatch, capsys, tmp_path):
    """历史日的缺口必须标 no_source（源无历史行情），与 no_trade / 漏抓区分开。"""
    import json as _json
    import verify_fetch as V
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    (snap_dir / "backfill_missing_2026-10-29.json").write_text(
        _json.dumps({"date": "2026-10-29", "verdict": "not_backfillable",
                     "reason": "no_source", "reason_zh": "实时接口无历史"}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(V, "DB_PATH", db.path)
    monkeypatch.setattr(V, "SNAP_DIR", str(snap_dir))
    rc = V.main(["--date", "2026-10-29", "--recent", "0"])
    out = capsys.readouterr().out
    assert rc == V.EXIT_GAP          # 覆盖率 0% < 99%，该红还是红（历史日的缺口是真的）
    assert "no_source" in out
    assert "backfill_missing_2026-10-29.json" in out


def test_strategy_sim_product_universe_not_called_at_import():
    """服务启动链 server → predictor → strategy_sim 是模块级 import。

    所以 fail-closed（主表缺失就 SystemExit）只能落在**运行期**，
    绝不能在导入期触发，否则主表一缺整个服务起不来。
    """
    import ast
    import io
    path = os.path.join(ROOT, "strategy_sim.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_product_universe":
                raise AssertionError("strategy_sim 在模块级（L%d）调用了 _product_universe"
                                     % sub.lineno)


def test_holders_fetch_secucode_delegates_to_master():
    """东财 secucode 后缀：920 段必须 .BJ（旧前缀表靠 "92" 恰好命中，理由错）。"""
    import holders_fetch as HF
    assert HF._secucode("600519") == "600519.SH"
    assert HF._secucode("688981") == "688981.SH"
    assert HF._secucode("689009") == "689009.SH"     # CDR
    assert HF._secucode("300750") == "300750.SZ"
    assert HF._secucode("000004") == "000004.SZ"
    assert HF._secucode("920000") == "920000.BJ"
    assert HF._secucode("835670") == "835670.BJ"
    assert HF._secucode("430047") == "430047.BJ"
    assert HF._secucode("111024") == "111024.SH"     # 沪市可转债
    assert HF._secucode("128136") == "128136.SZ"     # 深市可转债
    assert HF._secucode("513050") == "513050.SH"     # 沪市 ETF


def test_holders_fetch_excludes_bse_via_master(db, monkeypatch):
    """股东数据抓取（覆盖优先档）剔北交所，走主表而不是首字符判断。"""
    import holders_fetch as HF
    import viewer.universe_filter as VUF
    HF._ensure_tables(db.conn)
    db.conn.execute("INSERT INTO stock_holder_num (symbol,end_date) VALUES ('600519','2026-09-30')")
    db.conn.commit()
    monkeypatch.setattr(VUF, "all_ashares",
                        lambda: ["600519", "300750", "920000", "835670", "111024"])
    monkeypatch.setattr(VUF, "get_exclusion_pool", lambda: (set(), {}, []))
    codes = HF.collect_codes(db.conn)
    assert "300750" in codes, "沪深股票必须还在候选里（覆盖优先档没坏）"
    assert "600519" not in codes, "已有股东户数记录的不该重复抓"
    for c in ("920000", "835670"):
        assert c not in codes, "北交所是产品选择排除（%s 不该进候选）" % c
    assert "111024" not in codes, "可转债不是股票"

