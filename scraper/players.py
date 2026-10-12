"""实盘选手筛选与渠道标注。

覆盖需求 1 与需求 2，并补入「入围评判标准」：
- 需求 1：从实盘操作选手中筛选靠前用户（取前 30% 或前 50 名，以更严格者为准，作为规模截点）。
- 需求 2：按来源渠道（A、B、C、D 等）标注每位实盘用户，代表真实来源分组。

入围评判标准（config.sites.yaml 的 `selection` 段可配置）：
  1. 官方认证优先：带「认证/示范/实盘大赛」等官方标识的选手直接入围（certify_auto_include）。
  2. 真实性门槛：非认证选手必须能解析出可核验的持仓比例或实盘交易记录，否则视为空谈、剔除
     （require_verifiable；仅当「已确认无持仓/无交易」时才剔除，数据尚未采集到时不当作失败）。
  3. 收益/风控门槛：ranking_score 低于 min_score 的剔除（min_score 为 null 表示不卡收益）。
  4. 活跃度门槛：最近一次更新超过 active_within_days 天的剔除（null 表示不卡活跃度）。
  5. 规模截点：通过前四道门槛者，按「认证优先、其余按名次」排序后，再取前 top_n / 前 top_pct（更严格者）。

A/B/C/D 渠道（真实来源分组）：
  - A = 实盘大赛选手（标签含 大赛/比赛/擂台…）
  - B = 直播实盘高手（标签含 直播/直播间…）
  - C = 专栏作者（标签含 专栏/作家…）
  - D = 人气榜/活跃用户（标签含 人气/新锐…）
  判定优先级：显式 channel_rules（按 name/source_url 关键字）→ 选手自带 tags → source_url 关键字 → 名次轮询兜底。
  也可在入库后用 db.set_player_channel(name, channel) 手动改。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence


# 渠道默认关键词（可按 config 的 channel_keywords 覆盖）
DEFAULT_CHANNEL_KEYWORDS: dict[str, list[str]] = {
    "A": ["实盘大赛", "大赛", "比赛", "擂台", "挑战赛"],
    "B": ["直播", "直播间"],
    "C": ["专栏", "作家", "特约", "名家"],
    "D": ["人气", "热度", "新锐", "榜单"],
}

# ---------------------------------------------------------------- 层级体系（新增，不改动 ABCD 规则）
# 原 A/B/C/D 的判定规则**完全不变**（仍是来源渠道分组）；本段在其之上增设 E、F 两个层级，
# 用于容纳「热度不高但具备一定影响力」的选手（如较大主播 / 中V），并给出递减权重。
#
# 关键约束（需求原文）：低层级选手权重可降低，但**必须保留露出**、可查看其观点与持仓，
# 不因热度低而隐藏 —— 因此层级只影响权重与排序，不影响是否展示（is_visible 恒为 1）。
TIER_ORDER: list[str] = ["A", "B", "C", "D", "E", "F"]

TIER_WEIGHTS: dict[str, float] = {
    "A": 1.00,   # 实盘大赛（最高权重）
    "B": 0.80,   # 直播高手
    "C": 0.62,   # 专栏作者
    "D": 0.45,   # 人气榜
    "E": 0.28,   # 新增：主播 / 大V（影响力达标但热度较低）
    "F": 0.15,   # 新增：长尾观察名单（仅露出，权重最低）
}

TIER_LABELS: dict[str, str] = {
    "A": "A 实盘大赛",
    "B": "B 直播高手",
    "C": "C 专栏作者",
    "D": "D 人气榜",
    "E": "E 主播大V",
    "F": "F 长尾观察",
}


def assign_tier(channel: str, is_top_player: int, is_certified: int = 0,
                influence: float = 0.0, followers: int = 0,
                influence_threshold: float = 60.0, followers_threshold: int = 50000) -> str:
    """给选手定层级（A~F）。

    - 已入选核心池者：层级 = 其来源渠道（A/B/C/D），**沿用原规则，不做任何改动**；
      渠道异常时兜底 D。
    - 未进核心池者：仍**保留露出**，按影响力落在 E（主播/大V）或 F（长尾观察）：
        · 官方认证，或 influence >= 阈值，或 followers >= 阈值 → E
        · 其余 → F
    """
    if is_top_player:
        return channel if channel in ("A", "B", "C", "D") else "D"
    if is_certified or float(influence or 0) >= influence_threshold or int(followers or 0) >= followers_threshold:
        return "E"
    return "F"


def tier_weight(tier: str) -> float:
    return TIER_WEIGHTS.get((tier or "F").upper(), 0.15)


def tier_label(tier: str) -> str:
    return TIER_LABELS.get((tier or "F").upper(), f"{tier} 观察")


def apply_tiers(players: Sequence[dict], core_names: set[str],
                influence_map: dict[str, dict] | None = None) -> list[dict]:
    """批量为候选选手定层级，返回新增 tier/tier_weight/is_visible 的副本列表。

    players: 全部候选（含未入选者），每条含 name/channel/is_top_player/is_certified/platform。
    core_names: 已入选核心池的姓名集合（由 select_top_players 产出，规则不变）。
    influence_map: {name: {"influence": float, "followers": int}} 影响力证据。
    """
    influence_map = influence_map or {}
    out: list[dict] = []
    for p in players:
        name = p.get("name", "")
        is_top = 1 if (p.get("is_top_player") or name in core_names) else 0
        inf = influence_map.get(name, {})
        tier = assign_tier(
            channel=p.get("channel", "D"), is_top_player=is_top,
            is_certified=int(bool(p.get("is_certified"))),
            influence=float(inf.get("influence", p.get("influence", 0)) or 0),
            followers=int(inf.get("followers", p.get("followers", 0)) or 0),
        )
        out.append({**p, "is_top_player": is_top, "tier": tier,
                    "tier_weight": tier_weight(tier), "is_visible": 1,
                    "influence": float(inf.get("influence", p.get("influence", 0)) or 0),
                    "followers": int(inf.get("followers", p.get("followers", 0)) or 0)})
    return out


@dataclass
class Player:
    name: str
    rank_position: Optional[int] = None
    ranking_score: Optional[float] = None
    source_url: str = ""
    channel: str = "A"
    is_top_player: int = 0
    is_certified: int = 0          # 官方认证/示范标识（1=是）
    tags: list[str] = field(default_factory=list)   # 选手行内识别到的渠道关键词来源


def select_top_players(
    ranked: Sequence[dict],
    rules: Optional[dict] = None,
) -> list[Player]:
    """按「入围评判标准」筛选实盘选手。

    ranked: 已抓取到的候选选手列表（每条含 name / rank_position / ranking_score /
            is_certified / has_position / trade_count / last_active_days / source_url / tags）。
            其中 has_position / trade_count / last_active_days 为「可空」字段：
            - None 表示「尚未采集到，无法判断」→ 不当作失败；
            - 明确为 0 / 超阈值 表示「已确认不达标」→ 触发剔除。
    rules: config 的 selection 段，缺省使用保守默认。

    命中者 is_top_player=1，作为后续采集对象。
    """
    rules = rules or {}
    top_n = int(rules.get("top_n", 50))
    top_pct = float(rules.get("top_pct", 0.30))
    min_score = rules.get("min_score", None)
    require_verifiable = bool(rules.get("require_verifiable", True))
    certify_auto_include = bool(rules.get("certify_auto_include", True))
    active_within_days = rules.get("active_within_days", None)

    if not ranked:
        return []

    # 若列表未带 rank_position，则按出现顺序补 1..N
    for i, r in enumerate(ranked):
        if r.get("rank_position") is None:
            r["rank_position"] = i + 1
    ranked = sorted(ranked, key=lambda x: (x.get("rank_position") or 9999))

    def qualifies(r: dict) -> bool:
        # ① 官方认证直接入围
        if certify_auto_include and r.get("is_certified"):
            return True
        # ② 真实性门槛：仅在「已确认无持仓且无交易」时剔除
        verified_known = r.get("has_position") is not None or r.get("trade_count") is not None
        if require_verifiable and verified_known and not (r.get("has_position") or r.get("trade_count")):
            return False
        # ③ 收益门槛
        if min_score is not None:
            sc = r.get("ranking_score")
            if sc is None or sc < min_score:
                return False
        # ④ 活跃度门槛
        if active_within_days is not None:
            lad = r.get("last_active_days")
            if lad is None or lad > active_within_days:
                return False
        return True

    qualified = [r for r in ranked if qualifies(r)]
    # 认证优先，其余维持名次升序
    certified = [r for r in qualified if r.get("is_certified")]
    others = [r for r in qualified if not r.get("is_certified")]
    ordered = certified + others

    pct_cut = max(1, int(len(ranked) * top_pct))
    cut = min(top_n, pct_cut)
    chosen = ordered[:cut]

    out: list[Player] = []
    for r in chosen:
        out.append(Player(
            name=r["name"],
            rank_position=r.get("rank_position"),
            ranking_score=r.get("ranking_score"),
            source_url=r.get("source_url", ""),
            is_top_player=1,
            is_certified=int(bool(r.get("is_certified"))),
            tags=r.get("tags") or [],
            channel="A",  # 随后由 label_channel 覆盖
        ))
    return out


def _match_rule(text: str, rule: dict) -> bool:
    if not text:
        return False
    if "contains" in rule:
        return rule["contains"] in text
    if "regex" in rule:
        try:
            return re.search(rule["regex"], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def label_channel(
    player: Player,
    rules: Optional[list[dict]] = None,
    keywords: Optional[dict[str, list[str]]] = None,
    cycle: list[str] = ("A", "B", "C", "D"),
) -> str:
    """按规则/标签为某选手分配渠道（真实来源分组）。

    优先级：
      1) 显式 channel_rules（match_by=name/source_url 关键字）→ 直接定渠道；
      2) 选手自带 tags（parse_players 从行内文本识别的关键词来源）→ 命中 A/B/C/D 关键词即归位；
      3) source_url 含渠道关键词；
      4) 名次轮询兜底（保证人人有标注）。
    """
    rules = rules or []
    keywords = keywords or DEFAULT_CHANNEL_KEYWORDS
    tagtext = " ".join(player.tags)

    # 1) 显式规则
    for rule in rules:
        field_name = rule.get("match_by", "name")
        text = player.source_url if field_name == "source_url" else player.name
        if _match_rule(text, rule):
            return rule.get("channel", "A")

    # 2) 选手自带 tags（parse_players 输出的渠道标识）：
    #    - 若 tag 本身就是渠道字母（A/B/C/D）则直接命中；
    #    - 否则按 A→B→C→D 优先级检查 tag 文本是否含渠道关键词。
    for ch in cycle:
        if ch in player.tags:
            return ch
    for ch in cycle:
        for kw in keywords.get(ch, []):
            if kw and kw in tagtext:
                return ch

    # 3) source_url 关键词
    for ch in cycle:
        for kw in keywords.get(ch, []):
            if kw and kw in player.source_url:
                return ch

    # 4) 名次轮询兜底（rank 1 -> A, rank 2 -> B, ...）
    idx = ((player.rank_position or 1) - 1) % len(cycle)
    return cycle[idx]


def label_channels(
    players: Sequence[Player],
    rules: Optional[list[dict]] = None,
    keywords: Optional[dict[str, list[str]]] = None,
    cycle: list[str] = ("A", "B", "C", "D"),
) -> list[Player]:
    """批量标注渠道，并返回带 channel 的副本。"""
    out = []
    for p in players:
        p = Player(**{**p.__dict__})  # 浅拷贝，避免改动入参
        p.channel = label_channel(p, rules, keywords, cycle)
        out.append(p)
    return out


# ---------------------------------------------------------------- 行为证据入选（2026-09-25 新增）
# 背景：东财/同花顺解析器显式放弃 parse_players（无公开选手排行榜），雪球未配选手页，
# 导致选手池只剩淘股吧一站（示例数据清理后仅 68 人）。本节从**已入库的真实发帖/交易/持仓
# 行为**跨站推导候选选手，作为选手榜缺位的补充来源——数据全部来自库内真实记录，零杜撰。
#
# 入围评判标准（行为版，与上文 ranking 版并行使用）：
#   1. 榜选/认证保留：已有名次（rank_position）或官方认证的选手直接保留，不参与证据分排序；
#   2. 真实性门槛：必须库内有真实发帖（posts）才算候选——纯榜名无行为者不虚增；
#   3. 证据分 = 发帖数 + 3×交易记录数 + 5×持仓记录数（晒实盘 > 空谈）；
#   4. 规模截点：榜选/认证全保留，其余按证据分降序补足至 target_total（~200）。
EVIDENCE_W_POST = 1.0
EVIDENCE_W_TRADE = 3.0
EVIDENCE_W_POSITION = 5.0


def evidence_score(post_count: int, trade_count: int, position_count: int) -> float:
    """行为证据分：发帖为基础分，真实交易/持仓按更高权重加权。"""
    return (float(post_count or 0) * EVIDENCE_W_POST
            + float(trade_count or 0) * EVIDENCE_W_TRADE
            + float(position_count or 0) * EVIDENCE_W_POSITION)


def derive_channel(post_count: int, trade_count: int, position_count: int) -> str:
    """按行为推导来源渠道（保持 A/B/C/D 语义：渠道=来源分组）。

    - 有真实交易或持仓记录 → A（实盘类：晒实盘操作）；
    - 发帖量大（>=20 条，持续输出观点）→ C（专栏作者类）；
    - 其余活跃发帖 → D（人气榜/活跃用户）。
    """
    if int(trade_count or 0) > 0 or int(position_count or 0) > 0:
        return "A"
    if int(post_count or 0) >= 20:
        return "C"
    return "D"


def select_by_evidence(
    rows: Sequence[dict],
    keep_names: set[str],
    target_total: int = 200,
    diversify: bool = True,
) -> list[dict]:
    """从行为聚合行中选出入选名单（纯函数，便于单测）。

    rows: 每条含 {site, name, post_count, trade_count, position_count}；
          可选 is_certified / rank_position（来自已有榜选记录时带上）。
    keep_names: 榜选/认证选手名集合——直接入选，不占证据分名额。
    target_total: 入选总规模（含 keep），默认 200。
    diversify: 站间交错配额（2026-09-25）。True 时先按「站内证据分名次」排序再全局交错，
               保证每个来源站都有代表（否则单站作者数占优会把其它站全部挤出局）；
               站内仍按证据分择优，不牺牲 merit。

    返回入选列表，每条新增 evidence / channel / is_top_player=1；
    排序：keep（榜选/认证）在前，其余按（站内名次, 证据分降序, 名字兜底）确定性排序。
    """
    kept: list[dict] = []
    rest: list[dict] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        ev = evidence_score(r.get("post_count"), r.get("trade_count"), r.get("position_count"))
        item = {**r, "name": name, "evidence": ev,
                "channel": derive_channel(r.get("post_count"), r.get("trade_count"), r.get("position_count")),
                "is_top_player": 1}
        if name in keep_names or r.get("is_certified") or r.get("rank_position"):
            kept.append(item)
        else:
            rest.append(item)
    if diversify:
        # 站内名次：同站内按证据分降序，得到 1..N 的站内排名
        by_site: dict[str, list[dict]] = {}
        for it in rest:
            by_site.setdefault(str(it.get("site") or "?"), []).append(it)
        for site_items in by_site.values():
            site_items.sort(key=lambda x: (-x["evidence"], -int(x.get("post_count") or 0), x["name"]))
            for i, it in enumerate(site_items):
                it["_site_rank"] = i + 1
        rest.sort(key=lambda x: (x["_site_rank"], -x["evidence"], x["name"]))
    else:
        rest.sort(key=lambda x: (-x["evidence"], -int(x.get("post_count") or 0), x["name"]))
    fill = max(0, int(target_total) - len(kept))
    return kept + rest[:fill]
