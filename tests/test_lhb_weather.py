# -*- coding: utf-8 -*-
"""tests/test_lhb_weather.py —— 龙虎榜「资金风向」指标（D35）回归

重点**不是覆盖率，是前视**。这个模块最容易错的地方只有一个：`d1_chg` 是「上榜**次日**涨跌幅」，
算 d 日的赚钱效应只能用 `trade_date <= d-1` 的样本。10-29 真实数据里当日 `d1_chg` 全为 NULL
就是证据 —— 那天还没过完，收益还没发生。

全部跑在 `tmp_path` 的临时 sqlite 上：
  · `compute(con=tmp, market=fake)` —— `market` 是 D35 加的测试注入口（默认 None 时生产会去
    扫 `data/kline_daily.db` 的 1382 万行，单测不可能每条都等 4 分钟）；
  · **不碰生产 `viewer/app.db`**（`_PROD_GUARD` 在下面做显式断言）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import lhb_weather as W  # noqa: E402

_PROD_DB = os.path.abspath(W.DB)          # 快照，防止 monkeypatch 之后比错对象
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
        "2026-10-16", "2026-10-19", "2026-10-20"]


# ---------------------------------------------------------------- 造库
def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE lhb_stocks(trade_date TEXT, security_code TEXT,
                            net_amt REAL, d1_chg REAL);
    CREATE TABLE lhb_inst(trade_date TEXT, security_code TEXT, net_buy_amt REAL);
    CREATE TABLE lhb_depts(trade_date TEXT, operate_name TEXT, net_amt REAL);
    CREATE TABLE lhb_sector_focus(trade_date TEXT, sector TEXT, net_amt REAL);
    CREATE TABLE stock_indicators_daily(date TEXT, code TEXT, close REAL);
    CREATE TABLE stock_daily_snapshot(trade_date TEXT, code TEXT, close REAL);
    CREATE TABLE market_regime(trade_date TEXT PRIMARY KEY, regime TEXT);
    """)


def _mk(tmp_path, name="app.db") -> sqlite3.Connection:
    con = sqlite3.connect(str(tmp_path / name))
    _schema(con)
    return con


def _lhb(con, day, rows) -> None:
    """rows: [(code, net_amt, d1_chg), ...]"""
    con.executemany("INSERT INTO lhb_stocks VALUES(?,?,?,?)",
                    [(day, c, a, d) for c, a, d in rows])


def _market(days) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": list(days),
                         "market_amount": [1.0e12] * len(days),
                         "r_mean": [0.001] * len(days)})


def _seed_min(con, days) -> None:
    """每天 3 只上榜、结构/集中度各 1 行、指标层 100 只（分母 = 100）。"""
    for i, d in enumerate(days):
        _lhb(con, d, [("000001", 1.0e8 * (i + 1), 1.0 * i),
                      ("000002", 2.0e8 * (i + 1), -0.5 * i),
                      ("600000", 3.0e8 * (i + 1), None if i == len(days) - 1 else 0.3 * i)])
        con.execute("INSERT INTO lhb_inst VALUES(?,?,?)", (d, "000001", 5.0e7 * (i + 1)))
        con.execute("INSERT INTO lhb_depts VALUES(?,?,?)", (d, "某游资营业部", 1.0e7 * (i + 1)))
        con.execute("INSERT INTO lhb_sector_focus VALUES(?,?,?)", (d, "半导体", 1.0e8 * (i + 1)))
        con.executemany("INSERT INTO stock_indicators_daily VALUES(?,?,?)",
                        [(d, "9%05d" % k, 10.0) for k in range(100)])
    con.commit()


# ---------------------------------------------------------------- ① 前视：lag 1
def test_d1_chg_is_lagged_one_trading_day(tmp_path):
    """d 日的赚钱效应**不得**包含 d 日当天的 `d1_chg`（那条收益还没发生）。

    造一个极端反差：d1 的 d1_chg = [10, 20]（均值 15），d2 的 d1_chg = [1000]。
    · 若**有** lag：d2 的 d1_mean_20 = 15        （只用 d1 的样本）
    · 若**没有** lag：d2 的 d1_mean_20 = 343.333  （把 d2 自己的 1000 也算进去）
    两者差 22 倍，任何"忘了 shift"的实现都会当场红。
    """
    con = _mk(tmp_path)
    d1, d2, d3 = DAYS[0], DAYS[1], DAYS[2]
    _lhb(con, d1, [("000001", 1.0e8, 10.0), ("000002", 2.0e8, 20.0)])
    _lhb(con, d2, [("000003", 3.0e8, 1000.0)])
    _lhb(con, d3, [("000004", 4.0e8, None)])
    con.commit()

    df = W.compute(con=con, verbose=False, market=_market([d1, d2, d3]))
    got = dict(zip(df["trade_date"], df["d1_mean_20"]))

    assert pd.isna(got[d1]), "第一天没有历史样本，d1_mean_20 必须是 NaN（不猜）"
    assert got[d2] == pytest.approx(15.0), \
        "d2 的赚钱效应含了当日 d1_chg（前视！）：期望 15.0（只用 d1），实际 %.4f" % got[d2]
    assert got[d2] != pytest.approx(343.333, abs=1e-3)
    # d3 才允许看到 d2 那批样本
    assert got[d3] == pytest.approx((10.0 + 20.0 + 1000.0) / 3.0), \
        "d3 应当已经能用上 d2 的样本（lag 是 1 天，不是 2 天）"


def test_d1_win_rate_also_lagged(tmp_path):
    """胜率同样必须 lag：d2 的 d1_win_20 只看 d1 的两条（都 >0 → 1.0）。"""
    con = _mk(tmp_path)
    d1, d2 = DAYS[0], DAYS[1]
    _lhb(con, d1, [("000001", 1.0e8, 10.0), ("000002", 2.0e8, 5.0)])
    _lhb(con, d2, [("000003", 3.0e8, -99.0)])       # 当日巨亏，若参与则胜率变 2/3
    con.commit()
    df = W.compute(con=con, verbose=False, market=_market([d1, d2]))
    got = dict(zip(df["trade_date"], df["d1_win_20"]))
    assert got[d2] == pytest.approx(1.0), "d2 的胜率含了当日的 -99（前视）：实际 %.4f" % got[d2]


# ---------------------------------------------------------------- ② asof 截断
def test_asof_truncation_does_not_change_earlier_rows(tmp_path):
    """截断等价性：只喂到 d 的数据，d 那一行必须和喂全量时**逐字段相同**。

    这就是 `asof=d` 的语义（`compute` 没有 asof 参数，用"截断数据源"表达更严格：
    它证明的是输出对未来的**完全不依赖**，而不只是"不读某张表"）。
    字段里刻意排除 `sample_days_*` —— 那是全库统计量、天然随数据量变。
    """
    con_full = _mk(tmp_path, "full.db")
    _seed_min(con_full, DAYS)
    df_full = W.compute(con=con_full, verbose=False, market=_market(DAYS))

    cut = DAYS[:4]
    con_cut = _mk(tmp_path, "cut.db")
    _seed_min(con_cut, cut)
    df_cut = W.compute(con=con_cut, verbose=False, market=_market(cut))

    cols = ["trade_date", "n_listed", "denom", "denom_src", "list_ratio", "net_amt",
            "market_amount", "net_amt_ratio", "part_pct", "d1_mean_20", "d1_win_20",
            "effect_pct", "score", "q33", "q67", "state", "inst_net", "hot_net",
            "inst_share", "top3_share", "hhi"]
    a = df_full.head(4)[cols].reset_index(drop=True)
    b = df_cut[cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=0, atol=0)


# ---------------------------------------------------------------- ③ 短样本不定档
def test_short_sample_dims_never_change_state(tmp_path):
    """结构/集中度样本只有几天（< MIN_SAMPLE_FOR_STATE）时，**不许影响 `state`**。

    做法：同一份 `lhb_stocks`，把结构与集中度换成数值完全相反的两版，断言 `score`/`state`
    逐行相同 —— 这是"维度没进合成"的可执行证明，比断言"代码里没引用"强。
    """
    assert W.MIN_SAMPLE_FOR_STATE == 250

    con_a = _mk(tmp_path, "a.db")
    _seed_min(con_a, DAYS)
    df_a = W.compute(con=con_a, verbose=False, market=_market(DAYS))

    con_b = _mk(tmp_path, "b.db")
    for i, d in enumerate(DAYS):
        _lhb(con_b, d, [("000001", 1.0e8 * (i + 1), 1.0 * i),
                        ("000002", 2.0e8 * (i + 1), -0.5 * i),
                        ("600000", 3.0e8 * (i + 1), None if i == len(DAYS) - 1 else 0.3 * i)])
        # 结构与集中度：符号、量级全反过来
        con_b.execute("INSERT INTO lhb_inst VALUES(?,?,?)", (d, "000001", -9.0e9 * (i + 1)))
        con_b.execute("INSERT INTO lhb_depts VALUES(?,?,?)", (d, "机构专用", 9.0e9 * (i + 1)))
        con_b.execute("INSERT INTO lhb_sector_focus VALUES(?,?,?)", (d, "银行", -9.0e8 * (i + 1)))
        con_b.executemany("INSERT INTO stock_indicators_daily VALUES(?,?,?)",
                          [(d, "9%05d" % k, 10.0) for k in range(100)])
    con_b.commit()
    df_b = W.compute(con=con_b, verbose=False, market=_market(DAYS))

    assert df_a["score"].equals(df_b["score"]), "结构/集中度影响了 score（它们不该进合成）"
    assert df_a["state"].tolist() == df_b["state"].tolist(), "短样本维度影响了档位"
    # 如实记录样本天数（供以后升级判断）
    assert int(df_a["sample_days_structure"].iloc[0]) == len(DAYS)
    assert int(df_a["sample_days_sector"].iloc[0]) == len(DAYS)
    assert int(df_a["sample_days_structure"].iloc[0]) < W.MIN_SAMPLE_FOR_STATE
    # 结构/集中度本身仍然要算出来（只展示）
    assert df_a["inst_net"].notna().any() and df_a["inst_share"].notna().any()
    assert df_a["top3_share"].notna().any() and df_a["hhi"].notna().any()


# ---------------------------------------------------------------- ④ 分母降级链
def test_denominator_fallback_chain_is_explicit(tmp_path):
    """`list_ratio` 分母：indicators → snapshot → 5000 兜底，且 `denom_src` 必须如实标注。"""
    # ① indicators 在 → 用 indicators，100 只
    con = _mk(tmp_path, "ind.db")
    _seed_min(con, DAYS[:1])
    df = W.compute(con=con, verbose=False, market=_market(DAYS[:1]))
    assert df["denom_src"].iloc[0] == "indicators"
    assert int(df["denom"].iloc[0]) == 100

    # ② indicators 没数据、snapshot 有 60 只 → 落 snapshot
    con2 = _mk(tmp_path, "snap.db")
    _lhb(con2, DAYS[0], [("000001", 1.0e8, 1.0)])
    con2.executemany("INSERT INTO stock_daily_snapshot VALUES(?,?,?)",
                     [(DAYS[0], "8%05d" % k, 5.0) for k in range(60)])
    con2.commit()
    df2 = W.compute(con=con2, verbose=False, market=_market(DAYS[:1]))
    assert df2["denom_src"].iloc[0] == "snapshot"
    assert int(df2["denom"].iloc[0]) == 60

    # ③ 两个源都没有 → 兜底 5000，且必须标出来（不许静默）
    con3 = _mk(tmp_path, "none.db")
    _lhb(con3, DAYS[0], [("000001", 1.0e8, 1.0)])
    con3.commit()
    df3 = W.compute(con=con3, verbose=False, market=_market(DAYS[:1]))
    assert df3["denom_src"].iloc[0] == "fallback"
    assert int(df3["denom"].iloc[0]) == W.FALLBACK_DENOM
    assert df3["list_ratio"].iloc[0] == pytest.approx(1.0 / W.FALLBACK_DENOM)


# ---------------------------------------------------------------- ⑤ 落库边界
def test_save_writes_only_own_table(tmp_path):
    """`save()` 只写 `lhb_weather_daily`，**绝不碰 `market_regime`**（全项目回测基准口径）。"""
    con = _mk(tmp_path)
    _seed_min(con, DAYS)
    con.execute("INSERT INTO market_regime VALUES(?,?)", ("2026-10-29", "up"))
    con.commit()

    df = W.compute(con=con, verbose=False, market=_market(DAYS))
    n = W.save(con, df)
    assert n == len(DAYS)

    assert con.execute("SELECT COUNT(*) FROM lhb_weather_daily").fetchone()[0] == len(DAYS)
    assert con.execute("SELECT regime FROM market_regime").fetchall() == [("up",)], \
        "save() 动了 market_regime —— 等于让所有历史结论失效"
    got = con.execute("SELECT state, denom_src FROM lhb_weather_daily").fetchall()
    assert len(got) == len(DAYS)
    assert all(r[1] in ("indicators", "snapshot", "fallback") for r in got)
    assert all(r[0] in ("hot", "neutral", "cold", "unknown") for r in got)


# ---------------------------------------------------------------- ⑥ 生产守卫
def test_never_touches_prod_db():
    """本文件全程只用 tmp 库；这条守住"别人以后加用例时别忘了"。"""
    assert os.path.abspath(W.DB) == _PROD_DB, "W.DB 被改过，后续用例可能写到生产库"
