# -*- coding: utf-8 -*-
"""2026-10-17 交易日历修复的离线自测（零联网：sina_kline_dates 被打桩计数）。

场景：
  A. 休市日（周六）+ 缓存有历史日历  → 零联网、直接返回
  B. 休市日（周六）+ 缓存为空(此前失败) → 零联网、返回 []（is_trading_day 仍能靠确定性日历判 False）
  C. 交易日（周一）+ 日历落后 + 30s 内已试过 → 零联网、回旧缓存（节流）
  D. 交易日（周一）+ 日历落后 + 超 30s 未试 → 联网一次（计数=1），失败也只这次
  E. 交易日（周一）+ 30s 内已试过（刚失败）→ 零联网回 []
  F. 语义回归：is_trading_day(周六)=False / latest_trade_date(周六)=上周五
  G. 并发：休市日 20 线程并发调用 → 联网计数仍为 0
"""
import sys, time, threading
from datetime import datetime

import auction

calls = {"n": 0}

def fake_sina(sym, n=40):
    calls["n"] += 1
    raise IOError("模拟网络挂起")

auction.sina_kline_dates = fake_sina

fails = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)

def fake_today(s):
    # 注意：本机 Python 的 datetime.strftime 内部委托 time.strftime(fmt, self)——
    # 带 2 个位置参数的调用必须转给真实现，否则确定性日历内部日期全被污染。
    _real = time.strftime
    return lambda fmt=None, *a: (_real(fmt, *a) if a else s)

# A. 周六 + 有缓存
calls["n"] = 0
auction._TD_CACHE.update({"days": ["2026-10-12", "2026-10-15", "2026-10-16"], "ts": time.time() - 86400,
                          "asof": "2026-10-16", "last_try": time.time() - 86400})
_real_strftime = time.strftime
time.strftime = fake_today("2026-10-17")  # 周六
t0 = time.time()
r = auction.market_trade_days()
ok(r == ["2026-10-12", "2026-10-15", "2026-10-16"], "A 周六有缓存直接返回: %s" % r)
ok(calls["n"] == 0, "A 周六零联网 (calls=%d)" % calls["n"])
ok(time.time() - t0 < 0.05, "A 耗时 <50ms (%.1fms)" % ((time.time() - t0) * 1000))

# B. 周六 + 空缓存
calls["n"] = 0
auction._TD_CACHE.update({"days": [], "ts": time.time(), "asof": "2026-10-17", "last_try": time.time()})
r = auction.market_trade_days()
ok(r == [] and calls["n"] == 0, "B 周六空缓存零联网返回[]: r=%s calls=%d" % (r, calls["n"]))
ok(auction.is_trading_day() is False, "B 周六 is_trading_day=False (确定性日历兜底)")
ok(auction.latest_trade_date() == "2026-10-16", "B 周六 latest_trade_date=2026-10-16: %s" % auction.latest_trade_date())

# C. 周一（潜在交易日）+ 日历落后 + 30s 内已试过 → 节流零联网
calls["n"] = 0
auction._TD_CACHE.update({"days": ["2026-10-12", "2026-10-16"], "ts": time.time(), "asof": "2026-10-17",
                          "last_try": time.time() - 5})
time.strftime = fake_today("2026-10-19")  # 周一
r = auction.market_trade_days()
ok(calls["n"] == 0 and r == ["2026-10-12", "2026-10-16"], "C 周一30s节流零联网回旧缓存: calls=%d" % calls["n"])

# D. 周一 + 超 30s 未试 → 联网一次（打桩失败也只这次）
calls["n"] = 0
auction._TD_CACHE.update({"last_try": time.time() - 60})
r = auction.market_trade_days()
ok(calls["n"] == 1 and r == [], "D 周一超30s联网恰好一次: calls=%d r=%s" % (calls["n"], r))

# E. 周一 + 刚失败 → 30s 内零联网
calls["n"] = 0
r = auction.market_trade_days()
ok(calls["n"] == 0 and r == [], "E 周一失败后30s内零联网: calls=%d" % calls["n"])

# G. 休市日并发 20 线程
calls["n"] = 0
auction._TD_CACHE.update({"days": ["2026-10-16"], "ts": time.time(), "asof": "2026-10-17", "last_try": 0})
time.strftime = fake_today("2026-10-17")
ths = [threading.Thread(target=lambda: auction.market_trade_days()) for _ in range(20)]
[t.start() for t in ths]; [t.join() for t in ths]
ok(calls["n"] == 0, "G 周六20线程并发零联网 (calls=%d)" % calls["n"])

time.strftime = _real_strftime
print("\n%s" % ("ALL PASS (%d)" % len(fails) if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
