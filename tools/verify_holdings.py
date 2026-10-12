"""验收脚本：重载示例数据并核对「持仓起始日期 + 卖出分区」是否符合口径。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from viewer.db import Database  # noqa: E402
from viewer.demo_data import load_demo  # noqa: E402

db = Database()
# 2026-09-23：load_demo 新增 confirm 门槛（防误清真实数据）。此脚本面向验收，
# 显式确认；如需隔离请先设 VIEWER_DB 指向副本库。
load_demo(db, confirm=True)
print("示例数据已重载\n")

cases = ["大阪柯小李", "尾盘狙击", "价值投资人"]
for name in cases:
    d = db.player_detail(name, recent_days=5)
    if not d:
        print(f"[{name}] 未找到")
        continue
    print(f"=== {name} === 持仓 {d['holding_count']} / 卖出 {d['sell_count']} / 已清仓 {d['sold_out'] or '无'}")
    for h in d["holdings"]:
        print(f"  持仓 {h['symbol']} {h['symbol_name']:<6} 起始={h['hold_since'] or '—':<17} "
              f"持有={h['hold_days']} 天 比例={h['ratio']}% 建仓价={h['buy_price']} "
              f"{'[已清仓]' if h.get('closed') else ''}")
    for s in d["sells"]:
        print(f"  卖出 {s['symbol']} {s['symbol_name']:<6} 类型={s['close_type']} "
              f"卖出时间={s['sell_time']} 卖出价={s['sell_price']} 卖出比例={s['ratio']}% "
              f"起始={s['hold_since']} 持有={s['hold_days']}天 了结盈亏={s['pnl']}")

print("\n=== 口径自检 ===")
d = db.player_detail("大阪柯小李")
# 基准：positions 与 trades 中该标的**所有**建仓时间的最早值（两处都是真实来源）
cands = [it["buy_time"] for it in d["holdings"]
         if it.get("symbol") == "002594" and it.get("buy_time")]
cands += [it["buy_time"] for it in d["trades"]
          if it.get("symbol") == "002594" and (it.get("action") or "").upper() in ("BUY", "HOLD") and it.get("buy_time")]
h2594 = next((h for h in d["holdings"] if h["symbol"] == "002594"), None)
print(f"002594 比亚迪全部建仓时间={sorted(cands)}")
print(f"002594 持仓起始日期={h2594['hold_since'] if h2594 else None}")
ok = bool(h2594) and bool(cands) and h2594["hold_since"] == min(cands)
print("结论：", "PASS —— 多次加仓未把起始日推后" if ok else "FAIL —— 起始日口径有误")

# 持有天数：日期精度与分钟精度都必须算得出
bad = [h["symbol"] for h in d["holdings"] if h["hold_days"] is None]
print(f"持有天数为空的持仓={bad or '无'}　", "PASS" if not bad else "FAIL —— 日期精度未覆盖")

d2 = db.player_detail("价值投资人")
pv = next((h for h in d2["holdings"] if h["symbol"] == "601318"), None)
print(f"601318 显式披露 hold_since={pv['hold_since'] if pv else None}"
      f"（帖内披露 2026-07-18，应优先于买入记录 2026-07-20）")
print("结论：", "PASS —— 显式披露优先" if pv and pv["hold_since"] == "2026-07-18" else "FAIL")
