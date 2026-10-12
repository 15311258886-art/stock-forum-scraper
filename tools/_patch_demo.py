import io, sys, os, re

ROOT = r"f:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
PATH = os.path.join(ROOT, "viewer", "demo_data.py")

with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

repls = []

# R1 header
repls.append((
"""1. **选手池扩容**：候选与实盘选手扩充至 120+，覆盖 4 个平台
   （淘股吧 / 东方财富股吧 / 雪球 / 同花顺），用于扩大共同持仓共识的计算基数。""",
"""1. **选手池扩容（淘五八接入后）**：候选池扩至约 600 人（500~1000 区间），
   沿用原有 5 道入围筛选（认证优先 / 真实性 / 收益 / 活跃度 / 规模截点），取前 200 人纳入测评与关注；
   未入选者不入 players 表，保证新增关注质量。覆盖 4 个平台（淘股吧 / 东方财富股吧 / 雪球 / 同花顺）。"""))

# R2 line 116
repls.append((
"# 其余候选选手由「前缀×后缀」确定性生成（扩充到 120+），覆盖 4 个平台",
"# 其余候选选手由「前缀×后缀」确定性生成（候选池扩至约 600 人，落入 500~1000 区间），再经 select_top_players 取前 200 人；覆盖 4 个平台"))

# R3 _build_candidates
repls.append((
"""def _build_candidates() -> list[tuple]:
    \"\"\"合并种子 + 生成，总候选规模 >= 120（需求：扩充至 100 人以上）。\"\"\"
    names = {p[0] for p in SEED_PLAYERS}
    gen = _gen_candidates(names, need=140)
    return SEED_PLAYERS + gen""",
"""def _build_candidates() -> list[tuple]:
    \"\"\"合并种子 + 生成。候选池扩至约 600 人（500~1000 区间），
    供 select_top_players 经原有筛选逻辑取前 200 人纳入测评与关注。\"\"\"
    names = {p[0] for p in SEED_PLAYERS}
    gen = _gen_candidates(names, need=600)
    return SEED_PLAYERS + gen"""))

# R4 sel_rules
repls.append((
"""    # ⚠ 评分标准（select_top_players 的 5 道门槛）保持不变，仅放宽规模截点以演示扩容后的规模
    sel_rules = {"top_n": 999, "top_pct": 1.0, "require_verifiable": True,
                 "certify_auto_include": True, "min_score": None, "active_within_days": None}""",
"""    # ⚠ 评分标准（select_top_players 的 5 道门槛）保持不变，仅放宽规模截点：
    # 候选池约 600 → 取前 200 人纳入测评与关注（certify_auto_include 使认证选手始终入围，天然留有余量）。
    sel_rules = {"top_n": 200, "top_pct": 1.0, "require_verifiable": True,
                 "certify_auto_include": True, "min_score": None, "active_within_days": None}"""))

# R5 tier_map -> insert chosen_cands (current broken block)
repls.append((
"""    tier_map = {t["name"]: t for t in tiered}

    # 仅将入选的前 200 人写入 players 表（其余候选不入表，保证新增关注质量）
    for (name, rk, sc, cert, tags, hp, plat, fans, infl) in chosen_cands:""",
"""    tier_map = {t["name"]: t for t in tiered}

    # 2026-09-22 扩容（淘五八接入 + 用户要求）：从约 600 人候选池按原有筛选逻辑
    # 选出前 200 人纳入测评与关注；其余候选不入 players 表，保证新增关注质量。
    chosen_cands = sorted([c for c in candidates if c[0] in core_names],
                          key=lambda c: c[1])

    # 仅将入选的前 200 人写入 players 表（其余候选不入表，保证新增关注质量）
    for (name, rk, sc, cert, tags, hp, plat, fans, infl) in chosen_cands:"""))

# R6 positions
repls.append((
"    all_positions = SAMPLE_POSITIONS + _expand_positions(candidates, rnd)",
"    all_positions = SAMPLE_POSITIONS + _expand_positions(chosen_cands, rnd)"))

# R7 trades call
repls.append((
"    for (author, action, sym, name, price, qty, raw, off, cur, lvl, hint) in SAMPLE_TRADES + _expand_trades(candidates, rnd):",
"    for (author, action, sym, name, price, qty, raw, off, cur, lvl, hint) in SAMPLE_TRADES + _expand_trades(chosen_cands, rnd):"))

# R8 posts loop (match by following STOCK_UNIVERSE line to stay unique)
repls.append((
"    for i, (name, rank, score, cert, tags, hp, plat, fans, infl) in enumerate(candidates):\n        code, sname, cur = STOCK_UNIVERSE[i % len(STOCK_UNIVERSE)]",
"    for i, (name, rank, score, cert, tags, hp, plat, fans, infl) in enumerate(chosen_cands):\n        code, sname, cur = STOCK_UNIVERSE[i % len(STOCK_UNIVERSE)]"))

applied = []
for i, (old, new) in enumerate(repls, 1):
    cnt = src.count(old)
    if cnt == 0:
        print(f"[跳过 R{i}] 未找到匹配（可能已应用或文本不同）")
        continue
    if cnt > 1:
        print(f"[警告 R{i}] 匹配 {cnt} 处，仅替换第一处！old=\n{old[:80]}")
    src = src.replace(old, new, 1)
    applied.append(i)
    print(f"[应用 R{i}] OK")

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("\n已写入。应用项:", applied)
