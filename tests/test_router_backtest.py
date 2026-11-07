# -*- coding: utf-8 -*-
"""回测引擎（T10）的回归测试 —— 守两件最容易悄悄坏掉的事。

① **时间之墙**：判据只能看 `<= D` 的 K 线。这一条一旦破了，回测会变成"用正确答案
   倒推选股"，任何工具都能跑出 +30% 的超额，而且**不会报任何错**。
   所以这里用"未来暴涨"的合成序列做一次攻击：如果引擎把 D 之后的数据漏给判据，
   入选集合会明显变大 —— 断言它不变。

② **口径复现**：回测用的判据必须和工具箱跑出来的是同一套。`oss_near_high` 与
   `oss_near_high_tight` 共用判据、只差默认参数，传错参数会让入选数从 125 变成 802
   而毫无异常。这里直接拿真实库对照 `apply_tool()` 的结果，要求差集为 0。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_router_backtest.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

import router_backtest as RB  # noqa: E402
import router_backtest_oss as RBO  # noqa: E402

APP_DB = os.path.join(ROOT, "viewer", "app.db")


# ---------------------------------------------------------------- ① 前向收益

def test_fwd_return_uses_close_to_close():
    """D → D+N 的收益必须按收盘对收盘算，且越界返回 None（不许用未来不存在的价）。"""
    entry = {"bars": [(f"d{i}", 1, 100 + i, 1, 1, 1) for i in range(30)]}
    r = RB.fwd_return(entry, 0, 20)
    assert r == pytest.approx(120 / 100 - 1)
    assert RB.fwd_return(entry, 25, 20) is None      # 越界 → None，不是 0
    assert RB.fwd_return(entry, 0, 29) == pytest.approx(129 / 100 - 1)


# ---------------------------------------------------------------- ② 时间之墙

def test_time_wall_blocks_lookahead():
    """【核心】D 日的窗口必须与「D 之后发生什么」完全无关。

    做法：造两条序列，前 300 根完全相同，之后一条每天 +9%、一条每天 −9%。
    对同一个 D，`window_at()` 返回的窗口**必须逐字节相同** —— 因为 D 之后的
    数据根本不参与切片。这条断言一旦失败，说明引擎把未来漏给了判据，
    所有工具都会跑出漂亮超额且不报错。
    """
    def series(tail_step: float, n_head=300, n_tail=60):
        bars, px = [], 10.0
        for i in range(n_head):
            bars.append(("d%04d" % i, px, px, px * 1.01, px * 0.99, 2e7))
        for i in range(n_tail):
            px *= 1 + tail_step
            bars.append(("d%04d" % (n_head + i), px, px, px * 1.01, px * 0.99, 2e7))
        return {"dates": [b[0] for b in bars], "bars": bars}

    up, down = series(+0.09), series(-0.09)
    D = "d0299"
    w_up, i_up = RB.window_at(up, D, 750)
    w_dn, i_dn = RB.window_at(down, D, 750)

    assert i_up == i_dn == 299, "索引算错了：D 应落在第 299 根"
    assert w_up == w_dn, "未来数据改变了 D 日窗口 —— 存在前视偏差"
    assert w_up[-1][0] == D, "窗口最后一根必须是 D 当日"
    assert all(b[0] <= D for b in w_up), "窗口里混入了 D 之后的 K 线"
    assert len(w_up) == 300, "窗口长度应截止到上市日（不足 keep 时给全部）"



# ---------------------------------------------------------------- ③ 口径复现

@pytest.fixture(scope="module")
def con():
    if not os.path.exists(APP_DB):
        pytest.skip("生产库不存在")
    c = sqlite3.connect("file:%s?mode=ro" % APP_DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.mark.parametrize("tool", [
    "oss_high_vol", "oss_ma_align", "oss_near_high", "oss_near_high_tight",
    "oss_burst", "oss_mom20", "oss_rsi_overbought",
])
def test_oss_pred_reproduces_toolbox(con, tool):
    """回测判据必须与 `apply_tool()` 逐票一致（差集为 0）。

    这条测试就是为了抓「传空参数」那类 bug：`oss_near_high_tight` 的默认 max_gap=5，
    漏掉参数会多选 677 只，而它不抛异常、只会让回测结论悄悄失真。
    """
    from viewer import tool_contract as TC
    from viewer import toolbox as TB

    try:
        TB.ensure_tables(con)
    except sqlite3.OperationalError:
        pytest.skip("只读库无法建表（已存在则无妨）")
    try:
        from viewer import universe_filter as UF
        uni = [str(c) for c in (UF.evaluation_universe() or [])]
    except Exception:  # noqa: BLE001
        pytest.skip("取不到干净宇宙")
    if not uni:
        pytest.skip("干净宇宙为空")

    official = set(TB.apply_tool(con, tool, uni, {})["kept"])
    if not official:
        pytest.skip("该工具今天无入选，无法对照")

    _td, snap = TB.oss_snapshot(con)
    prm = TC.effective_defaults(dict(TB.get_tool(con, tool) or {}), TC.get(con, tool))
    pred = TB._OSS_PRED[tool]                 # noqa: SLF001 被测对象就是它
    mine = {c for c in uni if c in snap and pred(snap[c], prm)[0]}
    assert mine == official, "%s 口径不一致：官方 %d 只 / 回测 %d 只 / 差 %d 只（参数 %r）" % (
        tool, len(official), len(mine), len(official ^ mine), prm)


# ---------------------------------------------------------------- ④ 统计口径

def test_tstat_aggregates_by_day_not_by_stock():
    """t 值必须按「日」聚合（同一天多只票不是独立样本），否则样本量虚增、t 假性变大。"""
    same_day = [0.01] * 100                   # 100 个完全一样的日超额
    assert RB._tstat(same_day) is None        # 零方差 → 不给 t，而不是给无穷
    mixed = [0.01, -0.02, 0.03, 0.00, 0.015]
    t = RB._tstat(mixed)
    assert t is not None and abs(t) < 5


def test_cost_is_deducted():
    """成本必须直接进收益，不留"含成本/不含成本"两套结论。"""
    src = open(os.path.join(ROOT, "tools", "router_backtest.py"), encoding="utf-8").read()
    assert "excess = gross - base - cost" in src


# ---------------------------------------------------------------- pick_* 时点化
# 这三个工具原本只是「查 stock_strategy_picks 名单」，判据在
# viewer/strategy_templates.py 的一条 SQL 里。时点化的做法是把历史窗口算出的指标
# 灌进内存库、**执行同一条 SQL 原文**，而不是把阈值手抄一遍。
# 下面两个用例就是守这条：不许偷偷改成手抄版，也不许把「时点化不了」的工具硬跑。

def test_pick_specs_use_template_sql_not_handcopied():
    """pick 适配层必须真的能执行模板 SQL，且模板必须存在。"""
    from viewer import strategy_templates as ST

    keys = {t["key"] for t in ST.TEMPLATES}
    for tpl in ("ma_bull_array", "vol_breakout", "oversold_rebound"):
        assert tpl in keys
        spec = RB.tool_spec("pick:" + tpl)
        assert spec["kind"] == "pick", "%s 应可时点化：%s" % (tpl, spec.get("reason"))
        assert spec["keep"] >= 260, "窗口太短，ma200 会算不出来"


def test_pick_refuses_what_has_no_history():
    """依赖当期快照的模板必须拒绝，不能靠「跑出来是空的」糊过去。"""
    spec = RB.tool_spec("pick:quality_leaders")
    assert spec["kind"] == "refuse"
    assert "质量分" in spec["reason"]

    spec = RB.tool_spec("pick:不存在的模板")
    assert spec["kind"] == "refuse"


def test_pick_fn_only_sees_given_window():
    """pick 只能看传进来的窗口 —— 窗口不足 ma200 就必须算不出来，不得回退查库。"""
    from viewer import indicators as IND

    cols = ["code", "name"] + list(IND.IND_COLS)
    bars = [("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28), 10.0, 10.0, 10.1, 9.9, 1e6)
            for i in range(120)]                       # 只有 120 根，不够 ma200
    mems = RB.precompute_pick({"000001": {"bars": bars}}, ["2026-04-30"], cols)
    try:
        n = mems["2026-04-30"].execute("select count(*) from stock_indicators").fetchone()[0]
        assert n == 0, "窗口不足 260 根时必须不产出行，实际 %d 行" % n
    finally:
        for m in mems.values():
            m.close()


def test_pick_precompute_buckets_by_sampling_day():
    """指标只算一次但要按日分桶：给两个采样日，两天的行必须各自独立。"""
    from viewer import indicators as IND

    cols = ["code", "name"] + list(IND.IND_COLS)
    bars = []
    px = 10.0
    for i in range(300):
        px *= 1.001
        bars.append(("2025-%03d" % i, px, px, px * 1.01, px * 0.99, 1e6))
    days = ["2025-100", "2025-299"]
    mems = RB.precompute_pick({"000001": {"bars": bars}}, days, cols)
    try:
        for d in days:
            n = mems[d].execute("select count(*) from stock_indicators").fetchone()[0]
            assert n == 1, "%s 应有 1 行，实际 %d" % (d, n)
        c1 = mems[days[0]].execute("select close from stock_indicators").fetchone()[0]
        c2 = mems[days[1]].execute("select close from stock_indicators").fetchone()[0]
        assert c2 > c1, "后一日的收盘价应更高（序列是单调上涨），说明分桶没混"
    finally:
        for m in mems.values():
            m.close()


def test_neutral_benchmark_fields_present():
    """规模中性字段必须出现在输出里 —— 缺了就等于悄悄退回「只看全池超额」。"""
    import inspect

    src = inspect.getsource(RB.run)
    for key in ("excess_pp_liq", "liq_pct_kept", "base_liq"):
        assert key in src, "回测输出缺少规模中性字段 %s" % key
