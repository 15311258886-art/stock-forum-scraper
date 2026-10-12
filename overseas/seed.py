"""结构化示例海外条目。

用途与诚实声明
--------------
- 本机实测 YouTube / Reddit / Google News / Reuters / SCMP 被阻断（需 VPN 或 Key），
  这些渠道的**真实**内容暂不可得；为了让两个模块在「未开 VPN」时也具备完整形态与可核对结构，
  这里提供一批**人工整理的结构化示例条目**。
- 所有条目 `kind="seed"`、`source_name` 以「示例源·」开头，界面会显著标注「示例」，
  **不会**与真实抓取条目混淆；一旦对应渠道激活，真实条目会与示例并存、可逐条区分。
- 条目主题覆盖用户明确要求的：国外疫情舆论 / 国际新闻热点 / 国际形势 → 国内板块冲击。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from overseas.sources import NewsItem


def _t(days_ago: float, hour: int = 9, minute: int = 30) -> str:
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _src(label: str) -> str:
    return f"示例源·{label}"


SEED_NEWS: list[NewsItem] = [
    # ================= 国际形势 / 出口管制（半导体链） =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "US tightens semiconductor export controls on advanced chipmaking tools to China",
             "https://example.local/seed/semi-export-controls",
             "Washington expanded export restrictions on advanced chipmaking equipment, raising supply "
             "uncertainty for Chinese fabs while accelerating domestic substitution demand.",
             _t(0.2, 21, 10), ["US", "HK"]),
    NewsItem(_src("Bloomberg"), _src("Bloomberg"), "GLOBAL", "en", "seed",
             "China chip equipment makers rally as localization orders accelerate",
             "https://example.local/seed/china-semi-localization",
             "Domestic wafer fab equipment suppliers reported rising orders as localization push "
             "intensifies after the latest export controls.",
             _t(0.15, 15, 40), ["US", "HK"]),
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "ASML and Applied Materials slide on China export restriction headlines",
             "https://example.local/seed/asml-amat-slide",
             "Shares of semiconductor equipment makers fell after new export restrictions, while Chinese "
             "peers gained on domestic substitution expectations.",
             _t(0.1, 22, 5), ["US"]),

    # ================= 科技与 AI 资本开支（算力链） =================
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "Nvidia beats expectations, data center revenue surges on AI capex boom",
             "https://example.local/seed/nvda-beat",
             "The company reported record data center revenue and raised guidance, signalling hyperscaler "
             "AI capex remains strong.",
             _t(0.5, 6, 20), ["US", "HK"]),
    NewsItem(_src("Seeking Alpha"), _src("Seeking Alpha"), "US", "en", "seed",
             "AI capex cycle intact: optical module demand upside into 2027",
             "https://example.local/seed/optical-upside",
             "Blog analysis argues 800G and 1.6T optical module demand will beat street estimates as "
             "hyperscalers expand clusters.",
             _t(0.8, 20, 0), ["US", "HK"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Hyperscalers lift 2027 capex guidance; server and liquid cooling supply tight",
             "https://example.local/seed/hyperscaler-capex",
             "Cloud giants lifted capital spending plans, pointing to tight supply in server and liquid "
             "cooling supply chains.",
             _t(1.2, 5, 45), ["US"]),

    # ================= 国外疫情舆论 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "WHO warns new influenza variant spreading faster across northern hemisphere",
             "https://example.local/seed/who-influenza",
             "The WHO flagged a faster-spreading influenza variant, urging surveillance and vaccine "
             "stockpiling ahead of the winter season.",
             _t(0.3, 18, 15), ["US", "HK"]),
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "US CDC reports rising hospitalizations; vaccine makers see demand pick up",
             "https://example.local/seed/cdc-hospitalizations",
             "Hospitalization rates rose, prompting health authorities to expand vaccination campaigns; "
             "vaccine and diagnostic demand increased.",
             _t(0.6, 23, 30), ["US"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Airline and travel stocks slide as pandemic-era concerns resurface",
             "https://example.local/seed/airlines-slide",
             "Travel-related shares weakened amid renewed pandemic concerns, while protective equipment "
             "makers advanced.",
             _t(0.9, 21, 50), ["US", "HK"]),

    # ================= 国际新闻热点：美联储 / 利率 =================
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "Fed signals possible rate cut as inflation cools, dollar slips",
             "https://example.local/seed/fed-rate-cut",
             "Officials hinted at a possible rate cut as inflation cooled, weakening the dollar and "
             "lifting emerging market risk appetite.",
             _t(0.4, 3, 15), ["US", "HK"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Treasury yields fall; gold hits record high on easing expectations",
             "https://example.local/seed/gold-record",
             "Falling yields and a softer dollar pushed gold to a record high, with non-ferrous metals "
             "following higher.",
             _t(0.7, 4, 5), ["US"]),
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Global funds turn net buyers of Chinese equities for third straight week",
             "https://example.local/seed/northbound-return",
             "Cross-border flows shifted positive as investors positioned for easier monetary policy and "
             "a weaker dollar.",
             _t(1.1, 16, 20), ["US", "HK"]),

    # ================= 能源与大宗 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "OPEC+ extends output cuts; Brent crude climbs above recent range",
             "https://example.local/seed/opec-cuts",
             "The group extended voluntary production cuts, tightening supply expectations and lifting "
             "crude benchmarks.",
             _t(0.9, 19, 10), ["US", "HK"]),
    NewsItem(_src("Seeking Alpha"), _src("Seeking Alpha"), "US", "en", "seed",
             "Oilfield services earnings to benefit from higher crude prices",
             "https://example.local/seed/oilfield-earnings",
             "Analysts expect upstream service providers to report stronger orders on sustained crude "
             "price strength.",
             _t(1.4, 12, 0), ["US"]),

    # ================= 关税 / 贸易（新能源出口链） =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "EU and US weigh new tariffs on Chinese electric vehicles and solar exports",
             "https://example.local/seed/ev-tariffs",
             "Policy makers discussed new trade measures on Chinese electric vehicles and solar products, "
             "raising export-chain uncertainty.",
             _t(0.5, 20, 40), ["HK"]),
    NewsItem(_src("Bloomberg"), _src("Bloomberg"), "GLOBAL", "en", "seed",
             "Chinese EV makers accelerate overseas plants to blunt tariff impact",
             "https://example.local/seed/ev-overseas-plants",
             "Manufacturers ramped up overseas assembly plans to mitigate tariff exposure and protect "
             "export margins.",
             _t(0.75, 14, 25), ["HK"]),

    # ================= 港股流动性 =================
    NewsItem(_src("HKEX"), _src("HKEX"), "HK", "en", "seed",
             "HKEX reports stronger southbound turnover and rising new listing applications",
             "https://example.local/seed/hkex-turnover",
             "The exchange reported improved southbound trading turnover and a healthy pipeline of new "
             "listing applications.",
             _t(0.35, 17, 0), ["HK"]),
    NewsItem(_src("AAStocks"), _src("AAStocks"), "HK", "zh", "seed",
             "恒指反弹 港股科技股领涨 南向资金持续净流入",
             "https://example.local/seed/hsi-rebound",
             "港股科技板块走强，恒生科技指数领涨，南向资金连续多日净流入，市场情绪回暖。",
             _t(0.45, 16, 30), ["HK"]),

    # ================= 消费与医药 =================
    NewsItem(_src("Seeking Alpha"), _src("Seeking Alpha"), "US", "en", "seed",
             "Obesity drug demand drives pharma upgrade cycle; supply chain expands",
             "https://example.local/seed/obesity-drug",
             "Surging obesity drug demand lifted pharma guidance and expanded contract manufacturing "
             "capacity plans.",
             _t(1.3, 11, 20), ["US"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Luxury and travel retail soften as consumer spending cools",
             "https://example.local/seed/luxury-cools",
             "High-end retail demand weakened as consumer spending cooled, pressuring luxury and travel "
             "retail shares.",
             _t(1.5, 13, 45), ["US", "HK"]),

    # ================= 供应链与制造 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Red Sea shipping disruptions lift container rates and delay deliveries",
             "https://example.local/seed/red-sea",
             "Renewed shipping disruptions pushed container freight rates higher and delayed deliveries "
             "across Asia-Europe routes.",
             _t(1.0, 8, 55), ["US", "HK"]),

    # ================= 国内交叉比对源（权重 0.45，仅作对照） =================
    NewsItem("eastmoney_guba", "东方财富股吧", "CN", "zh", "domestic_crosscheck",
             "半导体设备板块异动 国产替代逻辑再获资金关注",
             "https://example.local/seed/cn-semi",
             "受海外出口管制消息影响，国内半导体设备与材料板块放量走强，国产替代逻辑持续强化。",
             _t(0.18, 10, 5), ["CN"]),
    NewsItem("eastmoney_guba", "东方财富股吧", "CN", "zh", "domestic_crosscheck",
             "疫苗与检测板块午后拉升 防疫链条情绪升温",
             "https://example.local/seed/cn-vaccine",
             "受境外疫情舆论影响，疫苗、体外诊断与中药板块午后集体拉升，资金短线关注度提升。",
             _t(0.55, 14, 50), ["CN"]),
    NewsItem("xueqiu_hot", "雪球", "CN", "zh", "domestic_crosscheck",
             "券商与黄金齐涨 市场押注美联储降息",
             "https://example.local/seed/cn-broker-gold",
             "美联储降息预期升温，美元与美债收益率回落，券商与黄金板块同步走强。",
             _t(0.42, 15, 10), ["CN"]),
    NewsItem("ths", "同花顺", "CN", "zh", "domestic_crosscheck",
             "算力与光模块走强 海外AI资本开支上修传导",
             "https://example.local/seed/cn-ai-capex",
             "海外科技巨头资本开支持续上修，国内算力、光模块与液冷环节订单能见度提升，相关个股走强。",
             _t(0.6, 11, 35), ["CN"]),
    NewsItem("ths", "同花顺", "CN", "zh", "domestic_crosscheck",
             "新能源车出口链承压 关税扰动引发观望",
             "https://example.local/seed/cn-ev-tariff",
             "受欧美关税扰动影响，新能源车与光伏出口链短期承压，资金观望情绪上升。",
             _t(0.7, 9, 20), ["CN"]),

    # ================= 【重点追踪】地缘冲突与战争 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Middle East conflict escalates as missile strikes threaten key shipping lane",
             "https://example.local/seed/geo-conflict-shipping",
             "Escalating conflict and missile strikes near a major strait raised fears of a shipping "
             "blockade, pushing insurers and tanker operators to reroute vessels.",
             _t(0.25, 20, 30), ["US", "HK"]),
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "Brent crude jumps as geopolitical tension raises oil supply risk",
             "https://example.local/seed/brent-geo-risk",
             "Crude benchmarks climbed as geopolitical tension in an energy-producing region raised "
             "supply disruption risk, lifting energy shares while travel names slipped.",
             _t(0.35, 5, 25), ["US"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Defense stocks rally, airlines slide as war risk premium builds",
             "https://example.local/seed/defense-airlines-war",
             "Defense contractors advanced and airline shares fell as investors priced in a higher "
             "war risk premium and rising jet fuel costs.",
             _t(0.6, 22, 40), ["US", "HK"]),

    # ================= 【重点追踪】洪涝等自然灾害 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Severe flooding forces mass evacuations and disrupts regional supply chains",
             "https://example.local/seed/se-asia-flooding",
             "Record flooding triggered large-scale evacuations, damaged infrastructure and halted "
             "freight movement across the affected region.",
             _t(0.45, 18, 45), ["US", "HK"]),
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Record monsoon floods damage cropland, raising food security concerns",
             "https://example.local/seed/flood-cropland-food",
             "Extensive flooding submerged cropland in a key grain region, prompting warnings that "
             "food security and crop output could be materially affected this season.",
             _t(0.5, 7, 10), ["US", "HK"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Rebuild demand lifts cement and construction machinery orders after floods",
             "https://example.local/seed/flood-rebuild-demand",
             "Analysts expect reconstruction spending to lift orders for cement, waterproofing "
             "materials and construction machinery, while insurers face higher claims.",
             _t(0.8, 13, 15), ["US"]),

    # ================= 【重点追踪】重大科研论文发布 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Landmark study published in Nature demonstrates breakthrough in AI chip material",
             "https://example.local/seed/nature-ai-chip-material",
             "A peer-reviewed study published in Nature reported a breakthrough material that could "
             "sharply improve AI chip efficiency, prompting renewed industrial interest.",
             _t(0.3, 9, 5), ["US", "HK"]),
    NewsItem(_src("CNBC"), _src("CNBC"), "US", "en", "seed",
             "Peer-reviewed study on quantum computing sends related technology shares higher",
             "https://example.local/seed/quantum-peer-review",
             "A landmark study in quantum error correction lifted expectations for commercial "
             "timelines, with technology shares in the field moving higher.",
             _t(0.65, 16, 55), ["US"]),

    # ================= 【重点追踪】粮食歉收与减产 =================
    NewsItem(_src("Reuters"), _src("Reuters"), "GLOBAL", "en", "seed",
             "Drought hits harvest in key exporting region; wheat shortage deepens",
             "https://example.local/seed/drought-wheat-shortage",
             "A prolonged drought cut yields in a major exporting region, deepening the wheat "
             "shortage and pushing global grain supply expectations lower.",
             _t(0.4, 6, 40), ["US", "HK"]),
    NewsItem(_src("Bloomberg"), _src("Bloomberg"), "GLOBAL", "en", "seed",
             "Export curbs on rice after poor harvest lift global food security worries",
             "https://example.local/seed/rice-export-curbs",
             "Authorities moved to restrict rice exports following a poor harvest, intensifying "
             "food security concerns and lifting benchmark grain prices.",
             _t(0.55, 12, 30), ["US", "HK"]),
    NewsItem(_src("MarketWatch"), _src("MarketWatch"), "US", "en", "seed",
             "Grain prices surge on crop failure fears; fertilizer demand seen rising",
             "https://example.local/seed/grain-crop-failure",
             "Benchmark grain prices surged on crop failure concerns, with analysts expecting "
             "higher planting intentions to support fertilizer and agrochemical demand.",
             _t(0.9, 10, 25), ["US"]),

    # ================= 国内交叉比对源（四类重点事件的国内对照） =================
    NewsItem("eastmoney_guba", "东方财富股吧", "CN", "zh", "domestic_crosscheck",
             "石油石化与军工走强 地缘冲突推升油价避险情绪升温",
             "https://example.local/seed/cn-geo-oil",
             "受境外地缘冲突消息影响，油价与避险情绪上行，石油石化、军工与黄金板块走强，航空与旅游板块承压。",
             _t(0.3, 10, 40), ["CN"]),
    NewsItem("xueqiu_hot", "雪球", "CN", "zh", "domestic_crosscheck",
             "水利与建材板块异动 灾后重建预期升温",
             "https://example.local/seed/cn-flood-rebuild",
             "境外洪涝灾害引发基建损毁与重建需求，水利建设、防水建材与工程机械板块受到资金关注。",
             _t(0.5, 14, 15), ["CN"]),
    NewsItem("ths", "同花顺", "CN", "zh", "domestic_crosscheck",
             "种业与农垦板块拉升 全球粮价上行传导",
             "https://example.local/seed/cn-grain-price",
             "境外主产区减产推动全球粮价上行，种业、种植业与农垦板块走强，养殖与食品加工板块因成本上升承压。",
             _t(0.48, 11, 25), ["CN"]),
    NewsItem("eastmoney_guba", "东方财富股吧", "CN", "zh", "domestic_crosscheck",
             "算力与创新药概念活跃 顶刊论文催化技术预期",
             "https://example.local/seed/cn-research-paper",
             "境外顶刊论文发布催化技术路线预期，相关算力与创新药概念股活跃，旧技术路线标的走弱。",
             _t(0.35, 13, 5), ["CN"]),
]
