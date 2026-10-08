# -*- coding: utf-8 -*-
"""阶段 0 正确性断言（测试自检闸的判定基准）。

【合成测试桩，非产品行情数据，不对外展示】
仅用受控合成数据验证回测/预测核心公式与隔离逻辑的正确性，不依赖任何真实网络行情；
需要真实行情才能验证的用例在沙箱里 pytest.skip 诚实降级，绝不以编造的真实数值冒充通过。

设计契约（来自 stage0_selfcheck_plan.md / team-lead 派发）：
  在干净代码（未施加任何变异）上，5 个用例必须全 PASS；
  当对应变异注入后，对应用例必须 FAIL（exit != 0），闸据此判定「已捕获该缺陷」。

运行：
  .venv/Scripts/python.exe test_stage0_correctness.py          # 直接跑（自带 __main__ 报告）
  .venv/Scripts/python.exe -m pytest test_stage0_correctness.py -q   # 或走 pytest
"""
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.join(ROOT, "viewer")
for _p in (VIEWER, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import screener_extra as se          # 回测/成本/幸存者：B7 核心（纯 Python，无第三方强依赖）
import predictor as P                 # 前视隔离 / 涨跌幅公式
import auction                         # 交易日历
from types import SimpleNamespace


# ---------------------------------------------------------------- 合成数据桩
def _gen_kline(code, start, end, close=100.0):
    """合成横盘日线（非真实行情）：open=high=low=close=close，volume/pct 合法，全不停牌。

    返回 [{date,d,open,high,low,close,volume,pct,suspended}, ...]，日期覆盖 [start, end] 每日。
    同时带 date 与 d 两个键：screener_extra 读 date，predictor 读 d（asof 截断用 d）。
    """
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    bars = []
    cur = d0
    while cur <= d1:
        d = cur.strftime("%Y-%m-%d")
        bars.append({
            "date": d, "d": d, "open": close, "high": close, "low": close, "close": close,
            "volume": 1000.0, "pct": 0.0, "suspended": False,
        })
        cur += timedelta(days=1)
    return bars


# ---------------------------------------------------------------- 1) 涨跌幅公式符号（纯公式）
def test_window_ret_sign():
    """_window_ret 必须返回 +0.10（涨为正）。符号反变异（return 1.0 - c1/c0）会返回 -0.10 -> FAIL。"""
    k = [{"c": 100.0}, {"c": 110.0}]
    ret = P._window_ret(k, 0, 1)
    assert ret is not None, "_window_ret 不应返回 None"
    assert abs(ret - 0.10) < 1e-9, "涨跌幅公式应返回 0.10，实际 %r" % ret
    # 反向抽样：下跌应为负
    k2 = [{"c": 110.0}, {"c": 100.0}]
    ret2 = P._window_ret(k2, 0, 1)
    assert ret2 is not None and ret2 < 0, "下跌应返回负值，实际 %r" % ret2
    return True


# ---------------------------------------------------------------- 2) 成本漏扣（回测 + 直接）
def test_cost_not_leaked():
    """真实成本下 _trade_cost 必须严格 > 0（成本漏扣后变为 0 -> FAIL）；
    横盘合成 K 线下，真实成本使组合净值 < 0，且 2 倍成本更差（漏扣后两者≈0 -> FAIL）。"""
    # (a) 直接断言成本函数本身：未注入时严格为正，2 倍成本高于 1 倍
    c1 = se._trade_cost(100000.0, True, "600000", 1.0)
    assert c1 > 0, "_trade_cost 真实成本下应 > 0，实际 %r" % c1
    c2 = se._trade_cost(100000.0, True, "600000", 2.0)
    assert c2 > c1, "2 倍成本应高于 1 倍成本，实际 1x=%r 2x=%r" % (c1, c2)

    # (b) 集成层：用合成横盘 K 线驱动真实 backtest（monkeypatch 掉网络 get_kline）
    codes = ["600000", "000001"]

    def fake_get_kline(code, start, end):
        return _gen_kline(code, start, end, close=100.0)

    orig = se.get_kline
    try:
        se.get_kline = fake_get_kline
        real = se.backtest(codes, "2024-01-01", "2024-12-31")
    finally:
        se.get_kline = orig

    assert real.get("ok") is True, "合成数据下 backtest 应成功，实际 ok=%r" % real.get("ok")
    tr = (real.get("metrics") or {}).get("total_return")
    tr2 = (real.get("metrics_2x") or {}).get("total_return")
    assert tr is not None and tr < 0, "横盘下真实成本应使 total_return<0，实际 %r" % tr
    assert tr2 is not None and tr2 < tr, "2x 成本应更差（tr2<tr），1x=%r 2x=%r" % (tr, tr2)
    return True


# ---------------------------------------------------------------- 3) 前视偏差（asof 隔离）
def test_asof_isolation():
    """预测日 td 的 asof 截断必须生效：sig_tech 收到的 bars 不应含 td 之后的未来 bar。

    做法：monkeypatch fetch_bars 让 600000 多塞一根未来 bar（2024-07-01 > td=2024-06-30），
    并用 recorder 替换 sig_tech 记录其收到的最大日期。asof 生效 -> max_d <= td；
    asof 被绕过（前视偏差）-> max_d > td -> FAIL。
    只依赖 sig_tech 在 _generate_inner 早期（落库/复盘之前）被调用这一事实，对后续管线无要求。
    """
    td = "2024-06-30"
    cap = {"max_d": None}

    def fake_sig_tech(bars_map, strat_list, progress=None):
        md = None
        for bars in bars_map.values():
            for b in bars:
                d = str(b.get("d", ""))[:10]
                if d:
                    md = d if (md is None or d > md) else md
        cap["max_d"] = md
        return {}

    orig_sig_tech = P.sig_tech
    orig_ss = P.SS
    orig_load_kcache = P.load_kcache
    orig_fetch = P.fetch_bars
    orig_load_strat = P._load_strategies

    def fake_fetch(codes, kc, force=False, progress=None):
        out = {}
        for c in codes:
            bars = _gen_kline(c, "2023-12-01", td, close=100.0)  # 截至 td，>= MIN_BARS(130)
            if c == "600000":
                # 注入未来 bar（日期 > td）——前视偏差的泄漏源（带 d 键，predictor 读 d）
                bars = bars + [{"date": "2024-07-01", "d": "2024-07-01", "open": 100.0,
                               "high": 100.0, "low": 100.0, "close": 100.0,
                               "volume": 1000.0, "pct": 0.0, "suspended": False}]
            out[c] = bars
        return out

    try:
        P.sig_tech = fake_sig_tech
        P.SS = SimpleNamespace(universe=lambda limit: (["600000", "000001"],
                                                       {"600000": "浦发", "000001": "平安"}, None))
        P.load_kcache = lambda: {}
        P.fetch_bars = fake_fetch
        P._load_strategies = lambda: []
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row  # predictor._migrate 以 r["name"] 字典式访问 PRAGMA 行，需 Row
        try:
            P._generate_inner(con, run_date=td, force=True, progress=None)
        except Exception:
            # 后续落库/复盘/权重阶段不影响 asof 判定（sig_tech 已完成捕获）
            pass
    finally:
        P.sig_tech = orig_sig_tech
        P.SS = orig_ss
        P.load_kcache = orig_load_kcache
        P.fetch_bars = orig_fetch
        P._load_strategies = orig_load_strat

    assert cap["max_d"] is not None, "sig_tech 未被调用（asof 测试桩失效）"
    assert cap["max_d"] <= td, "前视偏差：sig_tech 收到未来 bar，max_d=%s > td=%s" % (cap["max_d"], td)
    return True


# ---------------------------------------------------------------- 4) 幸存者未剔除
def test_survivorship_delisted_exit():
    """含中途退市标的时，backtest 必须识别并强制平仓（delisted_exits>=1）。

    合成：000001 为退市票，K 线只到 2024-06-30（窗口内最后交易日后无数据）；
    600000 为正常票。退市强平块被跳过（变异）-> delisted_exits=0 -> FAIL。
    """
    def fake_get_kline(code, start, end):
        if code == "600000":
            return _gen_kline("600000", "2024-01-01", "2024-12-31", close=100.0)
        # 000001：中途退市，末段无数据
        return _gen_kline("000001", "2024-01-01", "2024-06-30", close=100.0)

    orig = se.get_kline
    try:
        se.get_kline = fake_get_kline
        res = se.backtest(["600000", "000001"], "2024-01-01", "2024-12-31")
    finally:
        se.get_kline = orig

    assert res.get("ok") is True, "合成数据下 backtest 应成功，实际 ok=%r" % res.get("ok")
    cons = res.get("constraints") or {}
    assert cons.get("delisted_exits", 0) >= 1, \
        "应识别并强制平仓中途退市标的，delisted_exits=%r" % cons.get("delisted_exits")
    # 退市票有数据，不应被错误排除（排除是另一类问题，这里只验证「被持有并强平」）
    assert "000001" not in (res.get("codes_excluded") or []), "退市票有数据，不应被排除"
    # 净值不应被虚增：退市后被强制平仓结算现金，final_nav 应低于「持仓冻结在最后收盘价」的虚高情形
    fn = res.get("final_nav")
    assert fn is not None and fn <= se.INIT_CAPITAL, "退市后应结算现金，final_nav 不应虚高，实际 %r" % fn
    return True


# ---------------------------------------------------------------- 5) 日期/asof 错位（陈旧日历）
def test_trade_calendar_not_stale():
    """market_trade_days 日历落后真实今天时，必须重抓今日而非返回陈旧缓存。

    注入：把 re-fetch 入口替换为「永远返回陈旧缓存」。干净代码下，缓存落后 -> 重抓 ->
    latest_trade_date('2026-10-09') == '2026-10-09'；变异后返回陈旧 '2026-09-30' -> FAIL。
    """
    orig_cache = dict(auction._TD_CACHE)
    orig_sina = auction.sina_kline_dates
    try:
        auction._TD_CACHE.clear()
        auction._TD_CACHE.update({
            "days": ["2026-09-28", "2026-09-29", "2026-09-30"],
            "ts": 0.0, "asof": "2026-09-30", "last_try": 0.0,
        })
        # 模拟「真实今日」已刷新到 2026-10-09（注：market_trade_days 内部 today 取系统时间，
        # 但 latest_trade_date 的入参 t 我们显式传 '2026-10-09' 做判定；干净代码必重抓到它）
        auction.sina_kline_dates = lambda sym, n=40: [
            {"day": d} for d in ["2026-10-08", "2026-10-09", "2026-10-10"]
        ]
        got = auction.latest_trade_date("2026-10-09")
    finally:
        auction._TD_CACHE.clear()
        auction._TD_CACHE.update(orig_cache)
        auction.sina_kline_dates = orig_sina

    assert got == "2026-10-09", \
        "latest_trade_date 必须重抓到今日而非陈旧日，实际 %r" % got
    return True


# ---------------------------------------------------------------- 直接运行入口
if __name__ == "__main__":
    tests = [
        test_window_ret_sign,
        test_cost_not_leaked,
        test_asof_isolation,
        test_survivorship_delisted_exit,
        test_trade_calendar_not_stale,
    ]
    fails = []
    for t in tests:
        try:
            ok = t()
            print(("PASS " if ok else "FAIL ") + t.__name__)
            if not ok:
                fails.append(t.__name__)
        except AssertionError as e:
            print("FAIL " + t.__name__ + " :: " + str(e))
            fails.append(t.__name__)
        except Exception as e:  # noqa: BLE001
            print("ERROR " + t.__name__ + " :: " + repr(e))
            fails.append(t.__name__ + "(error)")
    passed = len(tests) - len(fails)
    print("\n==== %d/%d PASS ====" % (passed, len(tests)))
    sys.exit(1 if fails else 0)
