"""本地数据仓 · 全站统一的「先读自己存的数据」取数层（2026-10-29）。

## 为什么要有这一层

这个应用里有一批面板（换手榜 / 阴跌榜 / 低位箱体 / 智能选股 / 回测）需要**全市场日线**。
它们原来的取数方式是「逐只去腾讯/新浪/baostock/akshare 抓一遍」：

    · 全 A 股约 5500 只 → 5500 次 HTTP 请求，单次 100~300ms
    · 串行要 9~27 分钟，即使 8 线程并发也要 45 秒以上
    · 这 45 秒里**请求线程一直被占着**，而服务是单进程多线程 + Python GIL，
      于是整站所有接口一起冻住 —— 用户看到的就是「打开一分钟，一堆面板同时加载失败」

而本机其实**早就有一份完整的历史日线**：`viewer/kbase.db`
（5239 只票、1473 万根 K 线、日更到最新交易日）。读它一条 SQL 就能拿完全市场，
耗时 1~3 秒，且**不占网络、不受外网限次、不会封号**。

## 所以这里的纪律

1. **本地优先**：任何「全市场/批量」取数，先走 `pool()` + `bars_map()`；
   只有本地确实缺这只票时，才轮到调用方自己去补网。
2. **只读，不写**：本模块绝不改动 kbase.db / app.db，只作查询。
3. **一次批量，绝不逐只**：`bars_map()` 内部用 `kbase.ohlcv_bulk` 一次 SQL 分段取，
   日期字符串全池共享同一对象（内存与耗时都降到零头）。
4. **诚实降级**：本地没有就返回空，让调用方按自己的口径处理，**绝不编造假数据**。
"""
from __future__ import annotations

import sqlite3
import threading

_LOCK = threading.Lock()


def _kbase():
    """惰性拿 kbase 单例（避免循环导入：kbase → 本模块 → kbase）。"""
    from viewer import kbase as _kb
    return _kb.get_base()


def _appdb_path() -> str:
    try:
        from viewer.db import DB_PATH
        return DB_PATH
    except Exception:  # noqa: BLE001
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def pool(with_names: bool = True) -> dict:
    """全市场股票池 {code: name}：本地 K 线库 ∪ 名称表。

    · 以 `kbase.db` 的 code 为准（有日线才算「可扫描」，这是各面板的实际前提）；
    · 名称从 `app.db.stock_names` 补（kbase 不存名称）；
    · 名称缺失的票保留在池里但名字为空串 —— 宁可显示代码，也不把票从池里丢掉。
    """
    codes = []
    try:
        codes = _kbase().codes()
    except Exception:  # noqa: BLE001
        codes = []
    names = {}
    if with_names:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % _appdb_path(), uri=True)
            try:
                for code, name in con.execute("SELECT code, name FROM stock_names"):
                    if code:
                        names[str(code)] = (name or "").strip()
            finally:
                con.close()
        except sqlite3.Error:
            names = {}
    out = {}
    for c in codes:
        out[c] = names.get(c, "")
    # 名称表里有些票 kbase 没有（新上市/停采），也放进来，让调用方自己决定要不要补
    for c, n in names.items():
        if c not in out and n:
            out[c] = n
    return out


def names(codes) -> dict:
    """按代码批量取名称（一次 SQL，不逐条查）。"""
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _appdb_path(), uri=True)
        try:
            CH = 500
            for i in range(0, len(codes), CH):
                chunk = codes[i:i + CH]
                q = ("SELECT code, name FROM stock_names WHERE code IN (%s)"
                     % ",".join("?" * len(chunk)))
                for code, name in con.execute(q, chunk):
                    out[str(code)] = (name or "").strip()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return out


def bars_map(codes, keep_bars: int = 260) -> dict:
    """批量取日线 → {code: [(date, close, high, low, vol), ...]}（升序，最多 keep_bars 根）。

    元组口径与 `screener._load_bars_map` 完全一致（date, close, high, low, vol），
    这样换手率/阴跌/箱体那些既有算法一行都不用改。

    kbase 存的是**后复权**（KBASE_ADJ="hfq"，与策略回测同源），所以拿它算
    「区间涨跌幅 / 均线 / 换手率比值」时前后口径自洽；绝对价格与实时盘口可能有差异，
    面板上本来就有「数据日」标注，不构成误导。
    """
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    keep = max(30, int(keep_bars or 260))
    # 只取最近 keep 个**交易日**，而不是全历史：
    # kbase 有 1473 万行，全量读 5240 只实测 20s；按日期下界截断后只剩 ~70 万行，1~3s。
    # 换算用交易日历（distinct_dates 已按 mtime 记忆化，不再重复算）。
    min_date = ""
    try:
        cal = _kbase().distinct_dates()
        if cal:
            min_date = cal[-keep] if len(cal) > keep else cal[0]
    except Exception:  # noqa: BLE001
        min_date = ""
    try:
        raw = _kbase().ohlcv_bulk(codes, min_date=min_date)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        tail = rows[-keep:]
        out[code] = [(dt, c, h, low, v) for (dt, _o, h, low, c, v) in tail]
    return out


def klines_bulk(codes) -> dict:
    """批量取**全历史**日线 → {code: [{"date","open","high","low","close","volume"}, ...]}。

    给「低位箱体」这类需要**上市以来分位**的面板用：它们要的是全历史，
    必须走本地库，否则 80 只票 × 一次东财全历史请求 = 45 秒挂死。
    字典键与 `api_kline()` 返回的 `klines` 完全一致，调用方直接替换数据来源即可。
    """
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    try:
        raw = _kbase().ohlcv_bulk(codes)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        out[code] = [{"date": dt, "open": o, "high": h, "low": low, "close": c, "volume": v}
                     for (dt, o, h, low, c, v) in rows]
    return out


def latest_date() -> str:
    """本地日线的最新交易日（面板上「数据日」标注与新鲜度判断的基准）。"""
    try:
        return _kbase().latest_date() or ""
    except Exception:  # noqa: BLE001
        return ""


def coverage(codes) -> dict:
    """报告这批代码里有多少本地没有（调用方据此决定是否值得去补网）。"""
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {"total": 0, "local": 0, "missing": []}
    try:
        have = set(_kbase().codes())
    except Exception:  # noqa: BLE001
        return {"total": len(codes), "local": 0, "missing": codes}
    miss = [c for c in codes if c not in have]
    return {"total": len(codes), "local": len(codes) - len(miss), "missing": miss}
