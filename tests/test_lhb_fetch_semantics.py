# -*- coding: utf-8 -*-
"""D33 回归：lhb_fetch 的「明细失败」必须非 0 退出，「抓回 0 行」必须 0 退出。

背景（2026-10-30 定位）：2026-10-29 的 lhb_seats / lhb_depts / lhb_inst 全是 0 行，
而 lhb_stocks（主榜）有 75 行。根因不是接口没数据，而是 `main()` 只把「抛异常」当失败、
把明细接口的**结构性拒绝**（东财 code≠0，例如 9501「报表配置不存在」、限流）当成
「今天没数据」吞掉了 —— 这一轮照样 `return 0`，`lhb_daily.record_result` 记为
`ok=true` 并写 `last_date=今天`，当天再也不会重试，那张表就**永久缺一天**。

反向风险同样要防：「该日确实没有机构上榜」时东财返回
`success=False, code=9201, message='返回数据为空'`，与真失败在 HTTP 状态和返回体形状上
几乎一样。若用「抓到几行」当判据，就会天天误报失败、白烧 MAX_TRIES。唯一可靠判据是
**code**：9201 = 合法空结果，其它非 0 = 真失败（实测 2026-10-30：非交易日 2026-10-25
→ 9201；报表名打错 → 9501；正常 → 0）。

本文件全部跑在**临时 sqlite** 上，`_get_json` 被替换成内存桩，不联网、不碰生产库。
覆盖点：
  1. `_rows(strict=True)`：9201 → 返回 []，9501 → 抛 LhbApiError
  2. 明细接口抛异常 → `main()` 返回 1
  3. 明细接口正常但 0 行 → `main()` 返回 0（**不误判**）
  4. 明细失败时**不清空**库里已有明细（否则一次抖动抹掉 --days 8 覆盖的 8 天）
  5. 明细失败时主榜仍然落库（用户至少还能看榜单）
  6. `lhb_daily` 的重试闸门是 `last_date`（仅 ok 时写），`last_run_at` 不参与，
     所以 rc=1 确实能换来最多 MAX_TRIES 次重试
"""
from __future__ import annotations

import importlib
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"

# 与生产完全一致的两张表（lhb_depts / lhb_inst 由 lhb_fetch.ensure_schema 自建，
# 这里不用抄；lhb_seats 是历史遗留表，ensure_schema 不管它，必须由测试自己建）
DDL_STOCKS = """
CREATE TABLE lhb_stocks(
    trade_date TEXT NOT NULL DEFAULT '',
    security_code TEXT NOT NULL DEFAULT '',
    security_name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    close_price REAL,
    change_rate REAL,
    turnoverrate REAL,
    buy_amt REAL,
    sell_amt REAL,
    net_amt REAL,
    deal_amt REAL,
    deal_net_ratio REAL,
    free_market_cap REAL,
    reason TEXT NOT NULL DEFAULT '',
    explain TEXT NOT NULL DEFAULT '',
    d1_chg REAL,
    d2_chg REAL,
    d5_chg REAL,
    d10_chg REAL,
    d20_chg REAL,
    d30_chg REAL,
    buy_seat TEXT,
    sell_seat TEXT,
    sum_buy_amt REAL,
    sum_sell_amt REAL,
    net_bs_amt REAL,
    deal_amount_ratio REAL,
    disclose_at TEXT DEFAULT '',
    PRIMARY KEY(trade_date, security_code, reason)
)"""

DDL_SEATS = """
CREATE TABLE lhb_seats(
    trade_date TEXT, security_code TEXT, operate_code TEXT, operate_name TEXT,
    side TEXT, buy REAL, sell REAL, net REAL, rise_prob_3d REAL,
    PRIMARY KEY(trade_date, security_code, operate_code, side)
)"""


@pytest.fixture()
def lf():
    """lhb_fetch 模块（纯 import，不跑 main）。"""
    return importlib.import_module("lhb_fetch")


@pytest.fixture()
def db(tmp_path):
    """空临时库：只建 lhb_stocks + lhb_seats 两张「前置」表。"""
    p = tmp_path / "app.db"
    con = sqlite3.connect(p)
    con.executescript(DDL_STOCKS + ";" + DDL_SEATS + ";")
    con.commit()
    con.close()
    return p


# ---------------------------------------------------------------- 桩数据
def _day_of(url: str) -> str:
    """从 filter=(TRADE_DATE='YYYY-MM-DD') 里取出本次要抓的日子。"""
    m = re.search(r"TRADE_DATE='(\d{4}-\d{2}-\d{2})'", url)
    return m.group(1) if m else "2026-10-29"


def _report_of(url: str) -> str:
    m = re.search(r"reportName=([A-Za-z0-9_]+)", url)
    return m.group(1) if m else ""


def _ok(rows: list[dict]) -> dict:
    """东财正常返回：code=0 且 result 是字典。"""
    return {"version": "abc", "result": {"pages": 1, "data": rows, "count": len(rows)},
            "success": True, "message": "ok", "code": 0}


def _empty() -> dict:
    """东财「该日没有数据」：success=False 但 code=9201 —— 合法空结果，不是失败。"""
    return {"version": "abc", "result": None, "success": False,
            "message": "返回数据为空", "code": 9201}


def _bad_report() -> dict:
    """东财「报表配置不存在」：code=9501 —— 真失败，正是明细悄悄缺一天的典型成因。"""
    return {"version": "abc", "result": None, "success": False,
            "message": "报表配置不存在,RPT_XXX", "code": 9501}


def _main_row(day: str) -> dict:
    return {
        "TRADE_DATE": day, "SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台",
        "MARKET": "1", "CLOSE_PRICE": 1500.0, "CHANGE_RATE": 10.0, "TURNOVERRATE": 1.2,
        "BILLBOARD_BUY_AMT": 1.0e8, "BILLBOARD_SELL_AMT": 5.0e7,
        "BILLBOARD_NET_AMT": 5.0e7, "BILLBOARD_DEAL_AMT": 1.5e8,
        "DEAL_NET_RATIO": 3.3, "FREE_MARKET_CAP": 1.0e10,
        "EXPLANATION": "日涨幅偏离值达7%", "EXPLAIN": "营业部买入",
        "D1_CLOSE_ADJCHRATE": 1.0, "D2_CLOSE_ADJCHRATE": 2.0, "D5_CLOSE_ADJCHRATE": 3.0,
        "D10_CLOSE_ADJCHRATE": None, "D20_CLOSE_ADJCHRATE": None,
        "D30_CLOSE_ADJCHRATE": None,
        "BUY_SEAT": "1", "SELL_SEAT": "2",
        "SUM_BUY_AMT": 1.0e8, "SUM_SELL_AMT": 5.0e7,
        "NET_BS_AMT": 5.0e7, "DEAL_AMOUNT_RATIO": 12.5,
    }


def _seat_row(day: str, side: str) -> dict:
    return {"TRADE_DATE": day, "SECURITY_CODE": "600519",
            "OPERATEDEPT_CODE": "1001", "OPERATEDEPT_NAME": "某游资席位",
            "BUY": 1.0e7 if side == "BUY" else 0.0,
            "SELL": 0.0 if side == "BUY" else 2.0e7,
            "NET": 1.0e7 if side == "BUY" else -2.0e7,
            "RISE_PROBABILITY_3DAY": 55.5}


def _dept_row(day: str) -> dict:
    return {"TRADE_DATE": day, "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台", "OPERATEDEPT_CODE": "1001",
            "OPERATEDEPT_NAME": "某游资席位", "ORG_NAME_ABBR": "某营业部",
            "ACT_BUY": 1.0e7, "ACT_SELL": 0.0, "NET_AMT": 1.0e7,
            "EXPLANATION": "日涨幅偏离值达7%",
            "D1_CLOSE_ADJCHRATE": 1.0, "D3_CLOSE_ADJCHRATE": 2.0,
            "D5_CLOSE_ADJCHRATE": 3.0, "D10_CLOSE_ADJCHRATE": None,
            "D20_CLOSE_ADJCHRATE": None, "D30_CLOSE_ADJCHRATE": None}


def _inst_row(day: str) -> dict:
    return {"TRADE_DATE": day, "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台", "BUY_TIMES": 2, "SELL_TIMES": 1,
            "BUY_AMT": 3.0e7, "SELL_AMT": 1.0e7, "NET_BUY_AMT": 2.0e7, "RATIO": 4.4,
            "EXPLANATION": "机构专用", "D1_CLOSE_ADJCHRATE": 1.0,
            "D3_CLOSE_ADJCHRATE": 2.0, "D5_CLOSE_ADJCHRATE": 3.0,
            "D10_CLOSE_ADJCHRATE": None}


def _patch_get_json(monkeypatch, lf, detail: str) -> None:
    """把 `_get_json` 换成内存桩。

    detail:
      'ok'         —— 明细正常有数据
      'empty'      —— 明细返回 code=9201（该日确实没记录，合法空结果）
      'raise'      —— 明细接口抛 OSError（模拟 HTTP 非 200 / 连接重置 / 解析失败）
      'bad_report' —— 明细返回 code=9501（报表配置不存在）
    """

    def fake_get_json(url, timeout=15, retries=2):  # noqa: ARG001
        report = _report_of(url)
        day = _day_of(url)
        if report == MAIN_REPORT:
            return _ok([_main_row(day)])
        if detail == "raise":
            raise OSError("模拟：明细接口连接被重置（HTTP 非 200）")
        if detail == "empty":
            return _empty()
        if detail == "bad_report":
            return _bad_report()
        if report.startswith("RPT_BILLBOARD_DAILYDETAILS"):
            side = "BUY" if report.endswith("BUY") else "SELL"
            return _ok([_seat_row(day, side)])
        if report == "RPT_OPERATEDEPT_TRADE_DETAILS":
            return _ok([_dept_row(day)])
        return _ok([_inst_row(day)])

    monkeypatch.setattr(lf, "_get_json", fake_get_json)


def _run_main(monkeypatch, lf, db_path: Path, days: int = 1) -> int:
    monkeypatch.setattr(sys, "argv",
                        ["lhb_fetch.py", "--days", str(days), "--db", str(db_path)])
    return lf.main()


def _count(db_path: Path, table: str, day: str | None = None) -> int:
    con = sqlite3.connect(db_path)
    try:
        if day is None:
            return con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM %s WHERE trade_date=?" % table,
                           (day,)).fetchone()[0]
    finally:
        con.close()


TODAY = datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 1. 判据本身
def test_rows_9201_is_legit_empty(monkeypatch, lf):
    """code=9201（返回数据为空）在 strict 下**不能**抛异常 —— 它只是没数据的日子。"""
    monkeypatch.setattr(lf, "_get_json", lambda url, timeout=15, retries=2: _empty())
    assert lf._rows("http://stub", strict=True) == []


def test_rows_9501_is_failure(monkeypatch, lf):
    """code=9501（报表配置不存在）在 strict 下必须抛 LhbApiError。"""
    monkeypatch.setattr(lf, "_get_json", lambda url, timeout=15, retries=2: _bad_report())
    with pytest.raises(lf.LhbApiError):
        lf._rows("http://stub", strict=True)


def test_rows_malformed_envelope_is_failure(monkeypatch, lf):
    """code=0 但 result 不是字典（结构变更）也要按失败处理，不能静默写 0 行。"""
    monkeypatch.setattr(lf, "_get_json",
                        lambda url, timeout=15, retries=2: {"code": 0, "result": None})
    with pytest.raises(lf.LhbApiError):
        lf._rows("http://stub", strict=True)


def test_rows_lenient_mode_keeps_old_behavior(monkeypatch, lf):
    """strict=False（默认）保持原行为：不抛，返回 []，供运维脚本宽松调用。"""
    monkeypatch.setattr(lf, "_get_json", lambda url, timeout=15, retries=2: _bad_report())
    assert lf._rows("http://stub") == []


# ---------------------------------------------------------------- 2. 场景 A：异常 → rc≠0
def test_detail_exception_makes_main_nonzero(monkeypatch, lf, db, capsys):
    """明细接口抛异常 → main() 必须返回 1（让 lhb_daily 记 ok=false 并重试）。"""
    _patch_get_json(monkeypatch, lf, "raise")
    rc = _run_main(monkeypatch, lf, db)
    out = capsys.readouterr().out
    assert rc == 1, "明细接口异常被当成成功（rc=0），当天数据会永久缺失"
    assert "明细接口失败" in out
    assert "视为失败" in out, "日志必须明确写出「视为失败」，否则运维以为只是没数据"


def test_detail_bad_report_makes_main_nonzero(monkeypatch, lf, db):
    """code=9501（报表改名）同样要 rc=1。"""
    _patch_get_json(monkeypatch, lf, "bad_report")
    assert _run_main(monkeypatch, lf, db) == 1


# ---------------------------------------------------------------- 3. 场景 B：0 行 → rc=0
def test_detail_legit_empty_keeps_main_zero(monkeypatch, lf, db, capsys):
    """明细接口正常但 0 行 → main() 必须返回 0，**不能误判成失败**。"""
    _patch_get_json(monkeypatch, lf, "empty")
    rc = _run_main(monkeypatch, lf, db)
    out = capsys.readouterr().out
    assert rc == 0, "「该日无记录」被误判成失败，会天天白烧 MAX_TRIES"
    assert "不算失败" in out, "日志要写清「0 行不算失败」，与真失败区分"
    assert _count(db, "lhb_seats", TODAY) == 0


# ---------------------------------------------------------------- 4. 失败不得清空历史
def test_detail_failure_does_not_wipe_existing_rows(monkeypatch, lf, db):
    """db 里当已有明细时，抓失败**不能**先 DELETE —— 否则一次抖动抹掉 --days 8 的 8 天。"""
    con = sqlite3.connect(db)
    con.execute("INSERT INTO lhb_seats VALUES (?,?,?,?,?,?,?,?,?)",
                (TODAY, "600519", "1001", "旧席位", "BUY", 1.0, 0.0, 1.0, 50.0))
    con.commit()
    con.close()
    assert _count(db, "lhb_seats", TODAY) == 1

    _patch_get_json(monkeypatch, lf, "raise")
    assert _run_main(monkeypatch, lf, db) == 1
    assert _count(db, "lhb_seats", TODAY) == 1, "抓失败还把旧明细删了，等于用空数据覆盖好数据"


# ---------------------------------------------------------------- 5. 主榜仍要落库
def test_main_board_still_written_when_details_fail(monkeypatch, lf, db):
    """明细失败时主榜照常落库：用户至少还能看到「谁上榜了」。"""
    _patch_get_json(monkeypatch, lf, "raise")
    _run_main(monkeypatch, lf, db)
    assert _count(db, "lhb_stocks", TODAY) == 1


# ---------------------------------------------------------------- 6. 正常路径
def test_happy_path_writes_four_tables(monkeypatch, lf, db):
    """四张表都正常时有数据、rc=0（防止修复把正常路径改坏）。"""
    _patch_get_json(monkeypatch, lf, "ok")
    assert _run_main(monkeypatch, lf, db) == 0
    assert _count(db, "lhb_stocks", TODAY) == 1
    assert _count(db, "lhb_seats", TODAY) == 2, "买入+卖出两个 report 各一行"
    assert _count(db, "lhb_depts", TODAY) == 1
    assert _count(db, "lhb_inst", TODAY) == 1


def test_rerun_is_idempotent(monkeypatch, lf, db):
    """幂等：连跑两次，行数不变（先删后插）。"""
    _patch_get_json(monkeypatch, lf, "ok")
    assert _run_main(monkeypatch, lf, db) == 0
    assert _run_main(monkeypatch, lf, db) == 0
    assert _count(db, "lhb_stocks", TODAY) == 1
    assert _count(db, "lhb_seats", TODAY) == 2


# ---------------------------------------------------------------- 7. 重试闸门
@pytest.fixture()
def ld(monkeypatch, tmp_path):
    """lhb_daily 指向临时库；时间窗强制放行；日志不落盘；run_once 打成桩。

    2026-10-30 D33：`maybe_daily()` 已改为**同步**（起 daemon 线程在短命子进程里会被腰斩，
    见 `tests/test_lhb_daily_offproc.py`），所以它现在会真的去跑三个子脚本 —— 测试里必须把
    `run_once` 换成桩，否则会去打真实接口。返回值代表"跑完一轮"。
    """
    mod = importlib.import_module("lhb_daily")
    monkeypatch.setattr(mod, "DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(mod, "_append_result_log", lambda res: None)
    monkeypatch.setattr(mod, "_is_weekday", lambda now: True)
    monkeypatch.setattr(mod, "_past_publish", lambda now: True)
    mod._running["v"] = False
    mod._calls = []

    def fake_run_once(*a, **k):  # noqa: ARG001
        mod._calls.append(1)
        return {"ok": True, "started": "2026-10-30 18:00:00",
                "ended": "2026-10-30 18:08:04", "steps": []}

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    return mod


def _fail_result(script="lhb_fetch.py") -> dict:
    return {"ok": False, "started": "2026-10-30 18:00:00", "ended": "2026-10-30 18:01:00",
            "steps": [{"script": script, "returncode": 1}]}


def _ok_result() -> dict:
    return {"ok": True, "started": "2026-10-30 18:00:00", "ended": "2026-10-30 18:01:00",
            "steps": [{"script": "lhb_fetch.py", "returncode": 0}]}


def test_failed_round_does_not_mark_today_done(ld):
    """关键回归：rc=1 那一轮**不能**写 last_date，否则当天不再重试（2026-10-29 就是这么丢的）。"""
    ld.record_result(_fail_result(), TODAY)
    state = ld._state_read(sqlite3.connect(ld.DB))
    assert state.get("last_run_at"), "last_run_at 仍须写入（供巡检看「什么时候跑过」）"
    assert state.get("last_date") != TODAY, (
        "失败轮写了 last_date → 下一轮立刻 skip done_today，重试形同虚设")
    assert state.get("tries") == 1
    assert state.get("tries_date") == TODAY


def test_success_round_marks_today_done_and_resets_tries(ld):
    """成功轮才写 last_date 并清零 tries；此后 skip done_today 且不再跑 run_once。"""
    ld.record_result(_fail_result(), TODAY)
    ld.record_result(_ok_result(), TODAY)
    state = ld._state_read(sqlite3.connect(ld.DB))
    assert state.get("last_date") == TODAY
    assert int(state.get("tries", 0)) == 0
    with sqlite3.connect(ld.DB) as con:
        ld.ensure_schema(con)
        assert ld.maybe_daily(con) == {"skip": "done_today"}
    assert ld._calls == [], "当天已成功后不得真跑一轮"


def test_gate_uses_last_date_not_last_run_at(ld):
    """闸门必须是 `last_date`（只在成功时写），不能用 `last_run_at`（无条件写）。

    这是「rc=1 到底能不能换来重试」的核心：用错字段 → 失败一次就当天放弃。
    """
    ld.record_result(_fail_result(), TODAY)          # 失败轮：last_run_at 前进、last_date 不动
    with sqlite3.connect(ld.DB) as con:
        ld.ensure_schema(con)
        got = ld.maybe_daily(con)
    assert got.get("started") == TODAY, "失败后仍必须允许下一轮跑：实际 %s" % got
    assert len(ld._calls) == 1
