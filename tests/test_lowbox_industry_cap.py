# -*- coding: utf-8 -*-
"""低位池（lowbox）/ 低波池 行业上限的单元测试。

直接验证 viewer/pool_cap.py 的纯逻辑（生产代码 api_screener_lowbox 与回测脚本共用同一函数），
覆盖：一级行业归一、缺失归"未知"、按占比上限 / 按只数上限 生效、缺失字段不逃避限制。
运行：.venv/Scripts/python.exe tests/test_lowbox_industry_cap.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import pool_cap as pc

fails = []
def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("  " + detail if detail else ""))
    if not cond:
        fails.append(name)


# —— 一级行业归一 ——
check("一级行业取'-'前段", pc.industry_level1("制造业-食品制造业") == "制造业")
check("单段原样返回", pc.industry_level1("制造业") == "制造业")
check("缺失归未知", pc.industry_level1("") == "未知")
check("None归未知", pc.industry_level1(None) == "未知")
check("空白归未知", pc.industry_level1("   ") == "未知")


def make_rows(n_per_ind, industries):
    """构造按优先级（分）排好序的候选行：每个行业 n_per_ind 只，分数随序号递减。"""
    rows = []
    seq = 0
    for ind in industries:
        for _ in range(n_per_ind):
            seq += 1
            rows.append({"industry1": ind, "score": 100 - seq})
    return rows


# —— 不加任何上限 == 原 [:limit] 截断 ——
rows = make_rows(10, ["银行", "地产", "医药"])
capped, counts = pc.apply_industry_cap(rows, 30)
check("无上限取满30", len(capped) == 30, "len=%d" % len(capped))
check("无上限counts为空", counts == {}, str(counts))

# —— 按占比上限生效（20% → limit=30 ⇒ 每行业≤6）——
rows = make_rows(10, ["银行", "地产", "医药", "煤炭", "电力"])
capped, counts = pc.apply_industry_cap(rows, 30, max_industry_pct=0.20, industry_key="industry1")
check("20%上限每行业≤6", all(c <= 6 for c in counts.values()), str(counts))
check("20%上限总计≤30", sum(counts.values()) <= 30, "sum=%d" % sum(counts.values()))
# 银行原本有10只且分最高，应只保留前6只（最高分），其余被跳过
bank_kept = [r for r in capped if r["industry1"] == "银行"]
check("被限行业保留最高分6只", len(bank_kept) == 6 and bank_kept[0]["score"] == 99,
      "kept=%d top_score=%s" % (len(bank_kept), bank_kept[0]["score"] if bank_kept else None))
# 且保留的6只是银行业内分数最高的6只（分数严格降序）
bk_scores = [r["score"] for r in bank_kept]
check("保留的是行业内最高分", bk_scores == sorted(bk_scores, reverse=True) and bk_scores == [99, 98, 97, 96, 95, 94],
      str(bk_scores))

# —— 按只数上限生效（≤3）；候选足够多时应能填满到30 ——
rows = make_rows(8, ["银行", "地产", "医药", "煤炭", "电力", "钢铁", "医药2", "煤炭2", "电力2", "钢铁2"])
capped, counts = pc.apply_industry_cap(rows, 30, max_per_industry=3, industry_key="industry1")
check("单行业≤3只", all(c <= 3 for c in counts.values()), str(counts))
check("只数上限可填满到30", sum(counts.values()) == 30, "sum=%d" % sum(counts.values()))

# —— 两个阈值同时生效取更紧者（pct=20%⇒6 与 per=3 ⇒ 取3）——
rows = make_rows(8, ["银行", "地产", "医药", "煤炭", "电力", "钢铁"])
capped, counts = pc.apply_industry_cap(rows, 30, max_per_industry=3, max_industry_pct=0.20,
                                       industry_key="industry1")
check("双阈值取更紧(≤3)", all(c <= 3 for c in counts.values()), str(counts))

# —— 缺失字段归'未知'且同样受上限约束（不能逃避限制）——
rows = [{"industry1": "" if i % 2 == 0 else "银行", "score": 100 - i} for i in range(20)]
capped, counts = pc.apply_industry_cap(rows, 30, max_per_industry=4, industry_key="industry1")
check("未知组受上限约束", counts.get("未知", 0) <= 4, str(counts))

# —— 因上限无法填满 limit 时返回少于 limit ——
rows = make_rows(3, ["银行", "地产"])  # 仅 6 只，且每行业限2 ⇒ 最多4只
capped, counts = pc.apply_industry_cap(rows, 30, max_per_industry=2, industry_key="industry1")
check("上限致池子<limit", len(capped) == 4, "len=%d" % len(capped))

# —— concentration 计算 ——
top1, top3, nind, size = pc.concentration({"银行": 6, "地产": 3, "医药": 1})
check("concentration top1", abs(top1 - 0.6) < 1e-9, "top1=%.3f" % top1)
check("concentration top3", abs(top3 - 1.0) < 1e-9, "top3=%.3f" % top3)
check("concentration 空集合安全", pc.concentration({})[0] == 0.0)


print("\n=== 结果 ===")
if fails:
    print("失败 %d 项: %s" % (len(fails), fails))
    raise SystemExit(1)
print("全部通过 ✅")
