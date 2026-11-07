# -*- coding: utf-8 -*-
"""箱体标尺（static / blend / dyn）与历史快照的模式隔离 —— 2026-11-03 D71 的回归守卫。

## 为什么要有这个文件

D71 给中线主表加了一把**动态**尺子（布林 20 日 ±2σ 的 %B 分位），但硬纪律是
**默认 static、线上输出一个字都不许变**。这类"加了一条分支、默认走老路"的改动最容易
在半年后被某次重构悄悄改掉（比如有人把 `score_one` 的默认参数写成 blend，或者干脆
把两条路合成一条），而肉眼看榜单是看不出来的：分数只差几分、名次只挪几位。

所以这里钉四条断言，都是**口径断言**而不是实现断言：

1. `score_one` 默认走 static，且位置分与 D65 的旧公式逐位相等；
2. static 下 `parts` 的键集必须是 D65 那四个 —— 多一个键就会让落盘 json / 前端字段变化；
3. 动态字段算不出来（K 线不足 20 根、20 日一字横盘）时**整只票不许失效**，退回静态位置分；
4. `universe_box_hist` 的日变化只跟**同一把尺**的历史比 —— 老库（没有 box_mode 列）
   一律按 static 认，切了 blend 之后不许拿静态历史算出假跳变。

第 4 条是这次改动唯一有真实故障模式的地方（跨模式相减 = 全表突然集体涨跌），
所以它用一个临时 sqlite 库从"老表结构"开始建，把 ALTER 补列那一步也一起测了。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import universe_box as ub  # noqa: E402


# ---------------------------------------------------------------- 构造数据
def _bars(n: int, base: float = 10.0, amp: float = 1.0) -> list:
    """造 n 根日线：三角波 + 缓慢抬升，保证 CV/斜率/位置都落在门槛内。

    元组口径是 `scan_one` 认的 `(d, o, c, h, l, v)`（close 在第 3 位）。
    成交额门槛要求 close×v ≥ 3000 万，所以 v 给 1e7 量级。
    """
    out = []
    for i in range(n):
        c = base + amp * ((i % 40) / 40.0) + 0.0002 * i
        out.append(("2026-%02d-%02d" % (1 + i // 28 % 12, 1 + i % 28),
                    c, c, c * 1.02, c * 0.98, 2.0e7))
    return out


def _row(code: str, score: float, pos: float = 0.1, pctb_rank: float | None = 20.0) -> dict:
    return {"code": code, "name": code, "score": score, "pos": pos,
            "dwell_ratio": 0.6, "dwell_max_run": 40, "pct750": 12.0,
            "current": 10.0, "rank": 1, "boll_pctb": 0.2,
            "boll_pctb_rank": pctb_rank, "box_mode": "static"}


# ---------------------------------------------------------------- ① 默认值纪律
def test_default_mode_is_static(monkeypatch):
    """不设环境变量时，模块默认模式必须是 static。

    **必须强制清空环境变量**（2026-11-03 QA 指出）：原来写的是
    `if not os.environ.get("UNIVERSE_BOX_MODE")` 守卫 —— 在一台设了该变量的机器上，
    这条断言会**静默跳过**，等于守卫把守卫自己关掉了。测试要能失败才有意义。

    这里用 `importlib.reload` 是有意的：`BOX_MODE` 是**导入时**读一次环境变量得到的常量，
    只清空 `os.environ` 不会改变已经算好的值。要断言"没设变量时默认是 static"，
    就必须在"没设变量"的状态下真的重新导入一次。reload 是原地改这个模块对象，
    其它测试持有的引用不受影响。
    """
    import importlib
    monkeypatch.delenv("UNIVERSE_BOX_MODE", raising=False)
    mod = importlib.reload(ub)
    try:
        assert mod.BOX_MODE_STATIC == "static"
        assert mod.BOX_MODES == ("static", "blend", "dyn")
        assert mod.BOX_MODE == "static"
        # 反向确认这条断言不是恒真的：设成 blend 再导入，必须读到 blend
        monkeypatch.setenv("UNIVERSE_BOX_MODE", "blend")
        assert importlib.reload(ub).BOX_MODE == "blend"
    finally:
        monkeypatch.delenv("UNIVERSE_BOX_MODE", raising=False)
        importlib.reload(ub)          # 复位成默认状态，别把 blend 漏给后面的测试


def test_unknown_mode_falls_back_to_static():
    """模式串写错一个字母（blended / DYNN / 空串）不许换尺，一律退回 static。"""
    assert ub._norm_mode("blended") == "static"
    assert ub._norm_mode("") == "static"
    assert ub._norm_mode(None) == "static"
    assert ub._norm_mode("BLEND") == "blend"
    assert ub._norm_mode(" dyn ") == "dyn"


def test_weights_and_gates_untouched():
    """权重与门槛是这次改动的"不许动"清单 —— 数值漂了就是改判据，不是换尺子。"""
    assert (ub.W_POS, ub.W_DWELL, ub.W_REGULAR, ub.W_TOUCH) == (0.30, 0.35, 0.20, 0.15)
    assert ub.MIN_AMOUNT_AVG == 3.0e7
    assert (ub.PCT_MAX, ub.CV_MAX, ub.SLOPE_MAX) == (30.0, 40.0, 18.0)
    assert (ub.TOUCH_MIN, ub.BOX_WIN) == (2, 750)
    assert abs(ub.W_POS + ub.W_DWELL + ub.W_REGULAR + ub.W_TOUCH - 1.0) < 1e-12


# ---------------------------------------------------------------- ② 位置分口径
def test_static_score_matches_legacy_formula():
    """static 模式的位置分必须与 D65 那行公式逐位相等（不许因为加了分支而变）。"""
    m = {"pos": 0.12, "dwell_ratio": 0.55, "cv": 14.0, "slope_yr": 4.0,
         "touches": 9, "boll_pctb_rank": 8.0}
    got = ub.score_one(m, box_mode="static")
    want_pos = max(0.0, min(1.0, 1.0 - 0.12 / (1.0 / 3.0))) * 100.0
    assert got["parts"]["pos"] == pytest.approx(round(want_pos, 1))
    # parts 的键集必须与 D65 完全一致（多一个键就会让落盘 json / 前端字段变）
    assert set(got["parts"].keys()) == {"pos", "dwell", "regular", "touch"}
    assert got["box_mode"] == "static"
    # 总分 = 四维加权，一项都不许漏
    want = (ub.W_POS * got["parts"]["pos"] + ub.W_DWELL * got["parts"]["dwell"]
            + ub.W_REGULAR * got["parts"]["regular"] + ub.W_TOUCH * got["parts"]["touch"])
    assert got["score"] == pytest.approx(want, abs=0.02)


def test_default_call_equals_explicit_static():
    """不传 box_mode 与显式传 static 必须一样 —— 否则默认值的纪律就是空话。"""
    m = {"pos": 0.2, "dwell_ratio": 0.4, "cv": 20.0, "slope_yr": 6.0,
         "touches": 5, "boll_pctb_rank": 55.0}
    assert ub.score_one(m) == ub.score_one(m, box_mode="static")


def test_blend_and_dyn_change_only_position_part():
    """换尺只许动位置分：驻留/规整/触及三项与权重一字不动。"""
    m = {"pos": 0.05, "dwell_ratio": 0.5, "cv": 15.0, "slope_yr": 3.0,
         "touches": 8, "boll_pctb_rank": 10.0}
    s = ub.score_one(m, box_mode="static")
    b = ub.score_one(m, box_mode="blend")
    d = ub.score_one(m, box_mode="dyn")
    for k in ("dwell", "regular", "touch"):
        assert s["parts"][k] == b["parts"][k] == d["parts"][k]
    # 静态 pos=0.05 → 85 分；动态分位 10 → 90 分；blend = 0.5×85+0.5×90 = 87.5
    assert s["parts"]["pos"] == pytest.approx(85.0, abs=0.2)
    assert d["parts"]["pos"] == pytest.approx(90.0, abs=0.2)
    assert b["parts"]["pos"] == pytest.approx(
        (1 - ub.W_POS_DYN) * s["parts"]["pos"] + ub.W_POS_DYN * d["parts"]["pos"], abs=0.2)


# ---------------------------------------------------------------- ③ 动态字段
def test_boll_fields_none_when_short():
    """K 线不足 20 根：动态字段给 None，但**不许抛异常、不许把整只票判死**。"""
    f = ub._boll_fields([10.0] * 10, 10.0)
    assert all(v is None for v in f.values())
    # 20 日一字横盘 → 带宽 0 → %B 无定义，同样给 None 而不是除零
    f2 = ub._boll_fields([10.0] * 30, 10.0)
    assert f2["boll_pctb"] is None and f2["boll_pctb_rank"] is None


def test_boll_fields_basic_math():
    """正常序列：%B 落在 0 附近当且仅当跌破下轨，分位越低越贴下轨。"""
    closes = [10.0 + ((i * 7) % 13) / 10.0 for i in range(300)]
    f = ub._boll_fields(closes, closes[-1])
    assert f["boll_lower"] <= f["boll_mid"] <= f["boll_upper"]
    assert 0.0 <= f["boll_pctb_rank"] <= 100.0
    assert f["boll_bw"] > 0
    # 把当日价压到下轨以下，%B 必须为负、分位必须极低
    f2 = ub._boll_fields(closes, f["boll_lower"] - 1.0)
    assert f2["boll_pctb"] < 0
    assert f2["boll_pctb_rank"] <= 5.0


def test_score_falls_back_when_dynamic_missing():
    """动态分位缺失时不许剔除、不许给 0 分：退回静态位置分并标明来源。"""
    m = {"pos": 0.1, "dwell_ratio": 0.5, "cv": 15.0, "slope_yr": 3.0,
         "touches": 8, "boll_pctb_rank": None}
    for mode in ("blend", "dyn"):
        got = ub.score_one(m, box_mode=mode)
        assert got["pos_src"] == "static_fallback"
        assert got["parts"]["pos"] == pytest.approx(70.0, abs=0.2)
        assert got["score"] > 0


# ---------------------------------------------------------------- ④ 历史快照模式隔离
_OLD_DDL = """
CREATE TABLE universe_box_hist (
    trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    score REAL, pos REAL, dwell_ratio REAL, dwell_max_run INTEGER,
    pct750 REAL, current REAL, rank INTEGER,
    PRIMARY KEY (trade_date, code))
"""


@pytest.fixture()
def tmp_hist(monkeypatch, tmp_path):
    """建一个**老表结构**（没有 boll_pctb / box_mode 列）的临时库，把 _hist_con 指过去。

    从老结构开始是刻意的：项目里已有的 app.db 就是老结构，
    `CREATE TABLE IF NOT EXISTS` 对它不生效，只有 ALTER 补列这条路。
    """
    dbp = str(tmp_path / "hist.db")
    con = sqlite3.connect(dbp)
    con.execute(_OLD_DDL)
    con.executemany(
        "INSERT INTO universe_box_hist(trade_date,code,name,score,pos,dwell_ratio,"
        "dwell_max_run,pct750,current,rank) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [("2026-10-31", "600000", "浦发", 80.0, 0.1, 0.6, 30, 10.0, 9.0, 1),
         ("2026-10-31", "000001", "平安", 70.0, 0.2, 0.5, 20, 20.0, 8.0, 2)])
    con.commit()
    con.close()
    monkeypatch.setattr(ub, "_hist_con", lambda: sqlite3.connect(dbp, timeout=30))
    return dbp


def _cols(dbp: str) -> set:
    con = sqlite3.connect(dbp)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(universe_box_hist)")}
    finally:
        con.close()


def _pk(dbp: str) -> list:
    con = sqlite3.connect(dbp)
    try:
        return ub._hist_pk_cols(con)
    finally:
        con.close()


def _ensure(dbp: str) -> None:
    """在独立连接上跑一次迁移（显式关连接：Windows 上不关会让 tmp 目录删不掉）。"""
    con = sqlite3.connect(dbp, timeout=30)
    try:
        ub._ensure_hist(con)
        con.commit()
    finally:
        con.close()


def test_hist_adds_columns_on_old_db(tmp_hist):
    """老库要被 ALTER 补上 boll_pctb / box_mode 两列，且幂等（跑两次不报错）。"""
    rows = [_row("600000", 81.0)]
    ub._attach_delta("2026-11-02", rows, box_mode="static")
    cols = _cols(tmp_hist)
    assert "boll_pctb" in cols and "box_mode" in cols
    ub._attach_delta("2026-11-02", rows, box_mode="static")   # 幂等
    assert _cols(tmp_hist) == cols
    con = sqlite3.connect(tmp_hist)
    try:
        v = con.execute("SELECT boll_pctb, box_mode FROM universe_box_hist"
                        " WHERE trade_date='2026-11-02' AND code='600000'").fetchone()
        assert v == (0.2, "static")
    finally:
        con.close()


def test_hist_pk_migrated_to_three_cols(tmp_hist):
    """QA 第 1 项：老库的两列主键必须被迁成 (trade_date, code, box_mode)，且数据不丢。

    迁移是"建新表→搬→删旧→改名"，最容易把行数搬错，所以这里对齐三件事：
    迁移前 2 行 → 迁移后 2 行；老行 box_mode 一律填 static；主键列顺序正确。
    """
    before = _cols(tmp_hist)
    con = sqlite3.connect(tmp_hist)
    try:
        n_before = con.execute("SELECT COUNT(*) FROM universe_box_hist").fetchone()[0]
    finally:
        con.close()
    _ensure(tmp_hist)
    assert _pk(tmp_hist) == ["trade_date", "code", "box_mode"]
    con = sqlite3.connect(tmp_hist)
    try:
        n_after = con.execute("SELECT COUNT(*) FROM universe_box_hist").fetchone()[0]
        modes = con.execute("SELECT DISTINCT box_mode FROM universe_box_hist").fetchall()
    finally:
        con.close()
    assert n_before == n_after == 2
    assert modes == [("static",)]
    assert set(before).issubset(_cols(tmp_hist))
    # 再跑一次不许报错、不许再搬一次（幂等）
    _ensure(tmp_hist)
    con = sqlite3.connect(tmp_hist)
    try:
        assert con.execute("SELECT COUNT(*) FROM universe_box_hist").fetchone()[0] == 2
    finally:
        con.close()


def test_same_day_two_modes_both_survive(tmp_hist):
    """QA 复现的故障：同一天先写 static(90) 再写 blend(99)。

    旧主键下 `ON CONFLICT(trade_date,code)` 会把 static 那行**改成** blend(99)，
    static 当天快照被吞；于是下一日 static 的 prev_date 跳回大前天，
    d_score 变成三个交易日的变化，而 note 仍写"与上一交易日对比"。
    三列主键之后两行并存，且次日 static 只跟当天 static 比。
    """
    ub._attach_delta("2026-11-02", [_row("600000", 90.0)], box_mode="static")
    ub._attach_delta("2026-11-02", [_row("600000", 99.0)], box_mode="blend")
    con = sqlite3.connect(tmp_hist)
    try:
        rows = con.execute(
            "SELECT box_mode, score FROM universe_box_hist"
            " WHERE trade_date='2026-11-02' AND code='600000' ORDER BY box_mode").fetchall()
    finally:
        con.close()
    assert rows == [("blend", 99.0), ("static", 90.0)]      # 两行并存，没被吞

    rows_s = [_row("600000", 91.0)]
    info = ub._attach_delta("2026-11-03", rows_s, box_mode="static")
    assert info["prev_date"] == "2026-11-02"                 # 不是跳回 2026-10-31
    assert info["n_prev"] == 1
    assert rows_s[0]["d_score"] == pytest.approx(1.0)        # 91 − 90，一个交易日



def test_delta_static_compares_with_legacy_rows(tmp_hist):
    """老库里的行没有 box_mode（NULL），必须按 static 认 —— 否则上线第一天就是"全表新进"。"""
    rows = [_row("600000", 83.0), _row("000001", 65.0)]
    info = ub._attach_delta("2026-11-02", rows, box_mode="static")
    assert info["available"] is True
    assert info["prev_date"] == "2026-10-31"
    assert info["box_mode"] == "static"
    d = {r["code"]: r for r in rows}
    assert d["600000"]["is_new"] is False
    assert d["600000"]["d_score"] == pytest.approx(3.0)
    assert d["000001"]["d_score"] == pytest.approx(-5.0)


def test_delta_blend_does_not_compare_with_static_history(tmp_hist):
    """★ 本次改动的核心陷阱：切到 blend 后**不许**拿静态历史算 Δ（那是假跳变）。

    老库里只有 static 历史，所以第一天的 blend 结果必须是 available=False、
    Δ 全为 None —— 而不是"每只票都涨了 8 分"。
    """
    rows = [_row("600000", 88.0), _row("000001", 62.0)]
    info = ub._attach_delta("2026-11-02", rows, box_mode="blend")
    assert info["available"] is False
    assert info["prev_date"] is None
    for r in rows:
        assert r["d_score"] is None and r["d_rank"] is None and r["is_new"] is None


def test_delta_isolated_per_mode(tmp_hist):
    """同表里两种模式各存一份：blend 只跟 blend 比，static 只跟 static 比。"""
    ub._attach_delta("2026-11-02", [_row("600000", 80.0)], box_mode="static")
    ub._attach_delta("2026-11-03", [_row("600000", 90.0)], box_mode="blend")
    rows_s = [_row("600000", 82.0)]
    info_s = ub._attach_delta("2026-11-04", rows_s, box_mode="static")
    assert info_s["prev_date"] == "2026-11-02"      # 跳过 blend 那天
    assert rows_s[0]["d_score"] == pytest.approx(2.0)
    rows_b = [_row("600000", 91.0)]
    info_b = ub._attach_delta("2026-11-05", rows_b, box_mode="blend")
    assert info_b["prev_date"] == "2026-11-03"
    assert rows_b[0]["d_score"] == pytest.approx(1.0)


def test_history_filters_by_mode(tmp_hist):
    """单票轨迹也只回同一把尺的点，避免把两种分数连成一条看起来在涨的折线。

    注意老库那两行（2026-10-31，80 分）没有 box_mode，按 static 认 ——
    所以 static 轨迹是「老库那天 + 今天」，blend 轨迹只有它自己那一天。
    """
    ub._attach_delta("2026-11-02", [_row("600000", 80.0)], box_mode="static")
    ub._attach_delta("2026-11-03", [_row("600000", 90.0)], box_mode="blend")
    s = ub.history("600000", box_mode="static")
    b = ub.history("600000", box_mode="blend")
    assert [x["trade_date"] for x in s] == ["2026-10-31", "2026-11-02"]
    assert [x["score"] for x in s] == [80.0, 80.0]
    assert [x["trade_date"] for x in b] == ["2026-11-03"]
    assert all(x["box_mode"] == "static" for x in s)


# ---------------------------------------------------------------- ⑤ payload 自述
def test_params_declares_ruler():
    """payload 的 params 必须能回答"这一屏用的是哪把尺"，否则排查只能靠重启猜。"""
    kl = {"600000": _bars(800)}
    out = ub.scan_universe(kl, limit=5, min_dwell=0.0, box_mode="static")
    p = out["params"]
    assert p["box_mode"] == "static"
    assert (p["boll_win"], p["boll_k"], p["boll_rank_win"]) == (20, 2.0, 250)
    assert p["w_pos_dyn"] == 0.5
    assert p["weights"] == {"pos": 0.30, "dwell": 0.35, "regular": 0.20, "touch": 0.15}
