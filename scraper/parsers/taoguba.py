"""淘股吧（tgb.cn）解析器 —— 基于 2026-09-24 实测抓取的**真实 DOM** 重写。

实测结论（已逐页核对 raw_pages）：
- 站点已从 taoguba.com.cn 迁到 www.tgb.cn；`/user/<NAME>` 个人主页现已返回 **404**，
  不能再作为选手来源。真实可用页面为：
  · 首页 https://www.tgb.cn/            —— 右侧「周榜人气」(defaultContainerRight-weekUp-item)
                                          给出真实选手（昵称 / 粉丝数 / VIP 认证 / 博客链接 /blog/<id>）；
  · 实盘话题页 /talk/talkSeq/<N>          —— 帖子流（每篇 = topic-new 作者块 + topic-info 正文
                                          + topic-end 时间），内含真实买卖叙事。
- 帖子流 DOM（已验证）：
    <div class="topic-new"><a href="/blog/ID"><span>昵称</span></a></div>
    <div style="display:flex"><a href="/a/POSTID"><p class="topic-info">正文…</p></a></div>
    <div class="topic-end"><span> · N阅读数 · 09-24 15:54</span></div>
- 真实交易信号出现在 topic-info 正文里，形态包括：
    · 六位代码括号形式：特发信息(000070)尾盘买进 / 莲花控股(600186)出剩二手
    · 带市场前缀：$澳弘电子(sh605058)$ 配 “澳弘电子，B。华瓷股份，S。”
    · 动作+股票名：买入金健股份 / 持仓：大元泵业 / 哈药割了（名前动后）/ 南威软件 卖了

本解析器只解析 HTML，不发请求、不写库（与 eastmoney 解析器一致）。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from bs4 import BeautifulSoup

from .. import name_match as nm
from ..bot_filter import is_bot_author
from ..models import NewsItem, Trade
from .base import BaseParser


# 中文动作 -> 标准动作（覆盖淘股吧常用同义表达）
# 2026-09-29：补齐"补仓/半路/抄底/上车/进了/买"与"出掉/出货/出局/清掉/走了/下车/抛"等口语化动作，
# 与 ACTION_RE 保持一致——ACTION_RE 负责定位，本表负责给语义，缺失会导致该动作被整条跳过。
ACTION_MAP = {
    "买入": "BUY", "买进": "BUY", "建仓": "BUY", "加仓": "BUY", "低吸": "BUY", "扫板": "BUY",
    "打板": "BUY", "排板": "BUY", "补仓": "BUY", "半路": "BUY", "抄底": "BUY",
    "上车": "BUY", "进了": "BUY", "进去": "BUY", "冲进": "BUY", "买": "BUY", "B": "BUY",
    "卖出": "SELL", "卖": "SELL", "清仓": "SELL", "减仓": "SELL", "平仓": "SELL",
    "止盈": "SELL", "止损": "SELL", "割肉": "SELL", "割": "SELL", "抛": "SELL",
    "出掉": "SELL", "出货": "SELL", "出局": "SELL", "清掉": "SELL",
    "走了": "SELL", "下车": "SELL", "出": "SELL", "S": "SELL",
    "持仓": "HOLD", "持有": "HOLD",
}

# 用于"名前动后"与"名中含动作词"识别的动作关键词集合
ACTION_KEYWORDS = set(ACTION_MAP.keys())

# 股票名黑名单片段（命中即丢弃，避免把"卖出所有持仓""测试新买点"当成标的）
NAME_BLACKLIST = [
    "所有", "没问题", "今天", "早盘", "竞价", "昨天", "全部", "结果", "资金",
    "沽", "不出去", "测试", "亏了", "的资金", "继续开仓", "剩二手",
    "操作", "没有", "继续", "格局", "直接", "开盘", "跌到", "等开盘", "份开盘",
    "认错", "太大回撤", "月份合约", "太脆弱", "只保留", "的", "合约",
]
# 名字里一旦出现这些字符即截断（时间词/标点/连接词/动词/副词/量词等，几乎不会出现在股票名里）
NAME_BAD_CHARS = set(
    "的了也，。、：()（）【】[]$￥%早今昨直接退期A涨跌封停开盘认错回撤合约"
    "脆弱保留操作没有继续格局太大炸板上板主动地位后面所以剩买买卖卖皆所和"
)


class TaogubaParser(BaseParser):
    site_key = "taoguba"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "https://www.tgb.cn").rstrip("/")

    # ---------------- 时间解析 ----------------
    @staticmethod
    def _parse_time(s: str | None) -> datetime | None:
        if not s:
            return None
        s = s.strip()
        # 先抽取 "MM-DD HH:MM" / "YYYY-MM-DD HH:MM" 形态（topic-end 形如 "09-24 15:54"）
        m = re.search(r"(?:(\d{4})-)?(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", s)
        if m:
            y = int(m.group(1)) if m.group(1) else datetime.now().year
            try:
                return datetime(y, int(m.group(2)), int(m.group(3)),
                               int(m.group(4)), int(m.group(5)))
            except ValueError:
                return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                   "%Y/%m/%d %H:%M", "%m-%d %H:%M", "%m月%d日 %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    # ---------------- 帖子块迭代（作者/正文/链接/时间） ----------------
    def _iter_post_blocks(self, soup: BeautifulSoup):
        """从实盘话题页抽出每篇帖子：(author, body, post_url, post_time)。"""
        infos = soup.select("p.topic-info")
        if not infos:
            # 退化：没有帖子流结构（如首页）时，整页作为一篇（5 元组保持签名一致）
            yield ("", soup.get_text(" ", strip=True), "", "", None)
            return
        for info in infos:
            # 作者：向前找最近的 div.topic-new 内的 span
            author = ""
            node = info
            for _ in range(6):
                node = node.find_previous("div", class_="topic-new")
                if node is None:
                    break
                sp = node.select_one("span")
                if sp:
                    author = sp.get_text(strip=True)
                    break
            # 帖子链接：info 内的 <a href="/a/...">
            a = info.find_parent("a") or info.select_one("a[href*='/a/']")
            href = a.get("href", "") if a else ""
            post_url = href if href.startswith("http") else (self.base_url + href if href.startswith("/") else "")
            post_id = href.rsplit("/", 1)[-1] if href else ""
            # 时间：向后找最近的 div.topic-end 内的时间串
            tm = None
            end = info.find_next("div", class_="topic-end")
            if end:
                tm = self._parse_time(end.get_text(" ", strip=True))
            body = info.get_text(" ", strip=True)
            yield (author, body, post_url, post_id, tm)

    # ---------------- 工具：规范化股票名 ----------------
    # ⚠️ 遗留（2026-09-29 起不再用于交易抽取）：黑名单式"猜名"会把正文碎片当股票名。
    # 现全部走 scraper/name_match.py 的词典驱动匹配（命中真名才产出交易）。此函数仅供外部
    # 旧调用方兼容，勿在新规则里使用。
    @staticmethod
    def _clean_name(raw: str) -> str | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        # 形如 "买入农业的红棉" → 取最后一个"的"之后的片段（红棉）
        if "的" in raw:
            cand = raw.split("的")[-1]
            if 2 <= len(cand) <= 6:
                raw = cand
        # 截断首个坏字符
        cut = len(raw)
        for i, ch in enumerate(raw):
            if ch in NAME_BAD_CHARS:
                cut = i
                break
        raw = raw[:cut].strip()
        if not (2 <= len(raw) <= 6):
            return None
        if any(k in raw for k in ACTION_KEYWORDS):
            return None
        if any(b in raw for b in NAME_BLACKLIST):
            return None
        return raw

    @staticmethod
    def _action_from_window(text: str, start: int, end: int, name: str | None = None) -> str:
        """在代码附近找动作词，返回标准动作；找不到返回空。

        窗口口径（2026-09-29 放宽）：前面 20 字 / 后面 16 字——淘股吧叙事常用
        「神开股份(002278)早盘看到二次上冲，冲了进去」这种"代码在前、动作隔十几字"的写法。

        两条判定纪律（2026-09-29 补）：
        1) **取离代码最近的动作词**，而不是"窗口里最靠右的那个"——否则
           「…冲了进去，结果是高位横盘，估计又是出货的多」会把远处的"出货"扣到该股头上。
        2) 单字母 B/S 必须**紧跟在同一只股票名后**（"澳弘电子，S。"）才算数。否则
           「$众泰汽车(sz000980)$$澳弘电子(sh605058)$今天操作:众泰汽车，B。」里那个 B
           会被错扣到前一只票（澳弘电子）上，凭空造出 BUY。
        """
        lo, hi = max(0, start - 20), min(len(text), end + 16)
        window = text[lo:hi]
        best = None                                   # (距离, 动作)
        for kw in ACTION_KEYWORDS:
            for idx in (m.start() for m in re.finditer(re.escape(kw), window)):
                pos = lo + idx                        # 窗口内下标 → 全文下标
                kw_end = pos + len(kw)
                if pos >= end:
                    dist = pos - end                  # 关键字在代码之后
                elif kw_end <= start:
                    dist = start - kw_end             # 关键字在代码之前
                else:
                    dist = 0
                if kw in ("B", "S"):
                    if not name:
                        continue                      # 不知道标的，不用单字母判动作
                    head = text[max(0, pos - 12):pos]
                    m2 = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,8})[，,\s]*$", head)
                    if not m2 or nm.normalize(m2.group(1)) != nm.normalize(name):
                        continue                      # 这个 B/S 是另一只票的，跳过
                    dist = max(0, dist - 2)            # 紧邻标的的 B/S 视为最高优先
                if best is None or dist < best[0]:
                    best = (dist, ACTION_MAP.get(kw, ""))
        return best[1] if best else ""

    # 动作关键词（长词优先匹配，避免"买"误伤"买入"）
    ACTION_KW_LIST = sorted(ACTION_MAP.keys(), key=len, reverse=True)

    # 动作词正则（长词在前，避免"买"吞掉"买入"）。只用来**定位**，动作语义交给 ACTION_MAP。
    ACTION_RE = re.compile(
        r"买入|买进|补仓|建仓|加仓|低吸|扫板|打板|排板|半路|抄底|上车|冲进|进去|进了|持仓|持有|"
        r"卖出|清仓|减仓|平仓|止盈|止损|割肉|出掉|出货|出局|清掉|走了|下车|买|卖|割|出|抛"
    )

    # 规则④「名称 ↔ 动作词关联」的字符距离上限（2026-10-09 新增，治"一句多票"）。
    # 旧实现无上限（沿用 name_match.ASSOC_WINDOW=24 且按**名**认领），一个动作词会被
    # 半句内的多只股同时认领，把"提及/点名"也当成交易（"拉权重股出货，赶紧清仓" →
    # 同时 SELL 宁德 + SELL 茅台）。现改为：动作词 1:1 认领**最近 1 只**，且距离 ≤ 本上限。
    # 15 为实测初值（可调）；配合 associate_to_action 使用。
    ASSOC_MAX_GAP = 15

    # ---------------- 量价抽取（2026-10-05 新增，配合实测报告 P0-2） ----------------
    # 背景：实盘帖里「减仓到了 100 股」「果断卖出 300 手」「成本 12.30」这类量价信息，
    # 原先整库 price / quantity 全为 NULL（实测 268/268 条），明细只剩方向与标的，
    # 没法做盈亏 / 仓位复盘，selftest 也因此长期红着。
    # 设计原则：**只认带单位的显式写法，绝不猜裸数字**。
    # 理由不是保守，是实测出来的：样本里「跌到 1100 附近」「调整到 11.2 差不多了」
    # 这类**预测价**比真实成交价还多，裸数字匹配会把目标价当成交价写进库——
    # 那比留空更误导人，因为假数字看起来像真的。
    QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万股|千股|手|股|张)")
    # 价格形态①：价格词在前 —— 成交价12.30 / 成本 : 12.3 / @12.3 / 价12.3
    PRICE_RE = re.compile(
        r"(?:成交价|成本价|建仓价|加仓价|减仓价|均价|摊低成本|摊薄成本|买入价|卖出价|"
        r"价格|成本|价|@)\s*[:：=]?\s*(\d{1,4}(?:\.\d{1,3})?)"
    )
    # 价格形态②：数字带货币单位在后 —— "今天在 11.38 元买入平安银行"
    PRICE_NUM_RE = re.compile(r"(\d{1,4}(?:\.\d{1,3})?)\s*(?:元|块钱?)(?!\s*股)")
    # 预测/观点语境：这些词后面的数字是**目标价或观点价**，不是成交价，一律不抽。
    # 实测反例："中国国贸涨到200元是有投资逻辑" —— 200 是目标价，抽进"成交价"会直接误导。
    PRICE_CONTEXT_SKIP = (
        "涨到", "跌到", "涨至", "跌至", "看到", "目标", "预估", "预计", "打算",
        "如果", "一旦", "可能", "上方", "下方", "附近", "突破", "回踩", "冲", "看",
        "到", "预期", "值", "算", "块",
    )
    # 单位 → 股数换算（A 股 1 手 = 100 股）
    QTY_MULT = {"股": 1, "张": 1, "手": 100, "千股": 1000, "万股": 10000}

    def _extract_pq(self, text: str, anchor: int | None) -> tuple[float | None, int | None]:
        """在标的名附近的 ±60 字内抽 (price, 股数)。抽不到任一返回 None，绝不硬猜。

        三条实测教训（都来自库里 268 条真实原文）：
        1. **先找标的名「身后的」**：原文"割肉买入地产etf2手，津药药业6手"，
           只按距离取最近会把「2手」（地产etf 的）扣给津药。中文档位下量价多数在标的之后
           （"买入平安银行2000股"），身后没有才回溯身前（"今天在11.38元买入平安银行"）；
        2. 小数不能当股数（"0.33股息"会被 `0.33股` 命中成 0 股），故数量整数部分须 ≥ 1；
        3. 预测价不算成交价（"中国国贸涨到200元是有投资逻辑"），见 PRICE_CONTEXT_SKIP。
        """
        if not text or anchor is None:
            return None, None
        # 窗口：身后 22 字（中文档位量价通常紧贴标的后："买入平安银行2000股"）、
        # 身前 14 字（价格常在标的前："今天在11.38元买入平安银行"，取窄一点不伤）。
        # 实测反例：60 字窗口会把贴子末尾另一只票的量价（"…地产etf2手，津药药业6手"）
        # 扣给中间那只票，沈鼓/龙头都跟着误取到 600；收到 22/14 后三只各归各的。
        lo = max(0, anchor - 14)
        seg = text[lo:min(len(text), anchor + 22)]
        cut = anchor - lo                       # 标的名后缀在 seg 中的下标
        # 身后窗口必须**裁到下一只股票名之前**：原文"买入沈鼓同日买入龙头股份…
        # …上一交易日割肉买入地产etf2手，津药药业6手"，沈鼓与津药隔着一整个帖子，
        # 不裁的话尾巴会伸到别人家的量价上。裁完沈鼓身前身后都无 6 手 → 老老实实留空。
        tail_end = len(seg)
        for _n, _c, s2, _e2 in nm.scan_names(seg):
            if s2 >= cut and s2 < tail_end:
                tail_end = s2

        def pick(rx: re.Pattern, start: int, end: int):
            """在 seg[start:end] 内找离标的名最近的一个有效匹配。

            返回 (距离, 数值, 单位)；价格类返回单位 None。抽不到返回 None。
            """
            best = None
            for m in rx.finditer(seg, start, end):
                v = float(m.group(1))
                if rx is self.QTY_RE and v < 1:      # 0.33股息 这类不是数量
                    continue
                if rx is not self.QTY_RE:
                    if not (0.5 < v < 5000):        # A 股股价量级，越界视为误匹配
                        continue
                    if any(k in seg[max(0, m.start(1) - 4):m.start(1)]
                           for k in self.PRICE_CONTEXT_SKIP):
                        continue                    # 目标价 / 观点价，不是成交价
                d = abs(m.start(1) - cut)
                if best is None or d < best[0]:
                    best = (d, v, m.group(2) if rx is self.QTY_RE else None)
            return best

        # 身后优先 → 身前兜底；每种形态（价格词在前 / 数字+元在后）都试一遍
        def resolve(rxs):
            for start, end in ((cut, tail_end), (0, cut)):
                for rx in rxs:
                    got = pick(rx, start, end)
                    if got:
                        return got
            return None


        price = None
        for rx in (self.PRICE_RE, self.PRICE_NUM_RE):
            got = resolve((rx,))
            if got:
                price = round(got[1], 3)
                break
        qty = None
        got = resolve((self.QTY_RE,))
        if got:
            qty = int(got[1] * self.QTY_MULT[got[2]])
        return price, qty

    # ---------------- 单条正文 -> 交易记录（词典驱动，2026-09-29 重写） ----------------
    # 旧实现用「正则抓 2~6 汉字 + 黑名单」猜股票名，会把正文碎片当标的存进库：
    #   「第三次出发」→ SELL 第三次 ／「累计出金」→ SELL 累计 ／「超短线低吸全仓怼单标起」→ BUY 全仓怼单标起。
    # 这些词没有代码（前端点不开详情），且污染「选手实盘」面板。
    # 新实现：**只有命中股票名词典（scraper/name_match.py，约 5600 条真名）才产出记录**，
    # 命中即用词典代码补齐 symbol —— "名称+代码"永远成对、可点开。三层规则：
    #   ① 代码锚定（最高精度，正文自带 6 位代码）
    #   ② B/S 标记
    #   ③ 动作词邻域（前看/后看各取一名，跨过标点与连接字）
    #   ④ 名称 ↔ 最近动作词关联（补偿"名字在句首、动作在句尾"隔半句的叙事）
    def _extract_trades(self, text: str) -> list[dict]:
        trades: list[dict] = []
        if not text:
            return trades
        seen: set[tuple] = set()

        def add(action: str, name: str | None, code: str | None,
                ctx: str = "", anchor: int | None = None) -> None:
            if not action or not (name or code):
                return
            # P2（2026-10-10 r2，按 lead 纠正）：交易语境下**按"名称"命中指数 → 整行丢弃**。
            #   帖文原文写的是指数名（"科创50""上证指数"），谈的是指数点位/期权，不是个股买卖；
            #   改名保留（科创50→国城矿业）等于**凭空捏造一笔个股交易**，是更隐蔽的假阳性。
            #   仅当**按裸码**命中时（name 由 code_name 给出同码个股规范名、非指数）才保留 ——
            #   裸码 000688/000001 本身指的就是股票。
            #   指数、且该码无同码个股名 → 同样丢弃。
            if name and nm.is_index_name(name):
                return
            key = (action, code or name)
            if key in seen:
                return
            seen.add(key)
            price, qty = self._extract_pq(ctx, anchor)
            trades.append({"action": action, "symbol": code, "symbol_name": name,
                           "price": price, "quantity": qty})

        # ① 代码锚定（精度最高）：特发信息(000070) / $澳弘电子(sh605058)$ / 莲花控股(600186)
        #    动作词窗口放宽到 前 20 字 / 后 16 字——"神开股份(002278)早盘看到二次上冲，冲了进去"
        #    这类"代码在前、动作在十几字后"的叙事才不会漏。
        anchored: list[tuple[int, int]] = []      # 已被代码锚定覆盖的名字区间
        for m in re.finditer(r"(?:\$)?([\u4e00-\u9fa5A-Za-z]{0,8})?[（(](?:sh|sz|SH|SZ)?(\d{6})[)）]",
                             text):
            code, hint = m.group(2), m.group(1) or ""
            hit = nm.resolve(hint) if hint else None
            name = (hit or {}).get("name") or nm.code_name(code)
            action = self._action_from_window(text, m.start(), m.end(), name)
            if action:
                add(action, name, code, text, m.end())
            anchored.append((m.start(), m.end()))
            if hint:
                anchored.append((m.start() - len(hint), m.start()))

        # ①b 裸 6 位代码（2026-10-10 修 QA FAIL-1b）：正文只写"卖出600519"、不带括号。
        #     防误报三闸：① 必须命中已知 A 股代码集（stock_names_full.json）才认；
        #     ② 数字边界干净（不为更长数字 / 日期 / 小数片段）；③ 附近须有买卖动作词。
        known_codes = nm.all_codes()
        for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text):
            if any(m.start() < e and m.end() > s for s, e in anchored):
                continue                      # 已被括号式代码锚定覆盖，不重复
            if m.start() > 0 and text[m.start() - 1] == ".":
                continue                      # 小数/价格片段（如 12.600519）
            if (m.end() < len(text) and text[m.end()] == "."
                    and m.end() + 1 < len(text) and text[m.end() + 1].isdigit()):
                continue                      # "600519.12" 这类小数
            code = m.group(1)
            if code not in known_codes:
                continue                      # 非已知代码 → 不猜
            name = nm.code_name(code)
            if not name:
                continue
            action = self._action_from_window(text, m.start(), m.end(), name)
            if action:
                add(action, name, code, text, m.end())
            anchored.append((m.start(), m.end()))

        # ② B/S 标记：澳弘电子，B。 / 华瓷股份，S。
        for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z]{2,8})[，,]\s*([BS])\b", text):
            hit = nm.resolve(m.group(1))
            if not hit:
                continue
            add("BUY" if m.group(2) == "B" else "SELL", hit["name"], hit["code"],
                text, m.end(1))
            anchored.append((m.start(1), m.end(1)))

        # ③ 动作词邻域。两趟走，避免"一只票被两个动作抢"：
        #    第一趟「动后名前」（清仓 牧原股份 / 买入金健大米）——动作后紧跟的标的归它；
        #    第二趟「名前动后」（德石股份割了）——只认领第一趟没被收走的标的。
        #    ⚠️ 2026-10-10（QA FAIL-1a）：用一个 `used_actions` 记录**已被③认领过的动作词位置**，
        #       第二趟与规则④都据此排除 → 真正实现"1 个动作词最多 1 条"（此前③认领后④又认领第 2 只，
        #       "买入九阳股份/传艺科技/华北制药" 会被拆成 2 条）。
        actions: list[tuple[int, str]] = []
        consumed: list[tuple[int, int]] = [(s, e) for s, e in anchored]   # 已认领的名字区间
        used_actions: set[int] = set()        # 已被③消费的动作词起始位置（供 pass2 与④排除）
        for m in self.ACTION_RE.finditer(text):
            kw = m.group(0)
            actions.append((m.start(), kw))
            act = ACTION_MAP.get(kw, "")
            if not act:
                continue
            fwd = nm.match_after(text, m.end(), max_len=6)
            if fwd:
                add(act, fwd[0], fwd[1], text, fwd[3])
                consumed.append((fwd[2], fwd[3]))
                used_actions.add(m.start())
        for m in self.ACTION_RE.finditer(text):
            kw = m.group(0)
            act = ACTION_MAP.get(kw, "")
            if not act:
                continue
            if m.start() in used_actions:
                continue                      # 该动作词已在第一趟认领过标的（1 动作词 1 标的）
            back = nm.match_ending(text, m.start(), max_len=8)
            if not back:
                continue
            if any(back[2] < c_end and back[3] > c_start for c_start, c_end in consumed):
                continue                          # 该标的已有更精确的归属，不重复认领
            add(act, back[0], back[1], text, back[3])
            used_actions.add(m.start())

        # ④ 动作词 → 最近标的 关联（补偿隔半句的叙事："福建水泥…开盘冲高时出掉了"）。
        #    2026-10-09 改为**动作词 1:1 认领**：遍历动作词（而非遍历名），每个动作词
        #    只认领**最近的 1 只**未被收走的标的，且距离 ≤ ASSOC_MAX_GAP。
        #    这样根治了旧实现「一句动作词认领多只股」的 1:N 误报（详见类常量注释）。
        #    2026-10-10（QA FAIL-1a）：claimed_actions 以 used_actions 起手，跳过③已消费的动作词
        #    → 同一动作词不再跨③④被认领两次。
        names = nm.scan_names(text)

        def _consumed(ns: int, ne: int) -> bool:
            return any(ns < c_end and ne > c_start for c_start, c_end in consumed)

        claimed_actions: set[int] = set(used_actions)
        for apos, kw in actions:
            act = ACTION_MAP.get(kw, "")
            if act not in ("BUY", "SELL", "HOLD"):
                continue                      # 非买/卖/持（如"低吸"已含在上表）一律不作为交易
            if apos in claimed_actions:
                continue                      # 该动作词已被更近的标的认领，不再给第二只
            cand = nm.associate_to_action(
                text, apos, kw,
                [(n, c, s, e) for (n, c, s, e) in names if not _consumed(s, e)],
                window=self.ASSOC_MAX_GAP)
            if not cand:
                continue                      # 半句内没有名（或跨句）→ 这是"提及"，不入交易
            name, code, ns, ne = cand
            if _consumed(ns, ne):
                continue
            claimed_actions.add(apos)
            add(act, name, code, text, ne)

        return trades

    # ---------------- 资讯（首页热点异动股） ----------------
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        soup = BeautifulSoup(html, "lxml")
        base = self.base_url
        seen = set()
        # 首页"潜伏榜/异动"股票条目：defaultContainerRight-stock-item
        for el in soup.select("div.defaultContainerRight-stock-item"):
            a = el.select_one("a.defaultContainerRight-stock-name")
            if not a:
                continue
            name = a.get_text(strip=True)
            href = a.get("href", "")
            url = href if href.startswith("http") else (base + href if href.startswith("/") else page_url)
            rate = el.select_one("span.rise, .defaultContainerRight-stock-Rate")
            summary = (rate.get_text(strip=True) if rate else "") + " " + \
                      (el.select_one("a.defaultContainerRight-stock-7discuss").get_text(strip=True)
                       if el.select_one("a.defaultContainerRight-stock-7discuss") else "")
            if not name or name in seen:
                continue
            seen.add(name)
            yield NewsItem(
                source_site="taoguba",
                title=f"{name} 异动",
                url=url,
                publish_time=None,
                summary=summary.strip(),
            )

    # ---------------- 实盘交易 ----------------
    def parse_trades(self, html: str, page_url: str) -> Iterable[Trade]:
        soup = BeautifulSoup(html, "lxml")
        best: dict[tuple, Trade] = {}
        for author, body, post_url, post_id, ptime in self._iter_post_blocks(soup):
            if not body:
                continue
            if is_bot_author(author)[0]:
                continue                      # 机器资讯号：其内容不进交易统计（2026-10-09 A）
            for t in self._extract_trades(body):
                key = (author, t["action"], t.get("symbol") or t.get("symbol_name") or "")
                if key in best:
                    continue
                best[key] = Trade(
                    source_site="taoguba",
                    author=author or "unknown",
                    post_id=post_id or page_url,
                    post_url=post_url or page_url,
                    post_time=ptime,  # 解析不到真实发帖时间则留空（原先 `or datetime.now()` 会把抓取时刻当发帖时间，2026-09-25 修复）
                    action=t["action"],
                    symbol=t.get("symbol"),
                    symbol_name=t.get("symbol_name"),
                    price=t.get("price"),
                    quantity=t.get("quantity"),
                    raw_text=body[:500],
                    buy_time=ptime,
                    buy_price=t.get("price"),
                    current_price=None,
                    access_level="free",
                    access_hint="",
                )
        yield from best.values()

    # ---------------- 通用帖子（讨论视图） ----------------
    def parse_posts(self, html: str, page_url: str) -> Iterable[dict]:
        soup = BeautifulSoup(html, "lxml")
        out = []
        for author, body, post_url, post_id, ptime in self._iter_post_blocks(soup):
            if not body or not post_id:
                continue
            out.append({
                "author": author or "unknown",
                "post_id": post_id,
                "post_url": post_url or page_url,
                "post_time": ptime.strftime("%Y-%m-%d %H:%M:%S") if ptime else "",
                "text": body,
            })
        # 去重（同一帖子可能因结构重复）
        seen = set()
        for p in out:
            k = (p["post_id"], p["author"])
            if k in seen:
                continue
            seen.add(k)
            yield p

    # ---------------- 选手排行榜（真实来源：首页周榜 + 话题发起人） ----------------
    def parse_players(self, html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        out: list[dict] = []
        seen: set[str] = set()

        def add(name, source_url, fans=None, is_certified=0, ranking_score=None):
            name = (name or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            out.append({
                "name": name,
                "rank_position": None,
                "ranking_score": ranking_score if ranking_score is not None else (fans if fans is not None else None),
                "source_url": source_url,
                "is_certified": 1 if is_certified else 0,
                "tags": [],
            })

        # ① 首页「周榜人气」选手卡
        for el in soup.select("div.defaultContainerRight-weekUp-item"):
            a = el.select_one("a.defaultContainerRight-weekUp-userName")
            if not a:
                continue
            name = a.get_text(strip=True)
            href = a.get("href", "")
            url = href if href.startswith("http") else (self.base_url + href if href.startswith("/") else page_url)
            fans_el = el.select_one("span.defaultContainerRight-weekUp-fensi")
            fans = None
            if fans_el:
                fm = re.search(r"(\d+)", fans_el.get_text(strip=True))
                if fm:
                    fans = int(fm.group(1))
            vip = 1 if el.select_one("img.defaultContainerRight-weekUp-vip") else 0
            add(name, url, fans=fans, is_certified=vip, ranking_score=fans)

        # ② 话题发起人 / 帖子作者（/blog/<id> 链接）
        for el in soup.select("div.topic-new"):
            a = el.select_one("a[href*='/blog/']")
            if not a:
                continue
            sp = el.select_one("span")
            name = sp.get_text(strip=True) if sp else ""
            href = a.get("href", "")
            url = href if href.startswith("http") else (self.base_url + href if href.startswith("/") else page_url)
            add(name, url)

        return out

    # ---------------- 持仓比例（当前真实页不含结构化持仓，留空） ----------------
    def parse_positions(self, html: str, page_url: str, player: str = "") -> list[dict]:
        return []
