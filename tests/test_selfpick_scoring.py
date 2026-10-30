# -*- coding: utf-8 -*-
"""tests/test_selfpick_scoring.py —— 系统自选评分口径回归（QA 2026-10-30）

覆盖本次修复的两个核心口径：
  1. 博主维度永久下线：权重必须为 0，且**不参与分数**；
  2. 缺失维度不占权重：senti_n=0 才算缺失 → dims.senti 为 None、其余可用维度重归一化、
     coverage 落库；全池同值但 senti_n>0 属有效中性分，不得判为缺失。

运行：.venv/Scripts/python.exe -m pytest tests/test_selfpick_scoring.py -v
"""
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "viewer")):
    if p not in sys.path:
        sys.path.insert(0, p)

import selfpick as SP  # noqa: E402

# 真实学出的权重（evo_weight fit_date=2026-10-22，博主份额已并入其余三项）
W_LEARNED = {"blogger": 0.0, "strategy": 0.1574, "senti": 0.7593, "engine": 0.0833}
# 故意带 blogger 的历史快照权重（四维合计 1.0）
W_WITH_BLOGGER = {"blogger": 0.6, "strategy": 0.2, "senti": 0.1, "engine": 0.1}


def _cand(code, n_strat=3, senti_net=0.0, senti_n=1, engine="", blogger_w=0.0,
          n_blogger=0, lhb=0.0):
    return {"code": code, "name": code, "n_blogger": n_blogger, "blogger_w": blogger_w,
            "bloggers": "", "n_strat": n_strat, "strat_keys": "k1、k2",
            "strat_families": "", "senti_net": senti_net, "senti_n": senti_n,
            "engine_cross": engine, "lhb_bonus": lhb}


def _pool(senti_n=1, engine="a、b"):
    """5 只票：战法/舆情/主引擎三轴都不同，便于断言重归一化。"""
    return [
        _cand("000001", n_strat=1, senti_net=-20.0, senti_n=senti_n, engine=""),
        _cand("000002", n_strat=2, senti_net=-5.0, senti_n=senti_n, engine="a"),
        _cand("000003", n_strat=3, senti_net=0.0, senti_n=senti_n, engine="a、b"),
        _cand("000004", n_strat=4, senti_net=8.0, senti_n=senti_n, engine="a、b、c"),
        _cand("000005", n_strat=5, senti_net=20.0, senti_n=senti_n, engine="a、b、c、d"),
    ]


def _by_code(rows):
    return {r["code"]: r for r in rows}


# ---------------------------------------------------------------- (a) 博主维度
def test_blogger_weight_zero_and_contributes_nothing():
    rows = _by_code(SP._score_pool(_pool(), W_LEARNED, "", []))
    for code, x in rows.items():
        assert x["used_w"]["blogger"] == 0.0, "博主维度必须权重 0：%s" % code
        assert x["nb"] == 50.0  # 池内 blogger_w 全 0 → 百分位 50，但权重 0 不进分
        expected = (W_LEARNED["strategy"] * x["ns"] + W_LEARNED["senti"] * x["nu"]
                    + W_LEARNED["engine"] * x["ne"])
        assert x["base"] == pytest.approx(expected, abs=0.06), (
            "base 必须等于三可用维加权（博主 0 权重不贡献）：%s" % code)


def test_blogger_values_do_not_change_score():
    """同样的票，只改 blogger_w / n_blogger，分数必须完全一致。"""
    base_rows = _by_code(SP._score_pool(_pool(), W_LEARNED, "", []))
    cands = _pool()
    for i, c in enumerate(cands):
        c["blogger_w"] = 10.0 * (i + 1)
        c["n_blogger"] = 3
    rows = _by_code(SP._score_pool(cands, W_LEARNED, "", []))
    for code in base_rows:
        assert rows[code]["base"] == base_rows[code]["base"], code
        assert rows[code]["score"] == base_rows[code]["score"], code


def test_blogger_share_renormalized_when_weight_carries_blogger():
    """传入带 blogger=0.6 的旧权重时，博主份额不得被计入分数（三维护重归一化）。"""
    rows = _by_code(SP._score_pool(_pool(), W_WITH_BLOGGER, "", []))
    avail_total = W_WITH_BLOGGER["strategy"] + W_WITH_BLOGGER["senti"] + W_WITH_BLOGGER["engine"]
    for code, x in rows.items():
        expected = (x["ns"] * W_WITH_BLOGGER["strategy"] / avail_total
                    + x["nu"] * W_WITH_BLOGGER["senti"] / avail_total
                    + x["ne"] * W_WITH_BLOGGER["engine"] / avail_total)
        assert x["base"] == pytest.approx(expected, abs=0.06), code
        assert sum(x["used_w"].values()) == pytest.approx(1.0, abs=1e-6), code


# ---------------------------------------------------------------- (b) 缺失舆情
def test_senti_missing_is_none_and_dimension_weights_renormalized():
    rows = _by_code(SP._score_pool(_pool(senti_n=0), W_LEARNED, "", []))
    avail_total = W_LEARNED["strategy"] + W_LEARNED["engine"]
    for code, x in rows.items():
        assert x["nu"] is None, "senti_n=0 必须判为缺失（None）：%s" % code
        assert x["used_w"]["senti"] == 0.0, code
        expected = (x["ns"] * W_LEARNED["strategy"] / avail_total
                    + x["ne"] * W_LEARNED["engine"] / avail_total)
        assert x["base"] == pytest.approx(expected, abs=0.06), (
            "缺失维度不占权重，其余可用维度必须重归一化：%s" % code)


def test_senti_missing_coverage_equals_available_weight_share():
    rows = _by_code(SP._score_pool(_pool(senti_n=0), W_LEARNED, "", []))
    expect = (W_LEARNED["strategy"] + W_LEARNED["engine"]) / sum(
        W_LEARNED[k] for k in ("strategy", "senti", "engine"))
    for code, x in rows.items():
        assert x["coverage"] < 1.0, code
        assert x["coverage"] == pytest.approx(expect, abs=1e-4), code
        assert sum(x["used_w"].values()) == pytest.approx(1.0, abs=1e-6), code


def test_senti_present_coverage_is_one():
    rows = SP._score_pool(_pool(senti_n=2), W_LEARNED, "", [])
    for x in rows:
        assert x["nu"] is not None
        assert x["coverage"] == pytest.approx(1.0, abs=1e-9), x["code"]
        assert x["used_w"]["senti"] == pytest.approx(W_LEARNED["senti"], abs=1e-9)


# ---------------------------------------------------------------- (d) 全池同值
def test_same_senti_value_but_present_is_not_treated_as_missing():
    """全池 senti_net 同值 → 百分位 50（有效中性分），senti_n>0 时绝不能判缺失。"""
    cands = _pool(senti_n=3)
    for c in cands:
        c["senti_net"] = 0.0
    rows = _by_code(SP._score_pool(cands, W_LEARNED, "", []))
    for code, x in rows.items():
        assert x["nu"] == 50.0, "全池同值应为中性 50 分：%s" % code
        assert x["coverage"] == pytest.approx(1.0, abs=1e-9), code
        assert x["used_w"]["senti"] > 0, code


def test_mixed_pool_only_missing_rows_lose_coverage():
    """同池里部分票缺舆情：缺的降 coverage，有的保持 1.0，互不污染。"""
    cands = _pool(senti_n=1)
    cands[0]["senti_n"] = 0
    rows = _by_code(SP._score_pool(cands, W_LEARNED, "", []))
    assert rows["000001"]["coverage"] < 1.0
    assert rows["000001"]["nu"] is None
    for code in ("000002", "000003", "000004", "000005"):
        assert rows[code]["coverage"] == pytest.approx(1.0, abs=1e-9), code


# ---------------------------------------------------------------- 规则加成 / 排序
def test_rule_bonus_does_not_bypass_available_dimension_scoring():
    rules = [{"rid": "r1", "cond": {"strat": SP.E._bucket_strat(3)},
              "text": "测试规则", "n": 30, "avg_ex": 1.2, "lift": 1.1, "oos": 0.8}]
    rows = SP._score_pool(_pool(), W_LEARNED, "", rules)
    for x in rows:
        assert x["score"] == pytest.approx(min(100.0, x["base"] + x["bonus"]), abs=1e-6)


# ---------------------------------------------------------------- 证据文本
def test_evidence_renders_missing_senti_without_crash():
    rows = SP._score_pool(_pool(senti_n=0), W_LEARNED, "", [])
    ev = SP._evidence(rows[0], W_LEARNED, False)
    txt = " ".join(ev)
    assert "缺失" in txt
    assert "coverage" in txt, "打分证据必须披露 coverage：%s" % txt


# ---------------------------------------------------------------- (e) _env_weight
def test_env_weight_four_dims_sum_one_blogger_zero_live_db():
    db = os.path.join(ROOT, "viewer", "app.db")
    if not os.path.exists(db):
        pytest.skip("app.db 不存在")
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    try:
        w, mkt, src, snap = SP._env_weight(con)
    finally:
        con.close()
    assert set(w) >= {"blogger", "strategy", "senti", "engine"}
    assert w["blogger"] == 0.0
    assert sum(w[k] for k in ("blogger", "strategy", "senti", "engine")) == \
        pytest.approx(1.0, abs=1e-9)
    assert all(w[k] >= 0.0 for k in ("blogger", "strategy", "senti", "engine"))
    assert src
    js = json.loads(snap)
    assert js["w"]["blogger"] == 0.0
    assert sum(js["w"][k] for k in ("strategy", "senti", "engine")) == \
        pytest.approx(1.0, abs=1e-9)


def _mem_con(with_state, with_bystate):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE evo_weight(fit_date TEXT, w_blogger REAL, w_strategy REAL,
                                w_senti REAL, w_engine REAL, ic_json TEXT, note TEXT,
                                by_state_json TEXT);
        CREATE TABLE market_state_log(trade_date TEXT, state TEXT);
    """)
    con.execute("INSERT INTO evo_weight VALUES(?,?,?,?,?,?,?,?)",
                ("2026-10-22", 0.136, 0.136, 0.656, 0.072, "{}", "", None))
    if with_state:
        con.execute("INSERT INTO market_state_log VALUES(?,?)", ("2026-10-29", "普跌"))
    if with_bystate:
        # 历史环境快照仍带 blogger，且三维合计 0.864（删博主但没重归一化的历史脏数据）
        con.execute("UPDATE evo_weight SET by_state_json=?",
                    (json.dumps({"普跌": {"w": {"blogger": 0.136, "strategy": 0.136,
                                                "senti": 0.656, "engine": 0.072}}}),))
    con.commit()
    return con


def test_env_weight_historical_bystate_with_blogger_is_normalized():
    for with_state in (True, False):
        for with_bystate in (True, False):
            con = _mem_con(with_state, with_bystate)
            try:
                w, mkt, src, snap = SP._env_weight(con)
            finally:
                con.close()
            tag = "state=%s by_state=%s" % (with_state, with_bystate)
            assert w["blogger"] == 0.0, tag
            assert sum(w[k] for k in ("blogger", "strategy", "senti", "engine")) == \
                pytest.approx(1.0, abs=1e-9), tag
            assert w["senti"] == pytest.approx(0.656 / 0.864, abs=1e-4), tag


def test_env_weight_all_zero_weights_falls_back_to_default_three_dims():
    con = _mem_con(False, False)
    try:
        con.execute("UPDATE evo_weight SET w_strategy=0, w_senti=0, w_engine=0")
        con.commit()
        w, _mkt, _src, _snap = SP._env_weight(con)
    finally:
        con.close()
    assert w["blogger"] == 0.0
    assert sum(w[k] for k in ("blogger", "strategy", "senti", "engine")) == \
        pytest.approx(1.0, abs=1e-9)
    # 默认权重去博主后 strategy:senti:engine = 34:14:18 → 归一化
    assert w["strategy"] == pytest.approx(34 / 66.0, abs=1e-4)
    assert w["senti"] == pytest.approx(14 / 66.0, abs=1e-4)
    assert w["engine"] == pytest.approx(18 / 66.0, abs=1e-4)


# ---------------------------------------------------------------- 回算不得覆盖实盘
def _mem_self_pick():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    SP.ensure_tables(con)
    return con


def test_hist_rows_never_overwrite_live_rows():
    con = _mem_self_pick()
    rows = SP._score_pool(_pool(), W_LEARNED, "", [])
    wsnap = json.dumps({"w": W_LEARNED})
    # 先落 1 条实盘行（同日期同 kind 同 code）
    pick_date = "2026-10-29"
    SP._write_rows(con, pick_date, "main", rows[:1], W_LEARNED, wsnap, "", False)
    live = dict(con.execute("SELECT * FROM self_pick WHERE pick_date=? AND code=?",
                            (pick_date, rows[0]["code"])).fetchone())
    assert live["is_hist"] == 0
    # 再用历史回算写同一只票
    n = SP._write_rows(con, pick_date, "main", rows[:1], W_LEARNED, wsnap, "", True)
    assert n == 0, "同日实盘行已存在时，历史回算必须跳过"
    after = dict(con.execute("SELECT * FROM self_pick WHERE pick_date=? AND code=?",
                             (pick_date, rows[0]["code"])).fetchone())
    assert after["is_hist"] == 0
    assert after["score"] == live["score"]
    assert after["built_at"] == live["built_at"], "实盘行内容不得被回算改写"
    # 另一只票（无实盘行）可以正常写入历史行
    n2 = SP._write_rows(con, pick_date, "main", rows[1:2], W_LEARNED, wsnap, "", True)
    assert n2 == 1
    hist = con.execute("SELECT is_hist, dims_json FROM self_pick WHERE code=?",
                       (rows[1]["code"],)).fetchone()
    assert hist["is_hist"] == 1
    d = json.loads(hist["dims_json"])
    assert "coverage" in d and d["coverage"] == pytest.approx(1.0, abs=1e-9)
    con.close()


def test_hist_rewrite_replaces_previous_hist_rows():
    con = _mem_self_pick()
    rows = SP._score_pool(_pool(), W_LEARNED, "", [])
    wsnap = json.dumps({"w": W_LEARNED})
    SP._write_rows(con, "2026-10-20", "main", rows[:2], W_LEARNED, wsnap, "", True)
    SP._write_rows(con, "2026-10-20", "main", rows[:2], W_LEARNED, wsnap, "", True)
    n = con.execute("SELECT COUNT(*) FROM self_pick WHERE pick_date='2026-10-20'").fetchone()[0]
    assert n == 2, "同一历史日期重复回算应按 (pick_date,kind,code) 覆盖而不是堆叠"
    con.close()
