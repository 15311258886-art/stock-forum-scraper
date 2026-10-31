# -*- coding: utf-8 -*-
"""单位与量纲归一（2026-10-31 新增，测评「单位量纲」项）。

## 问题

`stock_daily_snapshot.volume` 同一列里混着不同单位：科创板（688/689）存"股"，
其余多数存"手"。`×k` 型污染的特征是 **比值不变、差值全错** —— 所以
`volume_ratio`（当日量 ÷ 5 日均量）一直是对的，而任何绝对量（成交多少股、
成交额、换手率反算）都可能差 100 倍，且**不会报错**。

## 修法：自证，不是猜

按代码前缀特判（"688 用股、其余用手"）是**又一个需要维护的映射表**，
一旦某天换了数据源、或某只票的板块表没跟上，错得比现在更隐蔽。
这里改用同一次快照里本来就有的两个字段自证：

    amount / close  =  （元）÷（元/股）= 股

拿它和 `volume` 比：
    ≈1     → 该行 volume 本来就是股
    ≈100   → 该行 volume 是手
    其它   → **判不出，返回 None，不猜**

这样 688/689 不需要特例，也不依赖任何板块表 —— 数据自己说话。
判不出时上游必须显式降级（标记 unknown），而不是回退到某个默认倍数。

## 实测（2026-10-30 全市场快照 5,240 行）

    hand  4,600 行
    share   616 行
    none     22 行（amount 或 close 为空，无法自证）
    ?         2 行 —— 111024 / 113710，**是可转债，不是股票**

后 2 行是自证过程的附带发现：可转债混进了股票快照表。本模块只负责
把它们标出来（`volume_to_shares` 返回 None），不擅自删除任何行。

## 涨跌停幅度

这部分按代码前缀判定是**定义性**的（交易所规则），不是对数据的猜测，
所以允许写前缀表；但它同样集中在这里，别处不许再散写 0.1 / 0.2 / 0.3。
"""
from __future__ import annotations

# 自证判据的容差带。取得很宽，因为 amount 是"元"四舍五入到万位、
# close 是两位小数，两者相除本来就有几个千分点的抖动。
_SHARE_BAND = (0.5, 2.0)
_HAND_BAND = (50.0, 200.0)


def _num(x):
    """安全转 float；空/异常一律 None（None 传播，不静默当 0）。"""
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


def infer_volume_unit(volume, amount, close):
    """自证这一行的 volume 单位。

    返回 "share" / "hand" / None。
    None 的语义是"判不出"，**不是**"按默认处理"——调用方必须显式处理。
    """
    v = _num(volume)
    amt = _num(amount)
    c = _num(close)
    if not v or v <= 0 or not amt or amt <= 0 or not c or c <= 0:
        return None
    ratio = (amt / c) / v
    if _SHARE_BAND[0] <= ratio <= _SHARE_BAND[1]:
        return "share"
    if _HAND_BAND[0] <= ratio <= _HAND_BAND[1]:
        return "hand"
    return None


def volume_to_shares(volume, amount=None, close=None, unit=None):
    """把 volume 归一到"股"。判不出单位时返回 None（不回退默认倍数）。

    unit 可外部传入（同一只票在同一天只需自证一次，批量时可先算好再传）。
    """
    v = _num(volume)
    if v is None or v < 0:
        return None
    if unit is None:
        unit = infer_volume_unit(volume, amount, close)
    if unit == "share":
        return v
    if unit == "hand":
        return v * 100.0
    return None


def snapshot_volume_units(conn, trade_date=None):
    """对某一交易日的快照逐行自证，返回 {code: "share"|"hand"|None}。

    只读，不改任何行。判不出的 code 也会出现在结果里（值为 None），
    这样调用方能区分"没这只票"和"有但判不出"。
    """
    if trade_date is None:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()
        trade_date = row[0] if row else None
    if not trade_date:
        return {}
    rows = conn.execute(
        "SELECT code, volume, amount, close FROM stock_daily_snapshot WHERE trade_date=?",
        (trade_date,)).fetchall()
    return {code: infer_volume_unit(v, amt, c) for code, v, amt, c in rows}


def snapshot_volume_unit_stats(conn, trade_date=None):
    """自证结果的分布 + 判不出的明细，供体检/冒烟断言引用。"""
    units = snapshot_volume_units(conn, trade_date)
    stat = {"total": len(units), "share": 0, "hand": 0, "unknown": 0,
            "unknown_codes": []}
    for code, u in units.items():
        if u == "share":
            stat["share"] += 1
        elif u == "hand":
            stat["hand"] += 1
        else:
            stat["unknown"] += 1
            if len(stat["unknown_codes"]) < 50:
                stat["unknown_codes"].append(code)
    return stat


# ── K 线成交量单位（2026-10-31 测评 P0）──────────────────────────────────────────
#
# ## 事故
#
# 600519 / 2026-10-30：行情区显示「5.99 万手」（对，来自东财 spot f47=59,927 手），
# 抽屉 K 线区却显示「599.27 万手」——同一个数字 5,992,729 被当成手格式化，差了 100 倍。
# 根因不是格式化写错，而是**接口边界没声明单位**：K 线各源给的单位本来就不统一
# （2026-10-30 实测：新浪 K 线 / 本地库 / 东财 push2his 给「股」，腾讯 K 线对主板给「手」，
#  而腾讯与东财对科创板（688）又给「股」），
# 前端只有「手」一套格式化函数，只能猜，猜错就是 100 倍且不报错。
#
# ## 所以这里做两件事
#
# 1. `kline_row_unit` / `kline_series_unit`：沿用上面 `infer_volume_unit` 的自证思路，
#    用 K 线行自带的 `amount`（元）/ `close`（元/股）/ `volume` 判定该行到底是股还是手；
#    整条序列取**最近 N 根的众数**——同一接口同一只票单位不会跳变，逐根判会让
#    同一张图里混着两种刻度。
# 2. `infer_unit_by_anchor`：给「amount 是估算值」的源留的第二条证据链。新浪/腾讯/本地库
#    的 K 线 amount 由「收盘价 × 成交量」算出，拿它自证恒得比值 1（自己证明自己），
#    不能作为证据；那就换一个**独立**锚点：同一天的真实成交量（股），
#    与 K 线 volume 相差 ≈1 倍是股、≈100 倍是手。
#
# 两条链都判不出时返回 None，由调用方把 volume 置空并显式标注 unknown——
# **绝不回退某个默认倍数**：宁可显示「单位未标注」，也不给一个错 100 倍的数。
KLINE_UNIT_WINDOW = 20          # 取最近多少根有 amount 的行投票
_ANCHOR_TOL = 0.02              # 锚点比对容差（2%：四舍五入到手的成交量差不到这个量级）


def kline_row_unit(row) -> str | None:
    """单根 K 线自证单位（"share" / "hand" / None）。

    要求该行的 `amount` 是**真实成交额**。若 amount 是「收盘价 × 成交量」估算出来的，
    比值恒为 1，这里会返回 "share"，但那只是自己的影子 —— 调用方必须先判断
    `payload["amount_est"]`，估算源请走 `infer_unit_by_anchor`。
    """
    if not isinstance(row, dict):
        return None
    return infer_volume_unit(row.get("volume"), row.get("amount"), row.get("close"))


def kline_series_unit(rows, window: int = KLINE_UNIT_WINDOW) -> str | None:
    """一条 K 线序列的单位：最近 window 根里能自证的那些行投票取众数。

    票数打平（各 1 票）或一票都没有 → None，语义是「判不出」，不是「按默认处理」。
    """
    if not rows:
        return None
    tail = list(rows)[-max(1, int(window or KLINE_UNIT_WINDOW)):]
    tally = {"share": 0, "hand": 0}
    for r in tail:
        u = kline_row_unit(r)
        if u:
            tally[u] += 1
    if tally["share"] == tally["hand"]:
        return None
    return "share" if tally["share"] > tally["hand"] else "hand"


def infer_unit_by_anchor(volume, anchor_shares, tol: float = _ANCHOR_TOL) -> str | None:
    """用「同一交易日的真实成交量（股）」反推 volume 的单位。

    anchor_shares 必须是**股**（调用方负责先自证锚点自身的单位）。
    volume 与它 ≈1 倍 → 该序列原本就是股；≈1/100 倍 → 原本是手；其它 → 判不出。
    """
    v = _num(volume)
    a = _num(anchor_shares)
    if not v or v <= 0 or not a or a <= 0:
        return None
    if abs(v - a) / a <= tol:
        return "share"
    if abs(v * 100.0 - a) / a <= tol:
        return "hand"
    return None


def kline_volume_to_shares(volume, unit: str | None):
    """按已判定的单位把 K 线量换成「股」。unit 为 None 时返回 None（不回退倍数）。"""
    return volume_to_shares(volume, unit=unit)


# ── 标的类型判定（定义性规则，非数据猜测）────────────────────────────────────────
#
# 2026-10-31（决策④）：为什么要有这一层。
#
# 原本 `is_convertible_bond()` 只被 executability 调用，回测和选股各写各的取码，
# 于是同一个问题在不同入口有不同答案：
#   · 回测取码不过滤任何标的 —— 指标库里躺着 111024（2010-01 ~ 2014-09 的债券，
#     名称却被写成 2021 年才上市的「澳弘转债」），窗口一拉长就会把它算进超额；
#   · 容量计算过滤了转债；
#   · 选股走 a_shares_universe，而这张表混了 2 只转债、又漏收全部 328 只北交所。
#
# 这里把「这是什么标的」收成一个函数，谁也不许再自己写前缀判断。
# 判不出时返回 unknown 并**由调用方决定**，绝不回退成 stock ——
# 默认当成股票，正是当初让债券混进统计的方式。
KIND_STOCK = "stock"
KIND_BOND = "convertible_bond"
KIND_FUND = "fund"
KIND_UNKNOWN = "unknown"

_BOND_PREFIX = ("110", "111", "113", "118", "123", "127", "128", "132")
_FUND_PREFIX = ("15", "16", "50", "51", "52", "56", "58")
# 交易所定义的 A 股代码段。含 920（北交所 2023 起的新段，是最常被漏掉的一段）。
_STOCK_PREFIX = ("60", "68", "00", "001", "002", "003", "30", "301", "302",
                 "43", "83", "87", "88", "92")


def stock_kind(code: str, name: str = "") -> str:
    """判定该代码是什么标的：stock / convertible_bond / fund / unknown。

    判据是「代码段 + 名称」双重，且以代码段为主：
      · 代码段是交易所规则，定义性的，可信；
      · 名称只用来兜住代码段认不出的情况（历史上出现过债券挂在股票代码段）。
    两者互相矛盾时（如代码像股票、名字叫「XX 转债」）判为转债 ——
    宁可漏算一只，也不要把债券当股票算进超额。
    """
    c = str(code or "").strip()
    nm = (name or "").upper().replace(" ", "")
    if not c:
        return KIND_UNKNOWN
    has_bond_name = ("转债" in nm) or ("可转债" in nm) or nm.endswith("EB")
    has_fund_name = ("ETF" in nm) or ("LOF" in nm) or ("基金" in nm)
    if c.startswith(_BOND_PREFIX) or has_bond_name:
        return KIND_BOND
    if has_fund_name:
        return KIND_FUND
    if c.startswith(_FUND_PREFIX) and not c.startswith(("500", "501")):
        # 500/501 是沪市老封闭式基金代码段，同样按基金处理；
        # 这里保留前缀表只是为了可读，实际两者都归 fund。
        return KIND_FUND
    if c.startswith(_STOCK_PREFIX):
        return KIND_STOCK
    return KIND_UNKNOWN


def is_convertible_bond(code: str, name: str = "") -> bool:
    """可转债（沪 110/111/113/118、深 12x）或名称含「转债」。

    2026-10-31：加 name 参数。旧签名只有 code，遇到「代码像股票、名字是转债」
    的情况会判错 —— 而 111024 正是这种错标的实例。
    """
    return stock_kind(code, name) == KIND_BOND


def is_tradable_stock(code: str, name: str = "") -> bool:
    """是不是可以进选股/回测的 A 股。

    只认 stock_kind()==stock。unknown 一律返回 False —— 判不出就别算，
    这与「宁可少算一只，不要把债券算进超额」是同一条原则。
    """
    return stock_kind(code, name) == KIND_STOCK


def price_limit_pct(code: str, name: str = "") -> float | None:
    """该票的涨跌停幅度。返回 None 表示"不设限"或"判断不了"。

    返回 None 的两种情形必须由调用方区分处理：
      · 新股上市首日 / 北交所部分品种 —— 真的不设限；
      · 代码不认识 —— 不知道。
    这里统一返回 None，并另给 `limit_is_known()`，避免把"不知道"当"不设限"。
    """
    c = str(code or "")
    nm = (name or "").upper().replace(" ", "")
    is_st = ("ST" in nm) or nm.startswith("*")
    if c.startswith(("688", "689")):        # 科创板
        return 0.20
    if c.startswith(("300", "301")):        # 创业板
        return 0.20
    if c.startswith(("8", "4", "9")) and len(c) == 6:
        # 北交所（43/83/87/88 等）及其过渡代码
        if c.startswith(("43", "83", "87", "88", "92")):
            return 0.30
    if c.startswith(("60", "00", "001", "002", "003")):
        return 0.05 if is_st else 0.10
    return None


def limit_is_known(code: str, name: str = "") -> bool:
    """价格限制是否已知（不是"是否设限"）。"""
    return price_limit_pct(code, name) is not None


def limit_up_price(prev_close, code: str, name: str = ""):
    """涨停价（按交易所四舍五入到分）。判断不了返回 None。"""
    pc = _num(prev_close)
    pct = price_limit_pct(code, name)
    if pc is None or pc <= 0 or pct is None:
        return None
    return round(pc * (1.0 + pct) + 1e-9, 2)


def limit_down_price(prev_close, code: str, name: str = ""):
    """跌停价。"""
    pc = _num(prev_close)
    pct = price_limit_pct(code, name)
    if pc is None or pc <= 0 or pct is None:
        return None
    return round(pc * (1.0 - pct) + 1e-9, 2)


def is_limit_up(close, prev_close, code: str, name: str = "", tol: float = 0.011):
    """是否涨停收盘。tol 给 1.1 分钱级容差（前复权后价格有舍入）。"""
    lu = limit_up_price(prev_close, code, name)
    c = _num(close)
    if lu is None or c is None:
        return None
    return c >= lu - tol


def is_limit_down(close, prev_close, code: str, name: str = "", tol: float = 0.011):
    ld = limit_down_price(prev_close, code, name)
    c = _num(close)
    if ld is None or c is None:
        return None
    return c <= ld + tol
