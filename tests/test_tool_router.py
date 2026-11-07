# -*- coding: utf-8 -*-
"""受约束策略路由（tool_router）回归测试 —— 守住审计 §8.4「最小路由验收用例」。

这个文件盯的是**结论层的不可变性**，不是具体数字：
  · 弃权态下不许有链（`ABSTAIN` 且 `selected_chain` 为空）
  · 链里的每个工具都必须在 eligible 里（不能从 blocked 里凑名单）
  · 筛道判资格时**不带持有期**（筛道没有「拿几天」这个输入）
  · 反向信号在筛道里必须判死（这是 P0-02 在筛道侧的落地）

为什么不断言「今天一定是 ABSTAIN」：市况每天在变，写死一个当日结论的测试
明天就会红，然后被人加 `skip`，最后等于没有测试。断言不变量才能一直活着。

`plan()` 用只读方式连生产库（`mode=ro`），保证测试不写任何东西（T12）。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_tool_router.py -q
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
from viewer import tool_router as TR  # noqa: E402

APP_DB = os.path.join(ROOT, "viewer", "app.db")


@pytest.fixture(scope="module")
def con():
    """内存库 + 磁盘契约：判据用真的，数据用空的。"""
    c = sqlite3.connect(":memory:")
    TC.sync(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def live_con():
    """生产库**只读**连接。库不在就跳过，不假装通过。"""
    if not os.path.exists(APP_DB):
        pytest.skip("生产库不存在：%s" % APP_DB)
    uri = "file:%s?mode=ro" % APP_DB.replace("\\", "/")
    try:
        c = sqlite3.connect(uri, uri=True)
        c.execute("SELECT 1 FROM tool_contract LIMIT 1")
    except sqlite3.Error as e:
        pytest.skip("生产库不可只读打开或无契约表：%s" % e)
    yield c
    c.close()


# ---------------------------------------------------------------- 策略自述

def test_policy_status_is_frozen_and_versioned():
    """线上不改权重，只执行版本化 policy（审计 §3.8）。"""
    s = TR.policy_status()
    assert s["policy_frozen"] is True
    assert s["online_weight_tuning"] is False
    assert s["abstain_is_valid"] is True
    assert s["contract_version"] == TC.CONTRACT_VERSION
    assert s["router_version"].startswith("router-v")


# ---------------------------------------------------------------- 筛道上下文

def test_screen_context_has_no_horizon(con):
    """筛道没有持有期语义：拿编出来的 20 日去卡 60/120 日工具会误杀。"""
    c = TR.screen_context(con)
    assert c["horizon_days"] is None
    assert c["market_regime"] in ("up", "down", "range")
    assert c["stock_code"] == ""


def test_screen_verdicts_block_reverse_signal(con):
    """P0-02 在筛道侧：`pick_ma_bull` 不能被当成正向筛选器跑。"""
    v = TR.screen_verdicts(con, ["pick_ma_bull", "box_bottom", "no_such_tool"])
    assert v["pick_ma_bull"]["decision"] == "blocked"
    assert v["pick_ma_bull"]["blocked_kind"] == "evidence"
    # 箱体在批量模式下只输出观察池，不该被判死（它是 watch，不是 blocked）
    assert v["box_bottom"]["decision"] != "blocked", v["box_bottom"]["reasons"]
    # 无契约 → fail-closed
    assert v["no_such_tool"]["decision"] == "blocked"
    assert v["no_such_tool"]["blocked_kind"] == "unknown"


def test_screen_verdicts_share_one_context(con):
    """一条筛道只判一次上下文（共用），否则 5 级筛道会白读 5 遍库。"""
    a = TR.screen_verdicts(con, ["reso_ab"])
    b = TR.screen_verdicts(con, ["reso_ab", "reso_ab"])
    assert a["reso_ab"]["decision"] == b["reso_ab"]["decision"]


# ---------------------------------------------------------------- 计划不变量（§8.4）

def test_plan_never_builds_chain_from_blocked_tools(live_con):
    """§8.4-1：不得返回任何基于反向工具的正向买入链。

    弃权 → 链必须为空；有链 → 链里每个工具都必须在 eligible 名单内。
    """
    for hz in (1, 5, 20, 60, 120):
        r = TR.plan(live_con, horizon_days=hz, persist=False)
        assert r["status"] in ("ACTIONABLE", "WATCH", "ABSTAIN"), (hz, r["status"])
        chain = r.get("selected_chain") or []
        if r["status"] != "ACTIONABLE":
            assert chain == [], "%s 日：非 ACTIONABLE 却给了链 %s" % (hz, chain)
            continue
        eligible = {t["tool_key"] for t in (r.get("eligible_tools") or [])}
        for step in chain:
            assert step["tool_key"] in eligible, (hz, step, eligible)


def test_plan_counts_cover_all_33_tools(live_con):
    """33 个工具一个都不能在计数里消失（消失=没人判它，等于默认放行）。"""
    r = TR.plan(live_con, horizon_days=20, persist=False)
    c = r.get("counts") or {}
    assert c.get("total") == 33, c
    assert (c.get("actionable", 0) + c.get("watch", 0) + c.get("blocked", 0)) == c["total"], c


def test_plan_blocked_items_always_explain(live_con):
    """被拒必须给得出中文原因，否则页面上就是一句「不可用」。"""
    r = TR.plan(live_con, horizon_days=20, persist=False)
    for b in r.get("blocked_tools") or []:
        assert str(b.get("reason") or "").strip(), b
        assert str(b.get("blocked_kind") or "").strip(), b


def test_plan_abstain_reason_is_human_readable(live_con):
    """弃权不是错误码，是一句人话（用户要看懂为什么今天不做）。"""
    r = TR.plan(live_con, horizon_days=20, persist=False)
    if r["status"] == "ABSTAIN":
        assert len(str(r.get("reason") or "")) >= 10
