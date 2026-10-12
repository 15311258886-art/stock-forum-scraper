# -*- coding: utf-8 -*-
"""QA(Edward) 独立验证 · 第1项：B 抽取修复 精度+召回。

只用解析器公开函数，跑「我手写用例」+「真实语料样本」。不发请求、不写库。
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from scraper.parsers.taoguba import TaogubaParser, ACTION_MAP  # noqa: E402
from scraper import name_match as nm  # noqa: E402

P = TaogubaParser({})
CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"


def show(title, text, expect):
    trades = P._extract_trades(text)
    print("=" * 100)
    print(f"[{title}]")
    print(f"  文本: {text}")
    print(f"  期望: {expect}")
    print(f"  实际({len(trades)}): " + json.dumps(
        [{"a": t["action"], "n": t["symbol_name"], "c": t["symbol"]} for t in trades],
        ensure_ascii=False))
    return trades


print("#" * 100)
print("# A) 手写用例")
print("#" * 100)

# --- 应剔除：防假阳性 ---
t = show("应剔除1 无标的泛述", "拉权重股出货，赶紧清仓", "0 条（无具体标的）")
show("应剔除2 列举非交易", "传媒：新华传媒、新华文轩", "0 条（列举≠交易）")
show("应剔除3 一动作词跟一串股名", "买入九阳股份、传艺科技、华北制药", "只应 1 条")
show("应剔除3b 原样例 @207 形态", "@207: 九阳股份/传艺科技/华北制药", "0 条（无动作词）")

# --- 应保留：防过度收紧 / 召回 ---
show("应保留1 清仓宁德时代", "清仓宁德时代", "1 条 SELL 宁德时代")
show("应保留2 买入贵州茅台", "买入贵州茅台", "1 条 BUY 贵州茅台")
show("应保留3 卖出600519", "卖出600519", "1 条 SELL 600519（名称可空）")
show("应保留4 三板上海洗霸无动词", "3板 上海洗霸", "0 条（无动词不入交易）")

# --- 重点探针：一动作词 + 并列多标的（合法句） ---
print()
print("#" * 100)
print("# B) 重点探针：一动作词 + 并列多个标的（合法买卖句）")
print("#" * 100)
show("探针1 清仓并列两标的", "清仓宁德时代、贵州茅台", "？(理想=2；精度优先可接受1)")
show("探针2 买入并列两标的", "买入贵州茅台、中国平安", "？(理想=2)")
show("探针3 卖出并列三标的", "卖出宁德时代、贵州茅台、中国平安", "？(理想=3)")
show("探针4 顿号并列", "今天买入宁德时代，贵州茅台也买了", "？(两个动作词，理想=2)")
show("探针5 隔句不得错配", "宁德时代今天很强。清仓贵州茅台", "1 条 SELL 茅台")

# --- 规则④ 是否会把同动作词的第二只也认领（1动作词→2票）检查 ---
print()
print("#" * 100)
print("# C) 真实语料样本（列举型复盘帖，最易假阳性）")
print("#" * 100)
rows = []
with open(CORPUS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("site") == "taoguba" and r.get("text_kind") == "full" and r.get("text"):
            rows.append(r)

# 取 tim炒股败家（九阳/传艺/华北 都在）帖
for r in rows:
    tx = r["text"]
    if "九阳股份" in tx and "传艺科技" in tx and "华北制药" in tx:
        tr = P._extract_trades(tx)
        print("=" * 100)
        print(f"作者={r['nickname']}  URL={r['post_url']}")
        print(f"  产出 {len(tr)} 条: " + json.dumps(
            [{"a": t["action"], "n": t["symbol_name"]} for t in tr], ensure_ascii=False))
        print(f"  原文(截300): {tx[:300]}")
        break
