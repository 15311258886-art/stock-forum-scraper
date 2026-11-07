# -*- coding: utf-8 -*-
"""工具资格契约（tool_contract）回归测试 —— 审计 P1-05「测试没有守住工具使用资格」。

为什么要这个文件：审计的核心结论是「33 个工具能算，但系统答不了『今天该不该开』」。
把判据写进代码还不够，**判据本身必须被测试钉死**，否则下一次改契约或改 evaluate()
时，"反向信号又能当正向用了"这种事不会有任何测试变红 —— 而它正是审计发现的那个 bug。

测试全部用内存库 + 磁盘上的契约 JSON，不碰 `viewer/app.db`，因此可只读运行（T12）。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_tool_contract.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import tool_contract as TC  # noqa: E402


@pytest.fixture(scope="module")
def con():
    c = sqlite3.connect(":memory:")
    TC.sync(c)
    yield c
    c.close()


def ctx(**kw) -> dict:
    """一份最小可用上下文。默认「下跌行情 / 20 日 / 无个股」，各用例按需覆盖。"""
    base = {
        "trade_date": "2026-11-06",
        "market_regime": "down",
        "horizon_days": 20,
        "stock_code": "",
        "data_quality": {"trade_date": "2026-11-06", "kronos_date": "2026-11-06",
                         "holders_date": "2026-11-03", "res_card_date": "2026-11-06"},
        "box": {"available": True, "positions": {}},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 契约本体

def test_spec_has_33_tools():
    """审计要求 33/33 全覆盖。少一条就是「这个工具没人知道该怎么用」。"""
    spec = TC.load_spec()
    tools = spec.get("tools") or {}
    assert len(tools) == 33, "契约定的是 33 个工具，实际 %d 条" % len(tools)


def test_every_contract_field_is_valid(con):
    """字段完整性 + 枚举合法性。契约里出现拼错的 role，判定会静默走 else 分支。"""
    bad = []
    for key, c in TC.list_all(con).items():
        if c["role"] not in TC.ROLES and c["role"] != "retired":
            bad.append((key, "role", c["role"]))
        for u in c["decision_use"]:
            if u not in TC.DECISION_USES:
                bad.append((key, "decision_use", u))
        if c["evidence_state"] not in TC.EVIDENCE_STATES:
            bad.append((key, "evidence_state", c["evidence_state"]))
        if c["role"] == "retired":
            # 已淘汰工具不参与任何决策，契约刻意不写 horizons / 缺数据策略 ——
            # 给它编一个持有期反而会让人以为"改天还能用"。
            assert c["evidence_state"] == "retired", key
            continue
        if not c["horizons"]:
            bad.append((key, "horizons", "空"))
        if not str(c.get("missing_data_policy") or "").strip():
            bad.append((key, "missing_data_policy", "空"))
    assert not bad, "契约字段不合法：%s" % bad[:8]


def test_unknown_tool_fails_closed(con):
    """没有契约 → blocked（fail-closed）。绝不因为「没报错」就放行。"""
    d = TC.evaluate("no_such_tool_xyz", None, None, ctx())
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "unknown"


def test_allowed_as_screen_rejects_missing_contract_and_retired(con):
    ok, _ = TC.allowed_as_screen("nope", None, {"status": "active"})
    assert ok is False
    ok, why = TC.allowed_as_screen("pick_ma_bull", TC.get(con, "pick_ma_bull"),
                                   {"status": "retired", "retire_reason": "测试"})
    assert ok is False and "淘汰" in why


# ---------------------------------------------------------------- 九条判定

def test_reverse_signal_cannot_be_positive_selector(con):
    """P0-02 的核心：`pick_ma_bull` 有 -3.683pp 反向结论，不能再当正向选股器。

    注意这里断言的是 `blocked_kind == "evidence"` 而**不是**某句具体文案：
    文案会随证据补齐而变化（`bt_verdict` 缺失时走 NOTE_CN 兜底），
    判定归因才是契约的一部分 —— 断言文案的测试一改数据就红，等于没守住东西。
    """
    d = TC.evaluate("pick_ma_bull", TC.get(con, "pick_ma_bull"), {"status": "active"}, ctx())
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "evidence"
    assert d["reasons"], "被拒必须给得出人能读懂的原因"


def test_reverse_evidence_does_not_kill_watch_use(con):
    """`box_bottom` 同为 validated_negative，但用途是 watch —— 只挡正向用途，不挡观察。

    箱体谷底只服务 60/120 日，所以上下文用 120 日；给它 20 日会先被周期错配拦下，
    那是另一条规则，测不到这里想守的东西。
    """
    d = TC.evaluate("box_bottom", TC.get(con, "box_bottom"), {"status": "active"},
                    ctx(horizon_days=120))
    assert d["decision"] == "watch", d["reasons"]
    assert d["blocked_kind"] == ""


def test_guard_is_exempt_from_regime_and_horizon(con):
    """排除器不受行情/持有期限制：风险与拿多久无关，拿 120 天更该排除。"""
    for key in ("oss_high_vol", "avoid_c"):
        d = TC.evaluate(key, TC.get(con, key), {"status": "active"},
                        ctx(horizon_days=120, market_regime="up"))
        assert d["decision"] != "blocked", (key, d["reasons"])
        assert d["blocked_kind"] not in ("horizon", "regime"), (key, d["reasons"])


def test_regime_mismatch_blocks(con):
    """行情不符 → blocked(regime)。`tv_platform` 契约只服务 up/range。"""
    d = TC.evaluate("tv_platform", TC.get(con, "tv_platform"), {"status": "active"},
                    ctx(market_regime="down"))
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "regime"


def test_horizon_mismatch_blocks(con):
    """周期错配 → blocked(horizon)。`reso_ab` 只服务 1/5 日，20 日视角应判死。"""
    d = TC.evaluate("reso_ab", TC.get(con, "reso_ab"), {"status": "active"}, ctx(horizon_days=20))
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "horizon"
    ok = TC.evaluate("reso_ab", TC.get(con, "reso_ab"), {"status": "active"}, ctx(horizon_days=5))
    assert ok["decision"] == "actionable", ok["reasons"]


def test_shadow_status_cannot_be_actionable(con):
    """shadow 工具不得产出可执行结论（审计 §4.1 第 3 条）。"""
    d = TC.evaluate("reso_ab", TC.get(con, "reso_ab"), {"status": "shadow"}, ctx(horizon_days=5))
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "authority"


def test_kronos_stale_blocks(con):
    """模型输出落后于最新交易日 → blocked(stale)。审计实测 kronos 停在 10-25。"""
    d = TC.evaluate("kronos_kline", TC.get(con, "kronos_kline"), {"status": "active"},
                    ctx(data_quality={"trade_date": "2026-11-06", "kronos_date": "2026-10-25"}))
    assert d["decision"] == "blocked"
    assert d["blocked_kind"] == "stale"


# ---------------------------------------------------------------- 箱体语义（§8.4 第 2/3/4 条）

def test_box_bottom_position_semantics(con):
    """35% 不是底（blocked）、22% 只观察（watch）、不在池不触发（blocked）。"""
    c = TC.get(con, "box_bottom")
    box = {"available": True, "positions": {"000731": 35.0, "000008": 22.0}}
    kw = dict(horizon_days=120, box=box)          # 箱体工具服务 60/120 日

    d = TC.evaluate("box_bottom", c, {"status": "active"}, ctx(stock_code="000731", **kw))
    assert d["decision"] == "blocked" and "1/3" in "".join(d["reasons"])

    d = TC.evaluate("box_bottom", c, {"status": "active"}, ctx(stock_code="000008", **kw))
    assert d["decision"] == "watch"
    assert d["state"] == "watch"

    d = TC.evaluate("box_bottom", c, {"status": "active"}, ctx(stock_code="600519", **kw))
    assert d["decision"] == "blocked"


def test_box_bottom_default_pos_max_is_one_third(con):
    """P0-04：默认 pos_max 从 1.0 改成 0.333，契约说了算，登记表的旧值不许盖它。"""
    c = TC.get(con, "box_bottom")
    prm = TC.effective_defaults({"default_params": {"pos_max": 1.0}}, c)
    assert abs(float(prm["pos_max"]) - 0.333) < 1e-9, prm


# ---------------------------------------------------------------- 三轴

def test_lifecycle_three_axes_stay_separate(con):
    """lifecycle / decision_mode / evidence_state 三个轴互不覆盖（T02）。"""
    life = TC.lifecycle_of({"status": "active"}, TC.get(con, "pick_ma_bull"))
    assert life["lifecycle"] == "kept_on_shelf"          # 还在货架上，看得见
    assert life["decision_mode"] != "normal"             # 但不许当正常决策工具
    assert not life["can_buy"] or life["evidence_state"] == "unverified"
    assert life["can_decide"] is False or life["decision_mode"] == "normal"
