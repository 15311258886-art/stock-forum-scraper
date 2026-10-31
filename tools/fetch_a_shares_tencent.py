# -*- coding: utf-8 -*-
"""全市场 A 股「日度报价 + 估值 + 成分宇宙」快照抓取器 —— 腾讯源（2026-10-27 改用）。

为什么不用东财（背景，进哥质疑后更正）：
  原 fetch_a_shares_snapshot.py 用东方财富 push2 接口，但本项目执行环境（WorkBuddy 沙箱）
  的出口 IP 被东财在连接层 RST 屏蔽（RemoteDisconnected，0.2s 秒断），而同环境的
  百度/新浪/腾讯/雪球均正常 —— 说明不是「东财系统性屏蔽所有人」，而是东财对云/数据中心
  IP 段的常规屏蔽，且绝非「我们抓太多被封」（首次连接即被断，无从超量）。
  腾讯 qt.gtimg.cn 行情接口含完整报价 + 估值（PE-TTM/PB/总市值/流通市值/换手率），
  且本环境可达，故改用腾讯源补齐 C4 估值空洞与全 A 宇宙。

字段取自腾讯 qt.gtimg.cn 的 ~ 分隔 88 字段（已逐字段交叉验证，茅台实测吻合）：
  f[1]名称 f[2]代码 f[3]现价 f[4]昨收 f[5]今开 f[31]涨跌额 f[32]涨跌幅%
  f[33]最高 f[34]最低 f[36]成交量(手) f[37]成交额(万元) f[38]换手率%
  f[39]市盈率TTM f[44]流通市值(亿) f[45]总市值(亿) f[46]市净率

落库（与东财版完全一致，verify_fetch.py / 漏斗无需改动）：
  stock_daily_snapshot(trade_date, code, ...)  —— 每日一张，PK(trade_date, code)
  a_shares_universe(code, name, board, exchange, status, first_seen, last_seen)
  stock_names(code, name, market, source, updated_at)

运行：python tools/fetch_a_shares_tencent.py
幂等：同 (trade_date,code) INSERT OR REPLACE。
"""
import io
import os
import sys
import time
import sqlite3
import urllib.request
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")

TENCENT_URL = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}
BATCH = 100          # 每批请求股票数（腾讯批量上限约 300，100 稳妥）
MAX_RETRY = 3
REQ_TIMEOUT = 12
PAGE_SLEEP = 0.12    # 批间礼貌间隔


def _market_prefix(code: str):
    """腾讯 qt.gtimg.cn 必须带市场前缀才识别：沪/科创 sh、深/创业 sz、北交所 bj。"""
    if code[0] == "6":
        return "sh"
    if code[0] in ("0", "3"):
        return "sz"
    if code[0] in ("8", "4"):
        return "bj"
    return "sh"


def _board_of(code: str):
    if not code or len(code) != 6 or not code.isdigit():
        return ("未知", "未知")
    h = code[0]
    if h == "6":
        return ("科创板", "上交所") if code.startswith("688") else ("沪市主板", "上交所")
    if h in ("0", "3"):
        return ("创业板", "深交所") if code.startswith("30") else ("深市主板", "深交所")
    if h in ("8", "4"):
        return ("北交所", "北交所")
    return ("其它", "其它")


def _coerce(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_batch(codes: list):
    """请求一批代码，返回 {code: 解析后字段dict}。失败抛异常由调用方重试。"""
    q = ",".join(_market_prefix(c) + c for c in codes)
    url = TENCENT_URL + urllib.parse.quote(q, safe=",")  # 逗号分隔符不能编码，否则腾讯不识别
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        raw = resp.read().decode("gbk", "ignore")
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        rhs = line.split("=")[1].strip().strip('"')
        f = rhs.split("~")
        if len(f) < 47:
            continue
        code = (f[2] or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            continue
        close = _coerce(f[3])
        total_mv = _coerce(f[45])   # 亿
        float_mv = _coerce(f[44])   # 亿
        total_shares = None
        float_shares = None
        if close and close > 0:
            if total_mv is not None:
                total_shares = total_mv * 1e8 / close   # 元 / 元 = 股
            if float_mv is not None:
                float_shares = float_mv * 1e8 / close
        out[code] = {
            "code": code,
            "name": (f[1] or "").strip(),
            "open": _coerce(f[5]),
            "high": _coerce(f[33]),
            "low": _coerce(f[34]),
            "close": close,
            "prev_close": _coerce(f[4]),
            "pct": _coerce(f[32]),
            "change": _coerce(f[31]),
            "volume": _coerce(f[36]),         # 手
            "amount": (_coerce(f[37]) or 0) * 10000.0,   # 万元 -> 元
            "turnover": _coerce(f[38]),       # %
            "pe_ttm": _coerce(f[39]),
            "pe_dynamic": _coerce(f[39]),     # 腾讯无单独动态 PE，以 TTM 代填
            "pb": _coerce(f[46]),
            "total_mv": (total_mv or 0) * 1e8,     # 亿 -> 元
            "float_mv": (float_mv or 0) * 1e8,     # 亿 -> 元
            "total_shares": total_shares,
            "float_shares": float_shares,
        }
    return out


def _ensure_schema(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS stock_daily_snapshot (
        trade_date  TEXT NOT NULL, code TEXT NOT NULL, name TEXT, board TEXT, exchange TEXT,
        open REAL, high REAL, low REAL, close REAL, prev_close REAL,
        pct REAL, change REAL, volume REAL, amount REAL, turnover REAL, vol_ratio REAL,
        pe_ttm REAL, pe_dynamic REAL, pb REAL, total_mv REAL, float_mv REAL,
        total_shares REAL, float_shares REAL, updated_at TEXT,
        PRIMARY KEY (trade_date, code))""")
    con.execute("""CREATE TABLE IF NOT EXISTS a_shares_universe (
        code TEXT PRIMARY KEY, name TEXT, board TEXT, exchange TEXT, status TEXT,
        first_seen TEXT, last_seen TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_date ON stock_daily_snapshot(trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_code ON stock_daily_snapshot(code)")
    con.execute("""CREATE TABLE IF NOT EXISTS stock_names (
        code TEXT PRIMARY KEY, name TEXT, company TEXT, market TEXT,
        source TEXT, updated_at TEXT)""")


def run(date_override=None, no_csv=False, dry_run=False, limit=None):
    trade_date = date_override or time.strftime("%Y-%m-%d")
    t0 = time.time()
    print("[tencent-snap] 开始 %s 源=腾讯qt.gtimg.cn" % trade_date, flush=True)

    # 全 A 候选 = 本地名称字典里的 6 位 A 股代码
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    _ensure_schema(con)
    all_codes = [r[0] for r in con.execute(
        "SELECT code FROM stock_names WHERE code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'").fetchall()]
    if limit:
        all_codes = all_codes[:limit]
    print("[tencent-snap] 本地候选代码 %d 只，分 %d 批（每批 %d）" %
          (len(all_codes), (len(all_codes) + BATCH - 1) // BATCH, BATCH), flush=True)

    parsed = {}
    failed = 0
    for i in range(0, len(all_codes), BATCH):
        batch = all_codes[i:i + BATCH]
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                got = _fetch_batch(batch)
                parsed.update(got)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5 * attempt)
        else:
            failed += 1
            print("  [warn] 第 %d 批连续失败：%s" % (i // BATCH + 1, last_err), flush=True)
        if (i // BATCH) % 10 == 9:
            print("  ... 已处理 %d/%d 批，已解析 %d 只" %
                  (i // BATCH + 1, (len(all_codes) + BATCH - 1) // BATCH, len(parsed)), flush=True)
        time.sleep(PAGE_SLEEP)

    if dry_run:
        print("[tencent-snap][dry-run] 拟落库 %d 只（失败批 %d）" % (len(parsed), failed), flush=True)
        con.close()
        return {"ok": True, "dry_run": True, "collected": len(parsed), "failed_batches": failed}

    # 落库
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    snap_rows, uni_rows, name_rows = [], [], []
    for code, r in parsed.items():
        board, exchange = _board_of(code)
        snap_rows.append((trade_date, code, r.get("name"), board, exchange,
                          r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("prev_close"),
                          r.get("pct"), r.get("change"), r.get("volume"), r.get("amount"),
                          r.get("turnover"), None, r.get("pe_ttm"), r.get("pe_dynamic"), r.get("pb"),
                          r.get("total_mv"), r.get("float_mv"), r.get("total_shares"), r.get("float_shares"),
                          now))
        uni_rows.append((code, r.get("name"), board, exchange, "listed", trade_date, trade_date))
        name_rows.append((code, r.get("name"), board, "tencent_snapshot", now))
    con.executemany(
        "INSERT OR REPLACE INTO stock_daily_snapshot("
        "trade_date,code,name,board,exchange,open,high,low,close,prev_close,pct,change,"
        "volume,amount,turnover,vol_ratio,pe_ttm,pe_dynamic,pb,total_mv,float_mv,"
        "total_shares,float_shares,updated_at) VALUES(" + ",".join(["?"] * 24) + ")", snap_rows)
    for u in uni_rows:
        con.execute(
            "INSERT INTO a_shares_universe(code,name,board,exchange,status,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
            "board=excluded.board, exchange=excluded.exchange, status='listed', "
            "last_seen=excluded.last_seen", u)
    con.executemany(
        "INSERT INTO stock_names(code,name,market,source,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
        "source=excluded.source, updated_at=excluded.updated_at", name_rows)
    con.commit()

    db_universe = con.execute("SELECT COUNT(*) FROM a_shares_universe").fetchone()[0]
    snap_count = con.execute(
        "SELECT COUNT(*) FROM stock_daily_snapshot WHERE trade_date=?", (trade_date,)).fetchone()[0]
    con.close()

    print("[tencent-snap] 完成：落库 %d 只（失败批 %d）/ 候选 %d；DB宇宙 %d；用时 %.0fs"
          % (len(parsed), failed, len(all_codes), db_universe, time.time() - t0), flush=True)
    return {"ok": True, "trade_date": trade_date, "collected": len(parsed),
            "failed_batches": failed, "db_universe": db_universe, "snap_rows": snap_count,
            "elapsed": round(time.time() - t0, 1)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 只（调试）")
    a = ap.parse_args()
    r = run(date_override=a.date, no_csv=a.no_csv, dry_run=a.dry_run, limit=a.limit)
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
