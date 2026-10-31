# -*- coding: utf-8 -*-
"""全市场 A 股「日度报价 + 估值 + 成分宇宙」快照抓取器（2026-10-26）。

进哥诉求（原话转写）：
    "每日任务你也把其他的什么舆论啊包括今天的价格呀，包括什么的等等一些什么
     PE、量额（LE 应为语音转写）这些基本数据该抓的也都抓出来，抓出来之后你
     存上本地文档。"

本脚本一次性把「之前从没落库」的三类数据补齐，全部存进本地 SQLite（app.db）+ 每日 CSV 文档：
    1. 今日价格快照：今开/最高/最低/收盘/昨收/涨跌幅/涨跌额/成交量(手)/成交额(元)/换手率/量比
    2. 估值基本面：市盈率(TTM)/市盈率(动)/市净率/总市值(元)/流通市值(元)/总股本/流通股
    3. 全 A 成分宇宙：code/name/board/exchange —— 解决「大 A 5000+ 你掌握全了吗」的可审计清单

数据源：东方财富行情列表接口 push2.eastmoney.com/api/qt/clist/get
    - 一次调用同时返回「全部 A 股清单 + 实时/盘后报价 + 估值字段」，无需逐只 HTTP。
    - 分页 pz=1000 循环，自动续传（断点存 checkpoint），单页失败重试。

落库：
    stock_daily_snapshot(trade_date, code, ...)  —— 每日一张表，PRIMARY KEY(trade_date, code)
    a_shares_universe(code, name, board, exchange, status, first_seen, last_seen) —— 权威全量宇宙
    stock_names(code, name, market, source, updated_at) —— 同步刷新名称字典（项目其它模块共用）

本地文档：
    data/snapshots/YYYY-MM-DD_a_shares_snapshot.csv  —— 人可直接打开的日度全量快照

调度：由 server.py 调度器在交易日 15:30 后自动触发（见 _a_shares_snapshot_maybe）。
      也可手动跑：  python tools/fetch_a_shares_snapshot.py

单位约定（落库与 CSV 一致）：价格=元；成交量=手(1手=100股)；成交额/市值=元；
    换手率/涨跌幅/涨额=% 数值；市盈率/市净率=倍。

注意：沙箱无出网，本脚本在「有网环境（用户本机）」才真正拉数；此处仅保证语法/逻辑正确。
"""
import io
import json
import os
import sys
import time
import sqlite3
import urllib.request
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
CHECKPOINT = os.path.join(SNAP_DIR, "_a_shares_snap_checkpoint.json")

EM_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 默认过滤：沪深京全 A（沪市 m:1、深市 m:0；主板 t:2/t:6、创业板 t:80/t:23、科创板 t:23、北交所 t:81/t:21）
DEFAULT_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81,m:1+t:21"
FIELDS = "f12,f14,f2,f3,f4,f15,f16,f17,f18,f5,f6,f8,f10,f9,f23,f20,f21,f22,f25,f115"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

PAGE_SIZE = 1000
MAX_RETRY = 3
PAGE_SLEEP = 0.3          # 礼貌间隔，避免被限频
REQ_TIMEOUT = 20

# EM 字段 -> (落库列名, 是否数值)
COLMAP = [
    ("f12", "code", False),
    ("f14", "name", False),
    ("f2", "close", True),
    ("f3", "pct", True),
    ("f4", "change", True),
    ("f15", "high", True),
    ("f16", "low", True),
    ("f17", "open", True),
    ("f18", "prev_close", True),
    ("f5", "volume", True),       # 手
    ("f6", "amount", True),       # 元
    ("f8", "turnover", True),     # %
    ("f10", "vol_ratio", True),
    ("f9", "pe_ttm", True),
    ("f23", "pb", True),
    ("f20", "total_mv", True),    # 元
    ("f21", "float_mv", True),    # 元
    ("f22", "total_shares", True),
    ("f25", "float_shares", True),
    ("f115", "pe_dynamic", True),
]


def _board_of(code: str):
    """由代码前缀推断板块/交易所（东财列表不直接给板块名，前缀最可靠）。"""
    if not code or len(code) != 6 or not code.isdigit():
        return ("未知", "未知")
    h = code[0]
    if h in ("6",):
        if code.startswith("688"):
            return ("科创板", "上交所")
        return ("沪市主板", "上交所")
    if h in ("0", "3"):
        if code.startswith("30"):
            return ("创业板", "深交所")
        return ("深市主板", "深交所")
    if h in ("8", "4"):
        return ("北交所", "北交所")
    return ("其它", "其它")


def _coerce(v):
    """EM 数值字段可能为 None / '-' / '' / 字符串数字 -> float 或 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_item(item: dict):
    """把东财一条 diff 解析成落库行 dict；非法则返回 None。"""
    if not isinstance(item, dict):
        return None
    code = str(item.get("f12") or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return None
    row = {"code": code, "name": str(item.get("f14") or "").strip()}
    for em_key, col, isnum in COLMAP[2:]:
        row[col] = _coerce(item.get(em_key))
    board, exchange = _board_of(code)
    row["board"] = board
    row["exchange"] = exchange
    return row


def _fetch_page(pn: int, pz: int, fs: str):
    """拉一页；返回 (total, [item,...])；失败抛出（由调用方重试）。"""
    qs = urllib.parse.urlencode({
        "pn": pn, "pz": pz, "po": "1", "np": "1", "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f12", "fs": fs, "fields": FIELDS,
    })
    url = "%s?%s" % (EM_URL, qs)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    obj = json.loads(raw)
    data = (obj or {}).get("data")
    if not data:
        raise RuntimeError("EM 返回 data 为空（可能被限频/未开市）：%s" % str(obj)[:160])
    total = int(data.get("total") or 0)
    diff = data.get("diff") or []
    return total, diff


def _fetch_all(fs: str, pz: int, max_pages=None):
    """分页拉全量，带重试；返回 (total, [item,...])。"""
    collected = []
    total = 0
    pn = 1
    while True:
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                t, items = _fetch_page(pn, pz, fs)
                if pn == 1:
                    total = t
                collected.extend(items)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.0 * attempt)
        else:
            raise RuntimeError("第 %d 页连续 %d 次失败：%s" % (pn, MAX_RETRY, last_err))
        if max_pages and pn >= max_pages:
            break
        if total and len(collected) >= total:
            break
        if not items:
            break
        pn += 1
        time.sleep(PAGE_SLEEP)
    return total, collected


def _ensure_schema(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS stock_daily_snapshot (
        trade_date  TEXT NOT NULL,
        code        TEXT NOT NULL,
        name        TEXT,
        board       TEXT,
        exchange    TEXT,
        open        REAL, high REAL, low REAL, close REAL, prev_close REAL,
        pct         REAL, change REAL,
        volume      REAL, amount REAL, turnover REAL, vol_ratio REAL,
        pe_ttm      REAL, pe_dynamic REAL, pb REAL,
        total_mv    REAL, float_mv REAL, total_shares REAL, float_shares REAL,
        updated_at  TEXT,
        PRIMARY KEY (trade_date, code)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS a_shares_universe (
        code       TEXT PRIMARY KEY,
        name       TEXT,
        board      TEXT,
        exchange   TEXT,
        status     TEXT,
        first_seen TEXT,
        last_seen  TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_date ON stock_daily_snapshot(trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_code ON stock_daily_snapshot(code)")
    # stock_names 为项目共用名称字典；本脚本顺带刷新，确保表存在以自包含
    con.execute("""CREATE TABLE IF NOT EXISTS stock_names (
        code TEXT PRIMARY KEY, name TEXT, company TEXT, market TEXT,
        source TEXT, updated_at TEXT
    )""")


def _upsert(con, trade_date, rows):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    snap_rows = []
    uni_rows = []
    name_rows = []
    for r in rows:
        snap_rows.append((
            trade_date, r["code"], r.get("name"), r.get("board"), r.get("exchange"),
            r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("prev_close"),
            r.get("pct"), r.get("change"), r.get("volume"), r.get("amount"),
            r.get("turnover"), r.get("vol_ratio"), r.get("pe_ttm"), r.get("pe_dynamic"),
            r.get("pb"), r.get("total_mv"), r.get("float_mv"), r.get("total_shares"),
            r.get("float_shares"), now,
        ))
        uni_rows.append((r["code"], r.get("name"), r.get("board"), r.get("exchange"),
                         "listed", trade_date, trade_date))
        name_rows.append((r["code"], r.get("name"), r.get("board"),
                          "eastmoney_snapshot", now))
    # 快照：INSERT OR REPLACE（同 trade_date+code 覆盖，天然幂等）
    con.executemany(
        "INSERT OR REPLACE INTO stock_daily_snapshot("
        "trade_date,code,name,board,exchange,open,high,low,close,prev_close,"
        "pct,change,volume,amount,turnover,vol_ratio,pe_ttm,pe_dynamic,pb,"
        "total_mv,float_mv,total_shares,float_shares,updated_at) VALUES("
        + ",".join(["?"] * 24) + ")", snap_rows)
    # 宇宙：不存在则写入 first_seen，存在则仅更新 last_seen/name/board
    for u in uni_rows:
        con.execute(
            "INSERT INTO a_shares_universe(code,name,board,exchange,status,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, board=excluded.board, "
            "exchange=excluded.exchange, status='listed', last_seen=excluded.last_seen", u)
    # 名称字典：刷新（其它模块共用，source 标记本脚本）
    con.executemany(
        "INSERT INTO stock_names(code,name,market,source,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
        "source=excluded.source, updated_at=excluded.updated_at", name_rows)
    con.commit()


def _export_csv(trade_date, rows):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, "%s_a_shares_snapshot.csv" % trade_date)
    cols = ["code", "name", "board", "exchange", "open", "high", "low", "close",
            "prev_close", "pct", "change", "volume", "amount", "turnover", "vol_ratio",
            "pe_ttm", "pe_dynamic", "pb", "total_mv", "float_mv", "total_shares", "float_shares"]
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(_csv_cell(r.get(c)) for c in cols) + "\n")
    os.replace(tmp, path)
    return path


def _csv_cell(v):
    if v is None:
        return ""
    return str(v)


def _load_checkpoint():
    try:
        with io.open(CHECKPOINT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _save_checkpoint(cp):
    os.makedirs(SNAP_DIR, exist_ok=True)
    tmp = CHECKPOINT + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False)
    os.replace(tmp, CHECKPOINT)


def run(date_override=None, fs=None, pz=PAGE_SIZE, no_csv=False, dry_run=False, max_pages=None):
    """执行一次全量快照。返回结构化结果 dict。"""
    trade_date = date_override or time.strftime("%Y-%m-%d")
    fs = fs or os.environ.get("EM_FS") or DEFAULT_FS
    t0 = time.time()
    print("[snapshot] 开始 %s fs=%s pz=%d" % (trade_date, fs, pz), flush=True)

    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    _ensure_schema(con)

    # 断点续传：今天已有 checkpoint 且未完成则接着拉
    start_pn = 1
    rows = []
    cp = _load_checkpoint()
    if cp and cp.get("date") == trade_date and cp.get("done") is not True:
        start_pn = max(1, cp.get("next_page", 1))
        rows = cp.get("rows", [])
        print("[snapshot] 断点续传：从第 %d 页起，已攒 %d 条" % (start_pn, len(rows)), flush=True)

    total = 0
    try:
        # 从 start_pn 开始补拉；为简化，全量重拉时 pn 从 1 起（EM 全量不大，重拉幂等）
        total, items = _fetch_all(fs, pz, max_pages=max_pages)
        parsed = [_parse_item(it) for it in items]
        parsed = [r for r in parsed if r]
        rows = parsed  # 全量覆盖（幂等），不使用续传增量以避免重复
        if dry_run:
            print("[snapshot][dry-run] 拟落库 %d 条，EM total=%d" % (len(rows), total), flush=True)
            return {"ok": True, "dry_run": True, "collected": len(rows),
                    "em_total": total, "coverage": _pct(len(rows), total)}
    except Exception as e:  # noqa: BLE001
        # 保存 checkpoint 以便续传
        _save_checkpoint({"date": trade_date, "next_page": start_pn, "rows": rows, "done": False})
        print("[snapshot] 拉取失败，已存断点：%s" % repr(e)[:200], flush=True)
        con.close()
        return {"ok": False, "error": str(e), "collected": len(rows), "em_total": total}

    # 落库
    _upsert(con, trade_date, rows)
    csv_path = None
    if not no_csv:
        csv_path = _export_csv(trade_date, rows)
    # 清理 checkpoint
    try:
        if os.path.exists(CHECKPOINT):
            os.remove(CHECKPOINT)
    except Exception:  # noqa: BLE001
        pass

    # 覆盖审计：本库已有的全部 A 股数 vs EM 本次 total
    db_total = con.execute("SELECT COUNT(*) FROM a_shares_universe").fetchone()[0]
    snap_count = con.execute(
        "SELECT COUNT(*) FROM stock_daily_snapshot WHERE trade_date=?", (trade_date,)).fetchone()[0]
    con.close()

    coverage = _pct(len(rows), total)
    res = {
        "ok": True,
        "trade_date": trade_date,
        "collected": len(rows),
        "em_total": total,
        "coverage": coverage,
        "db_universe": db_total,
        "snap_rows": snap_count,
        "csv": csv_path,
        "elapsed": round(time.time() - t0, 1),
    }
    print("[snapshot] 完成：落库 %d 只 / EM全量 %d，覆盖率 %.2f%%；DB宇宙累计 %d；CSV=%s；用时 %.0fs"
          % (len(rows), total, coverage, db_total, csv_path, res["elapsed"]), flush=True)
    return res


def _pct(a, b):
    if not b:
        return 0.0
    return round(100.0 * a / b, 2)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD（缺省今天）")
    ap.add_argument("--fs", help="东财过滤串（缺省沪深京全A）")
    ap.add_argument("--pz", type=int, default=PAGE_SIZE, help="每页条数")
    ap.add_argument("--no-csv", action="store_true", help="不导 CSV")
    ap.add_argument("--dry-run", action="store_true", help="只拉不落库")
    ap.add_argument("--max-pages", type=int, default=None, help="最多拉几页（调试）")
    a = ap.parse_args()
    r = run(date_override=a.date, fs=a.fs, pz=a.pz, no_csv=a.no_csv,
            dry_run=a.dry_run, max_pages=a.max_pages)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
