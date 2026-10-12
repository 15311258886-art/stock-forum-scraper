# -*- coding: utf-8 -*-
"""集合竞价快照（2026-10-01 王总拍板「方案 A」）——每交易日 9:25 定盘后扫全市场一次，落库成序列。

为什么做这件事（把上一轮实测的能力边界写死在这里，免得下次又猜）：
  · **免费源拿得到**的：竞价定盘价（= 当天开盘价）。腾讯 qt[5] 今开 / 新浪 Market_Center 的
    ``open`` / 日 K 的 ``open`` 三源一致，且**可回溯任意历史交易日**。
  · **免费源拿不到**的：9:15–9:25 的竞价过程（虚拟撮合价、委买委卖、撤单）——要 Level-2，
    年费几千，不划算（详见会话里的方案 A / B 对比，王总选了 A）。
  · **9:25 那一刻之后就再也拿不回的**：竞价成交量/额。9:26 再查，量里已经混进连续竞价了。
    所以**只有 9:25 那一瞬间的快照才有价值**——这就是本模块唯一要干的事。

数据口径（界面上原样写出来，不做模糊表述）：
  · ``pct``（竞价涨幅）= (open - prev_close) / prev_close × 100，prev_close 取入库时的昨收
  · ``volume`` / ``amount`` = 竞价期间的量额（股 / 元）；**历史回填日没有，一律 NULL**，
    界面显示「—」，绝不拿全日量冒充竞价量
  · ``ratio``（量比）= 竞价量 ÷ 前 5 个交易日的竞价均量；样本不足 5 日时为 None（不排序、不补零）

数据源：新浪 Market_Center（全市场清单 + 今开 + 昨收 + 量额，一次 100 只、翻页并发）；
日期校正用腾讯 qt 抽样 5 只读行情时间戳（字段 [30]，形如 20260930161454）——
**不能拿「执行当天」当数据日期**：盘后/休市日跑采集时行情里装的是上一交易日，
记错一天就是拿昨天的开盘价冒充今天。宁可采不到，也不能记错日。

落库：表 ``auction_snapshot``（UNIQUE(date, code) 幂等 UPSERT）+ meta 表 key ``auction_state``。
对外 API：``collect()`` / ``backfill()`` / ``summary()`` / ``maybe_snapshot()``（后台调度线程用）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

DDL = """
CREATE TABLE IF NOT EXISTS auction_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,                -- 交易日 YYYY-MM-DD（由行情时间戳校正，不取执行当天）
  code TEXT NOT NULL,                -- 6 位代码
  name TEXT,
  open REAL,                         -- 竞价定盘价（= 当日开盘价）
  prev_close REAL,                   -- 该日昨收
  pct REAL,                          -- 竞价涨幅 %
  volume REAL,                       -- 竞价成交量（股）；历史回填行为 NULL
  amount REAL,                       -- 竞价成交额（元）；历史回填行为 NULL
  ts TEXT,                           -- 行情时间戳
  src TEXT,                          -- sina_mc / kline_backfill
  collected_at TEXT,
  UNIQUE(date, code)
);
CREATE INDEX IF NOT EXISTS idx_auc_date ON auction_snapshot(date);
CREATE INDEX IF NOT EXISTS idx_auc_code ON auction_snapshot(code);
CREATE INDEX IF NOT EXISTS idx_auc_code_date ON auction_snapshot(code, date);
"""

_STATE_KEY = "auction_state"
_PAGE_SIZE = 100
_MAX_PAGES = 80                # 上限 8000 只，留足裕度
_WORKERS = 8
_RATIO_DAYS = 5                # 量比分母：前 5 个交易日的竞价均量
_INDEX_SYMBOL = "sh000001"     # 上证综指：指数不停牌，它的日 K 日期序列 = 交易日历
_AUTO_BACKFILL_DAYS = 4        # 自动补采往前扫几个交易日（够覆盖「服务起晚了」这种场景）
_AUTO_BACKFILL_LIMIT = 6000    # 自动补采要走全市场，半份样本会让那天看着像另一天
_INSERT_SQL = (
    "INSERT OR REPLACE INTO auction_snapshot"
    " (date, code, name, open, prev_close, pct, volume, amount, ts, src, collected_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    con = _conn()
    try:
        con.executescript(DDL)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- 取数
def _http(url: str, timeout: int = 20, referer: str = "https://finance.sina.com.cn") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _http_json(url: str, timeout: int = 20) -> list:
    try:
        raw = _http(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    if not raw or raw.strip() in ("null", "[]"):
        return []
    try:
        v = json.loads(raw)
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def _adjustable(name: str) -> bool:
    """是否「竞价涨幅可比的标的」。

    新股上市首日（N 前缀，例：N力勤 +206%）、除权除息日（XD/XR/DR 前缀）的开盘涨幅不是
    同一把尺——前者首日无昨收可比、后者开盘价已扣掉股息/股本。它们会霸榜、会污染涨跌家数，
    故采集阶段就不入库（不是事后打标的，省得历史回填也跟着算错）。
    """
    n = (name or "").strip().upper()
    if not n:
        return True
    return not n.startswith(("N", "XD", "XR", "DR"))


def market_list(skip_adjustable: bool = True) -> list[dict]:
    """全市场 A 股清单 + 清单快照里的 open/昨收/量/额（新浪 Market_Center，翻页并发）。"""
    url_tpl = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=%d&sort=symbol&asc=1&node=hs_a")

    def one(page: int) -> list:
        try:
            return _http_json(url_tpl % (page, _PAGE_SIZE))
        except Exception:  # noqa: BLE001 —— 单页失败不至于让整轮采集报废
            return []

    out: list = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for f in [ex.submit(one, p) for p in range(1, _MAX_PAGES + 1)]:
            try:
                out.extend(f.result())
            except Exception:  # noqa: BLE001
                continue

    rows = []
    skipped = 0
    for it in out:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        name = it.get("name") or ""
        if skip_adjustable and not _adjustable(name):
            skipped += 1        # 新股首日 / 除权除息：涨幅不可比，采集阶段就不入库
            continue
        try:
            o = float(it.get("open") or 0)
            pc = float(it.get("settlement") or 0)     # 昨收
        except (TypeError, ValueError):
            continue
        if o <= 0 or pc <= 0:                        # 停牌 / 未开盘 —— 不入库，免得算出假涨幅
            continue
        try:
            vol = float(it.get("volume") or 0)       # 股
            amt = float(it.get("amount") or 0)       # 元
        except (TypeError, ValueError):
            vol = amt = 0.0
        rows.append({"code": code, "name": name, "open": o, "prev_close": pc,
                     "volume": vol, "amount": amt, "ts": str(it.get("ticktime") or "")})
    return rows, skipped


def _sample_quote_date() -> str | None:
    """抽样 5 只读腾讯 qt 行情时间戳字段 [30] → 行情当前**实际**对应的交易日（YYYY-MM-DD）。

    只取众数、不做任何「今天应该是哪天」的推断：跑在盘后/休市日时它天然 = 上一交易日（≠今天）；
    交易日盘中 = 今天。少数停牌股滞后也带不偏（取众数）。
    """
    codes = ["sh600000", "sz000001", "sh600519", "sz300009", "sh601318"]
    try:
        txt = _http("https://qt.gtimg.cn/q=" + ",".join(codes), timeout=12, referer="https://gu.qq.com/")
    except Exception:  # noqa: BLE001
        return None
    days = []
    for line in txt.split(";"):
        if '="' not in line:
            continue
        a = line.split('"')[1].split("~") if '"' in line else []
        if len(a) > 30 and a[30].isdigit() and len(a[30]) >= 8:
            days.append(a[30][:8])
    if not days:
        return None
    top = max(set(days), key=days.count)
    return "%s-%s-%s" % (top[:4], top[4:6], top[6:8])


_LIVE_DATE_CACHE: dict = {"date": None, "ts": 0.0}
_LIVE_DATE_TTL = 20  # 秒：行情时间戳抽样的缓存 TTL，避免 maybe_snapshot 每 ~10s 都打一次腾讯


def live_quote_date(force: bool = False) -> str | None:
    """行情源「现在到底在报哪一天」——腾讯 qt 时间戳抽样的带缓存封装。

    交易日盘中/盘后 = 今天；休市日（周末/节假日）= 上一个交易日（≠今天）。
    用于「今天是否真开市」的二次确认，且**不依赖日K**——日K盘中不含今日（2026-10-09 实录），
    拿日K当开市判据会把真实交易日整段挡死。
    """
    now = time.time()
    c = _LIVE_DATE_CACHE
    if not force and c["date"] and now - c["ts"] < _LIVE_DATE_TTL:
        return c["date"]
    d = _sample_quote_date()
    c.update({"date": d, "ts": now})
    return d


def sample_trade_date() -> str | None:
    """数据真正对应的交易日（YYYY-MM-DD）：行情时间戳日期，落后于最近交易日时回落到最近交易日。"""
    sampled = _sample_quote_date()
    if not sampled:
        return None
    # 防错关键：若抽到的行情时间戳日期落后于「真实最近交易日」（长假后首日的 9:15–9:25 早期窗口
    # 里行情还装着上一交易日，或源尚未刷新），不要把它当数据日——返回真实最近交易日，
    # 避免把 10-08 数据错打成 09-30 戳、进而被去重锁死在节前。
    ltd = latest_trade_date()
    if ltd and sampled < ltd:
        return ltd
    return sampled


# ---------------------------------------------------------------- 交易日历（全项目唯一权威源）
# 为什么必须只有这一份：东财/新浪等分钟级接口在休市日照样返回 200 和数据，吐的是
# **最近一个交易日的完整曲线**。任何模块若自己另算「今天是交易日吗 / 最近交易日是哪天」，
# 就会出现「09-30 的曲线被写上 10-03 的戳」这种日期错配（2026-10-03 实录）。
# 因此：market_trade_days / latest_trade_date / is_trading_day 三函数是本项目的唯一入口，
# 别的模块一律 import，禁止自己再实现一遍（历史教训见 audit_semantics.py 的 C2 规则）。
_TD_CACHE: dict = {"days": None, "ts": 0.0, "asof": None, "last_try": 0.0}
_TD_TTL = 6 * 3600
_TD_RETRY = 30  # 缩短到 30s：日历落后真实今天时尽快重抓，不再死等 6h TTL 却吐节前日历

# ---------------------------------------------------------------- 2026 确定性交易日历（兜底 oracle）
# 长假后 Sina K线可能尚未刷新「最近交易日」，单纯依赖网络会在 10-08 这种日子被误判为休市，
# 导致 backfill 永远补不到节后首日。这里硬编码 2026 已知节假日（国庆 10/01–10/07 + 周末），
# 作为「不依赖网络」的兜底，让 10-08 等刚过去的交易日也能被正确识别为交易日。
# 注意：仅覆盖 2026；其它年份 / 更精细的调休以 Sina K线 oracle 为准（两者取较新者）。
_CN_HOLIDAYS_2026 = {
    # 国庆 2026-10-01 ~ 2026-10-07（A 股休市）
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}


def _cn_trade_days_2026(upto: str) -> list[str]:
    """2026 确定性交易日（周末 + 已知节假日排除），仅作 Sina K线未刷新时的兜底 oracle。

    返回 upto 之前 60 个自然日内的交易日列表（升序），足够覆盖回填窗口；不依赖网络。
    """
    try:
        y, m, d = (int(x) for x in upto.split("-"))
        end = datetime(y, m, d)
    except Exception:  # noqa: BLE001
        end = datetime.now()
    start = end - timedelta(days=60)
    out: list[str] = []
    cur = start
    step = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5 and cur.strftime("%Y-%m-%d") not in _CN_HOLIDAYS_2026:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += step
    return out


def market_trade_days(n: int = 12) -> list[str]:
    """上证综指日 K 日期序列 = 交易日历（指数不停牌）。进程内缓存 6h + 跨日强制失效。

    2026-10-08 实录根因之一：旧逻辑只按 6h TTL 失效，跨零点后若上一份日历是昨天抓的，
    仍会把「库里最新日(09-30)」当「今天」——全系统日期卡死。
    新逻辑：
      ① 正常快路径：未过期且这一份是「今天」抓的；
      ② 若缓存里最新交易日 < 真实今天（日历落后，如长假后首日上午源尚未刷新）→
         **直接重抓，绝不返回陈旧的节前日历**；源一旦补上今日数据，30s 内即可被系统看到
         （更稳的兜底见 latest_trade_date 的 2026 确定性日历）。
    取不到返回 []（未知），交调用方按未知处理，绝不擅自填日期。
    """
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    c = _TD_CACHE
    cached = c["days"]
    cached_max = max(cached) if cached else None
    behind = bool(cached_max and cached_max < today)
    if not behind:
        if cached is not None and now - c["ts"] < _TD_TTL and c["asof"] == today:
            return cached
    # behind（日历落后今天）→ 直接重抓，不返回陈旧日历；取不到则返回源当前真实最新（best-effort，不谎报）。
    days: list[str] = []
    try:
        rows = sina_kline_dates("sh000001", n) or []
        days = sorted({str(r.get("day") or r.get("date") or "")[:10] for r in rows})
        days = [d for d in days if d]
    except Exception:  # noqa: BLE001 —— 日历取不到不许阻断抓数，交由调用方按未知处理
        days = []
    c.update({"days": days, "ts": now, "asof": today, "last_try": now})
    return days


def latest_trade_date(today: str | None = None) -> str | None:
    """<= today（默认今天）的最近一个交易日；取不到返回 None（未知，绝不擅自阻断）。

    叠加 2026 确定性日历兜底：Sina K线未刷新（长假后）时，仍能正确识别 10-08 等刚过去的交易日，
    避免 backfill / 调度把「最近交易日」误判成节前最后一天。
    """
    t = today or time.strftime("%Y-%m-%d")
    sina = market_trade_days() or []
    sina_latest = max([d for d in sina if d <= t], default=None)
    det = _cn_trade_days_2026(t)
    det_latest = max([d for d in det if d <= t], default=None)
    cands = [x for x in (sina_latest, det_latest) if x]
    return max(cands) if cands else None


def is_trading_day(today: str | None = None):
    """今天是不是交易日；日历取不到返回 None（未知）。

    先问 Sina K线 oracle；Sina 未刷新时再问 2026 确定性日历兜底（覆盖 2026 已知节假日）。
    """
    t = today or time.strftime("%Y-%m-%d")
    sina = market_trade_days() or []
    if t in sina:
        return True
    det = _cn_trade_days_2026(t)
    if det and t in det:
        return True
    return False if (sina or det) else None


def sina_kline_dates(sym: str, n: int = 40) -> list[dict]:
    """新浪日 K：[{day, open, close}]（按日期升序）。历史回填只用 open / 前一交易日 close。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, n))
    arr = _http_json(url)
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        d = str(it.get("day") or "")[:10]
        try:
            o = float(it.get("open"))
            c = float(it.get("close"))
        except (TypeError, ValueError):
            continue
        if d and o > 0 and c > 0:
            out.append({"day": d, "open": o, "close": c})
    out.sort(key=lambda x: x["day"])
    return out


def sina_symbol(code: str) -> str:
    c = str(code or "").strip()
    if not c.isdigit():
        return ""
    if c.startswith(("92", "43", "88", "83", "87")):
        return "bj" + c
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("0", "2", "3")):
        return "sz" + c
    return "sh" + c


# ---------------------------------------------------------------- 状态（meta 表）
def _load_state(con) -> dict:
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (_STATE_KEY,)).fetchone()
    except Exception:  # noqa: BLE001
        return {}
    if row:
        try:
            return json.loads(row[0] or "{}") or {}
        except ValueError:
            return {}
    return {}


def _save_state(con, st: dict) -> None:
    try:
        raw = json.dumps(st, ensure_ascii=False)
        if con.execute("SELECT 1 FROM meta WHERE key=?", (_STATE_KEY,)).fetchone():
            con.execute("UPDATE meta SET value=? WHERE key=?", (raw, _STATE_KEY))
        else:
            con.execute("INSERT INTO meta(key, value) VALUES(?,?)", (_STATE_KEY, raw))
        con.commit()
    except Exception:  # noqa: BLE001 —— 状态写不进去不该影响已落的数据
        pass


def _record_error(con, msg: str) -> None:
    """采集 / 回填 / 调度失败：写 last_error / last_error_at 进 meta.auction_state，并落 viewer/logs/auction.log。

    旧实现失败只静默 return False，前端永远看不到「为什么今天没数据」。这里把最近一次错误显式沉淀下来，
    便于排障；写盘失败（权限 / 目录）也不影响主流程。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = str(msg)[:300]
    if con is not None:
        try:
            st = _load_state(con)
            st["last_error"] = msg
            st["last_error_at"] = ts
            _save_state(con, st)
        except Exception:  # noqa: BLE001
            pass
    try:
        log_dir = os.path.join(ROOT, "viewer", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "auction.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 采集 / 回填
def _in_auction_window(now: datetime | None = None) -> bool:
    """当前是否在「竞价已定盘、量额仍是竞价量」的窗口（9:15–9:35）。

    9:30 开盘之后量就滚进连续竞价了，再存就是假竞价量——这是本模块最容易踩的坑，写死在这里。
    """
    n = now or datetime.now()
    return 9 * 60 + 15 <= n.hour * 60 + n.minute <= 9 * 60 + 35


def collect(conn=None, date: str | None = None, force: bool = False) -> dict:
    """抓一次全市场竞价快照并落库。``date=None`` 时自动按行情时间戳校正（推荐）。"""
    ensure_table()
    own = conn is None
    con = conn if conn is not None else _conn()
    try:
        st = _load_state(con)
        d = date or sample_trade_date()
        if not d:
            return {"ok": False, "reason": "行情时间戳取不到（腾讯 qt 不可达），拒绝按执行当天日期落库"}
        if not force and st.get("last_ok_date") == d:
            return {"ok": True, "skipped": True, "date": d, "rows": st.get("last_rows", 0),
                    "reason": "该交易日已采集过，未重复写入"}

        rows, skipped = market_list()
        if len(rows) < 100:      # 全市场不可能少于 100 只，取这么少说明源挂了
            return {"ok": False, "date": d, "rows": len(rows),
                    "reason": "全市场清单过少，判定取数失败，未落库"}

        # 盘后跑采集时，接口里的 volume/amount 已经是**全日量**了，拿它当竞价量存就是造假。
        # 故：非竞价窗口一律只存价（open/prev_close），量额留空；已有真实竞价量的行也不冲掉。
        live = _in_auction_window()
        old: dict[str, tuple] = {}
        for r in con.execute("SELECT code, volume, amount FROM auction_snapshot WHERE date=?", (d,)).fetchall():
            old[r["code"]] = (r["volume"], r["amount"])

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute("DELETE FROM auction_snapshot WHERE date=?", (d,))   # 同一天重跑 = 覆盖成最新
        payload = []
        for r in rows:
            vol = r["volume"] if live else None
            amt = r["amount"] if live else None
            if not live and old.get(r["code"]) and old[r["code"]][0]:
                vol, amt = old[r["code"]]          # 保住 9:25 那次已经存下的真实竞价量
            payload.append((d, r["code"], r["name"], r["open"], r["prev_close"],
                            round((r["open"] - r["prev_close"]) / r["prev_close"] * 100, 3),
                            vol, amt, r["ts"], "sina_mc", now))
        con.executemany(_INSERT_SQL, payload)
        con.commit()
        st.update({"last_ok_date": d, "last_ok_at": now, "last_rows": len(rows),
                   "last_src": "sina_mc", "last_live": live})
        _save_state(con, st)
        return {"ok": True, "date": d, "rows": len(rows), "skipped_new_or_ex": skipped,
                "collected_at": now,
                "with_volume": sum(1 for _, _, _, _, _, _, v, _, _, _, _ in payload if v),
                "in_auction_window": live}
    finally:
        if own:
            con.close()


def backfill(days: int = 10, limit: int = 1200, conn=None, force: bool = False) -> dict:
    """历史回填：用日 K 的 open 补最近 N 个交易日的**竞价定盘价**。

    只补价、不补量（免费源没有历史竞价量，写 NULL 就是 NULL，不猜）。
    全市场 5000 只逐只翻日 K 太慢，默认只对成交额最活跃的 limit 只回填。
    """
    ensure_table()
    own = conn is None
    con = conn if conn is not None else _conn()
    try:
        row = con.execute("SELECT MAX(date) AS d FROM auction_snapshot").fetchone()
        anchor = row["d"] if row else None
        if not anchor:
            return {"ok": False, "reason": "库里还没有竞价快照，先跑一次 collect()"}
        # 已存在的日期集合（跳过已采过的日，避免重复写）
        existing = {r[0] for r in con.execute(
            "SELECT DISTINCT date FROM auction_snapshot").fetchall()}
        # 【核心修复】真实最近交易日（确定性日历 + Sina oracle，见 latest_trade_date）。
        # 旧逻辑 todo 只含 < anchor(=MAX date=9/30) 的日期，永远补不到 10-08；
        # 长假后必然卡在节前。这里把 real_end 推到最新交易日，并向前追平所有缺失日。
        real_end = latest_trade_date() or anchor
        # 候选日：Sina 上证综指日 K 序列（交易日历 oracle）叠加 2026 确定性日历兜底，
        # 让 Sina 未刷新时也能识别 10-08 这种刚过去的交易日。
        kline_days = [d["day"] for d in sorted(sina_kline_dates("sh000001", days + 6),
                                               key=lambda x: x["day"])]
        det_days = _cn_trade_days_2026(real_end)
        cand = sorted(set(kline_days) | {d for d in det_days if anchor < d <= real_end})
        # ① Forward-fill：(anchor, real_end] 内、库里还没有的交易日 → 必须追平（9/30 → 10/08）
        forward = [d for d in cand if anchor < d <= real_end and (force or d not in existing)]
        # ② Backward 补空洞：< anchor 的交易日里，样本不足 anchor 日 70% 的（历史缺口 / 口径偏小）
        target = con.execute("SELECT COUNT(*) AS n FROM auction_snapshot WHERE date=?",
                             (anchor,)).fetchone()["n"]
        backward = [d for d in cand if d < anchor
                    and con.execute("SELECT COUNT(*) AS n FROM auction_snapshot WHERE date=?",
                                    (d,)).fetchone()["n"] < max(target * 0.7, 1)]
        todo = forward + backward
        # 总量上限：forward（长假后刚缺失的最新交易日）必须全部保留，backward 只保留最近的若干天
        cap = max(days, len(forward) + 6)
        if len(todo) > cap:
            keep_back = max(0, cap - len(forward))
            todo = forward + backward[-keep_back:]
        if not todo:
            return {"ok": False, "reason": "该窗口内没有需要回填的日子"}

        codes = [r[0] for r in con.execute(
            "SELECT code FROM auction_snapshot WHERE date=? "
            "ORDER BY COALESCE(amount,0) DESC LIMIT ?", (anchor, limit)).fetchall()]

        plan: dict[str, list[dict]] = {}

        def one(code: str):
            sym = sina_symbol(code)
            return code, (sina_kline_dates(sym, len(todo) + 6) if sym else [])

        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            for code, ks in ex.map(one, codes):
                if ks:
                    plan[code] = ks

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        written = 0
        for code, ks in plan.items():
            by_day = {k["day"]: k for k in ks}
            ordered = sorted(ks, key=lambda x: x["day"])
            for d in todo:
                k = by_day.get(d)
                if not k:
                    continue
                earlier = [x for x in ordered if x["day"] < d]
                if not earlier:
                    continue
                prev_close = earlier[-1]["close"]
                if prev_close <= 0:
                    continue
                # 已有真实竞价量就别覆盖（回填只补价）
                have = con.execute(
                    "SELECT amount FROM auction_snapshot WHERE date=? AND code=?", (d, code)).fetchone()
                if have and (have["amount"] or 0) > 0:
                    continue
                pct = round((k["open"] - prev_close) / prev_close * 100, 3)
                con.execute(_INSERT_SQL, (d, code, None, k["open"], prev_close, pct,
                                          None, None, "", "kline_backfill", now))
                written += 1
        con.commit()
        return {"ok": True, "dates": todo, "codes": len(codes), "written": written, "anchor": anchor}
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 统计（给前端）
def _ratio_of(con, code: str, date: str) -> float | None:
    """量比 = 当日竞价量 ÷ 前 5 个交易日的竞价均量；凑不够 5 天就 None（不补零、不拿别的数凑）。"""
    rows = con.execute(
        "SELECT volume FROM auction_snapshot WHERE code=? AND date<? AND volume>0"
        " ORDER BY date DESC LIMIT ?", (code, date, _RATIO_DAYS)).fetchall()
    if len(rows) < _RATIO_DAYS:
        return None
    cur = con.execute("SELECT volume FROM auction_snapshot WHERE code=? AND date=?", (code, date)).fetchone()
    if cur is None or not cur["volume"]:
        return None
    avg = sum(r["volume"] for r in rows) / float(len(rows))
    if avg <= 0:
        return None
    return round(cur["volume"] / avg, 2)


def _note(date: str, real: bool, ratio_n: int) -> str:
    """口径自述——与项目其它判词同一条纪律：判词自带判据，不自称、不模糊。"""
    if not real:
        return ("本日系历史回填：竞价定盘价 = 当日日 K 开盘价（可回溯），**竞价量/额免费源无历史，显示「—」**；"
                "要带真实竞价量，得等 9:25 那一次自动采集。")
    base = ("竞价 = 9:25 集合竞价定盘价，涨幅按 (开盘价 − 昨收) ÷ 昨收 算；"
            "新股首日(N)与除权除息(XD/XR/DR)剔除，涨幅不可比。")
    if ratio_n:
        return base + "量比 = 竞价量 ÷ 前 5 个交易日均量（已有 %d 只可算）。" % ratio_n
    return base + "库里还凑不出 5 天的竞价量，量比榜暂不列（不拿别的数凑）。"


def summary(date: str | None = None, top: int = 10, conn=None) -> dict:
    """今日总览用的集合竞价统计。**永远返回 dict**（含 status + note），前端照渲染即可，不会抛错。"""
    ensure_table()
    own = conn is None
    con = conn if conn is not None else _conn()
    out = {"status": "empty", "date": None, "counts": {}, "top_pct": [], "top_amount": [],
           "top_ratio": [], "history": [], "note": "", "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if not con.execute("SELECT COUNT(*) AS n FROM auction_snapshot").fetchone()["n"]:
            out["note"] = ("还没有任何集合竞价快照。每个交易日 9:25 定盘后会自动采一次；"
                           "今天没采到就显示这一行——不会拿别的数凑一个出来。")
            return out

        # 展示哪一天：指定的日子 → 不晚于它的最近一个交易日；未指定 → 优先当天，其次最近交易日
        if date:
            r = con.execute("SELECT date FROM auction_snapshot WHERE date<=? ORDER BY date DESC LIMIT 1",
                            (date,)).fetchone()
            d = r["date"] if r else None
        else:
            today = time.strftime("%Y-%m-%d")
            r = con.execute("SELECT date FROM auction_snapshot WHERE date=?", (today,)).fetchone()
            if r:
                d = today
            else:
                r2 = con.execute("SELECT MAX(date) AS d FROM auction_snapshot").fetchone()
                d = r2["d"] if r2 else None
        if not d:
            out["note"] = "库里暂无可用交易日。"
            return out

        rows = [dict(x) for x in con.execute(
            "SELECT date, code, name, open, prev_close, pct, volume, amount FROM auction_snapshot"
            " WHERE date=? ORDER BY code", (d,)).fetchall()]
        if not rows:
            out["date"] = d
            out["note"] = "该交易日没有入库记录。"
            return out

        pcts = [r["pct"] for r in rows if r["pct"] is not None]
        up = sum(1 for p in pcts if p > 0.005)
        down = sum(1 for p in pcts if p < -0.005)
        limit_up = [r for r in rows
                    if (r["pct"] or 0) >= 9.5
                    and "ST" not in (r["name"] or "")
                    and not str(r["code"]).startswith(("30", "688"))]
        amounts = [r["amount"] for r in rows if r["amount"]]
        amount_sum = sum(amounts) if amounts else None
        date_rows = [dict(x) for x in con.execute(
            "SELECT code, name, pct, amount, src FROM auction_snapshot WHERE date=?", (d,)).fetchall()]
        srcs = {str(x["src"]) for x in date_rows}
        real = "kline_backfill" not in srcs
        ratios = [r for r in rows if _ratio_of(con, r["code"], d) is not None]

        def enrich(r):
            return {"code": r["code"], "name": r["name"], "open": r["open"], "pct": r["pct"],
                    "amount": r["amount"], "volume": r["volume"], "ratio": _ratio_of(con, r["code"], d)}

        hist_dates = [r[0] for r in con.execute(
            "SELECT date FROM auction_snapshot WHERE date<=? GROUP BY date ORDER BY date DESC LIMIT ?",
            (d, 10)).fetchall()][::-1]
        history = []
        for hd in hist_dates:
            rs = [dict(x) for x in con.execute(
                "SELECT code, name, pct, amount, src FROM auction_snapshot WHERE date=?", (hd,)).fetchall()]
            if not rs:
                continue
            u = sum(1 for x in rs if (x["pct"] or 0) > 0.005)
            dd = sum(1 for x in rs if (x["pct"] or 0) < -0.005)
            amts = [x["amount"] for x in rs if x["amount"]]
            s = {str(x["src"]) for x in rs}
            history.append({
                "date": hd, "total": len(rs), "up": u, "down": dd,
                "avg_pct": round(sum(x["pct"] for x in rs if x["pct"] is not None) / float(len(rs)), 2),
                "amount_yi": round(sum(amts) / 1e8, 2) if amts else None,
                "src": "+".join(sorted(s)),
                "real": "kline_backfill" not in s,
            })

        result_dates = [h for h in history if h["real"]]

        # 榜单由后端定「哪一栏、按什么排、数据够不够才列得出来」。
        # 前端不做兜底排序——否则会出现「标题写量比榜、内容其实是涨幅榜」这种名不副实的假榜，
        # 两栏排出来一模一样，看着像 bug 其实是口径骗人。
        boards = [{
            "key": "pct", "title": "竞价涨幅榜", "sub": "按竞价涨幅降序取前 %d（已剔除新股首日/除权除息）" % top,
            "rows": [enrich(r) for r in sorted(
                rows, key=lambda r: (r["pct"] if r["pct"] is not None else -999), reverse=True)[:top]],
        }]
        if _wl := [enrich(r) for r in sorted(
                ratios, key=lambda r: _ratio_of(con, r["code"], d) or 0, reverse=True)[:top]]:
            boards.append({"key": "ratio", "title": "量比榜",
                           "sub": "按量比降序（量比 = 竞价量 ÷ 前 5 个交易日均量），取前 %d" % top, "rows": _wl})
        elif _wa := [enrich(r) for r in sorted(
                [r for r in rows if r["amount"]], key=lambda r: r["amount"], reverse=True)[:top]]:
            boards.append({"key": "amount", "title": "竞价额最大",
                           "sub": "按竞价成交额降序，取前 %d" % top, "rows": _wa})
        if len(boards) == 1:      # 只列了涨幅榜：明说另一榜为什么没上，不让人以为它出现了
            boards[0]["sub"] += "；量比/额榜因为样本不够（量比要 5 天竞价量、额要 9:25 那次采集）暂不列"

        out.update({
            "status": "ok",
            "date": d,
            "is_live": d == time.strftime("%Y-%m-%d"),
            "counts": {
                "total": len(rows), "up": up, "down": down, "flat": len(rows) - up - down,
                "limit_intent": sum(1 for p in pcts if p >= 9.5),
                "limit_up": len(limit_up),
                "up_gt3": sum(1 for p in pcts if p >= 3),
                "down_lt_neg3": sum(1 for p in pcts if p <= -3),
                "avg_pct": round(sum(pcts) / float(len(pcts)), 2) if pcts else None,
                "amount_yi": round(amount_sum / 1e8, 2) if amount_sum else None,
            },
            "top_pct": [enrich(r) for r in
                        sorted(rows, key=lambda r: (r["pct"] if r["pct"] is not None else -999), reverse=True)[:top]],
            "top_amount": [enrich(r) for r in sorted([r for r in rows if r["amount"]],
                                                     key=lambda r: r["amount"], reverse=True)[:top]],
            "top_ratio": [enrich(r) for r in sorted(
                ratios, key=lambda r: _ratio_of(con, r["code"], d) or 0, reverse=True)[:top]],
            "ratio_ready": len(ratios),
            "boards": boards,
            "history": history,
            "history_real_days": len(result_dates),
            "note": _note(d, real, len(ratios)),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return out
    except Exception as e:  # noqa: BLE001 —— 今日总览整体不该因这一块崩掉
        out["status"] = "error"
        out["note"] = "竞价统计出错：%s" % str(e)[:160]
        return out
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 调度自动采集
def snapshot_needed(conn=None, date: str | None = None) -> bool:
    """指定日（默认今天）还没有任何竞价记录 → True。"""
    con = conn if conn is not None else _conn()
    try:
        d = date or time.strftime("%Y-%m-%d")
        return con.execute("SELECT 1 FROM auction_snapshot WHERE date=? LIMIT 1", (d,)).fetchone() is None
    finally:
        if conn is None:
            con.close()


_AUTO_BUSY = False
_AUTO_LOCK = threading.Lock()
_AUTO_LAST: dict = {}


def _auto_backfill_task() -> None:
    """补采一次全市场日 K（实测要 7 分钟），**必须**甩到后台线程——
    直接跑在调度线程里会把资金流/政策/盯盘这些定时任务一起堵住几分钟。
    自己开连接：调用方是调度线程，sqlite 连接不能跨线程用。

    注（2026-10-09）：maybe_snapshot 对「今天」已改用 _auto_today_task（collect 读 Market_Center）——
    因为日K盘中无今日，用 backfill 补「今天」必然写不进去。本函数保留供「历史日缺口」补价复用
    （手动 /api/auction?backfill=1 或未来接一个盘后历史补采），**不要**再拿它补「今天」。
    """
    global _AUTO_BUSY
    with _AUTO_LOCK:
        if _AUTO_BUSY:
            return
        _AUTO_BUSY = True
    try:
        _AUTO_LAST.clear()
        _AUTO_LAST.update(backfill(days=_AUTO_BACKFILL_DAYS, limit=_AUTO_BACKFILL_LIMIT) or {})
    except Exception as e:  # noqa: BLE001 —— 后台线程绝不能因补采崩掉
        _AUTO_LAST.clear()
        _AUTO_LAST.update({"ok": False, "reason": "自动补采异常：%s" % str(e)[:120]})
    finally:
        _AUTO_BUSY = False


# 2026-10-09 移除 `_is_trading_day`（原「Sina 日K-only」判据）：
# 它只用上证综指日K判断「今天开没开市」，但日K**盘中不含今日**（当日K线要收盘后才印出），
# 于是真实交易日的整个交易时段它恒返回 False，把 maybe_snapshot 的所有采集路径静默挡死——
# 正是「集合竞价面板停在上一交易日」的根因（2026-10-09 实录）。
# 现在「今天开没开市」一律走本项目唯一权威入口 is_trading_day / latest_trade_date
# （Sina K线 + 2026 确定性日历兜底），禁止再自造日K判据（审计规则见本文件顶部注释）。


_TODAY_BUSY = False
_TODAY_LOCK = threading.Lock()
_TODAY_LAST: dict = {}


def _auto_today_task(today: str) -> None:
    """盘中补存「今天」的竞价定盘价：后台线程跑 collect(force=True, date=today)。

    为什么不能走 backfill：backfill 对每只股票取新浪日K，而日K盘中不含今日（2026-10-09 实录），
    by_day.get(today)=None → 永远写不进今天。新浪 Market_Center 盘中即当日实时快照（今开/昨收），
    才是「今天」的正确生产路径。全市场 ~5500 只一轮要数秒，必须甩后台线程，
    别堵住调度线程里其它定时任务（同 _auto_backfill_task 的理由）。
    口径：collect 在 9:15–9:35 窗口之外只写价格（今开/昨收/据此算的竞价涨幅），量/额一律 NULL——
    「竞价总额」显示「—」是市场机制（盘中拿不到竞价量），不是缺数据。
    """
    global _TODAY_BUSY
    with _TODAY_LOCK:
        if _TODAY_BUSY:
            return
        _TODAY_BUSY = True
    try:
        _TODAY_LAST.clear()
        _TODAY_LAST.update(collect(force=True, date=today) or {})
    except Exception as e:  # noqa: BLE001 —— 后台线程绝不能因补存崩掉
        _TODAY_LAST.clear()
        _TODAY_LAST.update({"ok": False, "reason": "盘中补存异常：%s" % str(e)[:120]})
    finally:
        _TODAY_BUSY = False


def _today_collectable(today: str) -> bool:
    """今天到底能不能采（交易日闸）——抽成独立函数以便回归测试。

    ① 唯一权威入口 is_trading_day（Sina K线 + 2026 确定性日历兜底）**明确**判休市（False）→ 挡；
       为 None（未知）时不擅自阻断。★ 绝不能只用日K当日历：日K盘中不含今日（当日K线收盘后才印出），
       只信日K会把真实交易日的整个交易时段挡死（2026-10-09 实录）。
    ② 再用「行情源现在是否在报今天」（live_quote_date，腾讯时间戳）二次确认——休市日里行情装的
       是上一交易日的存量，拿它当今日竞价量写库是最害人的造假。行情源取不到（None）时不阻断。
    """
    if is_trading_day(today) is False:
        return False
    lq = live_quote_date()
    if lq is not None and lq != today:
        return False
    return True


_RECON_GUARD: dict = {"date": None, "ts": 0.0}


def maybe_snapshot(conn=None, now: datetime | None = None) -> bool:
    """后台调度线程每 ~10s 调一次：抓全市场竞价快照，别的时间一律不打扰行情源。

    ① 9:15–9:35 竞价窗口内，且当日还没采过 —— 这一次的量**就是**竞价量（9:30 之后量会滚进连续竞价，
       拿它存就是造假，所以这个窗口是唯一能拿到真竞价量的时机）。
    ② 9:36–14:59 当日一条记录都没有（典型：服务 9:35 后才起）—— 后台补存**竞价定盘价**
       （走 collect 读 Market_Center 的今开/昨收；backfill 逐只取日K、日K盘中无今日，对「今天」无效）。
    ③ 收盘后对账（>=15:00）：真实最近交易日(=今天)在库里缺失 → 补一次全市场价（每日最多一次）。
       只针对「今天」——绝不给过去的交易日盖今天的戳（避免日期错配造假）；force 透传 + 每日限一次，
       避免每 10s 轮询重复触发。

    所有路径都先过交易日校验：休市日（周末/节假日）行情里量是**上一个交易日的全日量**，
    这时候采下来当成竞价量写库，是最难发现也最害人的一种造假。宁可当天不采，不许录假数。
    采集 / 调度失败不再静默 return False，而是写 last_error / last_error_at 进 meta.auction_state
    并落 viewer/logs/auction.log。
    """
    try:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        # 交易日闸（2026-10-09 重写，详见 _today_collectable）：
        # ★ 旧实现用「上证综指日K里有没有今天」当开市判据，但日K盘中不含今日（当日K线收盘后才印出），
        #   于是真实交易日的整个交易时段闸恒为 False，把 maybe_snapshot 的所有采集路径静默挡死——
        #   正是「集合竞价面板整整一天停在上一交易日」的根因。现改走唯一权威入口 + 行情源日期二次确认。
        if not _today_collectable(today):
            return False
        # ① 9:15–9:35 竞价窗口：抓真竞价量
        if 9 * 60 + 15 <= now.hour * 60 + now.minute <= 9 * 60 + 35:
            if not snapshot_needed(conn, today):
                return False
            ok = bool(collect(conn=conn).get("ok"))
            if not ok:
                _record_error(conn, "竞价窗口采集 %s 失败" % today)
            return ok

        # ② 9:36–14:59：当天一条都没有 → 后台补存「今天」的定盘价。
        #    必须走 collect（读 Market_Center 今开/昨收），不能走 backfill：
        #    backfill 逐只取新浪日K，而日K盘中不含今日 → by_day.get(today)=None → 永远写不进今天（2026-10-09 实录）。
        if now.hour * 60 + now.minute < 15 * 60:
            if snapshot_needed(conn, today):
                threading.Thread(target=_auto_today_task, args=(today,), daemon=True).start()
                return True
            return False

        # ③ 收盘后对账（>=15:00）：真实最近交易日(=今天)若库里缺失，补一次全市场价（每日最多一次）
        td = latest_trade_date(today)
        if td == today and snapshot_needed(conn, today):
            g = _RECON_GUARD
            if g["date"] != today or time.time() - g["ts"] > 12 * 3600:
                g["date"] = today
                g["ts"] = time.time()
                try:
                    res = collect(conn=conn, force=True, date=today)
                    ok = bool(res.get("ok"))
                    if not ok:
                        _record_error(conn, "收盘后对账 %s 失败: %s" % (today, res.get("reason", "")))
                    return ok
                except Exception as e:  # noqa: BLE001
                    _record_error(conn, "收盘后对账 %s 异常: %s" % (today, str(e)[:160]))
                    return False
        return False
    except Exception as e:  # noqa: BLE001 —— 后台线程绝不能因采集崩掉
        try:
            _record_error(conn, "maybe_snapshot 异常: %s" % str(e)[:160])
        except Exception:
            pass
        return False


if __name__ == "__main__":
    import sys
    sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    ensure_table()
    if cmd == "collect":
        print(json.dumps(collect(), ensure_ascii=False))
    elif cmd == "backfill":
        print(json.dumps(backfill(days=int(sys.argv[2]) if len(sys.argv) > 2 else 10), ensure_ascii=False))
    elif cmd == "summary":
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
    elif cmd == "snapshot":     # 手动触发一次（9:25 窗口外也能测）
        print(json.dumps(collect(force=True), ensure_ascii=False))
