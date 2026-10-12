"""股票名匹配工具：词典驱动的「正文 → 股票名 + 代码」抽取（纯离线，不发请求）。

存在意义（2026-09-29 修复王总反馈的"标的点不开/显示文本碎片"）：
    旧解析器用「正则抓 2~6 个汉字 + 黑名单过滤」的办法猜股票名，遇到
    「第三次出发」（→ SELL 第三次）、「超短线低吸全仓怼单标起」（→ BUY 全仓怼单标起）、
    「累计出金」（→ SELL 累计）就会把**正文碎片当股票名**入库：既没有代码（点不开详情），
    又污染「选手实盘」面板。

    新策略 = 词典驱动（最左最长匹配）：
      1) 先把文本与词典都规范化（全角字母→半角、去空白）；
      2) 在正文里做最长匹配，**只在命中真实股票名时**才产出交易；
      3) 命中即用词典里的代码补齐 symbol（名字→代码），前端"名称+代码"因此可点开；
      4) 允许跳过 ST/XD/XR/DR/N/C 等名称前缀（"XD中国平安" → 中国平安）。

词典来源：scraper/stock_names_full.json（由 tools/export_stock_name_index.py 从库里导出，
约 5600 条：内置表 + stock_names 缓存 + 龙虎榜证券简称），并以 stock_name_map.json 兜底。
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_INDEX_PATH = _DIR / "stock_names_full.json"
_EXTRA_PATH = _DIR / "stock_name_map.json"

_FULLWIDTH = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz",
)

# 名称前缀：这些前缀出现在股票名前（交易所对除权/风险股加注），匹配时允许跳过
MARKET_PREFIX = ("*ST", "ST", "XD", "XR", "DR", "N", "C", "S")

# 明显不是股票名的短语（词典已能挡掉绝大多数，这里再兜一层"动作+口语"误配）
STOP_WORDS = {
    "全仓", "半仓", "清仓", "空仓", "重仓", "轻仓", "仓位", "今天", "明天", "昨天",
    "早盘", "尾盘", "盘中", "竞价", "开盘", "收盘", "涨停", "跌停", "地板", "天花板",
}

_index: dict[str, str] | None = None
_code2name: dict[str, str] | None = None
_all_codes: frozenset[str] | None = None
_maxlen: int = 0

# 代码 → 规范名 的「脏别名」过滤（2026-10-10 修 QA FAIL-1c）。
# stock_names_full.json 里同一代码可能挂多条别名，且脏别名（"为投资贵州茅台""提供贵州茅台"
# "上证指数"…）排在真名之前，`setdefault` 会取到首个 → 前端/派生字段显示脏名。
# 这里按「干净优先 → 短名优先 → 出现顺序兜底」挑规范名。
_DIRTY_NAME_MARKERS = (
    "为投资", "提供", "汇总", "解读", "报告", "聚焦", "关注", "布局", "点评",
    "看好", "重仓", "持有", "买入", "卖出", "增持", "减持", "指数",
)

# 指数名集合（2026-10-10 修 P2：6 位码「个股 vs 指数」撞车）。
# 交易语境下应**排除指数条目、优先取同码的个股名**：
#   000001 上证指数(SH) / 平安银行(SZ)；000688 科创50(SH) / 国城矿业(SZ)。
# 其余末字非"指数/成指/综指"的指数（如 科创50/沪深300/上证50）在此显式列出。
INDEX_NAMES = frozenset({
    "科创50", "科创100", "科创200", "科创综指", "科创板50", "科创创业50", "双创50",
    "沪深300", "中证500", "中证1000", "中证2000", "中证100", "中证A50", "中证A500",
    "中证红利", "中证消费", "中证医疗", "中证军工", "中证白酒",
    "上证50", "上证180", "上证380", "上证指数", "深证成指", "深证100", "深证300",
    "创业板指", "创业板50", "北证50", "国证2000", "国证1000",
})
_INDEX_SUFFIX = ("指数", "成指", "综指")


def is_index_name(nm: str) -> bool:
    """是否为指数名（交易语境下应排除，见 INDEX_NAMES 注释）。"""
    n = normalize(nm)
    return n in INDEX_NAMES or any(s in n for s in _INDEX_SUFFIX)



def normalize(s: str) -> str:
    """统一形态：全角字母→半角、去空白与首尾标点。词典与正文两侧都过一遍。"""
    return "".join(ch for ch in (s or "").translate(_FULLWIDTH) if ch.strip())


def index() -> dict[str, str]:
    """名称 → 代码（懒加载，进程内只读一次）。"""
    global _index, _maxlen
    if _index is None:
        idx: dict[str, str] = {}
        for path in (_INDEX_PATH, _EXTRA_PATH):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 缺文件/坏文件都静默
                continue
            for nm, code in data.items():
                n = normalize(nm)
                c = str(code).strip()
                if len(n) >= 2 and c.isdigit() and len(c) == 6:
                    idx.setdefault(n, c)
        _index = idx
        _maxlen = max((len(k) for k in idx), default=4)
    return _index


def _name_rank(nm: str, order: int) -> tuple:
    """规范名排序键：脏别名/指数名排后，短名优先，最后按出现顺序兜底。"""
    dirty = 1 if (any(mk in nm for mk in _DIRTY_NAME_MARKERS) or is_index_name(nm)) else 0
    return (dirty, len(nm), order)


def code_name(code: str) -> str | None:
    """代码 → **规范名**（用于「代码锚定」时把裸代码补成"名称+代码"）。

    2026-10-10（QA FAIL-1c）：同一代码挂多条别名时取**规范名**而非"首个"——
    过滤含修饰词的脏别名（为投资/提供/汇总/解读…）与指数名，短名优先。
    断言：600519→贵州茅台、300750→宁德时代、601318→中国平安、000001→平安银行。
    """
    global _code2name
    if _code2name is None:
        rev: dict[str, str] = {}
        rank: dict[str, tuple] = {}
        for order, (nm, c) in enumerate(index().items()):
            r = _name_rank(nm, order)
            if c not in rank or r < rank[c]:
                rev[c] = nm
                rank[c] = r
        _code2name = rev
    return _code2name.get(str(code).strip())


def all_codes() -> frozenset[str]:
    """已知 A 股代码全集（供「裸 6 位代码」锚定校验，2026-10-10 修 QA FAIL-1b）。"""
    global _all_codes
    if _all_codes is None:
        _all_codes = frozenset(index().values())
    return _all_codes


def max_name_len() -> int:
    index()
    return _maxlen


def is_known(name: str) -> bool:
    return normalize(name) in index()


def _skip_prefix(text: str, i: int) -> int:
    """跳过紧跟 i 的名称前缀（ST/XD/…）与空白；返回新的起始下标。"""
    j = i
    for _ in range(2):                      # 最多叠两个前缀（如 *ST、XD*ST）
        for p in MARKET_PREFIX:
            if text[j:j + len(p)].upper() == p:
                j += len(p)
                break
        else:
            break
        while j < len(text) and text[j] in " \u3000":
            j += 1
    return j


def match_at(text: str, i: int, max_len: int | None = None) -> tuple[str, str, int, int] | None:
    """从 text[i] 起做「最左最长」词典匹配（允许跳过 ST/XD 等前缀）。

    返回 (标准名, 代码, 起始下标, 结束下标)；未命中返回 None。
    只会匹配**从 i 附近开始**的名字，不做任意位置滑窗，避免"名字出现在句子中间"被误配。
    """
    idx = index()
    j = _skip_prefix(text, i)
    if j >= len(text):
        return None
    cap = max_len or max_name_len()
    upper = min(len(text) - j, cap)
    for ln in range(upper, 1, -1):
        cand = normalize(text[j:j + ln])
        if cand in idx:
            end = j + ln
            # 规范化会去掉空格，故按"规范化后长度"回推真实起点（供重叠判定用）
            return cand, idx[cand], end - len(cand), end
    return None


def match_ending(text: str, end: int, max_len: int | None = None) -> tuple[str, str, int, int] | None:
    """在 text[:end] 的**末尾**做最长词典匹配（用于「名前动后」：德石股份割了）。

    返回 (标准名, 代码, 起始下标, 结束下标)；未命中返回 None。
    """
    idx = index()
    lo = max(0, end - (max_len or max_name_len()))
    for ln in range(end - lo, 1, -1):
        cand = normalize(text[end - ln:end])
        if cand in idx:
            return cand, idx[cand], end - ln, end
    return None


# 名字与动作词之间允许出现的连接字/标点（"买了 德石股份"、"清仓：牧原股份"）
_GAP_CHARS = " \u3000\t的了在是和与、，,。.:：;；!！?？()（）$【】“”\"'"
# 关联窗口：名字与动作词相隔多远仍认为是同一笔操作（超短叙事里半句之内都算）
ASSOC_WINDOW = 24
# 关联阻断符：名字与动作词之间若跨了句子终止符，就不再认为是同一笔（防跨句误配）
ASSOC_BLOCK = "。；;\n"


def _skip_gap(text: str, i: int, limit: int = 3) -> int:
    """跨过最多 limit 个连接字/标点，返回新的起始下标。"""
    j = i
    n = 0
    while j < len(text) and n < limit and text[j] in _GAP_CHARS:
        j += 1
        n += 1
    return j


def match_after(text: str, i: int, max_len: int = 6) -> tuple[str, str, int, int] | None:
    """从 text[i] 向后找标的（允许先跨过 1~3 个连接字/标点，再跳 ST/XD 前缀）。

    返回 (标准名, 代码, 起始下标, 结束下标)。
    用于「动后名前」：买入金健股份 / 清仓 牧原股份 / 卖 了 德石股份。
    精确匹配失败时，对紧随其后的 2~6 字做「去尾/唯一前缀」解析（金健大米 → 金健米业）；
    仍有歧义不猜。
    """
    j = _skip_gap(text, i)
    hit = match_at(text, j, max_len=max_len)
    if hit:
        return hit
    j2 = _skip_prefix(text, j)
    tail = resolve(text[j2:j2 + 6])
    if tail:
        return tail["name"], tail["code"], j2, j2 + len(tail["name"])
    return None


def associate(text: str, name_start: int, name_end: int,
              action_positions: list[tuple[int, str]], window: int = ASSOC_WINDOW
              ) -> tuple[int, str] | None:
    """给一个已命中的股票名，找它**最近**的动作词（前 window 字内 / 后 window 字内）。

    用于补偿「名字在句首、动作在句尾」的叙事（"福建水泥跌停开盘…开盘冲高时出掉了"）。
    若名字与动作词之间跨了句号/分号（不同句子），视为无关，不关联。
    返回 (动作词起始下标, 动作词)；找不到返回 None。
    """
    best: tuple[int, str] | None = None
    best_gap = window + 1
    for pos, kw in action_positions:
        if pos >= name_end:
            gap = pos - name_end
            between = text[name_end:pos]
        elif pos + len(kw) <= name_start:
            gap = name_start - (pos + len(kw))
            between = text[pos + len(kw):name_start]
        else:
            continue                     # 与名字重叠，交给邻域规则处理
        if gap > window or gap >= best_gap:
            continue
        if any(bc in between for bc in ASSOC_BLOCK):
            continue                     # 跨句不算同笔操作
        best, best_gap = (pos, kw), gap
    return best


def associate_to_action(text: str, apos: int, kw: str,
                        names: list[tuple[str, str, int, int]],
                        window: int = ASSOC_WINDOW,
                        block: str = ASSOC_BLOCK) -> tuple[str, str, int, int] | None:
    """锚点从「名」换成「动作词」：给一个动作词，找它**最近的 1 个**标的名。

    这是「一句多票」的根治手段（2026-10-09）：旧流程按**名**逐个去认领动作词，
    于是一个动作词会被它附近的多只股同时认领（"拉权重股出货，赶紧清仓" →
    同时把宁德、茅台都判成 SELL）。改为按**动作词**逐个去认领**最近的 1 个**标的，
    天然 1:1，一个动作词最多产出一条交易。

    names: [(名, 代码, 起始, 结束)]（来自 scan_names）。
    带字符距离上限 window 与跨句阻断（block）；找不到返回 None。
    返回匹配到的 (名, 代码, 起始, 结束)，供调用方登记"已认领"区间。
    """
    aend = apos + len(kw)
    best: tuple[str, str, int, int] | None = None
    best_gap = window + 1
    for name, code, ns, ne in names:
        if ns >= aend:                       # 名在动作词之后
            gap = ns - aend
            between = text[aend:ns]
        elif ne <= apos:                     # 名在动作词之前
            gap = apos - ne
            between = text[ne:apos]
        else:
            continue                         # 与动作词重叠，交给邻域规则
        if gap >= best_gap:
            continue                         # 只留最近的一个（>= 保证不并列认领）
        if any(bc in between for bc in block):
            continue                         # 跨句不算同笔
        best, best_gap = (name, code, ns, ne), gap
    return best


def resolve(cand: str) -> dict | None:
    """把「疑似名」解析成词典里的真名（精确 → 去尾 → 唯一前缀）。

    用于动作词紧邻的短候选："金健大米" → 金健米业（去尾）、"平潭" → 平潭发展（唯一前缀）。
    歧义（多个候选）时返回 None，**不猜**。
    """
    nm = normalize(cand)
    if len(nm) < 2 or nm in STOP_WORDS:
        return None
    idx = index()
    if nm in idx:
        return {"name": nm, "code": idx[nm], "match": "exact"}
    for ln in (len(nm) - 1, len(nm) - 2):
        if ln >= 2 and nm[:ln] in idx:
            return {"name": nm[:ln], "code": idx[nm[:ln]], "match": "trim"}
    # 递减前缀 + 唯一性：候选常带垃圾尾巴（"崇达-6%" → 崇达技术、"金健大米博弈" → 金健米业）。
    # 只有"以该前缀开头的已知名**唯一**"才认（歧义不猜），这是防误配的关键闸门。
    for ln in range(len(nm) - 1, 1, -1):
        pre = nm[:ln]
        hits = {n for n in idx if n.startswith(pre)}
        if len(hits) == 1:
            only = next(iter(hits))
            return {"name": only, "code": idx[only], "match": "prefix"}
    return None


def scan_names(text: str) -> list[tuple[str, str, int, int]]:
    """扫出正文里**全部**真实股票名（最左最长、不重叠）。

    返回 [(标准名, 代码, 起始, 结束)]，按出现顺序。用于「名称 ↔ 最近动作词」关联，
    补偿"名字在句首、动作在句尾"的叙事。
    """
    out: list[tuple[str, str, int, int]] = []
    i = 0
    cap = max_name_len()
    while i < len(text):
        hit = match_at(text, i, max_len=cap)
        if hit:
            name, code, start, end = hit
            out.append((name, code, start, end))
            i = end
        else:
            i += 1
    return out
