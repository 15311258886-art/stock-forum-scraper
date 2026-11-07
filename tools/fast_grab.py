# -*- coding: utf-8 -*-
"""一次请求拿全市场当日一根 —— 日更抓取链的快速通路（2026-11-03 D72 新增）。

## 为什么要有这个脚本

日更链里 `kline_daily`（3362.8s）+ `kbase_daily`（1886.8s）合计 87 分钟，
两个都是**逐只调新浪**：5550 只 → 5550 次 HTTP，且新浪限流敏感、workers 只能开 2。
本脚本把这两步换成「一次请求拿全市场」：通达信盘后包一个 zip 覆盖全市场当日一根，
实测下载 + 解析 **不到 15 秒**。

## 三条纪律（这是最容易出事的地方，改动前请逐条读完）

### ① 前复权库 data/kline_daily.db：不是所有票都能直接追加原始价

前复权是**以最新价为锚**的：今天是除权日时，历史所有价位都要乘一个因子重新排。
所以「包里的今日原始价 == 今日前复权价」**只在没有新除权事件时成立**。

判据用现成的 `data/adjust_cache.json`：该票有事件且 `ex_date == 目标日` → 必须走
原有 `build_kline_db._fetch_one()` 全量重抓（它会重新算因子、重写全历史），
**绝不允许**直接追加原始价 —— 那会把这只票的历史价位永久错位，而且不报错。
实测一天这种票只有 3~4 只（2026-11-03：4 只；2026-11-02：3 只），全量重抓代价可忽略。

### ② 后复权库 viewer/kbase.db：用比值搬，不联网

    今日 hfq = 今日原始价 × (该票最近共同交易日的 hfq / qfq)

两个比值都能从本地两个库直接读（`kbase.kline` 与 `kline_daily.kline`），一次都不用出网。
取不到比值的票（kbase 里没这只、或 kbase 的末日不在 kline 里）→ **跳过并计数**，
不许拿 1.0 顶上 —— 那等于给除权票凭空造一个台阶。

实测：比值对 5150/5154 只恰好等于 1.0（两个库都是「锚定最新价」的口径），
只有当天发生除权的 4 只不为 1（如 300735 = 1.005884）。这正是这套算法要处理的情形。

### ③ 幂等：同一天重复跑不许多出行

两处都用 `INSERT OR IGNORE`（表本身有 PK）。所以：
**先 `--dry-run` 看对账，人工确认后再 `--write`。** `--write` 也可以放心重跑。

## 降级（进哥已确认选 A）

通达信盘后包**不是收盘就有**：实测 20261103（当天）HTTP 404，20261102 有包。
所以包没发布时本脚本**不报错退出**，改为只抓腾讯全市场快照，摘要里标
`degraded: true / reason: 日线包未发布，已用快照顶上`，并把快照落进
`viewer/app.db` 的 `stock_daily_snapshot`（口径完全照 `tools/fetch_a_shares_tencent.py`，
复用它的 `_ensure_schema` / 会话日校验 / `no_trade` 剔除规则）。

⚠️ 降级模式下**不写 kline_daily / kbase**。理由：腾讯的成交量是「手」、
截断到百股（600519 实测 7,412,100 vs 精确 7,412,056），而 kline 是 `INSERT OR IGNORE` ——
一旦用这个近似值占了位，晚上真包或原 kline_daily 跑起来就补不回精确值了。
宁可少一根 K 线，也不要一根永久偏小的量。

## 用法

    python tools/fast_grab.py                       # 缺省=最近交易日，只抓不写（对账）
    python tools/fast_grab.py --date 2026-11-02     # 指定日
    python tools/fast_grab.py --date 2026-11-02 --write   # 真写库

输出是一段 JSON 摘要（每步耗时 / 写入行数 / 跳过行数与原因直方图 / 对账结果），
既给人看，也给 `tools/sched_job.py` 的 `fast_daily` 与 `viewer/server.py` 的
`/api/fast_grab` 直接消费 —— 所以**只往 stdout 打这一份 JSON**，过程日志走 stderr。
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

KLINE_DB = os.environ.get("FASTGRAB_KLINE_DB") or os.path.join(ROOT, "data", "kline_daily.db")
KBASE_DB = os.environ.get("FASTGRAB_KBASE_DB") or os.path.join(ROOT, "viewer", "kbase.db")
APP_DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
ADJ_PATH = os.path.join(ROOT, "data", "adjust_cache.json")

# kline 表列序是 (code, d, o, c, h, l, v) —— 注意是 o,c,h,l 而不是常见的 o,h,l,c。
# 这张表 2026-10-21 建库时就是这么写的，改列序会把全站 K 线读错，所以这里照抄不「纠正」。
_SQL_KLINE_INS = "INSERT OR IGNORE INTO kline(code,d,o,c,h,l,v) VALUES(?,?,?,?,?,?,?)"
_SQL_KLINE_REPL = "INSERT OR REPLACE INTO kline(code,d,o,c,h,l,v) VALUES(?,?,?,?,?,?,?)"
# kbase 表列序是 (code, date, o, h, l, c, v, adj) —— 与 kline 相反，别顺手复用同一个元组。
_SQL_KBASE_INS = ("INSERT OR IGNORE INTO kline(code,date,o,h,l,c,v,adj) "
                  "VALUES(?,?,?,?,?,?,?,?)")


def _log(msg: str) -> None:
    """过程日志一律走 stderr —— stdout 只留给那份 JSON 摘要。"""
    sys.stderr.write("[fast_grab] %s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _today_cst() -> str:
    """服务器本地日期（本项目其余模块同理，不引入时区库）。"""
    return time.strftime("%Y-%m-%d")


def _iso(d8: str) -> str:
    d8 = str(d8 or "")
    return "%s-%s-%s" % (d8[:4], d8[4:6], d8[6:8]) if len(d8) == 8 else ""


def _d8(iso: str) -> int:
    return int("".join(ch for ch in str(iso or "") if ch.isdigit())[:8] or 0)


# ------------------------------------------------------------------ 目标交易日

def resolve_date(date=None) -> str:
    """定目标交易日，返回 ISO 'YYYY-MM-DD'。

    缺省不用「今天」而用「本地库已有的最新交易日」：
      · 周六日跑（很多人补数据就是在周末）→ 用今天会去找一个不存在的包，
        然后降级成一份「周六快照」，把周五的真实数据挤到次要位置；
      · 用库里最新日则在任何一天都指向「上一个真的开过市的日子」，语义稳定。
    唯一的例外：库里最新日**就是今天**时也用它（正常晚间日更）。
    """
    if date:
        d = str(date).strip().replace("/", "-")
        if re.match(r"^\d{8}$", d):
            return _iso(d)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            raise ValueError("--date 需要 YYYY-MM-DD 或 YYYYMMDD：%r" % (date,))
        return d
    try:
        con = sqlite3.connect("file:%s?mode=ro" % KLINE_DB, uri=True, timeout=15)
        row = con.execute("SELECT MAX(d) FROM kline").fetchone()
        con.close()
        if row and row[0]:
            return _iso(str(row[0]))
    except Exception as e:  # noqa: BLE001
        _log("读 kline_daily 最新日失败（退回今天）：%s" % e)
    return _today_cst()


# ------------------------------------------------------------------ 除权事件

def load_ex_date_map(adj_path: str = None) -> dict:
    """{除权日 ISO: [有该日除权事件的代码]}，来自 data/adjust_cache.json。

    只读不改。文件缺失/损坏时返回空字典并告警 —— 空字典会让所有票都走「直接追加」，
    而那正是纪律①要防的事，所以必须在摘要里把 `adjust_cache_ok: false` 露出来，
    不能静默当成「今天没有除权票」。
    """
    path = adj_path or ADJ_PATH
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with io.open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception as e:  # noqa: BLE001
        _log("adjust_cache 读取失败（今日除权票将无法识别）：%s" % e)
        return out
    for code, evs in (cache or {}).items():
        for e in (evs or []):
            try:
                ex = str(e[0])[:10]
            except Exception:  # noqa: BLE001
                continue
            if ex:
                out.setdefault(ex, []).append(str(code))
    return out


def codes_ex_after(ex_map: dict, target: str) -> set:
    """在 target **之后**发生过除权的代码集合。

    对账要用它，不是「target 当天除权」的那一组 —— 两者含义完全不同：
      · 当天除权（ex_date == target）→ 今天要整只全量重抓（纪律①）；
      · 之后除权（ex_date > target）→ 库里的历史价**已经被重排过**，
        所以「包里的原始价」与「库里的前复权价」本来就该不同。
    2026-11-02 实测：ex_date==11-02 的只有 3 只（001393/301536/688548），
    而库里 11-02 收盘价被改写过的是 ex_date==11-03 的那 4 只
    （605179/605300/000788/300735）—— 一开始我拿前者去解释差异，结果 4 只
    正常除权的票被报成 value_diff，等于用错的尺子量出「大面积不一致」。
    """
    out = set()
    for ex, codes in (ex_map or {}).items():
        if ex > target:
            out.update(codes)
    return out


# ------------------------------------------------------------------ 前复权库

def _existing_codes(con, d: int) -> set:
    return {r[0] for r in con.execute("SELECT code FROM kline WHERE d=?", (d,))}


def _count_new_codes(con, codes) -> int:
    """这批代码里有几只在整个 kline 表里一行都没有（= 本次首次进库）。

    分块查是为了避开 SQLite 的绑定参数上限；走 idx_kline_code，几千只也就几十毫秒。
    """
    if not codes:
        return 0
    known = 0
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        marks = ",".join("?" * len(chunk))
        known += con.execute(
            "SELECT COUNT(DISTINCT code) FROM kline WHERE code IN (%s)" % marks,
            chunk).fetchone()[0]
    return len(codes) - known


def write_kline(rows, target: str, ex_codes, dry_run: bool) -> dict:
    """把「今日一根」写进 data/kline_daily.db（前复权）。

    rows: [{code, open, close, high, low, volume, amount}]（原始价，来自盘后包）
    ex_codes: 目标日有除权事件的代码集合 → 走全量重抓，不直接追加。

    返回统计（含每个跳过原因的直方图）—— 对账时「跳过了多少、为什么」比「写了多少」重要。
    """
    d8 = _d8(target)
    t0 = time.time()
    stat = {"rows_in": len(rows or []), "insert": 0, "refetch_full": 0,
            "skip": {}, "elapsed_sec": 0.0, "refetch_errors": [],
            "rows_written": 0, "db_rows_after": None}
    inc = stat["skip"]

    def bump(reason):
        inc[reason] = inc.get(reason, 0) + 1

    con = sqlite3.connect(KLINE_DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    try:
        have = _existing_codes(con, d8)
        buf = []
        full_codes = []
        for r in rows or []:
            code = r.get("code") or ""
            if not code:
                bump("no_code")
                continue
            if not (r.get("close") or 0) > 0:
                bump("no_close")
                continue
            if code in ex_codes:
                # 纪律①：今日除权 → 全历史都要重排，只能整只重抓
                full_codes.append(code)
                continue
            if code in have:
                # 纪律③：幂等。已有的行不动（INSERT OR IGNORE 也只是兜底，
                # 提前判掉能少写进去、也才能如实报告「本次真的写了几行」）
                bump("already_exists")
                continue
            buf.append((code, d8, r.get("open"), r.get("close"),
                        r.get("high"), r.get("low"), r.get("volume")))
        stat["refetch_full"] = len(full_codes)

        # 诊断用：这批里有多少只**库里从来没有过**的代码。
        # 实测 2026-11-02 包里有 19 只 920xxx（北交所 2023 起的新段）在 kline 里
        # 一行都没有 —— 说明既有的 kline_daily 候选池漏了这段（screener._market_pool）。
        # 这个数字单独报出来，就不会被「写入 19 行」糊过去：否则下次有人看到
        # 「写入 19 行」会以为只是补了 19 根 K 线，其实是补了 19 只票。
        # 必须在 executemany 之前算 —— 插完再算永远是 0（第一版就踩了这个坑，
        # 明明报了 new_codes 却给出 0，比没有这个字段更容易误导）。
        stat["new_codes"] = _count_new_codes(con, [b[0] for b in buf])

        if not dry_run:
            if buf:
                con.executemany(_SQL_KLINE_INS, buf)
                con.commit()
            stat["insert"] = len(buf)
        else:
            stat["insert"] = len(buf)          # dry-run：报「将写入」的行数

        # 除权票全量重抓（只有 3~4 只，但必须做且必须用原函数）
        if full_codes:
            import build_kline_db as BKD
            from concurrent.futures import ThreadPoolExecutor

            # 并发抓、串行写。为什么不在线程里直接写库：sqlite3 的 Connection 默认
            # `check_same_thread=True`，跨线程使用会直接抛 ProgrammingError；
            # 而且多线程各写各的事务会把 write 锁打成交错，比串行慢。
            # 为什么要并发：`_fetch_one` 是**纯 IO**（新浪全历史 + 东财分红方案两次请求），
            # 实测 4 只串行要 52.4 秒（每只约 13 秒）——这一步不并发，「十几秒」的承诺
            # 会被这 4 只票单独吃掉。`build_kline_db` 自己就是 16 线程跑同一函数的，线程安全性有前例。
            def _one(code):
                try:
                    _c, bars, _ev, st = BKD._fetch_one(code)
                    return code, bars, st, None
                except Exception as e:  # noqa: BLE001
                    return code, None, "err", e

            with ThreadPoolExecutor(max_workers=min(4, len(full_codes))) as _ex:
                results = list(_ex.map(_one, full_codes))
            for code, bars, st, err in results:
                if err is not None:
                    bump("refetch_err:%s" % type(err).__name__)
                    stat["refetch_errors"].append([code, "%s: %s" % (type(err).__name__, str(err)[:80])])
                    _log("除权票 %s 全量重抓失败：%s" % (code, err))
                    continue
                if not bars:
                    bump("refetch_fail:%s" % st)
                    stat["refetch_errors"].append([code, st])
                    continue
                if not dry_run:
                    con.executemany(
                        _SQL_KLINE_REPL,
                        [(code, int(str(b[0]).replace("-", "")), b[1], b[2], b[3], b[4], b[5])
                         for b in bars])
                    con.commit()
                key = "refetch_full_done" if not dry_run else "refetch_full_planned"
                stat[key] = stat.get(key, 0) + 1

        stat["rows_written"] = len(buf)
        stat["db_rows_after"] = con.execute(
            "SELECT COUNT(*) FROM kline WHERE d=?", (d8,)).fetchone()[0]
    finally:
        con.close()
    stat["elapsed_sec"] = round(time.time() - t0, 2)
    return stat


# ------------------------------------------------------------------ 后复权库

def _kbase_anchors(con_kb, cutoff_iso: str) -> dict:
    """{code: (anchor_date, hfq_close)} —— 每只票在 kbase 里的最后一根（>= cutoff）。

    只扫最近一段（默认 20 天）而不是 GROUP BY 全表：
    实测全表 `GROUP BY code` 要 5.66 秒，而 12 天窗口 0.94 秒、结果等价
    （日更只会让末日往后走，锚点必然在最近这段里）。
    """
    best = {}
    for code, date, c in con_kb.execute(
            "SELECT code, date, c FROM kline WHERE date >= ?", (cutoff_iso,)):
        cur = best.get(code)
        if cur is None or str(date) > cur[0]:
            best[code] = (str(date), c)
    return best


def write_kbase(rows, target: str, dry_run: bool) -> dict:
    """把「今日一根」按 hfq = 原始价 × (hfq_prev/qfq_prev) 写进 viewer/kbase.db。

    rows 必须是**原始价**（前复权当日 == 原始价，见纪律①；对于除权票，
    kline 那边已经整只重抓过，这里仍用原始价 × 比值，两者是等价的写法：
    比值本身就吸收了当天的除权因子）。
    """
    target_iso = target
    t0 = time.time()
    stat = {"rows_in": len(rows or []), "insert": 0, "skip": {}, "elapsed_sec": 0.0,
            "kbase_max_date_before": None, "kbase_max_date_after": None,
            "ratio_ne_one": 0, "ratio_samples": []}
    inc = stat["skip"]

    def bump(reason):
        inc[reason] = inc.get(reason, 0) + 1

    cutoff = target
    # 往前看 20 个自然日足够覆盖春节/国庆长假里「上一个共同交易日」
    from datetime import date as _date, timedelta as _td
    try:
        y, m, dd = int(target[:4]), int(target[5:7]), int(target[8:10])
        cutoff = (_date(y, m, dd) - _td(days=20)).isoformat()
    except Exception:  # noqa: BLE001
        pass

    con_kb = sqlite3.connect(KBASE_DB, timeout=60)
    con_kb.execute("PRAGMA busy_timeout=60000")
    con_k = sqlite3.connect("file:%s?mode=ro" % KLINE_DB, uri=True, timeout=60)
    try:
        stat["kbase_max_date_before"] = con_kb.execute(
            "SELECT MAX(date) FROM kline").fetchone()[0]
        anchors = _kbase_anchors(con_kb, cutoff)
        # 锚点日期在 kline 里的前复权收盘 —— 批量取，别逐只查（5500 次查询会很慢）
        anchor_dates = sorted({a[0] for a in anchors.values()})
        d8s = [_d8(x) for x in anchor_dates if _d8(x)]
        qfq = {}
        if d8s:
            marks = ",".join("?" * len(d8s))
            for code, d, c in con_k.execute(
                    "SELECT code, d, c FROM kline WHERE d IN (%s)" % marks, d8s):
                qfq[(code, d)] = c
        exist_in_kb = {c for (c,) in con_kb.execute(
            "SELECT code FROM kline WHERE date=?", (target_iso,))}

        buf = []
        for r in rows or []:
            code = r.get("code") or ""
            if not code:
                bump("no_code")
                continue
            if code in exist_in_kb:
                bump("already_exists")
                continue
            a = anchors.get(code)
            if not a:
                # kbase 里根本没有这只票的锚点 → 算不出比值 → 跳过（纪律②）
                bump("no_anchor")
                continue
            anchor_date, hfq_c = a
            q_c = qfq.get((code, _d8(anchor_date)))
            if not q_c or q_c <= 0 or not hfq_c:
                bump("no_ratio")
                continue
            ratio = float(hfq_c) / float(q_c)
            if abs(ratio - 1.0) < 1e-9:
                ratio = 1.0
            else:
                stat["ratio_ne_one"] += 1
                if len(stat["ratio_samples"]) < 10:
                    stat["ratio_samples"].append(
                        [code, round(ratio, 6), anchor_date, hfq_c, q_c])
            o = r.get("open")
            h = r.get("high")
            l = r.get("low")
            c = r.get("close")
            v = r.get("volume")
            buf.append((code, target_iso,
                        None if o is None else round(o * ratio, 4),
                        None if h is None else round(h * ratio, 4),
                        None if l is None else round(l * ratio, 4),
                        None if c is None else round(c * ratio, 4),
                        v, "hfq"))
        stat["insert"] = len(buf)
        if not dry_run and buf:
            con_kb.executemany(_SQL_KBASE_INS, buf)
            con_kb.commit()
        stat["kbase_max_date_after"] = (
            target_iso if (buf and not dry_run) else
            con_kb.execute("SELECT MAX(date) FROM kline").fetchone()[0])
    finally:
        con_k.close()
        con_kb.close()
    stat["elapsed_sec"] = round(time.time() - t0, 2)
    return stat


# ------------------------------------------------------------------ 对账

def reconcile(package_rows, target: str, ex_after=None) -> dict:
    """拿盘后包与本地 kline_daily 同日数据逐只对账 —— **写库前的唯一验收手段**。

    比对 close 与 volume（kline 表没有 amount 列，所以 amount 另走快照表比对）。

    不一致分四类，因为处理方式完全不同：
      · `ex_div`      源=原始价、库=已按目标日之后的除权重排过的前复权价。
                      预期不一致，票数应等于「目标日之后一个交易日的除权票数」。
                      若对不上，说明不是除权导致的 —— 得当 real_mismatch 看。
      · `volume_round` 量差在 0.01% 以内：新浪的成交量做过百股取整，属正常。
      · `value_diff`   真实不一致 —— 这才是要停下来查单位/复权口径的信号。
    """
    d8 = _d8(target)
    ex_after = set(ex_after or ())
    out = {"date": target, "db": "data/kline_daily.db", "fields": ["close", "volume"],
           "compared": 0, "equal": 0, "mismatch": 0,
           "buckets": {}, "examples": [], "db_rows_for_date": 0,
           "package_rows": len(package_rows or []), "app_db_amount": None,
           "only_in_package_sample": []}
    db = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % KLINE_DB, uri=True, timeout=30)
        for code, o, c, h, l, v in con.execute(
                "SELECT code,o,c,h,l,v FROM kline WHERE d=?", (d8,)):
            db[code] = (o, c, h, l, v)
        con.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = "读 kline_daily 失败：%s" % e
        return out
    out["db_rows_for_date"] = len(db)
    if not db:
        out["note"] = "本地 %s 没有数据，无需对账（首次写入前）" % target
        return out

    def bucket(name):
        out["buckets"][name] = out["buckets"].get(name, 0) + 1

    for r in package_rows or []:
        code = r.get("code")
        if code not in db:
            bucket("only_in_package")
            # 包里有、库里没有 —— 正常情况是「kline_daily 上次跑完之后才上市的新股」，
            # 也可能是我方漏抓。留样例，好判断到底该不该补这一行。
            if len(out["only_in_package_sample"]) < 10:
                out["only_in_package_sample"].append(code)
            continue
        out["compared"] += 1
        _o, dc, _h, _l, dv = db[code]
        pc = r.get("close")
        pv = r.get("volume")
        diffs = []
        if pc is None or dc is None or abs(pc - dc) > max(0.005, abs(dc) * 1e-6):
            diffs.append("close")
        if pv is None or not dv:
            diffs.append("volume")
        elif abs(pv - dv) > 1e-6 * max(1.0, dv):
            # 0.01% 以内算「源取整」，超出才算真的对不上
            if abs(pv - dv) / max(1.0, dv) <= 1e-4:
                diffs.append("volume_round")
            else:
                diffs.append("volume")
        if not diffs:
            out["equal"] += 1
            bucket("equal")
            continue
        if all(x in ("close", "volume_round") for x in diffs) and code in ex_after:
            bucket("ex_div")
        elif diffs == ["volume_round"]:
            bucket("volume_round")
        else:
            bucket("value_diff")
            out["mismatch"] += 1
        if len(out["examples"]) < 10:
            pct = None
            if pc and dc:
                pct = round((pc - dc) / dc * 100.0, 4)
            out["examples"].append({
                "code": code, "fields": diffs,
                "close_pkg": pc, "close_db": dc, "close_diff_pct": pct,
                "vol_pkg": pv, "vol_db": dv,
                "vol_diff_pct": (None if not dv else round((pv - dv) / dv * 100.0, 6)),
                "reason": "ex_div" if code in ex_after else "",
            })

    # amount 只能拿快照表比：kline 表从建库起就没有 amount 列（不新增列是为了不动现网表结构）
    try:
        con_a = sqlite3.connect("file:%s?mode=ro" % APP_DB, uri=True, timeout=30)
        snap = {c: a for c, a in con_a.execute(
            "SELECT code, amount FROM stock_daily_snapshot WHERE trade_date=?", (target,))}
        con_a.close()
        if snap:
            n = cmp = ok = 0
            bad = []
            for r in package_rows or []:
                code = r.get("code")
                if code not in snap or not snap[code]:
                    continue
                n += 1
                a_pkg = r.get("amount") or 0.0
                a_db = float(snap[code])
                if a_db <= 0:
                    continue
                cmp += 1
                # 快照的 amount 是腾讯「万元×10000」，本身带万位取整，容差放到 0.5%
                if abs(a_pkg - a_db) / a_db <= 0.005:
                    ok += 1
                elif len(bad) < 5:
                    bad.append([code, round(a_pkg, 2), round(a_db, 2),
                                round((a_pkg - a_db) / a_db * 100.0, 4)])
            out["app_db_amount"] = {"snap_rows": len(snap), "compared": cmp,
                                    "within_0_5pct": ok, "bad_samples": bad}
    except Exception as e:  # noqa: BLE001
        out["app_db_amount"] = {"error": str(e)[:160]}
    return out


# ------------------------------------------------------------------ 降级：腾讯快照

def _snapshot_codes(con) -> list:
    """降级抓取的候选池 = 权威股票宇宙（含北交所，剔除转债/ETF）。

    直接复用 `fetch_a_shares_tencent._candidate_codes`：那是「哪个代码算股票」的
    既有唯一入口（`security_master.stock_codes()`，kind='stock'）。自己再写一套前缀
    判断就会重现 2026-11-01 那次「把可转债当股票抓进快照」的事故。
    """
    import fetch_a_shares_tencent as FT
    return FT._candidate_codes(con)


def write_snapshot(quotes, target: str, dry_run: bool) -> dict:
    """降级通道：把腾讯全市场快照落进 viewer/app.db 的 stock_daily_snapshot。

    口径完全照 `tools/fetch_a_shares_tencent.run()`：
      · schema 用它的 `_ensure_schema`（不自己 CREATE，避免两份 DDL 慢慢跑偏）
      · 源声明的行情日 ≠ 目标日 → **拒绝落库**（实时接口没有日期参数，
        拿最新一场去填历史日就是伪造历史）
      · 源**没有**声明行情日（拿不到可校验字段）→ **同样拒绝落库**。
        2026-11-04 P1：`_session_mismatch` 返回 `{}` 既可能「一致」也可能
        「一条可校验的都没有」，原代码把两者混为一谈 → 守卫恒通过。
        无法校验 ≠ 校验通过，这里按 fail-closed 处理（`refused=source_session_unknown`）。
      · 当日零成交（volume 与 amount 同时为 0）→ 记 `no_trade` 且**不落库**
        （退市股会一直返回最后一天的价格，照落就是给它伪造今日 K 线）
    """
    import fetch_a_shares_tencent as FT
    from viewer import units as U
    t0 = time.time()
    stat = {"insert": 0, "skip": {}, "elapsed_sec": 0.0, "session_check": None}
    inc = stat["skip"]
    q = quotes or {}
    want8 = str(target or "").replace("-", "")
    # 「可校验」= 这一行带着源自己声明的行情日（会话日）。不能拿「有没有 price」代替：
    # 价格任何一天都有，会话日才能回答「这是**哪一天**的价」。
    probe = [k for k in q if not str(k).startswith("__")]
    verified = {k for k in probe if str((q.get(k) or {}).get("_session") or "").strip()}
    clean = FT._session_mismatch(q, target)
    stat["session_check"] = {"want": want8, "got": clean,
                             "rows": len(probe), "checkable": len(verified)}

    # ⚠️ 2026-11-04 P1 的修法（两层，缺一层都不算修好）：
    #   ① `viewer/fastfetch.py` 解析腾讯时**同时**写 `_session`（源头统一键名）；
    #   ② 这里 fail-closed —— 因为 `_session_mismatch` 的返回值有个致命歧义：
    #      它返回 `{}` 既可能是「全部一致」，也可能是「一条可校验的都没有」。
    #      原先的代码把两者当同一件事，于是守卫恒通过、一行都不拦。
    #      **无法校验 ≠ 校验通过**：没有会话日时一律拒写，绝不当成一致。
    if not probe:
        stat["refused"] = "no_source_rows"
        stat["elapsed_sec"] = round(time.time() - t0, 2)
        stat["insert"] = 0
        return stat
    if not verified:
        # 整批都没有会话日 = 上游契约变了（换源/字段位移）→ 硬失败，不许静默落库。
        stat["refused"] = "source_session_unknown"
        stat["elapsed_sec"] = round(time.time() - t0, 2)
        stat["insert"] = 0
        return stat
    if clean:
        # 有任何一个会话日不符就整轮拒绝：分批抓到一半混两个日期，事后无法分辨
        stat["refused"] = "source_session_mismatch"
        stat["elapsed_sec"] = round(time.time() - t0, 2)
        stat["insert"] = 0
        return stat
    con = sqlite3.connect(APP_DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    try:
        FT._ensure_schema(con)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for code, r in (quotes or {}).items():
            if code.startswith("__"):
                continue
            if code not in verified:
                # 混合情形：整批里有会话日、但**这一行**没有 → 只丢这一行。
                # 宁可少写一行日期明确的价，也不写一行日期不明的价；
                # 丢了多少会出现在结果 JSON 的 skip 直方图里，不是静默吞掉。
                inc["session_unverified"] = inc.get("session_unverified", 0) + 1
                continue
            if U.stock_kind(code, r.get("name") or "") != U.KIND_STOCK:
                inc["non_stock"] = inc.get("non_stock", 0) + 1
                continue
            if not r.get("price"):
                inc["no_quote"] = inc.get("no_quote", 0) + 1
                continue
            if not (r.get("volume_raw") or 0) and not (r.get("amount") or 0):
                inc["no_trade"] = inc.get("no_trade", 0) + 1
                continue
            board, exchange = FT._board_of(code)
            rows.append((
                target, code, r.get("name"), board, exchange,
                r.get("open"), r.get("high"), r.get("low"), r.get("price"),
                r.get("prev_close"), r.get("pct"), r.get("change"),
                # volume 存**源原值**，不在这里归一：这张表的历史行本来就是
                # 「科创板存股、其余存手」，viewer/units.snapshot_volume_units()
                # 靠 amount÷close÷volume 自证。我若单方面改存「股」，
                # 反而会让那套自证对老行/新行给出两种答案。
                r.get("volume_raw"), r.get("amount"), r.get("turnover"), None,
                r.get("pe_ttm"), None, r.get("pb"), r.get("total_mv"), r.get("float_mv"),
                None, None, now))
        stat["insert"] = len(rows)
        if not dry_run and rows:
            con.executemany(
                "INSERT OR REPLACE INTO stock_daily_snapshot("
                "trade_date,code,name,board,exchange,open,high,low,close,prev_close,pct,change,"
                "volume,amount,turnover,vol_ratio,pe_ttm,pe_dynamic,pb,total_mv,float_mv,"
                "total_shares,float_shares,updated_at) VALUES(" + ",".join(["?"] * 24) + ")",
                rows)
            con.commit()
    finally:
        con.close()
    stat["elapsed_sec"] = round(time.time() - t0, 2)
    return stat


# ------------------------------------------------------------------ 主流程

STEPS = ["判包", "取数", "写前复权库", "写后复权库", "快照/收尾"]


def run(date=None, write: bool = False, progress=None) -> dict:
    """跑一轮。返回**可直接 JSON 序列化**的摘要。

    progress(step_index:int 1..5, step:str, detail:str) 可选回调 ——
    `POST /api/fast_grab` 的 status 端点靠它报「第几步 / 在干什么」。
    """
    t_all = time.time()
    steps = {}

    def step(i, detail=""):
        if progress:
            try:
                progress(i, STEPS[i - 1], detail)
            except Exception:  # noqa: BLE001 —— 回调出错绝不影响抓取
                pass
        _log("%d/%d %s %s" % (i, len(STEPS), STEPS[i - 1], detail))

    from viewer import fastfetch as FF

    summary = {"ok": True, "date": None, "write": bool(write), "dry_run": not write,
               "degraded": False, "reason": "", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "steps": steps, "elapsed_sec": 0.0}

    target = resolve_date(date)
    summary["date"] = target
    d8 = _d8(target)
    ex_map = load_ex_date_map()
    ex_codes = set(ex_map.get(target) or [])
    ex_after = codes_ex_after(ex_map, target)
    summary["ex_div_codes"] = sorted(ex_codes)
    summary["ex_div_after_codes"] = sorted(ex_after)
    summary["adjust_cache_ok"] = bool(ex_map)

    # ── 1. 判包 ────────────────────────────────────────────────────────────
    step(1, "HEAD %s" % target)
    t0 = time.time()
    avail, probe = FF.package_available(target)
    steps["probe"] = {"elapsed_sec": round(time.time() - t0, 2), "available": avail, **probe}

    package_rows = []
    quotes = {}
    if avail:
        # ── 2a. 抓包 ───────────────────────────────────────────────────────
        step(2, "下载并解析盘后包")
        t0 = time.time()
        raw = FF.download_package(target)
        allrows = FF.tdx_daily_package(target, blob=raw)
        stocks = FF.only_stocks(allrows)
        package_rows = stocks
        steps["fetch"] = {"source": "tdx_package", "bytes": len(raw),
                          "rows_all": len(allrows), "rows_stock": len(stocks),
                          "stats": FF.package_stats(allrows),
                          "elapsed_sec": round(time.time() - t0, 2)}
    else:
        # ── 2b. 降级：腾讯快照（**不写 kline/kbase**，见模块顶部说明）───────
        step(2, "降级：腾讯批量快照")
        t0 = time.time()
        con = sqlite3.connect("file:%s?mode=ro" % APP_DB, uri=True, timeout=30)
        try:
            codes = _snapshot_codes(con)
        finally:
            con.close()
        quotes = FF.tencent_quote_bulk(codes)
        failed = quotes.get("__failed_batches__") or []
        quotes = FF.strip_internal(quotes)
        summary["degraded"] = True
        summary["reason"] = "日线包未发布，已用快照顶上"
        steps["fetch"] = {"source": "tencent_quotes", "codes_requested": len(codes),
                          "parsed": len(quotes), "failed_batches": failed,
                          "elapsed_sec": round(time.time() - t0, 2)}

    # ── 3. 写前复权库 ─────────────────────────────────────────────────────
    if package_rows:
        step(3, "写 data/kline_daily.db")
        steps["kline"] = write_kline(package_rows, target, ex_codes, not write)
        # 对账放在写之后跑：写之前跑看到的是旧库，写之后跑才能证明「写进去的就是对的」。
        # 传入的是「target 之后除权」的集合（见 codes_ex_after 的说明），
        # 用它解释「源=原始价 / 库=已重排的前复权价」这类**预期内**的差异。
        steps["reconcile"] = reconcile(package_rows, target, ex_after)
    else:
        steps["kline"] = {"skipped": True,
                          "why": "降级模式不写前复权库（腾讯量按百股截断，见模块说明③）"}
        steps["reconcile"] = {"skipped": True, "why": "无盘后包，无可对账"}

    # ── 4. 写后复权库 ─────────────────────────────────────────────────────
    if package_rows:
        step(4, "写 viewer/kbase.db")
        steps["kbase"] = write_kbase(package_rows, target, not write)
    else:
        steps["kbase"] = {"skipped": True, "why": "降级模式不写后复权库"}

    # ── 5. 快照 / 收尾 ────────────────────────────────────────────────────
    step(5, "快照" if summary["degraded"] else "收尾")
    if summary["degraded"]:
        steps["snapshot"] = write_snapshot(quotes, target, not write)
        if steps["snapshot"].get("refused"):
            # 三种拒写原因要说人话分开讲：把「源日期不符」和「源没给日期」
            # 混成一句「源行情日 {} ≠ 目标 {}」，读的人会以为是数据错，其实是契约断。
            chk = steps["snapshot"].get("session_check") or {}
            refused = steps["snapshot"]["refused"]
            # 会话日直方图渲染成人话（`{'20261103': 5586}` 直接塞进报错里没法看）
            got_txt = ",".join("%s×%d 只" % (k, v)
                               for k, v in sorted((chk.get("got") or {}).items())) or "无"
            why = {
                "source_session_mismatch":
                    "源行情日 %s ≠ 目标 %s，写进去就是伪造历史" % (got_txt, chk.get("want")),
                "source_session_unknown":
                    "腾讯返回的 %s 行里没有任何可校验的行情日（session），无法确认这是哪一天的价"
                    % chk.get("rows"),
                "no_source_rows":
                    "腾讯没有返回任何行情行（%s 行），无可写" % chk.get("rows"),
            }.get(refused, refused)
            summary["ok"] = False
            summary["error"] = "已拒绝落库（%s）：%s" % (refused, why)
    else:
        steps["snapshot"] = {"skipped": True,
                             "why": "包已拿到，快照由既有 a_shares_snapshot 任务负责（含 PE/PB/市值）"}

    summary["elapsed_sec"] = round(time.time() - t_all, 2)
    summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return summary


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="一次请求拿全市场当日一根（通达信盘后包，缺包时降级腾讯快照）")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD；缺省=最近交易日")
    ap.add_argument("--dry-run", action="store_true", default=False,
                    help="只抓不写，打印对账（未给 --write 时的默认行为）")
    ap.add_argument("--write", action="store_true", default=False,
                    help="真写库（kline_daily.db / kbase.db / 降级时的 app.db 快照）")
    a = ap.parse_args(argv)
    write = bool(a.write) and not bool(a.dry_run)
    try:
        summary = run(date=a.date, write=write)
    except ValueError as e:
        # 「当日包未发布」在 run() 里已经被降级分支消化掉，能走到这里的
        # 是日期格式之类的前置错误 —— 明确报出来，不假装成功。
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)),
                          "traceback": traceback.format_exc()[-1200:]},
                         ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, default=str))
    # 只有「明确判失败」才给非 0：降级是有意的正常路径，不是失败。
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
