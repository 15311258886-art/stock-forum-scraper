import sys
from pathlib import Path
ROOT = Path("F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper")
sys.path.insert(0, str(ROOT / "viewer"))
import screener_extra as se

# 合成 K 线：open==close 隔离价格变动；1 只票，3 个交易日
bars = [
    {"date": "2024-01-02", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6, "pct": 0.0, "suspended": False},
    {"date": "2024-01-03", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6, "pct": 0.0, "suspended": False},
    {"date": "2024-01-04", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6, "pct": 0.0, "suspended": False},
]
klines = {"600519": bars}
INIT = se.INIT_CAPITAL
r = se._engine(klines, ["600519"], "2024-01-02", "2024-01-04", 1.0)
nav_last = r["nav_curve"][-1]["nav"]
cost = r["trades"][0]["cost"]
print("INIT:", INIT, "cost:", cost, "nav_last:", nav_last)
print("期望(INIT-cost):", INIT - cost, " 期望(INIT-2*cost,bug):", INIT - 2 * cost)
assert abs(nav_last - (INIT - cost)) < 1e-6, "BUG: 买入成本被双倍扣除! nav=%s" % nav_last
print("COST_CHECK_OK: 买入仅扣 1×cost，净值 = INIT - cost")
