# -*- coding: utf-8 -*-
"""tests/test_predictor_pit.py —— predictor 历史回放的**时点取数（PIT）**回归（2026-10-30）

防的是前视偏差（look-ahead bias）：回放 2020 年时，不许用 2020-09-30 甚至 2026 年的
机构持股快照打分。改前 `sig_holders(con, asof=...)` 有形参但函数体完全没用它，
一律取全表最新一期 —— 回放历史 = 拿着答案做预测（DECISIONS.md D31）。

纪律：
  * **不连生产库**：全部用 tmp_path 里的临时 sqlite，手工造 3 个报告期；
  * **不联网 / 不跑全量**：只调 sig_holders / sig_desk 两个纯函数；
  * 断言只盯「取到哪一期」和「日期过滤是否生效」，不锁定 strength 的具体数值口径。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_predictor_pit.py -q
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from viewer import predictor as P  # noqa: E402

# 三个报告期：机构家数 / 合计持股 / 股东户数 都刻意做成逐期递增可辨识
P1, P2, P3 = "2020-03-31", "2020-06-30", "2020-09-30"

DDL_HOLDERS = ("CREATE TABLE stock_holders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "symbol TEXT, symbol_name TEXT, end_date TEXT, notice_date TEXT, "
               "holder_rank INTEGER, holder_name TEXT, holder_type TEXT, is_org INTEGER, "
               "hold_num REAL, hold_ratio REAL, free_ratio REAL, hold_change TEXT, "
               "change_ratio REAL, scope TEXT, holder_kind TEXT, exclude_reason TEXT, "
               "disclose_at TEXT)")
DDL_HOLDER_NUM = ("CREATE TABLE stock_holder_num (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "symbol TEXT, symbol_name TEXT, end_date TEXT, notice_date TEXT, "
                  "holder_total_num INTEGER, total_num_ratio REAL, avg_hold_amt REAL, "
                  "price REAL, hold_focus TEXT)")
DDL_DESK = ("CREATE TABLE stock_desk (code TEXT, name TEXT, stance TEXT, verdict TEXT, "
            "risk_score INTEGER, opp_score INTEGER, risks TEXT, opps TEXT, triggers TEXT, "
            "finance TEXT, fresh_cnt INTEGER, scan_at TEXT)")


def _mkdb(tmp_path, name="pit.db"):
    con = sqlite3.connect(str(tmp_path / name))
    con.row_factory = sqlite3.Row
    con.execute(DDL_HOLDERS)
    con.execute(DDL_HOLDER_NUM)
    con.execute(DDL_DESK)
    return con


def _holder_rows(n_org: int, ratio_each: float = 10.0, symbol: str = "600519",
                 end_date: str = P1, dup: int = 0):
    """造一期前十大：n_org 家机构（rank 1..n_org）+ 若干自然人。

    dup > 0 时**重复**插入前 dup 个 rank（模拟同一股东 free/total 双 scope），
    用来验证去重键 (symbol, end_date, holder_rank) 仍然有效。
    """
    rows = []
    for i in range(1, n_org + 1):
        rows.append((symbol, end_date, i, "机构%d" % i, 1, "机构", ratio_each, None))
    for i in range(n_org + 1, 11):
        rows.append((symbol, end_date, i, "个人%d" % i, 0, "个人", 0.5, None))
    for i in range(1, dup + 1):                      # 双 scope 重复行（同 rank）
        rows.append((symbol, end_date, i, "机构%d" % i, 1, "机构", ratio_each, None))
    return rows


def _seed(con, rows):
    con.executemany(
        "INSERT INTO stock_holders (symbol, end_date, holder_rank, holder_name, is_org, "
        "holder_type, hold_ratio, hold_change) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _seed_num(con, pairs, symbol="600519"):
    con.executemany(
        "INSERT INTO stock_holder_num (symbol, end_date, holder_total_num) VALUES (?,?,?)",
        [(symbol, d, n) for d, n in pairs])
    con.commit()


@pytest.fixture()
def con3(tmp_path):
    """3 期标准库：机构家数 1 / 2 / 3，合计持股比例 10 / 25 / 60；户数 1000 / 800 / 400。"""
    con = _mkdb(tmp_path)
    _seed(con, _holder_rows(1, 10.0, end_date=P1)
          + _holder_rows(2, 12.5, end_date=P2)
          + _holder_rows(3, 20.0, end_date=P3))
    _seed_num(con, [(P1, 1000), (P2, 800), (P3, 400)])
    yield con
    con.close()


def _org_of(entry) -> int:
    """从 evidence 里读回「前十大含机构 N 家」，避免依赖 strength 的合成公式。"""
    m = re.search(r"含机构 (\d+) 家", entry["evidence"][0] or "")
    assert m, "evidence 里没有机构家数：%s" % (entry["evidence"],)
    return int(m.group(1))


# ================================================================ sig_holders 时点取数
def test_asof_picks_latest_period_not_after_asof(con3):
    """核心断言：asof=2020-07-15 只能看见 2020-06-30 期，**看不见** 2020-09-30 期。"""
    out = P.sig_holders(con3, asof="2020-07-15")
    e = out["600519"]
    assert _org_of(e) == 2, "应取 2020-06-30 期（2 家机构），实际 %s" % (e,)
    assert "2020-06-30" in e["evidence"][0]
    assert "2020-09-30" not in e["evidence"][0], "前视：拿到了 asof 之后才披露的报告期"
    assert abs(e["strength"] - (25.0 + 20.0)) < 1e-6, "2 家 × 12.5 = 25 分位，加户数环比 -20% 的 20 分"


def test_asof_before_all_periods_returns_empty_not_crash(con3):
    """asof 早于全部报告期 → 返回空 dict（沿用"这路没数据"的既有缺失语义），不抛异常。"""
    assert P.sig_holders(con3, asof="2019-01-01") == {}


def test_asof_none_keeps_latest_period(con3):
    """asof=None（实盘"当场"路径）→ 取全表最新一期，与改前口径一致。"""
    e = P.sig_holders(con3)["600519"]
    assert _org_of(e) == 3 and "2020-09-30" in e["evidence"][0]
    assert abs(e["strength"] - (60.0 + 50.0)) < 1e-6, "3 家 × 20 = 60 分位，加户数 800→400(-50%) 的 50 分"


def test_asof_on_period_date_is_inclusive(con3):
    """边界：asof 正好等于报告期截止日 → 该期算"已知"（含等号）。"""
    e = P.sig_holders(con3, asof=P2)["600519"]
    assert _org_of(e) == 2 and "2020-06-30" in e["evidence"][0]


def test_holder_num_also_filtered_by_asof(con3):
    """股东户数环比同样要按 asof 过滤 —— 只过滤前十大等于前视从大门赶进小门。"""
    e = P.sig_holders(con3, asof="2020-07-15")["600519"]
    ev = " ".join(e["evidence"])
    assert "股东户数较上期 -20.0%" in ev, "应按 1000→800 算 -20%%，实际：%s" % ev
    assert "-50.0%" not in ev, "前视：用了 asof 之后才有的 800→400(-50%%)"


def test_result_is_independent_of_row_order(tmp_path):
    """聚合不许依赖行序（改前无 ORDER BY，旧期会被累加进最新期 → "前十大含机构 30 家"）。

    同一份数据，一次按 end_date 升序插入、一次按降序插入，结果必须完全相同。
    """
    asc, desc = _mkdb(tmp_path, "a.db"), _mkdb(tmp_path, "d.db")
    periods = [(P1, 1, 10.0), (P2, 2, 12.5), (P3, 3, 20.0)]
    for c, seq in ((asc, periods), (desc, list(reversed(periods)))):
        for d, n, ratio in seq:
            _seed(c, _holder_rows(n, ratio, end_date=d))
    a, d_ = P.sig_holders(asc), P.sig_holders(desc)
    assert a == d_, "行序改变了结论：%s vs %s" % (a.get("600519"), d_.get("600519"))
    # 且必须是「最新一期独占」，不是各期累加（累加会得到 1+2+3=6 家）
    assert _org_of(a["600519"]) == 3, "疑似把多个报告期累加了：%s" % (a["600519"],)
    asc.close()
    desc.close()


def test_duplicate_scope_rows_counted_once(tmp_path):
    """同一 rank 的 free/total 双 scope 行只能算一次（去重键不能退化成累加）。"""
    con = _mkdb(tmp_path, "dup.db")
    _seed(con, _holder_rows(2, 12.5, end_date=P2, dup=2))
    e = P.sig_holders(con)["600519"]
    assert _org_of(e) == 2, "双 scope 行被重复计数：%s" % (e,)
    con.close()


def test_scope_tie_break_prefers_free_regardless_of_insert_order(tmp_path):
    """同一 `(symbol, end_date, holder_rank)` 的 free / total 两行：必须**固定选 free**。

    改前"恰好 free 在前"是 `UNIQUE(..., scope)` 索引里 `'free' < 'total'` 的运气，不是契约
    （DECISIONS.md D31 二之三：两行 is_org 不一致的有 15,569 组）。
    这里**故意反过来插入**（total 先入库 → rowid 更小）来证明 ORDER BY 的 tie-break 真的生效：
    total 行写成「非机构 / 占比 30%」，free 行写成「机构 / 占比 5%」——
    只有钉死 free 优先，`org_n` 才会是 1、合计持股才会是 5.0%。
    """
    con = _mkdb(tmp_path, "scope.db")
    for name, is_org, htype, ratio, scope in (
            ("总股本口径", 0, "个人", 30.0, "total"),      # 先插 total：rowid 更小
            ("流通口径机构", 1, "机构", 5.0, "free")):
        con.execute(
            "INSERT INTO stock_holders (symbol, end_date, holder_rank, holder_name, is_org, "
            "holder_type, hold_ratio, scope) VALUES ('600519', ?, 1, ?, ?, ?, ?, ?)",
            (P1, name, is_org, htype, ratio, scope))
    con.commit()
    e = P.sig_holders(con)["600519"]
    assert _org_of(e) == 1, "tie-break 失效：选中了 total 行（应为 free 行，org 家数 1）：%s" % (e,)
    assert "机构合计持股 5.0%" in " ".join(e["evidence"]), \
        "选中了 total 行的占比（30%），free 优先没生效：%s" % (e["evidence"],)
    con.close()


def test_missing_tables_yield_empty_not_crash(tmp_path):
    """表不存在（全新库）→ 返回空 dict，不抛异常（沿用既有缺失语义）。"""
    con = sqlite3.connect(str(tmp_path / "empty.db"))
    con.row_factory = sqlite3.Row
    assert P.sig_holders(con, asof="2020-07-15") == {}
    assert P.sig_desk(con, asof="2020-07-15") == {}
    con.close()


# ================================================================ sig_desk 不可回溯
def _seed_desk(con, scan_at="2026-10-30 09:30:00", code="600519"):
    con.execute("INSERT INTO stock_desk (code, name, stance, risk_score, opp_score, scan_at) "
                "VALUES (?,?,?,?,?,?)", (code, "贵州茅台", "机会", 10, 80, scan_at))
    con.commit()


def test_desk_unavailable_when_snapshot_is_from_future(tmp_path):
    """asof 早于快照日 → 整路不可用（空 dict）。不许拿当期快照冒充历史结论。"""
    con = _mkdb(tmp_path, "desk.db")
    _seed_desk(con)
    assert P.desk_snapshot_date(con) == "2026-10-30"
    assert P.sig_desk(con, asof="2020-06-30") == {}
    assert P.sig_desk(con, asof="2026-10-29") == {}
    con.close()


def test_desk_available_when_asof_not_earlier_than_snapshot(tmp_path):
    """asof ≥ 快照日（快照不来自未来）→ 照常可用，实盘当天行为与改前一致。"""
    con = _mkdb(tmp_path, "desk2.db")
    _seed_desk(con)
    assert set(P.sig_desk(con, asof="2026-10-30")) == {"600519"}
    assert set(P.sig_desk(con, asof=None)) == {"600519"}
    assert set(P.sig_desk(con, asof="2026-11-15")) == {"600519"}
    con.close()


def test_desk_mixed_scan_dates_use_max_date(tmp_path):
    """快照内混有两个扫描日时，用**最大**扫描日判前视。

    这条是给实盘定的：stock_desk 现状是 223 行 10-30 + 4 行 10-14，若按"必须等于 asof"
    判，实盘当天会被自己判成不可用 —— 那等于用修前视把实盘砸了。
    """
    con = _mkdb(tmp_path, "desk3.db")
    _seed_desk(con, scan_at="2026-10-14 09:27:41", code="000001")
    _seed_desk(con, scan_at="2026-10-30 11:55:31", code="600519")
    assert P.desk_snapshot_date(con) == "2026-10-30"
    assert set(P.sig_desk(con, asof="2026-10-30")) == {"000001", "600519"}   # 不来自未来 → 全用
    assert P.sig_desk(con, asof="2026-10-20") == {}                          # 含未来行 → 整路不用
    con.close()


# ================================================================ 不变量（锁死"跨期累加"这类回归）
RE_ORG = re.compile(r"含机构 (\d+) 家")
RE_RATIO = re.compile(r"机构合计持股 ([-\d.]+)%")


def _org_of_any(entry):
    m = RE_ORG.search((entry.get("evidence") or [""])[0] or "")
    return int(m.group(1)) if m else None


def _ratio_of_any(entry):
    for s in (entry.get("evidence") or []):
        m = RE_RATIO.search(s or "")
        if m:
            return float(m.group(1))
    return None


def _multi_symbol_db(tmp_path, name, n_symbols=4, n_periods=3, org_each=9, ratio_each=9.0):
    """造「多票 × 多期」的库，**按 end_date 降序插入**（最坏行序：旧期排在新期之后）。

    旧实现只在新期出现时 clear()，降序插入 → 每一期的前十大会被累加进最新期。
    """
    con = _mkdb(tmp_path, name)
    periods = ["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"][:n_periods]
    rows = []
    for d in sorted(periods, reverse=True):          # 降序 = 旧期在后
        for i in range(n_symbols):
            sym = "60000%d" % i
            for rank in range(1, org_each + 1):
                rows.append((sym, d, rank, "机构%d" % rank, 1, "机构", ratio_each, None))
    _seed(con, rows)
    return con


def test_invariant_org_count_never_exceeds_top10_slots(tmp_path):
    """`org_n <= 10`：前十大只有 10 个名额。

    这是**硬不变量**，不是口味问题。改前因为把多个报告期累加起来，实测全表 3977/5057 只
    票的 org_n > 10（最大 50 家），证据串会写成"前十大含机构 30 家"这种物理不可能的话。
    本用例按最坏行序造数据，任何人把 ORDER BY 拿掉 / 把跨期累加写回来，这里立刻红。
    """
    con = _multi_symbol_db(tmp_path, "inv_org.db", n_symbols=4, n_periods=3, org_each=9)
    out = P.sig_holders(con)
    assert out, "聚合结果为空，用例失去意义"
    bad = {s: _org_of_any(v) for s, v in out.items() if (_org_of_any(v) or 0) > 10}
    assert not bad, "前十大含机构数超过 10 个名额（跨期累加回归）：%s" % bad
    # 同时钉死"取最新一期"，不能是 3 期相加（9×3=27）
    assert _org_of_any(out["600000"]) == 9
    con.close()


def test_invariant_hold_ratio_never_exceeds_100_percent(tmp_path):
    """`ratio <= 100.0`：前十机构合计持股占比不可能超过 100%。

    改前实测全表 2429/5057 只票超 100%（000001 是 195.6%），成因同上（3 期累加）。
    """
    con = _multi_symbol_db(tmp_path, "inv_ratio.db", n_symbols=3, n_periods=3,
                           org_each=10, ratio_each=9.0)   # 单期 90%，累加 3 期 = 270%
    out = P.sig_holders(con)
    bad = {s: _ratio_of_any(v) for s, v in out.items() if (_ratio_of_any(v) or 0) > 100.0}
    assert not bad, "机构合计持股 > 100%%（跨期累加回归）：%s" % bad
    assert abs(_ratio_of_any(out["600000"]) - 90.0) < 1e-6, "应只算最新一期 90%%"
    con.close()


_PROD = os.path.join(ROOT, "viewer", "app.db")


@pytest.mark.skipif(not os.path.exists(_PROD), reason="生产 app.db 不存在")
def test_prod_invariants_hold_for_whole_table():
    """把上面两条不变量在**生产库全表**上跑一遍（只读，`mode=ro`，不进写事务）。

    这条是"线上真的没在算错值"的守门员：改前它会红（3977 只 org_n>10、2429 只 ratio>100%）。
    """
    con = sqlite3.connect("file:%s?mode=ro" % _PROD.replace("\\", "/"), uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        out = P.sig_holders(con)                      # asof=None = 实盘口径
        bad_org = {s: v for s, v in out.items() if (_org_of_any(v) or 0) > 10}
        bad_ratio = {s: v for s, v in out.items() if (_ratio_of_any(v) or 0) > 100.0}
        assert not bad_org, "全表 %d 只票 org_n>10（前十大只有 10 个名额）：%s" % (
            len(bad_org), sorted(bad_org.items())[:3])
        assert not bad_ratio, "全表 %d 只票机构合计持股>100%%：%s" % (
            len(bad_ratio), sorted(bad_ratio.items())[:3])
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))