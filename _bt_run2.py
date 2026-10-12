import sys, json
from pathlib import Path
ROOT = Path("F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper")
sys.path.insert(0, str(ROOT / "viewer"))
import screener_extra as se

codes = ["600519", "000001", "300750", "600036", "000858"]
r = se.backtest(codes, "2024-01-01", "2024-12-31")
print("ok:", r["ok"], "n:", r["n"], "excluded:", r["codes_excluded"])
print("总收益:", r["total_return"], "年化:", r["annual_return"], "最大回撤:", r["max_drawdown"])
print("夏普:", r["sharpe"], "索提诺:", r["sortino"], "卡玛:", r["calmar"])
print("胜率:", r["win_rate"], "盈亏比:", r["profit_loss_ratio"], "期末净值:", r["final_nav"])
print("2x成本总收益:", r["metrics_2x"]["total_return"])
print("成本差(基准-2x):", round(r["total_return"] - r["metrics_2x"]["total_return"], 4))
assert r["total_return"] == r["metrics"]["total_return"]
assert r["max_drawdown"] == r["metrics"]["max_drawdown"]
print("CONSISTENCY_OK")
