"""轻量启发式分析：从帖子文本派生「讨论股票 / 人物 / 情绪 / 热点」。

诚实边界（重要）：
- 全部为基于规则的启发式，不依赖NLP模型，准确率有限，仅作浏览参考；
  所有命中保留原文（raw_text / context）供人工复核。
- 股票识别优先用 6 位 A 股代码；中文名称识别需配合 code→name 映射，
  无映射时以代码展示。
- 人物 = 帖子作者 + 正文中的 @提及；不抓取任何画像字段。
- 情绪 = 正负向词库打分（简单加权重），分为 看多/看空/中性。
- 热点 = 中文 2~4 字 n-gram 词频（剔除停用词），取 Top N。
"""
from __future__ import annotations

import re
from collections import Counter

# 6 位 A 股代码（沪市 60/68 开头，深市 00/30 开头，北交所 43/83/87/92 开头）。
#
# 2026-09-26 修正（P0，影响「当前讨论股票」「舆论情绪」两个板块的数据量）：
#   原实现是 r"\b(60\d{4}|68\d{4}|00\d{4}|30\d{4})\b"。
#   Python 的 \b 是 **Unicode 词边界**，汉字本身属于"词字符"，
#   于是论坛里最常见的两种写法都匹配不到：
#       ① 代码紧跟汉字、中间无空格：「宁德时代300750」「茅台600519今天…」
#       ② 代码出现在中文标点旁：「（600519）」「600519、」
#   实测 1059 条帖子：旧正则仅命中 3 条、6 个代码；改为前后「非数字」断言后
#   命中 129 条、30 个代码（40 倍）。这才是「当前讨论股票」长期只有两三只股票的根因
#   —— 帖子是抓到了的，是"从帖子里抽代码"这一步失效了。
#   注：scraper/parsers/{eastmoney,ths}.py 用的本来就是 (?<![0-9])…(?![0-9])，
#   本次修正只是把这个正确写法同步到分析层，未引入新约定。
CODE_RE = re.compile(r"(?<!\d)(?:60|68|00|30|43|83|87|92)\d{4}(?!\d)")
AT_RE = re.compile(r"@([\u4e00-\u9fa5A-Za-z0-9_]{2,20})")

# ==================== 多空情绪词表（全站唯一真源，2026-10-02 统一）====================
# 背景：此前有两把「不一样」的尺子——
#   ① 本文件的老 `score_sentiment`：正负词**简单计数**，>0 看多 / <0 看空，且把「跌/亏/崩/弱/牛/赚」
#      这类**单字**也入表；ingest.py 入库时用它，posts.sentiment 与 predictor 的「论坛情绪」信号都依赖它。
#   ② 根目录 postdigest.py 的新加权词表（权重 + 否定式 + 强度词 + 分级判词）。
# 实测近 7 天 3067 帖：两把尺子结论**相反 535 帖（17.4%）**，错在两处典型：
#   「宁德时代跌不动，估值已经很便宜」→ 老尺子只看「跌」判**看空**（反了），新尺子先抹否定式判中性/看多；
#   「生意模式思考：对标【喜诗糖果】…」→ 单字「涨/跌」乱入，中性帖被打上方向。
# 统一方案：词表与打分下沉到本文件，postdigest / ingest / predictor 全部复用同一份，
#           以后改词表只改一处，不会再出现「同一个票两个地方结论相反」。
# 权重约定：1.0 = 方向明确强信号；0.5 = 弱信号（顺口提一句"风险/谨慎"不足以判看空）。
# 否定式**先抹再打分**：「跌不动 / 不跌 / 跌透了」是看多或观望，不是看空。
# 单字不入表：宁可判「中性」也不乱贴标签（项目既有纪律，见 postdigest 文档串）。
POS_W: dict[str, float] = {
    "涨停": 1.0, "看多": 1.0, "看好": 1.0, "买入": 1.0, "加仓": 1.0, "建仓": 1.0,
    "做多": 1.0, "抄底": 1.0, "突破": 1.0, "上攻": 1.0, "拉升": 1.0, "强势": 1.0,
    "主升": 1.0, "连板": 1.0, "封板": 1.0, "利好": 1.0, "爆发": 1.0, "大涨": 1.0,
    "上涨": 1.0, "新高": 1.0, "走强": 1.0, "反转": 1.0, "抢筹": 1.0, "满仓": 1.0,
    "长线持有": 1.0, "加速": 1.0, "拐点": 1.0, "回本": 0.5, "翻盘": 0.5, "便宜": 0.5,
    "机会": 0.5, "布局": 0.5, "低吸": 0.5, "目标价": 0.5, "推荐": 0.5, "增持": 0.5,
    "持有": 0.5, "牛": 0.5, "修复": 0.5, "低估": 0.5,
}
NEG_W: dict[str, float] = {
    "跌停": 1.0, "看空": 1.0, "割肉": 1.0, "套牢": 1.0, "清仓": 1.0, "崩盘": 1.0,
    "爆雷": 1.0, "退潮": 1.0, "杀跌": 1.0, "破位": 1.0, "出货": 1.0, "止损": 1.0,
    "被套": 1.0, "跑路": 1.0, "退市": 1.0, "腰斩": 1.0, "一字跌": 1.0, "大亏": 1.0,
    "亏惨": 1.0, "不看好": 1.0, "大跌": 1.0, "暴跌": 1.0, "减仓": 1.0,
    "下跌": 0.5, "走弱": 0.5, "回落": 0.5, "回调": 0.5, "亏钱": 0.5, "亏损": 0.5,
    "惨": 0.5, "高位": 0.5, "谨慎": 0.5, "观望": 0.5, "兑现": 0.5, "风险": 0.5,
}
# 强度词：命中则放大（一条"非常看多"不该和一句"看多"打平）
INTENSIFIERS = ("非常", "特别", "超级", "极度", "大幅", "巨", "猛", "强烈", "重仓", "满仓", "爆")
# 否定式 / 反讽式：先抹掉，免得「跌不动」「不跌」「不怕跌」被算成看空
_NEUTRAL_PAT = re.compile(
    r"跌不动|跌不下去|跌不下来|跌不到|跌透了|跌够了|不跌了|不跌|不怕跌|不会跌|算不上跌"
)
# 判方向所需的最小净分绝对值：一个强词(1.0) 或 两个弱词(0.5+0.5) 才够，单个弱词仍判中性
_SENT_THRESHOLD = 0.5
# 兼容旧调用方的常量名（老代码/脚本里出现过 POS_WORDS / NEG_WORDS 的引用）
POS_WORDS = list(POS_W)
NEG_WORDS = list(NEG_W)


def sentiment_from_score(s: float) -> str:
    """净分 → 看多 / 看空 / 中性。**判方向的门槛也在这里**，别在别处自己写 >=0.5，
    否则会出现「一个弱词(正好 0.5)在 A 处判看多、在 B 处判中性」这种差一格的裂缝
    （2026-10-02 实测就差在等号上，3067 帖里 187 条对不上）。"""
    if s >= _SENT_THRESHOLD:
        return "看多"
    if s <= -_SENT_THRESHOLD:
        return "看空"
    return "中性"


def _sent_score(text: str) -> float:
    """帖子正文 → 多空净分（>0 偏向看多，<0 偏向看空，0 中性）。**全站共用这一份打分**。"""
    if not text:
        return 0.0
    t = _NEUTRAL_PAT.sub(" ", text)
    s = 0.0
    for w, wt in POS_W.items():
        c = t.count(w)
        if c:
            s += wt * c
    for w, wt in NEG_W.items():
        c = t.count(w)
        if c:
            s -= wt * c
    if any(i in t for i in INTENSIFIERS):
        s *= 1.5
    return s

STOPWORDS = set(
    "的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 及 或 也 都 就 还 把 被 让 给 对 从 到 为 以 "
    "啊 吧 呢 吗 哦 嗯 哈 呀 么 个 不 没 别 要 会 能 可 该 应 已 将 又 再 很 太 更 最 多 少 "
    "今天 昨天 明天 现在 目前 其实 但是 因为 所以 如果 的话 我们 你们 他们 自己 什么 怎么 这个 "
    "那个 一个 一些 一直 已经 可以 应该 可能 觉得 认为 然后 之后 之前 这样 那样 这种 那种 不是 "
    "就是 还是 但是 不过 而且 并且 以及 比如 例如 进行 通过 关于 对于 根据 按照 由于 成为 包括 "
    "股票 个股 盘面 市场 大盘 指数 板块 资金 成交 换手 量能 走势 行情 盘中 尾盘 早盘 复盘 操作 "
    "建议 大家 老师 朋友 同学 同事 各位 朋友 兄弟 姐妹 老师 粉丝 关注 点赞 转发 评论 收藏".split()
)


def score_sentiment(text: str) -> tuple[str, float]:
    """帖子正文 → (看多/看空/中性, 净分)。

    2026-10-02 起与 postdigest 共用同一套 _sent_score（加权词表 + 否定式 + 强度词），
    ingest 入库、predictor「论坛情绪」信号、帖子情绪速览 结论 三处因此不再是两把尺子。
    """
    if not text:
        return "中性", 0.0
    s = _sent_score(text)
    return sentiment_from_score(s), round(s, 1)


# 中文名 → 代码（2026-10-01，BUG 报告 P0「股票名称抽取覆盖率仅 27%」）
# 沿用至今的 extract_codes 只认 6 位代码，"买茅台 / 看宁德时代 / 美的不错"这类中文名
# 一条都抽不出来 —— 报告实测 3067 帖只有 27% 能出票（70 只），净漏 151 只热门股。
# 补一条中文名通道：用 stock_names 的权威名字索引（内置表 + stock_names 缓存表 +
# 龙虎榜证券简称）拼成长名优先的交替正则，一次 findall 扫完全文。
#   ① 长名优先排序：re 交替取最左分支，同位置先试长名，避免「中国」抢在「中国平安」前；
#   ② 2 字名也要收（「买茅台」是论坛最常见的写法），靠"长名优先 + 前后非字母数字"挡噪音；
#   ③ 索引只构建一次（模块级缓存），几千条帖子的全量回填也就几秒。
_NAME_RE = None
_NAME_IDX: dict | None = None


def _name_index() -> dict:
    """名字 → 代码 索引（**必须带 db**）。

    2026-10-01 实测：不带 db 时 `_known_name_index(None)` 只回 83 条内置知名股
    （贵州茅台/宁德时代…），中文名通道等于摆设；带 db 才会并上 stock_names 缓存表
    （全 A 5000+ 条）与龙虎榜证券简称，覆盖率才上得去。
    模块级缓存，3067 帖全量回填只建一次连接。
    """
    global _NAME_IDX
    if _NAME_IDX is not None:
        return _NAME_IDX
    idx: dict = {}
    try:
        from viewer.db import Database
        from viewer.stock_names import _known_name_index
        idx = dict(_known_name_index(Database()) or {})
    except Exception:  # noqa: BLE001
        idx = {}
    # 内置 83 条兜底：即使本地库暂时打不开，也别让中文名通道整条空掉
    try:
        from viewer.stock_names import _known_name_index as _kn
        for k, v in (_kn(None) or {}).items():
            idx.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    _add_short_aliases(idx)
    _NAME_IDX = idx
    return idx


_ALIAS_BLOCK = None


def _build_alias_blocklist() -> set:
    """派生简称时的**通用词黑名单**。

    不设这道闸，去前缀会把「招商银行」→「银行」、「今天科技」→「今天」这类
    大路词当成股票名，核实测「银行/股份/科技」全都被判成了票（索引从 5590 涨到
    20305 条，其中大半是噪音），「当前讨论股票」会被一堆伪票污染。
    """
    ws = """科技 电子 信息 网络 软件 数据 智能 能源 电力 环保 生物 医药 医疗 化工 材料
    金属 矿业 资源 建设 工程 装备 机械 仪器 汽车 摩托 车业 食品 酒业 酒 饮料 农业 种业
    养殖 牧业 林业 服饰 纺织 服装 家居 家具 建材 水泥 玻璃 陶瓷 造纸 包装 印刷 物流
    商贸 贸易 实业 发展 投资 资本 金融 证券 保险 信托 租赁 地产 置业 物业 文旅 影视
    传媒 出版 教育 培训 供应 股份 集团 控股 实业 产业 工业 商业 贸易 国际 环球 东方
    南方 北方 西部 中华 中国 华夏 京 沪 深 港 台 海 江 河 山 川 广 东 西 南 北 中
    银行 保险 证券 信托 期货 基金 债 债卷 股份 有限 公司 企业 工厂 园区 中心 中心
    通用 大众 大众 民族 国民 全民 全球 时代 世纪 未来 天天 今日 今日 明天 昨天
    高兴 快乐 今天 昨日 时候 时期 时间 空间 生活 世界 亚洲 欧洲 地球 国家 城市
    商品 市场 股东 董事 系统 平台 项目 计划 报告 资料 文件 内容 情况 问题 原因
    结果 过程 状态 消息 新闻 公告 提示 帮忙 一定 支持 继续 结束 开始 出现 存在
    需要 应该 可以 质量 数量 水平 水平 部门 单位 组织 个人 朋友 老师 同学 兄弟
    大连 友谊 证券 期货 基金 资本 资产 现金 利润 成本 价格 价值 金额 数目 大连
    海洋 江河 湖泊 沙漠 森林 草原 平原 高原 海峡 港湾 岛屿 群岛 港口 车站 机场""".split()
    return {w for w in ws if len(w) >= 2}


def _add_short_aliases(idx: dict) -> None:
    """从交易所全称派生「民间简称」：『贵州茅台』→茅台、『中国平安』→平安、『美的集团』→美的。

    为什么必须这一步：stock_names 表里**一个 2 字名都没有**（交易所只发 3~4 字全称），
    而论坛最常写的恰恰是简称（"买茅台"、"加仓平安"）。不派生的话中文名通道只认全称，
    命中率根本上不去 —— 加之前实测只有 85 只票，派生后能上到 200 只量级。
    唯一性卡口：候选必须**在全表只对应这一只票**才采纳，「银行 / 股份 / 科技」这类
    大路词因 owners 不唯一被挡掉，不会污染「当前讨论股票」列表。
    """
    name_codes: dict[str, set] = {}
    by_code: dict[str, set] = {}
    for n, c in idx.items():
        name_codes.setdefault(n, set()).add(c)
        by_code.setdefault(c, set()).add(n)
    global _ALIAS_BLOCK
    if _ALIAS_BLOCK is None:
        _ALIAS_BLOCK = _build_alias_blocklist()
    for code, names in by_code.items():
        for raw in names:
            n = (raw or "").strip()
            if len(n) < 3:
                continue
            # 两个方向都试，缺一个都不行：
            #   去前 k 字「贵州茅台」→（k=2）茅台；去后 k 字「美的集团」→（k=2）美的。
            # 噪音（「今天科技」→「今天」）由 _ALIAS_BLOCK 黑名单统一挡掉。
            cands = []
            for k in (1, 2):
                if len(n) - k >= 2:
                    cands.append(n[k:])            # 去前缀
                    cands.append(n[:len(n) - k])   # 去后缀
            for c in cands:
                c = (c or "").strip()
                if len(c) < 2 or c == n or c in idx or c in _ALIAS_BLOCK:
                    continue
                owners = name_codes.get(c)
                if owners is None:
                    # 全新名字，没有任何歧义 —— 采纳（"贵州茅台"→"茅台"）
                    idx[c] = code
                    name_codes.setdefault(c, set()).add(code)
                elif owners == {code}:
                    # 已在表里且本来就指向这一只，无害
                    continue
                # owners 含多个 code（"银行"/"股份"这类大路词）→ 丢弃，不污染讨论列表


def _name_re():
    global _NAME_RE
    if _NAME_RE is not None:
        return _NAME_RE
    idx = _name_index()
    names = {n.strip() for n in idx if isinstance(n, str) and len(n.strip()) >= 2}
    if not names:
        _NAME_RE = None
        return None
    pat = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    _NAME_RE = re.compile(r"(?<![A-Za-z0-9])(?:" + pat + r")(?![A-Za-z0-9])")
    return _NAME_RE


def extract_codes(text: str, with_name: bool = True) -> list[str]:
    """帖子里提到哪些股票代码：6 位代码 + **中文名**（茅台 / 宁德时代 / 中国平安 …）。

    保序去重：代码在前（原口径不变），中文名在后补漏。
    """
    t = text or ""
    out: list[str] = list(CODE_RE.findall(t))
    if with_name:
        rx = _name_re()
        if rx is not None:
            try:
                idx = _name_index()
            except Exception:  # noqa: BLE001
                idx = {}
            for nm in rx.findall(t):
                c = idx.get(nm)
                if c and c not in out:
                    out.append(c)
    seen, res = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            res.append(c)
    return res


def extract_mentions(text: str) -> list[str]:
    return AT_RE.findall(text or "")


# 讨论热点关键词表（A 股社区常见话题词）。
# 原实现用「字符级 2~4 字 n-gram 词频」会把「当前」「我的」「的判」这类切分碎片当成话题
# （见 viewer/db.py:migrate_dedupe 对 topics 的清理），属解析器 bug。
# 改为基于关键词词典的命中统计：权重 = 命中该关键词的帖子数，与 demo_data 中
# 「热度=命中该词的帖子数」口径一致；同时保持纯标准库实现（不引入 jieba）。
_TOPIC_KEYWORDS = [
    "看多", "看空", "中性", "突破", "量能", "回踩", "主升", "套牢", "割肉", "分红",
    "仓位", "异动", "龙头", "涨停", "跌停", "建仓", "加仓", "减仓", "止盈", "止损",
    "趋势", "主力", "资金", "题材", "机器人", "新能源", "降准", "平台", "板块", "情绪",
    "价值", "成长", "周期", "红利", "波段", "日内", "埋伏", "赛道", "景气", "逆向",
    "量价", "龙虎", "席位", "打新", "转债", "期指", "港股", "美股", "北向", "游资",
    "反包", "二波", "首板", "连板", "分歧", "一致", "AI", "芯片", "半导体", "医药",
    "消费", "金融", "地产", "军工", "农业", "煤炭", "钢铁", "有色", "汽车", "光伏",
    "风电", "储能", "锂电", "白酒", "银行", "保险", "证券", "期货", "黄金", "原油",
    "创业板", "科创板", "沪指", "深成指", "成交额", "换手率", "支撑位", "压力位",
]


def extract_topics(texts: list[str], top_n: int = 20) -> list[tuple[str, int]]:
    cnt: Counter = Counter()
    for t in texts:
        t = t or ""
        for kw in _TOPIC_KEYWORDS:
            if kw in t:
                cnt[kw] += 1
    return cnt.most_common(top_n)


def code_name_lookup(symbol: str, trades: list[dict]) -> str:
    """从已解析交易里反查代码对应的名称。"""
    for tr in trades:
        if tr.get("symbol") == symbol and tr.get("symbol_name"):
            return tr["symbol_name"]
    return ""
