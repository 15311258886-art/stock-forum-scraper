"""本地 SQLite 数据库：作为查看器的后台数据库。

设计要点：
- 完整保留每次抓取到的「原始页面 HTML」（raw_pages 表），满足“原始文档作为后台数据库一部分完整保留”的要求。
- 结构化存储实盘交易 / 资讯 / 帖子，供分类视图与详情查看使用。
- 派生表（stock_mentions / persons / topics / login_status / meta）用于加速聚合查询。
- 仅存储公开内容；合规清洗在写入前已完成（见 ingest / compliance）。

本文件为纯标准库实现（sqlite3），不依赖第三方包。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 2026-09-22 修复（隔离能力）：库路径改为可被环境变量覆盖。
# 此前 DB_PATH 硬编码，任何写接口测试都只能对着生产库 app.db 跑，正是本次
# 「示例数据覆盖真实数据」事故得以发生的前提。现在回归可
#   VIEWER_DB=%TEMP%\qa_iso\app.db VIEWER_PORT=8799 python -m viewer.server
# 完全隔离，不触碰真库。
DB_PATH = Path(os.environ.get("VIEWER_DB") or (Path(__file__).resolve().parent / "app.db"))

# ---------------------------------------------------------------------------
# 2026-10-15 P0-1：positions 快照**时效闸**。
# 背景：positions 依赖「选手详情页」采集，本机从未配置 player_page_tpl（日志反复
#   「未配置 player_page_tpl，跳过持仓采集」），库里最新一行停在 2021-06-20。
#   不加闸时，5 年前的持仓会被当成「现在还有人在拿」，直接污染「共识榜 / 相关人物 /
#   个股详情持仓」。现规定：只采信 post_time 落在最近 N 天内的快照行，更旧的整批丢弃，
#   共识改由 trades / trades_v2 的新鲜流水兜底驱动（见 consensus_stocks 兜底源）。
# 可用环境变量 VIEWER_POSITIONS_MAX_AGE_DAYS 覆盖；<=0 表示不限（仅供调试/历史回溯）。
POSITIONS_MAX_AGE_DAYS = int(os.environ.get("VIEWER_POSITIONS_MAX_AGE_DAYS") or 90)


def positions_cutoff_date(max_age_days: int | None = None) -> str:
    """返回 positions 快照的时效下限日（YYYY-MM-DD）；空串表示「不限年龄」。"""
    n = POSITIONS_MAX_AGE_DAYS if max_age_days is None else int(max_age_days)
    if n <= 0:
        return ""
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 机器号（资讯/播报号）作者级识别 —— 「共识 / 共同持仓」展示层的兜底过滤
#   （2026-10-10，修 QA FAIL-D：Tier A 里仍混有「平安银行资讯」等机器号）
#
# 与 scraper/bot_filter.py 的 is_bot_author() 【保持同步】：确证 8 名名单（_BOT_NAMES）与
# 昵称正则（_BOT_NICK_PATTERNS）逐条对齐。此处为**只读复刻**（不 import scraper，避免 viewer 反向
# 依赖采集包；scraper 可能不存在于部署环境）。改名单/正则时请两处一起改。
# 例外：_BOT_NAMES_VIEWER_EXTRA 是 viewer 侧**临时兜底**（当前仅「新浪财经客户端」1 个），
# 因其语料已确证是机器号但 bot_filter 正则漏判；建议同步进 scraper/bot_filter.py 后即可删除本例外。
#
# 两道闸，互补：
#   ① DB 侧 axis：quality_filter.py [2b] 会把确证机器号的 axis 置为 'bot' → 直接认。
#      但注意 gap-3a：存量库里不少「XXX资讯」号的 axis 仍是 NULL / 'post'，光靠 axis 不够。
#   ② 昵称规则：与 bot_filter 对齐的兜底（确证 8 名 + XXX资讯|播报|快讯 类后缀正则）。
#
# 诚实原则（与 bot_filter 一致）：宁可漏判（放行可疑真人），不可误杀真人。
#   → **不把 axis='post' 当机器号**：'post' 是「站点级回溯不到人」的轴标记（quality_filter.AXIS），
#     而 Tier A 里的 eastmoney「股友XXXX」「涅槃回本」「比对分析回报率」等都是真人，误杀代价更高。
# ---------------------------------------------------------------------------
_BOT_NAMES = {
    "神光财经", "Sina财经产", "新浪财经客", "新浪财经芝",
    "看财经官博", "汽车营销分", "机工战略", "EC品牌观察",
}
# viewer 侧补充兜底名单（**未**收录于 scraper/bot_filter.py，见下方说明）。
#   「新浪财经客户端」：昵称以「客户端」结尾，不在 bot_filter 的 SUFFIX 正则内；但语料已确证是机器号——
#     36 帖全是【…公告/通稿】，2 笔 trades 均为 HOLD 且原文抄自【瑞银集团…】/【宁德时代：拟推…员工持股】，
#     无任何第一人称买卖。若不放行，默认共识视图仍会残留 1 个机器号（QA FAIL-D 要求「不再出现任何机器号昵称」）。
#     ⚠ 建议把该名同步进 scraper/bot_filter.py 的 BOT_NAMES（scraper 侧由他人维护，本处先兜底）。
_BOT_NAMES_VIEWER_EXTRA = {"新浪财经客户端"}
_BOT_PLATFORM = (r"新浪|Sina|sina|腾讯|网易|搜狐|凤凰|和讯|财联社|证券之星|金融界|"
                 r"同花顺|东方财富|格隆汇|新华|中金在线|每日经济|第一财经|界面|澎湃|"
                 r"平安银行|招商银行|中金|雪球|证券时报|上海证券报|中国证券报")
_BOT_SUFFIX = (r"资讯|播报|快讯|日报|晚报|早报|午报|公告|情报|速递|时报|周刊|"
               r"看盘|解盘|盘点|官博|官微|情报站")
_BOT_BRANDS = r"神光|益盟|大智慧|指南针|雪球财经|同花顺财经|东方财富网"
_BOT_NICK_PATTERNS = (
    re.compile(rf"^(?:{_BOT_PLATFORM}).{{0,8}}(?:{_BOT_SUFFIX}|财经|观察|视点|焦点)$"),
    re.compile(rf"^.{{0,6}}(?:{_BOT_BRANDS}).{{0,6}}$"),
    re.compile(rf"^.{{0,8}}(?:{_BOT_SUFFIX})$"),
)


def is_bot_author(name: str, axis: str | None = None) -> bool:
    """作者是否机器号（资讯/播报号）。

    axis=='bot'（quality_filter [2b] 打的标）直接认；否则回退**昵称规则**
    （与 scraper/bot_filter.py 的 is_bot_author 对齐）。空名不判为机器号。
    """
    if (axis or "").strip().lower() == "bot":
        return True
    n = (name or "").strip()
    if not n:
        return False
    if n in _BOT_NAMES or n in _BOT_NAMES_VIEWER_EXTRA:
        return True
    for rx in _BOT_NICK_PATTERNS:
        if rx.search(n):
            return True
    return False


class Database:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = str(path or DB_PATH)
        # 每个线程持有独立连接（指向同一文件），避免跨线程共享 SQLite 对象报错。
        self._local = threading.local()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
        # WAL：读不阻塞写、写不阻塞读。否则并发请求（ThreadingHTTPServer 多线程）
        # 在长写事务（如载入示例）期间会互抢锁，抛 sqlite3.OperationalError: database is locked。
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        return c

    def get_conn(self, *, read_only: bool = False) -> sqlite3.Connection:
        """连接工厂（P2-30）：默认返回可写连接；read_only=True 以 file:...?mode=ro 打开只读连接。

        只读连接不写 WAL（journal_mode 仅对可写库生效），但仍设 busy_timeout 防并发读锁等待。
        """
        if read_only:
            uri = "file:%s?mode=ro" % self.path
            c = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=15000")
            return c
        return self._connect()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def _init(self):
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                url TEXT,
                fetched_at TEXT,
                http_status INTEGER,
                html TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                author TEXT,
                post_id TEXT,
                post_url TEXT,
                post_time TEXT,
                text TEXT,
                sentiment TEXT,
                sentiment_score REAL,
                raw_page_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                author TEXT,
                post_id TEXT,
                post_url TEXT,
                post_time TEXT,
                action TEXT,
                symbol TEXT,
                symbol_name TEXT,
                price REAL,
                quantity INTEGER,
                position_ratio REAL,
                -- 实盘交易完整明细（详情页核心字段）
                buy_time TEXT,          -- 买入/成交时间（优先于 post_time）
                buy_price REAL,        -- 买入价（成交价）
                current_price REAL,    -- 当前股价（来自行情源，可能为空）
                -- 卖出/减仓明细：与买入分列，避免「卖出价被当成买入价」的语义错位
                sell_time TEXT,         -- 卖出/减仓成交时间
                sell_price REAL,        -- 卖出/减仓成交价
                hold_since TEXT,        -- 持仓起始日期（帖子明确披露时写入；未披露则由后端聚合推断）
                access_level TEXT DEFAULT 'free',  -- free/paid/points/locked
                access_hint TEXT,      -- 受限原因提示（如“需开通会员”）
                source_type TEXT DEFAULT 'bar_post',  -- bar_post=个股讨论页提及；player_page=选手自己主页/实盘页记录的买卖；player_live=登录墙后真实实盘流水(需cookie)
                raw_text TEXT
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                name TEXT UNIQUE,
                channel TEXT DEFAULT 'A',
                rank_position INTEGER,
                ranking_score REAL,
                is_top_player INTEGER DEFAULT 0,
                is_certified INTEGER DEFAULT 0,
                source_url TEXT,
                added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                player TEXT,
                channel TEXT,
                symbol TEXT,
                symbol_name TEXT,
                ratio REAL,
                action TEXT,
                post_time TEXT,
                post_url TEXT,
                -- 持仓完整明细（详情页核心字段）
                buy_time TEXT,          -- 建仓/买入时间
                buy_price REAL,        -- 买入价
                current_price REAL,    -- 当前股价（可能为空）
                -- 卖出/减仓明细 + 持仓起始日期（详见 trades 表同名注释）
                sell_time TEXT,
                sell_price REAL,
                hold_since TEXT,
                access_level TEXT DEFAULT 'free',  -- free/paid/points/locked
                access_hint TEXT,      -- 受限原因提示
                raw_text TEXT
            );
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                title TEXT,
                url TEXT,
                publish_time TEXT,
                summary TEXT,
                source TEXT
            );
            CREATE TABLE IF NOT EXISTS stock_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id_ref INTEGER,
                symbol TEXT,
                symbol_name TEXT,
                context TEXT
            );
            CREATE TABLE IF NOT EXISTS persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                mention_count INTEGER DEFAULT 1,
                role TEXT DEFAULT 'author'
            );
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                weight REAL,
                sample TEXT
            );
            CREATE TABLE IF NOT EXISTS login_status (
                site TEXT PRIMARY KEY,
                status TEXT,
                checked_at TEXT,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            -- 代码 → 名称 缓存表（离线可复用；由 stock_names 解析器写入）
            CREATE TABLE IF NOT EXISTS stock_names (
                code TEXT PRIMARY KEY,
                name TEXT,        -- 股票简称（如 贵州茅台）
                company TEXT,     -- 公司全称（如 贵州茅台酒股份有限公司）
                market TEXT,      -- 上市板块（如 沪市主板）
                source TEXT,      -- 来源：config/builtin/eastmoney/db
                updated_at TEXT
            );
            -- 个股「公司概况」缓存表（2026-09-26 新增）：主营业务 / 公司简介 / 行业。
            -- 来源：东方财富 F10 公司概况（RPT_F10_BASIC_ORGINFO）。变更频率极低（以年计），
            -- 故长期落库、离线可复用，前端「重要信息」板块末尾展示。
            CREATE TABLE IF NOT EXISTS stock_org (
                code TEXT PRIMARY KEY,
                name TEXT,             -- 股票简称
                full_name TEXT,        -- 公司全称
                main_business TEXT,    -- 主营业务（东财原文，逗号分隔短语）
                profile TEXT,          -- 公司简介
                business_scope TEXT,   -- 经营范围
                industry TEXT,         -- 东财行业（如 金融-非银行金融-证券）
                industry_csrc TEXT,    -- 证监会行业（如 金融业-资本市场服务）
                found_date TEXT,
                list_date TEXT,        -- 上市日期
                reg_capital REAL,      -- 注册资本（万元）
                emp_num INTEGER,       -- 员工人数
                web TEXT,              -- 公司官网
                source TEXT,
                updated_at TEXT
            );
            -- ============ 海外舆情（模块一 / 模块二） ============
            -- 海外新闻 / 观点原始条目（国外媒体、官方披露、意见领袖、博客评论、国内交叉比对源）
            CREATE TABLE IF NOT EXISTS overseas_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT,
                source_name TEXT,
                region TEXT,            -- US / HK / GLOBAL / CN
                lang TEXT,
                kind TEXT,              -- media/blog/official/influencer/forum/aggregator/domestic_crosscheck
                title TEXT,
                url TEXT,
                summary TEXT,
                publish_time TEXT,
                topic TEXT,             -- 主驱动主题（疫情/国际形势/科技与AI…）
                topics TEXT,            -- 全部命中主题（逗号分隔）
                sentiment TEXT,         -- 看多/看空/中性
                sentiment_score REAL,
                heat REAL DEFAULT 0,    -- 热度 = 情绪强度 + 主题命中 + 区域权重
                entities TEXT,          -- JSON: [{market,symbol,name}]
                sectors TEXT,           -- 逗号分隔：关联国内板块
                fetched_at TEXT,
                UNIQUE(url, title)
            );
            -- 海外新闻 → 标的提及
            CREATE TABLE IF NOT EXISTS overseas_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id_ref INTEGER,
                market TEXT,            -- US / HK / CN
                symbol TEXT,
                name TEXT
            );
            -- 按日 × 区域的海外情绪时间序列（供趋势图）
            CREATE TABLE IF NOT EXISTS overseas_sentiment_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT,
                region TEXT,
                bull INTEGER DEFAULT 0,
                bear INTEGER DEFAULT 0,
                neutral INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                heat REAL DEFAULT 0,
                mood REAL DEFAULT 0,    -- 情绪指数 [-100,100]
                UNIQUE(day, region)
            );
            -- 近期热点（国外新闻驱动）+ 提升性影响分析
            CREATE TABLE IF NOT EXISTS overseas_hotspots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                title TEXT,
                topic TEXT,
                overview TEXT,          -- 事件概述（发生了什么）
                driver_sources TEXT,    -- 驱动它的国外源（逗号分隔）
                driver_regions TEXT,
                direction TEXT,         -- 利多/利空/分化
                related_markets TEXT,   -- US,HK
                related_symbols TEXT,   -- 代码→名称（逗号分隔）
                domestic_sectors TEXT,  -- 关联国内板块
                variables TEXT,         -- 关键传导变量（逗号分隔：油价↑ / 粮价↑ / 航运费↑）
                sectors_up TEXT,        -- 预判上涨板块（逗号分隔）
                sectors_down TEXT,      -- 预判下跌板块（逗号分隔）
                sectors_up_detail TEXT,   -- 上涨领域→代表公司，JSON[{"sector","symbols":[{"code","name"}]}]
                sectors_down_detail TEXT, -- 下跌领域→代表公司，JSON 同上结构
                decision TEXT,          -- 决策导向（一句话结论：做多/规避哪些领域 + 确定性/放大系数）
                transmission TEXT,      -- 传导路径
                amplification TEXT,     -- 提升性（放大/衰减）分析
                cross_check TEXT,       -- 与国内源的交叉比对结论
                amplification_factor REAL DEFAULT 1.0,
                confidence REAL DEFAULT 0.5,
                heat REAL DEFAULT 0,
                evidence TEXT,          -- JSON: 证据条目
                first_seen TEXT,
                updated_at TEXT
            );
            -- 数据源接入状态（界面如实展示「已接入 / 待激活」）
            CREATE TABLE IF NOT EXISTS overseas_sources_status (
                source_key TEXT PRIMARY KEY,
                name TEXT,
                region TEXT,
                status TEXT,
                ok INTEGER DEFAULT 0,
                count INTEGER DEFAULT 0,
                error TEXT,
                latency REAL,
                requires_key INTEGER DEFAULT 0,
                requires_vpn INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 0,   -- 配置里的启用开关（0/1），界面据此区分「已关闭」与「待激活」
                note TEXT,
                checked_at TEXT
            );
            -- ============ 刷新状态（实时刷新 / 「最后更新」列） ============
            -- 每次「抓取并更新」按来源逐条落一行，记录该来源最近一次**成功**刷新的时刻。
            -- 前端「最后更新」列据此判断某条数据是否在规定时间内完成更新（超时则高亮告警）；
            -- source_key="__all__" 为整轮抓取汇总（有一源失败即记为 error）。
            CREATE TABLE IF NOT EXISTS refresh_status (
                source_key TEXT PRIMARY KEY,   -- taoguba/eastmoney/xueqiu/ths + __all__
                display_name TEXT,             -- 中文显示名（淘股吧/东方财富/…）
                last_ok_at TEXT,               -- 最近一次成功刷新完成时刻（失败不改动它）
                last_attempt_at TEXT,          -- 最近一次尝试时刻（成功/失败都写）
                status TEXT,                   -- ok / error / never
                item_count INTEGER DEFAULT 0,  -- 本次处理条目数（仅供参考）
                error TEXT
            );
            -- ============ 学习知识库（「跟我学习 → 中短线炒股」） ============
            -- 知识库是**只读、整篇替换**的文档型数据：读者永远「一次取一份文档」，
            -- 故结构化结果整体存成 payload(JSON)，不拆规范化表（拆表只增维护成本）。
            -- 重吸取幂等靠 content_hash：没变就跳过，上游每天覆盖写也不会重复重建。
            CREATE TABLE IF NOT EXISTS kb_doc (
                doc_key TEXT PRIMARY KEY,   -- zdx_stock（中短线炒股）
                track TEXT,                 -- 所属学习轨道（横向子板块名）
                title TEXT,
                version TEXT,               -- 从正文标题解析，如 v5.0
                source_path TEXT,           -- 本地 md 文件绝对路径
                source_mtime TEXT,          -- 上游文件修改时刻
                content_hash TEXT,          -- 文件内容 sha256（判「要不要重吸取」）
                char_count INTEGER DEFAULT 0,
                node_count INTEGER DEFAULT 0,
                status TEXT,                -- ok / error
                error TEXT,
                payload TEXT,               -- 完整结构化 JSON
                prev_hash TEXT,             -- 上一次的 content_hash
                prev_ingested_at TEXT,      -- 上一次吸取时刻
                ingested_at TEXT,           -- 本次吸取时刻
                changed INTEGER DEFAULT 0,  -- 本次是否真的发生了内容变化
                parser_version INTEGER DEFAULT 0,  -- 解析器版本（改了 kb.py 的解析逻辑必须变）
                reload_count INTEGER DEFAULT 0
            );
            """
        )
        c.commit()
        # 聚合查询加速索引
        c.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_posts_author ON posts(author);
            CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS ix_trades_author ON trades(author);
            CREATE INDEX IF NOT EXISTS ix_positions_symbol ON positions(symbol);
            CREATE INDEX IF NOT EXISTS ix_positions_player ON positions(player);
            CREATE INDEX IF NOT EXISTS ix_stock_mentions_symbol ON stock_mentions(symbol);
            CREATE INDEX IF NOT EXISTS ix_persons_name ON persons(name);
            CREATE INDEX IF NOT EXISTS ix_ov_news_region ON overseas_news(region);
            CREATE INDEX IF NOT EXISTS ix_ov_news_topic ON overseas_news(topic);
            CREATE INDEX IF NOT EXISTS ix_ov_news_time ON overseas_news(publish_time);
            CREATE INDEX IF NOT EXISTS ix_ov_mentions_symbol ON overseas_mentions(symbol);
            CREATE INDEX IF NOT EXISTS ix_ov_daily_day ON overseas_sentiment_daily(day);
            """
        )
        c.commit()
        # 兼容旧库：为 trades 表补齐 position_ratio 列
        cols = [r["name"] for r in c.execute("PRAGMA table_info(trades)").fetchall()]
        if "position_ratio" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN position_ratio REAL")
            c.commit()
        # 兼容旧库：为 trades / positions 补齐实盘明细字段
        for tbl in ("trades", "positions"):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
            for col, ctype in (
                ("buy_time", "TEXT"), ("buy_price", "REAL"), ("current_price", "REAL"),
                # 卖出/减仓与持仓起始日期：此前卖出记录只能挤在 buy_time/buy_price 里，
                # 导致「卖出价」被前端当成「买入价」展示，故单列字段区分。
                ("sell_time", "TEXT"), ("sell_price", "REAL"), ("hold_since", "TEXT"),
                ("access_level", "TEXT"), ("access_hint", "TEXT"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ctype}")
            c.commit()
        # 兼容旧库：overseas_hotspots 补齐「事件概述 / 关键传导变量 / 涨跌板块预判」列
        # 说明：为支持用户对地缘冲突、自然灾害、科研论文、粮食歉收四类事件的
        #       结构化输出（事件概述 → 关键传导变量 → 板块涨跌预判）而新增。
        hcols = [r["name"] for r in c.execute("PRAGMA table_info(overseas_hotspots)").fetchall()]
        for col, ctype in (
            ("overview", "TEXT"),       # 事件概述
            ("variables", "TEXT"),      # 关键传导变量（油价↑ / 粮价↑ …）
            ("sectors_up", "TEXT"),     # 预判上涨板块
            ("sectors_down", "TEXT"),   # 预判下跌板块
            ("sectors_up_detail", "TEXT"),    # 上涨领域→代表公司（JSON）
            ("sectors_down_detail", "TEXT"),  # 下跌领域→代表公司（JSON）
            ("decision", "TEXT"),             # 决策导向结论
        ):
            if col not in hcols:
                c.execute(f"ALTER TABLE overseas_hotspots ADD COLUMN {col} {ctype}")
        c.commit()
        # 兼容旧库：players 表补齐 is_certified 列
        pcols = [r["name"] for r in c.execute("PRAGMA table_info(players)").fetchall()]
        if "is_certified" not in pcols:
            c.execute("ALTER TABLE players ADD COLUMN is_certified INTEGER DEFAULT 0")
            c.commit()
        # 兼容旧库：overseas_sources_status 补齐 enabled 列
        # 说明：原表没有该列，界面无从区分「配置里已关闭」与「待激活（需 VPN）」，
        #       实测 8 个 enabled:false 的源被显示成「待激活」，属如实性问题。
        scols = [r["name"] for r in c.execute("PRAGMA table_info(overseas_sources_status)").fetchall()]
        if "enabled" not in scols:
            c.execute("ALTER TABLE overseas_sources_status ADD COLUMN enabled INTEGER DEFAULT 0")
            c.commit()
        # 兼容旧库：players 表补齐「层级 / 权重 / 影响力 / 是否露出 / 平台」列
        # 说明：在原有 A/B/C/D 之上新增 E、F 层级。低层级权重降低，但 is_visible=1
        #       一律露出（不因热度低而隐藏），保证多层级全覆盖。
        for col, ctype, dflt in (
            ("tier", "TEXT", "'A'"),            # 层级 A~F
            ("tier_weight", "REAL", "1.0"),     # 层级权重（递减）
            ("influence", "REAL", "0"),         # 影响力（粉丝/播放量等估分）
            ("is_visible", "INTEGER", "1"),     # 是否露出（1=露出，永不隐藏）
            ("platform", "TEXT", "''"),         # 来源平台：taoguba/eastmoney/xueqiu/ths
            ("followers", "INTEGER", "0"),      # 粉丝/关注数（影响力证据）
        ):
            if col not in pcols:
                c.execute(f"ALTER TABLE players ADD COLUMN {col} {ctype} DEFAULT {dflt}")
        c.commit()
        # 兼容旧库：kb_doc 表补齐 parser_version 列。
        # 说明：重吸取原本只比「来源文件 content_hash」——可这漏掉了一类真实变更：
        #       **解析器自己改了**（例如修好某个被静默丢弃的排版分支）。此时上游文件
        #       一个字节没动，哈希不变，旧库会一直跳过重建、把修复好的成果挡在门外。
        #       故引入 parser_version：kb.py 一旦改动解析逻辑就必须 +1，kb_save 会比较
        #       它与库中存值，不等即强制重建（即使 content_hash 一致）。
        kcols = [r["name"] for r in c.execute("PRAGMA table_info(kb_doc)").fetchall()]
        if kcols and "parser_version" not in kcols:
            c.execute("ALTER TABLE kb_doc ADD COLUMN parser_version INTEGER DEFAULT 0")
            c.commit()
        # 首次启用刷新状态（Request G）：refresh_status 为空时，用**真实已记录的抓取时刻**回填，
        # 避免「最后更新」列在功能刚上线时整列空白。回填依据：
        #   · 逐来源：raw_pages.fetched_at 的最大值（该来源最近一次真实抓取时刻）；
        #   · 整轮  ：meta.last_fetch_at。
        # 关键约束：只对有 raw_pages 真实抓取记录的来源回填——**绝不为无抓取记录的来源
        # 编造时刻**（那些来源在前端会如实显示为「未纳入刷新」）。display_name 留空，
        # 由 server.api_refresh_status 统一补中文名，避免在此重复维护一份名称表。
        if c.execute("SELECT COUNT(*) AS n FROM refresh_status").fetchone()["n"] == 0:
            for row in c.execute(
                "SELECT site, MAX(fetched_at) AS mx FROM raw_pages "
                "WHERE site IS NOT NULL AND site<>'' AND fetched_at IS NOT NULL "
                "GROUP BY site"
            ).fetchall():
                c.execute(
                    """INSERT INTO refresh_status
                           (source_key, display_name, last_ok_at, last_attempt_at, status, item_count, error)
                       VALUES (?,?,?,?,?,?,?)""",
                    (row["site"], "", row["mx"], row["mx"], "ok", 0, ""),
                )
            lf = c.execute("SELECT value FROM meta WHERE key='last_fetch_at'").fetchone()
            if lf and lf["value"]:
                c.execute(
                    """INSERT INTO refresh_status
                           (source_key, display_name, last_ok_at, last_attempt_at, status, item_count, error)
                       VALUES (?,?,?,?,?,?,?)""",
                    ("__all__", "", lf["value"], lf["value"], "ok", 0, ""),
                )
            c.commit()

        # 兼容旧库：披露时点 disclose_at（P1-09）
        # 说明：stock_holders / stock_ann / lhb_stocks 三张表的「披露时点」原先没落库，
        #       前端无法诚实区分「盘后披露 vs T+1 盘前可用」，容易把旧披露当成新数据。
        #       这里统一补列，并对存量空值回填（公告/股东取 notice_date，龙虎榜取 trade_date，
        #       统一标 " 15:00:00" 口径——数据于披露日盘后 / 次交易日口径可用）。
        for _t, _src in (("stock_holders", "notice_date"), ("stock_ann", "notice_date"),
                         ("lhb_stocks", "trade_date")):
            try:
                _cols = [r["name"] for r in c.execute(f"PRAGMA table_info({_t})").fetchall()]
            except sqlite3.OperationalError:
                continue  # 表可能尚未由对应采集脚本建出，跳过不阻塞迁移
            if "disclose_at" not in _cols:
                c.execute(f"ALTER TABLE {_t} ADD COLUMN disclose_at TEXT DEFAULT ''")
                c.commit()
            # 存量空值回填（幂等：只补 disclose_at 为空的行，且源字段非空）
            c.execute(
                f"UPDATE {_t} SET disclose_at = {_src} || ' 15:00:00' "
                f"WHERE (disclose_at IS NULL OR disclose_at = '') "
                f"AND {_src} IS NOT NULL AND {_src} <> ''"
            )
            c.commit()

    # ---------------- 实盘选手（含渠道标注） ----------------
    def save_player(self, site, name, channel="A", rank_position=None,
                    ranking_score=None, is_top_player=0, is_certified=0, source_url="",
                    tier=None, tier_weight=None, influence=None, is_visible=1,
                    platform="", followers=None):
        """写入选手。tier/tier_weight/influence/is_visible/platform/followers 为层级体系字段。

        未显式传入 tier 时，按 channel 兜底（保持旧行为不变）；compute 由调用方负责，
        本函数只落库，保证「评分标准与 ABCD 规则」不被改动。
        """
        from scraper.players import TIER_WEIGHTS  # 局部导入避免循环依赖
        t = (tier or channel or "A").upper()
        tw = float(tier_weight) if tier_weight is not None else TIER_WEIGHTS.get(t, 1.0)
        self.conn.execute(
            """INSERT INTO players (site, name, channel, rank_position, ranking_score, is_top_player,
                                    is_certified, source_url, added_at, tier, tier_weight, influence,
                                    is_visible, platform, followers)
               VALUES (:site,:name,:channel,:rank_position,:ranking_score,:is_top_player,
                       :is_certified,:source_url,:added_at,:tier,:tier_weight,:influence,
                       :is_visible,:platform,:followers)
               ON CONFLICT(name) DO UPDATE SET
                 channel=excluded.channel, rank_position=excluded.rank_position,
                 ranking_score=excluded.ranking_score, is_top_player=excluded.is_top_player,
                 is_certified=excluded.is_certified, source_url=excluded.source_url, added_at=excluded.added_at,
                 tier=excluded.tier, tier_weight=excluded.tier_weight, influence=excluded.influence,
                 is_visible=excluded.is_visible, platform=excluded.platform, followers=excluded.followers""",
            dict(site=site, name=name, channel=channel, rank_position=rank_position,
                 ranking_score=ranking_score, is_top_player=is_top_player,
                 is_certified=int(bool(is_certified)), source_url=source_url, added_at=_now(),
                 tier=t, tier_weight=tw, influence=float(influence or 0),
                 is_visible=int(bool(is_visible)), platform=platform or site,
                 followers=int(followers or 0)),
        )
        self.conn.commit()

    def get_players(self, top_only: bool = False, channel: str | None = None) -> list[dict]:
        sql = "SELECT * FROM players"
        where = []
        params = []
        if top_only:
            where.append("is_top_player=1")
        if channel:
            where.append("channel=?")
            params.append(channel)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rank_position ASC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def set_player_channel(self, name, channel):
        self.conn.execute("UPDATE players SET channel=? WHERE name=?", (channel, name))
        self.conn.commit()

    def set_player_top(self, name, value: int):
        self.conn.execute("UPDATE players SET is_top_player=? WHERE name=?", (int(bool(value)), name))
        self.conn.commit()

    # ---------------- 选手详情（富化：交易完整明细 + 近N日操作 + 受限项） ----------------
    @staticmethod
    def _parse_dt(s) -> Optional[datetime]:
        if not s:
            return None
        s = str(s).strip()
        # 支持「纯日期」精度：持仓起始日期常只精确到日（如 2026-07-18），
        # 若只认带时刻的格式，持有天数会算不出（此前 hold_days 恒为 None 即此因）。
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def player_detail(self, name: str, recent_days: int = 5) -> Optional[dict]:
        """实盘选手详情：渠道/名次/评分 + 持仓完整明细（买入时间/价/当前价/权限）
        + 全部交易 + 近 N 日操作记录 + 受限项汇总。供详情页渲染，轻量（不含原始页面）。"""
        row = self.conn.execute("SELECT * FROM players WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        info = dict(row)

        pos_rows = self.conn.execute(
            """SELECT site, player, channel, symbol, symbol_name, ratio, action, post_time, post_url,
                      buy_time, buy_price, current_price, sell_time, sell_price, hold_since,
                      access_level, access_hint
               FROM positions WHERE player=? ORDER BY symbol""",
            (name,),
        ).fetchall()
        positions = [dict(r) for r in pos_rows]

        tr_rows = self.conn.execute(
            """SELECT id, site, author, post_id, post_url, post_time, action, symbol, symbol_name,
                      price, quantity, position_ratio, buy_time, buy_price, current_price,
                      sell_time, sell_price, hold_since, access_level, access_hint, raw_text
               FROM trades WHERE author=? ORDER BY post_time DESC""",
            (name,),
        ).fetchall()
        trades = [dict(r) for r in tr_rows]

        # ---------- 持仓起始日期（hold_since）+ 买入/卖出分区 ----------
        # 口径：① 帖子明确披露「X 月建仓」时以其为准（显式值优先）；
        #      ② 否则取该标的历次建仓记录中最早的时间（含 positions 与 trades 的买入流水）。
        # 这样同一标的多次加仓时，起始日期仍是最早那次，而不是最近一次买入日。
        explicit_since: dict[str, str] = {}
        inferred: dict[str, str] = {}

        def _norm_action(a) -> str:
            return (a or "").strip().upper()

        def _fill(sym, ts):
            sym, ts = (sym or "").strip(), (ts or "").strip()
            if sym and ts and (not inferred.get(sym) or ts < inferred[sym]):
                inferred[sym] = ts

        for it in positions:
            if (it.get("hold_since") or "").strip():
                explicit_since.setdefault(it["symbol"], it["hold_since"].strip())
            _fill(it.get("symbol"), it.get("buy_time") or it.get("post_time"))
        for it in trades:
            if (it.get("hold_since") or "").strip():
                explicit_since.setdefault(it["symbol"], it["hold_since"].strip())
            if _norm_action(it.get("action")) in ("BUY", "HOLD"):
                _fill(it.get("symbol"), it.get("buy_time") or it.get("post_time"))
        since_map = {**inferred, **explicit_since}

        # 建仓成本价：首次买入价（用于计算卖出/减仓的了结盈亏）
        cost_map: dict[str, float] = {}
        for it in positions + trades:
            if _norm_action(it.get("action")) not in ("BUY", "HOLD"):
                continue
            price = it.get("buy_price") if it.get("buy_price") is not None else it.get("price")
            sym = (it.get("symbol") or "").strip()
            if sym and price is not None and sym not in cost_map:
                try:
                    cost_map[sym] = float(price)
                except (TypeError, ValueError):
                    pass

        def _end_ts(it) -> str:
            """该笔记录的结束/成交时点：卖出优先，其次买入，最后发帖时间。"""
            return (it.get("sell_time") or it.get("buy_time") or it.get("post_time") or "").strip()

        def _enrich(it: dict, is_sell: bool) -> dict:
            sym = (it.get("symbol") or "").strip()
            hs = since_map.get(sym) or ""
            it["hold_since"] = hs
            # 持有天数：卖出记录算到卖出日（区间长度），持仓记录算到今日（已持有）
            end = _end_ts(it) if is_sell else datetime.now().strftime("%Y-%m-%d %H:%M")
            d0, d1 = self._parse_dt(hs), self._parse_dt(end)
            it["hold_days"] = max((d1 - d0).days, 0) if (d0 and d1) else None
            # 2026-09-22 修复（A 点）：显式标注每条 hold_days 的口径，消解「同一 symbol 在
            # /api/player 与 /api/stock 两处天数不同」的困惑——此处是**流水级**口径：
            #   closed = 已了结，hold_days 是该笔交易的**实际持有区间长度**（卖出日 - 建仓日）
            #   open   = 仍在持，hold_days 是**已持有至今**（今日 - 建仓日）
            # 数值口径保持不动（若强行统一成"到今日"，已卖出的仓位会显示"已持有 59 天"，
            # 既非持仓状态也非持有期长度，反而制造新的误导）。前端据此文案区分。
            it["hold_days_mode"] = "closed" if is_sell else "open"
            return it

        holdings: list[dict] = []
        sells: list[dict] = []
        for it in positions:
            act = _norm_action(it.get("action"))
            if act == "SELL":
                # 兼容旧数据：卖出时间/价此前只能挤在 buy_time/buy_price 里
                if not (it.get("sell_time") or "").strip():
                    it["sell_time"] = it.get("post_time") or it.get("buy_time") or ""
                if it.get("sell_price") is None:
                    it["sell_price"] = it.get("buy_price")
                ratio = it.get("ratio")
                if ratio is None:
                    it["close_type"] = "卖出"
                else:
                    it["close_type"] = "清仓" if float(ratio) >= 100 else "减仓"
                cost = cost_map.get((it.get("symbol") or "").strip())
                it["cost_price"] = cost
                sp = it.get("sell_price")
                it["pnl"] = (float(sp) - cost) if (sp is not None and cost is not None) else None
                sells.append(_enrich(it, is_sell=True))
            else:
                holdings.append(_enrich(it, is_sell=False))

        # 已被清仓的标的：持仓分区中保留历史但明确标注，避免"卖出后仍显示为在持"
        sold_out = sorted({s["symbol"] for s in sells if (s.get("close_type") == "清仓") and s.get("symbol")})
        for h in holdings:
            h["closed"] = h.get("symbol") in sold_out
        for t in trades:
            _enrich(t, is_sell=_norm_action(t.get("action")) == "SELL")
            if _norm_action(t.get("action")) == "SELL":
                if not (t.get("sell_time") or "").strip():
                    t["sell_time"] = t.get("post_time") or t.get("buy_time") or ""
                if t.get("sell_price") is None:
                    t["sell_price"] = t.get("buy_price") if t.get("buy_price") is not None else t.get("price")

        # 近 N 日操作：以买入/成交时间优先，缺省回退到发帖时间
        cutoff = datetime.now() - timedelta(days=recent_days)
        recent = []
        for t in trades:
            dt = self._parse_dt(t.get("buy_time") or t.get("post_time"))
            if dt and dt >= cutoff:
                recent.append(t)
        recent.sort(key=lambda x: self._parse_dt(x.get("buy_time") or x.get("post_time")) or datetime.min,
                    reverse=True)

        # 受限项汇总（付费/积分/无权限），供详情页顶部提示
        access_items = []
        for grp, label in ((positions, "持仓"), (trades, "交易")):
            for it in grp:
                lvl = (it.get("access_level") or "free").lower()
                if lvl and lvl != "free":
                    access_items.append({
                        "kind": label,
                        "symbol": it.get("symbol") or "",
                        "symbol_name": it.get("symbol_name") or "",
                        "level": lvl,
                        "hint": it.get("access_hint") or self._default_hint(lvl),
                    })
        # 去重（同代码同权限保留一条）
        seen = set()
        unique_access = []
        for a in access_items:
            key = (a["kind"], a["symbol"], a["level"])
            if key in seen:
                continue
            seen.add(key)
            unique_access.append(a)

        return {
            "info": info,
            # 2026-09-22 修复（A 点）：显式声明本视图 hold_days 的基准口径。
            # player_first_build = 建仓起点取自「该选手对该股」最早的一条建仓记录；
            # 与 /api/stock 的 symbol_first_build（全市场最早建仓）不同，故两处天数
            # 可能不一致 —— 这是口径差异而非数据错误，前端需据此展示说明。
            "hold_days_basis": "player_first_build",
            "positions": positions,          # 全量（含卖出，兼容既有调用方）
            "holdings": holdings,            # 分区分区①：买入/持有（含持仓起始日期）
            "sells": sells,                  # 分区分区②：卖出/减仓（含卖出时间、价、了结盈亏）
            "trades": trades,
            "recent": recent,
            "recent_days": recent_days,
            "access_items": unique_access,
            "position_count": len(positions),
            "holding_count": len(holdings),
            "sell_count": len(sells),
            "sold_out": sold_out,            # 已清仓标的（供界面提示）
            "trade_count": len(trades),
            "recent_count": len(recent),
            "access_count": len(unique_access),
        }

    @staticmethod
    def _default_hint(level: str) -> str:
        return {
            "paid": "该内容需付费解锁，充值后可查看完整买卖明细",
            "points": "该内容需消耗积分，积分足够后可查看完整买卖明细",
            "locked": "无权限查看，需相应会员/权限后方可查看",
        }.get(level, "需相应权限方可查看完整明细")

    def clear_players(self):
        self.conn.execute("DELETE FROM players")
        self.conn.commit()

    # ---------------- 名称回填（防复发，2026-09-25） ----------------
    def _ensure_symbol_name(self, kw: dict) -> None:
        """symbol 有值而 symbol_name 为空时，用股票名称缓存回填。

        动机：trades 的唯一索引包含 symbol_name，而 NULL 在 SQLite 唯一索引中
        互不冲突，导致同一帖子的同一笔交易被重复写入（实测 103 行中有 35 行重复，
        另有 46 行缺名称）。补齐名称后唯一约束才能真正生效，同时前端不再出现
        「有代码没名字」的记录。positions 同理。
        """
        sym = (kw.get("symbol") or "").strip()
        if not sym or (kw.get("symbol_name") or "").strip():
            return
        cache = getattr(self, "_sn_name_cache", None)
        if cache is None:
            try:
                cache = {c: (v.get("name") or "") for c, v in self.stock_name_map().items()}
            except Exception:  # noqa: BLE001 —— 缓存表不可用时静默跳过，不阻断写入
                cache = {}
            self._sn_name_cache = cache
        kw["symbol_name"] = cache.get(sym, "")

    # ---------------- 持仓比例 ----------------
    def save_position(self, **kw):
        kw.setdefault("post_time", "")
        for k in ("sell_time", "sell_price", "hold_since"):
            kw.setdefault(k, "" if k != "sell_price" else None)
        self._ensure_symbol_name(kw)
        self.conn.execute(
            """INSERT INTO positions (site, player, channel, symbol, symbol_name, ratio, action, post_time, post_url,
                                    buy_time, buy_price, current_price,
                                    sell_time, sell_price, hold_since,
                                    access_level, access_hint, raw_text)
               VALUES (:site,:player,:channel,:symbol,:symbol_name,:ratio,:action,:post_time,:post_url,
                       :buy_time,:buy_price,:current_price,
                       :sell_time,:sell_price,:hold_since,
                       :access_level,:access_hint,:raw_text)""",
            kw,
        )
        self.conn.commit()

    def clear_positions(self):
        self.conn.execute("DELETE FROM positions")
        self.conn.commit()

    # ---------------- 多人共同持仓共识汇总 ----------------
    # 信号分级（2026-10-10 口径改革，王总批准）—— 每位选手对每只股票只保留一个「最强信号」：
    #   BUY   : 该(作者,标的)在 trades 中**最新一笔**动作为 BUY   → 真正的「喊买入 / 看多」
    #   HOLD  : 该(作者,标的)在 trades 中最新一笔动作为 HOLD     → 明确「喊持有 / 继续拿」
    #   WATCH : positions 持仓/关注快照（关注/操作、社区晒盘等）   → 持仓观察，非明确喊买
    #   SELL  : 明确卖出，不作为任何「持有/买入」信号计数
    # 优先级 BUY > HOLD > WATCH；同级取时间更新的一条。
    _CONSENSUS_SIGNAL_RANK = {"BUY": 3, "HOLD": 2, "WATCH": 1}

    @staticmethod
    def _signal_of(action) -> str:
        """把原始 action 归一为 BUY / HOLD / SELL / WATCH 四类信号。"""
        a = (action or "").strip().upper()
        if a == "BUY":
            return "BUY"
        if a == "HOLD":
            return "HOLD"
        if a == "SELL":
            return "SELL"
        return "WATCH"

    def consensus_stocks(self, min_holders: int = 2,
                         only_qualified: bool = False,
                         max_age_days: int | None = None) -> list[dict]:
        """各股被多少人（跨渠道/跨平台/跨层级）共同持仓/关注，按总人数降序。

        2026-10-10 口径改革（王总批准）—— **把「喊买入(BUY)」与「喊持有(HOLD)/持仓快照(WATCH)」拆开计数**。
        旧实现把 HOLD 与 BUY 混成单一 holders，令「看多人数」被「持有/关注」信号虚增：实测
        中国平安 holders=39 = BUY24+HOLD15；平安银行 holders=31 = BUY14+HOLD17（HOLD 比 BUY 还多），
        却被排进「看多共识」前四。现改为——每位选手对每只股票**只保留一个最强信号**
        （优先级 BUY > HOLD > WATCH，同级取最新），并分别返回三类人数：
            · buy_holders   真正「喊买入」的人数（「看多共识」应看这个）
            · hold_holders  明确「喊持有」的人数
            · watch_holders positions 持仓/关注快照人数
            · holders       三类去重后的**总**人数（向后兼容，排序仍按它）
        另返回层级加权的总/买入共识度 weighted_holders / buy_weighted。
        players 明细每项新增 signal 字段（BUY|HOLD|WATCH）。

        only_qualified=True 时只统计 **players.tier == 'A'** 的入围选手
        （B/C/D/E/F/社区晒盘 及其它未登记选手全部剔除）。

        【2026-10-10 FAIL-D】机器号（资讯/播报号）**无论开关与否都剔除**：判定见 `is_bot_author()`
        （axis=='bot' 或 昵称命中 bot_filter 名单/正则）。此前 Tier A 里仍混有「平安银行资讯」
        等号，令「入围=真人」不成立。

        参数：
            min_holders: 总人数下限。
            only_qualified: 是否仅统计入围选手（Tier A）。
        """
        _RANK = self._CONSENSUS_SIGNAL_RANK

        # 主源：positions 持仓/关注快照（有仓位占比；action 多为「关注/操作」）。
        # 2026-10-15 P0-1：加时效闸——只采信最近 max_age_days 天内的快照行。
        #   本机 positions 全表 271 行（267 行 post_time 为空 + 4 行停在 2021-06-20），
        #   不加闸会把 5 年前 / 无时间戳的持仓当成「现在还有人拿着」→ 共识榜 / 相关人物 /
        #   个股持仓全部失真。过滤后 positions 无近期行 → rows 只剩下方 trades 新鲜流水兜底源
        #   （这正是「彻底弃用陈旧快照、改由新鲜流水驱动」的期望结果）。
        _cut = positions_cutoff_date(max_age_days)
        rows = list(self.conn.execute(
            """SELECT p.symbol, p.symbol_name, p.player, p.channel, p.post_url, p.action,
                      p.ratio, p.site, p.post_time,
                      COALESCE(pl.tier, p.channel, 'D') AS tier,
                      COALESCE(pl.tier_weight, 0.45) AS tier_weight,
                      COALESCE(pl.platform, p.site) AS platform,
                      COALESCE(pl.ranking_score, 0) AS ranking_score,
                      CASE WHEN pl.tier = 'A' THEN 1 ELSE 0 END AS is_a,
                      COALESCE(p.axis, pl.axis) AS row_axis
               FROM positions p LEFT JOIN players pl ON pl.name = p.player
               WHERE p.symbol IS NOT NULL AND p.symbol <> ''
                 AND (? = '' OR COALESCE(substr(p.post_time,1,10),'') >= ?) """,
            (_cut, _cut)
        ).fetchall())
        # 2026-09-25 新增：**真实 trades 兜底源**。
        # 背景：positions 表依赖「详情页」（多数站点需登录）才有数据，真实采集下常为空，
        #       导致「共识/共同持仓」板块整体为空（实测 positions=0 时 consensus=[]）。
        #       trades 表有**真实**买卖记录（作者 + 标的 + 方向 + 时间），足以推导「现在谁还持有」：
        #       同一 (作者, 标的) 的**最新一笔**动作为 BUY/HOLD → 视为持有；
        #       最新一笔为 SELL → 视为已清仓，不计入。
        # 声明：这是从真实成交记录做的**推导**（不是伪造数据）。
        # 2026-10-10：不再用 _seen_ps 直接把「已有 positions 行的选手」整体跳过 —— 那会把
        #   同一位选手的 trades BUY 信号被 positions 的「关注/操作」行掩盖。改为把两个来源的
        #   全部候选行交给下面的「最强信号合并」统一按 (symbol, player) 去重。
        # 兜底源异常不得影响原有 positions 逻辑，故整段 try/except。
        try:
            tr_rows = self.conn.execute(
                """SELECT t.symbol, t.symbol_name, t.author AS player, NULL AS channel,
                          t.post_url, t.action, NULL AS ratio, t.site,
                          t.post_time,
                          COALESCE(pl.tier, 'D') AS tier,
                          COALESCE(pl.tier_weight, 0.45) AS tier_weight,
                          COALESCE(pl.platform, t.site) AS platform,
                          COALESCE(pl.ranking_score, 0) AS ranking_score,
                          CASE WHEN pl.tier = 'A' THEN 1 ELSE 0 END AS is_a,
                          COALESCE(t.axis, pl.axis) AS row_axis
                   FROM trades t LEFT JOIN players pl ON pl.name = t.author
                   WHERE t.symbol IS NOT NULL AND t.symbol <> ''
                     AND t.author IS NOT NULL AND t.author <> ''
                     AND UPPER(COALESCE(t.action, '')) IN ('BUY', 'HOLD')
                     AND NOT EXISTS (
                         SELECT 1 FROM trades t2
                          WHERE t2.author = t.author AND t2.symbol = t.symbol
                            AND (COALESCE(t2.post_time, '') > COALESCE(t.post_time, '')
                                 OR (COALESCE(t2.post_time, '') = COALESCE(t.post_time, '')
                                     AND t2.id > t.id))
                     )"""
            ).fetchall()
            rows = rows + list(tr_rows)
        except Exception:  # noqa: BLE001
            pass

        # 「该选手对该标的的最新一笔 trades 动作」：若为 SELL，则视为已清仓，
        # 该 (选手,标的) 不再作为任何「买入/持有」信号计入（修正「卖完仍显示为共同持仓者」）。
        latest_action: dict[tuple[str, str], str] = {}
        for r in self.conn.execute(
            """SELECT t.author, t.symbol, t.action FROM trades t
                WHERE t.symbol IS NOT NULL AND t.symbol <> ''
                  AND t.author IS NOT NULL AND t.author <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM trades t2
                       WHERE t2.author = t.author AND t2.symbol = t.symbol
                         AND (COALESCE(t2.post_time, '') > COALESCE(t.post_time, '')
                              OR (COALESCE(t2.post_time, '') = COALESCE(t.post_time, '')
                                  AND t2.id > t.id)))"""
        ).fetchall():
            latest_action[(r["author"], r["symbol"])] = (r["action"] or "").strip().upper()

        # 已清仓判定（positions 侧）：SELL 记录的 ratio 表示「本次卖出比例」，
        # 同一选手对同一标的累计 ≥100% 视为清仓。
        sold_ratio: dict[tuple[str, str], float] = {}
        for r in rows:
            if (r["action"] or "").upper() == "SELL" and r["player"]:
                k = (r["player"], r["symbol"])
                sold_ratio[k] = sold_ratio.get(k, 0.0) + float(r["ratio"] or 0)

        # 「最强信号合并」：每位选手对每只股票只保留一条（BUY > HOLD > WATCH；同级取最新）。
        best: dict[tuple[str, str], object] = {}
        for r in rows:
            if not r["player"]:
                continue
            # 机器号（资讯/播报号）**始终**剔除，不受 only_qualified 影响（QA FAIL-D）：
            # axis=='bot' 或 昵称命中 bot_filter 名单/正则 → 其观点不参与共识/共同持仓。
            if is_bot_author(r["player"], r["row_axis"]):
                continue
            if only_qualified and not r["is_a"]:
                continue
            sig = self._signal_of(r["action"])
            if sig == "SELL":
                continue
            k = (r["symbol"], r["player"])
            cur = best.get(k)
            if cur is None:
                best[k] = r
                continue
            cur_sig = self._signal_of(cur["action"])
            if _RANK[sig] > _RANK[cur_sig] or (
                    _RANK[sig] == _RANK[cur_sig]
                    and (r["post_time"] or "") > (cur["post_time"] or "")):
                best[k] = r

        agg: dict[str, dict] = {}
        for (symbol, player), r in best.items():
            sig = self._signal_of(r["action"])
            cleared = (sold_ratio.get((player, symbol), 0.0) >= 100
                       or latest_action.get((player, symbol)) == "SELL")
            d = agg.setdefault(symbol, {
                "symbol": symbol, "name": r["symbol_name"] or "",
                "holders": set(), "channels": set(), "sources": set(),
                "players": [], "sites": set(), "tiers": set(), "platforms": set(),
                "weighted": 0.0, "buy_weighted": 0.0, "seen": set(),
                "buy": set(), "hold": set(), "watch": set(),
            })
            if player and not cleared:
                d["holders"].add(player)
                d[{"BUY": "buy", "HOLD": "hold", "WATCH": "watch"}[sig]].add(player)
                if player not in d["seen"]:            # 同一选手只计一次权重
                    d["seen"].add(player)
                    tw = float(r["tier_weight"] or 0)
                    d["weighted"] = round(d["weighted"] + tw, 3)
                    if sig == "BUY":
                        d["buy_weighted"] = round(d["buy_weighted"] + tw, 3)
            if r["channel"]:
                d["channels"].add(r["channel"])
            if r["post_url"]:
                d["sources"].add(r["post_url"])
            if r["site"]:
                d["sites"].add(r["site"])
            if r["tier"]:
                d["tiers"].add(r["tier"])
            if r["platform"]:
                d["platforms"].add(r["platform"])
            d["players"].append({"player": player, "channel": r["channel"],
                                 "action": r["action"], "signal": sig, "ratio": r["ratio"],
                                 "source_url": r["post_url"], "site": r["site"],
                                 "post_time": r["post_time"],
                                 "tier": r["tier"], "tier_weight": r["tier_weight"],
                                 "platform": r["platform"], "ranking_score": r["ranking_score"],
                                 # 该选手对该标的已卖出累计比例（供界面标注「已清仓 / 部分减仓」）
                                 "sold_ratio": round(sold_ratio.get((player, symbol), 0.0), 1),
                                 "cleared": cleared})
            if not d["name"] and r["symbol_name"]:
                d["name"] = r["symbol_name"]

        # 讨论热度：posts 股票提及数 + trades 操作数
        heat = {}
        for r in self.conn.execute(
            "SELECT symbol, COUNT(*) n FROM stock_mentions GROUP BY symbol"
        ).fetchall():
            heat[r["symbol"]] = heat.get(r["symbol"], 0) + r["n"]
        for r in self.conn.execute(
            "SELECT symbol, COUNT(*) n FROM trades WHERE symbol IS NOT NULL GROUP BY symbol"
        ).fetchall():
            heat[r["symbol"]] = heat.get(r["symbol"], 0) + r["n"]

        # 2026-10-15 P0-1：数据截至日 = 本次**实际采信**的行的最新 post_time。
        # 供界面如实标注「数据截至 YYYY-MM-DD」，杜绝把 5 年前的快照当成今天的数据。
        _asof = max((str(r["post_time"] or "")[:10] for r in rows if r["post_time"]), default="")
        out = []
        for s, d in agg.items():
            if len(d["holders"]) < min_holders:
                continue
            # 名称缺失时回退查名称缓存表，避免表格里只剩裸代码
            name = d["name"] or (self.stock_name_map().get(s, {}).get("name") or "")
            out.append({
                "symbol": s,
                "symbol_name": name,
                "data_asof": _asof,
                "positions_max_age_days": (POSITIONS_MAX_AGE_DAYS if max_age_days is None
                                           else int(max_age_days)),
                "label": f"{name}（{s}）" if name else f"{s}（名称待补全）",
                "holders": len(d["holders"]),
                "buy_holders": len(d["buy"]),
                "hold_holders": len(d["hold"]),
                "watch_holders": len(d["watch"]),
                "weighted_holders": round(d["weighted"], 2),
                "buy_weighted": round(d["buy_weighted"], 2),
                "only_qualified": bool(only_qualified),
                "tiers": sorted(d["tiers"]),
                "platforms": sorted(d["platforms"]),
                "channels": sorted(d["channels"]),
                "sources": sorted(d["sources"]),
                "sites": sorted(d["sites"]),
                "discuss_heat": heat.get(s, 0),
                "players": d["players"],
            })
        out.sort(key=lambda x: (x["holders"], x["discuss_heat"]), reverse=True)
        return out

    # ---------------- 近 N 日同向操作（Request F：共同买入 / 共同卖出） ----------------

    def _latest_action_date(self) -> str:
        """数据里最后一次真实动作的日期（YYYY-MM-DD）。

        看四个时间列（positions/trades 各自的买、卖时间），取最大者。
        用途：窗口锚定。离线/示例数据下「按今天算最近5天」会全空，
        看起来像功能坏了——实际只是数据没跟上今天。
        """
        mx = ""
        for tbl, col in (("positions", "buy_time"), ("positions", "sell_time"),
                         ("trades", "buy_time"), ("trades", "sell_time")):
            row = self.conn.execute(
                f"SELECT MAX({col}) m FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> ''"
            ).fetchone()
            v = ((row["m"] or "")[:10]) if row else ""
            if v > mx:
                mx = v
        return mx

    def cohold_recent(self, days: int = 5, min_holders: int = 2,
                      anchor: str | None = None, only_qualified: bool = False) -> dict:
        """最近 N 日内的「多人同向操作」：≥min_holders 位选手在窗口内买入/卖出/表态持有同一标的。

        与 consensus_stocks 的分工（这正是本板块存在的理由）：
        - consensus_stocks 回答「现在还有多少人**持有**」——看的是持仓快照；
        - 本方法回答「最近 N 天里谁**同时动手**了」——看的是**动作发生时点**，
          所以必须用带时间的 buy_time / sell_time 做窗口过滤，持仓快照答不了这个问题。

        数据源必须合并两张表，只取其一都会漏掉大半：
        - positions：有仓位占比、买价、现价（可推浮盈）；
        - trades   ：有 **买入数量 quantity**（positions 表压根没有这一列）、更精确的成交时刻。
        同一 (选手, 标的) 的多条记录合并为一条，字段取「最近一笔 + 该组内最丰富者」互补。

        ⚠️ 2026-10-10 口径核查（王总批准的 HOLD/BUY 分离），**HOLD 不会被混进「共同买入」**：
          本方法的三组按 action 精确分组 —— buy 组只取 UPPER(action)='BUY'、sell 组只取 'SELL'、
          hold 组只取 'HOLD'，互不重叠；实测窗口内 BUY 组各股人数与「trades 表 action='BUY'
          的去重作者数」逐股一致，HOLD 行也确实一只都没进 buy 组（HOLD 行 sell_time 全空，
          亦不会误入 sell 组）。此前 buy 组不干净的说法不成立。为把「持有」也如实露出，
          本轮新增独立的 hold 组（不与 buy/sell 合并计数）。
          position 侧说明：positions.action 取值为「关注/操作 / 社区晒盘」，从不等于 BUY/SELL，
          故 positions 行目前**不会**进入本方法任何一组（本板块当前完全由 trades 驱动）——
          这是既有事实，此处如实标注，不做静默掩盖。

        anchor：窗口锚定日。默认 auto=「数据最新日」；传 "today" 按日历日，或传具体日期。
        返回值带 anchor_source / today / latest_data_date，前端据此**如实标注**窗口是按哪天算的，
        不做任何静默替换。
        only_qualified=True 时只统计 **players.tier == 'A'** 的入围选手（其余层级/未登记选手剔除）。
        【2026-10-10 FAIL-D】三组的作者集合**一律剔除机器号**（axis=='bot' 或昵称命中
        bot_filter 名单/正则，见 `is_bot_author()`）——与共识面板同一兜底，开关与否都生效。"""
        from datetime import date as _date, timedelta as _td

        span = max(1, int(days))
        today = _date.today().isoformat()
        latest = self._latest_action_date()
        key = (anchor or "auto").strip().lower()
        if key == "today":
            anchor_day, anchor_source = today, "today"
        elif key in ("", "auto", "latest", "data"):
            anchor_day, anchor_source = (latest or today), "latest_data"
        else:
            try:
                anchor_day = _date.fromisoformat(key[:10]).isoformat()
                anchor_source = "explicit"
            except ValueError:
                anchor_day, anchor_source = (latest or today), "latest_data"
        try:
            end_d = _date.fromisoformat(anchor_day)
        except ValueError:
            end_d = _date.today()
        start_d = end_d - _td(days=span - 1)
        lo, hi = start_d.isoformat(), end_d.isoformat()

        return {
            "days": span,
            "min_holders": int(min_holders),
            "anchor": anchor_day,
            "anchor_source": anchor_source,
            "today": today,
            "latest_data_date": latest,
            "window_start": lo,
            "window_end": hi,
            "only_qualified": bool(only_qualified),
            "tier_scope": "A" if only_qualified else "",
            "buy": self._cohold_group("BUY", lo, hi, min_holders, only_qualified),
            "sell": self._cohold_group("SELL", lo, hi, min_holders, only_qualified),
            "hold": self._cohold_group("HOLD", lo, hi, min_holders, only_qualified),
        }

    def _cohold_group(self, action: str, lo: str, hi: str, min_holders: int,
                      only_qualified: bool = False) -> list[dict]:
        """窗口内同一标的被 ≥min_holders 位选手同向操作的清单。

        action 精确分组，三组互不重叠：BUY / SELL / HOLD。
        - BUY 按**买入时刻**（buy_time）过滤；
        - SELL 按**卖出时刻**（sell_time）过滤 —— SELL 行里的 buy_time 是当初的买点，
          拿它判「最近 N 日卖出」会把几个月前建仓、昨天才卖的记录整批漏掉；
        - HOLD 按**表态时刻**（buy_time 优先，缺失回退 post_time）过滤。
        only_qualified=True 时丢弃非 Tier A 选手的行。"""
        action = (action or "BUY").upper()
        if action == "SELL":
            expr_p, expr_t = "p.sell_time", "t.sell_time"
        elif action == "HOLD":
            expr_p = "COALESCE(NULLIF(p.buy_time,''), p.post_time)"
            expr_t = "COALESCE(NULLIF(t.buy_time,''), t.post_time)"
        else:
            expr_p, expr_t = "p.buy_time", "t.buy_time"

        raw: dict[tuple[str, str], list[dict]] = {}

        def add(player, symbol, name, when, channel, site, tier, platform,
                price, quantity, ratio, current_price, hold_since, url, src, is_a=0, axis=None):
            if not player or not symbol or not when:
                return
            # 机器号（资讯/播报号）**始终**剔除，不受 only_qualified 影响（QA FAIL-D）。
            if is_bot_author(player, axis):
                return
            if only_qualified and not is_a:
                return
            d10 = str(when)[:10]
            if d10 < lo or d10 > hi:
                return
            raw.setdefault((player, symbol), []).append({
                "player": player, "symbol": symbol, "symbol_name": name or "",
                "when": str(when), "channel": channel or "", "site": site or "",
                "tier": tier or "", "platform": platform or "", "is_a": int(is_a or 0),
                "price": price, "quantity": quantity, "ratio": ratio,
                "current_price": current_price, "hold_since": hold_since or "",
                "source_url": url or "", "src": src,
            })

        for r in self.conn.execute(
            f"""SELECT p.player, p.symbol, p.symbol_name, {expr_p} AS when_,
                       p.channel, p.site, p.post_url, p.ratio, p.buy_price,
                       p.current_price, p.hold_since,
                       COALESCE(pl.tier, p.channel, '') AS tier,
                       COALESCE(pl.platform, p.site, '') AS platform,
                       CASE WHEN pl.tier = 'A' THEN 1 ELSE 0 END AS is_a,
                       COALESCE(p.axis, pl.axis) AS row_axis
                FROM positions p LEFT JOIN players pl ON pl.name = p.player
                WHERE UPPER(COALESCE(p.action,'')) = ?""", (action,)
        ).fetchall():
            add(r["player"], r["symbol"], r["symbol_name"], r["when_"], r["channel"],
                r["site"], r["tier"], r["platform"], r["buy_price"], None, r["ratio"],
                r["current_price"], r["hold_since"], r["post_url"], "positions", r["is_a"],
                r["row_axis"])

        for r in self.conn.execute(
            f"""SELECT t.author AS player, t.symbol, t.symbol_name, {expr_t} AS when_,
                       t.site, t.post_url, t.position_ratio AS ratio, t.price,
                       t.quantity, t.current_price, t.hold_since,
                       COALESCE(pl.tier, '') AS tier,
                       COALESCE(pl.platform, t.site, '') AS platform,
                       CASE WHEN pl.tier = 'A' THEN 1 ELSE 0 END AS is_a,
                       COALESCE(t.axis, pl.axis) AS row_axis
                FROM trades t LEFT JOIN players pl ON pl.name = t.author
                WHERE UPPER(COALESCE(t.action,'')) = ?""", (action,)
        ).fetchall():
            add(r["player"], r["symbol"], r["symbol_name"], r["when_"], None,
                r["site"], r["tier"], r["platform"], r["price"], r["quantity"],
                r["ratio"], r["current_price"], r["hold_since"], r["post_url"], "trades", r["is_a"],
                r["row_axis"])

        # 仍持有判定（BUY/HOLD 组有意义）：该选手在该标的上有没有「动作时点之后」的卖出记录
        sells: dict[tuple[str, str], list[str]] = {}
        if action in ("BUY", "HOLD"):
            for r in self.conn.execute(
                """SELECT player, symbol, sell_time FROM positions
                    WHERE UPPER(COALESCE(action,''))='SELL' AND sell_time <> ''
                   UNION ALL
                   SELECT author AS player, symbol, sell_time FROM trades
                    WHERE UPPER(COALESCE(action,''))='SELL' AND sell_time <> ''"""
            ).fetchall():
                if r["player"] and r["symbol"] and r["sell_time"]:
                    sells.setdefault((r["player"], r["symbol"]), []).append(r["sell_time"])

        nmap = self.stock_name_map()
        recs: list[dict] = []
        for (player, symbol), lst in raw.items():
            lst.sort(key=lambda x: x["when"], reverse=True)   # 最近一笔在前

            def pick(field, _lst=lst):
                """在「按时间倒序」的组里取第一个非空值 = 最近一笔里最丰富的那个字段。"""
                for x in _lst:
                    v = x[field]
                    if v not in (None, "", 0):
                        return v
                return None

            # 浮盈**只对买入/持有记录有意义**。SELL 行的 price/buy_price 是卖出价，
            # 拿它和现价比得到的是「卖完之后又涨跌了多少」，跟这笔交易的收益无关，
            # 会直接被误读成盈利 —— 故卖出组一律返回 None，不用近似值凑数。
            gain = None
            if action in ("BUY", "HOLD"):
                for x in lst:            # 必须用**同一笔**的买价与现价算，不能跨笔混算
                    if x["price"] and x["current_price"]:
                        gain = round((x["current_price"] / x["price"] - 1) * 100, 1)
                        break
            last_time = lst[0]["when"]
            if action in ("BUY", "HOLD"):
                # BUY/HOLD 都表示「当前还拿着」：除非动作时点之后另有卖出记录，否则记为持有中。
                done = [s for s in sells.get((player, symbol), []) if s >= last_time]
                exited, holding = bool(done), not done
            else:
                exited, holding = True, False
            recs.append({
                "player": player, "symbol": symbol,
                "symbol_name": lst[0]["symbol_name"] or (pick("symbol_name") or ""),
                "channel": pick("channel") or "", "site": lst[0]["site"] or "",
                "tier": pick("tier") or "", "platform": pick("platform") or "",
                "source_url": pick("source_url") or "",
                "first_time": lst[-1]["when"], "last_time": last_time, "ops": len(lst),
                "price": pick("price"), "quantity": pick("quantity"),
                "ratio": pick("ratio"), "current_price": pick("current_price"),
                "pct_gain": gain, "hold_since": pick("hold_since") or "",
                "still_holding": holding, "exited": exited,
                "evidence": sorted({x["src"] for x in lst}),
            })

        by_sym: dict[str, dict] = {}
        for rec in recs:
            g = by_sym.setdefault(rec["symbol"], {
                "symbol": rec["symbol"], "symbol_name": "", "players": [],
                "tiers": set(), "platforms": set(), "channels": set(), "sites": set()})
            g["players"].append(rec)
            if rec["symbol_name"] and not g["symbol_name"]:
                g["symbol_name"] = rec["symbol_name"]
            for key, field in (("tiers", "tier"), ("platforms", "platform"),
                               ("channels", "channel")):
                if rec[field]:
                    g[key].add(rec[field])
            if rec["site"]:
                g["sites"].add(rec["site"])

        out: list[dict] = []
        for symbol, g in by_sym.items():
            pls = g["players"]
            people = {p["player"] for p in pls}
            if len(people) < max(1, int(min_holders)):
                continue
            pls.sort(key=lambda x: x["last_time"], reverse=True)
            pairs = [(p["price"], p["quantity"] or 0) for p in pls if p["price"]]
            if pairs:
                wsum = sum(w for _, w in pairs)
                avg = (sum(pr * w for pr, w in pairs) / wsum) if wsum \
                    else (sum(pr for pr, _ in pairs) / len(pairs))
                avg_price = round(avg, 3)
            else:
                avg_price = None
            name = g["symbol_name"] or (nmap.get(symbol, {}).get("name") or "")
            out.append({
                "symbol": symbol,
                "symbol_name": name,
                "label": f"{name}（{symbol}）" if name else f"{symbol}（名称待补全）",
                "action": action,
                "player_count": len(people),
                "players": pls,
                "total_quantity": sum(p["quantity"] or 0 for p in pls) or None,
                "avg_price": avg_price,
                "first_time": min(p["first_time"] for p in pls),
                "last_time": max(p["last_time"] for p in pls),
                "tiers": sorted(x for x in g["tiers"] if x),
                "platforms": sorted(x for x in g["platforms"] if x),
                "channels": sorted(x for x in g["channels"] if x),
                "sites": sorted(g["sites"]),
                "urls": sorted({p["source_url"] for p in pls if p["source_url"]}),
                "evidence": sorted({e for p in pls for e in p["evidence"]}),
                "still_holding": sum(1 for p in pls if p["still_holding"]),
                "exited": sum(1 for p in pls if p["exited"]),
            })
        out.sort(key=lambda x: (x["player_count"], x["last_time"]), reverse=True)
        return out

    # ---------------- 原始页面（完整保留） ----------------
    def save_raw_page(self, site, url, html, http_status=200) -> int:
        # 缺陷 1 修复：raw_pages.html 内容会随页面变化（同一 URL 存在多版 html），
        # 故按 (site,url) 去重并保留最新一版（UPSERT）。迁移未执行、唯一索引尚不存在时
        # 降级为普通 INSERT（不去重但不报错），保证运行中的旧库不被破坏。
        has_ux = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_raw_pages_site_url'"
        ).fetchone() is not None
        if has_ux:
            self.conn.execute(
                """INSERT INTO raw_pages (site, url, fetched_at, http_status, html)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(site, url) DO UPDATE SET
                     html=excluded.html, fetched_at=excluded.fetched_at,
                     http_status=excluded.http_status""",
                (site, url, _now(), http_status, html),
            )
        else:
            self.conn.execute(
                "INSERT INTO raw_pages (site, url, fetched_at, http_status, html) VALUES (?,?,?,?,?)",
                (site, url, _now(), http_status, html),
            )
        self.conn.commit()
        # ⚠️ 不能用 lastrowid：同一 URL 二次抓取时 UPSERT 走的是 UPDATE 分支，
        # 而 SQLite 的 last_insert_rowid() 只被 INSERT 改写 —— UPDATE 后它仍是**更早那次**
        # INSERT 的 rowid（或 0）。旧写法直接 return lastrowid，会把本页解析出的所有帖子
        # 的 raw_page_id 指向一个不存在 / 别的页面的存档，表现就是「帖子详情里点不到原始存档」。
        # 列表页 URL 会被反复抓取 → 命中 UPDATE 分支是常态，所以 1059/1059 帖全部断链。
        # 统一按 (site,url) 回查真实 id（无唯一索引时可能多行，取最后写入的那条）。
        row = self.conn.execute(
            "SELECT id FROM raw_pages WHERE site=? AND url=? ORDER BY id DESC LIMIT 1",
            (site, url),
        ).fetchone()
        return int(row["id"]) if row else 0

    def get_raw_page(self, page_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT html FROM raw_pages WHERE id=?", (page_id,)
        ).fetchone()
        return row["html"] if row else None

    # ---------------- 帖子 / 交易 / 资讯 ----------------
    def save_post(self, **kw) -> int:
        # 缺陷 1 修复：INSERT OR IGNORE 保证重复抓取不再新增行（唯一索引建立后生效）。
        # 若被忽略（重复帖子），返回既有行 id，避免下游 save_stock_mention 写成孤儿。
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO posts (site, author, post_id, post_url, post_time, text, sentiment, sentiment_score, raw_page_id)
               VALUES (:site,:author,:post_id,:post_url,:post_time,:text,:sentiment,:sentiment_score,:raw_page_id)""",
            kw,
        )
        self.conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = self.conn.execute(
            "SELECT id FROM posts WHERE site=:site AND post_url=:post_url AND author=:author LIMIT 1",
            kw,
        ).fetchone()
        return int(row["id"]) if row else 0

    def save_trade(self, **kw):
        # 兼容旧调用方：新增列缺省补齐，避免 sqlite3 因缺少绑定参数而抛错
        for k, d in (("buy_time", ""), ("buy_price", None), ("current_price", None),
                     ("sell_time", ""), ("sell_price", None), ("hold_since", ""),
                     ("position_ratio", None), ("access_level", "free"),
                     ("access_hint", ""), ("raw_text", "")):
            kw.setdefault(k, d)
        self._ensure_symbol_name(kw)
        self.conn.execute(
            """INSERT OR IGNORE INTO trades (site, author, post_id, post_url, post_time, action, symbol, symbol_name,
                                  price, quantity, buy_time, buy_price, current_price,
                                  sell_time, sell_price, hold_since,
                                  access_level, access_hint, raw_text)
               VALUES (:site,:author,:post_id,:post_url,:post_time,:action,:symbol,:symbol_name,
                       :price,:quantity,:buy_time,:buy_price,:current_price,
                       :sell_time,:sell_price,:hold_since,
                       :access_level,:access_hint,:raw_text)""",
            kw,
        )
        self.conn.commit()

    def save_news(self, **kw):
        # 缺陷 1 修复：INSERT OR IGNORE 去重；被忽略时返回既有行 id。
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO news (site, title, url, publish_time, summary, source)
               VALUES (:site,:title,:url,:publish_time,:summary,:source)""",
            kw,
        )
        self.conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = self.conn.execute(
            "SELECT id FROM news WHERE site=:site AND url=:url AND title=:title LIMIT 1",
            kw,
        ).fetchone()
        return int(row["id"]) if row else 0

    # ---------------- 派生表 ----------------
    def save_stock_mention(self, post_id_ref, symbol, symbol_name, context):
        self.conn.execute(
            "INSERT INTO stock_mentions (post_id_ref, symbol, symbol_name, context) VALUES (?,?,?,?)",
            (post_id_ref, symbol, symbol_name, context),
        )
        self.conn.commit()

    # ---------------- 股票代码 → 名称 ----------------
    def stock_name_map(self) -> dict[str, dict]:
        """读取名称缓存表，返回 {代码: {name, company, market, source}}。"""
        try:
            rows = self.conn.execute("SELECT * FROM stock_names").fetchall()
        except Exception:  # noqa: BLE001
            return {}
        return {r["code"]: {"name": r["name"] or "", "company": r["company"] or "",
                            "market": r["market"] or "", "source": r["source"] or ""} for r in rows}

    def upsert_stock_name(self, code, name, company="", market="", source="manual"):
        self.conn.execute(
            "INSERT OR REPLACE INTO stock_names (code, name, company, market, source, updated_at) "
            "VALUES (?,?,?,?,?,?)", (code, name, company, market, source, _now()),
        )
        self.conn.commit()

    # ---------------- 个股「公司概况」（主营业务 / 公司简介 / 行业） ----------------
    # 2026-09-26 新增：由 server.py 的 F10 抓取写入，读侧走本表（离线可用）。
    _ORG_FIELDS = ("code", "name", "full_name", "main_business", "profile",
                   "business_scope", "industry", "industry_csrc", "found_date",
                   "list_date", "reg_capital", "emp_num", "web", "source")

    def org_get(self, code: str) -> dict | None:
        try:
            r = self.conn.execute("SELECT * FROM stock_org WHERE code=?", (code,)).fetchone()
        except Exception:  # noqa: BLE001
            return None
        return dict(r) if r else None

    def org_put(self, code, info) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO stock_org (code, name, full_name, main_business, profile, "
            "business_scope, industry, industry_csrc, found_date, list_date, reg_capital, emp_num, "
            "web, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, info.get("name"), info.get("full_name"), info.get("main_business"),
             info.get("profile"), info.get("business_scope"), info.get("industry"),
             info.get("industry_csrc"), info.get("found_date"), info.get("list_date"),
             info.get("reg_capital"), info.get("emp_num"), info.get("web"),
             info.get("source") or "eastmoney_f10", _now()),
        )
        self.conn.commit()

    def all_symbols(self) -> list[str]:
        """库中出现过的全部股票代码。

        ⚠️ 2026-10-31 换源：原实现是 stock_mentions ∪ trades ∪ positions ——
        三张表**全部是论坛抓取产物**，论坛 2026-10-30 停抓后已被清空，
        留在原处会让本函数永久返回空列表（而它有真实消费方：
        stock_names.backfill_missing 的默认代码集、/api/posts 的兜底）。
        现改为从「仍在日更的官方宇宙」取：a_shares_universe（全A报价快照日更）
        ∪ stock_names（名称表，含退市/历史代码），两者都是交易所口径、不依赖任何爬虫。
        """
        rows = self.conn.execute(
            "SELECT code FROM a_shares_universe WHERE code IS NOT NULL AND code<>'' "
            "UNION SELECT code FROM stock_names WHERE code IS NOT NULL AND code<>''"
        ).fetchall()
        return sorted({r["code"] for r in rows})

    def upsert_person(self, name, role="author"):
        self.conn.execute(
            "INSERT INTO persons (name, mention_count, role) VALUES (?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET mention_count = mention_count + 1",
            (name, role),
        )
        self.conn.commit()

    def rebuild_persons(self):
        """从 posts/trades 的作者与 @提及 重建 persons 统计（清空后重算）。"""
        self.conn.execute("DELETE FROM persons")
        for row in self.conn.execute(
            "SELECT author FROM posts UNION ALL SELECT author FROM trades"
        ):
            if row["author"] and row["author"] != "unknown":
                self.upsert_person(row["author"], "author")
        self.conn.commit()

    def save_topic(self, topic, weight, sample):
        self.conn.execute(
            "INSERT INTO topics (topic, weight, sample) VALUES (?,?,?)",
            (topic, weight, sample),
        )
        self.conn.commit()

    def clear_topics(self):
        self.conn.execute("DELETE FROM topics")
        self.conn.commit()

    # ---------------- 海外舆情（模块一 / 模块二） ----------------
    def clear_overseas_news(self):
        self.conn.execute("DELETE FROM overseas_news")
        self.conn.execute("DELETE FROM overseas_mentions")
        self.conn.commit()

    def save_overseas_news(self, **kw) -> int:
        """写入一条海外新闻/观点；按 (url,title) 去重。返回该行 id。"""
        cur = self.conn.execute(
            """INSERT INTO overseas_news
               (source_key, source_name, region, lang, kind, title, url, summary, publish_time,
                topic, topics, sentiment, sentiment_score, heat, entities, sectors, fetched_at)
               VALUES (:source_key,:source_name,:region,:lang,:kind,:title,:url,:summary,:publish_time,
                       :topic,:topics,:sentiment,:sentiment_score,:heat,:entities,:sectors,:fetched_at)
               ON CONFLICT(url, title) DO UPDATE SET
                 summary=excluded.summary, publish_time=excluded.publish_time, topic=excluded.topic,
                 topics=excluded.topics, sentiment=excluded.sentiment, sentiment_score=excluded.sentiment_score,
                 heat=excluded.heat, entities=excluded.entities, sectors=excluded.sectors,
                 fetched_at=excluded.fetched_at""",
            dict(source_key=kw.get("source_key", ""), source_name=kw.get("source_name", ""),
                 region=kw.get("region", "GLOBAL"), lang=kw.get("lang", "en"), kind=kw.get("kind", "media"),
                 title=kw.get("title", ""), url=kw.get("url", ""), summary=kw.get("summary", ""),
                 publish_time=kw.get("publish_time", ""), topic=kw.get("topic", "其他"),
                 topics=kw.get("topics", ""), sentiment=kw.get("sentiment", "中性"),
                 sentiment_score=float(kw.get("sentiment_score") or 0), heat=float(kw.get("heat") or 0),
                 entities=kw.get("entities", "[]"), sectors=kw.get("sectors", ""), fetched_at=_now()),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM overseas_news WHERE url=? AND title=?",
            (kw.get("url", ""), kw.get("title", ""))).fetchone()
        return row["id"] if row else 0

    def save_overseas_mention(self, news_id_ref, market, symbol, name=""):
        self.conn.execute(
            "INSERT INTO overseas_mentions (news_id_ref, market, symbol, name) VALUES (?,?,?,?)",
            (news_id_ref, market, symbol, name))
        self.conn.commit()

    def save_overseas_source_status(self, rows: list[dict]):
        keys = [r.get("key", "") for r in rows if r.get("key")]
        for r in rows:
            self.conn.execute(
                """INSERT INTO overseas_sources_status
                   (source_key,name,region,status,ok,count,error,latency,requires_key,requires_vpn,enabled,note,checked_at)
                   VALUES (:key,:name,:region,:status,:ok,:count,:error,:latency,:requires_key,:requires_vpn,:enabled,:note,:checked_at)
                   ON CONFLICT(source_key) DO UPDATE SET
                     status=excluded.status, ok=excluded.ok, count=excluded.count, error=excluded.error,
                     latency=excluded.latency, requires_key=excluded.requires_key,
                     requires_vpn=excluded.requires_vpn, enabled=excluded.enabled,
                     note=excluded.note, checked_at=excluded.checked_at""",
                dict(key=r.get("key", ""), name=r.get("name", ""), region=r.get("region", ""),
                     status=r.get("status", ""), ok=int(bool(r.get("ok"))), count=int(r.get("count") or 0),
                     error=r.get("error", ""), latency=float(r.get("latency") or 0),
                     requires_key=int(bool(r.get("requires_key"))),
                     requires_vpn=int(bool(r.get("requires_vpn"))),
                     enabled=int(bool(r.get("enabled"))),
                     note=r.get("note", ""), checked_at=_now()))
        # 本次写入后，把不在本批 key 集合内的旧行删掉。
        # 说明：原实现只有 ON CONFLICT DO UPDATE，配置里已移除的源会作为"幽灵源"永久残留在界面。
        if keys:
            marks = ",".join("?" * len(keys))
            self.conn.execute(
                f"DELETE FROM overseas_sources_status WHERE source_key NOT IN ({marks})", keys)
        self.conn.commit()

    def replace_overseas_daily(self, rows: list[dict]):
        self.conn.execute("DELETE FROM overseas_sentiment_daily")
        for r in rows:
            self.conn.execute(
                """INSERT INTO overseas_sentiment_daily
                   (day,region,bull,bear,neutral,total,heat,mood) VALUES (?,?,?,?,?,?,?,?)""",
                (r.get("day", ""), r.get("region", ""), int(r.get("bull") or 0),
                 int(r.get("bear") or 0), int(r.get("neutral") or 0), int(r.get("total") or 0),
                 float(r.get("heat") or 0), float(r.get("mood") or 0)))
        self.conn.commit()

    def save_overseas_hotspot(self, **kw):
        self.conn.execute(
            """INSERT INTO overseas_hotspots
               (key,title,topic,overview,driver_sources,driver_regions,direction,related_markets,related_symbols,
                domestic_sectors,variables,sectors_up,sectors_down,sectors_up_detail,sectors_down_detail,decision,
                transmission,amplification,cross_check,
                amplification_factor,confidence,heat,evidence,first_seen,updated_at)
               VALUES (:key,:title,:topic,:overview,:driver_sources,:driver_regions,:direction,:related_markets,
                       :related_symbols,:domestic_sectors,:variables,:sectors_up,:sectors_down,
                       :sectors_up_detail,:sectors_down_detail,:decision,:transmission,
                       :amplification,:cross_check,
                       :amplification_factor,:confidence,:heat,:evidence,:first_seen,:updated_at)
               ON CONFLICT(key) DO UPDATE SET
                 title=excluded.title, topic=excluded.topic, overview=excluded.overview,
                 driver_sources=excluded.driver_sources,
                 driver_regions=excluded.driver_regions, direction=excluded.direction,
                 related_markets=excluded.related_markets, related_symbols=excluded.related_symbols,
                 domestic_sectors=excluded.domestic_sectors, variables=excluded.variables,
                 sectors_up=excluded.sectors_up, sectors_down=excluded.sectors_down,
                 sectors_up_detail=excluded.sectors_up_detail,
                 sectors_down_detail=excluded.sectors_down_detail, decision=excluded.decision,
                 transmission=excluded.transmission,
                 amplification=excluded.amplification, cross_check=excluded.cross_check,
                 amplification_factor=excluded.amplification_factor, confidence=excluded.confidence,
                 heat=excluded.heat, evidence=excluded.evidence, updated_at=excluded.updated_at""",
            dict(key=kw.get("key", ""), title=kw.get("title", ""), topic=kw.get("topic", ""),
                 overview=kw.get("overview", ""),
                 driver_sources=kw.get("driver_sources", ""), driver_regions=kw.get("driver_regions", ""),
                 direction=kw.get("direction", ""), related_markets=kw.get("related_markets", ""),
                 related_symbols=kw.get("related_symbols", ""), domestic_sectors=kw.get("domestic_sectors", ""),
                 variables=kw.get("variables", ""), sectors_up=kw.get("sectors_up", ""),
                 sectors_down=kw.get("sectors_down", ""),
                 sectors_up_detail=kw.get("sectors_up_detail", ""),
                 sectors_down_detail=kw.get("sectors_down_detail", ""),
                 decision=kw.get("decision", ""),
                 transmission=kw.get("transmission", ""), amplification=kw.get("amplification", ""),
                 cross_check=kw.get("cross_check", ""),
                 amplification_factor=float(kw.get("amplification_factor") or 1.0),
                 confidence=float(kw.get("confidence") or 0.5), heat=float(kw.get("heat") or 0),
                 evidence=kw.get("evidence", "[]"), first_seen=kw.get("first_seen") or _now(),
                 updated_at=_now()))
        self.conn.commit()

    def clear_overseas_hotspots(self):
        self.conn.execute("DELETE FROM overseas_hotspots")
        self.conn.commit()

    def overseas_symbols(self) -> list[dict]:
        """海外新闻提及的标的聚合（供舆情监控展示重点标的）。"""
        rows = self.conn.execute(
            """SELECT om.market AS market, om.symbol AS symbol, om.name AS name,
                      COUNT(*) AS mentions,
                      SUM(CASE WHEN n.sentiment='看多' THEN 1 ELSE 0 END) AS bull,
                      SUM(CASE WHEN n.sentiment='看空' THEN 1 ELSE 0 END) AS bear
               FROM overseas_mentions om JOIN overseas_news n ON n.id=om.news_id_ref
               GROUP BY om.market, om.symbol ORDER BY mentions DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 登录态 ----------------
    def set_login_status(self, site, status, detail=""):
        self.conn.execute(
            "INSERT INTO login_status (site, status, checked_at, detail) VALUES (?,?,?,?) "
            "ON CONFLICT(site) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at, detail=excluded.detail",
            (site, status, _now(), detail),
        )
        self.conn.commit()

    def get_login_status(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM login_status")]

    # ---------------- meta ----------------
    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------------- 刷新状态（Request G：最后更新列 / 实时刷新记录） ----------------
    def record_refresh(self, source_key: str, status: str, item_count: int = 0,
                       display_name: str | None = None, error: str = ""):
        """记录一次抓取结果（on_conflict 覆盖同一来源的上一行）。

        关键语义（决定了「最后更新」列是否可信）：
        - status="ok"：last_ok_at = 当前时刻。这是一次**成功**刷新。
        - status="error"：**不动 last_ok_at**（也不沿用旧值覆盖为空），只更新
          last_attempt_at/status/error。理由：一次失败不能把「上次成功更新于何时」
          抹掉或前移，否则前端会误判为「刚刚更新过」或「从未更新」。
        """
        now = _now()
        prev = self.conn.execute(
            "SELECT display_name, last_ok_at FROM refresh_status WHERE source_key=?",
            (source_key,),
        ).fetchone()
        prev_ok = prev["last_ok_at"] if prev else None
        dn = display_name or (prev["display_name"] if prev else "") or source_key
        ok_at = now if status == "ok" else prev_ok
        self.conn.execute(
            """INSERT INTO refresh_status
                   (source_key, display_name, last_ok_at, last_attempt_at, status, item_count, error)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET
                   display_name   = excluded.display_name,
                   last_ok_at     = excluded.last_ok_at,
                   last_attempt_at= excluded.last_attempt_at,
                   status         = excluded.status,
                   item_count     = excluded.item_count,
                   error          = excluded.error""",
            (source_key, dn, ok_at, now, status, int(item_count or 0), (error or "")[:300]),
        )
        self.conn.commit()

    def get_refresh_status(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM refresh_status ORDER BY source_key")]

    # ---------------- 学习知识库（「跟我学习」） ----------------
    def kb_save(self, payload: dict, force: bool = False) -> dict:
        """落库一份知识库 payload（整篇替换）。返回落库结果摘要。

        幂等语义（每天定时跑也不会产生无意义重建）：
        - `content_hash` 与库中现存一致**且 parser_version 也一致** → **直接跳过**，
          不写 payload、不动 ingested_at，只回 {"changed": False, "skipped": True}。
        - `force=True` 忽略哈希强制重吸取（前端「重新吸取」按钮用）。
        - 两个触发条件缺一不可：上游文件变了（哈希不等）**或**解析器变了（版本不等）。
          后者是必要的——解析逻辑修好时上游文件纹丝不动，只比哈希会把修复成果锁在门外。
        - 无论是否变化，都把旧值挪进 prev_hash/prev_ingested_at，使「上一版是什么」可追溯。
        """
        import json as _json

        key = payload.get("doc_key") or "kb"
        prev = self.conn.execute(
            "SELECT content_hash, ingested_at, reload_count, parser_version "
            "FROM kb_doc WHERE doc_key=?",
            (key,),
        ).fetchone()
        prev_hash = prev["content_hash"] if prev else None
        new_hash = payload.get("content_hash") or ""
        new_pv = int(payload.get("parser_version") or 0)
        prev_pv = int(prev["parser_version"] or 0) if prev else 0
        parser_bumped = bool(prev and prev_pv != new_pv)
        changed = bool(force or not prev_hash or prev_hash != new_hash or parser_bumped)
        if not changed:
            return {"doc_key": key, "changed": False, "skipped": True,
                    "content_hash": new_hash, "parser_version": new_pv,
                    "ingested_at": prev["ingested_at"] if prev else "",
                    "reload_count": prev["reload_count"] if prev else 0}
        now = _now()
        self.conn.execute(
            """INSERT INTO kb_doc
                   (doc_key, track, title, version, source_path, source_mtime,
                    content_hash, char_count, node_count, status, error, payload,
                    prev_hash, prev_ingested_at, ingested_at, changed, reload_count,
                    parser_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(doc_key) DO UPDATE SET
                   track           = excluded.track,
                   title           = excluded.title,
                   version         = excluded.version,
                   source_path     = excluded.source_path,
                   source_mtime    = excluded.source_mtime,
                   content_hash    = excluded.content_hash,
                   char_count      = excluded.char_count,
                   node_count      = excluded.node_count,
                   status          = excluded.status,
                   error           = excluded.error,
                   payload         = excluded.payload,
                   prev_hash       = kb_doc.content_hash,
                   prev_ingested_at= kb_doc.ingested_at,
                   ingested_at     = excluded.ingested_at,
                   changed         = 1,
                   reload_count    = kb_doc.reload_count + 1,
                   parser_version  = excluded.parser_version""",
            (key, payload.get("track") or "", payload.get("title") or "",
             payload.get("version") or "", payload.get("source_path") or "",
             payload.get("source_mtime") or "", new_hash,
             int(payload.get("char_count") or 0), int(payload.get("node_count") or 0),
             payload.get("status") or "ok", (payload.get("error") or "")[:300],
             _json.dumps(payload, ensure_ascii=False),
             prev_hash, (prev["ingested_at"] if prev else None), now,
             1,   # 首次入库 reload_count=1；冲突路径由 ON CONFLICT 的 +1 接管
             new_pv),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT ingested_at, reload_count FROM kb_doc WHERE doc_key=?", (key,)).fetchone()
        return {"doc_key": key, "changed": True, "skipped": False,
                "content_hash": new_hash, "parser_version": new_pv,
                "parser_bumped": parser_bumped,
                "ingested_at": row["ingested_at"] if row else now,
                "reload_count": row["reload_count"] if row else 1,
                "prev_hash": prev_hash}

    def kb_get(self, doc_key: str = None) -> Optional[dict]:
        """取知识库文档。doc_key=None 时取最近吸取的一份（单知识库场景）。

        返回结构（刻意分成两层，避免调用方拿着嵌套三层的 JSON 到处找键）：
        - `doc`      = 解析出的**文档正文**（meta / outline / schools / bloggers / …）
        - `envelope` = 落库信封的其余字段（title / version / status / error / source_path…）
        行上的列（ingested_at / content_hash / reload_count…）与两者并列。
        """
        import json as _json

        if doc_key:
            row = self.conn.execute(
                "SELECT * FROM kb_doc WHERE doc_key=?", (doc_key,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM kb_doc ORDER BY ingested_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            env = _json.loads(d.pop("payload") or "{}")
            if not isinstance(env, dict):
                env = {}
        except (ValueError, TypeError) as e:  # 库内容损坏时不让整个接口 500
            env = {}
            d["payload_error"] = str(e)[:200]
        d["doc"] = env.get("doc") or {}
        d["envelope"] = {k: v for k, v in env.items() if k != "doc"}
        return d

    def kb_list(self) -> list[dict]:
        """列出全部知识库文档（不含 payload，供「学习轨道管理」列表用）。"""
        return [dict(r) for r in self.conn.execute(
            """SELECT doc_key, track, title, version, source_path, source_mtime,
                      content_hash, char_count, node_count, status, error,
                      prev_hash, prev_ingested_at, ingested_at, changed, reload_count,
                      parser_version
               FROM kb_doc ORDER BY track, doc_key""")]

    # ---------------- 快照（破坏性写操作的前置保护） ----------------
    def snapshot(self, prefix: str = "app.db.pre-demo", keep: int = 5) -> str:
        """一致性快照：把当前库完整复制到 <库目录>/backups/<prefix>-YYYYmmdd-HHMMSS。

        2026-09-22 新增（P0-2）：`/api/demo` 与 `reset=True` 的 `/api/overseas`
        都会先 DELETE 掉 14 张业务表，一旦执行即不可逆。此前没有任何回滚点，
        真实使用中等于「一键毁库」。改为在执行前用 sqlite3.Connection.backup()
        做一份**事务一致**的整库副本（WAL 下的未落盘改动也会被正确包含），
        保留最近 keep 份（同前缀），更旧的自动删除。返回快照绝对路径。

        调用方应在此方法抛异常时**中止**破坏性操作（宁可不清，也不裸清）。
        """
        base = Path(self.path).resolve().parent
        bdir = base / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = bdir / f"{prefix}-{stamp}"
        n = 1
        while dest.exists():  # 同一秒内重复调用时避免互相覆盖
            dest = bdir / f"{prefix}-{stamp}-{n}"
            n += 1
        try:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        except Exception as e:  # noqa: BLE001 —— 磁盘满 / 权限 / 库损坏等一律视为快照失败
            # 清掉可能写了一半的副本文件，避免留下「看似有快照、实为残file」的假回滚点。
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass
            raise RuntimeError(f"快照失败：{e}") from e
        # 只保留最近 keep 份（名称前缀含可排序的 YYYYmmdd-HHMMSS，按名排序即为时间序）
        olds = sorted(bdir.glob(f"{prefix}-*"))
        for old in olds[:-keep] if keep > 0 else []:
            try:
                old.unlink()
            except OSError:
                pass
        return str(dest)

    # ---------------- 幂等去重迁移（缺陷 1 修复） ----------------
    def ensure_dedup_indexes(self) -> dict:
        """为 news/posts/trades/raw_pages 建立唯一索引并清理存量重复行。

        幂等：重复调用不会报错，第二次调用各表删除 0 行。
        去重口径：
          - news/posts/trades：保留每组（唯一键）中 id 最小的一条；
          - raw_pages：同一 (site,url) 只保留 id 最大（最近抓取）的一条——
            raw_pages.html 内容会随页面变化（同一 URL 有多版 html），不能按 url
            简单去重，故按 (site,url) 保留最新一版（详见迁移说明）。
        返回 {表: 本次删除行数}。
        """
        c = self.conn
        removed: dict[str, int] = {}
        # news / posts / trades：按唯一键保留 id 最小
        for tbl, group in (
            ("news", "site, url, title"),
            ("posts", "site, post_url, author"),
            ("trades", "site, post_url, author, symbol_name, action"),
        ):
            before = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
            c.execute(
                f"DELETE FROM {tbl} WHERE id NOT IN "
                f"(SELECT MIN(id) FROM {tbl} GROUP BY {group})"
            )
            c.commit()
            after = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
            removed[tbl] = before - after
        # raw_pages：按 (site,url) 保留 id 最大（最新抓取）
        before = c.execute("SELECT COUNT(*) AS n FROM raw_pages").fetchone()["n"]
        c.execute(
            "DELETE FROM raw_pages WHERE id NOT IN "
            "(SELECT MAX(id) FROM raw_pages GROUP BY site, url)"
        )
        c.commit()
        after = c.execute("SELECT COUNT(*) AS n FROM raw_pages").fetchone()["n"]
        removed["raw_pages"] = before - after
        # 建唯一索引（幂等）
        c.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_news_site_url_title
                ON news(site, url, title);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_site_posturl_author
                ON posts(site, post_url, author);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_keys
                ON trades(site, post_url, author, symbol_name, action);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_pages_site_url
                ON raw_pages(site, url);
            """
        )
        c.commit()
        # 来源标签列（幂等）：既有库补建 source_type，并把历史 trades 标记为 bar_post
        try:
            c.execute("ALTER TABLE trades ADD COLUMN source_type TEXT DEFAULT 'bar_post'")
            c.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在
        c.execute("UPDATE trades SET source_type='bar_post' WHERE source_type IS NULL OR source_type=''")
        c.commit()
        return removed

    def recompute_topics(self, top_n: int = 25):
        """用（已修复的）关键词法从 posts 重新计算 topics。topics 是派生缓存表。"""
        from . import analytics  # 延迟导入，避免循环依赖

        texts = [r["text"] for r in self.conn.execute(
            "SELECT text FROM posts WHERE text IS NOT NULL AND text<>''").fetchall()]
        self.clear_topics()
        for topic, weight in analytics.extract_topics(texts, top_n=top_n):
            self.save_topic(topic, float(weight), "")

    def migrate_dedupe(self, apply_residual: bool = True) -> dict:
        """一次性迁移：业务表幂等去重 + 唯一索引 + 示例/孤儿残留清理。

        不在本方法内做备份——备份由调用方（迁移脚本）在调用前用 sqlite 在线备份完成。
        返回报告 dict（供脚本打印）。apply_residual=False 时只做业务表去重+索引。
        """
        report: dict = {}
        report["before"] = self.counts()
        report["dedup_removed"] = self.ensure_dedup_indexes()

        if apply_residual:
            c = self.conn
            # persons：role∈{author,mention} 且既非帖子/交易作者、也不在任何帖子正文中
            # 出现（即 @提及或作者）的，视为残留示例/孤儿。保留真实人物（含纯 @提及者）。
            before = c.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
            c.execute(
                "DELETE FROM persons "
                "WHERE role IN ('author','mention') "
                "AND name NOT IN ("
                "   SELECT author FROM posts WHERE author IS NOT NULL "
                "   UNION SELECT author FROM trades WHERE author IS NOT NULL) "
                "AND NOT EXISTS ("
                "   SELECT 1 FROM posts WHERE posts.text LIKE '%' || persons.name || '%')"
            )
            c.commit()
            after = c.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
            report["persons_removed"] = before - after

            # stock_mentions：post_id_ref 指向不存在的 post 即为孤儿（含示例残留）
            before = c.execute("SELECT COUNT(*) AS n FROM stock_mentions").fetchone()["n"]
            c.execute(
                "DELETE FROM stock_mentions "
                "WHERE post_id_ref IS NULL OR post_id_ref NOT IN (SELECT id FROM posts)"
            )
            c.commit()
            after = c.execute("SELECT COUNT(*) AS n FROM stock_mentions").fetchone()["n"]
            report["stock_mentions_removed"] = before - after

            # topics：清除旧解析器（字符 n-gram）产出的垃圾，并用修复后的解析器重算
            before = c.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
            self.recompute_topics()
            after = c.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
            report["topics_before"] = before
            report["topics_after"] = after

            # overseas_news：清理 seed 示例（url 形如 example.local/seed、source_key 形如 示例源）
            before = c.execute("SELECT COUNT(*) AS n FROM overseas_news").fetchone()["n"]
            c.execute(
                "DELETE FROM overseas_news "
                "WHERE url LIKE 'https://example.local/seed/%' OR source_key LIKE '示例源%'"
            )
            c.commit()
            after = c.execute("SELECT COUNT(*) AS n FROM overseas_news").fetchone()["n"]
            report["overseas_news_removed"] = before - after

            # overseas_mentions：清理指向已删除海外新闻的孤儿
            before = c.execute("SELECT COUNT(*) AS n FROM overseas_mentions").fetchone()["n"]
            c.execute(
                "DELETE FROM overseas_mentions "
                "WHERE news_id_ref NOT IN (SELECT id FROM overseas_news)"
            )
            c.commit()
            after = c.execute("SELECT COUNT(*) AS n FROM overseas_mentions").fetchone()["n"]
            report["overseas_mentions_removed"] = before - after

        report["after"] = self.counts()
        return report

    # ---------------- 计数 ----------------
    def counts(self) -> dict:
        def c(t):
            return self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]

        return {
            # ---- 论坛链路（2026-10-30 停抓、2026-10-31 已清表，恒为 0）----
            # 保留这些键是为了不炸旧调用方；前端统计卡已改用下面「活数据」那一组。
            "posts": c("posts"),
            "trades": c("trades"),
            "raw_pages": c("raw_pages"),
            "persons": c("persons"),
            "stock_mentions": c("stock_mentions"),
            "topics": c("topics"),
            "players": c("players"),
            "positions": c("positions"),
            # ---- 仍在日更的活数据 ----
            "news": c("news"),
            "stock_ann": c("stock_ann"),
            "lhb_stocks": c("lhb_stocks"),
            "lift_event": c("lift_event"),
            "self_pick": c("self_pick"),
            "douyin_corpus_video": c("douyin_corpus_video"),
            "overseas_news": c("overseas_news"),
            "overseas_hotspots": c("overseas_hotspots"),
        }

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    db = Database()
    print("DB OK", db.counts())
    db.close()
