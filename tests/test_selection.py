"""实盘选手入围标准 + A/B/C/D 分组的单元测试。

直接验证 scraper.players 的 select_top_players / label_channels 行为，无需启动 Web。
运行：.venv/Scripts/python.exe tests/test_selection.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper.players import select_top_players, label_channels, DEFAULT_CHANNEL_KEYWORDS

fails = []
def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        fails.append(name)

# —— 场景1：认证低名次直接入围，无数据非认证被剔除 ——
raw = [
    {"name": "冠军", "rank_position": 1, "ranking_score": 99, "is_certified": 0, "tags": ["A"], "has_position": 1},
    {"name": "榜眼", "rank_position": 2, "ranking_score": 90, "is_certified": 0, "tags": ["A"], "has_position": 1},
    {"name": "认证老周", "rank_position": 40, "ranking_score": 70, "is_certified": 1, "tags": ["A"], "has_position": 1},
    {"name": "水军小明", "rank_position": 55, "ranking_score": 40, "is_certified": 0, "tags": [], "has_position": 0},
]
rules = {"top_n": 999, "top_pct": 1.0, "require_verifiable": True, "certify_auto_include": True,
         "min_score": None, "active_within_days": None}
chosen = select_top_players(raw, rules)
names = {p.name for p in chosen}
check("认证低名次入围", "认证老周" in names, f"chosen={sorted(names)}")
check("无数据非认证剔除", "水军小明" not in names)
check("普通合格选手保留", {"冠军", "榜眼"} <= names)

# —— 场景2：收益门槛 min_score 生效 ——
rules2 = dict(rules); rules2["min_score"] = 92
chosen2 = select_top_players(raw, rules2)
names2 = {p.name for p in chosen2}
check("收益门槛剔除低分", "榜眼" not in names2 and "水军小明" not in names2, f"chosen={sorted(names2)}")
check("收益门槛保留认证", "认证老周" in names2)  # 认证优先于收益门槛

# —— 场景3：活跃度门槛 active_within_days 生效 ——
raw3 = [
    {"name": "活跃A", "rank_position": 1, "ranking_score": 90, "is_certified": 0, "tags": ["A"], "has_position": 1, "last_active_days": 2},
    {"name": "沉默B", "rank_position": 2, "ranking_score": 90, "is_certified": 0, "tags": ["A"], "has_position": 1, "last_active_days": 30},
    {"name": "认证C", "rank_position": 50, "ranking_score": 50, "is_certified": 1, "tags": ["A"], "has_position": 1, "last_active_days": 99},
]
rules3 = {"top_n": 999, "top_pct": 1.0, "require_verifiable": True, "certify_auto_include": True,
          "min_score": None, "active_within_days": 7}
chosen3 = select_top_players(raw3, rules3)
names3 = {p.name for p in chosen3}
check("活跃度门槛剔除沉默", "沉默B" not in names3, f"chosen={sorted(names3)}")
check("活跃度门槛保留认证", "认证C" in names3)

# —— 场景4：规模截点 top_pct 生效（只取前30%）——
raw4 = [{"name": f"P{i}", "rank_position": i, "ranking_score": 100 - i, "is_certified": 0, "tags": ["A"], "has_position": 1}
        for i in range(1, 21)]
rules4 = {"top_n": 999, "top_pct": 0.3, "require_verifiable": True, "certify_auto_include": True,
          "min_score": None, "active_within_days": None}
chosen4 = select_top_players(raw4, rules4)
check("规模截点 top_pct", len(chosen4) == 6, f"len={len(chosen4)} (期望6)")

# —— 场景5：真实性门槛——数据未采集(None)不误杀 ——
raw5 = [
    {"name": "未采A", "rank_position": 1, "ranking_score": 90, "is_certified": 0, "tags": ["A"], "has_position": None},
    {"name": "确认空", "rank_position": 2, "ranking_score": 90, "is_certified": 0, "tags": ["A"], "has_position": 0},
]
chosen5 = select_top_players(raw5, rules)
names5 = {p.name for p in chosen5}
check("未采集不误杀", "未采A" in names5)
check("确认空谈剔除", "确认空" not in names5)

# —— 场景6：A/B/C/D 按 tags 真实分组 ——
players = [
    select_top_players([{"name": "大赛王", "rank_position": 1, "tags": ["实盘大赛"]}], rules)[0],
    select_top_players([{"name": "直播姐", "rank_position": 2, "tags": ["直播"]}], rules)[0],
    select_top_players([{"name": "专栏君", "rank_position": 3, "tags": ["专栏"]}], rules)[0],
    select_top_players([{"name": "人气妹", "rank_position": 4, "tags": ["人气"]}], rules)[0],
]
labeled = label_channels(players, [], DEFAULT_CHANNEL_KEYWORDS)
ch_map = {p.name: p.channel for p in labeled}
check("标签→A 实盘大赛", ch_map["大赛王"] == "A", str(ch_map))
check("标签→B 直播", ch_map["直播姐"] == "B")
check("标签→C 专栏", ch_map["专栏君"] == "C")
check("标签→D 人气", ch_map["人气妹"] == "D")

# —— 场景7：无标签按名次轮询兜底 ——
players7 = [select_top_players([{"name": f"X{i}", "rank_position": i, "tags": []}], rules)[0] for i in range(1, 5)]
labeled7 = label_channels(players7, [], DEFAULT_CHANNEL_KEYWORDS)
ch7 = [p.channel for p in labeled7]
check("无标签轮询兜底", ch7 == ["A", "B", "C", "D"], str(ch7))

print("\n=== 结果 ===")
if fails:
    print(f"失败 {len(fails)} 项: {fails}")
    raise SystemExit(1)
print("全部通过 ✅")
