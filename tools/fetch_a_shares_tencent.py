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

2026-11-01（P0「全 A 股宇宙不完整」）：
  · 候选代码改从 `security_master.stock_codes()` 取（含 350 只北交所，非股票被剔除），
    不再用 stock_names 全表 —— 那里混着 2 只可转债 + 1 只 ETF，抓到就落库。
  · `_market_prefix` 修 920 段被判成 sh 的静默 bug（原来只认 8/4 开头是北交所，
    920xxx 落到默认分支拼成 sh，腾讯返回空，永远拿不到行情）。
  · **当日无成交不落库**：退市股（000004 国华退）源会返回陈旧最后价（volume/amount 全 0、
    close==prev_close、pct==0），照落就是伪造当日 K 线。现记 `no_trade` 并跳过，
    逐只写进 `data/snapshots/_fetch_attempt_<date>.json` 留痕。

增量补抓（不动已有行）：
  python tools/fetch_a_shares_tencent.py --date 2026-10-30 --boards 北交所 \
      --batch 50 --sleep 1.5 --merge
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

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 2026-11-01：板块/交易所判定与"这是不是股票"都委托公共模块，本脚本不再自带前缀表。
from viewer import security_master as SM  # noqa: E402
from viewer import units as U  # noqa: E402

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


def _market_prefix(code: str) -> str:
    """腾讯 qt.gtimg.cn 必须带市场前缀才识别：沪/科创 sh、深/创业 sz、北交所 bj。

    2026-11-01（P0）修一个静默 bug：原实现是 `code[0] in ("8","4") → bj`，
    **920 段（北交所 2023 起的新段）落到默认分支被拼成 sh**，腾讯直接返回空 ——
    不会报错，只是这 348 只永远拿不到行情。现在按交易所归属判，唯一来源是
    `security_master.exchange_of()`。
    """
    ex = SM.exchange_of(code)
    return {SM.EXCHANGE_SSE: "sh", SM.EXCHANGE_SZSE: "sz", SM.EXCHANGE_BSE: "bj"}.get(ex, "sh")


def _board_of(code: str):
    """板块/交易所：委托 security_master（全项目唯一允许写代码段规则的地方）。"""
    if not code or len(code) != 6 or not code.isdigit():
        return ("未知", "未知")
    board = SM.board_of(code)
    return (board, SM.exchange_zh(SM.exchange_of(code, board)))


def _candidate_codes(con) -> list:
    """抓取候选 = 权威宇宙里的全部股票（含 350 只北交所）。

    2026-11-01（P0）换源：原来是 `SELECT code FROM stock_names`，虽然覆盖全，
    但把可转债（111024/113710）和 ETF（513050）也当成"股票"去请求，抓到就落库。
    现在用 `security_master.stock_codes()`：kind='stock' 才算，北交所默认包含。
    主表未建时回落到 stock_names+units 过滤（并打印警告，不静默降级）。
    """
    try:
        codes = SM.stock_codes(con)
        if codes:
            return codes
    except Exception as e:  # noqa: BLE001 —— 含 SecurityMasterMissing
        print("[tencent-snap][warn] security_master 不可用（%s），回落到 stock_names" % e,
              flush=True)
    return [c for (c,) in con.execute(
        "SELECT code FROM stock_names WHERE code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'")
        if U.stock_kind(c) == U.KIND_STOCK]


def _codes_of_boards(con, codes, boards) -> list:
    """按板块过滤候选（boards 为板块名集合，如 {"北交所"}）。"""
    want = {boards} if isinstance(boards, str) else set(boards)
    rows = {r["code"]: r for r in SM.load(con)}
    return [c for c in codes if (rows.get(c) or {}).get("board") in want]


def _write_attempt_report(path: str, payload: dict) -> None:
    """落一份"这次抓了谁、谁没抓到、为什么"的取证文件。

    verify_fetch.py 会读它来区分「缺行情」和「源不覆盖」—— 光凭快照表缺一行，
    没法判断到底是停牌、退市，还是我们根本没请求到。取证比猜测重要。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 —— 取证失败不该让抓取结果作废
        print("[tencent-snap][warn] 取证文件写入失败：%s" % e, flush=True)


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
            # f[30] 是行情时间 yyyymmddHHMMSS —— 源自己声明的"这是哪一场的数据"。
            # 落库前必须和目标 trade_date 比对（见 run()），否则拿最新一场的行情
            # 去填历史某天 = 伪造历史快照（不报错、事后无从分辨）。
            "_session": (f[30] or "").strip(),
        }
    return out


def _session_mismatch(parsed: dict, trade_date: str) -> dict:
    """源声明的行情日期 ≠ 目标交易日时，返回 {源会话日: 只数}；空字典 = 全部一致。

    2026-11-01：`--date` 是给"当日重跑 / 补当日缺口"用的，**不是时光机**。
    腾讯 qt.gtimg.cn 是实时接口、没有日期参数，永远只返回最新一场（字段 f[30]）。
    若不校验，`--date 2026-10-29 --merge` 就会把 10-30 的收盘价写成 10-29 的行 ——
    得到一份"看着完整、实际全错"的历史快照。这种数据比缺数据危险得多：
    缺数据有人会问，错数据没人会问。
    """
    want = str(trade_date or "").replace("-", "")
    sessions: dict = {}
    for r in parsed.values():
        s = (r.get("_session") or "")[:8]
        if s:
            sessions[s] = sessions.get(s, 0) + 1
    return {s: n for s, n in sessions.items() if s != want}


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


def run(date_override=None, no_csv=False, dry_run=False, limit=None, boards=None,
        batch=None, sleep=None, merge=False, codes=None):
    """抓一次快照并落库。

    参数
    ----
    codes: 可迭代 | None
        只抓这些指定代码（按给定顺序，用于试点/排障）。给定后忽略 boards/limit。
    boards: str | 可迭代
        只抓这些板块（如 ``"北交所"``）。None = 全市场。
    batch / sleep: int / float
        每批代码数、批间间隔秒。补板块缺口时用 50 / 1.5（更礼貌，少量多次）。
    merge: bool
        **只补缺失**：已存在 (trade_date, code) 的行一律不动（INSERT OR IGNORE），
        且不写 a_shares_universe / stock_names，避免把别的表的合并结果覆盖掉。
        用于"补北交所缺口"这种增量场景；全量刷新请用默认 merge=False。

    无有效行情（停牌/退市/源不覆盖/当日零成交）不再当成 0：逐只写进
    ``data/snapshots/_fetch_attempt_<date>.json``，供 verify_fetch 区分原因。
    其中 ``no_trade``（源只给陈旧最后价、当日无成交）的行**不落库**。
    """
    trade_date = date_override or time.strftime("%Y-%m-%d")
    batch = int(batch or BATCH)
    sleep = float(PAGE_SLEEP if sleep is None else sleep)
    t0 = time.time()
    print("[tencent-snap] 开始 %s 源=腾讯qt.gtimg.cn%s%s"
          % (trade_date, " 板块=%s" % boards if boards else "",
             " 模式=只补缺失" if merge else ""), flush=True)

    # 全 A 候选 = 权威宇宙里的全部股票（含北交所；见 _candidate_codes）
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    _ensure_schema(con)
    all_codes = _candidate_codes(con)
    if codes:
        # 试点/排障：只抓指定代码，但仍校验它们是权威宇宙里的股票（防止拿错代码段）
        want = [c.strip() for c in codes]
        known = set(all_codes)
        unknown = [c for c in want if c not in known]
        if unknown:
            print("[tencent-snap][warn] %d 只不在权威股票宇宙，仍会尝试抓取：%s"
                  % (len(unknown), ",".join(unknown[:10])), flush=True)
        all_codes = want
    elif boards:
        all_codes = _codes_of_boards(con, all_codes, boards)
    if merge:
        have = {c for (c,) in con.execute(
            "SELECT code FROM stock_daily_snapshot WHERE trade_date=?", (trade_date,))}
        before = len(all_codes)
        all_codes = [c for c in all_codes if c not in have]
        print("[tencent-snap] 只补缺失：本日已有 %d 只，待补 %d 只（候选 %d）"
              % (len(have), len(all_codes), before), flush=True)
    if limit:
        all_codes = all_codes[:limit]
    if not all_codes:
        con.close()
        print("[tencent-snap] 无需抓取（候选为空）", flush=True)
        return {"ok": True, "trade_date": trade_date, "collected": 0, "requested": 0,
                "failed_batches": 0, "skipped": 0, "snap_rows": 0, "elapsed": 0.0}
    print("[tencent-snap] 候选 %d 只，分 %d 批（每批 %d，间隔 %.1fs）" %
          (len(all_codes), (len(all_codes) + batch - 1) // batch, batch, sleep), flush=True)

    parsed = {}
    failed = 0
    n_batch = (len(all_codes) + batch - 1) // batch
    for i in range(0, len(all_codes), batch):
        chunk = all_codes[i:i + batch]
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                got = _fetch_batch(chunk)
                parsed.update(got)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.8 * attempt)
        else:
            failed += 1
            print("  [warn] 第 %d/%d 批连续失败：%s"
                  % (i // batch + 1, n_batch, last_err), flush=True)
        if (i // batch) % 5 == 4 or i // batch + 1 == n_batch:
            print("  ... %d/%d 批，已解析 %d 只" % (i // batch + 1, n_batch, len(parsed)),
                  flush=True)
        time.sleep(sleep)

    # ── 会话日期校验：实时接口只给"最新一场"，拿它去填历史日 = 伪造 ────────────
    mismatch = _session_mismatch(parsed, trade_date)
    if mismatch:
        print("[tencent-snap][refuse] 源行情日期与目标交易日不符，拒绝落库："
              "目标=%s，源返回=%s。腾讯 qt.gtimg.cn 是实时接口、无日期参数，"
              "只能取到最新一场；历史某日只能用当日留存的库内数据回看，"
              "不可以用本接口回填（会把最新一场的价格写进历史日期）。"
              % (trade_date.replace("-", ""),
                 ",".join("%s(%d只)" % kv for kv in sorted(mismatch.items()))), flush=True)
        con.close()
        return {"ok": False, "error": "source_session_mismatch",
                "trade_date": trade_date,
                "want_session": trade_date.replace("-", ""),
                "got_sessions": mismatch, "requested": len(all_codes),
                "inserted": 0, "missing": {}}

    # 逐只记账：谁没拿到、为什么（不把"没抓到"当成 0）
    #
    # 2026-11-01 修一个"不报错但造假数据"的坑：退市股（如 000004 国华退）腾讯**照常返回**
    # 一个 87 字段的字符串，但 volume/amount/turnover 全是 0、close == prev_close、
    # pct == 0 —— 那是它最后一天的陈旧报价。若就此落库，等于给一只已退市的票伪造了
    # 一根"今日 K 线"：不报错、不留痕、下游换手率/涨跌幅统计全部失真。
    # 判定规则：当日无成交（volume 与 amount 同时为 0/缺失）即视为「无当日行情」，
    # **不落库**，只记 ``no_trade``。正常交易的股票成交量不可能为 0，
    # 命中者只有停牌、退市整理期结束这两种情况。
    missing = {}
    stale = set()
    for code in all_codes:
        r = parsed.get(code)
        if r is None:
            missing[code] = "no_response"      # 源完全没回这只（代码段/停更/退市）
        elif not r.get("close"):
            missing[code] = "no_quote"         # 回了但无成交价（停牌等）
        elif not (r.get("volume") or 0) and not (r.get("amount") or 0):
            missing[code] = "no_trade"         # 回了陈旧最后价，当日无成交 -> 不落库
            stale.add(code)
    if stale:
        for code in stale:
            parsed.pop(code, None)
        print("[tencent-snap] 剔除当日无成交 %d 只（源只给陈旧最后价，落库会伪造当日行情）：%s"
              % (len(stale), ",".join(sorted(stale)[:10])), flush=True)
    if missing:
        by_reason = {}
        for c, why in missing.items():
            by_reason.setdefault(why, []).append(c)
        print("[tencent-snap] 未取得有效行情 %d 只：%s"
              % (len(missing), "；".join("%s=%d" % (k, len(v))
                                         for k, v in sorted(by_reason.items()))), flush=True)

    if dry_run:
        print("[tencent-snap][dry-run] 拟落库 %d 只（失败批 %d）" % (len(parsed), failed),
              flush=True)
        con.close()
        return {"ok": True, "dry_run": True, "collected": len(parsed),
                "requested": len(all_codes), "failed_batches": failed,
                "missing": missing}

    # 落库
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    snap_rows, uni_rows, name_rows = [], [], []
    for code, r in parsed.items():
        # 兜底：只落股票（候选已过滤，这里防"接口回了没请求的代码"这类意外）
        if U.stock_kind(code, r.get("name") or "") != U.KIND_STOCK:
            continue
        board, exchange = _board_of(code)
        snap_rows.append((trade_date, code, r.get("name"), board, exchange,
                          r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("prev_close"),
                          r.get("pct"), r.get("change"), r.get("volume"), r.get("amount"),
                          r.get("turnover"), None, r.get("pe_ttm"), r.get("pe_dynamic"), r.get("pb"),
                          r.get("total_mv"), r.get("float_mv"), r.get("total_shares"), r.get("float_shares"),
                          now))
        uni_rows.append((code, r.get("name"), board, exchange, "listed", trade_date, trade_date))
        name_rows.append((code, r.get("name"), board, "tencent_snapshot", now))
    # merge 模式用 INSERT OR IGNORE：绝不允许拿空值覆盖当日已有行
    snap_sql_verb = "INSERT OR IGNORE" if merge else "INSERT OR REPLACE"
    con.executemany(
        snap_sql_verb + " INTO stock_daily_snapshot("
        "trade_date,code,name,board,exchange,open,high,low,close,prev_close,pct,change,"
        "volume,amount,turnover,vol_ratio,pe_ttm,pe_dynamic,pb,total_mv,float_mv,"
        "total_shares,float_shares,updated_at) VALUES(" + ",".join(["?"] * 24) + ")", snap_rows)
    if merge:
        # 只补快照缺口时不动宇宙/名称表：这两张表的合并有别的脚本参与，
        # 顺手 UPDATE 会把它们的成果覆盖掉（merge 的语义就是"只新增快照行"）。
        pass
    else:
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

    # 取证文件：verify_fetch 靠它区分"缺行情"与"源不覆盖"
    # requested_codes 是必需的：只记 missing 的话，无法区分"请求了但源没回"与
    # "这次压根没请求它（历史缺口）"，而这两种情况的处理方式完全不同。
    rep = os.path.join(SNAP_DIR, "_fetch_attempt_%s.json" % trade_date)
    _write_attempt_report(rep, {
        "date": trade_date, "source": "tencent_qt", "boards": boards,
        "merge": bool(merge), "requested": len(all_codes), "parsed": len(parsed),
        "inserted": len(snap_rows), "failed_batches": failed,
        "requested_codes": list(all_codes),
        "missing": missing, "at": now,
    })

    print("[tencent-snap] 完成：落库 %d 只（失败批 %d）/ 候选 %d；DB宇宙 %d；当日快照 %d；用时 %.0fs"
          % (len(snap_rows), failed, len(all_codes), db_universe, snap_count,
             time.time() - t0), flush=True)
    return {"ok": True, "trade_date": trade_date, "collected": len(parsed),
            "requested": len(all_codes), "inserted": len(snap_rows),
            "failed_batches": failed, "missing": missing, "db_universe": db_universe,
            "snap_rows": snap_count, "report": rep,
            "elapsed": round(time.time() - t0, 1)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 只（调试）")
    ap.add_argument("--boards", default=None,
                    help="只抓这些板块，逗号分隔（如 北交所）。默认全市场")
    ap.add_argument("--batch", type=int, default=None, help="每批代码数（默认 %d）" % BATCH)
    ap.add_argument("--sleep", type=float, default=None, help="批间间隔秒")
    ap.add_argument("--merge", action="store_true",
                    help="只补当日缺失的代码（INSERT OR IGNORE，不覆盖已有行）")
    ap.add_argument("--codes", default=None,
                    help="只抓指定代码，逗号分隔（试点/排障用）")
    a = ap.parse_args()
    boards = [x.strip() for x in a.boards.split(",") if x.strip()] if a.boards else None
    codes = [x.strip() for x in a.codes.split(",") if x.strip()] if a.codes else None
    r = run(date_override=a.date, no_csv=a.no_csv, dry_run=a.dry_run, limit=a.limit,
            boards=boards, batch=a.batch, sleep=a.sleep, merge=a.merge, codes=codes)
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
