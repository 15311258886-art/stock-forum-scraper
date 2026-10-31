"""板块资金流 · **日期区间**聚合（2026-10-05 王总要求）。

背景（这是用户当场揪出来的口径错误）：
  旧「板块资金流分时曲线」标题写「当日累计净流入」，纵轴其实是**当日开盘起逐分钟累加**；
  界面既没标日期、也没给「我要看 9 月 24 号到 9 月 30 号怎么流」的入口。
  更糟的是曲线只取「净流入 Top N」（按 main_net 降序取），**净流出板块根本没进图**，
  于是右上角「净流入 16 / 净流出 0」是拿"进图人数"硬凑的假统计，图里自然一条负线都看不到。

本模块做的事（三件，缺一不可）：
  1. `board_flow_daily` 表：把东财 `fflow/daykline`（klt=101）的日线主力净额**落库**，
     区间看板第二次打开就是毫秒级；落表前按 (trade_date, kind, board_code) 主键去重。
  2. `range_flow()`：按**起止日期区间**聚合，流出板块与流入板块**同图呈现**（0 轴上下），
     并给出**全池**净流入/净流出真数（不再是"进图人数"）。
  3. 所有日期一律过**交易日历闸**（`auction.market_trade_days`），
     传入的起止日若落在休市日，夹到就近交易日，绝不拿休市日当有效区间。

源说明（诚实口径）：
  日线历史走东财 `push2his.eastmoney.com/api/qt/stock/fflow/daykline/get`（lmt=0 → 约 120 个交易日），
  2026-10-05 实测可达，化学制药 09-30 主力净额 19.45 亿与当日分时尾值对得上。
  板块清单优先东财 `clist`（直接给 BK secid）；东财不可用时回退新浪快照按名称反查 secid。

零 JOIN、零外键，与既有业务表完全隔离（沿用 board_flow 的隔离纪律）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board_flow as bf  # noqa: E402
from board_flow import (  # noqa: E402
    DEFAULT_KIND, KINDS, _EM_QUOTE_REF, _EM_UTS, _http_text, _num, _ts,
    is_trading_day, latest_trade_date, market_trade_days,
)

logger = logging.getLogger("board_flow_range")

# ---- 常量（改数值前先看下面注释，别拍脑袋）----
DAILY_KEEP_DAYS = 40        # 只保留最近 40 个交易日：够看「近两月区间」，库也不会胀
DAY_TTL = 6 * 3600          # 一个 kind 的日线抓过之后，多久内不再重抓（秒）
FAIL_TTL = 300              # 抓失败冷却（秒）——失败不落缓存，但短时间别反复打同一个源
IDX_TTL = 6 * 3600          # 东财板块→secid 索引缓存（秒）
THREADS = 4                 # 并发拉板块日线（东财对并发也会掐，别开太大）
MEM_TOP = 16                # 区间图默认板块数（流入/流出各半）
POOL_LIMIT = 200            # 全池统计最多参与聚合的板块数（防止全市场概念刷爆）

_MEM: dict = {}
_LOCK = threading.Lock()
_BUSY = {"daily": False, "range": False}

# 板块日线主源（2026-10-30 新增）：东财 datacenter 报表。
# 为什么换源（实测证据，不是口味问题）：push2 的 `clist` 对本机已完全不可达 ——
# `_http_text` 直连与系统代理两条通道都返回空（1.10s，None），而
# `datacenter-web.eastmoney.com` 稳定可达，且一次请求就给出**全板块 + BK 码 + 主力净额 + 净占比**
# （实测 industry 128 个板块 0.26s / concept 410 个板块 3 页 0.67s）。
# 因此新链路：「按日拉全板块」，每天 4 个请求；旧链路 `_em_daykline` 保留做深历史（每板块 1 请求）。
_FF_REPORT = {"industry": "RPT_INDUSTRY_FUNDFLOW",
              "concept": "RPT_CONCEPT_FUNDFLOW"}
SRC_DC = "em_datacenter"     # board_flow_daily.src 的取值，前端按它标注数据来源
_DC_PAGE_SIZE = 200          # 单页上限（东财硬限 200）
_DC_MAX_PAGES = 6            # 概念板块约 410 个 → 3 页；留 6 页余量，防板块扩容


# ------------------------------------------------------------------ 建表
def ensure_schema(conn) -> None:
    """日线落库表（幂等）。与 board_flow.ensure_schema 分开调用也能建。"""
    if conn is None:
        return
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS board_flow_daily (
            trade_date TEXT NOT NULL,   -- 交易日 YYYY-MM-DD
            kind       TEXT NOT NULL,   -- industry / concept
            board_code TEXT NOT NULL,   -- 东财板块码（BK0xxx）
            board_name TEXT NOT NULL,
            main_net   REAL,            -- 当日主力净额（元，正=流入）
            net_pct    REAL,            -- 主力净占比（%）
            src        TEXT,            -- 数据源标注
            PRIMARY KEY (trade_date, kind, board_code)
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bfd_dk "
                     "ON board_flow_daily(trade_date, kind)")
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.debug("board_flow_daily 建表失败", exc_info=True)


def _new_conn():
    """后台线程不能复用一个 sqlite 连接（check_same_thread），现开一个。

    2026-10-09：原先 import `viewer.database`（该模块并不存在，真名是 `viewer.db`，
    server.py 亦从 `viewer.db` 导入），导致 conn=None 分支恒返回 None。改为优先
    `viewer.db`，并兼容旧名 `viewer.database`。
    """
    for mod in ("viewer.db", "viewer.database"):
        try:
            m = __import__(mod, fromlist=["Database"])
            return m.Database().conn
        except Exception:  # noqa: BLE001
            continue
    return None


# ------------------------------------------------------------------ 东财板块索引
def _em_board_index(kind: str) -> dict:
    """东财板块 → secid 索引：`{BK0xxx: name, name: BK0xxx}`。

    clist 是东财里最不容易被掐的一个端点（2026-10-05 实测 t:2/t:3 均 200），
    一次拿全量板块，缓存 6 小时。失败返回空表——调用方要自己有兜底。
    """
    key = "idx:%s" % kind
    hit = _MEM.get(key)
    if hit and time.time() - hit[1] < IDX_TTL:
        return hit[0]
    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    url = ("https://push2.eastmoney.com/api/qt/clist?fid=f62&po=1&pz=400&pn=1&np=1"
           "&fltt=2&invt=2&fs=%s&fields=f12,f14" % fs)
    txt = _http_text(url, _EM_QUOTE_REF, timeout=10, retries=1)
    out, by_name = {}, {}
    if txt:
        try:
            j = json.loads(txt)
            for it in ((j.get("data") or {}).get("diff") or []):
                code = str(it.get("f12") or "").strip()
                name = str(it.get("f14") or "").strip()
                if not code or not name or not code.startswith("BK"):
                    continue
                out[code] = name
                by_name[name] = code
        except Exception:  # noqa: BLE001
            logger.debug("东财板块索引解析失败", exc_info=True)
    with _LOCK:
        _MEM[key] = ({"code": out, "name": by_name}, time.time())
    return _MEM[key][0]


def _board_targets(kind: str, limit: int = 0) -> list:
    """本 kind 要拉日线的板块清单 → [(code, name)]，东财优先、新浪按名兜底。

    ⚠️ 新浪快照给的 `board_code` 是它自己的 category（`new_dzxx` 这类），
    东财 daykline 只认 `BK0xxx` secid，所以新浪模式下必须回过一次名称索引，
    匹配不上的直接跳过（宁可少几个板块，也不拿错 code 去打请求）。
    """
    idx = _em_board_index(kind) or {"code": {}, "name": {}}
    picked, seen = [], set()

    def add(code, name):
        if not code or code in seen:
            return
        seen.add(code)
        picked.append((code, name))

    # ① 东财直给：clist 的 f12 就是 BK 码
    for code, name in idx.get("code", {}).items():
        add(code, name)
    # ①.5 东财 datacenter 报表兜底（2026-10-30 新增，实测必需）
    # push2 的 clist 对本机**已完全不可达**（直连与系统代理两条通道都返回空，实测 1.10s/None），
    # 于是上面 ① 恒为空、下面 ② 的「新浪名称 → BK 码」反查也恒为空（索引是空的），
    # `_board_targets` 因此**恒返回 []** → `ensure_daily` 恒返回「板块清单取不到」。
    # datacenter 的板块资金流报表直接带 BK 码，实测 industry 128 个 / concept 410 个。
    if len(picked) < 12:
        for code, name in _dc_targets(kind):
            add(code, name)
    # ② 新浪兜底：用名称去东财索引反查 secid
    if len(picked) < 12:
        try:
            snap = bf.snapshot(kind, pz=90)
        except Exception:  # noqa: BLE001
            snap = {"rows": []}
        for r in snap.get("rows", []) or []:
            code, name = str(r.get("board_code") or ""), str(r.get("board_name") or "")
            if code.startswith("BK"):
                add(code, name)
                continue
            sec = idx.get("name", {}).get(name)
            if sec:
                add(sec, name)
    if limit and len(picked) > limit:
        picked = picked[:limit]
    return picked


# ------------------------------------------------------------------ datacenter 报表（主源）
def _dc_targets(kind: str) -> list:
    """从东财 datacenter 板块资金流报表取「最近一个有数据交易日」的板块清单 → [(BK码, 名称)]。

    这是 `clist` 不可达时的板块宇宙来源（实测：clist 空响应，本函数 0.3s 给全量）。
    取不到返回 []，调用方继续走后面的兜底，绝不抛。
    """
    ds = _dc_latest_date(kind)
    if not ds:
        return []
    rows, _meta = _dc_day_rows(kind, ds)
    out, seen = [], set()
    for r in rows:
        code, name = r[2], r[3]
        if not code or not name or code in seen:
            continue
        seen.add(code)
        out.append((code, name))
    return out


def _dc_latest_date(kind: str) -> str:
    """该 kind 的报表里「最近一个有数据的交易日」；取不到返回 ""（不猜日期）。"""
    try:
        import emapi
        return emapi.latest_report_date(_FF_REPORT.get(kind) or _FF_REPORT["industry"]) or ""
    except Exception:  # noqa: BLE001
        logger.debug("取 datacenter 板块报表最新日期失败", exc_info=True)
        return ""


def _dc_day_rows(kind: str, date: str, page_size: int = _DC_PAGE_SIZE,
                 max_pages: int = _DC_MAX_PAGES) -> tuple:
    """某交易日「全板块」的日线数据 → (rows, meta)。

    rows 每项是 board_flow_daily 的整行元组：
      (trade_date, kind, board_code, board_name, main_net, net_pct, src)
    成本：industry 1 页（实测 128 行 / 0.26s），concept 3 页（实测 410 行 / 0.67s）。
    口径：`NET_INFLOW` = 主力净流入（元），`NET_INFLOW_RATIO` = 主力净占比（%），
          与 board_flow_daily 的 `main_net` / `net_pct` 语义一致（emapi 已在个股链路上验证过）。
    ⚠️ 不完整即不认：第 1 页就失败 → 返回空 rows（调用方按失败处理，不落半截数据）；
       后续页失败 → 保留已拿到的页（meta["partial"]=True），宁少不错，且**绝不用空值补位**。
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    report = _FF_REPORT.get(kind) or _FF_REPORT["industry"]
    meta = {"ok": False, "pages": 0, "partial": False, "code": None, "reason": ""}
    if not date:
        meta["reason"] = "日期为空"
        return [], meta
    try:
        import emapi
    except Exception:  # noqa: BLE001
        meta["reason"] = "emapi 不可用"
        return [], meta
    out, seen = [], set()
    for pn in range(1, max(1, int(max_pages)) + 1):
        r = emapi.datacenter(report, columns="ALL", flt="(TRADE_DATE='%s')" % date,
                             sort_columns="NET_INFLOW", sort_types="-1",
                             page_size=int(page_size), page_number=pn, timeout=12)
        if not r.get("ok"):
            meta["code"] = r.get("code")
            meta["reason"] = str(r.get("err") or "报表无数据")[:120]
            if pn == 1:
                return [], meta              # 首页就失败：整日算失败，不许落半截
            meta["partial"] = True           # 后续页失败：保留已拿到的那几页
            break
        got = r.get("rows") or []
        meta["pages"] = pn
        for x in got:
            code = str(x.get("BOARD_CODE") or "").strip()
            name = str(x.get("BOARD_NAME") or "").strip()
            net = _num(x.get("NET_INFLOW"))
            if not code or not name or net is None or code in seen:
                continue                      # 缺码/缺名/缺净额一律跳过，不拿 0 冒充
            seen.add(code)
            out.append((date, kind, code, name, net,
                        _num(x.get("NET_INFLOW_RATIO")), SRC_DC))
        if len(got) < int(page_size):
            break
    meta["ok"] = bool(out)
    if not out and not meta["reason"]:
        meta["reason"] = "报表返回空"
    return out, meta


# ------------------------------------------------------------------ 日线抓取
def _em_daykline(code: str, day_max: int = 400) -> list | None:
    """东财单板块日线主力净额 → [(date, main_net, net_pct)]，失败返回 None。

    `lmt=0` 一次给全（约 120 个交易日），比按日拖 120 次请求稳得多。
    """
    if not code:
        return None
    secid = code if code.startswith("90.") else "90." + code
    for ut in _EM_UTS:
        url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
               "?lmt=0&klt=101&secid=%s&fields1=f1,f2,f3,f7"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
               "&ut=%s&_=%s" % (secid, ut, _ts()))
        txt = _http_text(url, _EM_QUOTE_REF, timeout=10, retries=0)
        if not txt:
            continue
        try:
            j = json.loads(txt)
        except Exception:  # noqa: BLE001
            continue
        if j.get("rc") not in (0, None):
            continue
        klines = ((j.get("data") or {}).get("klines")) or []
        out, seen_dates = [], set()
        for line in klines:
            p = str(line).split(",")
            if len(p) < 2:
                continue
            d = p[0].strip()
            net = _num(p[1])
            if not d or net is None or d in seen_dates:
                continue
            seen_dates.add(d)
            out.append((d, net, _num(p[6]) if len(p) > 6 else None))
        if out:
            return out
    return None


def _persist_day(conn, kind: str, rows: list) -> int:
    if conn is None or not rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO board_flow_daily"
            "(trade_date, kind, board_code, board_name, main_net, net_pct, src) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        logger.debug("board_flow_daily 落库失败", exc_info=True)
        return 0


def ensure_daily(kind: str = DEFAULT_KIND, force: bool = False, conn=None) -> dict:
    """把本 kind 的板块日线**深历史**抓全并落库（每板块 1 请求，一次约 120 个交易日）。

    返回 {ok, boards, points, elapsed, reason}。
    成本（2026-10-30 实测）：板块数来自 clist→datacenter 兜底，
      industry 128 个、concept 410 个；并发 4 + 每板块节流 0.12s →
      单请求 0.14s 时约 (128/4)×0.26 ≈ 8s / (410/4)×0.26 ≈ 27s；
      源被限流时每请求 0.5s 起（实测限流态 0.5~0.6s 且返回空）→ 会显著变慢。
    ⚠️ 这条链路是**尽力而为**的历史补齐工具（`fflow/daykline` 会被限流）；
       日更主链路走 `run_daily_sync`（datacenter 按日整表，一天 4 个请求）。
    `force=True` 无视命中判据重抓；`ok=False` 只记 FAIL_TTL 短冷却，下次会自己重试
    （**失败不许当成功**）。
    """

    kind = kind if kind in KINDS else DEFAULT_KIND
    ck = "daily:%s" % kind
    now = time.time()
    if not force:
        hit = _MEM.get(ck)
        if hit and now - hit[1] < DAY_TTL:
            return dict(hit[0], cached=True)
    if _BUSY.get("daily"):
        return {"ok": False, "busy": True, "boards": 0, "points": 0, "reason": "正在抓取"}
    with _LOCK:
        _BUSY["daily"] = True
    own = conn is None
    if own:
        conn = _new_conn()
    if not force:
        # 库里已覆盖「最近一个交易日」→ 直接算命中，不白打一遍。
        # 2026-10-30 修复（**这就是停更 21 个交易日的直接原因**）：
        # 原判据是 `now - fresh[1] < DAY_TTL`，而 `fresh[1]` 恒等于 `time.time()`
        # （`_db_latest` 返回的是 (MAX(trade_date), 此刻时钟)，库里没有任何"写入时刻"），
        # 于是这个 TTL 条件**永远成立**；再叠上 `not _db_missing_recent(conn, kind, fresh[0])`
        # —— 而 `fresh[0]` 本身就是从库里 MAX(trade_date) 查出来的，那天必定有行，
        # 所以后半句也**恒为真**。两个恒真叠起来 = 整个闸门是恒真式：
        # 只要库里有任意一天的数据，`ensure_daily(force=False)` 就 0 请求返回 cached，
        # 永远不会再去抓。实测：库副本上调一次耗时 0.000s、HTTP 请求数 0、
        # 返回 {'ok': True, 'boards': 0, 'cached': True, 'latest': '2026-10-08'}。
        # 现在的判据是**语义判据**：库里最新日 >= 最近一个交易日 才算命中（见 _db_covers）。
        covered = _db_covers(conn, kind)
        if covered:
            if own and conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            with _LOCK:
                _BUSY["daily"] = False
            return {"ok": True, "boards": 0, "points": 0, "cached": True,
                    "latest": covered}

    t0 = time.time()
    targets = _board_targets(kind)
    if not targets:
        with _LOCK:
            _BUSY["daily"] = False
        # 失败只缓存 FAIL_TTL（300s），别按 DAY_TTL 把"抓不到"记成 6 小时的"已是最新"
        _MEM[ck] = ({"ok": False, "boards": 0, "points": 0,
                     "reason": "板块清单取不到（东财 clist / datacenter / 新浪快照都失败）"},
                    now - (DAY_TTL - FAIL_TTL))
        return _MEM[ck][0]
    rows, fails = [], 0

    def work(item):
        code, name = item
        pts = _em_daykline(code)
        time.sleep(0.12)      # 轻度节流：东财对并发密集请求会掐连接
        return code, name, pts

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        for code, name, pts in ex.map(work, targets):
            if not pts:
                fails += 1
                continue
            for d, net, pct in pts:
                if not d or d < _keep_from():
                    continue
                # 2026-10-30 修复：这一行原先是
                #   (d, kind, names.get("", "") or "", name, net, pct, "em_daykline")
                # `names.get("", "")` 是「用空 key 查板块名→码字典」，**恒返回 ""**，
                # 于是 217 个板块的 board_code 全写成空串，而主键是
                # (trade_date, kind, board_code) → 同一天 217 行互相 INSERT OR REPLACE 覆盖，
                # 最终只留下**最后 1 行**。实测复现：写入 2 行 → 库里该日 1 行、board_code=''。
                # 也就是说旧链路即使抓成功，也永远凑不出一张能看的区间图。
                rows.append((d, kind, code, name, net, pct, "em_daykline"))

    if rows:
        _persist_day(conn, kind, rows)
    elapsed = round(time.time() - t0, 1)
    # 2026-10-30 修复：`ok` 原先是**写死的 True**，于是「217 个板块一个都没抓到」
    # 也照样返回 ok=True 并被缓存 6 小时（DAY_TTL）→ 上游再也看不到失败。
    # 判据改成"真的落到了行"。失败只缓存 FAIL_TTL（300s），让下次能早点重试。
    ok = bool(rows)
    res = {"ok": ok, "boards": len(targets) - fails, "points": len(rows),
           "fails": fails, "elapsed": elapsed, "latest": _db_latest(conn, kind)}
    if not ok:
        res["reason"] = "全部板块抓取失败（源可能被风控限流）"
    if own and conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        _BUSY["daily"] = False
    _MEM[ck] = (res, now if ok else now - (DAY_TTL - FAIL_TTL))
    logger.info("板块资金流日线落库：%s，%d 板块 / %d 行 / %.1fs（失败 %d，ok=%s）",
                kind, res["boards"], len(rows), elapsed, fails, ok)
    return res



def _keep_from() -> str:
    return (datetime.now() - timedelta(days=DAILY_KEEP_DAYS * 2)).strftime("%Y-%m-%d")


def _db_latest(conn, kind: str):
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT MAX(trade_date) FROM board_flow_daily WHERE kind=?",
                         (kind,)).fetchone()
        return (r[0], time.time()) if r and r[0] else None
    except Exception:  # noqa: BLE001
        return None


def _db_missing_recent(conn, kind: str, latest: str) -> bool:
    """库里最新一天是不是_missing（通常=还没抓过 / 抓失败）。"""
    if conn is None or not latest:
        return True
    try:
        r = conn.execute(
            "SELECT COUNT(*) FROM board_flow_daily WHERE kind=? AND trade_date=?",
            (kind, latest)).fetchone()
        return not (r and r[0] > 0)
    except Exception:  # noqa: BLE001
        return True


def _db_covers(conn, kind: str, expect: str = "") -> str:
    """库里是否已覆盖「最近一个交易日」→ 覆盖则返回该日期，否则返回 ""。

    这是 `ensure_daily` 的唯一命中判据（2026-10-30 起）。
    与旧实现（`_db_latest` 的 TTL）的本质区别：**答案只由数据本身决定，不看时钟**。
    `expect` 留空则取权威交易日历的 `latest_trade_date()`；日历取不到时返回 ""
    （宁可多抓一次，也不谎报"已是最新"——这正是旧实现的反面）。
    """
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM board_flow_daily WHERE kind=?", (kind,)).fetchone()
        latest = (row[0] if row else None) or ""
    except Exception:  # noqa: BLE001
        return ""
    if not latest:
        return ""
    target = (expect or "").strip() or (latest_trade_date() or "")
    if not target:
        return ""
    return latest if latest >= target else ""


# ------------------------------------------------------------------ 交易日历闸
def _day_set():
    days = market_trade_days(400) or []
    return sorted(set(d for d in days if d))


def _clamp(date_s: str) -> tuple:
    """把任意日期夹到合法交易日 → (结果日期, 是否被校正过)。"""
    days = _day_set()
    if not days:
        return date_s, False
    latest = latest_trade_date() or days[-1]
    if date_s > latest:
        return latest, True
    ok = [d for d in days if d <= date_s]
    return (ok[-1] if ok else days[0]), True


# ------------------------------------------------------------------ 区间聚合
def range_flow(conn, start: str = "", end: str = "", kind: str = DEFAULT_KIND,
               top: int = MEM_TOP, force: bool = False) -> dict:
    """区间资金流聚合：**流入流出同图 + 全池真统计**。

    返回体关键字段（前端照抄，别自己造）：
      trade_days   区间内交易日序列
      groups       [{code,name,total,points:{date:亿},in_days,out_days}]  双向 Top（“图上的”）
      pool         [{code,name,total,in_days,out_days}]                   全池（“统计用真数”）
      stats        {days, boards, up, down, flat}          up/down 是**全池**计数
      warming      库里没数据时置 True（后台已开线程补抓，前端轮询即可）
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    top = max(4, min(int(top or MEM_TOP), 24))
    t0 = time.time()
    note = []

    # ① 日期区间过交易日历闸
    latest = latest_trade_date() or ""
    days_all = _day_set()
    s_raw, e_raw = (start or latest), (end or latest)
    s, _ = _clamp(s_raw)
    e, _ = _clamp(e_raw)
    if s != s_raw or e != e_raw:
        note.append("起止日期按交易日历校正为 %s ~ %s" % (s, e))
    if s > e:
        s, e = e, s
    if days_all:
        trade_days = [d for d in days_all if s <= d <= e]
    else:
        trade_days = []
    if not trade_days:
        trade_days = _db_days(conn, kind, s, e)

    # ② 读库
    rows = _db_range(conn, kind, s, e)
    src_tag, src_label = "em_daykline", "东方财富（日线历史）"
    # 来源标注要跟着库里真实 src 走：日线源被风控时，快照落库(sina_snapshot)顶上，
    # 若仍标「东方财富（日线历史）」就是口径漂移。按占比最高的 src 如实标注。
    _dbsrc = _db_src(conn, kind, s, e) if rows else ""
    if _dbsrc.startswith("sina"):
        src_tag, src_label = "sina_snapshot", "新浪财经（快照落库）"
    elif _dbsrc:
        src_tag, src_label = _dbsrc, "东方财富（日线历史）"

    if not rows:
        # 单日区间且日线库里没有 → 当日快照兜底。
        # 2026-10-05 实测：东财 push2 / push2his 对本机 IP **已封禁**（单发也是 0.3s 空响应，
        # 而新浪快照 0.08s 正常）。单日分布是王总最关心的一屏，不能因为日线源被掐就整块空白，
        # 于是走新浪当日快照（它本身就是「全板块 + 流入流出双向」的稳定源，0.08s）。
        if s == e:
            rows = _snapshot_day_rows(kind)
        if not rows:
            # 库里没有 → 后台补抓，先返回 warming，前端轮询（不许让用户干等 20 秒）
            _maybe_spawn(kind)
            return {"ok": True, "warming": True, "kind": kind,
                    "kind_label": KINDS[kind]["label"], "start": s, "end": e,
                    "trade_days": trade_days, "groups": [], "pool": [],
                    "stats": {"days": len(trade_days), "boards": 0,
                              "up": 0, "down": 0, "flat": 0},
                    "source": "em_daykline", "source_label": "东方财富（日线 · 抓取中）",
                    "note": note + [("日线历史源暂时取不到数据，正在后台重试；"
                                     "也可把起止日都设成同一天，直接看当日快照分布。")]}
        src_tag, src_label = "sina_snapshot", "新浪财经（当日快照）"
        note.append("日线历史源（东财 push2his）当前不可达，"
                    "单日分布改由当日快照提供（新浪财经）")

    agg, name_of = {}, {}
    # 单日快照兜底的 rows 只在内存里、没落库，_db_points 从 board_flow_daily 取必然为空，
    # 曲线会一个点都画不出来——而「单日」恰恰是王总最关心的那屏。所以顺手把当天净值
    # 记成点位，下面并进 points_map。（2026-10-05）
    day_points = {}
    for d, code, name, net in rows:
        if net is None:
            continue
        name_of[code] = name
        if src_tag == "sina_snapshot":
            day_points.setdefault(code, {})[d] = net
        a = agg.setdefault(code, {"total": 0.0, "in_days": 0, "out_days": 0, "days": set()})
        a["total"] += net
        a["days"].add(d)
        if net > 0:
            a["in_days"] += 1
        elif net < 0:
            a["out_days"] += 1

    pool = []
    for code, a in agg.items():
        pool.append({"code": code, "name": name_of.get(code, code),
                     "total": round(a["total"] / 1e8, 4),     # 折算「亿」
                     "in_days": a["in_days"], "out_days": a["out_days"],
                     "days": len(a["days"])})
    pool.sort(key=lambda x: x["total"], reverse=True)

    up = len([p for p in pool if p["total"] > 0])
    down = len([p for p in pool if p["total"] < 0])
    flat = len(pool) - up - down

    half = max(2, top // 2)
    in_picks = [p for p in pool if p["total"] > 0][:half]
    out_picks = [p for p in pool if p["total"] < 0][:half]
    picks = in_picks + out_picks

    # 逐日曲线（只给上图的板块，避免全池 200 条折线糊成一片）
    pts = _db_points(conn, kind, s, e, [p["code"] for p in picks])
    points_map = {c: {d: (v / 1e8 if v is not None else None) for d, v in m.items()}
                  for c, m in pts.items()}
    if day_points:
        for c, m in day_points.items():
            points_map.setdefault(c, {}).update(
                {d: (v / 1e8 if v is not None else None) for d, v in m.items()})
    groups = []
    for p in picks:
        m = points_map.get(p["code"], {})
        groups.append(dict(p, points=m))
    # 图例跟着数据走：没进 Top 的板块不画
    for g in groups:
        g["series"] = [(d, g["points"].get(d)) for d in trade_days]

    res = {
        "ok": True, "warming": False, "kind": kind,
        "kind_label": KINDS[kind]["label"], "start": s, "end": e,
        "start_raw": s_raw, "end_raw": e_raw,
        "trade_days": trade_days, "groups": groups, "pool": pool[:POOL_LIMIT],
        "stats": {"days": len(trade_days), "boards": len(pool), "up": up,
                  "down": down, "flat": flat},
        "source": src_tag, "source_label": src_label,
        "elapsed": round(time.time() - t0, 2),
        "note": note,
    }
    return res


def _maybe_spawn(kind: str) -> None:
    if _BUSY.get("range") or _BUSY.get("daily"):
        return
    with _LOCK:
        _BUSY["range"] = True

    def run():
        try:
            ensure_daily(kind, force=False)
        except Exception:  # noqa: BLE001
            logger.exception("区间日线后台补抓异常（已忽略）")
        finally:
            with _LOCK:
                _BUSY["range"] = False

    threading.Thread(target=run, name="bf-range-%s" % kind, daemon=True).start()


def _snapshot_day_rows(kind: str, limit: int = 200) -> list:
    """当日快照兜底：新浪全板块主力净额 → [(date, code, name, main_net)]。

    新浪快照本身就是**流入榜 + 流出榜双向合并**的（`_merge_two_way`），
    所以它天然能让「净流出的板块」出现在图上——这正是旧曲线（只按净额降序取 Top）
    看不见流出的根因。
    """
    try:
        snap = bf.snapshot(kind, pz=limit)
    except Exception:  # noqa: BLE001
        logger.debug("当日快照兜底取数失败", exc_info=True)
        return []
    d = snap.get("trade_date") or latest_trade_date() or ""
    out = []
    for r in (snap.get("rows") or []):
        code = str(r.get("board_code") or "")
        name = str(r.get("board_name") or "")
        net = _num(r.get("main_net"))
        if not code or not name or net is None:
            continue
        out.append((d, code, name, net))
    return out


def _db_range(conn, kind: str, s: str, e: str) -> list:
    if conn is None:
        return []
    try:
        return conn.execute(
            "SELECT trade_date, board_code, board_name, main_net FROM board_flow_daily "
            "WHERE kind=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date ASC",
            (kind, s, e)).fetchall()
    except Exception:  # noqa: BLE001
        logger.debug("board_flow_daily 区间查询失败", exc_info=True)
        return []


def _db_src(conn, kind: str, s: str, e: str) -> str:
    """库内该区间行里占比最高的来源标注（src），供 range_flow 如实标注数据来源。"""
    if conn is None:
        return ""
    try:
        r = conn.execute(
            "SELECT src, COUNT(*) c FROM board_flow_daily "
            "WHERE kind=? AND trade_date BETWEEN ? AND ? AND src IS NOT NULL "
            "GROUP BY src ORDER BY c DESC LIMIT 1", (kind, s, e)).fetchone()
        return (r[0] if r else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _db_days(conn, kind: str, s: str, e: str) -> list:
    if conn is None:
        return []
    try:
        r = conn.execute(
            "SELECT DISTINCT trade_date FROM board_flow_daily "
            "WHERE kind=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date ASC",
            (kind, s, e)).fetchall()
        return [x[0] for x in r]
    except Exception:  # noqa: BLE001
        return []


def _db_points(conn, kind: str, s: str, e: str, codes: list) -> dict:
    if conn is None or not codes:
        return {}
    ph = ",".join("?" * len(codes))
    try:
        rows = conn.execute(
            "SELECT board_code, trade_date, main_net FROM board_flow_daily "
            "WHERE kind=? AND trade_date BETWEEN ? AND ? AND board_code IN (%s)"
            % ph, (kind, s, e, *codes)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for code, d, net in rows:
        out.setdefault(code, {})[d] = net
    return out


# ------------------------------------------------------------------ 快照落库（push2his 被风控时的区间点来源）
def persist_snapshot(kind: str = DEFAULT_KIND, conn=None) -> dict:
    """把「当前板块快照」按**真实数据交易日**落进 board_flow_daily（幂等）。

    背景：日线历史走东财 push2his，本机被风控时 board_flow_daily 会一直为空，
    区间看板（除单日快照兜底外）就没有点可画。此函数把新浪板块快照（长期稳定的真实数据）
    按交易日落库，让区间看板在日线源不可用期间也能攒出曲线点。

    日期口径：用 emapi.latest_report_date()（「最近一个有资金流数据的交易日」）作为 trade_date，
    **而不是 datetime.now()**——次日盘前日历已是新交易日、但资金流数据仍属上一交易日，
    按 now() 落库会给旧数据盖新戳（本模块反复栽过的坑）。数据日取不到则**不落库**（rows=0），
    绝不写空行、绝不造假。返回 {ok, kind, rows, trade_date, reason}。
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    own = conn is None
    if own:
        conn = _new_conn()
    if conn is None:
        return {"ok": False, "kind": kind, "rows": 0, "trade_date": None, "reason": "无数据库连接"}
    ensure_schema(conn)
    data_date = None
    try:
        import emapi
        data_date = emapi.latest_report_date("RPT_DMSK_TS_STOCKNEW")
    except Exception:  # noqa: BLE001
        logger.debug("取数据交易日失败", exc_info=True)
    rows = []
    try:
        if data_date:
            in_rows = bf._sina_snapshot(kind, 200, asc=0)
            out_rows = bf._sina_snapshot(kind, 200, asc=1)
            for r in bf._merge_two_way(in_rows, out_rows):
                code = str(r.get("board_code") or "")
                name = str(r.get("board_name") or "")
                net = _num(r.get("main_net"))
                if not code or not name or net is None:
                    continue
                # net_pct 列语义 = 主力净占比(%)，新浪快照的 main_pct 即占比
                rows.append((data_date, kind, code, name, net,
                             _num(r.get("main_pct")), "sina_snapshot"))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO board_flow_daily"
                "(trade_date, kind, board_code, board_name, main_net, net_pct, src) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.debug("板块快照落库失败", exc_info=True)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if rows:
        logger.info("板块资金流快照落库：%s %s，%d 行", kind, data_date, len(rows))
    return {"ok": bool(rows), "kind": kind, "rows": len(rows), "trade_date": data_date,
            "reason": None if rows else "快照或数据交易日取不到，未落库（不造假）"}


# ------------------------------------------------------------------ 日期可选范围
def date_options(conn, kind: str = DEFAULT_KIND) -> dict:
    """给前端日期框用：可选起止 = 交易日历最近 N 天 ∪ 库内已有日线日期。"""
    days = _day_set()[-60:]
    have = []
    if conn is not None:
        try:
            have = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM board_flow_daily "
                "WHERE kind=? ORDER BY trade_date DESC LIMIT 40", (kind,)).fetchall()]
        except Exception:  # noqa: BLE001
            have = []
    all_days = sorted(set(days) | set(have))
    return {"ok": True, "kind": kind, "latest_trade_date": latest_trade_date(),
            "is_trading_day": is_trading_day(), "days": all_days,
            "min": all_days[0] if all_days else "", "max": all_days[-1] if all_days else "",
            "count": len(all_days)}


def refresh(kind: str = DEFAULT_KIND, force: bool = False, conn=None) -> dict:
    """手动触发一次日线抓取（前端「强制刷新」按钮 / 护栏用）。"""
    return ensure_daily(kind, force=bool(force), conn=conn)


# ============================================================================
# 日更同步（2026-10-30 新增）
#
# 【为什么会有这一块】`board_flow_daily` 停更 21 个交易日（库内 217 行全落在 2026-10-08）。
# 根因是三层叠在一起，缺一层都不会停：
#   ① **没人调**：唯一写入路径 `ensure_daily` 只被「区间面板请求」间接触发
#      （`range_flow` → `_maybe_spawn`），`tools/sched_job.py` 里没有任何 board_flow 分支，
#      `_sched_tasks()` 里也没有 —— 用户不打开那个面板，表就永远不更新；
#   ② **调了也不抓**：`ensure_daily(force=False)` 的命中闸是恒真式（详见该函数内注释），
#      实测库副本上 0.000s / 0 请求返回 cached；
#   ③ **抓了也白抓**：行构造把 board_code 写成空串，主键塌成「一天一行」（实测复现）。
# 所以这里做的是「按日落库 + 日更调度」这一层：不依赖用户打开面板，每天自己把表推到当前。
#
# 【为什么用 datacenter 而不是 fflow/daykline】
#   · daykline 是「每板块一个请求」：217 个板块 = 217 请求（并发 4、节流 0.12s → 约 14s），
#     而且板块清单来自 clist，clist 对本机**已完全不可达**（实测空响应）→ 连清单都拿不到；
#   · datacenter 报表是「每天一次请求拿全板块」：industry 1 页 128 行 / 0.26s，
#     concept 3 页 410 行 / 0.67s → **一天 4 个请求、约 0.9s**，且直接带 BK 码。
#     实测 2026-10-30 与 2026-10-09 均有数据，2026-08-03 也可回（可回溯至少 3 个月）。
#
# 【幂等与"失败不许当成功"】
#   · 落库一律 `INSERT OR REPLACE`，主键 (trade_date, kind, board_code) → 重复落库不膨胀；
#   · 闸门是 `last_date`（**只在成功时写**），`last_run_at` 无条件写但不参与判定；
#   · 报表「无数据」（东财 code=9201，常见于非交易日/当日尚未生成）与「网络失败」都算失败：
#     不写 `last_date`，只累加 `tries`，次日自动恢复。
# ============================================================================
STATE_KEY = "board_flow_range_daily_state"
DB_PATH = os.path.join(_HERE, "viewer", "app.db")
LOG_FILE = os.path.join(_HERE, "viewer", "logs", "board_flow_range_daily.log")

PUBLISH_HOUR = 18        # 日线收盘数据已定；排在 lhb_daily(18:00) 之后，别抢同一条写域
PUBLISH_MIN = 30
MAX_TRIES = 4            # 当天连续失败上限（防限流打爆数据源），次日自动恢复
TRAIL_DAYS = 3           # 每轮顺带重扫最近 N 个交易日（幂等，漏一天能自愈）
SYNC_KINDS = ("industry", "concept")

# 2026-10-30 D33 事故的同款防线：测试永远不许写生产。
# 事故形态（真实发生过，见 lhb_daily.py 文件头）：pytest 进程内被替换的实现起了孤儿线程，
# 线程活过 monkeypatch 撤销后看到的 DB 路径又是生产路径 → 把桩数据写进生产 meta，
# 顶掉当晚真正该跑的那一轮。这里同样用 PYTEST_CURRENT_TEST 判据先拦一道。
_PROD_DB = os.path.abspath(DB_PATH)
_PROD_LOG = os.path.abspath(LOG_FILE)

_SYNC_LOCK = threading.Lock()
_SYNC_RUNNING = {"v": False}     # 本进程内是否有一轮正在跑（闸门职责，不是"有线程在跑"）
_STATE_CONN_FACTORY = None       # 测试/调用方可以注入；默认开 DB_PATH


def _prod_write_blocked(path: str, prod: str) -> bool:
    """pytest 环境下、且目标仍是生产路径 → 拦下这次写。返回 True 表示已拦截。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return os.path.abspath(path) == prod
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- meta 读写
def ensure_meta_schema(conn) -> None:
    """幂等：确保 meta 键值表存在（其它模块已在用，这里只是兜底）。"""
    if conn is None:
        return
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.debug("board_flow_range.ensure_meta_schema 失败（已忽略）", exc_info=True)


def _meta_get(conn, key: str, default=None):
    """读 meta[key]，兼容 row_factory=Row 与普通 tuple。"""
    if conn is None:
        return default
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return row["value"]
        except (TypeError, IndexError):
            return row[0]
    except Exception:  # noqa: BLE001
        logger.debug("读 meta[%s] 失败", key, exc_info=True)
        return default


def _meta_set(conn, key: str, value) -> bool:
    """写 meta[key]（UPSERT）。返回是否写成功（调用方据此判"闸门是否可信"）。"""
    if conn is None:
        return False
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("写 meta[%s] 失败", key, exc_info=True)
        return False


def _state_read(conn) -> dict:
    raw = _meta_get(conn, STATE_KEY)
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            logger.debug("解析 %s 失败，退回空状态", STATE_KEY, exc_info=True)
    return {}


def _state_write(conn, st: dict) -> bool:
    return _meta_set(conn, STATE_KEY, json.dumps(st, ensure_ascii=False))


# ---------------------------------------------------------------- 时间/类别闸
def _past_publish(now: datetime) -> bool:
    """是否已过当日刷新时点（默认 18:30）。"""
    return (now.hour, now.minute) >= (PUBLISH_HOUR, PUBLISH_MIN)


def _non_trading(today: str) -> bool:
    """今天是否「确定不是交易日」。日历取不到（None）时**不拦**——宁可空跑一次也不漏更。"""
    try:
        v = is_trading_day(today)
    except Exception:  # noqa: BLE001
        return False
    return v is False


def _gate(conn, now: datetime | None = None, force: bool = False) -> tuple:
    """判断「这一轮该不该跑」→ `(skip, today)`；`skip` 非 None 表示本轮跳过。

    **手工跑（`main()` / `run_daily_sync(force=True)`）与调度跑（sched_job 子进程）共用这一份判断。**
    两处各写一套的教训在本项目已经吃过一次（lhb_daily 的「手工跑得成、调度跑不成」），
    所以这里刻意做成唯一入口：`force` 只跳过「时间窗/非交易日」两道，
    **永远不跳过**「当日已成功」与「失败达上限」——手动重跑也不许绕过幂等。

    判断顺序：非交易日 → 未到 18:30 → 今日已成功 → 今日失败达上限 → 已有实例在跑。
    ⚠️ 闸门是 `last_date`（只在成功时写），不是 `last_run_at`（无条件写，只给巡检看）。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not force:
        if now.weekday() >= 5:
            return {"skip": "weekend"}, today
        if _non_trading(today):
            return {"skip": "non_trading_day"}, today
        if not _past_publish(now):
            return {"skip": "pre_publish"}, today
    st = _state_read(conn)
    if st.get("last_date") == today:
        return {"skip": "done_today"}, today
    tries = int(st.get("tries", 0) or 0) if st.get("tries_date") == today else 0
    if tries >= MAX_TRIES:
        return {"skip": "failed_today"}, today
    with _SYNC_LOCK:
        if _SYNC_RUNNING["v"]:
            return {"skip": "running"}, today
    return None, today


# ---------------------------------------------------------------- 单日 / 区间落库
def sync_day(kind: str = DEFAULT_KIND, conn=None, date: str = "",
             purge_sina: bool = False) -> dict:
    """把**某一天**的全板块日线落库（幂等）。返回
    `{ok, kind, trade_date, rows, boards, pages, reason, purged}`。

    `date` 留空 → 用东财报表的「最近一个有数据的交易日」（绝不拿日历日冒充数据日）。
    失败（无数据/网络不可达/落库异常）→ `ok=False` 且 **不写任何"已完成"标记**。
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    own = conn is None
    if own:
        conn = _open_conn()
    if conn is None:
        return {"ok": False, "kind": kind, "trade_date": date, "rows": 0, "boards": 0,
                "reason": "无数据库连接"}
    ensure_schema(conn)
    ds = (date or "").strip() or _dc_latest_date(kind)
    if not ds:
        return {"ok": False, "kind": kind, "trade_date": "", "rows": 0, "boards": 0,
                "reason": "取不到数据交易日"}
    try:
        rows, meta = _dc_day_rows(kind, ds)
        if not rows:
            return {"ok": False, "kind": kind, "trade_date": ds, "rows": 0, "boards": 0,
                    "pages": meta.get("pages"), "reason": meta.get("reason") or "报表无数据"}
        purged = 0
        if purge_sina:
            purged = _purge_src(conn, ds, kind, "sina_snapshot")
        n = _persist_day(conn, kind, rows)
        if n <= 0:
            return {"ok": False, "kind": kind, "trade_date": ds, "rows": 0,
                    "boards": 0, "reason": "落库失败"}
        return {"ok": True, "kind": kind, "trade_date": ds, "rows": n,
                "boards": len({r[2] for r in rows}), "pages": meta.get("pages"),
                "source": SRC_DC, "purged": purged, "reason": None}
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _purge_src(conn, date: str, kind: str, src: str) -> int:
    """删掉某日某 kind 下某个来源的行（用于 10-08 那批 sina_snapshot 与新 BK 码并存时收口）。"""
    try:
        cur = conn.execute(
            "DELETE FROM board_flow_daily WHERE trade_date=? AND kind=? AND src=?",
            (date, kind, src))
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:  # noqa: BLE001
        logger.debug("清理 %s/%s/%s 失败", date, kind, src, exc_info=True)
        return 0


def _recent_trade_days(n: int = TRAIL_DAYS, end: str = "") -> list:
    """最近 n 个交易日（升序），以 `end` 为右端点；日历取不到返回 []。"""
    days = _day_set()
    if not days:
        return []
    end = (end or "").strip() or (latest_trade_date() or "")
    if end:
        days = [d for d in days if d <= end]
    return days[-max(1, int(n)):] if days else []


def sync_trail(conn, kinds=SYNC_KINDS, days: int = TRAIL_DAYS, end: str = "") -> list:
    """重扫最近 N 个交易日（幂等）。返回每个 (date, kind) 的结果清单。"""
    out = []
    for d in _recent_trade_days(days, end):
        for kind in kinds:
            r = sync_day(kind, conn, d)
            r["date"] = d
            out.append(r)
    return out


def sync_range(conn, start: str, end: str, kinds=SYNC_KINDS,
               purge_sina: bool = False, sleep: float = 0.15) -> dict:
    """**区间回填入口**（可复用，不必做成 CLI）：把 [start, end] 内每个交易日 × 每个 kind 落库。

    成本：每「交易日×kind」1~3 个请求（industry 1 页 / concept 3 页），实测约 0.9s/交易日；
    16 个交易日 ≈ 64 个请求 ≈ 15s。逐日之间 sleep 0.15s 防限流。
    幂等：全部 `INSERT OR REPLACE`，重复跑不膨胀、不产生重复板块。
    返回 `{ok, start, end, days, days_ok, days_failed, rows, results, elapsed}`。
    """
    t0 = time.time()
    days = [d for d in (_day_set() or []) if start <= d <= end]
    results, rows_total, ok_days, failed = [], 0, [], []
    if not days:
        return {"ok": False, "start": start, "end": end, "days": 0, "days_ok": 0,
                "days_failed": 0, "rows": 0, "results": [],
                "elapsed": 0.0, "reason": "区间内没有交易日（或交易日历取不到）"}
    for d in days:
        day_ok = True
        for kind in kinds:
            r = sync_day(kind, conn, d, purge_sina=purge_sina)
            results.append(r)
            rows_total += int(r.get("rows") or 0)
            day_ok = day_ok and bool(r.get("ok"))
            time.sleep(max(0.0, float(sleep)))
        (ok_days if day_ok else failed).append(d)
    return {"ok": not failed, "start": start, "end": end, "days": len(days),
            "days_ok": len(ok_days), "days_failed": len(failed),
            "failed_dates": failed, "rows": rows_total, "results": results,
            "elapsed": round(time.time() - t0, 1)}


def _missing_pairs(conn, start: str, end: str, kinds=SYNC_KINDS) -> list:
    """区间内「库里没有行」的 (date, kind) 清单（按交易日历逐日核）。"""
    days = [d for d in (_day_set() or []) if start <= d <= end]
    out = []
    for d in days:
        for kind in kinds:
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM board_flow_daily WHERE trade_date=? AND kind=?",
                    (d, kind)).fetchone()[0]
            except Exception:  # noqa: BLE001
                n = 0
            if not n:
                out.append((d, kind))
    return out


def backfill(conn, start: str, end: str, kinds=SYNC_KINDS,
             daykline_fallback: bool = True, purge_sina: bool = False) -> dict:
    """**区间回填入口**（可复用；今晚 lhb_daily 跑完之后再执行）。

    两步走，因为两个 kind 的历史可得性不同（2026-10-30 实测）：
      ① **datacenter 逐日扫**（`sync_range`）：industry 有完整历史（实测 2026-07-01 仍可取，
         128 行/天、1 请求/天）；concept 报表**只保留最新一天**（10-27/10-28/10-29 均返回
         code=9201「返回数据为空」，只有当天可读）。
      ② 对 ① 之后仍缺的日子，用 **`ensure_daily(force=True)`** 走东财 `fflow/daykline`
         （每板块 1 请求、一次给约 120 个交易日）补齐 —— 这是 concept 历史的**唯一**来源。
         注意该通道对本机**会限流**：实测连续十几次请求后开始返回空（0.5s/None），
         需要隔一段时间再跑。所以 ② 是「尽力而为」，失败只报告不谎报。

    返回 `{ok, start, end, days, rows, missing, daykline, elapsed}`；
    `missing` 是 ② 之后仍然没有数据的 (date, kind) 清单 —— 空才代表真的补齐了。
    """
    t0 = time.time()
    out = {"ok": False, "start": start, "end": end, "rows": 0, "missing": [],
           "daykline": [], "purged": 0, "elapsed": 0.0}
    own = conn is None
    if own:
        conn = _open_conn()
    if conn is None:
        out["reason"] = "无数据库连接"
        return out
    try:
        ensure_schema(conn)
        sweep = sync_range(conn, start, end, kinds=kinds, purge_sina=purge_sina)
        out["rows"] = int(sweep.get("rows") or 0)
        out["sweep"] = {k: sweep.get(k) for k in ("days", "days_ok", "days_failed",
                                                  "failed_dates", "elapsed")}
        missing = _missing_pairs(conn, start, end, kinds)
        if missing and daykline_fallback:
            todo = []
            for _d, k in missing:
                if k not in todo:
                    todo.append(k)
            for k in todo:
                r = ensure_daily(k, force=True, conn=conn)
                out["daykline"].append({"kind": k, "ok": bool(r.get("ok")),
                                        "boards": r.get("boards"), "points": r.get("points"),
                                        "reason": r.get("reason")})
            missing = _missing_pairs(conn, start, end, kinds)
        out["missing"] = ["%s/%s" % (d, k) for d, k in missing]
        out["ok"] = not missing
        if not out["ok"]:
            out["reason"] = ("仍有 %d 个「交易日×kind」没有数据；concept 历史只能走 "
                             "fflow/daykline（当前可能被限流），隔 10~30 分钟重跑同一命令即可续上"
                             % len(missing))
        out["elapsed"] = round(time.time() - t0, 1)
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _open_conn():
    """开一个可写连接（state 需要写 meta；读路径仍在 ensure_daily 里走 _new_conn）。"""
    if _STATE_CONN_FACTORY is not None:
        return _STATE_CONN_FACTORY()
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con
    except Exception:  # noqa: BLE001
        logger.debug("打开 app.db 失败", exc_info=True)
        return _new_conn()


# ---------------------------------------------------------------- 结果回写
def _append_result_log(res: dict) -> None:
    """把每一轮结果追加到 viewer/logs/board_flow_range_daily.log（供巡检/证据留存）。"""
    if _prod_write_blocked(LOG_FILE, _PROD_LOG):
        logger.warning("写 board_flow_range_daily.log 被拦下：pytest 环境且指向生产日志 %s", LOG_FILE)
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        line = ("=== %s~%s ok=%s target=%s rows=%s " % (
            res.get("started"), res.get("ended"), res.get("ok"),
            res.get("target"), res.get("rows")) + "; ".join(
            "%s(%s,%s行)" % (r.get("kind"), r.get("trade_date"), r.get("rows"))
            for r in res.get("days", [])) + " ===\n")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        logger.debug("写 board_flow_range_daily.log 失败", exc_info=True)


def record_result(conn, res: dict, today: str) -> bool:
    """把一轮结果写回 meta（当日去重 / 失败计数）。**只有 ok 才写 last_date**。"""
    if _prod_write_blocked(DB_PATH, _PROD_DB):
        logger.warning("record_result 被拦下：pytest 环境且指向生产库 %s（不落盘）", DB_PATH)
        return False
    ensure_meta_schema(conn)
    st = _state_read(conn)
    st["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["last_result"] = {k: res.get(k) for k in ("ok", "started", "ended", "target", "rows")}
    if res.get("ok"):
        st["last_date"] = today
        st["last_ok_at"] = st["last_run_at"]
        st["last_data_date"] = res.get("target")
        st["tries"] = 0
        st["last_error"] = None
    else:
        if st.get("tries_date") != today:
            st["tries_date"] = today
            st["tries"] = 1
        else:
            st["tries"] = int(st.get("tries", 0) or 0) + 1
        st["last_error"] = str(res.get("reason") or "unknown")[:200]
    ok_write = _state_write(conn, st)
    _append_result_log(res)
    return ok_write


# ---------------------------------------------------------------- 调度入口
def run_daily_sync(conn=None, force: bool = False, kinds=SYNC_KINDS) -> dict:
    """**同步**跑完一轮日更，跑完才返回（供 `tools/sched_job.py` 的子进程调用）。

    为什么必须同步：`sched_job.py` 是**短命子进程**，print 完就 `return 0` 退出；
    CPython 关解释器会直接杀掉 daemon 线程 → "起了线程就返回"的写法会让这一轮一行都没跑，
    而退出码仍是 0。本项目已经为此吃过大亏（lhb_daily 骗了 11 天 / bili_scan 从未触发过），
    所以这里不用线程、不用后台任务，一路同步到底。

    成功判据（不含糊）：**报表里最新数据日 >= 权威交易日历的最近交易日**，
    且该日两个 kind 都真的落了行。否则 `ok=False`，父进程据此判 rc=1 并退避重试。
    """
    own = conn is None
    if own:
        conn = _open_conn()
    skip, today = _gate(conn, force=force)
    if skip is not None:
        logger.info("board_flow_range 日更跳过：%s", skip.get("skip"))
        return skip
    with _SYNC_LOCK:
        if _SYNC_RUNNING["v"]:
            return {"skip": "running"}
        _SYNC_RUNNING["v"] = True
    res = {"ok": False, "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ended": None, "target": "", "rows": 0, "days": [], "reason": None}
    try:
        # last_trigger_at 只给巡检看，**不是**闸门（闸门是 last_date）
        try:
            st = _state_read(conn)
            st["last_trigger_at"] = res["started"]
            _state_write(conn, st)
        except Exception:  # noqa: BLE001
            logger.debug("写 last_trigger_at 失败（已忽略）", exc_info=True)

        expected = latest_trade_date() or today
        target = _dc_latest_date(kinds[0]) or ""
        res["target"] = target or expected
        # `latest_report_date` 带 10 分钟缓存，且会自动回退找"最近一个有数据的日子"，
        # 所以这里不能直接拿它当"今天有没有数据"的判据 —— 先按日历取目标日，再看库。
        trail = sync_trail(conn, kinds=kinds, days=TRAIL_DAYS, end=expected)
        res["days"] = trail
        res["rows"] = sum(int(r.get("rows") or 0) for r in trail)
        covered = all(_db_covers(conn, k, expect=expected) for k in kinds)
        res["ok"] = bool(covered and res["rows"] > 0)
        if not res["ok"]:
            res["reason"] = ("库里最新日仍未覆盖 %s（报表最新数据日 %s；当日数据可能尚未生成）"
                             % (expected, target or "未知"))
        res["ended"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_result(conn, res, today)
        logger.info("board_flow_range 日更结束 ok=%s target=%s rows=%s",
                    res["ok"], res["target"], res["rows"])
        return res
    except Exception as e:  # noqa: BLE001
        res["ended"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        res["ok"] = False
        res["reason"] = "%s: %s" % (type(e).__name__, str(e)[:150])
        logger.exception("board_flow_range 日更异常")
        try:
            record_result(conn, res, today)
        except Exception:  # noqa: BLE001
            logger.debug("失败轮状态回写也失败（已忽略）", exc_info=True)
        return res
    finally:
        with _SYNC_LOCK:
            _SYNC_RUNNING["v"] = False
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def maybe_daily(conn=None) -> dict:
    """调度钩子别名（与 `run_daily_sync` 同义，异常在这里吞掉但返回值带 ok）。

    保留这个名是因为 `tools/sched_job.py` 的惯例是 `mod.maybe_daily(conn)`；
    但**行为必须同步**（见 `run_daily_sync` 里"短命子进程"的说明）。
    """
    try:
        return run_daily_sync(conn)
    except Exception:  # noqa: BLE001 —— 调度钩子绝不外抛
        logger.exception("board_flow_range.maybe_daily 异常（已忽略）")
        return {"error": "exception", "ok": False}


def status(conn=None) -> dict:
    """当前日更状态（供 API / 巡检读取）。"""
    own = conn is None
    if own:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=20)
        except Exception:  # noqa: BLE001
            conn = None
    try:
        st = _state_read(conn)
        st["running"] = bool(_SYNC_RUNNING["v"])
        st["window"] = "%02d:%02d" % (PUBLISH_HOUR, PUBLISH_MIN)
        st["max_tries"] = MAX_TRIES
        st["state_key"] = STATE_KEY
        return st
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 独立入口
def main(argv=None) -> int:
    """`python board_flow_range.py [--status|--force|--backfill 起 止]`（运维 / 手工补跑用）。"""
    import argparse

    ap = argparse.ArgumentParser(description="板块资金流日线日更 / 回填（东财 datacenter 真实数据）")
    ap.add_argument("--status", action="store_true", help="只打印当前日更状态")
    ap.add_argument("--force", action="store_true", help="忽略时间窗与交易日闸（仍受当日幂等约束）")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"),
                    help="回填区间（YYYY-MM-DD YYYY-MM-DD），逐交易日落库 + 缺口走 daykline 兜底")
    ap.add_argument("--purge-sina", action="store_true",
                    help="回填时顺带删除同日同 kind 的 sina_snapshot 旧行（10-08 那批并存数据收口用）")
    ap.add_argument("--no-daykline", action="store_true",
                    help="回填时不要用 fflow/daykline 兜底（该通道会被限流）")
    ap.add_argument("--kinds", default=",".join(SYNC_KINDS), help="industry,concept")
    args = ap.parse_args(argv)

    kinds = tuple(k.strip() for k in str(args.kinds).split(",") if k.strip() in KINDS) or SYNC_KINDS

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2), flush=True)
        return 0

    conn = _open_conn()
    if conn is None:
        print("[board_flow_range] 打不开数据库", flush=True)
        return 1
    try:
        if args.backfill:
            r = backfill(conn, args.backfill[0], args.backfill[1], kinds=kinds,
                         daykline_fallback=not args.no_daykline,
                         purge_sina=bool(args.purge_sina))
            print("[board_flow_range] backfill " + json.dumps(
                {k: r.get(k) for k in ("ok", "start", "end", "rows", "missing",
                                       "daykline", "sweep", "elapsed", "reason")},
                ensure_ascii=False, default=str)[:1200], flush=True)
            return 0 if r.get("ok") else 1
        r = run_daily_sync(conn, force=bool(args.force), kinds=kinds)
        print("[board_flow_range] " + json.dumps(r, ensure_ascii=False, default=str)[:800],
              flush=True)
        if r.get("skip"):
            return 0
        return 0 if r.get("ok") else 1
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())

