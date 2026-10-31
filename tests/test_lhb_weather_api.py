# -*- coding: utf-8 -*-
"""`/api/lhb_weather` 的口径单测（2026-10-30 D35 阶段二）。

只测**接口契约**，不测计算（计算在 `test_lhb_weather.py`）。这里要钉死的是四件事：

1. 表不存在时回「尚未落库」的中性说明，不是 500，也不要冒充有数据；
2. 四维卡里结构与集中度**必须是 `pct=None` + 角标**（样本只有 27/25 天，给分位就是编数据）；
3. 底部免责声明必须在返回体里，且返回体不含任何买卖暗示词（D35 §6 的产品边界）；
4. 「今日」取最后一行**有分值**的日期 —— 表里最后一行可能是当天上榜数据到了、
   但全市场成交额还没到的半成品（实测 2026-10-30 就是这样），不能被当成"今天"。

全部跑在 tmp 库上；`server.DB` 用一个只有 `.conn` 的壳子替换，生产 `viewer/app.db` 全程只读。
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from viewer import lhb_weather as LW
from viewer import server as S

_PROD_DB = os.path.abspath(os.path.join(os.path.dirname(S.__file__), "app.db"))


class _FakeDB:
    """替身：`api_lhb_weather` 只用到 `.conn`。"""

    def __init__(self, conn):
        self.conn = conn


def _mkdb(tmp_path, rows, with_table=True):
    con = sqlite3.connect(str(tmp_path / "t.db"))
    if not with_table:
        return con
    LW.ensure_schema(con)
    cols = ["trade_date", "n_listed", "denom", "denom_src", "list_ratio", "net_amt",
            "market_amount", "net_amt_ratio", "part_pct", "d1_mean_20", "d1_win_20",
            "effect_pct", "score", "q33", "q67", "state", "inst_net", "hot_net",
            "inst_share", "top3_share", "hhi", "sample_days_structure",
            "sample_days_sector", "updated_at"]
    con.executemany(
        "INSERT INTO %s (%s) VALUES (%s)" % (LW.TABLE, ",".join(cols), ",".join("?" * len(cols))),
        [tuple(r.get(c) for c in cols) for r in rows])
    con.commit()
    return con


def _day(i, date, score, state="neutral", **kw):
    """造一行；缺省字段给一份"看起来正常"的值。"""
    r = {"trade_date": date, "n_listed": 60, "denom": 5000, "denom_src": "indicators",
         "list_ratio": 0.012, "net_amt": 1.2e9, "market_amount": 1.5e12,
         "net_amt_ratio": 0.0008, "part_pct": 55.0, "d1_mean_20": 0.31,
         "d1_win_20": 0.52, "effect_pct": 48.0, "score": score, "q33": 35.0, "q67": 54.0,
         "state": state, "inst_net": -2.0e8, "hot_net": 5.0e8, "inst_share": -0.28,
         "top3_share": 0.61, "hhi": 0.12, "sample_days_structure": 27,
         "sample_days_sector": 25, "updated_at": "2026-10-30 19:05:00"}
    r.update(kw)
    return r


def _history(n=70, start_day=1):
    """n 个连续交易日、分值在 10~80 之间来回。"""
    out = []
    for i in range(n):
        d = "2026-%02d-%02d" % (1 + (i // 28), 1 + (i % 28))
        out.append(_day(i, d, float(10 + (i * 7) % 70)))
    return out


@pytest.fixture()
def patch_db(tmp_path, monkeypatch):
    def _apply(rows, with_table=True):
        con = _mkdb(tmp_path, rows, with_table)
        monkeypatch.setattr(S, "DB", _FakeDB(con))
        return con
    return _apply


def test_not_ready_when_table_missing(patch_db):
    """表没建 → 明确说「尚未落库」并给出该跑什么命令，而不是 500 或空面板。"""
    patch_db([], with_table=False)
    p = S.api_lhb_weather(120)
    assert p["ok"] is False and p["not_ready"] is True
    assert "--save" in p["note"]
    assert p["series"] == [] and p["dims"] == []


def test_short_sample_dims_have_no_percentile_and_carry_badge(patch_db):
    """结构与集中度：`pct` 必须是 None、必须带「仅供参考，不参与判定」角标。

    它们只有 27/25 天样本（远低于 `MIN_SAMPLE_FOR_STATE=250`）。给这两维算一个
    "250 日分位"就是在编数据 —— 这条测试就是防止后续迭代"顺手补个分位"。
    """
    patch_db(_history())
    p = S.api_lhb_weather(120)
    assert p["ok"] is True, p.get("error")
    assert [d["key"] for d in p["dims"]] == ["participation", "effect", "structure", "concentration"]
    assert p["dims"][0]["pct"] is not None and p["dims"][1]["pct"] is not None
    for dm in p["dims"][2:]:
        assert dm["in_state"] is False
        assert dm["pct"] is None and dm["pct_label"] is None
        assert "仅供参考，不参与判定" in dm["badge"]
        assert dm["sample_days"] < LW.MIN_SAMPLE_FOR_STATE
    assert p["min_sample_for_state"] == LW.MIN_SAMPLE_FOR_STATE == 250
    assert p["sample_days"] == {"structure": 27, "sector": 25}


def test_disclaimer_present_and_no_advice_words(patch_db):
    """底部免责必须在；返回体里不许出现买卖暗示（产品边界，写在 D35 §6）。"""
    patch_db(_history())
    p = S.api_lhb_weather(120)
    assert p["disclaimer"] == "本指标描述资金活跃度，不构成个股买卖依据。"
    body = str(p)
    # 「净买入」是交易所口径的名词，不算暗示；下面这些才是。
    for bad in ("建议", "看多", "看空", "可以买", "值得买", "推荐", "止损", "止盈", "目标价"):
        assert bad not in body, "返回体里出现了买卖暗示词：%s" % bad
    # 序列只回约定的三个字段，别偷偷塞收益
    assert all(set(x) == {"trade_date", "score", "state"} for x in p["series"])


def test_today_is_last_scored_row_and_pending_is_reported(patch_db):
    """表里最后一行没分值 → 今日取上一个有分值的日期，并把那一行如实报成 pending。

    实测背景：2026-10-30 当天已有 48 只上榜，但 `data/kline_daily.db` 的当日成交额
    与分母都还没到，`compute()` 给出 score=NULL / denom_src=fallback。如果直接取
    rows[-1]，面板会显示一个空温度计，看起来像功能坏了。
    """
    rows = _history(70)
    rows.append(_day(99, "2026-12-31", None, state="unknown", denom_src="fallback",
                     n_listed=48, market_amount=None, net_amt_ratio=None,
                     part_pct=None, effect_pct=None))
    patch_db(rows)
    p = S.api_lhb_weather(120)
    assert p["ok"] is True, p.get("error")
    assert p["asof"] == rows[-2]["trade_date"]
    assert p["today"]["score"] is not None
    assert p["pending"] is not None
    assert p["pending"]["trade_date"] == "2026-12-31"
    assert p["pending"]["n_listed"] == 48
    assert p["series"][-1]["trade_date"] == rows[-2]["trade_date"]


def test_days_is_clamped(patch_db):
    """`days` 夹在 20~400，曲线长度不超过表里的行数。"""
    patch_db(_history())
    assert S.api_lhb_weather(5)["days"] == 20
    assert S.api_lhb_weather(9999)["days"] == 400
    p = S.api_lhb_weather(30)
    assert len(p["series"]) == 30


def test_never_touches_prod_db(tmp_path, patch_db):
    """生产库 mtime 不得变化（本文件所有用例都跑 tmp）。"""
    before = os.path.getmtime(_PROD_DB) if os.path.exists(_PROD_DB) else None
    patch_db(_history())
    S.api_lhb_weather(120)
    after = os.path.getmtime(_PROD_DB) if os.path.exists(_PROD_DB) else None
    assert before == after
