# -*- coding: utf-8 -*-
"""证据硬门禁测试（2026-11-01 测评 P0-2）。

覆盖三件事，缺一件这套改动就等于没做：

  ① **规则按数据判，不是写死结论。** 把 `lhb_turnover` 的 excess 改成正数后，
     `mainline_tier` 必须跟着变成 watch/executable —— 否则「负面实证被屏蔽」
     只是把今天的答案抄进代码，明天账本翻正了界面还是错的。
  ② **失败方向是 fail-closed。** 未登记来源 → watch（不是 executable）；
     门禁内部异常 → watch（不是放行）。
  ③ **后端结构与前端渲染是同一份契约。** app.js 里必须真有 opportunities_watch
     的渲染路径，且「能上车的」这个副标题必须不存在（防止有人改回去）。

第 5 个用例用**内存 SQLite** 造 3 条主线 + 2 条四支柱，直接跑
`_today_decision_compute`，断言真实分流结果。不用 mock 掉计算逻辑 ——
要测的正是「计算完到上屏之间那道门」。
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import evidence_gate as G  # noqa: E402
from viewer import evidence as LEDGER  # noqa: E402

APP_JS = ROOT / "viewer" / "static" / "app.js"


# ---------------------------------------------------------------- ① 六种来源的裁决
def test_all_registered_sources_have_valid_tier():
    """登记表里每个来源都必须落在三档之一，且必须给出一句能上屏的理由。"""
    assert len(G.SOURCES) >= 8, "来源登记表条目太少，说明有人把来源删了却没管它在哪上屏"
    for key in G.SOURCES:
        ev = G.evaluate(key)
        assert ev["tier"] in (G.TIER_EXECUTABLE, G.TIER_WATCH, G.TIER_BLOCKED), \
            "%s 的裁决不在三档之内：%s" % (key, ev["tier"])
        assert ev["reason"] and len(ev["reason"]) > 8, "%s 没有可读理由" % key
        assert ev["invalidate"], "%s 没有失效条件（测评要求每张卡都有）" % key
        assert ev["source"] == key


def test_unknown_source_is_watch_never_executable():
    """未登记来源必须是 watch，且理由要写明「未纳入证据账本」。

    这是整条链路上最重要的一条不变式：默认值不能是 executable。
    默认放行的门禁，等于没有门禁。
    """
    ev = G.evaluate("some_brand_new_signal")
    assert ev["tier"] == G.TIER_WATCH, "未登记来源被判成 %s（默认值必须 fail-closed）" % ev["tier"]
    assert ev["status"] == "no_evidence"
    assert "未纳入证据账本" in ev["reason"]
    assert ev["claims"] == []


def test_pillar_sources_are_watch():
    """四支柱全部无 claim → 只能是 watch（无实证 ≠ 可用）。"""
    for key in ("pillar_policy_funnel", "pillar_dip_revert",
                "pillar_t1_surf", "pillar_resonance"):
        ev = G.evaluate(key)
        assert ev["tier"] == G.TIER_WATCH, "%s 被放进了可执行档" % key
        assert ev["status"] == "no_evidence"


def test_selfpick_and_forum_sources():
    """selfpick_a（watch）→ watch；forum_hold（refuted）→ blocked；forum_bullish → watch。"""
    assert G.evaluate("selfpick_a")["tier"] == G.TIER_WATCH
    assert G.evaluate("forum_hold")["tier"] == G.TIER_BLOCKED
    assert G.evaluate("forum_bullish")["tier"] == G.TIER_WATCH
    assert "已证伪" in G.evaluate("forum_hold")["label"]


# ---------------------------------------------------------------- ② 规则按数据判
def test_mainline_blocked_by_negative_lhb():
    """真实账本：lhb_turnover 是 active 且 excess<0 且 |t|≥2 → mainline_tier 必须 blocked。"""
    claim = LEDGER.get("lhb_turnover")
    assert claim["status"] == "active"
    assert claim["excess"] < 0
    assert abs(claim["t"]) >= 2
    ev = G.evaluate("mainline_tier")
    assert ev["tier"] == G.TIER_BLOCKED
    assert ev["level"] == "active", "level 应如实反映账本状态（active），不能改写成 refuted"
    assert "负面实证" in ev["label"]
    # 理由里必须带上真实数字（可从账本核对），不许写空泛结论
    assert "-0.91%" in ev["reason"]
    assert "-11.28" in ev["reason"]


def test_mainline_rule_follows_data_not_hardcoded(monkeypatch):
    """把 excess 改成正数后，mainline_tier 必须跟着松开（watch/executable）。

    这条用例存在的意义：证明 blocked 不是写死的结论，而是**按账本数字现算**的。
    如果哪天有人为了「让首页好看」把结论硬编码成 blocked，这里会立刻失败。
    """
    claim = LEDGER.get("lhb_turnover")
    monkeypatch.setitem(claim, "excess", 0.91)
    ev = G.evaluate("mainline_tier")
    assert ev["tier"] in (G.TIER_WATCH, G.TIER_EXECUTABLE), \
        "excess 翻正后仍被判 %s —— 说明裁决是写死的" % ev["tier"]
    assert ev["tier"] == G.TIER_EXECUTABLE, "0.91%% 显著为正且扣费后仍为正，应当可执行"


def test_mainline_becomes_watch_when_effect_too_small(monkeypatch):
    """方向为正但幅度不抵成本（比例极小）→ watch，不许当可执行。"""
    claim = LEDGER.get("lhb_turnover")
    monkeypatch.setitem(claim, "excess", 0.02)
    monkeypatch.setitem(claim, "t", 3.0)
    ev = G.evaluate("mainline_tier")
    assert ev["tier"] == G.TIER_WATCH, "扣费后为负的信号被放进了可执行档"
    assert "扣掉双边成本" in ev["reason"] or "幅度" in ev["reason"]


def test_mainline_becomes_watch_when_not_significant(monkeypatch):
    """|t|<2 → 分不清信号与噪声 → watch。"""
    claim = LEDGER.get("lhb_turnover")
    monkeypatch.setitem(claim, "t", -1.2)
    ev = G.evaluate("mainline_tier")
    assert ev["tier"] == G.TIER_WATCH


# ---------------------------------------------------------------- ③ 徽章字段
def test_annotate_mdd_is_none_and_label_says_untested():
    """账本没有 MDD → mdd 必须是 None，label 必须如实写「回撤未测」（不许估算）。"""
    ann = G.annotate({"symbol": "600519"}, "mainline_tier")
    ev = ann["evidence"]
    assert ev["mdd"] is None, "账本里没有 MDD，这里却给了数字（等于编造）"
    assert "回撤未测" in ev["label"]
    # 显式禁止「拿胜率冒充回撤」的两种典型写法
    assert ev["mdd"] != 39.9, "mdd 取到了 win（胜率冒充回撤）"


def test_annotate_fields_and_net_uses_cost_basis():
    """徽章六项字段齐全；net = 毛超额 − 回测口径双边成本（取自 params，不是硬编码）。"""
    from viewer.kernel import params as P

    ann = G.annotate({"symbol": "600519", "name": "贵州茅台"}, "mainline_tier")
    ev = ann["evidence"]
    assert ev["tier"] == G.TIER_BLOCKED
    assert ev["sample"] == "4077 个交易日 / 37.3 万样本"
    assert ev["excess"] == "T+5 超额 -0.91%"
    assert ev["t"] == -11.28
    cost_pct = round(P.BACKTEST_FEE_ROUNDTRIP * 100.0, 4)
    assert ev["net"] == pytest.approx(-0.91 - cost_pct, abs=0.01), \
        "扣费后收益没按 params.BACKTEST_FEE_ROUNDTRIP 算（成本口径必须单一来源）"
    assert ev["invalidate"] == "次日板块净流入转负，或龙头跌破当日低点即失效"
    assert ev["claim"] == "lhb_turnover"
    # annotate 不得改动调用方对象
    src = {"symbol": "600519"}
    G.annotate(src, "mainline_tier")
    assert "evidence" not in src, "annotate 原地改写了调用方的 item"


def test_annotate_without_claim_gives_dashes():
    """四支柱无 claim → sample/excess/net/mdd 全为空值（前端渲染「—」）。"""
    ev = G.annotate({"symbol": "000001"}, "pillar_t1_surf")["evidence"]
    assert ev["net"] is None
    assert ev["mdd"] is None
    assert ev["t"] is None
    assert ev["sample"] == "—"
    assert ev["excess"] == "—"


def test_direction_badge_is_no_evidence():
    """方向卡徽章必须是「无实证（板块资金流）」，不能显示成正面结论。"""
    ev = G.annotate({"sector": "人形机器人"}, "sector_flow")["evidence"]
    assert ev["tier"] == G.TIER_WATCH
    assert ev["level"] == "no_evidence"
    assert "无实证" in ev["label_short"]
    assert "板块资金流" in ev["label_short"]


# ---------------------------------------------------------------- ④ summary
def test_summary_source_counts():
    s = G.summary()
    assert s["count_unit"] == "sources"
    assert s["n_sources"] == len(G.SOURCES)
    assert s["executable"] + s["watch"] + s["blocked"] == s["n_sources"]
    assert "mainline_tier" in s["blocked_sources"], "龙虎榜高换手为负，主线来源必须被屏蔽"
    assert "forum_hold" in s["blocked_sources"]
    details = {d["source"]: d for d in s["blocked_details"]}
    assert details["mainline_tier"]["claim"] == "lhb_turnover"
    assert details["mainline_tier"]["excess"] == -0.91


def test_summary_item_counts():
    """传 items → 统计条数（首页「已屏蔽 N 条」只能这样来）。"""
    items = [G.annotate({"symbol": "1"}, "mainline_tier"),
             G.annotate({"symbol": "2"}, "mainline_tier"),
             G.annotate({"symbol": "3"}, "pillar_t1_surf")]
    s = G.summary(items)
    assert s["count_unit"] == "items"
    assert s["blocked"] == 2 and s["watch"] == 1 and s["executable"] == 0
    assert s["items"] == {"executable": 0, "watch": 1, "blocked": 2}


# ---------------------------------------------------------------- ⑤ 服务层分流
def _fake_conn():
    """内存 SQLite：只建 `_today_decision_compute` 会读的那几张表。

    刻意用真 SQL 而不是 mock 掉 execute —— 本次改动的前提就是「原来的 SQL 不变，
    只在 append 前加一道门」。用 mock 会把这个前提测丢。
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE sector_chain (trade_date TEXT, sector TEXT, net_amt REAL, onlist_n INT,
            leader_code TEXT, leader_name TEXT, leader_pct_now REAL, tiers_json TEXT,
            branches_json TEXT, t1_avg REAL);
        CREATE TABLE pillar_item (run_date TEXT, pillar TEXT, symbol TEXT, name TEXT, rank INT);
        CREATE TABLE pillar_run (run_date TEXT, pillar TEXT, title TEXT);
        CREATE TABLE lhb_sector_focus (trade_date TEXT, sector TEXT, net_amt REAL,
            t1_avg REAL, bench_t1 REAL, onlist_n INT);
        CREATE TABLE lhb_stocks (trade_date TEXT, security_code TEXT, security_name TEXT,
            net_amt REAL, change_rate REAL);
    """)
    tiers = ('[{"code":"600001","name":"甲","pct":3.1},'
             '{"code":"600002","name":"乙","pct":1.2},'
             '{"code":"600003","name":"丙","pct":5.0}]')
    c.execute("INSERT INTO sector_chain VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("2026-10-30", "人形机器人", 1.5e9, 3, "600001", "甲", 10.0, tiers, "[]", None))
    c.execute("INSERT INTO pillar_item VALUES (?,?,?,?,?)",
              ("2026-10-30", "policy_funnel", "000001", "平安银行", 1))
    c.execute("INSERT INTO pillar_item VALUES (?,?,?,?,?)",
              ("2026-10-30", "dip_revert", "000002", "万科A", 2))
    return c


def test_today_decision_gate_routing(monkeypatch):
    """3 条主线（负面实证）+ 2 条四支柱（无实证）→
    opportunities 空 / opportunities_watch 2 / blocked_count 3 / verdict 「无」。
    """
    server = pytest.importorskip("viewer.server")

    class _FakeDB:
        def __init__(self, conn):
            self.conn = conn

    monkeypatch.setattr(server, "DB", _FakeDB(_fake_conn()))
    # 这三个源会联网/查别的表，与本用例无关 → 打桩成空，保证用例确定、快速
    monkeypatch.setattr(server.etf, "etf_decision_overlay", lambda *a, **k: {})
    monkeypatch.setattr(server.resonance, "payload", lambda *a, **k: {})
    monkeypatch.setattr(server, "api_info_brief", lambda *a, **k: {"items": [], "sectors": []})
    monkeypatch.setattr(server.postdigest, "build", lambda *a, **k: {})

    out = server._today_decision_compute()

    assert out["opportunities"] == [], "主线票命中负面实证，不许进「可执行机会」"
    assert len(out["opportunities_watch"]) == 2, \
        "四支柱 2 条应降级进观察区，实际 %d 条" % len(out["opportunities_watch"])
    assert {x["symbol"] for x in out["opportunities_watch"]} == {"000001", "000002"}
    assert out["signal_state"]["blocked_count"] == 3
    assert out["signal_state"]["executable_count"] == 0
    assert out["signal_state"]["watch_count"] == 2
    assert out["signal_state"]["verdict"] == "无"
    assert out["signal_state"]["as_of"] == "2026-10-30"
    # 观察区每一条都要有徽章与「为什么不能执行」
    for x in out["opportunities_watch"]:
        assert x["evidence"]["tier"] == G.TIER_WATCH
        assert x["evidence"]["reason"]
        assert x["evidence"]["invalidate"]
    # 方向卡也要有徽章，且是「无实证」而不是正面结论
    assert out["directions"] and out["directions"][0]["evidence"]["level"] == "no_evidence"


def test_legacy_snapshot_is_downgraded(monkeypatch):
    """旧快照（无 evidence 字段）补齐时，必须降级进观察区而不是留在可执行区。"""
    server = pytest.importorskip("viewer.server")
    legacy = {"ok": True, "data_date": "2026-10-30",
              "opportunities": [{"symbol": "600001", "name": "甲", "note": "同题材尚未大涨"}],
              "directions": [], "risks": []}
    out = server._td_ensure_gate_fields(legacy)
    assert out["opportunities"] == [], "旧快照里的机会没有证据字段，不许继续当可执行"
    assert len(out["opportunities_watch"]) == 1
    assert out["signal_state"]["executable_count"] == 0
    assert out["signal_state"]["verdict"] == "无"
    assert "signal_state" in out and "evidence_gate" in out
    assert legacy["opportunities"], "不得原地改缓存里的 dict"


# ---------------------------------------------------------------- ⑥ 前端静态契约
def _app_js() -> str:
    return io.open(str(APP_JS), encoding="utf-8").read()


def test_frontend_renders_watch_section():
    js = _app_js()
    assert "opportunities_watch" in js, "前端没有渲染观察/待验证区"
    assert "tdEvidenceBadge" in js, "前端没有证据徽章组件"
    assert "tdOppEmptyHtml" in js, "前端没有「无可执行机会」的空态渲染"
    # 空态必须写理由：要用到后端给的 blocked_details / signal_state
    i = js.find("function tdOppEmptyHtml")
    assert i > 0
    seg = js[i:i + 2000]
    assert "blocked_details" in seg and "executable_count" in seg, \
        "空态没有引用后端计数/理由（可能在前端自己编数字）"
    assert "当前可交易信号数为" in seg


def test_frontend_card_title_changed():
    """「能上车的」必须消失 —— 这条是防回退的。"""
    js = _app_js()
    assert "能上车的" not in js, "「能上车的」副标题又出现了（正是本轮 P0-2 要消除的文案）"
    assert "今日可执行机会" in js
    assert "今日无可执行机会" in js
    assert "空仓也是一种判断" in js


def test_frontend_no_null_literal_rendering():
    """徽章里不允许出现裸 null/undefined 文案（缺字段必须显示「—」）。"""
    js = _app_js()
    i = js.find("function tdEvidenceBadge")
    assert i > 0
    seg = js[i:i + 1800]
    assert '"—"' in seg or '"—"' in seg
    assert "tdEvPct" in seg, "数值缺失去重逻辑没走统一出口"


def test_evidence_ledger_has_mdd_todo():
    """账本顶部必须有 MDD TODO（否则下一个人不知道这个字段为什么是空的）。"""
    src = io.open(str(ROOT / "viewer" / "evidence.py"), encoding="utf-8").read()
    head = src[:6000]
    assert "TODO" in head and "MDD" in head
    assert "回撤未测" in head
