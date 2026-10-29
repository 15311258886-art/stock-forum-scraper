# -*- coding: utf-8 -*-
"""选股方向引擎（2026-10-30）。

这个模块只做两件事，两件都在本系统严格口径下实测过，不是引用别人的说法：

【一、短线 T+1：实测无正超额形态 —— 否定性结论】
口径：d 日收盘出信号 → d+1 开盘买入 → d+1+h 开盘卖出（T+1 制度下限）；
     超额 = 个股收益 − 同日全市场等权（双方均剔上市未满 120 交易日的新股）；
     d+1 开盘即涨停判为买不进，剔除；基准另剔 |单日收益| > 35% 的异常值；
     t 值按日聚合后对日度超额序列做单样本检验。
样本：2016-01 ~ 2026-10，5,573 只，2,505 个交易日。

  超跌类（用户直觉的「超跌修复」）
    跌 5% + 破 MA20        h=1  超额 −0.17%  t=−5.8
    跌 7% + 破 MA20        h=1  超额 −0.24%  t=−6.9
    三日累跌 12%            h=1  超额 −0.40%  t=−10.8
    跌 9% + 破 MA20 深度 8%  h=1  超额 −0.76%  t=−10.3
  动量类（反向假设：追强势）
    涨停（全部）            h=1  超额 −0.72%  t=−17.0
    首板                    h=1  超额 −0.64%  t=−16.2
    二连板                  h=1  超额 −1.39%  t=−13.0
    放量突破（涨4%+量比2）    h=1  超额 −0.22%  t=−8.4
    创 60 日新高 + 放量       h=1  超额 −0.26%  t=−9.3

  两边都是负超额，且都高度显著。含义：在日线信号 + 次日开盘执行这个层面，
  A 股短线不存在可用的正超额形态；这条数据只能用来做风控（判断「不该买」），
  不能反过来当买入信号。

【二、中长期底部横盘：风险调整后有优势，但不是收益优势】
样本：2013-01 ~ 2026-10，持仓期 500 个交易日（约 2 年）。
关键是把「底部形态」拆成三种，实测差别极大：

  一直低波（前 250 日振幅≤45% 且 近 250 日≤45%）
      期末 +24.7%  胜率 65.9%  最大回撤 −20.4%  曾涨≥100% 概率 19.3%
      超额 +7.4%（相对全市场），胜率 +16.3%     ← 最优
  全市场基准
      期末 +17.2%  胜率 49.6%  最大回撤 −31.2%  曾涨≥100% 概率 23.2%
  曾活跃后沉寂（前 250 日振幅>90% 且 近 250 日≤45%）
      期末  −0.6%  胜率 35.0%  最大回撤 −38.5%  曾涨≥100% 概率 12.6%
      超额 −17.8%，胜率 −14.7%                   ← 最差

  也就是说：「曾经活跃、现在横盘蓄势」这一类在 A 股是最差的，
  2 年还亏钱、胜率只有三成半。真正有效的是「一直死气沉沉」的低波股。
  这与用户的直觉相反，但数据在 2013-2026、63 万个样本点上极为一致。

  300 个交易日（约 1 年）尺度上横盘组超额为负 → 优势只在 2 年尺度显现。
  分年度不稳健：2018、2024 这类全市场强势年，低波组会显著跑输。

  所以这个池子的定位是「用更小的回撤换更高的胜率」，
  不是「抓暴涨」。它的价值在风险端。想要暴涨的形态，数据说没有。

口径边界（必须随结论一起披露）：
  · 日线全库 data/kline_daily.db 为前复权，但仅保留 13 只退市股，
    长期持有类结论存在幸存者偏差，实际收益应低于本表。
  · 无法还原历史 ST 状态，回测未剔除 ST 股，真实可交易收益更差。
  · 基本面表 fundamentals 仅 1,101 条单期快照（2025-09 起），
    无历史时间序列，因此「按巴菲特标准选股」无法做历史验证，
    只能在当前池子上做标注，不得当作已验证因子。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import defaultdict

log = logging.getLogger("selection")

_HERE = os.path.dirname(os.path.abspath(__file__))
_KLINE = os.path.join(_HERE, "..", "data", "kline_daily.db")

# 横盘分层阈值（250 日振幅）
TIER_A = 0.25
TIER_B = 0.32
TIER_C = 0.45

# 池子过滤
# 必须同时满足「前 250 日振幅 ≤45%」与「近 250 日振幅 ≤45%」——
# 即「一直低波」。实测这是唯一有效的底部形态：
# 2 年期末 +24.7%、胜率 65.9%、回撤 −20.4%（全市场 +17.2% / 49.6% / −31.2%）。
# 反例对照：「曾活跃(>90%)后沉寂(≤45%)」2 年 −0.6%、胜率 35.0%，是最差的一组。
MIN_AMT = 1.0e7          # 最新成交额低于 1000 万剔掉（买不进卖不出）
MIN_BARS = 520           # 需要 250+250 两段窗口
# 池子上限（2026-10-30 由 300 提到 1200）：
# 原 300 会截断 —— 实测满足条件的 609 只只入库 300 只，漏掉 51%（且丢的全是
# C 级，等效于偷偷收紧了入池标准）。SQLite 存 1000 行毫无压力，故放到 1200，
# 正常行情下不再截断；真被截断时 payload.capped=True 会被前端显式标出。
POOL_MAX = 1200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sel_pool (
    asof        TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    board       TEXT,
    close       REAL,
    amp250      REAL,
    amp_prev    REAL,
    pos250      REAL,
    vr20        REAL,
    vol250      REAL,
    amt         REAL,
    chg_1y      REAL,
    tier        TEXT,
    roe         REAL,
    rev_yoy     REAL,
    np_yoy      REAL,
    PRIMARY KEY (asof, code)
);
CREATE INDEX IF NOT EXISTS ix_sel_pool_asof ON sel_pool(asof);

CREATE TABLE IF NOT EXISTS sel_evidence (
    key         TEXT PRIMARY KEY,
    kind        TEXT,
    title       TEXT,
    horizon     INTEGER,
    n           INTEGER,
    final_ret   REAL,           -- 期末/绝对收益均值 %
    excess      REAL,           -- 相对全市场等权超额 %
    t           REAL,
    win         REAL,           -- 胜率 %
    mdd         REAL,           -- 最大回撤均值 %
    note        TEXT,
    tested_at   TEXT,
    span        TEXT
);

CREATE TABLE IF NOT EXISTS sel_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。不缓存全局布尔——换连接/换库时那样会漏建。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def _kline_conn() -> sqlite3.Connection | None:
    if not os.path.exists(_KLINE):
        return None
    try:
        c = sqlite3.connect(_KLINE, timeout=20)
        c.execute("PRAGMA cache_size=-100000")
        return c
    except Exception:  # noqa: BLE001
        log.exception("[selection] 打不开日线库")
        return None


def _names(conn: sqlite3.Connection) -> dict:
    out = {}
    try:
        for code, name, board in conn.execute(
                "SELECT code,name,board FROM a_shares_universe"):
            out[code] = (name or "", board or "")
    except Exception:  # noqa: BLE001
        pass
    return out


def _fund(conn: sqlite3.Connection) -> dict:
    """当前基本面快照。仅用于标注，不参与任何打分。"""
    out = {}
    try:
        for sym, roe, rev, np_ in conn.execute(
                "SELECT symbol,roe,rev_yoy,np_yoy FROM fundamentals"):
            out[sym] = (roe, rev, np_)
    except Exception:  # noqa: BLE001
        pass
    return out


def _bad_code(code: str) -> bool:
    """北交所流动性太差、科创/创业涨跌幅规则不同，池子统一排除北交所。"""
    return code[:2] == "92" or code[:1] in ("4", "8")


def _bad_name(name: str) -> bool:
    u = (name or "").upper().replace(" ", "")
    return ("ST" in u) or ("退" in u) or (not name)


def _amp(seg) -> float | None:
    """250 日振幅 =（区间最高−最低）/ 区间均价。"""
    if not seg:
        return None
    lo, hi = min(seg), max(seg)
    if lo <= 0:
        return None
    m = sum(seg) / len(seg)
    return (hi - lo) / m if m > 0 else None


def refresh_pool(conn: sqlite3.Connection, keep_days: int = 60) -> dict:
    """算今日「低波池」。读最近 600 根日线，约 10~15 秒。"""
    ensure_schema(conn)
    kc = _kline_conn()
    if kc is None:
        return {"ok": False, "error": "日线库不可用"}
    t0 = time.time()

    last = kc.execute("SELECT MAX(d) FROM kline").fetchone()[0]
    if not last:
        return {"ok": False, "error": "日线库为空"}
    start = kc.execute(
        "SELECT MIN(d) FROM (SELECT DISTINCT d FROM kline WHERE d<=? "
        "ORDER BY d DESC LIMIT 600)", (last,)).fetchone()[0]

    rows = kc.execute(
        "SELECT code,d,c,v FROM kline WHERE d>=? AND d<=? AND c>0 "
        "ORDER BY code,d", (start, last))
    names = _names(conn)
    funds = _fund(conn)

    pool = []
    cc, buf = None, []
    def flush(code, bars):
        if len(bars) < MIN_BARS:
            return
        cl = [b[1] for b in bars]
        vl = [b[2] for b in bars]
        seg = cl[-250:]
        prev = cl[-500:-250]
        amp = _amp(seg)
        amp_prev = _amp(prev)
        if amp is None or amp_prev is None:
            return
        if amp > TIER_C or amp_prev > TIER_C:
            return
        amt = vl[-1] * cl[-1]
        if amt < MIN_AMT:
            return
        nm, board = names.get(code, ("", ""))
        if _bad_code(code) or _bad_name(nm):
            return
        v20 = sum(vl[-20:]) / 20.0
        vr = (vl[-1] / v20) if v20 > 0 else 1.0
        rs = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
        vol = (sum(x * x for x in rs) / len(rs)) ** 0.5 if rs else 0.0
        lo, hi = min(seg), max(seg)
        pos = (cl[-1] - lo) / (hi - lo) if hi > lo else 0.5
        chg1y = (cl[-1] / seg[0] - 1.0) if seg[0] > 0 else None
        tier = "A" if amp <= TIER_A else ("B" if amp <= TIER_B else "C")
        roe, rev, np_ = funds.get(code, (None, None, None))
        pool.append((last, code, nm, board, cl[-1], amp, amp_prev, pos, vr, vol,
                     amt, chg1y, tier, roe, rev, np_))

    for code, d, c, v in rows:
        if code != cc:
            if buf:
                flush(cc, buf)
            cc, buf = code, []
        buf.append((d, c, v or 0.0))
    if buf:
        flush(cc, buf)
    kc.close()

    pool.sort(key=lambda r: (r[12], r[5]))
    n_candidates = len(pool)          # 截断前（审计用：POOL_MAX 是否在漏股）
    pool = pool[:POOL_MAX]

    cur = conn.cursor()
    cur.execute("DELETE FROM sel_pool WHERE asof=?", (last,))
    cur.executemany(
        "INSERT OR REPLACE INTO sel_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        pool)
    cur.execute("DELETE FROM sel_pool WHERE asof < (SELECT MIN(asof) FROM "
                "(SELECT DISTINCT asof FROM sel_pool ORDER BY asof DESC LIMIT ?))",
                (keep_days,))
    conn.commit()

    cnt = defaultdict(int)
    for r in pool:
        cnt[r[12]] += 1
    # 审计留痕：截断前候选数 vs 实际入库数（2026-10-30 覆盖度排查新增）
    try:
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("selection_pool_candidates", str(n_candidates)),
             ("selection_pool_saved", str(len(pool))),
             ("selection_pool_capped", "1" if n_candidates > len(pool) else "0")])
        conn.commit()
    except Exception:  # noqa: BLE001 —— meta 结构异常不影响主流程
        pass
    return {"ok": True, "asof": last, "n": len(pool), "candidates": n_candidates,
            "capped": n_candidates > len(pool), "tier": dict(cnt),
            "elapsed": round(time.time() - t0, 1)}


def count_pool_candidates(conn) -> dict:
    """读取最近一次入池统计（截断前后只数），供 tools/audit_coverage.py 审计。

    返回 {"n": 满足条件只数, "saved": 实际入库只数}；无记录时返回 0。
    注意这只反映「上一次 refresh_pool」的结果，不做实时重算（重算要 10~15s）。
    """
    out = {"n": 0, "saved": 0}
    try:
        for k, key in (("selection_pool_candidates", "n"),
                       ("selection_pool_saved", "saved")):
            r = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            if r and r[0]:
                out[key] = int(r[0])
    except Exception:  # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------------ 证据回测
def _zt_thr(code: str) -> float:
    if code[:2] in ("30", "68"):
        return 0.198
    if code[:3] in ("43", "83", "87", "88", "92"):
        return 0.295
    return 0.098


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _tstat(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    return (m / (sd / n ** 0.5)) if sd > 0 else None


def _iter(conn, since):
    cur = conn.execute(
        "SELECT code,d,o,c,h,l,v FROM kline WHERE d>=? AND o>0 AND c>0 "
        "ORDER BY code,d", (since,))
    cc, buf = None, []
    for code, d, o, cl, h, lo, v in cur:
        if code != cc:
            if buf:
                yield cc, buf
            cc, buf = code, []
        buf.append((d, o, cl, h, lo, v or 0.0))
    if buf:
        yield cc, buf


def refresh_evidence(conn: sqlite3.Connection, full: bool = True) -> dict:
    """重跑两类验证并落表。约 90~150 秒，只在月度/手动触发。"""
    ensure_schema(conn)
    kc = _kline_conn()
    if kc is None:
        return {"ok": False, "error": "日线库不可用"}
    t0 = time.time()
    out = {}

    # ---- 短线：基准 + 候选形态 ----
    since = 20160101
    acc = defaultdict(lambda: [0, 0.0])
    for code, bars in _iter(kc, since):
        n = len(bars)
        for i in range(120, n - 2):
            o1, o2 = bars[i + 1][1], bars[i + 2][1]
            if o1 > 0 and o2 > 0:
                x = o2 / o1 - 1.0
                if -0.35 < x < 0.35:
                    c = acc[bars[i][0]]
                    c[0] += 1
                    c[1] += x
    bench = {d: s / k for d, (k, s) in acc.items() if k >= 300}
    del acc

    cases = [
        ("short_drop5", "超跌：跌5%+破MA20", "超跌", -0.05, 0, 0.03, None),
        ("short_drop9", "超跌：跌9%+破MA20深度8%", "超跌", -0.09, 0, 0.08, None),
        ("short_drop3d", "超跌：三日累跌12%", "超跌", None, 3, 0.03, None),
        ("short_zt", "动量：涨停（全部）", "动量", "ZT", 0, 0, None),
        ("short_zt1", "动量：首板", "动量", "ZT1", 0, 0, None),
        ("short_zt2", "动量：二连板", "动量", "ZT2", 0, 0, None),
        ("short_brk", "动量：放量突破（涨4%+量比2）", "动量", "BRK", 0, 0, None),
        ("short_hi60", "动量：创60日新高+放量", "动量", "HI60", 0, 0, None),
    ]
    st = {}
    for key, title, kind, a, b, c_, d_ in cases:
        st[key] = [0, 0.0, defaultdict(lambda: [0, 0.0])]
    base = [0, 0.0, defaultdict(lambda: [0, 0.0])]

    for code, bars in _iter(kc, since):
        n = len(bars)
        if n < 130:
            continue
        cl = [x[2] for x in bars]
        vl = [x[5] for x in bars]
        thr = _zt_thr(code)
        ma20 = sum(cl[:20])
        for i in range(n):
            if i >= 20:
                ma20 += cl[i] - cl[i - 20]
            if i < 120 or i > n - 2:
                continue
            d0 = bars[i][0]
            bm = bench.get(d0)
            if bm is None:
                continue
            o1 = bars[i + 1][1]
            if o1 <= 0 or o1 >= cl[i] * (1 + thr) * 0.998:
                continue
            m20 = ma20 / 20.0
            if m20 <= 0:
                continue
            r1 = cl[i] / cl[i - 1] - 1.0 if cl[i - 1] > 0 else 0.0
            r2 = cl[i - 1] / cl[i - 2] - 1.0 if cl[i - 2] > 0 else 0.0
            v20 = sum(vl[i - 19:i + 1]) / 20.0
            vr = (vl[i] / v20) if v20 > 0 else 1.0
            hi60 = max(cl[i - 59:i + 1])
            dev = cl[i] / m20 - 1.0
            tags = []
            for key, title, kind, a, b, c_, d_ in cases:
                if a == "ZT":
                    if r1 >= thr * 0.98:
                        tags.append(key)
                elif a == "ZT1":
                    if r1 >= thr * 0.98 and r2 < thr * 0.98:
                        tags.append(key)
                elif a == "ZT2":
                    if (r1 >= thr * 0.98 and r2 >= thr * 0.98
                            and i >= 3 and cl[i - 2] / cl[i - 3] - 1.0 < thr * 0.98):
                        tags.append(key)
                elif a == "BRK":
                    if r1 >= 0.04 and vr >= 2.0:
                        tags.append(key)
                elif a == "HI60":
                    if cl[i] >= hi60 * 0.999 and vr >= 1.5:
                        tags.append(key)
                else:
                    if b:
                        base_c = cl[i - b]
                        if base_c <= 0 or cl[i] / base_c - 1.0 > -0.12:
                            continue
                    elif a is not None and r1 > a:
                        continue
                    if dev > -c_:
                        continue
                    tags.append(key)
            j = i + 2
            if j < n and bars[j][1] > 0:
                ret = bars[j][1] / o1 - 1.0
                base[0] += 1
                base[1] += ret
                cell = base[2][d0]
                cell[0] += 1
                cell[1] += ret - bm
            for key in tags:
                if j >= n or bars[j][1] <= 0:
                    continue
                ret = bars[j][1] / o1 - 1.0
                s = st[key]
                s[0] += 1
                s[1] += ret
                cell = s[2][d0]
                cell[0] += 1
                cell[1] += ret - bm

    now = time.strftime("%Y-%m-%d %H:%M")
    rows = []
    for key, title, kind, *_ in cases:
        n_, sraw, daily = st[key]
        dser = [v[1] / v[0] for v in daily.values() if v[0] >= 3]
        if n_ < 300 or len(dser) < 30:
            continue
        rows.append((key, kind, title, 1, n_, round(100 * sraw / n_, 3),
                     round(100 * _mean(dser), 3), round(_tstat(dser) or 0, 2),
                     round(100.0 * sum(1 for x in dser if x > 0) / len(dser), 1),
                     None,
                     "负超额=次日开盘买入后跑输同日全市场；扣费后更差", now,
                     "2016-01~2026-10"))
    bn, braw, bdaily = base
    bdser = [v[1] / v[0] for v in bdaily.values() if v[0] >= 3]
    rows.append(("base_short", "基准", "全市场等权（同一执行口径）", 1, bn,
                 round(100 * braw / bn, 3), 0.0,
                 round(_tstat(bdser) or 0, 2),
                 round(100.0 * sum(1 for x in bdser if x > 0) / len(bdser), 1),
                 None, "基准自身超额定义为 0，绝对收益列即其水平", now,
                 "2016-01~2026-10"))
    out["short"] = len(rows)

    # ---- 中长期：一直低波 vs 曾活跃后沉寂 vs 全市场 ----
    since2 = 20130101
    acc2 = defaultdict(lambda: [0, 0.0, 0.0, 0, 0])
    for code, bars in _iter(kc, since2):
        n = len(bars)
        if n < 1020:
            continue
        cl = [x[2] for x in bars]
        for i in range(500, n - 500):
            b0 = cl[i]
            if b0 <= 0:
                continue
            a_now = _amp(cl[i - 249:i + 1])
            a_prev = _amp(cl[i - 499:i - 249])
            if a_now is None or a_prev is None:
                continue
            gs = []
            if i % 20 == 0:
                gs.append("all")
            if a_now <= TIER_C and a_prev <= TIER_C:
                gs.append("hold45")          # 一直低波（前后都 ≤45%）
            if a_now <= TIER_B and a_prev <= TIER_B:
                gs.append("hold32")          # 一直极低波
            if a_prev >= 0.90 and a_now <= TIER_C:
                gs.append("trap")            # 曾活跃后沉寂（用户直觉的形态）
            if not gs:
                continue
            fw = cl[i + 1:i + 501]
            if len(fw) < 500:
                continue
            fin = fw[-1] / b0 - 1.0
            mdd = min(fw) / b0 - 1.0
            big = 1 if max(fw) / b0 - 1.0 >= 1.0 else 0
            for g in gs:
                a = acc2[g]
                a[0] += 1
                a[1] += fin
                a[2] += mdd
                if fin > 0:
                    a[3] += 1
                a[4] += big
    kc.close()

    hold = {}

    def add2(key, title, g, note):
        a = acc2.get(g)
        if not a or a[0] < 200:
            return
        n_, sfin, smdd, cpos, cbig = a
        hold[key] = dict(title=title, n=n_,
                         fin=round(100 * sfin / n_, 2),
                         mdd=round(100 * smdd / n_, 2),
                         win=round(100.0 * cpos / n_, 1),
                         big=round(100.0 * cbig / n_, 1),
                         note=note)

    add2("hold32", "一直极低波（前后两段振幅均≤32%）持有2年", "hold32",
         "最优组：胜率最高、回撤最小")
    add2("hold45", "一直低波（前后两段振幅均≤45%）持有2年", "hold45",
         "同样有效，比极低波宽松")
    add2("hold_all", "全市场基准（同持有期）", "all",
         "什么都不选的对照组")
    add2("trap", "曾活跃后沉寂（前段>90% 近段≤45%）持有2年", "trap",
         "最差组：这才是「蓄势待爆发」的真实结果，2年仍亏钱")

    bfin = hold.get("hold_all", {}).get("fin")
    for key in ("hold32", "hold45", "hold_all", "trap"):
        h = hold.get(key)
        if not h:
            continue
        ex = 0.0 if (key == "hold_all" or bfin is None) else round(h["fin"] - bfin, 2)
        note = "%s；曾涨≥100%%的样本占 %.1f%%" % (h["note"], h["big"])
        rows.append((key, "中长期", h["title"], 500, h["n"], h["fin"], ex, None,
                     h["win"], h["mdd"], note, now, "2013-01~2026-10"))
        out[key] = h["n"]

    conn.executemany(
        "INSERT OR REPLACE INTO sel_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    conn.execute("INSERT OR REPLACE INTO sel_meta VALUES ('tested_at',?)", (now,))
    conn.commit()
    return {"ok": True, "rows": len(rows), "elapsed": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ 对外
def _rows(conn, sql, args=()):
    try:
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        log.exception("[selection] 查询失败")
        return []


def payload(conn: sqlite3.Connection) -> dict:
    try:
        ensure_schema(conn)
    except Exception:  # noqa: BLE001
        log.exception("[selection] 建表失败")
    ev = _rows(conn, "SELECT * FROM sel_evidence")
    asof = None
    try:
        r = conn.execute("SELECT MAX(asof) FROM sel_pool").fetchone()
        asof = r[0] if r else None
    except Exception:  # noqa: BLE001
        pass
    pool = _rows(conn, "SELECT * FROM sel_pool WHERE asof=? ORDER BY "
                       "CASE tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, amp250",
                 (asof,)) if asof else []
    meta = {m["k"]: m["v"] for m in _rows(conn, "SELECT k,v FROM sel_meta")}
    cand = count_pool_candidates(conn)

    short = [e for e in ev if e["kind"] in ("超跌", "动量")]
    short.sort(key=lambda e: (e["excess"] if e["excess"] is not None else 0))
    hold = [e for e in ev if e["kind"] == "中长期"]
    order = {"hold32": 0, "hold45": 1, "hold_all": 2, "trap": 3}
    hold.sort(key=lambda e: order.get(e["key"], 9))
    cnt = defaultdict(int)
    for p in pool:
        cnt[p["tier"]] += 1

    return {
        "ok": True,
        "available": bool(ev) or bool(pool),
        "asof": asof,
        "tested_at": meta.get("tested_at"),
        "stats": {"pool": len(pool), "A": cnt["A"], "B": cnt["B"], "C": cnt["C"],
                  "candidates": cand.get("n") or len(pool),
                  "capped": bool(cand.get("n") and cand["n"] > len(pool)),
                  "short_cases": len(short)},
        "short": short,
        "hold": hold,
        "pool": pool,
        "verdict": {
            "short": "实测无正超额形态：无论超跌还是追涨，次日开盘买入均为负超额",
            "hold": "「一直低波」2年胜率 66%、回撤 −20%，是唯一有效的底部形态；"
                    "「曾活跃后沉寂」反而最差（−0.6%、胜率 35%）",
        },
        "caveats": [
            "日线库仅保留 13 只退市股，长期持有类结论存在幸存者偏差",
            "无法还原历史 ST 状态，回测未剔 ST，真实收益更差",
            "基本面仅单期快照，无时间序列，「按巴菲特标准选股」无法历史验证",
            "低波组在 2018、2024 这类全市场强势年显著跑输，不是每年都赢",
        ],
    }


def text_block(conn: sqlite3.Connection) -> str:
    p = payload(conn)
    if not p.get("available"):
        return ""
    L = ["【选股方向实测结论（本系统严格口径，非引用他人说法）】"]
    L.append("短线 T+1：以下形态全部为负超额，t 值高度显著 —— "
             "超跌买入与追涨买入都不成立，此数据只能用于判断「不该买」。")
    for e in p["short"]:
        if e.get("excess") is None:
            continue
        L.append("  · %s：超额 %+.2f%%，t=%s" %
                 (e["title"], e["excess"], e["t"]))
    if p["hold"]:
        L.append("中长期（持有 500 个交易日约 2 年）：")
        for e in p["hold"]:
            L.append("  · %s：期末 %+.1f%%，超额 %+.2f%%，胜率 %.1f%%，最大回撤 %.1f%%"
                     % (e["title"], e.get("final_ret") or 0,
                        e.get("excess") or 0, e.get("win") or 0,
                        e.get("mdd") or 0))
    if p["pool"]:
        L.append("当前低波池 %d 只（A级≤25%%振幅 %d 只 / B级≤32%% %d 只 / C级≤45%% %d 只），"
                 "入池条件是「前后两段 250 日振幅都≤45%%」，即一直低波；"
                 "定位是低回撤候选池，不是买入信号。"
                 % (p["stats"]["pool"], p["stats"]["A"], p["stats"]["B"],
                    p["stats"]["C"]))
    L.append("强制要求：不得用「超跌反弹」「涨停次日溢价」「放量突破」作为看多理由；"
             "这些在本系统实测中均为负超额。也不得用「长期横盘蓄势待爆发」作为看多理由 —— "
             "「曾活跃后沉寂」在 2013-2026 实测 2 年 −0.6%、胜率 35.0%，是最差的一组；"
             "真正有效的是「一直低波」这种没人爱看的形态。")
    return "\n".join(L)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = sqlite3.connect(os.path.join(_HERE, "app.db"))
    print(refresh_pool(c))
    print(refresh_evidence(c))
    import json
    print(json.dumps(payload(c)["stats"], ensure_ascii=False))
