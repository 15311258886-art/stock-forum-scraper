# -*- coding: utf-8 -*-
"""AI 荐股板契约测试（2026-11-02）。

三个问题各钉一条，都带反例 —— 只测"正常路径能过"的测试等于没测：

1. `validate_picks` 必须抓住**凑数的占位票**（价位全 0 / 写「占位」字样）。
   来历：首跑真的吐了一只 entry/stop/target 全 0、thesis 写「占位」的票。
2. `picks_board` 的列表以**模型输出**为准，表现以**库里的回填**为准。
   两条都不能反过来：反过来要么占位票凭空消失、要么模型能自己填战绩。
3. `report` 必须输出**分档结构**（次日/T+3/T+5/T+10），且命中率与上涨率分开。
   只报一个"准确率"会把"看对了但账户是绿的"混成"看错了"。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "viewer")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- 1. 机检


def _pick(**kw):
    base = {
        "symbol": "000001", "name": "测试股", "score": 78,
        "score_breakdown": {"trend": 20, "money": 22, "position": 18, "catalyst": 18},
        "action": "买", "entry_low": 11.2, "entry_high": 11.55,
        "stop": 10.62, "target": 13.1, "horizon": 5,
        "thesis": "测试用逻辑", "why_now": "今天资金流入",
        "evidence": ["净流入 1.2 亿", "换手 8%"], "invalidate": "跌破 10.6",
    }
    base.update(kw)
    return base


def test_validate_accepts_a_complete_pick():
    import ai_review as AIR
    r = AIR.validate_picks({"picks": [_pick()]})
    assert r["clean"] is True and r["ok"] == 1


def test_validate_rejects_zero_price_placeholder():
    """反例：模型为凑数量写的全 0 占位票，必须被抓出来（而不是静默通过）。"""
    import ai_review as AIR
    bad = _pick(symbol="002555", entry_low=0, entry_high=0, stop=0, target=0,
                thesis="占位", why_now="占位", evidence=["占位1", "占位2"])
    r = AIR.validate_picks({"picks": [bad]})
    assert r["clean"] is False
    issues = " ".join(x["issue"] for x in r["bad"])
    assert "0" in issues and ("占位" in issues or "价位" in issues)


def test_validate_rejects_score_breakdown_mismatch():
    """反例：总分与四项分解对不上（先拍总分再倒推分项）。"""
    import ai_review as AIR
    r = AIR.validate_picks({"picks": [_pick(score=90)]})   # 分解仍合计 78
    assert r["clean"] is False
    assert any("分解" in x["issue"] for x in r["bad"])


def test_validate_rejects_stop_above_entry():
    """反例：止损设在买入价上方 = 一买就触发，落到实盘是钱。"""
    import ai_review as AIR
    r = AIR.validate_picks({"picks": [_pick(stop=12.0)]})
    assert r["clean"] is False
    assert any("止损" in x["issue"] for x in r["bad"])


def test_validate_flags_missing_picks_section():
    import ai_review as AIR
    assert AIR.validate_picks({})["clean"] is False
    assert AIR.validate_picks({"picks": []})["clean"] is False


# ---------------------------------------------------------------- 2. 票单板


@pytest.fixture()
def memdb():
    """最小内存库：ai_review + ai_call + 名称映射三张表。"""
    import review_score as RS
    import ai_review as AIR
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 两个模块都用「模块级 _ensured」做建表幂等，测试间必须重置，
    # 否则第二个用内存库的用例会以为表已经建过 → 报 no such table。
    RS._ensured = False
    AIR._ensured = False
    AIR.ensure_schema(conn)
    RS.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS stock_daily_snapshot "
                 "(code TEXT, name TEXT, trade_date TEXT)")
    conn.execute("INSERT INTO stock_daily_snapshot VALUES ('000001','测试股','2026-10-30')")
    yield conn
    conn.close()
    RS._ensured = False
    AIR._ensured = False


def _insert_review(conn, out: dict, rid: int = 1, date: str = "2026-10-30"):
    conn.execute(
        "INSERT INTO ai_review (id, review_date, provider, model, status, context_json, "
        "context_chars, output_json, raw_text, prompt_tokens, completion_tokens, "
        "elapsed_ms, err, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, date, "deepseek", "deepseek-chat", "ok", "{}", 100,
         json.dumps(out, ensure_ascii=False), None, 10, 10, 100, None, "2026-10-30 20:00:00"))
    conn.commit()


def test_record_skips_pick_without_prices(memdb):
    """价位不全的推荐**不入账** —— 否则它会以"永不触发的无效推荐"混进准确率分母。"""
    import review_score as RS
    out = {"picks": [
        _pick(),
        _pick(symbol="002555", entry_low=0, entry_high=0, stop=0, target=0),
    ]}
    _insert_review(memdb, out)
    n = RS.record_from_review(memdb, 1, "2026-10-30", "deepseek", out)
    rows = list(memdb.execute("SELECT symbol FROM ai_call WHERE kind='pick'"))
    assert n == 1, "只应入账 1 条（有价位的那条）"
    assert [r[0] for r in rows] == ["000001"]


def test_record_skips_pick_without_symbol(memdb):
    """没有代码的"票"（池名/板块名）不入账：无法验证的东西不该出现在统计里。"""
    import review_score as RS
    out = {"picks": [_pick(symbol="", name="低波池A级标的（98只）")]}
    _insert_review(memdb, out)
    RS.record_from_review(memdb, 1, "2026-10-30", "deepseek", out)
    assert memdb.execute("SELECT COUNT(*) FROM ai_call").fetchone()[0] == 0


def test_board_lists_from_model_output_not_db(memdb):
    """列表 = 模型输出（含被判不合格的那条），表现 = 库里回填。

    要点：那只占位票**必须在页面上出现**（让他看见模型凑数），
    但 tracked 必须是 False（表明它没进账本）。
    """
    import review_score as RS
    out = {"verdict": "测试", "picks": [
        _pick(),
        _pick(symbol="002555", name="凑数股", entry_low=0, entry_high=0,
              stop=0, target=0, thesis="占位"),
    ]}
    _insert_review(memdb, out)
    RS.record_from_review(memdb, 1, "2026-10-30", "deepseek", out)
    b = RS.picks_board(memdb)
    syms = [x["symbol"] for x in b["items"]]
    assert syms == ["000001", "002555"], "两条都要显示，且按分数降序"
    tracked = {x["symbol"]: x["tracked"] for x in b["items"]}
    assert tracked["000001"] is True
    assert tracked["002555"] is False, "占位票不能算作已入账"


def test_board_does_not_let_model_self_report_performance(memdb):
    """反例：模型在输出里编一个 n1_ret=+5%，不得被当成真实表现。"""
    import review_score as RS
    out = {"picks": [dict(_pick(), n1_ret=5.0, n1_hit=1, n1={"ret": 5.0, "hit": 1})]}
    _insert_review(memdb, out)
    RS.record_from_review(memdb, 1, "2026-10-30", "deepseek", out)
    b = RS.picks_board(memdb)
    n1 = b["items"][0]["n1"] or {}
    # 要点不是"必须是 None"，而是**没有任何一个字段带上模型编的那个 5.0**：
    # 库里这一条还没回填，所以各档位全空。若哪天有人图省事直接读 output_json 的
    # 同名字段，这里会立刻变成 5.0 并红掉。
    assert n1.get("ret") is None
    assert n1.get("hit") is None


# ---------------------------------------------------------------- 3. 成绩单


def _seed_scored(conn):
    """一条已回填的看多：次日 +2%（超额 +1%）、T+5 -3%（超额 -4%）。"""
    conn.execute(
        "INSERT INTO ai_call (review_id,review_date,provider,kind,symbol,name,stance,"
        "score,status,base_date,base_close,n1_ret,n1_ex,n1_hit,h5_ret,h5_ex,h5_hit,proxy,created_at) "
        "VALUES (1,'2026-10-28','deepseek','pick','000001','测试股','bull',70,'scored',"
        "'2026-10-29',10.0,2.0,1.0,1,-3.0,-4.0,0,0,'2026-10-30 20:00:00')")
    conn.commit()


def test_report_splits_hit_rate_and_up_rate(memdb):
    """命中率（超额>0）与上涨率（绝对>0）必须分开统计。

    这条样本：次日超额 +1% → 命中；T+5 超额 -4% → 不命中。
    只报一个数字的实现在这里会露馅。
    """
    import review_score as RS
    _seed_scored(memdb)
    r = RS.report(memdb)
    ov = r["overall"]
    assert ov["horizons"]["n1"]["hit"] == 100.0
    assert ov["horizons"]["n1"]["up"] == 100.0
    assert ov["horizons"]["n1"]["ret"] == pytest.approx(2.0, abs=0.01)
    assert ov["horizons"]["n1"]["ex"] == pytest.approx(1.0, abs=0.01)
    assert ov["horizons"]["h5"]["hit"] == 0.0
    assert ov["horizons"]["h5"]["ex"] == pytest.approx(-4.0, abs=0.01)


def test_report_marks_small_sample(memdb):
    import review_score as RS
    _seed_scored(memdb)
    r = RS.report(memdb)
    assert r["min_sample_ok"] is False, "1 条样本不能冒充准确率"


def test_scoreboard_text_mentions_next_day(memdb):
    """给模型看的成绩单必须包含「次日」——否则它永远看不到昨天的对错。"""
    import review_score as RS
    _seed_scored(memdb)
    txt = RS.scoreboard_text(memdb)
    assert "次日" in txt
    assert "2026-10-28" in txt


def test_score_pending_voids_codeless_rows(memdb):
    """反例：没有代码的个股条目不能永远挂在 open 里充"待验证"。"""
    import review_score as RS
    conn = memdb
    conn.execute("CREATE TABLE IF NOT EXISTS stock_indicators_daily "
                 "(code TEXT, date TEXT, close REAL)")
    conn.execute("INSERT INTO stock_indicators_daily VALUES ('000001','2026-10-29',10.0)")
    conn.execute("INSERT INTO stock_indicators_daily VALUES ('000001','2026-10-30',10.2)")
    conn.execute(
        "INSERT INTO ai_call (review_id,review_date,provider,kind,symbol,name,stance,"
        "created_at) VALUES (1,'2026-10-28','deepseek','opp','','低波池A级标的（98只）',"
        "'bull','2026-10-28 20:00:00')")
    conn.commit()
    RS.score_pending(conn)
    st = conn.execute("SELECT status, void_reason FROM ai_call").fetchone()
    assert st["status"] == "void"
    assert "代码" in (st["void_reason"] or "")
