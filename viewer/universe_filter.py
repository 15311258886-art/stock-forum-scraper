# -*- coding: utf-8 -*-
"""干净宇宙 / 剔除池 —— 选股的统一"负面清单"入口（只含客观硬垃圾）。

设计（2026-10-26 修正，进哥指出"博主反向信号有时效，不能永久剔除"）：
  剔除池只保留「客观、无时效争议」的硬垃圾：ST/退市/风险(st)、无K线(no_kline)、
  极弱流动性(illiquid)。**博主信号不作为剔除理由** —— 历史曾把 blogger_noise（80 只）
  永久剔除，已于 2026-10-26 释放回干净宇宙。
  2026-10-30 补充：当时的「带时效警示 overlay」也已下线（源停更 + 拿全期快照绑个股），
  避坑改由 viewer/avoid.py 按当日量价实时算。本模块因此与博主数据彻底无关联。

  注：本模块仅负责读取与过滤，**不做写操作**（写库在 tools/build_exclusion.py），避免循环依赖。

────────────────────────────────────────────────────────────────────────────
2026-10-31 整改（测评 P0-B，本模块负责"消费侧"这一层）：

原实现有两个洞，合起来会让 ST 股静默进入推荐名单：

  ① 读路径里调 `_ensure_table(con)` —— **CREATE TABLE IF NOT EXISTS**。
     表不存在时它不是报错，而是建一张空表，然后查出 0 行。
     于是"表丢了"和"池子确实是空的"在结果上完全同形。

  ② 调用方（viewer/indicators.py）写的是
     `try: … get_exclusion_pool() … except Exception: excl = set()`。
     读失败 = 空池 = 一只都不剔，而且不写日志、不中断。

  两处叠起来：**一次读失败 ⇒ st / no_kline / illiquid 全部不剔除 ⇒ ST 可以进名单，
  且没有任何告警、不中断。** 这不是"名单可能过期"（时效问题），
  而是"名单可能在本该剔除的那一刻没有剔除，而没有人知道"（正确性问题）。

整改后的三层接口，按"要不要为失败负责"分工：
  · `get_exclusion_pool()`  —— 保持原签名与原返回，供展示/统计用；**不抛错**，
    但池不可用时会打 ERROR 日志（不再静默），并把健康状态写进 `pool_health()`。
  · `get_exclusion_pool_strict()` —— 选股路径专用。池缺失 / 0 行 / 超龄 一律抛
    `ExclusionPoolUnavailable`，由调用方决定是"停一轮"还是"停下来等人"。
  · `pool_health()` —— 结构化体检：行数、pool_asof、账龄、各理由计数、是否 stale。

  另：`5,588`（stock_names 全表）与"过滤后只数"是两个谓词下的计数，不得并列、不得相减。
  本模块只报"扫描域"内的计数（分母 = stock_names 全表），并显式标注 `denominator`。
"""
import os
import time
import sqlite3
import logging

# 白名单（与 viewer/config.EXCLUSION_REASONS 同源）。任何非白名单的剔除理由
# （历史上曾误塞 blogger_noise）一律在读取时被忽略并告警 —— 从机制上杜绝
# 「把噪声/观点塞进剔除池、永久剥夺标的资格」的回归（2026-10-26 进哥指出）。
try:
    from viewer.config import EXCLUSION_REASONS
except Exception:  # noqa: BLE001
    EXCLUSION_REASONS = ("st", "no_kline", "illiquid")
VALID_REASONS = set(EXCLUSION_REASONS)

try:
    from viewer.config import POOL_MAX_STALENESS_DAYS
except Exception:  # noqa: BLE001
    POOL_MAX_STALENESS_DAYS = 7

logger = logging.getLogger(__name__)

_DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")

# 理由中文标签（前端展示用）。注意：blogger_noise 已于 2026-10-26 弃用（改时效 overlay）。
REASON_LABELS = {
    "st": "ST/退市/风险",
    "no_kline": "无K线(无法分析)",
    "illiquid": "极弱流动性",
}

_CACHE = {"ts": 0.0, "excluded": None, "rows": None, "counts": None, "health": None}
_LAST_ERR_LOG = {"ts": 0.0}
_ERR_LOG_GAP = 60.0


class ExclusionPoolUnavailable(RuntimeError):
    """剔除池不可用（表缺失 / 0 行 / 超龄）。

    专门用一个异常类型，是为了让调用方无法"顺手"把它当成空池继续跑 ——
    `except Exception: excl = set()` 这种写法会把 ValueError 也吞掉，
    所以关键不在这条异常叫什么，而在选股路径必须显式地**不**吞它。
    """


def _connect():
    return sqlite3.connect(_DB_PATH, timeout=30)


def _ensure_table(con):
    """建表 —— **只给写路径用**（本模块已不调用它，保留给外部工具与单测）。

    2026-10-31：原 `get_exclusion_pool()` 在读路径里调用了这个函数。
    它把"表不存在"变成"空表"再变成"0 行"，于是失败与正常同形。
    读路径现在只查 sqlite_master，绝不建表 —— **读操作不得修改被读对象的结构**。
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS exclusion_pool ("
        "code TEXT PRIMARY KEY, reason TEXT NOT NULL, detail TEXT, updated_at TEXT)"
    )


def _table_exists(con, name="exclusion_pool"):
    r = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(r)


def _parse_dt(s):
    """解析 'YYYY-MM-DD HH:MM:SS'；解析不了返回 None（不要拿异常当控制流）。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(str(s)[:19], fmt)
        except Exception:  # noqa: BLE001
            continue
    return None


def pool_health(force=False):
    """结构化体检：{"ok", "error", "rows", "pool_asof", "age_days", "stale",
    "batch_id", "writer", "by_reason", "denominator"}。

    `denominator` 恒为 "stock_names 全表" —— 本模块所有计数都只在扫描域内成立。
    """
    import time as _t
    now = _t.time()
    if not force and _CACHE["health"] is not None and now - _CACHE["ts"] < 60:
        return _CACHE["health"]
    h = {"ok": False, "error": None, "rows": 0, "pool_asof": None, "age_days": None,
         "stale": False, "batch_id": None, "writer": None, "by_reason": {},
         "denominator": "stock_names 全表", "max_staleness_days": POOL_MAX_STALENESS_DAYS}
    try:
        con = _connect()
        try:
            if not _table_exists(con):
                h["error"] = "exclusion_pool 表不存在"
                return h
            cols = set(r[1] for r in con.execute("PRAGMA table_info(exclusion_pool)"))
            h["rows"] = int(con.execute("SELECT COUNT(*) FROM exclusion_pool").fetchone()[0])
            for reason, n in con.execute(
                    "SELECT reason, COUNT(*) FROM exclusion_pool GROUP BY reason").fetchall():
                h["by_reason"][reason] = int(n)
            if "pool_asof" in cols:
                r = con.execute("SELECT MAX(pool_asof) FROM exclusion_pool").fetchone()
                h["pool_asof"] = r[0] if r else None
            if not h["pool_asof"]:
                r = con.execute("SELECT MAX(updated_at) FROM exclusion_pool").fetchone()
                h["pool_asof"] = r[0] if r else None
            if "batch_id" in cols:
                r = con.execute(
                    "SELECT batch_id, writer FROM exclusion_pool LIMIT 1").fetchone()
                if r:
                    h["batch_id"], h["writer"] = r[0], r[1]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        h["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        return h

    dt = _parse_dt(h["pool_asof"])
    if dt is not None:
        try:
            from datetime import datetime
            h["age_days"] = round((datetime.now() - dt).total_seconds() / 86400.0, 2)
            h["stale"] = bool(h["age_days"] > POOL_MAX_STALENESS_DAYS)
        except Exception:  # noqa: BLE001
            pass
    if h["rows"] == 0:
        h["error"] = "exclusion_pool 为 0 行（与『表丢失』同形，按不可用处理）"
    elif h["error"] is None:
        h["ok"] = True
    return h


def get_exclusion_pool(force=False):
    """返回 (set_of_excluded_codes, [(code,reason,detail),...], {reason: count})。带 60s 缓存。

    强制准入（2026-10-26）：只接受 VALID_REASONS 白名单内的理由；非白名单
    （如历史上误塞的 blogger_noise）一律忽略并告警，绝不影响干净宇宙。

    ⚠️ 本函数**不抛错**（展示/统计路径用），但池不可用时会打 ERROR ——
    要"为失败负责"的选股路径请用 `get_exclusion_pool_strict()`。
    """
    import time as _t
    now = _t.time()
    if not force and _CACHE["excluded"] is not None and now - _CACHE["ts"] < 60:
        return _CACHE["excluded"], _CACHE["rows"], _CACHE["counts"]
    health = pool_health(force=True)
    rows_all = []
    if health["ok"] or health["rows"] > 0:
        try:
            con = _connect()
            try:
                rows_all = con.execute(
                    "SELECT code, reason, COALESCE(detail,'') FROM exclusion_pool").fetchall()
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            health["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])

    if not rows_all:
        # 2026-10-31 整改：此处原为"静默返回空集"。
        # 空集在下游等于"一只都不剔"，而它与"池子坏了"完全同形 —— 必须留声。
        # 限速 60s，避免每 tick 刷屏把真问题淹掉。
        if now - _LAST_ERR_LOG["ts"] >= _ERR_LOG_GAP:
            _LAST_ERR_LOG["ts"] = now
            logger.error(
                "[universe_filter] 剔除池不可用（%s）—— 下游若把它当空池，"
                "ST/无K线/极弱流动性将全部**不被剔除**。health=%s",
                health.get("error"), health)
    bad = [(c, r) for c, r, _ in rows_all if r not in VALID_REASONS]
    if bad:
        logger.warning(
            "[universe_filter] exclusion_pool 含 %d 条非白名单理由(已忽略,不影响干净宇宙): %s",
            len(bad), bad[:10])
    rows = [(c, r, d) for c, r, d in rows_all if r in VALID_REASONS]
    excluded = set(r[0] for r in rows)
    counts = {}
    for _, reason, _ in rows:
        counts[reason] = counts.get(reason, 0) + 1
    _CACHE["excluded"] = excluded
    _CACHE["rows"] = rows
    _CACHE["counts"] = counts
    _CACHE["health"] = health
    _CACHE["ts"] = now
    return excluded, rows, counts


def get_exclusion_pool_strict(force=False, allow_stale=False):
    """选股路径专用：池不可用（缺失 / 0 行 / 超龄）直接抛 `ExclusionPoolUnavailable`。

    为什么要有这个函数，而不是让 `get_exclusion_pool` 自己抛：
    它有 7 个调用方，其中 3 个是展示/统计（服务器面板、覆盖率审计、持有人抓取），
    那些地方"读不到就不显示"是可接受的；而 indicators / universe_scan / pillars /
    strategy_sim 这四处决定**谁能进候选名单**，读不到就必须停。
    两种责任的失败方式本来就该不同，共用一个入口就一定有一边是错的。

    allow_stale=True 时只对"缺失/0 行"抛错，超龄仅告警 —— 用于"明知道池子旧了
    但今天必须有结果"的场景（必须在调用处写明理由）。
    """
    health = pool_health(force=True)
    if not health["ok"]:
        raise ExclusionPoolUnavailable(health.get("error") or "exclusion_pool 不可用")
    if health["stale"] and not allow_stale:
        raise ExclusionPoolUnavailable(
            "exclusion_pool 已超龄：pool_asof=%s，账龄 %s 天 > 上限 %s 天 "
            "（10-30 之后新增的 ST 不会自动进池，继续用等于用旧名单做筛选）"
            % (health["pool_asof"], health["age_days"], POOL_MAX_STALENESS_DAYS))
    if health["stale"]:
        logger.warning("[universe_filter] 使用超龄剔除池（allow_stale=True）：asof=%s age=%s 天",
                       health["pool_asof"], health["age_days"])
    return get_exclusion_pool(force=force)


def assert_clean_pool(con=None, strict=False):
    """硬校验：exclusion_pool 全表 reason 必须 ∈ 白名单。返回违例列表 [(code,reason)]。

    strict=True 时遇到违例直接抛 ValueError（用于启动自检 / CI）；否则仅返回违例。
    配合 get_exclusion_pool 的软过滤，形成「读取即忽略 + 运维可硬查」双保险。
    """
    own = con is None
    if con is None:
        con = _connect()
    try:
        if not _table_exists(con):
            return []
        rows = con.execute("SELECT code, reason FROM exclusion_pool").fetchall()
    finally:
        if own:
            con.close()
    violations = [(c, r) for c, r in rows if r not in VALID_REASONS]
    if strict and violations:
        raise ValueError("exclusion_pool 含非白名单理由: %r" % violations[:20])
    return violations


def clean_codes(codes, strict=False):
    """从给定候选列表里剔除 exclusion_pool 中的票。

    strict=True → 池不可用时抛错（选股路径必须用这个）。
    默认 False 是为兼容既有 7 个调用方，但**新增代码一律传 strict=True**。
    """
    excluded, _, _ = (get_exclusion_pool_strict() if strict else get_exclusion_pool())
    return [c for c in codes if c not in excluded]


def clean_universe_size():
    """干净宇宙规模（名称字典 − 剔除池）。"""
    con = _connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    except Exception:  # noqa: BLE001
        total = 0
    con.close()
    excluded, _, _ = get_exclusion_pool()
    return total - len(excluded), total, len(excluded)


def exclusion_breakdown():
    """给前端的透明报告：各理由剔除数量 + 干净宇宙规模 + 理由标签 + 池龄。

    2026-10-31 新增 pool 字段：`rows / pool_asof / age_days / stale / ok / error / batch_id`。
    面板上必须能一眼看出"这份名单是什么时候的、是谁写的" —— 之前四列里没有任何一列能回答。
    """
    excluded, rows, counts = get_exclusion_pool()
    clean, total, excl = clean_universe_size()
    h = pool_health()
    return {
        "total_a": total,
        "excluded_total": excl,
        "clean_universe": clean,
        "by_reason": {r: counts.get(r, 0) for r in REASON_LABELS},
        "labels": REASON_LABELS,
        "denominator": "stock_names 全表",
        "pool": {
            "ok": h.get("ok"), "error": h.get("error"), "rows": h.get("rows"),
            "pool_asof": h.get("pool_asof"), "age_days": h.get("age_days"),
            "stale": h.get("stale"), "batch_id": h.get("batch_id"),
            "writer": h.get("writer"),
            "max_staleness_days": h.get("max_staleness_days"),
        },
    }


def all_ashares(con=None):
    """名称字典里所有 A 股格式代码（6 位、首位 0/3/6/8/9）。"""
    own = con is None
    if con is None:
        con = _connect()
    try:
        rows = con.execute("SELECT code FROM stock_names").fetchall()
    except Exception:
        rows = []
    if own:
        con.close()
    out = []
    for (c,) in rows:
        c = str(c or "").strip()
        if len(c) == 6 and c.isdigit() and c[0] in "03689":
            out.append(c)
    return out


def evaluation_universe(con=None):
    """权威『评估宇宙』= 有因子快照且已剔除垃圾的干净宇宙（P2 统一入口）。"""
    own = con is None
    if con is None:
        con = _connect()
    try:
        rows = con.execute("SELECT code FROM stock_indicators").fetchall()
    except Exception:
        rows = []
    if own:
        con.close()
    return clean_codes([str(r[0]) for r in rows], strict=True)


if __name__ == "__main__":
    bd = exclusion_breakdown()
    print("全A:", bd["total_a"])
    print("剔除合计:", bd["excluded_total"])
    print("干净宇宙(可入选):", bd["clean_universe"])
    print("按理由:", bd["by_reason"])
    print("池状态:", bd["pool"])
    # 准入自检（2026-10-26 强制断言）：剔除池是否混入非白名单理由
    viol = assert_clean_pool(strict=False)
    if viol:
        print("[自检·告警] exclusion_pool 含 %d 条非白名单理由(已被读取时忽略): %s" % (len(viol), viol[:10]))
    else:
        print("[自检·OK] exclusion_pool 理由全部在白名单内")
    # 池健康自检（2026-10-31 新增）：缺失/0 行/超龄 一律 RED
    h = pool_health(force=True)
    if not h["ok"]:
        print("[自检·RED] 剔除池不可用：%s" % h["error"])
    elif h["stale"]:
        print("[自检·警告] 剔除池已超龄 %.2f 天（asof=%s，上限 %s 天）"
              % (h["age_days"], h["pool_asof"], h["max_staleness_days"]))
    else:
        print("[自检·OK] 剔除池可用：%d 行 / asof=%s / 账龄 %.2f 天 / batch=%s / writer=%s"
              % (h["rows"], h["pool_asof"], h["age_days"] or 0, h["batch_id"], h["writer"]))
