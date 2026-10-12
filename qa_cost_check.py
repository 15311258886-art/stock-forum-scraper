"""QA 独立核查：验证 _engine 买入交易的真实成本记账是否正确（一把尺子/真成本红线）。
不触网：直接喂合成 K 线给 se._engine。open==close 隔离价格变动，单独暴露成本记账。
"""
import sys
sys.path.insert(0, r"F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/viewer")
import screener_extra as se

code = "600000"
bars = [
    {"date": "2024-01-02", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
     "volume": 1000, "pct": 0.0, "suspended": False},
    {"date": "2024-01-03", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
     "volume": 1000, "pct": 0.0, "suspended": False},
]
res = se._engine({code: bars}, [code], "2024-01-02", "2024-01-03", 1.0)
nav_day2 = res["nav_curve"][-1]["nav"]
cost = se._trade_cost(se.INIT_CAPITAL, True, code, 1.0)
print("ok:", res["ok"])
print("nav_curve:", res["nav_curve"])
print("trades:", res["trades"])
print("cost:", round(cost, 4))
print("INIT_CAPITAL:", se.INIT_CAPITAL)
print("EXPECT correct nav (INIT-cost):", round(se.INIT_CAPITAL - cost, 4))
print("actual nav_day2:", nav_day2)
print("delta vs correct (want 0):", round(nav_day2 - (se.INIT_CAPITAL - cost), 4))
print("delta vs buggy -2cost (want +cost if bug):", round(nav_day2 - (se.INIT_CAPITAL - 2 * cost), 4))
