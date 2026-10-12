"""国际事件分类体系 + 影响映射。

设计原则（重要，别把两层混了）：
  1) 类别按**事件性质**划分（政治/贸易/地缘/能源/金融/科技/灾害），不按市场主题划分——
     同一场冲突可以同时利多军工、利空航空，按主题分会把一条事件劈成两半。
  2) `CATEGORY_IMPACT` 只用于**自动抓取的新闻流**给一句方向提示，前端明确标注
     "机器初判 · 未研判"；人工研判事件自带方向与个股，**不走这张表**，避免机器口径
     污染精判结论。
"""
from __future__ import annotations

CATEGORIES: list[dict] = [
    {"key": "geopolitics", "label": "地缘军事", "icon": "⚠",
     "desc": "战争与武装冲突、海峡与航道封锁、军事打击与演习、恐怖袭击"},
    {"key": "trade", "label": "关税贸易", "icon": "⇄",
     "desc": "加征与豁免关税、301/232 调查、出口管制、供应链本土化"},
    {"key": "energy", "label": "能源资源", "icon": "⛽",
     "desc": "原油与天然气、稀土与关键矿产、粮食、战略储备与产能"},
    {"key": "finance", "label": "金融货币", "icon": "◎",
     "desc": "货币政策与利率、汇率、主权债务、跨境资本流动"},
    {"key": "tech", "label": "科技产业", "icon": "⌬",
     "desc": "芯片与算力管制、AI 治理与对话、技术突破与产业政策"},
    {"key": "disaster", "label": "灾害公卫", "icon": "☂",
     "desc": "台风地震洪水、疫情与公共卫生事件、重大工业事故"},
    {"key": "politics", "label": "政治外交", "icon": "⚖",
     "desc": "元首会晤、制裁与豁免、条约签署、双边关系升降级、国际组织决议"},
]

CATEGORY_MAP = {c["key"]: c for c in CATEGORIES}

# 关键词 → 类别（顺序敏感：先命中的优先，故把"更具体"的类别放前面）
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("geopolitics", (
        "战争", "冲突", "交火", "空袭", "导弹", "无人机袭击", "停火", "封锁", "海峡",
        "霍尔木兹", "曼德海峡", "红海", "军事", "军演", "恐袭", "袭击", "ceasefire",
        "strike", "war", "conflict", "missile", "drone", "blockade", "strait", "hormuz",
        "military", "attack", "houthi", "invasion",
    )),
    ("trade", (
        "关税", "加征", "贸易战", "301", "232", "出口管制", "反倾销", "贸易休战", "磋商",
        "降税", "自由贸易", "制裁清单", "实体清单", "产地证", "转口", "tariff", "trade war",
        "export control", "sanction", "tariffs", "duty",
    )),
    ("disaster", (
        "台风", "地震", "洪水", "暴雨", "山体滑坡", "海啸", "干旱", "野火", "飓风", "疫情",
        "霍乱", "埃博拉", "病毒", "确诊", "死亡人数", "遇难", "失联", "撤离", "决堤",
        "typhoon", "earthquake", "flood", "landslide", "hurricane", "wildfire", "outbreak",
        "ebola", "cholera", "pandemic",
    )),
    ("energy", (
        "原油", "油价", "布伦特", "天然气", "LNG", "稀土", "镨钕", "锂", "铜", "镍", "煤炭",
        "铁矿", "黄金", "白银", "金价", "粮食", "小麦", "大豆", "产能", "减产", "储备", "opec",
        "crude", "brent", "gas", "lng", "rare earth", "copper", "gold", "wheat", "refinery",
    )),
    ("finance", (
        "美联储", "加息", "降息", "议息", "点阵图", "美债", "国债收益率", "实际利率", "汇率",
        "日元", "美元指数", "央行", "货币政策", "缩表", "fomc", "fed ", "rate hike", "rate cut",
        "treasury yield", "yen", "dollar index",
    )),
    ("tech", (
        "芯片", "半导体", "光刻", "算力", "人工智能", "大模型", "英伟达", "台积电", "出口许可",
        "AI", "chip", "semiconductor", "nvidia", "gpu", "blackwell", "ascend", "superintelligence",
    )),
    ("politics", (
        "元首", "峰会", "会晤", "访问", "决议", "联合国", "制裁", "断交", "建交", "协议",
        "备忘录", "大选", "议会", "总统", "summit", "talks", "visit", "treaty", "un ",
    )),
]

# 兜底影响映射：仅给**自动抓取的新闻**一句机器初判（前端标"机器初判·未研判"）
CATEGORY_IMPACT: dict[str, dict] = {
    "geopolitics": {
        "direction": "分化（利多能源/军工/避险，利空出行与下游成本）",
        "sectors_up": ["石油石化", "军工", "油运", "黄金"],
        "sectors_down": ["航空机场", "旅游酒店", "物流快递", "下游化工"],
        "commodities": ["原油", "黄金"],
    },
    "trade": {
        "direction": "分化（利空外需出口链，利多内需替代与自主可控）",
        "sectors_up": ["国产替代", "内需消费", "农业"],
        "sectors_down": ["出口链", "跨境电商", "消费电子代工"],
        "commodities": [],
    },
    "energy": {
        "direction": "分化（利多上游资源，利空下游制造成本）",
        "sectors_up": ["石油石化", "煤炭", "有色金属", "油运"],
        "sectors_down": ["下游化工", "航空机场", "物流快递"],
        "commodities": ["原油", "煤炭", "有色金属"],
    },
    "finance": {
        "direction": "分化（利多银行息差与红利，利空高估值成长）",
        "sectors_up": ["银行", "红利高股息", "非银金融"],
        "sectors_down": ["高估值成长", "黄金", "地产链"],
        "commodities": ["美元指数", "美债收益率"],
    },
    "tech": {
        "direction": "分化（利多国产算力与设备，利空海外算力依赖方）",
        "sectors_up": ["国产算力", "半导体设备", "AI 应用"],
        "sectors_down": ["海外算力依赖", "消费电子代工"],
        "commodities": [],
    },
    "disaster": {
        "direction": "分化（利多重建与防疫链，利空保险与区域消费）",
        "sectors_up": ["水利建设", "建材", "工程机械", "疫苗"],
        "sectors_down": ["保险", "旅游酒店", "航空机场"],
        "commodities": ["水泥", "钢材"],
    },
    "politics": {
        "direction": "中性偏结构性（看具体条款落到哪个产业链）",
        "sectors_up": ["券商", "黄金"],
        "sectors_down": ["高估值成长"],
        "commodities": [],
    },
}

_UNKNOWN = {
    "direction": "未识别方向（待研判）",
    "sectors_up": [], "sectors_down": [], "commodities": [],
}


def classify_text(text: str) -> tuple[str, list[str]]:
    """按关键词把一段文本归到七类之一，返回 (类别key, 命中词列表)。"""
    low = (text or "").lower()
    for key, words in CATEGORY_KEYWORDS:
        hits = [w for w in words if w.lower() in low]
        if hits:
            return key, hits[:4]
    return "politics", []


def impact_of(cat_key: str) -> dict:
    """取某类别的兜底影响模板（查不到给"未识别"）。"""
    return CATEGORY_IMPACT.get(cat_key) or _UNKNOWN


def label_of(cat_key: str) -> str:
    c = CATEGORY_MAP.get(cat_key)
    return c["label"] if c else "未分类"


def intensity_text(n: int) -> str:
    """强度 1-5 → 文字（前端配红/橙/灰三档着色）。"""
    n = int(n or 1)
    return {1: "一般", 2: "关注", 3: "重要", 4: "重大", 5: "极重大"}.get(n, "一般")
