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
    """把本 kind 的板块日线抓全并落库。返回 {ok, boards, points, elapsed, reason}。

    冷启动约 48 个板块 × 1 请求 ≈ 十几秒（落库后二次命中就是毫秒级）。
    `force=True` 无视 TTL 重抓；失败只记短冷却，下次会自己重试（**失败不许当成功**）。
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
        # 库里已有最近交易日且 TTL 未过 → 直接算命中，不白打一遍
        fresh = _db_latest(conn, kind)
        if fresh and now - fresh[1] < DAY_TTL and not _db_missing_recent(conn, kind, fresh[0]):
            if own and conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            with _LOCK:
                _BUSY["daily"] = False
            return {"ok": True, "boards": 0, "points": 0, "cached": True,
                    "latest": fresh[0]}
    t0 = time.time()
    targets = _board_targets(kind)
    if not targets:
        with _LOCK:
            _BUSY["daily"] = False
        _MEM[ck] = ({"ok": False, "boards": 0, "points": 0,
                     "reason": "板块清单取不到（东财 clist 与新浪快照都失败）"}, now)
        return _MEM[ck][0]
    rows, fails = [], 0
    names = dict((c, n) for c, n in targets)

    def work(item):
        code, name = item
        pts = _em_daykline(code)
        time.sleep(0.12)      # 轻度节流：东财对并发密集请求会掐连接
        return name, pts

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        for name, pts in ex.map(work, targets):
            if not pts:
                fails += 1
                continue
            for d, net, pct in pts:
                if not d or d < _keep_from():
                    continue
                rows.append((d, kind, names.get("", "") or "", name, net, pct, "em_daykline"))
    if rows:
        _persist_day(conn, kind, rows)
    elapsed = round(time.time() - t0, 1)
    res = {"ok": True, "boards": len(targets) - fails, "points": len(rows),
           "fails": fails, "elapsed": elapsed, "latest": _db_latest(conn, kind)}
    if own and conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        _BUSY["daily"] = False
    _MEM[ck] = (res, now)
    logger.info("板块资金流日线落库：%s，%d 板块 / %d 行 / %.1fs（失败 %d）",
                kind, res["boards"], len(rows), elapsed, fails)
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
