#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全市场「前复权」全历史日K 落库（2026-10-21 进哥「长期底部埋伏」板块底座）。

为什么必须有这个库（不是拍脑袋，是实测结论）：
    查看器现有的 data/decline_kcache.json 只有 141 根日K（约 7 个月），
    做不了「1年/2年/3年/5年/8年」的长期底部平台判定；
    而东财 push2his 行情接口在本机被 RemoteDisconnected 掐断，腾讯返回 None，
    只有新浪能拿到全历史（600519 实测 6024 根 / 1991 年起）——但新浪是**不复权**。

不复权对「长期底部」的杀伤是致命的：
    送转（10送10）会让历史价虚高一倍 → 当前价看起来永远在「底部」→ 全市场误报；
    除权台阶还会被当成「创新低」→ 把真正的底部平台判成阴跌。
    所以：拉新浪全历史不复权 K + 用东财数据中心的分红送股方案（IMP_PLAN_PROFILE 文本，
    "10送3股转2股派1.5元"）自算前复权因子，落到本地 sqlite。

输出：
    data/kline_daily.db   表 kline(code, d(YYYYMMDD 整数), o, c, h, l, v)  —— 前复权
    data/adjust_cache.json  每只票的除权事件（{code: [[ex_date, f], ...]}），复权审计用

用法：
    python tools/build_kline_db.py            # 增量（只补缺失日期，日常跑这个）
    python tools/build_kline_db.py --full     # 全量重建（首次）
    python tools/build_kline_db.py --codes 600519,002242   # 指定票调试
"""
import io
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA, "kline_daily.db")
ADJ_PATH = os.path.join(DATA, "adjust_cache.json")
WORKERS = 16

# 2026-10-30：本轮「成没成」的判定阈值（`main()` 返回值用，供 tools/sched_job.py 判 rc）。
#
# 为什么需要阈值而不是「有一只失败就算失败」：本任务跑全市场 5500+ 只，其中一类失败是
# **结构性**的 —— `too_short`（次新股上市不足 30 根 K 线），这批票永远修不好也不该算劣化。
# 实测正常轮（viewer/logs 里有据可查）：
#     ok=5550 skip=0  fail=24  → 24/5574 = 0.43%
#     ok=5574 skip=24 fail=16  → 16/5614 = 0.29%
#     ok=5545 skip=4  fail=31  → 31/5580 = 0.56%（其中 5~6 只 no_kline）
# 真正的劣化只有一次：
#     ok=5042 skip=3  fail=534 → 534/5579 = 9.57%（no_kline 满屏，网络/限流）
# 而这一次当时被报成 `ok 1139.2s`（rc=0）→ 父进程记成"今日已完成"，当天不再重试。
#
# 所以判据只看**硬失败**（`too_short` 以外的 no_kline / err:*）占比：
#     硬失败 <= max(3, 池子 × 2%)  →  ok
# 2% 相对正常基线 0.56% 有约 3.5 倍余量（不会因偶发几只网络抖动就整体判失败），
# 又能把 9.57% 那种劣化明确判失败、交给父进程退避重试。下限 3 是为了 `--codes` 调试
# 小样本时不至于「查两只、一只没数据」就判失败。
KLINE_HARD_FAIL_RATIO = 0.02
KLINE_HARD_FAIL_MIN_ALLOW = 3


def _log(msg):
    print("[kline_db] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _init_db():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS kline (
        code TEXT NOT NULL, d INTEGER NOT NULL,
        o REAL, c REAL, h REAL, l REAL, v REAL,
        PRIMARY KEY (code, d)) WITHOUT ROWID""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_kline_code ON kline(code)")
    con.commit()
    return con


# ---------- 分红送股方案解析 ----------
_RE_SONG = re.compile(r"10\s*送\s*([\d.]+)")
_RE_ZHUAN = re.compile(r"10\s*转(?:增)?\s*([\d.]+)")
_RE_PAI = re.compile(r"10\s*派\s*([\d.]+)")


def _parse_plan(txt):
    """'10送3股转2股派1.5元(含税)' → (送3, 转2, 派1.5)（每10股口径）。"""
    t = str(txt or "")
    if not t:
        return 0.0, 0.0, 0.0

    def _g(rx):
        m = rx.search(t)
        try:
            return float(m.group(1)) if m else 0.0
        except (TypeError, ValueError):
            return 0.0
    return _g(_RE_SONG), _g(_RE_ZHUAN), _g(_RE_PAI)


def _adjust_events(code):
    """取该票全部「实施分配」除权事件 → [(ex_date 'YYYY-MM-DD', 送股, 转增, 派息)] 升序。"""
    import emapi
    c = re.sub(r"\D", "", str(code or ""))
    if not c:
        return []
    if c.startswith(("43", "83", "87", "88", "92")):
        sc = c + ".BJ"
    elif c.startswith(("6", "5", "9")):
        sc = c + ".SH"
    else:
        sc = c + ".SZ"
    try:
        r = emapi.datacenter("RPT_SHAREBONUS_DET", flt='(SECUCODE="%s")' % sc,
                             sort_columns="EX_DIVIDEND_DATE", sort_types="-1", page_size=60)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in (r.get("rows") or []):
        if str(x.get("ASSIGN_PROGRESS") or "") != "实施分配":
            continue
        ex = str(x.get("EX_DIVIDEND_DATE") or "")[:10]
        if not ex or ex < "1990-01-01":
            continue
        song, zhuan, pai = _parse_plan(x.get("IMPL_PLAN_PROFILE"))
        # 兜底：文本缺失时用 PRETAX_BONUS_RMB（每10股派息）补派息
        if not (song or zhuan or pai):
            try:
                pai = float(x.get("PRETAX_BONUS_RMB") or 0.0)
            except (TypeError, ValueError):
                pai = 0.0
        if song or zhuan or pai:
            out.append((ex, song, zhuan, pai))
    out.sort(key=lambda z: z[0])
    return out


def _apply_qfq(bars, events):
    """把新浪不复权 bars 做**前复权**（以最新价为基准，历史价按除权因子回溯缩放）。

    bars: [[date, open, close, high, low, volume], ...] 升序
    因子 f = (前收盘 - 每股派息) / (1 + 每股送转) / 前收盘
      —— 派息 0.15元/股 = 每10股派1.5；送转 ratio = (送+转)/10。
    对除权日**之前**的所有 bar 乘 f 的累计值。
    """
    if not bars or not events:
        return bars, []
    idx = {b[0]: i for i, b in enumerate(bars)}
    factors = []          # [(bar_index_of_exday, f)]
    for ex, song, zhuan, pai in events:
        i = idx.get(ex)
        if i is None or i == 0:
            continue
        prev_close = bars[i - 1][2]
        if not prev_close or prev_close <= 0:
            continue
        div_ps = pai / 10.0
        ratio = (song + zhuan) / 10.0
        f = (prev_close - div_ps) / (1.0 + ratio) / prev_close
        if f <= 0 or f >= 1.0001:      # 只接受「向下」的除权因子，异常丢弃
            continue
        factors.append((i, round(f, 8)))
    if not factors:
        return bars, []
    factors.sort()
    # 从后往前累乘：每个 bar 的累计因子 = 所有「除权日 > 该 bar」的 f 之积
    cum = 1.0
    k = len(factors) - 1
    out = []
    scale = [1.0] * len(bars)
    for i in range(len(bars) - 1, -1, -1):
        while k >= 0 and factors[k][0] > i:
            cum *= factors[k][1]
            k -= 1
        scale[i] = cum
    for i, b in enumerate(bars):
        s = scale[i]
        if s == 1.0:
            out.append(b)
        else:
            out.append([b[0], round(b[1] * s, 4), round(b[2] * s, 4),
                        round(b[3] * s, 4), round(b[4] * s, 4), b[5]])
    return out, [{"ex": bars[i][0] if i < len(bars) else "", "f": f} for i, f in factors]


def _fetch_one(code):
    """拉一只票：全历史不复权 K → 前复权 → 返回 (code, bars, events)。"""
    from viewer import server as SVR
    try:
        kl = SVR.api_kline(code, "day", limit=0)
        if not kl or kl.get("error") or not kl.get("klines"):
            return code, None, [], "no_kline"
        bars = []
        for k in kl["klines"]:
            try:
                dt = str(k.get("date") or "")[:10]
                if len(dt) != 10:
                    continue
                bars.append([dt, float(k["open"]), float(k["close"]),
                             float(k["high"]), float(k["low"]),
                             float(k.get("volume") or 0)])
            except (TypeError, ValueError, KeyError):
                continue
        if len(bars) < 30:
            return code, None, [], "too_short"
        bars.sort(key=lambda b: b[0])
        ev = _adjust_events(code)
        adj, used = _apply_qfq(bars, ev)
        return code, adj, ev, ("ok" if used else "ok_noex")
    except Exception as e:  # noqa: BLE001
        return code, None, [], "err:%s" % type(e).__name__


def main():
    args = sys.argv[1:]
    full = "--full" in args
    codes_arg = None
    if "--codes" in args:
        codes_arg = args[args.index("--codes") + 1]

    t0 = time.time()
    con = _init_db()
    if codes_arg:
        codes = [c.strip() for c in re.split(r"[,，\s]+", codes_arg) if c.strip()]
    else:
        import screener
        pool = screener._market_pool()
        codes = list(pool) if isinstance(pool, dict) else list(pool or [])
    codes = [c for c in codes if re.match(r"^\d{6}$", c)]
    _log("目标 %d 只，workers=%d，mode=%s" % (len(codes), WORKERS, "full" if full else "incr"))
    if not codes:
        # 2026-10-30：池子取空绝不能当"成功"（以前落进下面的循环、0 只、rc=0、
        # 父进程写当日已完成标记 → 当天不再重试）。这是最典型的静默失败。
        con.close()
        _log("候选池为空 → 本轮判失败（禁止把空跑记成完成）")
        return {"ok": False, "codes": 0, "rows": 0,
                "elapsed": round(time.time() - t0, 1),
                "err": "候选池为空（screener._market_pool 未取到任何 6 位代码）"}

    # 已有最新日期（增量时用来跳过「已是最新」的票的写库，但 K 线仍需拉——新浪一次给全历史）
    existing = {}
    for row in con.execute("SELECT code, MAX(d) FROM kline GROUP BY code"):
        existing[row[0]] = row[1]
    _log("库内已有 %d 只" % len(existing))

    adj_cache = {}
    if os.path.exists(ADJ_PATH):
        try:
            with io.open(ADJ_PATH, encoding="utf-8") as fh:
                adj_cache = json.load(fh)
        except Exception:  # noqa: BLE001
            adj_cache = {}

    stat = {"ok": 0, "skip": 0, "fail": 0, "rows": 0, "ex": 0}
    fails = []

    def _work(code):
        return _fetch_one(code)

    BATCH = 200
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        buf = []
        for n, (code, bars, ev, st) in enumerate(ex.map(_work, codes), 1):
            if bars is None:
                stat["fail"] += 1
                fails.append((code, st))
            else:
                stat["ok"] += 1
                if ev:
                    stat["ex"] += 1
                    adj_cache[code] = [[e[0], e[1], e[2], e[3]] for e in ev]
                last = int(bars[-1][0].replace("-", ""))
                if existing.get(code) == last and not full:
                    stat["skip"] += 1       # 已最新 → 不重复写
                else:
                    for b in bars:
                        buf.append((code, int(b[0].replace("-", "")),
                                    b[1], b[2], b[3], b[4], b[5]))
                if len(buf) >= 40000:
                    con.executemany("INSERT OR REPLACE INTO kline(code,d,o,c,h,l,v)"
                                    " VALUES(?,?,?,?,?,?,?)", buf)
                    con.commit()
                    stat["rows"] += len(buf)
                    buf = []
            if n % 300 == 0:
                _log("进度 %d/%d ok=%d fail=%d rows=%d %.0fs"
                     % (n, len(codes), stat["ok"], stat["fail"], stat["rows"], time.time() - t0))
        if buf:
            con.executemany("INSERT OR REPLACE INTO kline(code,d,o,c,h,l,v)"
                            " VALUES(?,?,?,?,?,?,?)", buf)
            con.commit()
            stat["rows"] += len(buf)

    tmp = ADJ_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(adj_cache, fh, ensure_ascii=False)
    os.replace(tmp, ADJ_PATH)

    total = con.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
    ncode = con.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
    con.execute("PRAGMA optimize")
    con.close()
    _log("完成：ok=%d skip=%d fail=%d 写入%d行 有除权%d只；库内 %d 行 / %d 只；用时 %.0fs"
         % (stat["ok"], stat["skip"], stat["fail"], stat["rows"], stat["ex"],
            total, ncode, time.time() - t0))
    if fails:
        _log("失败样例：" + str(fails[:12]))

    # 2026-10-30：把「这一轮到底成没成」交出去。
    # 此前 `main()` 没有 return（隐式 None）→ `tools/sched_job.py` 的
    # `if isinstance(r, dict) and r.get("ok") is False: return 1` 永不成立 → 进程退出码恒 0
    # → 父进程把失败记成"今日已完成"→ 当天不再重试。日志里 `ok 840.4s None` / `ok 1139.2s None`
    # 的两种耗时都报 ok、却没有任何成功/失败信号，就是这个 None 造成的。
    n_pool = len(codes)
    soft_fail = sum(1 for _c, _st in fails if _st == "too_short")
    hard_fail = len(fails) - soft_fail
    allowed_hard = max(KLINE_HARD_FAIL_MIN_ALLOW, int(n_pool * KLINE_HARD_FAIL_RATIO))
    ok = bool(n_pool) and stat["ok"] > 0 and hard_fail <= allowed_hard
    return {
        "ok": ok,
        "codes": n_pool,
        "ok_n": stat["ok"],
        "skip": stat["skip"],
        "fail": stat["fail"],
        # 硬失败 = 取数失败 / 解析异常（no_kline、err:*），是**真劣化**；
        # 软失败 = too_short（次新股不足 MIN_BARS，结构性、不可修），单列出来便于诊断。
        "hard_fail": hard_fail,
        "soft_fail_too_short": soft_fail,
        "allowed_hard_fail": allowed_hard,
        "rows": stat["rows"],
        "db_rows": total,
        "db_codes": ncode,
        "elapsed": round(time.time() - t0, 1),
        "err": (None if ok else
                "硬失败 %d 只超过允许 %d 只（取数失败/解析异常；样例 %s）"
                % (hard_fail, allowed_hard, str(fails[:6])[:200])),
        "fail_sample": [[str(c), str(st)] for c, st in fails[:12]],
    }


if __name__ == "__main__":
    # ⚠️ 刻意**不**改成 `sys.exit(main())`：本文件同时也是给进哥手跑的独立 CLI，
    # 外部行为约定（跑完就结束、退出码 0）保持不变。rc=1 的判定只发生在调度侧的
    # `tools/sched_job.py`（它读 main() 的返回值）。要在 CLI 上也体现失败，请显式跑
    # `python tools/sched_job.py kline_daily` 看退出码。
    main()
