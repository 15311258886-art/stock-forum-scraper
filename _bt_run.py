import sys, json
from pathlib import Path
ROOT = Path("F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper")
sys.path.insert(0, str(ROOT / "viewer"))
import screener_extra as se

codes = ["600519", "000001", "300750", "600036", "000858"]
r = se.backtest(codes, "2024-01-01", "2024-12-31")
print("ok:", r["ok"], "n:", r["n"], "excluded:", r["codes_excluded"])
print("method:", r["method"])
print("总收益:", r["total_return"], "年化:", r["annual_return"], "最大回撤:", r["max_drawdown"])
print("夏普:", r["sharpe"], "索提诺:", r["sortino"], "卡玛:", r["calmar"])
print("胜率:", r["win_rate"], "盈亏比:", r["profit_loss_ratio"], "交易日:", r["trading_days"], "期末净值:", r["final_nav"])
m2 = r["metrics_2x"]
print("2x成本 总收益:", m2["total_return"], "最大回撤:", m2["max_drawdown"], "夏普:", m2["sharpe"])
ins = r["in_sample"]; outs = r["out_of_sample"]
print("样本内:", ins["start"], "~", ins["end"], "总收益:", ins["total_return"], "年化:", ins["annual_return"], "夏普:", ins["sharpe"])
print("样本外:", outs["start"], "~", outs["end"], "总收益:", outs["total_return"], "年化:", outs["annual_return"], "夏普:", outs["sharpe"])
print("oos_discount:", r["oos_discount_note"])
print("约束:", r["constraints"])
print("幸存者:", r["survivorship_note"])
print("caveats 条数:", len(r["caveats"]))
print("逐笔前3:", json.dumps(r["trades"][:3], ensure_ascii=False))
print("月度前3:", r["monthly"][:3])
print("nav 首末:", r["nav_curve"][0], r["nav_curve"][-1])
# 一致性断言：顶层便捷字段 == metrics
assert r["total_return"] == r["metrics"]["total_return"]
assert r["max_drawdown"] == r["metrics"]["max_drawdown"]
assert r["sharpe"] == r["metrics"]["sharpe"]
print("CONSISTENCY_OK: 顶层与 metrics 同源")
