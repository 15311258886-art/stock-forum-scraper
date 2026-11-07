# -*- coding: utf-8 -*-
"""前向记账公共库 —— 2026-11-05

## 这支文件解决的问题

决策台（`pillar_item`）与三条已采纳判据都是**快照型判断**：它们读的是「当日舆情 /
资金流 / 当日形态」，无法用历史数据重建，因此只能靠**前向记账**验证 ——
每天把「它今天说了什么」写下来，到期用真实收盘价回看准不准。

三个脚本（`forward_snapshot.py` / `forward_score.py` / `forward_scorecard.py`）
共用同一套口径。口径一旦各写一份，两份迟早会漂（本项目已经吃过「两套库两条管线」
的亏，见 `viewer/server.py::_kbase_daily_maybe` 的注释）。所以这里只放**口径**，
不放业务判断：

  · 表结构（`forward_book` / `forward_result`）
  · 交易日历、K 线读取、涨停门槛
  · 全市场等权基准（读 `viewer/logs/ew_index.json`，缺当日就按同一公式补算并落盘）

判据本身**不在这里**：走 `tools/cond_engine.py` 的 `TOOLS` 与 `viewer/playbook.py`，
指标走 `viewer/indicators.py` 的 `compute_indicators`。本库绝不重写公式。

## 口径（固定，不要各自改）

  · 持有期：1/3/5/20/60/120 个交易日（`HOLDS`）
  · 成本：双边合计 0.25%，**从收益里扣**（`COST_PCT`）
  · 收益：进场日收盘 → 到期日收盘（`kline` 表，已后复权，`d` 为 YYYYMMDD 整数）
  · 进场时点：信号日**次日收盘**；次日收盘涨停则记 `skip_reason='limit_up'`，不参与打分
  · 基准：同一区间的全市场等权收益
  · 准：前向超额 = 个股收益 − 等权收益 − 成本 > 0

## 涨停门槛

与 `viewer/playbook.py::_limit_thr`、`tools/cond_engine.py::limit_thr`、
`viewer/toolbox.py::_limit_up_thr` 同口径（三处一致，本库是第四个引用点）：

  前三位 300/301/688/689 → 19.8%；首位 4/8 或前三位 920 → 29.8%；其余 9.8%

判定式沿用 `playbook._limit_up_first` 的写法：`ret1 >= 阈值 - 0.2`。

## A 股口径

只记**真 A 股**（`is_ashare`，与 `viewer/quant.py::_is_ashare` 同口径），
不记 ETF / 可转债 / B 股 —— 决策台本来就只出个股，混进 ETF 会让胜率失真。
注意 `tools/cond_engine.py` 的池子是「kline 里的全部代码」（含 ETF），
所以那边的条数天然比本账本多，这是**有意**差异，不是 bug。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
# 路径可用环境变量覆盖 —— 只用于自测（把库指向副本，避免污染真账本）。
# 生产不设这些变量即可。
APP_DB = Path(os.environ.get("FORWARD_APP_DB") or (ROOT / "viewer" / "app.db"))
KLINE_DB = Path(os.environ.get("FORWARD_KLINE_DB") or (ROOT / "data" / "kline_daily.db"))
EW_JSON = Path(os.environ.get("FORWARD_EW_JSON") or (ROOT / "viewer" / "logs" / "ew_index.json"))
DOCS = ROOT / "docs"

# ---- 固定口径 -------------------------------------------------------------
COST_PCT = 0.25                     # 双边合计，百分点；从收益里扣
HOLDS = (1, 3, 5, 20, 60, 120)      # 交易日
TRAIL_BARS = 320                    # 单票回看根数：够 251 日新高 + 各项指标预热
MIN_BARS_CRIT = 60                  # 判据至少要这么多根才认（次新股给不出 250 日新高）
COVERAGE_MIN = 0.90                 # 当日 K 线覆盖率下限（低于它按「数据未更新」处理）

# 决策台在 forward_book.src 里的前缀，便于和判据键区分
PILLAR_PREFIX = "pillar:"


# ============================================================ 基础设施
def setup_stdout() -> None:
    """把 stdout/stderr 切成 UTF-8（Windows 控制台默认 cp936，中文会炸）。

    ⚠️ 不要用 `io.TextIOWrapper(sys.stdout.buffer)` 包一层：那会把底层 buffer
    一起接管，解释器退出时先关掉 wrapper、再关 buffer → 之后任何 print
    都报 `ValueError: I/O operation on closed file`。`reconfigure()` 只改编码，
    不动 buffer 生命周期，是本项目踩坑后统一采用的做法。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:       # noqa: BLE001 —— 老环境/被重定向的流不支持 reconfigure
            pass


def connect_app(timeout: float = 180.0) -> sqlite3.Connection:
    """打开 viewer/app.db（决策台表所在库）。

    `timeout` 给到 180s：查看器服务端本身在写这个库（调度里的
    `indicators_daily` 一轮能写几十分钟，实测 2026-11-05 记第一次账时撞到
    `database is locked`）。读不受影响（WAL），写会等锁 —— 等不到就是真占着，
    由上层重试，不在这里无限等。
    """
    if not APP_DB.exists():
        raise SystemExit("找不到 %s" % APP_DB)
    con = sqlite3.connect(str(APP_DB), timeout=float(timeout))
    con.row_factory = sqlite3.Row
    return con


def connect_kline(retries: int = 6, delay: float = 5.0) -> sqlite3.Connection:
    """打开 data/kline_daily.db（日线库），带重试。

    不设 `mode=ro`：该库是 WAL 模式且常被日更进程并发写入。实测（2026-11-05 15:38，
    `tools/sched_job.py kline_daily` 正在补当天数据）：此时**任何**打开方式
    （plain / `mode=ro` / `mode=rw&nolock=1`）都报 `unable to open database file`，
    只有 `immutable=1` 能读 —— 但它会忽略 WAL，读到的是「昨天」的数据。
    拿旧数据冒充今天比打不开更坏，所以这里不用 immutable，而是**重试**：
    日更跑完（或让开 WAL）之后同一句就能读了。

    重试用尽仍打不开 → 抛 `sqlite3.OperationalError`，由调用方按「本次跳过」处理。
    """
    if not KLINE_DB.exists():
        raise SystemExit("找不到 %s" % KLINE_DB)
    last: Exception | None = None
    for attempt in range(max(1, int(retries))):
        con = sqlite3.connect(str(KLINE_DB), timeout=60)
        try:
            con.execute("SELECT 1 FROM kline LIMIT 1").fetchone()   # 真正触发 -wal/-shm 打开
            return con
        except sqlite3.OperationalError as e:
            last = e
            con.close()
            if attempt + 1 < retries:
                print("  [kline] 第 %d 次打开失败（%s），%.0fs 后重试"
                      % (attempt + 1, e, delay))
                time.sleep(delay)
    raise sqlite3.OperationalError(
        "打不开 data/kline_daily.db：%s（已重试 %d 次；多半是日更正在写库）"
        % (last, retries))


def ensure_schema(con: sqlite3.Connection) -> None:
    """建 forward_book / forward_result（幂等）。

    幂等靠 `CREATE TABLE IF NOT EXISTS` + 唯一索引，重复跑不报错、不产生重复行。
    """
    con.executescript("""
    CREATE TABLE IF NOT EXISTS forward_book (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      snap_date   INTEGER NOT NULL,          -- 信号日 YYYYMMDD
      src         TEXT    NOT NULL,          -- 来源：pillar:<主线> / 判据键
      code        TEXT    NOT NULL,          -- 6 位代码
      name        TEXT,
      entry_date  INTEGER,                   -- 进场日（信号日次日）YYYYMMDD
      entry_px    REAL,                      -- 进场价（进场日收盘）
      skip_reason TEXT,                      -- limit_up | no_bar
      signal_json TEXT,                      -- 当天"它说了什么"的原始快照
      created_at  TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_forward_book
      ON forward_book(snap_date, src, code);
    CREATE INDEX IF NOT EXISTS ix_forward_book_pending
      ON forward_book(entry_px, skip_reason);

    CREATE TABLE IF NOT EXISTS forward_result (
      book_id    INTEGER NOT NULL,
      hold       INTEGER NOT NULL,           -- 持有期（交易日）
      exit_date  INTEGER,                    -- 到期日 YYYYMMDD（未到期为 NULL）
      exit_px    REAL,
      ret_pct    REAL,                       -- 个股收益（已扣成本），百分点
      bench_pct  REAL,                       -- 同期全市场等权收益，百分点
      excess_pct REAL,                       -- ret_pct - bench_pct
      matured    INTEGER NOT NULL DEFAULT 0, -- 1=到期；0=还没走完 H 个交易日
      PRIMARY KEY (book_id, hold)
    );
    """)
    con.commit()


# ============================================================ 交易日历 / K 线
def trading_dates(con: sqlite3.Connection) -> list[int]:
    """全市场交易日轴（升序）。来自 kline 的 DISTINCT d，约 4600 个，一次查询。"""
    return [int(r[0]) for r in con.execute("SELECT DISTINCT d FROM kline ORDER BY d")]


def day_bar_count(con: sqlite3.Connection, d: int) -> int:
    """某交易日有多少只票有 bar —— 用来判「当天数据是否补完」。"""
    row = con.execute("SELECT COUNT(*) FROM kline WHERE d=?", (int(d),)).fetchone()
    return int(row[0] or 0)


def limit_thr(code: str) -> float:
    """涨停门槛（%）。与 playbook._limit_thr / cond_engine.limit_thr 同口径。"""
    c = str(code or "")
    if c[:3] in ("300", "301", "688", "689"):
        return 19.8
    if c[:1] in ("4", "8") or c[:3] == "920":
        return 29.8
    return 9.8


def is_limit_up(code: str, prev_close: float | None, close: float | None) -> bool:
    """当日是否「收盘涨停」。判定式照抄 `playbook._limit_up_first`：ret1 >= 阈值 - 0.2。"""
    if not prev_close or not close or prev_close <= 0 or close <= 0:
        return False
    ret1 = (float(close) / float(prev_close) - 1.0) * 100.0
    return ret1 >= limit_thr(code) - 0.2


def is_ashare(code: str) -> bool:
    """真 A 股（与 viewer/quant.py::_is_ashare 同口径，剔可转债/B 股/ETF）。"""
    c = str(code or "")
    return len(c) == 6 and c.startswith(("60", "00", "30", "68", "92", "43", "83", "87"))


def load_bars(con: sqlite3.Connection, code: str, end_date: int,
              limit: int = TRAIL_BARS) -> list[tuple]:
    """取某票 `d <= end_date` 的最后 limit 根 K 线，**升序**返回 (d,o,c,h,l,v)。"""
    rows = con.execute(
        "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
        (str(code), int(end_date), int(limit))).fetchall()
    rows.reverse()
    return rows


def last_two_bars(con: sqlite3.Connection, code: str,
                  end_date: int) -> list[tuple[int, float]]:
    """取某票 `d <= end_date` 的最后两根 (d, c)，升序。用于回填与涨停判定。"""
    rows = con.execute(
        "SELECT d,c FROM kline WHERE code=? AND d<=? ORDER BY d DESC LIMIT 2",
        (str(code), int(end_date))).fetchall()
    rows.reverse()
    return [(int(r[0]), float(r[1] or 0.0)) for r in rows]


def code_name_map(con_app: sqlite3.Connection) -> dict[str, str]:
    """code → 简称（`stock_names`）。拿不到就给空串，不编名字。"""
    try:
        return {str(r["code"]): (r["name"] or "")
                for r in con_app.execute("SELECT code, name FROM stock_names")}
    except Exception:       # noqa: BLE001 —— 表缺失不该阻断记账
        return {}


def now_str() -> str:
    """北京时间字符串（项目统一时间源在 timeutil.now_cst，缺省时退回本地时间）。"""
    try:
        from timeutil import now_cst_str        # 根目录的 timeutil.py
        return now_cst_str()
    except Exception:       # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_int() -> int:
    """今天的 YYYYMMDD 整数（北京时区）。"""
    try:
        from timeutil import today_cst
        return int(today_cst().replace("-", ""))
    except Exception:       # noqa: BLE001
        return int(datetime.now().strftime("%Y%m%d"))


# ============================================================ 等权基准
class EwIndex:
    """全市场等权指数（`viewer/logs/ew_index.json`）。

    缓存结构：{"dates": [YYYYMMDD...], "index": [净值...], "n": 天数}，
    净值起点 1.0，逐日累乘「当日全市场等权日收益」。

    当日收益的定义（与 `tools/cond_box_market_base.py::build_index` 一致）：
    对每只票取它**自己上一根** bar 算 `c_t / c_{t-1} - 1`，再对当天有收益的票取均值。
    用「票自己的上一根」而不是「上一个全局交易日」，是为了让停牌复牌那天的
    跳空收益记在复牌日，不受成分变动干扰。

    缓存只到上一个交易日是常态（`cond_box_market_base.py` 手动跑才更新），
    所以 `ensure()` 会在需要时按**完全相同**的公式补算缺的日期并落盘，
    保证账本用的基准和回测卡片用的基准是同一条线。
    """

    def __init__(self) -> None:
        self.dates: list[int] = []
        self.values: list[float] = []
        self._pos: dict[int, int] = {}
        self.dirty = False
        self.extended: list[int] = []

    # ---- 读写 ----
    def load(self) -> "EwIndex":
        if not EW_JSON.exists():
            raise SystemExit("缺 %s —— 先跑 tools/cond_box_market_base.py 建等权基准" % EW_JSON)
        d = json.loads(EW_JSON.read_text(encoding="utf-8"))
        self.dates = [int(x) for x in d.get("dates") or []]
        self.values = [float(x) for x in d.get("index") or []]
        if not self.dates or len(self.dates) != len(self.values):
            raise SystemExit("%s 结构异常（dates/index 长度不一致）" % EW_JSON)
        self._pos = {v: i for i, v in enumerate(self.dates)}
        return self

    def save(self) -> None:
        """原子落盘（先写临时文件再 replace），避免与读方撞成半个 JSON。"""
        if not self.dirty:
            return
        EW_JSON.parent.mkdir(parents=True, exist_ok=True)
        payload = {"dates": self.dates,
                   "index": [round(float(x), 8) for x in self.values],
                   "n": len(self.dates)}
        tmp = EW_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, EW_JSON)
        self.dirty = False

    # ---- 补算 ----
    def ensure(self, target_date: int, con_kline: sqlite3.Connection) -> int:
        """补齐到 target_date（含）。返回本次新增的天数。"""
        if not self.dates:
            self.load()
        if int(target_date) <= self.dates[-1]:
            return 0
        new_days = [int(r[0]) for r in con_kline.execute(
            "SELECT DISTINCT d FROM kline WHERE d>? AND d<=? ORDER BY d",
            (int(self.dates[-1]), int(target_date)))]
        added = 0
        for nd in new_days:
            mean_ret = self._market_mean_ret(con_kline, nd)
            if mean_ret is None:
                # 当天一只票都没有 → 不是交易日；不往指数里塞假 0
                continue
            self.dates.append(nd)
            self.values.append(self.values[-1] * (1.0 + mean_ret))
            self._pos[nd] = len(self.dates) - 1
            self.extended.append(nd)
            added += 1
        if added:
            self.dirty = True
        return added

    @staticmethod
    def _market_mean_ret(con_kline: sqlite3.Connection, d: int) -> float | None:
        """当天全市场等权日收益（小数）。无数据返回 None。"""
        rows = con_kline.execute(
            "SELECT k.c, (SELECT p.c FROM kline p WHERE p.code=k.code AND p.d<? "
            "             ORDER BY p.d DESC LIMIT 1) AS prev "
            "FROM kline k WHERE k.d=?", (int(d), int(d))).fetchall()
        if not rows:
            return None
        arr = np.array([(float(r[0] or 0.0), float(r[1] or 0.0)) for r in rows],
                       dtype=np.float64)
        c, prev = arr[:, 0], arr[:, 1]
        ok = (c > 0) & (prev > 0)
        if not ok.any():
            return None
        return float(np.mean(c[ok] / prev[ok] - 1.0))

    # ---- 取值 ----
    def value_on(self, day: int) -> float | None:
        i = self._pos.get(int(day))
        return None if i is None else float(self.values[i])

    def ret_pct(self, d0: int, d1: int) -> float | None:
        """d0 → d1 的等权收益（百分点）。任一端缺值返回 None（不猜）。"""
        v0 = self.value_on(d0)
        v1 = self.value_on(d1)
        if v0 is None or v1 is None or v0 <= 0:
            return None
        return (v1 / v0 - 1.0) * 100.0
