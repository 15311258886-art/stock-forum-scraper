# -*- coding: utf-8 -*-
"""全市场证券主表 `security_master` —— 单一取码口径源。

## 为什么要有这张表（P0「全 A 股宇宙不完整」，2026-11-01）

这个项目里"有哪些股票"曾经有 **四个**互不一致的答案：

    a_shares_universe          5,240 行，混 2 只可转债 + 1 只 ETF，漏收全部 328 只北交所
    stock_daily_snapshot       同 5,240 只（跟着白名单走，所以一起错）
    stock_indicators_daily     5,337 只（含那 328 只北交所，也含 1 只 2014 年退市的债券）
    stock_names                5,588 行（覆盖最全，但混着 XD 除权临时名）

每个模块按自己方便的入口取码，于是"今天有没有信号"这类问题在不同页面上有不同的答案；
而当入口是 `a_shares_universe` 时，整个北交所（330 只）会**静默消失** —— 不报错、不告警，
只是永远选不出来。这正是这个项目反复踩的坑：**错得看起来很平静**。

所以这里建一张表，把「这是什么标的」「属于哪个板块」「归哪个交易所」一次性定下来，
所有模块都改从这里取码。

## 三条硬规矩

1. **非股票不是删除，而是标注**。可转债/ETF 留在表里（`kind != 'stock'`），
   这样"为什么它不见了"是可查的；直接删掉只会让下一个读库的人再踩一次。
2. **判不出就说判不出**。`kind` 判不出时记 `other`，`delist_date` 不知道就留空并在
   `source` 里写 `delist=unknown`。绝不编造日期，也不把 unknown 回退成 stock——
   默认当股票正是当初让债券混进统计的方式。
3. **缺表就炸，不返回空列表**。`load()` / `stock_codes()` 在表不存在时抛
   `SecurityMasterMissing`。静默返回 `[]` 会让上层"今天没有信号"看起来像正常业务状态，
   这是本项目最贵的一类 bug。

## kind 的判定复用 `viewer.units.stock_kind()`

不另写一套前缀判断。`classify_kind()` 只是把 units 的四个取值
（stock / convertible_bond / fund / unknown）映射进本表的七个取值
（stock / convertible_bond / etf / fund / index / other），并把 ETF 从基金里拆出来。
`index` 目前只保留词汇、不产出：units 层认不出指数（000300 会被判成股票），
要区分指数必须改 units，本轮不做——**一个判据，一处实现**，绝不搞双口径。

## 已知边界

* `delist_date` 全网没有可靠数据源（stock_org 只有 list_date），因此本轮全部为空。
  按约定 `delist_date` 为空 ⇒ `is_active=1`。唯一可信的存活线索是交易所命名约定：
  退市整理期/已退市标的的简称带「退」（如 000004「国华退」/"退市博天"）——
  这类行的 `source` 标 `delist=name_suffix`，并由 `delist_suspects()` 单列，
  但**不擅自改 `is_active`**（改了会让 8 只仍在行情表里的退市整理股从宇宙里消失，
  属于另一个决策）。**`is_active=1` 的含义是"没证据表明已退市"，不是"一定在交易"。**
* 北交所补齐依赖 kline 库 / 指标库 / 快照表里的 920 段；三者都没有的票（例如刚过会
  未上市）这张表也拿不到，`source` 会如实反映它只出现在哪张表。

## 建表时发现的两个"看起来像 bug、其实是对"的差异（别改回去）

1. **`689009 九号公司-WD` 是科创板，不是沪市主板。**
   `a_shares_universe` 把它记成「沪市主板」，本表按 689 段（科创板 CDR）判为「科创板」。
   这是本表与白名单**唯一一处板块分歧，以本表为准** —— 689 是 2020 年起上交所给
   CDR/存托凭证单独开的段，归科创板。别看到"两边不一致"就按旧表改回来。

2. **`000004 国华退` 只存在于 `stock_org`（工商资料表）。**
   它不在白名单、不在指标库、不在快照、不在 K 线库（一行行情都没有），
   但它是真实存在过的深市主板股票（1990-12-01 上市，已退市）。
   本表把它收进来并标 `delist=name_suffix`/`is_active=1`（见上面的约定）。
   别因为"行情表里查不到"就把它删掉 —— 那会让"这只票去哪了"变成一个查不到答案的问题。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from . import units as U

# ── 表名与词汇表 ──────────────────────────────────────────────────────────────
TABLE = "security_master"

KIND_STOCK = "stock"
KIND_BOND = "convertible_bond"
KIND_ETF = "etf"
KIND_FUND = "fund"
KIND_INDEX = "index"          # 词汇保留：units 暂不产出，见模块 docstring
KIND_OTHER = "other"

KINDS = (KIND_STOCK, KIND_BOND, KIND_ETF, KIND_FUND, KIND_INDEX, KIND_OTHER)

BOARD_SH_MAIN = "沪市主板"
BOARD_SZ_MAIN = "深市主板"
BOARD_CHINEXT = "创业板"
BOARD_STAR = "科创板"
BOARD_BSE = "北交所"
BOARD_OTHER = "其它"

# 真正属于"股票板块"的取值。'其它' / '未知板块' 是历史遗留的兜底值，不算板块。
STOCK_BOARDS = (BOARD_SH_MAIN, BOARD_SZ_MAIN, BOARD_CHINEXT, BOARD_STAR, BOARD_BSE)

EXCHANGE_SSE = "SSE"
EXCHANGE_SZSE = "SZSE"
EXCHANGE_BSE = "BSE"
EXCHANGE_OTHER = "OTHER"

# 白名单（a_shares_universe）用的是中文交易所名，落库时要转回来。
_EXCHANGE_ZH = {
    EXCHANGE_SSE: "上交所",
    EXCHANGE_SZSE: "深交所",
    EXCHANGE_BSE: "北交所",
    EXCHANGE_OTHER: "其它",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS security_master (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    exchange    TEXT,      -- SSE / SZSE / BSE
    board       TEXT,      -- 沪市主板 / 深市主板 / 创业板 / 科创板 / 北交所
    kind        TEXT,      -- stock / convertible_bond / etf / fund / index / other
    list_date   TEXT,
    delist_date TEXT,
    source      TEXT,
    as_of       TEXT,      -- 本行数据的快照日期
    is_active   INTEGER DEFAULT 1
)
"""

CREATE_INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_sm_kind_board "
                    "ON security_master(kind, board)")

COLUMNS = ("code", "name", "exchange", "board", "kind", "list_date",
           "delist_date", "source", "as_of", "is_active")

# 除权除息日的临时显示前缀（交易所给简称加的，第二天就没了）。
TEMP_PREFIXES = ("XD", "XR", "DR")

MISSING_HINT = (
    "security_master 不存在或为空。请先运行：\n"
    "    .venv/Scripts/python.exe tools/build_security_master.py\n"
    "（或对连接调用 viewer.security_master.build(conn)）。\n"
    "本模块不会静默返回空列表：空宇宙会让「今天没有信号」看起来像正常业务状态。"
)


# ── 异常 ──────────────────────────────────────────────────────────────────────
class SecurityMasterMissing(RuntimeError):
    """`security_master` 表缺失/为空。调用方必须把它当故障处理，而不是当"没数据"。"""


class NotAStock(ValueError):
    """给定代码在权威口径里不是可交易的 A 股（可转债/ETF/未知/已停用）。"""


# ── 建表 ──────────────────────────────────────────────────────────────────────
def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表 + 建索引。幂等，可在每次 build 前无条件调用。"""
    conn.execute(CREATE_SQL)
    conn.execute(CREATE_INDEX_SQL)


def table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return row is not None


# ── 标的类型判定（复用 units，不另写前缀表）────────────────────────────────────
def classify_kind(code: str, name: str = "") -> str:
    """把 `units.stock_kind()` 的取值映射进本表的 kind 词汇。

    · stock / convertible_bond 原样透传；
    · fund 再拆一层：名称含 ETF 记 etf，其余记 fund（513050 中概互联网ETF易方达 → etf）；
    · unknown 记 other —— 判不出就是判不出，不许默认成股票。

    这是全项目唯一的 kind 判定入口，`stock_kind()` 也只在这里被间接调用。
    """
    unit_kind = U.stock_kind(code, name)
    if unit_kind == U.KIND_STOCK:
        return KIND_STOCK
    if unit_kind == U.KIND_BOND:
        return KIND_BOND
    if unit_kind == U.KIND_FUND:
        nm = (name or "").upper().replace(" ", "")
        # ETF 与 LOF/场外基金的交易/估值口径不同，值得单列；代码段同为基金段。
        return KIND_ETF if "ETF" in nm else KIND_FUND
    return KIND_OTHER


def is_temporary_name(name: str) -> bool:
    """是否为除权除息临时名（XD/XR/DR 前缀）。"""
    nm = (name or "").strip().upper()
    return any(nm.startswith(p) for p in TEMP_PREFIXES)


def name_alias_matches(observed: str, canonical: str) -> bool:
    """临时名与主名是否指的是同一只票。

    交易所把除权提示名截短显示（"浙商证券" 除权日显示为 "XD浙商证"），
    所以去掉前缀后不能用等号比，要用前缀包含比：
        "XD浙商证" → "浙商证"  vs  "浙商证券"  → 命中（后者以前者开头）
    这正是审计里"XD 临时名不算 WARN"的判据。
    """
    a = (observed or "").strip()
    b = (canonical or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    for tmp, other in ((a, b), (b, a)):
        up = tmp.upper()
        for p in TEMP_PREFIXES:
            if up.startswith(p):
                core = tmp[len(p):].strip()
                if core and (other.startswith(core) or core.startswith(other)):
                    return True
    return False


# ── 板块 / 交易所（定义性规则，非数据猜测）────────────────────────────────────
def board_of(code: str, hint: str = "") -> str:
    """板块归属。`hint` 是来源表已有的板块名，可信时优先用它。

    代码段映射是交易所规则（定义性的，允许写在代码里）；`hint` 只在上游给出的是
    真正的股票板块时才采信，否则（'其它' / '未知板块'）一律回落到代码段判定。
    """
    h = (hint or "").strip()
    if h in STOCK_BOARDS:
        return h
    c = str(code or "")
    if c.startswith(("688", "689")):
        return BOARD_STAR
    if c.startswith("60"):
        return BOARD_SH_MAIN
    if c.startswith(("300", "301", "302")):
        return BOARD_CHINEXT
    if c.startswith(("000", "001", "002", "003")):
        return BOARD_SZ_MAIN
    if c.startswith(("92", "43", "83", "87", "88")):
        # 920 是北交所 2023 起的新段；43/83/87/88 是老新三板转板段。漏掉 920
        # 就是"整个北交所不存在"，这是本轮 P0 的根因。
        return BOARD_BSE
    return BOARD_OTHER


def exchange_of(code: str, board: str = "") -> str:
    """交易所归属。股票按板块推；非股票按代码段推（债券/ETF 的段同样是定义性的）。"""
    b = board or board_of(code)
    if b in (BOARD_SH_MAIN, BOARD_STAR):
        return EXCHANGE_SSE
    if b in (BOARD_SZ_MAIN, BOARD_CHINEXT):
        return EXCHANGE_SZSE
    if b == BOARD_BSE:
        return EXCHANGE_BSE
    c = str(code or "")
    if c.startswith(("11", "50", "51", "56", "58")):
        return EXCHANGE_SSE
    if c.startswith(("12", "15", "16")):
        return EXCHANGE_SZSE
    return EXCHANGE_OTHER


def exchange_zh(exchange: str) -> str:
    """SSE/SZSE/BSE → 白名单里的中文名（写回 a_shares_universe 时用）。"""
    return _EXCHANGE_ZH.get((exchange or "").strip().upper(), "其它")


# ── 缓存与读取 ────────────────────────────────────────────────────────────────
_CACHE = {"key": None, "rows": None, "by_code": None}


def reset_cache() -> None:
    """清缓存。build() 结束会自动调；测试与长驻进程也可手动调。"""
    _CACHE.update(key=None, rows=None, by_code=None)


def _db_key(conn: sqlite3.Connection) -> str:
    """按库文件路径做缓存键 —— 同一个 app.db 开两个连接也能共享缓存。"""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list"):
            if name == "main":
                return path or ("mem:%d" % id(conn))
    except sqlite3.Error:
        pass
    return "conn:%d" % id(conn)


def load(conn: sqlite3.Connection, force: bool = False) -> list:
    """返回 security_master 全部行（dict 列表），带进程内缓存。

    表不存在时抛 `SecurityMasterMissing` —— **不返回 []**。
    """
    if not table_exists(conn):
        raise SecurityMasterMissing(MISSING_HINT)

    key = _db_key(conn)
    if not force and _CACHE["key"] == key and _CACHE["rows"] is not None:
        return _CACHE["rows"]

    rows = []
    sql = "SELECT %s FROM %s" % (",".join(COLUMNS), TABLE)
    for r in conn.execute(sql):
        d = dict(zip(COLUMNS, r))
        d["is_active"] = 1 if d.get("is_active") in (1, True, "1") else 0
        rows.append(d)
    rows.sort(key=lambda d: d["code"] or "")

    _CACHE.update(key=key, rows=rows, by_code={d["code"]: d for d in rows})
    return rows


def _by_code(conn: sqlite3.Connection) -> dict:
    if _CACHE["key"] != _db_key(conn) or _CACHE["by_code"] is None:
        load(conn)
    return _CACHE["by_code"] or {}


def get(code: str, conn: sqlite3.Connection):
    """取单行，不存在返回 None（表缺失仍然抛异常，语义与 load 一致）。"""
    return _by_code(conn).get(str(code or "").strip())


def stock_codes(conn: sqlite3.Connection, boards=None, include_bse: bool = True,
                exclude_delist_suspects: bool = False) -> list:
    """可用股票代码列表：`kind='stock'` 且 `is_active=1`。

    ## 硬规矩：`exclude_delist_suspects` 不给默认值兜住（2026-11-01 决策）

    同一份数据在两类场景下**正确用法相反**，所以必须由调用方显式声明：

      · **买入候选 / 正向信号**（选股池、可买清单、推荐、回测多头样本）
        → 必须传 `exclude_delist_suspects=True`。
        理由：退市整理期标的还能交易 30 个交易日，但**不能推荐**；
        把它们留在候选池里，等于把"正在退市"当成投资机会。
      · **宇宙统计 / 覆盖率 / 数据完整性**（"我们有几只票"、快照覆盖率、
        名称回填、体检）
        → 保持默认 `False`。理由：把它们当"不存在"会让宇宙口径悄悄缩水，
        而这正是本项目反复踩的坑（用默认值把未知盖住）。

    默认 False 不是"推荐这么用"，而是"统计口径"这一侧的默认；
    任何面向用户展示"可以买什么"的路径都必须显式传 True。

    参数
    ----
    boards: None / str / 可迭代
        限定板块（如 ``("沪市主板", "深市主板")``）。传 None 表示不过滤。
    include_bse: bool
        默认 **True**。北交所是 A 股的一部分，默认排除它正是上一版的病根；
        需要老口径（不含北交所）时必须显式传 False，让"排除北交所"出现在调用处。
    exclude_delist_suspects: bool
        默认 **False**。True 时剔除名称带退市标记的标的（见 `delist_suspects()`）。
        买入候选/正向信号必须传 True。

    返回按代码升序的列表。表缺失抛 `SecurityMasterMissing`。
    """
    bset = None
    if boards is not None:
        bset = {boards} if isinstance(boards, str) else set(boards)

    out = []
    for row in load(conn):
        if row["kind"] != KIND_STOCK:
            continue
        if not row["is_active"]:
            continue
        if not include_bse and row["board"] == BOARD_BSE:
            continue
        if exclude_delist_suspects and "delist=name_suffix" in (row["source"] or ""):
            continue
        if bset is not None and row["board"] not in bset:
            continue
        out.append(row["code"])
    return sorted(out)


def is_stock(code: str, conn: sqlite3.Connection) -> bool:
    """权威口径下是否可交易 A 股。表缺失抛异常（不是返回 False）。"""
    row = get(code, conn)
    return bool(row and row["kind"] == KIND_STOCK and row["is_active"])


def assert_stock(code: str, conn: sqlite3.Connection | None = None) -> bool:
    """断言该代码是可用 A 股，否则抛 `NotAStock`；表缺失抛 `SecurityMasterMissing`。

    传了 conn：走权威口径（含 is_active）。
    没传 conn：只能按代码段判（units 口径），**看不到退市状态**，仅用于
    没有连接可用的早期校验。这个差异必须显式写在调用处，不要当成等价。
    """
    c = str(code or "").strip()
    if conn is not None:
        row = get(c, conn)
        if row is None:
            raise NotAStock("%s 不在 security_master（权威口径里没有这只标的）" % c)
        if row["kind"] != KIND_STOCK:
            raise NotAStock("%s（%s）的 kind=%s，不是 A 股"
                            % (c, row.get("name") or "", row["kind"]))
        if not row["is_active"]:
            raise NotAStock("%s（%s）已停用/退市" % (c, row.get("name") or ""))
        return True
    if not U.is_tradable_stock(c):
        raise NotAStock("%s 按代码段不是 A 股（未连库，未校验退市状态）" % c)
    return True


def delist_suspects(conn: sqlite3.Connection) -> list:
    """疑似已退市/退市整理期的行（source 里带 `delist=name_suffix`）。

    为什么单独给一个函数：本轮约定 `delist_date` 为空 ⇒ `is_active=1`，所以这 8 只
    带「退」的标的名仍在 `stock_codes()` 里。消费方若要在选股/回测里排除退市整理期
    标的，必须显式调这里过滤，而不是把 `is_active=1` 理解成"一定在正常交易"。
    """
    return [r for r in load(conn) if "delist=name_suffix" in (r.get("source") or "")]


# ── 构建（幂等）──────────────────────────────────────────────────────────────
# 名称候选的来源优先级：名称表最权威（东财/腾讯快照刷新），其次白名单，
# 最后才是行情表的 name 列（那是行情附带信息，可能滞后或被临时名覆盖）。
_NAME_SRC_PRIO = {
    "stock_names": 0,
    "a_shares_universe": 1,
    "stock_daily_snapshot": 2,
    "stock_indicators_daily": 3,
    "stock_org": 4,
}


def _default_kline_candidates(root: str) -> list:
    """K 线库的两个历史路径（决策变更过，两个都认）。"""
    return [os.path.join(root, "data", "kline_daily.db"),
            os.path.join(root, "viewer", "data", "kline_daily.db")]


def _open_ro(path: str):
    """只读打开（build 绝不写 K 线库）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=30)
    except sqlite3.Error:
        return None


def _collect_candidates(conn: sqlite3.Connection, kline_paths=None) -> dict:
    """扫所有来源，汇总每个代码的候选名称 / 板块 / 来源表。

    只 SELECT 需要的列，绝不 `SELECT *`：主库 9.5GB，指标库 1,300 万行。
    """
    cand = {}

    def put(code, name=None, board=None, source="", list_date=None):
        c = str(code or "").strip()
        if not c:
            return
        rec = cand.get(c)
        if rec is None:
            rec = {"code": c, "names": [], "boards": [], "sources": set(),
                   "list_date": None}
            cand[c] = rec
        rec["sources"].add(source)
        nm = (name or "").strip()
        if nm:
            rec["names"].append((_NAME_SRC_PRIO.get(source, 9), source, nm))
        bd = (board or "").strip()
        if bd:
            rec["boards"].append((0 if bd in STOCK_BOARDS else 1,
                                  _NAME_SRC_PRIO.get(source, 9), bd))
        if list_date and not rec["list_date"]:
            rec["list_date"] = str(list_date).strip()

    def has_table(t):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (t,)).fetchone() is not None

    # ① 白名单（基础盘子；含 2 只转债 + 1 只 ETF，不在此处删，交给 kind 标出）
    if has_table("a_shares_universe"):
        for code, name, board, _ex in conn.execute(
                "SELECT code, name, board, exchange FROM a_shares_universe"):
            put(code, name, board, "a_shares_universe")

    # ② 名称表（覆盖最全：5,588 行，含 350 只北交所）
    if has_table("stock_names"):
        for code, name, market in conn.execute(
                "SELECT code, name, market FROM stock_names"):
            put(code, name, market, "stock_names")

    # ③ 快照最新一日（行情附带名；北交所若只有快照也能被捞到）
    if has_table("stock_daily_snapshot"):
        row = conn.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()
        day = row[0] if row else None
        if day:
            for code, name, board in conn.execute(
                    "SELECT code, name, board FROM stock_daily_snapshot WHERE trade_date=?",
                    (day,)):
                put(code, name, board, "stock_daily_snapshot")

    # ④ 指标库（本轮 P0 的 328 只 920 段主要在这里）
    if has_table("stock_indicators_daily"):
        for code, name in conn.execute(
                "SELECT code, MAX(name) FROM stock_indicators_daily GROUP BY code"):
            put(code, name, None, "stock_indicators_daily")

    # ⑤ 上市日期（唯一可靠来源；退市日期全网无，故不填）
    #    name 也顺手带上：工商表里有些已退市代码（000004「国华退」）不在任何行情表里，
    #    没有名字就看不出"这是只退市股"，退市线索就丢了。优先级最低（工商资料会滞后）。
    if has_table("stock_org"):
        for code, name, list_date in conn.execute(
                "SELECT code, name, list_date FROM stock_org"):
            put(code, name, None, "stock_org", list_date)

    # ⑥ K 线库（1.6GB 只读；有 K 线但三张表都没有的代码，说明它确实在交易）
    kline_hit = None
    for p in (kline_paths or []):
        k = _open_ro(p)
        if k is None:
            continue
        try:
            for (code,) in k.execute("SELECT DISTINCT code FROM kline"):
                put(code, None, None, "kline_daily")
            kline_hit = p
        except sqlite3.Error:
            pass
        finally:
            k.close()
        break  # 只取第一个能打开的库，避免重复扫

    return {"codes": cand, "kline_used": kline_hit}


def _pick_name(rec: dict) -> tuple:
    """从候选名里挑主名：先排除除权临时名，再按来源优先级。

    返回 (name, source)。全部来源都是临时名时，保留临时名但 source 标出来 ——
    例如 920012 全网只有 "XD创达新"，那就只能如实记临时名，不能编。
    """
    names = rec["names"]
    if not names:
        return "", ""
    stable = [x for x in names if not is_temporary_name(x[2])]
    pool = stable or names
    pool.sort(key=lambda x: (x[0], x[1]))
    return pool[0][2], pool[0][1]


def _lookup_stable_name(conn: sqlite3.Connection, code: str) -> tuple:
    """临时名的还原：去快照/指标库里找该代码最近一个「非临时」名字。

    交易所除权日会把简称改成 XD+截短名，第二天恢复。主表应该记恢复后的名，
    否则一次除权就会让主表和行情表"名字不一致"，把真实缺陷淹掉。
    只对极少数临时名代码做逐代码查询（当前全市场 4 只），绝不整表扫。

    返回 (name, 提供该名字的表名)。
    """
    sql = ("SELECT name FROM {t} WHERE code=? AND COALESCE(name,'') <> '' "
           "AND name NOT LIKE 'XD%' AND name NOT LIKE 'XR%' AND name NOT LIKE 'DR%' "
           "ORDER BY {d} DESC LIMIT 1")
    for table, date_col in (("stock_daily_snapshot", "trade_date"),
                            ("stock_indicators_daily", "date")):
        try:
            row = conn.execute(sql.format(t=table, d=date_col), (code,)).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0]:
            return str(row[0]).strip(), table
    return "", ""


def _finalize_row(conn: sqlite3.Connection, rec: dict, as_of: str) -> dict:
    """把候选记录定型成一行 security_master。"""
    code = rec["code"]
    name, _name_src = _pick_name(rec)
    if name and is_temporary_name(name):
        stable, stable_src = _lookup_stable_name(conn, code)
        if stable:
            name = stable
            # 名称实际取自哪张表，如实补进 source（可追溯）
            rec["sources"].add(stable_src)

    kind = classify_kind(code, name)

    board_hint = ""
    if rec["boards"]:
        rec["boards"].sort()
        board_hint = rec["boards"][0][2]
    # board_of 只在 hint 是真正的股票板块时才采信（'其它'/'未知板块' 会回落代码段）。
    # 非股票（转债/ETF）代码段不在股票段内，因此天然落到 '其它'，不会被算进板块统计。
    board = board_of(code, board_hint)
    exchange = exchange_of(code, board)

    sources = "+".join(sorted(rec["sources"])) or "unknown"
    if not name:
        sources += ";name=unknown"

    # 退市状态：全网没有 delist_date 数据源（stock_org 只有 list_date），所以绝不编日期。
    # 按本轮约定：delist_date 为空 ⇒ is_active=1（见模块 docstring）。
    # 但"名称带退市标记"这个证据不能丢：交易所命名约定里，退市整理期/已退市标的的
    # 简称带「退」（"萃华退" / "退市博天"），这类行的 source 会标 delist=name_suffix，
    # 并被 delist_suspects() 单列出来 —— 记录证据，但不擅自改变 is_active 的约定口径。
    delist_note = "unknown"
    nm = (name or "").strip()
    if nm.endswith("退") or "退市" in nm:
        delist_note = "name_suffix"
    sources += ";delist=%s" % delist_note

    return {
        "code": code,
        "name": name,
        "exchange": exchange,
        "board": board,
        "kind": kind,
        "list_date": rec["list_date"] or None,
        "delist_date": None,
        "source": sources,
        "as_of": as_of,
        "is_active": 1,
    }


_UPSERT_SQL = (
    "INSERT INTO security_master (%s) VALUES (%s) "
    "ON CONFLICT(code) DO UPDATE SET %s" % (
        ",".join(COLUMNS),
        ",".join(["?"] * len(COLUMNS)),
        ",".join("%s=excluded.%s" % (c, c) for c in COLUMNS if c != "code"),
    )
)


def build(conn: sqlite3.Connection, kline_db=None, as_of: str | None = None,
          dry_run: bool = False, sync_universe: bool = False) -> dict:
    """扫描全部来源并重建 `security_master`（幂等）。

    幂等怎么做：每行都写入本次的 `as_of`；写完把 `as_of <> 本次` 的行删掉。
    于是"这次没扫到的代码"自然消失，重复运行行数不变、不会产生重复行。

    参数
    ----
    kline_db: str | list
        K 线库路径；None 时按 data/ 与 viewer/data/ 两个历史路径探测。
    as_of: str
        本次快照标识（默认当前时间）。写入每行的 as_of。
    dry_run: bool
        只算不写（表不存在时会抛 SecurityMasterMissing）。
    sync_universe: bool
        把主表里、白名单里缺的**股票**补写进 a_shares_universe（只 INSERT，绝不
        DELETE/UPDATE）。默认关闭：白名单是遗留表，是否让它跟上属于数据治理决策，
        不在本轮自动做。审计报告会给出这条修复命令。

    返回统计 dict（供 CLI 打印与测试断言）。
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    started = datetime.now()

    # dry_run 不建表：它是一次试算，连"表还不存在"的情况也要能算出来（首次上线时
    # 就是这种状态）。而读取路径（load/stock_codes）在缺表时必须炸，两者语义不同。
    if not dry_run:
        ensure_schema(conn)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if kline_db is None:
        paths = _default_kline_candidates(root)
    elif isinstance(kline_db, str):
        paths = [kline_db]
    else:
        # 显式传空列表 = "没有 K 线库可比"（测试用），不要回落到默认探测
        paths = list(kline_db)

    collected = _collect_candidates(conn, paths)
    cand = collected["codes"]
    if not cand:
        raise SecurityMasterMissing("所有来源表都扫不到代码：" + MISSING_HINT)

    rows = [_finalize_row(conn, rec, as_of) for rec in cand.values()]
    rows.sort(key=lambda d: d["code"])

    existing = set()
    if table_exists(conn):
        try:
            existing = {c for (c,) in conn.execute("SELECT code FROM security_master")}
        except sqlite3.Error:
            existing = set()

    stats = {
        "as_of": as_of,
        "as_of_date": as_of[:10],
        "kline_used": collected["kline_used"],
        "total": len(rows),
        "inserted": len([r for r in rows if r["code"] not in existing]),
        "updated": len([r for r in rows if r["code"] in existing]),
        "orphans_removed": 0,
        "by_kind": {},
        "by_board": {},
        "by_exchange": {},
        "stock_bse": 0,
        "inactive": 0,
        "nonstock": [],
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
    }
    for r in rows:
        stats["by_kind"][r["kind"]] = stats["by_kind"].get(r["kind"], 0) + 1
        stats["by_board"][r["board"]] = stats["by_board"].get(r["board"], 0) + 1
        stats["by_exchange"][r["exchange"]] = stats["by_exchange"].get(r["exchange"], 0) + 1
        if r["kind"] == KIND_STOCK and r["board"] == BOARD_BSE:
            stats["stock_bse"] += 1
        if not r["is_active"]:
            stats["inactive"] += 1
        if "delist=name_suffix" in (r["source"] or ""):
            stats["delist_suspects"] = stats.get("delist_suspects", 0) + 1
        if r["kind"] != KIND_STOCK:
            stats["nonstock"].append({"code": r["code"], "name": r["name"],
                                      "kind": r["kind"]})

    if dry_run:
        reset_cache()
        return stats

    conn.executemany(_UPSERT_SQL, [tuple(r[c] for c in COLUMNS) for r in rows])
    # 收敛：本次没扫到的代码（历史脏代码/已下架）必须离开权威表，否则它会永远
    # 躺在"全量宇宙"里骗下一个读库的人。
    cur = conn.execute("DELETE FROM security_master WHERE COALESCE(as_of,'') <> ?", (as_of,))
    stats["orphans_removed"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    reset_cache()

    if sync_universe:
        stats["universe_inserted"] = sync_universe_from_master(conn, stats["as_of_date"])
        reset_cache()
    stats["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
    return stats


def sync_universe_from_master(conn: sqlite3.Connection, as_of_date: str) -> int:
    """把主表里的股票补齐到 a_shares_universe（**只 INSERT，不删不改**）。

    只补股票：可转债/ETF 本来就该留在白名单外，补进去等于把污染扩大。
    返回新插入行数。
    """
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='a_shares_universe'").fetchone()
    if not exists:
        return 0
    present = {c for (c,) in conn.execute("SELECT code FROM a_shares_universe")}
    todo = [r for r in load(conn, force=True)
            if r["kind"] == KIND_STOCK and r["code"] not in present]
    if not todo:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO a_shares_universe "
        "(code, name, board, exchange, status, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?)",
        [(r["code"], r["name"], r["board"], exchange_zh(r["exchange"]),
          "listed", as_of_date, as_of_date) for r in todo])
    conn.commit()
    return len(todo)
