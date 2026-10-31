# -*- coding: utf-8 -*-
"""决策⑤：成本校准位（2026-10-31）。

这个模块最容易出的错不是"算错了"，而是**越权**：悄悄把建议值写回 COST_TIERS_BP、
或者在样本不足时照样给一个看起来很像结论的数字。两者都被下面的断言挡住。
"""
import os
import sqlite3

import pytest

from viewer import cost_calib as C
from viewer.kernel import params as P

_KL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "kline_daily.db")
need_kline = pytest.mark.skipif(not os.path.exists(_KL), reason="kline_daily.db 不存在")


def _mem():
    return sqlite3.connect(":memory:")


def _bench(code, d):
    con = sqlite3.connect(_KL)
    try:
        h, l, c = con.execute("SELECT h,l,c FROM kline WHERE code=? AND d=?",
                              (code, d)).fetchone()
        return (float(h) + float(l) + float(c)) / 3.0
    finally:
        con.close()


def _recent(code="000001"):
    con = sqlite3.connect(_KL)
    try:
        return con.execute("SELECT MAX(d) FROM kline WHERE code=?", (code,)).fetchone()[0]
    finally:
        con.close()


# ── 表与幂等 ──────────────────────────────────────────────────────────────
def test_ensure_table_idempotent():
    con = _mem()
    C.ensure_table(con)
    C.ensure_table(con)
    n = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_fill'").fetchone()[0]
    assert n == 1


@need_kline
def test_import_is_idempotent(tmp_path):
    code, d = "000001", _recent()
    bench = _bench(code, d)
    p = tmp_path / "t.csv"
    p.write_text("date,code,side,qty,price\n%d,%s,buy,1000,%.4f\n" % (d, code, bench * 0.99),
                 encoding="utf-8")
    con = _mem()
    r1 = C.import_csv(con, str(p))
    r2 = C.import_csv(con, str(p))
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0 and r2["skipped_dup"] == 1
    assert con.execute("SELECT COUNT(*) FROM trade_fill").fetchone()[0] == 1


@need_kline
def test_chinese_headers_and_side_alias(tmp_path):
    code, d = "000001", _recent()
    bench = _bench(code, d)
    p = tmp_path / "t.csv"
    p.write_text("日期,证券代码,买卖,数量,成交均价\n%d,%s,买入,1000,%.4f\n" % (d, code, bench),
                 encoding="utf-8-sig")
    con = _mem()
    r = C.import_csv(con, str(p))
    assert r["ok"] and r["inserted"] == 1
    assert con.execute("SELECT side FROM trade_fill").fetchone()[0] == "buy"


# ── 符号（买高吃亏为正、卖高有利为负）──────────────────────────────────────
@need_kline
def test_cost_sign_buy_above_bench_is_positive(tmp_path):
    code, d = "000001", _recent()
    bench = _bench(code, d)
    p = tmp_path / "t.csv"
    p.write_text("date,code,side,qty,price\n%d,%s,buy,1000,%.4f\n" % (d, code, bench * 1.02),
                 encoding="utf-8")
    con = _mem()
    C.import_csv(con, str(p))
    cost = con.execute("SELECT exec_cost_bp FROM trade_fill").fetchone()[0]
    assert 150 < cost < 250, "买在典型价上方 2%% 应约 +200bp，实际 %r" % cost


@need_kline
def test_cost_sign_sell_above_bench_is_negative(tmp_path):
    code, d = "000001", _recent()
    bench = _bench(code, d)
    p = tmp_path / "t.csv"
    p.write_text("date,code,side,qty,price\n%d,%s,sell,1000,%.4f\n" % (d, code, bench * 1.02),
                 encoding="utf-8")
    con = _mem()
    C.import_csv(con, str(p))
    cost = con.execute("SELECT exec_cost_bp FROM trade_fill").fetchone()[0]
    assert -250 < cost < -150, "卖在典型价上方应记为负成本（有利），实际 %r" % cost


# ── 不足样本不许给建议 ────────────────────────────────────────────────────
def test_not_ready_gives_no_suggestion():
    con = _mem()
    C.ensure_table(con)
    con.execute("INSERT INTO trade_fill (d,code,side,qty,price,adv,amount,exec_cost_bp)"
                " VALUES (20260101,'000001','buy',100,10,1e9,1000,12.0)")
    con.commit()
    s = C.stats(con)
    assert s["ready"] is False
    assert s["need_more"] == P.COST_CALIB_MIN_TRADES - 1
    assert all(t["suggested_bp"] is None for t in s["tiers"])
    assert "不构成建议" in s["note"]


def test_ready_when_enough_samples():
    con = _mem()
    C.ensure_table(con)
    for i in range(P.COST_CALIB_MIN_TRADES):
        con.execute("INSERT INTO trade_fill (d,code,side,qty,price,adv,amount,exec_cost_bp)"
                    " VALUES (20260101,?, 'buy',100,10,1e9,1000,?)",
                    ("%06d" % i, 5.0 + i * 0.1))
    con.commit()
    s = C.stats(con)
    assert s["ready"] is True
    assert s["tiers"][0]["ready"] is True
    assert s["tiers"][0]["suggested_bp"] is not None


def test_suggested_is_p75_rounded_up_to_5():
    con = _mem()
    C.ensure_table(con)
    for i in range(10):
        con.execute("INSERT INTO trade_fill (d,code,side,qty,price,adv,amount,exec_cost_bp)"
                    " VALUES (20260101,?, 'buy',100,10,1e9,1000,?)", ("%06d" % i, float(i)))
    con.commit()
    t = C.stats(con)["tiers"][0]
    assert t["median_bp"] == 4.5
    assert t["p75_bp"] > t["median_bp"], "p75 必须高于中位"
    assert t["suggested_bp"] % 5 == 0, "建议值向上取整到 5 的倍数"


# ── 越权检查：绝不写回参数 ────────────────────────────────────────────────
def test_never_writes_params():
    before = tuple(P.COST_TIERS_BP)
    con = _mem()
    C.ensure_table(con)
    for i in range(40):
        con.execute("INSERT INTO trade_fill (d,code,side,qty,price,adv,amount,exec_cost_bp)"
                    " VALUES (20260101,?, 'buy',100,10,1e9,1000,?)", ("%06d" % i, float(i)))
    con.commit()
    C.stats(con)
    assert tuple(P.COST_TIERS_BP) == before, "校准只出建议，不许改参数"


def test_describe_says_no_write():
    d = C.describe()
    assert d["writes_params"] is False
    assert d["current_tiers"] == [list(x) for x in P.COST_TIERS_BP]


# ── 口径限制必须随数据一起返回 ────────────────────────────────────────────
def test_caveats_always_returned():
    s = C.stats(_mem())
    assert len(s["caveats"]) >= 3
    joined = " ".join(s["caveats"])
    assert "不等于冲击成本" in joined
    assert "OHLC" in joined
