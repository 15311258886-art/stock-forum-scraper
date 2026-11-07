# -*- coding: utf-8 -*-
"""多智能体辩论研究（viewer/debate.py）离线单测。

不联网、不烧真实 Key：用 mock 替换 `debate._ask`（模型调用）与 `debate._backdrop`
（市场背景，避免依赖 ai_review 的全量上下文），只验证编排与契约：

  1. 形态 A（盘后批量）：候选池 → 分析师 → 辩论 → 票单 → 风控，终推数落在 [final_min, final_max]，
     且每只票都通过 ai_review.validate_picks 机检、代码为 6 位、结果落 ai_debate。
  2. 形态 B（单股深研）：只推 1 只，代码与输入一致。
  3. 候选池选择：ST / 停牌被剔除。
"""
from __future__ import annotations

import sqlite3

import pytest

from viewer import debate


# ---------------------------------------------------------------- fixtures


def _make_db(n: int = 60):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE stock_universe_scan(
            code TEXT PRIMARY KEY, name TEXT, score REAL, rank_pct REAL, tier TEXT,
            momentum REAL, volume REAL, quality REAL, reasons TEXT, updated TEXT);
        CREATE TABLE stock_indicators(
            code TEXT PRIMARY KEY, name TEXT, date TEXT, close REAL, ma20 REAL, ma60 REAL,
            macd REAL, macdh REAL, rsi6 REAL, atr REAL, volume_ratio REAL, bias12 REAL,
            adx REAL, supertrend REAL);
        CREATE TABLE stock_signals(
            code TEXT PRIMARY KEY, name TEXT, date TEXT,
            buy_score REAL, sell_score REAL, buy_reasons TEXT, sell_reasons TEXT);
        CREATE TABLE stock_daily_snapshot(
            trade_date TEXT, code TEXT, name TEXT, close REAL, pct REAL, turnover REAL,
            amount REAL, pe_ttm REAL, pb REAL, total_mv REAL, PRIMARY KEY(trade_date, code));
        CREATE TABLE fundamentals(
            symbol TEXT PRIMARY KEY, name TEXT, roe REAL, gm REAL, rev_yoy REAL, np_yoy REAL);
        CREATE TABLE stock_mentions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, symbol_name TEXT, context TEXT);
        CREATE TABLE stock_strategy_picks(
            template TEXT, code TEXT, name TEXT, score REAL, reasons TEXT, updated TEXT,
            PRIMARY KEY(template, code));
        CREATE TABLE suspended(code TEXT PRIMARY KEY, since TEXT, note TEXT);
        """
    )
    for i in range(n):
        code = "600%03d" % i
        name = "测试%02d" % i
        rp = 1.0 - i / (n + 1.0)
        tier = "A" if rp >= 0.9 else ("B" if rp >= 0.7 else "C")
        conn.execute(
            "INSERT INTO stock_universe_scan VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, name, 0.5 + rp * 0.3, rp, tier, 0.6, 0.5, 0.4, '["站上MA20"]', "2026-10-31"))
        conn.execute(
            "INSERT INTO stock_indicators VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, name, "2026-10-31", 10.0 + i, 9.8 + i, 9.5 + i,
             0.2, 0.1, 55.0, 0.3, 1.8, 2.0, 25.0, 9.9 + i))
        conn.execute(
            "INSERT INTO stock_signals VALUES(?,?,?,?,?,?,?)",
            (code, name, "2026-10-31", 62.0, 30.0, "量价齐升", "无"))
        conn.execute(
            "INSERT INTO stock_daily_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-31", code, name, 10.0 + i, 2.1, 3.2, 5e8, 15.0, 2.0, 1e10))
        conn.execute(
            "INSERT INTO fundamentals VALUES(?,?,?,?,?,?)",
            (code, name, 12.0, 30.0, 10.0, 8.0))
        conn.execute(
            "INSERT INTO stock_strategy_picks VALUES(?,?,?,?,?,?)",
            ("放量", code, name, 0.6, "[]", "2026-10-31"))
        conn.execute(
            "INSERT INTO stock_mentions(symbol, symbol_name, context) VALUES(?,?,?)",
            (code, name, "讨论热度不错"))
    # 噪音：ST + 停牌，应被剔除
    conn.execute("INSERT INTO stock_universe_scan VALUES('600999','ST退市','0.9',0.999,'A',0.9,0.9,0.9,'[]','2026-10-31')")
    conn.execute("INSERT INTO stock_indicators(code,name,date,close) VALUES('600999','ST退市','2026-10-31',1.0)")
    conn.execute("INSERT INTO suspended VALUES('600998','2026-10-20','停牌')")
    conn.execute("INSERT INTO stock_universe_scan VALUES('600998','停牌股','0.8',0.98,'A',0.8,0.8,0.8,'[]','2026-10-31')")
    conn.commit()
    return conn


def _fake_ask(provider, system, user, retries=1):
    if "首席分析师" in system:
        return ({"fundamental": {"stance": "偏多", "points": ["PE15 估值合理"], "score": 70},
                 "sentiment": {"stance": "中性", "points": ["提及量一般"], "score": 55},
                 "technical": {"stance": "偏多", "points": ["站上MA20"], "score": 72},
                 "sector": {"stance": "中性", "points": ["板块震荡"], "score": 58},
                 "pre_score": 68, "key_risk": "量能不足"}, {"prompt": 120, "completion": 60})
    if "辩论主持人" in system:
        return ({"bull_case": ["站上MA20且MACD在零轴上方"], "bear_case": ["量比仅1.8，跟风盘不足"],
                 "clash": [{"point": "量能", "bull": "温和放量", "bear": "不足以突破"}],
                 "judge": {"stance": "看好", "adjusted_score": 71, "confidence": 70, "one_line": "偏多但需放量"}},
                {"prompt": 120, "completion": 60})
    if "交易员" in system:
        return ({"symbol": "600000", "name": "x", "score": 70,
                 "score_breakdown": {"trend": 20, "money": 18, "position": 16, "catalyst": 16},
                 "action": "买", "entry_low": 10.0, "entry_high": 10.5, "stop": 9.6,
                 "target": 11.5, "horizon": 5, "thesis": "均线多头", "why_now": "放量突破",
                 "evidence": ["主力净流入1.2亿", "量比1.8"], "invalidate": "跌破MA20"},
                {"prompt": 120, "completion": 60})
    if "组合风控" in system:
        return ({"approved": [], "portfolio_note": "注意单票集中度"}, {"prompt": 80, "completion": 40})
    return ({}, {"prompt": 0, "completion": 0})


@pytest.fixture()
def patched(monkeypatch):
    debate._reset_ensured()
    monkeypatch.setattr(debate, "_ask", _fake_ask)
    monkeypatch.setattr(debate, "_backdrop",
                        lambda conn, date, provider: {"market_line": "结构市；全市场涨2500跌2000",
                                                      "doctrine_text": "不追高，单票不超20%",
                                                      "portfolio": []})
    monkeypatch.setattr(debate, "_resolve_provider", lambda p: "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")  # 让 provider_key() 非空，跳过 Key 检查
    yield
    debate._reset_ensured()


# ---------------------------------------------------------------- 形态 A


def test_batch_pipeline(patched):
    conn = _make_db(60)
    res = debate.debate_research(conn, date="2026-10-31", pool=50,
                                 debate_top=30, plan_top=20,
                                 final_min=15, final_max=18, concurrency=4)
    assert res["ok"] is True, res
    d = res["data"]
    assert 15 <= d["final"] <= 18, d["final"]
    assert d["final"] == len(d["picks"])
    # 每只票都通过机检，且代码为 6 位
    from viewer import ai_review as ar
    for p in d["picks"]:
        assert len(str(p["symbol"])) == 6 and str(p["symbol"]).isdigit()
        assert ar.validate_picks({"picks": [p]})["clean"] is True
    # 候选池里 ST / 停牌已被剔除
    assert all(p["symbol"] not in ("600999", "600998") for p in d["picks"])
    # 落库
    row = conn.execute("SELECT status, produced, pool FROM ai_debate ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "ok" and row["produced"] == d["final"] and row["pool"] == 50


# ---------------------------------------------------------------- 形态 B


def test_single_stock(patched):
    conn = _make_db(5)
    res = debate.single_stock_research(conn, "600001")
    assert res["ok"] is True, res
    d = res["data"]
    assert d["pick_valid"] is True
    assert d["pick"]["symbol"] == "600001"
    row = conn.execute("SELECT mode, run_key FROM ai_debate WHERE mode='single'").fetchone()
    assert row["run_key"] == "600001"


# ---------------------------------------------------------------- 候选池剔除噪音


def test_candidates_exclude_st_and_suspended(patched):
    conn = _make_db(30)
    codes = [c["code"] for c in debate.select_candidates(conn, pool=50)]
    assert "600999" not in codes       # ST
    assert "600998" not in codes       # 停牌
    assert len(codes) == 30            # 30 只干净票


# ---------------------------------------------------------------- 空池兜底


def test_empty_pool_without_crash(patched):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    res = debate.debate_research(conn, date="2026-10-31", pool=50)
    assert res["ok"] is False
    assert res["error"] == "empty_pool"
