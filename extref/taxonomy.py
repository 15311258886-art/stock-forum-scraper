"""外部参考 · 信源与信号的分类体系（纯常量 + 小工具，无 IO）。

四组维度（前端展示与筛选都按这四组走，避免各处硬编码中文）：
  ① kind   信源种类：软件/榜单/社区/研报/席位
  ② tier   信源分档：A 持牌投顾 / B 数据平台 / C 社区垂媒 / D 硬件终端
  ③ access 取数通道：公开页直采 / 合规 MCP / 人工导出 / 登录后抓 / 未开放
  ④ hardness 信号硬度：hard 可验证硬数据 / soft 观点软信号
"""
from __future__ import annotations

# ---------- ① 信源种类 ----------
KINDS: list[dict] = [
    {"key": "software",   "label": "选股软件", "desc": "第三方荐股/选股 App 的输出（问财、妙想、九方等）"},
    {"key": "mcp",        "label": "数据接口", "desc": "机构数据终端开放给 Agent 的 MCP/Skills 能力"},
    {"key": "rank",       "label": "公开榜单", "desc": "人气榜 / 热股榜 / 关注度榜（东财、雪球、淘股吧）"},
    {"key": "community",  "label": "社区共识", "desc": "论坛与社区的集体判断（股吧、雪球、淘股吧、韭研）"},
    {"key": "research",   "label": "机构观点", "desc": "券商研报 / 金股组合 / 投顾观点"},
    {"key": "seat",       "label": "游资席位", "desc": "龙虎榜买卖席位及其历史胜率统计"},
]
KIND_LABEL = {k["key"]: k["label"] for k in KINDS}

# ---------- ② 信源分档 ----------
TIERS: list[dict] = [
    {"key": "A", "label": "A 档 · 持牌投顾", "desc": "持有证监会证券投资咨询牌照的经营机构，可合法输出投资建议",
     "color": "ok"},
    {"key": "B", "label": "B 档 · 数据平台", "desc": "行情/数据终端服务商，已开放 MCP/Skills 供 Agent 调用",
     "color": "info"},
    {"key": "C", "label": "C 档 · 社区垂媒", "desc": "公开社区与榜单，反映群体注意力（非投资建议）",
     "color": "warn"},
    {"key": "D", "label": "D 档 · 硬件终端", "desc": "以专用设备为载体的投顾产品",
     "color": "warn"},
]
TIER_LABEL = {t["key"]: t["label"] for t in TIERS}

# ---------- ③ 取数通道 ----------
ACCESS: list[dict] = [
    {"key": "mcp",        "label": "合规 MCP/API", "desc": "官方开放的接口，最稳，可能需付费或商务开通"},
    {"key": "public_page", "label": "公开页直采",  "desc": "无需登录的公开榜单/页面，覆盖有限"},
    {"key": "manual",     "label": "人工导出",     "desc": "由用户导出后我结构化，覆盖最广但要花时间"},
    {"key": "login",      "label": "登录后抓",     "desc": "需登录态；有条款与风控风险，须谨慎低频"},
    {"key": "blocked",    "label": "未开放",       "desc": "暂无可用通道或已明确不对外开放"},
]
ACCESS_LABEL = {a["key"]: a["label"] for a in ACCESS}

# ---------- 接入状态 ----------
STATUS: list[dict] = [
    {"key": "live",    "label": "已接入", "desc": "已有真实信号进入本板块"},
    {"key": "pending", "label": "待接入", "desc": "通道已明确，等待开通/注册/导出"},
    {"key": "blocked", "label": "受限",   "desc": "合规红线或技术不通，暂不接入"},
]
STATUS_LABEL = {s["key"]: s["label"] for s in STATUS}

# ---------- ④ 信号硬度 ----------
HARDNESS: list[dict] = [
    {"key": "hard", "label": "硬数据", "desc": "来自可验证的公开披露（成交、席位、股东、榜单名次）"},
    {"key": "soft", "label": "软信号", "desc": "来自主体观点（软件推荐、研报、大V），必须回测才可用"},
]
HARDNESS_LABEL = {h["key"]: h["label"] for h in HARDNESS}

# ---------- 方向 ----------
DIRECTIONS = ["看多", "看空", "中性"]

# ---------- 合规红线（不可接入名单的口径说明，前端常驻展示） ----------
RED_LINE = {
    "title": "合规红线（不接入）",
    "items": [
        "以「包赚 / 保收益 / 内部票 / 稳赚不赔」为卖点的荐股 App 与软件；",
        "拉群收费、代客理财、承诺收益的所谓「老师」与社群；",
        "未取得证券投资咨询牌照却提供个股买卖建议的机构或个人。",
    ],
    "note": ("依据《证券法》《证券投资咨询业务暂行规定》，未经证监会许可从事证券投资咨询属违法行为；"
             "持牌仅代表业务资质，**不代表盈利保证**。本板块所列信号一律不构成投资建议。"),
}


def kind_label(k: str) -> str:
    return KIND_LABEL.get((k or "").strip(), k or "")


def tier_label(t: str) -> str:
    return TIER_LABEL.get((t or "").strip(), t or "")


def tier_color(t: str) -> str:
    for x in TIERS:
        if x["key"] == t:
            return x.get("color") or "info"
    return "info"


def tier_desc(t: str) -> str:
    for x in TIERS:
        if x["key"] == t:
            return x["desc"]
    return ""


def access_label(a: str) -> str:
    return ACCESS_LABEL.get((a or "").strip(), a or "")


def status_label(s: str) -> str:
    return STATUS_LABEL.get((s or "").strip(), s or "")


def hardness_label(h: str) -> str:
    return HARDNESS_LABEL.get((h or "").strip(), h or "")


def strength_text(n) -> str:
    try:
        n = int(n)
    except Exception:
        n = 1
    n = max(1, min(5, n))
    return {1: "低", 2: "偏低", 3: "中", 4: "偏高", 5: "高"}[n]
