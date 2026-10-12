# -*- coding: utf-8 -*-
"""同步 config/sites.yaml 的扩容参数（修复 Edit 工具未落盘的残留差异）。
- top_players.top_n: 50 -> 200（与 selection.top_n 一致，消除回退默认值不一致）
- top_players.top_pct: 0.3 -> 0.5
- player_pool.target_total: 120 -> 600（候选池 500~1000，最终取前 200 人）
selection 段保持 top_n:200 / top_pct:0.50 不变。
"""
import io, sys

PATH = r"f:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\config\sites.yaml"

with io.open(PATH, "r", encoding="utf-8") as f:
    s = f.read()

repls = [
    ("      top_n: 50\n", "      top_n: 200\n"),
    ("      top_pct: 0.3\n", "      top_pct: 0.5\n"),
    ("      target_total: 120                 # 目标候选规模（>100）\n",
     "      target_total: 600                 # 候选池目标规模（500~1000 区间，最终取前 200 人）\n"),
]

changed = 0
for old, new in repls:
    if old in s:
        s = s.replace(old, new, 1)
        changed += 1
        print("APPLIED:", old.strip())
    else:
        print("SKIP (not found):", repr(old.strip()))

# 断言：确认不再残留旧值
assert "top_n: 50\n" not in s, "top_n:50 仍残留"
assert "top_pct: 0.3\n" not in s, "top_pct:0.3 仍残留"
assert "target_total: 120" not in s, "target_total:120 仍残留"
# 断言：新值已就位
assert "top_n: 200\n" in s
assert "top_pct: 0.5\n" in s
assert "target_total: 600" in s

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(s)

print(f"OK: applied {changed}/{len(repls)} replacements")
