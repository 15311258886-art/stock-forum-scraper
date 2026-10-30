# -*- coding: utf-8 -*-
"""tests/test_ann_backfill.py —— 公告历史月窗回补（T03）离线回归（QA 2026-10-30）

纪律（工程师明确要求，务必遵守）：
  * **不联网**：全部通过 mock `ann_feed._fetch_page` / `emapi.get_json` 驱动；
  * **不跑全量**：CLI 测试只跑 mock 过的 2~5 个月窗；
  * **不写生产库**：所有写操作都在 tmp_path 里的临时 DB；生产 app.db 仅只读审计。

覆盖：
  A. month_windows 月窗边界 / 截断 / 闰年 / 非法入参；
  B. _fetch_page 的日期参数与失败契约；
  C. refresh_range 完整性契约（complete/ok/seen/total_hits）、空页、少页、页数上限、
     区间过滤、转债代码过滤、其他事务剔除、is_new、幂等重跑；
  D. run_month 失败不标 done + attempts 累加 + 重试转 done；
  E. CLI --dry-run / 跳过 done / --force / 连续失败熔断 / 参数错误；
  F. 生产库只读审计（播种月份库存、分类一致性、重复、checkpoint）。
"""
import json
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "viewer")):
    if p not in sys.path:
        sys.path.insert(0, p)

from viewer import ann_feed as af  # noqa: E402
from watchdesk import _classify, ensure_schema  # noqa: E402

import tools.backfill_ann as bf  # noqa: E402

PROD = os.path.join(ROOT, "viewer", "app.db")

# running 行超过这么多小时没更新 → 判为「崩溃残留」而非「正在跑」（见 D30 与下面的 checkpoint 测试）。
# 取值依据：单个 月窗 ≈ 数分钟~数十分钟（277 页/月的量级），6 小时 = 一个窗口正常耗时的两个数量级以上，
# 正常回补不可能在一个窗口上停留 6 小时；取这么大是刻意避免把"网速慢"误判成"进程死了"。
RUNNING_STALE_HOURS = 6


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 辅助
def _ann(code, title, nd, art="A1", codes=None):
    return {"title": title, "notice_date": nd, "art_code": art,
            "codes": codes if codes is not None else [{"stock_code": code, "short_name": "名"}]}


class FakeFeed:
    """按页返回预置数据；记录调用参数。"""

    def __init__(self, pages):
        self.pages = pages          # [(rows, total_hits), ...]
        self.calls = []

    def __call__(self, page, begin_time="", end_time="", timeout=15):
        self.calls.append({"page": page, "begin": begin_time, "end": end_time})
        if 1 <= page <= len(self.pages):
            return self.pages[page - 1]
        return [], (self.pages[-1][1] if self.pages else 0)


@pytest.fixture()
def con(tmp_path):
    db = tmp_path / "tmp_ann.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    bf.ensure_state(conn)
    yield conn
    conn.close()


@pytest.fixture()
def feed(monkeypatch):
    fx = FakeFeed([])
    monkeypatch.setattr(af, "_fetch_page", fx)
    return fx


# ================================================================ A 月窗
def test_month_windows_covers_each_month_inclusive():
    w = bf.month_windows("2016-01", "2016-03")
    assert w == [("2016-01-01", "2016-01-31"),
                 ("2016-02-01", "2016-02-29"),      # 2016 闰年
                 ("2016-03-01", "2016-03-31")]


def test_month_windows_truncates_last_month_to_end():
    assert bf.month_windows("2026-09", "2026-09-15") == [("2026-09-01", "2026-09-15")]
    assert bf.month_windows("2026-09-10", "2026-09-30") == [("2026-09-01", "2026-09-30")]


def test_month_windows_month_limit():
    assert len(bf.month_windows("2016-01", "2016-12")) == 12
    assert len(bf.month_windows("2016-01", "2016-12", months=2)) == 2


def test_month_windows_non_leap_february():
    assert bf.month_windows("2023-02", "2023-02") == [("2023-02-01", "2023-02-28")]


def test_month_windows_reversed_range_returns_empty():
    assert bf.month_windows("2016-03", "2016-01") == []


def test_month_windows_invalid_value_raises():
    with pytest.raises(ValueError):
        bf.month_windows("2016/01", "2016-03")
    with pytest.raises(ValueError):
        bf.month_windows("", "2016-03")


# ================================================================ B 单页抓取
def test_fetch_page_passes_closed_date_range(monkeypatch):
    captured = {}

    def fake_get_json(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return {"data": {"list": [{"x": 1}], "total_hits": 7}}

    monkeypatch.setattr(af.emapi, "get_json", fake_get_json)
    rows, total = af._fetch_page(3, "2016-01-01", "2016-01-31")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    assert rows == [{"x": 1}] and total == 7
    assert q["page_index"] == ["3"]
    assert q["page_size"] == [str(af.PAGE_SIZE)]
    assert q["ann_type"] == ["A"]
    assert q["begin_time"] == ["2016-01-01"]
    assert q["end_time"] == ["2016-01-31"]
    assert captured["kw"].get("referer", "").startswith("https://data.eastmoney.com/")


def test_fetch_page_without_dates_keeps_daily_semantics(monkeypatch):
    captured = {}

    def fake_get_json(url, **kw):
        captured["url"] = url
        return None

    monkeypatch.setattr(af.emapi, "get_json", fake_get_json)
    assert af._fetch_page(1) == (None, 0)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    assert "begin_time" not in q and "end_time" not in q


def test_fetch_page_failure_returns_none_zero(monkeypatch):
    monkeypatch.setattr(af.emapi, "get_json", lambda url, **kw: None)
    assert af._fetch_page(1, "2016-01-01", "2016-01-31") == (None, 0)


# ================================================================ C refresh_range
def test_refresh_range_complete(con, feed):
    feed.pages = [
        ([_ann("600519", "贵州茅台:关于股东减持股份的公告", "2016-01-05", "A1")], 2),
        ([_ann("000001", "平安银行:关于获得药物临床试验批准通知书的公告", "2016-01-06", "A2")], 2),
    ]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["ok"] is True and r["complete"] is True
    assert (r["pages"], r["seen"], r["total_hits"]) == (2, 2, 2)
    assert r["added"] == 2 and r["err"] is None
    assert r["cat_added"] == {"股东减持": 1, "产品/资质突破": 1}
    rows = con.execute("SELECT * FROM stock_ann ORDER BY code").fetchall()
    assert len(rows) == 2
    assert all(x["is_new"] == 0 for x in rows), "历史回补必须 is_new=0"
    for x in rows:
        assert (x["cat"], x["tone"]) == _classify(x["title"]), "分类必须与 watchdesk._classify 一致"
        assert x["disclose_at"] == x["notice_date"] + " 15:00:00"
        assert x["first_seen_at"]


def test_refresh_range_reports_incomplete_when_pages_short(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05")], 10)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["complete"] is False and r["ok"] is False
    assert r["seen"] == 1 and r["total_hits"] == 10
    assert "截断" in (r["err"] or "")


def test_refresh_range_incomplete_when_max_pages_hit(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05")], 100)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31", max_pages=1)
    assert r["complete"] is False and r["ok"] is False
    assert "页数上限截断" in (r["err"] or "")


def test_refresh_range_reports_page_failure(con, feed):
    feed.pages = [(None, 0)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["ok"] is False and r["complete"] is False
    assert "第 1 页抓取失败" in (r["err"] or "")


def test_refresh_range_empty_month_is_complete(con, feed):
    feed.pages = [([], 0)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["ok"] is True and r["complete"] is True
    assert (r["pages"], r["seen"], r["added"]) == (1, 0, 0)


def test_refresh_range_is_idempotent_on_rerun(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05", "A1"),
                    _ann("000001", "关于回购股份的公告", "2016-01-06", "A2")], 2)]
    first = af.refresh_range(con, "2016-01-01", "2016-01-31")
    second = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert first["added"] == 2
    assert second["added"] == 0, "同月重跑必须 added=0"
    assert second["duplicates"] == 2
    assert con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0] == 2


def test_refresh_range_daily_mode_marks_is_new(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05")], 1)]
    af.refresh_range(con, "2016-01-01", "2016-01-31", historical=False)
    assert con.execute("SELECT is_new FROM stock_ann").fetchone()[0] == 1


def test_refresh_range_filters_out_of_window_rows(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2015-12-01", "A0"),
                    _ann("000001", "关于回购股份的公告", "2016-02-01", "A9"),
                    _ann("000002", "关于获得药物临床试验批准通知书的公告", "2016-01-15", "A5")], 3)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    kept = [x["code"] for x in con.execute("SELECT code FROM stock_ann")]
    assert kept == ["000002"], "窗口外的行不得入库（早于 begin / 晚于 end）"
    assert r["seen"] == 1 and r["added"] == 1, "seen 只数窗口内行，窗口外行不得计入完整性"
    assert r["oldest"] == "2015-12-01" and r["newest"] == "2016-02-01"


def test_refresh_range_skips_non_a_share_and_picks_first_a_share_code(con, feed):
    feed.pages = [([
        _ann("", "统联转债:关于可转债转股价格调整的公告", "2016-01-05", "B1",
             codes=[{"stock_code": "118066", "short_name": "统联转债"}]),
        _ann("", "关于回购股份的公告", "2016-01-06", "B2",
             codes=[{"stock_code": "118066", "short_name": "统联转债"},
                    {"stock_code": "600519", "short_name": "贵州茅台"}]),
    ], 2)]
    af.refresh_range(con, "2016-01-01", "2016-01-31")
    rows = con.execute("SELECT code,name,url FROM stock_ann").fetchall()
    assert len(rows) == 1, "纯转债公告必须被过滤"
    assert rows[0]["code"] == "600519"
    assert "600519" in rows[0]["url"]


def test_refresh_range_skips_other_business_category(con, feed):
    noise = "贵州茅台:关于召开2026年第一次临时股东大会的通知"
    assert _classify(noise)[0] == "其他事务"        # 前提校验
    feed.pages = [([_ann("600519", noise, "2016-01-05", "C1"),
                    _ann("600519", "贵州茅台:关于回购股份的公告", "2016-01-06", "C2")], 2)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["kept"] == 1 and r["added"] == 1
    cats = [x["cat"] for x in con.execute("SELECT cat FROM stock_ann")]
    assert cats == ["回购增持"]
    assert "其他事务" not in cats, "ann_feed 不得入库其他事务"


def test_refresh_range_dedupes_inside_single_page(con, feed):
    same = _ann("600519", "关于股东减持股份的公告", "2016-01-05", "D1")
    feed.pages = [([same, dict(same)], 2)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["added"] == 1 and r["duplicates"] == 1
    assert con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0] == 1


@pytest.mark.parametrize("begin,end,needle", [
    ("2016-01-01", "2016-01-31", "第 1 页抓取失败"),
    ("bad", "2016-01-31", "日期必须是 YYYY-MM-DD"),
    ("2016-02-01", "2016-01-31", "begin_date 晚于 end_date"),
])
def test_refresh_range_error_contract(con, feed, begin, end, needle):
    feed.pages = [(None, 0)]
    r = af.refresh_range(con, begin, end)
    assert r["ok"] is False and r["complete"] is False
    assert needle in (r["err"] or "")
    assert set(("ok", "complete", "begin", "end", "total_hits", "pages", "seen",
                "kept", "added", "duplicates", "oldest", "newest", "err", "secs",
                "cat_added")) <= set(r)


def test_refresh_range_counts_duplicates_across_existing_rows(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05", "E1")], 1)]
    af.refresh_range(con, "2016-01-01", "2016-01-31")
    same_url = af.NOTICE_URL % ("000001", "E2")
    con.execute("INSERT INTO stock_ann(code,name,title,url,notice_date,cat,tone,is_new,"
                "first_seen_at,disclose_at) VALUES('000001','x','关于回购股份的公告',"
                "?, '2016-01-07','回购增持','good',0,'t','2016-01-07 15:00:00')", (same_url,))
    con.commit()
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05", "E1"),
                    _ann("000001", "关于回购股份的公告", "2016-01-07", "E2")], 2)]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert r["added"] == 0 and r["duplicates"] == 2


def test_refresh_daily_uses_recent_window(con, feed):
    feed.pages = [([], 0)]
    r = af.refresh(con, days=3)
    from datetime import date, timedelta
    assert r["begin"] == (date.today() - timedelta(days=3)).isoformat()
    assert r["end"] == date.today().isoformat()
    assert feed.calls[0]["begin"] == r["begin"] and feed.calls[0]["end"] == r["end"]


def test_refresh_range_complete_must_count_in_window_rows_only(con, feed):
    """API 混入 1 条窗口外的行时，seen 不应把该行算作「已读够 total_hits」。

    复现：page1 = 99 条窗口内 + 1 条 2016-02-05（>end），total_hits=100；page2 还有 1 条窗口内。
    当前实现：seen=100 >= total_hits=100 → complete=True，第 2 页不再抓取，丢 1 条公告。
    期望：seen 只计窗口内行（99），继续抓第 2 页。
    """
    feed.pages = [
        ([_ann("600519", "关于股东减持股份的公告", "2016-01-%02d" % (i % 28 + 1), "P1_%d" % i)
          for i in range(99)]
         + [_ann("600519", "关于回购股份的公告", "2016-02-05", "OUT1")], 100),
        ([_ann("000001", "关于获得药物临床试验批准通知书的公告", "2016-01-20", "P2_1")], 100),
    ]
    r = af.refresh_range(con, "2016-01-01", "2016-01-31")
    assert len(feed.calls) == 2, "窗口外行不应让分页提前结束（实际只抓了 %d 页）" % len(feed.calls)
    assert r["added"] == 100


# ================================================================ D run_month
def test_run_month_marks_done_only_when_complete(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05")], 1)]
    r = bf.run_month(con, "2016-01-01", "2016-01-31", 1000, lambda m: None)
    st = con.execute("SELECT * FROM ann_backfill_state").fetchone()
    assert r["complete"] is True
    assert st["status"] == "done" and st["error"] is None
    assert st["seen"] == 1 and st["total_hits"] == 1 and st["attempts"] == 1


def test_run_month_keeps_error_and_does_not_mark_done(con, feed):
    feed.pages = [([_ann("600519", "关于股东减持股份的公告", "2016-01-05")], 99)]
    bf.run_month(con, "2016-01-01", "2016-01-31", 1000, lambda m: None)
    st = con.execute("SELECT * FROM ann_backfill_state").fetchone()
    assert st["status"] == "error", "不完整的月份不得标 done"
    assert st["error"], "必须保留 error 原因供重试排障"
    assert st["seen"] == 1


def test_run_month_converts_exception_to_error_and_retry_becomes_done(con, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("QA 模拟中断")

    monkeypatch.setattr(bf.af, "refresh_range", boom)
    bf.run_month(con, "2016-01-01", "2016-01-31", 1000, lambda m: None)
    st = con.execute("SELECT * FROM ann_backfill_state").fetchone()
    assert st["status"] == "error" and "QA 模拟中断" in (st["error"] or "")
    assert st["attempts"] == 1

    monkeypatch.setattr(bf.af, "refresh_range", lambda *a, **kw: {
        "ok": True, "complete": True, "begin": "2016-01-01", "end": "2016-01-31",
        "total_hits": 5, "pages": 1, "seen": 5, "kept": 2, "added": 2,
        "duplicates": 0, "oldest": "", "newest": "", "err": None, "secs": 0.1,
        "cat_added": {"回购增持": 2}})
    bf.run_month(con, "2016-01-01", "2016-01-31", 1000, lambda m: None)
    st = con.execute("SELECT * FROM ann_backfill_state").fetchone()
    assert st["status"] == "done" and st["error"] is None
    assert st["attempts"] == 2, "重试成功必须累加 attempts 而不是清零"


def test_state_upsert_keeps_single_row_per_month(con, feed):
    feed.pages = [([], 0)]
    for _ in range(3):
        bf.run_month(con, "2016-01-01", "2016-01-31", 1000, lambda m: None)
    n = con.execute("SELECT COUNT(*) FROM ann_backfill_state").fetchone()[0]
    assert n == 1
    assert con.execute("SELECT attempts FROM ann_backfill_state").fetchone()[0] == 3


# ================================================================ E CLI
def _fake_complete(_con, begin, end, **kw):
    return {"ok": True, "complete": True, "begin": begin, "end": end, "total_hits": 1,
            "pages": 1, "seen": 1, "kept": 1, "added": 1, "duplicates": 0,
            "oldest": begin, "newest": begin, "err": None, "secs": 0.0,
            "cat_added": {"回购增持": 1}}


def test_cli_dry_run_does_not_fetch_or_write_state(con, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(bf.af, "refresh_range", lambda *a, **kw: called.append(1))
    db = str(tmp_path / "cli.db")
    import shutil
    shutil.copy2(con.execute("PRAGMA database_list").fetchone()[2], db)
    rc = bf.main(["--from", "2016-01", "--to", "2016-03", "--dry-run", "--db", db])
    assert rc == 0
    assert called == [], "--dry-run 不得抓取"
    c2 = sqlite3.connect(db)
    assert c2.execute("SELECT COUNT(*) FROM ann_backfill_state").fetchone()[0] == 0
    c2.close()


def test_cli_runs_months_then_skips_done_and_force_reruns(con, tmp_path, monkeypatch):
    import shutil
    db = str(tmp_path / "cli2.db")
    shutil.copy2(con.execute("PRAGMA database_list").fetchone()[2], db)
    called = []

    def fake(_con, begin, end, **kw):
        called.append((begin, end))
        return _fake_complete(_con, begin, end)

    monkeypatch.setattr(bf.af, "refresh_range", fake)
    assert bf.main(["--from", "2016-01", "--to", "2016-02", "--db", db]) == 0
    assert len(called) == 2
    assert bf.main(["--from", "2016-01", "--to", "2016-02", "--db", db]) == 0
    assert len(called) == 2, "done 月份默认必须跳过"
    assert bf.main(["--from", "2016-01", "--to", "2016-02", "--force", "--db", db]) == 0
    assert len(called) == 4, "--force 必须重跑"


def test_cli_months_limit(con, tmp_path, monkeypatch):
    import shutil
    db = str(tmp_path / "cli3.db")
    shutil.copy2(con.execute("PRAGMA database_list").fetchone()[2], db)
    called = []
    monkeypatch.setattr(bf.af, "refresh_range", lambda c, b, e, **kw: (
        called.append((b, e)) or _fake_complete(c, b, e)))
    assert bf.main(["--from", "2016-01", "--to", "2016-12", "--months", "3", "--db", db]) == 0
    assert len(called) == 3


def test_cli_stops_after_consecutive_failures(con, tmp_path, monkeypatch):
    import shutil
    db = str(tmp_path / "cli4.db")
    shutil.copy2(con.execute("PRAGMA database_list").fetchone()[2], db)
    called = []

    def incomplete(_con, begin, end, **kw):
        called.append(begin)
        return {"ok": False, "complete": False, "begin": begin, "end": end,
                "total_hits": 9, "pages": 1, "seen": 1, "kept": 0, "added": 0,
                "duplicates": 0, "oldest": "", "newest": "", "err": "分页未拿全",
                "secs": 0.0, "cat_added": {}}

    monkeypatch.setattr(bf.af, "refresh_range", incomplete)
    rc = bf.main(["--from", "2016-01", "--to", "2016-06", "--max-consecutive-failures", "3",
                  "--db", db])
    assert rc == 1
    assert len(called) == 3, "连续失败 3 个月必须熔断"


def test_cli_exit_code_on_bad_range(con, tmp_path, monkeypatch):
    monkeypatch.setattr(bf.af, "refresh_range", lambda *a, **kw: pytest.fail("不应抓取"))
    import shutil
    db = str(tmp_path / "cli5.db")
    shutil.copy2(con.execute("PRAGMA database_list").fetchone()[2], db)
    assert bf.main(["--from", "2016/01", "--to", "2016-03", "--db", db]) == 2
    assert bf.main(["--from", "2016-03", "--to", "2016-01", "--db", db]) == 2


# ================================================================ F 生产库只读审计
pytestmark_prod = pytest.mark.skipif(not os.path.exists(PROD), reason="生产 app.db 不存在")


def _prod():
    con = sqlite3.connect("file:%s?mode=ro" % PROD.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


@pytestmark_prod
def test_prod_checkpoint_all_done_and_complete():
    """公告回补 checkpoint 完整性校验。

    本测试的本意是「回补**全部完成后**做一次完整性校验」，不是「禁止回补运行」。
    全量回补 130 个月窗、小时级，运行期间必然有 1 个窗口处于 `running`；若把它判 fail，
    整套 pytest 会在回补的数小时里持续红灯，红灯变成噪音后真缺陷就没人看了（DECISIONS.md D30）。

    四条判据，语义严格区分「没做完 / 做坏了 / 进程死了」：
      ① `error` 非空 / status 不在 {done, running} → **失败**（真异常必须暴露）；
      ② **已完成（done）的月份，任何时候都必须自洽** —— 不看是否有别的窗口在 running。
         回补要跑 6~7 小时，若把这段窗口期的完整性校验一并 skip 掉，等于「已完成月份的数据
         完整性」整晚无人看守（QA 实测：任意一个窗口 running，另一个 done 月份 seen<total_hits
         也只 SKIP）；
      ③ `running` 但 `updated_at` 早于 6 小时 → **失败**：进程被 kill 在 `_mark(running)` 与
         收尾之间会永久留 running 行，若不判死就会变成「永远 skip、红灯彻底消失」；
      ④ 全部 done 时，② 已逐条校验过（①+③ 保证此时不存在非 done 行），无需重复。
    """
    con = _prod()
    try:
        rows = con.execute("SELECT * FROM ann_backfill_state ORDER BY window_start").fetchall()
        assert rows, "ann_backfill_state 不应为空"

        # ① 真异常先判死：与「是否正在回补」无关，任何状态下都必须失败。
        broken = [(r["window_start"], r["status"], r["error"]) for r in rows
                  if r["status"] not in ("done", "running") or r["error"]]
        assert not broken, "回补 checkpoint 存在异常窗口（status 非法或 error 非空）：%s" % broken

        # ② 已完成的月份：任何时候都必须自洽（这是原实现唯一的保护空窗，已补）。
        for r in [x for x in rows if x["status"] == "done"]:
            assert r["status"] == "done", "%s 未完成：%s" % (r["window_start"], r["error"])
            assert r["seen"] >= r["total_hits"] > 0, dict(r)
            assert r["error"] is None
            assert r["attempts"] >= 1
            assert r["updated_at"]

        # ③ running 残行判死；新鲜的 running 才 skip（等全部完成后自动恢复完整校验）。
        running = [x for x in rows if x["status"] == "running"]
        if running:
            now = datetime.now()
            stale = []
            for r in running:
                try:
                    age = now - datetime.strptime(str(r["updated_at"] or "")[:19],
                                                  "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    stale.append((r["window_start"], r["updated_at"], "updated_at 无法解析"))
                    continue
                if age > timedelta(hours=RUNNING_STALE_HOURS):
                    stale.append((r["window_start"], r["updated_at"],
                                  "%.1f 小时未更新" % (age.total_seconds() / 3600.0)))
            assert not stale, (
                "疑似崩溃残留的 running 行（阈值 %d 小时，%s）：%s\n"
                "回补进程被 kill 在 `_mark(running)` 与收尾之间会永久留 running，"
                "该窗口不会自愈 —— 请检查进程，或重置该窗口状态后重跑。"
                % (RUNNING_STALE_HOURS, _now_str(), stale))
            pytest.skip("公告回补进行中：%d 个窗口 running（%s），完成后自动恢复校验"
                        % (len(running), ", ".join(str(x["window_start"]) for x in running)))
    finally:
        con.close()


@pytestmark_prod
@pytest.mark.parametrize("month,expect_count", [("2016-01", 7027), ("2016-02", 4303),
                                               ("2026-09", 11061)])
def test_prod_backfilled_month_inventory(month, expect_count):
    """工程师抽测基线：2016-01=7027、2016-02=4303、2026-09=11061（含 watchdesk 旧行）。"""
    con = _prod()
    try:
        n = con.execute("SELECT COUNT(*) FROM stock_ann WHERE notice_date LIKE ?",
                        (month + "%",)).fetchone()[0]
        assert n == expect_count, "%s 库存 %d != 基线 %d" % (month, n, expect_count)
        state = con.execute("SELECT * FROM ann_backfill_state WHERE window_start=?",
                            (month + "-01",)).fetchone()
        assert state is not None
        assert n >= state["kept"], "库存不应少于本次 kept"
    finally:
        con.close()


@pytestmark_prod
@pytest.mark.parametrize("month", ["2016-01", "2016-02"])
def test_prod_backfilled_months_are_clean(month):
    """ann_feed 独占月份：无其他事务、分类一致、无重复、is_new=0、代码合法。"""
    con = _prod()
    try:
        rows = con.execute("SELECT * FROM stock_ann WHERE notice_date LIKE ?",
                           (month + "%",)).fetchall()
        assert rows
        assert sum(1 for r in rows if r["cat"] == "其他事务") == 0, \
            "ann_feed 不得入库其他事务"
        assert sum(1 for r in rows if r["is_new"]) == 0, "历史行必须 is_new=0"
        bad_cat = [(r["code"], r["title"]) for r in rows
                   if (r["cat"], r["tone"]) != _classify(r["title"] or "")]
        assert not bad_cat, "分类必须与 watchdesk._classify 完全一致：%s" % bad_cat[:3]
        assert all(af._A_SHARE.match(str(r["code"] or "")) for r in rows), "非 A 股代码入库"
        urls = [r["url"] for r in rows if r["url"]]
        assert len(urls) == len(set(urls)), "(code,url) 存在重复"
        assert all(r["disclose_at"] == r["notice_date"] + " 15:00:00" for r in rows)
    finally:
        con.close()


@pytestmark_prod
def test_prod_no_duplicate_ann_keys_globally():
    con = _prod()
    try:
        dup = con.execute(
            "SELECT COUNT(*) FROM (SELECT code,url,COUNT(*) c FROM stock_ann "
            "WHERE url<>'' GROUP BY code,url HAVING c>1)").fetchone()[0]
        assert dup == 0, "全局 (code,url) 重复组 %d 个（多写者竞态残留）" % dup
        bad_code = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM stock_ann WHERE code IS NOT NULL AND code<>''")
            if not af._A_SHARE.match(str(r[0]))]
        assert not bad_code, "非 A 股代码：%s" % bad_code[:5]
    finally:
        con.close()


@pytestmark_prod
def test_prod_classifier_consistency_whole_table():
    """全表分类一致性。已知 2 条历史遗留（watchdesk 旧版分类，见报告），用基线数挡住扩散。"""
    con = _prod()
    try:
        bad = []
        for r in con.execute("SELECT code,title,cat,tone,notice_date FROM stock_ann"):
            if (r["cat"], r["tone"]) != _classify(r["title"] or ""):
                bad.append((r["code"], r["notice_date"]))
        assert len(bad) <= 2, "分类不一致行数超基线：%d %s" % (len(bad), bad[:5])
    finally:
        con.close()


@pytestmark_prod
def test_prod_same_title_multiple_artcodes_is_bounded():
    """东财同一公告多 art_code → (code,date,title) 可能多行。用**密度**卡回归，不用绝对数。

    2026-10-30 基线修复（DECISIONS.md D30）：原断言为 `g <= 30`，但 30 是「只回补了 24 个月」
    时手标的经验值；全量 130 个月窗在跑，g 随数据量近似线性增长（实测 33 个月 = 22 组），
    回补越往后越必然越界。这不是「去重键退化」，而是「拿早期样本的绝对值当永久上限」——
    与 checkpoint 那条同类：环境/数据量变化被误判成缺陷。

    处置：上限取 `max(30, 总行数 × 0.05%)`。
      * 30 是**小库（< 6 万行）的地板**：小库样本少，密度项会算得过严，用绝对下限兜住；
      * 过 6 万行后生效的是密度项；回补到 130 个窗约 96 万行 → bound ≈ 480，
        仍远低于真退化（去重键失效）时的 50%~100% 量级（那会直接几十万组）；
      * 实测（2026-10-30，total=224,656 行）：**g=23、密度 0.0102%、bound=112、余量 4.9 倍**。
        `g` 增长极慢但**不是常数**（15.5 万行时 22 → 22.5 万行时 23），密度反而随回补下降，
        所以密度项只会越来越宽松、不会误报；核对请用密度或直接复现：
        `SELECT COUNT(*) FROM (SELECT code,notice_date,title,COUNT(*) c FROM stock_ann
        GROUP BY 1,2,3 HAVING c>1)`。
      * 用词更正：本阈值**不是"防护力不降"** —— 绝对容差确实随数据量放宽了（这是刻意的，
        防的是"拿早期样本的绝对值当永久上限"）；它保证的是**量级级退化（去重键失效）一定被抓到**。
    """
    con = _prod()
    try:
        g = con.execute(
            "SELECT COUNT(*) FROM (SELECT code,notice_date,title,COUNT(*) c FROM stock_ann "
            "GROUP BY 1,2,3 HAVING c>1)").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0]
        assert total > 0, "stock_ann 为空，本测试失去意义（应为历史回补未入库）"
        bound = max(30, int(total * 0.0005))
        assert g <= bound, (
            "同题重复组 %d 个 / %d 行 = %.4f%%，超上限 %d（= max(30, 行数×0.05%%)；"
            "实测 g 恒为 22、密度 ≈0.012%%），疑似去重键退化"
            % (g, total, g * 100.0 / total, bound))
    finally:
        con.close()
